#!/usr/bin/env python3
"""
inat2dwc — Incremental personal iNaturalist backup as a Darwin Core Archive.

Creates and keeps up to date a local copy of YOUR iNaturalist observations,
exported as a Darwin Core Archive (DwC-A) with:

  * occurrence.csv   — Occurrence core, with all iNaturalist fields that map
                       to Darwin Core terms (fields without a clear DwC
                       equivalent are preserved in dynamicProperties as JSON).
  * identification.csv — Identification extension holding ALL identifications
                       made on your observations (yours and others', current,
                       withdrawn and disagreeing).
  * multimedia.csv   — Multimedia (Audubon Core / GBIF) extension with links to
                       every photo and sound of your observations, ready for a
                       later bulk-download stage.
  * meta.xml + eml.xml — standard DwC-A descriptors, plus an optional .zip.

Design notes
------------
* HTTP layer is `pyinaturalist`, which brings built-in iNaturalist-compliant
  rate limiting (60 req/min, 10,000/day), retries, parameter conversion and
  JWT authentication. HTTP caching is disabled (expire_after=0) so every run
  sees fresh data.
* Data is mirrored into a local SQLite database keyed by iNaturalist IDs.
  Initial run = full download; later runs only fetch what changed:
  - Observations:   GET /v1/observations?user_id=...&updated_since=<watermark>
  - Identifications: iNaturalist has no updated_since filter on
    /v1/identifications, but every identification save/delete *touches* the
    observation's updated_at (verified in the iNaturalist Rails source), so
    all identification changes are covered by re-fetching identifications
    (current=any) for the observations updated since the last run.
  - Deletions are detected by a periodic reconciliation pass that lists all
    observation IDs (only_id=1) and removes local records that no longer
    exist remotely (kept in an obs_deletions tombstone table).
* Provide an API JWT token (https://www.inaturalist.org/users/api_token,
  expires after 24h) to also retrieve the true (unobscured) coordinates of
  your own observations; without it the public, possibly obscured,
  coordinates are used.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sqlite3
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dateutil import parser as dateparser

from pyinaturalist import (
    get_controlled_terms,
    get_identifications,
    get_observations,
    get_places_by_id,
    get_taxa_by_id,
    get_user_by_id,
    get_users_autocomplete,
)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PER_PAGE = 200                 # API maximum for /v1/observations & /v1/identifications
ID_BATCH_SIZE = 100            # observation IDs per /v1/identifications call
TAXA_BATCH_SIZE = 30            # IDs per /v1/taxa/{id} call (endpoint caps at 30)
PLACE_BATCH_SIZE = 30           # IDs per /v1/places call
DEFAULT_OVERLAP_MINUTES = 60    # safety overlap applied to the updated_since watermark
DEFAULT_RECONCILE_DAYS = 7      # run the deletion-reconciliation pass at most this old
MAX_429_RETRIES = 5
WAIT_429_SECONDS = 60          # iNat throttle window is per minute

# Folder layout: code lives in the app folder, its data (SQLite mirror,
# DwC-A exports, downloaded media, GUI config) in the "data" folder beside it.
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR.parent / "data"

INAT_BASE = "https://www.inaturalist.org"
OBS_URI = INAT_BASE + "/observations/{}"
ID_URI = INAT_BASE + "/identifications/{}"
TAXON_URI = INAT_BASE + "/taxa/{}"
PHOTO_URI = INAT_BASE + "/photos/{}"

DWC = "http://rs.tdwg.org/dwc/terms/"
DCT = "http://purl.org/dc/terms/"
ACT = "http://rs.tdwg.org/ac/terms/"
ROWTYPE_OCCURRENCE = DWC + "Occurrence"
ROWTYPE_IDENTIFICATION = DWC + "Identification"
ROWTYPE_MULTIMEDIA = "http://rs.gbif.org/terms/1.0/Multimedia"

# English country name → ISO 3166-1 alpha-2 code. The iNaturalist places API
# exposes the country of an observation (admin_level 0) but no ISO code, so
# the code is derived from the place name.
COUNTRY_CODES = {
    "Afghanistan": "AF", "Åland Islands": "AX", "Albania": "AL",
    "Algeria": "DZ", "American Samoa": "AS", "Andorra": "AD",
    "Angola": "AO", "Anguilla": "AI", "Antarctica": "AQ",
    "Antigua and Barbuda": "AG", "Argentina": "AR", "Armenia": "AM",
    "Aruba": "AW", "Australia": "AU", "Austria": "AT",
    "Azerbaijan": "AZ", "Bahamas": "BS", "Bahrain": "BH",
    "Bangladesh": "BD", "Barbados": "BB", "Belarus": "BY",
    "Belgium": "BE", "Belize": "BZ", "Benin": "BJ",
    "Bermuda": "BM", "Bhutan": "BT", "Bolivia, Plurinational State of": "BO",
    "Bonaire, Sint Eustatius and Saba": "BQ", "Bosnia and Herzegovina": "BA", "Botswana": "BW",
    "Bouvet Island": "BV", "Brazil": "BR", "British Indian Ocean Territory": "IO",
    "Brunei Darussalam": "BN", "Bulgaria": "BG", "Burkina Faso": "BF",
    "Burundi": "BI", "Cabo Verde": "CV", "Cambodia": "KH",
    "Cameroon": "CM", "Canada": "CA", "Cayman Islands": "KY",
    "Central African Republic": "CF", "Chad": "TD", "Chile": "CL",
    "China": "CN", "Christmas Island": "CX", "Cocos (Keeling) Islands": "CC",
    "Colombia": "CO", "Comoros": "KM", "Congo": "CG",
    "Congo, Democratic Republic of the": "CD", "Cook Islands": "CK", "Costa Rica": "CR",
    "Côte d'Ivoire": "CI", "Croatia": "HR", "Cuba": "CU",
    "Curaçao": "CW", "Cyprus": "CY", "Czechia": "CZ",
    "Denmark": "DK", "Djibouti": "DJ", "Dominica": "DM",
    "Dominican Republic": "DO", "Ecuador": "EC", "Egypt": "EG",
    "El Salvador": "SV", "Equatorial Guinea": "GQ", "Eritrea": "ER",
    "Estonia": "EE", "Eswatini": "SZ", "Ethiopia": "ET",
    "Falkland Islands (Malvinas)": "FK", "Faroe Islands": "FO", "Fiji": "FJ",
    "Finland": "FI", "France": "FR", "French Guiana": "GF",
    "French Polynesia": "PF", "French Southern Territories": "TF", "Gabon": "GA",
    "Gambia": "GM", "Georgia": "GE", "Germany": "DE",
    "Ghana": "GH", "Gibraltar": "GI", "Greece": "GR",
    "Greenland": "GL", "Grenada": "GD", "Guadeloupe": "GP",
    "Guam": "GU", "Guatemala": "GT", "Guernsey": "GG",
    "Guinea": "GN", "Guinea-Bissau": "GW", "Guyana": "GY",
    "Haiti": "HT", "Heard Island and McDonald Islands": "HM", "Holy See": "VA",
    "Honduras": "HN", "Hong Kong": "HK", "Hungary": "HU",
    "Iceland": "IS", "India": "IN", "Indonesia": "ID",
    "Iran, Islamic Republic of": "IR", "Iraq": "IQ", "Ireland": "IE",
    "Isle of Man": "IM", "Israel": "IL", "Italy": "IT",
    "Jamaica": "JM", "Japan": "JP", "Jersey": "JE",
    "Jordan": "JO", "Kazakhstan": "KZ", "Kenya": "KE",
    "Kiribati": "KI", "Korea, Democratic People's Republic of": "KP", "Korea, Republic of": "KR",
    "Kuwait": "KW", "Kyrgyzstan": "KG", "Lao People's Democratic Republic": "LA",
    "Latvia": "LV", "Lebanon": "LB", "Lesotho": "LS",
    "Liberia": "LR", "Libya": "LY", "Liechtenstein": "LI",
    "Lithuania": "LT", "Luxembourg": "LU", "Macao": "MO",
    "Madagascar": "MG", "Malawi": "MW", "Malaysia": "MY",
    "Maldives": "MV", "Mali": "ML", "Malta": "MT",
    "Marshall Islands": "MH", "Martinique": "MQ", "Mauritania": "MR",
    "Mauritius": "MU", "Mayotte": "YT", "Mexico": "MX",
    "Micronesia, Federated States of": "FM", "Moldova, Republic of": "MD", "Monaco": "MC",
    "Mongolia": "MN", "Montenegro": "ME", "Montserrat": "MS",
    "Morocco": "MA", "Mozambique": "MZ", "Myanmar": "MM",
    "Namibia": "NA", "Nauru": "NR", "Nepal": "NP",
    "Netherlands, Kingdom of the": "NL", "New Caledonia": "NC", "New Zealand": "NZ",
    "Nicaragua": "NI", "Niger": "NE", "Nigeria": "NG",
    "Niue": "NU", "Norfolk Island": "NF", "North Macedonia": "MK",
    "Northern Mariana Islands": "MP", "Norway": "NO", "Oman": "OM",
    "Pakistan": "PK", "Palau": "PW", "Palestine, State of": "PS",
    "Panama": "PA", "Papua New Guinea": "PG", "Paraguay": "PY",
    "Peru": "PE", "Philippines": "PH", "Pitcairn": "PN",
    "Poland": "PL", "Portugal": "PT", "Puerto Rico": "PR",
    "Qatar": "QA", "Réunion": "RE", "Romania": "RO",
    "Russian Federation": "RU", "Rwanda": "RW", "Saint Barthélemy": "BL",
    "Saint Helena, Ascension and Tristan da Cunha": "SH", "Saint Kitts and Nevis": "KN", "Saint Lucia": "LC",
    "Saint Martin (French part)": "MF", "Saint Pierre and Miquelon": "PM", "Saint Vincent and the Grenadines": "VC",
    "Samoa": "WS", "San Marino": "SM", "Sao Tome and Principe": "ST",
    "Saudi Arabia": "SA", "Senegal": "SN", "Serbia": "RS",
    "Seychelles": "SC", "Sierra Leone": "SL", "Singapore": "SG",
    "Sint Maarten (Dutch part)": "SX", "Slovakia": "SK", "Slovenia": "SI",
    "Solomon Islands": "SB", "Somalia": "SO", "South Africa": "ZA",
    "South Georgia and the South Sandwich Islands": "GS", "South Sudan": "SS", "Spain": "ES",
    "Sri Lanka": "LK", "Sudan": "SD", "Suriname": "SR",
    "Svalbard and Jan Mayen": "SJ", "Sweden": "SE", "Switzerland": "CH",
    "Syrian Arab Republic": "SY", "Taiwan, Province of China": "TW", "Tajikistan": "TJ",
    "Tanzania, United Republic of": "TZ", "Thailand": "TH", "Timor-Leste": "TL",
    "Togo": "TG", "Tokelau": "TK", "Tonga": "TO",
    "Trinidad and Tobago": "TT", "Tunisia": "TN", "Türkiye": "TR",
    "Turkmenistan": "TM", "Turks and Caicos Islands": "TC", "Tuvalu": "TV",
    "Uganda": "UG", "Ukraine": "UA", "United Arab Emirates": "AE",
    "United Kingdom of Great Britain and Northern Ireland": "GB", "United States of America": "US", "United States Minor Outlying Islands": "UM",
    "Uruguay": "UY", "Uzbekistan": "UZ", "Vanuatu": "VU",
    "Venezuela, Bolivarian Republic of": "VE", "Viet Nam": "VN", "Virgin Islands (British)": "VG",
    "Virgin Islands (U.S.)": "VI", "Wallis and Futuna": "WF", "Western Sahara": "EH",
    "Yemen": "YE", "Zambia": "ZM", "Zimbabwe": "ZW",
}

# Short/variant spellings used by iNaturalist country place names.
COUNTRY_CODE_ALIASES = {
    "United States": "US", "United Kingdom": "GB", "Czech Republic": "CZ",
    "Ivory Coast": "CI", "Cape Verde": "CV", "East Timor": "TL",
    "Swaziland": "SZ", "South Korea": "KR", "North Korea": "KP",
    "Russia": "RU", "Vietnam": "VN", "Laos": "LA", "Iran": "IR",
    "Syria": "SY", "Bolivia": "BO", "Venezuela": "VE", "Tanzania": "TZ",
    "Moldova": "MD", "Taiwan": "TW", "Palestine": "PS", "Brunei": "BN",
    "Turkey": "TR", "Türkiye": "TR", "Macau": "MO", "Hong Kong": "HK",
    "Republic of the Congo": "CG", "Democratic Republic of the Congo": "CD",
    "Macedonia": "MK", "North Macedonia": "MK", "Myanmar (Burma)": "MM",
    "Netherlands": "NL", "The Gambia": "GM", "The Bahamas": "BS",
}

# iNaturalist iconic taxon names that correspond to an actual DwC kingdom;
# others (Aves, Insecta, Mammalia, ...) are left empty. Only used as a last
# resort when the cached taxon ancestry is unavailable.
KINGDOMS = {"Animalia", "Plantae", "Fungi", "Bacteria", "Chromista", "Protozoa", "Viruses"}


def country_code(name: str) -> str:
    """ISO 3166-1 alpha-2 code for an English country name, or ""."""
    if not name:
        return ""
    name = name.strip()
    return COUNTRY_CODES.get(name) or COUNTRY_CODE_ALIASES.get(name) or ""


_warned_countries: set[str] = set()

CC_LICENSE_URIS = {
    "cc0": "https://creativecommons.org/publicdomain/zero/1.0/",
    "cc-by": "https://creativecommons.org/licenses/by/4.0/",
    "cc-by-sa": "https://creativecommons.org/licenses/by-sa/4.0/",
    "cc-by-nc": "https://creativecommons.org/licenses/by-nc/4.0/",
    "cc-by-nd": "https://creativecommons.org/licenses/by-nd/4.0/",
    "cc-by-nc-sa": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
    "cc-by-nc-nd": "https://creativecommons.org/licenses/by-nc-nd/4.0/",
}

# Content of dwc:dynamicProperties in the occurrence core. DwC recommends a
# flat JSON key:value encoding for this term, so the iNaturalist observation
# fields (ofvs) are reduced to their name/value pairs and tags are kept as
# one key; all other payload details stay in the SQLite mirror only.

# (header, term) — column 0 is the archive id (core) / coreid (extensions).
OCCURRENCE_COLUMNS = [
    ("id", None),
    ("occurrenceID", DWC + "occurrenceID"),
    ("basisOfRecord", DWC + "basisOfRecord"),
    ("catalogNumber", DWC + "catalogNumber"),
    ("modified", DCT + "modified"),
    ("datasetID", DWC + "datasetID"),
    ("recordedBy", DWC + "recordedBy"),
    ("recordedByID", DWC + "recordedByID"),
    ("occurrenceStatus", DWC + "occurrenceStatus"),
    ("eventDate", DWC + "eventDate"),
    ("eventTime", DWC + "eventTime"),
    ("verbatimEventDate", DWC + "verbatimEventDate"),
    ("occurrenceRemarks", DWC + "occurrenceRemarks"),
    ("verbatimLocality", DWC + "verbatimLocality"),
    ("country", DWC + "country"),
    ("countryCode", DWC + "countryCode"),
    ("decimalLatitude", DWC + "decimalLatitude"),
    ("decimalLongitude", DWC + "decimalLongitude"),
    ("coordinateUncertaintyInMeters", DWC + "coordinateUncertaintyInMeters"),
    ("geodeticDatum", DWC + "geodeticDatum"),
    ("establishmentMeans", DWC + "establishmentMeans"),
    ("scientificName", DWC + "scientificName"),
    ("taxonID", DWC + "taxonID"),
    ("taxonRank", DWC + "taxonRank"),
    ("kingdom", DWC + "kingdom"),
    ("phylum", DWC + "phylum"),
    ("class", DWC + "class"),
    ("order", DWC + "order"),
    ("family", DWC + "family"),
    ("genus", DWC + "genus"),
    ("vernacularName", DWC + "vernacularName"),
    ("sex", DWC + "sex"),
    ("lifeStage", DWC + "lifeStage"),
    ("reproductiveCondition", DWC + "reproductiveCondition"),
    ("identificationVerificationStatus", DWC + "identificationVerificationStatus"),
    ("license", DCT + "license"),
    ("rightsHolder", DCT + "rightsHolder"),
    ("references", DCT + "references"),
    ("dynamicProperties", DWC + "dynamicProperties"),
]

IDENTIFICATION_COLUMNS = [
    ("coreid", None),
    ("identificationID", DWC + "identificationID"),
    ("dateIdentified", DWC + "dateIdentified"),
    ("identifiedBy", DWC + "identifiedBy"),
    ("identifiedByID", DWC + "identifiedByID"),
    ("scientificName", DWC + "scientificName"),
    ("taxonID", DWC + "taxonID"),
    ("taxonRank", DWC + "taxonRank"),
    ("kingdom", DWC + "kingdom"),
    ("phylum", DWC + "phylum"),
    ("class", DWC + "class"),
    ("order", DWC + "order"),
    ("family", DWC + "family"),
    ("genus", DWC + "genus"),
    ("vernacularName", DWC + "vernacularName"),
    ("identificationRemarks", DWC + "identificationRemarks"),
    ("identificationVerificationStatus", DWC + "identificationVerificationStatus"),
]

MEDIA_COLUMNS = [
    ("coreid", None),
    ("identifier", DCT + "identifier"),
    ("type", DCT + "type"),
    ("format", DCT + "format"),
    ("license", DCT + "license"),
    ("rightsHolder", DCT + "rightsHolder"),
    ("creator", DCT + "creator"),
    ("title", DCT + "title"),
    ("references", DCT + "references"),
    ("thumbnailAccessURI", ACT + "thumbnailAccessURI"),
]

log = logging.getLogger("inat2dwc")

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def to_json(obj) -> str:
    """JSON serialization that tolerates datetime objects inserted by pyinaturalist."""
    return json.dumps(obj, default=str, ensure_ascii=False, separators=(",", ":"))


def as_utc(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = dateparser.parse(value)
        except (ValueError, OverflowError):
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_utc(value) -> str:
    dt = as_utc(value)
    return dt.isoformat(timespec="seconds") if dt else ""


def user_display(user: dict) -> str:
    return (user.get("name") or user.get("login") or "").strip()


def resolve_user(user: dict, users: dict[int, dict]) -> dict:
    """Central user cache lookup: merge the (often minimal) payload user
    object with the cached full record from /v1/users; non-empty payload
    values win, the cache fills the gaps (name, ORCID)."""
    cached = users.get(user.get("id")) or {}
    merged = dict(cached)
    for key, value in user.items():
        if value not in (None, ""):
            merged[key] = value
    return merged


def user_orcid(user: dict) -> str:
    """ORCID URI, or the iNaturalist profile link when the user has no ORCID."""
    orcid = str(user.get("orcid") or "").strip()
    if orcid:
        return orcid if orcid.startswith("http") else "https://orcid.org/" + orcid
    if user.get("id"):
        return INAT_BASE + "/users/" + str(user["id"])
    return ""


def license_uri(code) -> str:
    return CC_LICENSE_URIS.get(str(code or "").lower(), "")


def kingdom_from_iconic(iconic_name) -> str:
    return iconic_name if iconic_name in KINGDOMS else ""


def obs_coordinates(observation: dict) -> tuple[float | str, float | str, bool]:
    """(latitude, longitude, has_true_coordinates) of an observation.

    pyinaturalist normalises the API payload: latitude/longitude become
    ``location = [lat, lon]`` plus a GeoJSON point; the true unobscured
    coordinates of one's own observations arrive as private_location /
    private_geojson (only with an API token) and win over the public ones.
    """
    for prefix, private in (("private_", True), ("", False)):
        loc = observation.get(prefix + "location")
        if isinstance(loc, str) and "," in loc:
            lat, _, lon = loc.partition(",")
            return float(lat), float(lon.strip()), private
        if isinstance(loc, (list, tuple)) and len(loc) == 2:
            return float(loc[0]), float(loc[1]), private
        coords = (observation.get(prefix + "geojson") or {}).get("coordinates")
        if isinstance(coords, (list, tuple)) and len(coords) == 2:
            return float(coords[1]), float(coords[0]), private  # GeoJSON: [lon, lat]
        lat = observation.get(prefix + "latitude")
        lon = observation.get(prefix + "longitude")
        if lat is not None and lon is not None:
            return float(lat), float(lon), private
    return "", "", False


def kingdom_from_taxon(taxon: dict, taxa: dict[int, dict]) -> str:
    """Kingdom of a taxon, resolved through the cached /v1/taxa records whose
    ``ancestors`` chain carries the kingdom; the iconic taxon name is only a
    last resort (it is often a class like Aves or Insecta, not a kingdom)."""
    if not taxon:
        return ""
    for candidate in (taxon, taxa.get(taxon.get("id")) or {}):
        if candidate.get("rank") == "kingdom":
            return candidate.get("name") or ""
        for ancestor in candidate.get("ancestors") or []:
            if ancestor.get("rank") == "kingdom":
                return ancestor.get("name") or ""
    for aid in taxon.get("ancestor_ids") or []:
        candidate = taxa.get(aid) or {}
        if candidate.get("rank") == "kingdom":
            return candidate.get("name") or ""
        for ancestor in candidate.get("ancestors") or []:
            if ancestor.get("rank") == "kingdom":
                return ancestor.get("name") or ""
    return kingdom_from_iconic(taxon.get("iconic_taxon_name"))


# Higher-taxonomy DwC terms filled from the cached /v1/taxa ancestor chain.
HIGHER_RANKS = ("phylum", "class", "order", "family", "genus")


def higher_rank_names(taxon: dict, taxa: dict[int, dict]) -> dict[str, str]:
    """rank -> name for the higher-taxonomy DwC terms, from the taxon itself
    and the cached /v1/taxa record's full ancestor chain (payload taxon
    objects only carry their own name and rank)."""
    names: dict[str, str] = {}
    record = taxa.get(taxon.get("id")) if taxon.get("id") else None
    candidates = [record or {}, *((record or {}).get("ancestors") or []), taxon]
    for candidate in candidates:
        rank = candidate.get("rank")
        if rank in HIGHER_RANKS and rank not in names and candidate.get("name"):
            names[rank] = candidate["name"]
    return names


def country_from_places(observation: dict, places: dict[int, dict]) -> tuple[str, str]:
    """(country, countryCode) of an observation: the contained iNaturalist
    place with admin_level 0. True (private) place ids win over public ones."""
    ids = observation.get("private_place_ids") or observation.get("place_ids") or []
    for pid in ids:
        place = places.get(pid) or {}
        if place.get("admin_level") == 0:
            name = place.get("display_name") or place.get("name") or ""
            code = country_code(name)
            if name and not code and name not in _warned_countries:
                _warned_countries.add(name)
                log.warning("No ISO code for country place '%s'; countryCode "
                            "left empty for its observations.", name)
            return name, code
    return "", ""


# --------------------------------------------------------------------------- #
# SQLite mirror
# --------------------------------------------------------------------------- #


class Store:
    """Local SQLite mirror of the iNaturalist data plus sync state (watermarks)."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS observations (
        id INTEGER PRIMARY KEY, json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS identifications (
        id INTEGER PRIMARY KEY,
        observation_id INTEGER NOT NULL,
        json TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_ident_obs ON identifications(observation_id);
    CREATE TABLE IF NOT EXISTS media (
        id TEXT PRIMARY KEY,
        observation_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        json TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_media_obs ON media(observation_id);
    CREATE TABLE IF NOT EXISTS media_files (
        media_id TEXT PRIMARY KEY,
        observation_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        filename TEXT NOT NULL,
        rel_path TEXT NOT NULL,
        url TEXT NOT NULL,
        size_bytes INTEGER,
        sha256 TEXT,
        downloaded_at TEXT NOT NULL,
        metadata TEXT);
    CREATE INDEX IF NOT EXISTS idx_media_files_obs ON media_files(observation_id);
    CREATE TABLE IF NOT EXISTS taxa (
        id INTEGER PRIMARY KEY, json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS places (
        id INTEGER PRIMARY KEY, json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY, json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS obs_deletions (
        id INTEGER PRIMARY KEY, deleted_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY, value TEXT);
    """

    IN_CHUNK = 500  # stay below SQLite's bound-variable limit

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(self.SCHEMA)
        self.conn.commit()

    # -- state ------------------------------------------------------------- #
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    # -- observations ------------------------------------------------------ #
    def upsert_observations(self, observations: list[dict]) -> None:
        self.conn.executemany(
            "INSERT INTO observations (id, json) VALUES (?, ?) "
            "ON CONFLICT(id) DO UPDATE SET json = excluded.json",
            [(o["id"], to_json(o)) for o in observations],
        )
        self.conn.commit()

    def observation_ids(self) -> list[int]:
        return [r[0] for r in self.conn.execute("SELECT id FROM observations ORDER BY id")]

    def iter_observations(self):
        for oid, raw in self.conn.execute("SELECT id, json FROM observations ORDER BY id"):
            yield oid, json.loads(raw)

    def delete_observations(self, ids: list[int], deleted_at: str) -> None:
        for chunk in chunks(ids, self.IN_CHUNK):
            marks = ",".join("?" * len(chunk))
            self.conn.executemany(
                "INSERT INTO obs_deletions (id, deleted_at) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET deleted_at = excluded.deleted_at",
                [(i, deleted_at) for i in chunk],
            )
            self.conn.execute(f"DELETE FROM observations WHERE id IN ({marks})", chunk)
            self.conn.execute(f"DELETE FROM identifications WHERE observation_id IN ({marks})", chunk)
            self.conn.execute(f"DELETE FROM media WHERE observation_id IN ({marks})", chunk)
        self.conn.commit()

    # -- identifications ---------------------------------------------------- #
    def replace_identifications(self, observation_ids: list[int], idents: list[dict]) -> None:
        """Make the local ID set identical to the API for these observations."""
        for chunk in chunks(observation_ids, self.IN_CHUNK):
            marks = ",".join("?" * len(chunk))
            self.conn.execute(
                f"DELETE FROM identifications WHERE observation_id IN ({marks})", chunk
            )
        if idents:
            self.conn.executemany(
                "INSERT INTO identifications (id, observation_id, json) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET observation_id = excluded.observation_id, "
                "json = excluded.json",
                [(i["id"], i["observation"]["id"], to_json(i)) for i in idents],
            )
        self.conn.commit()

    def iter_identifications(self):
        for iid, obsid, raw in self.conn.execute(
            "SELECT id, observation_id, json FROM identifications "
            "ORDER BY observation_id, id"
        ):
            yield iid, obsid, json.loads(raw)

    # -- media --------------------------------------------------------------- #
    def replace_media(self, observation_ids: list[int], rows: list[dict]) -> None:
        for chunk in chunks(observation_ids, self.IN_CHUNK):
            marks = ",".join("?" * len(chunk))
            self.conn.execute(f"DELETE FROM media WHERE observation_id IN ({marks})", chunk)
        if rows:
            self.conn.executemany(
                "INSERT INTO media (id, observation_id, kind, json) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET observation_id = excluded.observation_id, "
                "kind = excluded.kind, json = excluded.json",
                [(r["id"], r["observation_id"], r["kind"], to_json(r)) for r in rows],
            )
        self.conn.commit()

    def iter_media(self):
        for mid, obsid, kind, raw in self.conn.execute(
            "SELECT id, observation_id, kind, json FROM media ORDER BY observation_id, id"
        ):
            yield mid, obsid, kind, json.loads(raw)

    # -- downloaded media files ---------------------------------------------- #
    def upsert_media_files(self, rows: list[dict]) -> None:
        """Record (or refresh) the local download state of media files.

        Each row is a dict with the media_files columns; `metadata` holds a
        JSON snapshot of everything the API knows about the file (original
        upload filename, dimensions, license, URLs, ...), so the downloaded
        file can always be traced back to its iNaturalist record.
        """
        if rows:
            self.conn.executemany(
                "INSERT INTO media_files (media_id, observation_id, kind, filename, "
                "rel_path, url, size_bytes, sha256, downloaded_at, metadata) "
                "VALUES (:media_id, :observation_id, :kind, :filename, :rel_path, "
                ":url, :size_bytes, :sha256, :downloaded_at, :metadata) "
                "ON CONFLICT(media_id) DO UPDATE SET "
                "observation_id = excluded.observation_id, kind = excluded.kind, "
                "filename = excluded.filename, rel_path = excluded.rel_path, "
                "url = excluded.url, size_bytes = excluded.size_bytes, "
                "sha256 = excluded.sha256, downloaded_at = excluded.downloaded_at, "
                "metadata = excluded.metadata",
                rows,
            )
            self.conn.commit()

    def media_file_ids(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT media_id FROM media_files")}

    def iter_media_files(self):
        columns = ("media_id", "observation_id", "kind", "filename", "rel_path",
                   "url", "size_bytes", "sha256", "downloaded_at", "metadata")
        for row in self.conn.execute(
            f"SELECT {', '.join(columns)} FROM media_files "
            "ORDER BY observation_id, media_id"
        ):
            yield dict(zip(columns, row))

    # -- taxa ---------------------------------------------------------------- #
    def taxon_ids(self) -> set[int]:
        return {r[0] for r in self.conn.execute("SELECT id FROM taxa")}

    def upsert_taxa(self, taxa: list[dict]) -> None:
        if taxa:
            self.conn.executemany(
                "INSERT INTO taxa (id, json) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET json = excluded.json",
                [(t["id"], to_json(t)) for t in taxa],
            )
            self.conn.commit()

    def all_taxa(self) -> dict[int, dict]:
        return {tid: json.loads(raw) for tid, raw in self.conn.execute("SELECT id, json FROM taxa")}

    # -- places ---------------------------------------------------------------- #
    def place_ids(self) -> set[int]:
        return {r[0] for r in self.conn.execute("SELECT id FROM places")}

    def upsert_places(self, places: list[dict]) -> None:
        if places:
            self.conn.executemany(
                "INSERT INTO places (id, json) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET json = excluded.json",
                [(p["id"], to_json(p)) for p in places],
            )
            self.conn.commit()

    def all_places(self) -> dict[int, dict]:
        return {pid: json.loads(raw)
                for pid, raw in self.conn.execute("SELECT id, json FROM places")}

    # -- users ----------------------------------------------------------------- #
    def user_ids(self) -> set[int]:
        return {r[0] for r in self.conn.execute("SELECT id FROM users")}

    def upsert_users(self, users: list[dict]) -> None:
        if users:
            self.conn.executemany(
                "INSERT INTO users (id, json) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET json = excluded.json",
                [(u["id"], to_json(u)) for u in users if u.get("id")],
            )
            self.conn.commit()

    def all_users(self) -> dict[int, dict]:
        return {uid: json.loads(raw)
                for uid, raw in self.conn.execute("SELECT id, json FROM users")}

    def counts(self) -> dict[str, int]:
        c = {}
        for table in ("observations", "identifications", "media", "media_files",
                      "taxa", "obs_deletions"):
            c[table] = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return c


# --------------------------------------------------------------------------- #
# iNaturalist API client (pyinaturalist + auth + 429 handling)
# --------------------------------------------------------------------------- #


class Cancelled(Exception):
    """Raised when a run is cancelled through InatClient.cancel_event (used by the GUI)."""


class InatClient:
    def __init__(self, token: str | None = None, user_agent: str = "inat2dwc",
                 cancel_event=None):
        self.token = token
        self.user_agent = user_agent
        self.cancel_event = cancel_event  # threading.Event; set to abort between requests

    def check_cancelled(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise Cancelled("Cancelled by user")

    def call(self, fn, **params):
        """Run a pyinaturalist request function with auth, no HTTP cache and
        explicit back-off when iNaturalist answers 429 (Too Many Requests)."""
        self.check_cancelled()
        params.setdefault("expire_after", 0)  # 0 == requests-cache DO_NOT_CACHE
        params.setdefault("user_agent", self.user_agent)
        if self.token:
            params["access_token"] = self.token
        last_exc = None
        for attempt in range(1, MAX_429_RETRIES + 1):
            try:
                return fn(**params)
            except requests.exceptions.HTTPError as exc:
                response = getattr(exc, "response", None)
                status = getattr(response, "status_code", None)
                if status == 429 and attempt < MAX_429_RETRIES:
                    wait = WAIT_429_SECONDS * attempt
                    log.warning(
                        "Rate limited by iNaturalist (HTTP 429); waiting %ss before retry %d/%d",
                        wait, attempt, MAX_429_RETRIES,
                    )
                    for _ in range(wait):
                        self.check_cancelled()
                        time.sleep(1)
                    last_exc = exc
                    continue
                raise
        raise last_exc  # pragma: no cover

    def paged(self, fn, label: str = "", limit: int | None = None, **params) -> list[dict]:
        """Cursor-based pagination over a /v1 search endpoint.

        iNaturalist (Elasticsearch) refuses to serve result windows beyond the
        first 10,000 records (HTTP 403), so plain page numbers stop working
        halfway through large accounts. Instead we page with ``id_above``,
        which both /v1/observations and /v1/identifications support: results
        are ordered by id ascending and every next request asks for records
        with an id above the last one seen.
        """
        params = dict(params)
        params.setdefault("per_page", PER_PAGE)
        # id_above cursoring requires a stable id ordering:
        params["order_by"] = "id"
        params["order"] = "asc"
        results: list[dict] = []
        requests = 0
        while True:
            response = self.call(fn, **params)
            batch = response.get("results", []) or []
            results.extend(batch)
            requests += 1
            if label and requests % 10 == 0:
                log.info("  %s: %d fetched so far", label, len(results))
            if limit is not None and len(results) >= limit:
                del results[limit:]
                break
            if len(batch) < params["per_page"]:
                break
            cursor = batch[-1].get("id") if isinstance(batch[-1], dict) else None
            if cursor is None:
                raise ValueError("Cursor pagination failed: response record without id")
            params["id_above"] = cursor
            self.check_cancelled()
        if label:
            log.info("  %s: %d fetched", label, len(results))
        return results

    # -- high-level operations --------------------------------------------- #
    def fetch_observations(self, user_id: int, updated_since: str | None = None,
                           limit: int | None = None) -> list[dict]:
        params: dict = {"user_id": user_id}
        if updated_since:
            params["updated_since"] = updated_since
        return self.paged(
            get_observations, label="observations fetched", limit=limit, **params
        )

    def fetch_observation_ids(self, user_id: int) -> set[int]:
        results = self.paged(
            get_observations, label="observation ids listed", user_id=user_id, only_id=True
        )
        return {r["id"] for r in results if isinstance(r, dict) and "id" in r}

    def fetch_identifications(self, observation_ids: list[int]) -> list[dict]:
        idents: list[dict] = []
        for i, chunk in enumerate(chunks(observation_ids, ID_BATCH_SIZE), start=1):
            log.info(
                "  identifications: batch %d/%d (%d observations)",
                i, (len(observation_ids) + ID_BATCH_SIZE - 1) // ID_BATCH_SIZE, len(chunk),
            )
            # current=any includes withdrawn identifications; observation_id
            # accepts a comma-delimited list (verified in the API source).
            idents.extend(
                self.paged(
                    get_identifications,
                    label="identifications fetched",
                    observation_id=chunk,
                    current="any",
                )
            )
        return idents

    def resolve_user(self, username: str) -> tuple[int, str]:
        """Resolve a username (or numeric id) to the numeric iNaturalist user id."""
        if username.isdigit():
            return int(username), username
        response = self.call(get_users_autocomplete, q=username)
        for user in response.get("results", []):
            if user.get("login", "").lower() == username.lower():
                return user["id"], user.get("login")
        raise SystemExit(
            f"ERROR: iNaturalist user '{username}' not found. "
            "Check the spelling of --user."
        )

    def fetch_controlled_terms(self) -> dict[int, dict]:
        """Fetch /v1/controlled_terms and index them for annotation mapping."""
        try:
            response = self.call(get_controlled_terms)
        except requests.exceptions.RequestException as exc:
            log.warning("Could not fetch controlled terms (%s); "
                        "sex/lifeStage/reproductiveCondition will stay empty.", exc)
            return {}
        terms = {}
        for term in response.get("results", []):
            values = {v["id"]: v.get("label", "") for v in term.get("values", [])}
            terms[term["id"]] = {"label": term.get("label", ""), "values": values}
        return terms

    def fetch_taxa(self, missing: list[int]) -> list[dict]:
        taxa: list[dict] = []
        for chunk in chunks(missing, TAXA_BATCH_SIZE):
            response = self.call(get_taxa_by_id, taxon_id=chunk)
            taxa.extend(response.get("results", []) or [])
        return taxa

    def fetch_places(self, missing: list[int]) -> list[dict]:
        places: list[dict] = []
        for chunk in chunks(missing, PLACE_BATCH_SIZE):
            response = self.call(get_places_by_id, place_id=chunk)
            places.extend(response.get("results", []) or [])
        return places

    def fetch_users(self, missing: list[int]) -> list[dict]:
        """Fetch full user records (name, ORCID, ...) one by one; the
        endpoint has no batch form. Users that no longer exist are skipped."""
        users: list[dict] = []
        for uid in missing:
            try:
                users.append(self.call(get_user_by_id, user_id=uid))
            except requests.exceptions.RequestException as exc:
                log.warning("Could not fetch user %d: %s", uid, exc)
        return users


def ensure_taxa(client: InatClient, store: Store, taxon_ids: set[int]) -> None:
    """Make sure every referenced taxon (and its iconic taxon) is cached locally."""
    taxon_ids = set(taxon_ids)
    for _round in range(3):
        missing = sorted(taxon_ids - store.taxon_ids())
        if not missing:
            return
        log.info("  taxa cache: fetching %d taxa", len(missing))
        fetched = client.fetch_taxa(missing)
        store.upsert_taxa(fetched)
        # Follow iconic_taxon_id one level up so kingdom can be resolved.
        taxa = store.all_taxa()
        taxon_ids |= {
            t["iconic_taxon_id"]
            for t in (taxa.get(tid) for tid in missing)
            if t and t.get("iconic_taxon_id")
        }


def ensure_places(client: InatClient, store: Store, place_ids: set[int]) -> None:
    """Cache the places referenced by observations (needed for the country
    lookup; the iNaturalist places API has no updated_since, so we simply
    cache every referenced place once)."""
    missing = sorted(set(place_ids) - store.place_ids())
    if not missing:
        return
    log.info("  places cache: fetching %d places", len(missing))
    store.upsert_places(client.fetch_places(missing))


def ensure_users(client: InatClient, store: Store, user_ids: set[int]) -> None:
    """Cache the full records of the users appearing in identification
    payloads (those carry only the login)."""
    missing = sorted(set(user_ids) - store.user_ids())
    if not missing:
        return
    log.info("  users cache: fetching %d users", len(missing))
    store.upsert_users(client.fetch_users(missing))


# --------------------------------------------------------------------------- #
# iNaturalist payload -> Darwin Core mapping
# --------------------------------------------------------------------------- #

# iNaturalist photo URLs look like .../photos/<id>/medium.jpeg — the size is
# the file-name segment, other sizes are derived by replacing it.
PHOTO_SIZE_SUB = re.compile(r"/(?:square|thumb|small|medium|large|original)(\.[A-Za-z0-9]+)")


def photo_variants(photo: dict) -> dict[str, str]:
    """All known size variants for a photo; iNaturalist documents that other
    sizes can be derived by replacing the size qualifier in the URL."""
    variants: dict[str, str] = {}
    sizes = ("original", "large", "medium", "small", "thumb", "square")
    for size in sizes:
        key = size + "_url"
        if photo.get(key):
            variants[key] = photo[key]
    base = photo.get("url") or ""
    if base and PHOTO_SIZE_SUB.search(base):
        for size in sizes:
            key = size + "_url"
            if key not in variants:
                variants[key] = PHOTO_SIZE_SUB.sub(f"/{size}\\g<1>", base, count=1)
    return variants


def guess_image_format(url: str) -> str:
    ext = Path(url.split("?")[0]).suffix.lower()
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}.get(ext, "image/jpeg")


def extract_media(observation: dict) -> list[dict]:
    """Normalized media rows for one observation (photos + sounds)."""
    obs_id = observation["id"]
    rows = []
    photos = observation.get("photos") or observation.get("observation_photos") or []
    for photo in photos:
        variants = photo_variants(photo)
        best = variants.get("original_url") or photo.get("url") or ""
        rows.append({
            "id": f"photo-{photo.get('id')}",
            "observation_id": obs_id,
            "kind": "photo",
            "identifier": best,
            "type": "StillImage",
            "format": photo.get("content_type") or guess_image_format(best),
            "license": license_uri(photo.get("license_code")),
            "references": PHOTO_URI.format(photo.get("id", "")),
            "thumbnail": variants.get("square_url") or variants.get("thumb_url") or "",
            "raw": photo,
            "variants": variants,
        })
    for sound in observation.get("sounds") or []:
        url = sound.get("file_url") or sound.get("url") or ""
        rows.append({
            "id": f"sound-{sound.get('id')}",
            "observation_id": obs_id,
            "kind": "sound",
            "identifier": url,
            "type": "Sound",
            "format": sound.get("file_content_type") or "",
            "license": license_uri(sound.get("license_code")),
            "references": "",
            "thumbnail": "",
            "raw": sound,
            "variants": {"original_url": url} if url else {},
        })
    return rows


def annotation_values(observation: dict, terms: dict[int, dict]) -> dict[str, str]:
    """Map iNaturalist annotations to DwC sex / lifeStage / reproductiveCondition."""
    out = {"sex": "", "lifeStage": "", "reproductiveCondition": ""}
    for ann in observation.get("annotations") or []:
        term = terms.get(ann.get("controlled_attribute_id"))
        if not term:
            continue
        value = term["values"].get(ann.get("controlled_value_id"), "")
        target = {
            "Sex": "sex",
            "Life Stage": "lifeStage",
            "Plant Phenology": "reproductiveCondition",
        }.get(term["label"])
        if target:
            out[target] = value
    return out


def dynamic_properties(observation: dict) -> dict:
    """Curated content for dwc:dynamicProperties, following the DwC
    recommendation of a flat JSON key:value encoding: iNaturalist observation
    fields reduced to name -> value, plus the tags."""
    dynamic: dict = {}
    for ofv in observation.get("ofvs") or []:
        name = str(ofv.get("name") or ofv.get("name_ci") or "").strip()
        if name:
            dynamic[name] = ofv.get("value")
    if observation.get("tags"):
        dynamic["tags"] = observation["tags"]
    return dynamic


def occurrence_row(observation: dict, terms: dict[int, dict],
                   taxa: dict[int, dict], places: dict[int, dict],
                   users: dict[int, dict]) -> list[str]:
    obs = observation
    obs_id = obs["id"]
    user = resolve_user(obs.get("user") or {}, users)
    taxon = obs.get("taxon") or {}

    lat, lon, has_true_coords = obs_coordinates(obs)
    if has_true_coords:
        uncertainty = obs.get("positional_accuracy") or ""
    else:
        uncertainty = obs.get("public_positional_accuracy") or obs.get("positional_accuracy") or ""

    event_dt = as_utc(obs.get("time_observed_at"))
    event_date = iso_utc(obs.get("time_observed_at")) or (obs.get("observed_on") or "")[:10]
    # dwc:eventTime: time of day (ISO 8601-1:2019), in UTC like eventDate.
    event_time = f"{event_dt:%H:%M:%S}Z" if event_dt else ""

    establishment = ""
    if obs.get("captive"):
        establishment = "cultivated"
    elif taxon.get("introduced"):
        establishment = "introduced"
    elif taxon.get("native"):
        establishment = "native"
    elif taxon.get("endemic"):
        establishment = "endemic"

    annotations = annotation_values(obs, terms)
    country, country_iso = country_from_places(obs, places)
    ranks = higher_rank_names(taxon, taxa)
    dynamic = dynamic_properties(obs)

    values = {
        "id": obs_id,
        "occurrenceID": OBS_URI.format(obs_id),
        "basisOfRecord": "HumanObservation",
        "catalogNumber": obs_id,
        "modified": iso_utc(obs.get("updated_at")),
        "datasetID": INAT_BASE,
        "recordedBy": user_display(user),
        "recordedByID": user_orcid(user),
        "occurrenceStatus": "present",
        "eventDate": event_date,
        "eventTime": event_time,
        "verbatimEventDate": obs.get("observed_on_string") or "",
        "occurrenceRemarks": obs.get("description") or "",
        "verbatimLocality": obs.get("place_guess") or "",
        "country": country,
        "countryCode": country_iso,
        "decimalLatitude": lat,
        "decimalLongitude": lon,
        "coordinateUncertaintyInMeters": uncertainty,
        "geodeticDatum": "WGS84",
        "establishmentMeans": establishment,
        "scientificName": taxon.get("name") or "",
        "taxonID": TAXON_URI.format(taxon["id"]) if taxon.get("id") else "",
        "taxonRank": taxon.get("rank") or "",
        "kingdom": kingdom_from_taxon(taxon, taxa),
        "phylum": ranks.get("phylum", ""),
        "class": ranks.get("class", ""),
        "order": ranks.get("order", ""),
        "family": ranks.get("family", ""),
        "genus": ranks.get("genus", ""),
        "vernacularName": taxon.get("preferred_common_name") or "",
        "sex": annotations["sex"],
        "lifeStage": annotations["lifeStage"],
        "reproductiveCondition": annotations["reproductiveCondition"],
        "identificationVerificationStatus": obs.get("quality_grade") or "",
        "license": license_uri(obs.get("license_code")),
        "rightsHolder": user_display(user),
        "references": obs.get("uri") or OBS_URI.format(obs_id),
        "dynamicProperties": json.dumps(dynamic, default=str, ensure_ascii=False)
        if dynamic else "",
    }
    return [values[header] for header, _ in OCCURRENCE_COLUMNS]


def identification_row(ident: dict, taxa: dict[int, dict],
                       users: dict[int, dict]) -> list[str]:
    ident_taxon = ident.get("taxon") or {}
    taxon_id = ident_taxon.get("id") or ident.get("taxon_id")
    cached = taxa.get(taxon_id, {}) if taxon_id else {}
    merged = {**cached, **ident_taxon}  # payload values win, cache fills the gaps

    # Identification payloads carry only a minimal user object (id, login);
    # the central user cache supplies the full name and ORCID.
    user = resolve_user(ident.get("user") or {}, users)
    ranks = higher_rank_names(merged, taxa)

    current = bool(ident.get("current"))

    values = {
        "coreid": (ident.get("observation") or {}).get("id"),
        "identificationID": ID_URI.format(ident["id"]),
        "dateIdentified": iso_utc(ident.get("created_at")),
        "identifiedBy": user_display(user),
        "identifiedByID": user_orcid(user),
        "scientificName": merged.get("name") or "",
        "taxonID": TAXON_URI.format(taxon_id) if taxon_id else "",
        "taxonRank": merged.get("rank") or "",
        "kingdom": kingdom_from_taxon(merged, taxa),
        "phylum": ranks.get("phylum", ""),
        "class": ranks.get("class", ""),
        "order": ranks.get("order", ""),
        "family": ranks.get("family", ""),
        "genus": ranks.get("genus", ""),
        "vernacularName": merged.get("preferred_common_name") or "",
        "identificationRemarks": ident.get("body") or "",
        "identificationVerificationStatus": (ident.get("category") or "unplaced")
        if current else "withdrawn",
    }
    return [values[header] for header, _ in IDENTIFICATION_COLUMNS]


def media_row(row: dict, uploaders: dict[int, str]) -> list[str]:
    """dcterms:creator / rightsHolder are the user who uploaded the
    observation (assumed to be the creator of its media)."""
    obs_id = row["observation_id"]
    creator = uploaders.get(obs_id) or ""
    values = {
        "coreid": obs_id,
        "identifier": row["identifier"],
        "type": row["type"],
        "format": row["format"],
        "license": row["license"],
        "rightsHolder": creator,
        "creator": creator,
        "title": ("Photo" if row["kind"] == "photo" else "Sound recording")
                 + f" of observation {obs_id}",
        "references": row["references"],
        "thumbnailAccessURI": row["thumbnail"],
    }
    return [values[header] for header, _ in MEDIA_COLUMNS]


# --------------------------------------------------------------------------- #
# Darwin Core Archive export
# --------------------------------------------------------------------------- #


def _fields_xml(columns, start_index: int) -> str:
    lines = []
    for index, (header, term) in enumerate(columns[1:], start=start_index):
        lines.append(f'        <field index="{index}" term="{term}"/>')
    return "\n".join(lines)


def build_meta_xml() -> str:
    core_fields = _fields_xml(OCCURRENCE_COLUMNS, 1)
    id_fields = _fields_xml(IDENTIFICATION_COLUMNS, 1)
    media_fields = _fields_xml(MEDIA_COLUMNS, 1)
    common = 'encoding="UTF-8" linesTerminatedBy="\\n" fieldsTerminatedBy="," fieldsEnclosedBy="&quot;" ignoreHeaderLines="1"'
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<archive xmlns="http://rs.tdwg.org/dwc/text/" metadata="eml.xml">
  <core {common} rowType="{ROWTYPE_OCCURRENCE}">
    <files>
      <location>occurrence.csv</location>
    </files>
    <id index="0"/>
{core_fields}
  </core>
  <extension {common} rowType="{ROWTYPE_IDENTIFICATION}">
    <files>
      <location>identification.csv</location>
    </files>
    <coreid index="0"/>
{id_fields}
  </extension>
  <extension {common} rowType="{ROWTYPE_MULTIMEDIA}">
    <files>
      <location>multimedia.csv</location>
    </files>
    <coreid index="0"/>
{media_fields}
  </extension>
</archive>
"""


def build_eml_xml(username: str) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<eml:eml xmlns:eml="eml://ecoinformatics.org/eml-2.1.1"
         packageId="inat-personal-archive-{username}-{today}"
         system="https://www.inaturalist.org" scope="system">
  <dataset>
    <title>iNaturalist personal archive: {username}</title>
    <creator>
      <individualName>
        <surName>{username}</surName>
      </individualName>
    </creator>
    <pubDate>{today}</pubDate>
    <language>en</language>
    <abstract>
      <para>Personal backup of the iNaturalist observations, identifications and
media of user {username}, exported in the Darwin Core Archive format by
inat2dwc. All identifications ever made on these observations (current and
withdrawn) are included in the Identification extension.</para>
    </abstract>
  </dataset>
</eml:eml>
"""


def write_csv(path: Path, columns, rows) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow([header for header, _ in columns])
        writer.writerows(rows)


def export_dwca(store: Store, export_dir: Path, username: str,
                 zip_dir: Path | None) -> Path | None:
    """Export the Darwin Core Archive; if zip_dir is given, also write a
    timestamped .zip of it there (every export gets its own archive)."""
    export_dir.mkdir(parents=True, exist_ok=True)
    taxa = store.all_taxa()
    places = store.all_places()
    users = store.all_users()
    # One central user cache for every core/extension that names a person.
    obs_users = {oid: resolve_user(obs.get("user") or {}, users)
                 for oid, obs in store.iter_observations()}
    # The uploader of an observation is assumed to be the creator of its media.
    uploaders = {oid: user_display(user) for oid, user in obs_users.items()}
    # Identification payloads carry only logins; without cached user records
    # the full names and ORCIDs cannot be resolved.
    missing_users = sorted({
        (ident.get("user") or {}).get("id")
        for _, _, ident in store.iter_identifications()
        if (ident.get("user") or {}).get("id")
    } - set(users))
    if missing_users:
        log.warning("%d identifier(s) are not in the user cache yet; their "
                    "names/ORCIDs cannot be resolved. Run a sync to fetch them.",
                    len(missing_users))
    terms = {}
    terms_raw = store.get_meta("controlled_terms")
    if terms_raw:
        # JSON round-trips integer keys as strings; normalize both levels.
        terms = {
            int(attr_id): {
                "label": term.get("label", ""),
                "values": {
                    int(value_id): label
                    for value_id, label in term.get("values", {}).items()
                },
            }
            for attr_id, term in json.loads(terms_raw).items()
        }

    log.info("Exporting occurrence core ...")
    write_csv(
        export_dir / "occurrence.csv",
        OCCURRENCE_COLUMNS,
        (occurrence_row(obs, terms, taxa, places, users)
         for _, obs in store.iter_observations()),
    )
    log.info("Exporting identification extension ...")
    write_csv(
        export_dir / "identification.csv",
        IDENTIFICATION_COLUMNS,
        (identification_row(ident, taxa, users)
         for _, _, ident in store.iter_identifications()),
    )
    log.info("Exporting multimedia extension ...")
    write_csv(
        export_dir / "multimedia.csv",
        MEDIA_COLUMNS,
        (media_row(row, uploaders) for _, _, _, row in store.iter_media()),
    )

    (export_dir / "meta.xml").write_text(build_meta_xml(), encoding="utf-8")
    (export_dir / "eml.xml").write_text(build_eml_xml(username), encoding="utf-8")

    zip_path = None
    if zip_dir:
        zip_dir.mkdir(parents=True, exist_ok=True)
        # Fixed name: each export replaces the previous archive; the Zenodo
        # version history (and any local backups) keep the older states.
        zip_path = zip_dir / "inat_dwca.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in ("meta.xml", "eml.xml", "occurrence.csv",
                         "identification.csv", "multimedia.csv"):
                archive.write(export_dir / name, name)
        log.info("Darwin Core Archive written to %s", zip_path)
    return zip_path


# --------------------------------------------------------------------------- #
# Sync logic
# --------------------------------------------------------------------------- #


def sync(args, client: InatClient, store: Store) -> None:
    run_start = datetime.now(timezone.utc)
    user_id, login = client.resolve_user(args.user)
    log.info("Backing up iNaturalist data of %s (user id %d)", login, user_id)

    # Test mode (--limit): fetch at most N observations without treating it
    # as a real sync — the incremental watermark and reconcile state stay
    # untouched so the next normal run still fetches everything.
    limit = getattr(args, "limit", None)
    last_sync = store.get_meta("last_sync")
    if limit:
        log.warning("TEST MODE: downloading at most %d observations; "
                    "the sync watermark will not be updated.", limit)
    full_run = not limit and (args.full or last_sync is None)
    updated_since = None
    if not full_run and not limit:
        watermark = as_utc(last_sync) - timedelta(minutes=args.overlap_minutes)
        updated_since = watermark.isoformat(timespec="seconds")
        log.info("Incremental run: updated since %s", updated_since)

    # ---- observations ------------------------------------------------------ #
    log.info("Fetching observations ...")
    observations = client.fetch_observations(user_id, updated_since, limit=limit)
    if updated_since and not observations:
        log.info("No updated observations; nothing to do for observations.")
    updated_ids = [o["id"] for o in observations]
    if observations:
        store.upsert_observations(observations)
        log.info("Upserted %d observations", len(observations))

        # Media is fully derived from the observation payload.
        media_rows = []
        for obs in observations:
            media_rows.extend(extract_media(obs))
        store.replace_media(updated_ids, media_rows)
        log.info("Synchronized %d media records", len(media_rows))

    # ---- identifications --------------------------------------------------- #
    # Identifications of *all* our observations on a full run, otherwise only
    # for the observations touched since the last run (any identification
    # change bumps its observation's updated_at).
    if full_run:
        target_obs_ids = store.observation_ids()
        log.info("Full run: fetching identifications for %d observations ...", len(target_obs_ids))
    else:
        target_obs_ids = updated_ids
        if target_obs_ids:
            log.info("Fetching identifications for %d updated observations ...", len(target_obs_ids))
    idents: list[dict] = []
    if target_obs_ids:
        idents = client.fetch_identifications(target_obs_ids)
        store.replace_identifications(target_obs_ids, idents)
        log.info("Synchronized %d identifications", len(idents))

        taxon_ids = set()
        for ident in idents:
            taxon = ident.get("taxon") or {}
            if taxon.get("id") or ident.get("taxon_id"):
                taxon_ids.add(taxon.get("id") or ident.get("taxon_id"))
        for obs in observations:
            taxon = obs.get("taxon") or {}
            if taxon.get("id"):
                taxon_ids.add(taxon["id"])
        ensure_taxa(client, store, taxon_ids)

    # ---- places & users caches ----------------------------------------------- #
    place_ids: set[int] = set()
    user_ids: set[int] = set()
    for obs in observations:
        place_ids.update(obs.get("private_place_ids") or obs.get("place_ids") or [])
        if (obs.get("user") or {}).get("id"):
            user_ids.add(obs["user"]["id"])
    for ident in idents:
        if (ident.get("user") or {}).get("id"):
            user_ids.add(ident["user"]["id"])
    ensure_places(client, store, place_ids)
    ensure_users(client, store, user_ids)

    # ---- controlled terms (annotation labels) ------------------------------ #
    if full_run or not store.get_meta("controlled_terms"):
        log.info("Fetching controlled terms (annotation labels) ...")
        terms = client.fetch_controlled_terms()
        if terms:
            store.set_meta("controlled_terms", to_json(terms))

    # ---- deletion reconciliation ------------------------------------------- #
    if limit:
        pass  # test mode: never lists all ids and never touches reconcile state
    elif full_run:
        # A full run just rebuilt the mirror; no reconcile needed.
        store.set_meta("last_reconcile", run_start.isoformat(timespec="seconds"))
    elif not args.no_reconcile:
        last_reconcile = as_utc(store.get_meta("last_reconcile"))
        due = (
            args.reconcile
            or last_reconcile is None
            or (datetime.now(timezone.utc) - last_reconcile).days >= args.reconcile_days
        )
        if due:
            log.info("Reconciling deletions (listing all observation ids) ...")
            remote_ids = client.fetch_observation_ids(user_id)
            local_ids = set(store.observation_ids())
            deleted = sorted(local_ids - remote_ids)
            if deleted:
                store.delete_observations(deleted, run_start.isoformat(timespec="seconds"))
                log.warning("Detected %d deleted observations: %s", len(deleted), deleted[:50])
            else:
                log.info("No deletions detected.")
            store.set_meta("last_reconcile", run_start.isoformat(timespec="seconds"))

    # ---- export ------------------------------------------------------------ #
    zip_dir = args.zip_dir if (getattr(args, "zip_dir", None)
                               and str(args.zip_dir).strip()) else None
    export_dwca(store, args.export_dir, login, zip_dir)

    if not limit:
        store.set_meta("last_sync", run_start.isoformat(timespec="seconds"))
    store.set_meta("username", login)

    counts = store.counts()
    log.info(
        "Sync complete. Database now holds %d observations, %d identifications, "
        "%d media records (%d taxa cached, %d deletions recorded).",
        counts["observations"], counts["identifications"], counts["media"],
        counts["taxa"], counts["obs_deletions"],
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="inat2dwc",
        description="Incrementally back up your iNaturalist data as a Darwin Core Archive "
                    "(occurrence core + identification and multimedia extensions).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-u", "--user", required=True,
                        help="your iNaturalist username (or numeric user id)")
    parser.add_argument("-t", "--token", default=os.environ.get("INAT_API_TOKEN"),
                        help="iNaturalist API token (JWT); also via --token-file or the "
                             "INAT_API_TOKEN environment variable")
    parser.add_argument("--token-file", type=Path,
                        help="file containing the API token on its first line")
    parser.add_argument("--db", type=Path, default=DATA_DIR / "inat_backup.sqlite3",
                        help="path of the local SQLite mirror")
    parser.add_argument("--export-dir", type=Path, default=DATA_DIR / "dwca",
                        help="directory for the exported Darwin Core Archive files")
    parser.add_argument("--zip-dir", type=Path, default=DATA_DIR / "dwca" / "zip", metavar="DIR",
                        help="also write a timestamped zipped Darwin Core Archive into "
                             "this folder (pass an empty value with --zip-dir= to disable)")
    parser.add_argument("--full", action="store_true",
                        help="force a full download, ignoring the incremental watermark")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="test mode: download at most N observations and leave the "
                             "incremental watermark untouched")
    parser.add_argument("--reconcile", action="store_true",
                        help="run the deletion-reconciliation pass now, regardless of age")
    parser.add_argument("--no-reconcile", action="store_true",
                        help="never run the deletion-reconciliation pass this run")
    parser.add_argument("--overlap-minutes", type=int, default=DEFAULT_OVERLAP_MINUTES,
                        help="safety overlap (minutes) applied to the updated_since watermark")
    parser.add_argument("--reconcile-days", type=int, default=DEFAULT_RECONCILE_DAYS,
                        help="run deletion reconciliation when the last one is older than this")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # pyinaturalist logs every HTTP request; keep it at WARNING unless --verbose
    logging.getLogger("pyinaturalist").setLevel(
        logging.DEBUG if args.verbose else logging.WARNING
    )

    token = args.token
    if not token and args.token_file and args.token_file.exists():
        token = args.token_file.read_text(encoding="utf-8").splitlines()[0].strip()
    if token:
        log.info("Using API token (true coordinates of your own observations will be fetched).")
    else:
        log.warning(
            "No API token provided (-t/--token/--token-file/INAT_API_TOKEN). "
            "Observations with obscured/private coordinates will be exported with their "
            "public (generalized) coordinates only."
        )

    client = InatClient(token=token)
    store = Store(args.db)
    sync(args, client, store)


if __name__ == "__main__":
    main()

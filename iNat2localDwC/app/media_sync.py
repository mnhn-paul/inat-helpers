#!/usr/bin/env python3
"""
media_sync — download local copies of the media files (photos + sounds) of
your iNaturalist observations.

Works purely from the local SQLite mirror built by inat2dwc.py: the `media`
table already holds every photo/sound with all its size variants, so no
iNaturalist API calls (and no API token) are needed — only downloads from the
public static file servers.

Layout: files are stored under the media folder per observation,

    <media_dir>/<observation_id>/<media_id>.<ext>

e.g. `media/1234567/photo-9876543.jpeg`. The best available variant is
downloaded (`original`, falling back to `large`/`medium`), written to a
temporary `.part` file and moved into place, so an interrupted download can
never leave a truncated file behind. A file counts as "already downloaded"
when it exists and is non-empty; the next run then only fetches what is new.

A small pause between downloads keeps the load on the iNaturalist servers
polite. Runs can be cancelled at any time (see inat2dwc.Cancelled).

Files are saved exactly as iNaturalist serves them (iNaturalist itself strips
camera EXIF from all sizes, so there is nothing richer to fetch); the
download is instead documented in the SQLite mirror's `media_files` table:
local filename and path, source URL, size, sha256 checksum, download time and
a snapshot of everything the API knows about the file (original upload
filename, dimensions, license, photo/observation URLs, ...). Files that were
downloaded before this bookkeeping existed are picked up by a backfill pass
that hashes and records them on the next run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from inat2dwc import Cancelled, Store, obs_coordinates

log = logging.getLogger("inat2dwc")

DOWNLOAD_DELAY = 1.0  # seconds between file downloads (server politeness)
TIMEOUT = (15, 300)   # connect, read — large originals can take a while
RETRIES = 3
USER_AGENT = "iNatDownloader media_sync"
EXTENSION_DEFAULTS = {"photo": ".jpg", "sound": ".mp3"}
INAT_BASE = "https://www.inaturalist.org"


def _check(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise Cancelled("Cancelled by user")


def best_url(row: dict) -> str:
    """Best (largest) available URL for a media row, or "" if none."""
    variants = row.get("variants") or {}
    if row.get("kind") == "photo":
        for key in ("original_url", "large_url", "medium_url", "small_url"):
            if variants.get(key):
                return variants[key]
    return row.get("identifier") or ""


def extension_for(row: dict, url: str) -> str:
    """File extension for a media row, taken from its URL when possible."""
    ext = Path(url.split("?")[0]).suffix.lower()
    if ext and ext.startswith(".") and len(ext) <= 6:
        return ext
    return EXTENSION_DEFAULTS.get(row.get("kind"), "")


def local_path(media_dir: Path, row: dict) -> Path:
    """Target path of a media row: <media_dir>/<observation_id>/<id>.<ext>."""
    return (media_dir / str(row["observation_id"])
            / f"{row['id']}{extension_for(row, best_url(row))}")


def media_rows(store: Store) -> list[dict]:
    """All media rows of the mirror, oldest observation first."""
    return [row for _mid, _obsid, _kind, row in store.iter_media()]


def media_metadata(store: Store, row: dict) -> dict:
    """Snapshot of everything known about a media record, kept with the
    download state so a local file can always be traced back to the
    iNaturalist record (and, via `original_filename`, to the file that was
    originally uploaded). Complements the full API payloads already mirrored
    in the `media` and `observations` tables.
    """
    raw = row.get("raw") or {}
    numeric_id = str(row.get("id") or "").rsplit("-", 1)[-1]
    meta: dict = {
        "media_id": row.get("id"),
        "kind": row.get("kind"),
        "original_filename": raw.get("original_filename"),
        "original_dimensions": raw.get("original_dimensions"),
        "attribution": raw.get("attribution"),
        "license": row.get("license"),
        "format": row.get("format"),
        "urls": row.get("variants") or {},
        "observation_id": row.get("observation_id"),
        "observation_url": f"{INAT_BASE}/observations/{row.get('observation_id')}",
    }
    if row.get("kind") == "photo":
        meta["photo_page_url"] = f"{INAT_BASE}/photos/{numeric_id}"
    obs_row = store.conn.execute(
        "SELECT json FROM observations WHERE id = ?",
        (row.get("observation_id"),),
    ).fetchone()
    if obs_row:
        obs = json.loads(obs_row[0])
        meta["observed_on"] = obs.get("observed_on_string") or obs.get("observed_on")
        meta["time_observed_at"] = obs.get("time_observed_at")
        lat, lon, is_private = obs_coordinates(obs)
        if lat != "" and lon != "":
            meta["latitude"] = lat
            meta["longitude"] = lon
            meta["coordinates_are_private"] = is_private
        meta["positional_accuracy"] = obs.get("positional_accuracy")
        taxon = obs.get("taxon") or {}
        meta["taxon_id"] = taxon.get("id")
        meta["taxon_name"] = taxon.get("name")
        meta["taxon_rank"] = taxon.get("rank")
        meta["taxon_common_name"] = taxon.get("preferred_common_name")
    return {k: v for k, v in meta.items() if v not in (None, "", {}, [])}


def file_digest(path: Path) -> tuple[int, str]:
    """(size in bytes, sha256 hex digest) of a file on disk."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def file_record(store: Store, row: dict, media_dir: Path, url: str,
                path: Path) -> dict:
    """A media_files row describing one downloaded (or previously downloaded)
    file: its name and location, the URL it came from, a checksum and a
    snapshot of the known API metadata. The file itself is never modified."""
    size, sha256 = file_digest(path)
    return {
        "media_id": row["id"],
        "observation_id": row["observation_id"],
        "kind": row.get("kind"),
        "filename": path.name,
        "rel_path": path.relative_to(media_dir).as_posix(),
        "url": url,
        "size_bytes": size,
        "sha256": sha256,
        "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "metadata": json.dumps(media_metadata(store, row), ensure_ascii=False),
    }


def backfill_media_files(store: Store, media_dir: Path,
                         cancel_event=None, log_fn=None) -> int:
    """Record files that exist on disk but have no media_files row yet
    (e.g. downloaded before download records were kept). Returns how many
    rows were added. Files are only hashed once; afterwards they are known.
    """
    known = store.media_file_ids()
    rows = media_rows(store)
    to_record = []
    for row in rows:
        _check(cancel_event)
        if row["id"] in known:
            continue
        path = local_path(media_dir, row)
        if path.is_file() and path.stat().st_size > 0:
            to_record.append(file_record(store, row, media_dir, best_url(row), path))
    if to_record:
        store.upsert_media_files(to_record)
        if log_fn:
            log_fn(f"Media files: recorded {len(to_record)} previously "
                   "downloaded file(s) in the database (with checksums).")
    return len(to_record)


def find_missing(store: Store, media_dir: Path,
                 cancel_event=None, log_fn=None) -> list[dict]:
    """Media rows whose local file does not exist (yet or not completely).

    A file counts as present when it exists and is non-empty; the partial
    `.part` files of interrupted downloads are ignored.
    """
    missing: list[dict] = []
    rows = media_rows(store)
    for i, row in enumerate(rows, start=1):
        _check(cancel_event)
        if i % 500 == 0 and log_fn:
            log_fn(f"Media check: {i}/{len(rows)} media records examined")
        path = local_path(media_dir, row)
        if not path.is_file() or path.stat().st_size == 0:
            if best_url(row):
                missing.append(row)
            elif log_fn:
                log_fn(f"WARNING: {row['id']} (observation {row['observation_id']})"
                      " has no download URL — skipped")
    return missing


def download_media(rows: list[dict], media_dir: Path,
                   store: Store | None = None,
                   cancel_event=None, log_fn=None,
                   delay: float = DOWNLOAD_DELAY) -> tuple[int, int]:
    """Download the given media rows into `media_dir`, byte-for-byte as
    served (the files are never modified afterwards). When `store` is
    given, every successful download is recorded in its media_files table.

    Returns (downloaded, failed). Failures are logged and skipped so one
    broken URL does not stop the whole run.
    """
    headers = {"User-Agent": USER_AGENT}
    downloaded = failed = 0
    for i, row in enumerate(rows, start=1):
        _check(cancel_event)
        url = best_url(row)
        path = local_path(media_dir, row)
        path.parent.mkdir(parents=True, exist_ok=True)
        if log_fn and (i % 10 == 0 or i == len(rows)):
            log_fn(f"Media download: {i}/{len(rows)} — {downloaded} ok, "
                   f"{failed} failed")
        last_error = None
        for attempt in range(1, RETRIES + 1):
            _check(cancel_event)
            try:
                with requests.get(url, headers=headers, timeout=TIMEOUT,
                                  stream=True) as resp:
                    resp.raise_for_status()
                    part = path.with_suffix(path.suffix + ".part")
                    with open(part, "wb") as fh:
                        for chunk in resp.iter_content(chunk_size=1 << 16):
                            fh.write(chunk)
                if part.stat().st_size == 0:
                    raise IOError("empty response body")
                part.replace(path)
                last_error = None
                break
            except requests.exceptions.RequestException as exc:
                last_error = exc
                if attempt < RETRIES:
                    time.sleep(delay * attempt)
        if last_error is None:
            downloaded += 1
            if store is not None:
                try:
                    store.upsert_media_files(
                        [file_record(store, row, media_dir, url, path)])
                except Exception as exc:  # noqa: BLE001 - recording is best effort
                    if log_fn:
                        log_fn(f"WARNING: could not record {row['id']} in the "
                               f"database: {exc}")
        else:
            failed += 1
            if log_fn:
                log_fn(f"WARNING: could not download {row['id']} from {url}: "
                       f"{last_error}")
        if i < len(rows):
            time.sleep(delay)
    return downloaded, failed


def sync_media(store: Store, media_dir: Path, confirm=None,
               cancel_event=None, log_fn=None,
               delay: float = DOWNLOAD_DELAY) -> tuple[int, int]:
    """Detect missing files, ask via `confirm(missing) -> bool` whether to
    download them, and do so. Returns (downloaded, failed)."""
    rows = media_rows(store)
    if log_fn:
        log_fn(f"Media sync: checking {len(rows)} media records against "
               f"{media_dir} ...")
    missing = find_missing(store, media_dir, cancel_event=cancel_event,
                           log_fn=log_fn)
    # Record files that predate download bookkeeping (local hashing only).
    backfill_media_files(store, media_dir, cancel_event=cancel_event,
                        log_fn=log_fn)
    if not missing:
        if log_fn:
            log_fn(f"Media sync: all {len(rows)} media files are already "
                   f"present in {media_dir}.")
        return 0, 0
    photos = sum(1 for r in missing if r.get("kind") == "photo")
    sounds = sum(1 for r in missing if r.get("kind") == "sound")
    if log_fn:
        log_fn(f"Media sync: {len(missing)} of {len(rows)} files are missing "
               f"({photos} photos, {sounds} sounds).")
    if confirm is not None and not confirm(missing):
        if log_fn:
            log_fn("Media sync: download declined — nothing downloaded.")
        return 0, 0
    return download_media(missing, media_dir, store=store,
                          cancel_event=cancel_event, log_fn=log_fn, delay=delay)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        prog="media_sync",
        description="Download local copies of the media files (photos and "
                    "sounds) recorded in the local iNaturalist SQLite mirror.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--db", type=Path, required=True,
                        help="path of the local SQLite mirror")
    parser.add_argument("--media-dir", type=Path, default=None,
                        help="target folder for the downloaded files "
                             "(default: <db folder>/media)")
    parser.add_argument("--yes", action="store_true",
                        help="download without asking for confirmation")
    parser.add_argument("--delay", type=float, default=DOWNLOAD_DELAY,
                        help="pause between downloads, in seconds")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    if not args.db.is_file():
        raise SystemExit(f"ERROR: no database at {args.db} — run inat2dwc first.")
    media_dir = args.media_dir or args.db.parent / "media"
    store = Store(args.db)
    confirm = None if args.yes else lambda missing: input(
        f"Download {len(missing)} missing media files into {media_dir}? [y/N] "
    ).strip().lower() in ("y", "yes")
    downloaded, failed = sync_media(store, media_dir, confirm=confirm,
                                    log_fn=lambda m: log.info("%s", m),
                                    delay=args.delay)
    log.info("Media sync done: %d downloaded, %d failed.", downloaded, failed)


if __name__ == "__main__":
    main()

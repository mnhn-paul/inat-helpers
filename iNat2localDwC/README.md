# iNatDownloader — personal iNaturalist backup as a Darwin Core Archive

`inat2dwc.py` periodically creates an up-to-date local copy of **your own**
iNaturalist data and exports it as a **Darwin Core Archive (DwC-A)**:

| File | DwC-A role | Content |
|---|---|---|
| `occurrence.csv` | Occurrence **core** | All your observations; every iNaturalist field that maps to a Darwin Core term (everything else is preserved as JSON in `dynamicProperties`) |
| `identification.csv` | Identification **extension** | **All** identifications made on your observations — your own and other people's, current, withdrawn and disagreeing ones |
| `multimedia.csv` | Multimedia (Audubon Core / GBIF) **extension** | Links to every photo and sound of your observations, ready for a later bulk-download stage |
| `meta.xml`, `eml.xml` | Archive descriptors | Standard DwC-A metadata; the three files are linked via the observation `id` / `coreid` |

The archive is also written as a zipped DwC-A (`inat_dwca.zip`) into a
dedicated zip folder, directly
loadable into GBIF IPT, the GBIF validator or any DwC-A reader.

## Why `pyinaturalist`?

The script uses [`pyinaturalist`](https://pyinaturalist.readthedocs.io) as its
HTTP layer. Evaluation outcome — it is the right choice here:

* **Rate limiting built in**: pyinaturalist enforces iNaturalist's guidelines
  (≤ 60 requests/minute, ≤ 10,000/day, burst control) for every request.
  The script adds explicit back-off + retry on HTTP 429 on top of that.
* **Retries, timeouts, parameter conversion** (booleans, lists →
  comma-separated values, ISO dates) are battle-tested instead of hand-rolled.
* **JWT authentication** via a single `access_token` argument.
* Caching is **disabled** (`expire_after=0`) so every run sees live data.

The script still passes raw API parameters where pyinaturalist's documented
signature is narrower than the actual API (e.g. `observation_id=1,2,...` and
`current=any` on `/v1/identifications`), which pyinaturalist forwards
transparently.

## Installation

```bash
pip install -r app/requirements.txt
```

Python ≥ 3.9.

## Folder layout

All code lives in the `app` folder; everything the app produces lives in the
`data` folder next to it (created automatically on the first launch, together
with a default config file, when missing):

```
iNatDownloader/
├── app/                      # the code
│   ├── inat2dwc.py           #   sync engine + CLI
│   ├── inat2dwc_gui.py       #   GUI
│   ├── media_sync.py         #   media downloader + CLI
│   ├── zenodo_upload.py      #   Zenodo publishing
│   └── requirements.txt
├── data/                     # data & configuration
│   ├── inat2dwc_settings.json  # GUI config (remembered settings)
│   ├── inat_backup.sqlite3    # SQLite mirror (default)
│   ├── dwca/                   # DwC-A exports (default)
│   └── media/                 # downloaded photos & sounds (default)
└── README.md
```

The database, export folder, zip folder and media folder default into
`data/` and can be changed at any time in the GUI.

## Usage

### GUI (recommended)

```bash
python app/inat2dwc_gui.py
```

A small tkinter window where you:

* fill in your **username** and **API token** (masked; the field wins over the
  `INAT_API_TOKEN` environment variable),
* choose **what to run**: full download, force/skip the deletion check,
  verbose logging, watermark overlap, database/export/zip/media paths,
* press **Run sync** (download + export), **Re-export only** (rebuild the
  archive from the local database without any download), **Sync media**
  (download local copies of your observation photos and sounds — see below)
  or **Stop** (cancels after the current API call; partial data is kept and
  the next run picks up the rest),
* watch the **live log** (also saveable to a file) and the status bar.

Settings — including the API token, which only lives 24 hours — are remembered
in `data/inat2dwc_settings.json`; use the GUI's **Reset config…** button to
delete the whole configuration (e.g. before sharing the code). The CLI keeps a
separate `--token-file` option for headless use.

### Local media copies (photos & sounds)

**Sync media** downloads the actual image/sound files behind the URLs in
`multimedia.csv` into a **media folder** (configured under "Sync options"; by
default a `media` folder next to the database). Files are stored per
observation as `<media folder>/<observation_id>/<media_id>.<ext>`, using the
largest available variant (`original`, falling back to `large`/`medium`).

Files are saved **exactly as iNaturalist serves them** and are never modified.
Note that iNaturalist itself strips all camera EXIF from every served size
(`exiftool -all=`, keeping only the ICC colour profile) and downscales
originals to at most 2048×2048 px, so no download can be richer than what
this fetches. Instead, every download is documented in the SQLite mirror's
**`media_files`** table:

* `filename` and `rel_path` — where the file lives in the media folder,
* `url` — the exact URL it was fetched from,
* `size_bytes` and `sha256` — integrity (also lets you re-locate the exact
  file on your own disk after re-uploading or moving things around),
* `downloaded_at` — when the file was fetched,
* `metadata` — a JSON snapshot of everything the API knows about the file:
  the **original upload filename** (e.g. `IMG_2043.JPG`, your best lead to
  trace the file back to your camera roll), original dimensions, license,
  attribution, all size-variant URLs, photo/observation page URLs, date,
  coordinates (true ones if the sync used an API token), positional accuracy
  and taxon.

The check works purely from the local database — no iNaturalist API calls
and no API token are needed, only downloads from the static file servers.
Already-present files are skipped, so a run only fetches what is new; you are
shown how many photos and sounds are missing and asked before the download
starts. Files downloaded before this bookkeeping existed are hashed and
recorded automatically by a backfill pass on the first run. Interrupted
downloads never leave partial files behind (temporary `.part` files are moved
into place only when complete). The same is available headlessly:

```bash
python app/media_sync.py --db data/inat_backup.sqlite3 --media-dir data/media --yes
```

### Upload to Zenodo

The GUI can publish the exported Darwin Core Archive as a **new version of an
existing Zenodo record**:

* create a personal access token at
  <https://zenodo.org/account/settings/applications/tokens/new/>
  with the *deposit:actions* and *deposit:write* scopes and paste it into the
  Zenodo box together with the numeric record id of your dataset,
* press **⬆ Upload DwC-A as new version** — this uploads the individual
  archive files (`occurrence.csv`, `identification.csv`, `multimedia.csv`,
  `meta.xml`, `eml.xml` — directly previewable on Zenodo) plus the
  `inat_dwca.zip` convenience copy, tags the version with the upload time,
  marks `occurrence.csv` as the file previewed on the record page and
  publishes it.

Each version contains exactly these files: any files inherited from the
previous version are removed from the draft first, so nothing accumulates,
and older versions remain available on Zenodo with their own DOIs.

**Versioning**: Zenodo numbers versions itself (Version 1, 2, 3 …) — every
published upload becomes the next version automatically, with its own record
id and version DOI, while the *concept DOI* always resolves to the latest
version. The uploader additionally writes the upload time into the
human-readable `version` metadata field. The configured record id may be any
version of the dataset: the uploader resolves it to the latest version of the
version chain before creating the new one (Zenodo only allows new versions
from the head).

The upload logic lives in `zenodo_upload.py` and can also be scripted:

```bash
python -c "from pathlib import Path; import zenodo_upload; \
zenodo_upload.new_version('16927623', [Path(p) for p in \
('dwca/occurrence.csv', 'dwca/identification.csv', 'dwca/multimedia.csv', \
'dwca/meta.xml', 'dwca/eml.xml', 'dwca/zip/inat_dwca.zip')], 'YOURKEY')"
```

### Get your API token (JWT)

You must be logged in to iNaturalist: open
<https://www.inaturalist.org/users/api_token>. The token expires after 24
hours; provide it per run.

### Command line

```bash
python app/inat2dwc.py --user YourUsername --token <JWT>

# or via a file / environment variable:
python app/inat2dwc.py --user YourUsername --token-file token.txt
INAT_API_TOKEN=<JWT> python app/inat2dwc.py --user YourUsername
```

With a token, the script fetches the **true (unobscured) coordinates** of your
own observations (`private_latitude/longitude`); without a token, only the
public, possibly generalized coordinates are available.

Options (see `--help` for all):

| Option | Default | Meaning |
|---|---|---|
| `--db` | `data/inat_backup.sqlite3` | local SQLite mirror |
| `--export-dir` | `data/dwca` | output directory for the archive files |
| `--zip-dir` | `data/dwca/zip` | folder for the zipped archive (`inat_dwca.zip`; `--zip-dir=` to disable) |
| `--full` | — | force a full download |
| `--reconcile` / `--no-reconcile` | auto | run/skip the deletion-reconciliation pass |
| `--overlap-minutes` | 60 | safety overlap on the incremental watermark |
| `--reconcile-days` | 7 | how often to reconcile deletions automatically |

### Scheduling

The first run is a full download; every later run retrieves **updates only**.
Run it as often as you like, e.g. daily:

```cron
# Linux crontab — every day at 06:00
0 6 * * * cd /path/to/iNatDownloader && /usr/bin/python3 app/inat2dwc.py -u YourUsername --token-file /secure/token.txt >> sync.log 2>&1
```

On Windows, use Task Scheduler with
`python C:\path\to\app\inat2dwc.py -u YourUsername --token-file C:\secure\token.txt`.

## How the incremental sync works

* Data is mirrored in SQLite keyed by iNaturalist IDs, so updates are
  idempotent upserts and the export is always regenerated from the full mirror.
* **Observations**: `GET /v1/observations?user_id=…&updated_since=<watermark>`
  (watermark = start of the previous successful run, minus a safety overlap).
* **Pagination**: iNaturalist (Elasticsearch) refuses to serve any result
  window beyond the first 10,000 records (HTTP 403, e.g. `page=51` at
  `per_page=200`). All fetches therefore use cursor-based pagination with
  `id_above` (ordered by id ascending), which works for arbitrarily large
  accounts.
* **Identifications**: `/v1/identifications` has **no** `updated_since` filter,
  but every identification save *and* delete *touches* its observation's
  `updated_at` (verified in the iNaturalist source code). Therefore all
  identification changes are captured by re-fetching **all** identifications
  (`current=any` — includes withdrawn ones) for the observations updated since
  the last run, in batches of 100 observation IDs per API call.
* **Deletions**: observations you delete on iNaturalist are detected by a
  periodic reconciliation pass (every 7 days by default, or `--reconcile`)
  that lists all your observation IDs (`only_id=1`) and removes local records
  that no longer exist remotely; removed IDs are kept in the
  `obs_deletions` table as tombstones.
* Media (photos/sounds) are part of the observation payload and are replaced
  wholesale for every updated observation.

## Field mapping (selection)

**Occurrence core** — iNaturalist → Darwin Core:

| iNaturalist | Darwin Core |
|---|---|
| `id` / `uri` | `id`, `catalogNumber`, `occurrenceID`, `references` |
| `user` (merged with the central `/v1/users` cache) | `recordedBy`, `recordedByID` |
| `time_observed_at` / `observed_on` | `eventDate` (UTC ISO 8601) |
| `time_observed_at` | `eventTime` (UTC `HH:MM:SSZ`; empty for date-only observations) |
| `observed_on_string` | `verbatimEventDate` |
| `place_guess` | `verbatimLocality` |
| `private_location/geojson` (if token) else `location/geojson` | `decimalLatitude/Longitude` (`geodeticDatum` is always WGS84) |
| `positional_accuracy` / `public_positional_accuracy` | `coordinateUncertaintyInMeters` |
| `place_ids` / `private_place_ids` → cached `/v1/places` lookup | `country`, `countryCode` (ISO 3166-1 alpha-2) |
| `taxon.name` / `taxon.id` / `taxon.rank` / `preferred_common_name` | `scientificName`, `taxonID`, `taxonRank`, `vernacularName` |
| taxon ancestry (cached `/v1/taxa` records) | `kingdom`, `phylum`, `class`, `order`, `family`, `genus` |
| `captive` / `taxon.introduced/native/endemic` | `establishmentMeans` (cultivated / introduced / native / endemic) |
| annotations (Sex / Life Stage / Plant Phenology) | `sex`, `lifeStage`, `reproductiveCondition` |
| `quality_grade` | `identificationVerificationStatus` |
| `license_code` | `dcterms:license` (CC URI) |
| `ofvs` (reduced to name → value) / `tags` | `dynamicProperties` (flat key:value JSON as recommended by DwC, occurrence core only) |

**Identification extension**: `id` → `identificationID`, `created_at` →
`dateIdentified`, `user` → `identifiedBy`/`identifiedByID` (full name and
ORCID — identification payloads carry only the login, so names and ORCIDs are
resolved through a cached `/v1/users` lookup, falling back to the iNaturalist
profile link), `taxon` → `scientificName`/`taxonID`/`taxonRank`/`vernacularName`
(names are resolved through a cached `/v1/taxa` lookup), `body` →
`identificationRemarks`, `current`/`category` →
`identificationVerificationStatus` (withdrawn or leading/improving/supporting/maverick).

**Multimedia extension**: best URL (original-size if derivable) in
`dcterms:identifier`, `StillImage`/`Sound` in `dcterms:type`, license from
the photo/sound, `dcterms:creator` and `dcterms:rightsHolder` are the user who
uploaded the observation (assumed to be the creator of its media), thumbnail
in `ac:thumbnailAccessURI`.

## Notes & limitations

* iNaturalist asks clients to stay ≤ 60 requests/minute and < 10,000
  requests/day — pyinaturalist enforces this; a large initial download simply
  takes as long as the rate limit dictates (a 5,000-observation account with
  identifications is typically a few hundred requests ≈ 10–15 minutes).
* The API token (JWT) expires after 24 hours; generate a fresh one at
  https://www.inaturalist.org/users/api_token for scheduled runs.
* `country`/`countryCode` are derived from the contained iNaturalist place
  with `admin_level` 0 (cached in the local `places` table); the ISO code is
  looked up from the place name and left empty (with a log warning) for
  unmapped names.
* Withdrawn identifications are included; identifications deleted outright by
  their author are removed on the next run that touches that observation
  (identifications are re-fetched per updated observation).
* Everything the API returns is preserved in the SQLite mirror (raw JSON
  payloads); the export keeps only the curated Darwin Core columns, with
  `dynamicProperties` (occurrence core) holding the user-entered iNaturalist
  observation fields (`ofvs`) and `tags`.

## Planned next stage

~~`multimedia.csv` (and the SQLite `media` table) already contain every media
URL and size variant, so a bulk media downloader can be added later without
touching the sync logic: iterate the media rows, download `original_url`,
verify content type, store under `<obs_id>/<media_id>.<ext>` and record
checksums.~~ — this is now implemented in `media_sync.py` (see
"Local media copies"), including the checksums in the `media_files` table.

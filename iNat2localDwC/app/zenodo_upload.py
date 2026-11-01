#!/usr/bin/env python3
"""
zenodo_upload — publish the DwC-A files as a new version of an existing Zenodo record.

Flow (Zenodo legacy deposit API, developers.zenodo.org):
  1. GET  /api/deposit/depositions/{record_id}
        — validates the API key and access to the record.
  2. POST /api/deposit/depositions/{record_id}/actions/newversion
        — creates a draft of a new version (metadata inherited).
  3. GET  the draft (via links.latest_draft) for its id and upload bucket,
        and DELETE any files inherited from the previous version, so every
        version contains exactly the files uploaded below.
  4. PUT  each file into the draft's bucket (stable names such as
        occurrence.csv simply replace the previous version's counterpart —
        older versions keep their own files and DOIs).
  5. PUT  the draft metadata with "version" set to the upload timestamp
        (best effort; skipped with a warning when Zenodo refuses).
  6. POST /api/deposit/depositions/{draft_id}/actions/publish
        — makes the new version public and returns its URL.

The API key needs the "deposit:actions" and "deposit:write" scopes; create one
at https://zenodo.org/account/settings/applications/tokens/new/
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

ZENODO_BASE = "https://zenodo.org"
RDM_HEADERS = {"Accept": "application/vnd.inveniordm.v1+json"}
PREVIEW_FILE = "occurrence.csv"
TIMEOUT = (15, 300)  # connect, read — file uploads can take a while


class ZenodoError(Exception):
    """Raised for any failed Zenodo API call or missing configuration."""


def _request(method: str, url: str, token: str, scheme: str = "Bearer", **kwargs):
    headers = dict(kwargs.pop("headers", {}))
    headers["Authorization"] = f"{scheme} {token}"
    resp = requests.request(method, url, headers=headers, timeout=TIMEOUT, **kwargs)
    if resp.status_code == 401 and scheme == "Bearer":
        # Some Zenodo deployments expect the legacy "Token" auth scheme.
        return _request(method, url, token, scheme="Token", **kwargs)
    return resp


def _fail(resp, what: str) -> None:
    try:
        data = resp.json()
        message = data.get("message") or data.get("text") or resp.text[:200]
    except ValueError:
        message = resp.text[:200]
    raise ZenodoError(f"{what} failed (HTTP {resp.status_code}): {message}")


def new_version(record_id: str, files, token: str, log=print) -> str:
    """Upload `files` (a path or a list of paths) as a new version of Zenodo
    record `record_id` and publish it. Returns the URL of the new version."""
    if not token:
        raise ZenodoError("No Zenodo API key configured.")
    if not record_id or not str(record_id).strip().isdigit():
        raise ZenodoError(f"Invalid Zenodo record id: {record_id!r} "
                          "(expected the numeric id, e.g. 16927623).")
    if isinstance(files, (str, Path)):
        files = [files]
    files = [Path(f) for f in files]
    if not files:
        raise ZenodoError("No files to upload.")
    for path in files:
        if not path.is_file():
            raise ZenodoError(f"No file at {path}.")

    # 1. validate the key and access to the record
    resp = _request("GET", f"{ZENODO_BASE}/api/deposit/depositions/{record_id}",
                    token, headers={"Accept": "application/json"})
    if resp.status_code == 401:
        raise ZenodoError("Zenodo API key rejected.")
    if resp.status_code == 403:
        raise ZenodoError(f"The key has no access to Zenodo record {record_id} "
                          "(it must be the record's owner).")
    if resp.status_code == 404:
        raise ZenodoError(f"Zenodo record {record_id} not found.")
    if resp.status_code != 200:
        _fail(resp, "Reading the Zenodo record")
    log(f"Zenodo: access to record {record_id} OK")

    # 1b. resolve the record to the latest version of its version chain —
    # Zenodo only lets new versions be created from the head, so a record id
    # pinned in the configuration (e.g. the first version) keeps working.
    # The versioning information only appears in the modern (InvenioRDM) view.
    resp = _request("GET", f"{ZENODO_BASE}/api/records/{record_id}",
                    token, headers=dict(RDM_HEADERS))
    if resp.status_code == 200:
        try:
            info = resp.json()
            if not (info.get("versions") or {}).get("is_latest", True):
                latest_url = (info.get("links") or {}).get("latest")
                if latest_url:
                    resp = _request("GET", latest_url, token, headers=dict(RDM_HEADERS))
                    if resp.status_code == 200:
                        latest_id = str(resp.json().get("id") or "")
                        if latest_id.isdigit():
                            log(f"Zenodo: record {record_id} is not the latest "
                                f"version — using {latest_id} instead")
                            record_id = latest_id
        except ValueError:
            pass  # unexpected payload; proceed with the given record id

    # 2. create the new version draft
    resp = _request(
        "POST",
        f"{ZENODO_BASE}/api/deposit/depositions/{record_id}/actions/newversion",
        token, headers={"Accept": "application/json", "Content-Type": "application/json"})
    if resp.status_code != 201:
        _fail(resp, "Creating the new version draft")
    draft_url = (resp.json().get("links") or {}).get("latest_draft")
    if not draft_url:
        raise ZenodoError("Zenodo response without a draft link.")
    log("Zenodo: new version draft created")

    # 3. fetch the draft for its id and upload bucket
    resp = _request("GET", draft_url, token, headers={"Accept": "application/json"})
    if resp.status_code != 200:
        _fail(resp, "Fetching the draft")
    draft = resp.json()
    draft_id = draft.get("id")
    bucket = (draft.get("links") or {}).get("bucket")
    if not draft_id or not bucket:
        raise ZenodoError("Draft without an id or upload bucket.")

    # 3b. remove any files inherited from the previous version, so each
    # version contains exactly the files uploaded below.
    inherited = [f.get("filename") or f.get("name")
                 for f in draft.get("files") or []]
    for filename in filter(None, inherited):
        resp = _request(
            "DELETE",
            f"{ZENODO_BASE}/api/deposit/depositions/{draft_id}/files/{quote(filename)}",
            token)
        if resp.status_code in (200, 204):
            log(f"Zenodo: removed inherited file {filename} from the draft")
        else:
            log(f"WARNING: could not remove inherited file {filename} "
                f"(HTTP {resp.status_code}).")

    # 4. upload the files (stable names replace the previous version's
    #    counterparts; older versions keep their own files and DOIs)
    for path in files:
        size_mb = path.stat().st_size / 1e6
        log(f"Zenodo: uploading {path.name} ({size_mb:.1f} MB) ...")
        resp = _request(
            "PUT", f"{bucket}/{quote(path.name)}", token,
            headers={"Content-Type": "application/octet-stream",
                     "Accept": "application/json"},
            data=path.read_bytes())
        if resp.status_code != 201:
            _fail(resp, f"Uploading {path.name}")
    log(f"Zenodo: {len(files)} file(s) uploaded to the draft")

    # 5. tag the draft with the upload timestamp as its version (best effort)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        metadata = dict(draft.get("metadata") or {})
        metadata["version"] = stamp
        resp = _request(
            "PUT", f"{ZENODO_BASE}/api/deposit/depositions/{draft_id}", token,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={"metadata": metadata})
        if resp.status_code == 200:
            log(f"Zenodo: draft metadata version set to {stamp}")
        else:
            log(f"WARNING: Zenodo metadata update skipped (HTTP {resp.status_code}).")
    except Exception as exc:  # noqa: BLE001 - metadata update is optional
        log(f"WARNING: Zenodo metadata update failed: {exc}")

    # 5b. mark the occurrence core as the file previewed on the record page
    # (best effort; the default_preview flag lives in the modern draft API).
    if PREVIEW_FILE in {p.name for p in files}:
        try:
            draft_api = f"{ZENODO_BASE}/api/records/{draft_id}/draft"
            resp = _request("GET", draft_api, token, headers=dict(RDM_HEADERS))
            if resp.status_code != 200:
                log(f"WARNING: could not set the preview file — draft not "
                    f"reachable via the modern API (HTTP {resp.status_code}).")
            else:
                etag = resp.headers.get("ETag")
                draft_rdm = resp.json()
                draft_rdm.setdefault("files", {})
                draft_rdm["files"]["default_preview"] = PREVIEW_FILE
                put_headers = dict(RDM_HEADERS)
                put_headers["Content-Type"] = "application/json"
                if etag:
                    put_headers["If-Match"] = etag
                resp = _request("PUT", draft_api, token, headers=put_headers,
                                json=draft_rdm)
                if resp.status_code == 200:
                    log(f"Zenodo: {PREVIEW_FILE} marked as the previewed file")
                else:
                    log(f"WARNING: setting the preview file failed "
                        f"(HTTP {resp.status_code}): {resp.text[:150]}")
        except Exception as exc:  # noqa: BLE001 - the preview flag is optional
            log(f"WARNING: setting the preview file failed: {exc}")

    # 6. publish
    resp = _request(
        "POST", f"{ZENODO_BASE}/api/deposit/depositions/{draft_id}/actions/publish",
        token, headers={"Accept": "application/json"})
    if resp.status_code != 202:
        _fail(resp, "Publishing the new version")
    published = resp.json()
    url = (published.get("links") or {}).get("html") or \
        f"{ZENODO_BASE}/records/{published.get('record_id', record_id)}"
    log(f"Zenodo: new version published — {url}")
    return url

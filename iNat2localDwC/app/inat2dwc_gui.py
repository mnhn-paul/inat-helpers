#!/usr/bin/env python3
"""
inat2dwc GUI — small tkinter front-end for inat2dwc.py.

Fill in your iNaturalist username and API token, choose what to run
(full download / incremental update / export-only / test run) and watch the
live log. "Sync media" downloads local copies of your observation photos
and sounds, asking before it starts. Settings — including the API token,
which expires after 24 hours — are remembered between sessions and can be
cleared at any time with "Reset config" (e.g. to share the code without
your configuration).

Uses ttkbootstrap when available (flatly/darkly themes, dark/light toggle and
font-size controls in the toolbar); falls back to plain ttk otherwise.

Folder layout: the code lives in the "app" folder, everything it produces —
the config file (inat2dwc_settings.json), the SQLite mirror, the DwC-A
exports and the downloaded media — in the "data" folder next to it, which is
created (with a default config) on the first launch if missing.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import queue
import sqlite3
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from tkinter import END, BOTH, DISABLED, NORMAL, X, BooleanVar, IntVar, StringVar, filedialog, messagebox, scrolledtext, ttk
from tkinter import Tk

THIS_DIR = Path(__file__).resolve().parent          # .../app  — code lives here
ROOT_DIR = THIS_DIR.parent                          # .../iNatDownloader
DATA_DIR = ROOT_DIR / "data"                        # data, config and exports live here
sys.path.insert(0, str(THIS_DIR))

import inat2dwc
import media_sync
import zenodo_upload
from inat2dwc import Cancelled, InatClient, Store

try:
    import ttkbootstrap as tb
    HAVE_TTKB = True
except ImportError:
    tb = None
    HAVE_TTKB = False

SETTINGS_PATH = DATA_DIR / "inat2dwc_settings.json"
TOKEN_URL = "https://www.inaturalist.org/users/api_token"
ZENODO_TOKEN_URL = "https://zenodo.org/account/settings/applications/tokens/new/"

APP_TITLE = "iNatDownloader — iNaturalist → Darwin Core Archive"

DEFAULT_SETTINGS = {
    "user": "",
    "token": "",
    "db": str(DATA_DIR / "inat_backup.sqlite3"),
    "export_dir": str(DATA_DIR / "dwca"),
    "zip_dir": str(DATA_DIR / "dwca" / "zip"),
    "media_dir": "",  # empty -> derived from the database folder (see below)
    "overlap_minutes": inat2dwc.DEFAULT_OVERLAP_MINUTES,
    "reconcile_days": inat2dwc.DEFAULT_RECONCILE_DAYS,
    "full": False,
    "test_mode": False,
    "reconcile": False,
    "no_reconcile": False,
    "verbose": False,
    "mask_token": True,
    "dark": False,
    "font_size": 10,
    "zenodo_token": "",
    "zenodo_record": "",
}


def default_media_dir(db_path: str) -> str:
    """Media folder shown when none is configured: <db folder>/media."""
    return str(Path(db_path).parent / "media")


def ensure_data_layout() -> None:
    """Create the data folder and a default config file on first launch
    (both live beside the app folder, so the code stays data-free)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not SETTINGS_PATH.exists():
        save_settings(dict(DEFAULT_SETTINGS))


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)
    settings = dict(DEFAULT_SETTINGS)
    settings.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
    # Migration: the old "zip" setting was a file path; keep its folder.
    if "zip_dir" not in data and data.get("zip"):
        settings["zip_dir"] = str(Path(data["zip"]).parent)
    if not str(settings.get("media_dir") or "").strip():
        settings["media_dir"] = default_media_dir(settings["db"])
    return settings


def save_settings(settings: dict) -> None:
    # The token is stored too (it only lives 24 hours); use "Reset config"
    # to wipe the whole configuration, token included.
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def jwt_expiry(token: str) -> datetime | None:
    """Expiry time (UTC) of an iNaturalist API token (a JWT), if decodable."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        exp = data.get("exp")
        return datetime.fromtimestamp(int(exp), tz=timezone.utc) if exp else None
    except Exception:  # noqa: BLE001 - any malformed token
        return None


class Tooltip:
    """Hover tooltip for ttk/tk widgets (ttk has none built in)."""

    def __init__(self, widget, text: str, delay: int = 500):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after_id: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after_id = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self._after_id is not None:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self) -> None:
        if self._tip is not None:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        # The 1px dark border is drawn by the Toplevel itself.
        self._tip.configure(background="#555555")
        label = tk.Label(
            self._tip, text=self.text, justify="left", wraplength=380,
            background="#fdfbe8", foreground="#333333",
            padx=8, pady=5, font=("Segoe UI", 9),
        )
        label.pack(padx=1, pady=1)

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            tip, self._tip = self._tip, None
            tip.destroy()


def add_tooltip(widget, text: str) -> None:
    Tooltip(widget, text)


class QueueLogHandler(logging.Handler):
    """Forward log records to the GUI thread via a queue."""

    def __init__(self, log_queue: "queue.Queue[logging.LogRecord]"):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        self.log_queue.put(record)


class InatGui:
    def __init__(self, root: tk.Tk, settings: dict):
        self.root = root
        root.minsize(960, 680)

        self.settings = settings
        self.theme_style = tb.Style() if HAVE_TTKB else ttk.Style(root)
        if not HAVE_TTKB:
            try:
                self.theme_style.theme_use("vista")
            except Exception:  # noqa: BLE001 - fall back to the default theme
                pass
            base_font = "Segoe UI" if os.name == "nt" else "TkDefaultFont"
            self.theme_style.configure("Primary.TButton",
                                       font=(base_font, 10, "bold"), padding=(14, 6))

        self.log_queue: "queue.Queue[logging.LogRecord]" = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.status_var = StringVar(value="Idle")
        self.last_sync_var = StringVar(value="Last sync: unknown")

        self._build_toolbar()
        self._build_account()
        self._build_zenodo()
        self._build_options()
        self._build_run_buttons()
        self._build_log()
        self._build_bottom()

        self.apply_colors()
        self.set_font_size(self.settings["font_size"])
        self._refresh_last_sync()
        self.update_token_status()

        # Route inat2dwc logging into the GUI log pane.
        handler = QueueLogHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(message)s"))
        inat_logger = logging.getLogger("inat2dwc")
        inat_logger.setLevel(logging.INFO)
        inat_logger.addHandler(handler)
        logging.getLogger("pyinaturalist").setLevel(logging.WARNING)

        root.after(150, self._poll_log_queue)
        root.after(30000, self._token_timer)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.log("info", f"inat2dwc GUI ready. Get a fresh API token at {TOKEN_URL} "
                         "(valid for 24 hours).")

    # ------------------------------------------------------------------ UI --
    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self.root)
        bar.pack(fill=X, padx=10, pady=(10, 2))

        ttk.Label(bar, text="API token:").pack(side="left")
        self.token_var = StringVar(value=self.settings.get("token", ""))
        self.token_var.trace_add("write", lambda *_: self.update_token_status())
        self.token_entry = ttk.Entry(
            bar, textvariable=self.token_var, width=44,
            show="*" if self.settings["mask_token"] else "",
        )
        self.token_entry.pack(side="left", padx=4)
        add_tooltip(self.token_entry, "Optional. Without a token you get the public "
                                      "(possibly obscured) coordinates; with one, the "
                                      "true coordinates of your own observations are "
                                      "fetched. Expires after 24 hours. It is stored in "
                                      "the config file so you do not have to re-paste "
                                      "it every session.")
        self.toggle_btn = ttk.Button(bar, width=8, text="Show", command=self._toggle_token)
        self.toggle_btn.pack(side="left")
        if not self.settings["mask_token"]:
            self.toggle_btn.configure(text="Hide")
        self.lbl_token = ttk.Label(bar, text="")
        self.lbl_token.pack(side="left", padx=8)
        ttk.Button(bar, text="Get token ↗",
                   command=lambda: webbrowser.open(TOKEN_URL)).pack(side="left")

        theme_btn = ttk.Button(bar, text="Dark / light", command=self.toggle_theme)
        theme_btn.pack(side="right", padx=6)
        add_tooltip(theme_btn, "Switch between the light (flatly) and dark (darkly) "
                               "themes. The choice is remembered.")
        if not HAVE_TTKB:
            theme_btn.configure(state=DISABLED)
        ttk.Button(bar, text="A-", width=3,
                   command=lambda: self.set_font_size(self.settings["font_size"] - 1)
                   ).pack(side="right", padx=2)
        ttk.Button(bar, text="A+", width=3,
                   command=lambda: self.set_font_size(self.settings["font_size"] + 1)
                   ).pack(side="right")
        add_tooltip(bar.winfo_children()[-1], "Increase the font size (A- decreases it). "
                                              "The size is remembered.")

    def _build_account(self) -> None:
        box = ttk.LabelFrame(self.root, text="iNaturalist account", padding=10)
        box.pack(fill=X, padx=10, pady=(6, 2))

        ttk.Label(box, text="Username:").grid(row=0, column=0, sticky="w")
        self.user_var = StringVar(value=self.settings["user"])
        user_entry = ttk.Entry(box, textvariable=self.user_var, width=28)
        user_entry.grid(row=0, column=1, sticky="w")
        add_tooltip(user_entry, "Your iNaturalist login (or numeric user id). "
                                "All observations of this account are backed up.")

        box.columnconfigure(1, weight=1)

    def _build_zenodo(self) -> None:
        box = ttk.LabelFrame(self.root, text="Zenodo", padding=10)
        box.pack(fill=X, padx=10, pady=2)

        ttk.Label(box, text="API key:").grid(row=0, column=0, sticky="w")
        self.zenodo_token_var = StringVar(value=self.settings.get("zenodo_token", ""))
        self.zenodo_token_entry = ttk.Entry(
            box, textvariable=self.zenodo_token_var, width=44,
            show="*" if self.settings["mask_token"] else "",
        )
        self.zenodo_token_entry.grid(row=0, column=1, sticky="w")
        add_tooltip(self.zenodo_token_entry,
                    f"Personal access token for the Zenodo deposit API — create one "
                    f"at {ZENODO_TOKEN_URL} with the 'deposit:actions' and "
                    f"'deposit:write' scopes. Stored in the config file.")
        self.zenodo_toggle_btn = ttk.Button(
            box, width=8, text="Show", command=self._toggle_zenodo_token)
        self.zenodo_toggle_btn.grid(row=0, column=2, padx=5)
        if not self.settings["mask_token"]:
            self.zenodo_toggle_btn.configure(text="Hide")
        ttk.Button(box, text="Get key ↗",
                   command=lambda: webbrowser.open(ZENODO_TOKEN_URL)).grid(
            row=0, column=3, padx=5)

        ttk.Label(box, text="Record id:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.zenodo_record_var = StringVar(value=self.settings.get("zenodo_record", ""))
        record_entry = ttk.Entry(box, textvariable=self.zenodo_record_var, width=16)
        record_entry.grid(row=1, column=1, sticky="w", pady=(6, 0))
        add_tooltip(record_entry, "Numeric id of the Zenodo record (dataset) that "
                                  "versions are added to, e.g. 16927623. Every "
                                  "upload creates and publishes a new version of it.")

        self.zenodo_upload_btn = ttk.Button(
            box, text="⬆ Upload DwC-A as new version",
            command=self._on_zenodo_upload)
        self.zenodo_upload_btn.grid(row=2, column=1, sticky="w", pady=(8, 0))
        add_tooltip(self.zenodo_upload_btn,
                    "Uploads the Darwin Core Archive files (occurrence.csv, "
                    "identification.csv, multimedia.csv, meta.xml, eml.xml) from the "
                    "export folder plus the inat_dwca.zip as a new version of the "
                    "record and publishes it.\nEvery version contains exactly these "
                    "files; previous versions remain available on Zenodo (each with "
                    "its own DOI).")

        box.columnconfigure(1, weight=1)

    def _build_options(self) -> None:
        box = ttk.LabelFrame(self.root, text="Sync options", padding=10)
        box.pack(fill=X, padx=10, pady=2)

        self.test_mode_var = BooleanVar(value=self.settings["test_mode"])
        test_btn = ttk.Checkbutton(
            box, text="Test mode — only download the first 100 records",
            variable=self.test_mode_var,
        )
        test_btn.grid(row=0, column=0, columnspan=2, sticky="w")
        add_tooltip(test_btn, "Quick trial run: downloads only the first 100 "
                              "observations (your oldest ones) plus their "
                              "identifications and exports them.\n"
                              "The sync watermark is NOT advanced, so the next "
                              "normal run still fetches everything.\n"
                              "Tip: point Database/Export to fresh paths to test "
                              "without touching your real backup.")

        self.full_var = BooleanVar(value=self.settings["full"])
        full_btn = ttk.Checkbutton(
            box, text="Full download (re-download everything)",
            variable=self.full_var,
        )
        full_btn.grid(row=1, column=0, columnspan=2, sticky="w")
        add_tooltip(full_btn, "Ignores the incremental watermark and re-fetches ALL "
                              "observations. Use for the very first run, or when you "
                              "suspect the local database is incomplete.\n"
                              "Slow on large accounts (rate limits apply).")

        self.reconcile_var = BooleanVar(value=self.settings["reconcile"])
        reconcile_btn = ttk.Checkbutton(
            box, text="Force deletion check now",
            variable=self.reconcile_var,
        )
        reconcile_btn.grid(row=2, column=0, columnspan=2, sticky="w")
        add_tooltip(reconcile_btn, "Lists ALL your observation ids on iNaturalist and "
                                   "removes local records of observations that were "
                                   "deleted there.\nNormally runs automatically when "
                                   "the last check is older than 'Deletion check "
                                   "every N days'; check this to force it now.")

        self.no_reconcile_var = BooleanVar(value=self.settings["no_reconcile"])
        no_reconcile_btn = ttk.Checkbutton(
            box, text="Skip deletion check (faster)",
            variable=self.no_reconcile_var,
        )
        no_reconcile_btn.grid(row=3, column=0, columnspan=2, sticky="w")
        add_tooltip(no_reconcile_btn, "Do not run the deletion check during this run, "
                                      "even if it is due. Makes runs faster.")

        self.verbose_var = BooleanVar(value=self.settings["verbose"])
        verbose_btn = ttk.Checkbutton(
            box, text="Verbose (debug) logging",
            variable=self.verbose_var,
        )
        verbose_btn.grid(row=4, column=0, columnspan=2, sticky="w")
        add_tooltip(verbose_btn, "Show debug messages in the log (every API request, "
                                 "fetched counts, …). Useful when something "
                                 "misbehaves.")

        ttk.Label(box, text="Overlap (min):").grid(row=5, column=0, sticky="w")
        self.overlap_var = IntVar(value=self.settings["overlap_minutes"])
        overlap_spin = ttk.Spinbox(box, from_=0, to=1440, increment=5, width=7,
                                   textvariable=self.overlap_var)
        overlap_spin.grid(row=5, column=1, sticky="w")
        add_tooltip(overlap_spin, "Incremental runs re-fetch everything changed since "
                                  "'last sync time minus this overlap'. This safety "
                                  "margin catches records that were updated on the "
                                  "server while the previous run was still going. "
                                  "Default 60 minutes.")

        ttk.Label(box, text="Deletion check every (days):").grid(row=6, column=0, sticky="w")
        self.reconcile_days_var = IntVar(value=self.settings["reconcile_days"])
        reconcile_days_spin = ttk.Spinbox(box, from_=1, to=365, width=7,
                                          textvariable=self.reconcile_days_var)
        reconcile_days_spin.grid(row=6, column=1, sticky="w")
        add_tooltip(reconcile_days_spin, "The deletion check (see 'Force deletion check "
                                         "now') runs automatically when the last one is "
                                         "older than this many days. Default 7 days.")

        paths = ttk.Frame(box)
        paths.grid(row=7, column=0, columnspan=3, sticky="we", pady=(10, 0))

        ttk.Label(paths, text="Database:").grid(row=0, column=0, sticky="w")
        self.db_var = StringVar(value=self.settings["db"])
        db_entry = ttk.Entry(paths, textvariable=self.db_var)
        db_entry.grid(row=0, column=1, sticky="we")
        add_tooltip(db_entry, "Path of the local SQLite mirror of your iNaturalist "
                              "data. Everything the API returns is stored here, so "
                              "re-exports never need new downloads.")

        ttk.Label(paths, text="Export dir:").grid(row=1, column=0, sticky="w")
        self.export_var = StringVar(value=self.settings["export_dir"])
        export_entry = ttk.Entry(paths, textvariable=self.export_var)
        export_entry.grid(row=1, column=1, sticky="we")
        add_tooltip(export_entry, "Folder for the Darwin Core Archive files "
                                  "(occurrence.csv, identification.csv, multimedia.csv, "
                                  "meta.xml, eml.xml).")

        ttk.Label(paths, text="Zip folder:").grid(row=2, column=0, sticky="w")
        self.zip_var = StringVar(value=self.settings["zip_dir"])
        zip_entry = ttk.Entry(paths, textvariable=self.zip_var)
        zip_entry.grid(row=2, column=1, sticky="we")
        add_tooltip(zip_entry, "Every export also writes the Darwin Core Archive as "
                               "inat_dwca.zip into this folder (the previous one is "
                               "replaced; the Zenodo version history keeps older "
                               "states).\nLeave empty to disable zipping.")

        ttk.Label(paths, text="Media folder:").grid(row=3, column=0, sticky="w")
        self.media_var = StringVar(value=self.settings.get("media_dir")
                                   or default_media_dir(self.settings["db"]))
        media_entry = ttk.Entry(paths, textvariable=self.media_var)
        media_entry.grid(row=3, column=1, sticky="we")
        add_tooltip(media_entry,
                    "Folder for the local copies of your observation photos and "
                    "sounds (one subfolder per observation). Used by 'Sync "
                    "media', which only downloads files that are not there "
                    "yet.\nBy default this sits next to the database.")
        paths.columnconfigure(1, weight=1)

    def _build_run_buttons(self) -> None:
        box = ttk.Frame(self.root)
        box.pack(fill=X, padx=10, pady=6)

        run_kwargs = dict(text="▶ Run sync", command=self._on_run_sync)
        if HAVE_TTKB:
            self.run_btn = tb.Button(box, bootstyle="primary", **run_kwargs)
        else:
            self.run_btn = ttk.Button(box, style="Primary.TButton", **run_kwargs)
        self.run_btn.pack(side="left")
        add_tooltip(self.run_btn, "Download everything that changed since the last run "
                                  "(or everything, if 'Full download' is checked), "
                                  "update the local database and write the export.")

        self.export_btn = ttk.Button(
            box, text="↻ Re-export only (no download)",
            command=self._on_export_only,
        )
        self.export_btn.pack(side="left", padx=6)
        add_tooltip(self.export_btn, "Write the Darwin Core Archive again from the data "
                                     "already in the local database, without contacting "
                                     "iNaturalist.")

        self.media_btn = ttk.Button(
            box, text="⤓ Sync media (photos & sounds)",
            command=self._on_sync_media,
        )
        self.media_btn.pack(side="left", padx=6)
        add_tooltip(self.media_btn,
                    "Checks which photos and sounds of your observations are "
                    "already in the media folder and downloads the missing "
                    "ones from the iNaturalist servers.\n"
                    "Files are saved as-is (never modified). Every download "
                    "is recorded in the database: filename, checksum and all "
                    "metadata known from the API (original upload filename, "
                    "URLs, license, ...).\n"
                    "Works from the local database alone (no API token needed, "
                    "no iNaturalist API calls); before downloading you are "
                    "asked how many files are missing and whether to proceed.")
        self.stop_btn = ttk.Button(box, text="■ Stop", command=self._on_stop, state=DISABLED)
        self.stop_btn.pack(side="left", padx=6)
        add_tooltip(self.stop_btn, "Stop after the current API request finishes. "
                                   "Partial data is kept; the next run picks up the rest.")
        ttk.Button(box, text="📂 Open export folder",
                   command=self._open_export_dir).pack(side="left", padx=6)

        reset_btn = ttk.Button(box, text="Reset config…", command=self._on_reset_config)
        reset_btn.pack(side="right")
        add_tooltip(reset_btn, f"Deletes {SETTINGS_PATH.name} — the remembered username, "
                                "API token, paths and options — and resets the form to "
                                "defaults.\nUse this to share the code without your "
                                "configuration. Your database and exports are NOT touched.")
        self.save_log_btn = ttk.Button(box, text="Save log…", command=self._save_log)
        self.save_log_btn.pack(side="right", padx=6)
        ttk.Button(box, text="Clear log", command=self._clear_log).pack(side="right")

    def _build_log(self) -> None:
        box = ttk.LabelFrame(self.root, text="Log", padding=5)
        box.pack(fill=BOTH, expand=True, padx=10, pady=2)
        self.log_text = scrolledtext.ScrolledText(
            box, height=12, state=DISABLED, wrap="word", borderwidth=0,
            highlightthickness=0,
            font=("Consolas" if os.name == "nt" else "Courier", 10),
        )
        self.log_text.pack(fill=BOTH, expand=True)
        for tag, color in (("DEBUG", "#888888"), ("INFO", "#222222"),
                           ("WARNING", "#a05a00"), ("ERROR", "#b00020"),
                           ("CRITICAL", "#b00020")):
            self.log_text.tag_configure(tag, foreground=color)
        self.log_text.tag_configure("time", foreground="#888888")

    def _build_bottom(self) -> None:
        bottom = ttk.Frame(self.root)
        bottom.pack(fill=X, side="bottom", padx=10, pady=(2, 8))
        self.progress = ttk.Progressbar(bottom, mode="indeterminate")
        self.progress.pack(fill=X, pady=(4, 2))
        row = ttk.Frame(bottom)
        row.pack(fill=X)
        ttk.Label(row, textvariable=self.status_var).pack(side="left")
        sync_label = ttk.Label(row, textvariable=self.last_sync_var)
        sync_label.pack(side="right")
        add_tooltip(sync_label, "Local time of the last completed sync, read from the "
                                "database. Test-mode runs do not advance it.")

    # ------------------------------------------------- theme & font sizing --
    def theme_colors(self) -> tuple[str, str]:
        bg = self.theme_style.lookup("TEntry", "fieldbackground") or "white"
        fg = self.theme_style.lookup("TEntry", "foreground") or "black"
        return bg, fg

    def apply_colors(self) -> None:
        bg, fg = self.theme_colors()
        self.log_text.config(background=bg, foreground=fg, insertbackground=fg)
        self.log_text.tag_configure("INFO", foreground=fg)
        self.log_text.tag_configure("DEBUG", foreground="#888888")
        self.log_text.tag_configure("WARNING", foreground="#e0a800")
        self.log_text.tag_configure("ERROR", foreground="#d63939")
        self.log_text.tag_configure("CRITICAL", foreground="#d63939")
        self.log_text.tag_configure("time", foreground="#888888")

    def set_font_size(self, size: int) -> None:
        size = max(8, min(20, size))
        self.settings["font_size"] = size
        for name in ("TkDefaultFont", "TkTextFont", "TkFixedFont", "TkMenuFont",
                     "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(size=size)
            except tk.TclError:
                pass
        save_settings(self.settings)

    def toggle_theme(self) -> None:
        if not HAVE_TTKB:
            return
        self.settings["dark"] = not self.settings["dark"]
        save_settings(self.settings)
        self.theme_style.theme_use("darkly" if self.settings["dark"] else "flatly")
        self.apply_colors()

    # ------------------------------------------------------------- helpers --
    def log(self, level: str, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state=NORMAL)
        self.log_text.insert(END, f"{stamp}  ", ("time",))
        self.log_text.insert(END, message.rstrip() + "\n", (level,))
        self.log_text.configure(state=DISABLED)
        self.log_text.see(END)

    def _poll_log_queue(self):
        while True:
            try:
                record = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log(record.levelname, record.getMessage())
        self.root.after(150, self._poll_log_queue)

    def update_token_status(self) -> None:
        """Coloured token expiry label next to the token entry."""
        token = self.token_var.get().strip()
        if not token:
            self.lbl_token.configure(text="no token — public coordinates only",
                                      foreground="gray")
            return
        exp = jwt_expiry(token)
        if exp is None:
            self.lbl_token.configure(text="expiry unknown", foreground="gray")
            return
        left = (exp - datetime.now(timezone.utc)).total_seconds()
        if left <= 0:
            self.lbl_token.configure(text="token EXPIRED — get a new one",
                                     foreground="#d63939")
        elif left < 1800:
            self.lbl_token.configure(text=f"expires in {int(left // 60)} min",
                                     foreground="#e0a800")
        else:
            self.lbl_token.configure(
                text=f"valid for ~{int(left // 3600)}h {int(left % 3600 // 60)}m",
                foreground="#2fb344")

    def _token_timer(self) -> None:
        self.update_token_status()
        self.root.after(30000, self._token_timer)

    def _refresh_last_sync(self) -> None:
        """Read the last-sync watermark from the database (without creating it)."""
        db_path = Path(self.settings["db"])
        if not db_path.is_file():
            self.last_sync_var.set("Last sync: no database yet")
            return
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key = 'last_sync'").fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            row = None
        if not row or not row[0]:
            self.last_sync_var.set("Last sync: never")
            return
        last_sync = inat2dwc.as_utc(row[0])
        if last_sync is None:
            self.last_sync_var.set("Last sync: never")
            return
        local = last_sync.astimezone()
        self.last_sync_var.set("Last sync: " + local.strftime("%Y-%m-%d %H:%M:%S"))

    def _apply_settings_to_form(self) -> None:
        """Push a settings dict into all form widgets (after a config reset)."""
        s = self.settings
        self.user_var.set(s["user"])
        self.token_var.set(s.get("token", ""))
        self.zenodo_token_var.set(s.get("zenodo_token", ""))
        self.zenodo_record_var.set(s.get("zenodo_record", ""))
        self.db_var.set(s["db"])
        self.export_var.set(s["export_dir"])
        self.zip_var.set(s["zip_dir"])
        self.media_var.set(s.get("media_dir") or default_media_dir(s["db"]))
        self.full_var.set(s["full"])
        self.test_mode_var.set(s["test_mode"])
        self.reconcile_var.set(s["reconcile"])
        self.no_reconcile_var.set(s["no_reconcile"])
        self.verbose_var.set(s["verbose"])
        self.overlap_var.set(s["overlap_minutes"])
        self.reconcile_days_var.set(s["reconcile_days"])
        masked = not s["mask_token"]
        self.token_entry.configure(show="" if masked else "*")
        self.toggle_btn.configure(text="Hide" if masked else "Show")

    def _collect_settings(self) -> dict:
        settings = {
            "user": self.user_var.get().strip(),
            "token": self.token_var.get().strip(),
            "db": self.db_var.get().strip(),
            "export_dir": self.export_var.get().strip(),
            "zip_dir": self.zip_var.get().strip(),
            "media_dir": self.media_var.get().strip() or default_media_dir(self.db_var.get().strip()),
            "full": self.full_var.get(),
            "test_mode": self.test_mode_var.get(),
            "reconcile": self.reconcile_var.get(),
            "no_reconcile": self.no_reconcile_var.get(),
            "verbose": self.verbose_var.get(),
            "overlap_minutes": self.overlap_var.get(),
            "reconcile_days": self.reconcile_days_var.get(),
            "mask_token": self.settings["mask_token"],
            "dark": self.settings["dark"],
            "font_size": self.settings["font_size"],
            "zenodo_token": self.zenodo_token_var.get().strip(),
            "zenodo_record": self.zenodo_record_var.get().strip(),
        }
        # The GUI state itself stays authoritative for theme/font.
        self.settings.update({k: settings[k] for k in
                              ("user", "token", "zenodo_token", "zenodo_record",
                               "db", "export_dir", "zip_dir", "media_dir",
                               "full", "test_mode", "reconcile", "no_reconcile",
                               "verbose", "overlap_minutes", "reconcile_days",
                               "mask_token")})
        return settings

    def _resolve_token(self, settings: dict) -> str | None:
        return settings["token"] or os.environ.get("INAT_API_TOKEN") or None

    def _make_args(self, settings: dict, full: bool):
        args = argparse.Namespace()
        args.user = settings["user"]
        args.full = full
        args.limit = 100 if settings["test_mode"] else None
        args.reconcile = settings["reconcile"]
        args.no_reconcile = settings["no_reconcile"]
        args.overlap_minutes = settings["overlap_minutes"]
        args.reconcile_days = settings["reconcile_days"]
        args.db = Path(settings["db"])
        args.export_dir = Path(settings["export_dir"])
        args.zip_dir = Path(settings["zip_dir"]) if settings["zip_dir"] else None
        return args

    # ------------------------------------------------------------ callbacks --
    def _toggle_token(self):
        masked = self.token_entry.cget("show") == "*"
        self.token_entry.configure(show="" if masked else "*")
        self.toggle_btn.configure(text="Hide" if masked else "Show")
        self.settings["mask_token"] = not masked

    def _toggle_zenodo_token(self):
        masked = self.zenodo_token_entry.cget("show") == "*"
        self.zenodo_token_entry.configure(show="" if masked else "*")
        self.zenodo_toggle_btn.configure(text="Hide" if masked else "Show")
        self.settings["mask_token"] = not masked

    def _on_zenodo_upload(self):
        settings = self._collect_settings()
        if not settings["zenodo_token"] or not settings["zenodo_record"]:
            messagebox.showerror(
                APP_TITLE, "Fill in the Zenodo API key and record id first.")
            return
        if not settings["export_dir"]:
            messagebox.showerror(APP_TITLE, "Configure the export folder first.")
            return
        save_settings(settings)
        self._start_worker(lambda: self._run_zenodo_upload(settings))

    def _open_export_dir(self):
        path = self.export_var.get().strip()
        if not path:
            return
        Path(path).mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            os.system(f'open "{path}"')  # noqa: S605,S607
        else:
            os.system(f'xdg-open "{path}"')  # noqa: S605,S607

    def _clear_log(self):
        self.log_text.configure(state=NORMAL)
        self.log_text.delete("1.0", END)
        self.log_text.configure(state=DISABLED)

    def _save_log(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".txt", filetypes=[("Text files", "*.txt")],
            initialfile="inat2dwc_log.txt",
        )
        if path:
            Path(path).write_text(self.log_text.get("1.0", "end-1c"), encoding="utf-8")

    def _on_reset_config(self):
        if not messagebox.askyesno(
            APP_TITLE,
            f"Delete the saved configuration ({SETTINGS_PATH.name})?\n\n"
            "This removes the remembered username, API token, paths and all "
            "saved options, and resets the form to defaults.\n"
            "Your database and exports are NOT touched.\n\n"
            "Use this to share the code without your configuration.",
            icon="warning",
        ):
            return
        try:
            SETTINGS_PATH.unlink(missing_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Could not delete the settings file:\n{exc}")
            return
        self.settings = dict(DEFAULT_SETTINGS)
        self._apply_settings_to_form()
        if HAVE_TTKB:
            self.theme_style.theme_use("darkly" if self.settings["dark"] else "flatly")
        self.apply_colors()
        self.set_font_size(self.settings["font_size"])
        self._refresh_last_sync()
        self.log("INFO", f"Configuration cleared ({SETTINGS_PATH.name} deleted). "
                         "The form now shows the default settings.")

    def _set_running(self, running: bool):
        for btn in (self.run_btn, self.export_btn, self.media_btn,
                    self.zenodo_upload_btn):
            btn.configure(state=DISABLED if running else NORMAL)
        self.stop_btn.configure(state=NORMAL if running else DISABLED)
        state = "Running…" if running else "Idle"
        self.status_var.set(state)
        self.root.config(cursor="watch" if running else "")
        if running:
            self.progress.start(12)
        else:
            self.progress.stop()

    def _validate(self, settings: dict, need_user: bool = True) -> bool:
        if need_user and not settings["user"]:
            messagebox.showerror(APP_TITLE, "Please fill in your iNaturalist username.")
            return False
        if not settings["db"] or not settings["export_dir"]:
            messagebox.showerror(APP_TITLE, "Database and export folder are required.")
            return False
        return True

    def _start_worker(self, target) -> None:
        self.cancel_event = threading.Event()
        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()
        self._set_running(True)

    def _on_run_sync(self):
        settings = self._collect_settings()
        if not self._validate(settings):
            return
        save_settings(settings)
        self._start_worker(lambda: self._run_sync(settings))

    def _on_export_only(self):
        settings = self._collect_settings()
        if not self._validate(settings, need_user=False):
            return
        save_settings(settings)
        self._start_worker(lambda: self._run_export_only(settings))

    def _on_sync_media(self):
        settings = self._collect_settings()
        if not self._validate(settings, need_user=False):
            return
        if not Path(settings["db"]).is_file():
            messagebox.showerror(
                APP_TITLE, f"No database at {settings['db']} — "
                           "run a sync first, so the media records are known.")
            return
        save_settings(settings)
        self._start_worker(lambda: self._run_media_sync(settings))

    def _ask_yes(self, message: str) -> bool:
        """Ask a yes/no question on the GUI thread; safe to call from a worker.

        Returns False when the question cannot be asked or is not answered
        (e.g. the window was closed).
        """
        result = {"yes": False}
        answered = threading.Event()

        def ask():
            try:
                result["yes"] = messagebox.askyesno(APP_TITLE, message)
            except tk.TclError:  # window already closed
                pass
            finally:
                answered.set()

        try:
            self.root.after(0, ask)
        except tk.TclError:
            return False
        answered.wait(timeout=3600)
        return result.get("yes", False)

    def _on_stop(self):
        self.cancel_event.set()
        self.status_var.set("Stopping… (waiting for the current API call to finish)")

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                APP_TITLE,
                "A run is still in progress. Stop it and exit anyway?",
            ):
                return
            self.cancel_event.set()
        save_settings(self.settings)
        self.root.destroy()

    # ---------------------------------------------------------- worker code --
    def _run_sync(self, settings: dict) -> None:
        started = time.time()
        token = self._resolve_token(settings)
        logging.getLogger("inat2dwc").setLevel(
            logging.DEBUG if settings["verbose"] else logging.INFO)
        try:
            args = self._make_args(settings, full=settings["full"])
            client = InatClient(token=token, cancel_event=self.cancel_event)
            store = Store(args.db)
            inat2dwc.sync(args, client, store)
            self.status_var.set(
                f"Done in {time.time() - started:.0f}s — "
                f"{args.export_dir} (zip folder: {args.zip_dir or 'disabled'})"
            )
        except Cancelled:
            self.log("WARNING", "Run cancelled. Partial data is kept; "
                                "the next run will pick up the rest.")
            self.status_var.set("Cancelled")
        except SystemExit as exc:  # raised by resolve_user on unknown username
            self.log("ERROR", str(exc))
            self.status_var.set("Failed")
        except Exception as exc:  # noqa: BLE001
            self.log("ERROR", f"{type(exc).__name__}: {exc}")
            self.status_var.set("Failed")
        finally:
            self.root.after(0, self._after_run)

    def _run_zenodo_upload(self, settings: dict) -> None:
        started = time.time()
        try:
            export_dir = Path(settings["export_dir"])
            names = ("occurrence.csv", "identification.csv", "multimedia.csv",
                     "meta.xml", "eml.xml")
            files = [export_dir / name for name in names]
            missing = [f.name for f in files if not f.is_file()]
            if missing:
                raise FileNotFoundError(
                    f"Missing {', '.join(missing)} in {export_dir} — "
                    "run an export first.")
            if settings["zip_dir"]:
                zip_path = Path(settings["zip_dir"]) / "inat_dwca.zip"
                if zip_path.is_file():
                    files.append(zip_path)
                else:
                    self.log("INFO", "Zenodo: no inat_dwca.zip found — "
                                     "uploading the DwC-A files without it.")
            self.log("INFO", f"Zenodo: uploading {', '.join(f.name for f in files)} "
                             f"as a new version of record {settings['zenodo_record']} ...")
            url = zenodo_upload.new_version(
                settings["zenodo_record"], files, settings["zenodo_token"],
                log=lambda m: self.log("INFO", m))
            self.status_var.set(
                f"Zenodo upload done in {time.time() - started:.0f}s — {url}")
        except zenodo_upload.ZenodoError as exc:
            self.log("ERROR", f"Zenodo: {exc}")
            self.status_var.set("Zenodo upload failed")
        except Exception as exc:  # noqa: BLE001
            self.log("ERROR", f"{type(exc).__name__}: {exc}")
            self.status_var.set("Zenodo upload failed")
        finally:
            self.root.after(0, self._after_run)

    def _run_export_only(self, settings: dict) -> None:
        started = time.time()
        try:
            db_path = Path(settings["db"])
            if not db_path.is_file():
                raise FileNotFoundError(
                    f"No database at {db_path} — run a sync first.")
            store = Store(db_path)
            username = settings["user"] or store.get_meta("username") or "unknown"
            export_dir = Path(settings["export_dir"])
            zip_dir = Path(settings["zip_dir"]) if settings["zip_dir"] else None
            zip_path = inat2dwc.export_dwca(store, export_dir, username, zip_dir)
            if zip_path:
                self.log("INFO", f"Archive written to {zip_path}")
            self.status_var.set(f"Export done in {time.time() - started:.0f}s — {export_dir}")
        except Exception as exc:  # noqa: BLE001
            self.log("ERROR", f"{type(exc).__name__}: {exc}")
            self.status_var.set("Failed")
        finally:
            self.root.after(0, self._after_run)

    def _run_media_sync(self, settings: dict) -> None:
        started = time.time()
        logging.getLogger("inat2dwc").setLevel(
            logging.DEBUG if settings["verbose"] else logging.INFO)
        media_dir = Path(settings["media_dir"])
        try:
            store = Store(Path(settings["db"]))
            rows = media_sync.media_rows(store)
            if not rows:
                raise FileNotFoundError(
                    "The database holds no media records — run a sync first.")
            self.log("INFO", f"Media sync: checking {len(rows)} media records "
                              f"against {media_dir} ...")
            missing = media_sync.find_missing(
                store, media_dir, cancel_event=self.cancel_event,
                log_fn=lambda m: self.log("INFO", m))
            # Record files downloaded before download bookkeeping existed.
            media_sync.backfill_media_files(
                store, media_dir, cancel_event=self.cancel_event,
                log_fn=lambda m: self.log("INFO", m))
            if not missing:
                self.log("INFO", f"Media sync: all {len(rows)} files are "
                                 f"already present in {media_dir}.")
                self.status_var.set(f"Media up to date — {media_dir}")
                return
            photos = sum(1 for r in missing if r.get("kind") == "photo")
            sounds = sum(1 for r in missing if r.get("kind") == "sound")
            self.log("INFO", f"Media sync: {len(missing)} of {len(rows)} files "
                             f"are missing ({photos} photos, {sounds} sounds).")
            proceed = self._ask_yes(
                f"{len(missing)} of {len(rows)} media files are not yet in\n"
                f"{media_dir}\n\n"
                f"({photos} photos, {sounds} sounds)\n\n"
                "Download the missing files now?")
            if not proceed:
                self.log("INFO", "Media sync: download declined — "
                                 "nothing downloaded.")
                self.status_var.set("Media sync cancelled")
                return
            downloaded, failed = media_sync.download_media(
                missing, media_dir, store=store,
                cancel_event=self.cancel_event,
                log_fn=lambda m: self.log("INFO", m))
            if failed:
                self.log("WARNING", f"Media sync: {failed} file(s) could not "
                                    "be downloaded (see log above).")
            self.log("INFO", f"Media sync: {downloaded} file(s) downloaded "
                             f"to {media_dir} ({failed} failed).")
            self.status_var.set(
                f"Media sync done in {time.time() - started:.0f}s — "
                f"{downloaded} downloaded, {failed} failed")
        except Cancelled:
            self.log("WARNING", "Media sync cancelled. Files already "
                                "downloaded are kept.")
            self.status_var.set("Cancelled")
        except Exception as exc:  # noqa: BLE001
            self.log("ERROR", f"{type(exc).__name__}: {exc}")
            self.status_var.set("Failed")
        finally:
            self.root.after(0, self._after_run)

    def _after_run(self) -> None:
        self._refresh_last_sync()
        self._set_running(False)


def main() -> None:
    ensure_data_layout()
    settings = load_settings()
    if HAVE_TTKB:
        root = tb.Window(title=APP_TITLE,
                         themename="darkly" if settings["dark"] else "flatly")
    else:
        root = Tk()
        root.title(APP_TITLE)
    InatGui(root, settings)
    root.mainloop()


if __name__ == "__main__":
    main()

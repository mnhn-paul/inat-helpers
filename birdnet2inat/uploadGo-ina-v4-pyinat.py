#!/usr/bin/env python3
"""iNaturalist Audio Observation Uploader (v5)

Required:
    pip install pyinaturalist tzdata
Optional (each feature switches off gracefully if missing):
    pip install tkcalendar       # calendar popup next to the date field
    pip install ttkbootstrap     # themes / dark mode
    pip install tkinterdnd2      # drag & drop of files and folders
    pip install matplotlib numpy # spectrogram preview
    ffmpeg on PATH               # decoding for preview, flac -> m4a conversion

Filename formats understood (everything stays editable in the UI):
    1) erithacus_rubecula_42p_20261018T095003.m4a   species_conf p_timestamp, LOCAL time
    2) 15356-20261018-165454.flac                   id-date-time, UTC, no species
"""
from __future__ import annotations

import base64
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
import webbrowser
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

from pyinaturalist import create_observation, get_taxa_autocomplete, upload

try:
    import ttkbootstrap as tb
    HAVE_TTKB = True
except ImportError:
    tb = None
    HAVE_TTKB = False

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAVE_DND = True
except ImportError:
    HAVE_DND = False

try:
    import numpy as np
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
    HAVE_PLOT = True
except ImportError:
    HAVE_PLOT = False

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
APP_TITLE = "iNaturalist Audio Observation Uploader"
LOCAL_TZ_NAME = "Europe/Luxembourg"
try:
    LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
except ZoneInfoNotFoundError:
    raise SystemExit(
        "Time zone data not found (common on Windows).\n"
        "Fix: pip install tzdata   (use the same Python that runs this script)"
    )

INAT_SITE = "https://www.inaturalist.lu"
INAT_API_ME = "https://api.inaturalist.org/v1/users/me"
CONFIG_PATH = Path.home() / ".inat_audio_uploader.json"
INAT_OK_EXT = {".m4a", ".mp3", ".wav", ".aac"}          # sent as-is
SCAN_EXT = INAT_OK_EXT | {".flac", ".ogg", ".opus"}      # accepted in the queue
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
MAX_PREVIEW_S = 180
HISTORY_MAX = 15

DEFAULT_CONFIG = {
    "locations": {"Home": {"lat": "49.5965", "lon": "6.1423", "acc": "20"}},
    "last_location": "Home",
    "tags": "sound recording, BirdNET-Go, BirdNET",
    "last_dir": "",
    "uploaded": {},            # filename -> observation id
    "species_history": [],     # [{id, name, preferred_common_name, rank}]
    "species_only": True,
    "move_uploaded": False,
    "dark": True,
    "font_size": 10,
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    # migrate v4 config (single location)
    if "latitude" in stored and "locations" not in stored:
        stored["locations"] = {"Home": {"lat": str(stored.get("latitude")),
                                        "lon": str(stored.get("longitude")),
                                        "acc": str(stored.get("accuracy", "20"))}}
        stored["last_location"] = "Home"
    for k in ("latitude", "longitude", "accuracy"):
        stored.pop(k, None)
    cfg.update(stored)
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------
# Filename parsing / time
# --------------------------------------------------------------------------
@dataclass
class ParsedName:
    species: str | None
    confidence: int | None
    stamp: datetime        # naive, exactly as written in the filename
    tz_kind: str           # "local" or "utc"


BIRDNET_RE = re.compile(
    r"^(?P<sp>[a-z][a-z_\-]*?)_(?P<conf>\d{1,3})p_(?P<ts>\d{8}T\d{6})Z?\.\w+$", re.IGNORECASE)
RECORDER_RE = re.compile(r"^(?P<id>\d+)-(?P<date>\d{8})-(?P<time>\d{6})\.\w+$")


def parse_filename(filename: str) -> ParsedName | None:
    m = BIRDNET_RE.match(filename)
    if m:
        try:
            stamp = datetime.strptime(m["ts"], "%Y%m%dT%H%M%S")
        except ValueError:
            return None
        words = m["sp"].replace("_", " ").replace("-", " ").split()
        species = " ".join(words).capitalize() if words else None
        return ParsedName(species, int(m["conf"]), stamp, "local")
    m = RECORDER_RE.match(filename)
    if m:
        try:
            stamp = datetime.strptime(m["date"] + m["time"], "%Y%m%d%H%M%S")
        except ValueError:
            return None
        return ParsedName(None, None, stamp, "utc")
    return None


def to_local(stamp: datetime, tz_kind: str) -> datetime:
    """Naive datetime in Luxembourg local time."""
    if tz_kind == "utc":
        return stamp.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).replace(tzinfo=None)
    return stamp


# --------------------------------------------------------------------------
# Coordinates from pasted map links
# --------------------------------------------------------------------------
COORD_PATTERNS = [
    r"@(-?\d+\.\d+),(-?\d+\.\d+)",                          # google .../@lat,lon,17z
    r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)",                      # google place data
    r"#map=\d+/(-?\d+\.\d+)/(-?\d+\.\d+)",                  # openstreetmap
    r"mlat=(-?\d+\.\d+)&mlon=(-?\d+\.\d+)",                 # openstreetmap marker
    r"[?&](?:q|ll|query)=(-?\d+\.\d+)(?:,|%2C)(-?\d+\.\d+)",
    r"^\s*(-?\d+(?:\.\d+)?)\s*[,;\s]\s*(-?\d+(?:\.\d+)?)\s*$",  # plain "lat, lon"
]


def parse_coords(text: str) -> tuple[float, float] | None:
    for pat in COORD_PATTERNS:
        m = re.search(pat, text)
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon
    return None


# --------------------------------------------------------------------------
# Token helpers
# --------------------------------------------------------------------------
def jwt_expiry(token: str) -> datetime | None:
    """Expiry (UTC) read from the JWT payload, or None if unreadable."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return datetime.fromtimestamp(
            json.loads(base64.urlsafe_b64decode(payload))["exp"], tz=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# Audio helpers
# --------------------------------------------------------------------------
def convert_to_m4a(src: Path, out_dir: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(f"{src.suffix} is not accepted by iNaturalist and ffmpeg "
                           "was not found on PATH.")
    dst = out_dir / (src.stem + ".m4a")
    cmd = [ffmpeg, "-y", "-i", str(src), "-vn", "-map_metadata", "0",
           "-c:a", "aac", "-b:a", "192k", str(dst)]
    res = subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW)
    if res.returncode != 0 or not dst.exists():
        raise RuntimeError(f"ffmpeg failed:\n{res.stderr[-500:]}")
    return dst


def decode_to_wav(src: Path, out_dir: str, key: str) -> str:
    """Mono 16-bit wav copy for preview/playback."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        if src.suffix.lower() == ".wav":
            return str(src)
        raise RuntimeError("ffmpeg not found - needed to preview this format")
    dst = Path(out_dir) / f"{key}.wav"
    cmd = [ffmpeg, "-y", "-i", str(src), "-vn", "-ac", "1", "-c:a", "pcm_s16le",
           "-t", str(MAX_PREVIEW_S), str(dst)]
    res = subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW)
    if res.returncode != 0 or not dst.exists():
        raise RuntimeError(f"ffmpeg decode failed: {res.stderr[-200:]}")
    return str(dst)


def wav_info(path: str):
    """(samples as float32 mono array or None, sample rate, duration seconds)."""
    with wave.open(path) as w:
        sr, n, ch, width = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
        dur = n / sr if sr else 0.0
        if not HAVE_PLOT:
            return None, sr, dur
        if width != 2:
            raise RuntimeError("only 16-bit wav supported for preview")
        raw = w.readframes(min(n, sr * MAX_PREVIEW_S))
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        data = data[: len(data) // ch * ch].reshape(-1, ch).mean(axis=1)
    return data, sr, dur


class Player:
    """Plays a wav file without extra Python dependencies."""

    def __init__(self):
        self.proc = None
        self._win = False

    def play(self, wav: str) -> bool:
        self.stop()
        if sys.platform.startswith("win"):
            import winsound
            winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
            self._win = True
            return True
        for exe, args in (("ffplay", ["-nodisp", "-autoexit", "-loglevel", "quiet"]),
                          ("afplay", []), ("aplay", ["-q"])):
            path = shutil.which(exe)
            if path:
                self.proc = subprocess.Popen([path, *args, wav], stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL)
                return True
        return False

    def stop(self):
        if self._win:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
            self._win = False
        if self.proc:
            try:
                self.proc.terminate()
            except OSError:
                pass
            self.proc = None


def extract_id(response) -> int | None:
    if isinstance(response, list) and response:
        response = response[0]
    if isinstance(response, dict):
        return response.get("id")
    return getattr(response, "id", None)


# --------------------------------------------------------------------------
# Queue item
# --------------------------------------------------------------------------
@dataclass
class Item:
    iid: str
    path: Path
    parsed: ParsedName | None
    tz_kind: str
    local_time: datetime
    species_query: str = ""
    confidence: int | None = None
    taxon: dict | None = None
    candidates: list = field(default_factory=list)
    lat: str = ""
    lon: str = ""
    acc: str = ""
    tags: str = ""
    description: str = ""
    desc_edited: bool = False
    status: str = "pending"       # pending | uploading | uploaded | failed | duplicate
    obs_id: int | None = None
    error: str = ""

    @property
    def taxon_name(self) -> str:
        return self.taxon["name"] if self.taxon else ""

    def state_label(self) -> str:
        if self.status in ("uploading", "uploaded", "failed", "duplicate"):
            return self.status
        return "ready" if self.taxon else "needs species"


def default_description(it: Item) -> str:
    name = it.taxon_name
    if it.confidence is not None:
        return ("Automatic recording via BirdNET-Go. Initial ID via BirdNET soundscape "
                f"analyzer: *{name or '?'}*, at a confidence of {it.confidence}%.")
    if name:
        return f"Sound recording. Identification: *{name}*."
    return "Sound recording."


def render_template(template: str, it: Item) -> str:
    if "{confidence}" in template and it.confidence is None:
        return default_description(it)
    return (template.replace("{species}", it.taxon_name or "?")
            .replace("{confidence}", str(it.confidence)))



class DateField(ttk.Frame):
    """yyyy-mm-dd entry with -/+ day buttons and an optional calendar popup.

    Replaces tkcalendar.DateEntry, which crashes under ttkbootstrap.
    """

    def __init__(self, master, width: int = 11):
        super().__init__(master)
        self.var = tk.StringVar()
        self.entry = ttk.Entry(self, textvariable=self.var, width=width)
        ttk.Button(self, text="<", width=2, command=lambda: self._shift(-1)).pack(side=tk.LEFT)
        self.entry.pack(side=tk.LEFT, padx=2)
        ttk.Button(self, text=">", width=2, command=lambda: self._shift(1)).pack(side=tk.LEFT)
        ttk.Button(self, text="...", width=3, command=self._popup).pack(side=tk.LEFT, padx=(4, 0))

    def get_date(self) -> date:
        return datetime.strptime(self.var.get().strip(), "%Y-%m-%d").date()

    def set_date(self, d: date):
        self.var.set(d.strftime("%Y-%m-%d"))

    def bind(self, sequence=None, func=None, add=None):
        return self.entry.bind(sequence, func, add)

    def _changed(self):
        self.entry.event_generate("<<DateEntrySelected>>")

    def _shift(self, days: int):
        try:
            cur = self.get_date()
        except ValueError:
            cur = date.today()
        self.set_date(cur + timedelta(days=days))
        self._changed()

    def _popup(self):
        top = None
        try:
            from tkcalendar import Calendar
            try:
                cur = self.get_date()
            except ValueError:
                cur = date.today()
            top = tk.Toplevel(self)
            top.title("Pick date")
            top.transient(self.winfo_toplevel())
            cal = Calendar(top, selectmode="day", year=cur.year, month=cur.month, day=cur.day,
                           date_pattern="yyyy-mm-dd")
            cal.pack(padx=8, pady=8)

            def pick(_e=None):
                self.set_date(cal.selection_get())
                top.destroy()
                self._changed()
            cal.bind("<<CalendarSelected>>", pick)
        except Exception as e:  # noqa: BLE001
            if top is not None:
                top.destroy()
            messagebox.showinfo("Calendar unavailable",
                                f"Use the < > buttons or type the date.\n({e})")

# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
BaseWin = tb.Window if HAVE_TTKB else tk.Tk
STATUS_COLORS = {"ready": None, "pending": "#e0a800", "uploaded": "#2fb344",
                 "failed": "#d63939", "duplicate": "#8a8a8a", "uploading": "#4299e1"}


class INatUploaderApp(BaseWin):
    def __init__(self):
        cfg = load_config()
        if HAVE_TTKB:
            super().__init__(title=APP_TITLE, themename="darkly" if cfg["dark"] else "flatly")
            self.theme_style = tb.Style()
        else:
            super().__init__()
            self.title(APP_TITLE)
            self.theme_style = ttk.Style()
        self.cfg = cfg
        self.geometry("1320x980")
        self.minsize(1000, 700)

        self.items: dict[str, Item] = {}
        self.counter = 0
        self.current: Item | None = None
        self._loading = False
        self._sort_rev: dict[str, bool] = {}

        self.busy = False
        self.cancel_event = threading.Event()
        self.search_q: queue.Queue = queue.Queue()
        self.search_cache: dict[str, list] = {}

        self.tmp_dir = tempfile.mkdtemp(prefix="inat_preview_")
        self.wav_cache: dict[str, str] = {}
        self.player = Player()
        self.cur_wav: str | None = None
        self.audio_dur = 0.0
        self.playing = False
        self.play_start = 0.0
        self.spec_data = None
        self.spec_msg = "Select a file to preview"
        self.playhead = None

        self._build_toolbar()
        self._build_bottom()
        main = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        left, right = ttk.Frame(main), ttk.Frame(main)
        main.add(left, weight=2)
        main.add(right, weight=3)
        self._build_queue(left)
        self._build_details(right)

        self.set_font_size(self.cfg["font_size"])
        self.apply_colors()
        self.draw_spectrogram()
        self._setup_dnd([self.tree, left, right])
        self.update_token_status()
        self.after(30000, self._token_timer)
        threading.Thread(target=self._search_loop, daemon=True).start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- layout ----------------------------------------------------------
    def _build_toolbar(self):
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, padx=8, pady=(8, 2))
        ttk.Label(bar, text="API token:").pack(side=tk.LEFT)
        self.token_var = tk.StringVar(value=os.environ.get("INAT_TOKEN", ""))
        self.token_var.trace_add("write", lambda *_: self.update_token_status())
        ttk.Entry(bar, textvariable=self.token_var, width=44, show="*").pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="Test", command=self.test_token).pack(side=tk.LEFT)
        ttk.Button(bar, text="Get token",
                   command=lambda: webbrowser.open(f"{INAT_SITE}/users/api_token")
                   ).pack(side=tk.LEFT, padx=4)
        self.lbl_token = ttk.Label(bar, text="")
        self.lbl_token.pack(side=tk.LEFT, padx=8)

        ttk.Button(bar, text="A+", width=3,
                   command=lambda: self.set_font_size(self.cfg["font_size"] + 1)).pack(side=tk.RIGHT)
        ttk.Button(bar, text="A-", width=3,
                   command=lambda: self.set_font_size(self.cfg["font_size"] - 1)
                   ).pack(side=tk.RIGHT, padx=2)
        theme_btn = ttk.Button(bar, text="Dark / light", command=self.toggle_theme)
        theme_btn.pack(side=tk.RIGHT, padx=6)
        if not HAVE_TTKB:
            theme_btn.config(state=tk.DISABLED)

    def _build_bottom(self):
        bottom = ttk.Frame(self)
        bottom.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(0, 8))
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(fill=tk.X, pady=(4, 2))
        self.lbl_status = ttk.Label(bottom, text="Ready")
        self.lbl_status.pack(anchor="w")
        self.log_box = scrolledtext.ScrolledText(bottom, height=5, state=tk.DISABLED)
        self.log_box.pack(fill=tk.X, pady=(2, 0))

    def _build_queue(self, parent):
        btns = ttk.Frame(parent)
        btns.pack(fill=tk.X, pady=(0, 4))
        ttk.Button(btns, text="Add files...", command=self.add_files_dialog).pack(side=tk.LEFT)
        ttk.Button(btns, text="Add folder...", command=self.add_folder_dialog
                   ).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Remove selected", command=self.remove_selected).pack(side=tk.LEFT)
        ttk.Button(btns, text="Clear uploaded", command=self.clear_uploaded
                   ).pack(side=tk.LEFT, padx=4)

        hint = ("Drop audio files or folders here" if HAVE_DND
                else "(pip install tkinterdnd2 for drag & drop)")
        ttk.Label(parent, text=hint, foreground="gray").pack(anchor="w")

        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True)
        cols = ("status", "file", "species", "conf", "time")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")
        for col, text, width in (("status", "Status", 95), ("file", "File", 230),
                                 ("species", "Species", 170), ("conf", "Conf", 50),
                                 ("time", "Local time", 140)):
            self.tree.heading(col, text=text, command=lambda c=col: self.sort_by(c))
            self.tree.column(col, width=width, anchor="w", stretch=(col == "file"))
        sb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        for tag, color in STATUS_COLORS.items():
            if color:
                self.tree.tag_configure(tag, foreground=color)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Double-1>", self.on_tree_double)

        self.move_var = tk.BooleanVar(value=self.cfg["move_uploaded"])
        ttk.Checkbutton(parent, text="Move uploaded files to an 'uploaded' subfolder",
                        variable=self.move_var).pack(anchor="w", pady=(6, 2))

        act = ttk.Frame(parent)
        act.pack(fill=tk.X, pady=4)
        self.btn_up_sel = ttk.Button(act, text="Upload selected",
                                     command=lambda: self.start_upload("selected"))
        self.btn_up_sel.pack(side=tk.LEFT)
        self.btn_up_all = ttk.Button(act, text="Upload all ready",
                                     command=lambda: self.start_upload("all"))
        self.btn_up_all.pack(side=tk.LEFT, padx=4)
        self.btn_cancel = ttk.Button(act, text="Cancel", command=self.cancel_upload,
                                     state=tk.DISABLED)
        self.btn_cancel.pack(side=tk.LEFT)
        ttk.Label(act, text="(double-click an uploaded row to open it)",
                  foreground="gray").pack(side=tk.LEFT, padx=8)

    def _build_details(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(3, weight=1)

        # Species
        sf = ttk.LabelFrame(parent, text="Species")
        sf.grid(row=0, column=0, columnspan=2, sticky="ew", pady=3)
        sf.columnconfigure(1, weight=1)
        ttk.Label(sf, text="Search:").grid(row=0, column=0, sticky="e", padx=4)
        self.species_var = tk.StringVar()
        ent = ttk.Entry(sf, textvariable=self.species_var)
        ent.grid(row=0, column=1, sticky="ew", padx=4, pady=2)
        ent.bind("<Return>", lambda e: self.manual_search())
        ttk.Button(sf, text="Search", command=self.manual_search).grid(row=0, column=2)
        self.species_only = tk.BooleanVar(value=self.cfg["species_only"])
        ttk.Checkbutton(sf, text="Species rank only", variable=self.species_only
                        ).grid(row=0, column=3, padx=6)
        self.taxa_list = tk.Listbox(sf, height=4, exportselection=False)
        self.taxa_list.grid(row=1, column=0, columnspan=4, sticky="ew", padx=4, pady=2)
        self.taxa_list.bind("<<ListboxSelect>>", self.on_taxon_selected)
        ttk.Label(sf, text="Recent:").grid(row=2, column=0, sticky="e", padx=4)
        self.hist_combo = ttk.Combobox(sf, state="readonly")
        self.hist_combo.grid(row=2, column=1, columnspan=3, sticky="ew", padx=4, pady=2)
        self.hist_combo.bind("<<ComboboxSelected>>", self.on_history_selected)
        self.refresh_history_combo()
        self.lbl_species = ttk.Label(sf, text="Species: none")
        self.lbl_species.grid(row=3, column=0, columnspan=4, sticky="w", padx=4, pady=2)

        # Time
        tf = ttk.LabelFrame(parent, text="Date and time")
        tf.grid(row=1, column=0, sticky="nsew", pady=3, padx=(0, 3))
        ttk.Label(tf, text="Local date:").grid(row=0, column=0, sticky="e", padx=4, pady=2)
        self.date_picker = DateField(tf, width=11)
        self.date_picker.grid(row=0, column=1, columnspan=3, sticky="w", pady=2)
        for ev in ("<<DateEntrySelected>>", "<FocusOut>", "<Return>", "<KeyRelease>"):
            self.date_picker.bind(ev, lambda e: self.commit_form(), add="+")
        ttk.Label(tf, text="Time:").grid(row=1, column=0, sticky="e", padx=4)
        self.h_var, self.m_var, self.s_var = (tk.StringVar(value="00") for _ in range(3))
        for col, (var, hi) in enumerate(((self.h_var, 23), (self.m_var, 59), (self.s_var, 59)),
                                        start=1):
            ttk.Spinbox(tf, from_=0, to=hi, width=3, format="%02.0f", wrap=True,
                        textvariable=var).grid(row=1, column=col, sticky="w")
            var.trace_add("write", self._on_var_change)
        self.tz_var = tk.StringVar(value="local")
        ttk.Label(tf, text="Filename time is:").grid(row=2, column=0, columnspan=2,
                                                      sticky="e", padx=4, pady=(6, 0))
        ttk.Radiobutton(tf, text="Local", variable=self.tz_var, value="local",
                        command=self.on_tz_changed).grid(row=2, column=2, pady=(6, 0))
        ttk.Radiobutton(tf, text="UTC", variable=self.tz_var, value="utc",
                        command=self.on_tz_changed).grid(row=2, column=3, pady=(6, 0))
        self.lbl_tz = ttk.Label(tf, text="", foreground="gray", wraplength=330)
        self.lbl_tz.grid(row=3, column=0, columnspan=4, sticky="w", padx=4, pady=2)

        # Location
        lf = ttk.LabelFrame(parent, text="Location")
        lf.grid(row=1, column=1, sticky="nsew", pady=3, padx=(3, 0))
        lf.columnconfigure(1, weight=1)
        ttk.Label(lf, text="Preset:").grid(row=0, column=0, sticky="e", padx=4, pady=2)
        self.preset_combo = ttk.Combobox(lf, state="readonly", width=16)
        self.preset_combo.grid(row=0, column=1, sticky="ew", pady=2)
        self.preset_combo.bind("<<ComboboxSelected>>", self.on_preset_selected)
        ttk.Button(lf, text="Save", width=5, command=self.save_preset).grid(row=0, column=2, padx=2)
        ttk.Button(lf, text="Del", width=4, command=self.delete_preset).grid(row=0, column=3)
        self.lat_var, self.lon_var, self.acc_var = (tk.StringVar() for _ in range(3))
        for r, (label, var) in enumerate((("Latitude:", self.lat_var),
                                          ("Longitude:", self.lon_var),
                                          ("Accuracy (m):", self.acc_var)), start=1):
            ttk.Label(lf, text=label).grid(row=r, column=0, sticky="e", padx=4, pady=1)
            ttk.Entry(lf, textvariable=var, width=14).grid(row=r, column=1, sticky="w")
            var.trace_add("write", self._on_var_change)
        ttk.Label(lf, text="Paste map link:").grid(row=4, column=0, sticky="e", padx=4, pady=2)
        self.link_var = tk.StringVar()
        ttk.Entry(lf, textvariable=self.link_var).grid(row=4, column=1, sticky="ew")
        ttk.Button(lf, text="Use", width=5, command=self.use_link).grid(row=4, column=2, padx=2)

        # Tags / description / bulk
        df = ttk.LabelFrame(parent, text="Tags and description")
        df.grid(row=2, column=0, columnspan=2, sticky="ew", pady=3)
        df.columnconfigure(1, weight=1)
        ttk.Label(df, text="Tags:").grid(row=0, column=0, sticky="e", padx=4, pady=2)
        self.tags_var = tk.StringVar()
        self.tags_var.trace_add("write", self._on_var_change)
        ttk.Entry(df, textvariable=self.tags_var).grid(row=0, column=1, columnspan=2,
                                                        sticky="ew", padx=4)
        ttk.Label(df, text="Description:").grid(row=1, column=0, sticky="ne", padx=4, pady=2)
        self.txt_desc = tk.Text(df, height=3, wrap=tk.WORD)
        self.txt_desc.grid(row=1, column=1, columnspan=2, sticky="ew", padx=4, pady=2)
        self.txt_desc.bind("<KeyRelease>", self.on_desc_key)
        ttk.Button(df, text="Reset description", command=self.reset_description
                   ).grid(row=2, column=1, sticky="w", padx=4, pady=2)
        self.bulk_btn = ttk.Menubutton(df, text="Apply current to selected rows")
        menu = tk.Menu(self.bulk_btn, tearoff=False)
        for label, kind in (("Location (lat / lon / accuracy)", "location"),
                            ("Tags", "tags"), ("Description (per-species text)", "description"),
                            ("Time-zone interpretation", "tz"), ("All of the above", "all")):
            menu.add_command(label=label, command=lambda k=kind: self.bulk(k))
        self.bulk_btn["menu"] = menu
        self.bulk_btn.grid(row=2, column=2, sticky="e", padx=4)

        # Preview
        pf = ttk.LabelFrame(parent, text="Audio preview")
        pf.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=3)
        ctl = ttk.Frame(pf)
        ctl.pack(side=tk.BOTTOM, fill=tk.X, pady=2)
        self.btn_play = ttk.Button(ctl, text="Play", width=8, command=self.toggle_play,
                                   state=tk.DISABLED)
        self.btn_play.pack(side=tk.LEFT, padx=4)
        self.lbl_audio = ttk.Label(ctl, text="")
        self.lbl_audio.pack(side=tk.LEFT, padx=6)
        if HAVE_PLOT:
            self.fig = Figure(figsize=(6, 2.4), dpi=100)
            self.ax = self.fig.add_subplot(111)
            self.fig.subplots_adjust(left=0.08, right=0.99, top=0.95, bottom=0.2)
            self.canvas = FigureCanvasTkAgg(self.fig, master=pf)
            self.canvas.get_tk_widget().configure(highlightthickness=0)
            self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        else:
            ttk.Label(pf, text="pip install matplotlib numpy for the spectrogram "
                               "(playback still works)", foreground="gray"
                      ).pack(fill=tk.BOTH, expand=True)

    # ---- theme / fonts ----------------------------------------------------
    def set_font_size(self, size: int):
        size = max(8, min(20, size))
        self.cfg["font_size"] = size
        for name in ("TkDefaultFont", "TkTextFont", "TkFixedFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(size=size)
            except tk.TclError:
                pass
        self.theme_style.configure("Treeview", rowheight=int(size * 2.4))

    def toggle_theme(self):
        if not HAVE_TTKB:
            return
        self.cfg["dark"] = not self.cfg["dark"]
        self.theme_style.theme_use("darkly" if self.cfg["dark"] else "flatly")
        self.apply_colors()
        self.set_font_size(self.cfg["font_size"])
        self.draw_spectrogram()

    def theme_colors(self):
        bg = self.theme_style.lookup("TEntry", "fieldbackground") or "white"
        fg = self.theme_style.lookup("TEntry", "foreground") or "black"
        panel = self.theme_style.lookup("TFrame", "background") or bg
        return bg, fg, panel

    def apply_colors(self):
        bg, fg, _ = self.theme_colors()
        for w in (self.taxa_list, self.txt_desc, self.log_box):
            w.config(background=bg, foreground=fg)
        for w in (self.txt_desc, self.log_box):
            w.config(insertbackground=fg)

    # ---- misc helpers -------------------------------------------------------
    def log(self, msg: str):
        self.log_box.config(state=tk.NORMAL)
        self.log_box.insert(tk.END, msg + "\n")
        self.log_box.see(tk.END)
        self.log_box.config(state=tk.DISABLED)

    def ui(self, fn, *args):
        self.after(0, lambda: fn(*args))

    def _on_close(self):
        if self.busy and not messagebox.askyesno("Upload running", "An upload is running. Quit anyway?"):
            return
        self.cancel_event.set()
        self.commit_form()
        self.cfg["move_uploaded"] = self.move_var.get()
        self.cfg["species_only"] = self.species_only.get()
        save_config(self.cfg)
        self.player.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        self.destroy()

    # ---- token --------------------------------------------------------------
    def update_token_status(self):
        token = self.token_var.get().strip()
        if not token:
            self.lbl_token.config(text="no token", foreground="gray")
            return
        exp = jwt_expiry(token)
        if exp is None:
            self.lbl_token.config(text="expiry unknown (use Test)", foreground="gray")
            return
        left = (exp - datetime.now(timezone.utc)).total_seconds()
        if left <= 0:
            self.lbl_token.config(text="token EXPIRED - get a new one", foreground="#d63939")
        elif left < 1800:
            self.lbl_token.config(text=f"expires in {int(left // 60)} min", foreground="#e0a800")
        else:
            self.lbl_token.config(text=f"valid for ~{int(left // 3600)}h {int(left % 3600 // 60)}m",
                                  foreground="#2fb344")

    def _token_timer(self):
        self.update_token_status()
        self.after(30000, self._token_timer)

    def test_token(self):
        token = self.token_var.get().strip()
        if not token:
            messagebox.showerror("No token", "Enter your API token first.")
            return
        self.lbl_token.config(text="testing...", foreground="gray")
        threading.Thread(target=self._test_token_worker, args=(token,), daemon=True).start()

    def _test_token_worker(self, token: str):
        try:
            req = urllib.request.Request(INAT_API_ME, headers={
                "Authorization": f"Bearer {token}", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                login = json.load(r)["results"][0]["login"]
            self.ui(self._token_test_done, True, f"Token OK - logged in as {login}")
        except urllib.error.HTTPError as e:
            msg = "Token rejected (expired or invalid)" if e.code in (401, 403) else f"HTTP {e.code}"
            self.ui(self._token_test_done, False, msg)
        except Exception as e:  # noqa: BLE001
            self.ui(self._token_test_done, False, f"Test failed: {e}")

    def _token_test_done(self, ok: bool, msg: str):
        self.log(msg)
        self.lbl_token.config(text=msg, foreground="#2fb344" if ok else "#d63939")
        self.after(8000, self.update_token_status)

    # ---- drag & drop ----------------------------------------------------------
    def _setup_dnd(self, widgets):
        self.dnd_ok = False
        if not HAVE_DND:
            return
        try:
            TkinterDnD._require(self)
            for w in widgets:
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self.on_drop)
            self.dnd_ok = True
        except Exception as e:  # noqa: BLE001
            self.log(f"Drag & drop unavailable: {e}")

    def on_drop(self, event):
        self.add_paths(self.tk.splitlist(event.data))

    # ---- queue management -------------------------------------------------------
    def add_files_dialog(self):
        paths = filedialog.askopenfilenames(
            initialdir=self.cfg.get("last_dir") or None,
            filetypes=[("Audio files", " ".join(f"*{e}" for e in sorted(SCAN_EXT))),
                       ("All files", "*.*")])
        if paths:
            self.add_paths(paths)

    def add_folder_dialog(self):
        folder = filedialog.askdirectory(initialdir=self.cfg.get("last_dir") or None)
        if folder:
            self.add_paths([folder])

    def default_location(self) -> dict:
        locs = self.cfg["locations"]
        name = self.cfg.get("last_location")
        if name in locs:
            return locs[name]
        if locs:
            return next(iter(locs.values()))
        return {"lat": "49.5965", "lon": "6.1423", "acc": "20"}

    def add_paths(self, paths):
        files: list[Path] = []
        for p in map(Path, paths):
            if p.is_dir():
                files += sorted(q for q in p.iterdir() if q.is_file() and q.suffix.lower() in SCAN_EXT)
            elif p.suffix.lower() in SCAN_EXT:
                files.append(p)
            else:
                self.log(f"Skipped (not audio): {p.name}")
        if files:
            self.cfg["last_dir"] = str(files[0].parent)
        existing = {it.path for it in self.items.values()}
        added = [self.make_item(f) for f in files if f not in existing]
        self.log(f"Added {len(added)} file(s) ({len(files) - len(added)} already in queue).")
        if added and not self.tree.selection():
            self.tree.selection_set(added[0].iid)

    def make_item(self, path: Path) -> Item:
        parsed = parse_filename(path.name)
        if parsed:
            tz, local = parsed.tz_kind, to_local(parsed.stamp, parsed.tz_kind)
        else:
            tz = "local"
            local = datetime.fromtimestamp(path.stat().st_mtime).replace(microsecond=0)
            self.log(f"{path.name}: no timestamp in filename, using file modified time.")
        loc = self.default_location()
        self.counter += 1
        it = Item(iid=f"i{self.counter}", path=path, parsed=parsed, tz_kind=tz, local_time=local,
                  species_query=(parsed.species or "") if parsed else "",
                  confidence=parsed.confidence if parsed else None,
                  lat=loc["lat"], lon=loc["lon"], acc=loc["acc"], tags=self.cfg["tags"])
        it.description = default_description(it)
        if path.name in self.cfg["uploaded"]:
            it.status = "duplicate"
            it.obs_id = self.cfg["uploaded"][path.name]
        self.items[it.iid] = it
        self.tree.insert("", tk.END, iid=it.iid, values=self.row_values(it),
                         tags=(self.row_tag(it),))
        if it.species_query:
            self.search_q.put((it.iid, it.species_query, False))
        return it

    def species_text(self, it: Item) -> str:
        if it.taxon:
            return it.taxon_name
        return f"? {it.species_query}" if it.species_query else "-"

    def row_values(self, it: Item):
        return (it.state_label(), it.path.name, self.species_text(it),
                f"{it.confidence}%" if it.confidence is not None else "",
                it.local_time.strftime("%Y-%m-%d %H:%M:%S"))

    def row_tag(self, it: Item) -> str:
        return it.status if it.status in ("uploading", "uploaded", "failed", "duplicate") \
            else ("ready" if it.taxon else "pending")

    def refresh_row(self, it: Item):
        if self.tree.exists(it.iid):
            self.tree.item(it.iid, values=self.row_values(it), tags=(self.row_tag(it),))

    def remove_selected(self):
        sel = [i for i in self.tree.selection() if self.items[i].status != "uploading"]
        if not sel:
            return
        self.current = None
        for iid in sel:
            self.tree.delete(iid)
            self.items.pop(iid, None)
        self.on_select()

    def clear_uploaded(self):
        done = [i for i, it in self.items.items() if it.status == "uploaded"]
        if self.current and self.current.iid in done:
            self.current = None
        for iid in done:
            self.tree.delete(iid)
            self.items.pop(iid, None)
        self.on_select()

    def sort_by(self, col: str):
        rev = self._sort_rev.get(col, False)

        def key(iid):
            it = self.items[iid]
            return {"status": it.state_label(), "file": it.path.name.lower(),
                    "species": self.species_text(it).lower(),
                    "conf": it.confidence if it.confidence is not None else -1,
                    "time": it.local_time}[col]
        for i, iid in enumerate(sorted(self.tree.get_children(), key=key, reverse=rev)):
            self.tree.move(iid, "", i)
        self._sort_rev[col] = not rev

    def on_tree_double(self, event):
        iid = self.tree.identify_row(event.y)
        it = self.items.get(iid)
        if it and it.obs_id and it.status in ("uploaded", "duplicate"):
            webbrowser.open(f"{INAT_SITE}/observations/{it.obs_id}")

    # ---- selection <-> form ------------------------------------------------------
    def on_select(self, _event=None):
        self.commit_form()
        sel = self.tree.selection()
        focus = self.tree.focus()
        iid = focus if focus in sel else (sel[0] if sel else None)
        self.load_item(self.items.get(iid) if iid else None)

    def _on_var_change(self, *_):
        self.commit_form()

    def commit_form(self):
        it = self.current
        if it is None or self._loading:
            return
        it.species_query = self.species_var.get().strip()
        try:
            d = self.date_picker.get_date()
            it.local_time = datetime(d.year, d.month, d.day, int(self.h_var.get()),
                                     int(self.m_var.get()), int(self.s_var.get()))
        except (ValueError, TypeError):
            pass
        it.lat, it.lon, it.acc = (self.lat_var.get().strip(), self.lon_var.get().strip(),
                                  self.acc_var.get().strip())
        it.tags = self.tags_var.get().strip()
        self.cfg["tags"] = it.tags or self.cfg["tags"]
        self.update_preset_combo(it)
        self.refresh_row(it)

    def on_desc_key(self, _event=None):
        it = self.current
        if it is None:
            return
        it.description = self.txt_desc.get("1.0", "end-1c").strip()
        it.desc_edited = it.description != default_description(it)

    def reset_description(self):
        it = self.current
        if it:
            it.description = default_description(it)
            it.desc_edited = False
            self.show_description(it)

    def show_description(self, it: Item):
        self.txt_desc.delete("1.0", tk.END)
        self.txt_desc.insert(tk.END, it.description)

    def set_time_widgets(self, lt: datetime):
        self.date_picker.set_date(lt.date())
        self.h_var.set(f"{lt.hour:02d}")
        self.m_var.set(f"{lt.minute:02d}")
        self.s_var.set(f"{lt.second:02d}")

    def update_tz_label(self, it: Item):
        if it.parsed:
            raw = it.parsed.stamp.strftime("%Y-%m-%d %H:%M:%S")
            mode = "UTC" if it.tz_kind == "utc" else "local time"
            self.lbl_tz.config(text=f"Filename time {raw} read as {mode} -> "
                                    f"{it.local_time:%Y-%m-%d %H:%M:%S} {LOCAL_TZ_NAME}")
        else:
            self.lbl_tz.config(text="No timestamp in filename - set the time manually.")

    def load_item(self, it: Item | None):
        self.stop_audio()
        self._loading = True
        try:
            self.current = it
            if it is None:
                self.species_var.set("")
                for v in (self.lat_var, self.lon_var, self.acc_var, self.tags_var):
                    v.set("")
                self.taxa_list.delete(0, tk.END)
                self.txt_desc.delete("1.0", tk.END)
                self.lbl_species.config(text="Species: none")
                self.lbl_tz.config(text="")
                self.preset_combo.set("")
                self.btn_play.config(state=tk.DISABLED)
                self.spec_data, self.spec_msg, self.cur_wav = None, "Select a file to preview", None
                self.draw_spectrogram()
                return
            self.species_var.set(it.species_query or it.taxon_name)
            self.fill_candidates(it)
            self.set_time_widgets(it.local_time)
            self.tz_var.set(it.tz_kind)
            self.update_tz_label(it)
            self.lat_var.set(it.lat)
            self.lon_var.set(it.lon)
            self.acc_var.set(it.acc)
            self.update_preset_combo(it)
            self.tags_var.set(it.tags)
            self.show_description(it)
        finally:
            self._loading = False
        if it is not None:
            self.load_preview(it)

    # ---- species search ---------------------------------------------------------------
    def manual_search(self):
        it = self.current
        if it is None:
            return
        self.commit_form()
        q = it.species_query
        if not q:
            messagebox.showerror("Missing name", "Enter a species name to search.")
            return
        self.log(f"Searching for '{q}'...")
        self.search_q.put((it.iid, q, True))

    def _search_loop(self):
        while True:
            iid, q, force = self.search_q.get()
            try:
                key = q.lower()
                if key not in self.search_cache:
                    res = get_taxa_autocomplete(q=q, per_page=20)
                    self.search_cache[key] = res.get("results", []) if isinstance(res, dict) else []
                self.ui(self._apply_search, iid, q, self.search_cache[key], force, None)
            except Exception as e:  # noqa: BLE001
                self.ui(self._apply_search, iid, q, [], force, e)

    def _apply_search(self, iid: str, q: str, raw: list, force: bool, err):
        it = self.items.get(iid)
        if it is None:
            return
        if err:
            self.log(f"Search error for '{q}': {err}")
            return
        results = [t for t in raw if t.get("rank") == "species"] if self.species_only.get() else list(raw)
        it.candidates = results
        if results and (force or it.taxon is None):
            self.set_taxon(it, results[0], from_user=False)
        elif not results:
            if force:
                it.taxon = None
            hint = " (try unticking 'Species rank only')" if raw and self.species_only.get() else ""
            self.log(f"No match for '{q}'{hint}")
        self.refresh_row(it)
        if self.current is it:
            self.fill_candidates(it)

    def set_taxon(self, it: Item, taxon: dict, from_user: bool):
        it.taxon = taxon
        if not it.desc_edited:
            it.description = default_description(it)
        if from_user:
            self.add_history(taxon)
        self.refresh_row(it)
        if self.current is it:
            self.show_description(it)
            self.update_taxon_label(it)

    @staticmethod
    def taxon_label(t: dict) -> str:
        return f"{t.get('name')}  -  {t.get('preferred_common_name') or ''}  [{t.get('rank')}]  (id {t.get('id')})"

    def update_taxon_label(self, it: Item):
        if it.taxon:
            common = it.taxon.get("preferred_common_name")
            self.lbl_species.config(text=f"Selected: {it.taxon_name}"
                                         f"{f' ({common})' if common else ''}"
                                         f" - taxon_id {it.taxon.get('id')}")
        else:
            self.lbl_species.config(text="Species: none")

    def fill_candidates(self, it: Item):
        self.taxa_list.delete(0, tk.END)
        for t in it.candidates:
            self.taxa_list.insert(tk.END, self.taxon_label(t))
        if it.taxon:
            for i, t in enumerate(it.candidates):
                if t.get("id") == it.taxon.get("id"):
                    self.taxa_list.selection_set(i)
                    break
        self.update_taxon_label(it)

    def on_taxon_selected(self, _event=None):
        it, sel = self.current, self.taxa_list.curselection()
        if it and sel and sel[0] < len(it.candidates):
            self.set_taxon(it, it.candidates[sel[0]], from_user=True)

    def refresh_history_combo(self):
        self.hist_combo["values"] = [
            f"{h['name']} - {h.get('preferred_common_name') or ''}" for h in self.cfg["species_history"]]

    def add_history(self, taxon: dict):
        entry = {k: taxon.get(k) for k in ("id", "name", "preferred_common_name", "rank")}
        hist = [h for h in self.cfg["species_history"] if h.get("id") != entry["id"]]
        self.cfg["species_history"] = ([entry] + hist)[:HISTORY_MAX]
        self.refresh_history_combo()

    def on_history_selected(self, _event=None):
        it, idx = self.current, self.hist_combo.current()
        if it is None or idx < 0:
            return
        taxon = self.cfg["species_history"][idx]
        self.species_var.set(taxon["name"])
        it.species_query = taxon["name"]
        self.taxa_list.selection_clear(0, tk.END)
        self.set_taxon(it, dict(taxon), from_user=True)

    # ---- location presets ---------------------------------------------------------------
    def _same_loc(self, loc: dict, it: Item) -> bool:
        try:
            return (abs(float(loc["lat"]) - float(it.lat)) < 1e-9 and
                    abs(float(loc["lon"]) - float(it.lon)) < 1e-9 and
                    abs(float(loc["acc"]) - float(it.acc)) < 1e-9)
        except ValueError:
            return False

    def update_preset_combo(self, it: Item):
        locs = self.cfg["locations"]
        self.preset_combo["values"] = sorted(locs)
        name = next((n for n, loc in locs.items() if self._same_loc(loc, it)), "(custom)")
        self.preset_combo.set(name)

    def on_preset_selected(self, _event=None):
        name = self.preset_combo.get()
        loc = self.cfg["locations"].get(name)
        if loc:
            self.cfg["last_location"] = name
            self.lat_var.set(loc["lat"])
            self.lon_var.set(loc["lon"])
            self.acc_var.set(loc["acc"])
            self.commit_form()

    def save_preset(self):
        try:
            lat, lon = float(self.lat_var.get()), float(self.lon_var.get())
            acc = float(self.acc_var.get() or 20)
            assert -90 <= lat <= 90 and -180 <= lon <= 180
        except (ValueError, AssertionError):
            messagebox.showerror("Invalid location", "Enter valid coordinates first.")
            return
        name = simpledialog.askstring("Save location", "Preset name:", parent=self)
        if name and name.strip():
            self.cfg["locations"][name.strip()] = {"lat": self.lat_var.get().strip(),
                                                   "lon": self.lon_var.get().strip(),
                                                   "acc": f"{acc:g}"}
            self.cfg["last_location"] = name.strip()
            if self.current:
                self.update_preset_combo(self.current)

    def delete_preset(self):
        name = self.preset_combo.get()
        if name in self.cfg["locations"] and messagebox.askyesno("Delete preset", f"Delete '{name}'?"):
            del self.cfg["locations"][name]
            if self.current:
                self.update_preset_combo(self.current)

    def use_link(self):
        coords = parse_coords(self.link_var.get())
        if not coords:
            messagebox.showinfo(
                "No coordinates found",
                "Could not find coordinates. Paste a full Google Maps / OpenStreetMap URL "
                "(short links like maps.app.goo.gl are not expanded) or 'lat, lon'.")
            return
        self.lat_var.set(f"{coords[0]:.6f}")
        self.lon_var.set(f"{coords[1]:.6f}")
        self.link_var.set("")

    # ---- time zone / bulk -----------------------------------------------------------------
    def on_tz_changed(self):
        it = self.current
        if it is None:
            return
        if it.parsed:
            it.tz_kind = self.tz_var.get()
            it.local_time = to_local(it.parsed.stamp, it.tz_kind)
            self._loading = True
            try:
                self.set_time_widgets(it.local_time)
            finally:
                self._loading = False
            self.refresh_row(it)
        self.update_tz_label(it)

    def bulk(self, kind: str):
        base = self.current
        targets = [self.items[i] for i in self.tree.selection() if i in self.items]
        if base is None or not targets:
            return
        self.commit_form()
        kinds = {"location", "tags", "description", "tz"} if kind == "all" else {kind}
        template = base.description
        if base.taxon_name:
            template = template.replace(base.taxon_name, "{species}")
        if base.confidence is not None:
            template = template.replace(f"confidence of {base.confidence}%", "confidence of {confidence}%")
        for it in targets:
            if "location" in kinds:
                it.lat, it.lon, it.acc = base.lat, base.lon, base.acc
            if "tags" in kinds:
                it.tags = base.tags
            if "description" in kinds:
                it.description = render_template(template, it)
                it.desc_edited = it.description != default_description(it)
            if "tz" in kinds and it.parsed:
                it.tz_kind = self.tz_var.get()
                it.local_time = to_local(it.parsed.stamp, it.tz_kind)
            self.refresh_row(it)
        self.log(f"Applied {', '.join(sorted(kinds))} to {len(targets)} row(s).")
        self.load_item(base)

    # ---- preview ------------------------------------------------------------------------
    def load_preview(self, it: Item):
        self.spec_data, self.cur_wav = None, None
        self.spec_msg = "Loading preview..."
        self.btn_play.config(state=tk.DISABLED)
        self.lbl_audio.config(text="")
        self.draw_spectrogram()
        threading.Thread(target=self._decode_worker, args=(it.iid, it.path), daemon=True).start()

    def _decode_worker(self, iid: str, path: Path):
        try:
            wav = self.wav_cache.get(iid)
            if not wav or not Path(wav).exists():
                wav = decode_to_wav(path, self.tmp_dir, iid)
                self.wav_cache[iid] = wav
            data, sr, dur = wav_info(wav)
            self.ui(self._preview_ready, iid, wav, data, sr, dur, None)
        except Exception as e:  # noqa: BLE001
            self.ui(self._preview_ready, iid, None, None, 0, 0.0, str(e))

    def _preview_ready(self, iid, wav, data, sr, dur, err):
        if self.current is None or self.current.iid != iid:
            return
        if err:
            self.spec_msg = f"No preview: {err}"
            self.draw_spectrogram()
            return
        self.cur_wav, self.audio_dur = wav, dur
        self.spec_data = (data, sr) if data is not None else None
        self.spec_msg = "Spectrogram unavailable"
        self.btn_play.config(state=tk.NORMAL)
        self.lbl_audio.config(text=f"{dur:.1f} s")
        self.draw_spectrogram()

    def draw_spectrogram(self):
        if not HAVE_PLOT:
            return
        bg, fg, panel = self.theme_colors()
        self.fig.set_facecolor(panel)
        self.ax.clear()
        self.ax.set_facecolor(panel)
        self.playhead = None
        if self.spec_data is None:
            self.ax.set_axis_off()
            self.ax.text(0.5, 0.5, self.spec_msg, ha="center", va="center", color=fg,
                         transform=self.ax.transAxes)
        else:
            self.ax.set_axis_on()
            data, sr = self.spec_data
            if len(data) < 1024:
                data = np.pad(data, (0, 1024 - len(data)))
            self.ax.specgram(data, NFFT=1024, Fs=sr, noverlap=768, cmap="magma")
            self.ax.set_ylim(0, min(sr / 2, 12000))
            self.ax.set_xlabel("seconds", color=fg)
            self.ax.set_ylabel("Hz", color=fg)
            self.ax.tick_params(colors=fg)
            for spine in self.ax.spines.values():
                spine.set_color(fg)
            self.playhead = self.ax.axvline(0, color="cyan", lw=1.2, visible=False)
        self.canvas.draw_idle()

    def toggle_play(self):
        if self.playing:
            self.stop_audio()
            return
        if not self.cur_wav:
            return
        if not self.player.play(self.cur_wav):
            self.log("No audio player found (on Linux/macOS install ffmpeg for ffplay).")
            return
        self.playing = True
        self.play_start = time.monotonic()
        self.btn_play.config(text="Stop")
        self.tick_playhead()

    def tick_playhead(self):
        if not self.playing:
            return
        t = time.monotonic() - self.play_start
        if t >= self.audio_dur:
            self.stop_audio()
            return
        if HAVE_PLOT and self.playhead is not None:
            self.playhead.set_xdata([t, t])
            self.playhead.set_visible(True)
            self.canvas.draw_idle()
        self.after(120, self.tick_playhead)

    def stop_audio(self):
        self.player.stop()
        self.playing = False
        self.btn_play.config(text="Play")
        if HAVE_PLOT and self.playhead is not None:
            self.playhead.set_visible(False)
            self.canvas.draw_idle()

    # ---- upload -------------------------------------------------------------------------
    def set_busy(self, busy: bool):
        self.busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        self.btn_up_sel.config(state=state)
        self.btn_up_all.config(state=state)
        self.btn_cancel.config(state=tk.NORMAL if busy else tk.DISABLED)

    def build_params(self, it: Item) -> dict:
        try:
            lat = float(it.lat.replace(",", "."))
            lon = float(it.lon.replace(",", "."))
            acc = int(float(it.acc.replace(",", ".")))
        except ValueError:
            raise ValueError("latitude, longitude and accuracy must be numbers")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180 and acc >= 0):
            raise ValueError("coordinates or accuracy out of range")
        return dict(path=it.path, taxon_id=it.taxon["id"], observed=it.local_time,
                    lat=lat, lon=lon, acc=acc, description=it.description,
                    tags=it.tags, obs_id=it.obs_id)

    def start_upload(self, which: str):
        if self.busy:
            return
        self.commit_form()
        if which == "selected":
            cands = [self.items[i] for i in self.tree.selection() if i in self.items]
            dups = [it for it in cands if it.status == "duplicate"]
            if dups and not messagebox.askyesno(
                    "Already uploaded",
                    f"{len(dups)} selected file(s) were uploaded before. Upload them again?"):
                cands = [it for it in cands if it.status != "duplicate"]
        else:
            cands = [self.items[i] for i in self.tree.get_children()
                     if self.items[i].status not in ("uploaded", "duplicate")]
        cands = [it for it in cands if it.status != "uploaded"]

        jobs = []
        for it in cands:
            if not it.taxon:
                self.log(f"Skipped {it.path.name}: no species selected.")
                continue
            try:
                jobs.append((it.iid, self.build_params(it)))
            except ValueError as e:
                it.status, it.error = "failed", str(e)
                self.refresh_row(it)
                self.log(f"Skipped {it.path.name}: {e}")
        if not jobs:
            messagebox.showinfo("Nothing to upload", "No ready files to upload (see the log).")
            return

        token = self.token_var.get().strip()
        if not token:
            messagebox.showerror("No token", "Please enter your iNaturalist API token.")
            return
        exp = jwt_expiry(token)
        if exp is not None:
            left = (exp - datetime.now(timezone.utc)).total_seconds()
            if left <= 0:
                messagebox.showerror("Token expired", "Your token has expired. Get a new one.")
                return
            if left < 900 and not messagebox.askyesno(
                    "Token expires soon", f"Your token expires in {int(left // 60)} min. Continue?"):
                return

        self.cfg["move_uploaded"] = self.move_var.get()
        self.cancel_event.clear()
        self.set_busy(True)
        self.progress.config(maximum=len(jobs), value=0)
        self.log(f"Starting upload of {len(jobs)} file(s)...")
        threading.Thread(target=self._upload_loop, args=(jobs, token), daemon=True).start()

    def cancel_upload(self):
        self.cancel_event.set()
        self.lbl_status.config(text="Cancelling after the current file...")

    def _upload_loop(self, jobs, token):
        ok = failed = 0
        for n, (iid, p) in enumerate(jobs, start=1):
            if self.cancel_event.is_set():
                break
            self.ui(self._item_started, iid, n, len(jobs))
            obs_id, tmp = p["obs_id"], None
            try:
                audio: Path = p["path"]
                if audio.suffix.lower() not in INAT_OK_EXT:
                    tmp = tempfile.mkdtemp(prefix="inat_audio_")
                    self.ui(self.log, f"Converting {audio.name} to m4a...")
                    audio = convert_to_m4a(audio, Path(tmp))
                if not obs_id:   # a retry after a failed sound upload reuses the observation
                    response = create_observation(
                        taxon_id=p["taxon_id"], observed_on_string=p["observed"].isoformat(),
                        time_zone=LOCAL_TZ_NAME, description=p["description"],
                        tag_list=p["tags"], latitude=p["lat"], longitude=p["lon"],
                        positional_accuracy=p["acc"], access_token=token)
                    obs_id = extract_id(response)
                    if not obs_id:
                        raise RuntimeError(f"No observation ID in response: {response}")
                    self.ui(self._item_created, iid, obs_id)
                upload(obs_id, sounds=[str(audio)], access_token=token)
                ok += 1
                self.ui(self._item_done, iid, obs_id, None)
            except Exception as e:  # noqa: BLE001
                failed += 1
                self.ui(self._item_done, iid, obs_id, e)
            finally:
                if tmp:
                    shutil.rmtree(tmp, ignore_errors=True)
        self.ui(self._run_finished, ok, failed, self.cancel_event.is_set())

    def _item_started(self, iid, n, total):
        it = self.items.get(iid)
        if it:
            it.status = "uploading"
            self.refresh_row(it)
            self.lbl_status.config(text=f"Uploading {n}/{total}: {it.path.name}")

    def _item_created(self, iid, obs_id):
        it = self.items.get(iid)
        if it:
            it.obs_id = obs_id

    def _item_done(self, iid, obs_id, err):
        self.progress.step(1)
        it = self.items.get(iid)
        if it is None:
            return
        if err:
            it.status, it.error = "failed", str(err)
            extra = (f" Observation {obs_id} exists; a retry will only re-upload the sound."
                     if obs_id else "")
            self.log(f"FAILED {it.path.name}: {err}.{extra}")
        else:
            it.status, it.obs_id, it.error = "uploaded", obs_id, ""
            self.cfg["uploaded"][it.path.name] = obs_id
            if it.taxon:
                self.add_history(it.taxon)
            self.log(f"Uploaded {it.path.name} -> {INAT_SITE}/observations/{obs_id}")
            if self.move_var.get():
                self.move_uploaded_file(it)
        self.refresh_row(it)

    def move_uploaded_file(self, it: Item):
        try:
            dest_dir = it.path.parent / "uploaded"
            dest_dir.mkdir(exist_ok=True)
            dest = dest_dir / it.path.name
            k = 1
            while dest.exists():
                dest = dest_dir / f"{it.path.stem}_{k}{it.path.suffix}"
                k += 1
            shutil.move(str(it.path), str(dest))
            it.path = dest
        except OSError as e:
            self.log(f"Could not move {it.path.name}: {e}")

    def _run_finished(self, ok, failed, cancelled):
        self.set_busy(False)
        for it in self.items.values():       # items skipped by a cancel go back to normal
            if it.status == "uploading":
                it.status = "pending"
                self.refresh_row(it)
        save_config(self.cfg)
        summary = f"Done: {ok} uploaded, {failed} failed" + (" (cancelled)" if cancelled else "")
        self.lbl_status.config(text=summary)
        self.log(summary)


if __name__ == "__main__":
    INatUploaderApp().mainloop()

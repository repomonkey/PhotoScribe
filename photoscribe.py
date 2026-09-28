#!/usr/bin/env python3
"""
PhotoScribe - AI-Powered Photo Metadata Generator
Uses local Ollama models (Gemma 3) to analyse photographs and generate
title, caption, and keywords, then writes them directly to IPTC/XMP metadata.

Requires: Python 3.10+, PySide6, Pillow, requests, rawpy, exiftool (system)
"""

import sys
import os
import re
import json
import base64
import threading
import time
import subprocess
import shutil
from pathlib import Path
from io import BytesIO
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime as _datetime

import requests
from PIL import Image

# RAW support (optional but recommended)
try:
    import rawpy
    HAS_RAWPY = True
except ImportError:
    HAS_RAWPY = False

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()  # Lets PIL open .heic/.heif (iPhone photos)
    HAS_HEIF = True
except ImportError:
    HAS_HEIF = False


# On Windows, each child process (exiftool, etc.) would pop a console window
# that steals focus from whatever you're doing. These wrappers suppress it;
# on other platforms creationflags=0 is a harmless no-op.
_real_run = subprocess.run
_real_popen = subprocess.Popen


def _run(*args, **kwargs):
    if sys.platform == "win32":
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
    return _real_run(*args, **kwargs)


def _popen(*args, **kwargs):
    if sys.platform == "win32":
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
    return _real_popen(*args, **kwargs)


# Single source of truth for the app version (the build reads this too).
APP_VERSION = "1.7.0"
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QTextEdit, QLineEdit, QComboBox,
    QProgressBar, QScrollArea, QFrame, QSplitter, QGroupBox, QCheckBox,
    QFileDialog, QMessageBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QTabWidget, QSpinBox, QMenu, QToolButton, QSizePolicy, QPlainTextEdit,
    QAbstractItemView, QStyledItemDelegate, QStyle
)
from PySide6.QtCore import (
    Qt, Signal, QThread, QSize, QMimeData, QTimer, QSettings, QUrl, QRectF
)
from PySide6.QtGui import (
    QPixmap, QImage, QDragEnterEvent, QDropEvent, QFont, QColor,
    QPalette, QIcon, QAction, QPainter, QFontDatabase, QPen, QPainterPath
)


# ─────────────────────────────────────────────────────────
# Supported file formats
# ─────────────────────────────────────────────────────────

SUPPORTED_EXTENSIONS = {
    # Standard image formats
    ".jpg", ".jpeg", ".tif", ".tiff", ".png", ".webp",
    ".heic", ".heif",
    # RAW formats
    ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".raf",
    ".rw2", ".pef", ".srw", ".x3f", ".3fr", ".mrw", ".nrw",
    ".raw", ".sr2", ".srf", ".erf",
}

RAW_EXTENSIONS = {
    ".cr2", ".cr3", ".nef", ".arw", ".orf", ".raf", ".rw2",
    ".dng", ".pef", ".srw", ".x3f", ".3fr", ".mrw", ".nrw",
    ".raw", ".sr2", ".srf", ".erf",
}


def _is_supported_file(path) -> bool:
    """True for a real, supported image — excludes macOS AppleDouble stubs
    (._foo) and hidden dotfiles, which carry image extensions but aren't photos."""
    name = os.path.basename(path)
    if name.startswith("."):
        return False
    return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS

# ─────────────────────────────────────────────────────────
# Folder context detection
# ─────────────────────────────────────────────────────────

# Patterns for folder name date detection
_FOLDER_DATE_PATTERNS = [
    re.compile(r"^(\d{4})(\d{2})(\d{2})\s*[-–—]\s*(.+)$"),
    re.compile(r"^(\d{4})[-.](\d{2})[-.](\d{2})\s*[-–—]\s*(.+)$"),
    re.compile(r"^(\d{4})(\d{2})(\d{2})\s+(.+)$"),
    re.compile(r"^(\d{4})[-.](\d{2})[-.](\d{2})\s+(.+)$"),
]

@dataclass
class FolderContext:
    date_str: str = ""
    location: str = ""
    raw_folder: str = ""
    subfolder: str = ""

def parse_folder_name(folder_name: str) -> Optional[tuple]:
    """Try all patterns, return (date_str, description) or None."""
    name = folder_name.strip()
    for pattern in _FOLDER_DATE_PATTERNS:
        m = pattern.match(name)
        if m:
            year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            description = m.group(4).strip()
            try:
                dt = _datetime(year, month, day)
                date_str = dt.strftime("%-d %B %Y") if sys.platform != "win32" else dt.strftime("%#d %B %Y")
            except ValueError:
                continue
            return date_str, description
    return None

def detect_folder_context(filepath: str, max_levels: int = 5) -> Optional[FolderContext]:
    """Walk up directory tree looking for matching folder."""
    path = Path(filepath).resolve()
    current = path.parent
    subfolder_parts = []
    for _ in range(max_levels):
        folder_name = current.name
        if not folder_name:
            break
        result = parse_folder_name(folder_name)
        if result:
            date_str, description = result
            subfolder = " / ".join(reversed(subfolder_parts)) if subfolder_parts else ""
            return FolderContext(date_str=date_str, location=description, raw_folder=folder_name, subfolder=subfolder)
        else:
            subfolder_parts.append(folder_name)
        current = current.parent
    return None

def detect_batch_folder_context(filepaths: list) -> Optional[FolderContext]:
    """Return most common context from a batch of files."""
    contexts = {}
    for fp in filepaths:
        ctx = detect_folder_context(fp)
        if ctx:
            key = (ctx.date_str, ctx.location)
            if key not in contexts:
                contexts[key] = (ctx, 0)
            contexts[key] = (contexts[key][0], contexts[key][1] + 1)
    if not contexts:
        return None
    best = max(contexts.values(), key=lambda x: x[1])
    return best[0]

# ─────────────────────────────────────────────────────────
# Keyword deduplication
# ─────────────────────────────────────────────────────────

def deduplicate_keywords(keywords: list) -> list:
    """Remove near-duplicate keywords (plurals, case variants).
    Keeps the first occurrence of each unique concept.
    """
    if not keywords:
        return keywords

    def normalise(kw):
        kw = kw.lower().strip()
        if kw.endswith("ies") and len(kw) > 4:
            kw = kw[:-3] + "y"
        elif kw.endswith("es") and len(kw) > 3 and (
            kw.endswith("ches") or kw.endswith("shes") or
            kw.endswith("ses") or kw.endswith("xes") or
            kw.endswith("zes") or kw.endswith("oes")
        ):
            kw = kw[:-2]
        elif kw.endswith("s") and not kw.endswith("ss") and len(kw) > 3:
            kw = kw[:-1]
        return kw

    seen_normalised = {}
    result = []
    for kw in keywords:
        norm = normalise(kw)
        if norm not in seen_normalised:
            seen_normalised[norm] = kw
            result.append(kw)
    return result

# ─────────────────────────────────────────────────────────
# EXIF date reading
# ─────────────────────────────────────────────────────────

def read_exif_date(filepath: str, with_time: bool = False) -> Optional[str]:
    """Read DateTimeOriginal and return it as a human-readable string.

    Checks the file and any co-located .xmp sidecar, for the same reason the
    GPS read does. With `with_time`, the clock time is kept as well — that is
    what tells a model whether it is looking at sunrise or sunset, which it
    often cannot settle from the image alone.
    """
    exiftool = MetadataWriter.find_exiftool()
    if not exiftool:
        return None
    targets = [str(filepath)] + MetadataWriter._sidecar_paths(filepath)
    for tgt in targets:
        try:
            result = _run(
                [exiftool, "-j", "-DateTimeOriginal", tgt],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                continue
            data = json.loads(result.stdout)
            if not data:
                continue
            dto = data[0].get("DateTimeOriginal", "")
            if not dto or ":" not in dto:
                continue
            chunks = str(dto).split(" ")
            parts = chunks[0].split(":")
            if len(parts) != 3:
                continue
            hh = mm = 0
            if with_time and len(chunks) > 1:
                clock = chunks[1].split(":")
                if len(clock) >= 2 and clock[0].isdigit() and clock[1].isdigit():
                    hh, mm = int(clock[0]), int(clock[1])
            dt = _datetime(int(parts[0]), int(parts[1]), int(parts[2]), hh, mm)
            day_fmt = "%#d" if sys.platform == "win32" else "%-d"
            out = dt.strftime(f"{day_fmt} %B %Y")
            if with_time and (hh or mm):
                hour_fmt = "%#I" if sys.platform == "win32" else "%-I"
                out += dt.strftime(f", {hour_fmt}:%M %p").replace("AM", "am").replace("PM", "pm")
            return out
        except Exception:
            continue
    return None

# ─────────────────────────────────────────────────────────
# GPS reverse-geocoding
# ─────────────────────────────────────────────────────────

def read_gps_coordinates(filepath: str) -> Optional[tuple]:
    """Read GPS coordinates from a photo's EXIF/XMP data.

    Checks the file itself AND any co-located .xmp sidecar — RAW files
    (Fuji .RAF etc.) geotagged in Lightroom or Geotag Photos Pro keep the
    GPS in the sidecar, not baked into the raw, so reading only the raw
    silently misses it. Returns (latitude, longitude) as floats, or None.
    """
    exiftool = MetadataWriter.find_exiftool()
    if not exiftool:
        return None
    targets = [str(filepath)] + MetadataWriter._sidecar_paths(filepath)
    for tgt in targets:
        try:
            result = _run(
                [exiftool, "-j", "-n", "-GPSLatitude", "-GPSLongitude", tgt],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                if data:
                    lat = data[0].get("GPSLatitude")
                    lon = data[0].get("GPSLongitude")
                    if lat is not None and lon is not None:
                        return (float(lat), float(lon))
        except Exception:
            continue
    return None


def read_location_fields(filepath: str) -> Optional[str]:
    """Read the human-readable location a cataloguer (e.g. Lightroom) already
    resolved — Sublocation, City, State/Province, Country — from the file AND
    any co-located .xmp sidecar. This is exact and needs no network, so it is
    preferred over GPS reverse-geocoding. Returns a "Sublocation, City, State,
    Country" string (deduped, in that order) or None if nothing is set.
    """
    exiftool = MetadataWriter.find_exiftool()
    if not exiftool:
        return None
    # (json-key, exiftool-tag) in the order we want them to appear.
    fields = [
        ("Sublocation", "-XMP-iptcCore:Location"),
        ("Location", "-IPTC:Sub-location"),
        ("City", "-IPTC:City"),
        ("City", "-XMP-photoshop:City"),
        ("State", "-IPTC:Province-State"),
        ("State", "-XMP-photoshop:State"),
        ("Country", "-IPTC:Country-PrimaryLocationName"),
        ("Country", "-XMP-photoshop:Country"),
    ]
    targets = [str(filepath)] + MetadataWriter._sidecar_paths(filepath)
    # Collect the first non-empty value per conceptual slot, preserving order
    # and de-duplicating (IPTC and XMP usually mirror each other).
    slots = ["", "", "", ""]  # sublocation, city, state, country
    slot_index = {"Sublocation": 0, "Location": 0, "City": 1,
                  "State": 2, "Country": 3}
    args = [exiftool, "-j"] + [tag for _, tag in fields]
    for tgt in targets:
        try:
            result = _run(args + [tgt], capture_output=True, text=True, timeout=10)
            if result.returncode != 0 or not result.stdout:
                continue
            data = json.loads(result.stdout)
            if not data:
                continue
            entry = data[0]
            for jkey, tag in fields:
                idx = slot_index[jkey]
                if slots[idx]:
                    continue
                # exiftool strips the group prefix in -j output; the JSON key
                # is the bare tag name.
                bare = tag.split(":")[-1]
                val = entry.get(bare)
                if val is None:
                    # Some tags share a JSON key (City/State/Country); the
                    # first matching entry wins, which is fine.
                    val = entry.get(jkey)
                if val is not None and str(val).strip():
                    slots[idx] = str(val).strip()
        except Exception:
            continue
    parts, seen = [], set()
    for s in slots:
        if s and s.lower() not in seen:
            seen.add(s.lower())
            parts.append(s)
    return ", ".join(parts) if parts else None


# Nominatim's usage policy allows at most one request per second, and asks
# that results be cached. Photos from one shoot share a location, so caching
# on rounded coordinates (4dp ≈ 11m) turns a whole folder into one request.
_geocode_cache = {}
_geocode_lock = threading.Lock()
_geocode_last_call = 0.0
_GEOCODE_MIN_INTERVAL = 1.0


def reverse_geocode(lat: float, lon: float) -> Optional[str]:
    """Reverse geocode via OpenStreetMap Nominatim (free, no API key).
    NOTE: This makes an external network request. Results are cached and
    rate-limited to one request per second per Nominatim's usage policy.
    Returns place description or None.
    """
    global _geocode_last_call
    key = (round(lat, 4), round(lon, 4))
    with _geocode_lock:
        if key in _geocode_cache:
            return _geocode_cache[key]
        wait = _GEOCODE_MIN_INTERVAL - (time.monotonic() - _geocode_last_call)
        if wait > 0:
            time.sleep(wait)
        place = _reverse_geocode_uncached(lat, lon)
        _geocode_last_call = time.monotonic()
        _geocode_cache[key] = place
        return place


def _reverse_geocode_uncached(lat: float, lon: float) -> Optional[str]:
    try:
        url = (
            f"https://nominatim.openstreetmap.org/reverse"
            f"?lat={lat}&lon={lon}&format=json&zoom=14"
            f"&addressdetails=1&accept-language=en"
        )
        resp = requests.get(url, timeout=10, headers={
            "User-Agent": "PhotoScribe/1.0 (photo metadata tool)"
        })
        resp.raise_for_status()
        data = resp.json()
        addr = data.get("address", {})
        parts = []
        # "locality" and "neighbourhood" matter: rural and coastal places
        # (e.g. Gerroa, NSW) come back only under those keys, and dropping
        # them leaves you with a useless "New South Wales, Australia".
        for key in ["tourism", "natural", "leisure", "amenity",
                    "hamlet", "village", "locality", "neighbourhood",
                    "suburb", "town", "city"]:
            if key in addr:
                parts.append(addr[key])
                break
        for key in ["county", "state_district", "state"]:
            if key in addr and addr[key] not in parts:
                parts.append(addr[key])
                break
        country = addr.get("country", "")
        if country and country not in parts:
            parts.append(country)
        if parts:
            return ", ".join(parts)
        display = data.get("display_name", "")
        if display:
            return ", ".join(display.split(", ")[:3])
    except Exception:
        pass
    return None


def resolve_photo_location(filepath: str) -> Optional[str]:
    """The place this one photo was taken, or None.

    Prefers the place name a cataloguer (e.g. Lightroom) already resolved into
    the file or its .xmp sidecar — exact, and no network request. Only when
    there is no such name does it reverse-geocode the photo's GPS coordinates.
    Resolved per photo, so a folder spanning several places tags each one
    correctly.
    """
    place = read_location_fields(filepath)
    if place:
        return place
    coords = read_gps_coordinates(filepath)
    if coords:
        return reverse_geocode(coords[0], coords[1])
    return None


def resolve_photo_date(filepath: str) -> Optional[str]:
    """When this one photo was taken, or None.

    Resolved per photo for the same reason the location is: sampling the first
    file and pinning its date to the whole batch labels a sunset shot with the
    morning's date the moment a folder spans more than one moment.
    """
    return read_exif_date(filepath, with_time=True)

# ─────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────

@dataclass
class PhotoMetadata:
    title: str = ""
    caption: str = ""
    keywords: list = field(default_factory=list)
    # When "skip if already present" is on and the file already had a
    # title/caption, we show (and keep) the existing value rather than the
    # AI's — these flags let the UI mark it as "kept".
    title_kept: bool = False
    caption_kept: bool = False
    # Ready-to-post text for social platforms, keyed by POST_TARGETS id. Shown
    # in Results with a copy button; never written into the photo.
    posts: dict = field(default_factory=dict)

@dataclass
class PhotoItem:
    filepath: str
    filename: str
    thumbnail: Optional[QPixmap] = None
    metadata: Optional[PhotoMetadata] = None
    status: str = "pending"  # pending, processing, done, error
    error_msg: str = ""


# ─────────────────────────────────────────────────────────
# Output styles
# ─────────────────────────────────────────────────────────

# How the caption written INTO the photo is styled. That field is read by
# Lightroom, Flickr, WordPress and the stock agencies, so these change the
# title, caption and keywords themselves. "catalogue" is the long-standing
# default and adds nothing to the prompt.
FILE_STYLES = [
    ("catalogue", "Catalogue (factual, searchable)", ""),
    ("stock", "Stock (Adobe Stock, Shutterstock, Alamy)",
     "This metadata is for stock photography agencies such as Adobe Stock, "
     "Shutterstock and Alamy, whose buyers find photos by searching. "
     "Title: a plain, literal statement of what the photo shows, up to about "
     "ten words, with no artistic or poetic phrasing. "
     "Caption: one or two factual sentences describing the subject, the "
     "setting and any action, worded the way a buyer would search for it. "
     "Keywords: give 25 to 45 keywords, overriding any smaller number asked "
     "for above, ordered most important first: the main subject, then the "
     "setting and location, then concepts, mood and uses. Use no brand names, "
     "logos or trademarks, and no subjective praise such as 'stunning' or "
     "'beautiful'. If the photo shows identifiable people, recognisable "
     "brands or a news event it can only be licensed as editorial; in that "
     "case begin the caption with the place and date in the form "
     "'[Town], [Region], [Country] - [day month year]: ' and then the factual "
     "description, using only a place and date that were provided to you."),
    ("flickr", "Flickr (read from the file on upload)",
     "This metadata will be uploaded to Flickr, which reads the title, "
     "caption and keywords straight from the file. Title: evocative but "
     "accurate, up to about eight words. Caption: two to four sentences, "
     "warmer and more descriptive than a catalogue entry (the scene, the "
     "light, the mood), as you would write under a photo in a gallery. "
     "Keywords become Flickr tags: give 15 to 25, including any place names "
     "at several levels (town, region, country) as separate tags."),
]
FILE_STYLE_IDS = [sid for sid, _, _ in FILE_STYLES]

# Extra, ready-to-paste text generated alongside the metadata. These platforms
# strip embedded metadata on upload, so writing this into the photo would do
# nothing for them and leave hashtags in the catalogue for good. They live in
# PhotoMetadata.posts and are shown in Results with a copy button instead.
# (id, label, character limit or None, instructions)
POST_TARGETS = [
    ("instagram", "Instagram", 2200,
     "an Instagram caption: open with a short line that makes someone stop "
     "scrolling, follow it with two or three conversational sentences about "
     "the photo, then a blank line and 3 to 5 relevant hashtags built from "
     "the subject and the place."),
    ("facebook", "Facebook", None,
     "a Facebook post: two to four warm, story-led sentences about the "
     "photo, natural rather than promotional, with no more than two "
     "hashtags. It may end with a light question that invites comments."),
    ("threads_bluesky", "Threads / Bluesky", 300,
     "a Threads or Bluesky post: one or two short, punchy sentences, 280 "
     "characters or fewer in total including any hashtags, with no more "
     "than two hashtags."),
    ("mastodon", "Mastodon", 500,
     "a Mastodon post: one to three sentences, then 2 to 4 hashtags written "
     "in CamelCase (#GoldenHour rather than #goldenhour) so screen readers "
     "can say them. 450 characters or fewer in total."),
    ("alt_text", "Alt text", 150,
     "alt text for screen readers: a plain, literal description of what is "
     "visible, 125 characters or fewer, with no hashtags and no opinions, "
     "and not starting with 'Image of' or 'Photo of'."),
]
POST_TARGET_IDS = [pid for pid, _, _, _ in POST_TARGETS]
POST_TARGET_LABELS = {pid: label for pid, label, _, _ in POST_TARGETS}
POST_TARGET_LIMITS = {pid: limit for pid, _, limit, _ in POST_TARGETS}

# ─────────────────────────────────────────────────────────
# Ollama API worker thread
# ─────────────────────────────────────────────────────────

class OllamaWorker(QThread):
    """Processes photos through a local AI backend (Ollama or LM Studio)."""
    progress = Signal(int, str)        # index, status
    result = Signal(int, object)       # index, PhotoMetadata or error string
    finished_all = Signal()
    log_message = Signal(str)

    # At or under this many existing tags, treat them as deliberate (a
    # specialist tagger, or the photographer's own) and trust them. Above it,
    # the library has been through an auto-tagger and they're only hints.
    _DELIBERATE_TAG_LIMIT = 8
    # How many of those hints to show at all — no point pasting in forty.
    _MAX_TAG_HINTS = 12

    # Below this many tokens a second (averaged over the whole request), the
    # model is almost certainly too big for the memory it has and is being
    # read back from disk as it runs. Measured on an M2 Max with 32GB: Gemma 4
    # 12B ran at 33 tok/s; a 27B and a 26B-A4B that didn't fit ran at 3-6.
    _SLOW_TOKENS_PER_SEC = 8.0
    # Needs a reasonably long answer to judge, or start-up time dominates.
    _SLOW_MIN_TOKENS = 60
    # Warn once per session, not once per photo.
    _slow_warned = False

    def __init__(self, photos, model, prompt, context, ollama_url,
                 keywords_list=None, backend="ollama", max_tokens=2048,
                 describe_people=True, skip_existing=False,
                 gps_lookup=False, has_manual_location=False,
                 exif_date=False, has_manual_date=False,
                 file_style="catalogue", post_targets=None, first_person=False,
                 posts_only=False):
        super().__init__()
        self.photos = photos
        self.model = model
        self.prompt = prompt
        self.context = context
        self.ollama_url = ollama_url
        self.keywords_list = keywords_list or []
        self.backend = backend  # "ollama" or "openai"
        self.max_tokens = max_tokens
        self.describe_people = describe_people
        self.skip_existing = skip_existing
        # Location is resolved per photo at generation time (not once at load),
        # so ticking the option or re-running Regenerate takes effect, and a
        # folder spanning several places tags each photo with its own.
        # A location typed into the context fields overrides all of that.
        self.gps_lookup = gps_lookup
        self.has_manual_location = has_manual_location
        # The capture date is resolved per photo for the same reason, so a
        # folder spanning a whole day doesn't get the first frame's timestamp
        # stamped on every shot in it.
        self.exif_date = exif_date
        self.has_manual_date = has_manual_date
        # What the file caption is for, and which posts to write alongside it.
        self.file_style = file_style if file_style in FILE_STYLE_IDS else "catalogue"
        wanted = set(post_targets or [])
        self.post_targets = [pid for pid in POST_TARGET_IDS if pid in wanted]
        self.first_person = first_person
        # Write posts for photos that already have their metadata, without
        # regenerating the title, caption or keywords (Results → Write posts).
        self.posts_only = posts_only and bool(self.post_targets)
        self._last_rate = None
        self._logged_no_location = False
        self._cancelled = False
        self.batch_total_time = 0.0
        self.batch_processed = 0
        self.batch_avg_time = 0.0

    def cancel(self):
        self._cancelled = True

    def _encode_image(self, filepath):
        """Load and resize image for the model. Handles RAW files via rawpy."""
        try:
            ext = Path(filepath).suffix.lower()

            if ext in RAW_EXTENSIONS:
                if not HAS_RAWPY:
                    raise RuntimeError(
                        f"rawpy not installed. Run: pip install rawpy"
                    )
                img = None
                # Prefer the embedded preview JPEG: it's fast and avoids
                # LibRaw postprocess edge cases (some DNGs — phone/linear/HDR —
                # decode dark or blank, which makes the AI return nothing).
                try:
                    with rawpy.imread(filepath) as raw:
                        thumb = raw.extract_thumb()
                    if thumb.format == rawpy.ThumbFormat.JPEG:
                        img = Image.open(BytesIO(thumb.data)).convert("RGB")
                    elif thumb.format == rawpy.ThumbFormat.BITMAP:
                        img = Image.fromarray(thumb.data)
                    # Ignore tiny previews — too small for useful analysis
                    if img is not None and max(img.size) < 512:
                        img = None
                except Exception:
                    img = None
                # Fall back to a full RAW decode if there's no usable preview
                if img is None:
                    with rawpy.imread(filepath) as raw:
                        rgb = raw.postprocess(
                            use_camera_wb=True,
                            half_size=True,  # Faster, plenty for AI analysis
                            no_auto_bright=False,
                        )
                    img = Image.fromarray(rgb)
            else:
                img = Image.open(filepath)
                img = img.convert("RGB")

            # Resize to max 1024px on longest side for efficiency
            max_dim = 1024
            if max(img.size) > max_dim:
                ratio = max_dim / max(img.size)
                new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
                img = img.resize(new_size, Image.BILINEAR)
            buffer = BytesIO()
            img.save(buffer, format="JPEG", quality=85)
            return base64.b64encode(buffer.getvalue()).decode("utf-8")
        except Exception as e:
            raise RuntimeError(f"Failed to load image: {e}")

    def _build_prompt(self, photo=None):
        """Construct the full prompt with context (per-photo, so it can weave in
        any person names already tagged on that specific image)."""
        parts = []

        ctx = self.context.strip()
        if ctx:
            parts.append(f"Context for this photo: {ctx}")

        # Per-photo location, unless the user typed one into the context fields
        # (which then applies to every photo, as an explicit override).
        photo_location = ""
        if photo and self.gps_lookup and not self.has_manual_location:
            photo_location = resolve_photo_location(photo.filepath) or ""
            if photo_location:
                self.log_message.emit(
                    f"Location for {photo.filename}: {photo_location}")
                # Phrase this as an instruction, not a permission. Stating only
                # "taken at X" (or worse, only forbidding other places) leaves
                # the place name in the keywords and out of the prose — the
                # same trap the Lightroom plugin hit. Tell it to use the name.
                parts.append(
                    f"Location: this photo was taken at {photo_location}. This "
                    "is accurate information that has been provided to you, not "
                    "a guess — name this place in the title AND in the caption, "
                    "and include it in the keywords. Use the recognisable place "
                    "name (the town, suburb, or locality) rather than a street "
                    "address or house number."
                )
            elif not self._logged_no_location:
                self._logged_no_location = True
                self.log_message.emit(
                    "Location lookup: no GPS or place name found on "
                    f"{photo.filename} (further misses won't be logged)")

        # Per-photo capture date, on the same terms as the location.
        photo_date = ""
        if photo and self.exif_date and not self.has_manual_date:
            photo_date = resolve_photo_date(photo.filepath) or ""
            if photo_date:
                parts.append(
                    f"This photo was taken on {photo_date}. Use the time of day "
                    "to judge the light — whether this is sunrise or sunset, "
                    "morning or evening — rather than guessing from the image "
                    "alone. Do not put the date itself in the title or caption."
                )

        if ctx or photo_location or photo_date:
            parts.append(
                "This context is factual ground truth about the photo. In "
                "particular, if a Location is given, the photo was taken there "
                "— use that place and never name a different city, region, or "
                "country, even if the scene reminds you of somewhere else. If "
                "no location is given, or you are not certain of the exact "
                "place, describe the scene without naming a specific location "
                "rather than guessing one."
            )

        if self.posts_only:
            # The metadata already exists: give it to the model so the posts
            # stay consistent with it, and ask for nothing else.
            meta = photo.metadata if photo is not None else None
            if meta and (meta.title or meta.caption):
                parts.append(
                    "The metadata for this photo has already been written. "
                    f'Title: "{meta.title}". Caption: "{meta.caption}". '
                    "Keep the posts consistent with it."
                )
        else:
            parts.append(self.prompt.strip())

        # The destination of the file caption. Appended after the user's own
        # prompt so prompt presets keep working: presets describe what is in
        # the photo, this describes where the metadata is going.
        style_text = dict((sid, txt) for sid, _, txt in FILE_STYLES)[self.file_style]
        if style_text and not self.posts_only:
            parts.append(style_text)

        persons = []
        if self.describe_people:
            persons = MetadataWriter.read_persons(photo.filepath) if photo else []
            if persons:
                parts.append(
                    "People named in this photo: " + ", ".join(persons) + ". "
                    "Use these exact names when referring to them in the title and "
                    "caption, and include them in the keywords. Never invent names. "
                    "If other people appear who aren't named, refer to them neutrally "
                    "(e.g. 'another person') without inventing names."
                )
            else:
                parts.append(
                    "If people are visible in the photo, describe their positions, "
                    "roles, and actions (e.g. 'bride and groom exchanging rings', "
                    "'group of hikers on a trail', 'child playing in the sand'). "
                    "Do not attempt to identify individuals by name. Include "
                    "people-related terms in the keywords where relevant."
                )

        # Existing keyword/species tags already on the file (e.g. bird names
        # from a specialist tagger like SuperPicky). Local generalist vision
        # models can't ID species reliably, so a known tag is worth offering.
        #
        # But offer it as a HINT, never as fact, and only a handful. A library
        # that has been through an auto-tagger carries dozens of machine labels
        # ("Rectangle", "Land lot", "Loch", "Caribbean" on a NSW beach). Calling
        # those accurate and asking the model to keep them launders someone
        # else's noise into the user's metadata — and into the caption. The
        # originals are still preserved on the file by "Append keywords to
        # existing", so nothing is lost by letting the model keyword afresh.
        existing_tags = MetadataWriter.read_keywords(photo.filepath) if photo else []
        if persons:
            _pers = {p.lower() for p in persons}
            existing_tags = [t for t in existing_tags if t.lower() not in _pers]
        # How much to trust these depends on how many there are. A specialist
        # tagger (or the photographer) applies a handful of deliberate, high
        # value tags — "Superb Fairywren", "Werri Beach" — which the model
        # genuinely cannot derive and should use. An auto-tagger applies dozens
        # of generic machine labels, and trusting those launders them into the
        # user's metadata. Count is the only reliable signal between the two.
        if existing_tags and len(existing_tags) <= self._DELIBERATE_TAG_LIMIT:
            parts.append(
                "This photo has already been tagged with: "
                + ", ".join(existing_tags) + ". "
                "These were applied deliberately and are likely accurate. Use "
                "the specific ones (species, named places, events) in the title "
                "and caption where they fit what you see — you could not "
                "identify them on your own — and keep them in the keywords. "
                "Ignore any that clearly don't match the image."
            )
        elif existing_tags:
            parts.append(
                "For reference, this photo carries a large number of existing "
                "tags, which look automatically generated and may or may not be "
                "accurate: "
                + ", ".join(existing_tags[:self._MAX_TAG_HINTS]) + ". "
                "Treat them as hints, not as facts. Use one only if it names "
                "something specific that the photo plainly shows. Ignore "
                "generic descriptive labels and anything that doesn't match "
                "what you see. Do not copy this list into the keywords: choose "
                "the keywords yourself, from what is actually in the photo."
            )

        if self.keywords_list and not self.posts_only:
            vocab = ", ".join(self.keywords_list[:200])
            parts.append(
                f"\nWhen generating keywords, prefer terms from this vocabulary "
                f"where applicable: {vocab}"
            )

        if self.post_targets:
            instr = {pid: text for pid, _, _, text in POST_TARGETS}
            lines = "\n".join(f'- "{pid}": {instr[pid]}' for pid in self.post_targets)
            if self.first_person:
                # Phrased as a requirement on EVERY post: asked more gently, a
                # model makes one post first person and lets the rest drift
                # into pronoun-free past tense ("The water was so still").
                # First person invites the model to invent the photographer's
                # life: "I stopped by today" on a 2015 photo, "one of my
                # favourite spots". Reactions to the photo are fine; claims
                # about when, how often, or what happened are not.
                voice = ("Write every post except the alt text as the "
                         "photographer sharing their own photo, in the first "
                         "person: each of those posts must use 'I', 'my' or "
                         "'me' at least once, in your own words and different "
                         "in each post. Use the first person only for "
                         "reactions to the photo, such as what you like about "
                         "its light, colour or composition. Never narrate what "
                         "you did, so no 'I was standing', 'I found', 'I "
                         "stopped', 'I waited' or 'I took this from'. Don't "
                         "say when it was taken relative to now ('today', "
                         "'this evening'), don't claim you were alone, and "
                         "don't claim it's a favourite or regular spot. The "
                         "alt text stays neutral.")
            else:
                voice = ("Write the posts in a neutral voice, without 'I' or "
                         "'we'.")
            intro = ("Write the following ready-to-post text for sharing this "
                     "photo" if self.posts_only else
                     "As well as the title, caption and keywords, write the "
                     "following ready-to-post text for sharing this photo")
            parts.append(
                intro + ', returned in a "posts" object under the key shown:\n'
                + lines + "\n"
                + voice + " The posts are separate from the metadata: never put "
                "hashtags or emoji in the title, caption or keywords. Where a "
                "location is given above, use it in the posts too. Do not "
                "invent anything that isn't shown or given, such as how long "
                "something took, what happened before or after, or anything "
                "about the photographer's experience."
            )

        # Anti-confabulation: small vision models will happily invent a
        # plausible-but-wrong proper noun (a specific bridge/landmark/street
        # name) to fill a gap, and pick a different one each frame. Only let
        # them name specifics that are given above or legible in the image;
        # otherwise stay generic. Better general-and-right than specific-and-wrong.
        parts.append(
            "Only state a specific proper name — of a person, place, landmark, "
            "street, building, event, or species — when it is given in the "
            "information above or clearly legible in the image (e.g. on a sign). "
            "Never guess or invent one. When you are not certain of a specific "
            "name, describe it generically instead (e.g. 'a bridge over a river', "
            "'a historic building', 'a city street') rather than naming it. It is "
            "better to be general and correct than specific and wrong."
        )

        posts_fmt = ('"posts": {'
                     + ", ".join(f'"{pid}": "..."' for pid in self.post_targets)
                     + "}")
        if self.posts_only:
            fmt = "{" + posts_fmt + "}"
        else:
            fmt = ('{"title": "Short descriptive title", '
                   '"caption": "Detailed description of the image in 1-3 sentences", '
                   '"keywords": ["keyword1", "keyword2", "keyword3"]')
            if self.post_targets:
                fmt += ", " + posts_fmt
            fmt += "}"
        parts.append(
            "\nRespond ONLY with valid JSON in this exact format, no other text:\n"
            + fmt
        )
        return "\n\n".join(parts)

    def _clean_keywords(self, raw):
        """Normalise the model's keywords.

        - Snap to the user's exact vocabulary spelling: if a generated keyword
          matches a vocabulary term case-insensitively, use the vocabulary's
          spelling verbatim. This stops Lightroom (which matches keywords as
          case-sensitive strings) treating "sunset" as a new keyword separate
          from your existing "Sunset".
        - Drop blanks and case-insensitive duplicates, keeping first order.
        """
        canon = {v.strip().lower(): v.strip()
                 for v in self.keywords_list if v and v.strip()}
        out, seen = [], set()
        for kw in raw:
            # A model may return a numeric keyword (e.g. a year) as a JSON
            # number — coerce to str so .strip()/.lower() are safe.
            kw = str(kw).strip() if kw is not None else ""
            if not kw:
                continue
            final = canon.get(kw.lower(), kw)
            key = final.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(final)
        return out

    # ── Response parsing (tolerant of small-model malformations) ──

    @staticmethod
    def _json_candidates(text):
        """Yield every brace-balanced {...} substring in a model response, in
        order. A ```json fenced block (if any) is tried first. Scanning all
        objects — not just the first '{' — means a stray brace in a 'thinking'
        preamble can't hide the real JSON that follows."""
        if not text:
            return
        seen = set()

        def scan(s):
            i, n = 0, len(s)
            while i < n:
                if s[i] != "{":
                    i += 1
                    continue
                depth, in_str, esc = 0, False, False
                for j in range(i, n):
                    c = s[j]
                    if in_str:
                        if esc:
                            esc = False
                        elif c == "\\":
                            esc = True
                        elif c == '"':
                            in_str = False
                    elif c == '"':
                        in_str = True
                    elif c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            cand = s[i:j + 1]
                            if cand not in seen:
                                seen.add(cand)
                                yield cand
                            i = j + 1
                            break
                else:
                    # Unbalanced from here (truncated) — last resort span
                    end = s.rfind("}")
                    if end > i:
                        cand = s[i:end + 1]
                        if cand not in seen:
                            seen.add(cand)
                            yield cand
                    return
        s = text.strip()
        m = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL)
        if m:
            yield from scan(m.group(1).strip())
        yield from scan(s)

    @staticmethod
    def _repair_json(candidate, quote_bare_arrays=False):
        """Conservative structural cleanups for near-JSON. Removes
        comments/trailing commas and normalises smart quotes. With
        quote_bare_arrays, also quotes bare words in an unquoted list
        (e.g. [gulls, beach] -> ["gulls","beach"]) — last-resort only, as it
        can touch bracketed text, so it runs after safer attempts fail."""
        t = candidate
        t = re.sub(r"/\*.*?\*/", "", t, flags=re.DOTALL)   # /* block */ comments
        t = re.sub(r"(?m)//.*$", "", t)                     # // line comments
        t = (t.replace("“", '"').replace("”", '"')  # smart double quotes
              .replace("‘", "'").replace("’", "'"))  # smart single quotes
        t = re.sub(r",(\s*[}\]])", r"\1", t)                # trailing commas
        if quote_bare_arrays:
            def _q(m):
                inner = m.group(1).strip()
                if not inner:
                    return "[]"
                parts = [p.strip().replace('"', "") for p in inner.split(",")]
                return "[" + ",".join(f'"{p}"' for p in parts if p) + "]"
            # Only arrays with no quotes/braces inside (untouched if already valid)
            t = re.sub(r"\[([^\[\]{}\"]*?)\]", _q, t)
        return t

    def _parse_response(self, response_text):
        """Return a {title, caption, keywords} dict from a model response, or
        None. Tolerant: for each brace-balanced candidate, try strict JSON,
        then structural repair, then a Python-literal fallback (single-quoted
        dicts), then a bare-array repair. Prefer a dict that looks like our
        metadata (has title/caption/keywords)."""
        import ast
        fallback = None
        for candidate in self._json_candidates(response_text):
            attempts = [
                lambda c=candidate: json.loads(c),
                lambda c=candidate: json.loads(self._repair_json(c)),
                lambda c=candidate: ast.literal_eval(c),
                lambda c=candidate: json.loads(
                    self._repair_json(c, quote_bare_arrays=True)),
            ]
            for attempt in attempts:
                try:
                    data = attempt()
                except Exception:
                    continue
                if isinstance(data, dict):
                    if any(k in data for k in ("title", "caption", "keywords")):
                        return data
                    if fallback is None:
                        fallback = data
        return fallback

    # JSON schema for structured output (LM Studio / OpenAI json_schema mode).
    # Grammar-constrains decoding so the model can only emit this shape — this
    # is what stops smaller models "thinking out loud" in prose instead of
    # returning JSON. The tolerant parser stays as a secondary safety net.
    _JSON_SCHEMA = {
        "name": "photo_metadata",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "caption": {"type": "string"},
                "keywords": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "caption", "keywords"],
        },
    }

    def _check_speed(self):
        """Log a one-off warning when generation is crawling. That almost
        always means the model doesn't fit in memory, which is otherwise
        invisible: nothing errors, it just takes a minute per photo."""
        if OllamaWorker._slow_warned or not self._last_rate:
            return
        tokens, secs = self._last_rate
        if tokens < self._SLOW_MIN_TOKENS or secs <= 0:
            return
        rate = tokens / secs
        if rate >= self._SLOW_TOKENS_PER_SEC:
            return
        OllamaWorker._slow_warned = True
        self.log_message.emit(
            f"Slow generation: {rate:.1f} tokens a second. That usually means "
            f"the model ({self.model}) is too large for the memory available, "
            "so parts of it are read back from disk as it runs. A smaller model "
            "will be several times faster; Recommend Model suggests one that "
            "fits this computer."
        )

    def _schema(self):
        """The structured-output schema for this run. Identical to the base
        schema unless posts were asked for, so the default path is unchanged;
        with posts, each requested platform becomes a required string."""
        if not self.post_targets:
            return self._JSON_SCHEMA
        import copy
        sch = copy.deepcopy(self._JSON_SCHEMA)
        sch["schema"]["properties"]["posts"] = {
            "type": "object",
            "properties": {pid: {"type": "string"} for pid in self.post_targets},
            "required": list(self.post_targets),
        }
        sch["schema"]["required"] = ["title", "caption", "keywords", "posts"]
        if self.posts_only:
            sch["schema"]["properties"] = {"posts": sch["schema"]["properties"]["posts"]}
            sch["schema"]["required"] = ["posts"]
        return sch

    def _call_ollama(self, img_b64, full_prompt):
        """Ollama /api/chat format."""
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a photo metadata generator. Output ONLY a valid JSON object — no reasoning, no explanation, no markdown, no text before or after the JSON.",
                },
                {
                    "role": "user",
                    "content": full_prompt,
                    "images": [img_b64],
                }
            ],
            "stream": False,
            # Constrain output to our schema (Ollama structured outputs). Stops
            # the model returning a prose/bulleted plan instead of JSON.
            "format": self._schema()["schema"],
            "options": {
                "temperature": 0.3,
                "num_predict": self.max_tokens,
            },
            # Ollama's switch for the same problem the OpenAI path solves with
            # reasoning_effort: stop the model burning num_predict on a
            # reasoning pass before it writes any JSON.
            "think": False,
        }
        url = f"{self.ollama_url}/api/chat"
        started = time.monotonic()
        resp = requests.post(url, json=payload, timeout=180)
        # Fall back gracefully if the server rejects the schema in `format`
        # (older Ollama): retry with plain "json", then with no constraint.
        if resp.status_code >= 400:
            payload["format"] = "json"
            resp = requests.post(url, json=payload, timeout=180)
            if resp.status_code >= 400:
                payload.pop("format", None)
                resp = requests.post(url, json=payload, timeout=180)
        resp.raise_for_status()
        body = resp.json()
        # Ollama reports generation time itself; fall back to wall time.
        tokens = body.get("eval_count") or 0
        secs = (body.get("eval_duration") or 0) / 1e9 or (time.monotonic() - started)
        self._last_rate = (tokens, secs)
        return body.get("message", {}).get("content", "")

    def _call_openai(self, img_b64, full_prompt):
        """LM Studio / OpenAI-compatible chat format with vision."""
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a photo metadata generator. Output ONLY a valid JSON object — no reasoning, no explanation, no markdown, no text before or after the JSON.",
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": full_prompt},
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/jpeg;base64,{img_b64}"
                        }},
                    ],
                }
            ],
            "temperature": 0.3,
            "max_tokens": self.max_tokens,
            "stream": False,
            "think": False,
            # Turn off "thinking". Reasoning models (Gemma 4 et al) spend their
            # whole token budget on a reasoning pass before emitting any answer
            # — measured at 840-1200 reasoning tokens for one photo, which
            # overruns max_tokens and truncates the JSON mid-write. This flag
            # takes a request from ~1400 completion tokens to ~140. `think` is
            # the Ollama spelling and is ignored here; this is the OpenAI one.
            "reasoning_effort": "none",
            # Structured output — LM Studio grammar-constrains the model to
            # this schema, so it can't return a prose/bulleted plan. (LM Studio
            # wants json_schema, not the OpenAI json_object type.)
            "response_format": {
                "type": "json_schema",
                "json_schema": self._schema(),
            },
        }
        url = f"{self.ollama_url}/v1/chat/completions"
        started = time.monotonic()
        resp = requests.post(url, json=payload, timeout=180)
        # Drop optional params one at a time if the backend rejects them, so an
        # older or stricter server still works rather than failing outright.
        for param in ("reasoning_effort", "response_format"):
            if resp.status_code == 400 and param in resp.text and param in payload:
                payload.pop(param, None)
                resp = requests.post(url, json=payload, timeout=180)
        resp.raise_for_status()
        body = resp.json()
        # The OpenAI endpoint has no timing, so rate over the whole request.
        self._last_rate = (((body.get("usage") or {}).get("completion_tokens") or 0),
                           time.monotonic() - started)
        choice = (body.get("choices") or [{}])[0]
        msg = choice.get("message", {}) or {}
        content = (msg.get("content") or "").strip()
        # A reasoning model that ran out of budget mid-thought leaves `content`
        # empty and its scratchpad in `reasoning_content`. Returning that prose
        # gets reported as "not valid JSON", which sends people off tuning the
        # model when the real cause is the token ceiling — so say so plainly.
        if not content and choice.get("finish_reason") == "length":
            raise RuntimeError(
                "ran out of tokens before finishing — raise Response length "
                "in Settings (the model spent the budget on reasoning)"
            )
        return content or msg.get("reasoning_content", "")

    def run(self):
        from concurrent.futures import ThreadPoolExecutor

        call_fn = self._call_openai if self.backend == "openai" else self._call_ollama

        # Build list of pending photo indices. Writing posts on demand runs on
        # photos that are already done — that's the point of it.
        pending = [(i, photo) for i, photo in enumerate(self.photos)
                   if self.posts_only or photo.status != "done"]
        if not pending:
            self.finished_all.emit()
            return

        batch_start = time.monotonic()
        per_photo_times = []

        with ThreadPoolExecutor(max_workers=1) as executor:
            # Pre-encode the first image
            next_future = None
            next_idx = 0

            if pending:
                idx0, photo0 = pending[0]
                self.progress.emit(idx0, "processing")
                self.log_message.emit(f"Processing: {photo0.filename}")
                next_future = executor.submit(self._encode_image, photo0.filepath)

            for pos, (i, photo) in enumerate(pending):
                if self._cancelled:
                    break

                # Get the pre-encoded image for this iteration
                try:
                    img_b64 = next_future.result()
                except Exception as e:
                    self.log_message.emit(f"Error: {e}")
                    self.result.emit(i, str(e))
                    # Start encoding next if available
                    if pos + 1 < len(pending):
                        next_i, next_photo = pending[pos + 1]
                        self.progress.emit(next_i, "processing")
                        self.log_message.emit(f"Processing: {next_photo.filename}")
                        next_future = executor.submit(self._encode_image, next_photo.filepath)
                    continue

                # Start encoding the NEXT image while we call the AI
                if pos + 1 < len(pending):
                    next_i, next_photo = pending[pos + 1]
                    self.progress.emit(next_i, "processing")
                    self.log_message.emit(f"Processing: {next_photo.filename}")
                    next_future = executor.submit(self._encode_image, next_photo.filepath)

                # Call the AI model with current image
                try:
                    photo_start = time.monotonic()
                    full_prompt = self._build_prompt(photo)
                    self.log_message.emit(f"Sending to {self.model}...")

                    # Call the model, retrying once if it returns nothing usable.
                    meta = None
                    last_reason = "no response"
                    base_max_tokens = self.max_tokens
                    for attempt in range(2):
                        if self._cancelled:
                            break
                        if attempt > 0:
                            # Retrying with the identical budget just reproduces
                            # a truncated answer, so give the retry more room.
                            self.max_tokens = base_max_tokens * 2
                            self.log_message.emit(
                                "Empty/unparseable response — retrying with a "
                                f"larger token budget ({self.max_tokens})..."
                            )
                        try:
                            response_text = call_fn(img_b64, full_prompt)
                        except RuntimeError as e:
                            last_reason = str(e)
                            self.log_message.emit(f"Model call failed: {e}")
                            continue
                        finally:
                            self.max_tokens = base_max_tokens
                        self.log_message.emit(
                            f"Response received ({len(response_text)} chars)"
                        )

                        if not (response_text or "").strip():
                            last_reason = "the model returned an empty response"
                            continue

                        data = self._parse_response(response_text)
                        if data is None:
                            last_reason = "the response wasn't valid JSON"
                            # Log the raw response so a failure is diagnosable
                            snippet = response_text.strip().replace("\n", " ")
                            if len(snippet) > 800:
                                snippet = snippet[:800] + "…"
                            self.log_message.emit(f"Raw response was: {snippet}")
                            continue

                        posts = {}
                        raw_posts = data.get("posts")
                        if isinstance(raw_posts, dict):
                            for pid in self.post_targets:
                                val = raw_posts.get(pid)
                                if val is not None and str(val).strip():
                                    posts[pid] = str(val).strip()
                        if self.posts_only and not posts:
                            last_reason = "the response had no posts in it"
                            continue
                        missing = [POST_TARGET_LABELS[pid] for pid in self.post_targets
                                   if pid not in posts]
                        if missing:
                            self.log_message.emit(
                                f"No post returned for: {', '.join(missing)}")
                        if self.posts_only:
                            meta = PhotoMetadata(posts=posts)
                        else:
                            meta = PhotoMetadata(
                                title=str(data.get("title") or "").strip(),
                                caption=str(data.get("caption") or "").strip(),
                                keywords=self._clean_keywords(data.get("keywords") or []),
                                posts=posts,
                            )
                        self._check_speed()
                        break

                    if meta is None:
                        raise ValueError(
                            f"Couldn't read metadata — {last_reason}. "
                            f"Try a different model, or increase Keyword density."
                        )

                    # When "skip if already present" is on, the existing
                    # title/caption is what actually gets written — so show that
                    # (marked "kept") in Results, not the AI's discarded one.
                    # (We still generate, because keywords are always produced.)
                    if self.skip_existing and not self.posts_only:
                        e_title, e_caption, _ = \
                            MetadataWriter.read_existing_metadata(photo.filepath)
                        if e_title:
                            meta.title = e_title
                            meta.title_kept = True
                        if e_caption:
                            meta.caption = e_caption
                            meta.caption_kept = True

                    elapsed = time.monotonic() - photo_start
                    per_photo_times.append(elapsed)
                    self.result.emit(i, meta)
                    self.log_message.emit(f"Done: {photo.filename} ({elapsed:.1f}s)")

                except requests.exceptions.ConnectionError:
                    self.log_message.emit("Connection failed — is your AI backend running?")
                    self.result.emit(i, "Cannot connect to AI backend. Check the URL and that it's running.")
                except Exception as e:
                    self.log_message.emit(f"Error: {e}")
                    self.result.emit(i, str(e))

        # Batch timing summary (read by the UI in _on_finished)
        self.batch_total_time = time.monotonic() - batch_start
        self.batch_processed = len(per_photo_times)
        self.batch_avg_time = (
            sum(per_photo_times) / len(per_photo_times) if per_photo_times else 0.0
        )
        if self.batch_processed:
            self.log_message.emit(
                f"Batch complete: {self.batch_processed} photos in "
                f"{self.batch_total_time:.1f}s (avg {self.batch_avg_time:.1f}s)"
            )

        self.finished_all.emit()


# ─────────────────────────────────────────────────────────
# File loader worker thread (performance: no thumbnails at load time)
# ─────────────────────────────────────────────────────────

class FileLoaderWorker(QThread):
    """Loads photo files in background without generating thumbnails."""
    progress = Signal(int, int)  # current, total
    finished_loading = Signal(list)  # list of PhotoItem

    def __init__(self, filepaths):
        super().__init__()
        self.filepaths = filepaths

    def run(self):
        items = []
        total = len(self.filepaths)
        for i, fp in enumerate(self.filepaths):
            items.append(PhotoItem(filepath=fp, filename=os.path.basename(fp)))
            if (i + 1) % 50 == 0 or i == total - 1:
                self.progress.emit(i + 1, total)
        self.finished_loading.emit(items)


# ─────────────────────────────────────────────────────────
# Metadata writer (ExifTool)
# ─────────────────────────────────────────────────────────

class MetadataWriter:
    """Writes IPTC and XMP metadata using exiftool."""

    # Common install locations on macOS (PATH is restricted inside .app bundles)
    _EXIFTOOL_PATHS_MAC = [
        "/usr/local/bin/exiftool",      # ExifTool official .pkg installer
        "/opt/homebrew/bin/exiftool",   # Homebrew (Apple Silicon)
        "/usr/local/opt/exiftool/bin/exiftool",  # old Homebrew Intel
        "/opt/local/bin/exiftool",      # MacPorts
    ]

    # Common install locations on Windows
    _EXIFTOOL_PATHS_WIN = [
        r"C:\Windows\exiftool.exe",
        r"C:\Program Files\ExifTool\exiftool.exe",
        r"C:\Program Files (x86)\ExifTool\exiftool.exe",
    ]

    @staticmethod
    def find_exiftool():
        """Return the path to exiftool, or None if not found."""
        exe = "exiftool.exe" if sys.platform == "win32" else "exiftool"

        # In a frozen build, ExifTool ships next to the app executable
        if getattr(sys, 'frozen', False):
            exedir = os.path.dirname(sys.executable)
            bundled = os.path.join(exedir, exe)
            if os.path.isfile(bundled):
                return bundled

        # Fallback: PyInstaller _MEIPASS (onefile builds / bundled data)
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            bundled = os.path.join(meipass, exe)
            if os.path.isfile(bundled):
                return bundled

        # Check next to this script (dev / source installs)
        script_dir = os.path.dirname(os.path.abspath(__file__))
        local_exe = os.path.join(script_dir, exe)
        if os.path.isfile(local_exe):
            return local_exe

        found = shutil.which("exiftool")
        if found:
            return found

        paths = (MetadataWriter._EXIFTOOL_PATHS_WIN if sys.platform == "win32"
                 else MetadataWriter._EXIFTOOL_PATHS_MAC)
        for path in paths:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        return None

    @staticmethod
    def check_exiftool():
        return MetadataWriter.find_exiftool() is not None

    @staticmethod
    def _norm_keywords(kws):
        """Coerce a keyword list to clean, non-empty strings. Guards the write
        path against numeric keywords (e.g. a year), which arrive as ints from
        ExifTool's JSON or a model response and would break .lower()/.strip()."""
        return [str(k).strip() for k in (kws or [])
                if k is not None and str(k).strip()]

    @staticmethod
    def read_existing_metadata(filepath):
        """Read existing title, caption, keywords across IPTC, XMP and EXIF.

        Checking all carriers (not just IPTC) means the skip/append logic works
        regardless of where the existing tool stored them — e.g. Photo Mechanic
        and Lightroom often write the caption to XMP dc:description, not just
        IPTC:Caption-Abstract.
        """
        try:
            exiftool = MetadataWriter.find_exiftool() or "exiftool"
            result = _run(
                [exiftool, "-j",
                 "-IPTC:ObjectName", "-XMP:Title",
                 "-IPTC:Caption-Abstract", "-XMP:Description", "-EXIF:ImageDescription",
                 "-IPTC:Keywords", "-XMP:Subject",
                 filepath],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                if data:
                    entry = data[0]
                    title = entry.get("ObjectName") or entry.get("Title") or ""
                    caption = (entry.get("Caption-Abstract")
                               or entry.get("Description")
                               or entry.get("ImageDescription") or "")
                    keywords = entry.get("Keywords")
                    if not keywords:
                        keywords = entry.get("Subject") or []
                    # ExifTool -j returns a purely-numeric keyword (e.g. a year
                    # like 2025) as a JSON number, and a single value isn't a
                    # list. Normalise to a list of non-empty strings so callers
                    # can safely call .lower()/.strip() on every element.
                    if not isinstance(keywords, list):
                        keywords = [keywords]
                    keywords = [str(k).strip() for k in keywords
                                if k is not None and str(k).strip()]
                    return str(title), str(caption), keywords
        except Exception:
            pass
        return "", "", []

    @staticmethod
    def _sidecar_paths(filepath):
        """Existing XMP sidecars for a file — both naming conventions."""
        paths = []
        base = os.path.splitext(str(filepath))[0]
        for cand in (base + ".xmp", str(filepath) + ".xmp"):
            if os.path.isfile(cand) and cand not in paths:
                paths.append(cand)
        return paths

    @staticmethod
    def read_persons(filepath):
        """Names of people already tagged on the image, read from the file AND
        any co-located XMP sidecar (HEIC/RAW often keep them there). Sources:
        Excire FaceTags, IPTC PersonInImage, MWG face-region names. Returns []
        when nothing is found. Based on @Boui3D's reference in issue #9."""
        try:
            exiftool = MetadataWriter.find_exiftool() or "exiftool"
            targets = [str(filepath)] + MetadataWriter._sidecar_paths(filepath)
            fields = ["-XMP-excire:all", "-XMP-iptcExt:PersonInImage", "-RegionName"]
            names, seen = [], set()
            for tgt in targets:
                r = _run([exiftool, "-j", "-sep", ", "] + fields + [tgt],
                         capture_output=True, text=True, timeout=15)
                if r.returncode != 0 or not r.stdout:
                    continue
                try:
                    data = json.loads(r.stdout)
                except Exception:
                    continue
                if not data:
                    continue
                entry = data[0]
                for key in ("FaceTags", "PersonInImage", "RegionName"):
                    val = entry.get(key, "")
                    if isinstance(val, list):
                        val = ", ".join(str(x) for x in val)
                    for n in (x.strip() for x in str(val).split(",") if x.strip()):
                        if n.lower() not in seen:
                            seen.add(n.lower())
                            names.append(n)
            return names
        except Exception:
            return []

    @staticmethod
    def read_keywords(filepath):
        """Keyword / subject tags already on the image (and any co-located XMP
        sidecar) — e.g. species names written by a specialist tagger like
        SuperPicky (birds), or place/event tags. Weaving these into the prompt
        lets the caption use the real subject ('a Superb Fairywren' instead of
        'a small bird'), which local generalist vision models can't identify on
        their own. Returns [] when nothing is found."""
        try:
            exiftool = MetadataWriter.find_exiftool() or "exiftool"
            targets = [str(filepath)] + MetadataWriter._sidecar_paths(filepath)
            # darktable keeps its tags in lr:HierarchicalSubject, so a file
            # tagged there would otherwise look untagged to us.
            fields = ["-IPTC:Keywords", "-XMP-dc:Subject",
                      "-XMP-lr:HierarchicalSubject"]
            tags, seen = [], set()
            for tgt in targets:
                r = _run([exiftool, "-j", "-sep", "\n"] + fields + [tgt],
                         capture_output=True, text=True, timeout=15)
                if r.returncode != 0 or not r.stdout:
                    continue
                try:
                    data = json.loads(r.stdout)
                except Exception:
                    continue
                if not data:
                    continue
                entry = data[0]
                for key in ("Keywords", "Subject", "HierarchicalSubject"):
                    val = entry.get(key, "")
                    if isinstance(val, list):
                        val = "\n".join(str(x) for x in val)
                    for t in (x.strip() for x in str(val).split("\n") if x.strip()):
                        # A hierarchical tag is "Places|Australia|Gerroa" —
                        # the leaf is the useful part for prompt context.
                        if key == "HierarchicalSubject" and "|" in t:
                            t = t.rsplit("|", 1)[-1].strip()
                        if t and t.lower() not in seen:
                            seen.add(t.lower())
                            tags.append(t)
            return tags
        except Exception:
            return []

    @staticmethod
    def write_metadata(filepath, metadata: PhotoMetadata, backup=True,
                       append_keywords=False, skip_existing=False,
                       use_sidecar=False, adobe_naming=False):
        """Write title, caption, keywords to file (or XMP sidecar) via exiftool."""
        p = Path(filepath)
        if use_sidecar and p.suffix.lower() in RAW_EXTENSIONS:
            return MetadataWriter._write_sidecar(
                p, metadata, adobe_naming=adobe_naming,
                skip_existing=skip_existing, append_keywords=append_keywords)

        exiftool = MetadataWriter.find_exiftool() or "exiftool"
        # Mark the IPTC block as UTF-8 so accented keywords (e.g. "Château de
        # Chenonceau") survive. Without this, exiftool falls back to Latin-1
        # and readers like Lightroom show a "?" for the accented characters.
        args = [exiftool, "-codedcharacterset=utf8"]

        if not backup:
            args.append("-overwrite_original")

        # Check existing metadata if we need to skip or append
        existing_title, existing_caption, existing_keywords = "", "", []
        if skip_existing or append_keywords:
            existing_title, existing_caption, existing_keywords = \
                MetadataWriter.read_existing_metadata(filepath)

        # Title: skip if exists and skip_existing is on
        if not (skip_existing and existing_title):
            args.append(f"-IPTC:ObjectName={metadata.title}")
            args.append(f"-XMP:Title={metadata.title}")

        # Caption: skip if exists and skip_existing is on
        if not (skip_existing and existing_caption):
            args.append(f"-IPTC:Caption-Abstract={metadata.caption}")
            args.append(f"-XMP:Description={metadata.caption}")
            args.append(f"-EXIF:ImageDescription={metadata.caption}")

        # Keywords: append or replace
        kw_list = MetadataWriter._norm_keywords(metadata.keywords)
        if append_keywords:
            existing_lower = {k.lower() for k in existing_keywords}
            new_keywords = [
                kw for kw in kw_list
                if kw.lower() not in existing_lower
            ]
            for kw in new_keywords:
                args.append(f"-IPTC:Keywords+={kw}")
                args.append(f"-XMP:Subject+={kw}")
                args.append(f"-XMP-lr:HierarchicalSubject+={kw}")
        else:
            # Two-pass: clear all keywords first, then write new ones
            clear_args = [exiftool]
            if not backup:
                clear_args.append("-overwrite_original")
            clear_args.extend([
                "-IPTC:Keywords=",
                "-XMP:Subject=",
                "-XMP-lr:HierarchicalSubject=",
                filepath
            ])
            _run(clear_args, capture_output=True, text=True, timeout=15)

            # Now add the new keywords
            for kw in kw_list:
                args.append(f"-IPTC:Keywords+={kw}")
                args.append(f"-XMP:Subject+={kw}")
                args.append(f"-XMP-lr:HierarchicalSubject+={kw}")

        args.append(filepath)

        result = _run(args, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"exiftool error: {result.stderr}")
        return True

    @staticmethod
    def _write_sidecar(raw_path: Path, metadata: PhotoMetadata, adobe_naming=False,
                       skip_existing=False, append_keywords=False):
        """Write metadata to an XMP sidecar alongside the RAW file.

        Honours skip_existing and append_keywords the same way the embedded
        path does, reading what's already in the sidecar so existing captions
        (e.g. from Photo Mechanic) aren't overwritten.
        """
        exiftool = MetadataWriter.find_exiftool() or "exiftool"
        xmp_path = raw_path.with_suffix(".xmp") if adobe_naming else Path(str(raw_path) + ".xmp")

        # If a sidecar already exists under the OTHER naming convention, write
        # to that one instead of creating a second file. Two sidecars for one
        # RAW means the cataloguer keeps reading its own and never sees what we
        # wrote — and any rating/label living in it gets stranded.
        if not xmp_path.exists():
            other = (Path(str(raw_path) + ".xmp") if adobe_naming
                     else raw_path.with_suffix(".xmp"))
            if other.exists():
                xmp_path = other

        existing_title, existing_caption, existing_keywords = "", "", []
        if (skip_existing or append_keywords) and xmp_path.exists():
            existing_title, existing_caption, existing_keywords = \
                MetadataWriter.read_existing_metadata(str(xmp_path))

        args = [exiftool, "-overwrite_original"]

        if not (skip_existing and existing_title):
            args.append(f"-XMP-dc:Title={metadata.title}")
        if not (skip_existing and existing_caption):
            args.append(f"-XMP-dc:Description={metadata.caption}")

        kw_list = MetadataWriter._norm_keywords(metadata.keywords)
        if append_keywords:
            existing_lower = {k.lower() for k in existing_keywords}
            for kw in kw_list:
                if kw.lower() not in existing_lower:
                    args.append(f"-XMP-dc:Subject+={kw}")
                    args.append(f"-XMP-lr:HierarchicalSubject+={kw}")
        else:
            # Replace: clear existing keywords in a separate pass first — a
            # single "-Subject= ... +=" pass doesn't reliably clear a list tag
            # on an existing sidecar (matches the embedded path's two-pass).
            if xmp_path.exists():
                _run(
                    [exiftool, "-overwrite_original", "-XMP-dc:Subject=",
                     "-XMP-lr:HierarchicalSubject=", str(xmp_path)],
                    capture_output=True, text=True, timeout=15
                )
            for kw in kw_list:
                args.append(f"-XMP-dc:Subject+={kw}")
                args.append(f"-XMP-lr:HierarchicalSubject+={kw}")

        # Nothing left to write (everything skipped) — leave the sidecar as-is
        if len(args) <= 2:
            return True

        if xmp_path.exists():
            args.append(str(xmp_path))
        else:
            args.extend(["-o", str(xmp_path), str(raw_path)])

        result = _run(args, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"exiftool sidecar error: {result.stderr}")
        return True

    @staticmethod
    def write_metadata_batch(items, backup=True, append_keywords=False,
                             skip_existing=False, use_sidecar=False,
                             adobe_naming=False):
        """Write metadata to multiple files using exiftool -stay_open batch mode.

        items: list of (filepath, PhotoMetadata) tuples.
        Sidecar items are handled individually (they need -o flag logic).
        Returns (success_count, errors_list).
        """
        exiftool = MetadataWriter.find_exiftool() or "exiftool"
        success = 0
        errors = []

        # Separate sidecar items from regular items
        regular_items = []
        sidecar_items = []
        for filepath, metadata in items:
            p = Path(filepath)
            if use_sidecar and p.suffix.lower() in RAW_EXTENSIONS:
                sidecar_items.append((filepath, metadata))
            else:
                regular_items.append((filepath, metadata))

        # Process regular items in batch mode
        if regular_items:
            try:
                proc = _popen(
                    [exiftool, "-stay_open", "True", "-@", "-"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    encoding="utf-8",
                    errors="replace",
                )

                for filepath, metadata in regular_items:
                    arg_lines = []
                    # UTF-8 IPTC so accented keywords aren't mangled to "?".
                    arg_lines.append("-codedcharacterset=utf8")
                    if not backup:
                        arg_lines.append("-overwrite_original")

                    # Check existing metadata if we need to skip or append
                    existing_title, existing_caption, existing_keywords = "", "", []
                    if skip_existing or append_keywords:
                        existing_title, existing_caption, existing_keywords = \
                            MetadataWriter.read_existing_metadata(filepath)

                    # Title
                    if not (skip_existing and existing_title):
                        arg_lines.append(f"-IPTC:ObjectName={metadata.title}")
                        arg_lines.append(f"-XMP:Title={metadata.title}")

                    # Caption
                    if not (skip_existing and existing_caption):
                        arg_lines.append(f"-IPTC:Caption-Abstract={metadata.caption}")
                        arg_lines.append(f"-XMP:Description={metadata.caption}")
                        arg_lines.append(f"-EXIF:ImageDescription={metadata.caption}")

                    # Keywords
                    if append_keywords:
                        existing_lower = {k.lower() for k in existing_keywords}
                        new_keywords = [
                            kw for kw in metadata.keywords
                            if kw.lower() not in existing_lower
                        ]
                        for kw in new_keywords:
                            arg_lines.append(f"-IPTC:Keywords+={kw}")
                            arg_lines.append(f"-XMP:Subject+={kw}")
                            arg_lines.append(f"-XMP-lr:HierarchicalSubject+={kw}")
                    else:
                        arg_lines.append("-IPTC:Keywords=")
                        arg_lines.append("-XMP:Subject=")
                        arg_lines.append("-XMP-lr:HierarchicalSubject=")
                        for kw in metadata.keywords:
                            arg_lines.append(f"-IPTC:Keywords+={kw}")
                            arg_lines.append(f"-XMP:Subject+={kw}")
                            arg_lines.append(f"-XMP-lr:HierarchicalSubject+={kw}")

                    arg_lines.append(filepath)
                    arg_lines.append("-execute")

                    proc.stdin.write("\n".join(arg_lines) + "\n")
                    proc.stdin.flush()
                    success += 1

                # Close the batch session
                proc.stdin.write("-stay_open\nFalse\n")
                proc.stdin.flush()
                proc.wait(timeout=120)

            except Exception as e:
                errors.append(f"Batch write error: {e}")

        # Process sidecar items individually
        for filepath, metadata in sidecar_items:
            try:
                MetadataWriter._write_sidecar(
                    Path(filepath), metadata, adobe_naming=adobe_naming
                )
                success += 1
            except Exception as e:
                errors.append(f"{os.path.basename(filepath)}: {e}")

        return success, errors


# ─────────────────────────────────────────────────────────
# Metadata write worker thread
# ─────────────────────────────────────────────────────────

class MetadataWriteWorker(QThread):
    """Writes metadata to files in a background thread.

    Uses ExifTool's -stay_open batch mode (single persistent process) for
    regular files, giving per-file progress callbacks.  Sidecar items are
    handled individually (they need -o flag logic).
    """
    progress = Signal(int, int)  # current, total
    file_done = Signal(str, bool, str)  # filename, success, error_msg
    finished_writing = Signal(int, int)  # success_count, error_count

    def __init__(self, items, backup=True, append_keywords=False,
                 skip_existing=False, use_sidecar=False, adobe_naming=False):
        super().__init__()
        self.items = items  # list of (filepath, PhotoMetadata)
        self.backup = backup
        self.append_keywords = append_keywords
        self.skip_existing = skip_existing
        self.use_sidecar = use_sidecar
        self.adobe_naming = adobe_naming

    def run(self):
        total = len(self.items)
        success_count = 0
        error_count = 0

        exiftool = MetadataWriter.find_exiftool()
        if not exiftool:
            for i, (filepath, _) in enumerate(self.items):
                self.file_done.emit(os.path.basename(filepath), False,
                                    "ExifTool not found")
                error_count += 1
                self.progress.emit(i + 1, total)
            self.finished_writing.emit(success_count, error_count)
            return

        # Separate sidecar items from regular items
        regular_items = []
        sidecar_items = []
        for filepath, metadata in self.items:
            p = Path(filepath)
            if self.use_sidecar and p.suffix.lower() in RAW_EXTENSIONS:
                sidecar_items.append((filepath, metadata))
            else:
                regular_items.append((filepath, metadata))

        progress_idx = 0

        # ── Process regular items using -stay_open batch mode ──
        if regular_items:
            try:
                proc = _popen(
                    [exiftool, "-stay_open", "True", "-@", "-"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    encoding="utf-8",
                    errors="replace",
                )

                for filepath, metadata in regular_items:
                    try:
                        arg_lines = self._build_args_for_file(
                            filepath, metadata, exiftool
                        )
                        arg_lines.append(filepath)
                        arg_lines.append("-execute")

                        proc.stdin.write("\n".join(arg_lines) + "\n")
                        proc.stdin.flush()

                        # Read exiftool's per-file response (ends with {ready})
                        output_lines = []
                        while True:
                            line = proc.stdout.readline()
                            if not line:
                                break
                            line = line.strip()
                            if line == "{ready}":
                                break
                            output_lines.append(line)

                        output_text = " ".join(output_lines)
                        if ("error" in output_text.lower()
                                and "0 image files updated" in output_text):
                            raise RuntimeError(output_text)

                        success_count += 1
                        self.file_done.emit(
                            os.path.basename(filepath), True, "")

                    except Exception as e:
                        error_count += 1
                        self.file_done.emit(
                            os.path.basename(filepath), False, str(e))

                    progress_idx += 1
                    self.progress.emit(progress_idx, total)

                # Close the batch session
                proc.stdin.write("-stay_open\nFalse\n")
                proc.stdin.flush()
                proc.wait(timeout=30)

            except Exception as e:
                # If the process itself failed, report remaining files as errors
                for filepath, _ in regular_items[progress_idx:]:
                    error_count += 1
                    self.file_done.emit(
                        os.path.basename(filepath), False,
                        f"Batch process error: {e}")
                    progress_idx += 1
                    self.progress.emit(progress_idx, total)

        # ── Process sidecar items individually ──
        for filepath, metadata in sidecar_items:
            try:
                MetadataWriter._write_sidecar(
                    Path(filepath), metadata,
                    adobe_naming=self.adobe_naming,
                    skip_existing=self.skip_existing,
                    append_keywords=self.append_keywords,
                )
                success_count += 1
                self.file_done.emit(os.path.basename(filepath), True, "")
            except Exception as e:
                error_count += 1
                self.file_done.emit(os.path.basename(filepath), False, str(e))

            progress_idx += 1
            self.progress.emit(progress_idx, total)

        self.finished_writing.emit(success_count, error_count)

    def _build_args_for_file(self, filepath, metadata, exiftool):
        """Build exiftool arg lines for a single file within the batch.

        Does NOT include the filepath or -execute terminator — those are
        appended by the caller.
        """
        arg_lines = []

        if not self.backup:
            arg_lines.append("-overwrite_original")

        # Read existing metadata if needed for skip/append logic
        existing_title, existing_caption, existing_keywords = "", "", []
        if self.skip_existing or self.append_keywords:
            existing_title, existing_caption, existing_keywords = \
                MetadataWriter.read_existing_metadata(filepath)

        # Title
        if not (self.skip_existing and existing_title):
            arg_lines.append(f"-IPTC:ObjectName={metadata.title}")
            arg_lines.append(f"-XMP:Title={metadata.title}")

        # Caption
        if not (self.skip_existing and existing_caption):
            arg_lines.append(f"-IPTC:Caption-Abstract={metadata.caption}")
            arg_lines.append(f"-XMP:Description={metadata.caption}")
            arg_lines.append(f"-EXIF:ImageDescription={metadata.caption}")

        # Keywords
        kw_list = MetadataWriter._norm_keywords(metadata.keywords)
        if self.append_keywords:
            existing_lower = {k.lower() for k in existing_keywords}
            new_keywords = [
                kw for kw in kw_list
                if kw.lower() not in existing_lower
            ]
            for kw in new_keywords:
                arg_lines.append(f"-IPTC:Keywords+={kw}")
                arg_lines.append(f"-XMP:Subject+={kw}")
        else:
            # Replace mode: use plain = for each keyword. ExifTool processes
            # args in order, so the first = clears the list and subsequent =
            # assignments append to it within the same -execute block.
            arg_lines.append("-IPTC:Keywords=")
            arg_lines.append("-XMP:Subject=")
            for kw in kw_list:
                arg_lines.append(f"-IPTC:Keywords={kw}")
                arg_lines.append(f"-XMP:Subject={kw}")

        return arg_lines


# ─────────────────────────────────────────────────────────
# Stylesheet
# ─────────────────────────────────────────────────────────

# ── Palette ───────────────────────────────────────────────
# Warm, desaturated dark theme. The accent (#c05a2e, a burnt terracotta) is
# rationed deliberately: active tab, section eyebrows, checked boxes, and the
# primary Generate button only. Everything else is neutral, so the accent
# actually means something. Qt Style Sheets do NOT support `text-transform`,
# so anything that reads as uppercase is uppercased in the widget text itself.
#
#   page   #141416   card   #1d1d20   input  #17171a   hairline #2a2a2f
#   text   #f2f0eb   muted  #9a9aa0   dim    #5f5f66
#   accent #c05a2e   tint   #e0a06a   eyebrow #d1935e
#   green  #7bc9a0   teal   #2c5a4d   danger #e2796a

STYLESHEET = """
QMainWindow, QDialog {
    background-color: #141416;
}
QWidget {
    color: #f2f0eb;
    font-family: 'Inter', 'SF Pro Text', 'Helvetica Neue', 'Segoe UI', sans-serif;
    font-size: 13px;
}

/* ── Cards ── */
QGroupBox {
    background-color: #1d1d20;
    border: 1px solid #2a2a2f;
    border-radius: 12px;
    margin-top: 0px;
    padding: 34px 18px 18px 18px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1px;
    color: #d1935e;
}
QGroupBox::title {
    subcontrol-origin: padding;
    subcontrol-position: top left;
    left: 18px;
    top: 14px;
    padding: 0px;
    background: transparent;
}

/* ── Buttons: one height, one radius ── */
QPushButton {
    background-color: #232326;
    border: 1px solid #2f2f35;
    border-radius: 8px;
    padding: 9px 16px;
    font-weight: 500;
    color: #e6e4df;
    min-height: 20px;
}
QPushButton:hover {
    background-color: #2b2b30;
    border-color: #3d3d45;
}
QPushButton:pressed {
    background-color: #1b1b1e;
}
QPushButton:disabled {
    background-color: #1b1b1e;
    color: #4e4e55;
    border-color: #262629;
}
/* Primary — the only accent-filled button */
QPushButton#primaryBtn {
    background-color: #c05a2e;
    color: #fff5ee;
    font-weight: 600;
    border: 1px solid #c05a2e;
}
QPushButton#primaryBtn:hover {
    background-color: #d97a4a;
    border-color: #d97a4a;
}
QPushButton#primaryBtn:pressed {
    background-color: #a84a24;
    border-color: #a84a24;
}
QPushButton#primaryBtn:disabled {
    background-color: #3a2419;
    border-color: #3a2419;
    color: #8a6a58;
}
/* Secondary — muted teal */
QPushButton#writeBtn {
    background-color: #2c5a4d;
    color: #eafff3;
    font-weight: 600;
    border: 1px solid #34695a;
}
QPushButton#writeBtn:hover {
    background-color: #37705f;
}
QPushButton#writeBtn:disabled {
    background-color: #1e2f29;
    border-color: #24352e;
    color: #5c7c70;
}
/* Tertiary — bordered ghost */
QPushButton#exportBtn {
    background-color: transparent;
    border: 1px solid #2f2f35;
    color: #b6b4af;
    font-weight: 500;
}
QPushButton#exportBtn:hover {
    background-color: #202024;
    border-color: #3d3d45;
    color: #f2f0eb;
}
QPushButton#exportBtn:disabled {
    background-color: transparent;
    border-color: #262629;
    color: #4e4e55;
}
/* Destructive — ghost, tinted */
QPushButton#dangerBtn {
    background-color: transparent;
    border: 1px solid #4a2723;
    color: #e2796a;
    font-weight: 500;
}
QPushButton#dangerBtn:hover {
    background-color: #2b1614;
    border-color: #6b3931;
}
QPushButton#dangerBtn:disabled {
    background-color: transparent;
    border-color: #2b1614;
    color: #6b4a44;
}

QComboBox {
    background-color: #17171a;
    border: 1px solid #2f2f35;
    border-radius: 8px;
    padding: 7px 10px;
    min-height: 20px;
    color: #f2f0eb;
}
QComboBox:hover {
    border-color: #3d3d45;
}
QComboBox QAbstractItemView {
    background-color: #1d1d20;
    border: 1px solid #2f2f35;
    border-radius: 8px;
    selection-background-color: #c05a2e;
    selection-color: #fff5ee;
    outline: none;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}

QLineEdit, QTextEdit, QPlainTextEdit {
    background-color: #17171a;
    border: 1px solid #2a2a2f;
    border-radius: 8px;
    padding: 10px 12px;
    color: #f2f0eb;
    selection-background-color: #c05a2e;
    selection-color: #fff5ee;
}
QLineEdit {
    min-height: 22px;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {
    border-color: #c05a2e;
}
/* Hints should sit well behind real values */
QLineEdit[echoMode="0"]::placeholder {
    color: #4e4e55;
}

QTableWidget {
    background-color: #1d1d20;
    border: 1px solid #2a2a2f;
    border-radius: 10px;
    gridline-color: #26262a;
    selection-background-color: #2b1c14;
    outline: none;
}
QTableWidget::item {
    padding: 6px 8px;
    border-bottom: 1px solid #26262a;
}
QTableWidget::item:selected {
    background-color: #2b1c14;
    color: #e0a06a;
}
QHeaderView::section {
    background-color: #1d1d20;
    color: #7e7c78;
    border: none;
    border-bottom: 1px solid #2a2a2f;
    padding: 8px;
    font-weight: 600;
    font-size: 10px;
    letter-spacing: 1px;
}

QProgressBar {
    background-color: #17171a;
    border: 1px solid #2a2a2f;
    border-radius: 5px;
    height: 8px;
    text-align: center;
    font-size: 10px;
}
QProgressBar::chunk {
    background-color: #c05a2e;
    border-radius: 4px;
}

QScrollArea {
    border: none;
    background: transparent;
}
/* The widget inside the viewport keeps painting with Qt's default (light)
   brush unless it is explicitly cleared — otherwise it shows as a white strip
   wherever the content doesn't cover it. */
QScrollArea > QWidget > QWidget {
    background: transparent;
}
/* The groove must be painted explicitly. Left transparent, Qt falls back to
   the default (light) brush and the scrollbar shows as a white bar. */
QScrollBar:vertical {
    background-color: #141416;
    width: 10px;
    border: none;
    margin: 0;
}
QScrollBar::handle:vertical {
    background-color: #2f2f35;
    border-radius: 5px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background-color: #3d3d45;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background-color: #141416;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
    border: none;
    background: none;
}
QScrollBar:horizontal {
    background-color: #141416;
    height: 10px;
    border: none;
    margin: 0;
}
QScrollBar::handle:horizontal {
    background-color: #2f2f35;
    border-radius: 5px;
    min-width: 30px;
}
QScrollBar::handle:horizontal:hover {
    background-color: #3d3d45;
}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background-color: #141416;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
    border: none;
    background: none;
}

/* ── Tabs: pill-shaped active state ── */
QTabWidget::pane {
    border: none;
    background: transparent;
    top: 4px;
}
QTabBar::tab {
    background-color: transparent;
    color: #8a8a90;
    border: none;
    padding: 8px 16px;
    margin-right: 4px;
    border-radius: 8px;
    font-weight: 500;
}
QTabBar::tab:selected {
    background-color: #e0a06a;
    color: #1a1a1d;
    font-weight: 600;
}
QTabBar::tab:hover:!selected {
    background-color: #202024;
    color: #f2f0eb;
}

/* ── Checkboxes: filled when checked, hairline when not ── */
QCheckBox {
    spacing: 9px;
    color: #d8d6d1;
}
QCheckBox::indicator {
    width: 15px;
    height: 15px;
    border-radius: 5px;
    border: 1px solid #3a3a42;
    background-color: transparent;
}
QCheckBox::indicator:hover {
    border-color: #5f5f6655f;
}
QCheckBox::indicator:checked {
    background-color: #c05a2e;
    border-color: #c05a2e;
}

QSplitter::handle {
    background-color: #232326;
    width: 2px;
}
QSplitter::handle:hover {
    background-color: #2f2f35;
}

QLabel#statusLabel {
    color: #7e7c78;
    font-size: 11px;
}
QLabel#dropLabel {
    color: #b6b4af;
    font-size: 15px;
    font-weight: 400;
}
QLabel#titleLabel {
    font-size: 26px;
    font-weight: 800;
    color: #f2f0eb;
    letter-spacing: 1px;
}
QLabel#subtitleLabel {
    font-size: 11px;
    color: #6f6d6a;
    font-weight: 400;
}
/* Section eyebrow, for cards that draw their own label */
QLabel#eyebrow {
    color: #d1935e;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1px;
    background: transparent;
}
QLabel#cardHint {
    color: #6f6d6a;
    font-size: 11px;
    background: transparent;
}
/* Connection status pill */
QLabel#statusPill {
    font-size: 12px;
    font-weight: 500;
    padding: 5px 12px;
    border-radius: 11px;
    background-color: #16241d;
    border: 1px solid #2c5a4d;
    color: #7bc9a0;
}
QLabel#statusPill[state="off"] {
    background-color: #2b1614;
    border: 1px solid #4a2723;
    color: #e2796a;
}
QLabel#statusPill[state="wait"] {
    background-color: #201a15;
    border: 1px solid #3d3128;
    color: #d1935e;
}
"""


# ─────────────────────────────────────────────────────────
# Drop zone widget
# ─────────────────────────────────────────────────────────

class DropZone(QFrame):
    files_dropped = Signal(list)

    # Idle reads as a quiet card; only an active drag earns the accent.
    _IDLE_QSS = """
        DropZone {
            border: 1px solid #2a2a2f;
            border-radius: 12px;
            background-color: #1d1d20;
        }
        DropZone:hover {
            border-color: #3d3d45;
            background-color: #202024;
        }
    """
    _ACTIVE_QSS = """
        DropZone {
            border: 1px solid #c05a2e;
            border-radius: 12px;
            background-color: #221812;
        }
    """

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setMinimumHeight(120)
        self.setStyleSheet(self._IDLE_QSS)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(6)

        icon_label = QLabel("📷")
        icon_label.setStyleSheet(
            "font-size: 22px; background-color: #232326; border: 1px solid #2f2f35;"
            "border-radius: 10px; padding: 8px;"
        )
        icon_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(icon_label, alignment=Qt.AlignCenter)

        text_label = QLabel("Drop photos here or click Browse")
        text_label.setObjectName("dropLabel")
        text_label.setAlignment(Qt.AlignCenter)
        text_label.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(text_label)

        formats_label = QLabel("JPEG  ·  HEIC  ·  TIFF  ·  PNG  ·  RAW  ·  DNG  ·  CR2/CR3  ·  NEF  ·  ARW  ·  ORF  ·  RAF")
        formats_label.setStyleSheet(
            "color: #56545a; font-size: 11px; background: transparent; border: none;"
        )
        formats_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(formats_label)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setStyleSheet(self._ACTIVE_QSS)

    def dragLeaveEvent(self, event):
        self.setStyleSheet(self._IDLE_QSS)

    def dropEvent(self, event: QDropEvent):
        self.setStyleSheet(self._IDLE_QSS)
        urls = event.mimeData().urls()
        files = []
        for url in urls:
            path = url.toLocalFile()
            if os.path.isfile(path) and _is_supported_file(path):
                files.append(path)
            elif os.path.isdir(path):
                for root, dirs, fnames in os.walk(path):
                    for f in fnames:
                        fp = os.path.join(root, f)
                        if _is_supported_file(fp):
                            files.append(fp)
        if files:
            self.files_dropped.emit(files)


# ─────────────────────────────────────────────────────────
# Status indicator widget
# ─────────────────────────────────────────────────────────

class StatusDot(QLabel):
    COLOURS = {
        "pending": "#5f5f66",
        "processing": "#d1935e",
        "done": "#7bc9a0",
        "error": "#e2796a",
    }

    def __init__(self, status="pending"):
        super().__init__()
        self.setFixedSize(12, 12)
        self.set_status(status)

    def set_status(self, status):
        colour = self.COLOURS.get(status, "#5f5f66")
        self.setStyleSheet(f"""
            background-color: {colour};
            border-radius: 6px;
            border: none;
        """)


# ─────────────────────────────────────────────────────────
# Main window
# ─────────────────────────────────────────────────────────

class PhotoScribe(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PhotoScribe")
        self.setMinimumSize(900, 600)

        # Dynamic window sizing based on screen resolution
        screen = QApplication.primaryScreen()
        if screen:
            available = screen.availableGeometry()
            w = min(int(available.width() * 0.80), available.width() - 100)
            h = min(int(available.height() * 0.85), available.height() - 80)
            w = max(w, 1100)
            h = max(h, 750)
            self.resize(w, h)
            x = available.x() + (available.width() - w) // 2
            y = available.y() + (available.height() - h) // 2
            self.move(x, y)
        else:
            self.resize(1300, 850)

        self.photos: list[PhotoItem] = []
        self.worker: Optional[OllamaWorker] = None
        # Separate from the batch worker: writes posts for one photo on demand.
        self._posts_worker: Optional[OllamaWorker] = None
        self._posts_photo: Optional[PhotoItem] = None
        self.settings = QSettings("PhotoScribe", "PhotoScribe")
        self._detected_folder_context: Optional[FolderContext] = None
        # Context fields we filled in ourselves, so Clear All can undo them
        # without also wiping anything the user typed.
        self._autofilled_fields: set = set()
        self._preview_cache = {}

        self._init_ui()
        self._load_settings()
        self._check_dependencies()
        QTimer.singleShot(500, self._refresh_models)

    def _check_dependencies(self):
        if not MetadataWriter.check_exiftool():
            self.log("⚠ ExifTool not found — writing metadata will not work.")
            QTimer.singleShot(200, self._show_exiftool_missing_dialog)
        if not HAS_RAWPY:
            self.log("⚠ rawpy not installed. RAW file support disabled.")

    def _show_exiftool_missing_dialog(self):
        import webbrowser
        is_win = sys.platform == "win32"
        msg = QMessageBox(self)
        msg.setWindowTitle("ExifTool Required")
        msg.setIcon(QMessageBox.Warning)
        msg.setText("ExifTool is not installed.")
        msg.setInformativeText(
            "ExifTool is needed to write metadata to your photo files.\n\n"
            + ("Click 'Download Installer' to get the official Windows installer. "
               "After installing, restart PhotoScribe."
               if is_win else
               "Click 'Download Installer' to get the official macOS package "
               "(no Terminal required). After installing, restart PhotoScribe.")
        )
        install_btn = msg.addButton("Download Installer", QMessageBox.AcceptRole)
        msg.addButton("Later", QMessageBox.RejectRole)
        msg.exec()
        if msg.clickedButton() == install_btn:
            webbrowser.open("https://exiftool.org/install.html")

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(16, 12, 16, 12)
        main_layout.setSpacing(8)

        # ── Header ──
        header = QHBoxLayout()

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title = QLabel("PHOTOSCRIBE")
        title.setObjectName("titleLabel")
        title_col.addWidget(title)
        subtitle = QLabel(
            f"AI-powered metadata generation using local models  ·  v{APP_VERSION}"
        )
        subtitle.setObjectName("subtitleLabel")
        title_col.addWidget(subtitle)
        header.addLayout(title_col)
        header.addStretch()

        # Backend status, as a pill badge. The colour comes from the `state`
        # property so the pill restyles without rebuilding its stylesheet.
        self.ollama_status = QLabel("● Checking Ollama...")
        self.ollama_status.setObjectName("statusPill")
        self.ollama_status.setProperty("state", "wait")
        header.addWidget(self.ollama_status, alignment=Qt.AlignVCenter)

        main_layout.addLayout(header)

        # ── Splitter: left panel + right panel ──
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(3)

        # ═══ LEFT PANEL ═══
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 8, 0)
        left_layout.setSpacing(8)

        # Drop zone
        self.drop_zone = DropZone()
        self.drop_zone.files_dropped.connect(self._on_files_dropped)
        left_layout.addWidget(self.drop_zone)

        # Browse + clear buttons
        btn_row = QHBoxLayout()
        browse_btn = QPushButton("Browse Files")
        browse_btn.clicked.connect(self._browse_files)
        btn_row.addWidget(browse_btn)

        browse_dir_btn = QPushButton("Browse Folder")
        browse_dir_btn.clicked.connect(self._browse_folder)
        btn_row.addWidget(browse_dir_btn)

        self.clear_btn = QPushButton("Clear All")
        self.clear_btn.setObjectName("dangerBtn")
        self.clear_btn.clicked.connect(self._clear_all)
        btn_row.addWidget(self.clear_btn)
        left_layout.addLayout(btn_row)

        # Photo list table
        self.photo_table = QTableWidget()
        self.photo_table.setColumnCount(4)
        self.photo_table.setHorizontalHeaderLabels(["", "Filename", "Status", "Title"])
        self.photo_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.photo_table.setColumnWidth(0, 16)
        self.photo_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.photo_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
        self.photo_table.setColumnWidth(2, 80)
        self.photo_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.photo_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.photo_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.photo_table.verticalHeader().setVisible(False)
        self.photo_table.setShowGrid(False)
        self.photo_table.setAlternatingRowColors(True)
        self.photo_table.setStyleSheet(
            self.photo_table.styleSheet() +
            "QTableWidget { alternate-background-color: #1f1f23; }"
        )
        self.photo_table.currentCellChanged.connect(self._on_photo_selected)
        self.photo_table.cellDoubleClicked.connect(self._on_photo_double_clicked)
        self.photo_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.photo_table.customContextMenuRequested.connect(self._photo_context_menu)
        left_layout.addWidget(self.photo_table, 1)

        # Folder name
        self.folder_name_label = QLabel("")
        self.folder_name_label.setStyleSheet(
            "color: #d1935e; font-size: 12px; font-weight: 600; border: none;"
        )
        left_layout.addWidget(self.folder_name_label)

        # Photo count
        self.photo_count_label = QLabel("0 photos loaded")
        self.photo_count_label.setObjectName("statusLabel")
        left_layout.addWidget(self.photo_count_label)

        splitter.addWidget(left_panel)

        # ═══ RIGHT PANEL ═══
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.setSpacing(8)

        # Tabs
        self.tabs = QTabWidget()

        # ── Settings Tab ──
        settings_tab = QWidget()
        settings_tab_layout = QVBoxLayout(settings_tab)
        settings_tab_layout.setContentsMargins(0, 0, 0, 0)
        settings_tab_layout.setSpacing(0)

        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.NoFrame)
        settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        settings_tab_layout.addWidget(settings_scroll)

        settings_inner = QWidget()
        settings_scroll.setWidget(settings_inner)
        settings_layout = QVBoxLayout(settings_inner)
        settings_layout.setSpacing(14)   # consistent gap between cards
        settings_layout.setContentsMargins(0, 2, 6, 0)

        # Model selection
        model_group = QGroupBox("MODEL")
        model_layout = QHBoxLayout(model_group)
        self.model_combo = QComboBox()
        self.model_combo.setMinimumWidth(200)
        model_layout.addWidget(self.model_combo)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.setFixedWidth(80)
        refresh_btn.clicked.connect(self._refresh_models)
        model_layout.addWidget(refresh_btn)
        model_layout.addStretch()

        # Ollama URL
        model_layout.addWidget(QLabel("URL:"))
        self.ollama_url = QLineEdit("http://localhost:1234")
        self.ollama_url.setFixedWidth(220)
        self.ollama_url.setToolTip(
            "LM Studio: http://localhost:1234 (default)\n"
            "Ollama: http://localhost:11434"
        )
        model_layout.addWidget(self.ollama_url)

        recommend_btn = QPushButton("Recommend Model")
        recommend_btn.setFixedWidth(160)
        recommend_btn.setToolTip("Detect your GPU/RAM and recommend the best model")
        recommend_btn.clicked.connect(self._recommend_model)
        model_layout.addWidget(recommend_btn)

        settings_layout.addWidget(model_group)

        # Model download progress (hidden by default)
        self.model_download_widget = QWidget()
        dl_layout = QVBoxLayout(self.model_download_widget)
        dl_layout.setContentsMargins(0, 4, 0, 0)
        dl_layout.setSpacing(4)
        self.model_download_label = QLabel("")
        self.model_download_label.setStyleSheet(
            "font-size: 14px; font-weight: 600; color: #d1935e; border: none;"
        )
        dl_layout.addWidget(self.model_download_label)
        self.model_download_progress = QProgressBar()
        self.model_download_progress.setMinimum(0)
        self.model_download_progress.setMaximum(100)
        self.model_download_progress.setTextVisible(True)
        self.model_download_progress.setFormat("%p%")
        self.model_download_progress.setFixedHeight(14)
        dl_layout.addWidget(self.model_download_progress)
        self.model_download_widget.setVisible(False)
        settings_layout.addWidget(self.model_download_widget)

        # Prompt
        prompt_group = QGroupBox("PROMPT")
        prompt_layout = QVBoxLayout(prompt_group)
        self.prompt_edit = QTextEdit()
        # Tall enough that the default prompt reads in full without scrolling,
        # capped so a long custom prompt can't crowd out the cards below it.
        self.prompt_edit.setMinimumHeight(155)
        self.prompt_edit.setMaximumHeight(220)
        self.prompt_edit.setPlaceholderText("Enter your prompt for the AI model...")
        self.prompt_edit.setText(
            "Analyse this photograph and generate metadata for it.\n\n"
            "Title: A concise, descriptive title (5-10 words).\n"
            "Caption: A detailed description of the scene, subjects, "
            "lighting, mood, and composition (1-3 sentences).\n"
            "Keywords: 10-20 relevant keywords for search and cataloguing, "
            "covering subject matter, location type, mood, colours, "
            "photographic style, and season where apparent."
        )
        prompt_layout.addWidget(self.prompt_edit)

        preset_row = QHBoxLayout()
        preset_label = QLabel("Presets:")
        preset_label.setStyleSheet("color: #7e7c78; font-size: 11px;")
        preset_row.addWidget(preset_label)

        self.prompt_preset_combo = QComboBox()
        self.prompt_preset_combo.setMinimumWidth(160)
        self.prompt_preset_combo.currentTextChanged.connect(self._on_prompt_preset_selected)
        preset_row.addWidget(self.prompt_preset_combo)

        save_preset_btn = QPushButton("Save As...")
        save_preset_btn.setFixedHeight(24)
        save_preset_btn.setStyleSheet("font-size: 11px; padding: 2px 10px;")
        save_preset_btn.clicked.connect(self._save_prompt_preset)
        preset_row.addWidget(save_preset_btn)

        overwrite_preset_btn = QPushButton("Update")
        overwrite_preset_btn.setFixedHeight(24)
        overwrite_preset_btn.setStyleSheet("font-size: 11px; padding: 2px 10px;")
        overwrite_preset_btn.clicked.connect(self._overwrite_prompt_preset)
        preset_row.addWidget(overwrite_preset_btn)

        delete_preset_btn = QPushButton("Delete")
        delete_preset_btn.setFixedHeight(24)
        delete_preset_btn.setStyleSheet("font-size: 11px; padding: 2px 10px; color: #e2796a;")
        delete_preset_btn.clicked.connect(self._delete_prompt_preset)
        preset_row.addWidget(delete_preset_btn)

        preset_row.addStretch()
        prompt_layout.addLayout(preset_row)
        settings_layout.addWidget(prompt_group)

        # Initialise prompt presets
        self._init_prompt_presets()

        # Outputs: what the file caption is for, plus optional ready-to-post
        # text for social platforms (shown in Results, never written to file).
        outputs_group = QGroupBox("OUTPUTS")
        outputs_layout = QVBoxLayout(outputs_group)
        outputs_layout.setSpacing(10)

        style_row = QHBoxLayout()
        style_label = QLabel("Caption written to the file:")
        style_label.setStyleSheet("color: #8f8d89; font-size: 12px;")
        style_row.addWidget(style_label)
        self.file_style_combo = QComboBox()
        for sid, label, _ in FILE_STYLES:
            self.file_style_combo.addItem(label, sid)
        self.file_style_combo.setToolTip(
            "What the title, caption and keywords written INTO the photo are\n"
            "for. Lightroom, Flickr, WordPress and stock agencies all read them."
        )
        style_row.addWidget(self.file_style_combo)
        style_row.addStretch()
        outputs_layout.addLayout(style_row)

        posts_label = QLabel("Also write posts for:")
        posts_label.setStyleSheet("color: #8f8d89; font-size: 12px;")
        outputs_layout.addWidget(posts_label)
        posts_row = QHBoxLayout()
        posts_row.setSpacing(18)
        self.post_checks = {}
        for pid, label, _, _ in POST_TARGETS:
            cb = QCheckBox(label)
            cb.toggled.connect(self._update_first_person_enabled)
            self.post_checks[pid] = cb
            posts_row.addWidget(cb)
        posts_row.addStretch()
        outputs_layout.addLayout(posts_row)

        self.batch_posts_check = QCheckBox(
            "Write posts for every photo during Generate"
        )
        self.batch_posts_check.setToolTip(
            "Off: write posts one photo at a time from Results, only for the\n"
            "photos you'll actually share. On: write them for the whole batch,\n"
            "which takes considerably longer."
        )
        outputs_layout.addWidget(self.batch_posts_check)

        self.first_person_check = QCheckBox(
            "Write posts in the first person, as the photographer"
        )
        self.first_person_check.setToolTip(
            "e.g. 'Caught this one just as the light went'. Off writes posts\n"
            "in a neutral voice. Alt text is always neutral."
        )
        outputs_layout.addWidget(self.first_person_check)

        outputs_hint = QLabel(
            "Use Write posts on a photo in Results to write the ticked posts "
            "for it. They are never written into the photo. Writing them for "
            "every photo during Generate roughly doubles its time with all five."
        )
        outputs_hint.setObjectName("cardHint")
        outputs_hint.setWordWrap(True)
        outputs_layout.addWidget(outputs_hint)
        settings_layout.addWidget(outputs_group)
        self._update_first_person_enabled()

        # Batch context
        context_group = QGroupBox("BATCH CONTEXT")
        context_layout = QGridLayout(context_group)
        context_layout.setSpacing(10)
        context_layout.setContentsMargins(12, 8, 12, 12)

        # Card header row: the descriptor that used to live in the group title
        # (it reads as a hint, not a heading), then the folder-context toggle.
        folder_ctx_row = QHBoxLayout()
        context_hint = QLabel("applied to all photos")
        context_hint.setObjectName("cardHint")
        folder_ctx_row.addWidget(context_hint)

        self.folder_context_label = QLabel("")
        self.folder_context_label.setStyleSheet(
            "color: #d1935e; font-size: 11px; font-style: italic;"
        )
        folder_ctx_row.addWidget(self.folder_context_label, 1)

        self.clear_folder_ctx_btn = QPushButton("Clear detected")
        self.clear_folder_ctx_btn.setFixedHeight(24)
        self.clear_folder_ctx_btn.setStyleSheet("font-size: 11px; padding: 2px 10px;")
        self.clear_folder_ctx_btn.setVisible(False)
        self.clear_folder_ctx_btn.clicked.connect(self._clear_folder_context)
        folder_ctx_row.addWidget(self.clear_folder_ctx_btn)

        self.folder_context_check = QCheckBox("Use folder context")
        self.folder_context_check.setChecked(True)
        self.folder_context_check.toggled.connect(self._on_folder_context_toggled)
        folder_ctx_row.addWidget(self.folder_context_check)

        context_layout.addLayout(folder_ctx_row, 0, 0, 1, 2)

        fields = [
            ("Location:", "ctx_location", "e.g. Berry, NSW, Australia"),
            ("Event:", "ctx_event", "e.g. Berry Show 2026"),
            ("Date/Time:", "ctx_datetime", "e.g. Morning, March 2026"),
            ("Photographer:", "ctx_photographer", "e.g. Andy Hutchinson"),
            ("Notes:", "ctx_notes", "Any additional context for the AI"),
        ]
        self.context_fields = {}
        for row_idx, (label, key, placeholder) in enumerate(fields):
            row = row_idx + 1  # offset by 1 for the folder context row
            lbl = QLabel(label)
            lbl.setStyleSheet("color: #8f8d89; font-size: 12px;")
            context_layout.addWidget(lbl, row, 0)
            edit = QLineEdit()
            edit.setPlaceholderText(placeholder)
            context_layout.addWidget(edit, row, 1)
            self.context_fields[key] = edit
        settings_layout.addWidget(context_group)

        # Options
        options_group = QGroupBox("OPTIONS")
        options_layout = QVBoxLayout(options_group)

        # Checkboxes in two columns to save vertical space
        checks_grid = QGridLayout()
        checks_grid.setHorizontalSpacing(24)
        checks_grid.setColumnStretch(0, 1)
        checks_grid.setColumnStretch(1, 1)

        self.backup_check = QCheckBox("Create backup files when writing metadata")
        self.backup_check.setChecked(True)
        checks_grid.addWidget(self.backup_check, 0, 0)

        self.append_keywords_check = QCheckBox(
            "Append keywords to existing (instead of replacing)"
        )
        self.append_keywords_check.setChecked(False)
        checks_grid.addWidget(self.append_keywords_check, 0, 1)

        self.skip_existing_check = QCheckBox(
            "Skip title/caption if file already has them"
        )
        self.skip_existing_check.setChecked(False)
        checks_grid.addWidget(self.skip_existing_check, 1, 0)

        self.describe_people_check = QCheckBox(
            "Describe people in photos (positions, roles, actions)"
        )
        self.describe_people_check.setChecked(True)
        self.describe_people_check.setToolTip(
            "When enabled, the AI prompt includes instructions to describe\n"
            "people visible in the photo (e.g. 'bride and groom',\n"
            "'group of hikers', 'child playing'). Does not attempt\n"
            "to identify individuals by name."
        )
        checks_grid.addWidget(self.describe_people_check, 1, 1)

        self.dedup_keywords_check = QCheckBox(
            "Auto-deduplicate similar keywords (plurals, near-duplicates)"
        )
        self.dedup_keywords_check.setChecked(True)
        checks_grid.addWidget(self.dedup_keywords_check, 2, 0)

        self.exif_date_fallback_check = QCheckBox(
            "Use EXIF date as fallback when folder date not detected"
        )
        self.exif_date_fallback_check.setChecked(True)
        checks_grid.addWidget(self.exif_date_fallback_check, 2, 1)

        self.gps_lookup_check = QCheckBox(
            "Look up location from photo GPS / metadata"
        )
        self.gps_lookup_check.setChecked(False)
        self.gps_lookup_check.setToolTip(
            "Fills the Location field from each photo's own metadata.\n"
            "Uses the place name your cataloguer (e.g. Lightroom) already\n"
            "resolved — City, State, Country, from the file or its .xmp\n"
            "sidecar — when present, with no network request. Only if no\n"
            "such place name exists does it reverse-geocode the GPS\n"
            "coordinates via OpenStreetMap Nominatim (a free public API,\n"
            "which is an external request). Off by default."
        )
        self.gps_lookup_check.toggled.connect(self._on_gps_lookup_toggled)
        checks_grid.addWidget(self.gps_lookup_check, 3, 0)

        self.sidecar_check = QCheckBox(
            "Write to XMP sidecar for RAW files"
        )
        self.sidecar_check.setChecked(True)
        checks_grid.addWidget(self.sidecar_check, 3, 1)

        self.auto_write_check = QCheckBox(
            "Write to files automatically after generating"
        )
        self.auto_write_check.setChecked(False)
        self.auto_write_check.setToolTip(
            "When on, metadata is written to your files as soon as generation\n"
            "finishes — useful for leaving a large folder to run unattended.\n"
            "Honours your backup/append/skip options below."
        )
        checks_grid.addWidget(self.auto_write_check, 4, 0)

        options_layout.addLayout(checks_grid)

        sidecar_naming_row = QHBoxLayout()
        sidecar_naming_label = QLabel("DAM")
        sidecar_naming_label.setStyleSheet("color: #8f8d89; font-size: 12px;")
        sidecar_naming_label.setFixedWidth(110)
        sidecar_naming_row.addWidget(sidecar_naming_label)
        self.sidecar_naming_combo = QComboBox()
        self.sidecar_naming_combo.addItems([
            "Adobe / Lightroom / PhotoLab  (photo.xmp)",
            "Darktable / DigiKam  (photo.cr2.xmp)",
        ])
        self.sidecar_naming_combo.setCurrentIndex(0)
        self.sidecar_naming_combo.setFixedWidth(300)
        sidecar_naming_row.addWidget(self.sidecar_naming_combo)
        sidecar_naming_row.addStretch()
        options_layout.addLayout(sidecar_naming_row)
        self.sidecar_check.toggled.connect(self.sidecar_naming_combo.setEnabled)
        self.sidecar_check.toggled.connect(sidecar_naming_label.setEnabled)

        response_length_row = QHBoxLayout()
        response_length_label = QLabel("Keyword density:")
        response_length_label.setStyleSheet("color: #8f8d89; font-size: 12px;")
        response_length_label.setFixedWidth(110)
        response_length_row.addWidget(response_length_label)
        self.response_length_combo = QComboBox()
        self.response_length_combo.addItems(["Fewer keywords", "Standard", "More keywords"])
        self.response_length_combo.setCurrentIndex(1)
        self.response_length_combo.setFixedWidth(200)
        response_length_row.addWidget(self.response_length_combo)
        response_length_row.addStretch()
        options_layout.addLayout(response_length_row)

        settings_layout.addWidget(options_group)
        settings_layout.addStretch()

        self.tabs.addTab(settings_tab, "Settings")

        # ── Keywords Tab ──
        keywords_tab = QWidget()
        kw_layout = QVBoxLayout(keywords_tab)

        kw_desc = QLabel(
            "Optional: supply a keyword vocabulary. The model will prefer "
            "these terms where applicable, giving you consistent keywording."
        )
        kw_desc.setWordWrap(True)
        kw_desc.setStyleSheet("color: #7e7c78; font-size: 12px; margin-bottom: 8px;")
        kw_layout.addWidget(kw_desc)

        self.keywords_edit = QPlainTextEdit()
        self.keywords_edit.setPlaceholderText(
            "One keyword per line, or comma-separated.\n\n"
            "landscape, seascape, golden hour, blue hour,\n"
            "sunrise, sunset, cloudy, stormy, misty..."
        )
        kw_layout.addWidget(self.keywords_edit)

        kw_btn_row = QHBoxLayout()
        load_kw_btn = QPushButton("Load from File")
        load_kw_btn.clicked.connect(self._load_keywords)
        kw_btn_row.addWidget(load_kw_btn)
        clear_kw_btn = QPushButton("Clear")
        clear_kw_btn.clicked.connect(self.keywords_edit.clear)
        kw_btn_row.addWidget(clear_kw_btn)
        kw_btn_row.addStretch()
        kw_layout.addLayout(kw_btn_row)

        self.tabs.addTab(keywords_tab, "Keywords")

        # ── Folder Presets Tab ──
        presets_tab = QWidget()
        presets_layout = QVBoxLayout(presets_tab)

        presets_desc = QLabel(
            "Define rules to auto-apply prompt presets or keyword files based on "
            "folder names. When photos are loaded, if the detected folder name "
            "contains a match, the corresponding preset/keywords are applied."
        )
        presets_desc.setWordWrap(True)
        presets_desc.setStyleSheet("color: #7e7c78; font-size: 12px; margin-bottom: 8px;")
        presets_layout.addWidget(presets_desc)

        self.presets_table = QTableWidget()
        self.presets_table.setColumnCount(3)
        self.presets_table.setHorizontalHeaderLabels(["Folder Contains", "Prompt Preset", "Keywords File"])
        self.presets_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.presets_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.presets_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.presets_table.verticalHeader().setVisible(False)
        self.presets_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.presets_table.setSelectionMode(QAbstractItemView.SingleSelection)
        presets_layout.addWidget(self.presets_table)

        presets_btn_row = QHBoxLayout()
        add_preset_btn = QPushButton("Add Rule")
        add_preset_btn.clicked.connect(self._add_folder_preset)
        presets_btn_row.addWidget(add_preset_btn)
        remove_preset_btn = QPushButton("Remove Selected")
        remove_preset_btn.clicked.connect(self._remove_folder_preset)
        presets_btn_row.addWidget(remove_preset_btn)
        presets_btn_row.addStretch()
        presets_layout.addLayout(presets_btn_row)

        self.tabs.addTab(presets_tab, "Folder Presets")

        # ── Results Tab ──
        results_tab = QWidget()
        results_layout = QVBoxLayout(results_tab)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.setSpacing(0)

        results_splitter = QSplitter(Qt.Horizontal)
        results_splitter.setHandleWidth(3)

        # Left: file list
        results_list_widget = QWidget()
        results_list_layout = QVBoxLayout(results_list_widget)
        results_list_layout.setContentsMargins(0, 0, 0, 0)
        results_list_layout.setSpacing(4)

        self.results_table = QTableWidget()
        self.results_table.setColumnCount(2)
        self.results_table.setHorizontalHeaderLabels(["", "Filename"])
        self.results_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.results_table.setColumnWidth(0, 16)
        self.results_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.results_table.verticalHeader().setVisible(False)
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.results_table.setShowGrid(False)
        self.results_table.setAlternatingRowColors(True)
        self.results_table.setStyleSheet(
            self.results_table.styleSheet() +
            "QTableWidget { alternate-background-color: #1f1f23; }"
        )
        self.results_table.currentCellChanged.connect(self._on_result_selected)
        results_list_layout.addWidget(self.results_table)

        results_nav_row = QHBoxLayout()
        self.results_prev_btn = QPushButton("◀ Prev")
        self.results_prev_btn.setFixedHeight(28)
        self.results_prev_btn.setStyleSheet("font-size: 11px; padding: 2px 10px;")
        self.results_prev_btn.clicked.connect(self._results_prev)
        results_nav_row.addWidget(self.results_prev_btn)

        self.results_pos_label = QLabel("0 / 0")
        self.results_pos_label.setAlignment(Qt.AlignCenter)
        self.results_pos_label.setStyleSheet("color: #7e7c78; font-size: 11px;")
        results_nav_row.addWidget(self.results_pos_label)

        self.results_next_btn = QPushButton("Next ▶")
        self.results_next_btn.setFixedHeight(28)
        self.results_next_btn.setStyleSheet("font-size: 11px; padding: 2px 10px;")
        self.results_next_btn.clicked.connect(self._results_next)
        results_nav_row.addWidget(self.results_next_btn)
        results_list_layout.addLayout(results_nav_row)

        results_splitter.addWidget(results_list_widget)

        # Right: detail/edit panel
        detail_widget = QWidget()
        detail_layout = QVBoxLayout(detail_widget)
        # Right margin leaves room for the scroll bar beside the copy buttons.
        detail_layout.setContentsMargins(8, 0, 14, 0)
        detail_layout.setSpacing(8)

        # Folder path in results
        self.detail_folder = QLabel("")
        self.detail_folder.setStyleSheet(
            "font-size: 11px; color: #7e7c78; border: none; padding: 0;"
        )
        detail_layout.addWidget(self.detail_folder)

        # Filename header
        self.detail_filename = QLabel("Select a photo to view metadata")
        self.detail_filename.setStyleSheet(
            "font-size: 14px; font-weight: 600; color: #d1935e; "
            "padding: 4px 0; border: none;"
        )
        detail_layout.addWidget(self.detail_filename)

        # Photo preview
        self.detail_preview = QLabel()
        self.detail_preview.setFixedHeight(380)
        # Ignore the pixmap's width when sizing: otherwise a 720px preview
        # forces the panel wider than its scroll area and the right-hand edge
        # (including the copy buttons) is clipped off.
        self.detail_preview.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.detail_preview.setAlignment(Qt.AlignCenter)
        self.detail_preview.setStyleSheet(
            "background-color: #17171a; border: 1px solid #2a2a2f; "
            "border-radius: 6px; color: #5f5f66; font-size: 12px;"
        )
        self.detail_preview.setText("No preview")
        detail_layout.addWidget(self.detail_preview)

        # Title
        self.detail_title_label = QLabel("TITLE")
        self.detail_title_label.setStyleSheet(
            "color: #7e7c78; font-size: 10px; font-weight: 600; "
            "letter-spacing: 1px; margin-top: 4px; border: none;"
        )
        detail_layout.addLayout(self._field_header(
            self.detail_title_label,
            self._make_copy_button(lambda: self.detail_title.text(), "title")))
        self.detail_title = QLineEdit()
        self.detail_title.setPlaceholderText("Title")
        self.detail_title.textChanged.connect(self._on_detail_edited)
        detail_layout.addWidget(self.detail_title)

        # Caption
        self.detail_caption_label = QLabel("CAPTION")
        self.detail_caption_label.setStyleSheet(
            "color: #7e7c78; font-size: 10px; font-weight: 600; "
            "letter-spacing: 1px; margin-top: 4px; border: none;"
        )
        detail_layout.addLayout(self._field_header(
            self.detail_caption_label,
            self._make_copy_button(
                lambda: self.detail_caption.toPlainText(), "caption")))
        self.detail_caption = QTextEdit()
        self.detail_caption.setPlaceholderText("Caption / description")
        self.detail_caption.setMinimumHeight(80)
        self.detail_caption.setMaximumHeight(140)
        self.detail_caption.textChanged.connect(self._on_detail_edited)
        detail_layout.addWidget(self.detail_caption)

        # Keywords
        kw_label = QLabel("KEYWORDS")
        kw_label.setStyleSheet(
            "color: #7e7c78; font-size: 10px; font-weight: 600; "
            "letter-spacing: 1px; margin-top: 4px; border: none;"
        )
        detail_layout.addLayout(self._field_header(
            kw_label,
            self._make_copy_button(
                lambda: self.detail_keywords.toPlainText(), "keywords")))
        self.detail_keywords = QTextEdit()
        self.detail_keywords.setPlaceholderText(
            "Comma-separated keywords"
        )
        self.detail_keywords.setMinimumHeight(80)
        self.detail_keywords.setMaximumHeight(120)
        self.detail_keywords.textChanged.connect(self._on_detail_edited)
        detail_layout.addWidget(self.detail_keywords)

        # Keyword count
        self.kw_count_label = QLabel("")
        self.kw_count_label.setStyleSheet("color: #5f5f66; font-size: 11px; border: none;")
        detail_layout.addWidget(self.kw_count_label)

        # Posts: one box per platform, shown only when the photo has one.
        self.posts_header = QLabel("POSTS · copy and paste, not written to the file")
        self.posts_header.setStyleSheet(
            "color: #d1935e; font-size: 10px; font-weight: 700; "
            "letter-spacing: 1px; margin-top: 10px; border: none;"
        )
        self.write_posts_btn = QPushButton("Write posts")
        self.write_posts_btn.setFixedHeight(26)
        self.write_posts_btn.setStyleSheet("font-size: 11px; padding: 2px 12px;")
        self.write_posts_btn.clicked.connect(self._write_posts_for_current)
        posts_row = QHBoxLayout()
        posts_row.setContentsMargins(0, 0, 0, 0)
        posts_row.addWidget(self.posts_header)
        posts_row.addStretch()
        posts_row.addWidget(self.write_posts_btn)
        self.posts_row_widget = QWidget()
        self.posts_row_widget.setLayout(posts_row)
        self.posts_row_widget.setVisible(False)
        detail_layout.addWidget(self.posts_row_widget)
        self.post_boxes = {}
        for pid, label, limit, _ in POST_TARGETS:
            box = QWidget()
            box_layout = QVBoxLayout(box)
            box_layout.setContentsMargins(0, 0, 0, 0)
            box_layout.setSpacing(4)
            name = QLabel(label.upper())
            name.setStyleSheet(
                "color: #7e7c78; font-size: 10px; font-weight: 600; "
                "letter-spacing: 1px; margin-top: 4px; border: none;"
            )
            count = QLabel("")
            count.setStyleSheet("color: #5f5f66; font-size: 11px; border: none;")
            edit = QTextEdit()
            # Tall enough to read a typical post without an inner scroll bar;
            # the short formats get short boxes.
            short = limit is not None and limit <= 300
            edit.setMinimumHeight(78 if short else 128)
            edit.setMaximumHeight(110 if short else 220)
            edit.textChanged.connect(self._on_post_edited)
            btn = self._make_copy_button(
                lambda e=edit: e.toPlainText(), f"{label} post")
            head = self._field_header(name, btn)
            head.insertWidget(2, count)
            box_layout.addLayout(head)
            box_layout.addWidget(edit)
            box.setVisible(False)
            detail_layout.addWidget(box)
            self.post_boxes[pid] = (box, edit, count, btn)

        detail_layout.addStretch()

        # Scrollable, since a photo with several posts runs well past the
        # height of the window.
        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setFrameShape(QFrame.NoFrame)
        detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        detail_scroll.setWidget(detail_widget)
        results_splitter.addWidget(detail_scroll)
        results_splitter.setSizes([250, 500])

        results_layout.addWidget(results_splitter)
        self._results_tab_index = self.tabs.addTab(results_tab, "Results")

        # Track which result is selected
        self._current_result_index = -1
        self._updating_detail = False

        # ── Log Tab ──
        log_tab = QWidget()
        log_layout = QVBoxLayout(log_tab)
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet(
            "font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace; "
            "font-size: 11px; color: #7e7c78;"
        )
        log_layout.addWidget(self.log_text)
        self.tabs.addTab(log_tab, "Log")

        right_layout.addWidget(self.tabs, 1)

        # ── Action buttons ──
        action_row = QHBoxLayout()

        self.generate_btn = QPushButton("▶  Generate Metadata")
        self.generate_btn.setObjectName("primaryBtn")
        self.generate_btn.setMinimumHeight(40)
        self.generate_btn.clicked.connect(self._start_processing)
        action_row.addWidget(self.generate_btn)

        self.stop_btn = QPushButton("■  Stop")
        self.stop_btn.setObjectName("dangerBtn")
        self.stop_btn.setMinimumHeight(40)
        self.stop_btn.setVisible(False)
        self.stop_btn.clicked.connect(self._stop_processing)
        action_row.addWidget(self.stop_btn)

        self.write_btn = QPushButton("💾  Write Metadata to Files")
        self.write_btn.setObjectName("writeBtn")
        self.write_btn.setMinimumHeight(40)
        self.write_btn.setEnabled(False)
        self.write_btn.clicked.connect(self._write_metadata)
        action_row.addWidget(self.write_btn)

        self.export_btn = QPushButton("📋  Export CSV")
        self.export_btn.setObjectName("exportBtn")
        self.export_btn.setMinimumHeight(40)
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._export_csv)
        action_row.addWidget(self.export_btn)

        self.import_btn = QPushButton("📥  Import CSV")
        self.import_btn.setObjectName("exportBtn")
        self.import_btn.setMinimumHeight(40)
        self.import_btn.clicked.connect(self._import_csv)
        action_row.addWidget(self.import_btn)

        right_layout.addLayout(action_row)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setTextVisible(False)
        right_layout.addWidget(self.progress_bar)

        # Status bar
        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("statusLabel")
        right_layout.addWidget(self.status_label)

        splitter.addWidget(right_panel)
        splitter.setSizes([400, 700])

        main_layout.addWidget(splitter, 1)

        # ── Footer with logo ──
        logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
        if os.path.exists(logo_path):
            footer = QHBoxLayout()
            footer.setContentsMargins(0, 4, 0, 0)
            logo_label = QLabel()
            logo_pixmap = QPixmap(logo_path)
            logo_pixmap = logo_pixmap.scaledToHeight(32, Qt.SmoothTransformation)
            logo_label.setPixmap(logo_pixmap)
            logo_label.setStyleSheet("border: none; opacity: 0.6;")
            footer.addWidget(logo_label)
            footer.addStretch()
            main_layout.addLayout(footer)

        # Kick off model check
        QTimer.singleShot(500, self._refresh_models)

    # ── Settings persistence ──

    def _load_settings(self):
        url = self.settings.value("ollama_url", "http://localhost:1234")
        # Migrate anyone who accidentally saved the wrong default
        if not url:
            url = "http://localhost:11434"
        self.ollama_url.setText(url)
        prompt = self.settings.value("prompt", "")
        if prompt:
            self.prompt_edit.setText(prompt)
        photographer = self.settings.value("photographer", "")
        if photographer:
            self.context_fields["ctx_photographer"].setText(photographer)
        backup = self.settings.value("create_backup", "true")
        self.backup_check.setChecked(backup == "true")
        append_kw = self.settings.value("append_keywords", "false")
        self.append_keywords_check.setChecked(append_kw == "true")
        skip_existing = self.settings.value("skip_existing", "false")
        self.skip_existing_check.setChecked(skip_existing == "true")
        auto_write = self.settings.value("auto_write", "false")
        self.auto_write_check.setChecked(auto_write == "true")
        sidecar = self.settings.value("use_sidecar", "true")
        self.sidecar_check.setChecked(sidecar == "true")
        sidecar_naming = self.settings.value("sidecar_naming", "0")
        self.sidecar_naming_combo.setCurrentIndex(int(sidecar_naming))
        response_length = self.settings.value("response_length", "1")
        self.response_length_combo.setCurrentIndex(int(response_length))
        keywords_vocab = self.settings.value("keywords_vocab", "")
        if keywords_vocab:
            self.keywords_edit.setPlainText(keywords_vocab)
        # These used to reset on every launch, so a ticked location lookup
        # silently turned itself off between sessions (#18 follow-up).
        gps_lookup = self.settings.value("gps_lookup", "false")
        # Restoring a ticked box must not re-open the one-time consent dialog.
        self.gps_lookup_check.blockSignals(True)
        self.gps_lookup_check.setChecked(gps_lookup == "true")
        self.gps_lookup_check.blockSignals(False)
        folder_context = self.settings.value("folder_context", "true")
        self.folder_context_check.setChecked(folder_context == "true")
        exif_date_fallback = self.settings.value("exif_date_fallback", "true")
        self.exif_date_fallback_check.setChecked(exif_date_fallback == "true")
        idx = self.file_style_combo.findData(
            self.settings.value("file_style", "catalogue"))
        self.file_style_combo.setCurrentIndex(max(idx, 0))
        saved_posts = set(filter(None, str(
            self.settings.value("post_targets", "") or "").split(",")))
        for pid, cb in self.post_checks.items():
            cb.setChecked(pid in saved_posts)
        self.first_person_check.setChecked(
            self.settings.value("posts_first_person", "false") == "true")
        self.batch_posts_check.setChecked(
            self.settings.value("posts_in_batch", "false") == "true")
        self._update_first_person_enabled()
        self._load_folder_presets()

    def _save_settings(self):
        self.settings.setValue("ollama_url", self.ollama_url.text())
        self.settings.setValue("prompt", self.prompt_edit.toPlainText())
        self.settings.setValue(
            "create_backup",
            "true" if self.backup_check.isChecked() else "false"
        )
        self.settings.setValue(
            "append_keywords",
            "true" if self.append_keywords_check.isChecked() else "false"
        )
        self.settings.setValue(
            "skip_existing",
            "true" if self.skip_existing_check.isChecked() else "false"
        )
        self.settings.setValue(
            "auto_write",
            "true" if self.auto_write_check.isChecked() else "false"
        )
        self.settings.setValue(
            "use_sidecar",
            "true" if self.sidecar_check.isChecked() else "false"
        )
        self.settings.setValue(
            "gps_lookup",
            "true" if self.gps_lookup_check.isChecked() else "false"
        )
        self.settings.setValue(
            "folder_context",
            "true" if self.folder_context_check.isChecked() else "false"
        )
        self.settings.setValue(
            "exif_date_fallback",
            "true" if self.exif_date_fallback_check.isChecked() else "false"
        )
        self.settings.setValue(
            "file_style", self.file_style_combo.currentData() or "catalogue")
        self.settings.setValue(
            "post_targets", ",".join(self._selected_post_targets()))
        self.settings.setValue(
            "posts_first_person",
            "true" if self.first_person_check.isChecked() else "false"
        )
        self.settings.setValue(
            "posts_in_batch",
            "true" if self.batch_posts_check.isChecked() else "false"
        )
        self.settings.setValue("sidecar_naming", str(self.sidecar_naming_combo.currentIndex()))
        self.settings.setValue("response_length", str(self.response_length_combo.currentIndex()))
        self.settings.setValue(
            "photographer",
            self.context_fields["ctx_photographer"].text()
        )
        self.settings.setValue("keywords_vocab", self.keywords_edit.toPlainText())
        self._save_folder_presets()

    def closeEvent(self, event):
        self._save_settings()
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(3000)
        # Save progress if there are processed photos
        if any(p.status == "done" for p in self.photos):
            self._save_progress()
        event.accept()

    # ── Logging ──

    def _worker_settings(self, post_targets, posts_only=False, photos=None):
        """Everything a generation run needs from the UI, shared by Generate
        and by Write posts so the two can't drift apart."""
        max_tokens_map = {0: 2048, 1: 4096, 2: 8192}
        base = 1024 if posts_only else max_tokens_map.get(
            self.response_length_combo.currentIndex(), 2048)
        return dict(
            photos=self.photos if photos is None else photos,
            model=self.model_combo.currentText(),
            prompt=self.prompt_edit.toPlainText(),
            context=self._get_context_string(),
            ollama_url=self.ollama_url.text().rstrip("/"),
            keywords_list=self._get_keywords_list(),
            backend=getattr(self, "backend", "ollama"),
            # Each post adds a few hundred tokens of output on top.
            max_tokens=base + 300 * len(post_targets),
            describe_people=self.describe_people_check.isChecked(),
            skip_existing=self.skip_existing_check.isChecked(),
            gps_lookup=self.gps_lookup_check.isChecked(),
            has_manual_location=bool(
                self.context_fields["ctx_location"].text().strip()),
            exif_date=self.exif_date_fallback_check.isChecked(),
            has_manual_date=bool(
                self.context_fields["ctx_datetime"].text().strip()),
            file_style=self.file_style_combo.currentData() or "catalogue",
            post_targets=post_targets,
            first_person=self.first_person_check.isChecked(),
            posts_only=posts_only,
        )

    def _write_posts_for_current(self):
        """Results → Write posts: write the ticked posts for the photo on
        screen, leaving its title, caption and keywords alone."""
        targets = self._selected_post_targets()
        completed = self._get_completed_photos()
        idx = self._current_result_index
        if not targets or not (0 <= idx < len(completed)):
            return
        if (self.worker and self.worker.isRunning()) or \
                (self._posts_worker and self._posts_worker.isRunning()):
            self.status_label.setText("Wait for the current generation to finish")
            return
        if not self.model_combo.currentText():
            self.status_label.setText("No model selected — click Refresh to connect")
            return
        photo = completed[idx]
        self._posts_photo = photo
        self._posts_worker = OllamaWorker(**self._worker_settings(
            post_targets=targets, posts_only=True, photos=[photo]))
        self._posts_worker.result.connect(self._on_posts_result)
        self._posts_worker.log_message.connect(self.log)
        self._posts_worker.finished_all.connect(self._on_posts_finished)
        self.write_posts_btn.setEnabled(False)
        self.write_posts_btn.setText("Writing posts…")
        self.status_label.setText(f"Writing posts for {photo.filename}…")
        self._posts_worker.start()

    def _on_posts_result(self, index, result):
        photo = getattr(self, "_posts_photo", None)
        if photo is None:
            return
        if isinstance(result, PhotoMetadata):
            # Merge: a rewrite replaces the platforms asked for and keeps any
            # others the photo already had.
            photo.metadata.posts.update(result.posts)
            self._save_progress()
            completed = self._get_completed_photos()
            if 0 <= self._current_result_index < len(completed) and \
                    completed[self._current_result_index] is photo:
                self._load_detail(photo)
            self.status_label.setText(f"Posts written for {photo.filename}")
        else:
            self.status_label.setText(f"Couldn't write posts: {result}")

    def _on_posts_finished(self):
        self._posts_photo = None
        QTimer.singleShot(500, lambda: setattr(self, "_posts_worker", None))
        self._update_write_posts_btn()

    def _update_write_posts_btn(self, *_):
        """Enabled only with a photo on screen and at least one post ticked."""
        if not hasattr(self, "write_posts_btn"):
            return
        busy = bool(getattr(self, "_posts_worker", None)
                    and self._posts_worker.isRunning())
        completed = self._get_completed_photos() if hasattr(self, "photos") else []
        idx = getattr(self, "_current_result_index", -1)
        photo = completed[idx] if 0 <= idx < len(completed) else None
        has_posts = bool(photo and (photo.metadata.posts or {}))
        targets = self._selected_post_targets()
        self.write_posts_btn.setText("Rewrite posts" if has_posts else "Write posts")
        self.write_posts_btn.setEnabled(bool(photo and targets and not busy))
        self.write_posts_btn.setToolTip(
            "Write the posts ticked in Settings → Outputs for this photo."
            if targets else
            "Tick the posts you want in Settings → Outputs first."
        )

    def _selected_post_targets(self):
        return [pid for pid in POST_TARGET_IDS if self.post_checks[pid].isChecked()]

    def _update_first_person_enabled(self, *_):
        """Voice and batch writing only matter when there's a post to write."""
        any_posts = bool(self._selected_post_targets())
        if hasattr(self, "first_person_check"):
            self.first_person_check.setEnabled(any_posts)
        if hasattr(self, "batch_posts_check"):
            self.batch_posts_check.setEnabled(any_posts)
        self._update_write_posts_btn()

    @staticmethod
    def _field_header(label, button):
        """A field label with a copy button at the right-hand end."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(label)
        row.addStretch()
        row.addWidget(button)
        return row

    def _copy_icons(self):
        """Painted (not font-glyph) icons, so they look the same on every OS:
        two overlapping sheets for copy, a tick once copied."""
        if getattr(self, "_icons", None):
            return self._icons
        def paint(draw, colour):
            pm = QPixmap(32, 32)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            p.scale(2, 2)
            pen = QPen(QColor(colour))
            pen.setWidthF(1.3)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            draw(p)
            p.end()
            return QIcon(pm)
        def sheets(p):
            p.drawRoundedRect(QRectF(5.5, 5.5, 8, 8), 1.8, 1.8)
            back = QPainterPath()
            back.moveTo(4.5, 10.5)
            back.lineTo(3.8, 10.5)
            back.quadTo(2.5, 10.5, 2.5, 9.2)
            back.lineTo(2.5, 3.8)
            back.quadTo(2.5, 2.5, 3.8, 2.5)
            back.lineTo(9.2, 2.5)
            back.quadTo(10.5, 2.5, 10.5, 3.8)
            back.lineTo(10.5, 4.5)
            p.drawPath(back)
        def tick(p):
            path = QPainterPath()
            path.moveTo(3.5, 8.5)
            path.lineTo(6.5, 11.5)
            path.lineTo(12.5, 4.5)
            p.drawPath(path)
        self._icons = (paint(sheets, "#8f8d89"), paint(tick, "#7bc9a0"))
        return self._icons

    def _make_copy_button(self, get_text, what):
        """A small copy-to-clipboard button. Shows a tick for a moment after
        copying, so it's clear something happened."""
        copy_icon, done_icon = self._copy_icons()
        btn = QToolButton()
        btn.setIcon(copy_icon)
        btn.setIconSize(QSize(16, 16))
        btn.setFixedSize(26, 22)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip(f"Copy {what}")
        btn.setStyleSheet(
            "QToolButton { border: none; background: transparent; "
            "border-radius: 5px; padding: 2px; }"
            "QToolButton:hover { background-color: #232326; }"
        )
        def do_copy():
            text = get_text() or ""
            if not text.strip():
                return
            QApplication.clipboard().setText(text)
            btn.setIcon(done_icon)
            QTimer.singleShot(1200, lambda: btn.setIcon(copy_icon))
            if hasattr(self, "status_label"):
                self.status_label.setText(f"Copied {what} to the clipboard")
        btn.clicked.connect(do_copy)
        return btn

    def _set_status_pill(self, text, state):
        """Update the header connection pill. `state` is on / off / wait —
        the stylesheet picks the colour off the property, so re-polish it."""
        self.ollama_status.setText(text)
        self.ollama_status.setProperty("state", state)
        self.ollama_status.style().unpolish(self.ollama_status)
        self.ollama_status.style().polish(self.ollama_status)

    def log(self, msg):
        self.log_text.appendPlainText(msg)

    # ── Model management ──

    def _refresh_models(self):
        url = self.ollama_url.text().rstrip("/")
        self.backend = "ollama"  # default; overridden below if OpenAI API detected

        model_names = []
        backend_label = ""
        connected_url = url

        # Known fallback URLs to probe if the configured URL fails
        FALLBACK_URLS = ["http://localhost:1234", "http://localhost:11434"]

        def _probe(probe_url):
            """Try Ollama then OpenAI-compatible API at probe_url."""
            # Ollama
            try:
                resp = requests.get(f"{probe_url}/api/tags", timeout=3)
                resp.raise_for_status()
                names = [m["name"] for m in resp.json().get("models", [])]
                if names:
                    return names, "ollama", "Ollama"
            except Exception:
                pass
            # OpenAI-compatible (LM Studio, Jan, etc.)
            try:
                resp = requests.get(f"{probe_url}/v1/models", timeout=3)
                resp.raise_for_status()
                SKIP = ("embed", "rerank", "whisper", "tts", "dall-e")
                names = [
                    m["id"] for m in resp.json().get("data", [])
                    if isinstance(m, dict)
                    and not any(s in m["id"].lower() for s in SKIP)
                ]
                if names:
                    return names, "openai", "LM Studio"
            except Exception:
                pass
            return [], None, ""

        # Try configured URL first
        model_names, backend, backend_label = _probe(url)

        # If that failed, silently try known fallback URLs
        if not model_names:
            for fallback in FALLBACK_URLS:
                if fallback.rstrip("/") == url:
                    continue
                model_names, backend, backend_label = _probe(fallback)
                if model_names:
                    connected_url = fallback
                    self.ollama_url.setText(fallback)
                    break

        if backend:
            self.backend = backend

        if model_names:
            # Sort: prefer gemma models first, then alphabetical
            model_names = sorted(
                model_names,
                key=lambda n: (0 if "gemma" in n.lower() else 1, n)
            )
            self.model_combo.clear()
            self.model_combo.addItems(model_names)

            # Default to a Gemma 12B if there is one (4, then 3), else the first.
            for wanted in ("gemma4:12b", "gemma-4-12b", "gemma3:12b", "gemma-3-12b"):
                hit = next((i for i, n in enumerate(model_names)
                            if wanted in n.lower()), None)
                if hit is not None:
                    self.model_combo.setCurrentIndex(hit)
                    break

            n = len(model_names)
            self._set_status_pill(
                f"● {backend_label} · {n} model{'s' if n != 1 else ''}", "on")
            self.log(f"Connected to {backend_label} at {connected_url} ({len(model_names)} models)")
        else:
            self._set_status_pill("● Not connected", "off")
            self.log(
                f"Cannot connect at {url}\n"
                "  Ollama default:   http://localhost:11434\n"
                "  LM Studio default: http://localhost:1234"
            )
            self.model_combo.clear()

    # ── File handling ──

    def _browse_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select Photos", "",
            "Images (*.jpg *.jpeg *.tif *.tiff *.png *.heic *.heif *.dng *.webp "
            "*.cr2 *.cr3 *.nef *.arw *.orf *.raf *.rw2 *.pef *.srw *.raw)"
        )
        if files:
            self._on_files_dropped(files)

    def _browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder")
        if folder:
            files = []
            for root, dirs, fnames in os.walk(folder):
                for f in fnames:
                    fp = os.path.join(root, f)
                    if _is_supported_file(fp):
                        files.append(fp)
            if files:
                self._on_files_dropped(files)

    def _on_files_dropped(self, filepaths):
        existing = {p.filepath for p in self.photos}
        new_files = [f for f in filepaths if f not in existing]
        if not new_files:
            return
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(len(new_files))
        self.progress_bar.setValue(0)
        self.status_label.setText(f"Loading {len(new_files)} files...")
        self._file_loader = FileLoaderWorker(new_files)
        self._file_loader.progress.connect(self._on_file_load_progress)
        self._file_loader.finished_loading.connect(self._on_file_load_finished)
        self._file_loader.start()

    def _on_file_load_progress(self, current, total):
        self.progress_bar.setValue(current)

    def _on_file_load_finished(self, items):
        self.progress_bar.setVisible(False)
        self.photos.extend(items)
        self._refresh_photo_table()
        self.log(f"Added {len(items)} photos ({len(self.photos)} total)")

        # Show source folder name
        if items:
            parent_folder = os.path.basename(os.path.dirname(items[0].filepath))
            if parent_folder:
                self.folder_name_label.setText(f"\U0001f4c1 {parent_folder}")

        self.status_label.setText("Ready")

        # Detect and apply folder context
        filepaths = [item.filepath for item in items]
        self._detect_and_apply_folder_context(filepaths)

        # Try to restore progress from a previous session
        self._load_progress(filepaths)

        self._file_loader = None

    def _clear_all(self):
        # Delete this folder's resume file first — it's keyed off the first
        # photo's directory, so we have to do it before dropping the list.
        progress_file = self._get_progress_filepath()
        if progress_file and os.path.isfile(progress_file):
            try:
                os.remove(progress_file)
                self.log("Cleared saved progress for this folder")
            except Exception:
                pass
        self.photos.clear()
        self.folder_name_label.setText("")
        # Drop anything we auto-detected for the folder just cleared, so the
        # next batch starts clean instead of inheriting the last one's context.
        self._clear_folder_context()
        self._current_result_index = -1
        self._refresh_photo_table()
        self._refresh_results_table()
        # Clear detail panel
        self._updating_detail = True
        self.detail_folder.setText("")
        self.detail_filename.setText("Select a photo to view metadata")
        self.detail_title.clear()
        self.detail_caption.clear()
        self.detail_keywords.clear()
        self.kw_count_label.setText("")
        for box, edit, _, _ in self.post_boxes.values():
            edit.clear()
            box.setVisible(False)
        self.posts_row_widget.setVisible(False)
        self._updating_detail = False
        self.log("Cleared all photos")

    def _refresh_photo_table(self):
        self.photo_table.setRowCount(len(self.photos))
        for row, photo in enumerate(self.photos):
            # Status dot
            dot = StatusDot(photo.status)
            container = QWidget()
            cl = QHBoxLayout(container)
            cl.setContentsMargins(4, 0, 0, 0)
            cl.addWidget(dot)
            self.photo_table.setCellWidget(row, 0, container)

            # Filename
            item = QTableWidgetItem(photo.filename)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            if photo.status == "error":
                item.setForeground(QColor("#e2796a"))
            self.photo_table.setItem(row, 1, item)

            # Status text
            status_item = QTableWidgetItem(photo.status.capitalize())
            status_item.setFlags(status_item.flags() & ~Qt.ItemIsEditable)
            status_colours = {
                "pending": "#5f5f66",
                "processing": "#d1935e",
                "done": "#7bc9a0",
                "error": "#e2796a",
            }
            status_item.setForeground(QColor(status_colours.get(photo.status, "#5f5f66")))
            self.photo_table.setItem(row, 2, status_item)

            # Title preview
            title_text = photo.metadata.title if photo.metadata else ""
            title_item = QTableWidgetItem(title_text)
            title_item.setFlags(title_item.flags() & ~Qt.ItemIsEditable)
            self.photo_table.setItem(row, 3, title_item)

            self.photo_table.setRowHeight(row, 32)

        total = len(self.photos)
        done = sum(1 for p in self.photos if p.status == "done")
        self.photo_count_label.setText(f"{total} photos loaded, {done} processed")

    def _update_photo_row(self, row):
        """Update a single row in the photo table (avoids full refresh)."""
        if row < 0 or row >= len(self.photos):
            return
        photo = self.photos[row]
        dot = StatusDot(photo.status)
        container = QWidget()
        cl = QHBoxLayout(container)
        cl.setContentsMargins(4, 0, 0, 0)
        cl.addWidget(dot)
        self.photo_table.setCellWidget(row, 0, container)
        item = QTableWidgetItem(photo.filename)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        if photo.status == "error":
            item.setForeground(QColor("#e2796a"))
        self.photo_table.setItem(row, 1, item)
        status_item = QTableWidgetItem(photo.status.capitalize())
        status_item.setFlags(status_item.flags() & ~Qt.ItemIsEditable)
        status_colours = {"pending": "#5f5f66", "processing": "#d1935e", "done": "#7bc9a0", "error": "#e2796a"}
        status_item.setForeground(QColor(status_colours.get(photo.status, "#5f5f66")))
        self.photo_table.setItem(row, 2, status_item)
        title_text = photo.metadata.title if photo.metadata else ""
        title_item = QTableWidgetItem(title_text)
        title_item.setFlags(title_item.flags() & ~Qt.ItemIsEditable)
        self.photo_table.setItem(row, 3, title_item)
        total = len(self.photos)
        done = sum(1 for p in self.photos if p.status == "done")
        self.photo_count_label.setText(f"{total} photos loaded, {done} processed")

    def _photo_context_menu(self, pos):
        """Right-click menu on the photo list: regenerate photos (e.g. after
        changing settings). Resets them to pending so Generate reprocesses."""
        if not self.photos:
            return
        if self.worker and self.worker.isRunning():
            return  # not while a batch is running
        row = self.photo_table.rowAt(pos.y())
        menu = QMenu(self)
        if 0 <= row < len(self.photos):
            act = menu.addAction("Regenerate this photo")
            act.triggered.connect(lambda: self._regenerate_photos([row]))
        act_all = menu.addAction("Regenerate all photos")
        act_all.triggered.connect(
            lambda: self._regenerate_photos(range(len(self.photos))))
        menu.exec(self.photo_table.viewport().mapToGlobal(pos))

    def _regenerate_photos(self, indices):
        """Reset the given photos to pending and (re)run generation on them."""
        if self.worker and self.worker.isRunning():
            return
        count = 0
        for i in indices:
            if 0 <= i < len(self.photos):
                self.photos[i].status = "pending"
                self._update_photo_row(i)
                count += 1
        if count == 0:
            return
        self.log(f"Regenerating {count} photo{'s' if count != 1 else ''}...")
        self._start_processing()

    def _on_photo_selected(self, row, col, prev_row, prev_col):
        pass  # Could show preview in future

    def _on_photo_double_clicked(self, row, col):
        """Double-click jumps to the photo in the Results tab."""
        if row < 0 or row >= len(self.photos):
            return
        photo = self.photos[row]
        if photo.status != "done" or not photo.metadata:
            return
        completed = self._get_completed_photos()
        try:
            result_idx = completed.index(photo)
        except ValueError:
            return
        self.tabs.setCurrentIndex(self._results_tab_index)
        self.results_table.selectRow(result_idx)

    # ── Folder context detection ──

    def _on_gps_lookup_toggled(self, checked):
        """Show one-time consent dialog when GPS lookup is first enabled."""
        if not checked:
            return
        # If user has already consented, allow silently
        if self.settings.value("gps_consent_given", "false") == "true":
            return
        # Show consent dialog
        reply = QMessageBox.question(
            self, "Location Lookup",
            "This fills the Location field from each photo's own metadata.\n\n"
            "When a photo already has a place name (City/State/Country, e.g.\n"
            "from Lightroom), that is used directly with no network request.\n"
            "Only when there is no such place name does it send the photo's\n"
            "GPS coordinates to OpenStreetMap (nominatim.openstreetmap.org)\n"
            "to look one up — latitude and longitude only, never image data.\n\n"
            "Do you want to enable this?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.settings.setValue("gps_consent_given", "true")
        else:
            # User declined — uncheck without re-triggering this handler
            self.gps_lookup_check.blockSignals(True)
            self.gps_lookup_check.setChecked(False)
            self.gps_lookup_check.blockSignals(False)

    def _on_folder_context_toggled(self, state):
        """Handle folder context checkbox toggle."""
        if not state:
            self._clear_folder_context()

    def _clear_folder_context(self):
        """Clear detected folder context, and the fields it filled in."""
        self._detected_folder_context = None
        self.folder_context_label.setText("")
        self.clear_folder_ctx_btn.setVisible(False)
        # Only clear what we auto-filled — anything typed by hand is the
        # user's, and survives.
        for key in self._autofilled_fields:
            self.context_fields[key].clear()
        self._autofilled_fields.clear()

    def _apply_folder_context(self, ctx: FolderContext):
        """Fill Location and Date/Time fields from detected context (only if empty)."""
        if not self.folder_context_check.isChecked():
            return
        self._detected_folder_context = ctx
        # Show detection indicator
        self.folder_context_label.setText(f"Detected: {ctx.raw_folder}")
        self.clear_folder_ctx_btn.setVisible(True)
        # Fill fields only if they are currently empty
        if not self.context_fields["ctx_location"].text().strip():
            self.context_fields["ctx_location"].setText(ctx.location)
            self._autofilled_fields.add("ctx_location")
        if not self.context_fields["ctx_datetime"].text().strip():
            self.context_fields["ctx_datetime"].setText(ctx.date_str)
            self._autofilled_fields.add("ctx_datetime")
        self.log(f"Folder context detected: {ctx.location}, {ctx.date_str} (from '{ctx.raw_folder}')")

    def _detect_and_apply_folder_context(self, filepaths: list):
        """On load, auto-fill the context fields. Each source is gated on its
        OWN checkbox — folder-name detection, the EXIF-date fallback, and the
        location lookup are independent. They must not require 'Use folder
        context' to be enabled (issue #18: the GPS/location lookup silently did
        nothing whenever folder context was off, because this whole method used
        to bail out early)."""
        if self.folder_context_check.isChecked():
            ctx = detect_batch_folder_context(filepaths)
            if ctx:
                self._apply_folder_context(ctx)
                self._apply_folder_presets(ctx.raw_folder)

        # NOTE: neither the location nor the capture date is auto-filled into
        # the shared context fields here. Both are resolved per photo at
        # generation time (see OllamaWorker._build_prompt), so that enabling
        # either option after loading takes effect, Regenerate picks it up, and
        # a folder spanning several places or several hours tags each photo
        # with its own rather than pinning the batch to whichever file happened
        # to be scanned first. Anything typed into those fields still overrides.

    # ── Folder presets ──

    def _add_folder_preset(self):
        """Add an empty row to the folder presets table."""
        row = self.presets_table.rowCount()
        self.presets_table.insertRow(row)
        self.presets_table.setItem(row, 0, QTableWidgetItem(""))
        self.presets_table.setItem(row, 1, QTableWidgetItem(""))
        self.presets_table.setItem(row, 2, QTableWidgetItem(""))
        self.presets_table.setRowHeight(row, 32)

    def _remove_folder_preset(self):
        """Remove the selected row from folder presets table."""
        row = self.presets_table.currentRow()
        if row >= 0:
            self.presets_table.removeRow(row)

    def _get_folder_presets(self) -> list:
        """Get all folder presets as a list of dicts."""
        presets = []
        for row in range(self.presets_table.rowCount()):
            folder_item = self.presets_table.item(row, 0)
            prompt_item = self.presets_table.item(row, 1)
            keywords_item = self.presets_table.item(row, 2)
            presets.append({
                "folder_contains": folder_item.text() if folder_item else "",
                "prompt_preset": prompt_item.text() if prompt_item else "",
                "keywords_file": keywords_item.text() if keywords_item else "",
            })
        return presets

    def _save_folder_presets(self):
        """Save folder presets to settings as JSON."""
        presets = self._get_folder_presets()
        self.settings.setValue("folder_presets", json.dumps(presets))

    def _load_folder_presets(self):
        """Load folder presets from settings."""
        data = self.settings.value("folder_presets", "")
        if not data:
            return
        try:
            presets = json.loads(data)
            self.presets_table.setRowCount(0)
            for preset in presets:
                row = self.presets_table.rowCount()
                self.presets_table.insertRow(row)
                self.presets_table.setItem(row, 0, QTableWidgetItem(preset.get("folder_contains", "")))
                self.presets_table.setItem(row, 1, QTableWidgetItem(preset.get("prompt_preset", "")))
                self.presets_table.setItem(row, 2, QTableWidgetItem(preset.get("keywords_file", "")))
                self.presets_table.setRowHeight(row, 32)
        except (json.JSONDecodeError, TypeError):
            pass

    def _apply_folder_presets(self, folder_name: str):
        """Apply matching folder presets based on folder name."""
        if not folder_name:
            return
        presets = self._get_folder_presets()
        folder_lower = folder_name.lower()
        for preset in presets:
            match_text = preset.get("folder_contains", "").strip().lower()
            if not match_text:
                continue
            if match_text in folder_lower:
                # Apply prompt preset if specified
                prompt_text = preset.get("prompt_preset", "").strip()
                if prompt_text:
                    self.prompt_edit.setText(prompt_text)
                    self.log(f"Folder preset applied prompt for '{match_text}'")
                # Load keywords file if specified
                keywords_file = preset.get("keywords_file", "").strip()
                if keywords_file and os.path.isfile(keywords_file):
                    try:
                        with open(keywords_file, "r", encoding="utf-8") as f:
                            self.keywords_edit.setPlainText(f.read())
                        self.log(f"Folder preset loaded keywords from '{keywords_file}'")
                    except Exception as e:
                        self.log(f"Error loading preset keywords: {e}")
                break  # Apply first matching rule only

    # ── Progress persistence ──

    _PROGRESS_FILENAME = ".photoscribe_progress.json"

    def _get_progress_filepath(self) -> Optional[str]:
        """Get progress file path based on first photo's directory."""
        if not self.photos:
            return None
        first_dir = os.path.dirname(self.photos[0].filepath)
        return os.path.join(first_dir, self._PROGRESS_FILENAME)

    def _save_progress(self):
        """Save current processing state to a JSON file for resume."""
        progress_file = self._get_progress_filepath()
        if not progress_file:
            return
        progress_data = []
        for photo in self.photos:
            entry = {"filepath": photo.filepath, "status": photo.status}
            if photo.metadata:
                entry["metadata"] = {
                    "title": photo.metadata.title,
                    "caption": photo.metadata.caption,
                    "keywords": photo.metadata.keywords,
                    "posts": getattr(photo.metadata, "posts", {}) or {},
                }
            if photo.error_msg:
                entry["error_msg"] = photo.error_msg
            progress_data.append(entry)
        try:
            with open(progress_file, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "photos": progress_data}, f, indent=2, ensure_ascii=False)
            self.log(f"Progress saved ({len(progress_data)} photos)")
        except Exception as e:
            self.log(f"Warning: could not save progress: {e}")

    def _load_progress(self, filepaths: list) -> int:
        """Restore progress for loaded files. Returns count of restored photos."""
        if not filepaths:
            return 0
        first_dir = os.path.dirname(filepaths[0])
        progress_file = os.path.join(first_dir, self._PROGRESS_FILENAME)
        if not os.path.isfile(progress_file):
            return 0
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return 0
        if not isinstance(data, dict) or data.get("version") != 1:
            return 0
        saved = {entry["filepath"]: entry for entry in data.get("photos", [])}
        restored = 0
        for photo in self.photos:
            if photo.filepath in saved:
                entry = saved[photo.filepath]
                if entry.get("status") == "done" and "metadata" in entry:
                    meta = entry["metadata"]
                    photo.metadata = PhotoMetadata(
                        title=meta.get("title", ""),
                        caption=meta.get("caption", ""),
                        keywords=meta.get("keywords", []),
                        posts=dict(meta.get("posts") or {}),
                    )
                    photo.status = "done"
                    restored += 1
        if restored > 0:
            self._refresh_photo_table()
            self._refresh_results_table()
            self.write_btn.setEnabled(True)
            self.export_btn.setEnabled(True)
            self.log(f"Resumed progress: {restored} photos already processed.")
        return restored

    def _clear_progress(self):
        """Delete the progress file."""
        progress_file = self._get_progress_filepath()
        if progress_file and os.path.isfile(progress_file):
            try:
                os.remove(progress_file)
            except Exception:
                pass

    # ── Processing ──

    def _get_context_string(self):
        parts = []
        field_map = {
            "ctx_location": "Location",
            "ctx_event": "Event",
            "ctx_datetime": "Date/Time",
            "ctx_photographer": "Photographer",
            "ctx_notes": "Additional context",
        }
        for key, label in field_map.items():
            val = self.context_fields[key].text().strip()
            if val:
                parts.append(f"{label}: {val}")
        return "; ".join(parts)

    def _get_keywords_list(self):
        text = self.keywords_edit.toPlainText().strip()
        if not text:
            return []
        # Handle both comma-separated and newline-separated
        keywords = []
        for line in text.split("\n"):
            for kw in line.split(","):
                kw = kw.strip()
                if kw:
                    keywords.append(kw)
        return list(dict.fromkeys(keywords))  # Deduplicate, preserve order

    def _start_processing(self):
        if not self.photos:
            self.status_label.setText("No photos loaded")
            return
        if not self.model_combo.currentText():
            self.status_label.setText("No model selected — click Refresh to connect")
            return

        pending = [p for p in self.photos if p.status != "done"]
        if not pending:
            self.status_label.setText("All photos already processed")
            return

        self.generate_btn.setVisible(False)
        self.stop_btn.setVisible(True)
        self.write_btn.setEnabled(False)
        self.export_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(len(self.photos))
        self.progress_bar.setValue(0)

        # Headroom matters more than it looks: a reasoning model that ignores
        # the "no thinking" flag can spend 1000+ tokens before writing any
        # JSON, and a short ceiling truncates the answer mid-object.
        # Posts are only written during the batch when asked to; otherwise
        # they're written per photo from Results, where they're wanted.
        batch_posts = (self._selected_post_targets()
                       if self.batch_posts_check.isChecked() else [])
        self.worker = OllamaWorker(**self._worker_settings(post_targets=batch_posts))
        self.worker.progress.connect(self._on_progress)
        self.worker.result.connect(self._on_result)
        self.worker.finished_all.connect(self._on_finished)
        self.worker.log_message.connect(self.log)
        self.worker.start()

        self._gen_start = time.monotonic()
        self._gen_total = len(pending)
        self._gen_done = 0
        self.status_label.setText(f"Processing 0/{self._gen_total}...")

    def _stop_processing(self):
        if self.worker:
            self.worker.cancel()
            self.log("Stopping...")
            self.status_label.setText("Stopping...")

    @staticmethod
    def _format_duration(seconds):
        seconds = int(round(seconds))
        if seconds < 60:
            return f"{seconds}s"
        m, s = divmod(seconds, 60)
        if m < 60:
            return f"{m}m {s:02d}s"
        h, m = divmod(m, 60)
        return f"{h}h {m:02d}m"

    def _on_progress(self, index, status):
        if index < 0 or index >= len(self.photos):
            return
        self.photos[index].status = status
        self._update_photo_row(index)
        self.progress_bar.setValue(
            sum(1 for p in self.photos if p.status in ("done", "error"))
        )

    def _on_result(self, index, result):
        if index < 0 or index >= len(self.photos):
            return
        if isinstance(result, PhotoMetadata):
            if self.dedup_keywords_check.isChecked():
                result.keywords = deduplicate_keywords(result.keywords)
            self.photos[index].metadata = result
            self.photos[index].status = "done"
        else:
            self.photos[index].status = "error"
            self.photos[index].error_msg = str(result)

        self._update_photo_row(index)
        self.progress_bar.setValue(
            sum(1 for p in self.photos if p.status in ("done", "error"))
        )

        # Live ETA based on average time per processed photo
        self._gen_done = getattr(self, "_gen_done", 0) + 1
        total = getattr(self, "_gen_total", 0)
        start = getattr(self, "_gen_start", None)
        if start and total and self._gen_done < total:
            avg = (time.monotonic() - start) / self._gen_done
            eta = avg * (total - self._gen_done)
            self.status_label.setText(
                f"Processing {self._gen_done}/{total} — ~{self._format_duration(eta)} left"
            )

    def _on_finished(self):
        self.generate_btn.setVisible(True)
        self.stop_btn.setVisible(False)
        self.progress_bar.setVisible(False)

        done = sum(1 for p in self.photos if p.status == "done")
        errors = sum(1 for p in self.photos if p.status == "error")

        # Batch timing (from the worker)
        timing = ""
        if self.worker and getattr(self.worker, "batch_processed", 0):
            total_t = self.worker.batch_total_time
            avg_t = self.worker.batch_avg_time
            timing = f" in {total_t:.1f}s (avg {avg_t:.1f}s)"

        self.status_label.setText(f"Finished: {done} processed, {errors} errors")
        if timing:
            total_photos = len(self.photos)
            self.photo_count_label.setText(
                f"{total_photos} photos loaded, {done} processed{timing}"
            )

        if done > 0:
            self.write_btn.setEnabled(True)
            self.export_btn.setEnabled(True)
            self._refresh_results_table()
            self._save_progress()

        self.worker = None

        # Auto-write to files if requested (unattended generate → write)
        if done > 0 and self.auto_write_check.isChecked():
            self.log("Auto-writing metadata to files...")
            self._write_metadata(auto=True)

    # ── Results ──

    def _refresh_results_table(self):
        completed = [p for p in self.photos if p.status == "done" and p.metadata]
        self.results_table.setRowCount(len(completed))

        for row, photo in enumerate(completed):
            # Status dot (green = done)
            dot = StatusDot("done")
            container = QWidget()
            cl = QHBoxLayout(container)
            cl.setContentsMargins(4, 0, 0, 0)
            cl.addWidget(dot)
            self.results_table.setCellWidget(row, 0, container)

            # Filename
            item = QTableWidgetItem(photo.filename)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.results_table.setItem(row, 1, item)
            self.results_table.setRowHeight(row, 30)

        # Auto-select first if nothing selected
        if completed and self._current_result_index < 0:
            self.results_table.selectRow(0)

        self._update_results_pos_label()

    def _get_completed_photos(self):
        return [p for p in self.photos if p.status == "done" and p.metadata]

    def _on_result_selected(self, row, col, prev_row, prev_col):
        completed = self._get_completed_photos()
        if row < 0 or row >= len(completed):
            return
        self._current_result_index = row
        self._load_detail(completed[row])
        self._update_results_pos_label()

    def _load_detail(self, photo):
        """Load a photo's metadata into the detail panel."""
        self._updating_detail = True
        # Show folder path
        self.detail_folder.setText(os.path.dirname(photo.filepath))
        self.detail_filename.setText(photo.filename)
        # Load preview
        self._load_preview(photo.filepath)
        self.detail_title.setText(photo.metadata.title)
        self.detail_caption.setText(photo.metadata.caption)
        self.detail_keywords.setText(", ".join(photo.metadata.keywords))
        # Mark fields whose existing value was kept (skip-if-present was on)
        kept_style = (
            "color: #2e9e5b; font-size: 10px; font-weight: 600; "
            "letter-spacing: 1px; margin-top: 4px; border: none;"
        )
        plain_style = (
            "color: #7e7c78; font-size: 10px; font-weight: 600; "
            "letter-spacing: 1px; margin-top: 4px; border: none;"
        )
        t_kept = getattr(photo.metadata, "title_kept", False)
        c_kept = getattr(photo.metadata, "caption_kept", False)
        self.detail_title_label.setText("TITLE · KEPT (already on file)" if t_kept else "TITLE")
        self.detail_title_label.setStyleSheet(kept_style if t_kept else plain_style)
        self.detail_caption_label.setText("CAPTION · KEPT (already on file)" if c_kept else "CAPTION")
        self.detail_caption_label.setStyleSheet(kept_style if c_kept else plain_style)
        kw_count = len(photo.metadata.keywords)
        self.kw_count_label.setText(f"{kw_count} keyword{'s' if kw_count != 1 else ''}")
        posts = getattr(photo.metadata, "posts", None) or {}
        for pid, (box, edit, _, _) in self.post_boxes.items():
            text = posts.get(pid, "")
            edit.setPlainText(text)
            box.setVisible(bool(text))
            self._update_post_count(pid)
        # The header row carries the Write posts button, so it shows for any
        # photo on screen, whether or not it has posts yet.
        self.posts_row_widget.setVisible(True)
        self._update_write_posts_btn()
        self._updating_detail = False

    def _load_preview(self, filepath: str):
        """Load photo preview with caching."""
        if not hasattr(self, "_preview_cache"):
            self._preview_cache = {}
        if filepath in self._preview_cache:
            self.detail_preview.setPixmap(self._preview_cache[filepath])
            return
        try:
            ext = Path(filepath).suffix.lower()
            if ext in RAW_EXTENSIONS and HAS_RAWPY:
                with rawpy.imread(filepath) as raw:
                    rgb = raw.postprocess(use_camera_wb=True, half_size=True)
                img = Image.fromarray(rgb)
            else:
                img = Image.open(filepath)
            if img.mode != "RGB":
                img = img.convert("RGB")
            max_w, max_h = 720, 380
            ratio = min(max_w / img.width, max_h / img.height)
            if ratio < 1:
                new_size = (int(img.width * ratio), int(img.height * ratio))
                img = img.resize(new_size, Image.LANCZOS)
            data = img.tobytes("raw", "RGB")
            qimg = QImage(data, img.width, img.height, img.width * 3, QImage.Format_RGB888)
            pixmap = QPixmap.fromImage(qimg)
            # Cache (limit 50 entries)
            if len(self._preview_cache) >= 50:
                oldest = next(iter(self._preview_cache))
                del self._preview_cache[oldest]
            self._preview_cache[filepath] = pixmap
            self.detail_preview.setPixmap(pixmap)
        except Exception:
            self.detail_preview.setText("Preview unavailable")

    def _on_detail_edited(self):
        """Sync edits from detail panel back to the photo data."""
        if self._updating_detail:
            return
        completed = self._get_completed_photos()
        if self._current_result_index < 0 or self._current_result_index >= len(completed):
            return
        photo = completed[self._current_result_index]
        photo.metadata.title = self.detail_title.text()
        photo.metadata.caption = self.detail_caption.toPlainText()
        kws_text = self.detail_keywords.toPlainText()
        photo.metadata.keywords = [k.strip() for k in kws_text.split(",") if k.strip()]
        kw_count = len(photo.metadata.keywords)
        self.kw_count_label.setText(f"{kw_count} keyword{'s' if kw_count != 1 else ''}")

    def _on_post_edited(self):
        """Sync edits in the post boxes back to the photo, and recount."""
        for pid in self.post_boxes:
            self._update_post_count(pid)
        if self._updating_detail:
            return
        completed = self._get_completed_photos()
        if self._current_result_index < 0 or self._current_result_index >= len(completed):
            return
        photo = completed[self._current_result_index]
        for pid, (box, edit, _, _) in self.post_boxes.items():
            # isHidden, not isVisible: the latter is False whenever the Results
            # tab isn't the one on screen, which would silently drop edits.
            if not box.isHidden():
                photo.metadata.posts[pid] = edit.toPlainText()

    def _update_post_count(self, pid):
        """Character count under a post, red once it's over the platform limit."""
        _, edit, count, _ = self.post_boxes[pid]
        n = len(edit.toPlainText())
        limit = POST_TARGET_LIMITS.get(pid)
        if limit:
            count.setText(f"{n} / {limit}")
            colour = "#e2796a" if n > limit else "#5f5f66"
        else:
            count.setText(f"{n} characters")
            colour = "#5f5f66"
        count.setStyleSheet(f"color: {colour}; font-size: 11px; border: none;")

    def _results_prev(self):
        completed = self._get_completed_photos()
        if not completed:
            return
        new_idx = max(0, self._current_result_index - 1)
        self.results_table.selectRow(new_idx)

    def _results_next(self):
        completed = self._get_completed_photos()
        if not completed:
            return
        new_idx = min(len(completed) - 1, self._current_result_index + 1)
        self.results_table.selectRow(new_idx)

    def _update_results_pos_label(self):
        completed = self._get_completed_photos()
        total = len(completed)
        pos = self._current_result_index + 1 if self._current_result_index >= 0 else 0
        self.results_pos_label.setText(f"{pos} / {total}")

    # ── Write metadata ──

    def _write_metadata(self, auto=False):
        if not MetadataWriter.check_exiftool():
            if not auto:
                self._show_exiftool_missing_dialog()
            return

        completed = [p for p in self.photos if p.status == "done" and p.metadata]
        if not completed:
            return

        use_sidecar = self.sidecar_check.isChecked()
        adobe_naming = self.sidecar_naming_combo.currentIndex() == 0
        raw_count = sum(
            1 for p in completed
            if Path(p.filepath).suffix.lower() in RAW_EXTENSIONS
        )
        sidecar_note = ""
        if use_sidecar and raw_count:
            sidecar_note = f"\nRAW files ({raw_count}): metadata written to XMP sidecar."

        if not auto:
            reply = QMessageBox.question(
                self, "Write Metadata",
                f"Write metadata to {len(completed)} file(s)?\n\n"
                f"{'Backup files will be created.' if self.backup_check.isChecked() else 'WARNING: No backup will be created!'}\n"
                f"{'Keywords will be appended to existing.' if self.append_keywords_check.isChecked() else 'Keywords will replace existing.'}\n"
                f"{'Title/caption will be skipped if already present.' if self.skip_existing_check.isChecked() else 'Title/caption will be overwritten.'}"
                f"{sidecar_note}",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes
            )
            if reply != QMessageBox.Yes:
                return

        # Prepare items list
        items = [(p.filepath, p.metadata) for p in completed]

        # Show progress
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(len(items))
        self.progress_bar.setValue(0)
        self.status_label.setText("Writing metadata...")
        self.write_btn.setEnabled(False)

        self._write_worker = MetadataWriteWorker(
            items=items,
            backup=self.backup_check.isChecked(),
            append_keywords=self.append_keywords_check.isChecked(),
            skip_existing=self.skip_existing_check.isChecked(),
            use_sidecar=use_sidecar,
            adobe_naming=adobe_naming,
        )
        self._write_worker.progress.connect(self._on_write_progress)
        self._write_worker.file_done.connect(self._on_write_file_done)
        self._write_worker.finished_writing.connect(self._on_write_finished)
        self._write_worker.start()

    def _on_write_progress(self, current, total):
        self.progress_bar.setValue(current)

    def _on_write_file_done(self, filename, success, error_msg):
        if success:
            self.log(f"Wrote metadata: {filename}")
        else:
            self.log(f"Error writing {filename}: {error_msg}")

    def _on_write_finished(self, success_count, error_count):
        self.progress_bar.setVisible(False)
        self.write_btn.setEnabled(True)
        self.status_label.setText(f"Written: {success_count} OK, {error_count} errors")
        # Defer cleanup — the thread is still winding down when this signal fires
        QTimer.singleShot(500, lambda: setattr(self, '_write_worker', None))
        QMessageBox.information(
            self, "Complete",
            f"Metadata written to {success_count} file(s).\n"
            f"Errors: {error_count}"
        )
        if success_count > 0:
            self._clear_progress()

    # ── Export ──

    def _export_csv(self):
        completed = [p for p in self.photos if p.status == "done" and p.metadata]
        if not completed:
            QMessageBox.information(self, "Export", "No processed photos to export.")
            return

        # Default filename from source folder
        default_name = "photo_metadata.csv"
        if self.photos:
            parent_folder = os.path.basename(os.path.dirname(self.photos[0].filepath))
            if parent_folder:
                default_name = f"{parent_folder}.csv"

        path, _ = QFileDialog.getSaveFileName(
            self, "Export CSV", default_name, "CSV Files (*.csv)"
        )
        if not path:
            return

        import csv
        # A column per platform that any exported photo actually has a post for.
        post_cols = [pid for pid in POST_TARGET_IDS
                     if any((getattr(p.metadata, "posts", None) or {}).get(pid)
                            for p in completed)]
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Filename", "Filepath", "Title", "Caption", "Keywords"]
                            + [f"Post: {POST_TARGET_LABELS[pid]}" for pid in post_cols])
            for photo in completed:
                posts = getattr(photo.metadata, "posts", None) or {}
                writer.writerow([
                    photo.filename,
                    photo.filepath,
                    photo.metadata.title,
                    photo.metadata.caption,
                    "; ".join(photo.metadata.keywords),
                ] + [posts.get(pid, "") for pid in post_cols])
        self.log(f"Exported CSV: {path}")
        self.status_label.setText(f"CSV exported to {path}")

    def _import_csv(self):
        """Import metadata from a previously exported CSV file."""
        import csv

        if not self.photos:
            QMessageBox.information(
                self, "Import CSV",
                "Load photos first, then import a CSV to apply metadata to them."
            )
            return

        path, _ = QFileDialog.getOpenFileName(
            self, "Import CSV", "", "CSV Files (*.csv);;All Files (*)"
        )
        if not path:
            return

        # Read the CSV
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                csv_rows = list(reader)
        except Exception as e:
            QMessageBox.critical(self, "Import Error", f"Failed to read CSV:\n{e}")
            return

        if not csv_rows:
            QMessageBox.information(self, "Import CSV", "CSV file is empty.")
            return

        # Build lookup of loaded photos by filepath and filename
        by_filepath = {p.filepath: p for p in self.photos}
        by_filename = {}
        for p in self.photos:
            if p.filename not in by_filename:
                by_filename[p.filename] = p

        matched = 0
        unmatched = []

        for row in csv_rows:
            filepath = row.get("Filepath", "").strip()
            filename = row.get("Filename", "").strip()
            title = row.get("Title", "").strip()
            caption = row.get("Caption", "").strip()
            keywords_str = row.get("Keywords", "").strip()

            # Parse keywords (semicolon or comma separated)
            if ";" in keywords_str:
                keywords = [k.strip() for k in keywords_str.split(";") if k.strip()]
            else:
                keywords = [k.strip() for k in keywords_str.split(",") if k.strip()]

            # Match: try filepath first, then filename
            photo = None
            if filepath and filepath in by_filepath:
                photo = by_filepath[filepath]
            elif filename and filename in by_filename:
                photo = by_filename[filename]

            if photo:
                posts = {}
                for pid in POST_TARGET_IDS:
                    val = (row.get(f"Post: {POST_TARGET_LABELS[pid]}") or "").strip()
                    if val:
                        posts[pid] = val
                photo.metadata = PhotoMetadata(
                    title=title,
                    caption=caption,
                    keywords=keywords,
                    posts=posts,
                )
                photo.status = "done"
                matched += 1
            else:
                unmatched.append(filename or filepath or "(unknown)")

        # Refresh UI
        self._refresh_photo_table()
        if matched > 0:
            self._refresh_results_table()
            self.write_btn.setEnabled(True)
            self.export_btn.setEnabled(True)

        # Show summary
        msg = f"Imported metadata for {matched} of {len(csv_rows)} rows."
        if unmatched:
            msg += f"\n\n{len(unmatched)} rows could not be matched to loaded photos:\n"
            for name in unmatched[:20]:
                msg += f"  \u2022 {name}\n"
            if len(unmatched) > 20:
                msg += f"  ... and {len(unmatched) - 20} more\n"
            msg += "\nThese rows were skipped. Only matched photos will be written."

        QMessageBox.information(self, "Import CSV", msg)
        self.log(f"CSV imported: {matched} matched, {len(unmatched)} unmatched")
        self.status_label.setText(f"CSV imported: {matched} matched, {len(unmatched)} unmatched")

    # ── Keywords loading ──

    def _load_keywords(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Keywords", "",
            "Text Files (*.txt *.csv);;All Files (*)"
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            self.keywords_edit.setPlainText(text)
            self.log(f"Loaded keywords from {path}")
        except Exception as e:
            self.log(f"Error loading keywords: {e}")

    # ── Prompt presets ──

    _BUILTIN_PRESETS = {
        "Default": (
            "Analyse this photograph and generate metadata for it.\n\n"
            "Title: A concise, descriptive title (5-10 words).\n"
            "Caption: A detailed description of the scene, subjects, "
            "lighting, mood, and composition (1-3 sentences).\n"
            "Keywords: 10-20 relevant keywords for search and cataloguing, "
            "covering subject matter, location type, mood, colours, "
            "photographic style, and season where apparent."
        ),
        "Landscape": (
            "Analyse this landscape photograph.\n\n"
            "Title: A concise, evocative title (5-10 words).\n"
            "Caption: Describe the scene, terrain, weather, light quality, "
            "and mood (1-3 sentences).\n"
            "Keywords: 15-20 keywords covering landscape type, geological "
            "features, vegetation, sky conditions, season, time of day, "
            "colours, and photographic style."
        ),
        "Event": (
            "Analyse this event photograph.\n\n"
            "Title: A descriptive title capturing the moment (5-10 words).\n"
            "Caption: Describe the action, participants, setting, and "
            "atmosphere (1-3 sentences).\n"
            "Keywords: 15-20 keywords covering the event type, activities, "
            "people, setting, mood, and photographic style."
        ),
        "Product": (
            "Analyse this product photograph.\n\n"
            "Title: A clear, descriptive title (5-10 words).\n"
            "Caption: Describe the product, its features, styling, "
            "and presentation (1-3 sentences).\n"
            "Keywords: 15-20 keywords covering product type, features, "
            "materials, colours, style, and use case."
        ),
    }

    def _init_prompt_presets(self):
        """Initialise the prompt preset combo box with built-in and saved presets."""
        self._prompt_presets = dict(self._BUILTIN_PRESETS)

        # Load user-saved presets from settings
        raw = self.settings.value("prompt_presets", "{}")
        try:
            user_presets = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            user_presets = {}
        self._prompt_presets.update(user_presets)
        self._user_preset_names = set(user_presets.keys())

        self._refresh_preset_combo()

    def _refresh_preset_combo(self):
        """Refresh the preset combo box contents."""
        self.prompt_preset_combo.blockSignals(True)
        current = self.prompt_preset_combo.currentText()
        self.prompt_preset_combo.clear()

        # Built-in presets first
        for name in self._BUILTIN_PRESETS:
            self.prompt_preset_combo.addItem(name)

        # User presets after a separator
        if self._user_preset_names:
            self.prompt_preset_combo.insertSeparator(self.prompt_preset_combo.count())
            for name in sorted(self._user_preset_names):
                self.prompt_preset_combo.addItem(name)

        # Restore selection
        idx = self.prompt_preset_combo.findText(current)
        if idx >= 0:
            self.prompt_preset_combo.setCurrentIndex(idx)
        self.prompt_preset_combo.blockSignals(False)

    def _on_prompt_preset_selected(self, name: str):
        """Handle preset selection change — auto-load into editor."""
        if name and name in self._prompt_presets:
            self.prompt_edit.setText(self._prompt_presets[name])

    def _save_prompt_preset(self):
        """Save the current prompt as a new named preset."""
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(
            self, "Save Preset", "Preset name:",
        )
        if not ok or not name.strip():
            return
        name = name.strip()

        # Prevent overwriting built-in presets via Save As
        if name in self._BUILTIN_PRESETS:
            QMessageBox.warning(
                self, "Cannot Save",
                f"'{name}' is a built-in preset. Use 'Update' to modify it,\n"
                f"or choose a different name."
            )
            return

        self._prompt_presets[name] = self.prompt_edit.toPlainText()
        self._user_preset_names.add(name)
        self._save_user_presets()
        self._refresh_preset_combo()
        self.prompt_preset_combo.setCurrentText(name)
        self.log(f"Saved preset: {name}")

    def _overwrite_prompt_preset(self):
        """Overwrite the currently selected preset with the current prompt text."""
        name = self.prompt_preset_combo.currentText()
        if not name:
            return

        reply = QMessageBox.question(
            self, "Update Preset",
            f"Overwrite preset '{name}' with the current prompt?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        self._prompt_presets[name] = self.prompt_edit.toPlainText()

        # If it's a built-in preset being modified, save it as a user override
        if name in self._BUILTIN_PRESETS:
            self._user_preset_names.add(name)

        self._save_user_presets()
        self.log(f"Updated preset: {name}")

    def _delete_prompt_preset(self):
        """Delete the currently selected preset."""
        name = self.prompt_preset_combo.currentText()
        if not name:
            return

        if name in self._BUILTIN_PRESETS and name not in self._user_preset_names:
            QMessageBox.information(
                self, "Cannot Delete",
                f"'{name}' is a built-in preset and cannot be deleted."
            )
            return

        reply = QMessageBox.question(
            self, "Delete Preset",
            f"Delete preset '{name}'?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        self._user_preset_names.discard(name)
        # Restore built-in version if it was overridden
        if name in self._BUILTIN_PRESETS:
            self._prompt_presets[name] = self._BUILTIN_PRESETS[name]
        else:
            self._prompt_presets.pop(name, None)

        self._save_user_presets()
        self._refresh_preset_combo()
        self.log(f"Deleted preset: {name}")

    def _save_user_presets(self):
        """Persist user presets to QSettings."""
        user_presets = {
            name: self._prompt_presets[name]
            for name in self._user_preset_names
            if name in self._prompt_presets
        }
        self.settings.setValue("prompt_presets", json.dumps(user_presets))

    def _reset_prompt(self):
        """Reset prompt to default."""
        self.prompt_edit.setText(self._BUILTIN_PRESETS["Default"])
        self.prompt_preset_combo.setCurrentText("Default")

    # ── Model recommendation ──

    def _detect_hardware(self) -> dict:
        """Detect GPU VRAM and system RAM. Cross-platform."""
        info = {"gpu_name": None, "vram_mb": None, "ram_mb": 0, "platform": "cpu_only"}

        # System RAM
        try:
            if sys.platform == "win32":
                # Try PowerShell first (wmic is deprecated/removed on Win11 24H2+)
                result = _run(
                    ["powershell", "-NoProfile", "-Command",
                     "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory"],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0 and result.stdout.strip().isdigit():
                    info["ram_mb"] = int(result.stdout.strip()) // (1024 * 1024)
                else:
                    # Fallback to wmic for older Windows versions
                    result = _run(
                        ["wmic", "computersystem", "get", "TotalPhysicalMemory", "/value"],
                        capture_output=True, text=True, timeout=10
                    )
                    for line in result.stdout.strip().split("\n"):
                        if "TotalPhysicalMemory=" in line:
                            info["ram_mb"] = int(line.split("=")[1].strip()) // (1024 * 1024)
            elif sys.platform == "darwin":
                result = _run(
                    ["sysctl", "-n", "hw.memsize"],
                    capture_output=True, text=True, timeout=5
                )
                info["ram_mb"] = int(result.stdout.strip()) // (1024 * 1024)
            else:
                with open("/proc/meminfo") as f:
                    for line in f:
                        if line.startswith("MemTotal:"):
                            info["ram_mb"] = int(line.split()[1]) // 1024
                            break
        except Exception:
            pass

        # NVIDIA GPU
        try:
            result = _run(
                ["nvidia-smi", "--query-gpu=name,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                best_name, best_vram = None, 0
                for line in result.stdout.strip().split("\n"):
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 2:
                        try:
                            vram = int(parts[1])
                            if vram > best_vram:
                                best_vram = vram
                                best_name = parts[0]
                        except ValueError:
                            pass
                if best_name:
                    info["gpu_name"] = best_name
                    info["vram_mb"] = best_vram
                    info["platform"] = "nvidia"
                    return info
        except Exception:
            pass

        # Apple Silicon (shared memory)
        if sys.platform == "darwin":
            try:
                result = _run(
                    ["sysctl", "-n", "machdep.cpu.brand_string"],
                    capture_output=True, text=True, timeout=5
                )
                if "Apple" in result.stdout:
                    info["platform"] = "apple_silicon"
                    info["gpu_name"] = "Apple Silicon (shared memory)"
                    info["vram_mb"] = int(info["ram_mb"] * 0.7)
            except Exception:
                pass

        # AMD / Intel / other GPUs — only when no NVIDIA or Apple GPU was found.
        # Cosmetic for the recommender: it shows the user their actual GPU
        # instead of "None detected", and uses VRAM where we can read it.
        if not info["gpu_name"]:
            try:
                name, vram = None, 0
                if sys.platform == "win32":
                    r = _run(["powershell", "-NoProfile", "-Command",
                              "Get-CimInstance Win32_VideoController | "
                              "Select-Object Name,AdapterRAM | ConvertTo-Json -Compress"],
                             capture_output=True, text=True, timeout=10)
                    if r.returncode == 0 and r.stdout.strip():
                        data = json.loads(r.stdout)
                        if isinstance(data, dict):
                            data = [data]
                        for gpu in data:
                            gname = (gpu.get("Name") or "").strip()
                            try:
                                gram_mb = int(gpu.get("AdapterRAM") or 0) // (1024 * 1024)
                            except (ValueError, TypeError):
                                gram_mb = 0
                            if gname and gram_mb >= vram:
                                name, vram = gname, gram_mb
                        # AdapterRAM is a 32-bit field capped at ~4095 MB, so it
                        # under-reports larger cards — treat that as unknown and
                        # let the recommender fall back to system RAM.
                        if vram >= 4095:
                            vram = 0
                elif sys.platform == "darwin":
                    r = _run(["system_profiler", "SPDisplaysDataType"],
                             capture_output=True, text=True, timeout=15)
                    if r.returncode == 0:
                        for line in r.stdout.split("\n"):
                            s = line.strip()
                            m = re.match(r"Chipset Model:\s*(.+)", s)
                            if m:
                                name = m.group(1).strip()
                            m = re.match(r"VRAM.*?:\s*(\d+)\s*(MB|GB)", s)
                            if m:
                                v = int(m.group(1))
                                vram = v * 1024 if m.group(2) == "GB" else v
                else:
                    r = _run(["lspci"], capture_output=True, text=True, timeout=10)
                    if r.returncode == 0:
                        for line in r.stdout.split("\n"):
                            if ("VGA compatible controller" in line
                                    or "3D controller" in line
                                    or "Display controller" in line):
                                name = line.split(":", 2)[-1].strip()
                                break
                    import glob
                    for p in glob.glob("/sys/class/drm/card*/device/mem_info_vram_total"):
                        try:
                            with open(p) as f:
                                vram = max(vram, int(f.read().strip()) // (1024 * 1024))
                        except Exception:
                            pass
                if name:
                    info["gpu_name"] = name
                    low = name.lower()
                    info["platform"] = (
                        "amd" if any(k in low for k in ("amd", "radeon", "ati"))
                        else "intel" if "intel" in low
                        else "gpu"
                    )
                    if vram > 0:
                        info["vram_mb"] = vram
            except Exception:
                pass

        return info

    # Gemma 4 lineup, sizes as listed in Ollama's library (QAT builds, which
    # hold up best at 4-bit). Every Gemma 4 model reads images, and every one
    # is a reasoning model — handled by switching reasoning off per request.
    _GEMMA4 = {
        "e2b": {"model": "gemma4:e2b-it-qat", "lm_studio": "gemma-4-e2b",
                "desc": "Gemma 4 E2B", "size": "~4.3GB download"},
        "e4b": {"model": "gemma4:e4b-it-qat", "lm_studio": "gemma-4-e4b",
                "desc": "Gemma 4 E4B", "size": "~6.1GB download"},
        "12b": {"model": "gemma4:12b-it-qat", "lm_studio": "gemma-4-12b",
                "desc": "Gemma 4 12B", "size": "~7.2GB download"},
        "26b": {"model": "gemma4:26b-a4b-it-qat", "lm_studio": "gemma-4-26b-a4b",
                "desc": "Gemma 4 26B-A4B", "size": "~16GB download"},
    }

    def _get_model_recommendation(self, info: dict) -> dict:
        """Recommend a model that fits with room to spare.

        The old rule counted 70% of a Mac's memory as usable, which ignored
        everything else running (Lightroom, a browser). A model that only just
        fits gets read back from disk as it generates: on a 32GB M2 Max a 27B
        and a 26B-A4B both ran at 3-6 tokens a second, against 14-19 for one
        that fitted. So size by total memory with generous headroom."""
        ram_gb = (info.get("ram_mb") or 0) / 1024
        vram_gb = (info.get("vram_mb") or 0) / 1024
        platform = info.get("platform") or ""

        if platform == "apple_silicon":
            # Unified memory is shared with the OS and every open app.
            if ram_gb >= 46:
                key, why = "26b", ("A mixture-of-experts model: 26B of knowledge, "
                                   "but only about 4B used per word, so it runs "
                                   "much faster than its size suggests.")
            elif ram_gb >= 23:
                key, why = "12b", ("Good captions, and small enough to stay in "
                                   "memory alongside Lightroom and a browser.")
            elif ram_gb >= 14:
                key, why = "e4b", "Fits comfortably alongside your other apps."
            else:
                key, why = "e2b", "The smallest option; leaves room for the system."
        elif vram_gb > 0:
            # A discrete card holds the model on its own, but needs a couple of
            # GB spare for the image and working memory.
            if vram_gb >= 20:
                key, why = "26b", ("Fits in your graphics memory with room to spare, "
                                   "and only about 4B of it is used per word.")
            elif vram_gb >= 10:
                key, why = "12b", "Fits in your graphics memory with room to spare."
            elif vram_gb >= 7.5:
                # E4B is 6.1GB, so a 6GB card can't hold it with any room.
                key, why = "e4b", "Fits in your graphics memory."
            else:
                key, why = "e2b", "The smallest option, for limited graphics memory."
        else:
            # No usable GPU: it runs on the processor, where size costs most.
            key = "e4b" if ram_gb >= 16 else "e2b"
            why = ("Running on the processor is slow at any size, so a small "
                   "model keeps it bearable.")
        rec = dict(self._GEMMA4[key])
        rec["why"] = why
        rec["pull"] = f"ollama pull {rec['model']}"
        return rec

    def _recommend_model(self):
        """Detect hardware and show recommendation."""
        self.log("Detecting hardware...")
        info = self._detect_hardware()

        hw_lines = []
        if info["gpu_name"]:
            hw_lines.append(f"GPU: {info['gpu_name']}")
        # On Apple Silicon "VRAM" is only an estimate carved out of shared
        # memory, and the recommendation no longer uses it, so don't show it.
        if info["vram_mb"] and info.get("platform") != "apple_silicon":
            hw_lines.append(f"VRAM: {info['vram_mb'] / 1024:.1f} GB")
        if info["ram_mb"]:
            hw_lines.append(f"System RAM: {info['ram_mb'] / 1024:.1f} GB")
        if not info["gpu_name"]:
            hw_lines.append("GPU: None detected (will use system RAM)")
        hw_summary = "\n".join(hw_lines)
        self.log(f"Hardware: " + "; ".join(hw_lines))

        rec = self._get_model_recommendation(info)

        msg = QMessageBox(self)
        msg.setWindowTitle("Model Recommendation")
        msg.setIcon(QMessageBox.Information)
        msg.setText(f"Detected Hardware:\n{hw_summary}")

        backend = getattr(self, "backend", "ollama")
        if backend == "openai":
            # LM Studio user
            # MLX is Apple Silicon's native format in LM Studio; elsewhere a
            # 4-bit GGUF build (QAT where offered) is the right choice.
            build = ("Choose an MLX build, the native format for Apple Silicon "
                     "(QAT or 4-bit)."
                     if info.get("platform") == "apple_silicon" else
                     "Choose a QAT or Q4_K_M build.")
            msg.setInformativeText(
                f"Recommended: {rec['desc']}\n{rec['size']}\n{rec['why']}\n\n"
                f"In LM Studio, go to the Discover tab and search for:\n"
                f"  {rec['lm_studio']}\n\n{build}"
            )
            msg.addButton("OK", QMessageBox.AcceptRole)
        else:
            # Ollama user
            msg.setInformativeText(
                f"Recommended: {rec['desc']}\n{rec['size']}\n{rec['why']}\n\n"
                f"Command: {rec['pull']}\n\n"
                f"Click 'Pull Model' to download now, or 'Copy Command' to run manually."
            )
            pull_btn = msg.addButton("Pull Model", QMessageBox.AcceptRole)
            copy_btn = msg.addButton("Copy Command", QMessageBox.ActionRole)
            msg.addButton("Close", QMessageBox.RejectRole)

        msg.exec()

        if backend != "openai":
            if msg.clickedButton() == pull_btn:
                self._pull_model(rec["pull"])
            elif msg.clickedButton() == copy_btn:
                QApplication.clipboard().setText(rec["pull"])
                self.status_label.setText("Pull command copied to clipboard")

    def _pull_model(self, command: str):
        """Pull model via Ollama with progress."""
        model_name = command.replace("ollama pull ", "").strip()
        self.log(f"Pulling model: {model_name}...")
        self.model_download_widget.setVisible(True)
        self.model_download_label.setText(f"Downloading {model_name}...")
        self.model_download_label.setStyleSheet(
            "font-size: 14px; font-weight: 600; color: #d1935e; border: none;"
        )
        self.model_download_progress.setValue(0)

        class PullThread(QThread):
            progress_update = Signal(int)
            finished = Signal(str)

            def run(self_thread):
                try:
                    proc = _popen(
                        command.split(),
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace", bufsize=1,
                    )
                    last_pct = 0
                    for line in iter(proc.stdout.readline, ""):
                        pct_match = re.search(r"(\d+)%", line)
                        if pct_match:
                            pct = int(pct_match.group(1))
                            if pct != last_pct:
                                last_pct = pct
                                self_thread.progress_update.emit(pct)
                    proc.wait()
                    if proc.returncode == 0:
                        self_thread.finished.emit(f"Model {model_name} downloaded successfully!")
                    else:
                        self_thread.finished.emit(f"Error (exit code {proc.returncode})")
                except FileNotFoundError:
                    self_thread.finished.emit("Ollama not found. Install from ollama.com")
                except Exception as e:
                    self_thread.finished.emit(f"Error: {e}")

        self._pull_thread = PullThread()
        self._pull_thread.progress_update.connect(self._on_pull_progress)
        self._pull_thread.finished.connect(self._on_pull_finished)
        self._pull_thread.start()

    def _on_pull_progress(self, percent: int):
        self.model_download_progress.setValue(percent)
        self.model_download_label.setText(f"Downloading... {percent}%")

    def _on_pull_finished(self, message: str):
        self.log(message)
        if "successfully" in message:
            self.model_download_progress.setValue(100)
            self.model_download_label.setText(message)
            self.model_download_label.setStyleSheet(
                "font-size: 14px; font-weight: 600; color: #7bc9a0; border: none;"
            )
            QTimer.singleShot(1000, self._refresh_models)
            QTimer.singleShot(8000, lambda: self.model_download_widget.setVisible(False))
        else:
            self.model_download_label.setText(message)
            self.model_download_label.setStyleSheet(
                "font-size: 14px; font-weight: 600; color: #e2796a; border: none;"
            )
        # Clean up thread reference safely
        QTimer.singleShot(2000, lambda: setattr(self, '_pull_thread', None))


# ─────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)

    # Dark palette
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor("#141416"))
    palette.setColor(QPalette.WindowText, QColor("#e0e0e0"))
    palette.setColor(QPalette.Base, QColor("#17171a"))
    palette.setColor(QPalette.AlternateBase, QColor("#1f1f23"))
    palette.setColor(QPalette.Text, QColor("#e0e0e0"))
    palette.setColor(QPalette.Button, QColor("#232326"))
    palette.setColor(QPalette.ButtonText, QColor("#e0e0e0"))
    palette.setColor(QPalette.Highlight, QColor("#c05a2e"))
    palette.setColor(QPalette.HighlightedText, QColor("#141416"))
    app.setPalette(palette)

    window = PhotoScribe()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

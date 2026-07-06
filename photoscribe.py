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
APP_VERSION = "1.4.2"
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QTextEdit, QLineEdit, QComboBox,
    QProgressBar, QScrollArea, QFrame, QSplitter, QGroupBox, QCheckBox,
    QFileDialog, QMessageBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QTabWidget, QSpinBox, QMenu, QToolButton, QSizePolicy, QPlainTextEdit,
    QAbstractItemView, QStyledItemDelegate, QStyle
)
from PySide6.QtCore import (
    Qt, Signal, QThread, QSize, QMimeData, QTimer, QSettings, QUrl
)
from PySide6.QtGui import (
    QPixmap, QImage, QDragEnterEvent, QDropEvent, QFont, QColor,
    QPalette, QIcon, QAction, QPainter, QFontDatabase
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

def read_exif_date(filepath: str) -> Optional[str]:
    """Read DateTimeOriginal from EXIF and return as human-readable string."""
    exiftool = MetadataWriter.find_exiftool()
    if not exiftool:
        return None
    try:
        result = _run(
            [exiftool, "-j", "-DateTimeOriginal", filepath],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            if data:
                dto = data[0].get("DateTimeOriginal", "")
                if dto and ":" in dto:
                    date_part = dto.split(" ")[0]
                    parts = date_part.split(":")
                    if len(parts) == 3:
                        dt = _datetime(int(parts[0]), int(parts[1]), int(parts[2]))
                        if sys.platform == "win32":
                            return dt.strftime("%#d %B %Y")
                        else:
                            return dt.strftime("%-d %B %Y")
    except Exception:
        pass
    return None

# ─────────────────────────────────────────────────────────
# GPS reverse-geocoding
# ─────────────────────────────────────────────────────────

def read_gps_coordinates(filepath: str) -> Optional[tuple]:
    """Read GPS coordinates from photo EXIF data.
    Returns (latitude, longitude) as floats, or None.
    """
    exiftool = MetadataWriter.find_exiftool()
    if not exiftool:
        return None
    try:
        result = _run(
            [exiftool, "-j", "-n", "-GPSLatitude", "-GPSLongitude", filepath],
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
        pass
    return None


def reverse_geocode(lat: float, lon: float) -> Optional[str]:
    """Reverse geocode via OpenStreetMap Nominatim (free, no API key).
    NOTE: This makes an external network request.
    Returns place description or None.
    """
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
        for key in ["tourism", "natural", "leisure", "amenity",
                    "hamlet", "village", "suburb", "town", "city"]:
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

@dataclass
class PhotoItem:
    filepath: str
    filename: str
    thumbnail: Optional[QPixmap] = None
    metadata: Optional[PhotoMetadata] = None
    status: str = "pending"  # pending, processing, done, error
    error_msg: str = ""


# ─────────────────────────────────────────────────────────
# Ollama API worker thread
# ─────────────────────────────────────────────────────────

class OllamaWorker(QThread):
    """Processes photos through a local AI backend (Ollama or LM Studio)."""
    progress = Signal(int, str)        # index, status
    result = Signal(int, object)       # index, PhotoMetadata or error string
    finished_all = Signal()
    log_message = Signal(str)

    def __init__(self, photos, model, prompt, context, ollama_url,
                 keywords_list=None, backend="ollama", max_tokens=2048,
                 describe_people=True, skip_existing=False):
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

        if self.context.strip():
            parts.append(f"Context for this photo: {self.context.strip()}")

        parts.append(self.prompt.strip())

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
        # models can't ID species reliably, so feed the known tags in as facts.
        existing_tags = MetadataWriter.read_keywords(photo.filepath) if photo else []
        if persons:
            _pers = {p.lower() for p in persons}
            existing_tags = [t for t in existing_tags if t.lower() not in _pers]
        if existing_tags:
            parts.append(
                "This photo is already tagged with these subjects: "
                + ", ".join(existing_tags[:50]) + ". "
                "These tags are accurate — use the specific ones (species, "
                "place names, events) in the title and caption where they fit "
                "what you see, and keep them in the keywords. Don't force in a "
                "tag that clearly doesn't match the image."
            )

        if self.keywords_list:
            vocab = ", ".join(self.keywords_list[:200])
            parts.append(
                f"\nWhen generating keywords, prefer terms from this vocabulary "
                f"where applicable: {vocab}"
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

        parts.append(
            "\nRespond ONLY with valid JSON in this exact format, no other text:\n"
            '{"title": "Short descriptive title", '
            '"caption": "Detailed description of the image in 1-3 sentences", '
            '"keywords": ["keyword1", "keyword2", "keyword3"]}'
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
            "options": {
                "temperature": 0.3,
                "num_predict": self.max_tokens,
            },
            "think": False,
        }
        resp = requests.post(
            f"{self.ollama_url}/api/chat",
            json=payload,
            timeout=180
        )
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "")

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
        }
        resp = requests.post(
            f"{self.ollama_url}/v1/chat/completions",
            json=payload,
            timeout=180
        )
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        # Gemma 4 thinking models may return content in reasoning_content
        return msg.get("content") or msg.get("reasoning_content", "")

    def run(self):
        from concurrent.futures import ThreadPoolExecutor

        call_fn = self._call_openai if self.backend == "openai" else self._call_ollama

        # Build list of pending photo indices
        pending = [(i, photo) for i, photo in enumerate(self.photos)
                   if photo.status != "done"]
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
                    for attempt in range(2):
                        if self._cancelled:
                            break
                        if attempt > 0:
                            self.log_message.emit(
                                "Empty/unparseable response — retrying once..."
                            )
                        response_text = call_fn(img_b64, full_prompt)
                        self.log_message.emit(
                            f"Response received ({len(response_text)} chars)"
                        )

                        # Strip markdown fences and isolate the JSON object
                        cleaned = response_text.strip()
                        if cleaned.startswith("```"):
                            lines = cleaned.split("\n")
                            lines = [l for l in lines if not l.strip().startswith("```")]
                            cleaned = "\n".join(lines).strip()
                        start = cleaned.find("{")
                        end = cleaned.rfind("}") + 1
                        if start >= 0 and end > start:
                            cleaned = cleaned[start:end]

                        if not cleaned:
                            last_reason = "the model returned an empty response"
                            continue
                        try:
                            data = json.loads(cleaned)
                        except json.JSONDecodeError as e:
                            last_reason = f"the response wasn't valid JSON ({e})"
                            continue

                        meta = PhotoMetadata(
                            title=data.get("title", "").strip(),
                            caption=data.get("caption", "").strip(),
                            keywords=self._clean_keywords(data.get("keywords", []))
                        )
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
                    if self.skip_existing:
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
            fields = ["-IPTC:Keywords", "-XMP-dc:Subject"]
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
                for key in ("Keywords", "Subject"):
                    val = entry.get(key, "")
                    if isinstance(val, list):
                        val = "\n".join(str(x) for x in val)
                    for t in (x.strip() for x in str(val).split("\n") if x.strip()):
                        if t.lower() not in seen:
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
        args = [exiftool]

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
        else:
            # Two-pass: clear all keywords first, then write new ones
            clear_args = [exiftool]
            if not backup:
                clear_args.append("-overwrite_original")
            clear_args.extend([
                "-IPTC:Keywords=",
                "-XMP:Subject=",
                filepath
            ])
            _run(clear_args, capture_output=True, text=True, timeout=15)

            # Now add the new keywords
            for kw in kw_list:
                args.append(f"-IPTC:Keywords+={kw}")
                args.append(f"-XMP:Subject+={kw}")

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
        else:
            # Replace: clear existing keywords in a separate pass first — a
            # single "-Subject= ... +=" pass doesn't reliably clear a list tag
            # on an existing sidecar (matches the embedded path's two-pass).
            if xmp_path.exists():
                _run(
                    [exiftool, "-overwrite_original", "-XMP-dc:Subject=", str(xmp_path)],
                    capture_output=True, text=True, timeout=15
                )
            for kw in kw_list:
                args.append(f"-XMP-dc:Subject+={kw}")

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
                    else:
                        arg_lines.append("-IPTC:Keywords=")
                        arg_lines.append("-XMP:Subject=")
                        for kw in metadata.keywords:
                            arg_lines.append(f"-IPTC:Keywords+={kw}")
                            arg_lines.append(f"-XMP:Subject+={kw}")

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

STYLESHEET = """
QMainWindow {
    background-color: #1a1a1e;
}
QWidget {
    color: #e0e0e0;
    font-family: 'Helvetica Neue', 'Segoe UI', sans-serif;
    font-size: 13px;
}
QGroupBox {
    font-weight: 600;
    font-size: 12px;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    color: #a0a0a0;
    border: 1px solid #2a2a30;
    border-radius: 8px;
    margin-top: 12px;
    padding: 12px 12px 10px 12px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 8px;
    margin-left: 8px;
}
QPushButton {
    background-color: #2a2a30;
    border: 1px solid #3a3a42;
    border-radius: 6px;
    padding: 8px 16px;
    font-weight: 500;
    color: #e0e0e0;
    min-height: 20px;
}
QPushButton:hover {
    background-color: #35353d;
    border-color: #e8a23a;
}
QPushButton:pressed {
    background-color: #e8a23a;
    color: #1a1a1e;
}
QPushButton:disabled {
    background-color: #222226;
    color: #555;
    border-color: #2a2a30;
}
QPushButton#primaryBtn {
    background-color: #e8a23a;
    color: #1a1a1e;
    font-weight: 700;
    border: none;
}
QPushButton#primaryBtn:hover {
    background-color: #f0b04a;
}
QPushButton#primaryBtn:disabled {
    background-color: #5a4a20;
    color: #888;
}
QPushButton#dangerBtn {
    background-color: #c0392b;
    color: #fff;
    border: none;
}
QPushButton#dangerBtn:hover {
    background-color: #e74c3c;
}
QPushButton#writeBtn {
    background-color: #27ae60;
    color: #fff;
    font-weight: 700;
    border: none;
}
QPushButton#writeBtn:hover {
    background-color: #2ecc71;
}
QPushButton#exportBtn {
    background-color: #2980b9;
    color: #fff;
    font-weight: 700;
    border: none;
}
QPushButton#exportBtn:hover {
    background-color: #3498db;
}
QPushButton#exportBtn:disabled {
    background-color: #1a3a50;
    color: #888;
}
QComboBox {
    background-color: #2a2a30;
    border: 1px solid #3a3a42;
    border-radius: 6px;
    padding: 6px 10px;
    min-height: 20px;
}
QComboBox:hover {
    border-color: #e8a23a;
}
QComboBox QAbstractItemView {
    background-color: #2a2a30;
    border: 1px solid #3a3a42;
    selection-background-color: #e8a23a;
    selection-color: #1a1a1e;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}
QLineEdit, QTextEdit, QPlainTextEdit {
    background-color: #222226;
    border: 1px solid #3a3a42;
    border-radius: 6px;
    padding: 10px 12px;
    color: #e0e0e0;
    selection-background-color: #e8a23a;
    selection-color: #1a1a1e;
}
QLineEdit {
    min-height: 22px;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {
    border-color: #e8a23a;
}
QTableWidget {
    background-color: #1e1e22;
    border: 1px solid #2a2a30;
    border-radius: 6px;
    gridline-color: #2a2a30;
    selection-background-color: #3a3520;
}
QTableWidget::item {
    padding: 6px 8px;
    border-bottom: 1px solid #2a2a30;
}
QTableWidget::item:selected {
    background-color: #3a3520;
    color: #e8a23a;
}
QHeaderView::section {
    background-color: #222226;
    color: #a0a0a0;
    border: none;
    border-bottom: 2px solid #e8a23a;
    padding: 8px;
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}
QProgressBar {
    background-color: #222226;
    border: 1px solid #2a2a30;
    border-radius: 4px;
    height: 8px;
    text-align: center;
    font-size: 10px;
}
QProgressBar::chunk {
    background-color: #e8a23a;
    border-radius: 3px;
}
QScrollArea {
    border: none;
}
QScrollBar:vertical {
    background-color: #1a1a1e;
    width: 10px;
    border: none;
}
QScrollBar::handle:vertical {
    background-color: #3a3a42;
    border-radius: 5px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background-color: #e8a23a;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QTabWidget::pane {
    border: 1px solid #2a2a30;
    border-radius: 6px;
    background-color: #1e1e22;
}
QTabBar::tab {
    background-color: #222226;
    color: #a0a0a0;
    border: 1px solid #2a2a30;
    border-bottom: none;
    padding: 8px 16px;
    margin-right: 2px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    font-weight: 500;
}
QTabBar::tab:selected {
    background-color: #1e1e22;
    color: #e8a23a;
    border-bottom: 2px solid #e8a23a;
}
QTabBar::tab:hover:!selected {
    color: #e0e0e0;
}
QCheckBox {
    spacing: 8px;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 1px solid #3a3a42;
    background-color: #222226;
}
QCheckBox::indicator:checked {
    background-color: #e8a23a;
    border-color: #e8a23a;
}
QSplitter::handle {
    background-color: #2a2a30;
    width: 2px;
}
QSplitter::handle:hover {
    background-color: #e8a23a;
}
QLabel#statusLabel {
    color: #888;
    font-size: 11px;
}
QLabel#dropLabel {
    color: #666;
    font-size: 15px;
    font-weight: 300;
}
QLabel#titleLabel {
    font-family: 'Bebas Neue', 'Arial Narrow', sans-serif;
    font-size: 28px;
    font-weight: 400;
    color: #e8a23a;
    letter-spacing: 2px;
}
QLabel#subtitleLabel {
    font-size: 11px;
    color: #666;
    font-weight: 400;
    letter-spacing: 1px;
}
"""


# ─────────────────────────────────────────────────────────
# Drop zone widget
# ─────────────────────────────────────────────────────────

class DropZone(QFrame):
    files_dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setMinimumHeight(120)
        self.setStyleSheet("""
            DropZone {
                border: 2px dashed #3a3a42;
                border-radius: 12px;
                background-color: #1e1e22;
            }
            DropZone:hover {
                border-color: #e8a23a;
                background-color: #222226;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)

        icon_label = QLabel("📷")
        icon_label.setStyleSheet("font-size: 32px; background: transparent; border: none;")
        icon_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(icon_label)

        text_label = QLabel("Drop photos here or click Browse")
        text_label.setObjectName("dropLabel")
        text_label.setAlignment(Qt.AlignCenter)
        text_label.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(text_label)

        formats_label = QLabel("JPEG  ·  HEIC  ·  TIFF  ·  PNG  ·  RAW  ·  DNG  ·  CR2/CR3  ·  NEF  ·  ARW  ·  ORF  ·  RAF")
        formats_label.setStyleSheet(
            "color: #555; font-size: 11px; background: transparent; border: none;"
        )
        formats_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(formats_label)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setStyleSheet("""
                DropZone {
                    border: 2px solid #e8a23a;
                    border-radius: 12px;
                    background-color: #2a2520;
                }
            """)

    def dragLeaveEvent(self, event):
        self.setStyleSheet("""
            DropZone {
                border: 2px dashed #3a3a42;
                border-radius: 12px;
                background-color: #1e1e22;
            }
            DropZone:hover {
                border-color: #e8a23a;
                background-color: #222226;
            }
        """)

    def dropEvent(self, event: QDropEvent):
        self.setStyleSheet("""
            DropZone {
                border: 2px dashed #3a3a42;
                border-radius: 12px;
                background-color: #1e1e22;
            }
            DropZone:hover {
                border-color: #e8a23a;
                background-color: #222226;
            }
        """)
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
        "pending": "#555",
        "processing": "#e8a23a",
        "done": "#27ae60",
        "error": "#c0392b",
    }

    def __init__(self, status="pending"):
        super().__init__()
        self.setFixedSize(12, 12)
        self.set_status(status)

    def set_status(self, status):
        colour = self.COLOURS.get(status, "#555")
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
        self.settings = QSettings("PhotoScribe", "PhotoScribe")
        self._detected_folder_context: Optional[FolderContext] = None
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
        title = QLabel("PhotoScribe")
        title.setObjectName("titleLabel")
        title_col.addWidget(title)
        subtitle = QLabel(
            f"AI-powered metadata generation using local models   ·   v{APP_VERSION}"
        )
        subtitle.setObjectName("subtitleLabel")
        title_col.addWidget(subtitle)
        header.addLayout(title_col)
        header.addStretch()

        # Ollama status
        self.ollama_status = QLabel("● Checking Ollama...")
        self.ollama_status.setStyleSheet("color: #e8a23a; font-size: 12px;")
        header.addWidget(self.ollama_status)

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
            "QTableWidget { alternate-background-color: #1c1c20; }"
        )
        self.photo_table.currentCellChanged.connect(self._on_photo_selected)
        self.photo_table.cellDoubleClicked.connect(self._on_photo_double_clicked)
        left_layout.addWidget(self.photo_table, 1)

        # Folder name
        self.folder_name_label = QLabel("")
        self.folder_name_label.setStyleSheet(
            "color: #e8a23a; font-size: 12px; font-weight: 600; border: none;"
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
        settings_layout.setSpacing(8)
        settings_layout.setContentsMargins(0, 0, 6, 0)

        # Model selection
        model_group = QGroupBox("Model")
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
            "font-size: 14px; font-weight: 600; color: #e8a23a; border: none;"
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
        prompt_group = QGroupBox("Prompt")
        prompt_layout = QVBoxLayout(prompt_group)
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setMaximumHeight(120)
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
        preset_label.setStyleSheet("color: #888; font-size: 11px;")
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
        delete_preset_btn.setStyleSheet("font-size: 11px; padding: 2px 10px; color: #c0392b;")
        delete_preset_btn.clicked.connect(self._delete_prompt_preset)
        preset_row.addWidget(delete_preset_btn)

        preset_row.addStretch()
        prompt_layout.addLayout(preset_row)
        settings_layout.addWidget(prompt_group)

        # Initialise prompt presets
        self._init_prompt_presets()

        # Batch context
        context_group = QGroupBox("Batch Context (applied to all photos)")
        context_layout = QGridLayout(context_group)
        context_layout.setSpacing(10)
        context_layout.setContentsMargins(12, 8, 12, 12)

        # Folder context detection toggle
        folder_ctx_row = QHBoxLayout()
        self.folder_context_check = QCheckBox("Use folder context")
        self.folder_context_check.setChecked(True)
        self.folder_context_check.toggled.connect(self._on_folder_context_toggled)
        folder_ctx_row.addWidget(self.folder_context_check)

        self.folder_context_label = QLabel("")
        self.folder_context_label.setStyleSheet(
            "color: #e8a23a; font-size: 11px; font-style: italic;"
        )
        folder_ctx_row.addWidget(self.folder_context_label, 1)

        self.clear_folder_ctx_btn = QPushButton("Clear detected")
        self.clear_folder_ctx_btn.setFixedHeight(24)
        self.clear_folder_ctx_btn.setStyleSheet("font-size: 11px; padding: 2px 10px;")
        self.clear_folder_ctx_btn.setVisible(False)
        self.clear_folder_ctx_btn.clicked.connect(self._clear_folder_context)
        folder_ctx_row.addWidget(self.clear_folder_ctx_btn)

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
            lbl.setStyleSheet("color: #a0a0a0; font-size: 12px;")
            context_layout.addWidget(lbl, row, 0)
            edit = QLineEdit()
            edit.setPlaceholderText(placeholder)
            context_layout.addWidget(edit, row, 1)
            self.context_fields[key] = edit
        settings_layout.addWidget(context_group)

        # Options
        options_group = QGroupBox("Options")
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
            "Look up location from GPS coordinates (requires internet)"
        )
        self.gps_lookup_check.setChecked(False)
        self.gps_lookup_check.setToolTip(
            "When enabled and photos have GPS EXIF data, looks up the\n"
            "place name via OpenStreetMap Nominatim (free public API).\n"
            "This makes an external network request. Off by default."
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
        sidecar_naming_label.setStyleSheet("color: #a0a0a0; font-size: 12px;")
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
        response_length_label.setStyleSheet("color: #a0a0a0; font-size: 12px;")
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
        kw_desc.setStyleSheet("color: #888; font-size: 12px; margin-bottom: 8px;")
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
        presets_desc.setStyleSheet("color: #888; font-size: 12px; margin-bottom: 8px;")
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
            "QTableWidget { alternate-background-color: #1c1c20; }"
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
        self.results_pos_label.setStyleSheet("color: #888; font-size: 11px;")
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
        detail_layout.setContentsMargins(8, 0, 0, 0)
        detail_layout.setSpacing(8)

        # Folder path in results
        self.detail_folder = QLabel("")
        self.detail_folder.setStyleSheet(
            "font-size: 11px; color: #888; border: none; padding: 0;"
        )
        detail_layout.addWidget(self.detail_folder)

        # Filename header
        self.detail_filename = QLabel("Select a photo to view metadata")
        self.detail_filename.setStyleSheet(
            "font-size: 14px; font-weight: 600; color: #e8a23a; "
            "padding: 4px 0; border: none;"
        )
        detail_layout.addWidget(self.detail_filename)

        # Photo preview
        self.detail_preview = QLabel()
        self.detail_preview.setFixedHeight(380)
        self.detail_preview.setAlignment(Qt.AlignCenter)
        self.detail_preview.setStyleSheet(
            "background-color: #1a1a1e; border: 1px solid #2a2a30; "
            "border-radius: 6px; color: #555; font-size: 12px;"
        )
        self.detail_preview.setText("No preview")
        detail_layout.addWidget(self.detail_preview)

        # Title
        self.detail_title_label = QLabel("TITLE")
        self.detail_title_label.setStyleSheet(
            "color: #888; font-size: 10px; font-weight: 600; "
            "letter-spacing: 1px; margin-top: 4px; border: none;"
        )
        detail_layout.addWidget(self.detail_title_label)
        self.detail_title = QLineEdit()
        self.detail_title.setPlaceholderText("Title")
        self.detail_title.textChanged.connect(self._on_detail_edited)
        detail_layout.addWidget(self.detail_title)

        # Caption
        self.detail_caption_label = QLabel("CAPTION")
        self.detail_caption_label.setStyleSheet(
            "color: #888; font-size: 10px; font-weight: 600; "
            "letter-spacing: 1px; margin-top: 4px; border: none;"
        )
        detail_layout.addWidget(self.detail_caption_label)
        self.detail_caption = QTextEdit()
        self.detail_caption.setPlaceholderText("Caption / description")
        self.detail_caption.setMinimumHeight(80)
        self.detail_caption.setMaximumHeight(140)
        self.detail_caption.textChanged.connect(self._on_detail_edited)
        detail_layout.addWidget(self.detail_caption)

        # Keywords
        kw_label = QLabel("KEYWORDS")
        kw_label.setStyleSheet(
            "color: #888; font-size: 10px; font-weight: 600; "
            "letter-spacing: 1px; margin-top: 4px; border: none;"
        )
        detail_layout.addWidget(kw_label)
        self.detail_keywords = QTextEdit()
        self.detail_keywords.setPlaceholderText(
            "Comma-separated keywords"
        )
        self.detail_keywords.setMinimumHeight(60)
        self.detail_keywords.setMaximumHeight(120)
        self.detail_keywords.textChanged.connect(self._on_detail_edited)
        detail_layout.addWidget(self.detail_keywords)

        # Keyword count
        self.kw_count_label = QLabel("")
        self.kw_count_label.setStyleSheet("color: #555; font-size: 11px; border: none;")
        detail_layout.addWidget(self.kw_count_label)

        detail_layout.addStretch()

        results_splitter.addWidget(detail_widget)
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
            "font-size: 11px; color: #888;"
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

            # Default to gemma3:12b or first model
            for i, name in enumerate(model_names):
                if "gemma3:12b" in name or "gemma-3-12b" in name.lower():
                    self.model_combo.setCurrentIndex(i)
                    break

            self.ollama_status.setText(
                f"● {backend_label} connected ({len(model_names)} models)"
            )
            self.ollama_status.setStyleSheet("color: #27ae60; font-size: 12px;")
            self.log(f"Connected to {backend_label} at {connected_url} ({len(model_names)} models)")
        else:
            self.ollama_status.setText("● Not connected")
            self.ollama_status.setStyleSheet("color: #c0392b; font-size: 12px;")
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
                item.setForeground(QColor("#c0392b"))
            self.photo_table.setItem(row, 1, item)

            # Status text
            status_item = QTableWidgetItem(photo.status.capitalize())
            status_item.setFlags(status_item.flags() & ~Qt.ItemIsEditable)
            status_colours = {
                "pending": "#555",
                "processing": "#e8a23a",
                "done": "#27ae60",
                "error": "#c0392b",
            }
            status_item.setForeground(QColor(status_colours.get(photo.status, "#555")))
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
            item.setForeground(QColor("#c0392b"))
        self.photo_table.setItem(row, 1, item)
        status_item = QTableWidgetItem(photo.status.capitalize())
        status_item.setFlags(status_item.flags() & ~Qt.ItemIsEditable)
        status_colours = {"pending": "#555", "processing": "#e8a23a", "done": "#27ae60", "error": "#c0392b"}
        status_item.setForeground(QColor(status_colours.get(photo.status, "#555")))
        self.photo_table.setItem(row, 2, status_item)
        title_text = photo.metadata.title if photo.metadata else ""
        title_item = QTableWidgetItem(title_text)
        title_item.setFlags(title_item.flags() & ~Qt.ItemIsEditable)
        self.photo_table.setItem(row, 3, title_item)
        total = len(self.photos)
        done = sum(1 for p in self.photos if p.status == "done")
        self.photo_count_label.setText(f"{total} photos loaded, {done} processed")

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
            self, "GPS Location Lookup",
            "This feature sends your photos' GPS coordinates to OpenStreetMap\n"
            "(nominatim.openstreetmap.org) to look up place names.\n\n"
            "No image data is sent — only latitude and longitude.\n\n"
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
        """Clear detected folder context and hide indicator."""
        self._detected_folder_context = None
        self.folder_context_label.setText("")
        self.clear_folder_ctx_btn.setVisible(False)

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
        if not self.context_fields["ctx_datetime"].text().strip():
            self.context_fields["ctx_datetime"].setText(ctx.date_str)
        self.log(f"Folder context detected: {ctx.location}, {ctx.date_str} (from '{ctx.raw_folder}')")

    def _detect_and_apply_folder_context(self, filepaths: list):
        """Detect context from folder names and apply if found."""
        if not self.folder_context_check.isChecked():
            return
        ctx = detect_batch_folder_context(filepaths)
        if ctx:
            self._apply_folder_context(ctx)
            self._apply_folder_presets(ctx.raw_folder)

        # EXIF date fallback
        if self.exif_date_fallback_check.isChecked():
            datetime_field = self.context_fields["ctx_datetime"]
            if not datetime_field.text():
                for fp in filepaths[:3]:
                    exif_date = read_exif_date(fp)
                    if exif_date:
                        datetime_field.setText(exif_date)
                        self.log(f"EXIF date fallback: {exif_date}")
                        break

        # GPS reverse-geocode (opt-in, makes external request)
        if self.gps_lookup_check.isChecked():
            location_field = self.context_fields["ctx_location"]
            if not location_field.text():
                for fp in filepaths[:5]:
                    coords = read_gps_coordinates(fp)
                    if coords:
                        place = reverse_geocode(coords[0], coords[1])
                        if place:
                            location_field.setText(place)
                            self.log(f"GPS location: {place}")
                            break

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

        max_tokens_map = {0: 1024, 1: 2048, 2: 4096}
        self.worker = OllamaWorker(
            photos=self.photos,
            model=self.model_combo.currentText(),
            prompt=self.prompt_edit.toPlainText(),
            context=self._get_context_string(),
            ollama_url=self.ollama_url.text().rstrip("/"),
            keywords_list=self._get_keywords_list(),
            backend=getattr(self, "backend", "ollama"),
            max_tokens=max_tokens_map.get(self.response_length_combo.currentIndex(), 512),
            describe_people=self.describe_people_check.isChecked(),
            skip_existing=self.skip_existing_check.isChecked(),
        )
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
            "color: #888; font-size: 10px; font-weight: 600; "
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
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Filename", "Filepath", "Title", "Caption", "Keywords"])
            for photo in completed:
                writer.writerow([
                    photo.filename,
                    photo.filepath,
                    photo.metadata.title,
                    photo.metadata.caption,
                    "; ".join(photo.metadata.keywords),
                ])
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
                photo.metadata = PhotoMetadata(
                    title=title,
                    caption=caption,
                    keywords=keywords,
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

    def _get_model_recommendation(self, info: dict) -> dict:
        """Recommend best model based on hardware."""
        vram = info.get("vram_mb") or 0
        ram = info.get("ram_mb") or 0
        effective = vram if vram > 0 else ram

        if effective >= 28000:
            return {"model": "gemma3:27b", "lm_studio": "gemma-3-27b-it",
                    "desc": "Gemma 3 27B \u2014 best quality", "size": "~17GB download",
                    "pull": "ollama pull gemma3:27b"}
        elif effective >= 14000:
            return {"model": "gemma3:12b", "lm_studio": "gemma-3-12b-it",
                    "desc": "Gemma 3 12B \u2014 great quality/speed balance", "size": "~8GB download",
                    "pull": "ollama pull gemma3:12b"}
        elif effective >= 6000:
            return {"model": "gemma3:4b", "lm_studio": "gemma-3-4b-it",
                    "desc": "Gemma 3 4B \u2014 lightweight, still solid", "size": "~3GB download",
                    "pull": "ollama pull gemma3:4b"}
        else:
            return {"model": "gemma3:4b", "lm_studio": "gemma-3-4b-it",
                    "desc": "Gemma 3 4B \u2014 smallest option", "size": "~3GB download",
                    "pull": "ollama pull gemma3:4b"}

    def _recommend_model(self):
        """Detect hardware and show recommendation."""
        self.log("Detecting hardware...")
        info = self._detect_hardware()

        hw_lines = []
        if info["gpu_name"]:
            hw_lines.append(f"GPU: {info['gpu_name']}")
        if info["vram_mb"]:
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
            msg.setInformativeText(
                f"Recommended: {rec['desc']}\n{rec['size']}\n\n"
                f"In LM Studio, go to the Discover tab and search for:\n"
                f"  {rec['lm_studio']}\n\n"
                f"Download a Q4_K_M quantized version for best results."
            )
            msg.addButton("OK", QMessageBox.AcceptRole)
        else:
            # Ollama user
            msg.setInformativeText(
                f"Recommended: {rec['desc']}\n{rec['size']}\n\n"
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
            "font-size: 14px; font-weight: 600; color: #e8a23a; border: none;"
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
                "font-size: 14px; font-weight: 600; color: #27ae60; border: none;"
            )
            QTimer.singleShot(1000, self._refresh_models)
            QTimer.singleShot(8000, lambda: self.model_download_widget.setVisible(False))
        else:
            self.model_download_label.setText(message)
            self.model_download_label.setStyleSheet(
                "font-size: 14px; font-weight: 600; color: #c0392b; border: none;"
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
    palette.setColor(QPalette.Window, QColor("#1a1a1e"))
    palette.setColor(QPalette.WindowText, QColor("#e0e0e0"))
    palette.setColor(QPalette.Base, QColor("#222226"))
    palette.setColor(QPalette.AlternateBase, QColor("#1c1c20"))
    palette.setColor(QPalette.Text, QColor("#e0e0e0"))
    palette.setColor(QPalette.Button, QColor("#2a2a30"))
    palette.setColor(QPalette.ButtonText, QColor("#e0e0e0"))
    palette.setColor(QPalette.Highlight, QColor("#e8a23a"))
    palette.setColor(QPalette.HighlightedText, QColor("#1a1a1e"))
    app.setPalette(palette)

    window = PhotoScribe()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

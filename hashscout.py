#!/usr/bin/env python3
"""
HashScout v4.0 - Smart Duplicate File, Video & Image Finder
By Mohamed BOURI (enhanced by AI)

What's new in v4
  * Size -> partial(head/middle/tail) -> full-hash pipeline: only files that can
    actually collide are ever read in full
  * Persistent SQLite cache: re-scans of unchanged files are near-instant
  * Hard-link aware, overlap-safe discovery (same file never counted twice)
  * Robust audio fingerprint (spectral-sign / Haitsma-Kalker style, offset tolerant)
  * Letterbox-aware, brightness-normalised video fingerprints, windowed matching
  * Evidence-based confidence fusion with gating and per-group explanations
  * Near-duplicate IMAGE finder (multi-index hashing, no O(n^2) blow-up)
  * Safer cleanup: stat + byte verification, protect/prefer rules, quarantine with
    restore, hard-link replacement, JSONL action log, best-quality strategy
  * Same options for the interactive shell and the command line
"""

from __future__ import annotations

import argparse
import atexit
import cmd
import csv
import difflib
import functools
import hashlib
import io
import json
import math
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from array import array
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
APP_NAME = "HashScout"
VERSION = "4.0.0"

HOME_DIR = Path(os.environ.get("HASHSCOUT_HOME") or (Path.home() / ".hashscout"))
CACHE_PATH = HOME_DIR / "cache.db"
QUARANTINE_ROOT = HOME_DIR / "quarantine"
LOG_DIR = HOME_DIR / "logs"

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv",
    ".webm", ".m4v", ".mpg", ".mpeg", ".3gp", ".ts", ".ogv",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"}

# Directories that must never be scanned by default (OS junk, VCS internals).
DEFAULT_EXCLUDES = [
    "$RECYCLE.BIN", "System Volume Information", ".Trash", ".Trash-*", ".Trashes",
    ".Spotlight-V100", ".fseventsd", "@eaDir", ".git", ".hg", ".svn",
    "__pycache__", ".hashscout",
]

SAMPLE_BYTES = 64 * 1024          # head / middle / tail sample size
READ_CHUNK = 1024 * 1024          # full-hash read size
FP_VERSION = "v4"                 # bump to invalidate cached fingerprints
LSH_PAIR_LIMIT = 250_000          # above this many candidate video pairs, use LSH pruning
MAX_BUCKET = 400                  # ignore degenerate LSH buckets bigger than this

BANNER = r"""
    =/\                 /\=
    / \'._   (\_/)   _.'/ \
   / .''._'--(o.o)--'_.''. \
  /.' _/ |`'=/ " \='`| \_ `.\
 /` .' `\;-,'\___/',-;/` '. '\
/.-'       `\(-V-)/`       `-.\
`      _  _  "   "  _
      | || |__ _ __| |_
      | __ / _` (_-< '  \
      |_||_\__,_/__/_||_|Scout v4
Smart Duplicate File, Video & Image
   Finder by Mohamed BOURI

Type 'help' or '?' to list commands.
Type 'exit' to quit.
contact@mbeffects.com for more help :-)
"""

PROMPT = "\033[92mHashScout>\033[0m "

popcount = int.bit_count if hasattr(int, "bit_count") else (lambda x: bin(x).count("1"))


# ---------------------------------------------------------------------------
# Console helpers
# ---------------------------------------------------------------------------

class UI:
    """All human-oriented status output goes through here so machine formats
    (json/csv) can keep stdout clean by redirecting status to stderr."""

    def __init__(self) -> None:
        self.stream = None

    def out(self):
        return self.stream or sys.stdout

    def say(self, msg: str = "", end: str = "\n") -> None:
        s = self.out()
        s.write(msg + end)
        s.flush()

    def is_tty(self) -> bool:
        try:
            return self.out().isatty()
        except Exception:
            return False


ui = UI()


def color(text: str, code: str) -> str:
    if os.environ.get("NO_COLOR") or not ui.is_tty():
        return text
    return f"\033[{code}m{text}\033[0m"


def tier_color(tier: str, text: str) -> str:
    return color(text, {"HIGH": "92", "MEDIUM": "93", "LOW": "33", "UNLIKELY": "90"}.get(tier, "0"))


class Progress:
    """Throttled single-line progress with rate and ETA (TTY only)."""

    def __init__(self, label: str, total: int, bytes_mode: bool = False) -> None:
        self.label, self.total, self.bytes_mode = label, max(total, 0), bytes_mode
        self.done = 0
        self.start = time.time()
        self.last = 0.0
        self.lock = threading.Lock()
        self.tty = ui.is_tty()

    def update(self, n: int = 1) -> None:
        with self.lock:
            self.done += n
            now = time.time()
            if not self.tty or (now - self.last < 0.15 and self.done < self.total):
                return
            self.last = now
            el = max(now - self.start, 1e-6)
            rate = self.done / el
            eta = (self.total - self.done) / rate if rate > 0 else 0
            pct = 100.0 * self.done / self.total if self.total else 100.0
            if self.bytes_mode:
                body = f"{format_size(self.done)}/{format_size(self.total)} ({pct:.0f}%) {format_size(rate)}/s"
            else:
                body = f"{self.done}/{self.total} ({pct:.0f}%) {rate:.1f}/s"
            line = f"    {self.label}: {body} ETA {format_duration(eta)}"
            ui.say("\r" + line[:100].ljust(100), end="")

    def finish(self) -> None:
        if self.tty:
            ui.say("\r" + " " * 100 + "\r", end="")


# ---------------------------------------------------------------------------
# Enums & Data Classes
# ---------------------------------------------------------------------------

class KeepStrategy(Enum):
    MANUAL = "manual"
    NEWEST = "newest"
    OLDEST = "oldest"
    LARGEST = "largest"
    SMALLEST = "smallest"
    SHORTEST = "shortest"
    LONGEST = "longest"
    FIRST = "first"
    SPREAD = "spread"
    BEST = "best"          # highest resolution/bitrate/codec, then cleanest name
    CLEANEST = "cleanest"  # least "Copy of"/"(1)" looking name, shallowest path


class ScanCancelled(Exception):
    pass


@dataclass
class MediaProbe:
    duration: Optional[float] = None
    width: int = 0
    height: int = 0
    vcodec: str = ""
    acodec: str = ""
    bitrate: int = 0
    fps: float = 0.0
    has_audio: bool = False

    @property
    def resolution(self) -> Optional[Tuple[int, int]]:
        return (self.width, self.height) if self.width and self.height else None

    @property
    def aspect(self) -> Optional[float]:
        return self.width / self.height if self.width and self.height else None


@dataclass
class VideoFP:
    pos: List[float] = field(default_factory=list)   # relative position 0..1 of each kept frame
    ph: List[int] = field(default_factory=list)
    dh: List[int] = field(default_factory=list)
    ah: List[int] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(s: str) -> "VideoFP":
        return VideoFP(**json.loads(s))


@dataclass
class AudioFP:
    kind: str = "hk"                                 # "hk" bit-matrix or "env" energy envelope
    data: List[Any] = field(default_factory=list)
    _mat: Any = field(default=None, repr=False, compare=False)

    def to_json(self) -> str:
        return json.dumps({"kind": self.kind, "data": self.data})

    @staticmethod
    def from_json(s: str) -> "AudioFP":
        d = json.loads(s)
        return AudioFP(kind=d["kind"], data=d["data"])


@dataclass
class ImageFP:
    ph: int = 0
    dh: int = 0
    pixels: int = 0
    aspect: float = 1.0

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(s: str) -> "ImageFP":
        return ImageFP(**json.loads(s))


@dataclass(eq=False)
class FileInfo:
    path: Path
    size: int
    mtime_ns: int
    drive: str = ""
    dev: int = 0
    ino: int = 0
    nlink: int = 1
    partial: Optional[str] = None
    digest: Optional[str] = None
    verified: bool = False            # digest came from a FULL read
    probe: Optional[MediaProbe] = None
    vfp: Optional[VideoFP] = None
    afp: Optional[AudioFP] = None
    ifp: Optional[ImageFP] = None
    links: List[Path] = field(default_factory=list)   # other hard links to the same inode

    @property
    def mtime(self) -> float:
        return self.mtime_ns / 1e9

    @property
    def ext(self) -> str:
        return self.path.suffix.lower()

    @property
    def is_video(self) -> bool:
        return self.ext in VIDEO_EXTENSIONS

    @property
    def is_image(self) -> bool:
        return self.ext in IMAGE_EXTENSIONS

    @property
    def duration(self) -> Optional[float]:
        return self.probe.duration if self.probe else None

    @property
    def resolution(self) -> Optional[Tuple[int, int]]:
        return self.probe.resolution if self.probe else None

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "size": self.size,
            "mtime": datetime.fromtimestamp(self.mtime).isoformat(timespec="seconds"),
            "drive": self.drive,
            "hash": self.digest,
            "verified": self.verified,
            "duration": self.duration,
            "resolution": list(self.resolution) if self.resolution else None,
            "codec": self.probe.vcodec if self.probe else None,
            "bitrate": self.probe.bitrate if self.probe else None,
            "hardlinks": [str(p) for p in self.links],
        }


def confidence_tier(c: float) -> str:
    if c >= 85:
        return "HIGH"
    if c >= 70:
        return "MEDIUM"
    if c >= 55:
        return "LOW"
    return "UNLIKELY"


@dataclass
class DuplicateGroup:
    hash_key: str
    files: List[FileInfo] = field(default_factory=list)
    verified: bool = True
    total_size: int = 0
    wasted_size: int = 0
    drives_involved: Set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.files:
            self.total_size = self.files[0].size * len(self.files)
            self.wasted_size = self.files[0].size * (len(self.files) - 1)
            self.drives_involved = {f.drive for f in self.files if f.drive}


@dataclass
class FuzzyDuplicateGroup:
    files: List[FileInfo] = field(default_factory=list)
    kind: str = "video"
    confidence: float = 0.0
    confidence_tier: str = "unknown"
    relation: str = ""
    similarity_details: Dict[str, float] = field(default_factory=dict)
    total_size: int = 0
    wasted_size: int = 0
    drives_involved: Set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.files:
            self.files = sorted(self.files, key=quality_key, reverse=True)
            self.total_size = sum(f.size for f in self.files)
            self.wasted_size = self.total_size - self.files[0].size
            self.drives_involved = {f.drive for f in self.files if f.drive}
            self.confidence_tier = confidence_tier(self.confidence)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def format_size(n: float) -> str:
    n = float(n)
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n < 1:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{sign}{n:.2f} {unit}"
        n /= 1024
    return f"{sign}{n:.2f} PB"


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "N/A"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgtp]?)(?:i?b)?\s*$", re.I)


def parse_size(text: str) -> int:
    """'10MB', '10M', '1.5 GiB', '4096' -> bytes."""
    m = _SIZE_RE.match(text or "")
    if not m:
        raise ValueError(f"invalid size: {text!r}")
    unit = m.group(2).lower()
    return int(float(m.group(1)) * (1024 ** ("kmgtp".index(unit) + 1) if unit else 1))


@functools.lru_cache(maxsize=None)
def _mount_of(d: str) -> str:
    try:
        if os.path.ismount(d):
            return d
    except OSError:
        return d
    parent = os.path.dirname(d)
    if not parent or parent == d:
        return d
    return _mount_of(parent)


def drive_of_dir(d: str) -> str:
    if os.name == "nt":
        drive, _ = os.path.splitdrive(d)
        return drive.upper() or "?"
    return _mount_of(d)


def get_drive_label(path: Path) -> str:
    return drive_of_dir(str(path.parent))


def same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def files_identical(a: Path, b: Path) -> bool:
    """Streaming byte-for-byte comparison (fails fast on first difference)."""
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                x, y = fa.read(READ_CHUNK), fb.read(READ_CHUNK)
                if x != y:
                    return False
                if not x:
                    return True
    except OSError:
        return False


_NOISE = re.compile(
    r"\b(copy|copie|kopie|final|edited|backup|bak|\d{3,4}p|[hx]\.?26[45]|hevc|avc|av1|aac|ac3|dts|"
    r"mp3|web[-.]?dl|webrip|bluray|brrip|hdtv|hdrip|dvdrip|proper|repack|4k|uhd)\b", re.I)


def normalize_stem(name: str) -> str:
    s = Path(name).stem.lower()
    s = re.sub(r"\(\d+\)", " ", s)
    s = _NOISE.sub(" ", s)
    s = re.sub(r"[\W_]+", " ", s)
    return " ".join(s.split())


def filename_similarity(a: str, b: str) -> float:
    """0.0-1.0 similarity of names after stripping 'copy', '(1)', resolution/codec tags."""
    x, y = normalize_stem(a), normalize_stem(b)
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    if x in y or y in x:
        return 0.85
    return min(0.8, difflib.SequenceMatcher(None, x, y).ratio())


def name_penalty(path: Path) -> float:
    """Lower is a 'cleaner' looking original name. Used as a tie-breaker."""
    n = path.stem.lower()
    pen = 0.0
    if re.search(r"\(\d+\)\s*$", n):
        pen += 3
    if re.search(r"(?<![a-z])(copy|copie|kopie)(?![a-z])", n):
        pen += 3
    if re.search(r"\b(backup|bak|old|tmp|temp)\b", n):
        pen += 1
    return pen + len(n) / 1000.0 + len(path.parts) / 100.0


CODEC_RANK = {"av1": 3, "hevc": 3, "h265": 3, "vp9": 2, "h264": 2, "avc": 2, "mpeg4": 1}


def quality_key(f: FileInfo) -> Tuple:
    """Higher is better: pixels, bitrate, codec generation, has-audio, size."""
    p = f.probe
    if p and p.width:
        pixels = p.width * p.height
    elif f.ifp:
        pixels = f.ifp.pixels
    else:
        pixels = 0
    bitrate = 0
    if p:
        bitrate = p.bitrate or (int(f.size * 8 / p.duration) if p.duration else 0)
    codec = CODEC_RANK.get(p.vcodec.lower(), 1) if p and p.vcodec else 0
    return (pixels, bitrate, codec, 1 if (p and p.has_audio) else 0, f.size)


def split_args(line: str) -> List[str]:
    """Shell-style split that keeps Windows backslashes and handles quoted paths."""
    try:
        if os.name == "nt":
            parts = shlex.split(line, posix=False)
            return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]
        return shlex.split(line)
    except ValueError as e:
        raise ArgError(str(e))


# ---------------------------------------------------------------------------
# Argument parsing plumbing (shared by CLI and shell)
# ---------------------------------------------------------------------------

class ArgError(Exception):
    pass


class ArgExit(Exception):
    def __init__(self, code: int = 0) -> None:
        super().__init__(code)
        self.code = code


class Parser(argparse.ArgumentParser):
    """ArgumentParser that raises instead of killing the interactive shell."""

    def __init__(self, *a, **kw) -> None:
        kw.setdefault("allow_abbrev", False)
        super().__init__(*a, **kw)

    def error(self, message: str) -> None:  # type: ignore[override]
        raise ArgError(message)

    def exit(self, status: int = 0, message: Optional[str] = None) -> None:  # type: ignore[override]
        if message:
            print(message, file=sys.stderr)
        raise ArgExit(status)


# ---------------------------------------------------------------------------
# Persistent cache (SQLite)
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hashes (
    path TEXT NOT NULL, algo TEXT NOT NULL, kind TEXT NOT NULL,
    size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, digest TEXT NOT NULL,
    PRIMARY KEY (path, algo, kind));
CREATE TABLE IF NOT EXISTS blobs (
    path TEXT NOT NULL, kind TEXT NOT NULL, params TEXT NOT NULL,
    size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (path, kind, params));
"""


class Cache:
    """Remembers hashes, probes and fingerprints keyed by (path, size, mtime_ns)."""

    def __init__(self, path: Optional[Path]) -> None:
        self.conn: Optional[sqlite3.Connection] = None
        self.lock = threading.Lock()
        self.dirty = 0
        self.hits = 0
        self.path = path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
            self.conn.executescript(_SCHEMA)
        except (sqlite3.Error, OSError):
            self.conn = None

    @property
    def enabled(self) -> bool:
        return self.conn is not None

    def _one(self, sql: str, args: tuple) -> Optional[tuple]:
        if not self.conn:
            return None
        with self.lock:
            try:
                return self.conn.execute(sql, args).fetchone()
            except sqlite3.Error:
                return None

    def _write(self, sql: str, args: tuple) -> None:
        if not self.conn:
            return
        with self.lock:
            try:
                self.conn.execute(sql, args)
                self.dirty += 1
                if self.dirty >= 500:
                    self.conn.commit()
                    self.dirty = 0
            except sqlite3.Error:
                pass

    def get_hash(self, path: Path, algo: str, kind: str, size: int, mtime_ns: int) -> Optional[str]:
        row = self._one("SELECT digest FROM hashes WHERE path=? AND algo=? AND kind=? AND size=? AND mtime_ns=?",
                        (str(path), algo, kind, size, mtime_ns))
        if row:
            self.hits += 1
            return row[0]
        return None

    def put_hash(self, path: Path, algo: str, kind: str, size: int, mtime_ns: int, digest: str) -> None:
        self._write("INSERT OR REPLACE INTO hashes VALUES (?,?,?,?,?,?)",
                    (str(path), algo, kind, size, mtime_ns, digest))

    def get_blob(self, path: Path, kind: str, params: str, size: int, mtime_ns: int) -> Optional[str]:
        row = self._one("SELECT data FROM blobs WHERE path=? AND kind=? AND params=? AND size=? AND mtime_ns=?",
                        (str(path), kind, params, size, mtime_ns))
        if row:
            self.hits += 1
            return row[0]
        return None

    def put_blob(self, path: Path, kind: str, params: str, size: int, mtime_ns: int, data: str) -> None:
        self._write("INSERT OR REPLACE INTO blobs VALUES (?,?,?,?,?,?)",
                    (str(path), kind, params, size, mtime_ns, data))

    def flush(self) -> None:
        if self.conn:
            with self.lock:
                try:
                    self.conn.commit()
                    self.dirty = 0
                except sqlite3.Error:
                    pass

    def close(self) -> None:
        self.flush()
        if self.conn:
            with self.lock:
                try:
                    self.conn.close()
                except sqlite3.Error:
                    pass
            self.conn = None

    def stats(self) -> Dict[str, Any]:
        if not self.conn:
            return {"enabled": False}
        with self.lock:
            h = self.conn.execute("SELECT COUNT(*) FROM hashes").fetchone()[0]
            b = self.conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
        size = self.path.stat().st_size if self.path and self.path.exists() else 0
        return {"enabled": True, "path": str(self.path), "hash_entries": h, "fingerprint_entries": b,
                "file_size": size}

    def clear(self) -> None:
        if self.conn:
            with self.lock:
                self.conn.executescript("DELETE FROM hashes; DELETE FROM blobs;")
                self.conn.commit()
                try:
                    self.conn.execute("VACUUM")
                except sqlite3.Error:
                    pass

    def prune_missing(self) -> int:
        """Drop entries whose file no longer exists."""
        if not self.conn:
            return 0
        removed = 0
        with self.lock:
            for table in ("hashes", "blobs"):
                rows = self.conn.execute(f"SELECT DISTINCT path FROM {table}").fetchall()
                gone = [(r[0],) for r in rows if not os.path.exists(r[0])]
                self.conn.executemany(f"DELETE FROM {table} WHERE path=?", gone)
                removed += len(gone)
            self.conn.commit()
        return removed


# ---------------------------------------------------------------------------
# External media tools (ffmpeg / ffprobe)
# ---------------------------------------------------------------------------

_CREATIONFLAGS = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW


def _find_tool(name: str) -> Optional[str]:
    env = os.environ.get(f"HASHSCOUT_{name.upper()}")
    if env and (shutil.which(env) or Path(env).exists()):
        return env
    found = shutil.which(name)
    if found:
        return found
    if name == "ffmpeg":
        try:  # a pip-installed static ffmpeg works too
            import imageio_ffmpeg  # type: ignore
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None
    return None


class MediaTools:
    def __init__(self) -> None:
        self.ffmpeg = _find_tool("ffmpeg")
        self.ffprobe = _find_tool("ffprobe")

    @property
    def can_probe(self) -> bool:
        return bool(self.ffprobe or self.ffmpeg)


_TOOLS: Optional[MediaTools] = None


def get_tools() -> MediaTools:
    global _TOOLS
    if _TOOLS is None:
        _TOOLS = MediaTools()
    return _TOOLS


def run_cmd(cmd_list: List[str], timeout: float) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(cmd_list, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout, creationflags=_CREATIONFLAGS)
    except (subprocess.TimeoutExpired, OSError):
        return None


def _num(x: Any) -> Optional[float]:
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _probe_from_json(data: dict) -> Optional[MediaProbe]:
    fmt, streams = data.get("format", {}), data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    p = MediaProbe()
    p.duration = _num(fmt.get("duration")) or (_num(v.get("duration")) if v else None)
    p.bitrate = int(_num(fmt.get("bit_rate")) or 0)
    if v:
        p.width, p.height = int(v.get("width") or 0), int(v.get("height") or 0)
        p.vcodec = v.get("codec_name", "") or ""
        rot = _num((v.get("tags") or {}).get("rotate"))
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = _num(sd["rotation"])
        if rot is not None and int(abs(rot)) % 180 == 90:       # phone video: frames are auto-rotated
            p.width, p.height = p.height, p.width
        rate = str(v.get("avg_frame_rate") or "0/1")
        try:
            n, d = rate.split("/")
            p.fps = float(n) / float(d) if float(d) else 0.0
        except (ValueError, ZeroDivisionError):
            pass
    if a:
        p.has_audio = True
        p.acodec = a.get("codec_name", "") or ""
    return p if (p.duration or p.width) else None


def _probe_from_ffmpeg(ffmpeg: str, path: Path) -> Optional[MediaProbe]:
    r = run_cmd([ffmpeg, "-hide_banner", "-nostdin", "-i", str(path)], 20)
    if not r:
        return None
    text = r.stderr.decode("utf-8", "replace")
    p = MediaProbe()
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        p.duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(r"Video:\s*([\w\-]+).*?,\s*(\d{2,5})x(\d{2,5})", text)
    if m:
        p.vcodec, p.width, p.height = m.group(1), int(m.group(2)), int(m.group(3))
    m = re.search(r"([\d.]+)\s*fps", text)
    if m:
        p.fps = float(m.group(1))
    m = re.search(r"bitrate:\s*(\d+)\s*kb/s", text)
    if m:
        p.bitrate = int(m.group(1)) * 1000
    m = re.search(r"Audio:\s*([\w\-]+)", text)
    if m:
        p.has_audio, p.acodec = True, m.group(1)
    return p if (p.duration or p.width) else None


def probe_media(path: Path) -> Optional[MediaProbe]:
    """One ffprobe call -> duration, resolution (rotation-corrected), codecs, bitrate."""
    t = get_tools()
    if t.ffprobe:
        r = run_cmd([t.ffprobe, "-v", "error", "-print_format", "json", "-show_format",
                     "-show_streams", str(path)], 20)
        if r and r.returncode == 0 and r.stdout:
            try:
                got = _probe_from_json(json.loads(r.stdout.decode("utf-8", "replace")))
                if got:
                    return got
            except ValueError:
                pass
    if t.ffmpeg:
        return _probe_from_ffmpeg(t.ffmpeg, path)
    return None


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def make_hasher(algo: str):
    if algo == "xxh3":
        import xxhash  # type: ignore
        return xxhash.xxh3_128()
    return hashlib.new(algo)


def hash_file(path: Path, size: int, algo: str, mode: str,
              cancel: Optional[threading.Event] = None) -> Optional[str]:
    """mode='partial': head+middle+tail samples (whole file if small). mode='full': everything."""
    try:
        h = make_hasher(algo)
        with open(path, "rb", buffering=0) as f:
            if mode == "partial" and size > SAMPLE_BYTES * 3:
                for off in (0, max(0, size // 2 - SAMPLE_BYTES // 2), size - SAMPLE_BYTES):
                    f.seek(off)
                    h.update(f.read(SAMPLE_BYTES))
            else:
                buf = bytearray(READ_CHUNK)
                mv = memoryview(buf)
                while True:
                    if cancel is not None and cancel.is_set():
                        return None
                    n = f.readinto(buf)
                    if not n:
                        break
                    h.update(mv[:n])
        return h.hexdigest()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Perceptual fingerprints (video frames, audio, images)
# ---------------------------------------------------------------------------

def _np():
    try:
        import numpy as np  # type: ignore
        return np
    except ImportError:
        return None


def pillow_ready() -> bool:
    try:
        import PIL  # noqa: F401
        import imagehash  # noqa: F401
        return True
    except ImportError:
        return False


def _lanczos():
    from PIL import Image
    return getattr(getattr(Image, "Resampling", Image), "LANCZOS")


def _content_box(data: bytes, n: int, thr: int = 24) -> Optional[Tuple[int, int, int, int]]:
    """Bounding box of non-black content inside an n x n gray frame (kills letter/pillar-boxing)."""
    rows = [max(data[y * n:(y + 1) * n]) for y in range(n)]
    cols = [max(data[x::n]) for x in range(n)]
    ys = [y for y, v in enumerate(rows) if v > thr]
    xs = [x for x, v in enumerate(cols) if v > thr]
    if not ys or not xs:
        return None
    top, bottom, left, right = ys[0], ys[-1] + 1, xs[0], xs[-1] + 1
    if (bottom - top) < n * 0.4 or (right - left) < n * 0.4:
        return None
    if (bottom - top) >= n * 0.97 and (right - left) >= n * 0.97:
        return None
    return left, top, right, bottom


def extract_video_fp(ffmpeg: str, path: Path, duration: float, num_frames: int) -> Optional[VideoFP]:
    """Resolution/bitrate/letterbox-robust visual fingerprint.

    One raw 96x96 gray frame per sample point is piped straight from ffmpeg (no temp PNGs).
    Black bars are auto-cropped, the frame is cropped to its central 90 percent (logos/watermarks),
    resized to 32x32, contrast-normalised and hashed with phash+dhash+ahash. Black, white and
    flat frames are skipped because they hash alike across unrelated videos."""
    from PIL import Image, ImageOps, ImageStat
    import imagehash

    if not duration or duration <= 1:
        return None
    n, margin = 96, 0.05
    if num_frames <= 1:
        fracs = [0.5]
    else:
        fracs = [margin + (1 - 2 * margin) * i / (num_frames - 1) for i in range(num_frames)]
    rs = _lanczos()
    fp = VideoFP()
    for fr in fracs:
        r = run_cmd([ffmpeg, "-nostdin", "-v", "error", "-ss", f"{duration * fr:.3f}", "-i", str(path),
                     "-frames:v", "1", "-vf", f"scale={n}:{n}:flags=area", "-pix_fmt", "gray",
                     "-f", "rawvideo", "-"], 30)
        if not r or len(r.stdout) < n * n:
            continue
        data = r.stdout[:n * n]
        img = Image.frombytes("L", (n, n), data)
        st = ImageStat.Stat(img)
        if st.mean[0] < 12 or st.stddev[0] < 5:
            continue
        box = _content_box(data, n)
        if box:
            img = img.crop(box)
        w, h = img.size
        dx, dy = int(w * 0.05), int(h * 0.05)
        if w - 2 * dx >= 16 and h - 2 * dy >= 16:
            img = img.crop((dx, dy, w - dx, h - dy))
        img = ImageOps.autocontrast(img.resize((32, 32), rs), cutoff=1)
        fp.pos.append(fr)
        fp.ph.append(int(str(imagehash.phash(img)), 16))
        fp.dh.append(int(str(imagehash.dhash(img)), 16))
        fp.ah.append(int(str(imagehash.average_hash(img)), 16))
    return fp if len(fp.ph) >= 3 else None


HK_SR, HK_FRAME, HK_HOP = 5512, 2048, 512


def _hk_fingerprint(np, pcm: bytes) -> Optional[AudioFP]:
    """Haitsma-Kalker style: 33 log-spaced bands (300-2000 Hz); each bit is the sign of the
    time/frequency energy gradient. Survives re-encoding, bitrate and loudness changes."""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    if len(x) < HK_FRAME * 8 or float(np.abs(x).mean()) < 20.0:
        return None                                         # too short / silent
    win = np.hanning(HK_FRAME).astype(np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(x, HK_FRAME)[::HK_HOP] * win
    spec = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    edges = np.logspace(np.log10(300), np.log10(2000), 34)
    bins = np.round(edges / (HK_SR / HK_FRAME)).astype(int)
    energy = np.stack([spec[:, bins[i]:bins[i + 1]].sum(axis=1) for i in range(33)], axis=1)
    d = energy[:, :-1] - energy[:, 1:]
    bits = (d[1:] - d[:-1]) > 0
    weights = np.uint64(1) << np.arange(32, dtype=np.uint64)
    ints = (bits.astype(np.uint64) * weights).sum(axis=1)
    return AudioFP("hk", [int(v) for v in ints])


def _env_fingerprint(pcm: bytes) -> Optional[AudioFP]:
    """Dependency-free fallback: RMS loudness envelope, 10 values per second."""
    a = array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        a.byteswap()
    win = HK_SR // 10
    env = []
    for i in range(0, len(a) - win + 1, win):
        seg = a[i:i + win]
        env.append(math.sqrt(sum(v * v for v in seg) / win))
    if len(env) < 40 or sum(env) / len(env) < 20:
        return None
    return AudioFP("env", [round(e, 1) for e in env])


def extract_audio_fp(ffmpeg: str, path: Path, duration: float) -> Optional[AudioFP]:
    if not duration or duration < 8:
        return None
    start, length = duration * 0.25, min(60.0, duration * 0.4)
    r = run_cmd([ffmpeg, "-nostdin", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{length:.2f}",
                 "-i", str(path), "-vn", "-ac", "1", "-ar", str(HK_SR), "-f", "s16le", "-"], 90)
    if not r or len(r.stdout) < HK_SR * 2 * 4:
        return None
    np = _np()
    return _hk_fingerprint(np, r.stdout) if np is not None else _env_fingerprint(r.stdout)


def _bit_matrix(np, fp: AudioFP):
    if fp._mat is None:
        arr = np.array(fp.data, dtype=np.uint64)
        fp._mat = ((arr[:, None] >> np.arange(32, dtype=np.uint64)) & np.uint64(1)).astype(np.uint8)
    return fp._mat


def _pearson(x: Sequence[float], y: Sequence[float]) -> float:
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((v - mx) ** 2 for v in x)
    syy = sum((v - my) ** 2 for v in y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else 0.0


def audio_similarity(a: Optional[AudioFP], b: Optional[AudioFP]) -> Optional[float]:
    """0..1. Tolerates small timing offsets (best bit-error-rate over +-120 frames, about 11 s)."""
    if a is None or b is None or a.kind != b.kind:
        return None
    if a.kind == "hk":
        np = _np()
        if np is None:
            return None
        A, B = _bit_matrix(np, a), _bit_matrix(np, b)
        n = min(len(A), len(B))
        if n < 20:
            return None
        best = 1.0
        for s in range(-min(120, n // 4), min(120, n // 4) + 1):
            x, y = (A[s:], B) if s >= 0 else (A, B[-s:])
            m = min(len(x), len(y))
            if m < n * 0.6:
                continue
            ber = float(np.count_nonzero(x[:m] != y[:m])) / (m * 32)
            best = min(best, ber)
        return max(0.0, min(1.0, (0.40 - best) / 0.30))      # random audio ~ 0.5 BER
    n = min(len(a.data), len(b.data))
    if n < 40:
        return None
    best = -1.0
    for s in range(-30, 31):
        x, y = (a.data[s:], b.data) if s >= 0 else (a.data, b.data[-s:])
        m = min(len(x), len(y))
        if m < n * 0.6:
            continue
        best = max(best, _pearson(x[:m], y[:m]))
    return max(0.0, min(1.0, (best - 0.5) / 0.4))


def _frame_sim(f1: VideoFP, i: int, f2: VideoFP, j: int) -> float:
    dp = popcount(f1.ph[i] ^ f2.ph[j])
    dd = popcount(f1.dh[i] ^ f2.dh[j])
    da = popcount(f1.ah[i] ^ f2.ah[j])
    # dhash is the most robust to resolution/bitrate changes, so it carries the most weight
    return 0.30 * max(0.0, 1 - dp / 18.0) + 0.50 * max(0.0, 1 - dd / 14.0) + 0.20 * max(0.0, 1 - da / 12.0)


def visual_similarity(f1: Optional[VideoFP], f2: Optional[VideoFP]) -> Optional[float]:
    """0..1 blend of strict position-aligned matching and windowed matching (+-10 percent of
    the timeline), which tolerates small trims and intro/outro differences."""
    if not f1 or not f2 or len(f1.ph) < 3 or len(f2.ph) < 3:
        return None

    def directed(a: VideoFP, b: VideoFP) -> Tuple[Optional[float], float]:
        strict, windowed = [], []
        for i, pi in enumerate(a.pos):
            near = min(range(len(b.pos)), key=lambda j: abs(b.pos[j] - pi))
            if abs(b.pos[near] - pi) <= 0.06:
                strict.append(_frame_sim(a, i, b, near))
            best = 0.0
            for j, pj in enumerate(b.pos):
                if abs(pj - pi) <= 0.10:
                    best = max(best, _frame_sim(a, i, b, j))
            windowed.append(best)
        s = sum(strict) / len(strict) if len(strict) >= 3 else None
        return s, sum(windowed) / len(windowed)

    s1, w1 = directed(f1, f2)
    s2, w2 = directed(f2, f1)
    windowed = (w1 + w2) / 2
    if s1 is None or s2 is None:
        return windowed * 0.9
    return 0.5 * ((s1 + s2) / 2) + 0.5 * windowed


SIG_WEIGHTS = {"visual": 0.50, "audio": 0.20, "duration": 0.15, "aspect": 0.05,
               "size": 0.04, "filename": 0.06}


def pair_confidence(a: FileInfo, b: FileInfo, tol_abs: float = 3.0) -> Tuple[float, Dict[str, float]]:
    """Evidence-based fusion. Signals that are unavailable are left out and the rest renormalised
    (v3 padded missing signals with a neutral 0.5). Visual evidence gates the result: a pair that
    does not look alike can never score high however similar the durations or names are."""
    vis = visual_similarity(a.vfp, b.vfp)
    if vis is None:
        return 0.0, {}
    sig: Dict[str, float] = {"visual": vis}

    if a.duration and b.duration:
        ad = abs(a.duration - b.duration)
        ratio = min(a.duration, b.duration) / max(a.duration, b.duration)
        if ad <= tol_abs or ratio >= 0.92:
            sig["duration"] = 1.0
        elif ad <= 10.0 or ratio >= 0.85:
            sig["duration"] = 0.7
        elif ratio >= 0.75:
            sig["duration"] = 0.4
        else:
            sig["duration"] = 0.0

    weights = dict(SIG_WEIGHTS)
    if a.afp and b.afp and a.duration and b.duration \
            and abs(a.duration - b.duration) <= max(2.0, 0.03 * max(a.duration, b.duration)):
        s = audio_similarity(a.afp, b.afp)
        if s is not None:
            sig["audio"] = s
            if a.afp.kind == "env":
                weights["audio"] = 0.10          # loudness envelopes are weak evidence

    if a.probe and b.probe and a.probe.aspect and b.probe.aspect:
        rel = abs(a.probe.aspect - b.probe.aspect) / max(a.probe.aspect, b.probe.aspect)
        sig["aspect"] = 1.0 if rel < 0.02 else 0.7 if rel < 0.08 else 0.3
    if a.size > 0 and b.size > 0:
        sig["size"] = 0.4 + 0.6 * min(a.size, b.size) / max(a.size, b.size)
    sig["filename"] = filename_similarity(str(a.path), str(b.path))

    tot = sum(weights[k] for k in sig)
    conf = 100.0 * sum(sig[k] * weights[k] for k in sig) / tot
    if vis < 0.55:
        conf = min(conf, 40 + vis * 20)
    if sig.get("duration") == 0.0:
        conf = min(conf, 50.0)
    if sig.get("audio", 0.0) >= 0.85 and vis >= 0.70:
        conf += 4.0                               # audio corroborates the picture
    return max(0.0, min(99.5, conf)), sig


def diff_notes(best: FileInfo, other: FileInfo) -> List[str]:
    notes = []
    if best.resolution and other.resolution and best.resolution != other.resolution:
        notes.append("resolution")
    pb, po = best.probe, other.probe
    if pb and po and pb.vcodec and po.vcodec and pb.vcodec != po.vcodec:
        notes.append("codec")
    if pb and po and pb.bitrate and po.bitrate and \
            abs(pb.bitrate - po.bitrate) / max(pb.bitrate, po.bitrate) > 0.25:
        notes.append("bitrate")
    if best.ext != other.ext:
        notes.append("container")
    if best.duration and other.duration and abs(best.duration - other.duration) > 1.0:
        notes.append("duration")
    return notes


def describe_group(files: List[FileInfo]) -> str:
    ranked = sorted(files, key=quality_key, reverse=True)
    notes: List[str] = []
    for o in ranked[1:]:
        for n in diff_notes(ranked[0], o):
            if n not in notes:
                notes.append(n)
    return ("differs in: " + ", ".join(notes)) if notes else "near-identical content"


def cluster_items(n: int, edges: List[Tuple[int, int, float]], score: Callable[[int, int], float],
                  threshold: float, slack: float = 8.0) -> List[List[int]]:
    """Average-safe agglomerative clustering. Plain union-find chains A~B and B~C into one group
    even when A and C look nothing alike; here two clusters only merge when EVERY cross pair
    still scores at least threshold - slack."""
    parent = list(range(n))
    members: Dict[int, List[int]] = {i: [i] for i in range(n)}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j, _ in sorted(edges, key=lambda e: -e[2]):
        ri, rj = find(i), find(j)
        if ri == rj:
            continue
        if all(score(x, y) >= threshold - slack for x in members[ri] for y in members[rj]):
            if len(members[ri]) < len(members[rj]):
                ri, rj = rj, ri
            parent[rj] = ri
            members[ri].extend(members.pop(rj))
    return [m for m in members.values() if len(m) > 1]


def extract_image_fp(path: Path) -> Optional[ImageFP]:
    from PIL import Image, ImageOps, ImageStat
    import imagehash
    try:
        with Image.open(path) as im:
            ow, oh = im.size
            try:
                im.draft("RGB", (640, 640))      # JPEG: decode at reduced size, much faster
            except Exception:
                pass
            im = ImageOps.exif_transpose(im)
            tw, th = im.size
            g = im.convert("L")
        if ImageStat.Stat(g).stddev[0] < 3:      # blank/flat image: would collide with everything
            return None
        return ImageFP(ph=int(str(imagehash.phash(g)), 16), dh=int(str(imagehash.dhash(g)), 16),
                       pixels=ow * oh, aspect=(tw / th) if th else 1.0)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Core Engine
# ---------------------------------------------------------------------------

class HashScoutCore:
    def __init__(self, target_dirs: List[Path], only_exts: Optional[Set[str]] = None,
                 min_size: int = 1, max_size: int = 0,
                 exclude_patterns: Optional[List[str]] = None, default_excludes: bool = True,
                 workers: int = 4, quick_mode: bool = False, algo: str = "sha256",
                 cache: Optional[Cache] = None, hdd: bool = False, use_audio: bool = True) -> None:
        dirs: List[Path] = []
        resolved = sorted({Path(d).resolve() for d in target_dirs if Path(d).is_dir()},
                          key=lambda p: len(p.parts))
        for d in resolved:                      # drop targets nested inside other targets
            if not any(d == k or k in d.parents for k in dirs):
                dirs.append(d)
        self.target_dirs = dirs
        self.only_exts = only_exts
        self.min_size = min_size
        self.max_size = max_size
        pats = (DEFAULT_EXCLUDES if default_excludes else []) + list(exclude_patterns or [])
        self.exclude = [p.replace("\\", "/").lower() for p in pats]
        self.workers = max(1, workers)
        self.quick = quick_mode
        self.algo = algo
        self.cache = cache or Cache(None)
        self.hdd = hdd
        self.use_audio = use_audio
        self.cancel = threading.Event()
        self.tools = get_tools()
        self.stats: Dict[str, float] = defaultdict(float)
        self._lock = threading.Lock()
        self._collapsed: Set[int] = set()

    # ------------------------------------------------------------ helpers
    def _bump(self, key: str, n: float = 1) -> None:
        with self._lock:
            self.stats[key] += n

    def _excluded(self, full: str, name: str, rel: str) -> bool:
        ln, lr, lf = name.lower(), rel.replace("\\", "/").lower(), full.replace("\\", "/").lower()
        return any(fnmatchcase(ln, p) or fnmatchcase(lr, p) or fnmatchcase(lf, p) for p in self.exclude)

    def _parallel(self, items: List[Any], fn: Callable[[Any], Any], label: str,
                  weight: Optional[Callable[[Any], int]] = None,
                  drive: Optional[Callable[[Any], str]] = None) -> List[Tuple[Any, Any]]:
        """Thread-pool map with progress, Ctrl+C cancellation and optional one-reader-per-drive
        mode (--hdd) so spinning disks are read sequentially instead of thrashing."""
        out: List[Tuple[Any, Any]] = []
        if not items:
            return out
        sems: Dict[str, threading.Semaphore] = {}
        if self.hdd and drive:
            items = sorted(items, key=lambda i: str(getattr(i, "path", i)))
            sems = {d: threading.Semaphore(1) for d in {drive(i) for i in items}}
        prog = Progress(label, sum(weight(i) for i in items) if weight else len(items), bool(weight))

        def run(item: Any) -> Any:
            if self.cancel.is_set():
                return None
            if sems and drive:
                with sems[drive(item)]:
                    return fn(item)
            return fn(item)

        ex = ThreadPoolExecutor(max_workers=self.workers)
        try:
            futs = {ex.submit(run, it): it for it in items}
            for fut in as_completed(futs):
                it = futs[fut]
                try:
                    res = fut.result()
                except Exception:
                    res = None
                out.append((it, res))
                prog.update(weight(it) if weight else 1)
        except KeyboardInterrupt:
            self.cancel.set()
            ex.shutdown(wait=True, cancel_futures=True)
            prog.finish()
            raise ScanCancelled()
        ex.shutdown(wait=True)
        prog.finish()
        if self.cancel.is_set():
            raise ScanCancelled()
        return out

    # ---------------------------------------------------------- discovery
    def discover(self) -> List[FileInfo]:
        found: List[FileInfo] = []
        seen: Set[str] = set()
        skipped: List[str] = []
        last = 0.0
        for base in self.target_dirs:
            base_s = str(base)
            stack = [base_s]
            while stack:
                d = stack.pop()
                try:
                    with os.scandir(d) as it:
                        entries = list(it)
                except OSError:
                    skipped.append(d)
                    continue
                drive = drive_of_dir(d)
                for e in entries:
                    try:
                        if e.is_symlink():
                            continue
                        name = e.name
                        rel = e.path[len(base_s):].lstrip("\\/")
                        if e.is_dir(follow_symlinks=False):
                            attrs = getattr(e.stat(follow_symlinks=False), "st_file_attributes", 0)
                            if attrs & 0x400:                  # Windows junction / reparse point
                                continue
                            if not self._excluded(e.path, name, rel):
                                stack.append(e.path)
                            continue
                        if not e.is_file(follow_symlinks=False):
                            continue
                        if self.only_exts is not None and os.path.splitext(name)[1].lower() not in self.only_exts:
                            continue
                        if self._excluded(e.path, name, rel):
                            continue
                        key = os.path.normcase(e.path)
                        if key in seen:                         # overlapping targets: count a file once
                            continue
                        st = e.stat(follow_symlinks=False)
                        if getattr(st, "st_file_attributes", 0) & (0x1000 | 0x40000 | 0x400000):
                            self._bump("cloud_placeholders_skipped")   # OneDrive etc: reading would download
                            continue
                        if st.st_size < self.min_size or (self.max_size and st.st_size > self.max_size):
                            continue
                        seen.add(key)
                        found.append(FileInfo(Path(e.path), st.st_size, st.st_mtime_ns, drive,
                                              st.st_dev, st.st_ino, st.st_nlink))
                    except OSError:
                        continue
                if ui.is_tty() and time.time() - last > 0.25:
                    last = time.time()
                    ui.say(f"\r    Discovering... {len(found)} files", end="")
        if ui.is_tty():
            ui.say("\r" + " " * 60 + "\r", end="")
        self.stats["files"] = len(found)
        self.stats["bytes"] = sum(f.size for f in found)
        self.stats["skipped_dirs"] = len(skipped)
        if skipped:
            ui.say(f"[!] Skipped {len(skipped)} unreadable director{'y' if len(skipped) == 1 else 'ies'}")
            for s in skipped[:5]:
                ui.say(f"    - {s}")
            if len(skipped) > 5:
                ui.say(f"    ... and {len(skipped) - 5} more")
        if self.stats.get("cloud_placeholders_skipped"):
            ui.say(f"[*] Skipped {int(self.stats['cloud_placeholders_skipped'])} cloud-only placeholder file(s)")
        return found

    # ------------------------------------------------------ exact matching
    @staticmethod
    def _fill_id(i: FileInfo) -> None:
        if i.ino:
            return
        try:
            st = os.stat(i.path)
            i.dev, i.ino, i.nlink = st.st_dev, st.st_ino, st.st_nlink
        except OSError:
            pass

    def _collapse_links(self, group: List[FileInfo]) -> List[FileInfo]:
        """Hard links to one inode are one file: keep a single representative."""
        seen: Dict[Tuple[int, int], FileInfo] = {}
        out: List[FileInfo] = []
        for i in group:
            if i.ino:
                key = (i.dev, i.ino)
                if key in seen:
                    seen[key].links.append(i.path)
                    self._collapsed.add(id(i))
                    self._bump("hardlinks_collapsed")
                    continue
                seen[key] = i
            out.append(i)
        return out

    def _hash(self, info: FileInfo, kind: str) -> Optional[str]:
        if self.cancel.is_set():
            return None
        d = self.cache.get_hash(info.path, self.algo, kind, info.size, info.mtime_ns)
        if d:
            self._bump("cache_hits")
            return d
        d = hash_file(info.path, info.size, self.algo, kind, self.cancel)
        if d:
            self.cache.put_hash(info.path, self.algo, kind, info.size, info.mtime_ns, d)
            self._bump("files_hashed")
            self._bump("bytes_hashed", info.size if kind == "full" or info.size <= SAMPLE_BYTES * 3
                       else SAMPLE_BYTES * 3)
        return d

    def analyze_files(self, infos: List[FileInfo]) -> List[FileInfo]:
        """size -> sampled hash -> full hash. Only files that can still collide advance."""
        t0 = time.time()
        by_size: Dict[int, List[FileInfo]] = defaultdict(list)
        for i in infos:
            by_size[i.size].append(i)
        cands: List[FileInfo] = []
        for g in by_size.values():
            if len(g) < 2:
                continue
            for i in g:
                self._fill_id(i)
            g = self._collapse_links(g)
            if len(g) >= 2:
                cands.extend(g)
        ui.say(f"[*] {len(infos)} files | {len(cands)} share a size with another file "
               f"-> only those are hashed ({self.workers} workers, {self.algo})")

        for i, d in self._parallel(cands, lambda x: self._hash(x, "partial"), "Sampling",
                                   drive=lambda x: x.drive):
            i.partial = d
        buckets: Dict[Tuple[int, str], List[FileInfo]] = defaultdict(list)
        for i in cands:
            if i.partial:
                buckets[(i.size, i.partial)].append(i)
        survivors = [i for g in buckets.values() if len(g) > 1 for i in g]

        need_full: List[FileInfo] = []
        for i in survivors:
            if i.size <= SAMPLE_BYTES * 3:          # sample == whole file
                i.digest, i.verified = i.partial, True
            elif self.quick:
                i.digest, i.verified = i.partial, False
            else:
                need_full.append(i)
        if need_full:
            ui.say(f"[*] Full-hash verification of {len(need_full)} candidate(s) "
                   f"({format_size(sum(i.size for i in need_full))})")
            for i, d in self._parallel(need_full, lambda x: self._hash(x, "full"), "Hashing",
                                       weight=lambda x: x.size, drive=lambda x: x.drive):
                i.digest, i.verified = d, d is not None
        self.cache.flush()
        if self._collapsed:                       # hard-link twins are one file, not a duplicate
            infos[:] = [i for i in infos if id(i) not in self._collapsed]
        self.stats["elapsed"] = time.time() - t0
        ui.say(f"[+] Analysis complete in {self.stats['elapsed']:.1f}s | "
               f"read {format_size(self.stats['bytes_hashed'])} | cache hits {int(self.stats['cache_hits'])}"
               + (f" | hard links collapsed {int(self.stats['hardlinks_collapsed'])}"
                  if self.stats["hardlinks_collapsed"] else ""))
        if self.quick:
            ui.say("[!] Quick mode: large-file matches are NOT byte-verified (cleanup will verify before deleting)")
        return infos

    def find_duplicates(self, infos: List[FileInfo]) -> List[DuplicateGroup]:
        m: Dict[Tuple[int, str], List[FileInfo]] = defaultdict(list)
        for i in infos:
            if i.digest:
                m[(i.size, i.digest)].append(i)
        groups = [DuplicateGroup(hash_key=k[1], files=sorted(v, key=lambda f: str(f.path)),
                                 verified=all(f.verified for f in v))
                  for k, v in m.items() if len(v) > 1]
        groups.sort(key=lambda g: g.wasted_size, reverse=True)
        return groups

    # ------------------------------------------------------------- probing
    def ensure_probes(self, infos: List[FileInfo]) -> None:
        need = [i for i in infos if i.is_video and i.probe is None]
        if not need or not self.tools.can_probe:
            return

        def work(i: FileInfo) -> Optional[MediaProbe]:
            raw = self.cache.get_blob(i.path, "probe", FP_VERSION, i.size, i.mtime_ns)
            if raw:
                try:
                    return MediaProbe(**json.loads(raw))
                except (ValueError, TypeError):
                    pass
            p = probe_media(i.path)
            if p:
                self.cache.put_blob(i.path, "probe", FP_VERSION, i.size, i.mtime_ns, json.dumps(asdict(p)))
            return p

        for i, p in self._parallel(need, work, "Probing", drive=lambda x: x.drive):
            i.probe = p
        self.cache.flush()

    # --------------------------------------------------------- fuzzy video
    def _ensure_video_fp(self, i: FileInfo, frames: int) -> None:
        if i.vfp or not i.duration:
            return
        params = f"{FP_VERSION}:{frames}"
        raw = self.cache.get_blob(i.path, "vfp", params, i.size, i.mtime_ns)
        if raw:
            try:
                i.vfp = VideoFP.from_json(raw)
                return
            except (ValueError, TypeError):
                pass
        fp = extract_video_fp(self.tools.ffmpeg, i.path, i.duration, frames)  # type: ignore[arg-type]
        if fp:
            i.vfp = fp
            self.cache.put_blob(i.path, "vfp", params, i.size, i.mtime_ns, fp.to_json())

    def _ensure_audio_fp(self, i: FileInfo) -> None:
        if i.afp or not i.duration:
            return
        params = f"{FP_VERSION}:{'hk' if _np() is not None else 'env'}"
        raw = self.cache.get_blob(i.path, "afp", params, i.size, i.mtime_ns)
        if raw:
            try:
                i.afp = AudioFP.from_json(raw)
                return
            except (ValueError, TypeError, KeyError):
                pass
        fp = extract_audio_fp(self.tools.ffmpeg, i.path, i.duration)  # type: ignore[arg-type]
        if fp:
            i.afp = fp
            self.cache.put_blob(i.path, "afp", params, i.size, i.mtime_ns, fp.to_json())

    @staticmethod
    def _candidate_pairs(videos: List[FileInfo], tol_abs: float, tol_rel: float) -> List[Tuple[int, int]]:
        """videos must be sorted by duration. Duration window first; if that still leaves too many
        pairs for a big library, prune with locality-sensitive buckets on frame dhashes."""
        n = len(videos)
        durs = [v.duration or 0.0 for v in videos]

        def within(di: float, dj: float) -> bool:
            ad = dj - di
            return ad <= tol_abs or (ad / dj if dj else 0.0) <= tol_rel

        count, j = 0, 0
        for i in range(n):
            j = max(j, i + 1)
            while j < n and within(durs[i], durs[j]):
                j += 1
            count += j - (i + 1)
        if count <= LSH_PAIR_LIMIT:
            return _pairs_in_window(durs, within)
        ui.say(f"[*] {count} duration-compatible pairs -> pruning with perceptual buckets")
        buckets: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        for idx, v in enumerate(videos):
            assert v.vfp
            for h in v.vfp.dh:
                for b in range(4):
                    buckets[(b, (h >> (16 * b)) & 0xFFFF)].append(idx)
        hits: Dict[Tuple[int, int], int] = defaultdict(int)
        for members in buckets.values():
            uniq = sorted(set(members))
            if len(uniq) < 2 or len(uniq) > MAX_BUCKET:
                continue
            for x in range(len(uniq)):
                for y in range(x + 1, len(uniq)):
                    hits[(uniq[x], uniq[y])] += 1
        return [p for p, c in hits.items() if c >= 2 and within(durs[p[0]], durs[p[1]])]

    def find_fuzzy_video_duplicates(self, infos: List[FileInfo], num_frames: int = 12,
                                    confidence_threshold: float = 65.0, duration_tolerance: float = 3.0,
                                    relative_tolerance: float = 0.15) -> List[FuzzyDuplicateGroup]:
        """Find same-content videos of different size/resolution/bitrate/container."""
        if not pillow_ready():
            ui.say("[!] Fuzzy video matching needs Pillow and imagehash:  pip install Pillow imagehash")
            return []
        if not self.tools.ffmpeg:
            ui.say("[!] Fuzzy video matching requires ffmpeg (install it or set HASHSCOUT_FFMPEG).")
            return []
        self.ensure_probes(infos)
        videos = [i for i in infos if i.is_video and i.duration and i.duration > 1]
        if len(videos) < 2:
            return []
        ui.say(f"[*] Fuzzy match: fingerprinting {len(videos)} videos "
               f"(visual{' + audio' if self.use_audio else ''}; audio engine: "
               f"{'spectral' if _np() is not None else 'envelope (install numpy for better accuracy)'})")

        def work(i: FileInfo) -> None:
            self._ensure_video_fp(i, num_frames)
            if self.use_audio and i.probe and i.probe.has_audio:
                self._ensure_audio_fp(i)

        self._parallel(videos, work, "Fingerprinting", drive=lambda x: x.drive)
        self.cache.flush()
        videos = sorted([v for v in videos if v.vfp], key=lambda v: v.duration or 0.0)
        if len(videos) < 2:
            return []

        pairs = self._candidate_pairs(videos, duration_tolerance, relative_tolerance)
        if not pairs:
            return []
        ui.say(f"[*] Scoring {len(pairs)} candidate pairs...")
        memo: Dict[Tuple[int, int], Tuple[float, Dict[str, float]]] = {}

        def pc(i: int, j: int) -> Tuple[float, Dict[str, float]]:
            k = (i, j) if i < j else (j, i)
            if k not in memo:
                memo[k] = pair_confidence(videos[k[0]], videos[k[1]], duration_tolerance)
            return memo[k]

        def score(i: int, j: int) -> float:
            return pc(i, j)[0]

        edges = [(i, j, score(i, j)) for i, j in pairs if score(i, j) >= confidence_threshold]
        groups: List[FuzzyDuplicateGroup] = []
        for members in cluster_items(len(videos), edges, score, confidence_threshold):
            confs: List[float] = []
            acc: Dict[str, List[float]] = defaultdict(list)
            for x in range(len(members)):
                for y in range(x + 1, len(members)):
                    c, sig = pc(members[x], members[y])
                    confs.append(c)
                    for k, v in sig.items():
                        acc[k].append(v)
            files = [videos[m] for m in members]
            groups.append(FuzzyDuplicateGroup(
                files=files, kind="video", confidence=sum(confs) / len(confs),
                relation=describe_group(files),
                similarity_details={k: sum(v) / len(v) for k, v in acc.items()}))
        groups.sort(key=lambda g: (g.confidence, g.wasted_size), reverse=True)
        return groups

    # ------------------------------------------------------- similar images
    def find_similar_images(self, infos: List[FileInfo], max_distance: int = 5) -> List[FuzzyDuplicateGroup]:
        """Near-duplicate photos (resized, re-saved, recompressed, lightly edited).
        Multi-index hashing: split the 64-bit phash into (max_distance+1) bands; two hashes within
        that Hamming distance MUST agree exactly on at least one band, so no O(n^2) comparison."""
        if not pillow_ready():
            ui.say("[!] Similar-image matching needs Pillow and imagehash:  pip install Pillow imagehash")
            return []
        imgs = [i for i in infos if i.is_image]
        if len(imgs) < 2:
            return []
        ui.say(f"[*] Similar images: fingerprinting {len(imgs)} images")

        def work(i: FileInfo) -> None:
            raw = self.cache.get_blob(i.path, "ifp", FP_VERSION, i.size, i.mtime_ns)
            if raw:
                try:
                    i.ifp = ImageFP.from_json(raw)
                    return
                except (ValueError, TypeError):
                    pass
            fp = extract_image_fp(i.path)
            if fp:
                i.ifp = fp
                self.cache.put_blob(i.path, "ifp", FP_VERSION, i.size, i.mtime_ns, fp.to_json())

        self._parallel(imgs, work, "Hashing images", drive=lambda x: x.drive)
        self.cache.flush()
        imgs = [i for i in imgs if i.ifp]
        if len(imgs) < 2:
            return []

        T = max_distance

        def pair(a: FileInfo, b: FileInfo) -> Optional[float]:
            assert a.ifp and b.ifp
            dp, dd = popcount(a.ifp.ph ^ b.ifp.ph), popcount(a.ifp.dh ^ b.ifp.dh)
            if dp > T or dd > T + 4:
                return None
            conf = 100.0 - (dp + dd) / 2 * 8
            if abs(a.ifp.aspect - b.ifp.aspect) / max(a.ifp.aspect, b.ifp.aspect, 1e-6) > 0.05:
                conf -= 12.0                      # different crop / aspect
            return max(1.0, conf)

        k = T + 1
        bounds = [round(b * 64 / k) for b in range(k + 1)]
        buckets: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        for idx, im in enumerate(imgs):
            for b in range(k):
                lo, hi = bounds[b], bounds[b + 1]
                buckets[(b, (im.ifp.ph >> lo) & ((1 << (hi - lo)) - 1))].append(idx)  # type: ignore[union-attr]
        seen: Set[Tuple[int, int]] = set()
        edges: List[Tuple[int, int, float]] = []
        for members in buckets.values():
            if len(members) < 2 or len(members) > MAX_BUCKET:
                continue
            for x in range(len(members)):
                for y in range(x + 1, len(members)):
                    p = (members[x], members[y])
                    if p in seen:
                        continue
                    seen.add(p)
                    c = pair(imgs[p[0]], imgs[p[1]])
                    if c is not None:
                        edges.append((p[0], p[1], c))
        floor = max(1.0, 100.0 - (T + 2) * 8 - 12)
        cache_s: Dict[Tuple[int, int], float] = {(a, b): c for a, b, c in edges}

        def score(i: int, j: int) -> float:
            key = (i, j) if i < j else (j, i)
            if key not in cache_s:
                cache_s[key] = pair(imgs[key[0]], imgs[key[1]]) or 0.0
            return cache_s[key]

        groups: List[FuzzyDuplicateGroup] = []
        for members in cluster_items(len(imgs), edges, score, floor, slack=6.0):
            confs, dps, dds = [], [], []
            for x in range(len(members)):
                for y in range(x + 1, len(members)):
                    a, b = imgs[members[x]], imgs[members[y]]
                    confs.append(score(members[x], members[y]))
                    dps.append(popcount(a.ifp.ph ^ b.ifp.ph))   # type: ignore[union-attr]
                    dds.append(popcount(a.ifp.dh ^ b.ifp.dh))   # type: ignore[union-attr]
            files = [imgs[m] for m in members]
            conf = sum(confs) / len(confs)
            px = {f.ifp.pixels for f in files if f.ifp}
            rel = ("same picture, re-saved/recompressed" if conf >= 92 else
                   "same picture, minor edits" if conf >= 76 else "similar picture, heavier edits")
            if len(px) > 1:
                rel += " (different dimensions)"
            groups.append(FuzzyDuplicateGroup(
                files=files, kind="image", confidence=conf, relation=rel,
                similarity_details={"phash": 1 - sum(dps) / len(dps) / 64,
                                    "dhash": 1 - sum(dds) / len(dds) / 64}))
        groups.sort(key=lambda g: (g.confidence, g.wasted_size), reverse=True)
        return groups


def _pairs_in_window(durs: List[float], within: Callable[[float, float], bool]) -> List[Tuple[int, int]]:
    out: List[Tuple[int, int]] = []
    n = len(durs)
    for i in range(n):
        for j in range(i + 1, n):
            if not within(durs[i], durs[j]):
                break
            out.append((i, j))
    return out


# ---------------------------------------------------------------------------
# Output Formatters
# ---------------------------------------------------------------------------

def _file_meta(f: FileInfo) -> str:
    bits = [format_size(f.size), datetime.fromtimestamp(f.mtime).strftime("%Y-%m-%d %H:%M")]
    if f.duration:
        bits.append(format_duration(f.duration))
    if f.resolution:
        bits.append(f"{f.resolution[0]}x{f.resolution[1]}")
    if f.ifp and f.ifp.pixels:
        bits.append(f"{f.ifp.pixels / 1e6:.2f} MP")
    if f.probe and f.probe.vcodec:
        bits.append(f.probe.vcodec)
    if f.probe and f.probe.bitrate:
        bits.append(f"{f.probe.bitrate // 1000} kb/s")
    if f.links:
        bits.append(f"+{len(f.links)} hard link(s)")
    return " | ".join(bits)


class OutputFormatter:
    @staticmethod
    def table(groups: List[DuplicateGroup]) -> str:
        if not groups:
            return "[+] No exact duplicates found."
        lines = [f"{'GROUP':<7} {'FILES':<6} {'DRIVES':<16} {'SIZE EACH':<12} {'WASTED':<12} {'OK':<4} SAMPLE PATH",
                 "-" * 100]
        for i, g in enumerate(groups, 1):
            drives = ", ".join(sorted(g.drives_involved))[:15]
            lines.append(f"{i:<7} {len(g.files):<6} {drives:<16} {format_size(g.files[0].size):<12} "
                         f"{format_size(g.wasted_size):<12} {'yes' if g.verified else 'NO':<4} {str(g.files[0].path)[:40]}")
        lines += ["-" * 100, f"Total groups: {len(groups)} | Total wasted space: "
                  f"{format_size(sum(g.wasted_size for g in groups))}"]
        return "\n".join(lines)

    @staticmethod
    def detailed(groups: List[DuplicateGroup]) -> str:
        if not groups:
            return "[+] No exact duplicates found."
        lines: List[str] = []
        for i, g in enumerate(groups, 1):
            flag = "" if g.verified else color("  [UNVERIFIED quick match]", "93")
            lines.append(f"\n--- [ Group {i} / {len(groups)} ] ---")
            lines.append(f"Hash: {g.hash_key[:16]}... | Size each: {format_size(g.files[0].size)} | "
                         f"Wasted: {format_size(g.wasted_size)}{flag}")
            lines.append(f"Drives: {', '.join(sorted(g.drives_involved))}")
            for j, f in enumerate(g.files, 1):
                lines.append(f"  [{j}] [{f.drive}] {f.path.name}")
                lines.append(f"      Path: {f.path}")
                lines.append(f"      {_file_meta(f)}")
        lines += [f"\n{'=' * 60}", f"Total wasted space: {format_size(sum(g.wasted_size for g in groups))}"]
        return "\n".join(lines)

    @staticmethod
    def fuzzy_detailed(groups: List[FuzzyDuplicateGroup], kind: str = "video") -> str:
        label = "videos" if kind == "video" else "images"
        if not groups:
            return f"[+] No fuzzy (same-content, different-file) {label} found."
        title = ("PROBABLE VIDEO DUPLICATES (visual + audio + duration fusion)" if kind == "video"
                 else "SIMILAR IMAGES (perceptual hash, multi-index)")
        lines = ["\n" + "#" * 72, f"  {title}", "#" * 72]
        for i, g in enumerate(groups, 1):
            sig = " | ".join(f"{k}={v:.0%}" for k, v in sorted(g.similarity_details.items()))
            lines.append(f"\n--- [ {kind.title()} Group {i}/{len(groups)} ] "
                         f"{tier_color(g.confidence_tier, g.confidence_tier)} ~{g.confidence:.1f}% ---")
            lines.append(f"Why: {g.relation} | Drives: {', '.join(sorted(g.drives_involved))} | "
                         f"Potential savings: {format_size(g.wasted_size)}")
            lines.append(f"Signals: {sig}")
            for j, f in enumerate(g.files, 1):
                tag = color(" <- best quality", "92") if j == 1 else ""
                lines.append(f"  [{j}] [{f.drive}] {f.path.name}{tag}")
                lines.append(f"      Path: {f.path}")
                lines.append(f"      {_file_meta(f)}")
        lines += [f"\n{'=' * 72}",
                  "NOTE: These matched by perceptual fusion, NOT byte-for-byte. Review before deleting.",
                  f"Potential additional savings: {format_size(sum(g.wasted_size for g in groups))}"]
        return "\n".join(lines)

    @staticmethod
    def json_out(exact: Sequence[DuplicateGroup], fuzzy: Sequence[FuzzyDuplicateGroup] = (),
                 similar: Sequence[FuzzyDuplicateGroup] = (), stats: Optional[Dict[str, Any]] = None) -> str:
        def fz(g: FuzzyDuplicateGroup) -> dict:
            return {"confidence": round(g.confidence, 2), "tier": g.confidence_tier, "relation": g.relation,
                    "signals": {k: round(v, 3) for k, v in g.similarity_details.items()},
                    "potential_savings_bytes": g.wasted_size, "drives": sorted(g.drives_involved),
                    "files": [f.to_dict() for f in g.files]}
        data = {
            "tool": APP_NAME, "version": VERSION, "timestamp": datetime.now().isoformat(timespec="seconds"),
            "stats": stats or {},
            "exact": {"total_groups": len(exact), "total_wasted_bytes": sum(g.wasted_size for g in exact),
                      "groups": [{"hash": g.hash_key, "verified": g.verified, "wasted_bytes": g.wasted_size,
                                  "drives": sorted(g.drives_involved), "files": [f.to_dict() for f in g.files]}
                                 for g in exact]},
            "similar_videos": [fz(g) for g in fuzzy],
            "similar_images": [fz(g) for g in similar],
        }
        return json.dumps(data, indent=2)

    @staticmethod
    def csv_out(exact: Sequence[DuplicateGroup], fuzzy: Sequence[FuzzyDuplicateGroup] = (),
                similar: Sequence[FuzzyDuplicateGroup] = ()) -> str:
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["type", "group", "confidence", "relation", "hash", "verified", "drive", "path", "size",
                    "mtime", "duration", "resolution"])

        def row(kind: str, gi: int, conf: Any, rel: str, h: str, ver: Any, f: FileInfo) -> None:
            res = f"{f.resolution[0]}x{f.resolution[1]}" if f.resolution else ""
            w.writerow([kind, gi, conf, rel, h, ver, f.drive, str(f.path), f.size, f.mtime,
                        f.duration if f.duration is not None else "", res])
        for i, g in enumerate(exact, 1):
            for f in g.files:
                row("exact", i, "", "identical bytes", g.hash_key, g.verified, f)
        for kind, gs in (("similar_video", fuzzy), ("similar_image", similar)):
            for i, g in enumerate(gs, 1):
                for f in g.files:
                    row(kind, i, f"{g.confidence:.1f}", g.relation, "", "", f)
        return out.getvalue()


# ---------------------------------------------------------------------------
# Cleanup Engine
# ---------------------------------------------------------------------------

class ActionLog:
    """Append-only JSONL audit trail of everything a cleanup run actually did."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.path = LOG_DIR / f"actions-{run_id}.jsonl"
        self._opened = False

    def write(self, rec: Dict[str, Any]) -> None:
        try:
            if not self._opened:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                self._opened = True
            rec = {"ts": datetime.now().isoformat(timespec="seconds"), **rec}
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        except OSError:
            pass


def restore_quarantine(target: Path) -> List[str]:
    """Move quarantined files back. target = a run folder or its manifest.jsonl."""
    if target.is_dir() and not (target / "manifest.jsonl").exists():
        runs = sorted(p.name for p in target.iterdir() if (p / "manifest.jsonl").exists()) if target.exists() else []
        return ["[*] Quarantine runs: " + (", ".join(runs) if runs else "(none)"),
                "    restore <run-folder>   to put one back"]
    manifest = target / "manifest.jsonl" if target.is_dir() else target
    if not manifest.exists():
        return [f"[!] No manifest at {manifest}"]
    msgs: List[str] = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        stored, orig = Path(rec["stored"]), Path(rec["orig"])
        if not stored.exists():
            msgs.append(f"[SKIP] not in quarantine anymore: {orig}")
        elif orig.exists():
            msgs.append(f"[SKIP] original path occupied: {orig}")
        else:
            try:
                orig.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(stored), str(orig))
                msgs.append(f"[RESTORED] {orig}")
            except OSError as e:
                msgs.append(f"[ERROR] {orig}: {e}")
    return msgs or ["[*] Manifest is empty."]


class CleanupEngine:
    def __init__(self, strategy: KeepStrategy, action: str = "trash", dry_run: bool = True,
                 prefer: Sequence[str] = (), protect: Sequence[str] = (), verify: str = "stat") -> None:
        self.strategy = strategy
        self.action = action
        self.dry_run = dry_run
        self.verify = verify
        self.run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.qdir = QUARANTINE_ROOT / self.run_id
        self.log: Optional[ActionLog] = None if dry_run else ActionLog(self.run_id)
        self.removed: Set[str] = set()
        self.t: Dict[str, int] = defaultdict(int)
        self._prefer = [os.path.normcase(str(Path(p).expanduser().resolve())) for p in prefer]
        self._protect = [p for p in protect]
        self._noted_quarantine = False

    # ------------------------------------------------------------ selection
    def _is_preferred(self, f: FileInfo) -> bool:
        s = os.path.normcase(str(f.path))
        return any(s.startswith(p + os.sep) or s == p for p in self._prefer)

    def _is_protected(self, f: FileInfo) -> bool:
        full = str(f.path).replace("\\", "/").lower()
        name = f.path.name.lower()
        for pat in self._protect:
            pl = pat.replace("\\", "/").lower()
            if os.path.isdir(os.path.expanduser(pat)):
                base = os.path.normcase(str(Path(pat).expanduser().resolve()))
                if os.path.normcase(str(f.path)).startswith(base + os.sep):
                    return True
            elif fnmatchcase(name, pl) or fnmatchcase(full, pl):
                return True
        return False

    def _rank(self, f: FileInfo) -> Tuple:
        s = self.strategy
        if s == KeepStrategy.NEWEST:
            primary: Any = -f.mtime_ns
        elif s == KeepStrategy.OLDEST:
            primary = f.mtime_ns
        elif s == KeepStrategy.LARGEST:
            primary = -f.size
        elif s == KeepStrategy.SMALLEST:
            primary = f.size
        elif s == KeepStrategy.SHORTEST:
            primary = f.duration if f.duration is not None else float("inf")
        elif s == KeepStrategy.LONGEST:
            primary = -(f.duration if f.duration is not None else -1.0)
        elif s == KeepStrategy.BEST:
            primary = tuple(-x for x in quality_key(f))
        else:                                   # CLEANEST / SPREAD tie-break
            primary = 0
        return (primary, name_penalty(f.path), f.mtime_ns, str(f.path))

    def pick_keepers(self, files: Sequence[FileInfo]) -> Tuple[List[FileInfo], List[FileInfo]]:
        files = [f for f in files if str(f.path) not in self.removed]
        if len(files) < 2:
            return list(files), []
        if self.strategy == KeepStrategy.MANUAL:
            return [], files
        prot = [f for f in files if self._is_protected(f)]
        if prot:                                    # a protected copy is itself the keeper
            return prot, [f for f in files if f not in prot]
        if self.strategy == KeepStrategy.SPREAD:
            by_drive: Dict[str, FileInfo] = {}
            for f in sorted(files, key=self._rank):
                by_drive.setdefault(f.drive, f)
            keepers = list(by_drive.values())
            return keepers, [f for f in files if f not in keepers]
        pool = [f for f in files if self._is_preferred(f)] or files
        keeper = pool[0] if self.strategy == KeepStrategy.FIRST else min(pool, key=self._rank)
        return [keeper], [f for f in files if f is not keeper]

    # ------------------------------------------------------------ execution
    def _quarantine(self, f: FileInfo) -> Tuple[bool, str]:
        parts = [re.sub(r"[^\w.\-]+", "_", p).strip("_") or "root" for p in f.path.resolve().parts]
        dest = self.qdir.joinpath(*parts)
        n = 1
        while dest.exists():
            dest = dest.with_name(f"{dest.stem}.{n}{dest.suffix}")
            n += 1
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f.path), str(dest))
            with open(self.qdir / "manifest.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"orig": str(f.path), "stored": str(dest), "size": f.size}) + "\n")
            return True, f"[QUARANTINED] [{f.drive}] {f.path}"
        except OSError as e:
            return False, f"[ERROR] [{f.drive}] {f.path}: {e}"

    def _do(self, keeper: FileInfo, f: FileInfo) -> Tuple[bool, str]:
        a = self.action
        if a == "delete":
            try:
                f.path.unlink()
                return True, f"[DELETED] [{f.drive}] {f.path}"
            except OSError as e:
                return False, f"[ERROR] [{f.drive}] {f.path}: {e}"
        if a == "hardlink":
            tmp = f.path.with_name(f.path.name + f".hs{os.getpid()}.tmp")
            try:
                os.link(keeper.path, tmp)
                os.replace(tmp, f.path)
                return True, f"[HARDLINKED] [{f.drive}] {f.path} -> {keeper.path.name}"
            except OSError as e:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass
                return False, f"[ERROR] [{f.drive}] {f.path}: {e}"
        if a == "trash":
            try:
                import send2trash  # type: ignore
            except ImportError:
                if not self._noted_quarantine:
                    self._noted_quarantine = True
                    ui.say("    [i] send2trash not installed: using the HashScout quarantine "
                           "(restore with: restore <run-folder>)")
                return self._quarantine(f)
            try:
                send2trash.send2trash(str(f.path))
                return True, f"[TRASHED] [{f.drive}] {f.path}"
            except Exception as e:
                return False, f"[TRASH FAIL] [{f.drive}] {f.path}: {e}"
        return self._quarantine(f)

    def _one(self, k: FileInfo, d: FileInfo, identical: bool, force_bytes: bool) -> str:
        if not d.path.exists():
            self.t["skipped"] += 1
            return f"[SKIP] already gone: {d.path}"
        if same_file(k.path, d.path):
            self.t["skipped"] += 1
            return f"[SKIP] same underlying file as the keeper: {d.path}"
        try:
            sd, sk = d.path.stat(), k.path.stat()
        except OSError as e:
            self.t["failed"] += 1
            return f"[ERROR] {d.path}: {e}"
        if (sd.st_size, sd.st_mtime_ns) != (d.size, d.mtime_ns):
            self.t["skipped"] += 1
            return f"[SKIP] changed since scan: {d.path}"
        if (sk.st_size, sk.st_mtime_ns) != (k.size, k.mtime_ns):
            self.t["skipped"] += 1
            return f"[SKIP] keeper changed since scan: {k.path}"
        if self.action == "hardlink":
            if not identical:
                self.t["skipped"] += 1
                return f"[SKIP] hard-linking is only allowed for byte-identical files: {d.path}"
            if sd.st_dev != sk.st_dev:
                self.t["skipped"] += 1
                return f"[SKIP] different volume, cannot hard-link: {d.path}"
        if identical and (self.verify == "bytes" or force_bytes) and not self.dry_run:
            if not files_identical(k.path, d.path):
                self.t["skipped"] += 1
                return f"[SKIP] content differs from keeper, NOT touching: {d.path}"
        verb = {"trash": "move to trash", "quarantine": "quarantine", "delete": "PERMANENTLY delete",
                "hardlink": "replace with hard link"}[self.action]
        if self.dry_run:
            self.removed.add(str(d.path))
            self.t["ok"] += 1
            self.t["freed"] += d.size
            return f"[DRY-RUN] Would {verb}: [{d.drive}] {d.path}"
        ok, msg = self._do(k, d)
        if ok:
            self.removed.add(str(d.path))
            self.t["ok"] += 1
            self.t["freed"] += d.size
            if self.log:
                self.log.write({"action": self.action, "path": str(d.path), "keeper": str(k.path), "size": d.size})
        else:
            self.t["failed"] += 1
        return msg

    def process(self, keepers: List[FileInfo], deleters: List[FileInfo], identical: bool = True,
                force_bytes: bool = False) -> List[str]:
        if not keepers or not any(k.path.exists() for k in keepers):
            self.t["skipped"] += len(deleters)
            return ["[SKIP] no surviving keeper, nothing deleted"]
        msgs = []
        for d in deleters:
            live = [k for k in keepers if k.path.exists()]
            k = next((x for x in live if x.drive == d.drive), live[0])
            msgs.append(self._one(k, d, identical, force_bytes))
        return msgs


# ---------------------------------------------------------------------------
# Argument definitions (one source of truth for the CLI and the interactive shell)
# ---------------------------------------------------------------------------

def add_scan_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("paths", nargs="*", help="Directories/drives to scan (default: current)")
    p.add_argument("--video", "--video-only", "-v", dest="video", action="store_true", help="Only video files")
    p.add_argument("--images", action="store_true", help="Only image files")
    p.add_argument("--ext", help="Only these extensions, e.g. .mp4,.mkv")
    p.add_argument("--quick", "-q", action="store_true", help="Skip full hash (cleanup still byte-verifies)")
    p.add_argument("--workers", "-w", type=int, help="Worker threads")
    p.add_argument("--min-size", "-min", default="", help="Minimum size, e.g. 10MB (default: skip empty files)")
    p.add_argument("--max-size", "-max", default="", help="Maximum size, e.g. 1GB")
    p.add_argument("--include-empty", action="store_true", help="Also report zero-byte files")
    p.add_argument("--exclude", "-x", action="append", help="Glob to ignore (repeatable)")
    p.add_argument("--no-default-excludes", action="store_true", help="Also scan .git, $RECYCLE.BIN, ...")
    p.add_argument("--algo", choices=["sha256", "blake2b", "md5", "xxh3"], help="Hash algorithm (default sha256)")
    p.add_argument("--no-cache", action="store_true", help="Do not read/write the persistent cache")
    p.add_argument("--hdd", action="store_true", help="One sequential reader per drive (spinning disks)")
    p.add_argument("--format", choices=["table", "detailed", "json", "csv"], default="detailed",
                   help="Output format (default: detailed)")


def add_detect_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--fuzzy", action="store_true", help="Also find same-content videos of different size/encoding")
    p.add_argument("--similar", action="store_true", help="Also find near-duplicate images")
    p.add_argument("--confidence", type=float, help="Minimum fuzzy video confidence 0-100 (default 65)")
    p.add_argument("--fuzzy-frames", type=int, default=12, help="Frames sampled per video (default 12)")
    p.add_argument("--duration-tolerance", type=float, default=3.0, help="Seconds of duration slack (default 3)")
    p.add_argument("--image-distance", type=int, default=5, help="Max perceptual hash distance 0-12 (default 5)")
    p.add_argument("--no-audio", action="store_true", help="Skip audio fingerprinting")


def add_clean_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--strategy", choices=[s.value for s in KeepStrategy], default="manual",
                   help="Which copy to keep (best = resolution/bitrate/codec, then cleanest name)")
    p.add_argument("--prefer", action="append", default=[], metavar="DIR", help="Keep copies inside DIR (repeatable)")
    p.add_argument("--protect", action="append", default=[], metavar="GLOB|DIR",
                   help="Never delete matching files; a protected copy counts as the keeper")
    p.add_argument("--action", choices=["trash", "quarantine", "delete", "hardlink"],
                   help="trash (default) | quarantine (restorable) | delete | hardlink (replace dupes with links)")
    p.add_argument("--trash", action="store_true", help="(legacy) same as --action trash")
    p.add_argument("--no-trash", action="store_true", help="(legacy) same as --action delete")
    p.add_argument("--apply", action="store_true", help="Actually change files (default is a dry-run)")
    p.add_argument("--interactive", "-i", action="store_true", help="Pick the keeper for every group")
    p.add_argument("--verify", choices=["stat", "bytes"], default="stat",
                   help="Pre-delete check: stat = unchanged since scan, bytes = full byte compare")
    p.add_argument("--min-confidence", type=float, default=85.0,
                   help="Only clean fuzzy/similar groups at or above this confidence (default 85)")
    p.add_argument("--yes", action="store_true", help="Do not ask before permanent deletes")


def build_scan_parser(prog: str = "scan") -> Parser:
    p = Parser(prog=prog, add_help=True)
    add_scan_args(p)
    add_detect_args(p)
    return p


def build_clean_parser(prog: str = "clean") -> Parser:
    p = Parser(prog=prog)
    add_clean_args(p)
    p.add_argument("--fuzzy", action="store_true", help="Include fuzzy video groups from the last scan")
    p.add_argument("--similar", action="store_true", help="Include similar-image groups from the last scan")
    return p


# ---------------------------------------------------------------------------
# Session (state shared by the shell and the command line)
# ---------------------------------------------------------------------------

@dataclass
class CleanItem:
    kind: str
    title: str
    files: List[FileInfo]
    identical: bool
    verified: bool


class Session:
    def __init__(self) -> None:
        self.dirs: List[Path] = [Path(".").resolve()]
        self.workers = 4
        self.confidence = 65.0
        self.excludes: List[str] = []
        self.cache: Optional[Cache] = None
        self.core: Optional[HashScoutCore] = None
        self.infos: List[FileInfo] = []
        self.groups: List[DuplicateGroup] = []
        self.fuzzy: List[FuzzyDuplicateGroup] = []
        self.similar: List[FuzzyDuplicateGroup] = []
        self.fmt = OutputFormatter()

    def close(self) -> None:
        if self.cache:
            self.cache.close()

    def get_cache(self, no_cache: bool) -> Cache:
        if no_cache:
            return Cache(None)
        if self.cache is None:
            self.cache = Cache(CACHE_PATH)
        return self.cache

    def resolve_targets(self, paths: Sequence[str]) -> List[Path]:
        if not paths:
            return list(self.dirs)
        out: List[Path] = []
        for p in paths:
            q = Path(p).expanduser()
            try:
                q = q.resolve()
            except OSError:
                pass
            if q.is_dir():
                out.append(q)
            else:
                ui.say(f"[!] Skipping invalid path: {p}")
        return out

    def make_core(self, a: argparse.Namespace, targets: List[Path]) -> HashScoutCore:
        exts: Set[str] = set()
        if a.video:
            exts |= VIDEO_EXTENSIONS
        if a.images:
            exts |= IMAGE_EXTENSIONS
        if a.ext:
            exts |= {e if e.startswith(".") else "." + e for e in (x.strip().lower() for x in a.ext.split(",")) if e}
        algo = a.algo or "sha256"
        if algo == "xxh3":
            try:
                import xxhash  # noqa: F401
            except ImportError:
                raise ArgError("--algo xxh3 needs the 'xxhash' package (pip install xxhash)")
        try:
            min_size = parse_size(a.min_size) if a.min_size else (0 if a.include_empty else 1)
            max_size = parse_size(a.max_size) if a.max_size else 0
        except ValueError as e:
            raise ArgError(str(e))
        return HashScoutCore(
            target_dirs=targets, only_exts=exts or None, min_size=min_size, max_size=max_size,
            exclude_patterns=list(self.excludes) + list(a.exclude or []),
            default_excludes=not a.no_default_excludes, workers=a.workers or self.workers,
            quick_mode=a.quick, algo=algo, cache=self.get_cache(a.no_cache), hdd=a.hdd,
            use_audio=not a.no_audio)

    # ----------------------------------------------------- candidate plumbing
    def _fuzzy_candidates(self, pred: Callable[[FileInfo], bool]) -> Tuple[List[FileInfo], Dict[int, List[FileInfo]]]:
        """Files already proven byte-identical are represented by ONE copy, so a resized/re-encoded
        version can still match them; the identical siblings are re-attached to the result."""
        in_exact = {id(f) for g in self.groups for f in g.files}
        cands = [i for i in self.infos if pred(i) and id(i) not in in_exact]
        sib: Dict[int, List[FileInfo]] = {}
        for g in self.groups:
            if pred(g.files[0]):
                cands.append(g.files[0])
                sib[id(g.files[0])] = g.files[1:]
        return cands, sib

    @staticmethod
    def _expand(groups: List[FuzzyDuplicateGroup], sib: Dict[int, List[FileInfo]]) -> None:
        for g in groups:
            extra = [x for f in list(g.files) for x in sib.get(id(f), [])]
            if extra:
                g.files = g.files + extra
                g.__post_init__()

    def run_fuzzy(self, core: HashScoutCore, a: argparse.Namespace, human: bool = True) -> None:
        core.use_audio = not getattr(a, "no_audio", False)
        cands, sib = self._fuzzy_candidates(lambda i: i.is_video)
        conf = a.confidence if getattr(a, "confidence", None) is not None else self.confidence
        self.fuzzy = core.find_fuzzy_video_duplicates(
            cands, num_frames=a.fuzzy_frames, confidence_threshold=conf, duration_tolerance=a.duration_tolerance)
        self._expand(self.fuzzy, sib)
        if human:
            ui.say(self.fmt.fuzzy_detailed(self.fuzzy, "video"))

    def run_similar(self, core: HashScoutCore, a: argparse.Namespace, human: bool = True) -> None:
        cands, sib = self._fuzzy_candidates(lambda i: i.is_image)
        self.similar = core.find_similar_images(cands, max_distance=max(0, min(12, a.image_distance)))
        self._expand(self.similar, sib)
        if human:
            ui.say(self.fmt.fuzzy_detailed(self.similar, "image"))

    # ----------------------------------------------------------------- scan
    def run_scan(self, a: argparse.Namespace) -> bool:
        human = a.format in ("table", "detailed")
        targets = self.resolve_targets(a.paths)
        if not targets:
            ui.say("[!] No valid directories provided.")
            return False
        core = self.make_core(a, targets)
        self.core = core
        self.infos, self.groups, self.fuzzy, self.similar = [], [], [], []
        infos = core.discover()
        if not infos:
            ui.say("[!] No files matched.")
            return False
        ui.say(f"[*] {len(infos)} files ({format_size(sum(i.size for i in infos))}) in "
               f"{len(core.target_dirs)} location(s)")
        self.infos = core.analyze_files(infos)
        self.groups = core.find_duplicates(self.infos)
        core.ensure_probes([f for g in self.groups for f in g.files])
        if human:
            ui.say(self.fmt.table(self.groups) if a.format == "table" else self.fmt.detailed(self.groups))
            if self.groups:
                ui.say(f"\n[+] {len(self.groups)} exact duplicate group(s), reclaimable: "
                       f"{format_size(sum(g.wasted_size for g in self.groups))}")
        if a.fuzzy:
            self.run_fuzzy(core, a, human)
        if a.similar:
            self.run_similar(core, a, human)
        core.cache.flush()
        return True

    def stats_dict(self) -> Dict[str, Any]:
        s = {k: (int(v) if float(v).is_integer() else round(v, 2)) for k, v in (self.core.stats.items() if self.core else [])}
        s["cache"] = self.cache.stats() if self.cache else {"enabled": False}
        return s

    def render_machine(self, fmt: str) -> str:
        if fmt == "json":
            return self.fmt.json_out(self.groups, self.fuzzy, self.similar, self.stats_dict())
        return self.fmt.csv_out(self.groups, self.fuzzy, self.similar)

    def export(self, out: Path) -> None:
        suf = out.suffix.lower()
        if suf == ".json":
            text = self.render_machine("json")
        elif suf == ".csv":
            text = self.render_machine("csv")
        else:
            text = "\n".join([self.fmt.detailed(self.groups), self.fmt.fuzzy_detailed(self.fuzzy, "video"),
                              self.fmt.fuzzy_detailed(self.similar, "image")])
        out.write_text(text, encoding="utf-8")
        ui.say(f"[+] Report saved to: {out.resolve()}")

    # ---------------------------------------------------------------- clean
    def _clean_items(self, a: argparse.Namespace) -> List[CleanItem]:
        items = [CleanItem("exact", f"Exact {n}/{len(self.groups)} | wasted {format_size(g.wasted_size)}",
                           g.files, True, g.verified) for n, g in enumerate(self.groups, 1)]
        for flag, groups, kind in ((a.similar, self.similar, "image"), (a.fuzzy, self.fuzzy, "video")):
            if not flag:
                continue
            keep = [g for g in groups if g.confidence >= a.min_confidence]
            skipped = len(groups) - len(keep)
            if skipped:
                ui.say(f"[i] {skipped} {kind} group(s) below --min-confidence {a.min_confidence:.0f} left untouched")
            items += [CleanItem(kind, f"Similar {kind} {n}/{len(keep)} | {g.confidence_tier} ~{g.confidence:.0f}% "
                                      f"| {g.relation}", g.files, False, True) for n, g in enumerate(keep, 1)]
        return items

    @staticmethod
    def _print_files(files: List[FileInfo], marks: Optional[Dict[int, str]] = None) -> None:
        for i, f in enumerate(files, 1):
            m = (marks or {}).get(id(f), "")
            ui.say(f"  [{i}] [{f.drive}] {f.path.name}{m}")
            ui.say(f"      {f.path}")
            ui.say(f"      {_file_meta(f)}")

    def cmd_clean(self, a: argparse.Namespace) -> int:
        if a.fuzzy and not self.fuzzy and self.core and self.infos:
            self.run_fuzzy(self.core, _detect_defaults(self), human=False)
        if a.similar and not self.similar and self.core and self.infos:
            self.run_similar(self.core, _detect_defaults(self), human=False)
        items = self._clean_items(a)
        if not items:
            ui.say("[+] Nothing to clean.")
            return 0
        strategy = KeepStrategy(a.strategy)
        action = a.action or ("delete" if a.no_trash else "trash")
        dry = not a.apply
        engine = CleanupEngine(strategy, action, dry, a.prefer, a.protect, a.verify)
        if action == "delete" and a.apply and not a.yes:
            try:
                ans = input(f"[?] PERMANENTLY delete files in {len(items)} group(s)? type 'yes' to continue: ")
            except (EOFError, KeyboardInterrupt):
                ans = ""
            if ans.strip().lower() != "yes":
                ui.say("[!] Aborted.")
                return 1
        ui.say(f"[*] Strategy: {strategy.value} | Action: {action} | Verify: {a.verify} | "
               f"{'DRY-RUN (add --apply to execute)' if dry else color('APPLYING', '91')}")
        if any(i.kind != "exact" for i in items) and strategy not in (KeepStrategy.BEST, KeepStrategy.MANUAL):
            ui.say("[i] Fuzzy groups hold different files; --strategy best is the safest choice for them.")
        if strategy == KeepStrategy.MANUAL or a.interactive:
            self._interactive(engine, items)
        else:
            self._auto(engine, items)
        t = engine.t
        ui.say(f"\n[+] Done. {'Would free' if dry else 'Freed'} {format_size(t['freed'])} | "
               f"{'planned' if dry else 'removed'} {t['ok']} | skipped {t['skipped']} | failed {t['failed']}")
        if engine.log and t["ok"]:
            ui.say(f"    Action log: {engine.log.path}")
        if action in ("trash", "quarantine") and not dry and engine.qdir.exists():
            ui.say(f"    Quarantine: {engine.qdir}   (undo with: restore \"{engine.qdir}\")")
        return 0 if not t["failed"] else 1

    def _auto(self, engine: CleanupEngine, items: List[CleanItem]) -> None:
        for it in items:
            keepers, deleters = engine.pick_keepers(it.files)
            if not deleters:
                continue
            ui.say(f"\n[{it.title}]")
            for k in keepers:
                ui.say(f"    KEEP [{k.drive}] {k.path}")
            for m in engine.process(keepers, deleters, it.identical, force_bytes=not it.verified):
                ui.say(f"    {m}")

    def _interactive(self, engine: CleanupEngine, items: List[CleanItem]) -> None:
        for n, it in enumerate(items, 1):
            live = [f for f in it.files if str(f.path) not in engine.removed and f.path.exists()]
            if len(live) < 2:
                continue
            best = min(live, key=lambda f: (tuple(-x for x in quality_key(f)), name_penalty(f.path)))
            ui.say(f"\n--- {n}/{len(items)} | {it.title} ---")
            self._print_files(live, {id(best): color("  <- suggested", "92")})
            while True:
                try:
                    ch = input("Keep (numbers like 1 or 1,3 | b=suggested | s=skip | q=quit) > ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    ui.say("\n[!] Aborted.")
                    return
                if ch in ("", "s", "skip"):
                    break
                if ch == "q":
                    return
                if ch == "b":
                    chosen = [best]
                else:
                    try:
                        idx = {int(x) - 1 for x in ch.replace(" ", "").split(",") if x}
                    except ValueError:
                        ui.say("[!] Invalid input.")
                        continue
                    if not idx or any(not 0 <= x < len(live) for x in idx):
                        ui.say("[!] Invalid index.")
                        continue
                    chosen = [live[x] for x in sorted(idx)]
                deleters = [f for f in live if all(f is not c for c in chosen)]
                for m in engine.process(chosen, deleters, it.identical, force_bytes=not it.verified):
                    ui.say(f"    {m}")
                break


def _detect_defaults(sess: Session) -> argparse.Namespace:
    return argparse.Namespace(confidence=None, fuzzy_frames=12, duration_tolerance=3.0, image_distance=5,
                              no_audio=False)


# ---------------------------------------------------------------------------
# Interactive Shell (cmd.Cmd)
# ---------------------------------------------------------------------------

class HashScoutShell(cmd.Cmd):
    intro = BANNER
    prompt = PROMPT

    def __init__(self, session: Optional[Session] = None) -> None:
        super().__init__()
        self.s = session or Session()
        self.p_scan = build_scan_parser("scan")
        self.p_clean = build_clean_parser("clean")
        self.p_fuzzy = Parser(prog="fuzzy")
        self.p_fuzzy.add_argument("--frames", "--fuzzy-frames", dest="fuzzy_frames", type=int, default=12)
        self.p_fuzzy.add_argument("--confidence", type=float)
        self.p_fuzzy.add_argument("--duration-tolerance", type=float, default=3.0)
        self.p_fuzzy.add_argument("--no-audio", action="store_true")
        self.p_similar = Parser(prog="similar")
        self.p_similar.add_argument("--distance", "--image-distance", dest="image_distance", type=int, default=5)

    def emptyline(self) -> bool:      # cmd.Cmd would silently repeat the previous command (e.g. a scan)
        return False

    def onecmd(self, line: str) -> bool:
        try:
            return bool(super().onecmd(line))
        except ArgExit:
            return False
        except ArgError as e:
            print(f"[!] {e}   (type 'help')")
        except ScanCancelled:
            print("\n[!] Cancelled.")
        except KeyboardInterrupt:
            print("\n[!] Interrupted.")
        except Exception as e:                                   # keep the shell alive
            print(f"[!] Error: {e}")
            if os.environ.get("HASHSCOUT_DEBUG"):
                raise
        return False

    def do_scan(self, arg: str) -> None:
        """scan <path> [path2 ...] [options]   (type 'help' for the option list)"""
        if not arg.strip():
            print("[-] Usage: scan <path> [more paths...] [options]")
            return
        a = self.p_scan.parse_args(split_args(arg))
        if a.format in ("json", "csv"):
            a.format = "detailed"
        self.s.run_scan(a)

    def do_fuzzy(self, arg: str) -> None:
        """fuzzy [--frames N] [--confidence N] [--duration-tolerance S] [--no-audio]"""
        if not (self.s.infos and self.s.core):
            print("[!] No scan results. Run 'scan <path>' first.")
            return
        a = self.p_fuzzy.parse_args(split_args(arg))
        self.s.core.cancel.clear()
        self.s.run_fuzzy(self.s.core, a)

    def do_similar(self, arg: str) -> None:
        """similar [--distance N]   near-duplicate images from the last scan"""
        if not (self.s.infos and self.s.core):
            print("[!] No scan results. Run 'scan <path> --images' first.")
            return
        a = self.p_similar.parse_args(split_args(arg))
        self.s.core.cancel.clear()
        self.s.run_similar(self.s.core, a)

    def do_clean(self, arg: str) -> None:
        """clean [--strategy S] [--action A] [--apply] ..."""
        if not (self.s.groups or self.s.fuzzy or self.s.similar or self.s.infos):
            print("[!] No scan results. Run 'scan <path>' first.")
            return
        self.s.cmd_clean(self.p_clean.parse_args(split_args(arg)))

    def do_export(self, arg: str) -> None:
        """export <file.json|csv|txt>"""
        if not self.s.infos:
            print("[!] No scan results. Run 'scan <path>' first.")
            return
        if not arg.strip():
            print("[-] Usage: export <filename.json|csv|txt>")
            return
        self.s.export(Path(arg.strip().strip("\"'")))

    def do_stats(self, arg: str) -> None:
        """Show statistics of the last scan."""
        if not self.s.core:
            print("[!] No scan results.")
            return
        for k, v in self.s.stats_dict().items():
            print(f"  {k}: {v}")

    def do_cache(self, arg: str) -> None:
        """cache [stats|clear|prune]"""
        sub = (arg.strip() or "stats").lower()
        c = self.s.get_cache(False)
        if sub == "clear":
            c.clear()
            print("[+] Cache cleared.")
        elif sub == "prune":
            print(f"[+] Removed {c.prune_missing()} stale entries.")
        else:
            for k, v in c.stats().items():
                print(f"  {k}: {v}")

    def do_restore(self, arg: str) -> None:
        """restore [run-folder|manifest]   undo a quarantine/trash fallback"""
        target = Path(arg.strip().strip("\"'")) if arg.strip() else QUARANTINE_ROOT
        for m in restore_quarantine(target):
            print(m)

    def do_selftest(self, arg: str) -> None:
        """Run the built-in self test."""
        run_selftest()

    def do_cd(self, arg: str) -> None:
        """cd <path>   change the default scan directory"""
        if not arg.strip():
            print(f"[*] Current: {', '.join(str(d) for d in self.s.dirs)}")
            return
        p = Path(arg.strip().strip("\"'")).expanduser().resolve()
        if p.is_dir():
            self.s.dirs = [p]
            print(f"[+] Changed to: {p}")
        else:
            print(f"[!] Not a directory: {p}")

    def do_pwd(self, arg: str) -> None:
        """Show current default directories."""
        for d in self.s.dirs:
            print(f"[*] {d}")

    def do_exclude(self, arg: str) -> None:
        """exclude <pattern> | exclude --list | exclude --clear"""
        a = arg.strip()
        if not a:
            print("[-] Usage: exclude <pattern> | exclude --list | exclude --clear")
        elif a == "--list":
            print("[*] Exclude patterns:" if self.s.excludes else "[*] No exclude patterns set.")
            for p in self.s.excludes:
                print(f"    - {p}")
        elif a == "--clear":
            self.s.excludes = []
            print("[+] Exclude patterns cleared.")
        else:
            self.s.excludes.append(a)
            print(f"[+] Added exclude: {a}")

    def do_workers(self, arg: str) -> None:
        """workers <N>"""
        if arg.strip().isdigit() and int(arg) > 0:
            self.s.workers = int(arg)
            print(f"[+] Workers set to: {self.s.workers}")
        else:
            print(f"[*] Current workers: {self.s.workers}")

    def do_confidence(self, arg: str) -> None:
        """confidence <0-100>"""
        try:
            self.s.confidence = float(arg.strip())
            print(f"[+] Confidence threshold set to: {self.s.confidence}")
        except ValueError:
            print(f"[*] Current confidence threshold: {self.s.confidence}")

    def do_clear(self, arg: str) -> None:
        """Clear the terminal screen."""
        os.system("cls" if os.name == "nt" else "clear")

    def do_exit(self, arg: str) -> bool:
        """Exit HashScout."""
        print("Exiting HashScout. Stay organized.")
        return True

    def do_quit(self, arg: str) -> bool:
        """Exit HashScout."""
        return self.do_exit(arg)

    def do_EOF(self, arg: str) -> bool:
        print()
        return self.do_exit(arg)

    def do_help(self, arg: str) -> None:
        """Show help."""
        print(r"""
====================================================================
                         AVAILABLE COMMANDS
====================================================================
  scan <path> [path2 ...] [opts]     Find exact duplicates (multi-drive)
      --video, -v / --images / --ext .mp4,.mkv      File type filters
      --quick, -q          Skip full hash (cleanup still byte-verifies)
      --workers N          Threads          --hdd   One reader per drive
      --min-size 10MB  --max-size 1GB  --include-empty
      --exclude GLOB       Repeatable       --no-default-excludes
      --algo sha256|blake2b|md5|xxh3        --no-cache
      --fuzzy              Same-content videos, different encoding/size
      --similar            Near-duplicate images (resized/recompressed)
      --confidence N       Fuzzy video threshold 0-100 (default 65)
      --fuzzy-frames N     Frames per video (default 12)
      --duration-tolerance S    --no-audio    --image-distance N

      scan ~/Videos --video --fuzzy --confidence 75
      scan "D:\Photos" E:\ --images --similar

  fuzzy [opts]     Re-run fuzzy video matching on the last scan
  similar [opts]   Re-run similar-image matching on the last scan

  clean [opts]     Clean the last scan (dry-run unless --apply)
      --strategy best|cleanest|newest|oldest|largest|smallest|
                 shortest|longest|first|spread|manual
      --prefer DIR         Keep copies inside DIR      (repeatable)
      --protect GLOB|DIR   Never delete these          (repeatable)
      --action trash|quarantine|delete|hardlink
      --verify stat|bytes  --interactive/-i  --yes
      --fuzzy / --similar  Also clean those groups (>= --min-confidence 85)

  restore [run-folder]     Undo a quarantine run
  export <file>            Save last scan (.json, .csv, .txt)
  stats | cache [stats|clear|prune] | selftest
  cd <path> | pwd | exclude <glob|--list|--clear> | workers N | confidence N
  clear | exit | quit
====================================================================
        """)


# ---------------------------------------------------------------------------
# Self test
# ---------------------------------------------------------------------------

def run_selftest() -> int:
    """Builds a throw-away tree and exercises the safety-critical paths."""
    global QUARANTINE_ROOT, LOG_DIR
    saved = (QUARANTINE_ROOT, LOG_DIR, ui.stream)
    results: List[Tuple[str, bool]] = []

    def check(name: str, cond: bool) -> None:
        results.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        QUARANTINE_ROOT, LOG_DIR = root / "q", root / "logs"
        data = root / "data"
        (data / "sub").mkdir(parents=True)
        blob = os.urandom(300 * 1024)
        (data / "a1.bin").write_bytes(blob)
        (data / "sub" / "a2 (1).bin").write_bytes(blob)
        linked = True
        try:
            os.link(data / "a1.bin", data / "a3.bin")
        except OSError:
            linked = False
        quick = root / "quick"
        quick.mkdir()
        b1 = bytearray(os.urandom(300 * 1024))
        b2 = bytearray(b1)
        b2[len(b2) // 4] ^= 0xFF                      # differs OUTSIDE the head/middle/tail samples
        for d in (data, quick):
            (d / "b1.bin").write_bytes(bytes(b1))
            (d / "b2.bin").write_bytes(bytes(b2))
        (data / "empty1").write_bytes(b"")
        (data / "empty2").write_bytes(b"")
        P, sess = build_cli_parser(), Session()
        ui.stream = io.StringIO()
        try:
            a = P.parse_args(["scan", str(data), str(data / "sub"), "--no-cache"])   # overlapping targets
            sess.run_scan(a)
            paths = [f.path.name for g in sess.groups for f in g.files]
            check("exact duplicates found once despite overlapping targets",
                  len(paths) == 2 and "a2 (1).bin" in paths)
            check("hard link collapsed instead of reported", (not linked) or any(f.links for g in sess.groups for f in g.files))
            check("same size + same samples but different content NOT grouped", "b1.bin" not in paths)
            check("empty files ignored by default", not any(n.startswith("empty") for n in paths))

            q = P.parse_args(["scan", str(quick), "--no-cache", "--quick"])
            sess.run_scan(q)
            check("quick mode flags large matches as unverified",
                  any((not g.verified) and {f.path.name for f in g.files} == {"b1.bin", "b2.bin"} for g in sess.groups))
            c = P.parse_args(["clean", str(quick), "--no-cache", "--quick", "--strategy", "first",
                              "--action", "quarantine", "--apply"])
            sess.cmd_clean(c)
            check("byte verification refuses to delete a false quick match",
                  (quick / "b1.bin").exists() and (quick / "b2.bin").exists())

            sess.run_scan(a)
            sess.cmd_clean(P.parse_args(["clean", str(data), "--no-cache", "--strategy", "cleanest"]))
            check("dry-run changes nothing", (data / "a1.bin").exists() and (data / "sub" / "a2 (1).bin").exists())
            sess.cmd_clean(P.parse_args(["clean", str(data), "--no-cache", "--strategy", "cleanest",
                                         "--action", "quarantine", "--apply"]))
            check("cleanest keeps the original-looking name", (data / "a1.bin").exists()
                  and not (data / "sub" / "a2 (1).bin").exists())
            runs = [p for p in QUARANTINE_ROOT.iterdir() if (p / "manifest.jsonl").exists()] if QUARANTINE_ROOT.exists() else []
            check("quarantine manifest written", bool(runs))
            if runs:
                restore_quarantine(runs[0])
            check("restore puts the file back", (data / "sub" / "a2 (1).bin").exists())

            a2 = Session()
            a2.run_scan(a)
            hs = P.parse_args(["clean", str(data), "--no-cache", "--strategy", "cleanest", "--protect", "a2*",
                               "--action", "quarantine", "--apply"])
            a2.cmd_clean(hs)
            check("--protect keeps the protected copy", (data / "sub" / "a2 (1).bin").exists())
        finally:
            ui.stream = saved[2]
            QUARANTINE_ROOT, LOG_DIR = saved[0], saved[1]
    for name, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# Command-line entry point
# ---------------------------------------------------------------------------

def build_cli_parser() -> Parser:
    p = Parser(prog="hashscout", formatter_class=argparse.RawDescriptionHelpFormatter,
               description=f"{APP_NAME} v{VERSION} - Smart Duplicate File, Video & Image Finder",
               epilog=r"""
Examples:
  hashscout                                   Launch the interactive shell
  hashscout scan ~/Downloads ~/Backups --workers 8
  hashscout scan D:\ E:\ --video --fuzzy --confidence 75
  hashscout scan ~/Photos --images --similar --format json > report.json
  hashscout clean ~/Videos --fuzzy --strategy best --min-confidence 90          (dry-run)
  hashscout clean ~/Downloads --strategy cleanest --action quarantine --apply
  hashscout clean D:\ E:\ --action hardlink --apply                             (reclaim space, keep paths)
  hashscout restore                           List quarantine runs
""")
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    p.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    sub = p.add_subparsers(dest="command", parser_class=Parser)

    sp = sub.add_parser("scan", help="Scan and report duplicates")
    add_scan_args(sp)
    add_detect_args(sp)

    cp = sub.add_parser("clean", help="Scan, then clean (dry-run unless --apply)")
    add_scan_args(cp)
    add_detect_args(cp)
    add_clean_args(cp)

    ep = sub.add_parser("export", help="Scan and write a report (.json/.csv/.txt)")
    add_scan_args(ep)
    add_detect_args(ep)
    ep.add_argument("--output", "-o", required=True, help="Output file path")

    rp = sub.add_parser("restore", help="Restore files from a quarantine run")
    rp.add_argument("target", nargs="?", help="Run folder or manifest.jsonl (omit to list runs)")

    kp = sub.add_parser("cache", help="Inspect or reset the persistent cache")
    kp.add_argument("action", choices=["stats", "clear", "prune"], nargs="?", default="stats")

    sub.add_parser("selftest", help="Run built-in safety tests")
    return p


def dispatch(sess: Session, a: argparse.Namespace) -> int:
    cmd_name = a.command
    if cmd_name == "selftest":
        return run_selftest()
    if cmd_name == "restore":
        for m in restore_quarantine(Path(a.target) if a.target else QUARANTINE_ROOT):
            print(m)
        return 0
    if cmd_name == "cache":
        c = sess.get_cache(False)
        if a.action == "clear":
            c.clear()
            print("[+] Cache cleared.")
        elif a.action == "prune":
            print(f"[+] Removed {c.prune_missing()} stale entries.")
        else:
            for k, v in c.stats().items():
                print(f"  {k}: {v}")
        return 0

    machine = cmd_name == "scan" and a.format in ("json", "csv")
    if machine or cmd_name == "export":
        ui.stream = sys.stderr           # keep stdout clean for the report
    if cmd_name == "export":
        a.format = "json"
    ok = sess.run_scan(a)
    ui.stream = None
    if not ok:
        return 0
    if cmd_name == "scan":
        if machine:
            print(sess.render_machine(a.format))
        return 1 if (sess.groups or sess.fuzzy or sess.similar) else 0
    if cmd_name == "export":
        sess.export(Path(a.output))
        return 0
    if cmd_name == "clean":
        if not (sess.groups or sess.fuzzy or sess.similar):
            print("[+] No duplicates found.")
            return 0
        return sess.cmd_clean(a)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    if os.name == "nt":
        os.system("")                                    # enable ANSI escapes in the Windows console
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")              # odd filenames must never crash a report
        except Exception:
            pass
    parser = build_cli_parser()
    try:
        args = parser.parse_args(argv)
    except ArgExit as e:
        return e.code
    except ArgError as e:
        parser.print_usage(sys.stderr)
        print(f"hashscout: error: {e}", file=sys.stderr)
        return 2
    if args.no_color:
        os.environ["NO_COLOR"] = "1"
    sess = Session()
    atexit.register(sess.close)
    try:
        if not args.command:
            HashScoutShell(sess).cmdloop()
            return 0
        return dispatch(sess, args)
    except ArgError as e:
        print(f"hashscout: error: {e}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, ScanCancelled):
        print("\n[!] Cancelled.", file=sys.stderr)
        return 130


def run_cli() -> None:
    sys.exit(main())


if __name__ == "__main__":
    run_cli()

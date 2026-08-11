#!/usr/bin/env python3
"""
HashScout v2.3 - Advanced Duplicate File & Video Finder
Multi-drive scanning, cross-volume deduplication, smart auto-cleanup,
forensic reporting, and interactive shell mode.
By Mohamed BOURI

Changelog (v2.2 -> v2.3, accuracy & robustness pass):
  * Drive/volume identification now keyed off the OS device ID (st_dev)
    instead of re-walking parent directories per file. This is both
    correct (the old string-matching approach could mislabel volumes
    when mount-point detection failed) and far faster on large scans.
  * Hardlink-aware duplicate accounting: files that are hardlinks of
    each other (same device+inode) are detected and no longer counted
    as recoverable "wasted space" -- they already share the same bytes
    on disk, so deleting one frees nothing. Reports now say so.
  * A pre-delete safety check re-verifies each file's size/mtime right
    before it's removed, so a scan that's gone stale (edited/replaced
    file since the scan ran) can't cause a wrong deletion.
  * Permanent (non-trash) batch deletes now require an explicit
    confirmation ("--yes" to skip it in scripts) before anything is
    removed.
  * 0-byte files are excluded by default (they can't be "deduplicated"
    for any real disk-space benefit); opt back in with --include-empty.
  * Excluded directories are now pruned during the walk instead of
    filtered after the fact, so large ignored trees (.git, node_modules,
    backup snapshots) are never even descended into. --skip-system adds
    a small built-in ignore list for common OS/junk directories.
  * ffprobe duration lookups now fall back to stream-level duration when
    the container doesn't report one, improving fuzzy-match accuracy.
  * Optional persistent hash cache (--cache-file / shell `cache`
    command) remembers verified full-file hashes by path+size+mtime, so
    re-scanning an unchanged tree skips re-hashing entirely.
"""

from __future__ import annotations

import argparse
import cmd
import csv
import hashlib
import json
import os
import shutil
import stat as stat_module
import subprocess
import sys
import shlex
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from datetime import datetime
from enum import Enum
from fnmatch import fnmatch
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Set

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
APP_NAME = "HashScout"
VERSION = "2.3.0"
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv",
    ".webm", ".m4v", ".mpg", ".mpeg", ".3gp", ".ts", ".ogv"
}

# Common junk/system directories nobody wants scanned or reported as
# "duplicates". Only applied when the user opts in via --skip-system,
# since silently ignoring paths by default would be surprising.
DEFAULT_SYSTEM_IGNORE = [
    ".git", ".svn", ".hg", "node_modules", "__pycache__",
    "System Volume Information", "$RECYCLE.BIN",
    ".Trash*", ".Trashes", ".fseventsd", ".Spotlight-V100",
    ".DocumentRevisions-V100",
]

# Bytes read per I/O call while hashing. 1MB instead of the old 64KB cuts
# syscall count ~16x on large files (a 10GB video drops from ~160,000
# read() calls to ~10,000) -- this matters a lot on network/USB drives
# where each syscall has real round-trip latency, and it's still tiny
# next to typical video file sizes.
READ_CHUNK_SIZE = 1024 * 1024

# Sensible default thread count: enough to keep several files' I/O in
# flight at once (hashing is I/O-bound, so more threads than cores helps
# up to a point), but capped so a huge machine doesn't try to hammer a
# single spinning disk with dozens of concurrent seeks.
DEFAULT_WORKERS = min(8, max(4, os.cpu_count() or 4))

BANNER = r"""
    =/\                 /\=
    / \'._   (\_/)   _.'/ \
   / .''._'--(o.o)--'_.''. \
  /.' _/ |`'=/ " \='`| \_ `.\
 /` .' `\;-,'\___/',-;/` '. '\
/.-'       `\(-V-)/`       `-.\
`            "   "
      _  _         _    
     | || |__ _ __| |_ 
     | __ / _` (_-< '  \
     |_||_\__,_/__/_||_|Scout
Smart Video & Bit-Exact Duplicate 
   Finder by Mohamed BOURI

Type 'help' or '?' to list commands. 
Type 'exit' to quit.
contact@mbeffects.com for more help :-)
"""

PROMPT = "HashScout>"

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
    SPREAD = "spread"          # Keep one per drive (new!)


@dataclass
class FileInfo:
    path: Path
    size: int
    mtime: float
    drive: str = ""           # Drive/volume label
    sha256: Optional[str] = None
    partial_hash: Optional[str] = None
    duration: Optional[float] = None
    is_video: bool = False
    video_phash: Optional[List[Tuple[float, str]]] = None  # (timestamp, phash) per sampled frame, chronological
    dev_ino: Optional[Tuple[int, int]] = None  # (st_dev, st_ino) -- identifies hardlinks

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "size": self.size,
            "mtime": self.mtime,
            "drive": self.drive,
            "sha256": self.sha256,
            "duration": self.duration,
            "is_video": self.is_video,
            "inode": f"{self.dev_ino[0]}:{self.dev_ino[1]}" if self.dev_ino else None,
        }


@dataclass
class DuplicateGroup:
    hash_key: str
    files: List[FileInfo] = field(default_factory=list)
    total_size: int = 0
    wasted_size: int = 0
    drives_involved: Set[str] = field(default_factory=set)
    has_hardlinks: bool = False

    def __post_init__(self):
        if self.files:
            size = self.files[0].size
            self.total_size = size * len(self.files)

            # Files that are hardlinks of each other (same device+inode)
            # already share the same on-disk bytes -- deleting one frees
            # nothing. Count unique inodes rather than raw file count so
            # "wasted size" reflects space actually recoverable.
            inodes = [f.dev_ino for f in self.files]
            known_inodes = [i for i in inodes if i is not None]
            unique_inodes = set(known_inodes)
            unknown_count = len(inodes) - len(known_inodes)
            effective_copies = len(unique_inodes) + unknown_count
            self.has_hardlinks = len(unique_inodes) < len(known_inodes)

            self.wasted_size = size * max(effective_copies - 1, 0)
            self.drives_involved = {f.drive for f in self.files if f.drive}


@dataclass
class FuzzyDuplicateGroup:
    """Videos that are probably the same content but NOT byte-identical
    (different size/bitrate/resolution/container, or one is a trimmed cut
    of the other). Matched by duration + perceptual frame hashing rather
    than exact hash, so these are probabilistic -- always review before
    deleting."""
    files: List[FileInfo] = field(default_factory=list)
    similarity: float = 0.0   # average perceptual match, 0-100%
    total_size: int = 0
    wasted_size: int = 0
    drives_involved: Set[str] = field(default_factory=set)
    duration_spread: float = 0.0  # seconds between the shortest and longest member -- a large spread means trimming, not just re-encoding

    def __post_init__(self):
        if self.files:
            self.total_size = sum(f.size for f in self.files)
            # Assumes you'd keep the largest/highest-quality copy
            self.wasted_size = self.total_size - max(f.size for f in self.files)
            self.drives_involved = {f.drive for f in self.files if f.drive}
            durations = [f.duration for f in self.files if f.duration]
            if durations:
                self.duration_spread = max(durations) - min(durations)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def format_size(bytes_size: int) -> str:
    if bytes_size <= 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(bytes_size) < 1024:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.2f} PB"


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "N/A"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def hamming_distance(hash1: str, hash2: str) -> int:
    """Bit difference between two hex perceptual hashes. 0 = identical frame,
    higher = more different. imagehash.phash defaults to a 64-bit hash."""
    try:
        return bin(int(hash1, 16) ^ int(hash2, 16)).count("1")
    except (ValueError, TypeError):
        return 64


def parse_size(size_str: str) -> int:
    size_str = size_str.strip().upper().replace(" ", "")
    multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    for suffix, mult in sorted(multipliers.items(), key=lambda x: -x[1]):
        if size_str.endswith(suffix):
            return int(float(size_str[:-len(suffix)]) * mult)
    return int(size_str)


def safe_trash(path: Path) -> Tuple[bool, str]:
    try:
        import send2trash
        send2trash.send2trash(str(path))
        return True, f"[TRASHED] {path}"
    except ImportError:
        pass
    except Exception as e:
        return False, f"[TRASH FAIL] {path}: {e}"

    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        return True, f"[DELETED] {path}"
    except Exception as e:
        return False, f"[ERROR] {path}: {e}"


# ---------------------------------------------------------------------------
# Core Engine
# ---------------------------------------------------------------------------

class HashScoutCore:
    def __init__(self, target_dirs: List[Path], video_only: bool = False,
                 min_size: int = 0, max_size: int = 0,
                 exclude_patterns: Optional[List[str]] = None,
                 workers: int = DEFAULT_WORKERS, quick_mode: bool = False,
                 include_empty: bool = False, skip_system: bool = False,
                 cache_file: Optional[str] = None):
        self.target_dirs = [d.resolve() for d in target_dirs if d.exists() and d.is_dir()]
        self.video_only = video_only
        self.min_size = min_size
        self.max_size = max_size
        self.exclude = list(exclude_patterns or [])
        if skip_system:
            self.exclude.extend(DEFAULT_SYSTEM_IGNORE)
        self.workers = workers
        self.quick_mode = quick_mode
        self.include_empty = include_empty
        self.cache_file = cache_file
        self._ffprobe_available: Optional[bool] = None
        self._ffmpeg_available: Optional[bool] = None
        self._drive_label_cache: Dict[int, str] = {}
        self._cache: Dict[str, dict] = {}
        self._load_cache()

    # ---------------------------------------------------------------- #
    # Drive/volume identification & persistent hash cache
    # ---------------------------------------------------------------- #

    def _get_drive_label(self, path: Path, st_dev: int) -> str:
        """Resolve a stable volume/drive label for `path`.

        The OS device ID (st_dev) is the source of truth for "which
        volume is this file on" -- it's what actually distinguishes
        drives/mounts and is already available from the stat() call we
        make anyway. We only do the more expensive mount-point string
        lookup once per unique device and cache the friendly label after
        that, instead of walking parent directories for every file (the
        old approach, which was both slow on large scans and could
        silently mislabel a file's volume if a stat() in the walk
        failed partway up)."""
        cached = self._drive_label_cache.get(st_dev)
        if cached is not None:
            return cached

        label = f"dev{st_dev}"
        try:
            if os.name == "nt":
                label = path.drive.upper() if path.drive else label
            else:
                resolved = path.resolve()
                for parent in [resolved] + list(resolved.parents):
                    try:
                        if parent.stat().st_dev != st_dev:
                            break
                        if parent.is_mount():
                            label = str(parent)
                            break
                    except OSError:
                        break
                else:
                    label = "/"
        except Exception:
            pass

        self._drive_label_cache[st_dev] = label
        return label

    def _load_cache(self) -> None:
        if not self.cache_file:
            return
        try:
            self._cache = json.loads(Path(self.cache_file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._cache = {}

    def _save_cache(self) -> None:
        if not self.cache_file:
            return
        try:
            Path(self.cache_file).write_text(json.dumps(self._cache), encoding="utf-8")
        except OSError as e:
            print(f"[!] Could not write hash cache: {e}")

    def _cache_lookup(self, path: Path, size: int, mtime: float) -> Optional[dict]:
        entry = self._cache.get(str(path))
        if entry and entry.get("size") == size and entry.get("mtime") == mtime and entry.get("sha256"):
            return entry
        return None

    def _cache_store(self, path: Path, size: int, mtime: float,
                      sha256: str, duration: Optional[float]) -> None:
        self._cache[str(path)] = {"size": size, "mtime": mtime, "sha256": sha256, "duration": duration}

    def _should_ignore(self, path: Path, base_dir: Path) -> bool:
        try:
            rel = path.relative_to(base_dir).as_posix()
        except ValueError:
            rel = path.name
        for pat in self.exclude:
            if fnmatch(rel, pat) or fnmatch(path.name, pat):
                return True
        return False

    def discover(self) -> List[Tuple[Path, Path, os.stat_result]]:
        """Return list of (file_path, base_dir, stat_result) tuples.

        stat() is captured exactly once here and threaded through the rest
        of the pipeline (cache lookup, size pre-filtering, hashing, FileInfo
        construction) instead of every stage re-stating the same file --
        that alone removes 2-3 redundant syscalls per file, which adds up
        fast on network/removable drives where each syscall is a real
        round trip.

        When multiple target directories are given (e.g. scanning several
        drives at once), each is walked on its own thread. Directory
        listing and stat() calls block on I/O and release the GIL, so
        walking N physically-separate drives concurrently is a genuine
        speedup, not just concurrency theater."""
        skipped: List[str] = []
        skipped_empty = 0

        def _walk_one(base_dir: Path) -> Tuple[List[Tuple[Path, Path, os.stat_result]], List[str], int]:
            local_results: List[Tuple[Path, Path, os.stat_result]] = []
            local_skipped: List[str] = []
            local_empty = 0

            def _on_error(err: OSError) -> None:
                local_skipped.append(getattr(err, "filename", None) or str(err))

            for root, dirs, names in os.walk(base_dir, onerror=_on_error):
                root_path = Path(root)
                # Prune ignored directories in place so os.walk never even
                # descends into them -- much faster than filtering files
                # after the fact when excluding large trees like .git,
                # node_modules, or backup snapshots.
                dirs[:] = [d for d in dirs if not self._should_ignore(root_path / d, base_dir)]

                for name in names:
                    fpath = root_path / name
                    if fpath.is_symlink():
                        continue
                    if self.video_only and fpath.suffix.lower() not in VIDEO_EXTENSIONS:
                        continue
                    if self._should_ignore(fpath, base_dir):
                        continue
                    try:
                        st = fpath.stat()
                        # Reuse the stat we already paid for instead of
                        # calling is_file() (which would silently stat()
                        # the path again under the hood).
                        if not stat_module.S_ISREG(st.st_mode):
                            continue
                        sz = st.st_size
                        if sz == 0 and not self.include_empty:
                            local_empty += 1
                            continue
                        if self.min_size and sz < self.min_size:
                            continue
                        if self.max_size and sz > self.max_size:
                            continue
                        local_results.append((fpath, base_dir, st))
                    except OSError:
                        continue

            return local_results, local_skipped, local_empty

        results: List[Tuple[Path, Path, os.stat_result]] = []
        if len(self.target_dirs) > 1:
            with ThreadPoolExecutor(max_workers=min(len(self.target_dirs), 8)) as ex:
                for local_results, local_skipped, local_empty in ex.map(_walk_one, self.target_dirs):
                    results.extend(local_results)
                    skipped.extend(local_skipped)
                    skipped_empty += local_empty
        else:
            for base_dir in self.target_dirs:
                local_results, local_skipped, local_empty = _walk_one(base_dir)
                results.extend(local_results)
                skipped.extend(local_skipped)
                skipped_empty += local_empty

        if skipped:
            label = "directory" if len(skipped) == 1 else "directories"
            print(f"[!] Skipped {len(skipped)} unreadable {label} (permission denied)")
            for d in skipped[:5]:
                print(f"    - {d}")
            if len(skipped) > 5:
                print(f"    ... and {len(skipped) - 5} more")

        if skipped_empty:
            print(f"[*] Ignored {skipped_empty} empty (0-byte) file(s) -- use --include-empty to include them")

        return results

    def _has_ffprobe(self) -> bool:
        if self._ffprobe_available is not None:
            return self._ffprobe_available
        try:
            subprocess.run(["ffprobe", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            self._ffprobe_available = True
        except Exception:
            self._ffprobe_available = False
        return self._ffprobe_available

    def _get_video_duration(self, path: Path) -> Optional[float]:
        if not self._has_ffprobe():
            return None
        # Primary: container-level duration (fast, works for almost everything).
        dur = self._probe_duration(path, "format=duration")
        if dur is not None:
            return dur
        # Fallback: some containers (certain MKV/TS files, VFR streams, or
        # files with a damaged header) don't report a container duration --
        # ask the video stream itself instead. This matters for clean
        # accuracy: shortest/longest strategies and fuzzy video matching
        # both depend on getting a real duration rather than silently
        # treating the file as duration-unknown.
        return self._probe_duration(path, "stream=duration", select_video_stream=True)

    def _probe_duration(self, path: Path, entries: str, select_video_stream: bool = False) -> Optional[float]:
        cmd = ["ffprobe", "-v", "error"]
        if select_video_stream:
            cmd += ["-select_streams", "v:0"]
        cmd += ["-show_entries", entries, "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
            out = result.stdout.strip()
            if result.returncode == 0 and out and out.upper() != "N/A":
                return float(out)
        except Exception:
            pass
        return None

    def _hash_file(self, path: Path, known_size: int, partial: bool = False,
                    compute_duration: bool = True) -> Optional[Tuple[str, Optional[float]]]:
        try:
            hasher = hashlib.sha256()
            chunk = READ_CHUNK_SIZE
            with open(path, "rb") as f:
                if partial and known_size > chunk * 2:
                    hasher.update(f.read(chunk))
                    f.seek(-chunk, os.SEEK_END)
                    hasher.update(f.read(chunk))
                else:
                    while data := f.read(chunk):
                        hasher.update(data)

            dur = None
            if compute_duration and path.suffix.lower() in VIDEO_EXTENSIONS:
                dur = self._get_video_duration(path)

            return hasher.hexdigest(), dur
        except (OSError, PermissionError):
            return None

    def analyze_files(self, file_tuples: List[Tuple[Path, Path, os.stat_result]]) -> List[FileInfo]:
        infos: List[FileInfo] = []
        total = len(file_tuples)
        cache_hits = 0

        print(f"[*] Analyzing {total} files across {len(self.target_dirs)} location(s) using {self.workers} workers...")
        start = time.time()

        # Stage 0: satisfy anything the persistent cache already has a
        # verified full hash for (same path + size + mtime as last time),
        # skipping disk I/O entirely for unchanged files.
        pending: List[Tuple[Path, Path, os.stat_result]] = []
        for fpath, base_dir, st in file_tuples:
            entry = self._cache_lookup(fpath, st.st_size, st.st_mtime)
            if entry:
                cache_hits += 1
                sha = entry["sha256"]
                infos.append(FileInfo(
                    path=fpath, size=st.st_size, mtime=st.st_mtime,
                    drive=self._get_drive_label(fpath, st.st_dev),
                    dev_ino=(st.st_dev, st.st_ino),
                    partial_hash=sha, sha256=sha,
                    duration=entry.get("duration"),
                    is_video=fpath.suffix.lower() in VIDEO_EXTENSIONS,
                ))
            else:
                pending.append((fpath, base_dir, st))

        if cache_hits:
            print(f"[*] {cache_hits}/{total} file(s) unchanged since the last cached scan -- reused stored hashes.")

        # Stage 0.5: size pre-filter. A file can only be a byte-for-byte
        # duplicate of something the exact same size, so any file whose
        # size doesn't match ANY other file in this scan -- cache hits
        # included -- mathematically cannot be part of a duplicate group.
        # There's no reason to read a single byte of it. Most media
        # libraries have mostly-distinct file sizes, so this alone
        # eliminates the bulk of the I/O a naive "hash everything" scan
        # would otherwise do.
        size_counts: Dict[int, int] = {}
        for _, _, st in file_tuples:
            size_counts[st.st_size] = size_counts.get(st.st_size, 0) + 1

        to_hash: List[Tuple[Path, Path, os.stat_result]] = []
        unique_non_video: List[Tuple[Path, Path, os.stat_result]] = []
        unique_video: List[Tuple[Path, Path, os.stat_result]] = []
        for fpath, base_dir, st in pending:
            if size_counts.get(st.st_size, 0) > 1:
                to_hash.append((fpath, base_dir, st))
            elif fpath.suffix.lower() in VIDEO_EXTENSIONS:
                unique_video.append((fpath, base_dir, st))
            else:
                unique_non_video.append((fpath, base_dir, st))

        skipped_unique = len(unique_non_video) + len(unique_video)
        if skipped_unique:
            print(f"[*] {skipped_unique} file(s) have a one-of-a-kind size -- skipped hashing (can't have an exact duplicate).")

        # Unique-size, non-video: nothing else needs them hashed OR probed
        # for duration -- just record them.
        for fpath, base_dir, st in unique_non_video:
            infos.append(FileInfo(
                path=fpath, size=st.st_size, mtime=st.st_mtime,
                drive=self._get_drive_label(fpath, st.st_dev),
                dev_ino=(st.st_dev, st.st_ino),
                is_video=False,
            ))

        # Unique-size videos still need a duration for fuzzy matching
        # (which compares by duration + perceptual frame hash, not exact
        # hash) even though they'll never need a content hash. Fetch
        # durations in parallel rather than one ffprobe call at a time.
        if unique_video:
            with ThreadPoolExecutor(max_workers=self.workers) as ex:
                durations = list(ex.map(self._get_video_duration, (fp for fp, _, _ in unique_video)))
            for (fpath, base_dir, st), dur in zip(unique_video, durations):
                infos.append(FileInfo(
                    path=fpath, size=st.st_size, mtime=st.st_mtime,
                    drive=self._get_drive_label(fpath, st.st_dev),
                    dev_ino=(st.st_dev, st.st_ino),
                    duration=dur, is_video=True,
                ))

        # Stage 1: cheap partial hash (first+last chunk) only for files
        # whose size actually collides with something else.
        hashed_infos: List[FileInfo] = []
        completed = 0
        total_to_hash = len(to_hash)
        if to_hash:
            with ThreadPoolExecutor(max_workers=self.workers) as ex:
                future_map = {ex.submit(self._hash_file, fp, st.st_size, True): (fp, bd, st) for fp, bd, st in to_hash}
                for future in as_completed(future_map):
                    fpath, base_dir, st = future_map[future]
                    result = future.result()
                    completed += 1
                    if completed % 100 == 0 or completed == total_to_hash:
                        print(f"    Progress: {completed}/{total_to_hash} files...", end="\r")

                    if result:
                        phash, dur = result
                        fi = FileInfo(
                            path=fpath, size=st.st_size, mtime=st.st_mtime,
                            drive=self._get_drive_label(fpath, st.st_dev),
                            dev_ino=(st.st_dev, st.st_ino),
                            partial_hash=phash, duration=dur,
                            is_video=fpath.suffix.lower() in VIDEO_EXTENSIONS,
                        )
                        infos.append(fi)
                        hashed_infos.append(fi)
            print()

        # Stage 2: full-hash verification, but only for files that actually
        # collide on (size, partial_hash) -- everything else is unique
        # enough already and doesn't need a full read.
        if not self.quick_mode:
            if hashed_infos:
                print(f"[*] Full-hash verification stage...")
                pre_groups: Dict[Tuple[int, str], List[FileInfo]] = {}
                for info in hashed_infos:
                    key = (info.size, info.partial_hash or "")
                    pre_groups.setdefault(key, []).append(info)

                collision_infos = []
                for group in pre_groups.values():
                    if len(group) > 1:
                        collision_infos.extend(group)

                if collision_infos:
                    with ThreadPoolExecutor(max_workers=self.workers) as ex:
                        # compute_duration=False: duration was already
                        # captured in stage 1, no need to re-invoke ffprobe.
                        future_map = {ex.submit(self._hash_file, i.path, i.size, False, False): i for i in collision_infos}
                        for future in as_completed(future_map):
                            info = future_map[future]
                            result = future.result()
                            info.sha256 = result[0] if result else info.partial_hash
                for info in hashed_infos:
                    if info.sha256 is None:
                        info.sha256 = info.partial_hash
        else:
            for info in hashed_infos:
                info.sha256 = info.partial_hash

        # Persist newly-confirmed FULL hashes to the cache. Quick-mode
        # results are only a partial hash and are never written back --
        # doing so would let a later full (non-quick) scan wrongly trust
        # an unverified hash as if it had been fully confirmed.
        if self.cache_file and not self.quick_mode and hashed_infos:
            for info in hashed_infos:
                if info.sha256:
                    self._cache_store(info.path, info.size, info.mtime, info.sha256, info.duration)
            self._save_cache()

        elapsed = time.time() - start
        if cache_hits or skipped_unique:
            print(f"[+] Analysis complete in {elapsed:.1f}s "
                  f"({cache_hits} from cache, {skipped_unique} skipped by size, {total_to_hash} hashed)")
        else:
            print(f"[+] Analysis complete in {elapsed:.1f}s")
        return infos

    def find_duplicates(self, infos: List[FileInfo]) -> List[DuplicateGroup]:
        hash_map: Dict[str, List[FileInfo]] = {}
        for info in infos:
            if info.sha256:
                hash_map.setdefault(info.sha256, []).append(info)

        groups = []
        for hkey, files in hash_map.items():
            if len(files) > 1:
                groups.append(DuplicateGroup(hash_key=hkey, files=files))

        groups.sort(key=lambda g: g.wasted_size, reverse=True)
        return groups

    # ---------------------------------------------------------------- #
    # Fuzzy video matching (same content, different size/bitrate/format)
    # ---------------------------------------------------------------- #

    def _has_ffmpeg(self) -> bool:
        if self._ffmpeg_available is not None:
            return self._ffmpeg_available
        try:
            subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            self._ffmpeg_available = True
        except Exception:
            self._ffmpeg_available = False
        return self._ffmpeg_available

    @staticmethod
    def _fingerprint_frame_count(duration: float, base_frames: int) -> int:
        """How many frames to sample for a video of this length. A fixed
        small frame count works fine for a 2-minute clip but leaves huge,
        easy-to-miss gaps across a 2-hour one -- if the true matching
        moment falls between two sparse samples, nothing will look close
        enough to register as a match. Scaling sample count with duration
        (roughly one sample every ~15s, capped) keeps that worst-case gap
        bounded regardless of how long the video is."""
        if duration <= 0:
            return base_frames
        target_gap = 15.0
        by_duration = int(duration / target_gap) + 1
        return max(base_frames, min(by_duration, 30))

    def _extract_frame_phashes(self, path: Path, num_frames: int = 8) -> Optional[List[Tuple[float, str]]]:
        """Sample frames evenly across the video and return (timestamp,
        phash) pairs in chronological order. Skips a small margin at the
        start/end to avoid intro logos / black frames throwing off the
        match. Frame count scales with duration -- see
        _fingerprint_frame_count."""
        duration = self._get_video_duration(path)
        if not duration or duration <= 1:
            return None
        try:
            import imagehash
            from PIL import Image
        except ImportError:
            return None

        n = self._fingerprint_frame_count(duration, num_frames)
        margin = min(duration * 0.03, 2.0)
        span = max(duration - 2 * margin, 0.1)
        if n <= 1:
            timestamps = [duration / 2]
        else:
            timestamps = [margin + span * i / (n - 1) for i in range(n)]

        frames: List[Tuple[float, str]] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            for i, ts in enumerate(timestamps):
                out_file = Path(tmpdir) / f"frame_{i}.jpg"
                try:
                    subprocess.run(
                        ["ffmpeg", "-ss", f"{ts:.2f}", "-i", str(path), "-frames:v", "1",
                         "-q:v", "3", "-y", str(out_file)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20
                    )
                    if out_file.exists() and out_file.stat().st_size > 0:
                        with Image.open(out_file) as img:
                            frames.append((ts, str(imagehash.phash(img))))
                except Exception:
                    continue
        return frames if frames else None

    @staticmethod
    def _compare_fingerprints(short_fp: List[Tuple[float, str]], long_fp: List[Tuple[float, str]],
                               phash_threshold: int) -> Optional[Tuple[float, float]]:
        """Compare two chronological (timestamp, phash) fingerprints,
        where `short_fp` belongs to the shorter-duration video of the pair
        and `long_fp` to the longer one.

        The old approach assumed frame k in one video lines up with frame
        k in the other -- true only if both videos start at the same
        moment and run at the same relative pace, which silently breaks
        the instant either one is trimmed from the start, the end, or
        anywhere in between. Instead, each query (short_fp) frame is
        matched against its single best match anywhere in the reference
        (long_fp) fingerprint. That alone would be too permissive on its
        own (generic-looking frames can spuriously "best match" almost
        anything), so a genuine trimmed-subsequence match is additionally
        required to have those best-match positions increase in roughly
        the same order as the query frames -- real trims shift content,
        they don't shuffle it. Matches whose best-hits land in scattered,
        non-chronological positions are rejected even if the raw hash
        distances looked fine in isolation.

        Returns (similarity_percent, estimated_offset_seconds) if judged a
        genuine match, else None. The offset is a rough estimate of how
        much later the matching content starts in the reference video
        (e.g. ~60s for a video with the first minute trimmed off)."""
        if not short_fp or not long_fp:
            return None

        best_dists: List[int] = []
        best_positions: List[int] = []
        offsets: List[float] = []
        for ts, h in short_fp:
            best_d, best_idx = 65, -1
            for idx, (ref_ts, ref_h) in enumerate(long_fp):
                d = hamming_distance(h, ref_h)
                if d < best_d:
                    best_d, best_idx = d, idx
            best_dists.append(best_d)
            if best_idx >= 0:
                best_positions.append(best_idx)
                offsets.append(long_fp[best_idx][0] - ts)

        if not best_dists:
            return None
        avg_dist = sum(best_dists) / len(best_dists)
        if avg_dist > phash_threshold:
            return None

        # Same footage, just clipped, should produce best-match positions
        # that climb in step with the query's own chronological order.
        # Allow some slack for individual frame noise.
        if len(best_positions) >= 2:
            ordered_pairs = sum(1 for a, b in zip(best_positions, best_positions[1:]) if b >= a)
            monotonic_fraction = ordered_pairs / (len(best_positions) - 1)
            if monotonic_fraction < 0.7:
                return None

        similarity = max(0.0, 100.0 * (1 - avg_dist / 64.0))
        est_offset = sum(offsets) / len(offsets) if offsets else 0.0
        return similarity, est_offset

    def find_fuzzy_video_duplicates(self, infos: List[FileInfo], duration_tolerance: float = 5.0,
                                     duration_tolerance_ratio: float = 0.5,
                                     num_frames: int = 8, phash_threshold: int = 10) -> List[FuzzyDuplicateGroup]:
        """Find videos that are probably the same content despite different
        bytes/size -- re-encoded, different bitrate/resolution/container,
        OR trimmed (a few seconds or several minutes cut from the start,
        end, or middle). Two-stage: duration bucketing narrows candidates,
        then alignment-robust perceptual frame hashing confirms. Results
        are probabilistic -- callers should treat them as review-only,
        never auto-delete.

        The duration gate allows a candidate pair's runtimes to differ by
        whichever is larger of `duration_tolerance` (a flat number of
        seconds -- keeps short clips from matching everything) or
        `duration_tolerance_ratio` of the shorter video's own duration
        (lets a long video tolerate a proportionally large trim, e.g. a
        couple of minutes off a feature-length file)."""
        videos = [i for i in infos if i.is_video and i.duration is not None and i.duration > 0]
        if len(videos) < 2:
            return []

        try:
            import imagehash  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError:
            print("[!] Fuzzy video matching needs 'Pillow' and 'imagehash'.")
            print("    Install with: pip install Pillow imagehash")
            return []

        if not self._has_ffmpeg():
            print("[!] Fuzzy video matching needs full ffmpeg (frame extraction), not just ffprobe.")
            return []

        videos.sort(key=lambda f: f.duration)
        n = len(videos)

        def _max_gap(shorter_duration: float) -> float:
            return max(duration_tolerance, duration_tolerance_ratio * shorter_duration)

        candidate_pairs: List[Tuple[int, int]] = []
        for i in range(n):
            allowed = _max_gap(videos[i].duration)
            for j in range(i + 1, n):
                # videos[] is duration-sorted and `allowed` only depends on
                # i, so once the gap exceeds it, every later j (an even
                # larger duration) only makes the gap bigger -- break holds.
                if videos[j].duration - videos[i].duration > allowed:
                    break
                candidate_pairs.append((i, j))

        if not candidate_pairs:
            return []

        needed_idx = sorted({idx for pair in candidate_pairs for idx in pair})
        print(f"[*] Fuzzy match: {len(needed_idx)} candidate video(s) within duration tolerance, "
              f"fingerprinting frames...")

        def _compute(idx: int) -> None:
            info = videos[idx]
            if info.video_phash is None:
                info.video_phash = self._extract_frame_phashes(info.path, num_frames)

        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futures = [ex.submit(_compute, idx) for idx in needed_idx]
            done = 0
            for _ in as_completed(futures):
                done += 1
                if done % 5 == 0 or done == len(futures):
                    print(f"    Fingerprinted: {done}/{len(futures)}...", end="\r")
        print()

        # Union-find to merge transitively-matched videos into groups
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        pair_similarity: Dict[Tuple[int, int], float] = {}
        for i, j in candidate_pairs:
            # videos is duration-sorted (i <= j), so i is always the
            # shorter/query side and j the longer/reference side.
            result = self._compare_fingerprints(videos[i].video_phash, videos[j].video_phash, phash_threshold)
            if result is None:
                continue
            similarity, _offset = result
            union(i, j)
            pair_similarity[(i, j)] = similarity

        clusters: Dict[int, List[int]] = {}
        for idx in range(n):
            if videos[idx].video_phash is None:
                continue
            clusters.setdefault(find(idx), []).append(idx)

        groups: List[FuzzyDuplicateGroup] = []
        for members in clusters.values():
            if len(members) < 2:
                continue
            files = [videos[m] for m in members]
            sims = [v for (a, b), v in pair_similarity.items() if a in members and b in members]
            avg_sim = sum(sims) / len(sims) if sims else 0.0
            groups.append(FuzzyDuplicateGroup(files=files, similarity=avg_sim))

        groups.sort(key=lambda g: g.wasted_size, reverse=True)
        return groups


# ---------------------------------------------------------------------------
# Output Formatters
# ---------------------------------------------------------------------------

class OutputFormatter:
    @staticmethod
    def table(groups: List[DuplicateGroup]) -> str:
        if not groups:
            return "[+] No duplicates found."
        lines = []
        lines.append(f"{'GROUP':<8} {'FILES':<8} {'DRIVES':<20} {'SIZE EACH':<14} {'WASTED':<14} {'SAMPLE PATH'}")
        lines.append("-" * 100)
        any_hardlinks = False
        for i, g in enumerate(groups, 1):
            sample = str(g.files[0].path)[:35]
            drives = ", ".join(sorted(g.drives_involved))[:18]
            mark = "*" if g.has_hardlinks else ""
            any_hardlinks = any_hardlinks or g.has_hardlinks
            lines.append(f"{i:<8} {len(g.files):<8} {drives:<20} {format_size(g.files[0].size):<14} {format_size(g.wasted_size):<14} {mark}{sample}")
        total_wasted = sum(g.wasted_size for g in groups)
        lines.append("-" * 100)
        lines.append(f"Total groups: {len(groups)} | Total wasted space: {format_size(total_wasted)}")
        if any_hardlinks:
            lines.append("* = group includes hardlinked copies (already share disk space; wasted size already accounts for this)")
        return "\n".join(lines)

    @staticmethod
    def detailed(groups: List[DuplicateGroup]) -> str:
        if not groups:
            return "[+] No duplicates found."
        lines = []
        for i, g in enumerate(groups, 1):
            drives_str = ", ".join(sorted(g.drives_involved))
            lines.append(f"\n--- [ Group {i} / {len(groups)} ] ---")
            lines.append(f"Hash: {g.hash_key[:16]}... | Size each: {format_size(g.files[0].size)} | Wasted: {format_size(g.wasted_size)}")
            if g.has_hardlinks:
                lines.append("NOTE: includes hardlinked copies -- they already share the same disk")
                lines.append("      space, so 'Wasted' above already excludes them from the total.")
            lines.append(f"Drives: {drives_str}")
            for j, f in enumerate(g.files, 1):
                dur = f" | {format_duration(f.duration)}" if f.duration else ""
                mod = datetime.fromtimestamp(f.mtime).strftime("%Y-%m-%d %H:%M")
                lines.append(f"  [{j}] [{f.drive}] {f.path.name}")
                lines.append(f"      Path: {f.path}")
                lines.append(f"      Modified: {mod}{dur}")
        total = sum(g.wasted_size for g in groups)
        lines.append(f"\n{'='*60}")
        lines.append(f"Total wasted space: {format_size(total)}")
        return "\n".join(lines)

    @staticmethod
    def json_out(groups: List[DuplicateGroup]) -> str:
        data = {
            "timestamp": datetime.now().isoformat(),
            "total_groups": len(groups),
            "total_wasted_bytes": sum(g.wasted_size for g in groups),
            "groups": [
                {
                    "hash": g.hash_key,
                    "wasted_bytes": g.wasted_size,
                    "has_hardlinks": g.has_hardlinks,
                    "drives": sorted(g.drives_involved),
                    "files": [f.to_dict() for f in g.files],
                }
                for g in groups
            ],
        }
        return json.dumps(data, indent=2)

    @staticmethod
    def fuzzy_detailed(groups: List[FuzzyDuplicateGroup]) -> str:
        if not groups:
            return "[+] No fuzzy (same-content, different-size) video duplicates found."
        lines = ["\n" + "#" * 60, "  PROBABLE VIDEO DUPLICATES (perceptual match, not exact)", "#" * 60]
        for i, g in enumerate(groups, 1):
            drives_str = ", ".join(sorted(g.drives_involved))
            lines.append(f"\n--- [ Fuzzy Group {i} / {len(groups)} ] ~{g.similarity:.0f}% match ---")
            lines.append(f"Drives: {drives_str} | Potential savings: {format_size(g.wasted_size)}")
            if g.duration_spread > 2:
                lines.append(f"Runtime differs by up to {format_duration(g.duration_spread)} across this group "
                              "-- looks like a trim, not just a re-encode.")
            for j, f in enumerate(g.files, 1):
                dur = f" | {format_duration(f.duration)}" if f.duration else ""
                mod = datetime.fromtimestamp(f.mtime).strftime("%Y-%m-%d %H:%M")
                lines.append(f"  [{j}] [{f.drive}] {f.path.name} ({format_size(f.size)}{dur})")
                lines.append(f"      Path: {f.path} | Modified: {mod}")
        total = sum(g.wasted_size for g in groups)
        lines.append(f"\n{'='*60}")
        lines.append("NOTE: These matched by duration + frame fingerprint, not byte-for-byte.")
        lines.append("Review before deleting -- 'clean' does not touch these groups.")
        lines.append(f"Potential additional savings: {format_size(total)}")
        return "\n".join(lines)

    @staticmethod
    def csv_out(groups: List[DuplicateGroup]) -> str:
        import io
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["group", "hash", "drive", "path", "size", "mtime", "duration",
                          "is_video", "inode", "group_has_hardlinks"])
        for i, g in enumerate(groups, 1):
            for f in g.files:
                inode_str = f"{f.dev_ino[0]}:{f.dev_ino[1]}" if f.dev_ino else ""
                writer.writerow([i, g.hash_key, f.drive, str(f.path), f.size, f.mtime, f.duration,
                                  f.is_video, inode_str, g.has_hardlinks])
        return out.getvalue()


# ---------------------------------------------------------------------------
# Cleanup Engine
# ---------------------------------------------------------------------------

def compute_freed_bytes(keepers: List[FileInfo], deleters: List[FileInfo]) -> int:
    """Bytes actually reclaimed by removing `deleters`, accounting for
    hardlinks: unlinking a path that shares its inode with a kept file
    frees nothing (the data survives via the kept path), and unlinking
    several paths that share one inode among themselves only frees that
    inode's space once, not once per path."""
    keeper_inodes = {k.dev_ino for k in keepers if k.dev_ino}
    freed_inodes: Set[Tuple[int, int]] = set()
    freed = 0
    for d in deleters:
        if d.dev_ino is None:
            freed += d.size
        elif d.dev_ino in keeper_inodes or d.dev_ino in freed_inodes:
            continue
        else:
            freed_inodes.add(d.dev_ino)
            freed += d.size
    return freed


def confirm_permanent_delete(count: int, total_bytes: int, assume_yes: bool = False) -> bool:
    """Gate for irreversible (non-trash) batch deletes. Returns True only
    if the user explicitly confirmed, or --yes/assume_yes was passed."""
    if count == 0:
        return True
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("[!] Permanent delete needs confirmation but input isn't interactive.")
        print("    Re-run with --yes to confirm non-interactively, or use --trash instead.")
        return False
    print(f"\n[!] About to PERMANENTLY delete {count} file(s), freeing ~{format_size(total_bytes)}.")
    print("    This bypasses the trash/recycle bin and cannot be undone by HashScout.")
    try:
        resp = input("    Type DELETE to confirm: ").strip()
    except (EOFError, KeyboardInterrupt):
        return False
    return resp == "DELETE"


class CleanupEngine:
    def __init__(self, strategy: KeepStrategy, trash: bool = True, dry_run: bool = True):
        self.strategy = strategy
        self.trash = trash
        self.dry_run = dry_run

    def pick_keepers(self, group: DuplicateGroup) -> Tuple[List[FileInfo], List[FileInfo]]:
        files = group.files[:]

        if self.strategy == KeepStrategy.SPREAD:
            # Keep one per drive, delete extras on same drive
            seen_drives: Set[str] = set()
            keepers = []
            deleters = []
            for f in files:
                if f.drive not in seen_drives:
                    keepers.append(f)
                    seen_drives.add(f.drive)
                else:
                    deleters.append(f)
            return keepers, deleters

        if self.strategy == KeepStrategy.NEWEST:
            files.sort(key=lambda f: f.mtime, reverse=True)
        elif self.strategy == KeepStrategy.OLDEST:
            files.sort(key=lambda f: f.mtime)
        elif self.strategy == KeepStrategy.LARGEST:
            files.sort(key=lambda f: f.size, reverse=True)
        elif self.strategy == KeepStrategy.SMALLEST:
            files.sort(key=lambda f: f.size)
        elif self.strategy == KeepStrategy.SHORTEST:
            files.sort(key=lambda f: f.duration if f.duration is not None else float('inf'))
        elif self.strategy == KeepStrategy.LONGEST:
            files.sort(key=lambda f: f.duration if f.duration is not None else -1, reverse=True)
        elif self.strategy == KeepStrategy.FIRST:
            pass
        else:
            return [], files

        return [files[0]], files[1:]

    @staticmethod
    def verify_unchanged(f: FileInfo) -> Optional[str]:
        """Safety check run immediately before deletion: make sure the file
        on disk still matches what the scan recorded. Scans and cleanup can
        be minutes, hours, or (in scripted use) days apart -- if the file
        was edited, replaced, or already removed since the scan, acting on
        stale data could destroy something that's no longer a duplicate.
        Returns a message to report (and skip deletion) if verification
        fails, or None if it's safe to proceed."""
        try:
            st = f.path.stat()
        except OSError:
            return f"[SKIPPED] [{f.drive}] {f.path}: file no longer exists"
        if st.st_size != f.size or st.st_mtime != f.mtime:
            return (f"[SKIPPED] [{f.drive}] {f.path}: changed since scan "
                     "(size/modified time differ) -- re-scan to confirm it's still a duplicate")
        return None

    def execute(self, deleters: List[FileInfo]) -> List[str]:
        results = []
        for f in deleters:
            if self.dry_run:
                results.append(f"[DRY-RUN] Would delete: [{f.drive}] {f.path}")
                continue

            skip_msg = self.verify_unchanged(f)
            if skip_msg:
                results.append(skip_msg)
                continue

            if self.trash:
                ok, msg = safe_trash(f.path)
            else:
                try:
                    f.path.unlink()
                    ok, msg = True, f"[DELETED] [{f.drive}] {f.path}"
                except Exception as e:
                    ok, msg = False, f"[ERROR] [{f.drive}] {f.path}: {e}"
            results.append(msg)
        return results


# ---------------------------------------------------------------------------
# Interactive Shell (cmd.Cmd)
# ---------------------------------------------------------------------------

class HashScoutShell(cmd.Cmd):
    intro = BANNER
    prompt = PROMPT

    def __init__(self):
        super().__init__()
        self.current_dirs: List[Path] = [Path(".").resolve()]
        self.last_groups: List[DuplicateGroup] = []
        self.last_infos: List[FileInfo] = []
        self.last_fuzzy_groups: List[FuzzyDuplicateGroup] = []
        self.default_workers = DEFAULT_WORKERS
        self.default_trash = True
        self.default_exclude: List[str] = []
        self.default_include_empty = False
        self.default_skip_system = False
        self.default_cache_file: Optional[str] = None

    # Flags accepted by `scan` that consume the NEXT token as a value
    # (e.g. "--fuzzy-frames 10") -- these values must not be mistaken for
    # paths, which is exactly what happened before this fix: "10" isn't a
    # directory, so it was reported as an invalid path instead of being
    # recognized as --fuzzy-frames' argument.
    _SCAN_VALUE_FLAGS = {
        "--workers", "-w", "--min-size", "-min", "--max-size", "-max",
        "--fuzzy-tolerance", "--fuzzy-tolerance-ratio", "--fuzzy-frames",
        "--fuzzy-threshold", "--cache",
    }

    def _parse_paths(self, args: List[str]) -> List[Path]:
        paths = []
        skip_next = False
        for a in args:
            if skip_next:
                skip_next = False
                continue
            if a in self._SCAN_VALUE_FLAGS:
                skip_next = True
                continue
            if a.startswith("-"):
                continue
            p = Path(a).expanduser().resolve()
            if p.exists() and p.is_dir():
                paths.append(p)
            else:
                print(f"[!] Skipping invalid path: {a}")
        return paths if paths else self.current_dirs

    def _make_core(self, targets: List[Path], video_only: bool = False,
                   min_size: int = 0, max_size: int = 0,
                   quick: bool = False, workers: int = DEFAULT_WORKERS,
                   include_empty: Optional[bool] = None,
                   skip_system: Optional[bool] = None,
                   cache_file: Optional[str] = None) -> HashScoutCore:
        return HashScoutCore(
            target_dirs=targets,
            video_only=video_only,
            min_size=min_size,
            max_size=max_size,
            exclude_patterns=self.default_exclude,
            workers=workers,
            quick_mode=quick,
            include_empty=self.default_include_empty if include_empty is None else include_empty,
            skip_system=self.default_skip_system if skip_system is None else skip_system,
            cache_file=self.default_cache_file if cache_file is None else cache_file,
        )

    def _show_groups(self, groups: List[DuplicateGroup]):
        if not groups:
            print("[+] No duplicates found.")
            return
        print(OutputFormatter.detailed(groups))
        total = sum(g.wasted_size for g in groups)
        print(f"\n[+] Found {len(groups)} duplicate groups. Wasted: {format_size(total)}")

    # -------------------------------------------------------------- #
    # Commands
    # -------------------------------------------------------------- #

    def do_scan(self, arg):
        r"""
        scan <path1> [path2] [path3] [--video] [--quick] [--fuzzy] [--min-size 10MB] [--max-size 1GB] [--workers 4]
        Scan one or multiple directories/drives for duplicates.

        --fuzzy                 Also find videos that are the SAME content
                                 but a DIFFERENT size (re-encoded, different
                                 bitrate/resolution/container) OR TRIMMED
                                 (seconds or minutes cut from the start,
                                 end, or middle). Matches by duration +
                                 alignment-aware frame hashing, so a 1080p
                                 original and a 720p copy missing its first
                                 minute still match. Needs ffmpeg + Pillow +
                                 imagehash.
        --fuzzy-tolerance N      Flat duration-difference allowance in
                                 seconds (default 5)
        --fuzzy-tolerance-ratio R  ALSO allow a duration gap up to R times
                                 the shorter video's own length (default
                                 0.5 = 50%%), so a big trim on a long
                                 video is still considered a candidate.
                                 Whichever of the two allowances is larger
                                 wins.
        --fuzzy-frames N         Baseline frames sampled per video; longer
                                 videos sample more automatically (default 8)
        --fuzzy-threshold N      Max avg hash distance to count as a match,
                                 0-64, lower = stricter (default 10)
        --include-empty          Also consider 0-byte files (skipped by default)
        --skip-system            Ignore common junk/system dirs (.git,
                                 node_modules, System Volume Information, etc.)
        --cache <path>           Persist verified hashes to <path> so a
                                 re-scan of unchanged files skips re-hashing
        Examples:
          scan C:\Users\Admin\Pictures
          scan C:\ D:\ E:\ --video
          scan ~/Downloads ~/Videos ~/Backups --workers 8
          scan ~/Videos --video --fuzzy
          scan ~/Videos --video --fuzzy --fuzzy-tolerance-ratio 0.4
          scan ~/Media --skip-system --cache ~/.hashscout_cache.json
        """
        if not arg.strip():
            print("[-] Usage: scan <path> [more paths...] [options]")
            print("    scan C:\\Users\\Admin\\Pictures D:\\Backups --video")
            return

        try:
            parts = shlex.split(arg, posix=(os.name != "nt"))
        except ValueError as e:
            print(f"[!] Error parsing command: {e}")
            return
        targets = self._parse_paths(parts)
        if not targets:
            print("[!] No valid directories provided.")
            return

        video_only = "--video" in parts or "-v" in parts
        quick = "--quick" in parts or "-q" in parts
        fuzzy = "--fuzzy" in parts
        include_empty = "--include-empty" in parts or self.default_include_empty
        skip_system = "--skip-system" in parts or self.default_skip_system
        workers = self.default_workers
        min_sz = 0
        max_sz = 0
        fuzzy_tolerance = 5.0
        fuzzy_tolerance_ratio = 0.5
        fuzzy_frames = 8
        fuzzy_threshold = 10
        cache_file = self.default_cache_file

        for i, p in enumerate(parts):
            if p in ("--workers", "-w") and i + 1 < len(parts):
                workers = int(parts[i + 1])
            if p in ("--min-size", "-min") and i + 1 < len(parts):
                min_sz = parse_size(parts[i + 1])
            if p in ("--max-size", "-max") and i + 1 < len(parts):
                max_sz = parse_size(parts[i + 1])
            if p == "--fuzzy-tolerance" and i + 1 < len(parts):
                fuzzy_tolerance = float(parts[i + 1])
            if p == "--fuzzy-tolerance-ratio" and i + 1 < len(parts):
                fuzzy_tolerance_ratio = float(parts[i + 1])
            if p == "--fuzzy-frames" and i + 1 < len(parts):
                fuzzy_frames = int(parts[i + 1])
            if p == "--fuzzy-threshold" and i + 1 < len(parts):
                fuzzy_threshold = int(parts[i + 1])
            if p == "--cache" and i + 1 < len(parts):
                cache_file = parts[i + 1]

        core = self._make_core(targets, video_only, min_sz, max_sz, quick, workers,
                                include_empty=include_empty, skip_system=skip_system,
                                cache_file=cache_file)
        file_tuples = core.discover()
        if not file_tuples:
            print("[!] No files matched.")
            return

        infos = core.analyze_files(file_tuples)
        self.last_infos = infos
        self.last_groups = core.find_duplicates(infos)
        self._show_groups(self.last_groups)

        if fuzzy:
            self._run_fuzzy(core, fuzzy_tolerance, fuzzy_tolerance_ratio, fuzzy_frames, fuzzy_threshold)

    def _run_fuzzy(self, core: HashScoutCore, tolerance: float, tolerance_ratio: float, frames: int, threshold: int):
        """Run fuzzy video matching over the last scan's files, skipping
        anything already caught by exact-hash matching."""
        exact_paths = {f.path for g in self.last_groups for f in g.files}
        candidates = [i for i in self.last_infos if i.is_video and i.path not in exact_paths]
        self.last_fuzzy_groups = core.find_fuzzy_video_duplicates(
            candidates, duration_tolerance=tolerance, duration_tolerance_ratio=tolerance_ratio,
            num_frames=frames, phash_threshold=threshold
        )
        print(OutputFormatter.fuzzy_detailed(self.last_fuzzy_groups))

    def do_fuzzy(self, arg):
        """
        fuzzy [--tolerance 5] [--ratio 0.5] [--frames 8] [--threshold 10]
        Find videos that are the SAME content but a DIFFERENT file size --
        re-encoded/different resolution, OR trimmed (seconds to minutes
        cut off the start/end/middle) -- among the files from the last
        'scan' (run 'scan' first). Matches by duration + alignment-aware
        perceptual frame hashing -- these are PROBABLE matches, not
        byte-identical, so review before deleting. 'clean' never touches
        fuzzy groups.
        """
        if not self.last_infos:
            print("[!] No scan results. Run 'scan <path>' first.")
            return

        try:
            parts = shlex.split(arg, posix=(os.name != "nt"))
        except ValueError as e:
            print(f"[!] Error parsing command: {e}")
            return 
        tolerance, tolerance_ratio, frames, threshold = 5.0, 0.5, 8, 10
        for i, p in enumerate(parts):
            if p == "--tolerance" and i + 1 < len(parts):
                tolerance = float(parts[i + 1])
            if p == "--ratio" and i + 1 < len(parts):
                tolerance_ratio = float(parts[i + 1])
            if p == "--frames" and i + 1 < len(parts):
                frames = int(parts[i + 1])
            if p == "--threshold" and i + 1 < len(parts):
                threshold = int(parts[i + 1])

        core = self._make_core(self.current_dirs, workers=self.default_workers)
        core.exclude = self.default_exclude  # inherit current shell excludes
        self._run_fuzzy(core, tolerance, tolerance_ratio, frames, threshold)

    def do_clean(self, arg):
        """
        clean [--strategy newest|oldest|largest|smallest|shortest|longest|first|spread]
              [--trash] [--no-trash] [--apply] [--interactive] [--yes]
        Clean duplicates from the last scan.
        Without --apply, runs in dry-run mode. Permanent (--no-trash)
        batch deletes ask for confirmation unless --yes is given.
        """
        if not self.last_groups:
            print("[!] No scan results. Run 'scan <path>' first.")
            return

        try:
            parts = shlex.split(arg, posix=(os.name != "nt"))
        except ValueError as e:
            print(f"[!] Error parsing command: {e}")
            return 
        strategy = KeepStrategy.MANUAL
        trash = self.default_trash
        dry_run = "--apply" not in parts
        force_interactive = "--interactive" in parts or "-i" in parts
        assume_yes = "--yes" in parts or "-y" in parts

        for i, p in enumerate(parts):
            if p == "--strategy" and i + 1 < len(parts):
                try:
                    strategy = KeepStrategy(parts[i + 1])
                except ValueError:
                    print(f"[!] Unknown strategy: {parts[i + 1]}")
                    return
            if p == "--no-trash":
                trash = False
            if p == "--trash":
                trash = True

        if force_interactive or strategy == KeepStrategy.MANUAL:
            self._interactive_clean(trash, dry_run)
        else:
            self._auto_clean(strategy, trash, dry_run, assume_yes)

    def _auto_clean(self, strategy: KeepStrategy, trash: bool, dry_run: bool, assume_yes: bool = False):
        engine = CleanupEngine(strategy, trash=trash, dry_run=dry_run)
        print(f"[*] Strategy: {strategy.value} | Trash: {trash} | Dry-run: {dry_run}")

        # Preview pass (non-destructive) so we can ask for one confirmation
        # up front on permanent deletes, instead of deleting group-by-group
        # and only finding out how much was destroyed at the very end.
        plan = [(g, *engine.pick_keepers(g)) for g in self.last_groups]
        plan = [(g, k, d) for g, k, d in plan if k]
        total_to_delete = sum(len(d) for _, _, d in plan)
        total_freed = sum(compute_freed_bytes(k, d) for _, k, d in plan)

        if not dry_run and not trash and total_to_delete:
            if not confirm_permanent_delete(total_to_delete, total_freed, assume_yes):
                print("[!] Aborted -- no files were deleted.")
                return

        for group, keepers, deleters in plan:
            print(f"\n[+] Keeping {len(keepers)} file(s):")
            for k in keepers:
                print(f"    [{k.drive}] {k.path.name}")
            results = engine.execute(deleters)
            for r in results:
                print(f"    {r}")
        print(f"\n[+] Done. Space {'freed' if not dry_run else 'to free'}: {format_size(total_freed)}")

    def _interactive_clean(self, trash: bool, dry_run: bool):
        if not self.last_groups:
            print("[+] Nothing to clean.")
            return

        if dry_run:
            print("\n [DRY-RUN] No files deleted yet. Use --apply to execute.\n")
        elif not trash:
            print("\n [!] Permanent delete mode -- removed files will NOT go to the trash.\n")

        total_freed = 0
        for idx, group in enumerate(self.last_groups, 1):
            drives_str = ", ".join(sorted(group.drives_involved))
            hardlink_note = "  (some copies are hardlinks -- see 'export' for detail)" if group.has_hardlinks else ""
            print(f"\n--- Group {idx}/{len(self.last_groups)} | Wasted: {format_size(group.wasted_size)} | Drives: {drives_str}{hardlink_note} ---")
            for i, f in enumerate(group.files, 1):
                dur = f" | {format_duration(f.duration)}" if f.duration else ""
                mod = datetime.fromtimestamp(f.mtime).strftime("%Y-%m-%d %H:%M")
                print(f"  [{i}] [{f.drive}] {f.path.name} ({mod}{dur})")

            if dry_run:
                continue

            print("\nSelect: number=KEEP that file (delete rest) | s=skip | q=quit")
            while True:
                try:
                    choice = input("Keep > ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print("\n[!] Aborted.")
                    print(f"[+] Space freed before abort: {format_size(total_freed)}")
                    return
                if choice in ("s", "skip", ""):
                    print("[*] Skipped.")
                    break
                elif choice == "q":
                    print("[!] Exiting cleanup.")
                    print(f"[+] Space freed: {format_size(total_freed)}")
                    return
                elif choice.isdigit():
                    keep_idx = int(choice) - 1
                    if 0 <= keep_idx < len(group.files):
                        keepers = [group.files[keep_idx]]
                        deleters = [f for i, f in enumerate(group.files) if i != keep_idx]
                        print(f"[+] Keeping: [{keepers[0].drive}] {keepers[0].path.name}")
                        for d in deleters:
                            skip_msg = CleanupEngine.verify_unchanged(d)
                            if skip_msg:
                                print(f"    {skip_msg}")
                                continue
                            if trash:
                                ok, msg = safe_trash(d.path)
                            else:
                                try:
                                    d.path.unlink()
                                    msg = f"[DELETED] [{d.drive}] {d.path}"
                                except Exception as e:
                                    msg = f"[ERROR] [{d.drive}] {d.path}: {e}"
                            print(f"    {msg}")
                        total_freed += compute_freed_bytes(keepers, deleters)
                        break
                    else:
                        print("[!] Invalid index.")
                else:
                    print("[!] Invalid input.")
        print(f"\n[+] Done. Space freed: {format_size(total_freed)}")

    def do_export(self, arg):
        """
        export <filename.json|csv>
        Export the last scan results to a file.
        """
        if not self.last_groups:
            print("[!] No scan results. Run 'scan <path>' first.")
            return
        if not arg.strip():
            print("[-] Usage: export <filename.json|csv>")
            return

        out_path = Path(arg.strip())
        fmt = OutputFormatter()
        if out_path.suffix.lower() == ".json":
            out_path.write_text(fmt.json_out(self.last_groups), encoding="utf-8")
        elif out_path.suffix.lower() == ".csv":
            out_path.write_text(fmt.csv_out(self.last_groups), encoding="utf-8")
        else:
            out_path.write_text(fmt.detailed(self.last_groups), encoding="utf-8")
        print(f"[+] Report saved to: {out_path.resolve()}")

    def do_cd(self, arg):
        """
        cd <path>
        Change the default scan directory.
        """
        if not arg.strip():
            print(f"[*] Current: {', '.join(str(d) for d in self.current_dirs)}")
            return
        p = Path(arg.strip()).expanduser().resolve()
        if p.exists() and p.is_dir():
            self.current_dirs = [p]
            print(f"[+] Changed to: {p}")
        else:
            print(f"[!] Not a directory: {p}")

    def do_pwd(self, arg):
        """Show current default directories."""
        for d in self.current_dirs:
            print(f"[*] {d}")

    def do_exclude(self, arg):
        """
        exclude <pattern> | exclude --list | exclude --clear
        Add/list/clear ignore patterns.
        """
        if not arg.strip():
            print("[-] Usage: exclude <pattern> | exclude --list | exclude --clear")
            return
        if arg.strip() == "--list":
            if self.default_exclude:
                print("[*] Current exclude patterns:")
                for p in self.default_exclude:
                    print(f"    - {p}")
            else:
                print("[*] No exclude patterns set.")
            return
        if arg.strip() == "--clear":
            self.default_exclude = []
            print("[+] Exclude patterns cleared.")
            return
        pattern = arg.strip().strip('"').strip("'")
        self.default_exclude.append(pattern)
        print(f"[+] Added exclude: {pattern}")

    def do_workers(self, arg):
        """
        workers <number>
        Set default number of hashing threads.
        """
        if arg.strip().isdigit():
            self.default_workers = int(arg.strip())
            print(f"[+] Workers set to: {self.default_workers}")
        else:
            print(f"[*] Current workers: {self.default_workers}")

    def do_cache(self, arg):
        """
        cache [<path> | --off]
        Show, set, or disable the persistent hash-cache file. When set,
        HashScout remembers verified full-file hashes (keyed by
        path+size+modified-time) so a re-scan of files that haven't
        changed skips re-hashing them entirely. Off by default.
        """
        arg = arg.strip()
        if not arg:
            print(f"[*] Cache file: {self.default_cache_file or '(disabled)'}")
            return
        if arg == "--off":
            self.default_cache_file = None
            print("[+] Hash cache disabled.")
            return
        self.default_cache_file = arg
        print(f"[+] Hash cache set to: {arg}")

    def do_clear(self, arg):
        """Clear the terminal screen."""
        os.system('cls' if os.name == 'nt' else 'clear')

    def do_exit(self, arg):
        """Exit HashScout."""
        print("Exiting HashScout. Stay organized.")
        return True

    def do_quit(self, arg):
        """Exit HashScout."""
        return self.do_exit(arg)

    def do_help(self, arg):
        """Show help."""
        print(r"""
╔════════════════════════════════════════════════════════════════════════════╗
║                          HASH SCOUT  v2.3                                  ║
║                    Intelligent Duplicate File Finder                       ║
╚════════════════════════════════════════════════════════════════════════════╝

╔════════════════════════════════════════════════════════════════════════════╗
║                         AVAILABLE COMMANDS                                 ║
╚════════════════════════════════════════════════════════════════════════════╝

╭────────────────────────────────────────────────────────────────────────────╮
│  scan <path> [path2] ... [opts]                                            │
│      Scan one or multiple directories/drives for duplicates.               │
│                                                                            │
│      OPTIONS:                                                              │
│      --video, -v          Only video files                                 │
│      --quick, -q          Skip full hash (faster)                          │
│      --workers N          Thread count                                     │
│      --min-size 10MB      Minimum file size                                │
│      --max-size 1GB       Maximum file size                                │
│      --include-empty      Also consider 0-byte files (skipped by default)  │
│      --skip-system        Ignore .git, node_modules, System Volume         │
│                           Information, and other common junk dirs          │
│      --cache <path>       Reuse/save verified hashes across scans          │
│      --fuzzy              Also find same-content videos of DIFFERENT size  │
│                           OR TRIMMED length (seconds to minutes cut from   │
│                           start/end/middle) via duration + alignment-      │
│                           aware frame hashing -- resolution changes        │
│                           (1080p vs 720p) match fine too                   │
│                                                                            │
│      EXAMPLES:                                                             │
│        scan C:\Users\Admin\Pictures                                        │
│        scan C:\ D:\ E:\ --video                                            │
│        scan ~/Downloads ~/Videos ~/Backups --workers 8                     │
│        scan ~/Videos --video --fuzzy                                       │
│        scan ~/Videos --video --fuzzy --fuzzy-tolerance-ratio 0.4           │
│        scan ~/Media --skip-system --cache ~/.hashscout_cache.json          │
╰────────────────────────────────────────────────────────────────────────────╯

╭────────────────────────────────────────────────────────────────────────────╮
│  fuzzy [opts]                                                              │
│      Find same-content/different-size/trimmed video duplicates             │
│      from the last scan                                                    │
│                                                                            │
│      OPTIONS:                                                              │
│      --tolerance N          Flat duration-diff allowance, seconds (def 5)  │
│      --ratio R              ALSO allow gap up to R × shorter video length  │
│                             for big trims (default 0.5)                    │
│      --frames N             Baseline frames/video; scales up for longer    │
│                             videos automatically (default 8)               │
│      --threshold N          Match strictness, 0-64 (default 10)            │
╰────────────────────────────────────────────────────────────────────────────╯

╭────────────────────────────────────────────────────────────────────────────╮
│  clean [opts]                                                              │
│      Clean duplicates from last scan                                       │
│                                                                            │
│      OPTIONS:                                                              │
│      --strategy <name>    newest | oldest | largest | smallest |           │
│                           shortest | longest | first | spread | manual     │
│                           (default)                                        │
│      --trash              Move to trash (default)                          │
│      --no-trash           Permanent delete (asks to confirm; --yes skips)  │
│      --apply              Actually delete (default is dry-run)             │
│      --interactive, -i    Force interactive picker                         │
│      --yes, -y            Skip the permanent-delete confirmation prompt    │
│                                                                            │
│      NOTES:                                                                │
│      spread strategy:     Keeps one copy per drive, deletes extras         │
│      Hardlinked copies never count toward "space freed" — they already     │
│      share the same bytes on disk, so removing one frees nothing.          │
╰────────────────────────────────────────────────────────────────────────────╯

╭────────────────────────────────────────────────────────────────────────────╮
│  export <file>            Export last scan (.json, .csv, .txt)             │
│  cd <path>                Change default directory                         │
│  pwd                      Show current directory                           │
│  exclude <pattern>        Add ignore glob pattern                          │
│  exclude --list           List ignore patterns                             │
│  exclude --clear          Clear ignore patterns                            │
│  cache <path>             Set persistent hash-cache file (speeds re-scans) │
│  cache --off              Disable the hash cache                           │
│  workers <N>              Set default thread count                         │
│  clear                    Clear screen                                     │
│  exit / quit              Leave HashScout                                  │
╰────────────────────────────────────────────────────────────────────────────╯

╔════════════════════════════════════════════════════════════════════════════╗
║                         QUICK TIPS                                         ║
╠════════════════════════════════════════════════════════════════════════════╣
║  * Use --quick for initial scans, then follow up with full hashing         ║
║  * Enable --cache to dramatically speed up repeated scans                  ║
║  * Try --fuzzy for finding near-duplicate videos (trimmed, re-encoded)     ║
║  * Always run clean as dry-run first (default) to review before deleting   ║
║  * Use spread strategy to keep at least one copy per physical drive        ║
╚════════════════════════════════════════════════════════════════════════════╝
        """)


# ---------------------------------------------------------------------------
# CLI Parser (for direct command mode)
# ---------------------------------------------------------------------------

def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hashscout",
        description="HashScout v2.2 - Advanced Duplicate File & Video Finder (Multi-Drive)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=r"""
Examples:
  hashscout                          # Launch interactive shell
  hashscout scan C:\\Users\\Admin\\Pictures D:\\Backups --video-only
  hashscout scan ~/Downloads ~/Videos ~/Backups --workers 8
  hashscout clean --strategy spread --trash --apply
  hashscout export ~/Desktop/report.json
        """,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Hashing threads (default: {DEFAULT_WORKERS}, based on this machine's CPU count)")
    parser.add_argument("--video-only", action="store_true", help="Scan only video files")
    parser.add_argument("--min-size", type=str, default="", help="Minimum file size (e.g., 10MB, 1GB)")
    parser.add_argument("--max-size", type=str, default="", help="Maximum file size")
    parser.add_argument("--exclude", action="append", help="Glob ignore pattern (repeatable)")
    parser.add_argument("--quick", action="store_true", help="Skip full hash (faster)")
    parser.add_argument("--include-empty", action="store_true", help="Also consider 0-byte files (excluded by default)")
    parser.add_argument("--skip-system", action="store_true",
                        help="Ignore common junk/system dirs (.git, node_modules, System Volume Information, etc.)")
    parser.add_argument("--cache-file", type=str, default=None,
                        help="Persist verified hashes here to speed up repeat scans")
    parser.add_argument("--fuzzy", action="store_true",
                        help="Also find same-content videos of different size or trimmed length")
    parser.add_argument("--fuzzy-tolerance", type=float, default=5.0,
                        help="Flat duration-difference allowance in seconds (default: 5)")
    parser.add_argument("--fuzzy-tolerance-ratio", type=float, default=0.5,
                        help="ALSO allow a duration gap up to this fraction of the shorter video's "
                             "own length, so long videos tolerate bigger trims (default: 0.5)")
    parser.add_argument("--fuzzy-frames", type=int, default=8,
                        help="Baseline frames sampled per video; longer videos sample more automatically (default: 8)")
    parser.add_argument("--fuzzy-threshold", type=int, default=10, help="Match strictness, 0-64 (default: 10)")
    parser.add_argument("--format", choices=["table", "detailed", "json", "csv"], default="detailed",
                        help="Output format (default: detailed)")

    sub = parser.add_subparsers(dest="command", help="Commands")

    scan_p = sub.add_parser("scan", help="Scan and report duplicates")
    scan_p.add_argument("paths", nargs="*", default=["."], help="Target directories (default: current)")

    clean_p = sub.add_parser("clean", help="Auto or interactive cleanup")
    clean_p.add_argument("paths", nargs="*", default=["."], help="Target directories (default: current)")
    clean_p.add_argument("--strategy", choices=[s.value for s in KeepStrategy], default="manual")
    clean_p.add_argument("--interactive", action="store_true", help="Force interactive mode")
    clean_p.add_argument("--trash", action="store_true", default=True, help="Move to trash")
    clean_p.add_argument("--no-trash", action="store_true", help="Permanent delete")
    clean_p.add_argument("--apply", action="store_true", help="Actually delete")
    clean_p.add_argument("--yes", "-y", action="store_true",
                         help="Skip the confirmation prompt for permanent (non-trash) deletes")

    exp_p = sub.add_parser("export", help="Export scan report to file")
    exp_p.add_argument("paths", nargs="*", default=["."], help="Target directories (default: current)")
    exp_p.add_argument("--output", "-o", required=True, help="Output file path (.json or .csv)")

    return parser


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

def run_cli():
    parser = create_parser()
    args = parser.parse_args()

    # No command = launch interactive shell
    if not args.command:
        try:
            HashScoutShell().cmdloop()
        except KeyboardInterrupt:
            print("\nExiting HashScout.")
            sys.exit(0)
        return

    # Direct command mode
    targets = [Path(p).expanduser().resolve() for p in args.paths]
    targets = [t for t in targets if t.exists() and t.is_dir()]
    if not targets:
        print("[!] No valid directories provided.")
        sys.exit(1)

    min_sz = parse_size(args.min_size) if args.min_size else 0
    max_sz = parse_size(args.max_size) if args.max_size else 0

    core = HashScoutCore(
        target_dirs=targets,
        video_only=args.video_only,
        min_size=min_sz,
        max_size=max_sz,
        exclude_patterns=args.exclude,
        workers=args.workers,
        quick_mode=args.quick,
        include_empty=args.include_empty,
        skip_system=args.skip_system,
        cache_file=args.cache_file,
    )

    fmt = OutputFormatter()

    if args.command == "scan":
        file_tuples = core.discover()
        if not file_tuples:
            print("[!] No files matched.")
            sys.exit(0)
        infos = core.analyze_files(file_tuples)
        groups = core.find_duplicates(infos)

        if args.format == "table":
            print(fmt.table(groups))
        elif args.format == "detailed":
            print(fmt.detailed(groups))
        elif args.format == "json":
            print(fmt.json_out(groups))
        elif args.format == "csv":
            print(fmt.csv_out(groups))

        fuzzy_groups: List[FuzzyDuplicateGroup] = []
        if args.fuzzy:
            exact_paths = {f.path for g in groups for f in g.files}
            candidates = [i for i in infos if i.is_video and i.path not in exact_paths]
            fuzzy_groups = core.find_fuzzy_video_duplicates(
                candidates, duration_tolerance=args.fuzzy_tolerance,
                duration_tolerance_ratio=args.fuzzy_tolerance_ratio,
                num_frames=args.fuzzy_frames, phash_threshold=args.fuzzy_threshold,
            )
            print(fmt.fuzzy_detailed(fuzzy_groups))

        sys.exit(1 if (groups or fuzzy_groups) else 0)

    elif args.command == "export":
        file_tuples = core.discover()
        if not file_tuples:
            print("[!] No files matched.")
            sys.exit(0)
        infos = core.analyze_files(file_tuples)
        groups = core.find_duplicates(infos)
        out_path = Path(args.output)
        if out_path.suffix.lower() == ".json":
            out_path.write_text(fmt.json_out(groups), encoding="utf-8")
        elif out_path.suffix.lower() == ".csv":
            out_path.write_text(fmt.csv_out(groups), encoding="utf-8")
        else:
            out_path.write_text(fmt.detailed(groups), encoding="utf-8")
        print(f"[+] Report saved to: {out_path}")
        sys.exit(0)

    elif args.command == "clean":
        file_tuples = core.discover()
        if not file_tuples:
            print("[!] No files matched.")
            sys.exit(0)
        infos = core.analyze_files(file_tuples)
        groups = core.find_duplicates(infos)

        if not groups:
            print("[+] No duplicates found.")
            sys.exit(0)

        strategy = KeepStrategy(args.strategy)
        dry_run = not args.apply
        trash = not args.no_trash

        if args.interactive or strategy == KeepStrategy.MANUAL:
            shell = HashScoutShell()
            shell.last_groups = groups
            shell._interactive_clean(trash, dry_run)
        else:
            engine = CleanupEngine(strategy, trash=trash, dry_run=dry_run)
            print(f"[*] Strategy: {strategy.value} | Trash: {trash} | Dry-run: {dry_run}")

            # Non-destructive preview pass so we can ask for a single
            # confirmation before any permanent deletion happens.
            plan = [(g, *engine.pick_keepers(g)) for g in groups]
            plan = [(g, k, d) for g, k, d in plan if k]
            total_to_delete = sum(len(d) for _, _, d in plan)
            total_freed = sum(compute_freed_bytes(k, d) for _, k, d in plan)

            if not dry_run and not trash and total_to_delete:
                if not confirm_permanent_delete(total_to_delete, total_freed, assume_yes=args.yes):
                    print("[!] Aborted -- no files were deleted.")
                    sys.exit(1)

            for group, keepers, deleters in plan:
                print(f"\n[+] Keeping {len(keepers)} file(s):")
                for k in keepers:
                    print(f"    [{k.drive}] {k.path.name}")
                results = engine.execute(deleters)
                for r in results:
                    print(f"    {r}")
            print(f"\n[+] Done. Space {'freed' if not dry_run else 'to free'}: {format_size(total_freed)}")


if __name__ == "__main__":
    run_cli()

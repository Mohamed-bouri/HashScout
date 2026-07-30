#!/usr/bin/env python3
"""
HashScout v2.2.1 - Advanced Duplicate File & Video Finder
Multi-drive scanning, cross-volume deduplication, smart auto-cleanup,
forensic reporting, and interactive shell mode.
By Mohamed BOURI
"""

from __future__ import annotations

import argparse
import cmd
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
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
VERSION = "2.2.1"
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv",
    ".webm", ".m4v", ".mpg", ".mpeg", ".3gp", ".ts", ".ogv"
}

BANNER = r"""
 _  _         _    ___              _   
| || |__ _ __| |_ / __| __ ___ _  _| |_ 
| __ / _` (_-< '  \__ \/ _/ _ \ || |  _|
|_||_\__,_/__/_||_|___/\__\___/\_,_|\__|
         Smart Duplicate & Video Finder v2.2
              by Mohamed BOURI

Type 'help' or '?' to list commands. Type 'exit' to quit.
"""

PROMPT = "\033[92mHashScout>\033[0m "

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
    video_phash: Optional[List[str]] = None  # perceptual hash per sampled frame

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "size": self.size,
            "mtime": self.mtime,
            "drive": self.drive,
            "sha256": self.sha256,
            "duration": self.duration,
            "is_video": self.is_video,
        }


@dataclass
class DuplicateGroup:
    hash_key: str
    files: List[FileInfo] = field(default_factory=list)
    total_size: int = 0
    wasted_size: int = 0
    drives_involved: Set[str] = field(default_factory=set)

    def __post_init__(self):
        if self.files:
            self.total_size = self.files[0].size * len(self.files)
            self.wasted_size = self.files[0].size * (len(self.files) - 1)
            self.drives_involved = {f.drive for f in self.files if f.drive}


@dataclass
class FuzzyDuplicateGroup:
    """Videos that are probably the same content but NOT byte-identical
    (different size/bitrate/resolution/container). Matched by duration +
    perceptual frame hashing rather than exact hash, so these are
    probabilistic -- always review before deleting."""
    files: List[FileInfo] = field(default_factory=list)
    similarity: float = 0.0   # average perceptual match, 0-100%
    total_size: int = 0
    wasted_size: int = 0
    drives_involved: Set[str] = field(default_factory=set)

    def __post_init__(self):
        if self.files:
            self.total_size = sum(f.size for f in self.files)
            # Assumes you'd keep the largest/highest-quality copy
            self.wasted_size = self.total_size - max(f.size for f in self.files)
            self.drives_involved = {f.drive for f in self.files if f.drive}


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


def get_drive_label(path: Path) -> str:
    """Return drive letter on Windows, mount point on Linux/Mac."""
    try:
        if os.name == "nt":
            return path.drive.upper() if path.drive else "?"
        else:
            # Return the mount point (e.g., /, /mnt/data, /media/usb)
            resolved = path.resolve()
            for parent in [resolved] + list(resolved.parents):
                if parent.is_mount():
                    return str(parent)
            return "/"
    except Exception:
        return "?"


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
                 workers: int = 4, quick_mode: bool = False):
        self.target_dirs = [d.resolve() for d in target_dirs if d.exists() and d.is_dir()]
        self.video_only = video_only
        self.min_size = min_size
        self.max_size = max_size
        self.exclude = exclude_patterns or []
        self.workers = workers
        self.quick_mode = quick_mode
        self._ffprobe_available: Optional[bool] = None
        self._ffmpeg_available: Optional[bool] = None

    def _should_ignore(self, path: Path, base_dir: Path) -> bool:
        try:
            rel = path.relative_to(base_dir).as_posix()
        except ValueError:
            rel = path.name
        for pat in self.exclude:
            if fnmatch(rel, pat) or fnmatch(path.name, pat):
                return True
        return False

    def discover(self) -> List[Tuple[Path, Path]]:
        """Return list of (file_path, base_dir) tuples."""
        results: List[Tuple[Path, Path]] = []
        skipped: List[str] = []

        def _on_error(err: OSError) -> None:
            skipped.append(getattr(err, "filename", None) or str(err))

        for base_dir in self.target_dirs:
            for root, _dirs, names in os.walk(base_dir, onerror=_on_error):
                for name in names:
                    fpath = Path(root) / name
                    if fpath.is_symlink():
                        continue
                    if self.video_only and fpath.suffix.lower() not in VIDEO_EXTENSIONS:
                        continue
                    if self._should_ignore(fpath, base_dir):
                        continue
                    try:
                        if not fpath.is_file():
                            continue
                        sz = fpath.stat().st_size
                        if self.min_size and sz < self.min_size:
                            continue
                        if self.max_size and sz > self.max_size:
                            continue
                        results.append((fpath, base_dir))
                    except OSError:
                        continue

        if skipped:
            label = "directory" if len(skipped) == 1 else "directories"
            print(f"[!] Skipped {len(skipped)} unreadable {label} (permission denied)")
            for d in skipped[:5]:
                print(f"    - {d}")
            if len(skipped) > 5:
                print(f"    ... and {len(skipped) - 5} more")

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
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
        except Exception:
            pass
        return None

    def _hash_file(self, path: Path, partial: bool = False) -> Optional[Tuple[str, Optional[float]]]:
        try:
            hasher = hashlib.sha256()
            size = path.stat().st_size
            chunk = 64 * 1024
            with open(path, "rb") as f:
                if partial and size > chunk * 2:
                    hasher.update(f.read(chunk))
                    f.seek(-chunk, os.SEEK_END)
                    hasher.update(f.read(chunk))
                else:
                    while data := f.read(chunk):
                        hasher.update(data)

            dur = None
            if path.suffix.lower() in VIDEO_EXTENSIONS:
                dur = self._get_video_duration(path)

            return hasher.hexdigest(), dur
        except (OSError, PermissionError):
            return None

    def analyze_files(self, file_tuples: List[Tuple[Path, Path]]) -> List[FileInfo]:
        infos: List[FileInfo] = []
        total = len(file_tuples)
        completed = 0

        print(f"[*] Analyzing {total} files across {len(self.target_dirs)} location(s) using {self.workers} workers...")
        start = time.time()

        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            future_map = {ex.submit(self._hash_file, ft[0], True): ft for ft in file_tuples}
            for future in as_completed(future_map):
                fpath, base_dir = future_map[future]
                result = future.result()
                completed += 1
                if completed % 100 == 0 or completed == total:
                    print(f"    Progress: {completed}/{total} files...", end="\r")

                if result:
                    phash, dur = result
                    st = fpath.stat()
                    drive = get_drive_label(fpath)
                    infos.append(FileInfo(
                        path=fpath,
                        size=st.st_size,
                        mtime=st.st_mtime,
                        drive=drive,
                        partial_hash=phash,
                        duration=dur,
                        is_video=fpath.suffix.lower() in VIDEO_EXTENSIONS,
                    ))

        print()
        if not self.quick_mode:
            print(f"[*] Full-hash verification stage...")
            pre_groups: Dict[Tuple[int, str], List[FileInfo]] = {}
            for info in infos:
                key = (info.size, info.partial_hash or "")
                pre_groups.setdefault(key, []).append(info)

            collision_infos = []
            for group in pre_groups.values():
                if len(group) > 1:
                    collision_infos.extend(group)

            if collision_infos:
                with ThreadPoolExecutor(max_workers=self.workers) as ex:
                    future_map = {ex.submit(self._hash_file, i.path, False): i for i in collision_infos}
                    for future in as_completed(future_map):
                        info = future_map[future]
                        result = future.result()
                        if result:
                            info.sha256 = result[0]
                        else:
                            info.sha256 = info.partial_hash
            else:
                for info in infos:
                    info.sha256 = info.partial_hash
        else:
            for info in infos:
                info.sha256 = info.partial_hash

        elapsed = time.time() - start
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

    def _extract_frame_phashes(self, path: Path, num_frames: int = 5) -> Optional[List[str]]:
        """Sample num_frames evenly across the video and return a perceptual
        hash per frame. Skips a small margin at the start/end to avoid
        intro logos / black frames throwing off the match."""
        duration = self._get_video_duration(path)
        if not duration or duration <= 1:
            return None
        try:
            import imagehash
            from PIL import Image
        except ImportError:
            return None

        margin = min(duration * 0.05, 3.0)
        span = max(duration - 2 * margin, 0.1)
        if num_frames <= 1:
            timestamps = [duration / 2]
        else:
            timestamps = [margin + span * i / (num_frames - 1) for i in range(num_frames)]

        hashes: List[str] = []
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
                            hashes.append(str(imagehash.phash(img)))
                except Exception:
                    continue
        return hashes if hashes else None

    def find_fuzzy_video_duplicates(self, infos: List[FileInfo], duration_tolerance: float = 2.0,
                                     num_frames: int = 5, phash_threshold: int = 10) -> List[FuzzyDuplicateGroup]:
        """Find videos that are probably the same content despite different
        bytes/size (re-encoded, different bitrate/resolution/container).
        Two-stage: cheap duration bucketing narrows candidates, then
        perceptual frame hashing confirms. Results are probabilistic --
        callers should treat them as review-only, never auto-delete."""
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

        candidate_pairs: List[Tuple[int, int]] = []
        for i in range(n):
            for j in range(i + 1, n):
                if videos[j].duration - videos[i].duration > duration_tolerance:
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
            hi, hj = videos[i].video_phash, videos[j].video_phash
            if not hi or not hj:
                continue
            k = min(len(hi), len(hj))
            if k == 0:
                continue
            avg_dist = sum(hamming_distance(hi[x], hj[x]) for x in range(k)) / k
            if avg_dist <= phash_threshold:
                union(i, j)
                pair_similarity[(i, j)] = max(0.0, 100.0 * (1 - avg_dist / 64.0))

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
        for i, g in enumerate(groups, 1):
            sample = str(g.files[0].path)[:35]
            drives = ", ".join(sorted(g.drives_involved))[:18]
            lines.append(f"{i:<8} {len(g.files):<8} {drives:<20} {format_size(g.files[0].size):<14} {format_size(g.wasted_size):<14} {sample}")
        total_wasted = sum(g.wasted_size for g in groups)
        lines.append("-" * 100)
        lines.append(f"Total groups: {len(groups)} | Total wasted space: {format_size(total_wasted)}")
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
        writer.writerow(["group", "hash", "drive", "path", "size", "mtime", "duration", "is_video"])
        for i, g in enumerate(groups, 1):
            for f in g.files:
                writer.writerow([i, g.hash_key, f.drive, str(f.path), f.size, f.mtime, f.duration, f.is_video])
        return out.getvalue()


# ---------------------------------------------------------------------------
# Cleanup Engine
# ---------------------------------------------------------------------------

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

    def execute(self, deleters: List[FileInfo]) -> List[str]:
        results = []
        for f in deleters:
            if self.dry_run:
                results.append(f"[DRY-RUN] Would delete: [{f.drive}] {f.path}")
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
        self.default_workers = 4
        self.default_trash = True
        self.default_exclude: List[str] = []

    def _parse_paths(self, args: List[str]) -> List[Path]:
        paths = []
        for a in args:
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
                   quick: bool = False, workers: int = 4) -> HashScoutCore:
        return HashScoutCore(
            target_dirs=targets,
            video_only=video_only,
            min_size=min_size,
            max_size=max_size,
            exclude_patterns=self.default_exclude,
            workers=workers,
            quick_mode=quick,
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
                                 but a DIFFERENT size (re-encoded, trimmed,
                                 different bitrate/resolution/container).
                                 Matches by duration + perceptual frame
                                 hashing. Needs ffmpeg + Pillow + imagehash.
        --fuzzy-tolerance N      Max duration difference in seconds (default 2)
        --fuzzy-frames N         Frames sampled per video (default 5)
        --fuzzy-threshold N      Max avg hash distance to count as a match,
                                 0-64, lower = stricter (default 10)
        Examples:
          scan C:\Users\Admin\Pictures
          scan C:\ D:\ E:\ --video
          scan ~/Downloads ~/Videos ~/Backups --workers 8
          scan ~/Videos --video --fuzzy
        """
        if not arg.strip():
            print("[-] Usage: scan <path> [more paths...] [options]")
            print("    scan C:\\Users\\Admin\\Pictures D:\\Backups --video")
            return

        parts = arg.split()
        targets = self._parse_paths(parts)
        if not targets:
            print("[!] No valid directories provided.")
            return

        video_only = "--video" in parts or "-v" in parts
        quick = "--quick" in parts or "-q" in parts
        fuzzy = "--fuzzy" in parts
        workers = self.default_workers
        min_sz = 0
        max_sz = 0
        fuzzy_tolerance = 2.0
        fuzzy_frames = 5
        fuzzy_threshold = 10

        for i, p in enumerate(parts):
            if p in ("--workers", "-w") and i + 1 < len(parts):
                workers = int(parts[i + 1])
            if p in ("--min-size", "-min") and i + 1 < len(parts):
                min_sz = parse_size(parts[i + 1])
            if p in ("--max-size", "-max") and i + 1 < len(parts):
                max_sz = parse_size(parts[i + 1])
            if p == "--fuzzy-tolerance" and i + 1 < len(parts):
                fuzzy_tolerance = float(parts[i + 1])
            if p == "--fuzzy-frames" and i + 1 < len(parts):
                fuzzy_frames = int(parts[i + 1])
            if p == "--fuzzy-threshold" and i + 1 < len(parts):
                fuzzy_threshold = int(parts[i + 1])

        core = self._make_core(targets, video_only, min_sz, max_sz, quick, workers)
        file_tuples = core.discover()
        if not file_tuples:
            print("[!] No files matched.")
            return

        infos = core.analyze_files(file_tuples)
        self.last_infos = infos
        self.last_groups = core.find_duplicates(infos)
        self._show_groups(self.last_groups)

        if fuzzy:
            self._run_fuzzy(core, fuzzy_tolerance, fuzzy_frames, fuzzy_threshold)

    def _run_fuzzy(self, core: HashScoutCore, tolerance: float, frames: int, threshold: int):
        """Run fuzzy video matching over the last scan's files, skipping
        anything already caught by exact-hash matching."""
        exact_paths = {f.path for g in self.last_groups for f in g.files}
        candidates = [i for i in self.last_infos if i.is_video and i.path not in exact_paths]
        self.last_fuzzy_groups = core.find_fuzzy_video_duplicates(
            candidates, duration_tolerance=tolerance, num_frames=frames, phash_threshold=threshold
        )
        print(OutputFormatter.fuzzy_detailed(self.last_fuzzy_groups))

    def do_fuzzy(self, arg):
        """
        fuzzy [--tolerance 2] [--frames 5] [--threshold 10]
        Find videos that are the SAME content but a DIFFERENT file size,
        among the files from the last 'scan' (run 'scan' first). Matches
        by duration + perceptual frame hashing -- these are PROBABLE
        matches, not byte-identical, so review before deleting.
        'clean' never touches fuzzy groups.
        """
        if not self.last_infos:
            print("[!] No scan results. Run 'scan <path>' first.")
            return

        parts = arg.split() if arg else []
        tolerance, frames, threshold = 2.0, 5, 10
        for i, p in enumerate(parts):
            if p == "--tolerance" and i + 1 < len(parts):
                tolerance = float(parts[i + 1])
            if p == "--frames" and i + 1 < len(parts):
                frames = int(parts[i + 1])
            if p == "--threshold" and i + 1 < len(parts):
                threshold = int(parts[i + 1])

        core = self._make_core(self.current_dirs, workers=self.default_workers)
        self._run_fuzzy(core, tolerance, frames, threshold)

    def do_clean(self, arg):
        """
        clean [--strategy newest|oldest|largest|smallest|shortest|longest|first|spread]
              [--trash] [--apply] [--interactive]
        Clean duplicates from the last scan.
        Without --apply, runs in dry-run mode.
        """
        if not self.last_groups:
            print("[!] No scan results. Run 'scan <path>' first.")
            return

        parts = arg.split() if arg else []
        strategy = KeepStrategy.MANUAL
        trash = self.default_trash
        dry_run = "--apply" not in parts
        force_interactive = "--interactive" in parts or "-i" in parts

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
            self._interactive_clean_groups(self.last_groups, trash, dry_run)
        else:
            self._auto_clean_groups(self.last_groups, strategy, trash, dry_run)

    def do_fuzzyclean(self, arg):
        """
        fuzzyclean [--strategy newest|oldest|largest|smallest|shortest|longest|first]
                   [--trash] [--no-trash] [--apply] [--interactive]
        Clean up fuzzy (perceptual) video duplicate groups from the last
        'fuzzy' / 'scan --fuzzy' run.

        These are PROBABLE matches, not byte-identical -- unlike 'clean',
        this command always asks for a typed confirmation before it
        actually deletes anything (with --apply). Without --apply, runs
        in dry-run mode. 'spread' strategy is not supported here.
        """
        if not self.last_fuzzy_groups:
            print("[!] No fuzzy results. Run 'scan <path> --fuzzy' or 'fuzzy' first.")
            return

        parts = arg.split() if arg else []
        strategy = KeepStrategy.MANUAL
        trash = self.default_trash
        dry_run = "--apply" not in parts
        force_interactive = "--interactive" in parts or "-i" in parts

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

        if strategy == KeepStrategy.SPREAD:
            print("[!] 'spread' strategy isn't supported for fuzzy groups (files differ in size/quality).")
            return

        if not dry_run:
            print("\n[!] WARNING: fuzzy groups are PROBABLE duplicates, not byte-identical.")
            print("    If two different videos happened to look similar, deleting here")
            print("    could remove something you meant to keep. Review the groups first.")
            try:
                confirm = input("    Type YES to continue and actually delete files: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n[!] Cancelled. No files were touched.")
                return
            if confirm != "YES":
                print("[*] Cancelled. No files were touched.")
                return

        if force_interactive or strategy == KeepStrategy.MANUAL:
            self._interactive_clean_groups(self.last_fuzzy_groups, trash, dry_run)
        else:
            self._auto_clean_groups(self.last_fuzzy_groups, strategy, trash, dry_run)

    def _auto_clean_groups(self, groups, strategy: KeepStrategy, trash: bool, dry_run: bool):
        engine = CleanupEngine(strategy, trash=trash, dry_run=dry_run)
        print(f"[*] Strategy: {strategy.value} | Trash: {trash} | Dry-run: {dry_run}")
        total_saved = 0
        for group in groups:
            keepers, deleters = engine.pick_keepers(group)
            if not keepers:
                continue
            print(f"\n[+] Keeping {len(keepers)} file(s):")
            for k in keepers:
                print(f"    [{k.drive}] {k.path.name}")
            results = engine.execute(deleters)
            for r in results:
                print(f"    {r}")
            total_saved += sum(d.size for d in deleters)
        print(f"\n[+] Done. Space {'saved' if not dry_run else 'to save'}: {format_size(total_saved)}")

    def _interactive_clean_groups(self, groups, trash: bool, dry_run: bool):
        if not groups:
            print("[+] Nothing to clean.")
            return

        print(f"\n [DRY-RUN] No files deleted yet. Use --apply to execute.\n" if dry_run else "")

        for idx, group in enumerate(groups, 1):
            drives_str = ", ".join(sorted(group.drives_involved))
            print(f"\n--- Group {idx}/{len(groups)} | Wasted: {format_size(group.wasted_size)} | Drives: {drives_str} ---")
            for i, f in enumerate(group.files, 1):
                dur = f" | {format_duration(f.duration)}" if f.duration else ""
                mod = datetime.fromtimestamp(f.mtime).strftime("%Y-%m-%d %H:%M")
                print(f"  [{i}] [{f.drive}] {f.path.name} ({format_size(f.size)}{dur}) ({mod})")

            if dry_run:
                continue

            print("\nSelect: number=KEEP that file (delete rest) | s=skip | q=quit")
            while True:
                try:
                    choice = input("Keep > ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print("\n[!] Aborted.")
                    return
                if choice in ("s", "skip", ""):
                    print("[*] Skipped.")
                    break
                elif choice == "q":
                    print("[!] Exiting cleanup.")
                    return
                elif choice.isdigit():
                    keep_idx = int(choice) - 1
                    if 0 <= keep_idx < len(group.files):
                        keepers = [group.files[keep_idx]]
                        deleters = [f for i, f in enumerate(group.files) if i != keep_idx]
                        print(f"[+] Keeping: [{keepers[0].drive}] {keepers[0].path.name}")
                        for d in deleters:
                            ok, msg = safe_trash(d.path) if trash else (False, "")
                            if not trash:
                                try:
                                    d.path.unlink()
                                    msg = f"[DELETED] [{d.drive}] {d.path}"
                                except Exception as e:
                                    msg = f"[ERROR] [{d.drive}] {d.path}: {e}"
                            print(f"    {msg}")
                        break
                    else:
                        print("[!] Invalid index.")
                else:
                    print("[!] Invalid input.")

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
        self.default_exclude.append(arg.strip())
        print(f"[+] Added exclude: {arg.strip()}")

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
====================================================================
                         AVAILABLE COMMANDS
====================================================================
  scan <path> [path2] ... [opts]
      Scan one or multiple directories/drives for duplicates.
      --video, -v          Only video files
      --quick, -q          Skip full hash (faster)
      --workers N          Thread count
      --min-size 10MB      Minimum file size
      --max-size 1GB       Maximum file size
      --fuzzy              Also find same-content videos of DIFFERENT size
                           (re-encoded/trimmed) via duration + frame hash

      Examples:
        scan C:\\Users\\Admin\\Pictures
        scan C:\\ D:\\ E:\\ --video
        scan ~/Downloads ~/Videos ~/Backups --workers 8
        scan ~/Videos --video --fuzzy

  fuzzy [opts]              Find same-content/different-size video
                             duplicates from the last scan
      --tolerance N          Max duration difference, seconds (default 2)
      --frames N             Frames sampled per video (default 5)
      --threshold N          Match strictness, 0-64 (default 10)

  fuzzyclean [opts]         Clean up fuzzy video groups from last 'fuzzy'/
                             'scan --fuzzy' run. Asks for typed confirmation
                             before --apply actually deletes anything.
      --strategy <n>          newest | oldest | largest | smallest |
                               shortest | longest | first | manual (default)
      --trash / --no-trash
      --apply                 Actually delete (default is dry-run)
      --interactive, -i

  clean [opts]             Clean duplicates from last scan
      --strategy <name>    newest | oldest | largest | smallest |
                           shortest | longest | first | spread |
                           manual (default)
      --trash              Move to trash (default)
      --no-trash           Permanent delete
      --apply              Actually delete (default is dry-run)
      --interactive, -i    Force interactive picker

      spread strategy:     Keeps one copy per drive, deletes extras

  export <file>            Export last scan (.json, .csv, .txt)

  cd <path>                Change default directory
  pwd                      Show current directory
  exclude <pattern>        Add ignore glob pattern
  exclude --list           List ignore patterns
  exclude --clear          Clear ignore patterns
  workers <N>              Set default thread count
  clear                    Clear screen
  exit / quit              Leave HashScout
====================================================================
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
    parser.add_argument("paths", nargs="*", default=["."], help="Target directories (default: current)")
    parser.add_argument("--workers", type=int, default=4, help="Hashing threads (default: 4)")
    parser.add_argument("--video-only", action="store_true", help="Scan only video files")
    parser.add_argument("--min-size", type=str, default="", help="Minimum file size (e.g., 10MB, 1GB)")
    parser.add_argument("--max-size", type=str, default="", help="Maximum file size")
    parser.add_argument("--exclude", action="append", help="Glob ignore pattern (repeatable)")
    parser.add_argument("--quick", action="store_true", help="Skip full hash (faster)")
    parser.add_argument("--fuzzy", action="store_true",
                        help="Also find same-content videos of different size (re-encoded/trimmed)")
    parser.add_argument("--fuzzy-tolerance", type=float, default=2.0, help="Max duration diff in seconds (default: 2)")
    parser.add_argument("--fuzzy-frames", type=int, default=5, help="Frames sampled per video (default: 5)")
    parser.add_argument("--fuzzy-threshold", type=int, default=10, help="Match strictness, 0-64 (default: 10)")
    parser.add_argument("--format", choices=["table", "detailed", "json", "csv"], default="detailed",
                        help="Output format (default: detailed)")

    sub = parser.add_subparsers(dest="command", help="Commands")

    scan_p = sub.add_parser("scan", help="Scan and report duplicates")

    clean_p = sub.add_parser("clean", help="Auto or interactive cleanup")
    clean_p.add_argument("--strategy", choices=[s.value for s in KeepStrategy], default="manual")
    clean_p.add_argument("--interactive", action="store_true", help="Force interactive mode")
    clean_p.add_argument("--trash", action="store_true", default=True, help="Move to trash")
    clean_p.add_argument("--no-trash", action="store_true", help="Permanent delete")
    clean_p.add_argument("--apply", action="store_true", help="Actually delete")

    exp_p = sub.add_parser("export", help="Export scan report to file")
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
            total_saved = 0
            for group in groups:
                keepers, deleters = engine.pick_keepers(group)
                if not keepers:
                    continue
                print(f"\n[+] Keeping {len(keepers)} file(s):")
                for k in keepers:
                    print(f"    [{k.drive}] {k.path.name}")
                results = engine.execute(deleters)
                for r in results:
                    print(f"    {r}")
                total_saved += sum(d.size for d in deleters)
            print(f"\n[+] Done. Space {'saved' if not dry_run else 'to save'}: {format_size(total_saved)}")


if __name__ == "__main__":
    run_cli()

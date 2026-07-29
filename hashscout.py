#!/usr/bin/env python3
"""
HashScout v2.1 - Advanced Duplicate File & Video Finder
Smart deduplication with multi-threaded hashing, auto-cleanup strategies,
forensic reporting, safe deletion, and interactive shell mode.
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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from datetime import datetime
from enum import Enum
from fnmatch import fnmatch
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
APP_NAME = "HashScout"
VERSION = "2.1.0"
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv",
    ".webm", ".m4v", ".mpg", ".mpeg", ".3gp", ".ts", ".ogv"
}

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


@dataclass
class FileInfo:
    path: Path
    size: int
    mtime: float
    sha256: Optional[str] = None
    partial_hash: Optional[str] = None
    duration: Optional[float] = None
    is_video: bool = False

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "size": self.size,
            "mtime": self.mtime,
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

    def __post_init__(self):
        if self.files:
            self.total_size = self.files[0].size * len(self.files)
            self.wasted_size = self.files[0].size * (len(self.files) - 1)


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
    def __init__(self, target_dir: Path, video_only: bool = False,
                 min_size: int = 0, max_size: int = 0,
                 exclude_patterns: Optional[List[str]] = None,
                 workers: int = 4, quick_mode: bool = False):
        self.target_dir = target_dir.resolve()
        self.video_only = video_only
        self.min_size = min_size
        self.max_size = max_size
        self.exclude = exclude_patterns or []
        self.workers = workers
        self.quick_mode = quick_mode
        self._ffprobe_available: Optional[bool] = None

    def _should_ignore(self, path: Path) -> bool:
        try:
            rel = path.relative_to(self.target_dir).as_posix()
        except ValueError:
            rel = path.name
        for pat in self.exclude:
            if fnmatch(rel, pat) or fnmatch(path.name, pat):
                return True
        return False

    def discover(self) -> List[Path]:
        files: List[Path] = []
        skipped: List[str] = []

        def _on_error(err: OSError) -> None:
            skipped.append(getattr(err, "filename", None) or str(err))

        for root, _dirs, names in os.walk(self.target_dir, onerror=_on_error):
            for name in names:
                fpath = Path(root) / name
                if fpath.is_symlink():
                    continue
                if self.video_only and fpath.suffix.lower() not in VIDEO_EXTENSIONS:
                    continue
                if self._should_ignore(fpath):
                    continue
                try:
                    if not fpath.is_file():
                        continue
                    sz = fpath.stat().st_size
                    if self.min_size and sz < self.min_size:
                        continue
                    if self.max_size and sz > self.max_size:
                        continue
                    files.append(fpath)
                except OSError:
                    continue

        if skipped:
            label = "directory" if len(skipped) == 1 else "directories"
            print(f"[!] Skipped {len(skipped)} unreadable {label} (permission denied)")
            for d in skipped[:5]:
                print(f"    - {d}")
            if len(skipped) > 5:
                print(f"    ... and {len(skipped) - 5} more")

        return files

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

    def analyze_files(self, paths: List[Path]) -> List[FileInfo]:
        infos: List[FileInfo] = []
        total = len(paths)
        completed = 0

        print(f"[*] Analyzing {total} files using {self.workers} workers...")
        start = time.time()

        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            future_map = {ex.submit(self._hash_file, p, True): p for p in paths}
            for future in as_completed(future_map):
                path = future_map[future]
                result = future.result()
                completed += 1
                if completed % 100 == 0 or completed == total:
                    print(f"    Progress: {completed}/{total} files...", end="\r")

                if result:
                    phash, dur = result
                    st = path.stat()
                    infos.append(FileInfo(
                        path=path,
                        size=st.st_size,
                        mtime=st.st_mtime,
                        partial_hash=phash,
                        duration=dur,
                        is_video=path.suffix.lower() in VIDEO_EXTENSIONS,
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


# ---------------------------------------------------------------------------
# Output Formatters
# ---------------------------------------------------------------------------

class OutputFormatter:
    @staticmethod
    def table(groups: List[DuplicateGroup]) -> str:
        if not groups:
            return "[+] No duplicates found."
        lines = []
        lines.append(f"{'GROUP':<8} {'FILES':<8} {'SIZE EACH':<14} {'WASTED':<14} {'SAMPLE PATH'}")
        lines.append("-" * 90)
        for i, g in enumerate(groups, 1):
            sample = str(g.files[0].path)[:40]
            lines.append(f"{i:<8} {len(g.files):<8} {format_size(g.files[0].size):<14} {format_size(g.wasted_size):<14} {sample}")
        total_wasted = sum(g.wasted_size for g in groups)
        lines.append("-" * 90)
        lines.append(f"Total groups: {len(groups)} | Total wasted space: {format_size(total_wasted)}")
        return "\n".join(lines)

    @staticmethod
    def detailed(groups: List[DuplicateGroup]) -> str:
        if not groups:
            return "[+] No duplicates found."
        lines = []
        for i, g in enumerate(groups, 1):
            lines.append(f"\n--- [ Group {i} / {len(groups)} ] ---")
            lines.append(f"Hash: {g.hash_key[:16]}... | Size each: {format_size(g.files[0].size)} | Wasted: {format_size(g.wasted_size)}")
            for j, f in enumerate(g.files, 1):
                dur = f" | {format_duration(f.duration)}" if f.duration else ""
                mod = datetime.fromtimestamp(f.mtime).strftime("%Y-%m-%d %H:%M")
                lines.append(f"  [{j}] {f.path.name}")
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
                    "files": [f.to_dict() for f in g.files],
                }
                for g in groups
            ],
        }
        return json.dumps(data, indent=2)

    @staticmethod
    def csv_out(groups: List[DuplicateGroup]) -> str:
        import io
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["group", "hash", "path", "size", "mtime", "duration", "is_video"])
        for i, g in enumerate(groups, 1):
            for f in g.files:
                writer.writerow([i, g.hash_key, str(f.path), f.size, f.mtime, f.duration, f.is_video])
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
                results.append(f"[DRY-RUN] Would delete: {f.path}")
                continue
            if self.trash:
                ok, msg = safe_trash(f.path)
            else:
                try:
                    f.path.unlink()
                    ok, msg = True, f"[DELETED] {f.path}"
                except Exception as e:
                    ok, msg = False, f"[ERROR] {f.path}: {e}"
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
        self.current_dir = Path(".").resolve()
        self.last_groups: List[DuplicateGroup] = []
        self.default_workers = 4
        self.default_trash = True
        self.default_exclude: List[str] = []

    # -------------------------------------------------------------- #
    # Helpers
    # -------------------------------------------------------------- #

    def _parse_path(self, arg: str) -> Path:
        p = Path(arg).expanduser().resolve()
        if not p.exists():
            print(f"[!] Path not found: {p}")
        return p

    def _make_core(self, target: Path, video_only: bool = False,
                   min_size: int = 0, max_size: int = 0,
                   quick: bool = False, workers: int = 4) -> HashScoutCore:
        return HashScoutCore(
            target_dir=target,
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
        """
        scan <path> [--video] [--quick] [--min-size 10MB] [--max-size 1GB] [--workers 4]
        Scan a directory for duplicates and display results.
        """
        if not arg.strip():
            print("[-] Usage: scan <path> [options]")
            return

        parts = arg.split()
        target = self._parse_path(parts[0])
        if not target.exists():
            return

        video_only = "--video" in parts or "-v" in parts
        quick = "--quick" in parts or "-q" in parts
        workers = self.default_workers
        min_sz = 0
        max_sz = 0

        for i, p in enumerate(parts):
            if p in ("--workers", "-w") and i + 1 < len(parts):
                workers = int(parts[i + 1])
            if p in ("--min-size", "-min") and i + 1 < len(parts):
                min_sz = parse_size(parts[i + 1])
            if p in ("--max-size", "-max") and i + 1 < len(parts):
                max_sz = parse_size(parts[i + 1])

        core = self._make_core(target, video_only, min_sz, max_sz, quick, workers)
        files = core.discover()
        if not files:
            print("[!] No files matched.")
            return

        infos = core.analyze_files(files)
        self.last_groups = core.find_duplicates(infos)
        self._show_groups(self.last_groups)

    def do_clean(self, arg):
        """
        clean [--strategy newest|oldest|largest|smallest|shortest|longest|first]
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
            self._interactive_clean(trash, dry_run)
        else:
            self._auto_clean(strategy, trash, dry_run)

    def _auto_clean(self, strategy: KeepStrategy, trash: bool, dry_run: bool):
        engine = CleanupEngine(strategy, trash=trash, dry_run=dry_run)
        print(f"[*] Strategy: {strategy.value} | Trash: {trash} | Dry-run: {dry_run}")
        total_saved = 0
        for group in self.last_groups:
            keepers, deleters = engine.pick_keepers(group)
            if not keepers:
                continue
            print(f"\n[+] Keeping: {keepers[0].path.name}")
            results = engine.execute(deleters)
            for r in results:
                print(f"    {r}")
            total_saved += sum(d.size for d in deleters)
        print(f"\n[+] Done. Space {'saved' if not dry_run else 'to save'}: {format_size(total_saved)}")

    def _interactive_clean(self, trash: bool, dry_run: bool):
        if not self.last_groups:
            print("[+] Nothing to clean.")
            return

        print(f"\n [DRY-RUN] No files deleted yet. Use --apply to execute.\n" if dry_run else "")

        for idx, group in enumerate(self.last_groups, 1):
            print(f"\n--- Group {idx}/{len(self.last_groups)} | Wasted: {format_size(group.wasted_size)} ---")
            for i, f in enumerate(group.files, 1):
                dur = f" | {format_duration(f.duration)}" if f.duration else ""
                mod = datetime.fromtimestamp(f.mtime).strftime("%Y-%m-%d %H:%M")
                print(f"  [{i}] {f.path.name} ({mod}{dur})")

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
                        print(f"[+] Keeping: {keepers[0].path.name}")
                        for d in deleters:
                            ok, msg = safe_trash(d.path) if trash else (False, "")
                            if not trash:
                                try:
                                    d.path.unlink()
                                    msg = f"[DELETED] {d.path}"
                                except Exception as e:
                                    msg = f"[ERROR] {d.path}: {e}"
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
        Change the current working directory.
        """
        if not arg.strip():
            print(f"[*] Current: {self.current_dir}")
            return
        p = self._parse_path(arg.strip())
        if p.exists() and p.is_dir():
            self.current_dir = p
            print(f"[+] Changed to: {p}")
        else:
            print(f"[!] Not a directory: {p}")

    def do_pwd(self, arg):
        """Show current directory."""
        print(f"[*] {self.current_dir}")

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
        print("""
====================================================================
                         AVAILABLE COMMANDS
====================================================================
  scan <path> [opts]     Scan directory for duplicates
      --video, -v          Only video files
      --quick, -q          Skip full hash (faster)
      --workers N          Thread count
      --min-size 10MB      Minimum file size
      --max-size 1GB       Maximum file size

  clean [opts]             Clean duplicates from last scan
      --strategy <name>    newest | oldest | largest | smallest |
                           shortest | longest | first | manual
      --trash              Move to trash (default)
      --no-trash           Permanent delete
      --apply              Actually delete (default is dry-run)
      --interactive, -i    Force interactive picker

  export <file>            Export last scan (.json, .csv, .txt)

  cd <path>                Change directory
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
        description="HashScout v2.1 - Advanced Duplicate File & Video Finder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  hashscout                          # Launch interactive shell
  hashscout scan ~/Videos --video-only --format json
  hashscout clean ~/Downloads --strategy keep-newest --trash --apply
  hashscout export ~/Videos --output report.csv
        """,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("-p", "--path", default=".", help="Target directory")
    parser.add_argument("--workers", type=int, default=4, help="Hashing threads (default: 4)")
    parser.add_argument("--video-only", action="store_true", help="Scan only video files")
    parser.add_argument("--min-size", type=str, default="", help="Minimum file size (e.g., 10MB, 1GB)")
    parser.add_argument("--max-size", type=str, default="", help="Maximum file size")
    parser.add_argument("--exclude", action="append", help="Glob ignore pattern (repeatable)")
    parser.add_argument("--quick", action="store_true", help="Skip full hash (faster)")
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
    target = Path(args.path).expanduser().resolve()
    if not target.exists() or not target.is_dir():
        print(f"[!] Invalid directory: {target}")
        sys.exit(1)

    min_sz = parse_size(args.min_size) if args.min_size else 0
    max_sz = parse_size(args.max_size) if args.max_size else 0

    core = HashScoutCore(
        target_dir=target,
        video_only=args.video_only,
        min_size=min_sz,
        max_size=max_sz,
        exclude_patterns=args.exclude,
        workers=args.workers,
        quick_mode=args.quick,
    )

    fmt = OutputFormatter()

    if args.command == "scan":
        files = core.discover()
        if not files:
            print("[!] No files matched.")
            sys.exit(0)
        infos = core.analyze_files(files)
        groups = core.find_duplicates(infos)

        if args.format == "table":
            print(fmt.table(groups))
        elif args.format == "detailed":
            print(fmt.detailed(groups))
        elif args.format == "json":
            print(fmt.json_out(groups))
        elif args.format == "csv":
            print(fmt.csv_out(groups))
        sys.exit(1 if groups else 0)

    elif args.command == "export":
        files = core.discover()
        if not files:
            print("[!] No files matched.")
            sys.exit(0)
        infos = core.analyze_files(files)
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
        files = core.discover()
        if not files:
            print("[!] No files matched.")
            sys.exit(0)
        infos = core.analyze_files(files)
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
                print(f"\n[+] Keeping: {keepers[0].path.name}")
                results = engine.execute(deleters)
                for r in results:
                    print(f"    {r}")
                total_saved += sum(d.size for d in deleters)
            print(f"\n[+] Done. Space {'saved' if not dry_run else 'to save'}: {format_size(total_saved)}")


if __name__ == "__main__":
    run_cli()
#!/usr/bin/env python3
"""
HashScout v2.0 - Advanced Duplicate File & Video Finder
Smart deduplication with multi-threaded hashing, auto-cleanup strategies,
forensic reporting, and safe deletion (trash-aware).
By Mohamed BOURI
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from datetime import datetime
from enum import Enum
from fnmatch import fnmatch
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
APP_NAME = "HashScout"
VERSION = "2.0.0"
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv",
    ".webm", ".m4v", ".mpg", ".mpeg", ".3gp", ".ts", ".ogv"
}

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
"""

# ---------------------------------------------------------------------------
# Enums & Data Classes
# ---------------------------------------------------------------------------

class KeepStrategy(Enum):
    MANUAL = "manual"           # Interactive pick
    NEWEST = "newest"           # Keep most recently modified
    OLDEST = "oldest"           # Keep earliest modified
    LARGEST = "largest"         # Keep biggest file
    SMALLEST = "smallest"       # Keep smallest file
    SHORTEST = "shortest"       # Keep shortest video duration
    LONGEST = "longest"         # Keep longest video duration
    FIRST = "first"             # Keep first found, delete rest


@dataclass
class FileInfo:
    path: Path
    size: int
    mtime: float
    sha256: Optional[str] = None
    partial_hash: Optional[str] = None
    duration: Optional[float] = None
    is_video: bool = False

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "size": self.size,
            "mtime": self.mtime,
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

    def __post_init__(self):
        if self.files:
            self.total_size = self.files[0].size * len(self.files)
            self.wasted_size = self.files[0].size * (len(self.files) - 1)


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


def parse_size(size_str: str) -> int:
    """Parse human-readable size like '10MB', '1.5GB' to bytes."""
    size_str = size_str.strip().upper().replace(" ", "")
    multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    for suffix, mult in sorted(multipliers.items(), key=lambda x: -x[1]):
        if size_str.endswith(suffix):
            return int(float(size_str[:-len(suffix)]) * mult)
    return int(size_str)


def safe_trash(path: Path) -> Tuple[bool, str]:
    """Move to trash if possible, else delete with warning."""
    try:
        # Try send2trash if available
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
    def __init__(self, target_dir: Path, video_only: bool = False,
                 min_size: int = 0, max_size: int = 0,
                 exclude_patterns: Optional[List[str]] = None,
                 workers: int = 4, quick_mode: bool = False):
        self.target_dir = target_dir.resolve()
        self.video_only = video_only
        self.min_size = min_size
        self.max_size = max_size
        self.exclude = exclude_patterns or []
        self.workers = workers
        self.quick_mode = quick_mode
        self._ffprobe_available: Optional[bool] = None

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #

    def _should_ignore(self, path: Path) -> bool:
        rel = path.relative_to(self.target_dir).as_posix()
        for pat in self.exclude:
            if fnmatch(rel, pat) or fnmatch(path.name, pat):
                return True
        return False

    def discover(self) -> List[Path]:
        """Collect candidate files with permission reporting."""
        files: List[Path] = []
        skipped: List[str] = []

        def _on_error(err: OSError) -> None:
            skipped.append(getattr(err, "filename", None) or str(err))

        for root, _dirs, names in os.walk(self.target_dir, onerror=_on_error):
            for name in names:
                fpath = Path(root) / name
                if fpath.is_symlink():
                    continue
                if self.video_only and fpath.suffix.lower() not in VIDEO_EXTENSIONS:
                    continue
                if self._should_ignore(fpath):
                    continue
                try:
                    if not fpath.is_file():
                        continue
                    sz = fpath.stat().st_size
                    if self.min_size and sz < self.min_size:
                        continue
                    if self.max_size and sz > self.max_size:
                        continue
                    files.append(fpath)
                except OSError:
                    continue

        if skipped:
            label = "directory" if len(skipped) == 1 else "directories"
            print(f"[!] Skipped {len(skipped)} unreadable {label} (permission denied)")
            for d in skipped[:5]:
                print(f"    - {d}")
            if len(skipped) > 5:
                print(f"    ... and {len(skipped) - 5} more")

        return files

    # ------------------------------------------------------------------ #
    # Hashing & Metadata
    # ------------------------------------------------------------------ #

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
        """Returns (sha256, duration) or None."""
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

    def analyze_files(self, paths: List[Path]) -> List[FileInfo]:
        """Multi-threaded hash and metadata extraction."""
        infos: List[FileInfo] = []
        total = len(paths)
        completed = 0

        print(f"[*] Analyzing {total} files using {self.workers} workers...")
        start = time.time()

        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            future_map = {ex.submit(self._hash_file, p, True): p for p in paths}
            for future in as_completed(future_map):
                path = future_map[future]
                result = future.result()
                completed += 1
                if completed % 100 == 0 or completed == total:
                    print(f"    Progress: {completed}/{total} files...", end="\r")

                if result:
                    phash, dur = result
                    st = path.stat()
                    infos.append(FileInfo(
                        path=path,
                        size=st.st_size,
                        mtime=st.st_mtime,
                        partial_hash=phash,
                        duration=dur,
                        is_video=path.suffix.lower() in VIDEO_EXTENSIONS,
                    ))

        print()  # newline after progress
        if not self.quick_mode:
            print(f"[*] Full-hash verification stage...")
            # Group by partial hash+size, then full hash only the collisions
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
                            info.sha256 = info.partial_hash  # fallback
            else:
                for info in infos:
                    info.sha256 = info.partial_hash
        else:
            for info in infos:
                info.sha256 = info.partial_hash

        elapsed = time.time() - start
        print(f"[+] Analysis complete in {elapsed:.1f}s")
        return infos

    # ------------------------------------------------------------------ #
    # Deduplication Pipeline
    # ------------------------------------------------------------------ #

    def find_duplicates(self, infos: List[FileInfo]) -> List[DuplicateGroup]:
        """Group files by full hash."""
        hash_map: Dict[str, List[FileInfo]] = {}
        for info in infos:
            if info.sha256:
                hash_map.setdefault(info.sha256, []).append(info)

        groups = []
        for hkey, files in hash_map.items():
            if len(files) > 1:
                groups.append(DuplicateGroup(hash_key=hkey, files=files))

        # Sort by wasted size (biggest impact first)
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
        lines.append(f"{'GROUP':<8} {'FILES':<8} {'SIZE EACH':<14} {'WASTED':<14} {'SAMPLE PATH'}")
        lines.append("-" * 90)
        for i, g in enumerate(groups, 1):
            sample = str(g.files[0].path)[:40]
            lines.append(f"{i:<8} {len(g.files):<8} {format_size(g.files[0].size):<14} {format_size(g.wasted_size):<14} {sample}")
        total_wasted = sum(g.wasted_size for g in groups)
        lines.append("-" * 90)
        lines.append(f"Total groups: {len(groups)} | Total wasted space: {format_size(total_wasted)}")
        return "\n".join(lines)

    @staticmethod
    def detailed(groups: List[DuplicateGroup]) -> str:
        if not groups:
            return "[+] No duplicates found."
        lines = []
        for i, g in enumerate(groups, 1):
            lines.append(f"\n--- [ Group {i} / {len(groups)} ] ---")
            lines.append(f"Hash: {g.hash_key[:16]}... | Size each: {format_size(g.files[0].size)} | Wasted: {format_size(g.wasted_size)}")
            for j, f in enumerate(g.files, 1):
                dur = f" | {format_duration(f.duration)}" if f.duration else ""
                mod = datetime.fromtimestamp(f.mtime).strftime("%Y-%m-%d %H:%M")
                lines.append(f"  [{j}] {f.path.name}")
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
                    "files": [f.to_dict() for f in g.files],
                }
                for g in groups
            ],
        }
        return json.dumps(data, indent=2)

    @staticmethod
    def csv_out(groups: List[DuplicateGroup]) -> str:
        import io
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["group", "hash", "path", "size", "mtime", "duration", "is_video"])
        for i, g in enumerate(groups, 1):
            for f in g.files:
                writer.writerow([i, g.hash_key, str(f.path), f.size, f.mtime, f.duration, f.is_video])
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
        """Returns (keepers, deleters)."""
        files = group.files[:]
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
            pass  # Keep original order
        else:
            # MANUAL - return all as deleters (handled by caller)
            return [], files

        return [files[0]], files[1:]

    def execute(self, deleters: List[FileInfo]) -> List[str]:
        results = []
        for f in deleters:
            if self.dry_run:
                results.append(f"[DRY-RUN] Would delete: {f.path}")
                continue
            if self.trash:
                ok, msg = safe_trash(f.path)
            else:
                try:
                    f.path.unlink()
                    ok, msg = True, f"[DELETED] {f.path}"
                except Exception as e:
                    ok, msg = False, f"[ERROR] {f.path}: {e}"
            results.append(msg)
        return results


# ---------------------------------------------------------------------------
# Interactive Shell
# ---------------------------------------------------------------------------

def interactive_clean(groups: List[DuplicateGroup], trash: bool = True, dry_run: bool = True) -> None:
    if not groups:
        print("[+] No duplicates to clean.")
        return

    engine = CleanupEngine(KeepStrategy.MANUAL, trash=trash, dry_run=dry_run)
    print(OutputFormatter.detailed(groups))

    if dry_run:
        print("\n [DRY RUN] No files will be deleted. Use --apply to execute.\n")

    for idx, group in enumerate(groups, 1):
        print(f"\n--- Group {idx}/{len(groups)} | Wasted: {format_size(group.wasted_size)} ---")
        for i, f in enumerate(group.files, 1):
            dur = f" | {format_duration(f.duration)}" if f.duration else ""
            mod = datetime.fromtimestamp(f.mtime).strftime("%Y-%m-%d %H:%M")
            print(f"  [{i}] {f.path.name} ({mod}{dur})")

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
                print("[!] Exiting.")
                return
            elif choice.isdigit():
                keep_idx = int(choice) - 1
                if 0 <= keep_idx < len(group.files):
                    keepers = [group.files[keep_idx]]
                    deleters = [f for i, f in enumerate(group.files) if i != keep_idx]
                    print(f"[+] Keeping: {keepers[0].path.name}")
                    for d in deleters:
                        ok, msg = safe_trash(d.path) if trash else (False, "")
                        if not trash:
                            try:
                                d.path.unlink()
                                msg = f"[DELETED] {d.path}"
                            except Exception as e:
                                msg = f"[ERROR] {d.path}: {e}"
                        print(f"    {msg}")
                    break
                else:
                    print("[!] Invalid index.")
            else:
                print("[!] Invalid input.")


# ---------------------------------------------------------------------------
# CLI Parser
# ---------------------------------------------------------------------------

def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hashscout",
        description="HashScout v2.0 - Advanced Duplicate File & Video Finder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  hashscout scan ~/Videos --video-only --format json
  hashscout scan ~/Downloads --min-size 10MB --exclude "*.tmp" --quick
  hashscout clean ~/Videos --strategy keep-newest --trash --apply
  hashscout clean ~/Videos --interactive --trash --apply
  hashscout export ~/Videos --output report.csv
        """,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("-p", "--path", default=".", help="Target directory")
    parser.add_argument("--workers", type=int, default=4, help="Hashing threads (default: 4)")
    parser.add_argument("--video-only", action="store_true", help="Scan only video files")
    parser.add_argument("--min-size", type=str, default="", help="Minimum file size (e.g., 10MB, 1GB)")
    parser.add_argument("--max-size", type=str, default="", help="Maximum file size")
    parser.add_argument("--exclude", action="append", help="Glob ignore pattern (repeatable)")
    parser.add_argument("--quick", action="store_true", help="Skip full hash (faster, less precise)")
    parser.add_argument("--format", choices=["table", "detailed", "json", "csv"], default="detailed",
                        help="Output format (default: detailed)")

    sub = parser.add_subparsers(dest="command", help="Commands")

    # scan
    scan_p = sub.add_parser("scan", help="Scan and report duplicates")

    # clean
    clean_p = sub.add_parser("clean", help="Auto or interactive cleanup")
    clean_p.add_argument("--strategy", choices=[s.value for s in KeepStrategy], default="manual",
                         help="Auto-keep strategy (default: manual/interactive)")
    clean_p.add_argument("--interactive", action="store_true", help="Force interactive mode")
    clean_p.add_argument("--trash", action="store_true", help="Move to trash instead of permanent delete")
    clean_p.add_argument("--apply", action="store_true", help="Actually delete (default is dry-run)")

    # export
    exp_p = sub.add_parser("export", help="Export scan report to file")
    exp_p.add_argument("--output", "-o", required=True, help="Output file path (.json or .csv)")

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = create_parser()
    args = parser.parse_args()

    if not args.command:
        print(BANNER)
        parser.print_help()
        sys.exit(0)

    target = Path(args.path).expanduser().resolve()
    if not target.exists() or not target.is_dir():
        print(f"[!] Invalid directory: {target}")
        sys.exit(1)

    min_sz = parse_size(args.min_size) if args.min_size else 0
    max_sz = parse_size(args.max_size) if args.max_size else 0

    core = HashScoutCore(
        target_dir=target,
        video_only=args.video_only,
        min_size=min_sz,
        max_size=max_sz,
        exclude_patterns=args.exclude,
        workers=args.workers,
        quick_mode=args.quick,
    )

    # Discovery
    print(f"[*] Scanning: {target}")
    files = core.discover()
    if not files:
        print("[!] No files matched your criteria.")
        sys.exit(0)

    # Analysis
    infos = core.analyze_files(files)
    groups = core.find_duplicates(infos)

    formatter = OutputFormatter()

    if args.command == "scan":
        if args.format == "table":
            print(formatter.table(groups))
        elif args.format == "detailed":
            print(formatter.detailed(groups))
        elif args.format == "json":
            print(formatter.json_out(groups))
        elif args.format == "csv":
            print(formatter.csv_out(groups))
        sys.exit(1 if groups else 0)

    elif args.command == "export":
        out_path = Path(args.output)
        if out_path.suffix.lower() == ".json":
            out_path.write_text(formatter.json_out(groups), encoding="utf-8")
        elif out_path.suffix.lower() == ".csv":
            out_path.write_text(formatter.csv_out(groups), encoding="utf-8")
        else:
            out_path.write_text(formatter.detailed(groups), encoding="utf-8")
        print(f"[+] Report saved to: {out_path}")
        sys.exit(0)

    elif args.command == "clean":
        if not groups:
            print("[+] No duplicates found. Nothing to clean.")
            sys.exit(0)

        strategy = KeepStrategy(args.strategy)
        dry_run = not args.apply
        trash = args.trash

        if args.interactive or strategy == KeepStrategy.MANUAL:
            interactive_clean(groups, trash=trash, dry_run=dry_run)
        else:
            engine = CleanupEngine(strategy, trash=trash, dry_run=dry_run)
            print(f"[*] Auto-clean strategy: {strategy.value} | Trash: {trash} | Dry-run: {dry_run}")
            total_saved = 0
            for group in groups:
                keepers, deleters = engine.pick_keepers(group)
                if not keepers:
                    continue
                print(f"\n[Group] Keeping: {keepers[0].path.name}")
                results = engine.execute(deleters)
                for r in results:
                    print(f"  {r}")
                total_saved += sum(d.size for d in deleters)
            print(f"\n[+] Cleanup complete. Space {'saved' if not dry_run else 'to be saved'}: {format_size(total_saved)}")


if __name__ == "__main__":
    main()

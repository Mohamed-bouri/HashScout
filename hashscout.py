#!/usr/bin/env python3
"""
HashScout - Smart Video & File Duplicate Finder
Combines Bit-Exact SHA256 Hashing, Video Duration Analysis, Title Matching,
and an Interactive Deletion Manager.
"""

import os
import sys
import argparse
import hashlib
import subprocess
import json
from pathlib import Path
from difflib import SequenceMatcher
from datetime import datetime

# Common video extensions
VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.mpg', '.mpeg'}

BANNER = r"""
  _  _           _     ___cout 
 | || |__ _ ___| |_  / __| __ ___ _  _| |_ 
 | __ / _` (_-< ' \| (__| _|/ _ \ || |  _|
 |_||_\__,_/__/_||_|\___|_| \___/\_,_|\__|
   [Smart Video & Bit-Exact Duplicate Finder by Mohamed BOURI]
"""

def format_size(bytes_size: int) -> str:
    """Convert bytes to human-readable format."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_size < 1024:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.2f} PB"

def get_video_duration(file_path: Path) -> float | None:
    """Extract video duration in seconds using ffprobe if available."""
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(file_path)
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return None

def format_duration(seconds: float | None) -> str:
    """Format duration in HH:MM:SS format."""
    if seconds is None:
        return "N/A"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def get_file_hash(file_path: Path, partial: bool = False) -> str:
    """Calculate SHA256 hash. If partial=True, hashes first and last 64KB."""
    hasher = hashlib.sha256()
    size = file_path.stat().st_size
    chunk_size = 64 * 1024  # 64 KB

    with open(file_path, "rb") as f:
        if partial and size > (chunk_size * 2):
            hasher.update(f.read(chunk_size))
            f.seek(-chunk_size, os.SEEK_END)
            hasher.update(f.read(chunk_size))
        else:
            while chunk := f.read(chunk_size):
                hasher.update(chunk)

    return hasher.hexdigest()

def title_similarity(name1: str, name2: str) -> float:
    """Calculate string similarity ratio between two file titles."""
    return SequenceMatcher(None, name1.lower(), name2.lower()).ratio()

def scan_directory(target_dir: Path, video_only: bool = False) -> list[Path]:
    """Recursively collect files from target directory."""
    files = []
    for path in target_dir.rglob("*"):
        if path.is_file() and not path.is_symlink():
            if video_only and path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            files.append(path)
    return files

def find_duplicates(files: list[Path], fuzzy_titles: bool = False) -> list[list[Path]]:
    """Pipeline to detect duplicates using Size -> Partial Hash -> Full Hash / Duration."""
    print(f"[*] Total files scanned: {len(files)}")

    # Stage 1: Group by File Size
    size_groups: dict[int, list[Path]] = {}
    for f in files:
        sz = f.stat().st_size
        size_groups.setdefault(sz, []).append(f)

    candidate_groups = [group for group in size_groups.values() if len(group) > 1]
    print(f"[*] Stage 1 (Size Check): Found {len(candidate_groups)} potential duplicate group(s).")

    if not candidate_groups:
        return []

    # Stage 2: Partial Hash (Head/Tail)
    partial_groups: dict[tuple[int, str], list[Path]] = {}
    for group in candidate_groups:
        for f in group:
            p_hash = get_file_hash(f, partial=True)
            partial_groups.setdefault((f.stat().st_size, p_hash), []).append(f)

    stage2_candidates = [group for group in partial_groups.values() if len(group) > 1]
    print(f"[*] Stage 2 (Partial Hash): Narrowed to {len(stage2_candidates)} group(s).")

    # Stage 3: Full SHA-256 Bit-Exact Verification
    exact_duplicates: dict[str, list[Path]] = {}
    for group in stage2_candidates:
        for f in group:
            f_hash = get_file_hash(f, partial=False)
            exact_duplicates.setdefault(f_hash, []).append(f)

    final_groups = [group for group in exact_duplicates.values() if len(group) > 1]
    print(f"[*] Stage 3 (Full Hash): Confirmed {len(final_groups)} bit-exact duplicate group(s).\n")

    return final_groups

def interactive_delete(duplicate_groups: list[list[Path]], dry_run: bool = True) -> None:
    """Interactive loop asking user which files to delete/keep."""
    if not duplicate_groups:
        print("[+] No duplicates found!")
        return

    total_wasted_bytes = 0
    for group in duplicate_groups:
        single_size = group[0].stat().st_size
        total_wasted_bytes += single_size * (len(group) - 1)

    print("=" * 75)
    print(f" SUMMARY: {len(duplicate_groups)} duplicate sets found.")
    print(f" WASTED SPACE: ~{format_size(total_wasted_bytes)}")
    print("=" * 75)

    if dry_run:
        print("\n [DRY RUN MODE] - No files will be deleted on disk.")
        print(" Pass `--apply` or `-a` to enable deletion.\n")

    for group_idx, group in enumerate(duplicate_groups, start=1):
        print(f"\n--- [ Group {group_idx} / {len(duplicate_groups)} ] ---")
        print(f"File Size: {format_size(group[0].stat().st_size)}")

        for i, file in enumerate(group, start=1):
            duration = get_video_duration(file) if file.suffix.lower() in VIDEO_EXTENSIONS else None
            dur_str = f" | Duration: {format_duration(duration)}" if duration else ""
            mod_time = datetime.fromtimestamp(file.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")

            print(f"  [{i}] {file.name}")
            print(f"      Path: {file}")
            print(f"      Modified: {mod_time}{dur_str}")

        if dry_run:
            continue

        print("\nOptions:")
        print("  Enter the number [1..N] of the file you want to KEEP (others will be deleted).")
        print("  Type 'k' or 's' to SKIP this group.")
        print("  Type 'q' to QUIT execution.")

        while True:
            choice = input("\nSelect file to KEEP > ").strip().lower()

            if choice in ['s', 'k', '']:
                print("[*] Skipped group.")
                break
            elif choice == 'q':
                print("[!] Exiting duplicate manager.")
                return
            elif choice.isdigit():
                keep_idx = int(choice) - 1
                if 0 <= keep_idx < len(group):
                    file_to_keep = group[keep_idx]
                    files_to_delete = [f for idx, f in enumerate(group) if idx != keep_idx]

                    print(f"\n[+] Keeping: {file_to_keep.name}")
                    for f in files_to_delete:
                        try:
                            f.unlink()
                            print(f"  [DELETED] {f}")
                        except Exception as e:
                            print(f"  [ERROR] Could not delete {f}: {e}")
                    break
                else:
                    print("[!] Invalid index number.")
            else:
                print("[!] Invalid input. Try again.")

def main():
    print(BANNER)
    parser = argparse.ArgumentParser(description="HashScout - Video & Bit-Exact Duplicate Finder")
    parser.add_argument("-p", "--path", required=True, help="Target directory to scan")
    parser.add_argument("-v", "--video-only", action="store_true", help="Scan only video formats")
    parser.add_argument("-a", "--apply", action="store_true", help="Enable interactive deletion mode")

    args = parser.parse_args()

    target_path = Path(args.path).expanduser().resolve()
    if not target_path.exists() or not target_path.is_dir():
        print(f"[!] Error: Directory '{target_path}' does not exist.")
        sys.exit(1)

    all_files = scan_directory(target_path, video_only=args.video_only)
    if not all_files:
        print("[!] No matching files found.")
        return

    duplicates = find_duplicates(all_files)
    interactive_delete(duplicates, dry_run=not args.apply)

if __name__ == "__main__":
    main()

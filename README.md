# HashScout v2.2.0

```
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
```
# HashScout

**Advanced Duplicate File & Video Finder** — multi-drive scanning, cross-volume
deduplication, smart auto-cleanup, forensic reporting, and an interactive shell.

HashScout finds duplicate files by content (not filename), across multiple
drives/folders at once, and can also spot videos that are the **same content
saved at a different size** (re-encoded, trimmed, different bitrate or
resolution) using duration + perceptual frame matching.

---

## Table of Contents

- [Requirements](#requirements)
- [Installation](#installation)
  - [1. Python](#1-python)
  - [2. FFmpeg / FFprobe](#2-ffmpeg--ffprobe)
  - [3. Python packages](#3-python-packages)
  - [Verify everything is installed](#verify-everything-is-installed)
- [Quick Start](#quick-start)
- [Two ways to use HashScout](#two-ways-to-use-hashscout)
- [Command Reference (Interactive Shell)](#command-reference-interactive-shell)
- [Command Reference (Direct CLI)](#command-reference-direct-cli)
- [How duplicate detection works](#how-duplicate-detection-works)
- [Common workflows](#common-workflows)
- [Troubleshooting](#troubleshooting)
- [Safety notes](#safety-notes)
- [Q?](#Q?)
- [liecence](#License)
---

## Requirements

| Tool | Why it's needed | Required? |
|---|---|---|
| Python 3.9+ | Runs the script | Yes |
| `ffprobe` | Reads video duration | Yes, for video features |
| `ffmpeg` | Extracts frames for fuzzy video matching | Only for `--fuzzy` / `fuzzy` |
| `Pillow` (pip) | Opens extracted frames | Only for `--fuzzy` / `fuzzy` |
| `imagehash` (pip) | Computes perceptual frame hashes | Only for `--fuzzy` / `fuzzy` |
| `send2trash` (pip) | Sends deleted files to Recycle Bin/Trash instead of permanent delete | Optional but recommended |

If you only care about exact duplicate files (documents, photos, archives,
etc.) and don't need the fuzzy video matching, you can skip `ffmpeg`,
`Pillow`, and `imagehash` — the script degrades gracefully and just tells you
those features aren't available.

---

## Installation
-git clone https://github.com/Mohamed-bouri/hashscout.git
cd hashscout
python hashscout.py --help

### 1. Python

Check what you have:

```bash
python3 --version    # Linux/macOS
python --version     # Windows PowerShell
```

You need 3.9 or newer.

**Windows** — install from [python.org](https://www.python.org/downloads/)
(check "Add python.exe to PATH" during setup) or via `winget`:

```powershell
winget install Python.Python.3.12
```

**Linux (Debian/Ubuntu)**

```bash
sudo apt update
sudo apt install python3 python3-pip
```

**Linux (Fedora)**

```bash
sudo dnf install python3 python3-pip
```

**Linux (Arch)**

```bash
sudo pacman -S python python-pip
```

---

### 2. FFmpeg / FFprobe

`ffprobe` ships together with `ffmpeg` in every package below — installing
one installs both.

**Windows — winget (recommended, built into Windows 10/11)**

```powershell
winget install ffmpeg
```

Close and reopen PowerShell afterward so your `PATH` refreshes.

**Windows — Chocolatey** (if you already use it)

```powershell
choco install ffmpeg
```

**Windows — Scoop** (if you already use it)

```powershell
scoop install ffmpeg
```

**Windows — Manual install**

1. Download a build from [gyan.dev's FFmpeg builds](https://www.gyan.dev/ffmpeg/builds/)
   (the "essentials" or "full" zip).
2. Extract it to somewhere like `C:\ffmpeg`.
3. Add `C:\ffmpeg\bin` to your `PATH`:
   *Start → "Edit the system environment variables" → Environment Variables
   → select `Path` under User variables → New → `C:\ffmpeg\bin` → OK.*
4. Restart PowerShell.

**Linux (Debian/Ubuntu)**

```bash
sudo apt update
sudo apt install ffmpeg
```

**Linux (Fedora)**

```bash
sudo dnf install ffmpeg
```
(If it's not in the default repos, enable [RPM Fusion](https://rpmfusion.org/) first.)

**Linux (Arch)**

```bash
sudo pacman -S ffmpeg
```

**macOS (Homebrew)**

```bash
brew install ffmpeg
```

---

### 3. Python packages

Install the packages HashScout uses:

```bash
pip install Pillow imagehash send2trash
```

On Linux, if `pip` complains about an "externally managed environment", use:

```bash
pip install --break-system-packages Pillow imagehash send2trash
```

or install into a virtual environment instead:

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install Pillow imagehash send2trash
```

- **Pillow + imagehash** — required only for `--fuzzy` video matching.
- **send2trash** — optional. Without it, HashScout falls back to permanent
  delete when you don't pass `--no-trash`.

---

### Verify everything is installed

```bash
python3 --version
ffmpeg -version
ffprobe -version
python3 -c "import PIL, imagehash; print('Pillow + imagehash OK')"
```

(On Windows, use `python` instead of `python3` if that's how it's aliased on
your system.)

---

## Quick Start

```bash
# Launch the interactive shell
python3 hashscout.py

# Or scan directly from the command line
python3 hashscout.py scan ~/Downloads ~/Videos --video-only
```

---

## Two ways to use HashScout

**1. Interactive shell** — run `python3 hashscout.py` with no arguments to
drop into a `HashScout>` prompt where you can `scan`, `fuzzy`, `clean`,
`export`, etc. across multiple commands without re-scanning each time.

**2. Direct CLI mode** — run `python3 hashscout.py <command> <paths> [options]`
for one-off scans, e.g. from scripts or scheduled tasks.

Both modes support the same core options; the interactive shell additionally
remembers your last scan so you can run `fuzzy`, `clean`, or `export` against
it without re-scanning the disk.

---

## Command Reference (Interactive Shell)

Start the shell:

```bash
python3 hashscout.py
```

### `scan`

```
scan <path1> [path2] [path3] ... [options]
```

Scans one or more directories/drives and reports exact duplicate files
(grouped by content hash, not filename).

| Option | Description |
|---|---|
| `--video`, `-v` | Only scan video files |
| `--quick`, `-q` | Skip full-file hash verification (faster, slightly less certain) |
| `--workers N`, `-w N` | Number of hashing threads (default: 4) |
| `--min-size 10MB` | Skip files smaller than this |
| `--max-size 1GB` | Skip files larger than this |
| `--fuzzy` | Also find videos that are the **same content but a different size** (see [How duplicate detection works](#how-duplicate-detection-works)) |
| `--fuzzy-tolerance N` | Max duration difference in seconds to consider (default: `2`) |
| `--fuzzy-frames N` | Frames sampled per video for fingerprinting (default: `5`) |
| `--fuzzy-threshold N` | Match strictness, 0–64, lower = stricter (default: `10`) |

Examples:

```
scan C:\Users\Admin\Pictures
scan C:\ D:\ E:\ --video
scan ~/Downloads ~/Videos ~/Backups --workers 8
scan ~/Videos --video --fuzzy
```

### `fuzzy`

```
fuzzy [--tolerance 2] [--frames 5] [--threshold 10]
```

Re-runs fuzzy video matching against the results of your **last `scan`**,
without touching the disk again — handy for tuning the tolerance/threshold
without waiting through a full re-scan. Requires `scan` to have been run
first in this session.

### `clean`

```
clean [--strategy <name>] [--trash] [--no-trash] [--apply] [--interactive]
```

Cleans up the duplicate groups from your last `scan`. **Only acts on exact
(hash-based) duplicates — fuzzy video matches are never touched by `clean`.**

| Strategy | Keeps |
|---|---|
| `manual` (default) | Prompts you to pick which file to keep, per group |
| `newest` | Most recently modified file |
| `oldest` | Oldest file |
| `largest` | Largest file |
| `smallest` | Smallest file |
| `shortest` | Shortest video duration |
| `longest` | Longest video duration |
| `first` | First file found |
| `spread` | One copy per drive (deletes extra copies on the same drive) |

| Option | Description |
|---|---|
| `--trash` | Move deleted files to Recycle Bin/Trash (default) |
| `--no-trash` | Permanently delete instead |
| `--apply` | Actually delete files. **Without this, it's a dry-run** that only prints what *would* happen |
| `--interactive`, `-i` | Force the manual picker even with a non-manual strategy |

Examples:

```
clean --strategy spread --trash --apply
clean --strategy largest --apply
clean                       # dry-run, manual picker
```

### `export`

```
export <filename.json|csv|txt>
```

Exports the last scan's exact-duplicate results to a file. Format is chosen
by the file extension.

### Other commands

| Command | Description |
|---|---|
| `cd <path>` | Change the default scan directory |
| `pwd` | Show the current default directory |
| `exclude <pattern>` | Add a glob ignore pattern (e.g. `exclude *.tmp`) |
| `exclude --list` | List active ignore patterns |
| `exclude --clear` | Clear all ignore patterns |
| `workers <N>` | Set the default thread count for future scans |
| `clear` | Clear the terminal screen |
| `help` | Show the full command list |
| `exit` / `quit` | Leave HashScout |

---

## Command Reference (Direct CLI)

```
python3 hashscout.py [paths...] <command> [options]
```

### Global options (apply to `scan`, `clean`, `export`)

| Option | Description |
|---|---|
| `--workers N` | Hashing threads (default: 4) |
| `--video-only` | Scan only video files |
| `--min-size SIZE` | Minimum file size (e.g. `10MB`, `1GB`) |
| `--max-size SIZE` | Maximum file size |
| `--exclude PATTERN` | Glob ignore pattern (repeatable) |
| `--quick` | Skip full-file hash verification |
| `--fuzzy` | Also find same-content/different-size video duplicates |
| `--fuzzy-tolerance N` | Max duration difference in seconds (default: `2`) |
| `--fuzzy-frames N` | Frames sampled per video (default: `5`) |
| `--fuzzy-threshold N` | Match strictness, 0–64 (default: `10`) |
| `--format {table,detailed,json,csv}` | Output format for `scan` (default: `detailed`) |
| `--version` | Show version and exit |

### Subcommands

```
hashscout scan                          # scan and print duplicates
hashscout clean --strategy <name>       # auto or interactive cleanup
hashscout export -o report.json         # export scan results
```

`clean` subcommand options: `--strategy`, `--interactive`, `--trash`,
`--no-trash`, `--apply` (same meaning as the shell's `clean` command above).

`export` subcommand options: `-o` / `--output <file>` (required).

### Examples

```bash
# Scan and print a table
python3 hashscout.py ~/Downloads scan --format table

# Scan two drives for videos only, including fuzzy matches
python3 hashscout.py C:\ D:\ scan --video-only --fuzzy

# Auto-clean, keeping one copy per drive, actually delete (to trash)
python3 hashscout.py D:\Backups clean --strategy spread --apply

# Export a JSON report without deleting anything
python3 hashscout.py ~/Videos export -o report.json
```

---

## How duplicate detection works

**Exact duplicates (default, always on):**
1. Files are grouped by size.
2. Files sharing a size get a fast *partial* hash (first + last 64KB).
3. Files that still collide get a *full* SHA-256 hash to confirm.

This only matches files that are byte-for-byte identical — a re-encoded or
resized copy of a video will **not** match, even though it's "the same video".

**Fuzzy video duplicates (opt-in via `--fuzzy` / `fuzzy`):**
1. Video files are bucketed by duration — only videos within
   `--fuzzy-tolerance` seconds of each other are compared further.
2. For each candidate, `ffmpeg` extracts a handful of evenly-spaced frames
   (`--fuzzy-frames`), skipping a small margin at the start/end to avoid
   intro logos or black frames.
3. Each frame gets a perceptual hash (`imagehash.phash`) that's robust to
   resolution, bitrate, and format changes.
4. Videos whose average frame-hash distance is below `--fuzzy-threshold` are
   grouped together with a similarity percentage.

This is **probabilistic, not exact** — always review a fuzzy group before
deleting anything. That's also why `clean` never auto-deletes fuzzy matches.

Tuning tips:
- Getting false positives (unrelated clips grouped together)? Lower
  `--fuzzy-threshold` (stricter) or `--fuzzy-tolerance`.
- Missing matches you know exist? Raise `--fuzzy-threshold`, or raise
  `--fuzzy-frames` for more accuracy (at the cost of speed).
- Videos with different intros/outros (e.g. trimmed differently) may not
  match even with the same core content, since frames are sampled at
  proportional timestamps.

---

## Common workflows

**Find and review exact duplicates in Downloads:**
```
scan ~/Downloads
```

**Find duplicate videos across three drives, including re-encoded copies:**
```
scan C:\ D:\ E:\ --video --fuzzy
```

**Free up space automatically, one copy per drive, straight to Trash:**
```
scan D:\Media
clean --strategy spread --trash --apply
```

**Just get a report, don't delete anything:**
```
scan ~/Photos
export ~/Desktop/duplicates.json
```

---

## Troubleshooting

**`ffmpeg is not recognized as an internal or external command` (Windows)**
`ffmpeg`/`ffprobe` isn't installed or isn't on your `PATH`. See
[FFmpeg / FFprobe](#2-ffmpeg--ffprobe) above. After installing, close and
reopen PowerShell.

**`Fuzzy video matching needs 'Pillow' and 'imagehash'`**
```bash
pip install Pillow imagehash
```

**`Fuzzy video matching needs full ffmpeg (frame extraction), not just ffprobe`**
You have `ffprobe` but not `ffmpeg` on `PATH`. They ship together in every
package above — reinstall via one of the methods in
[FFmpeg / FFprobe](#2-ffmpeg--ffprobe).

**`pip: error: externally-managed-environment` (Linux)**
```bash
pip install --break-system-packages Pillow imagehash send2trash
```
or use a virtual environment (see [Python packages](#3-python-packages)).

**`SyntaxError: (unicode error) 'unicodeescape' codec can't decode bytes...`**
This was a bug in older copies of `hashscout.py` where a Windows example
path (`C:\Users\...`) sat inside a non-raw docstring, and Python tried to
parse `\U` as a Unicode escape. Fixed in current versions — make sure you're
running the latest `hashscout.py`.

**Scan finds nothing / "No files matched"**
Check `--min-size` / `--max-size` aren't excluding everything, and that the
path(s) you passed actually exist and are directories.

**Permission errors during scan**
HashScout skips unreadable directories and reports how many were skipped.
Run as Administrator (Windows) or with appropriate permissions (Linux) if
you need to scan protected locations.

---

## Safety notes

- `clean` defaults to **dry-run** — nothing is deleted unless you pass `--apply`.
- Deletions go to Trash/Recycle Bin by default (`--trash`); pass `--no-trash`
  for permanent deletion.
- `strategy manual` (the default) always asks you which file to keep, per group.
- Fuzzy video matches are **never** touched by `clean` — they require manual
  review and deletion.
  ---
  
##  Q?
- any questions contact me :) contact@mbeffects.com

##  License

Distributed under the **MIT License**. See `LICENSE` for details.

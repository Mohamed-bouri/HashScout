# HashScout v2.0

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

**Advanced Duplicate File & Video Finder with Smart Auto-Cleanup**

HashScout finds duplicate files using bit-exact SHA-256 hashing, analyzes video durations, and cleans them up using intelligent strategies — or lets you decide interactively. Built for photographers, video editors, system administrators, and anyone drowning in duplicate data.

---

## ⚠️ Safety Notice

> **Deletion is opt-in.** HashScout runs in **dry-run mode by default**. You must explicitly pass `--apply` to delete anything. When possible, files are moved to trash instead of permanently deleted.

---

## Features

| Feature | Description |
|---------|-------------|
| **Bit-Exact SHA-256** | Full cryptographic hashing with optional quick partial-hash mode |
| **3-Stage Pipeline** | Size → Partial Hash → Full Hash for speed and accuracy |
| **Multi-Threaded** | Parallel hashing with live progress indicator |
| **Video-Aware** | Extracts duration via `ffprobe` for video duplicates |
| **Smart Auto-Cleanup** | 7 keep strategies: newest, oldest, largest, smallest, shortest, longest, first |
| **Trash-Safe Deletion** | Uses `send2trash` when available; falls back to permanent delete |
| **Dry-Run by Default** | Preview every action before executing (`--apply` required) |
| **Size Filters** | `--min-size` and `--max-size` to target specific file ranges |
| **Glob Exclusions** | `--exclude "*.tmp"` to skip temp files, caches, etc. |
| **Multi-Format Output** | `detailed` (human), `table` (compact), `json` (automation), `csv` (reports) |
| **Export Reports** | Save scan results to `.json` or `.csv` without deleting |
| **CI/CD Exit Codes** | Returns `1` if duplicates found, `0` if clean |

---

## Installation

### From Source

```bash
git clone https://github.com/Mohamed-bouri/hashscout.git
cd hashscout
python hashscout_v2.py --help
```

### Install as CLI Command

```bash
pip install -e .
```

This installs `hashscout` as a system command:

```bash
hashscout --version
```

### Requirements

- **Python 3.8+** (standard library only)
- Optional: `send2trash` for trash-aware deletion
  ```bash
  pip install send2trash
  ```
- Optional: `ffprobe` (part of FFmpeg) for video duration analysis
  ```bash
  # Ubuntu/Debian
  sudo apt install ffmpeg

  # macOS
  brew install ffmpeg

  # Windows
  choco install ffmpeg
  ```

---

## Quick Start

### 1. Scan a Directory

```bash
hashscout scan ~/Downloads
```

**Example Output:**
```
[*] Scanning: /home/user/Downloads
[*] Analyzing 3,421 files using 4 workers...
    Progress: 3421/3421 files...
[*] Full-hash verification stage...
[+] Analysis complete in 4.2s

--- [ Group 1 / 12 ] ---
Hash: a3f5b7e2... | Size each: 15.50 MB | Wasted: 31.00 MB
  [1] vacation_pic.jpg
      Path: /home/user/Downloads/vacation_pic.jpg
      Modified: 2026-07-15 14:30
  [2] vacation_pic_copy.jpg
      Path: /home/user/Downloads/backup/vacation_pic_copy.jpg
      Modified: 2026-07-20 09:15
  [3] IMG_4521.jpg
      Path: /home/user/Downloads/camera/IMG_4521.jpg
      Modified: 2026-07-15 14:30

--- [ Group 2 / 12 ] ---
Hash: 9c8d2e1a... | Size each: 1.20 GB | Wasted: 1.20 GB
  [1] movie_2024.mkv
      Path: /home/user/Downloads/movie_2024.mkv
      Modified: 2026-06-01 22:00 | 02:15:30
  [2] movie_2024 (1).mkv
      Path: /home/user/Downloads/movie_2024 (1).mkv
      Modified: 2026-06-01 22:00 | 02:15:30

============================================================
Total wasted space: 4.85 GB
```

### 2. Scan Videos Only

```bash
hashscout scan ~/Videos --video-only --format table
```

**Example Output:**
```
GROUP    FILES    SIZE EACH      WASTED         SAMPLE PATH
------------------------------------------------------------------------------------------
1        3        1.20 GB        2.40 GB        /home/user/Videos/movie_2024.mkv
2        2        450.00 MB      450.00 MB      /home/user/Videos/clip_01.mp4
3        4        25.00 MB       75.00 MB       /home/user/Videos/thumb_01.jpg

Total groups: 3 | Total wasted space: 2.93 GB
```

### 3. Quick Scan (Partial Hash Only)

```bash
hashscout scan ~/Downloads --quick --format json
```

> `--quick` skips full hashing. Faster but slightly less precise. Good for first-pass scans.

### 4. Auto-Clean: Keep Newest, Trash Rest

```bash
hashscout clean ~/Downloads --strategy keep-newest --trash --apply
```

**Example Output:**
```
[*] Auto-clean strategy: newest | Trash: True | Dry-run: False

[Group] Keeping: vacation_pic.jpg
  [TRASHED] /home/user/Downloads/backup/vacation_pic_copy.jpg
  [TRASHED] /home/user/Downloads/camera/IMG_4521.jpg

[Group] Keeping: movie_2024.mkv
  [TRASHED] /home/user/Downloads/movie_2024 (1).mkv

[+] Cleanup complete. Space saved: 4.85 GB
```

### 5. Auto-Clean Videos: Keep Shortest Duration

```bash
hashscout clean ~/Videos --video-only --strategy keep-shortest --trash --apply
```

### 6. Interactive Manual Cleanup

```bash
hashscout clean ~/Downloads --interactive --trash --apply
```

**Example Interaction:**
```
--- Group 1/12 | Wasted: 31.00 MB ---
  [1] vacation_pic.jpg (2026-07-15 14:30)
  [2] vacation_pic_copy.jpg (2026-07-20 09:15)
  [3] IMG_4521.jpg (2026-07-15 14:30)

Select: number=KEEP that file (delete rest) | s=skip | q=quit
Keep > 1
[+] Keeping: vacation_pic.jpg
    [TRASHED] /home/user/Downloads/backup/vacation_pic_copy.jpg
    [TRASHED] /home/user/Downloads/camera/IMG_4521.jpg
```

### 7. Export Report Without Deleting

```bash
hashscout export ~/Downloads --output duplicates.json
hashscout export ~/Videos --output report.csv
```

### 8. Filter by Size

```bash
# Only files larger than 10MB
hashscout scan ~/Downloads --min-size 10MB

# Only files between 1MB and 100MB
hashscout scan ~/Downloads --min-size 1MB --max-size 100MB
```

### 9. Exclude Patterns

```bash
hashscout scan ~/Downloads --exclude "*.tmp" --exclude "*.part" --exclude "node_modules/*"
```

---

## Commands Reference

### `scan`

Scan a directory and report duplicates.

```bash
hashscout scan ~/Downloads
hashscout scan ~/Videos --video-only --format json
hashscout scan ~/Downloads --quick --min-size 5MB --exclude "*.log"
hashscout scan . --workers 8 --format csv > report.csv
```

**Options:**
- `--video-only` — Scan only video files
- `--min-size` — Minimum file size (e.g., `10MB`, `1GB`)
- `--max-size` — Maximum file size
- `--exclude` — Glob ignore pattern (repeatable)
- `--quick` — Skip full hash verification (faster)
- `--workers` — Hashing threads (default: 4)
- `--format` — Output: `detailed` (default), `table`, `json`, `csv`

**Exit Codes:**
| Code | Meaning |
|------|---------|
| `0` | No duplicates found |
| `1` | Duplicates found |

---

### `clean`

Clean up duplicates using auto-strategies or interactive mode.

```bash
hashscout clean ~/Downloads --strategy keep-newest --trash --apply
hashscout clean ~/Videos --video-only --strategy keep-shortest --apply
hashscout clean ~/Downloads --interactive --trash --apply
```

**Options:**
- `--strategy` — Auto-keep strategy (default: `manual`)
- `--interactive` — Force interactive picker
- `--trash` — Move to trash instead of permanent delete
- `--apply` — **Required.** Actually execute deletions

> ⚠️ Without `--apply`, `clean` runs in dry-run mode and only previews what it would do.

**Strategies:**

| Strategy | Keeps | Best For |
|----------|-------|----------|
| `keep-newest` | Most recently modified | Keeping latest versions |
| `keep-oldest` | Earliest modified | Preserving originals |
| `keep-largest` | Biggest file | Keeping highest quality |
| `keep-smallest` | Smallest file | Saving disk space |
| `keep-shortest` | Shortest video | Removing long duplicates |
| `keep-longest` | Longest video | Keeping full versions |
| `keep-first` | First found | Fastest cleanup |
| `manual` | You pick | Full control |

---

### `export`

Export scan results to a file without deleting anything.

```bash
hashscout export ~/Downloads --output report.json
hashscout export ~/Videos --output duplicates.csv
```

**Options:**
- `--output, -o` — Output file path (`.json` or `.csv`)

---

## Global Flags

| Flag | Description |
|------|-------------|
| `--version` | Show version number |
| `-p, --path` | Target directory (default: current directory) |
| `--workers` | Number of hashing threads (default: 4) |
| `--video-only` | Scan only video files |
| `--min-size` | Minimum file size filter |
| `--max-size` | Maximum file size filter |
| `--exclude` | Glob ignore pattern (repeatable) |
| `--quick` | Skip full hash verification |
| `--format` | Output format: `detailed`, `table`, `json`, `csv` |
| `-h, --help` | Show help message |

---

## Output Formats

### Detailed (Default)
Full human-readable report with file paths, modification times, and video durations.

```bash
hashscout scan ~/Videos
```

### Table
Compact summary table. Best for quick overviews.

```bash
hashscout scan ~/Downloads --format table
```

### JSON
Machine-parseable with full forensic detail. Best for automation and scripting.

```bash
hashscout scan ~/Videos --format json | jq '.groups[0].files[0].path'
```

### CSV
Spreadsheet-friendly. Best for reports and documentation.

```bash
hashscout scan ~/Downloads --format csv > duplicates.csv
```

---

## How It Works

HashScout uses a **3-stage deduplication pipeline** for efficiency:

```
Stage 1: Size Grouping
    Group files by exact byte size.
    Eliminates ~95% of files immediately.

Stage 2: Partial Hash (Head + Tail)
    Hash first 64KB and last 64KB of each file.
    Catches most duplicates without reading entire files.

Stage 3: Full SHA-256 Hash
    Bit-exact verification of remaining candidates.
    100% accuracy guarantee.
```

With `--quick`, Stage 3 is skipped for maximum speed.

---

## Deletion Safety

| Mode | Behavior |
|------|----------|
| **Default (no `--apply`)** | Dry-run only. Nothing is deleted. |
| `--apply` without `--trash`** | Permanently deletes files. |
| `--apply --trash`** | Moves to system trash (Recycle Bin). |

**Trash support requires:**
```bash
pip install send2trash
```

If `send2trash` is not installed, HashScout warns you and falls back to permanent deletion.

---

## Performance Tips

| Scenario | Recommendation |
|----------|---------------|
| Huge directories (100k+ files) | Use `--quick` for first pass |
| Slow HDD / network drives | Reduce `--workers 2` to avoid I/O thrashing |
| SSD / fast storage | Increase `--workers 8` or `--workers 16` |
| Only care about big files | Add `--min-size 10MB` |
| Video collections | Use `--video-only` to skip everything else |
| Automated scripts | Use `--format json` and check exit code |

---

## Video Duration Analysis

HashScout extracts video duration using `ffprobe` (from FFmpeg). This lets you:

- Sort duplicates by length (`keep-shortest`, `keep-longest`)
- See duration in scan reports
- Identify truncated or corrupted video copies

If `ffprobe` is not installed, duration shows as `N/A` and video strategies fall back to file metadata.

---

## Project Structure

```
hashscout/
├── hashscout_v2.py      # Main application
├── pyproject.toml       # Package configuration
├── requirements.txt     # Optional dependencies
├── README.md            # This file
└── tests/
    └── test_hashscout.py
```

---

## Comparison: HashScout v1 vs v2

| Capability | v1.0 | v2.0 |
|-----------|------|------|
| SHA-256 hashing | ✅ | ✅ |
| Multi-threading | ❌ | ✅ |
| Auto-cleanup strategies | ❌ | ✅ (7 strategies) |
| Trash-safe deletion | ❌ | ✅ |
| Dry-run by default | ❌ | ✅ |
| Size filters | ❌ | ✅ |
| Glob exclusions | ❌ | ✅ |
| JSON/CSV output | ❌ | ✅ |
| Export command | ❌ | ✅ |
| CI/CD exit codes | ❌ | ✅ |
| Progress indicator | ❌ | ✅ |

---

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/awesome-feature`)
3. Commit your changes (`git commit -m 'Add awesome feature'`)
4. Push to the branch (`git push origin feature/awesome-feature`)
5. Open a Pull Request

---

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

---

## Related Tools

- **[PortSpy](../portspy/)** — Network port & process auditor
- **[PyGuard](../pyguard/)** — File integrity monitor with forensic trails

---

<p align="center">
  <i>Find it. Know it. Clean it.</i>
</p>
# HashScout

```text
                                         
 __ __         _   _____             _   
|  |  |___ ___| |_|   __|___ ___ _ _| |_ 
|     | .'|_ -|   |__   |  _| . | | |  _|
|__|__|__,|___|_|_|_____|___|___|___|_|  
                                         
[Smart Video & Bit-Exact Duplicate Finder]
            BY Mohamed BOURI

```

> High-performance duplicate file finder tailored for media libraries, utilizing multi-stage SHA-256 hashing, video duration analysis, and an interactive CLI deletion manager.

---

##  Features

* **Multi-Stage Hashing Pipeline:** Extremely fast. Filters candidate files by byte size, partial head/tail hashes, and full SHA-256 bit-exact verification.
* **Video Duration Extraction:** Automatically queries video runtime via `ffprobe` to give clear context when selecting copies.
* **Interactive Deletion Prompt:** Choose which copy to keep per duplicate group with real-time feedback.
* **Safety First:** Default **Dry Run** mode ensures no files are removed without explicit `--apply` flags.

---

##  Deletion Is Permanent

HashScout deletes files with `os.unlink()` — there is **no trash/recycle bin, and no undo**. Always run without `--apply` first and read the preview carefully. For anything irreplaceable, keep a backup before you run deletion mode.

---

##  Requirements

* **Python 3.10+**
* **FFmpeg/FFprobe** *(Optional, used for extracting video runtimes)*:
* macOS: `brew install ffmpeg`
* Ubuntu/Debian: `sudo apt install ffmpeg`
* Windows: `winget install ffmpeg`

---

##  Quick Start

1. **Clone the repository:**
   ```bash
   git clone https://github.com/mohamed-bouri/hashscout.git
   cd hashscout
   ```

2. **Run a dry-run scan:**
   ```bash
   python3 hashscout.py -p ~/Videos --video-only
   ```

3. **Run interactive deletion mode:**
   ```bash
   python3 hashscout.py -p ~/Videos --video-only --apply
   ```

---

##  CLI Flags

| Short | Long | Description |
| --- | --- | --- |
| `-p` | `--path` | **(Required)** Path to directory to scan. |
| `-v` | `--video-only` | Restrict scan strictly to video extensions (`.mp4`, `.mkv`, `.mov`, etc.). |
| `-a` | `--apply` | Enables interactive prompt to select files for deletion. |

---

##  Example Terminal Output

```text
[*] Total files scanned: 1420
[*] Stage 1 (Size Check): Found 3 potential duplicate group(s).
[*] Stage 2 (Partial Hash): Narrowed to 2 group(s).
[*] Stage 3 (Full Hash): Confirmed 2 bit-exact duplicate group(s).

===========================================================================
 SUMMARY: 2 duplicate sets found.
 WASTED SPACE: ~1.42 GB
===========================================================================

--- [ Group 1 / 2 ] ---
File Size: 720.50 MB
  [1] sample_clip.mp4
      Path: /Users/username/Videos/sample_clip.mp4
      Modified: 2024-02-10 12:10:00 | Duration: 01:14:30
  [2] sample_clip_copy.mp4
      Path: /Users/username/Downloads/sample_clip_copy.mp4
      Modified: 2024-03-01 09:45:12 | Duration: 01:14:30

Select file to KEEP > 1

[+] Keeping: sample_clip.mp4
  [DELETED] /Users/username/Downloads/sample_clip_copy.mp4

```

---

## 📄 License

Distributed under the **MIT License**. See `LICENSE` for details.

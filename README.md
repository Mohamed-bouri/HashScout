

# HashScout

```text
                                         
 _____         _   _____             _   
|  |  |___ ___| |_|   __|___ ___ _ _| |_ 
|     | .'|_ -|   |__   |  _| . | | |  _|
|__|__|__,|___|_|_|_____|___|___|___|_|  
                                         
   [Smart Video & Bit-Exact Duplicate Finder]

```

> High-performance duplicate file finder tailored for media libraries, utilizing multi-stage SHA-256 hashing, video duration analysis, and an interactive CLI deletion manager.

---

## ⚡ Features

* **Multi-Stage Hashing Pipeline:** Extremely fast. Filters candidate files by byte size, partial head/tail hashes, and full SHA-256 bit-exact verification.
* **Video Duration Extraction:** Automatically queries video runtime via `ffprobe` to give clear context when selecting copies.
* **Interactive Deletion Prompt:** Choose which copy to keep per duplicate group with real-time feedback.
* **Safety First:** Default **Dry Run** mode ensures no files are removed without explicit `--apply` flags.

---

## 🛠️ Requirements

* **Python 3.10+**
* **FFmpeg/FFprobe** *(Optional, used for extracting video runtimes)*:
* macOS: `brew install ffmpeg`
* Ubuntu/Debian: `sudo apt install ffmpeg`
* Windows: `winget install ffmpeg`



---

## 🚀 Quick Start

1. **Clone the repository:**
```bash
git clone [https://github.com/your-username/hashscout.git](https://github.com/your-username/hashscout.git)
cd hashscout

```


2. **Run a Dry-Run Scan:**
```bash
python hashscout.py -p ~/Videos --video-only

```


3. **Run Interactive Deletion Mode:**
```bash
python hashscout.py -p ~/Videos --video-only --apply

```



---

## 📖 CLI Flags

| Short | Long | Description |
| --- | --- | --- |
| `-p` | `--path` | **(Required)** Path to directory to scan. |
| `-v` | `--video-only` | Restrict scan strictly to video extensions (`.mp4`, `.mkv`, `.mov`, etc.). |
| `-a` | `--apply` | Enables interactive prompt to select files for deletion. |

---

## 💻 Example Terminal Output

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

```

```

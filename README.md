# MULTI_DOWNLOADER

> **The ultimate all-in-one multi-platform media downloader.**  
> Seamlessly unifies [**`yt-dlp`**](https://github.com/yt-dlp/yt-dlp), [**`gallery-dl`**](https://github.com/mikf/gallery-dl), [**`terabox-dl`**](https://github.com/Alex-0099/Terabox-DL), [**`telegram-dl`**](https://github.com/Alex-0099/Telegram-dl), [**`cyberdrop-dl`**](https://github.com/Cyberdrop-DL/cyberdrop-dl), and [**`anypic-dl`**](https://github.com/Alex-0099) under a single command-line interface and a modern desktop GUI with smart auto-routing, automatic failover, and a shared deduplication archive.

![MULTI_DOWNLOADER Terminal UI](assets/terminal_preview.png)

---

## ✨ Features at a Glance

* **6 Powerful Engines in One**: Video streams, audio tracks, image galleries, cloud archives, Telegram chats, and web pages are all handled from a single interface.
* **Smart Automatic Routing**: Simply paste any link. MULTI_DOWNLOADER inspects the domain and dispatches it to the ideal engine automatically.
* **Automatic Failover**: If one scraper fails (e.g. anti-bot or dead extractor), the link automatically fails over to secondary engines without user intervention.
* **Dual Interfaces (CLI + GUI)**:
  * **Sleek Terminal UI**: Animated ANSI worker cards, real-time download speeds, dual-unit byte metrics, and stationary progress tracks.
  * **Desktop GUI Application**: Built with [Flet](https://flet.dev/), featuring dark-mode dashboards, live regex engine badges, interactive queue controls, archive browser, and visual settings.
* **Live Clipboard Auto-Watcher (`watch`)**: Copy any media link in your web browser or apps, and MULTI_DOWNLOADER automatically captures and downloads it in the background with audio chimes.
* **Unified SQLite Archive (`archive.db`)**: Never download duplicates. Every successful transfer is indexed so subsequent runs skip existing files in sub-millisecond time.
* **Multi-Stream Priority Queue**: Manage large batches, assign priority weights, limit per-engine and per-domain concurrent connections, and resume interrupted queues.
* **Isolated Configuration Sandboxes**: Each backend maintains its own isolated configuration profile, preventing collisions with global system tools.

---

## 🚀 Supported Media & Engines

| Engine | Supported Platforms & Media Types | Highlights |
| :--- | :--- | :--- |
| **`yt-dlp`** | YouTube, Twitch, TikTok, Vimeo, Twitter/X, SoundCloud, Reddit, 1000+ video & audio streaming sites | 4K/8K stream merging, PO-token sidecar, browser cookies, subtitle extraction |
| **`gallery-dl`** | Imgur, Reddit, Twitter/X, Instagram, Pixiv, Danbooru, ArtStation, Flickr, DeviantArt, 300+ image boards | High-speed batch image downloads, album metadata parsing, custom naming |
| **`terabox-dl`** | TeraBox cloud shares (`terabox.com`, `1024tera.com`, `teraboxlink.com`, `mirrobox.com`, etc.) | Direct link scraper, multi-chunk sequential streaming, HTTP Range resume |
| **`telegram-dl`** | Telegram public channels, private channels, groups, and topic forums (`t.me/...`) | Native Telethon MTProto client, interactive login wizard, session reuse |
| **`cyberdrop-dl`** | Bunkr, Pixeldrain, Catbox, Gofile, Saint.to, Mega.nz, forum aggregators, locker crawlers | Multi-threaded locker crawling, dynamic sandbox, automatic retry |
| **`anypic-dl`** | Any image-bearing webpage, personal blogs, direct picture links with no dedicated extractor | Smart full-resolution image discovery, static HTML & dynamic page parsing |

---

## 📋 Prerequisites & Dependencies

Before installing MULTI_DOWNLOADER, ensure you have the following installed on your system:

### 1. Python 3.10 or Higher
* Verify with:
  ```bash
  python --version
  ```

### 2. FFmpeg (Required for Video/Audio Merging)
FFmpeg is required by `yt-dlp` to combine high-definition video and audio streams (such as 1080p, 1440p, and 4K YouTube videos) into single MP4/MKV files.
* **Windows (via winget or Chocolatey)**:
  ```powershell
  winget install Gyan.FFmpeg
  # or
  choco install ffmpeg
  ```
* **macOS (via Homebrew)**:
  ```bash
  brew install ffmpeg
  ```
* **Linux (Ubuntu/Debian)**:
  ```bash
  sudo apt update && sudo apt install ffmpeg
  ```

### 3. Node.js (Optional, Recommended for YouTube)
Node.js enables the local Proof-of-Origin (PO) token sidecar to solve JavaScript challenges on YouTube videos seamlessly.
* Install from [nodejs.org](https://nodejs.org/) or via `winget install OpenJS.NodeJS`.

---

## 📥 Installation

```bash
# 1. Clone the repository
git clone https://github.com/Alex-0099/MULTI_DOWNLOADER.git
cd MULTI_DOWNLOADER

# 2. Create and activate a Python virtual environment
# Windows (PowerShell):
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Linux / macOS:
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt
```

Verify your installation:
```bash
python multi-dl.py --version
```

---

## 🎮 How to Run

### 1. Direct Downloads (CLI)
Download any video, gallery, cloud file, or chat message directly:
```bash
# Single video or stream
python multi-dl.py "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# Image gallery or album
python multi-dl.py "https://imgur.com/a/sample_album"

# TeraBox cloud share
python multi-dl.py "https://terabox.com/s/1abcdef..."

# Telegram post or media
python multi-dl.py "https://t.me/channel_name/123"

# Multiple links with 4 parallel worker streams
python multi-dl.py "URL_1" "URL_2" "URL_3" -c 4
```

> **Windows PowerShell Shortcut:** You can run `.\multi-dl "<URL>"` directly without typing `python multi-dl.py`.

---

### 2. Desktop GUI Application
If you prefer a visual interface, launch the dark-mode desktop GUI:
```powershell
# Windows launcher (batch or PowerShell):
.\multi-dl-gui.bat
# or
python multi-dl-gui.py
```
* **Features in GUI**: Live link analysis, preset quality filters (Extract MP3, Video Only, Images Only), progress cards with ETA, searchable SQLite archive, and one-click clipboard monitoring.

---

### 3. Interactive Shell (`multi-dl`)
Start an interactive command center by running MULTI_DOWNLOADER with no URL arguments:
```powershell
python multi-dl.py
```
Inside the interactive prompt, you can paste links or execute natural commands:
* `watch` — Turn on real-time clipboard monitoring.
* `retry` — Re-download failed links from the previous batch.
* `open` — Open the `downloads/` folder in your file explorer.
* `archive` — Display download history and storage statistics.
* `queue` — View queued tasks waiting to run.
* `config` — View active configuration and worker settings.
* `update` — Check and update all 6 download engines.
* `help` — Show command reference cheatsheet.
* `exit` — Exit the prompt.

---

### 4. Real-Time Clipboard Auto-Watcher
Monitor your system clipboard in the background. Simply copy any supported link in your browser or chat app, and MULTI_DOWNLOADER automatically picks it up, verifies it, and downloads it:
```bash
python multi-dl.py watch
```

---

### 5. Batch Downloads from a Text File
Create a text file (e.g. `links.txt`) with one URL per line:
```text
https://www.youtube.com/watch?v=...
https://imgur.com/a/...
https://terabox.com/s/...
```
Execute the batch immediately with parallel workers:
```bash
python multi-dl.py links.txt -c 4
```

---

### 6. Queue Management (Download Later)
Organize downloads into a persistent queue (`data/queue.json`) to execute when you are ready:
```bash
# Add links to the queue with priority
python multi-dl.py queue add links.txt
python multi-dl.py queue add "https://..." -p 10

# List queued items
python multi-dl.py queue list

# Start downloading the queue with 5 workers
python multi-dl.py queue start -c 5

# Retry failed queue items
python multi-dl.py queue retry

# Clear completed items
python multi-dl.py queue clear --status completed
```

---

## ⚙️ Configuration & Authentication

### Central Settings (`config.json`)
The primary configuration file resides at the project root. You can edit `config.json` directly or use the CLI:
```bash
# View all settings
python multi-dl.py config

# Change download destination directory
python multi-dl.py config set download_dir "D:/Media/Downloads"

# Change default concurrency limit
python multi-dl.py config set concurrent_downloads 4

# Toggle site/domain folder organization
python multi-dl.py config set organize_by_site true
```

| Setting | Default | Description |
| :--- | :--- | :--- |
| `download_dir` | `"downloads"` | Destination root folder for all downloaded files |
| `organize_by_backend` | `true` | Groups files by engine folder (`downloads/<engine>/`) |
| `organize_by_site` | `true` | Groups files by website host (`downloads/<engine>/<site>/`) |
| `concurrent_downloads` | `3` | Maximum simultaneous worker streams across the pool |
| `archive_enabled` | `true` | Enables automatic duplicate prevention via SQLite |

---

### Authentication Wizard
Configure accounts for services requiring login:
```bash
# Launch interactive wizard
python multi-dl.py auth

# Or configure a specific service:
python multi-dl.py auth telegram
python multi-dl.py auth terabox
```
* **Telegram**: Prompts for your `api_id`, `api_hash`, phone number, and 2FA password to generate a secure local session.
* **TeraBox**: Prompts for your `ndus` browser cookie to unlock high-speed sequential chunk streaming.

---

### Browser Cookies (Age-Gated & Member Media)
Pass session cookies from your web browser to download private or age-restricted media:
```bash
# Use cookies directly from installed browser:
python multi-dl.py "https://www.youtube.com/watch?v=..." --cookies-from-browser firefox
python multi-dl.py "https://www.youtube.com/watch?v=..." --cookies-from-browser chrome

# Or provide a Netscape-format cookies.txt file:
python multi-dl.py "https://example.com/media" --cookies "cookies.txt"
```

---

### Network Proxy Setup
Route all traffic through an HTTP or SOCKS5 proxy:
```bash
# Set a global proxy
python multi-dl.py proxy set "socks5://127.0.0.1:1080"
python multi-dl.py proxy set "http://user:password@proxy.example.com:8080"

# Test proxy connectivity
python multi-dl.py proxy test

# Disable proxy (revert to direct connection)
python multi-dl.py proxy off
```

---

### Managing the Deduplication Archive
MULTI_DOWNLOADER tracks downloaded media in `data/archive.db`. You can manage this database anytime:
```bash
# View archive statistics (total files, storage used, engine distribution)
python multi-dl.py archive --stats

# List recent download entries
python multi-dl.py archive --list --limit 25

# Search your download history
python multi-dl.py archive --search "nature timelapse"

# Force re-downloading a file even if already downloaded
python multi-dl.py "https://example.com/video.mp4" --force

# Download without recording in archive
python multi-dl.py "https://example.com/video.mp4" --no-archive
```

---

### Keeping Engines Updated
Keep `yt-dlp`, `gallery-dl`, and other dependencies up to date with the latest platform fixes:
```bash
# Update all download engines
python multi-dl.py update

# Update a specific engine
python multi-dl.py update yt-dlp
python multi-dl.py update gallery-dl
```

---

## 📖 Command Reference Cheat Sheet

| Command / Option | Description |
| :--- | :--- |
| `python multi-dl.py "<URL>"` | Download a single URL with automatic engine routing |
| `python multi-dl.py <file.txt>` | Immediately download all links from a text file |
| `python multi-dl.py -c <N>` | Set concurrent download slots (e.g. `-c 4`) |
| `python multi-dl.py -f "<format>"` | Custom format string for yt-dlp (e.g. `-f "bestvideo+bestaudio"`) |
| `python multi-dl.py -o "<dir>"` | Override output directory for the current transfer |
| `python multi-dl.py --backend <name>` | Force a specific engine (`yt-dlp`, `gallery-dl`, `terabox-dl`, `telegram-dl`, `cyberdrop-dl`, `anypic-dl`) |
| `python multi-dl.py --force` | Force download even if already recorded in archive or present on disk |
| `python multi-dl.py --no-archive` | Download without checking or writing to SQLite archive |
| `python multi-dl.py watch` | Start real-time clipboard monitor with audio notifications |
| `python multi-dl.py retry` | Re-queue and re-download all failed links from the previous run |
| `python multi-dl.py queue start` | Process all items currently queued in `data/queue.json` |
| `python multi-dl.py queue add <links>` | Add URLs or text files to queue without downloading now |
| `python multi-dl.py queue list` | Display status of all queued items |
| `python multi-dl.py archive` | Display SQLite deduplication archive metrics and history |
| `python multi-dl.py auth` | Launch interactive credential configuration wizard |
| `python multi-dl.py update` | Check for and apply updates across all engines |
| `python multi-dl.py proxy` | View, set, or test network proxy configuration |
| `python multi-dl.py route "<URL>"` | Test and preview which backend will handle a URL |
| `python multi-dl-gui.py` | Launch the desktop GUI interface |

---

## 📁 Output Directory Structure

By default, files are neatly organized by engine and website domain:

```
downloads/
├── yt-dlp/
│   └── youtube/
│       └── Milky_Way_4K.mp4
├── gallery-dl/
│   └── artstation/
│       └── Cyberpunk_Alley_01.png
├── terabox-dl/
│   └── terabox/
│       └── Design_Assets_Master.zip
└── telegram-dl/
    └── tech_digest/
        └── document.pdf
```

---

## 🤝 Upstream Projects & Credits

MULTI_DOWNLOADER orchestrates and relies on these outstanding open-source projects:

* [**yt-dlp**](https://github.com/yt-dlp/yt-dlp) — The premier video/audio extraction framework.
* [**gallery-dl**](https://github.com/mikf/gallery-dl) — Image board and gallery crawler.
* [**Telethon**](https://github.com/LonamiWebs/Telethon) — Pure Python MTProto client for Telegram media.
* [**Terabox-DL**](https://github.com/Alex-0099/Terabox-DL) — Direct link extractor and chunked stream engine for TeraBox.
* [**cyberdrop-dl**](https://github.com/Cyberdrop-DL/cyberdrop-dl) — File locker and forum media crawler.
* [**FFmpeg**](https://ffmpeg.org/) — Multimedia processing and stream muxing engine.
* [**Flet**](https://flet.dev/) — Fast Flutter-based UI engine for Python.

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).

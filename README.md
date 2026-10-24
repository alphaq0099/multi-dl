# MULTI_DOWNLOADER

> A modern, unified multi-platform downloader orchestrating [`yt-dlp`](https://github.com/yt-dlp/yt-dlp), [`gallery-dl`](https://github.com/mikf/gallery-dl), [`terabox-dl`](https://github.com/Alex-0099/Terabox-DL), [`telegram-dl`](https://github.com/Alex-0099/Telegram-dl), [`cyberdrop-dl`](https://github.com/Cyberdrop-DL/cyberdrop-dl), and [`anypic-dl`](https://github.com/Alex-0099) with isolated configurations, shared SQLite deduplication archive, persistent priority queue, two-way automatic failover, and interactive terminal interface.

**Current Version:** `v4.7.0`

---

## 📋 Changelog

* **v4.7.0 (Audit Hardening, Credential Safety & Project Cleanup)**: 21-point reliability/security audit remedies (atomic queue/config persistence with crash recovery, transfer integrity checks, gallery-dl process isolation, hardened local PO-token provider), credential safety (validated TeraBox ndus handling, secrets-store-only persistence, pre-commit secret-scan guard, CLI exit codes), TeraBox CDN-403 auto-recovery with fresh dlink resolution, and a project-wide dead-code/stale-comment cleanup with refreshed dependency constraints.
* **v4.6.0 (Live Clipboard Watcher, Cross-Directory Resilience & Recovery)**: Real-time Clipboard Auto-Watcher (`watch` / `clipboard`) with auto-downloading and audio notifications, permanent cross-directory path anchoring (ensures identical behavior regardless of execution directory), failed download auto-recovery (`retry` / `data/failed_links.txt`), extended locker routing (`hotpic2.cc`, `hotpic.cc`), and gallery-dl cache isolation.
* **v4.5.0 (Centralized Configuration & Proxy Architecture)**: Dot-notation config management (`multi-dl config set/get`), engine concurrency limits, per-domain connection throttling guards, network proxy testing (`multi-dl proxy test`), and sensitive credential masking.
* **v4.4.0 (Unified Deduplication Archive & Container-Aware Smart Skipping)**: Central SQLite archive (`data/archive.db`), multi-tier container intelligence (skipping single media in 1ms while delegating playlists/bookmarks/albums to granular per-category item archives), `multi-dl archive` CLI management (`--list`, `--search`, `--remove`, `--clear`), `--force` and `--no-archive` flags, and native PowerShell runner (`multi-dl.ps1`).
* **v4.3.0 (Concurrent Multi-Engine Queue & Dispatcher)**: Fast pre-routing ingestion, intelligent per-engine concurrency guards (Telegram max 1, yt-dlp max 2), atomic multi-stream progress canvas, retry handling, and consolidated batch summary reports.
* **v4.x.x (`cyberdrop-dl`)**: Subprocess engine integration for file lockers & forums, live SQLite progress tracking, directory hierarchy normalization, existing-file skip detection, and bidirectional failover.
* **v3.x.x (`telegram-dl`)**: Native Telethon client for public/private Telegram channels & chats, interactive CLI authentication wizard (`multi-dl auth`), and engine auto-updater (`multi-dl -U`).
* **v2.x.x (`terabox-dl`)**: Native TeraBox engine port with JS token scraping, dynamic direct links, validated sequential chunk streaming, HTTP Range resume, and interactive TUI mode (`multi-dl -i`).
* **v1.x.x (`gallery-dl`)**: Python library integration for 300+ image hosting platforms and galleries, in-memory config sandboxing, and dual-line batch progress tracker.
* **v0.x.x (`yt-dlp` & Core Architecture)**: Foundation framework with unified data models, SQLite deduplication archive, priority queue, 3-tier URL router, and yt-dlp backend with automatic Proof-of-Origin (PO) token sidecar.

---

## 🚀 Supported Engines

| Engine | Primary Media & Domains | Integration Method | Key Features |
| :--- | :--- | :--- | :--- |
| [**`yt-dlp`**](https://github.com/yt-dlp/yt-dlp) | YouTube, Twitch, TikTok, Twitter/X, Reddit, Vimeo, SoundCloud, 1000+ streaming sites | Embedded Python API | PO Token sidecar, EJS challenge solver, browser cookies, custom formats (`-f`), auto ffmpeg |
| [**`gallery-dl`**](https://github.com/mikf/gallery-dl) | Image boards, manga, galleries, social media, and file hosts (Imgur, Pixiv, Danbooru, Reddit, Twitter/X, Flickr, DeviantArt, etc.) | Embedded Python Library | Serialized jobs with isolated in-memory configuration, batch counter, dual-line progress display |
| [**`terabox-dl`**](https://github.com/Alex-0099/Terabox-DL) | TeraBox cloud links (`terabox.com`, `1024tera.com`, `teraboxapp.com`, etc.) | Native Python Port | JS token scraper, direct link extractor, validated sequential chunk streaming, HTTP `Range` resume, `ndus` cookie |
| [**`telegram-dl`**](https://github.com/Alex-0099/Telegram-dl) | Public/private Telegram channels, groups, direct chats (`t.me/...`) | Native [Telethon](https://github.com/LonamiWebs/Telethon) Library | Interactive auth setup wizard (`multi-dl auth`), session persistence, media type filtering |
| [**`cyberdrop-dl`**](https://github.com/Cyberdrop-DL/cyberdrop-dl) | File lockers, community forums, image hosts, album crawlers, and multi-host aggregators | Subprocess CLI Wrapper | Live SQLite progress bar & HTTP size probing, dynamic config sandbox, locker crawler |

---

## 🔄 Smart Routing & Automatic Failover

`MULTI_DOWNLOADER` uses a **3-tier intelligent routing pipeline**:

```
                       ┌─────────────────────────┐
                       │       Incoming URL      │
                       └────────────┬────────────┘
                                    │
                                    ▼
                       ┌─────────────────────────┐
                       │  Tier 1: Domain Mapping │
                       └────────────┬────────────┘
                                    │
                ┌───────────────────┴───────────────────┐
                ▼                                       ▼
     Common File Lockers                        Streaming / Cloud / Chats
(Image hosts, albums, lockers...)               (YouTube, TeraBox, Telegram...)
                │                                       │
                ▼                                       ▼
    ┌───────────────────────┐               ┌───────────────────────┐
    │  Primary: gallery-dl  │               │ Assigned Engine Direct│
    └───────────┬───────────┘               └───────────────────────┘
                │
                ├───────► Success! Download Complete.
                │
          Scraper Failed?
                │
                ▼
    ┌───────────────────────┐
    │ Failover: cyberdrop-dl│
    └───────────────────────┘
```

1. **Tier 1 — Domain Matching:** Fast Regex map routes URLs to their optimal engine.
2. **Tier 2 — Locker Prioritization:** Common file lockers default to `gallery-dl` for clean single-file downloads and reliable extraction.
3. **Tier 3 — Unknown-Page Routing:** Pages with no gallery-dl/yt-dlp extractor and no domain-map entry route to `anypic-dl`, the generic image engine that resolves original-resolution images (disable via `anypic_dl.handle_unknown_pages=false`).
4. **Tier 4 — Automatic Failover Chain:** `gallery-dl` ⇄ `cyberdrop-dl` → `anypic-dl`, plus `yt-dlp` → `anypic-dl` for unsupported URLs (one level deep, never chained).

### Engine Ownership Matrix

| Engine | Owns | Examples |
| --- | --- | --- |
| `yt-dlp` | Video/audio streaming sites with native extractors, direct media files | YouTube, Twitch, Vimeo, SoundCloud, Rumble, `*.mp4` links |
| `gallery-dl` | Image boards, social galleries, native-extractor lockers/forums | Twitter/X, Reddit, Pixiv, Instagram, Imgur, Danbooru, nhentai, Bunkr, Erome, Coomer/Kemono, Catbox, XenForo forums |
| `cyberdrop-dl` | Locker/forum crawling beyond gallery-dl's extractors | hotpic.cc, saint.to, F95zone, mega.nz, sendvid, streamtape, puter |
| `anypic-dl` | Everything image-bearing with no other owner — unknown pages, direct images gallery-dl can't claim | random blogs, `.jpg` links on unknown hosts, JS-heavy image pages (Playwright optional) |
| `telegram-dl` | Telegram channels/chats/topics | `t.me/...` |
| `terabox-dl` | TeraBox family shares | terabox.com, 1024tera, teraboxlink, mirrors |

When several engines could claim a URL, the more specific one wins: extractor-backed `gallery-dl` over heuristic matches, `yt-dlp`'s registry over the anypic fallback, and `anypic-dl` last by design — it only takes what nothing else wanted.

---

## 🖥️ Modern Desktop GUI (Phase 4)

MULTI_DOWNLOADER includes a standalone graphical desktop interface built with [Flet](https://flet.dev/) matching the Telegram-DL dark mode ecosystem:

```powershell
# Launch the desktop GUI via double-click or CLI:
.\multi-dl-gui.bat
.\multi-dl-gui.ps1
python multi-dl-gui.py
python multi-dl.py gui
```

### Key GUI Features:
* **📥 Downloads Dashboard**: Multi-line URL box with real-time regex engine detection badges (`[YT-DLP]`, `[GALLERY-DL]`, `[TERABOX-DL]`, `[TELEGRAM-DL]`, `[CYBERDROP-DL]`), quick preset filter chips (Extract Audio MP3, Video Only, Images Only, Resolution limit), and dynamic transfer cards with dual-unit byte metrics, live speed, and ETA.
* **📋 Queue Manager**: Live view of pending/active/completed queue tasks, one-click `Start Queue`, `Retry Failed` (loads `data/failed_links.txt`), and individual item deletion.
* **🗄️ Archive Browser**: Interactive history browser querying the SQLite database in real time, engine filter dropdown, storage statistics cards, and one-click "Show in Folder" button.
* **⚙️ Settings Editor**: Visual configuration for download folder picker, concurrency slider, audio chimes, proxy testing, and engine credentials (`ndus` cookie, Telegram API ID/Hash).
* **📋 Clipboard Watcher Toggle**: Header switch to monitor the Windows clipboard and auto-download copied media links in the background.

---

## 💻 CLI Usage

### 1. Direct Downloads
Pass URLs directly using the native PowerShell runner `.\multi-dl` or `python multi-dl.py`:
```powershell
# PowerShell launcher (runs directly in active console using project .venv):
.\multi-dl "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
.\multi-dl "https://nhentai.net/g/649681/"
.\multi-dl "https://bunkr.cr/a/MYwOPfwS"

# Or standard python invocation:
python multi-dl.py "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
python multi-dl.py "https://terabox.com/s/1abcdef..."
python multi-dl.py "https://t.me/channel_name/123"
```

### 2. Immediate Batch Downloads & Concurrency
Pass a text file of URLs or multiple links to download them immediately in parallel with real-time multi-stream progress bars (processed via unique files in `data/batches/`; `queue --batch` selects the latest batch, keeping your persistent `data/queue.json` untouched):
```powershell
# Directly pass a text file of links (executes immediately with concurrent streams):
python multi-dl.py links.txt
python multi-dl.py C:\Users\LENOVO\src\urls.txt

# Customize concurrency level (e.g. 5 parallel streams):
python multi-dl.py links.txt -c 5

# Pass multiple links directly:
python multi-dl.py "https://youtu.be/..." "https://imgur.com/a/..." -c 4
```

### 3. Persistent Queue Management (`multi-dl queue`)
Queue downloads for later without executing them immediately (stored in `data/queue.json`):
```powershell
# Queue links from a text file or URL arguments for later (does NOT download now):
python multi-dl.py queue add links.txt
python multi-dl.py queue add "https://www.youtube.com/watch?v=..." "https://imgur.com/a/..."

# Set priority on queued items:
python multi-dl.py queue add links.txt -p 10

# List all queued downloads awaiting execution:
python multi-dl.py queue list
python multi-dl.py queue list --status failed

# Start the concurrent worker pool when you are ready to download:
python multi-dl.py queue start
python multi-dl.py queue start -c 4

# Retry all failed downloads:
python multi-dl.py queue retry

# Clear queue items:
python multi-dl.py queue clear
python multi-dl.py queue clear --status completed
```

### 4. Interactive Mode & Shell Command Center
Launch an interactive session to paste single URLs, multiple pasted links, or paths to `.txt` files:
```powershell
.\multi-dl
# or
python multi-dl.py -i
```

Inside the interactive prompt, you can type natural commands directly without prefixing `:`:
* `watch` — Monitor system clipboard in real time: copy any media link in your browser to auto-capture & download!
* `retry` — Re-queue and download failed links from previous session (`data/failed_links.txt`).
* `open` — Open the `downloads\` folder in Windows File Explorer (`os.startfile`).
* `archive` — View SQLite archive metrics (total downloads, size) and recent entries table.
* `queue` — View pending and queued downloads.
* `clear` — Clear terminal screen and redraw the clean cyan banner.
* `config` — Display active configuration settings, workers, and deduplication rules.
* `update` — Check and update all download engines directly from the prompt.
* `help` — Show the interactive command cheatsheet.
* `exit` — Exit the interactive session cleanly (or `Ctrl+C`).

**Dedicated CLI shortcuts:**
```powershell
# Live clipboard monitor (polls clipboard, plays audio chime, and auto-downloads):
.\multi-dl watch

# Re-download failed links from previous batch:
.\multi-dl retry
```

### 5. Engine Authentication Wizard
Configure credentials for Telegram (API ID, Hash, 2FA) or TeraBox (`ndus` cookie):
```powershell
python multi-dl.py auth
# or for a specific engine:
python multi-dl.py auth telegram
python multi-dl.py auth terabox
```

### 6. Engine Auto-Updater
Check for and apply updates across all download engines:
```powershell
# Update everything:
python multi-dl.py -U
# or
python multi-dl.py update

# Update a specific engine:
python multi-dl.py update yt-dlp
python multi-dl.py update gallery-dl
python multi-dl.py update cyberdrop-dl
```

### 7. Force a Specific Backend
Override automatic routing and force an engine:
```powershell
python multi-dl.py "https://example.com/media" --backend gallery-dl
python multi-dl.py "https://example.com/media" --backend cyberdrop-dl
```

### 8. Authentication & Cookies (Age-Gated / Member Media)
```powershell
# Extract session cookies directly from your web browser:
python multi-dl.py "https://www.youtube.com/watch?v=..." --cookies-from-browser chrome
python multi-dl.py "https://www.youtube.com/watch?v=..." --cookies-from-browser firefox

# Or pass a Netscape cookies.txt file:
python multi-dl.py "https://example.com/video" --cookies "cookies.txt"

# Provide TeraBox session cookie:
python multi-dl.py "https://terabox.com/s/..." --ndus "YOUR_NDUS_COOKIE"
```

### 9. Custom Formats & Quality
```powershell
python multi-dl.py "https://www.youtube.com/watch?v=..." -f "bestvideo*+bestaudio/best"
```

### 10. Inspect & Manage SQLite Download Archive
```powershell
# View archive metrics and recent downloads:
.\multi-dl archive
# or
.\multi-dl archive --stats

# List recent download records table:
.\multi-dl archive --list
.\multi-dl archive --list --limit 50

# Search download history by title, filename, or URL:
.\multi-dl archive --search "search_term"

# Remove an entry by URL or ID (allowing it to be re-downloaded):
.\multi-dl archive --remove "https://example.com/video"

# Clear entire download archive (prompts for confirmation):
.\multi-dl archive --clear

# Force re-download even if already recorded in archive or present on disk:
.\multi-dl "https://example.com/video" --force

# Run download without checking or writing to archive:
.\multi-dl "https://example.com/video" --no-archive
```

### 11. Test URL Routing
```powershell
python multi-dl.py route "https://imgur.com/a/sample_album"
```

### 12. Check Version
```powershell
python multi-dl.py -v
# or
python multi-dl.py --version
```

---

## 🔧 Downloader Configuration Guide

Each engine runs inside an isolated sandbox, keeping its settings independent from your global operating system profiles.

### 1. Master Configuration (`config.json`)
The primary configuration file at the project root connects all engines, sets download folders, and determines global options:
* `"download_dir"`: Destination folder for all downloads (defaults to `"downloads"`).
* `"organize_by_backend"`: Sorts files into subdirectories by engine (`downloads/<engine>/`).
* `"organize_by_site"`: Sorts files into subdirectories by host domain (`downloads/<engine>/<site>/`).
* `"concurrent_downloads"`: Number of concurrent worker threads.

---

### 2. `yt-dlp` Configuration (`configs/yt-dlp.conf`)
* **How it works**: Uses standard `yt-dlp` command-line flags written line-by-line (e.g. `--format`, `--sub-langs`, `--embed-subs`). It is isolated using `ignoreconfig=True` to never conflict with `%APPDATA%/yt-dlp/config`.
* **Example options**:
  ```ini
  --merge-output-format mp4
  --embed-metadata
  --sub-langs all
  --concurrent-fragments 4
  ```
* 📖 **Documentation**: [yt-dlp Configuration Guide](https://github.com/yt-dlp/yt-dlp#configuration)

---

### 3. `gallery-dl` Configuration (`configs/gallery-dl.json`)
* **How it works**: Uses JSON format to configure extractors, file naming patterns, postprocessors, and domain-specific options. Each job runs in its own worker process with private library configuration, allowing different galleries to download concurrently within the configured queue, engine, and host limits. Repeated jobs for the same URL and destination wait for one another to protect files. Cancellation terminates and awaits the worker process.
* **Key fields**:
  * `"extractor.<domain>"`: Custom options (e.g. image format, sleep intervals, tags) for specific websites.
  * `"skip"`: Set to `true` to skip re-downloading files that already exist locally.
  * `"cookies"`: Browser session cookie sources (e.g. `["firefox"]`, `["chrome"]`).
* 📖 **Documentation**: [gallery-dl Configuration Reference](https://github.com/mikf/gallery-dl/blob/master/docs/configuration.rst) • [Sample gallery-dl.json](https://github.com/mikf/gallery-dl/blob/master/docs/gallery-dl.json)

---

### 4. `cyberdrop-dl` Configuration (`configs/cyberdrop-dl.json`)
* **How it works**: Passed dynamically via `--config` to the isolated `cyberdrop-dl` subprocess runner.
* **Key fields**:
  * `"max_simultaneous"`: Number of concurrent file downloads (default: `5`).
  * `"use_domain_subfolders"`: Organizes output by domain subfolders.
  * `"include_domain_in_path"`: Eliminates redundant domain folder nesting.
* 📖 **Documentation**: [cyberdrop-dl Wiki & Config Options](https://github.com/Cyberdrop-DL/cyberdrop-dl#configuration) • [Sample config.json](https://github.com/Cyberdrop-DL/cyberdrop-dl/blob/main/cyberdrop_dl/assets/config.json)

---

### 5. `telegram-dl` Configuration (`configs/telegram-dl.json`)
* **How it works**: Stores Telegram API credentials, session name, and media filters.
* **Setup**: Automatically generated and validated using the interactive wizard:
  ```powershell
  python multi-dl.py auth telegram
  ```
* 📖 **Documentation**: [Telethon Documentation](https://docs.telethon.dev/) • [Get Telegram API ID/Hash](https://my.telegram.org/)

---

### 6. `terabox-dl` Configuration (`configs/terabox-dl.json`)
* **How it works**: Stores TeraBox session authentication and stream chunking parameters.
* **Key fields**:
  * `"ndus_cookie"`: TeraBox session cookie for high-speed direct downloads.
  * `"chunk_size_mb"`: Download chunk segment size (default: `4` MB).
  * `"max_threads"`: Number of parallel chunk workers (default: `4`).
* **Setup**: Can be set interactively via `python multi-dl.py auth terabox` or directly in the file.

---

## 📁 Project Architecture

```
MULTI_DOWNLOADER/
├── backends/         # Engine adapters (yt-dlp, gallery-dl, terabox-dl, telegram-dl, cyberdrop-dl)
├── core/             # Core logic (URL router, archive database, queue, dispatcher, config, updater, terminal UI)
├── configs/          # Dedicated sandbox configuration files for each engine
├── data/             # Persistent SQLite archive (archive.db), download queue (queue.json), and runtime cache
├── downloads/        # Output root: downloads/<engine>/<site>/<file>
├── tests/            # Automated test suite (66 unit & integration tests)
├── config.json       # Master configuration file
├── multi-dl.py       # Main CLI & interactive entry point
└── requirements.txt  # Python environment dependencies
```

---

## 📦 Upstream Projects & Credits

This tool orchestrates and relies on the following open-source projects:

* [**yt-dlp**](https://github.com/yt-dlp/yt-dlp) — Feature-rich audio and video downloader for YouTube and thousands of streaming sites.
* [**gallery-dl**](https://github.com/mikf/gallery-dl) — Image and album scraper for image boards, galleries, and archives ([Codeberg Mirror](https://codeberg.org/mikf/gallery-dl)).
* [**cyberdrop-dl**](https://github.com/Cyberdrop-DL/cyberdrop-dl) — Bulk file and forum scraper for lockers, forums, and multi-host aggregators.
* [**Telegram-dl**](https://github.com/Alex-0099/Telegram-dl) & [**Telethon**](https://github.com/LonamiWebs/Telethon) — Fast, asynchronous MTProto Python client for Telegram media downloads.
* [**Terabox-DL**](https://github.com/Alex-0099/Terabox-DL) — Direct link extractor and resumable chunked-stream downloader for TeraBox.
* [**anypic-dl**](https://github.com/Alex-0099) — Bundled generic image-page engine: resolves original-resolution images from pages no other engine owns (static HTTP, optional Playwright fallback).
* [**FFmpeg**](https://ffmpeg.org/) — Cross-platform multimedia framework for stream muxing, remuxing, and post-processing.
* [**curl_cffi**](https://github.com/yifeikong/curl_cffi) — Python binding for `curl-impersonate` enabling browser TLS fingerprint emulation.

---

## 🧪 Testing & Verification

Run the automated test suite with pytest:

```powershell
.\.venv\Scripts\pytest -v
```

All 66 test cases covering routing, pre-routing classification, queue dispatcher, concurrency limits, all 5 engine adapters, configuration isolation, updater, and terminal UI pass.


## October 2026 reliability fixes

- Telegram/TeraBox filenames and folders include stable IDs. Existing older names are preserved.
- Incomplete TeraBox transfers and partial batches remain retryable failures. Abandoned queue claims recover on startup.
- Failed CLI downloads exit with code `1`; interruption exits `130`.
- Settings credentials and proxy details are saved to ignored `config.secrets.json`. `.env` remains an input fallback; authentication tests and custom auth configurations do not rewrite it.
- `--force` on Cyberdrop stores the new copy in a separate `forced_<id>` folder. `--no-archive` uses disposable native Cyberdrop history.
- The bundled PO-token service uses authenticated loopback access. Restart an older running provider before using the hardened build. Startup automatically applies the tracked security patch to recognized bundled builds.
- Clipboard URL history retains the latest 1,000 entries. Clear it with `python multi-dl.py watch --clear-history`; clear failed-link recovery history with `python multi-dl.py retry --clear`.

Install the tested direct dependency versions:

```powershell
python -m pip install -r requirements.txt -c constraints-tested.txt
```

Run offline checks (development dependencies are in `requirements-dev.txt`):

```powershell
python -m pytest -q -p no:cacheprovider --basetemp scratch/verification
```

Set `RUN_NETWORK_TESTS=1` explicitly to enable the two live YouTube integration tests. The optional loopback token-service smoke check is `python tests/verify_token_service.py`.

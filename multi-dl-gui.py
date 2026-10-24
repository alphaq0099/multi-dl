"""
MULTI_DOWNLOADER — Modern Desktop GUI Application (v4.6.0)
Universal modular downloader combining yt-dlp, gallery-dl, terabox-dl, telegram-dl,
and cyberdrop-dl with unified dark-mode UI, active download cards, queue manager,
archive deduplication browser, live clipboard auto-watcher, and visual settings.
"""

import asyncio
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import traceback
from typing import Any, Dict, List, Optional, Set

import flet as ft

# Ensure Flet 0.x and 1.x cross-version compatibility for layout helper methods
# 1. Border
if hasattr(ft, "Border") and not hasattr(ft.Border, "all") and hasattr(ft, "border") and hasattr(ft.border, "all"):
    ft.Border.all = staticmethod(ft.border.all)
    ft.Border.only = staticmethod(ft.border.only)
    ft.Border.symmetric = staticmethod(ft.border.symmetric)
if not hasattr(ft, "border") or not hasattr(ft.border, "all"):
    class _BorderCompat:
        all = staticmethod(getattr(ft, "Border", None).all if hasattr(ft, "Border") and hasattr(ft.Border, "all") else lambda *a, **k: None)
        only = staticmethod(getattr(ft, "Border", None).only if hasattr(ft, "Border") and hasattr(ft.Border, "only") else lambda *a, **k: None)
        symmetric = staticmethod(getattr(ft, "Border", None).symmetric if hasattr(ft, "Border") and hasattr(ft.Border, "symmetric") else lambda *a, **k: None)
    ft.border = _BorderCompat

# 2. Padding
if hasattr(ft, "Padding") and not hasattr(ft.Padding, "symmetric") and hasattr(ft, "padding") and hasattr(ft.padding, "symmetric"):
    ft.Padding.all = staticmethod(ft.padding.all)
    ft.Padding.symmetric = staticmethod(ft.padding.symmetric)
    ft.Padding.only = staticmethod(ft.padding.only)
if not hasattr(ft, "padding") or not hasattr(ft.padding, "symmetric"):
    class _PaddingCompat:
        all = staticmethod(getattr(ft, "Padding", None).all if hasattr(ft, "Padding") and hasattr(ft.Padding, "all") else lambda *a, **k: None)
        only = staticmethod(getattr(ft, "Padding", None).only if hasattr(ft, "Padding") and hasattr(ft.Padding, "only") else lambda *a, **k: None)
        symmetric = staticmethod(getattr(ft, "Padding", None).symmetric if hasattr(ft, "Padding") and hasattr(ft.Padding, "symmetric") else lambda *a, **k: None)
        zero = staticmethod(getattr(ft, "Padding", None).zero if hasattr(ft, "Padding") and hasattr(ft.Padding, "zero") else lambda *a, **k: None)
    ft.padding = _PaddingCompat

# 3. Margin
if hasattr(ft, "Margin") and not hasattr(ft.Margin, "symmetric") and hasattr(ft, "margin") and hasattr(ft.margin, "symmetric"):
    ft.Margin.all = staticmethod(ft.margin.all)
    ft.Margin.symmetric = staticmethod(ft.margin.symmetric)
    ft.Margin.only = staticmethod(ft.margin.only)
if not hasattr(ft, "margin") or not hasattr(ft.margin, "symmetric"):
    class _MarginCompat:
        all = staticmethod(getattr(ft, "Margin", None).all if hasattr(ft, "Margin") and hasattr(ft.Margin, "all") else lambda *a, **k: None)
        only = staticmethod(getattr(ft, "Margin", None).only if hasattr(ft, "Margin") and hasattr(ft.Margin, "only") else lambda *a, **k: None)
        symmetric = staticmethod(getattr(ft, "Margin", None).symmetric if hasattr(ft, "Margin") and hasattr(ft.Margin, "symmetric") else lambda *a, **k: None)
    ft.margin = _MarginCompat

# 4. Alignment
if hasattr(ft, "alignment") and hasattr(ft.alignment, "center"):
    if hasattr(ft, "Alignment") and not hasattr(ft.Alignment, "center"):
        ft.Alignment.center = ft.alignment.center
        ft.Alignment.CENTER = ft.alignment.center
if hasattr(ft, "Alignment") and hasattr(ft.Alignment, "CENTER"):
    if not hasattr(ft, "alignment") or not hasattr(ft.alignment, "center"):
        class _AlignmentCompat:
            center = ft.Alignment.CENTER
        ft.alignment = _AlignmentCompat

# 5. BorderRadius
if hasattr(ft, "BorderRadius") and not hasattr(ft.BorderRadius, "all") and hasattr(ft, "border_radius") and hasattr(ft.border_radius, "all"):
    ft.BorderRadius.all = staticmethod(ft.border_radius.all)
    ft.BorderRadius.only = staticmethod(ft.border_radius.only)
    ft.BorderRadius.horizontal = staticmethod(ft.border_radius.horizontal)
    ft.BorderRadius.vertical = staticmethod(ft.border_radius.vertical)
if not hasattr(ft, "border_radius") or not hasattr(ft.border_radius, "all"):
    class _BorderRadiusCompat:
        all = staticmethod(getattr(ft, "BorderRadius", None).all if hasattr(ft, "BorderRadius") and hasattr(ft.BorderRadius, "all") else lambda *a, **k: None)
        only = staticmethod(getattr(ft, "BorderRadius", None).only if hasattr(ft, "BorderRadius") and hasattr(ft.BorderRadius, "only") else lambda *a, **k: None)
        horizontal = staticmethod(getattr(ft, "BorderRadius", None).horizontal if hasattr(ft, "BorderRadius") and hasattr(ft.BorderRadius, "horizontal") else lambda *a, **k: None)
        vertical = staticmethod(getattr(ft, "BorderRadius", None).vertical if hasattr(ft, "BorderRadius") and hasattr(ft.BorderRadius, "vertical") else lambda *a, **k: None)
    ft.border_radius = _BorderRadiusCompat

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

# Handle windowed execution where stdout/stderr might be None
is_windowed = sys.stdout is None
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

# Setup logging
log_handlers = [
    logging.FileHandler(PROJECT_ROOT / "gui_debug.log", encoding="utf-8", mode="w")
]
if not is_windowed:
    log_handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=log_handlers,
)
logger = logging.getLogger("MultiDlGUI")

# Multi-Downloader Core Modules
from core.archive import ArchiveManager
from core.clipboard import ClipboardWatcher, FAILED_LINKS_PATH, load_failed_urls, play_chime
from core.config import ConfigManager
from core.dispatcher import QueueDispatcher
from core.gui_tracker import GuiDownloadSlot, GuiMultiBarTracker, format_bytes, format_dual_bytes
from core.models import ArchiveEntry, QueueItem, TaskStatus
from core.queue_manager import QueueManager
from core.router import URLRouter

# --- Color Theme Palette ---
BG_COLOR = "#121214"
SURFACE_COLOR = "#18181D"
CARD_BG = "#202028"
BORDER_COLOR = "#2D2D3B"
ACCENT_CYAN = "#00E5FF"
ACCENT_GREEN = "#00E676"
ACCENT_YELLOW = "#FFD600"
ACCENT_RED = "#FF5252"
ACCENT_BLUE = "#29B6F6"
ACCENT_MAGENTA = "#E040FB"
TEXT_DIM = "#8E8E9A"

ENGINE_COLORS: Dict[str, str] = {
    "yt-dlp": ACCENT_RED,
    "gallery-dl": ACCENT_YELLOW,
    "telegram-dl": ACCENT_BLUE,
    "terabox-dl": ACCENT_CYAN,
    "cyberdrop-dl": ACCENT_MAGENTA,
    "auto": "#9E9E9E",
}


def get_engine_color(engine_name: str) -> str:
    return ENGINE_COLORS.get((engine_name or "auto").lower(), ACCENT_CYAN)


def open_in_file_manager(target_path: Path):
    """Reveals the path in Windows Explorer."""
    try:
        p = Path(target_path).resolve()
        if p.is_file():
            subprocess.run(["explorer", f"/select,{p}"])
        elif p.is_dir():
            os.startfile(str(p))
        else:
            parent = p.parent
            if parent.exists():
                os.startfile(str(parent))
    except Exception as e:
        logger.error(f"Failed to open path {target_path}: {e}")


async def main(page: ft.Page):
    logger.info("Initializing MULTI_DOWNLOADER GUI...")

    # --- Page & Window Configuration ---
    page.title = "MULTI_DOWNLOADER — Universal Media & File Downloader (v4.6.0)"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = BG_COLOR
    page.padding = 0

    page.window.width = 1200
    page.window.height = 820
    page.window.min_width = 1000
    page.window.min_height = 680

    # Core system singletons
    config = ConfigManager()
    archive = ArchiveManager(config.archive_db_path)
    queue_manager = QueueManager(PROJECT_ROOT / "data" / "queue.json")
    router = URLRouter.create_default(config=config)
    dispatcher = QueueDispatcher(config=config, queue_manager=queue_manager, archive=archive, router=router)

    # State tracking
    is_downloading = False
    active_download_tasks: Set[asyncio.Task] = set()
    clipboard_watcher_running = False
    clipboard_stop_event = asyncio.Event()
    focused_textfield: Optional[ft.TextField] = None

    # Safe Control Update Helper
    def safe_update(*ctrls: Optional[ft.Control]):
        for c in ctrls:
            if c is not None and getattr(c, "page", None) is not None:
                try:
                    c.update()
                except Exception:
                    pass

    # Global SnackBar helper
    def show_toast(message: str, is_error: bool = False, is_success: bool = False):
        bg = ACCENT_RED if is_error else (ACCENT_GREEN if is_success else CARD_BG)
        snack = ft.SnackBar(
            content=ft.Text(message, color=ft.Colors.WHITE, weight=ft.FontWeight.W_500),
            bgcolor=bg,
            duration=3500,
        )
        page.overlay.append(snack)
        snack.open = True
        try:
            page.update()
        except Exception:
            pass

    # --- Windows Ctrl+V Paste Shortcut Handler ---
    async def on_keyboard_event(e: ft.KeyboardEvent):
        nonlocal focused_textfield
        is_ctrl_down = e.ctrl
        try:
            import ctypes
            if sys.platform == "win32":
                is_ctrl_down = is_ctrl_down or ((ctypes.windll.user32.GetKeyState(0x11) & 0x8000) != 0)
        except Exception:
            pass

        if is_ctrl_down and e.key.lower() == "v" and focused_textfield:
            try:
                import pyperclip
                val = pyperclip.paste()
                if val:
                    curr = focused_textfield.value or ""
                    focused_textfield.value = f"{curr}\n{val}".strip() if curr else val
                    safe_update(focused_textfield)
                    if hasattr(focused_textfield, "_on_change_handler"):
                        focused_textfield._on_change_handler(None)
            except Exception:
                pass

    page.on_keyboard_event = on_keyboard_event

    # ----------------------------------------------------
    # UI Tracker Adapter
    # ----------------------------------------------------
    ui_update_pending = False
    async def refresh_download_ui():
        nonlocal ui_update_pending
        try:
            update_active_downloads_ui()
            update_queue_stats_ui()
        finally:
            ui_update_pending = False

    ui_loop = asyncio.get_running_loop()
    def on_tracker_update():
        def schedule():
            nonlocal ui_update_pending
            if not ui_update_pending:
                ui_update_pending = True
                asyncio.create_task(refresh_download_ui())
        ui_loop.call_soon_threadsafe(schedule)

    gui_tracker = GuiMultiBarTracker(on_update_callback=on_tracker_update)

    # ----------------------------------------------------
    # Header Bar
    # ----------------------------------------------------
    header_title = ft.Row(
        spacing=8,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.Icon(ft.Icons.CLOUD_DOWNLOAD_ROUNDED, color=ACCENT_CYAN, size=28),
            ft.Text("MULTI_DOWNLOADER", size=20, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
            ft.Container(
                content=ft.Text("v4.6.0", size=11, color=ACCENT_CYAN, weight=ft.FontWeight.W_600),
                bgcolor="#00E5FF1A",
                border=ft.Border.all(1, "#00E5FF4D"),
                border_radius=6,
                padding=ft.Padding.symmetric(horizontal=6, vertical=2),
            ),
        ],
    )

    watcher_switch = ft.Switch(
        label="Clipboard Watcher",
        value=False,
        active_color=ACCENT_CYAN,
    )

    header_active_chip = ft.Container(
        content=ft.Row(
            spacing=6,
            controls=[
                ft.Icon(ft.Icons.STREAM_ROUNDED, size=14, color=ACCENT_GREEN),
                ft.Text("0 Active", size=12, color=ft.Colors.WHITE, weight=ft.FontWeight.W_500),
            ],
        ),
        bgcolor="#00E6761A",
        border=ft.Border.all(1, "#00E6764D"),
        border_radius=8,
        padding=ft.Padding.symmetric(horizontal=10, vertical=4),
    )

    open_folder_btn = ft.IconButton(
        icon=ft.Icons.FOLDER_OPEN_ROUNDED,
        icon_color=ACCENT_CYAN,
        tooltip="Open Downloads Folder",
        on_click=lambda _: open_in_file_manager(config.download_dir),
    )

    header = ft.Container(
        bgcolor=SURFACE_COLOR,
        border=ft.Border.only(bottom=ft.BorderSide(1, BORDER_COLOR)),
        padding=ft.Padding.symmetric(horizontal=20, vertical=12),
        content=ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            controls=[
                header_title,
                ft.Row(
                    spacing=16,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        watcher_switch,
                        header_active_chip,
                        open_folder_btn,
                    ],
                ),
            ],
        ),
    )

    # ----------------------------------------------------
    # TAB 1: DOWNLOADS DASHBOARD
    # ----------------------------------------------------
    url_input = ft.TextField(
        multiline=True,
        min_lines=3,
        max_lines=5,
        hint_text="Enter or paste URLs (one per line, or space-separated)...\nSupports YouTube, Telegram, TeraBox, Gallery, Bunkr, Coomer, Kemono, etc.",
        border_color=BORDER_COLOR,
        focused_border_color=ACCENT_CYAN,
        cursor_color=ACCENT_CYAN,
        text_size=13,
        bgcolor=CARD_BG,
        expand=True,
    )

    def on_focus_field(e):
        nonlocal focused_textfield
        focused_textfield = e.control

    def on_blur_field(e):
        nonlocal focused_textfield
        if focused_textfield == e.control:
            focused_textfield = None

    url_input.on_focus = on_focus_field
    url_input.on_blur = on_blur_field

    detected_engines_row = ft.Row(wrap=True, spacing=6)

    def parse_input_urls(text: str) -> List[str]:
        if not text:
            return []
        tokens = re.split(r"[\r\n\s]+", text.strip())
        return [t.strip() for t in tokens if t.strip() and (t.startswith("http://") or t.startswith("https://") or t.startswith("tg://") or t.startswith("t.me/"))]

    def update_detected_engines(e=None):
        urls = parse_input_urls(url_input.value or "")
        engine_counts: Dict[str, int] = {}
        for u in urls:
            eng = router.detect_backend_name(u)
            engine_counts[eng] = engine_counts.get(eng, 0) + 1

        detected_engines_row.controls.clear()
        if not engine_counts:
            detected_engines_row.controls.append(
                ft.Text("Paste links to auto-detect download engines", size=12, color=TEXT_DIM, italic=True)
            )
        else:
            for eng, count in engine_counts.items():
                c = get_engine_color(eng)
                detected_engines_row.controls.append(
                    ft.Container(
                        content=ft.Row(
                            spacing=4,
                            tight=True,
                            controls=[
                                ft.Container(width=6, height=6, border_radius=3, bgcolor=c),
                                ft.Text(f"{eng.upper()} ({count})", size=11, color=ft.Colors.WHITE, weight=ft.FontWeight.W_600),
                            ],
                        ),
                        bgcolor=f"{c}26",
                        border=ft.Border.all(1, f"{c}80"),
                        border_radius=6,
                        padding=ft.Padding.symmetric(horizontal=8, vertical=3),
                    )
                )
        safe_update(detected_engines_row)

    url_input.on_change = update_detected_engines
    url_input._on_change_handler = update_detected_engines

    # Filters & options
    audio_only_check = ft.Checkbox(label="Extract Audio (MP3)", value=False, active_color=ACCENT_CYAN)
    video_only_check = ft.Checkbox(label="Video Only", value=False, active_color=ACCENT_CYAN)
    images_only_check = ft.Checkbox(label="Images Only", value=False, active_color=ACCENT_CYAN)
    resolution_dropdown = ft.Dropdown(
        label="Max Resolution",
        value="best",
        width=160,
        options=[
            ft.dropdown.Option("best", "Best / Source"),
            ft.dropdown.Option("1080p", "1080p (FHD)"),
            ft.dropdown.Option("720p", "720p (HD)"),
            ft.dropdown.Option("480p", "480p (SD)"),
            ft.dropdown.Option("4k", "4K (UHD)"),
        ],
        border_color=BORDER_COLOR,
        focused_border_color=ACCENT_CYAN,
        text_size=12,
    )

    active_downloads_column = ft.Column(spacing=10, scroll=ft.ScrollMode.AUTO, expand=True)
    active_downloads_empty = ft.Container(
        alignment=ft.Alignment.CENTER,
        padding=30,
        content=ft.Column(
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=8,
            controls=[
                ft.Icon(ft.Icons.CLOUD_DOWNLOAD_OUTLINED, size=48, color="#3A3A4A"),
                ft.Text("No Active Downloads", size=15, color=TEXT_DIM, weight=ft.FontWeight.W_600),
                ft.Text("Paste URLs above or switch on Clipboard Watcher to start downloading.", size=12, color=TEXT_DIM),
            ],
        ),
    )
    active_downloads_column.controls.append(active_downloads_empty)

    def update_active_downloads_ui():
        # Update active streams count chip
        slots, history = gui_tracker.snapshot()
        active_count = sum(1 for s in slots.values() if s.status == "active")
        header_active_chip.content.controls[1].value = f"{active_count} Active"
        safe_update(header_active_chip)

        active_downloads_column.controls.clear()
        if not slots and not history:
            active_downloads_column.controls.append(active_downloads_empty)
            safe_update(active_downloads_column)
            return

        # Render active slots first, then recent finished
        rendered_slots = list(slots.values())
        # Show last 5 finished
        recent_history = [s for s in history if s.slot_id not in slots][-5:]
        all_display = rendered_slots + list(reversed(recent_history))

        for slot in all_display:
            c = get_engine_color(slot.backend)
            pct = slot.percent if slot.percent is not None else 0.0
            pct_val = min(max(pct / 100.0, 0.0), 1.0) if slot.percent is not None else None

            status_badge_bg = (
                "#00E67626" if slot.status == "completed"
                else ("#FFD60026" if slot.status == "skipped"
                else ("#FF525226" if slot.status == "failed"
                else "#00E5FF26"))
            )
            status_badge_color = (
                ACCENT_GREEN if slot.status == "completed"
                else (ACCENT_YELLOW if slot.status == "skipped"
                else (ACCENT_RED if slot.status == "failed"
                else ACCENT_CYAN))
            )

            # Metrics string
            dual_str = format_dual_bytes(slot.downloaded_bytes, slot.total_bytes)
            speed_str = f"@{format_bytes(int(slot.speed_bytes_sec))}/s" if slot.speed_bytes_sec else "--/s"
            eta_str = f"ETA {int(slot.eta_seconds // 60):02d}:{int(slot.eta_seconds % 60):02d}" if (slot.eta_seconds and slot.eta_seconds > 0) else ""
            batch_str = f"({slot.file_index}/{slot.total_files})" if (slot.total_files and slot.total_files > 1 and slot.file_index) else ""

            metrics_items = [
                ft.Text(dual_str, size=11, color=ft.Colors.WHITE, weight=ft.FontWeight.W_500),
            ]
            if batch_str:
                metrics_items.append(ft.Text(batch_str, size=11, color=ACCENT_CYAN))
            metrics_items.append(ft.Text(f"{pct:5.1f}%" if slot.percent is not None else "--%", size=11, color=status_badge_color, weight=ft.FontWeight.BOLD))
            metrics_items.append(ft.Text(speed_str, size=11, color=TEXT_DIM))
            if eta_str:
                metrics_items.append(ft.Text(eta_str, size=11, color=ACCENT_YELLOW))

            card = ft.Container(
                bgcolor=CARD_BG,
                border=ft.Border.all(1, BORDER_COLOR),
                border_radius=10,
                padding=12,
                content=ft.Column(
                    spacing=8,
                    controls=[
                        ft.Row(
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            controls=[
                                ft.Row(
                                    spacing=8,
                                    controls=[
                                        ft.Container(
                                            content=ft.Text(slot.backend.upper(), size=10, color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD),
                                            bgcolor=c,
                                            border_radius=4,
                                            padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                                        ),
                                        ft.Text(slot.title or slot.url, size=13, weight=ft.FontWeight.W_600, color=ft.Colors.WHITE, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, expand=True),
                                    ],
                                    expand=True,
                                ),
                                ft.Container(
                                    content=ft.Text(slot.status_message or slot.status.title(), size=11, color=status_badge_color, weight=ft.FontWeight.W_600),
                                    bgcolor=status_badge_bg,
                                    border_radius=6,
                                    padding=ft.Padding.symmetric(horizontal=8, vertical=2),
                                ),
                            ],
                        ),
                        ft.ProgressBar(
                            value=pct_val,
                            color=status_badge_color,
                            bgcolor="#2A2A38",
                            height=6,
                        ),
                        ft.Row(
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            controls=[
                                ft.Row(spacing=8, controls=metrics_items),
                                ft.Row(
                                    spacing=4,
                                    controls=[
                                        ft.IconButton(
                                            icon=ft.Icons.FOLDER_OPEN,
                                            icon_size=16,
                                            icon_color=TEXT_DIM,
                                            tooltip="Open Folder",
                                            visible=bool(slot.saved_path),
                                            on_click=lambda _, p=slot.saved_path: open_in_file_manager(Path(p)),
                                        )
                                    ],
                                ),
                            ],
                        ),
                    ],
                ),
            )
            active_downloads_column.controls.append(card)

        safe_update(active_downloads_column)

    # Download action trigger
    async def trigger_download(urls_to_download: Optional[List[str]] = None):
        nonlocal is_downloading
        if is_downloading:
            if urls_to_download:
                queue_manager.add_batch(urls_to_download)
                show_toast(f"Added {len(urls_to_download)} links to the active queue.", is_success=True)
            return

        if urls_to_download is not None and not urls_to_download:
            show_toast("No valid media URLs found in input.", is_error=True)
            return

        is_downloading = True
        opts: Dict[str, Any] = {}
        if urls_to_download is not None:
            if audio_only_check.value:
                opts["extract_audio"] = True
                opts["audio_only"] = True
            if video_only_check.value:
                opts["video_only"] = True
            if images_only_check.value:
                opts["images_only"] = True
            if resolution_dropdown.value and resolution_dropdown.value != "best":
                opts["resolution"] = resolution_dropdown.value

        download_now_btn.disabled = True
        safe_update(download_now_btn)
        count_desc = f"{len(urls_to_download)} URL(s)" if urls_to_download is not None else "queued items"
        show_toast(f"Starting transfer for {count_desc}...", is_success=True)

        async def _run_task():
            nonlocal is_downloading
            batch_finished = False
            try:
                # Add to queue and execute via dispatcher with GUI tracker
                report = await dispatcher.run(
                    urls=urls_to_download,
                    options=opts if urls_to_download is not None else None,
                    tracker=gui_tracker,
                    quiet=True,
                )
                batch_finished = True
                show_toast(
                    f"Batch finished: {report.succeeded_count} succeeded, {report.skipped_count} skipped, {report.failed_count} failed.",
                    is_success=(report.failed_count == 0),
                    is_error=(report.failed_count > 0 and report.succeeded_count == 0),
                )
            except Exception as ex:
                logger.error(f"Download execution error: {traceback.format_exc()}")
                show_toast(f"Download error: {str(ex)}", is_error=True)
            finally:
                is_downloading = False
                download_now_btn.disabled = False
                safe_update(download_now_btn)
                update_queue_stats_ui()
                update_archive_table_ui()
                if batch_finished and queue_manager.count_remaining():
                    asyncio.create_task(trigger_download(None))

        task = asyncio.create_task(_run_task())
        active_download_tasks.add(task)
        task.add_done_callback(active_download_tasks.discard)

    async def on_download_click(e):
        urls = parse_input_urls(url_input.value or "")
        if not urls:
            show_toast("Please enter at least one media link.", is_error=True)
            return
        asyncio.create_task(trigger_download(urls))

    def on_add_queue_click(e):
        urls = parse_input_urls(url_input.value or "")
        if not urls:
            show_toast("Please enter at least one media link.", is_error=True)
            return
        opts = {}
        if audio_only_check.value:
            opts["extract_audio"] = True
        if video_only_check.value:
            opts["video_only"] = True
        if images_only_check.value:
            opts["images_only"] = True
        if resolution_dropdown.value and resolution_dropdown.value != "best":
            opts["resolution"] = resolution_dropdown.value

        added = queue_manager.add_batch(urls, options=opts)
        show_toast(f"Enqueued {len(added)} URL(s) to Queue.", is_success=True)
        url_input.value = ""
        safe_update(url_input)
        update_detected_engines()
        update_queue_stats_ui()

    def on_clear_input_click(e):
        url_input.value = ""
        safe_update(url_input)
        update_detected_engines()

    async def on_paste_input_click(e):
        try:
            import pyperclip
            val = pyperclip.paste()
            if val:
                curr = url_input.value or ""
                url_input.value = f"{curr}\n{val}".strip() if curr else val
                safe_update(url_input)
                update_detected_engines()
        except Exception as ex:
            show_toast(f"Clipboard paste error: {ex}", is_error=True)

    download_now_btn = ft.FilledButton(
        text="Download Now",
        icon=ft.Icons.ROCKET_LAUNCH_ROUNDED,
        style=ft.ButtonStyle(
            color=BG_COLOR,
            bgcolor=ACCENT_CYAN,
            shape=ft.RoundedRectangleBorder(radius=8),
        ),
        on_click=on_download_click,
    )

    add_queue_btn = ft.OutlinedButton(
        text="Add to Queue",
        icon=ft.Icons.ADD_TO_QUEUE_ROUNDED,
        style=ft.ButtonStyle(
            color=ACCENT_CYAN,
            side=ft.BorderSide(1, ACCENT_CYAN),
            shape=ft.RoundedRectangleBorder(radius=8),
        ),
        on_click=on_add_queue_click,
    )

    clear_input_btn = ft.TextButton(
        text="Clear",
        icon=ft.Icons.CLEAR_ALL_ROUNDED,
        icon_color=TEXT_DIM,
        on_click=on_clear_input_click,
    )

    paste_input_btn = ft.IconButton(
        icon=ft.Icons.CONTENT_PASTE_ROUNDED,
        icon_color=ACCENT_CYAN,
        tooltip="Paste from Clipboard",
        on_click=on_paste_input_click,
    )

    # Input card container
    input_card = ft.Container(
        bgcolor=CARD_BG,
        border=ft.Border.all(1, BORDER_COLOR),
        border_radius=12,
        padding=16,
        content=ft.Column(
            spacing=12,
            controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ft.Text("Media Links & Batch Ingestion", size=14, weight=ft.FontWeight.W_600, color=ft.Colors.WHITE),
                        paste_input_btn,
                    ],
                ),
                url_input,
                detected_engines_row,
                ft.Divider(color=BORDER_COLOR, height=1),
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ft.Row(
                            spacing=16,
                            controls=[
                                audio_only_check,
                                video_only_check,
                                images_only_check,
                                resolution_dropdown,
                            ],
                        ),
                        ft.Row(
                            spacing=8,
                            controls=[
                                clear_input_btn,
                                add_queue_btn,
                                download_now_btn,
                            ],
                        ),
                    ],
                ),
            ],
        ),
    )

    downloads_tab_view = ft.Container(
        padding=20,
        content=ft.Column(
            spacing=16,
            expand=True,
            controls=[
                input_card,
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ft.Text("Active Transfers", size=14, weight=ft.FontWeight.W_600, color=ft.Colors.WHITE),
                        ft.TextButton(
                            "Clear Finished",
                            icon=ft.Icons.CLEANING_SERVICES_ROUNDED,
                            icon_color=TEXT_DIM,
                            on_click=lambda _: gui_tracker.clear_finished(),
                        ),
                    ],
                ),
                active_downloads_column,
            ],
        ),
    )

    # ----------------------------------------------------
    # TAB 2: QUEUE MANAGER
    # ----------------------------------------------------
    queue_stat_total = ft.Text("0", size=18, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE)
    queue_stat_pending = ft.Text("0", size=18, weight=ft.FontWeight.BOLD, color=ACCENT_CYAN)
    queue_stat_completed = ft.Text("0", size=18, weight=ft.FontWeight.BOLD, color=ACCENT_GREEN)
    queue_stat_failed = ft.Text("0", size=18, weight=ft.FontWeight.BOLD, color=ACCENT_RED)

    def make_stat_card(label: str, value_control: ft.Control, icon_name: str, color_hex: str):
        return ft.Container(
            bgcolor=CARD_BG,
            border=ft.Border.all(1, BORDER_COLOR),
            border_radius=10,
            padding=14,
            expand=True,
            content=ft.Row(
                spacing=12,
                controls=[
                    ft.Container(
                        content=ft.Icon(icon_name, size=24, color=color_hex),
                        bgcolor=f"{color_hex}1A",
                        border_radius=8,
                        padding=8,
                    ),
                    ft.Column(
                        spacing=2,
                        controls=[
                            ft.Text(label, size=11, color=TEXT_DIM),
                            value_control,
                        ],
                    ),
                ],
            ),
        )

    queue_list_column = ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO, expand=True)

    def update_queue_stats_ui():
        items = queue_manager.list_all()
        total_count = len(items)
        pending_count = sum(1 for it in items if it.status in (TaskStatus.PENDING, TaskStatus.QUEUED))
        completed_count = sum(1 for it in items if it.status == TaskStatus.COMPLETED)
        failed_count = sum(1 for it in items if it.status == TaskStatus.FAILED)

        queue_stat_total.value = str(total_count)
        queue_stat_pending.value = str(pending_count)
        queue_stat_completed.value = str(completed_count)
        queue_stat_failed.value = str(failed_count)
        safe_update(queue_stat_total, queue_stat_pending, queue_stat_completed, queue_stat_failed)

        # Update queue items list
        queue_list_column.controls.clear()
        if not items:
            queue_list_column.controls.append(
                ft.Container(
                    alignment=ft.Alignment.CENTER,
                    padding=30,
                    content=ft.Text("Queue is empty. Add links above or import a file.", size=13, color=TEXT_DIM),
                )
            )
        else:
            for it in items:
                eng = it.backend or "auto"
                c = get_engine_color(eng)
                st_color = (
                    ACCENT_GREEN if it.status == TaskStatus.COMPLETED
                    else (ACCENT_RED if it.status == TaskStatus.FAILED
                    else (ACCENT_YELLOW if it.status == TaskStatus.DOWNLOADING
                    else ACCENT_CYAN))
                )

                row = ft.Container(
                    bgcolor=CARD_BG,
                    border=ft.Border.all(1, BORDER_COLOR),
                    border_radius=8,
                    padding=10,
                    content=ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        controls=[
                            ft.Row(
                                spacing=8,
                                expand=True,
                                controls=[
                                    ft.Container(
                                        content=ft.Text(eng.upper(), size=10, color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD),
                                        bgcolor=c,
                                        border_radius=4,
                                        padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                                    ),
                                    ft.Text(it.url, size=12, color=ft.Colors.WHITE, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, expand=True),
                                ],
                            ),
                            ft.Row(
                                spacing=8,
                                controls=[
                                    ft.Container(
                                        content=ft.Text(it.status.value.upper(), size=10, color=st_color, weight=ft.FontWeight.W_600),
                                        bgcolor=f"{st_color}26",
                                        border_radius=6,
                                        padding=ft.Padding.symmetric(horizontal=8, vertical=2),
                                    ),
                                    ft.IconButton(
                                        icon=ft.Icons.DELETE_OUTLINE_ROUNDED,
                                        icon_size=16,
                                        icon_color=TEXT_DIM,
                                        tooltip="Remove Item",
                                        on_click=lambda _, item_id=it.id: on_remove_queue_item(item_id),
                                    ),
                                ],
                            ),
                        ],
                    ),
                )
                queue_list_column.controls.append(row)
        safe_update(queue_list_column)

    def on_remove_queue_item(item_id: str):
        queue_manager.remove(item_id)
        update_queue_stats_ui()
        show_toast("Item removed from queue.")

    async def on_start_queue_click(e):
        pending_items = queue_manager.get_pending_items()
        if not pending_items:
            show_toast("No pending items in queue.", is_error=True)
            return
        asyncio.create_task(trigger_download(None))

    def on_retry_failed_click(e):
        failed_links = load_failed_urls()
        if not failed_links:
            show_toast("No failed links found in data/failed_links.txt.", is_error=True)
            return
        queue_manager.add_batch(failed_links)
        update_queue_stats_ui()
        show_toast(f"Re-enqueued {len(failed_links)} failed link(s).", is_success=True)

    def on_clear_queue_click(e):
        queue_manager.clear()
        update_queue_stats_ui()
        show_toast("Queue cleared.")

    queue_tab_view = ft.Container(
        padding=20,
        content=ft.Column(
            spacing=16,
            expand=True,
            controls=[
                ft.Row(
                    spacing=12,
                    controls=[
                        make_stat_card("Total in Queue", queue_stat_total, ft.Icons.QUEUE_MUSIC_ROUNDED, ACCENT_YELLOW),
                        make_stat_card("Pending", queue_stat_pending, ft.Icons.HOURGLASS_EMPTY_ROUNDED, ACCENT_CYAN),
                        make_stat_card("Completed", queue_stat_completed, ft.Icons.CHECK_CIRCLE_OUTLINE_ROUNDED, ACCENT_GREEN),
                        make_stat_card("Failed", queue_stat_failed, ft.Icons.ERROR_OUTLINE_ROUNDED, ACCENT_RED),
                    ],
                ),
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ft.Text("Queued Transfers", size=14, weight=ft.FontWeight.W_600, color=ft.Colors.WHITE),
                        ft.Row(
                            spacing=8,
                            controls=[
                                ft.FilledButton(
                                    text="Start Queue",
                                    icon=ft.Icons.PLAY_ARROW_ROUNDED,
                                    style=ft.ButtonStyle(bgcolor=ACCENT_GREEN, color=BG_COLOR),
                                    on_click=on_start_queue_click,
                                ),
                                ft.OutlinedButton(
                                    text="Retry Failed",
                                    icon=ft.Icons.REFRESH_ROUNDED,
                                    style=ft.ButtonStyle(color=ACCENT_YELLOW, side=ft.BorderSide(1, ACCENT_YELLOW)),
                                    on_click=on_retry_failed_click,
                                ),
                                ft.TextButton(
                                    text="Clear All",
                                    icon=ft.Icons.DELETE_SWEEP_ROUNDED,
                                    icon_color=TEXT_DIM,
                                    on_click=on_clear_queue_click,
                                ),
                            ],
                        ),
                    ],
                ),
                queue_list_column,
            ],
        ),
    )

    # ----------------------------------------------------
    # TAB 3: ARCHIVE & DEDUPLICATION BROWSER
    # ----------------------------------------------------
    archive_stat_total = ft.Text("0", size=18, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE)
    archive_stat_bytes = ft.Text("0 B", size=18, weight=ft.FontWeight.BOLD, color=ACCENT_CYAN)
    archive_stat_top_engine = ft.Text("None", size=18, weight=ft.FontWeight.BOLD, color=ACCENT_YELLOW)

    archive_search_input = ft.TextField(
        hint_text="Search history by title, filename, URL, or domain...",
        prefix_icon=ft.Icons.SEARCH_ROUNDED,
        border_color=BORDER_COLOR,
        focused_border_color=ACCENT_CYAN,
        text_size=12,
        expand=True,
    )
    archive_engine_filter = ft.Dropdown(
        label="Engine",
        value="all",
        width=150,
        options=[
            ft.dropdown.Option("all", "All Backends"),
            ft.dropdown.Option("yt-dlp", "yt-dlp"),
            ft.dropdown.Option("gallery-dl", "gallery-dl"),
            ft.dropdown.Option("telegram-dl", "telegram-dl"),
            ft.dropdown.Option("terabox-dl", "terabox-dl"),
            ft.dropdown.Option("cyberdrop-dl", "cyberdrop-dl"),
        ],
        border_color=BORDER_COLOR,
        focused_border_color=ACCENT_CYAN,
        text_size=12,
    )

    archive_list_column = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO, expand=True)

    def update_archive_table_ui(e=None):
        stats = archive.get_stats()
        archive_stat_total.value = str(stats.get("total_count", 0))
        archive_stat_bytes.value = format_bytes(stats.get("total_bytes", 0))
        by_backend = stats.get("by_backend", {})
        top_eng = max(by_backend.items(), key=lambda x: x[1])[0] if by_backend else "None"
        archive_stat_top_engine.value = top_eng.upper()

        safe_update(archive_stat_total, archive_stat_bytes, archive_stat_top_engine)

        # Query entries
        query = (archive_search_input.value or "").strip()
        eng_filter = archive_engine_filter.value or "all"
        backend_arg = None if eng_filter == "all" else eng_filter

        entries: List[ArchiveEntry] = []
        if query:
            entries = archive.search(query, backend=backend_arg)
        else:
            entries = archive.list_recent(limit=50, backend=backend_arg)

        archive_list_column.controls.clear()
        if not entries:
            archive_list_column.controls.append(
                ft.Container(
                    alignment=ft.Alignment.CENTER,
                    padding=30,
                    content=ft.Text("No archive entries found matching query.", size=13, color=TEXT_DIM),
                )
            )
        else:
            for en in entries:
                eng = en.backend or "auto"
                c = get_engine_color(eng)
                ts_str = en.downloaded_at.strftime("%Y-%m-%d %H:%M") if hasattr(en.downloaded_at, "strftime") else str(en.downloaded_at)[:16]
                size_str = format_bytes(en.file_size) if en.file_size else "-- MB"

                row = ft.Container(
                    bgcolor=CARD_BG,
                    border=ft.Border.all(1, BORDER_COLOR),
                    border_radius=8,
                    padding=10,
                    content=ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        controls=[
                            ft.Row(
                                spacing=10,
                                expand=True,
                                controls=[
                                    ft.Container(
                                        content=ft.Text(eng.upper(), size=9, color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD),
                                        bgcolor=c,
                                        border_radius=4,
                                        padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                                    ),
                                    ft.Column(
                                        spacing=2,
                                        expand=True,
                                        controls=[
                                            ft.Text(en.file_name or en.url, size=13, color=ft.Colors.WHITE, weight=ft.FontWeight.W_500, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                                            ft.Text(f"{ts_str} • {size_str} • {en.url}", size=11, color=TEXT_DIM, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                                        ],
                                    ),
                                ],
                            ),
                            ft.Row(
                                spacing=4,
                                controls=[
                                    ft.IconButton(
                                        icon=ft.Icons.FOLDER_OPEN_ROUNDED,
                                        icon_size=16,
                                        icon_color=ACCENT_CYAN,
                                        tooltip="Show in Folder",
                                        on_click=lambda _, p=en.file_path: open_in_file_manager(Path(p)),
                                    ),
                                    ft.IconButton(
                                        icon=ft.Icons.CONTENT_COPY_ROUNDED,
                                        icon_size=16,
                                        icon_color=TEXT_DIM,
                                        tooltip="Copy URL",
                                        on_click=lambda _, u=en.url: (setattr(page, "clipboard", u), show_toast("URL copied to clipboard.")),
                                    ),
                                ],
                            ),
                        ],
                    ),
                )
                archive_list_column.controls.append(row)

        safe_update(archive_list_column)

    archive_search_input.on_change = update_archive_table_ui
    archive_engine_filter.on_change = update_archive_table_ui

    archive_tab_view = ft.Container(
        padding=20,
        content=ft.Column(
            spacing=16,
            expand=True,
            controls=[
                ft.Row(
                    spacing=12,
                    controls=[
                        make_stat_card("Total Archived Items", archive_stat_total, ft.Icons.INVENTORY_2_ROUNDED, ACCENT_CYAN),
                        make_stat_card("Data Transferred", archive_stat_bytes, ft.Icons.DATA_SAVER_ON_ROUNDED, ACCENT_GREEN),
                        make_stat_card("Top Download Engine", archive_stat_top_engine, ft.Icons.AUTO_GRAPH_ROUNDED, ACCENT_YELLOW),
                    ],
                ),
                ft.Row(
                    spacing=10,
                    controls=[
                        archive_search_input,
                        archive_engine_filter,
                        ft.IconButton(
                            icon=ft.Icons.REFRESH_ROUNDED,
                            icon_color=ACCENT_CYAN,
                            tooltip="Refresh Archive",
                            on_click=update_archive_table_ui,
                        ),
                    ],
                ),
                archive_list_column,
            ],
        ),
    )

    # ----------------------------------------------------
    # TAB 4: SETTINGS & CONFIGURATION
    # ----------------------------------------------------
    cfg_dl_dir = ft.TextField(
        label="Download Directory",
        value=str(config.download_dir),
        border_color=BORDER_COLOR,
        focused_border_color=ACCENT_CYAN,
        text_size=12,
        expand=True,
    )
    cfg_concurrency = ft.Slider(
        min=1,
        max=8,
        divisions=7,
        value=float(config.get("general", "concurrent_downloads", 3)),
        label="{value} streams",
        active_color=ACCENT_CYAN,
    )
    cfg_notifications = ft.Switch(
        label="Sound Notifications & Completion Chimes",
        value=config.notifications_enabled,
        active_color=ACCENT_CYAN,
    )
    cfg_organize_backend = ft.Checkbox(
        label="Organize by Backend folder (downloads/<backend>/...)",
        value=config.get("general", "organize_by_backend", True),
        active_color=ACCENT_CYAN,
    )
    cfg_organize_site = ft.Checkbox(
        label="Organize by Domain/Site (downloads/<backend>/<site>/...)",
        value=config.get("general", "organize_by_site", True),
        active_color=ACCENT_CYAN,
    )
    cfg_proxy = ft.TextField(
        label="HTTP / SOCKS5 Proxy (e.g. http://127.0.0.1:7890)",
        value=config.get("network", "proxy") or "",
        border_color=BORDER_COLOR,
        focused_border_color=ACCENT_CYAN,
        text_size=12,
        expand=True,
    )

    # Engine configs
    cfg_ytdlp_res = ft.Dropdown(
        label="yt-dlp Default Resolution",
        value=config.get("yt_dlp", "preferred_resolution", "best"),
        options=[
            ft.dropdown.Option("best", "Best"),
            ft.dropdown.Option("1080p", "1080p"),
            ft.dropdown.Option("720p", "720p"),
            ft.dropdown.Option("480p", "480p"),
        ],
        border_color=BORDER_COLOR,
        focused_border_color=ACCENT_CYAN,
        text_size=12,
    )
    cfg_ytdlp_sponsor = ft.Checkbox(
        label="yt-dlp: Remove Sponsors (SponsorBlock)",
        value=bool(config.get("yt_dlp", "sponsorblock", False)),
        active_color=ACCENT_CYAN,
    )
    cfg_gdl_cookies = ft.Dropdown(
        label="gallery-dl: Browser Cookies",
        value=config.get("gallery_dl", "fallback_browser_cookies", "firefox") or "firefox",
        options=[
            ft.dropdown.Option("firefox", "Firefox"),
            ft.dropdown.Option("chrome", "Chrome"),
            ft.dropdown.Option("edge", "Edge"),
            ft.dropdown.Option("brave", "Brave"),
            ft.dropdown.Option("opera", "Opera"),
        ],
        border_color=BORDER_COLOR,
        focused_border_color=ACCENT_CYAN,
        text_size=12,
    )
    cfg_terabox_ndus = ft.TextField(
        label="terabox-dl: ndus Cookie Token",
        value=config.get("terabox_dl", "ndus_cookie", "") or "",
        border_color=BORDER_COLOR,
        focused_border_color=ACCENT_CYAN,
        password=True,
        can_reveal_password=True,
        text_size=12,
    )
    cfg_tg_api_id = ft.TextField(
        label="telegram-dl: API ID",
        value=str(config.get("telegram_dl", "api_id", "") or ""),
        border_color=BORDER_COLOR,
        focused_border_color=ACCENT_CYAN,
        text_size=12,
    )
    cfg_tg_api_hash = ft.TextField(
        label="telegram-dl: API Hash",
        value=str(config.get("telegram_dl", "api_hash", "") or ""),
        border_color=BORDER_COLOR,
        focused_border_color=ACCENT_CYAN,
        password=True,
        can_reveal_password=True,
        text_size=12,
    )

    async def on_save_settings_click(e):
        nonlocal router, dispatcher
        if is_downloading:
            show_toast("Save settings after the active batch finishes.", is_error=True)
            return
        try:
            config.set("general", "download_dir", cfg_dl_dir.value.strip())
            config.set("general", "concurrent_downloads", int(cfg_concurrency.value))
            config.set("general", "notifications", cfg_notifications.value)
            config.set("general", "play_sound", cfg_notifications.value)
            config.set("general", "organize_by_backend", cfg_organize_backend.value)
            config.set("general", "organize_by_site", cfg_organize_site.value)

            proxy_val = cfg_proxy.value.strip() if cfg_proxy.value else None
            config.set("network", "proxy", proxy_val)

            config.set("yt_dlp", "preferred_resolution", cfg_ytdlp_res.value)
            config.set("yt_dlp", "sponsorblock", cfg_ytdlp_sponsor.value)
            config.set("gallery_dl", "fallback_browser_cookies", cfg_gdl_cookies.value)
            config.set("terabox_dl", "ndus_cookie", cfg_terabox_ndus.value.strip())
            config.set("telegram_dl", "api_id", cfg_tg_api_id.value.strip())
            config.set("telegram_dl", "api_hash", cfg_tg_api_hash.value.strip())

            config.save()
            for backend in router.backends.values():
                if hasattr(backend, 'close'):
                    await backend.close()
            router = URLRouter.create_default(config=config)
            dispatcher = QueueDispatcher(config=config, queue_manager=queue_manager, archive=archive, router=router)
            show_toast("Configuration saved successfully!", is_success=True)
        except Exception as ex:
            show_toast(f"Failed to save configuration: {ex}", is_error=True)

    settings_tab_view = ft.Container(
        padding=20,
        content=ft.Column(
            spacing=16,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
            controls=[
                ft.Text("General & Concurrency", size=15, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
                ft.Container(
                    bgcolor=CARD_BG,
                    border=ft.Border.all(1, BORDER_COLOR),
                    border_radius=10,
                    padding=16,
                    content=ft.Column(
                        spacing=12,
                        controls=[
                            cfg_dl_dir,
                            ft.Text("Parallel Download Streams:", size=12, color=TEXT_DIM),
                            cfg_concurrency,
                            ft.Row(spacing=20, controls=[cfg_organize_backend, cfg_organize_site]),
                            cfg_notifications,
                        ],
                    ),
                ),
                ft.Text("Network & Proxy", size=15, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
                ft.Container(
                    bgcolor=CARD_BG,
                    border=ft.Border.all(1, BORDER_COLOR),
                    border_radius=10,
                    padding=16,
                    content=ft.Column(
                        spacing=12,
                        controls=[
                            cfg_proxy,
                        ],
                    ),
                ),
                ft.Text("Engine Authentication & Defaults", size=15, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
                ft.Container(
                    bgcolor=CARD_BG,
                    border=ft.Border.all(1, BORDER_COLOR),
                    border_radius=10,
                    padding=16,
                    content=ft.Column(
                        spacing=12,
                        controls=[
                            ft.Row(spacing=16, controls=[cfg_ytdlp_res, cfg_ytdlp_sponsor]),
                            cfg_gdl_cookies,
                            cfg_terabox_ndus,
                            ft.Row(spacing=16, controls=[cfg_tg_api_id, cfg_tg_api_hash]),
                        ],
                    ),
                ),
                ft.Row(
                    alignment=ft.MainAxisAlignment.END,
                    controls=[
                        ft.FilledButton(
                            text="Save Settings",
                            icon=ft.Icons.SAVE_ROUNDED,
                            style=ft.ButtonStyle(bgcolor=ACCENT_CYAN, color=BG_COLOR),
                            on_click=on_save_settings_click,
                        ),
                    ],
                ),
            ],
        ),
    )

    # ----------------------------------------------------
    # Clipboard Auto-Watcher Task
    # ----------------------------------------------------
    async def clipboard_watcher_loop():
        nonlocal clipboard_watcher_running
        watcher = ClipboardWatcher(poll_interval=0.4, play_sound=config.notifications_enabled)
        show_toast("Clipboard Auto-Watcher is now ACTIVE. Copied media links will download automatically.", is_success=True)

        try:
            while not clipboard_stop_event.is_set():
                new_urls = watcher.poll_new_urls()
                if new_urls:
                    show_toast(f"Detected copied link: {new_urls[0]}", is_success=True)
                    asyncio.create_task(trigger_download(new_urls))
                await asyncio.sleep(0.4)
        except asyncio.CancelledError:
            pass
        finally:
            clipboard_watcher_running = False

    async def on_watcher_switch_change(e):
        nonlocal clipboard_watcher_running, clipboard_stop_event
        if watcher_switch.value:
            clipboard_watcher_running = True
            clipboard_stop_event.clear()
            asyncio.create_task(clipboard_watcher_loop())
        else:
            clipboard_stop_event.set()
            show_toast("Clipboard Auto-Watcher stopped.")

    watcher_switch.on_change = on_watcher_switch_change

    # ----------------------------------------------------
    # Navigation Tabs Setup
    # ----------------------------------------------------
    tabs = ft.Tabs(
        selected_index=0,
        animation_duration=200,
        indicator_color=ACCENT_CYAN,
        label_color=ACCENT_CYAN,
        unselected_label_color=TEXT_DIM,
        divider_color=BORDER_COLOR,
        expand=True,
        tabs=[
            ft.Tab(
                text="Downloads",
                icon=ft.Icons.DOWNLOAD_ROUNDED,
                content=downloads_tab_view,
            ),
            ft.Tab(
                text="Queue",
                icon=ft.Icons.FORMAT_LIST_BULLETED_ROUNDED,
                content=queue_tab_view,
            ),
            ft.Tab(
                text="Archive",
                icon=ft.Icons.INVENTORY_2_ROUNDED,
                content=archive_tab_view,
            ),
            ft.Tab(
                text="Settings",
                icon=ft.Icons.SETTINGS_ROUNDED,
                content=settings_tab_view,
            ),
        ],
    )

    async def close_downloads(e=None):
        clipboard_stop_event.set()
        tasks = list(active_download_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for backend in router.backends.values():
            if hasattr(backend, 'close'):
                await backend.close()

    page.on_disconnect = close_downloads
    page.window.prevent_close = True
    async def on_window_event(e):
        if e.data == 'close':
            await close_downloads()
            page.window.destroy()
    page.window.on_event = on_window_event

    # Initial UI state sync
    update_detected_engines()
    update_queue_stats_ui()
    update_archive_table_ui()

    # Assemble Main Page
    page.add(
        header,
        tabs,
    )

    try:
        page.update()
    except Exception:
        pass
    logger.info("MULTI_DOWNLOADER GUI initialized successfully.")


if __name__ == "__main__":
    if hasattr(ft, "app"):
        ft.app(target=main)
    elif hasattr(ft, "run"):
        ft.run(main)

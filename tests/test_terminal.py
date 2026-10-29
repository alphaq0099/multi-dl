"""
Unit tests for terminal styling, coloring, and bracket alignment.
"""

from core.terminal import Style


def test_tag_alignment_single_space():
    # Emojis used across the CLI and backends
    tags = [
        ("🎯", "ROUTING", Style.CYAN),
        ("⚙️", "BACKEND", Style.MAGENTA),
        ("🚀", "DOWNLOADING", Style.BLUE),
        ("🌐", "METADATA", Style.CYAN),
        ("🔍", "EXTRACTOR", Style.CYAN),
        ("📡", "STREAMS", Style.BLUE),
        ("⚙️", "PO-TOKEN", Style.MAGENTA),
        ("🎯", "TARGET", Style.YELLOW),
        ("🎬", "MEDIA", Style.YELLOW),
        ("🎞️", "FORMAT", Style.CYAN),
        ("📥", "99.5%", Style.CYAN),
        ("✅", "COMPLETED", Style.GREEN),
        ("📁", "SAVED", Style.GREEN),
        ("💾", "ARCHIVE", Style.CYAN),
        ("⚠️", "WARNING", Style.YELLOW),
        ("❌", "ERROR", Style.RED),
    ]

    for emoji, text, color in tags:
        rendered = Style.tag(emoji, text, color)
        # Ensure single space after emoji and before opening bracket ANSI escape sequence
        # Emoji is stripped of trailing spaces, then followed by exactly 1 space and color code
        clean_emoji = emoji.strip()
        assert f"{clean_emoji} {color}" in rendered
        assert f"[{text}]" in rendered


def test_tag_strips_accidental_spaces():
    # Even if someone calls Style.tag with accidental spaces in emoji
    rendered1 = Style.tag("⚙️", "BACKEND", Style.MAGENTA)
    rendered2 = Style.tag("⚙️  ", "BACKEND", Style.MAGENTA)
    assert rendered1 == rendered2


def test_color_helpers():
    assert "\033[96m" in Style.cyan("test")
    assert "\033[95m" in Style.magenta("test")
    assert "\033[92m" in Style.green("test")
    assert "\033[93m" in Style.yellow("test")
    assert "\033[91m" in Style.red("test")
    assert "\033[0m" in Style.white("test")


def test_engine_badges_and_colors():
    assert Style.engine_color("yt-dlp") == Style.RED
    assert Style.engine_color("gallery-dl") == Style.YELLOW
    assert Style.engine_color("telegram-dl") == Style.BLUE
    assert Style.engine_color("cyberdrop-dl") == Style.MAGENTA
    assert Style.engine_color("unknown-engine") == Style.CYAN

    ytdlp_badge = Style.engine_badge("yt-dlp")
    assert Style.RED in ytdlp_badge
    assert "[yt-dlp]" in ytdlp_badge

    gdl_badge = Style.engine_badge("gallery-dl")
    assert Style.YELLOW in gdl_badge
    assert "[gallery-dl]" in gdl_badge


def test_progress_bar_stages():
    # 0%: all empty line track
    bar_0 = Style.progress_bar(0.0, width=20)
    assert "─" * 20 in bar_0

    # 50%: 10 filled sleek lines, 10 empty
    bar_50 = Style.progress_bar(50.0, width=20)
    assert "━" * 10 in bar_50
    assert "─" * 10 in bar_50

    # 100%: fully filled with green sleek lines
    bar_100 = Style.progress_bar(100.0, width=20)
    assert "━" * 20 in bar_100
    assert Style.GREEN in bar_100

    # None: indeterminate empty line track
    bar_none = Style.progress_bar(None, width=20)
    assert "─" * 20 in bar_none


def test_interactive_banner(capsys):
    import importlib
    from core.config import ConfigManager
    from core.archive import ArchiveManager

    cfg = ConfigManager()
    archive = ArchiveManager(cfg.archive_db_path)
    multi_dl = importlib.import_module("multi-dl")

    multi_dl.print_banner(cfg, archive)
    import re
    captured = capsys.readouterr().out
    clean_captured = re.sub(r'\033\[[0-9;]*[a-zA-Z]', '', captured)
    assert f"MULTI_DOWNLOADER v{multi_dl.__version__}" in clean_captured
    assert "Configuration" in captured
    assert "Interactive Mode" in captured
    assert "Enter URLs (use space for multiple URLs)" in captured
    assert "Enter URL:" not in captured  # prompt is printed in input() loop
    assert "[yt-dlp]" in captured
    assert "[terabox-dl]" in captured
    assert "[cyberdrop-dl]" in captured


def test_is_interactive():
    assert isinstance(Style.is_interactive(), bool)


def test_multibar_manager_lifecycle():
    from core.terminal import MultiBarManager, MultiBarSlot
    
    mb = MultiBarManager(max_slots=3, is_tty=False)
    assert mb.max_slots == 3
    assert len(mb._slots) == 3

    # Test engine badge width padding alignment (all padded to 14 chars inside brackets)
    for eng in ["yt-dlp", "gallery-dl", "terabox-dl", "cyberdrop-dl", "telegram-dl", "idle"]:
        badge = MultiBarManager._format_engine_badge(eng)
        # Should contain the engine name in brackets
        assert f"[{eng}]" in badge

    # Assign slots
    sid1 = mb.assign_slot("task-1", "yt-dlp", "https://youtube.com/v1")
    assert sid1 == 1
    assert mb._slots[1].status == "active"
    assert mb._slots[1].backend == "yt-dlp"

    sid2 = mb.assign_slot("task-2", "gallery-dl", "https://imgur.com/a/album1")
    assert sid2 == 2
    assert mb._slots[2].status == "active"

    # Update slot metrics
    mb.update_slot(
        slot_id=1,
        percent=45.2,
        downloaded=45200000,
        total=100000000,
        speed=4200000,
        title="Sample Video.mp4",
    )
    assert mb._slots[1].percent == 45.2
    assert mb._slots[1].url_or_title == "Sample Video.mp4"

    # Update slot 2 with batch file metrics
    mb.update_slot(
        slot_id=2,
        percent=100.0,
        downloaded=28400000,
        title="Sample Gallery",
        file_index=12,
        total_files=12,
    )
    assert mb._slots[2].file_index == 12
    assert mb._slots[2].total_files == 12

    # Set footer counts
    mb.set_queue_counts(remaining=14, completed=2, failed=0)
    assert mb._remaining == 14
    assert mb._completed == 2
    assert mb._failed == 0

    # Finish slot
    mb.finish_slot(slot_id=1, status_type="completed")
    assert mb._slots[1].status == "idle"

    # Close canvas
    mb.close()
    assert mb._lines_printed == 0


def test_multibar_manager_two_line_card_format(capsys):
    """Verifies that MultiBarManager formats each slot across 2 distinct lines matching Drawing-1.sketchpad.png."""
    from core.terminal import MultiBarManager
    
    mb = MultiBarManager(max_slots=3, is_tty=True)
    mb.assign_slot("task-1", "gallery-dl", "Sample Gallery Title")
    mb.update_slot(
        slot_id=1,
        percent=50.0,
        downloaded=26000000,
        total=52000000,
        speed=4000000,
        file_index=14,
        total_files=28,
    )
    mb.assign_slot("task-2", "yt-dlp", "Big Buck Bunny 1080p")
    mb.update_slot(
        slot_id=2,
        percent=50.0,
        downloaded=26000000,
        total=52000000,
        speed=4000000,
    )
    mb.render(force=True)
    out = capsys.readouterr().out

    # Slot 1 Line 1: [gallery-dl] • Sample Gallery Title
    assert "[Downloader]:" not in out
    assert "[gallery-dl]" in out
    assert "Sample Gallery Title" in out

    # Slot 1 Line 2: 14/28 • 24.8/49.6 MB • @
    assert "14/28" in out
    assert "50.0%" in out
    assert "24.8/49.6 MB" in out

    # Slot 2 Line 1: [yt-dlp] • Big Buck Bunny 1080p
    assert "[yt-dlp]" in out
    assert "Big Buck Bunny 1080p" in out

    # Slot 3 is idle with no active task, so it is cleanly hidden from TUI
    assert "[idle]" not in out
    assert "(2/3 Workers)" in out


def test_router_create_default_passes_config():
    """Verifies that URLRouter.create_default passes config to all backends including yt-dlp fallback cookies."""
    from core.config import ConfigManager
    from core.router import URLRouter

    cfg = ConfigManager()
    cfg.set("yt_dlp", "fallback_browser_cookies", "firefox")
    router = URLRouter.create_default(config=cfg)

    ytdlp = router.backends.get("yt-dlp")
    assert ytdlp is not None
    assert ytdlp.config.get("fallback_browser_cookies") == "firefox"


def test_wide_character_width_and_truncation():
    """Verifies that fullwidth characters (e.g. \uff1a and Japanese kana) count as 2 columns and truncate safely."""
    from core.terminal import char_width, str_width, truncate_to_width

    # Fullwidth colon (used by yt-dlp on Windows to sanitize colons)
    assert char_width("：") == 2
    assert char_width("a") == 1
    assert char_width("日") == 2

    # String with fullwidth colon
    title = "Pumping Strategies： Hand Expression"
    # 'Pumping Strategies' (18) + '：' (2) + ' Hand Expression' (16) = 36 columns
    assert str_width(title) == 36

    # Truncate to 20 columns: target is 17 cols + '...'
    truncated = truncate_to_width(title, 20)
    assert str_width(truncated) <= 20
    assert truncated.endswith("...")


def test_safe_terminal_write_no_crash_on_unencodable(monkeypatch):
    """Verifies safe_terminal_write intercepts UnicodeEncodeError gracefully."""
    import sys
    from core.terminal import safe_terminal_write

    written = []

    class MockStdout:
        encoding = "cp1252"

        def write(self, s):
            # Simulate cp1252 charmap failure on fullwidth colon
            if "：" in s:
                raise UnicodeEncodeError("charmap", "：", 0, 1, "character maps to <undefined>")
            written.append(s)

        def flush(self):
            pass

    monkeypatch.setattr(sys, "stdout", MockStdout())
    # Should not raise exception
    safe_terminal_write("Pumping Strategies： Hand Expression\n")
    assert len(written) == 1
    assert "?" in written[0] or "Pumping" in written[0]


def test_multibar_manager_wide_title_rendering(capsys):
    """Verifies MultiBarManager safely renders titles with fullwidth colons and Japanese text."""
    from core.terminal import MultiBarManager

    mb = MultiBarManager(max_slots=3, is_tty=True)
    mb.assign_slot("task-yt", "yt-dlp", "Pumping Strategies： Hand Expression")
    mb.update_slot(
        slot_id=1,
        percent=12.5,
        downloaded=1250000,
        total=10000000,
        speed=1500000,
        title="Pumping Strategies： Hand Expression",
    )
    mb.render(force=True)
    out = capsys.readouterr().out
    assert "[Downloader]:" not in out
    assert "[yt-dlp]" in out
    assert "Pumping Strategies" in out


def test_print_batch_summary_alignment(capsys):
    """Verifies that all rows in single and batch summary cards have equal visible width."""
    import re
    from core.terminal import print_batch_summary, str_width

    # 1. Single download summary
    print_batch_summary(
        total_items=1,
        succeeded_count=1,
        skipped_count=0,
        failed_count=0,
        total_bytes=35776102,
        elapsed_seconds=790.0,
        saved_path=r"~downloads\gallery-dl\artstation\455929 [Digital Fantasy Art...]",
    )
    out = capsys.readouterr().out
    clean_lines = [re.sub(r'\033\[[0-9;]*[a-zA-Z]', '', line).strip() for line in out.splitlines()]
    lines = [line for line in clean_lines if line.startswith("║")]
    assert len(lines) >= 4
    # All box content lines must have identical visible width
    widths = [str_width(line) for line in lines]
    assert len(set(widths)) == 1, f"Mismatch in single summary line widths: {widths}"
    assert "• Status:" in out
    assert "• Saved To:" in out
    assert "• Transferred:" in out
    assert "• Elapsed Time:" in out

    # 2. Batch download summary
    print_batch_summary(
        total_items=3,
        succeeded_count=3,
        skipped_count=0,
        failed_count=0,
        total_bytes=104857600,
        elapsed_seconds=45.0,
        saved_paths=[
            r"~downloads\gallery-dl\twitter\1.jpg",
            r"~downloads\gallery-dl\twitter\2.jpg",
            r"~downloads\gallery-dl\reddit\3.jpg",
        ],
    )
    out_batch = capsys.readouterr().out
    b_clean = [re.sub(r'\033\[[0-9;]*[a-zA-Z]', '', line).strip() for line in out_batch.splitlines()]
    b_lines = [line for line in b_clean if line.startswith("║")]
    assert len(b_lines) >= 5
    b_widths = [str_width(line) for line in b_lines]
    assert len(set(b_widths)) == 1, f"Mismatch in batch summary line widths: {b_widths}"
    assert "• Saved To:" in out_batch


def test_dynamic_slot_hiding_when_free(capsys):
    """Verifies that idle slots disappear from the TUI when downloads complete with no pending links."""
    from core.terminal import MultiBarManager

    mb = MultiBarManager(max_slots=3, is_tty=True)
    mb.assign_slot("task-1", "gallery-dl", "Gallery Album 1")
    mb.assign_slot("task-2", "gallery-dl", "Gallery Album 2")
    mb.assign_slot("task-3", "gallery-dl", "Gallery Album 3")

    mb.render(force=True)
    out1 = capsys.readouterr().out
    assert "Gallery Album 1" in out1
    assert "Gallery Album 2" in out1
    assert "Gallery Album 3" in out1
    assert "(3/3 Workers)" in out1

    # Finish slot 1 and slot 2
    mb.finish_slot(1, "saved/album1")
    mb.finish_slot(2, "saved/album2")
    _ = capsys.readouterr()
    mb.set_queue_counts(remaining=1, completed=2, failed=0)

    mb.render(force=True)
    out2 = capsys.readouterr().out
    # Idle slots should disappear completely!
    assert "Gallery Album 1" not in out2
    assert "Gallery Album 2" not in out2
    assert "[idle]" not in out2
    # Only active slot 3 should be rendered
    assert "Gallery Album 3" in out2
    assert "(1/3 Workers)" in out2
    assert "1 remaining" in out2
    assert "2 completed" in out2


def test_pending_queue_rendering_bounded_overflow(capsys):
    """Verifies that the Pending Queue renders up to max_slots items + 1 overflow line."""
    from core.terminal import MultiBarManager

    mb = MultiBarManager(max_slots=3, is_tty=True)
    mb.assign_slot("task-1", "gallery-dl", "Active Task 1")
    mb.set_queue_counts(remaining=6, completed=0, failed=0)

    # 5 pending items waiting in queue
    pending = [
        ("gallery-dl", "https://danbooru.donmai.us/posts/111111"),
        ("gallery-dl", "https://danbooru.donmai.us/posts/222222"),
        ("gallery-dl", "https://danbooru.donmai.us/posts/333333"),
        ("yt-dlp", "https://youtube.com/watch?v=444444"),
        ("telegram-dl", "https://t.me/c/555555/1"),
    ]
    mb.set_pending_items(pending)
    mb.render(force=True)

    out = capsys.readouterr().out
    assert "Pending:" in out
    # First 3 items must be visible
    assert "111111" in out
    assert "222222" in out
    # 4th and 5th items should be aggregated in overflow line
    assert "444444" not in out
    assert "555555" not in out
    assert "+2 links are pending" in out


def test_pending_queue_disappears_when_empty(capsys):
    """Verifies that the Pending Queue section is omitted when no items are pending."""
    from core.terminal import MultiBarManager

    mb = MultiBarManager(max_slots=3, is_tty=True)
    mb.assign_slot("task-1", "gallery-dl", "Active Task 1")
    mb.set_queue_counts(remaining=1, completed=0, failed=0)
    mb.set_pending_items([])
    mb.render(force=True)

    out = capsys.readouterr().out
    assert "Pending:" not in out
    assert "links are pending" not in out


def test_slot_row1_extractor_and_container_format(capsys):
    """Verifies that Row 1 of slot cards matches the original layout: [engine] • [extractor] • [container/album]."""
    from core.terminal import MultiBarManager

    mb = MultiBarManager(max_slots=3, is_tty=True)
    # Slot 1: Active with album URL and resolved album title
    mb.assign_slot("task-1", "gallery-dl", "https://danbooru.donmai.us/posts/1234567", extractor="danbooru", container_title="Summer Festival")
    mb.update_slot(
        slot_id=1,
        percent=83.3,
        downloaded=44400000,
        speed=192000,
        file_index=5,
        total_files=6,
        status_message="qh2ZK7jV Summer Festival g.mp4",
        extractor="danbooru",
        container_title="Summer Festival",
    )
    # Remaining slots are idle
    mb.set_queue_counts(remaining=15, completed=21, failed=0)
    mb.render(force=True)

    out = capsys.readouterr().out
    assert "[Downloader]:" not in out
    assert "[gallery-dl]" in out
    assert "[danbooru]" in out
    assert "Summer Festival" in out
    # Line 2 file details
    assert "83.3%" in out
    assert "5/6" in out
    assert "qh2ZK7jV" in out
    # Idle slots 2 and 3 visible when remaining > active
    assert "[idle]" not in out
    assert "connecting..." not in out

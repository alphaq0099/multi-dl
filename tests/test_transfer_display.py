"""Regression checks for truthful phases and bounded terminal cards."""
import os
import re
from unittest.mock import MagicMock

import pytest

from core.archive import ArchiveManager
from core.config import ConfigManager
from core.dispatcher import QueueDispatcher
from core.gui_tracker import GuiMultiBarTracker
from core.models import TaskStatus
from core.queue_manager import QueueManager
from core.terminal import MultiBarManager, str_width, truncate_to_width


def frame_text(manager):
    manager.render(force=True)
    return re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', '\n'.join(manager._last_frame))


def test_waiting_job_is_not_reported_as_downloading():
    display = MultiBarManager(max_slots=3, is_tty=False)
    sid = display.assign_slot('one', 'gallery-dl', 'https://danbooru.donmai.us/posts/107374')
    display.update_slot(sid, phase='resolving', status_message='Fetching metadata')
    text = frame_text(display)
    assert 'Fetching metadata' in text
    assert 'Active Downloads' in text
    assert 'Connecting' not in text
    assert '1/1' not in text
    assert '??' not in text
    assert '[Downloader]:' not in text


def test_album_count_and_current_file_bytes_are_distinct():
    display = MultiBarManager(max_slots=3, is_tty=False)
    sid = display.assign_slot('one', 'gallery-dl', 'Album title', extractor='danbooru')
    display.update_slot(sid, phase='downloading', downloaded=20 * 1024**2, percent=0,
                        total_files=48, file_index=0, completed_files=0,
                        file_name='page001.png', file_downloaded_bytes=384 * 1024,
                        file_total_bytes=1024 * 1024, speed=1024 * 1024)
    text = frame_text(display)
    assert '0/48' in text
    assert '0.0%' in text
    assert '0.4/1.0 MB' in text
    assert 'page001.png' in text
    assert '20.0' not in text  # Batch bytes must not masquerade as the current file.


@pytest.mark.parametrize('tracker_type', [MultiBarManager, GuiMultiBarTracker])
def test_processing_clears_stale_speed_and_preserves_last_bytes(tracker_type):
    display = tracker_type()
    sid = display.assign_slot('one', 'yt-dlp', 'Video title')
    display.update_slot(sid, downloaded=1024, total=2048, speed=2048, phase='downloading')
    display.update_slot(sid, phase='processing', status_message='FFmpeg: merging')
    slot = display._slots[sid] if isinstance(display, MultiBarManager) else display.slots[sid]
    assert slot.downloaded_bytes == 1024
    assert slot.speed_bytes_sec is None
    assert slot.phase == 'processing'


def test_assigning_slot_retires_exactly_one_pending_row_atomically():
    display = MultiBarManager(max_slots=2, is_tty=False)
    url = 'https://danbooru.donmai.us/posts/107374'
    display.set_pending_items([('gallery-dl', url)])
    display.assign_slot('one', 'gallery-dl', url)
    text = frame_text(display)
    assert 'Pending:' not in text
    assert not display._pending_items
    display.set_pending_items([('gallery-dl', url), ('gallery-dl', url)])
    display.assign_slot('two', 'gallery-dl', url)
    assert len(display._pending_items) == 1  # An actual repeated input remains visible.


@pytest.mark.parametrize('columns,rows', [(20, 8), (40, 12), (80, 24), (120, 32), (160, 40)])
def test_canvas_preserves_width_height_and_footer(monkeypatch, columns, rows):
    monkeypatch.setattr('core.terminal.shutil.get_terminal_size', lambda *args: os.terminal_size((columns, rows)))
    display = MultiBarManager(max_slots=3, is_tty=False)
    for index in range(3):
        sid = display.assign_slot(str(index), 'gallery-dl', '長いアルバム名：' * 12, extractor='danbooru')
        display.update_slot(sid, phase='downloading', total_files=48, completed_files=index,
                            file_downloaded_bytes=1000, file_total_bytes=5000, speed=2000)
    display.set_pending_items([('telegram-dl', 'https://t.me/channel/123', 'Waiting for telegram-dl worker')] * 4)
    display.set_queue_counts(remaining=7, completed=2, failed=1)
    text = frame_text(display)
    assert len(display._last_frame) <= rows - 2
    assert all(str_width(line) < columns for line in display._last_frame)
    assert 'Queue:' in text
    if columns >= 80:
        assert '1 failed' in text


def test_queue_displays_guard_reason_and_protects_repeated_urls(tmp_path):
    config = ConfigManager(tmp_path / 'config.json')
    queue = QueueManager(tmp_path / 'queue.json')
    first = queue.add('https://danbooru.donmai.us/posts/1/', backend='gallery-dl')
    repeat = queue.add(first.url, backend='gallery-dl')
    third = queue.add('https://example.com/other.jpg', backend='gallery-dl')
    queue.mark_status(first.id, TaskStatus.DOWNLOADING)
    assert queue.get_next_eligible({}, {'gallery-dl': 4}).id == third.id
    dispatcher = QueueDispatcher(config, queue, ArchiveManager(tmp_path / 'archive.db'), MagicMock())
    dispatcher._active_engine_counts = {'gallery-dl': 1}
    dispatcher._scheduler_limits = {'gallery-dl': 1}
    display = MultiBarManager(max_slots=3, is_tty=False)
    dispatcher._sync_pending_display(display)
    reasons = {item[1]: item[2] for item in display._pending_items}
    assert reasons[first.url] == 'Same URL already active'
    assert reasons[third.url] == 'Waiting for gallery-dl worker'
    queue.mark_status(first.id, TaskStatus.COMPLETED)
    assert queue.get_next_eligible({}, {'gallery-dl': 4}).id == repeat.id


def test_ansi_and_wide_titles_are_clipped_without_broken_escape_sequences():
    value = '\x1b[96m' + '名前：' * 30 + '\x1b[0m'
    clipped = truncate_to_width(value, 25)
    assert str_width(clipped) <= 25
    assert '\x1b' not in re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', clipped)


def test_remote_titles_cannot_inject_terminal_controls():
    display = MultiBarManager(is_tty=False)
    display.assign_slot('one', 'gallery-dl', 'title\x1b[2J\nINJECTED', extractor='site')
    text = frame_text(display)
    assert '\x1b[2J' not in '\n'.join(display._last_frame)
    assert 'title INJECTED' in text


def test_gallery_finish_does_not_invent_completed_files():
    from backends.gallerydl_backend import _GalleryDlBatchTracker
    events = []
    tracker = _GalleryDlBatchTracker(progress_callback=events.append, quiet=True)
    tracker.total_files = 48
    tracker.completed_count = 2
    tracker.finish()
    assert events[-1].completed_files == 2
    assert events[-1].file_index == 2
    assert events[-1].percent == pytest.approx(100 * 2 / 48)


def test_concurrent_slots_keep_two_content_rows_through_stage_changes(monkeypatch):
    monkeypatch.setattr('core.terminal.shutil.get_terminal_size', lambda *args: os.terminal_size((120, 32)))
    display = MultiBarManager(max_slots=3, is_tty=False)
    for index in range(3):
        display.assign_slot(str(index), 'gallery-dl', f'Album {index}', extractor='danbooru')
    for phase in ('waiting', 'resolving', 'downloading', 'resolving', 'processing'):
        display.update_slot(1, phase=phase, status_message='Fetching next file' if phase == 'resolving' else phase,
                            total_files=28, completed_files=4, file_downloaded_bytes=32768,
                            file_total_bytes=421069)
        text = frame_text(display)
        lines = text.splitlines()
        tops = [index for index, line in enumerate(lines) if line.startswith('╭')]
        bottoms = [index for index, line in enumerate(lines) if line.startswith('╰')]
        assert len(tops) == len(bottoms) == 3
        assert all(bottom - top == 3 for top, bottom in zip(tops, bottoms))
        for top in tops:
            assert lines[top + 1].startswith('│ [gallery-dl] • [danbooru] • [Album')
            assert lines[top + 2].startswith('│ [')
        assert '[Downloader]' not in text

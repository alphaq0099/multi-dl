"""
Unit tests for GuiMultiBarTracker adapter.
"""

from core.gui_tracker import GuiMultiBarTracker, format_bytes, format_dual_bytes


def test_format_bytes_and_dual():
    assert format_bytes(None) == "??"
    assert "MB" in format_bytes(1024 * 1024 * 15)
    assert "GB" in format_bytes(1024 * 1024 * 1024 * 2)

    dual = format_dual_bytes(1024 * 1024 * 10, 1024 * 1024 * 20)
    assert "10.0 / 20.0 MB" in dual


def test_gui_tracker_lifecycle():
    notified = []

    def on_update():
        notified.append(True)

    tracker = GuiMultiBarTracker(on_update_callback=on_update)
    tracker._min_interval = 0.0  # Instant notification for test

    # 1. Assign slot
    sid = tracker.assign_slot("task-1", "yt-dlp", "https://youtube.com/watch?v=123")
    assert sid in tracker.slots
    slot = tracker.slots[sid]
    assert slot.backend == "yt-dlp"
    assert slot.status == "active"
    assert len(notified) >= 1

    # 2. Update progress
    tracker.update_slot(
        slot_id=sid,
        percent=50.0,
        downloaded=5000000,
        total=10000000,
        speed=1000000,
        title="Test Video Title",
    )
    assert slot.percent == 50.0
    assert slot.title == "Test Video Title"
    assert slot.eta_seconds == 5.0

    # 3. Finish slot
    tracker.finish_slot(sid, saved_path="~/downloads/test.mp4", status_type="completed")
    assert slot.status == "completed"
    assert slot.percent == 100.0
    assert len(tracker.history) == 1

    # 4. Clear finished
    tracker.clear_finished()
    assert sid not in tracker.slots

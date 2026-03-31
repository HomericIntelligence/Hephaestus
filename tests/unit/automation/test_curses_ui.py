"""Tests for the curses_ui module."""

import threading
from unittest.mock import patch

from hephaestus.automation.curses_ui import CursesUI, LogBuffer, ThreadLogManager
from hephaestus.automation.status_tracker import StatusTracker


class TestLogBuffer:
    """Tests for LogBuffer class."""

    def test_append_and_get_recent(self) -> None:
        """Test basic append and get_recent operations."""
        buf = LogBuffer(maxlen=10)
        buf.append("msg1")
        buf.append("msg2")
        buf.append("msg3")

        recent = buf.get_recent(2)
        assert recent == ["msg2", "msg3"]

    def test_get_recent_more_than_available(self) -> None:
        """Test get_recent when n > buffer size."""
        buf = LogBuffer(maxlen=10)
        buf.append("only one")

        recent = buf.get_recent(100)
        assert recent == ["only one"]

    def test_maxlen_overflow(self) -> None:
        """Test that buffer respects maxlen."""
        buf = LogBuffer(maxlen=3)
        for i in range(10):
            buf.append(f"msg{i}")

        recent = buf.get_recent(100)
        assert len(recent) == 3
        assert recent == ["msg7", "msg8", "msg9"]

    def test_clear(self) -> None:
        """Test clearing the buffer."""
        buf = LogBuffer(maxlen=10)
        buf.append("msg1")
        buf.clear()
        assert buf.get_recent(10) == []

    def test_thread_safety(self) -> None:
        """Test concurrent access from multiple threads."""
        buf = LogBuffer(maxlen=1000)
        errors: list[Exception] = []

        def writer(n: int) -> None:
            try:
                for i in range(100):
                    buf.append(f"thread-{n}-msg-{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(buf.get_recent(1000)) <= 1000


class TestThreadLogManager:
    """Tests for ThreadLogManager class."""

    def test_get_buffer_creates_new(self) -> None:
        """Test get_buffer creates a new LogBuffer for unknown thread."""
        manager = ThreadLogManager()
        buf = manager.get_buffer(12345)
        assert isinstance(buf, LogBuffer)

    def test_get_buffer_returns_same(self) -> None:
        """Test get_buffer returns same buffer for same thread."""
        manager = ThreadLogManager()
        buf1 = manager.get_buffer(99)
        buf2 = manager.get_buffer(99)
        assert buf1 is buf2

    def test_log_appends_message(self) -> None:
        """Test log appends message to thread's buffer."""
        manager = ThreadLogManager()
        manager.log(42, "hello world")
        buf = manager.get_buffer(42)
        assert buf.get_recent(1) == ["hello world"]

    def test_separate_buffers_per_thread(self) -> None:
        """Test different thread IDs get separate buffers."""
        manager = ThreadLogManager()
        manager.log(1, "thread-1-msg")
        manager.log(2, "thread-2-msg")

        buf1 = manager.get_buffer(1)
        buf2 = manager.get_buffer(2)

        assert buf1.get_recent(1) == ["thread-1-msg"]
        assert buf2.get_recent(1) == ["thread-2-msg"]


class TestCursesUI:
    """Tests for CursesUI class."""

    def _make_ui(self, num_workers: int = 2) -> CursesUI:
        """Create a CursesUI with mock dependencies."""
        tracker = StatusTracker(num_workers)
        log_manager = ThreadLogManager()
        return CursesUI(tracker, log_manager)

    def test_init(self) -> None:
        """Test CursesUI initialization."""
        ui = self._make_ui()
        assert ui.running is False
        assert ui.thread is None
        assert ui.stdscr is None

    def test_stop_when_not_running(self) -> None:
        """Test stop() is a no-op when not running."""
        ui = self._make_ui()
        ui.stop()  # Should not raise

    def test_start_sets_running(self) -> None:
        """Test start() sets running flag and spawns thread."""
        ui = self._make_ui()
        with patch.object(ui, "_run_ui"):
            ui.start()
            assert ui.running is True
            assert ui.thread is not None
            ui.stop()

    def test_start_twice_logs_warning(self) -> None:
        """Test second start() is a no-op and logs warning."""
        ui = self._make_ui()
        with patch.object(ui, "_run_ui"):
            ui.start()
            first_thread = ui.thread
            ui.start()  # Second call - should be a no-op
            assert ui.thread is first_thread
            ui.stop()

    def test_emergency_cleanup_calls_endwin(self) -> None:
        """Test emergency cleanup calls curses.endwin."""
        ui = self._make_ui()
        with (
            patch("hephaestus.automation.curses_ui.curses.endwin") as mock_endwin,
            patch("hephaestus.automation.curses_ui.restore_terminal"),
        ):
            ui._emergency_cleanup()
        mock_endwin.assert_called_once()

    def test_run_ui_resets_running_on_error(self) -> None:
        """Test _run_ui resets running flag even if curses.wrapper fails."""
        ui = self._make_ui()
        ui.running = True

        with patch(
            "hephaestus.automation.curses_ui.curses.wrapper",
            side_effect=RuntimeError("fail"),
        ):
            ui._run_ui()

        assert ui.running is False

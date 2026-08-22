"""Tests for the curses_ui module."""

import importlib
import threading
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.status_tracker import StatusTracker
from hephaestus.cli.colors import Colors

if TYPE_CHECKING:
    import curses as curses_module

    from hephaestus.automation.curses_ui import CursesUI, LogBuffer, ThreadLogManager
else:
    try:
        curses_module = importlib.import_module("curses")
    except ModuleNotFoundError:
        pytest.skip("curses_ui tests require the stdlib curses module", allow_module_level=True)

    _curses_ui = importlib.import_module("hephaestus.automation.curses_ui")
    CursesUI = _curses_ui.CursesUI
    LogBuffer = _curses_ui.LogBuffer
    ThreadLogManager = _curses_ui.ThreadLogManager


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
        assert ui._colors_enabled is False

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

    def test_curses_unavailable_raises_actionable_error(self) -> None:
        with (
            patch("hephaestus.automation.curses_ui.curses", None),
            pytest.raises(RuntimeError, match="--no-ui"),
        ):
            CursesUI(StatusTracker(1), ThreadLogManager())

    def test_refresh_display_renders_and_refreshes(self) -> None:
        ui = CursesUI(StatusTracker(1), ThreadLogManager())
        screen = MagicMock()
        screen.getmaxyx.return_value = (20, 80)
        ui.stdscr = screen

        with (
            patch.object(ui, "_draw_workers", return_value=3),
            patch.object(ui, "_draw_separator", return_value=4),
            patch.object(ui, "_draw_logs", return_value=5),
        ):
            ui._refresh_display()

        screen.clear.assert_called_once()
        screen.refresh.assert_called_once()

    def test_curses_main_recovers_from_resize_without_sleeping(self) -> None:
        ui = CursesUI(StatusTracker(1), ThreadLogManager())
        ui.running = True
        screen = MagicMock()
        screen.getmaxyx.return_value = (24, 80)
        with (
            patch.object(curses_module, "curs_set"),
            patch.object(curses_module, "has_colors", return_value=False),
            patch.object(curses_module, "resizeterm") as resize,
            patch.object(
                ui,
                "_refresh_display",
                side_effect=[curses_module.error(), KeyboardInterrupt()],
            ),
            patch("hephaestus.automation.curses_ui.time.sleep") as sleep,
        ):
            ui._curses_main(screen)

        resize.assert_called_once_with(24, 80)
        sleep.assert_not_called()

    def test_no_color_prevents_curses_pair_initialization(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NO_COLOR disables curses color pairs through the shared policy."""
        ui = self._make_ui()
        ui.running = False
        monkeypatch.setenv("NO_COLOR", "1")
        Colors.auto()

        with (
            patch("hephaestus.automation.curses_ui.curses.curs_set"),
            patch(
                "hephaestus.automation.curses_ui.curses.has_colors",
                return_value=True,
            ),
            patch("hephaestus.automation.curses_ui.curses.start_color") as start_color,
            patch("hephaestus.automation.curses_ui.curses.init_pair") as init_pair,
        ):
            ui._curses_main(MagicMock())

        assert ui._colors_enabled is False
        start_color.assert_not_called()
        init_pair.assert_not_called()

    def test_no_color_worker_text_retains_status_labels(self) -> None:
        """Worker labels remain meaningful when curses colors are unavailable."""
        ui = self._make_ui(num_workers=2)
        ui.stdscr = MagicMock()
        ui._colors_enabled = False
        ui.status_tracker.update_slot(1, "failed")

        with patch("hephaestus.automation.curses_ui.curses.color_pair") as color_pair:
            ui._draw_workers(start_row=2, height=10, width=80)

        color_pair.assert_not_called()
        rendered = [call.args[2] for call in ui.stdscr.addstr.call_args_list]
        assert rendered == ["Worker 0: [idle]", "Worker 1: failed"]

    def test_draw_workers_returns_next_row(self) -> None:
        """Test _draw_workers returns the next free row."""
        ui = self._make_ui(num_workers=2)
        ui.stdscr = MagicMock()
        ui.status_tracker.update_slot(0, "working")
        ui.status_tracker.update_slot(1, "idle")
        ui._colors_enabled = False

        next_row = ui._draw_workers(start_row=2, height=10, width=80)

        # Should have rendered 2 workers starting at row 2, so next row is 4
        assert next_row == 4

    def test_draw_workers_stops_at_height_boundary(self) -> None:
        """Test _draw_workers stops rendering when it hits the height boundary."""
        ui = self._make_ui(num_workers=5)
        ui.stdscr = MagicMock()
        for i in range(5):
            ui.status_tracker.update_slot(i, f"task {i}")
        ui._colors_enabled = False

        next_row = ui._draw_workers(start_row=8, height=10, width=80)

        # Only 1 worker can fit before height - 1 (9), so next row is 9
        assert next_row == 9

    def test_draw_workers_truncates_long_status(self) -> None:
        """Test _draw_workers truncates long status text."""
        ui = self._make_ui(num_workers=1)
        ui.stdscr = MagicMock()
        long_status = "x" * 100
        ui.status_tracker.update_slot(0, long_status)
        ui._colors_enabled = False

        ui._draw_workers(start_row=2, height=10, width=80)

        # Check that addstr was called with truncated text
        calls = ui.stdscr.addstr.call_args_list
        assert len(calls) > 0
        # Text should be truncated to width - 4 + "..."
        text_arg = calls[0][0][2]
        assert len(text_arg) <= 80 - 1

    def test_draw_separator_returns_next_row(self) -> None:
        """Test _draw_separator returns the next free row."""
        ui = self._make_ui()
        ui.stdscr = MagicMock()

        next_row = ui._draw_separator(start_row=4, height=10, width=80)

        assert next_row == 5

    def test_draw_separator_stops_at_boundary(self) -> None:
        """Test _draw_separator doesn't draw if at boundary."""
        ui = self._make_ui()
        ui.stdscr = MagicMock()

        next_row = ui._draw_separator(start_row=9, height=10, width=80)

        # Should return start_row unchanged since it's at height - 1
        assert next_row == 9
        ui.stdscr.addstr.assert_not_called()

    def test_draw_logs_returns_next_row(self) -> None:
        """Test _draw_logs returns the next free row."""
        ui = self._make_ui()
        ui.stdscr = MagicMock()
        ui.log_manager.log(1, "test log")

        with patch("hephaestus.automation.curses_ui.curses.A_BOLD", 1):
            next_row = ui._draw_logs(start_row=5, height=10, width=80)

        # Should have at least rendered the header
        assert next_row >= 6

    def test_draw_logs_stops_at_boundary(self) -> None:
        """Test _draw_logs returns immediately if at boundary."""
        ui = self._make_ui()
        ui.stdscr = MagicMock()

        next_row = ui._draw_logs(start_row=9, height=10, width=80)

        # Should return start_row unchanged since it's at height - 1
        assert next_row == 9
        ui.stdscr.addstr.assert_not_called()

    def test_draw_logs_truncates_long_messages(self) -> None:
        """Test _draw_logs truncates long log messages."""
        ui = self._make_ui()
        ui.stdscr = MagicMock()
        long_msg = "x" * 100
        ui.log_manager.log(1, long_msg)

        with patch("hephaestus.automation.curses_ui.curses.A_BOLD", 1):
            ui._draw_logs(start_row=5, height=10, width=80)

        # Check that messages are truncated
        calls = ui.stdscr.addstr.call_args_list
        # Second call (after header) should have truncated message
        if len(calls) > 1:
            text_arg = calls[1][0][2]
            assert len(text_arg) <= 80 - 1

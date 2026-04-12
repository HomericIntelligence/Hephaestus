"""Tests for terminal utilities."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import hephaestus.utils.terminal as terminal_module
from hephaestus.utils.terminal import (
    install_signal_handlers,
    restore_terminal,
    terminal_guard,
)


class TestRestoreTerminal:
    """Tests for restore_terminal."""

    def test_no_op_when_not_tty(self) -> None:
        """Does not call stty when stdin is not a TTY."""
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            with patch("subprocess.run") as mock_run:
                restore_terminal()
                mock_run.assert_not_called()

    def test_no_op_when_not_main_thread(self) -> None:
        """Does not call stty when called from a non-main thread."""
        called = []

        def run_from_thread() -> None:
            with patch("subprocess.run") as mock_run:
                with patch("sys.stdin") as mock_stdin:
                    mock_stdin.isatty.return_value = True
                    restore_terminal()
                    called.append(mock_run.called)

        t = threading.Thread(target=run_from_thread)
        t.start()
        t.join()
        assert called == [False]

    def test_calls_stty_when_tty_and_main_thread(self) -> None:
        """Calls stty sane when stdin is a TTY in the main thread."""
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            with patch("subprocess.run") as mock_run:
                restore_terminal()
                mock_run.assert_called_once()
                args = mock_run.call_args[0][0]
                assert args == ["stty", "sane"]

    def test_swallows_exceptions(self) -> None:
        """Does not raise even if subprocess raises."""
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            with patch("subprocess.run", side_effect=OSError("stty not found")):
                restore_terminal()  # Must not raise


class TestInstallSignalHandlers:
    """Tests for install_signal_handlers."""

    def test_first_signal_calls_shutdown(self) -> None:
        """First signal calls the shutdown function."""
        shutdown = MagicMock()
        terminal_module._shutdown_requested = False

        install_signal_handlers(shutdown)

        import signal as signal_module

        handler = signal_module.getsignal(signal_module.SIGINT)
        assert callable(handler)

        handler(signal_module.SIGINT, None)
        shutdown.assert_called_once()
        assert terminal_module._shutdown_requested is True

    def test_resets_shutdown_flag_on_install(self) -> None:
        """Re-installing handlers resets the shutdown flag."""
        terminal_module._shutdown_requested = True
        install_signal_handlers(MagicMock())
        assert terminal_module._shutdown_requested is False


class TestTerminalGuard:
    """Tests for terminal_guard context manager."""

    def test_yields_and_restores_terminal(self) -> None:
        """Context manager yields and calls restore_terminal on exit."""
        with patch("hephaestus.utils.terminal.restore_terminal") as mock_restore:
            with terminal_guard():
                pass
            mock_restore.assert_called_once()

    def test_restores_terminal_on_exception(self) -> None:
        """Calls restore_terminal even when body raises."""
        with patch("hephaestus.utils.terminal.restore_terminal") as mock_restore:
            try:
                with terminal_guard():
                    raise ValueError("boom")
            except ValueError:
                pass
            mock_restore.assert_called_once()

    def test_installs_signal_handlers_when_fn_given(self) -> None:
        """Installs signal handlers when shutdown_fn is provided."""
        shutdown = MagicMock()
        with patch("hephaestus.utils.terminal.install_signal_handlers") as mock_install:
            with patch("hephaestus.utils.terminal.restore_terminal"):
                with terminal_guard(shutdown):
                    pass
                mock_install.assert_called_once_with(shutdown)

    def test_no_signal_handlers_when_fn_is_none(self) -> None:
        """Does not install signal handlers when shutdown_fn is None."""
        with patch("hephaestus.utils.terminal.install_signal_handlers") as mock_install:
            with patch("hephaestus.utils.terminal.restore_terminal"):
                with terminal_guard():
                    pass
                mock_install.assert_not_called()

"""Tests for terminal utilities."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import hephaestus.utils.terminal as terminal_module
from hephaestus.utils.terminal import (
    install_signal_handlers,
    install_sigtstp_only,
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
        terminal_module._shutdown_requested[0] = False

        install_signal_handlers(shutdown)

        import signal as signal_module

        handler = signal_module.getsignal(signal_module.SIGINT)
        assert callable(handler)

        handler(signal_module.SIGINT, None)
        shutdown.assert_called_once()
        assert terminal_module._shutdown_requested[0] is True

    def test_resets_shutdown_flag_on_install(self) -> None:
        """Re-installing handlers resets the shutdown flag."""
        terminal_module._shutdown_requested[0] = True
        install_signal_handlers(MagicMock())
        assert terminal_module._shutdown_requested[0] is False

    def test_installs_sigtstp_handler(self) -> None:
        """install_signal_handlers also wires up SIGTSTP via install_sigtstp_only."""
        import signal as signal_module

        with patch("signal.signal") as mock_signal:
            install_signal_handlers(MagicMock())
            registered = {call.args[0] for call in mock_signal.call_args_list}
            assert signal_module.SIGTSTP in registered


class TestInstallSigtstpOnly:
    """Tests for install_sigtstp_only."""

    def test_registers_only_sigtstp(self) -> None:
        """Registers a handler for SIGTSTP and touches no other signal."""
        import signal as signal_module

        with patch("signal.signal") as mock_signal:
            install_sigtstp_only()
            registered = {call.args[0] for call in mock_signal.call_args_list}
            assert registered == {signal_module.SIGTSTP}

    def test_handler_restores_terminal_then_sigstops_self(self) -> None:
        """The handler restores the terminal, self-SIGSTOPs, then re-arms."""
        import signal as signal_module
        from collections.abc import Callable

        captured: dict[int, Callable[[int, object], None]] = {}

        def fake_signal(sig: int, handler: Callable[[int, object], None]) -> None:
            captured[sig] = handler

        with (
            patch("signal.signal", side_effect=fake_signal),
            patch("hephaestus.utils.terminal.restore_terminal") as mock_restore,
            patch("os.kill") as mock_kill,
            patch("os.getpid", return_value=4242),
        ):
            install_sigtstp_only()
            captured[signal_module.SIGTSTP](signal_module.SIGTSTP, None)
            mock_restore.assert_called_once()
            mock_kill.assert_called_once_with(4242, signal_module.SIGSTOP)
            # Re-armed: signal.signal was called for SIGTSTP more than once
            # (initial install + SIG_DFL + re-arm inside the handler).
            assert captured[signal_module.SIGTSTP] is not None

    def test_noop_when_sigtstp_unavailable(self) -> None:
        """Does not raise or call signal.signal when SIGTSTP is unavailable."""
        with (
            patch("signal.signal") as mock_signal,
            patch("hephaestus.utils.terminal.hasattr", return_value=False, create=True),
        ):
            install_sigtstp_only()
            mock_signal.assert_not_called()


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

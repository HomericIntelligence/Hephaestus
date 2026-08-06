"""Terminal color-policy tests for the shell installer helpers."""

import contextlib
import errno
import os
import re
import select
import subprocess
import time
from pathlib import Path

import pytest

pty = pytest.importorskip("pty", reason="installer color tests require a POSIX PTY")
pytestmark = pytest.mark.requires_posix

_HELPERS = Path(__file__).resolve().parents[3] / "scripts/shell/lib/install_helpers.sh"
_CONTROL_ENV = {
    "NO_COLOR",
    "FORCE_COLOR",
    "CLICOLOR",
    "CLICOLOR_FORCE",
    "INSTALL_HELPERS_LOADED",
}
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_COMMAND = 'source "$1"; check_fail "build failed"'


def _render_failure(overrides: dict[str, str], *, tty: bool) -> str:
    """Source the helpers and render a failure under controlled output."""
    env = os.environ.copy()
    for name in _CONTROL_ENV:
        env.pop(name, None)
    env.update(overrides)
    argv = ["bash", "-c", _COMMAND, "bash", str(_HELPERS)]

    if not tty:
        result = subprocess.run(
            argv,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
        return result.stdout

    pid, master_fd = pty.fork()
    if pid == 0:
        os.execvpe(argv[0], argv, env)
        os._exit(127)

    try:
        chunks: list[bytes] = []
        deadline = time.monotonic() + 5
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                os.kill(pid, 9)
                raise subprocess.TimeoutExpired(argv, 5)
            readable, _, _ = select.select([master_fd], [], [], remaining)
            if not readable:
                os.kill(pid, 9)
                raise subprocess.TimeoutExpired(argv, 5)
            try:
                chunk = os.read(master_fd, 4096)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            chunks.append(chunk)
        _, status = os.waitpid(pid, 0)
        return_code = os.waitstatus_to_exitcode(status)
        if return_code:
            raise subprocess.CalledProcessError(return_code, argv)
        return b"".join(chunks).decode()
    finally:
        os.close(master_fd)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)


def test_non_tty_output_is_plain_by_default() -> None:
    """Redirected output is plain by default."""
    assert "\x1b[" not in _render_failure({}, tty=False)


def test_tty_output_is_colored_by_default() -> None:
    """TTY output is colored by default."""
    assert "\x1b[" in _render_failure({}, tty=True)


def test_clicolor_one_does_not_force_non_tty_color() -> None:
    """CLICOLOR=1 does not force color into redirected output."""
    assert "\x1b[" not in _render_failure({"CLICOLOR": "1"}, tty=False)


def test_clicolor_one_keeps_tty_color() -> None:
    """CLICOLOR=1 preserves TTY color."""
    assert "\x1b[" in _render_failure({"CLICOLOR": "1"}, tty=True)


def test_clicolor_zero_disables_tty_color() -> None:
    """CLICOLOR=0 disables TTY color."""
    assert "\x1b[" not in _render_failure({"CLICOLOR": "0"}, tty=True)


@pytest.mark.parametrize("force_name", ["FORCE_COLOR", "CLICOLOR_FORCE"])
def test_force_controls_enable_non_tty_color(force_name: str) -> None:
    """Force controls enable color for redirected output."""
    assert "\x1b[" in _render_failure({force_name: "1"}, tty=False)


@pytest.mark.parametrize("force_name", ["FORCE_COLOR", "CLICOLOR_FORCE"])
def test_force_controls_override_clicolor_zero(force_name: str) -> None:
    """Force controls override CLICOLOR=0."""
    output = _render_failure({"CLICOLOR": "0", force_name: "1"}, tty=True)
    assert "\x1b[" in output


@pytest.mark.parametrize("force_name", ["FORCE_COLOR", "CLICOLOR_FORCE"])
def test_no_color_wins_over_force_controls(force_name: str) -> None:
    """NO_COLOR has precedence over force controls."""
    output = _render_failure({"NO_COLOR": "0", force_name: "1"}, tty=True)
    assert "\x1b[" not in output


def test_empty_no_color_is_ignored() -> None:
    """An empty NO_COLOR value does not disable TTY color."""
    assert "\x1b[" in _render_failure({"NO_COLOR": ""}, tty=True)


def test_empty_force_color_is_ignored() -> None:
    """An empty FORCE_COLOR value does not force redirected color."""
    assert "\x1b[" not in _render_failure({"FORCE_COLOR": ""}, tty=False)


def test_empty_clicolor_force_is_ignored() -> None:
    """An empty CLICOLOR_FORCE value does not force redirected color."""
    assert "\x1b[" not in _render_failure({"CLICOLOR_FORCE": ""}, tty=False)


def test_clicolor_force_zero_does_not_force_color() -> None:
    """CLICOLOR_FORCE=0 does not force redirected color."""
    assert "\x1b[" not in _render_failure({"CLICOLOR_FORCE": "0"}, tty=False)


def test_empty_clicolor_is_ignored() -> None:
    """An empty CLICOLOR value falls through to TTY detection."""
    assert "\x1b[" in _render_failure({"CLICOLOR": ""}, tty=True)


def test_failure_meaning_survives_without_color() -> None:
    """The failure glyph and wording remain when ANSI styling is disabled."""
    output = _render_failure({"NO_COLOR": "1"}, tty=True)
    assert _ANSI.sub("", output).strip() == "✗ build failed"

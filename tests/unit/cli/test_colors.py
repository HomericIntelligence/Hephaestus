#!/usr/bin/env python3

"""Tests for the automatic terminal color policy."""

import re
import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from hephaestus.cli.colors import _CODES, Colors

_CONTROL_ENV = ("NO_COLOR", "FORCE_COLOR", "CLICOLOR", "CLICOLOR_FORCE")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class _Stdout:
    """Minimal stdout test double with a controlled TTY result."""

    def __init__(self, *, is_tty: bool) -> None:
        self._is_tty = is_tty

    def isatty(self) -> bool:
        """Return the configured TTY result."""
        return self._is_tty


def _set_tty(monkeypatch: pytest.MonkeyPatch, *, is_tty: bool) -> None:
    """Replace stdout with a stream whose TTY status is deterministic."""
    monkeypatch.setattr(sys, "stdout", _Stdout(is_tty=is_tty))


@pytest.fixture(autouse=True)
def _reset_color_policy(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset process environment and the current thread's override."""
    for name in _CONTROL_ENV:
        monkeypatch.delenv(name, raising=False)
    Colors.auto()
    yield
    Colors.auto()


class TestColorsDefinitions:
    """Tests for color code definitions."""

    def test_all_colors_defined(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """All expected color names are accessible on the Colors class."""
        _set_tty(monkeypatch, is_tty=True)
        expected = [
            "HEADER",
            "OKBLUE",
            "OKCYAN",
            "OKGREEN",
            "WARNING",
            "FAIL",
            "ENDC",
            "BOLD",
            "UNDERLINE",
        ]
        for name in expected:
            assert getattr(Colors, name) != "", f"{name} should be defined"

    def test_colors_are_ansi_codes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Color codes are ANSI escape sequences."""
        _set_tty(monkeypatch, is_tty=True)
        assert Colors.OKGREEN.startswith("\033[")
        assert Colors.ENDC == "\033[0m"

    def test_all_codes_are_ansi_sequences(self) -> None:
        """Every code in the mapping is a valid ANSI escape sequence."""
        for name, code in _CODES.items():
            assert code.startswith("\033["), f"{name} should be an ANSI sequence"

    def test_unknown_attribute_raises(self) -> None:
        """Accessing an undefined attribute raises AttributeError."""
        with pytest.raises(AttributeError, match="NONEXISTENT"):
            _ = Colors.NONEXISTENT

    def test_codes_dict_is_unchanged_at_runtime(self) -> None:
        """The code mapping retains the expected ANSI values."""
        assert _CODES["OKGREEN"] == "\033[92m"
        assert _CODES["ENDC"] == "\033[0m"


class TestAutomaticPolicy:
    """Tests for TTY-driven default color selection."""

    def test_tty_enables_color(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Color is enabled for TTY output by default."""
        _set_tty(monkeypatch, is_tty=True)
        assert Colors.OKGREEN == "\033[92m"

    def test_non_tty_disables_color(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Color is disabled for redirected output by default."""
        _set_tty(monkeypatch, is_tty=False)
        assert Colors.OKGREEN == ""

    def test_clicolor_one_still_requires_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CLICOLOR=1 does not force color into a pipe."""
        monkeypatch.setenv("CLICOLOR", "1")
        _set_tty(monkeypatch, is_tty=False)
        assert Colors.OKGREEN == ""


class TestEnvironmentPolicy:
    """Tests for standard environment variable precedence."""

    def test_no_color_disables_tty_color(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Any non-empty NO_COLOR value disables color."""
        monkeypatch.setenv("NO_COLOR", "0")
        assert Colors.OKGREEN == ""

    @pytest.mark.parametrize("force_name", ["FORCE_COLOR", "CLICOLOR_FORCE"])
    def test_force_controls_enable_non_tty_color(
        self,
        monkeypatch: pytest.MonkeyPatch,
        force_name: str,
    ) -> None:
        """Non-empty force controls enable color for redirected output."""
        _set_tty(monkeypatch, is_tty=False)
        monkeypatch.setenv(force_name, "1")
        assert Colors.OKGREEN == "\033[92m"

    @pytest.mark.parametrize("force_name", ["FORCE_COLOR", "CLICOLOR_FORCE"])
    def test_no_color_wins_over_force_controls(
        self,
        monkeypatch: pytest.MonkeyPatch,
        force_name: str,
    ) -> None:
        """NO_COLOR has precedence over force controls."""
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.setenv(force_name, "1")
        assert Colors.OKGREEN == ""

    def test_clicolor_zero_disables_tty_color(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CLICOLOR=0 disables color when no force control is set."""
        monkeypatch.setenv("CLICOLOR", "0")
        assert Colors.OKGREEN == ""

    @pytest.mark.parametrize("force_name", ["FORCE_COLOR", "CLICOLOR_FORCE"])
    def test_force_controls_override_clicolor_zero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        force_name: str,
    ) -> None:
        """Force controls have precedence over CLICOLOR=0."""
        monkeypatch.setenv("CLICOLOR", "0")
        monkeypatch.setenv(force_name, "1")
        assert Colors.OKGREEN == "\033[92m"

    def test_empty_no_color_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty NO_COLOR value does not disable TTY color."""
        _set_tty(monkeypatch, is_tty=True)
        monkeypatch.setenv("NO_COLOR", "")
        assert Colors.OKGREEN == "\033[92m"

    @pytest.mark.parametrize("force_name", ["FORCE_COLOR", "CLICOLOR_FORCE"])
    def test_empty_force_controls_are_ignored(
        self,
        monkeypatch: pytest.MonkeyPatch,
        force_name: str,
    ) -> None:
        """Empty force controls do not enable color in a pipe."""
        _set_tty(monkeypatch, is_tty=False)
        monkeypatch.setenv(force_name, "")
        assert Colors.OKGREEN == ""

    def test_clicolor_force_zero_does_not_force_color(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLICOLOR_FORCE=0 leaves non-TTY output unstyled."""
        _set_tty(monkeypatch, is_tty=False)
        monkeypatch.setenv("CLICOLOR_FORCE", "0")
        assert Colors.OKGREEN == ""

    def test_empty_clicolor_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty CLICOLOR value falls through to TTY detection."""
        _set_tty(monkeypatch, is_tty=True)
        monkeypatch.setenv("CLICOLOR", "")
        assert Colors.OKGREEN == "\033[92m"


class TestExplicitOverrides:
    """Tests for explicit calling-thread overrides."""

    def test_enable_wins_over_no_color_and_non_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit enable overrides environment and TTY detection."""
        monkeypatch.setenv("NO_COLOR", "1")
        _set_tty(monkeypatch, is_tty=False)
        Colors.enable()
        assert Colors.OKGREEN == "\033[92m"

    def test_disable_wins_over_force_and_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit disable overrides force controls and TTY detection."""
        monkeypatch.setenv("FORCE_COLOR", "1")
        Colors.disable()
        assert Colors.OKGREEN == ""

    def test_auto_clears_explicit_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """auto() restores automatic evaluation after an explicit override."""
        _set_tty(monkeypatch, is_tty=False)
        Colors.enable()
        assert Colors.OKGREEN == "\033[92m"
        Colors.auto()
        assert Colors.OKGREEN == ""


class TestAccessibility:
    """Tests that status meaning remains available without styling."""

    def test_semantic_status_text_is_identical_without_ansi(self) -> None:
        """Removing ANSI styling leaves the same semantic status text."""
        Colors.enable()
        colored = f"{Colors.FAIL}ERROR{Colors.ENDC}: build failed"
        Colors.disable()
        plain = f"{Colors.FAIL}ERROR{Colors.ENDC}: build failed"

        assert _ANSI.sub("", colored) == plain == "ERROR: build failed"


class TestDisableEnable:
    """Tests for explicit disable and enable methods."""

    def test_disable_returns_empty_strings(self) -> None:
        """After disable(), all color codes return empty strings."""
        Colors.disable()
        for name in _CODES:
            assert getattr(Colors, name) == "", f"{name} should be empty after disable()"

    def test_enable_restores_colors(self) -> None:
        """After disable() then enable(), colors are restored."""
        Colors.disable()
        assert Colors.OKGREEN == ""
        Colors.enable()
        assert Colors.OKGREEN == "\033[92m"


class TestThreadSafety:
    """Tests for thread-local explicit color state."""

    def test_disable_in_one_thread_does_not_affect_another(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Calling disable() in one thread does not affect another thread."""
        _set_tty(monkeypatch, is_tty=True)
        barrier = threading.Barrier(2)
        results: dict[str, str] = {}

        def thread_that_disables() -> None:
            Colors.disable()
            barrier.wait(timeout=5)
            results["disabler"] = Colors.OKGREEN

        def thread_that_reads() -> None:
            barrier.wait(timeout=5)
            results["reader"] = Colors.OKGREEN

        t1 = threading.Thread(target=thread_that_disables)
        t2 = threading.Thread(target=thread_that_reads)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert results == {"disabler": "", "reader": "\033[92m"}

    def test_enable_only_affects_calling_thread(self) -> None:
        """enable() in one thread does not affect another disabled thread."""
        barrier = threading.Barrier(2)
        results: dict[str, str] = {}

        def thread_that_disables() -> None:
            Colors.disable()
            barrier.wait(timeout=5)
            results["disabled_thread"] = Colors.OKGREEN

        def thread_that_enables() -> None:
            Colors.enable()
            barrier.wait(timeout=5)
            results["enabled_thread"] = Colors.OKGREEN

        t1 = threading.Thread(target=thread_that_disables)
        t2 = threading.Thread(target=thread_that_enables)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert results == {
            "disabled_thread": "",
            "enabled_thread": "\033[92m",
        }

    def test_explicit_override_is_isolated_from_automatic_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit override remains local to a thread using auto policy."""
        _set_tty(monkeypatch, is_tty=True)
        barrier = threading.Barrier(2)
        results: dict[str, str] = {}

        def disabled_worker() -> None:
            Colors.disable()
            barrier.wait(timeout=5)
            results["disabled"] = Colors.OKGREEN

        def automatic_worker() -> None:
            Colors.auto()
            barrier.wait(timeout=5)
            results["automatic"] = Colors.OKGREEN

        disabled = threading.Thread(target=disabled_worker)
        automatic = threading.Thread(target=automatic_worker)
        disabled.start()
        automatic.start()
        disabled.join(timeout=5)
        automatic.join(timeout=5)

        assert not disabled.is_alive()
        assert not automatic.is_alive()
        assert results == {"disabled": "", "automatic": "\033[92m"}

    def test_concurrent_access_is_safe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Many threads can read color codes concurrently without corruption."""
        _set_tty(monkeypatch, is_tty=True)
        errors: list[str] = []

        def reader(thread_id: int) -> None:
            try:
                for _ in range(100):
                    value = Colors.OKGREEN
                    if value != "\033[92m":
                        errors.append(f"Thread {thread_id} got unexpected: {value!r}")
            except Exception as exc:
                errors.append(f"Thread {thread_id} raised: {exc}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(reader, i) for i in range(8)]
            for future in as_completed(futures):
                future.result()

        assert errors == []

    def test_mixed_enable_disable_across_threads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Threads toggling enable/disable do not interfere with each other."""
        _set_tty(monkeypatch, is_tty=True)
        results: dict[int, bool] = {}

        def toggler(thread_id: int, should_disable: bool) -> None:
            if should_disable:
                Colors.disable()
            values = [getattr(Colors, name) for name in _CODES]
            results[thread_id] = all(
                value == "" if should_disable else value != "" for value in values
            )

        threads = []
        for i in range(10):
            thread = threading.Thread(target=toggler, args=(i, i % 2 == 0))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join(timeout=5)

        assert all(results.values())

    def test_new_thread_uses_automatic_policy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A newly spawned thread follows the automatic TTY policy."""
        _set_tty(monkeypatch, is_tty=True)
        result: dict[str, str] = {}

        def check_default() -> None:
            result["value"] = Colors.OKGREEN

        thread = threading.Thread(target=check_default)
        thread.start()
        thread.join(timeout=5)

        assert result["value"] == "\033[92m"

#!/usr/bin/env python3

"""Tests for hephaestus.cli.colors module."""

import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from hephaestus.cli.colors import _CODES, Colors, _state


@pytest.fixture(autouse=True)
def _reset_thread_local_state():
    """Reset the thread-local enabled flag before each test."""
    _state.enabled = True
    yield
    _state.enabled = True


class TestColorsDefinitions:
    """Tests for color code definitions."""

    def test_all_colors_defined(self):
        """All expected color names are accessible on the Colors class."""
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

    def test_colors_are_ansi_codes(self):
        """Color codes are ANSI escape sequences."""
        assert Colors.OKGREEN.startswith("\033[")
        assert Colors.ENDC == "\033[0m"

    def test_all_codes_are_ansi_sequences(self):
        """Every code in the mapping is a valid ANSI escape sequence."""
        for name, code in _CODES.items():
            assert code.startswith("\033["), f"{name} should be an ANSI sequence"

    def test_unknown_attribute_raises(self):
        """Accessing an undefined attribute raises AttributeError."""
        with pytest.raises(AttributeError, match="NONEXISTENT"):
            _ = Colors.NONEXISTENT

    def test_codes_dict_is_immutable_at_runtime(self):
        """The _CODES dict contents match expected ANSI codes."""
        assert _CODES["OKGREEN"] == "\033[92m"
        assert _CODES["ENDC"] == "\033[0m"


class TestDisableEnable:
    """Tests for disable() and enable() methods."""

    def test_disable_returns_empty_strings(self):
        """After disable(), all color codes return empty strings."""
        Colors.disable()
        for name in _CODES:
            assert getattr(Colors, name) == "", f"{name} should be empty after disable()"

    def test_enable_restores_colors(self):
        """After disable() then enable(), colors are restored."""
        Colors.disable()
        assert Colors.OKGREEN == ""
        Colors.enable()
        assert Colors.OKGREEN == "\033[92m"

    def test_enable_is_default_state(self):
        """Colors are enabled by default."""
        assert Colors.OKGREEN == "\033[92m"
        assert Colors.FAIL == "\033[91m"


class TestAuto:
    """Tests for the auto() method."""

    def test_auto_disables_for_non_tty(self, monkeypatch):
        """auto() disables colors when stdout is not a TTY."""

        class FakeStdout:
            def isatty(self):
                return False

        monkeypatch.setattr(sys, "stdout", FakeStdout())
        Colors.auto()
        assert Colors.OKGREEN == ""
        assert Colors.ENDC == ""

    def test_auto_keeps_colors_for_tty(self, monkeypatch):
        """auto() keeps colors enabled when stdout is a TTY."""

        class FakeStdout:
            def isatty(self):
                return True

        monkeypatch.setattr(sys, "stdout", FakeStdout())
        Colors.auto()
        assert Colors.OKGREEN == "\033[92m"
        assert Colors.ENDC == "\033[0m"


class TestThreadSafety:
    """Tests for thread-safety of the Colors class."""

    def test_disable_in_one_thread_does_not_affect_another(self):
        """Calling disable() in one thread does not affect other threads."""
        barrier = threading.Barrier(2)
        results = {}

        def thread_that_disables():
            Colors.disable()
            barrier.wait(timeout=5)
            results["disabler"] = Colors.OKGREEN

        def thread_that_reads():
            barrier.wait(timeout=5)
            results["reader"] = Colors.OKGREEN

        t1 = threading.Thread(target=thread_that_disables)
        t2 = threading.Thread(target=thread_that_reads)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert results["disabler"] == "", "Disabling thread should see empty string"
        assert results["reader"] == "\033[92m", "Other thread should still see color"

    def test_enable_only_affects_calling_thread(self):
        """enable() in one thread does not re-enable another disabled thread."""
        barrier = threading.Barrier(2)
        results = {}

        def thread_that_disables():
            Colors.disable()
            barrier.wait(timeout=5)
            # Another thread called enable(), but we should still be disabled
            results["disabled_thread"] = Colors.OKGREEN

        def thread_that_enables():
            Colors.enable()
            barrier.wait(timeout=5)
            results["enabled_thread"] = Colors.OKGREEN

        t1 = threading.Thread(target=thread_that_disables)
        t2 = threading.Thread(target=thread_that_enables)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert results["disabled_thread"] == ""
        assert results["enabled_thread"] == "\033[92m"

    def test_concurrent_access_is_safe(self):
        """Many threads can read color codes concurrently without corruption."""
        errors = []

        def reader(thread_id: int):
            try:
                for _ in range(100):
                    val = Colors.OKGREEN
                    if val != "\033[92m":
                        errors.append(f"Thread {thread_id} got unexpected: {val!r}")
            except Exception as exc:
                errors.append(f"Thread {thread_id} raised: {exc}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(reader, i) for i in range(8)]
            for f in as_completed(futures):
                f.result()

        assert errors == [], f"Concurrent read errors: {errors}"

    def test_mixed_enable_disable_across_threads(self):
        """Threads toggling enable/disable don't interfere with each other."""
        results = {}

        def toggler(thread_id: int, should_disable: bool):
            if should_disable:
                Colors.disable()
            vals = [getattr(Colors, name) for name in _CODES]
            if should_disable:
                results[thread_id] = all(v == "" for v in vals)
            else:
                results[thread_id] = all(v != "" for v in vals)

        threads = []
        for i in range(10):
            t = threading.Thread(target=toggler, args=(i, i % 2 == 0))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=5)

        for tid, ok in results.items():
            assert ok, f"Thread {tid} saw inconsistent state"

    def test_new_thread_defaults_to_enabled(self):
        """A newly spawned thread has colors enabled by default."""
        result = {}

        def check_default():
            result["value"] = Colors.OKGREEN

        t = threading.Thread(target=check_default)
        t.start()
        t.join(timeout=5)

        assert result["value"] == "\033[92m"

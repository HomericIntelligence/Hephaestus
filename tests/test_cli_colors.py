#!/usr/bin/env python3

"""Tests for hephaestus.cli.colors module."""

import sys
from io import StringIO

from hephaestus.cli.colors import Colors


def test_colors_defined():
    """Test that all color codes are defined."""
    assert hasattr(Colors, "HEADER")
    assert hasattr(Colors, "OKBLUE")
    assert hasattr(Colors, "OKCYAN")
    assert hasattr(Colors, "OKGREEN")
    assert hasattr(Colors, "WARNING")
    assert hasattr(Colors, "FAIL")
    assert hasattr(Colors, "ENDC")
    assert hasattr(Colors, "BOLD")
    assert hasattr(Colors, "UNDERLINE")


def test_colors_are_ansi_codes():
    """Test that color codes are ANSI escape sequences."""
    # Reset Colors to default state first
    Colors.OKGREEN = "\033[92m"
    Colors.ENDC = "\033[0m"

    assert Colors.OKGREEN.startswith("\033[")
    assert Colors.ENDC == "\033[0m"


def test_colors_disable():
    """Test that disable() sets all colors to empty strings."""
    # First ensure colors are enabled
    Colors.OKGREEN = "\033[92m"
    Colors.FAIL = "\033[91m"
    Colors.ENDC = "\033[0m"

    # Call disable
    Colors.disable()

    # Check all colors are empty
    assert Colors.HEADER == ""
    assert Colors.OKBLUE == ""
    assert Colors.OKCYAN == ""
    assert Colors.OKGREEN == ""
    assert Colors.WARNING == ""
    assert Colors.FAIL == ""
    assert Colors.ENDC == ""
    assert Colors.BOLD == ""
    assert Colors.UNDERLINE == ""


def test_colors_auto_detects_non_tty(monkeypatch):
    """Test that auto() disables colors when stdout is not a TTY."""
    # Reset colors first
    Colors.OKGREEN = "\033[92m"
    Colors.ENDC = "\033[0m"

    # Mock sys.stdout.isatty to return False
    class FakeStdout:
        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdout", FakeStdout())

    # Call auto
    Colors.auto()

    # Colors should be disabled
    assert Colors.OKGREEN == ""
    assert Colors.ENDC == ""


def test_colors_auto_keeps_tty_enabled(monkeypatch):
    """Test that auto() keeps colors when stdout is a TTY."""
    # Reset colors first
    Colors.OKGREEN = "\033[92m"
    Colors.ENDC = "\033[0m"

    # Mock sys.stdout.isatty to return True
    class FakeStdout:
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdout", FakeStdout())

    # Call auto
    Colors.auto()

    # Colors should remain enabled
    assert Colors.OKGREEN == "\033[92m"
    assert Colors.ENDC == "\033[0m"

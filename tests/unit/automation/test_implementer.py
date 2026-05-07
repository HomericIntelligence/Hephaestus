"""Tests for hephaestus.automation.implementer.

Smoke-level coverage of the CLI surface: argument parser shape and
top-level help. Deeper behavioral tests for IssueImplementer live next
to the workflow they exercise; here we guard against regressions in
the public CLI contract that other repos shell out to.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from hephaestus.automation import implementer


class TestModuleSurface:
    """Tests for module surface."""

    def test_main_callable(self) -> None:
        assert callable(implementer.main)

    def test_implementer_class_exposed(self) -> None:
        assert hasattr(implementer, "IssueImplementer")


class TestParseArgs:
    """Tests for parse args."""

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["impl"])
        args = implementer._parse_args()
        assert args.epic is None
        assert args.issues is None
        assert args.dry_run is False

    def test_explicit_issues(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["impl", "--issues", "1", "2", "3", "--dry-run"])
        args = implementer._parse_args()
        assert args.issues == [1, 2, 3]
        assert args.dry_run is True

    def test_epic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["impl", "--epic", "42"])
        args = implementer._parse_args()
        assert args.epic == 42


class TestHelpInvocation:
    """Tests for help invocation."""

    def test_module_help_exits_zero(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "hephaestus.automation.implementer", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "usage" in (result.stdout + result.stderr).lower()

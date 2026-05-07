"""Tests for hephaestus.automation.reviewer.

Smoke-level coverage of the CLI surface — argument parser shape and
top-level help. Mirrors test_implementer.py.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from hephaestus.automation import reviewer


class TestModuleSurface:
    """Tests for module surface."""

    def test_main_callable(self) -> None:
        assert callable(reviewer.main)

    def test_pr_reviewer_class_exposed(self) -> None:
        assert hasattr(reviewer, "PRReviewer")


class TestParseArgs:
    """Tests for parse args."""

    def test_requires_issues(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["rev"])
        with pytest.raises(SystemExit):
            reviewer._parse_args()

    def test_basic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["rev", "--issues", "1", "2"])
        args = reviewer._parse_args()
        assert args.issues == [1, 2]
        assert args.max_workers == 3
        assert args.dry_run is False

    def test_max_workers_out_of_range(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["rev", "--issues", "1", "--max-workers", "99"])
        with pytest.raises(SystemExit):
            reviewer._parse_args()


class TestHelpInvocation:
    """Tests for help invocation."""

    def test_module_help_exits_zero(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "hephaestus.automation.reviewer", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "usage" in (result.stdout + result.stderr).lower()

"""Tests for the extracted ``implementer_cli`` module (#468, #714).

Argument parsing and logging setup (``_parse_args`` / ``_setup_logging``) live
in ``implementer_cli.py`` (SRP — #468) and are re-exported from ``implementer``.
``main`` itself lives in ``implementer.py`` (relocated in #714 to break the
import cycle), where it resolves its collaborators through that module's own
namespace. These tests lock the contract that makes the split safe:

1. ``_parse_args`` / ``_setup_logging`` are re-exported from ``implementer`` and
   are the *same* objects as in ``implementer_cli`` (back-compat imports).
2. ``main`` is defined on ``implementer`` and observes
   ``patch.object(implementer, ...)`` on its collaborators — this is what keeps
   the pre-existing ``test_implementer_main`` smoke tests valid.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.automation import implementer, implementer_cli


class TestReExportIdentity:
    """The re-exports must be the same objects, not copies."""

    def test_main_defined_on_implementer(self) -> None:
        # ``main`` was relocated into ``implementer`` (#714); ``implementer_cli``
        # no longer defines it.
        assert implementer.main.__module__ == "hephaestus.automation.implementer"
        assert not hasattr(implementer_cli, "main")

    def test_parse_args_reexported(self) -> None:
        assert implementer._parse_args is implementer_cli._parse_args

    def test_setup_logging_reexported(self) -> None:
        assert implementer._setup_logging is implementer_cli._setup_logging


class TestPatchRoutingThroughImplementer:
    """``main`` must observe patches applied on the ``implementer`` module."""

    def test_main_uses_patched_implementer_deps(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Patched implementer deps must be honored by ``implementer.main``.

        Patching ``implementer.gh_list_open_issues`` / ``get_repo_root`` /
        ``IssueImplementer`` must reach the lookups inside ``main``.
        """
        monkeypatch.setattr(sys, "argv", ["impl", "--dry-run", "--no-ui", "--agent", "claude"])

        with (
            patch.object(implementer, "gh_list_open_issues", return_value=[]) as mock_list,
            patch.object(implementer, "get_repo_root", return_value=tmp_path),
        ):
            rc = implementer.main()

        assert rc == 0
        # Auto-discovery path with no --issues/--epic must consult the patched
        # lookup on the implementer module.
        mock_list.assert_called_once()


class TestParseArgs:
    """Argument parsing moved intact."""

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["impl"])
        args = implementer_cli._parse_args()
        assert args.epic is None
        assert args.issues is None
        assert args.dry_run is False
        assert args.no_advise is False

    def test_no_advise_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["impl", "--no-advise"])
        args = implementer_cli._parse_args()
        assert args.no_advise is True

    def test_nitpick_defaults_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["impl"])
        args = implementer_cli._parse_args()
        assert args.nitpick is False

    def test_nitpick_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["impl", "--nitpick"])
        args = implementer_cli._parse_args()
        assert args.nitpick is True

    def test_epic_and_issues_mutually_exclusive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["impl", "--epic", "1", "--issues", "2"])
        with pytest.raises(SystemExit):
            implementer_cli._parse_args()

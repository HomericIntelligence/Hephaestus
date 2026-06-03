"""Smoke tests for ``IssueImplementer.run()`` and ``implementer.main()``.

These tests pin the current behavior of the top-level entry points so that
the upcoming class split (state manager / summary printer / phase runner)
can be verified as a pure move-and-delegate. Three paths are covered:

1. ``main()`` with no discoverable issues returns 0 without raising.
2. ``main(--health-check)`` returns 0 (health check is best-effort and
   never marks the run as failed in current code).
3. ``--no-ui`` causes ``CursesUI`` to never be instantiated.

Tests intentionally exercise the real ``ImplementerOptions``/``IssueImplementer``
plumbing (no patching of the class under test) so they catch any behavioral
regression introduced by the refactor.

See #597.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest


def test_main_returns_zero_when_no_open_issues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Auto-discovery path with zero open issues must exit 0, not crash.

    Mirrors the #574 short-circuit: when ``gh_list_open_issues()`` returns
    ``[]``, ``IssueImplementer.run()`` returns ``{}`` and ``main()`` reports
    success.
    """
    from hephaestus.automation import implementer

    monkeypatch.setattr(sys, "argv", ["impl", "--dry-run", "--no-ui", "--agent", "claude"])

    with (
        patch.object(implementer, "gh_list_open_issues", return_value=[]),
        patch.object(implementer, "get_repo_root", return_value=tmp_path),
    ):
        rc = implementer.main()

    assert rc == 0


def test_main_health_check_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``--health-check`` must exit 0 even if individual probes log errors.

    Current ``_health_check()`` logs subprocess failures but always returns
    an empty results dict, and ``main()`` skips the failure-detection branch
    for health-check mode — so the documented exit code is 0.
    """
    from hephaestus.automation import implementer

    monkeypatch.setattr(sys, "argv", ["impl", "--health-check", "--no-ui", "--agent", "claude"])

    with patch.object(implementer, "get_repo_root", return_value=tmp_path):
        rc = implementer.main()

    assert rc == 0


def test_no_ui_flag_skips_curses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``--no-ui`` must prevent ``CursesUI`` from ever being instantiated.

    The curses UI is only constructed inside ``run()`` when
    ``options.enable_ui`` is True; ``--no-ui`` flips that flag off in
    ``main()``. We assert the class constructor is never reached.
    """
    from hephaestus.automation import implementer

    monkeypatch.setattr(sys, "argv", ["impl", "--dry-run", "--no-ui", "--agent", "claude"])

    with (
        patch.object(implementer, "gh_list_open_issues", return_value=[]),
        patch.object(implementer, "get_repo_root", return_value=tmp_path),
        patch.object(implementer, "CursesUI") as mock_curses,
    ):
        rc = implementer.main()

    assert rc == 0
    mock_curses.assert_not_called()

"""Tests for the local private denylist pre-commit hook."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_private_denylist_hook_scans_staged_and_tracked() -> None:
    """The hook should run the whole-repo/index privacy guard."""
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hook = next(
        h
        for repo in config["repos"]
        for h in repo.get("hooks", [])
        if h.get("id") == "check-private-denylist"
    )

    assert hook["entry"] == "python3 scripts/check_private_denylist.py --staged --tracked"
    assert hook["pass_filenames"] is False
    assert hook["always_run"] is True
    assert "types" not in hook

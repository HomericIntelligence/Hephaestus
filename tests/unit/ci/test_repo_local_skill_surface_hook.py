"""Tests for the local repo-local skill surface pre-commit hook."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_repo_local_skill_surface_hook_is_registered() -> None:
    """The hook must stay registered with an always-run system command."""
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hook = next(
        candidate
        for repo in config["repos"]
        for candidate in repo.get("hooks", [])
        if candidate.get("id") == "check-repo-local-skill-surface"
    )

    assert hook["entry"] == "python3 scripts/check_repo_local_skill_surface.py"
    assert hook["language"] == "system"
    assert hook["pass_filenames"] is False
    assert hook["always_run"] is True
    assert "files" not in hook
    assert "types" not in hook

"""Tests for the TODO/FIXME/HACK issue-link pre-commit hook."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_unlinked_todo_hook_is_registered() -> None:
    """The documented TODO marker convention must be enforced by pre-commit."""
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hook = next(
        (
            h
            for repo in config["repos"]
            for h in repo.get("hooks", [])
            if h.get("id") == "check-no-unlinked-todo"
        ),
        None,
    )

    assert hook is not None
    assert hook["entry"] == "pixi run --environment default hephaestus-check-unlinked-todo"
    assert hook["language"] == "system"
    assert hook["pass_filenames"] is False
    assert hook["files"] == r"^(hephaestus|scripts)/.*\.py$|^docs/TECH_DEBT\.md$"

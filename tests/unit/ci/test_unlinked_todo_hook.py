"""Tests for the local check-no-unlinked-todo pre-commit hook registration."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_unlinked_todo_hook_is_registered() -> None:
    """The hook must stay registered with its canonical entry command."""
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hook = next(
        h
        for repo in config["repos"]
        for h in repo.get("hooks", [])
        if h.get("id") == "check-no-unlinked-todo"
    )

    assert hook["entry"] == (
        "pixi run --environment default python3 -m hephaestus.validation.unlinked_todo"
    )
    assert hook["language"] == "system"
    assert hook["pass_filenames"] is False
    assert hook["files"] == r"^(hephaestus|scripts)/.*\.py$|^docs/TECH_DEBT\.md$"

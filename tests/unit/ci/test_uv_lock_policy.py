"""Guard uv.lock freshness enforcement in the pre-commit hook contract."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_uv_lock_hook_is_check_only() -> None:
    """The read-only uv lock hook has the expected scope and arguments."""
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    repo = next(
        repo
        for repo in config["repos"]
        if repo["repo"] == "https://github.com/astral-sh/uv-pre-commit"
    )
    hook = next(hook for hook in repo["hooks"] if hook["id"] == "uv-lock")

    # The rev must be an immutable full commit SHA (digest-pinned like every
    # other third-party hook; the tag is kept as a comment beside it).
    assert isinstance(repo["rev"], str)
    assert re.fullmatch(r"[0-9a-f]{40}", repo["rev"]), repo["rev"]
    assert hook["args"] == ["--check"]
    assert hook["files"] == r"^(pyproject\.toml|uv\.lock)$"
    assert hook["pass_filenames"] is False

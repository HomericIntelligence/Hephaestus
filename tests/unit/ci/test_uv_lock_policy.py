"""Guard uv.lock freshness enforcement and lifecycle documentation."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_uv_lock_hook_is_check_only() -> None:
    """The local uv lock hook is read-only and scoped to lock inputs."""
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    repo = next(
        repo
        for repo in config["repos"]
        if repo["repo"] == "https://github.com/astral-sh/uv-pre-commit"
    )
    hook = next(hook for hook in repo["hooks"] if hook["id"] == "uv-lock")

    assert repo["rev"] == "0.11.28"
    assert hook["args"] == ["--check"]
    assert hook["files"] == r"^(pyproject\.toml|uv\.lock)$"
    assert hook["pass_filenames"] is False


def test_contributing_documents_uv_lock_lifecycle() -> None:
    """The contributor guide must describe the complete uv lock lifecycle."""
    text = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    heading = "### Python dependency and `uv.lock` lifecycle"
    section = text.split(heading, 1)[1].split("\n## ", 1)[0]

    for required in (
        "Dependabot",
        "weekly",
        "`pyproject.toml`",
        "`uv.lock`",
        "uv lock",
        "uv lock --upgrade-package <name>",
        "uv lock --upgrade",
        "uv lock --check",
    ):
        assert required in section

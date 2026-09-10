"""Test session lookup across registered worktrees."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.automation import agent_config
from hephaestus.automation.agent_config import session_jsonl_path, session_uuid


def test_registered_worktree_resolves_repo_root_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worktree caller finds a session first created from the repo root."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo_root = tmp_path / "owner-a" / "Hephaestus"
    worktree = repo_root / "build" / ".worktrees" / "issue-2284"
    worktree.mkdir(parents=True)
    monkeypatch.setattr(agent_config, "_checkout_identity", lambda _cwd: "checkout-family")
    sid = session_uuid("Hephaestus", 2284, "plan-reviewer", "fable", cwd=repo_root)

    transcript = session_jsonl_path(sid, repo_root)
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("{}\n", encoding="utf-8")

    with patch(
        "hephaestus.automation.agent_config._registered_worktree_roots",
        return_value=(repo_root.resolve(), worktree.resolve()),
    ):
        assert agent_config.resolve_session_jsonl_path(sid, worktree) == transcript

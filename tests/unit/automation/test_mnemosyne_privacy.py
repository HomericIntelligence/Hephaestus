"""Privacy tests for durable Mnemosyne learning artifacts."""
# ruff: noqa: D101, D103

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hephaestus.automation.mnemosyne_delivery import (
    ExistingPullRequest,
    LearnDeliveryError,
    LearnDeliveryRequest,
    LearnDeliveryService,
)


class FakeGit:
    def __call__(
        self,
        cwd: Path,
        argv: tuple[str, ...],
        timeout_s: int,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout_s
        if argv == ("diff", "--name-only", "HEAD"):
            return subprocess.CompletedProcess(["git"], 0, stdout="skills/example.md\n")
        if argv == ("rev-parse", "HEAD"):
            return subprocess.CompletedProcess(["git"], 0, stdout=f"{'a' * 40}\n")
        return subprocess.CompletedProcess(["git"], 0, stdout="")


class FakeGitHub:
    def __init__(self) -> None:
        self.created = False

    def create_pr(self, **_kwargs: object) -> int:
        self.created = True
        return 1

    def read_pr_head(self, **_kwargs: object) -> tuple[str, str]:
        return "https://github.com/acme/Mnemosyne/pull/1", "a" * 40

    def read_existing_pr(self, **_kwargs: object) -> ExistingPullRequest:
        raise AssertionError("existing PR binding is not expected for this test")


def _request(tmp_path: Path, **overrides: object) -> LearnDeliveryRequest:
    values: dict[str, object] = {
        "repository": "acme/Mnemosyne",
        "worktree_path": tmp_path,
        "branch": "skill/example",
        "base_branch": "main",
        "allowed_paths": ("skills/example.md",),
        "commit_message": "docs(skills): capture reusable workflow",
        "pr_title": "docs(skills): capture reusable workflow",
        "pr_body": "Summary.\n",
        "disposition": "create",
        "validation_evidence": ("pytest",),
    }
    values.update(overrides)
    return LearnDeliveryRequest(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("commit_message", "docs(skills): capture token=abc123"),
        ("pr_title", "docs(skills): capture SECRET=value"),
        ("pr_body", "Includes sk-abcdefghijklmnopqrstuvwxyz"),
    ],
)
def test_delivery_rejects_sensitive_text_before_commit_or_pr(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    github = FakeGitHub()
    service = LearnDeliveryService(git=FakeGit(), github=github)

    with pytest.raises(LearnDeliveryError, match="sensitive material"):
        service.deliver(_request(tmp_path, **{field: value}))

    assert github.created is False

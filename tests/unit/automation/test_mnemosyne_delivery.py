"""Tests for PR-backed Mnemosyne learning delivery."""
# ruff: noqa: D103

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hephaestus.automation.mnemosyne_delivery import (
    LearnDeliveryError,
    LearnDeliveryRequest,
    LearnDeliveryService,
)

HEAD = "f" * 40


class FakeGitHub:
    """PR create/readback fake."""

    def __init__(self, *, create_url: str = "https://github.com/acme/Mnemosyne/pull/7") -> None:
        self.create_url = create_url
        self.created: list[dict[str, object]] = []
        self.read: list[int] = []

    def create_pr(self, *, repository: str, head: str, base: str, title: str, body: str) -> int:
        self.created.append(
            {"repository": repository, "head": head, "base": base, "title": title, "body": body}
        )
        return int(self.create_url.rsplit("/", 1)[1])

    def read_pr_head(self, *, repository: str, number: int) -> tuple[str, str]:
        self.read.append(number)
        return self.create_url, HEAD


class FakeGit:
    """Command recorder for delivery git operations."""

    def __init__(self, *, diff: str = "skills/example.md\n", head: str = HEAD) -> None:
        self.diff = diff
        self.head = head
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self,
        cwd: Path,
        argv: tuple[str, ...],
        timeout_s: int,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout_s
        self.calls.append(argv)
        if argv == ("diff", "--name-only", "HEAD"):
            return _completed(stdout=self.diff)
        if argv == ("rev-parse", "HEAD"):
            return _completed(stdout=f"{self.head}\n")
        return _completed()


def _completed(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["git"], returncode, stdout=stdout, stderr=stderr)


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
        "validation_evidence": ("pytest tests/unit",),
    }
    values.update(overrides)
    return LearnDeliveryRequest(**values)  # type: ignore[arg-type]


def test_delivery_commits_pushes_and_reads_back_pr(tmp_path: Path) -> None:
    git = FakeGit()
    github = FakeGitHub()
    service = LearnDeliveryService(git=git, github=github)

    receipt = service.deliver(_request(tmp_path))

    assert receipt.pr_url == "https://github.com/acme/Mnemosyne/pull/7"
    assert receipt.pr_number == 7
    assert receipt.readback_head_sha == HEAD
    assert receipt.commit_sha == HEAD
    assert receipt.final_disposition == "create"
    assert receipt.local_only is False
    assert ("commit", "-S", "-s", "-m", "docs(skills): capture reusable workflow") in git.calls
    assert (
        "push",
        "--force-with-lease",
        "--force-if-includes",
        "origin",
        "skill/example",
    ) in git.calls
    assert github.created[0]["repository"] == "acme/Mnemosyne"


def test_delivery_rejects_paths_outside_allowlist(tmp_path: Path) -> None:
    service = LearnDeliveryService(
        git=FakeGit(diff="skills/example.md\ndocs/extra.md\n"), github=FakeGitHub()
    )

    with pytest.raises(LearnDeliveryError, match="outside write allowlist"):
        service.deliver(_request(tmp_path))


def test_delivery_rejects_local_only_when_pr_create_fails(tmp_path: Path) -> None:
    class NoPullRequest(FakeGitHub):
        def create_pr(self, **_kwargs: object) -> int:
            raise LearnDeliveryError("PR creation unavailable")

    service = LearnDeliveryService(git=FakeGit(), github=NoPullRequest())

    with pytest.raises(LearnDeliveryError, match="PR creation unavailable"):
        service.deliver(_request(tmp_path))


def test_delivery_rejects_readback_head_mismatch(tmp_path: Path) -> None:
    class Drifted(FakeGitHub):
        def read_pr_head(self, *, repository: str, number: int) -> tuple[str, str]:
            del repository, number
            return "https://github.com/acme/Mnemosyne/pull/7", "0" * 40

    service = LearnDeliveryService(git=FakeGit(), github=Drifted())

    with pytest.raises(LearnDeliveryError, match="readback head"):
        service.deliver(_request(tmp_path))


def test_existing_pr_mode_pushes_bound_branch_without_creating_competing_pr(tmp_path: Path) -> None:
    git = FakeGit()
    github = FakeGitHub()
    service = LearnDeliveryService(git=git, github=github)

    receipt = service.deliver(_request(tmp_path, existing_pr_number=12))

    assert receipt.pr_number == 12
    assert github.created == []
    assert github.read == [12]

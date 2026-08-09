"""Host-owned PR delivery for Mnemosyne learning updates."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from hephaestus.utils.helpers import NETWORK_TIMEOUT


class LearnDeliveryError(RuntimeError):
    """Raised when learning cannot be proven by a PR readback receipt."""


class LearnGitHub(Protocol):
    """Closed GitHub operations used by learning delivery."""

    def create_pr(self, *, repository: str, head: str, base: str, title: str, body: str) -> int:
        """Create a PR and return its number."""

    def read_pr_head(self, *, repository: str, number: int) -> tuple[str, str]:
        """Return ``(url, head_sha)`` for a PR number."""


GitRunner = Callable[[Path, tuple[str, ...], int], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class LearnDeliveryRequest:
    """Inputs for host-owned Mnemosyne PR delivery."""

    repository: str
    worktree_path: Path
    branch: str
    base_branch: str
    allowed_paths: tuple[str, ...]
    commit_message: str
    pr_title: str
    pr_body: str
    disposition: str
    validation_evidence: tuple[str, ...]
    existing_pr_number: int | None = None


@dataclass(frozen=True)
class LearnDeliveryReceipt:
    """Receipt proving Mnemosyne learning ended in a readback PR."""

    repository: str
    branch: str
    base_branch: str
    commit_sha: str
    pr_url: str
    pr_number: int
    readback_head_sha: str
    validation_evidence: tuple[str, ...]
    final_disposition: str
    local_only: bool = False
    signed_commit: bool = True
    dco_signed_off: bool = True

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable receipt dictionary."""
        return asdict(self)


def _run_git(cwd: Path, argv: tuple[str, ...], timeout_s: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", *argv),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )


def _require_success(result: subprocess.CompletedProcess[str], action: str) -> str:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise LearnDeliveryError(f"{action} failed: {detail or result.returncode}")
    return result.stdout or ""


def _validate_branch(branch: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) or ".." in branch:
        raise LearnDeliveryError(f"unsafe branch name: {branch}")


def _validate_delivery_text(text: str, field: str) -> None:
    secret_markers = (r"sk-[A-Za-z0-9_-]{12,}", r"(?i)secret\s*=", r"(?i)token\s*=")
    if any(re.search(pattern, text) for pattern in secret_markers):
        raise LearnDeliveryError(f"{field} contains sensitive material")


class LearnDeliveryService:
    """Commit, push, open/read back a PR, and return a delivery receipt."""

    def __init__(
        self,
        *,
        git: GitRunner = _run_git,
        github: LearnGitHub,
        timeout_s: int = NETWORK_TIMEOUT,
    ) -> None:
        """Initialize delivery with injectable Git and GitHub adapters."""
        self.git = git
        self.github = github
        self.timeout_s = timeout_s

    def deliver(self, request: LearnDeliveryRequest) -> LearnDeliveryReceipt:
        """Deliver a learning change through a verified Mnemosyne PR."""
        _validate_branch(request.branch)
        _validate_delivery_text(request.commit_message, "commit message")
        _validate_delivery_text(request.pr_title, "PR title")
        _validate_delivery_text(request.pr_body, "PR body")
        changed_paths = self._changed_paths(request.worktree_path)
        outside = sorted(set(changed_paths).difference(request.allowed_paths))
        if outside:
            raise LearnDeliveryError("changed paths outside write allowlist: " + ", ".join(outside))
        if not changed_paths:
            raise LearnDeliveryError("no Mnemosyne changes to deliver")

        _require_success(
            self.git(request.worktree_path, ("add", *request.allowed_paths), self.timeout_s),
            "git add",
        )
        _require_success(
            self.git(
                request.worktree_path,
                ("commit", "-S", "-s", "-m", request.commit_message),
                self.timeout_s,
            ),
            "git commit",
        )
        commit_sha = _require_success(
            self.git(request.worktree_path, ("rev-parse", "HEAD"), self.timeout_s),
            "HEAD read",
        ).strip()
        if re.fullmatch(r"[0-9a-f]{40}", commit_sha) is None:
            raise LearnDeliveryError(f"invalid commit SHA: {commit_sha}")
        _require_success(
            self.git(
                request.worktree_path,
                (
                    "push",
                    "--force-with-lease",
                    "--force-if-includes",
                    "origin",
                    request.branch,
                ),
                self.timeout_s,
            ),
            "git push",
        )

        if request.existing_pr_number is None:
            pr_number = self.github.create_pr(
                repository=request.repository,
                head=request.branch,
                base=request.base_branch,
                title=request.pr_title,
                body=request.pr_body,
            )
        else:
            pr_number = request.existing_pr_number
        pr_url, readback_head = self.github.read_pr_head(
            repository=request.repository,
            number=pr_number,
        )
        if readback_head != commit_sha:
            raise LearnDeliveryError(
                f"PR readback head {readback_head} does not match pushed {commit_sha}"
            )
        return LearnDeliveryReceipt(
            repository=request.repository,
            branch=request.branch,
            base_branch=request.base_branch,
            commit_sha=commit_sha,
            pr_url=pr_url,
            pr_number=pr_number,
            readback_head_sha=readback_head,
            validation_evidence=request.validation_evidence,
            final_disposition=request.disposition,
        )

    def _changed_paths(self, worktree_path: Path) -> tuple[str, ...]:
        output = _require_success(
            self.git(worktree_path, ("diff", "--name-only", "HEAD"), self.timeout_s),
            "changed path discovery",
        )
        return tuple(line.strip() for line in output.splitlines() if line.strip())


def valid_delivery_receipt(value: object) -> bool:
    """Return True when a value is a PR-backed learning receipt."""
    if isinstance(value, LearnDeliveryReceipt):
        return bool(
            value.pr_url
            and value.pr_number > 0
            and value.commit_sha
            and value.readback_head_sha == value.commit_sha
            and not value.local_only
        )
    if isinstance(value, dict):
        commit_sha = value.get("commit_sha")
        return bool(
            value.get("pr_url")
            and isinstance(value.get("pr_number"), int)
            and int(value.get("pr_number", 0)) > 0
            and isinstance(commit_sha, str)
            and value.get("readback_head_sha") == commit_sha
            and value.get("local_only") is not True
        )
    return False

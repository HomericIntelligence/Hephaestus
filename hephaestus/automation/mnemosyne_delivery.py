"""Host-owned PR delivery for Mnemosyne learning updates."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from hephaestus.config.child_environments import build_git_signing_env
from hephaestus.utils.helpers import NETWORK_TIMEOUT, run_subprocess


class LearnDeliveryError(RuntimeError):
    """Raised when learning cannot be proven by a PR readback receipt."""


class LearnGitHub(Protocol):
    """Closed GitHub operations used by learning delivery."""

    def create_pr(self, *, repository: str, head: str, base: str, title: str, body: str) -> int:
        """Create a PR and return its number."""

    def read_pr_head(self, *, repository: str, number: int) -> tuple[str, str]:
        """Return ``(url, head_sha)`` for a PR number."""

    def read_existing_pr(self, *, repository: str, number: int) -> ExistingPullRequest:
        """Return the immutable source binding for an existing open PR."""


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
class ExistingPullRequest:
    """Fresh GitHub identity and source-ref facts for an existing PR."""

    repository: str
    number: int
    url: str
    state: str
    base_ref: str
    source_repository: str
    source_ref: str
    head_sha: str


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
    return run_subprocess(
        ["git", *argv],
        env=build_git_signing_env(),
        cwd=cwd,
        check=False,
        timeout=timeout_s,
        track_process_group=True,
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


def _origin_matches_repository(origin: str, repository: str) -> bool:
    """Return whether a Git remote URL identifies an expected repository slug."""
    normalized = origin.strip().removesuffix(".git")
    return (
        normalized == repository
        or normalized.endswith(f"/{repository}")
        or normalized.endswith(f":{repository}")
    )


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
        existing_pr = self._bind_existing_pr_worktree(request)
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
        self._push(request, existing_pr)

        if request.existing_pr_number is None:
            pr_number = self.github.create_pr(
                repository=request.repository,
                head=request.branch,
                base=request.base_branch,
                title=request.pr_title,
                body=request.pr_body,
            )
        else:
            if existing_pr is None:
                raise LearnDeliveryError("existing PR binding was not established")
            pr_number = existing_pr.number
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

    def _bind_existing_pr_worktree(
        self, request: LearnDeliveryRequest
    ) -> ExistingPullRequest | None:
        """Bind an existing PR and require its worktree to start at its live head.

        Existing-PR writers must create their isolated worktree from this bound
        ``head_sha`` before they edit it.  This delivery boundary rechecks that
        invariant before staging any path, so a stale number, a different
        source ref, or a moved remote is never published.
        """
        if request.existing_pr_number is None:
            return None
        binding = self.github.read_existing_pr(
            repository=request.repository,
            number=request.existing_pr_number,
        )
        if binding.repository != request.repository or binding.number != request.existing_pr_number:
            raise LearnDeliveryError("existing PR identity does not match delivery request")
        if binding.state != "OPEN":
            raise LearnDeliveryError(f"existing PR is not open: {binding.state or '<unknown>'}")
        if binding.base_ref != request.base_branch:
            raise LearnDeliveryError("existing PR base ref does not match delivery request")
        if binding.source_ref != request.branch:
            raise LearnDeliveryError("existing PR source ref does not match delivery request")
        _validate_branch(binding.source_ref)
        if not binding.source_repository:
            raise LearnDeliveryError("existing PR lacks source repository")
        if re.fullmatch(r"[0-9a-f]{40}", binding.head_sha) is None:
            raise LearnDeliveryError("existing PR lacks a valid source head SHA")

        _require_success(
            self.git(
                request.worktree_path,
                ("fetch", "origin", binding.source_ref),
                self.timeout_s,
            ),
            "existing PR source fetch",
        )
        origin = _require_success(
            self.git(request.worktree_path, ("remote", "get-url", "origin"), self.timeout_s),
            "existing PR source origin read",
        )
        if not _origin_matches_repository(origin, binding.source_repository):
            raise LearnDeliveryError("existing PR source repository does not match worktree origin")
        remote_head = _require_success(
            self.git(
                request.worktree_path,
                ("rev-parse", f"refs/remotes/origin/{binding.source_ref}"),
                self.timeout_s,
            ),
            "existing PR source ref read",
        ).strip()
        if remote_head != binding.head_sha:
            raise LearnDeliveryError("existing PR source ref moved before mutation")
        worktree_head = _require_success(
            self.git(request.worktree_path, ("rev-parse", "HEAD"), self.timeout_s),
            "existing PR worktree HEAD read",
        ).strip()
        if worktree_head != binding.head_sha:
            raise LearnDeliveryError(
                "existing PR worktree was not created from the bound source head"
            )
        return binding

    def _push(
        self,
        request: LearnDeliveryRequest,
        existing_pr: ExistingPullRequest | None,
    ) -> None:
        """Push a new branch or a bound existing source ref with a strict lease."""
        if existing_pr is None:
            argv = (
                "push",
                "--force-with-lease",
                "--force-if-includes",
                "origin",
                request.branch,
            )
        else:
            ref = f"refs/heads/{existing_pr.source_ref}"
            argv = (
                "push",
                f"--force-with-lease={ref}:{existing_pr.head_sha}",
                "--force-if-includes",
                "origin",
                f"HEAD:{ref}",
            )
        _require_success(self.git(request.worktree_path, argv, self.timeout_s), "git push")

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

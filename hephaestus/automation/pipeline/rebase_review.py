"""Host evidence that keeps a review apart from a later rebase."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from hephaestus.automation import git_utils

REBASE_REVIEW_PROOF_KEY = "retained_rebase_review_proof"
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


@dataclass(frozen=True)
class RebaseReviewProof:
    """Bind the initial review to a published tree that the host checked."""

    repository: str
    issue_number: int
    pr_number: int
    reviewed_head_sha: str
    reviewed_base_sha: str
    source_head_sha: str
    target_base_sha: str
    resulting_head_sha: str
    resulting_tree_sha: str
    original_audit_id: str

    def __post_init__(self) -> None:
        """Reject identities that are not full and commit references."""
        if (
            not isinstance(self.repository, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository) is None
        ):
            raise ValueError("rebase review repository is invalid")
        if any(
            type(value) is not int or value <= 0 for value in (self.issue_number, self.pr_number)
        ):
            raise ValueError("rebase review issue or PR is invalid")
        for value in (
            self.reviewed_head_sha,
            self.reviewed_base_sha,
            self.source_head_sha,
            self.target_base_sha,
            self.resulting_head_sha,
            self.resulting_tree_sha,
        ):
            if not isinstance(value, str) or _SHA.fullmatch(value) is None:
                raise ValueError("rebase review commit is invalid")
        expected = (
            f"<!-- hephaestus-implementation-go-audit:pr={self.pr_number}:"
            f"head={self.reviewed_head_sha} -->"
        )
        if self.original_audit_id != expected:
            raise ValueError("rebase review audit identity is invalid")


def verify_rebase_tree(
    cwd: Path,
    *,
    reviewed_head_sha: str,
    reviewed_base_sha: str,
    target_base_sha: str,
    resulting_head_sha: str,
    timeout: int,
) -> str | None:
    """Require the exact tree from the initial change and the new base."""
    if any(
        _SHA.fullmatch(value) is None
        for value in (reviewed_head_sha, reviewed_base_sha, target_base_sha, resulting_head_sha)
    ):
        return None
    try:
        ancestor = git_utils.run(
            ["git", "merge-base", "--all", reviewed_head_sha, reviewed_base_sha],
            cwd=cwd,
            check=False,
            timeout=timeout,
        )
        base = ancestor.stdout.strip()
        if ancestor.returncode != 0 or _SHA.fullmatch(base) is None:
            return None
        replay = git_utils.run(
            [
                "git",
                "merge-tree",
                "--write-tree",
                "--no-messages",
                f"--merge-base={base}",
                reviewed_head_sha,
                target_base_sha,
            ],
            cwd=cwd,
            check=False,
            timeout=timeout,
        )
        if replay.returncode != 0:
            return None
        tree = replay.stdout.strip()
        if _SHA.fullmatch(tree) is None:
            return None
        actual = git_utils.run(
            ["git", "rev-parse", "--verify", f"{resulting_head_sha}^{{tree}}"],
            cwd=cwd,
            check=False,
            timeout=timeout,
        )
        if actual.returncode != 0 or actual.stdout.strip() != tree:
            return None
        return tree
    except (OSError, subprocess.SubprocessError):
        return None

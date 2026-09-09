"""Bounded, inert diagnostics published by the PR-review stage."""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from logging import Logger
from typing import Protocol

from ..diagnostics import redact_diagnostic_text
from .pr_review_verification import (
    HOST_VERIFICATION_DIAGNOSTIC_MAX,
    _HostVerificationSpec,
)

_HOST_VERIFICATION_FAILURE_COMMENT_PREFIX = "<!-- hephaestus-host-verification-failure:"


class _CommentWriter(Protocol):
    """Minimal GitHub mutation surface needed by diagnostics."""

    def upsert_issue_comment(self, issue_number: int, marker: str, body: str) -> None:
        """Create or update one marker-owned issue/PR comment."""


def _indented_diagnostic(value: object) -> str:
    """Render bounded, redacted diagnostic text as inert Markdown code."""
    text = redact_diagnostic_text(str(value or "")[-HOST_VERIFICATION_DIAGNOSTIC_MAX:])
    return "\n".join(f"    {line}" for line in text.splitlines() or [""])


def host_verification_failure_comment(
    verification: _HostVerificationSpec | None,
    diagnostic: Mapping[str, object],
) -> tuple[str, str]:
    """Return the exact-head marker and bounded host-failure comment."""
    head = str(diagnostic.get("head_sha") or "")
    verification_id = verification.descr if verification is not None else "unknown"
    marker = f"{_HOST_VERIFICATION_FAILURE_COMMENT_PREFIX}{head}:{verification_id} -->"
    raw_argv = diagnostic.get("argv")
    argv = raw_argv if isinstance(raw_argv, (list, tuple)) else ()
    command = shlex.join(str(part) for part in argv)
    path = str(diagnostic.get("path") or "")
    sections = [
        marker,
        "### Host verification failed",
        "",
        "[auto-msg] The fixed host-verification command failed before source review. "
        + "The PR remains `state:implementation-no-go`.",
        "",
        "**Reviewed head**",
        "",
        _indented_diagnostic(head),
        "",
        "**Verification command**",
        "",
        _indented_diagnostic(command),
    ]
    if path:
        sections.extend(["", "**Affected path**", "", _indented_diagnostic(path)])
    sections.extend(
        [
            "",
            "**Failure classification**",
            "",
            _indented_diagnostic(diagnostic.get("failure_kind")),
            "",
            "**Failure**",
            "",
            _indented_diagnostic(diagnostic.get("error")),
        ]
    )
    stdout_tail = str(diagnostic.get("stdout_tail") or "")
    if stdout_tail:
        sections.extend(["", "**Standard output (tail)**", "", _indented_diagnostic(stdout_tail)])
    stderr_tail = str(diagnostic.get("stderr_tail") or "")
    if stderr_tail:
        sections.extend(["", "**Standard error (tail)**", "", _indented_diagnostic(stderr_tail)])
    return marker, "\n".join(sections)


def publish_host_verification_failure(
    github: _CommentWriter,
    pr_number: int,
    verification: _HostVerificationSpec | None,
    diagnostic: Mapping[str, object],
    logger: Logger,
) -> bool:
    """Publish one exact-head diagnostic, returning whether it was durable."""
    try:
        github.upsert_issue_comment(
            pr_number, *host_verification_failure_comment(verification, diagnostic)
        )
    except Exception as error:
        logger.warning(
            "pr_review: failed to publish PR #%d host-verification failure: %s",
            pr_number,
            error,
        )
        return False
    return True


__all__ = ["host_verification_failure_comment", "publish_host_verification_failure"]

"""Bounded, inert host-verification diagnostics.

A fixed host check can fail before source review starts. The host publishes
one exact-head comment that names the failure. The comment is stage-neutral:
any stage may publish it, and no stage owns the text.

The module belongs to the library layer. It keeps its own redaction patterns
so it never imports the automation diagnostic module.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Mapping
from logging import Logger
from typing import Protocol

from hephaestus.diagnostics import redact_private_key_blocks

__all__ = [
    "HOST_VERIFICATION_DIAGNOSTIC_MAX",
    "host_verification_failure_comment",
    "publish_host_verification_failure",
    "publish_stage_diagnostic",
]

HOST_VERIFICATION_DIAGNOSTIC_MAX = 4_000
_REDACTION = "<redacted>"
_HOST_VERIFICATION_FAILURE_COMMENT_PREFIX = "<!-- hephaestus-host-verification-failure:"

_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[0-9A-Za-z]{36,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bsk_(?:live|test)_[0-9A-Za-z]{16,}\b"),
)


def _prefix_replacement(prefix_group: str) -> Callable[[re.Match[str]], str]:
    """Return the replacement callable for one prefix-preserving pattern."""

    def _replace(match: re.Match[str]) -> str:
        suffix = match.group("suffix") if "suffix" in match.groupdict() else ""
        return f"{match.group(prefix_group)}{_REDACTION}{suffix}"

    return _replace


_PREFIX_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(
            r"(?P<prefix>(?:authorization\s*[:=]\s*)(?:bearer|basic|token)\s+)"
            r"(?P<value>[^\s,;]+)",
            flags=re.IGNORECASE,
        ),
        "prefix",
        "value",
    ),
    (
        re.compile(
            r"(?P<prefix>\b(?:token|api[_-]?key|apikey|access[_-]?token|"
            r"client[_-]?secret|password|passwd|secret)\b\s*[:=]\s*[\"']?)"
            r"(?P<value>[^\s,;}\"'\\\n]+)",
            flags=re.IGNORECASE,
        ),
        "prefix",
        "value",
    ),
    (
        re.compile(r"(?P<prefix>://[^/\s:@]+:)(?P<value>[^@\s/]+)(?P<suffix>@)"),
        "prefix",
        "value",
    ),
)


def _redact(text: str) -> str:
    """Return text with secret-like content masked for durable persistence."""
    redacted = redact_private_key_blocks(text)
    for pattern in _TOKEN_PATTERNS:
        redacted = pattern.sub(_REDACTION, redacted)
    for pattern, prefix_group, _value_group in _PREFIX_PATTERNS:
        redacted = pattern.sub(_prefix_replacement(prefix_group), redacted)
    return redacted


class _HostVerificationSpec(Protocol):
    """The part of a fixed host check that a diagnostic names."""

    descr: str
    argv: tuple[str, ...]
    changed_path: str | None


class _CommentWriter(Protocol):
    """Minimal GitHub mutation surface needed by diagnostics."""

    def upsert_issue_comment(self, issue_number: int, marker: str, body: str) -> None:
        """Create or update one marker-owned issue/PR comment."""


def _indented_diagnostic(value: object) -> str:
    """Render bounded, redacted diagnostic text as inert Markdown code."""
    text = _redact(str(value or "")[-HOST_VERIFICATION_DIAGNOSTIC_MAX:])
    return "\n".join(f"    {line}" for line in text.splitlines() or [""])


def host_verification_failure_comment(
    verification: _HostVerificationSpec | None,
    diagnostic: Mapping[str, object],
) -> tuple[str, str]:
    """Return the exact-head marker and bounded host-failure comment."""
    head = str(diagnostic.get("head_sha") or "")
    rebase = diagnostic.get("operation") == "rebase"
    verification_id = (
        verification.descr if verification is not None else "rebase" if rebase else "unknown"
    )
    marker = f"{_HOST_VERIFICATION_FAILURE_COMMENT_PREFIX}{head}:{verification_id} -->"
    raw_argv = diagnostic.get("argv")
    argv = raw_argv if isinstance(raw_argv, (list, tuple)) else ()
    command = shlex.join(str(part) for part in argv)
    path = str(diagnostic.get("path") or "")
    labels = (
        "The PR verdict labels did not change."
        if diagnostic.get("labels_unchanged") is True
        else "The source-verdict result is separate from this host diagnostic."
    )
    summary = (
        "[auto-msg] A required host capability is unavailable. The fixed check did not start. "
        if diagnostic.get("capability_failure") is True
        else "[auto-msg] The fixed host-verification command failed before source review. "
    )
    if rebase:
        summary = "[auto-msg] A required rebase host operation is blocked. "
    sections = [
        marker,
        "### Host verification failed",
        "",
        summary + labels,
        "",
        "**Source head**" if rebase else "**Reviewed head**",
        "",
        _indented_diagnostic(head),
        "",
        "**Verification command**",
        "",
        _indented_diagnostic(command),
    ]
    if path:
        sections.extend(["", "**Affected path**", "", _indented_diagnostic(path)])
    for title, key in (
        ("Capability step", "failed_step"),
        ("Purpose", "purpose"),
        ("Receipt", "receipt_id"),
        ("Retained inspection directory", "retained_root"),
        ("Operating-system error", "operating_system_error"),
        ("Exception type", "exception_type"),
        ("Return code", "return_code"),
        ("Cleanup state", "cleanup_state"),
        ("Receipt storage error", "persistence_error"),
        ("Storage exception type", "persistence_exception_type"),
    ):
        value = diagnostic.get(key)
        if value is not None and value != "":
            sections.extend(["", f"**{title}**", "", _indented_diagnostic(value)])
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


def publish_stage_diagnostic(
    github: _CommentWriter,
    issue_number: int,
    marker: str,
    body: str,
) -> None:
    """Publish one automation-owned marker comment for a stage diagnostic.

    The helper does no logging and no retry. Every failure reaches the caller,
    which keeps its own recovery route.
    """
    github.upsert_issue_comment(issue_number, marker, body)

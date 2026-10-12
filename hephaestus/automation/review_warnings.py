"""Shared warning contract for informational review finding validation.

Review finding validation is informational. One malformed finding does not
invalidate a review audit. The host keeps the valid findings, warns about each
malformed finding, and carries the malformed content as bounded text.

Every warning carries three parts:

- a stable machine-readable code,
- a short message that states the problem, and
- a recovery instruction that states the action a reviewer must take.

A warning without a recovery instruction does not meet this contract. The
constructor refuses such a warning.

The explicit reviewer verdict is not a warning. It stays required, because it
is the implementation sign-off gate. This module never infers a verdict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

MAX_REVIEW_WARNING_CODE_CHARS = 64
MAX_REVIEW_WARNING_MESSAGE_CHARS = 1_024
MAX_REVIEW_WARNING_RECOVERY_CHARS = 1_024
_TRUNCATION_SUFFIX = "... [truncated]"
_CODE_RE = re.compile(r"[a-z0-9_-]+")

MalformedReviewFindingReason = Literal[
    "finding_invalid",
    "finding_not_an_object",
    "finding_path_invalid",
    "finding_line_invalid",
    "finding_side_unsupported",
    "finding_severity_invalid",
    "finding_body_invalid",
    "finding_evidence_invalid",
    "finding_id_invalid",
    "finding_reserved_control_text",
    "finding_scope_retraction_paths_invalid",
    "finding_scope_retraction_conflict",
    "finding_content_truncated",
    "malformed_finding_budget_exhausted",
]

AnchorCorrectionWarningReason = Literal[
    "anchor_reviewed_diff_unavailable",
    "anchor_path_not_in_diff",
    "anchor_line_not_in_diff",
    "anchor_unsupported_side",
]

ReviewWarningReason = Literal[
    MalformedReviewFindingReason,
    AnchorCorrectionWarningReason,
]


def bounded_review_text(value: str, limit: int) -> str:
    """Return text that is no longer than the limit.

    Args:
        value: Untrusted reviewer text.
        limit: Maximum number of characters to keep.

    Returns:
        The complete text, or a truncated text with a marker.

    Raises:
        ValueError: If the limit is not positive.

    """
    if limit <= 0:
        raise ValueError("review text limit must be positive")
    if len(value) <= limit:
        return value
    keep = max(limit - len(_TRUNCATION_SUFFIX), 0)
    return f"{value[:keep].rstrip()}{_TRUNCATION_SUFFIX}"


@dataclass(frozen=True)
class ReviewWarning:
    """One informational warning about a reviewer finding.

    Attributes:
        code: Stable machine-readable slug.
        message: Short description of the problem.
        recovery: Human-readable recovery instruction.

    """

    code: str
    message: str
    recovery: str

    def __post_init__(self) -> None:
        """Check the bounded warning fields.

        Raises:
            ValueError: If a required field is empty or a code is not a slug.

        """
        code = self.code.strip() if isinstance(self.code, str) else ""
        if len(code) > MAX_REVIEW_WARNING_CODE_CHARS or _CODE_RE.fullmatch(code) is None:
            raise ValueError("review warning code must be a bounded slug")
        message = self.message.strip() if isinstance(self.message, str) else ""
        if not message:
            raise ValueError("review warning message must not be empty")
        recovery = self.recovery.strip() if isinstance(self.recovery, str) else ""
        if not recovery:
            raise ValueError("review warning recovery instruction must not be empty")
        object.__setattr__(self, "code", code)
        object.__setattr__(
            self, "message", bounded_review_text(message, MAX_REVIEW_WARNING_MESSAGE_CHARS)
        )
        object.__setattr__(
            self, "recovery", bounded_review_text(recovery, MAX_REVIEW_WARNING_RECOVERY_CHARS)
        )


def make_review_warning(
    reason: ReviewWarningReason,
    *,
    message: str,
    recovery: str,
) -> ReviewWarning:
    """Build one bounded warning from a typed reason.

    Args:
        reason: Stable machine-readable slug for the warning.
        message: Short description of the problem.
        recovery: Human-readable recovery instruction.

    Returns:
        One bounded warning value.

    Raises:
        ValueError: If the reason is not a slug or the recovery text is empty.

    """
    return ReviewWarning(code=reason, message=message, recovery=recovery)

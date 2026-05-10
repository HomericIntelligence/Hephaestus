"""Shared Claude-CLI helpers: verdict parsing and rate-limit detection.

The original version of this module also implemented a Claude CLI invocation
helper with a sonnet→opus/haiku fallback chain. That role was superseded by
:mod:`hephaestus.automation.claude_models`, which assigns a fixed model per
phase and supports per-phase ``HEPH_<PHASE>_MODEL`` env-var overrides for
operator-driven tier swapping.

What remains here is:
- the verdict parser used by the strict review loops
- :func:`scan_quota_reset` — a shared cross-stream rate-limit scanner so all
  phases (planner, plan_reviewer, ...) get identical 429 handling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from hephaestus.github.rate_limit import detect_claude_usage_cap, detect_rate_limit


def scan_quota_reset(*texts: str) -> int | None:
    """Find a quota-reset epoch across one or more output streams.

    Inspects each text for either form of rate-limit message — the GitHub-CLI
    "Limit reached ..." form or the Claude-CLI "out of extra usage · resets
    ..." form. ``is not None`` chaining preserves an epoch of ``0`` (rate-
    limited, reset time unknown) instead of confusing it with "no rate limit".
    """
    for text in texts:
        for detect in (detect_rate_limit, detect_claude_usage_cap):
            epoch = detect(text)
            if epoch is not None:
                return epoch
    return None


@dataclass(frozen=True)
class ReviewVerdict:
    """Parsed verdict from a review response.

    Attributes:
        grade: Letter grade extracted from ``Grade: <X>`` line. ``None`` if absent.
        verdict: One of ``"GO"``, ``"NOGO"``, or ``"AMBIGUOUS"``.
        raw: Full review text (kept for downstream prompts and logs).

    """

    grade: str | None
    verdict: str
    raw: str

    @property
    def is_go(self) -> bool:
        """True only on an unambiguous GO."""
        return self.verdict == "GO"


_GRADE_RE = re.compile(
    r"^\s*\**\s*Grade\s*:\s*\**\s*([A-F][+-]?)(?![A-Za-z])",
    re.MULTILINE | re.IGNORECASE,
)
_VERDICT_RE = re.compile(
    r"^\s*\**\s*Verdict\s*:\s*\**\s*(GO|NO[\s-]?GO)\b", re.MULTILINE | re.IGNORECASE
)


def parse_review_verdict(text: str) -> ReviewVerdict:
    """Extract grade and Go/NoGo verdict from a review response.

    Looks for lines like:
        Grade: B+
        Verdict: GO     (or NOGO, NO-GO, NO GO)

    A response missing or contradicting these markers is treated as
    AMBIGUOUS — which the loop treats as NoGo (continue iterating).

    Args:
        text: The full review text from Claude.

    Returns:
        :class:`ReviewVerdict`.

    """
    grade_match = _GRADE_RE.search(text)
    grade = grade_match.group(1).upper() if grade_match else None

    verdict_match = _VERDICT_RE.search(text)
    if verdict_match:
        raw_verdict = re.sub(r"[\s-]", "", verdict_match.group(1).upper())
        verdict = "GO" if raw_verdict == "GO" else "NOGO"
    else:
        verdict = "AMBIGUOUS"

    return ReviewVerdict(grade=grade, verdict=verdict, raw=text)

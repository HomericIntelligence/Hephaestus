"""PR review-phase prompts.

Contains the PR review analysis prompt (inline-comment generator) and the
plain PR description template.
"""

from __future__ import annotations

import base64
import hashlib
import json

from ._review_rubric import get_pr_review_rubric
from ._shared import FencedContent, fence_content, get_terse_output_directive
from .catalog import PromptCatalog

#: Severities that BLOCK a GO when their automation thread is unresolved (#1856).
#: ``minor``/``nitpick`` are advisory — a clean audit must not
#: deadlock to state:skip over a nit it declined to block on. An unmarked or
#: unknown severity is treated as BLOCKING (fail-safe), which reproduces the
#: pre-#1856 all-blocking behavior when severity is not yet seeded.
BLOCKING_SEVERITIES: frozenset[str] = frozenset({"critical", "major"})
VALID_SEVERITIES: frozenset[str] = frozenset({"critical", "major", "minor", "nitpick"})

#: HTML-comment marker prepended to each posted review-thread body so the GO
#: gate can recover the reviewer's severity after the GitHub round-trip.
#: Anchored as a full marker line — never matched by a free substring scan.
SEVERITY_MARKER_PREFIX = "<!-- hephaestus-severity:"

#: Maximum size of a fully rendered direct PR-review prompt. The provider
#: request also contains agent and repository instructions. Keep sufficient
#: space below the provider's 1 MiB request limit.
MAX_PR_REVIEW_RENDERED_CHARS = 350_000

_MAX_PR_REVIEW_ISSUE_BODY_CHARS = 30_000
_MAX_PR_REVIEW_DESCRIPTION_CHARS = 20_000
_MAX_PR_REVIEW_ADVISE_CHARS = 20_000
_MAX_PR_REVIEW_RECEIPTS_CHARS = 64_000
_MAX_HOST_RECEIPT_STREAM_CHARS = 512
_MAX_REVIEW_VALIDATION_COMMENTS_CHARS = 40_000
_MAX_REVIEW_VALIDATION_TITLE_CHARS = 4_000

_HOST_RECEIPT_IDENTITY_FIELDS = (
    "argv",
    "head_sha",
    "immutable_source",
    "ok",
    "status",
    "platform",
    "failure_kind",
)
_HOST_RECEIPT_SUMMARY_POLICY = "host-receipt-identities-v1"
_HOST_RECEIPT_DIGEST_SUMMARY_POLICY = "host-receipt-digests-v1"
_HOST_RECEIPT_AGGREGATE_SUMMARY_POLICY = "host-receipt-aggregate-v1"
_HOST_RECEIPT_TRUNCATION_MARKER = "[... host verification receipts truncated ...]"
_PROMPT_LIMIT_ERROR = (
    "pr_review_prompt_limit_exceeded: required prompt content exceeds "
    f"{MAX_PR_REVIEW_RENDERED_CHARS} characters"
)


class PrReviewPromptSizeError(RuntimeError):
    """Report that required PR-review content cannot fit the prompt limit."""


def _truncate_review_text(text: str, *, max_chars: int, label: str) -> str:
    """Keep both ends of oversized review context with a clear marker."""
    if len(text) <= max_chars:
        return text
    marker = f"\n\n[... {label} truncated ...]\n\n"
    available = max_chars - len(marker)
    if available <= 0:
        return marker[:max_chars]
    prefix_chars = (available * 3) // 4
    suffix_chars = available - prefix_chars
    return f"{text[:prefix_chars]}{marker}{text[-suffix_chars:]}"


def _summary_metadata(policy: str, receipt_count: int) -> dict[str, object]:
    """Return the common explicit metadata for a receipt summary."""
    return {
        "summary_policy": policy,
        "truncated": True,
        "truncation_marker": _HOST_RECEIPT_TRUNCATION_MARKER,
        "receipt_count": receipt_count,
        "identity_fields": list(_HOST_RECEIPT_IDENTITY_FIELDS),
    }


def _compact_host_verifications_json(host_verifications_json: str) -> str:
    """Keep valid, bounded receipt evidence and an explicit summary policy."""
    if not host_verifications_json:
        return "[]"
    try:
        parsed = json.loads(host_verifications_json)
    except (TypeError, json.JSONDecodeError):
        return json.dumps(
            {
                "summary_policy": "host-receipt-invalid-json-v1",
                "truncated": True,
                "truncation_marker": _HOST_RECEIPT_TRUNCATION_MARKER,
                "source_length": len(host_verifications_json),
                "source_sha256": hashlib.sha256(
                    host_verifications_json.encode("utf-8")
                ).hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    if not isinstance(parsed, list):
        return json.dumps(
            {
                "summary_policy": "host-receipt-invalid-shape-v1",
                "truncated": True,
                "truncation_marker": _HOST_RECEIPT_TRUNCATION_MARKER,
                "source_sha256": hashlib.sha256(
                    json.dumps(parsed, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    compacted: list[object] = []
    for receipt in parsed:
        if not isinstance(receipt, dict):
            compacted.append(receipt)
            continue
        compact = dict(receipt)
        for key in ("stdout_tail", "stderr_tail"):
            value = compact.get(key)
            if isinstance(value, str):
                compact[key] = _truncate_review_text(
                    value,
                    max_chars=_MAX_HOST_RECEIPT_STREAM_CHARS,
                    label="host verification output",
                )
        compacted.append(compact)
    serialized = json.dumps(compacted, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(serialized) <= _MAX_PR_REVIEW_RECEIPTS_CHARS:
        return serialized

    identity_records = [
        (
            [receipt.get(field) for field in _HOST_RECEIPT_IDENTITY_FIELDS]
            if isinstance(receipt, dict)
            else [receipt, *([None] * (len(_HOST_RECEIPT_IDENTITY_FIELDS) - 1))]
        )
        for receipt in compacted
    ]
    identity_summary = json.dumps(
        {
            **_summary_metadata(_HOST_RECEIPT_SUMMARY_POLICY, len(compacted)),
            "receipts": identity_records,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(identity_summary) <= _MAX_PR_REVIEW_RECEIPTS_CHARS:
        return identity_summary

    receipt_digests = [
        base64.urlsafe_b64encode(
            hashlib.sha256(
                json.dumps(
                    identity,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).digest()
        )
        .decode("ascii")
        .rstrip("=")
        for identity in identity_records
    ]
    digest_summary = json.dumps(
        {
            **_summary_metadata(_HOST_RECEIPT_DIGEST_SUMMARY_POLICY, len(compacted)),
            "receipt_digests": receipt_digests,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(digest_summary) <= _MAX_PR_REVIEW_RECEIPTS_CHARS:
        return digest_summary

    return json.dumps(
        {
            **_summary_metadata(_HOST_RECEIPT_AGGREGATE_SUMMARY_POLICY, len(compacted)),
            "identity_sha256": hashlib.sha256(
                json.dumps(
                    identity_records,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _truncate_thread_text(value: object, *, max_chars: int) -> object:
    """Bound thread prose without changing thread identity or JSON structure."""
    if isinstance(value, list):
        return [_truncate_thread_text(item, max_chars=max_chars) for item in value]
    if not isinstance(value, dict):
        return value
    bounded: dict[str, object] = {}
    for key, item in value.items():
        if key in {"body", "implementation_reply_body"} and isinstance(item, str):
            bounded[key] = _truncate_review_text(
                item,
                max_chars=max_chars,
                label="review comment body",
            )
        else:
            bounded[key] = _truncate_thread_text(item, max_chars=max_chars)
    return bounded


def _compact_prior_comments_json(prior_comments_json: str) -> str:
    """Keep every prior thread in valid JSON while bounding its prose."""
    try:
        parsed = json.loads(prior_comments_json)
    except (TypeError, json.JSONDecodeError) as error:
        raise PrReviewPromptSizeError(_PROMPT_LIMIT_ERROR) from error
    if not isinstance(parsed, list):
        raise PrReviewPromptSizeError(_PROMPT_LIMIT_ERROR)
    for max_chars in (2_048, 1_024, 512, 256, 128, 64):
        compacted = json.dumps(
            _truncate_thread_text(parsed, max_chars=max_chars),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(compacted) <= _MAX_REVIEW_VALIDATION_COMMENTS_CHARS:
            return compacted
    raise PrReviewPromptSizeError(_PROMPT_LIMIT_ERROR)


def _budget_review_diff(diff_text: str, *, max_chars: int) -> str:
    """Keep an exact-size head-and-tail sample of an oversized review diff."""
    return _truncate_review_text(diff_text, max_chars=max_chars, label="PR diff")


def get_pr_review_analysis_prompt(
    pr_number: int,
    issue_number: int,
    pr_diff: str = "",
    issue_body: str = "",
    pr_description: str = "",
    advise_findings: str = "",
    host_verifications_json: str = "",
    include_nitpicks: bool = False,
    review_context_kind: str = "issue",
) -> str:
    """Get the `$athena:pr-review` analysis prompt for inline review comments.

    All free-text fields are fenced as untrusted (see module docstring).

    This is the loop's only automated review gate. When the Athena skill is
    available, the prompt directs the reviewer to run its default profile;
    otherwise the inline-review contract below is the fallback.

    Args:
        pr_number: GitHub PR number
        issue_number: Linked GitHub issue number
        pr_diff: PR diff output
        issue_body: Issue body/description
        pr_description: PR description body
        advise_findings: Prior team learnings from Mnemosyne to give the
            reviewer continuity with the advise-first implementation turn.
        host_verifications_json: Host-captured output from every fixed,
            repository-owned validation command bound to the reviewed head.
        include_nitpicks: When False (default), the reviewer is told to OMIT
            ``nitpick``-severity comments entirely. When True (``--nitpick``),
            nitpick comments are re-enabled. Either way every emitted comment
            carries a ``severity`` tag (#1083).
        review_context_kind: Human-readable numeric context kind for the
            prompt header. Pipeline reviews use the default ``"issue"``;
            callers with an independently verified alternate context may
            provide another label.

    Returns:
        Formatted PR review analysis prompt

    """
    return _render_pr_review_analysis_prompt(
        pr_number=pr_number,
        issue_number=issue_number,
        pr_diff=pr_diff,
        issue_body=issue_body,
        pr_description=pr_description,
        advise_findings=advise_findings,
        host_verifications_json=host_verifications_json,
        include_nitpicks=include_nitpicks,
        review_context_kind=review_context_kind,
        fenced=fence_content(),
    )


def _render_pr_review_analysis_prompt(
    *,
    pr_number: int,
    issue_number: int,
    pr_diff: str,
    issue_body: str,
    pr_description: str,
    advise_findings: str,
    host_verifications_json: str,
    include_nitpicks: bool,
    review_context_kind: str,
    fenced: FencedContent,
) -> str:
    """Render an analysis prompt with one caller-owned fence nonce."""
    nitpick_template = (
        "pr_review/nitpick_include.j2" if include_nitpicks else "pr_review/nitpick_suppress.j2"
    )
    nitpick_directive = PromptCatalog.current().render(nitpick_template).strip()
    return PromptCatalog.current().render(
        "pr_review/analysis.j2",
        pr_number=pr_number,
        issue_number=issue_number,
        review_context_kind=review_context_kind,
        pr_diff_block=fenced.fence("PR_DIFF", pr_diff),
        issue_body_block=fenced.fence("ISSUE_BODY", issue_body),
        advise_findings_block=fenced.fence(
            "ADVISE_FINDINGS",
            advise_findings or "_(no prior advise findings supplied)_",
        ),
        host_verifications_block=fenced.fence(
            "HOST_VERIFICATIONS",
            host_verifications_json or "[]",
        ),
        pr_description_block=fenced.fence("PR_DESCRIPTION", pr_description),
        untrusted_notice=fenced.untrusted_notice,
        review_rubric=get_pr_review_rubric().strip(),
        nitpick_directive=nitpick_directive,
        terse_output_directive=get_terse_output_directive(
            terminal_output_contract=(
                "End with exactly one fenced structural review-audit JSON object "
                "that includes a typed verdict field (`GO`, `NOGO`, or `BLOCKED`); "
                "missing or malformed verdicts are invalid. Do not emit a Verdict "
                "line or any other textual decision token."
            )
        ),
    )


def build_bounded_pr_review_analysis_prompt(
    pr_number: int,
    issue_number: int,
    pr_diff: str = "",
    issue_body: str = "",
    pr_description: str = "",
    advise_findings: str = "",
    host_verifications_json: str = "",
    include_nitpicks: bool = False,
    review_context_kind: str = "issue",
) -> str:
    """Render a direct analysis prompt within the provider-safe limit."""
    fenced = fence_content()

    def render(
        *,
        diff: str,
        issue: str,
        description: str,
        advise: str,
        receipts: str,
    ) -> str:
        return _render_pr_review_analysis_prompt(
            pr_number=pr_number,
            issue_number=issue_number,
            pr_diff=diff,
            issue_body=issue,
            pr_description=description,
            advise_findings=advise,
            host_verifications_json=receipts,
            include_nitpicks=include_nitpicks,
            review_context_kind=review_context_kind,
            fenced=fenced,
        )

    prompt = render(
        diff=pr_diff,
        issue=issue_body,
        description=pr_description,
        advise=advise_findings,
        receipts=host_verifications_json,
    )
    if len(prompt) <= MAX_PR_REVIEW_RENDERED_CHARS:
        return prompt

    bounded_context = {
        "issue": _truncate_review_text(
            issue_body,
            max_chars=_MAX_PR_REVIEW_ISSUE_BODY_CHARS,
            label="issue body",
        ),
        "description": _truncate_review_text(
            pr_description,
            max_chars=_MAX_PR_REVIEW_DESCRIPTION_CHARS,
            label="PR description",
        ),
        "advise": _truncate_review_text(
            advise_findings,
            max_chars=_MAX_PR_REVIEW_ADVISE_CHARS,
            label="advise findings",
        ),
        "receipts": _compact_host_verifications_json(host_verifications_json),
    }
    fixed_prompt = render(diff="", **bounded_context)
    remaining = MAX_PR_REVIEW_RENDERED_CHARS - len(fixed_prompt)
    if remaining < 0:
        raise PrReviewPromptSizeError(_PROMPT_LIMIT_ERROR)
    bounded_diff = _budget_review_diff(pr_diff, max_chars=remaining)
    prompt = render(diff=bounded_diff, **bounded_context)
    if len(prompt) > MAX_PR_REVIEW_RENDERED_CHARS:
        raise PrReviewPromptSizeError(_PROMPT_LIMIT_ERROR)
    return prompt


def get_review_validation_prompt(
    pr_number: int,
    issue_number: int,
    prior_comments_json: str,
    diff_text: str = "",
    host_verifications_json: str = "",
    pr_title: str = "",
    pr_description: str = "",
    review_context_kind: str = "issue",
) -> str:
    """Get the prompt that validates whether prior review comments were addressed.

    Used by the pipeline PR-review stage for a fresh comment-review of the
    current change, prior review, and implementation reply. It verifies that
    the implementer responded to each thread with coherent, diff-consistent
    evidence; the original implementation review remains the thorough
    correctness review. It keeps an incomplete
    original thread open and supplies a concrete reviewer reply describing
    what remains; it never creates a replacement inline thread.

    Both inputs are fenced as untrusted (prior comment bodies + the diff are
    GitHub-sourced).

    Args:
        pr_number: GitHub PR number under validation.
        issue_number: Linked GitHub issue number.
        prior_comments_json: JSON array string of prior comment dicts
            (``path``/``line``/``body``).
        diff_text: The current cumulative PR diff.
        host_verifications_json: Host-captured output from every fixed,
            repository-owned validation command bound to the reviewed head.
        pr_title: Current GitHub PR title captured with the reviewed head.
        pr_description: Current GitHub PR body captured with the reviewed head.
        review_context_kind: Human-readable numeric context kind for the
            prompt header (defaults to ``"issue"``).

    Returns:
        Formatted review-validation prompt.

    """
    return _render_review_validation_prompt(
        pr_number=pr_number,
        issue_number=issue_number,
        prior_comments_json=prior_comments_json,
        diff_text=diff_text,
        host_verifications_json=host_verifications_json,
        pr_title=pr_title,
        pr_description=pr_description,
        review_context_kind=review_context_kind,
        fenced=fence_content(),
    )


def _render_review_validation_prompt(
    *,
    pr_number: int,
    issue_number: int,
    prior_comments_json: str,
    diff_text: str,
    host_verifications_json: str,
    pr_title: str,
    pr_description: str,
    review_context_kind: str,
    fenced: FencedContent,
) -> str:
    """Render a validation prompt with one caller-owned fence nonce."""
    return PromptCatalog.current().render(
        "pr_review/validation.j2",
        pr_number=pr_number,
        issue_number=issue_number,
        review_context_kind=review_context_kind,
        prior_comments_block=fenced.fence("PRIOR_COMMENTS", prior_comments_json),
        diff_block=fenced.fence("DIFF", diff_text),
        host_verifications_block=fenced.fence(
            "HOST_VERIFICATIONS",
            host_verifications_json or "[]",
        ),
        pr_title_block=fenced.fence("PR_TITLE", pr_title),
        pr_description_block=fenced.fence("PR_DESCRIPTION", pr_description),
        untrusted_notice=fenced.untrusted_notice,
        terse_output_directive=get_terse_output_directive(),
    )


def build_bounded_review_validation_prompt(
    pr_number: int,
    issue_number: int,
    prior_comments_json: str,
    diff_text: str = "",
    host_verifications_json: str = "",
    pr_title: str = "",
    pr_description: str = "",
    review_context_kind: str = "issue",
) -> str:
    """Render a validation prompt within the provider-safe limit."""
    fenced = fence_content()

    def render(*, diff: str, context: dict[str, str]) -> str:
        return _render_review_validation_prompt(
            pr_number=pr_number,
            issue_number=issue_number,
            prior_comments_json=context["prior_comments_json"],
            diff_text=diff,
            host_verifications_json=context["host_verifications_json"],
            pr_title=context["pr_title"],
            pr_description=context["pr_description"],
            review_context_kind=review_context_kind,
            fenced=fenced,
        )

    original_context = {
        "prior_comments_json": prior_comments_json,
        "host_verifications_json": host_verifications_json,
        "pr_title": pr_title,
        "pr_description": pr_description,
    }
    prompt = render(diff=diff_text, context=original_context)
    if len(prompt) <= MAX_PR_REVIEW_RENDERED_CHARS:
        return prompt

    bounded_context = {
        "prior_comments_json": _compact_prior_comments_json(prior_comments_json),
        "host_verifications_json": _compact_host_verifications_json(host_verifications_json),
        "pr_title": _truncate_review_text(
            pr_title,
            max_chars=_MAX_REVIEW_VALIDATION_TITLE_CHARS,
            label="PR title",
        ),
        "pr_description": _truncate_review_text(
            pr_description,
            max_chars=_MAX_PR_REVIEW_DESCRIPTION_CHARS,
            label="PR description",
        ),
    }
    fixed_prompt = render(diff="", context=bounded_context)
    remaining = MAX_PR_REVIEW_RENDERED_CHARS - len(fixed_prompt)
    if remaining < 0:
        raise PrReviewPromptSizeError(_PROMPT_LIMIT_ERROR)
    bounded_diff = _budget_review_diff(diff_text, max_chars=remaining)
    prompt = render(diff=bounded_diff, context=bounded_context)
    if len(prompt) > MAX_PR_REVIEW_RENDERED_CHARS:
        raise PrReviewPromptSizeError(_PROMPT_LIMIT_ERROR)
    return prompt


def get_comment_difficulty_prompt(
    issue_number: int,
    comments_json: str,
    review_context_kind: str = "issue",
) -> str:
    """Get the prompt that classifies review-comment fix difficulty (#1083).

    Used by :mod:`hephaestus.automation.comment_difficulty` to label each
    unresolved comment ``simple`` / ``medium`` / ``hard`` so the per-comment fix
    sub-agent runs at the matching model tier. The comment bodies are fenced as
    untrusted (GitHub-sourced).

    Args:
        issue_number: Linked GitHub issue number (for log/context only).
        comments_json: JSON array string of comment dicts
            (``thread_id``/``path``/``line``/``body``).
        review_context_kind: Human-readable numeric context kind for the
            prompt header (defaults to ``"issue"``).

    Returns:
        Formatted comment-difficulty classification prompt.

    """
    fenced = fence_content()
    return PromptCatalog.current().render(
        "pr_review/comment_difficulty.j2",
        issue_number=issue_number,
        review_context_kind=review_context_kind,
        comments_block=fenced.fence("REVIEW_COMMENTS", comments_json),
        untrusted_notice=fenced.untrusted_notice,
        terse_output_directive=get_terse_output_directive(),
    )


def get_pr_description(
    issue_number: int,
    summary: str,
    changes: str,
    testing: str,
    generated_by: str = "Hephaestus automation",
) -> str:
    """Generate a PR description.

    Args:
        issue_number: GitHub issue number
        summary: Brief summary of changes
        changes: Detailed list of changes
        testing: Testing information
        generated_by: Short description of the tool/agent that generated the PR

    Returns:
        Formatted PR description

    """
    return PromptCatalog.current().render(
        "pr_review/description.j2",
        issue_number=issue_number,
        summary=summary,
        changes=changes,
        testing=testing,
        generated_by=generated_by,
    )

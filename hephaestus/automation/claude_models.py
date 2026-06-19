"""Claude model selection per automation phase.

Each Claude automation phase calls the ``claude`` CLI with ``--model <id>`` so
the chosen model is pinned regardless of the user's CLI default. The mapping
reflects the cost/quality tradeoff for each phase:

- Planning needs reasoning quality but few tokens overall → Opus
- Implementation is a long mechanical tool-use loop → Haiku
- Reviewers / advise / learn → Sonnet (middle ground)
- Git/PR message writing is tiny metadata generation → Haiku

Each function honors a ``HEPH_<PHASE>_MODEL`` environment variable so an
operator can override without code changes (e.g. when one tier's quota is
exhausted).  Unknown overrides emit a **warning** but are still accepted so
operators can experiment with preview models without a code change.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

OPUS = "claude-opus-4-7"
SONNET = "claude-sonnet-4-6"
HAIKU = "claude-haiku-4-5"
CODEX_ADVISE = "gpt-5.4-mini"

# Newer tiers that are valid model IDs but not the per-phase defaults. Listed in
# the known set so pinning them via HEPH_*_MODEL doesn't emit a spurious
# "Unknown model" warning. (Fable sits above Opus; 4.8 is the current Opus.)
OPUS_48 = "claude-opus-4-8"
FABLE = "claude-fable-5"

# The set of model IDs the automation suite recognizes. Overrides to values
# outside this set are still accepted (operators may have preview access) but
# trigger a one-time warning so misconfigured/typo'd env vars are visible.
_KNOWN_MODELS: frozenset[str] = frozenset({OPUS, SONNET, HAIKU, OPUS_48, FABLE})


def _resolve_model(env_var: str, default: str) -> str:
    """Return the model ID for *env_var*, warning if the value is unknown.

    Args:
        env_var: Name of the environment variable to check.
        default: Default model ID to use when the variable is unset.

    Returns:
        The resolved model ID string.

    """
    value = os.environ.get(env_var)
    if value is None:
        return default
    if value not in _KNOWN_MODELS:
        logger.warning(
            "Unknown model %r set in %s (known: %s). "
            "Proceeding, but verify the model ID is correct.",
            value,
            env_var,
            ", ".join(sorted(_KNOWN_MODELS)),
        )
    return value


def planner_model() -> str:
    """Model used to generate implementation plans from issue text."""
    return _resolve_model("HEPH_PLANNER_MODEL", OPUS)


def implementer_model() -> str:
    """Model used by the implementer worker that runs ``claude`` in a worktree.

    Also used for any phase that resumes the implementer's session
    (e.g. address-review, ci-driver), since ``claude --resume`` is locked
    to the model that created the session.
    """
    return _resolve_model("HEPH_IMPLEMENTER_MODEL", HAIKU)


def reviewer_model() -> str:
    """Model used by plan/PR reviewers and the review-fix loop."""
    return _resolve_model("HEPH_REVIEWER_MODEL", SONNET)


def advise_model() -> str:
    """Claude model used by the advise skill-selection step."""
    return _resolve_model("HEPH_ADVISE_MODEL", HAIKU)


def codex_advise_model() -> str:
    """Codex model used by the advise skill-selection step."""
    return CODEX_ADVISE


def learn_model() -> str:
    """Model used by /learn and follow-up issue filing."""
    return _resolve_model("HEPH_LEARN_MODEL", HAIKU)


def git_message_model() -> str:
    """Model used by the lightweight commit/PR message writer."""
    return _resolve_model("HEPH_GIT_MESSAGE_MODEL", HAIKU)

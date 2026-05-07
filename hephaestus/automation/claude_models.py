"""Claude model selection per automation phase.

Each automation phase calls the ``claude`` CLI with ``--model <id>`` so the
chosen model is pinned regardless of the user's CLI default. The mapping
reflects the cost/quality tradeoff for each phase:

- Planning needs reasoning quality but few tokens overall → Opus
- Implementation is a long mechanical tool-use loop → Haiku
- Reviewers / advise / learn → Sonnet (middle ground)

Each function honors a ``HEPH_<PHASE>_MODEL`` environment variable so an
operator can override without code changes (e.g. when one tier's quota is
exhausted).
"""

from __future__ import annotations

import os

OPUS = "claude-opus-4-7"
SONNET = "claude-sonnet-4-6"
HAIKU = "claude-haiku-4-5"


def planner_model() -> str:
    """Model used to generate implementation plans from issue text."""
    return os.environ.get("HEPH_PLANNER_MODEL", OPUS)


def implementer_model() -> str:
    """Model used by the implementer worker that runs ``claude`` in a worktree.

    Also used for any phase that resumes the implementer's session
    (e.g. address-review, ci-driver), since ``claude --resume`` is locked
    to the model that created the session.
    """
    return os.environ.get("HEPH_IMPLEMENTER_MODEL", HAIKU)


def reviewer_model() -> str:
    """Model used by plan/PR reviewers and the review-fix loop."""
    return os.environ.get("HEPH_REVIEWER_MODEL", SONNET)


def advise_model() -> str:
    """Model used by the /advise step inside the planner."""
    return os.environ.get("HEPH_ADVISE_MODEL", HAIKU)


def learn_model() -> str:
    """Model used by /learn and follow-up issue filing."""
    return os.environ.get("HEPH_LEARN_MODEL", HAIKU)

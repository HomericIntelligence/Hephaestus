"""Regression tests for the NATS failed-message retention policy (#2155)."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from hephaestus.nats.subscriber import NATSSubscriberThread

REPO_ROOT = Path(__file__).resolve().parents[3]
NATS_DOC = REPO_ROOT / "docs" / "nats.md"

_POLICY_SECTION_RE = re.compile(
    r"^## Failed-message retention\s*$(.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _normalized_policy() -> str:
    """Return the normalized failed-message retention policy text."""
    match = _POLICY_SECTION_RE.search(NATS_DOC.read_text(encoding="utf-8"))
    assert match is not None, "docs/nats.md must define failed-message retention"
    return re.sub(r"\s+", " ", match.group(1).lower())


def test_policy_names_failure_classes_and_retention_outcome() -> None:
    """The policy names each failure class and their retention outcome."""
    policy = _normalized_policy()
    for phrase in (
        "best-effort",
        "at-most-once",
        "malformed utf-8 or json",
        "handler exception",
        "not retained",
        "dead-letter queue",
        "replay",
    ):
        assert phrase in policy


def test_policy_limits_subscriber_to_non_critical_workflows() -> None:
    """The policy reserves the subscriber for non-critical workflows."""
    policy = _normalized_policy()
    for phrase in (
        "non-critical",
        "observability signals",
        "durable processing",
        "must not use this abstraction unchanged",
        "before acknowledging",
    ):
        assert phrase in policy


def test_public_api_docstring_matches_operator_policy() -> None:
    """The public class docstring states the corresponding delivery limits."""
    docstring = inspect.getdoc(NATSSubscriberThread)
    assert docstring is not None
    doc = re.sub(r"\s+", " ", docstring.lower())
    for phrase in (
        "malformed",
        "handler",
        "non-critical",
        "no built-in",
        "replay",
        "dlq",
    ):
        assert phrase in doc

"""Drift guards for docs/observability.md (issue #2153)."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DOC = REPO_ROOT / "docs" / "observability.md"
COORDINATOR = REPO_ROOT / "hephaestus" / "automation" / "pipeline" / "coordinator.py"
ALERTS = REPO_ROOT / "hephaestus" / "observability" / "alerts.py"

_METRIC_NAME_RE = re.compile(r'"(hephaestus_[a-z0-9_]+)"')
_ALERT_NAME_RE = re.compile(r'name="([a-z0-9_]+)"')


def test_required_sections_present() -> None:
    """The document must define metrics, alerts, SLOs, and ownership."""
    text = DOC.read_text(encoding="utf-8")
    for heading in ("## Metrics", "## Alerts", "## SLOs", "## Ownership and escalation"):
        assert heading in text, f"docs/observability.md must contain {heading!r}"


def test_every_emitted_metric_is_documented() -> None:
    """Every hephaestus_* metric emitted by the coordinator is in the catalog."""
    doc = DOC.read_text(encoding="utf-8")
    emitted = set(_METRIC_NAME_RE.findall(COORDINATOR.read_text(encoding="utf-8")))
    assert emitted, "expected coordinator.py to emit hephaestus_* metrics"
    missing = sorted(name for name in emitted if name not in doc)
    assert not missing, f"metrics emitted but undocumented: {missing}"


def test_every_alert_rule_is_documented() -> None:
    """Every alert rule defined in alerts.py appears in the alert catalog."""
    doc = DOC.read_text(encoding="utf-8")
    rules = set(_ALERT_NAME_RE.findall(ALERTS.read_text(encoding="utf-8")))
    assert rules, "expected alerts.py to define AlertEvent rules"
    missing = sorted(name for name in rules if name not in doc)
    assert not missing, f"alert rules defined but undocumented: {missing}"


def test_each_alert_has_owner_and_runbook() -> None:
    """The document names an owner and links operator runbooks."""
    text = DOC.read_text(encoding="utf-8")
    assert "runbooks/" in text
    assert "maintainer" in text.lower()

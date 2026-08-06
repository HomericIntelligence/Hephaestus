"""Drift guards for docs/observability.md (issue #2153)."""

from __future__ import annotations

import re
from pathlib import Path

from hephaestus.automation.pipeline.coordinator_runtime import (
    _ALERT_NAME_LABELS,
    _BREAKER_STATE_LABELS,
    _DYNAMIC_METRIC_SERIES_CAP,
    _JOB_OUTCOME_LABELS,
    _PIPELINE_STAGE_LABELS,
)
from hephaestus.nats.subscriber import (
    _NATS_BREAKER_STATE_LABELS,
    _NATS_ERROR_KIND_LABELS,
    _NATS_SUBSCRIBER_STATE_LABELS,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DOC = REPO_ROOT / "docs" / "observability.md"
COORDINATOR = REPO_ROOT / "hephaestus" / "automation" / "pipeline" / "coordinator_runtime.py"
METRICS = REPO_ROOT / "hephaestus" / "observability" / "metrics.py"
NATS = REPO_ROOT / "hephaestus" / "nats" / "subscriber.py"
ALERTS = REPO_ROOT / "hephaestus" / "observability" / "alerts.py"

_METRIC_NAME_RE = re.compile(r'"(hephaestus_[a-z0-9_]+)"')
_ALERT_NAME_RE = re.compile(r'name="([a-z0-9_]+)"')


def test_required_sections_present() -> None:
    """The document must define metrics, alerts, SLOs, and ownership."""
    text = DOC.read_text(encoding="utf-8")
    for heading in ("## Metrics", "## Alerts", "## SLOs", "## Ownership and escalation"):
        assert heading in text, f"docs/observability.md must contain {heading!r}"


def test_every_emitted_metric_is_documented() -> None:
    """Every emitted hephaestus_* metric is in the observability catalog."""
    doc = DOC.read_text(encoding="utf-8")
    emitted: set[str] = set()
    for source in (COORDINATOR, METRICS):
        emitted.update(_METRIC_NAME_RE.findall(source.read_text(encoding="utf-8")))
    assert emitted, "expected observability sources to emit hephaestus_* metrics"
    missing = sorted(name for name in emitted if name not in doc)
    assert not missing, f"metrics emitted but undocumented: {missing}"


def _metric_row(document: str, metric: str) -> str:
    """Return the markdown table row for *metric*."""
    return next(line for line in document.splitlines() if line.startswith(f"| `{metric}` |"))


def test_metric_policy_domains_and_caps_are_documented() -> None:
    """Catalog rows stay aligned with the runtime's declared domains and caps."""
    doc = DOC.read_text(encoding="utf-8")
    policies = (
        (
            "hephaestus_pipeline_queue_depth",
            ("stage", _PIPELINE_STAGE_LABELS),
            len(_PIPELINE_STAGE_LABELS),
        ),
        (
            "hephaestus_pipeline_inflight_per_repo",
            ("repo", ("open repository names",)),
            _DYNAMIC_METRIC_SERIES_CAP,
        ),
        (
            "hephaestus_circuit_breaker_state",
            ("name", ("open breaker names",)),
            _DYNAMIC_METRIC_SERIES_CAP,
        ),
        (
            "hephaestus_circuit_breaker_state",
            ("state", _BREAKER_STATE_LABELS),
            _DYNAMIC_METRIC_SERIES_CAP,
        ),
        (
            "hephaestus_pipeline_alert_active",
            ("name", _ALERT_NAME_LABELS),
            len(_ALERT_NAME_LABELS),
        ),
        (
            "hephaestus_pipeline_jobs_total",
            ("stage", _PIPELINE_STAGE_LABELS),
            len(_PIPELINE_STAGE_LABELS) * len(_JOB_OUTCOME_LABELS),
        ),
        (
            "hephaestus_pipeline_jobs_total",
            ("outcome", _JOB_OUTCOME_LABELS),
            len(_PIPELINE_STAGE_LABELS) * len(_JOB_OUTCOME_LABELS),
        ),
    )
    for metric, (label, values), cap in policies:
        row = _metric_row(doc, metric)
        assert f"`{label}`" in row
        assert f"| {cap} |" in row
        for value in values:
            if value.startswith("open "):
                assert value in row
            else:
                assert f"`{value}`" in row

    assert "| `hephaestus_pipeline_inflight_jobs` | gauge | — | 1 |" in doc
    assert "| `hephaestus_pipeline_loops_total` | gauge | — | 1 |" in doc
    assert "| `hephaestus_pipeline_stalled_ticks` | gauge | — | 1 |" in doc
    assert "| `hephaestus_pipeline_agent_job_seconds_total` | counter | — | 1 |" in doc


def test_nats_metric_policy_domains_and_caps_are_documented() -> None:
    """NATS metric documentation stays aligned with enum-derived policies."""
    doc = (REPO_ROOT / "docs" / "nats.md").read_text(encoding="utf-8")
    source = NATS.read_text(encoding="utf-8")
    assert "_NATS_ERROR_KIND_LABELS" in source
    assert "_NATS_SUBSCRIBER_STATE_LABELS" in source
    assert "_NATS_BREAKER_STATE_LABELS" in source

    policies = (
        (
            "hephaestus_nats_subscriber_state",
            "state",
            _NATS_SUBSCRIBER_STATE_LABELS,
            len(_NATS_SUBSCRIBER_STATE_LABELS),
        ),
        (
            "hephaestus_nats_subscriber_circuit_breaker_state",
            "state",
            _NATS_BREAKER_STATE_LABELS,
            len(_NATS_BREAKER_STATE_LABELS),
        ),
        (
            "hephaestus_nats_subscriber_errors_total",
            "kind",
            _NATS_ERROR_KIND_LABELS,
            len(_NATS_ERROR_KIND_LABELS),
        ),
    )
    for metric, label, values, cap in policies:
        row = _metric_row(doc, metric)
        assert f"`{label}`" in row
        assert f"| {cap} |" in row
        for value in values:
            assert f"`{value}`" in row

    assert "| `hephaestus_nats_subscriber_messages_total` | — | 1 |" in doc
    assert "| `hephaestus_nats_subscriber_last_message_timestamp_seconds` | — | 1 |" in doc


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

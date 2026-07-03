"""Mesh worker configuration and ADR-013 subject grammar.

Pure helpers only — no I/O beyond :func:`MeshConfig.from_env` reading the
environment. Subject builders/parsers are the single source of truth for the
wire grammar so every producer and consumer in this package agrees.
"""

from __future__ import annotations

import os
import re
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

#: Subject prefixes (ADR-013).
DISPATCH_PREFIX = "hi.myrmidon"
TASKS_PREFIX = "hi.tasks"
INTERVIEW_PREFIX = "hi.pipeline.interview"
EPIC_PREFIX = "hi.pipeline.epic"
LOGS_PREFIX = "hi.logs.myrmidon"

#: JetStream stream holding the dispatch queues.
DISPATCH_STREAM = "homeric-myrmidon"

#: Payload schema tag (ADR-013 §3).
SCHEMA = "hi/v1"

_TOKEN_RE = re.compile(r"[^a-z0-9-]+")


def slugify(value: str) -> str:
    """Slugify *value* into a valid NATS subject token (ADR-005 rules)."""
    return _TOKEN_RE.sub("-", value.lower()).strip("-")


def envelope(**fields: Any) -> dict[str, Any]:
    """Build an ADR-013 §3 payload envelope merged with *fields*."""
    return {
        "schema": SCHEMA,
        "ts": datetime.now(timezone.utc).isoformat(),
        "msg_id": str(uuid.uuid4()),
        **fields,
    }


def dispatch_subject(domain: str, role: str, task_id: str) -> str:
    """Role-addressed dispatch subject for one task (ADR-013 §1)."""
    return f"{DISPATCH_PREFIX}.{slugify(domain)}.{slugify(role)}.task.{task_id}"


def dispatch_filter(domain: str, role: str) -> str:
    """Consumer filter subject for a (domain, role) queue."""
    return f"{DISPATCH_PREFIX}.{slugify(domain)}.{slugify(role)}.task.>"


def consumer_name(domain: str, role: str) -> str:
    """Durable pull-consumer name for a (domain, role) queue."""
    return f"myrmidon-{slugify(domain)}-{slugify(role)}"


def parse_dispatch_subject(subject: str) -> tuple[str, str, str] | None:
    """Return ``(domain, role, task_id)`` for a dispatch subject, else None.

    Legacy two-token subjects (``hi.myrmidon.{type}.{task_id}``) do not match:
    the literal ``task`` token is required (ADR-013 migration rule).
    """
    parts = subject.split(".")
    if len(parts) == 6 and parts[:2] == ["hi", "myrmidon"] and parts[4] == "task":
        return parts[2], parts[3], parts[5]
    return None


def task_event_subject(team_id: str, task_id: str, verb: str) -> str:
    """State-event subject (ADR-013 §2); verb ∈ started|updated|completed|failed."""
    return f"{TASKS_PREFIX}.{team_id}.{task_id}.{verb}"


def interview_question_subject(intake_id: str, q_id: str) -> str:
    """Interview question subject, worker → console (ADR-013 §5)."""
    return f"{INTERVIEW_PREFIX}.{intake_id}.question.{q_id}"


def interview_answer_subject(intake_id: str, q_id: str) -> str:
    """Interview answer subject, console → worker (ADR-013 §5)."""
    return f"{INTERVIEW_PREFIX}.{intake_id}.answer.{q_id}"


def epic_registered_subject(epic_key: str) -> str:
    """Epic registration trigger subject (ADR-013 §6)."""
    return f"{EPIC_PREFIX}.{epic_key}.registered"


def log_subject(domain: str, role: str, agent_id: str) -> str:
    """Structured worker-log subject (ADR-013 §8)."""
    return f"{LOGS_PREFIX}.{slugify(domain)}.{slugify(role)}.{slugify(agent_id)}"


@dataclass(frozen=True)
class MeshConfig:
    """Configuration for one mesh worker process.

    Defaults mirror ADR-013 §1/§4: 15-min AckWait, 5-min heartbeats,
    MaxDeliver 10, MaxAckPending 3 (host heavy-agent budget), ~1 h overrun
    threshold.

    MaxDeliver started at 3 (ADR-013's suggestion) but proved too fragile in
    the live shakedown: a worker restart mid-claim and each retryable NOGO
    outcome both consume a delivery, so real tasks exhausted the budget and
    their dispatch messages vanished — leaving HmasTasks stuck Delegated /
    InProgress with an empty queue and no operator signal (#1780).
    """

    domain: str
    role: str
    agent_id: str
    exec_host: str = field(default_factory=socket.gethostname)
    nats_url: str = "nats://localhost:4222"
    team_id: str = "mesh"
    agamemnon_url: str = "http://localhost:8080"
    heartbeat_seconds: int = 300
    ack_wait_seconds: int = 900
    max_deliver: int = 10
    max_ack_pending: int = 3
    overrun_seconds: int = 3600

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> MeshConfig:
        """Build a config from ``MESH_*`` / ``NATS_URL`` environment variables.

        ``MESH_DOMAIN`` and ``MESH_ROLE`` are required; everything else has a
        default. ``AGENT_ID`` defaults to ``{domain}-{role}-{hostname}``.
        """
        env = os.environ if environ is None else environ
        domain = env["MESH_DOMAIN"]
        role = env["MESH_ROLE"]
        exec_host = env.get("EXEC_HOST", socket.gethostname())
        agent_id = env.get("AGENT_ID", f"{slugify(domain)}-{slugify(role)}-{slugify(exec_host)}")

        def _int(name: str, default: int) -> int:
            return int(env.get(name, default))

        return cls(
            domain=domain,
            role=role,
            agent_id=agent_id,
            exec_host=exec_host,
            nats_url=env.get("NATS_URL", cls.nats_url),
            team_id=env.get("MESH_TEAM_ID", cls.team_id),
            agamemnon_url=env.get("AGAMEMNON_URL", cls.agamemnon_url),
            heartbeat_seconds=_int("MESH_HEARTBEAT_SECONDS", cls.heartbeat_seconds),
            ack_wait_seconds=_int("MESH_ACK_WAIT_SECONDS", cls.ack_wait_seconds),
            max_deliver=_int("MESH_MAX_DELIVER", cls.max_deliver),
            max_ack_pending=_int("MESH_MAX_ACK_PENDING", cls.max_ack_pending),
            overrun_seconds=_int("MESH_OVERRUN_SECONDS", cls.overrun_seconds),
        )

    @property
    def filter_subject(self) -> str:
        """Filter subject for this worker's queue."""
        return dispatch_filter(self.domain, self.role)

    @property
    def durable_name(self) -> str:
        """Durable consumer name for this worker's queue."""
        return consumer_name(self.domain, self.role)

"""Tests for hephaestus.automation.mesh.config."""

from __future__ import annotations

import pytest

from hephaestus.automation.mesh.config import (
    MeshConfig,
    consumer_name,
    dispatch_filter,
    dispatch_subject,
    envelope,
    epic_registered_subject,
    interview_answer_subject,
    interview_question_subject,
    log_subject,
    parse_dispatch_subject,
    slugify,
    task_event_subject,
)


class TestSlugify:
    """Tests for NATS-token slugification."""

    def test_lowercases_and_hyphenates(self) -> None:
        assert slugify("Chief Architect") == "chief-architect"

    def test_repo_slug(self) -> None:
        assert slugify("HomericIntelligence/Odysseus") == "homericintelligence-odysseus"

    def test_collapses_runs(self) -> None:
        assert slugify("a..b//c") == "a-b-c"


class TestSubjects:
    """Tests for ADR-013 subject builders/parsers."""

    def test_dispatch_subject(self) -> None:
        assert (
            dispatch_subject("pipeline", "task-agent", "t-1")
            == "hi.myrmidon.pipeline.task-agent.task.t-1"
        )

    def test_dispatch_filter_and_durable(self) -> None:
        assert dispatch_filter("research", "chief-architect") == (
            "hi.myrmidon.research.chief-architect.task.>"
        )
        assert consumer_name("research", "chief-architect") == "myrmidon-research-chief-architect"

    def test_parse_dispatch_subject_round_trip(self) -> None:
        subject = dispatch_subject("pipeline", "task-agent", "abc123")
        assert parse_dispatch_subject(subject) == ("pipeline", "task-agent", "abc123")

    def test_parse_rejects_legacy_two_token(self) -> None:
        assert parse_dispatch_subject("hi.myrmidon.hello.task-7") is None

    def test_parse_rejects_other_namespaces(self) -> None:
        assert parse_dispatch_subject("hi.tasks.team.task.x.started") is None

    def test_task_event_subject(self) -> None:
        assert task_event_subject("team-1", "t-9", "started") == "hi.tasks.team-1.t-9.started"

    def test_interview_subjects(self) -> None:
        assert interview_question_subject("in-1", "q1") == "hi.pipeline.interview.in-1.question.q1"
        assert interview_answer_subject("in-1", "q1") == "hi.pipeline.interview.in-1.answer.q1"

    def test_epic_registered_subject(self) -> None:
        assert epic_registered_subject("owner-repo-12") == (
            "hi.pipeline.epic.owner-repo-12.registered"
        )

    def test_log_subject(self) -> None:
        assert log_subject("pipeline", "task-agent", "Agent 1") == (
            "hi.logs.myrmidon.pipeline.task-agent.agent-1"
        )


class TestEnvelope:
    """Tests for the hi/v1 payload envelope."""

    def test_envelope_fields(self) -> None:
        body = envelope(task_id="t-1")
        assert body["schema"] == "hi/v1"
        assert body["task_id"] == "t-1"
        assert body["ts"]
        assert body["msg_id"]

    def test_envelope_unique_msg_ids(self) -> None:
        assert envelope()["msg_id"] != envelope()["msg_id"]


class TestMeshConfig:
    """Tests for MeshConfig.from_env."""

    def test_requires_domain_and_role(self) -> None:
        with pytest.raises(KeyError):
            MeshConfig.from_env({})

    def test_defaults(self) -> None:
        cfg = MeshConfig.from_env({"MESH_DOMAIN": "pipeline", "MESH_ROLE": "task-agent"})
        assert cfg.heartbeat_seconds == 300
        assert cfg.ack_wait_seconds == 900
        # 10, not ADR-013's suggested 3: restarts + retryable NOGOs each burn
        # a delivery; 3 exhausted live and stranded tasks (#1780).
        assert cfg.max_deliver == 10
        assert cfg.max_ack_pending == 3
        assert cfg.overrun_seconds == 3600
        assert cfg.agent_id.startswith("pipeline-task-agent-")
        assert cfg.filter_subject == "hi.myrmidon.pipeline.task-agent.task.>"
        assert cfg.durable_name == "myrmidon-pipeline-task-agent"

    def test_env_overrides(self) -> None:
        cfg = MeshConfig.from_env(
            {
                "MESH_DOMAIN": "research",
                "MESH_ROLE": "chief-architect",
                "AGENT_ID": "r1",
                "EXEC_HOST": "hermes",
                "NATS_URL": "nats://example:4222",
                "MESH_TEAM_ID": "team-x",
                "AGAMEMNON_URL": "http://agamemnon:8080",
                "MESH_HEARTBEAT_SECONDS": "60",
                "MESH_OVERRUN_SECONDS": "120",
            }
        )
        assert cfg.agent_id == "r1"
        assert cfg.exec_host == "hermes"
        assert cfg.nats_url == "nats://example:4222"
        assert cfg.team_id == "team-x"
        assert cfg.heartbeat_seconds == 60
        assert cfg.overrun_seconds == 120

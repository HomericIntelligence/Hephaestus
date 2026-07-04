"""Tests for hephaestus.automation.mesh.roles.research."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hephaestus.automation.mesh.config import MeshConfig
from hephaestus.automation.mesh.interview import InterviewAnswer
from hephaestus.automation.mesh.roles.research import (
    ResearchHandler,
    extract_workflow_yaml,
    parse_questions,
)
from hephaestus.automation.mesh.worker import TaskContext

CFG = MeshConfig(domain="research", role="chief-architect", agent_id="r-1", exec_host="h")

BRIEF_WITH_YAML = """## Brief

Feasible. Prior art in tools/.

```yaml
name: slice
description: demo
agents:
  pipeline.task-agent: {type: claude}
tasks:
  - subject: do-it
    description: implement
    assign_to: pipeline.task-agent
    blocked_by: []
```
"""


class TestParseQuestions:
    """Tests for question extraction."""

    def test_parses_json_object(self) -> None:
        text = 'Here you go:\n{"questions": ["A?", "B?"]}'
        assert parse_questions(text) == ["A?", "B?"]

    def test_caps_at_three(self) -> None:
        text = '{"questions": ["1", "2", "3", "4"]}'
        assert len(parse_questions(text)) == 3

    def test_garbage_returns_empty(self) -> None:
        assert parse_questions("no json at all") == []
        assert parse_questions("{broken json") == []


class TestExtractWorkflowYaml:
    """Tests for the fenced yaml block extraction."""

    def test_extracts_block(self) -> None:
        yaml_text = extract_workflow_yaml(BRIEF_WITH_YAML)
        assert yaml_text is not None
        assert yaml_text.startswith("name: slice")

    def test_missing_block_is_none(self) -> None:
        assert extract_workflow_yaml("no yaml here") is None


class _Ctx(TaskContext):
    """TaskContext with ask/progress stubbed for handler tests."""

    answers: list[InterviewAnswer]

    def ask(self, question: str, q_id: str | None = None) -> InterviewAnswer:
        return self.answers.pop(0)


def _ctx(payload: dict[str, Any]) -> tuple[_Ctx, list[str]]:
    progressed: list[str] = []
    ctx = _Ctx(
        config=CFG,
        payload=payload,
        task_id="t-1",
        team_id="mesh",
        attempt=1,
        publisher=None,  # type: ignore[arg-type]
        agamemnon=None,  # type: ignore[arg-type]
        deadline=float("inf"),
    )
    ctx.answers = [InterviewAnswer(q_id="q1", answer="small", channel="console")]
    ctx.progress = progressed.append  # type: ignore[method-assign, assignment]
    return ctx, progressed


class TestResearchHandler:
    """Tests for the research role."""

    def test_missing_idea_is_non_retryable(self) -> None:
        handler = ResearchHandler(invoke=lambda p, c: "", register_epic=lambda p, r: {})
        ctx, _ = _ctx({})
        result = handler.handle(ctx)
        assert not result.ok
        assert result.error_kind == "BadDispatch"

    def test_happy_path_registers_epic(self, tmp_path: Path, monkeypatch: Any) -> None:
        monkeypatch.chdir(tmp_path)
        invocations: list[str] = []

        def invoke(prompt: str, ctx: TaskContext) -> str:
            invocations.append(prompt)
            if "clarifying questions" in prompt:
                return '{"questions": ["How big?"]}'
            return BRIEF_WITH_YAML

        registered: list[tuple[Path, str]] = []

        def register(path: Path, repo: str) -> dict[str, Any]:
            registered.append((path, repo))
            return {"epic": 77, "key": "o-r-77", "children": [78, 79]}

        handler = ResearchHandler(invoke=invoke, register_epic=register)
        ctx, progressed = _ctx(
            {"idea": "build the slice", "intake_id": "in-1", "issue": 5, "repo": "o/r"}
        )

        result = handler.handle(ctx)

        assert result.ok
        assert "epic #77" in result.summary
        # Interview Q&A mirrored to the intake issue.
        assert any("How big?" in p for p in progressed)
        # Workflow YAML written under build/ and handed to register-epic.
        path, repo = registered[0]
        assert repo == "o/r"
        assert path.exists()
        assert "pipeline.task-agent" in path.read_text()
        # Transcript flowed into the brief prompt.
        assert any("small" in p for p in invocations if "TRANSCRIPT" in p)

    def test_payload_intake_id_cannot_escape_mesh_work_dir(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        monkeypatch.chdir(tmp_path)
        registered: list[Path] = []

        def register(path: Path, repo: str) -> dict[str, Any]:
            registered.append(path)
            return {"epic": 77, "key": "o-r-77"}

        handler = ResearchHandler(
            invoke=lambda p, c: '{"questions": []}' if "clarifying" in p else BRIEF_WITH_YAML,
            register_epic=register,
        )
        ctx, _ = _ctx({"idea": "build the slice", "intake_id": "../../../../evil", "issue": 5})

        result = handler.handle(ctx)

        assert result.ok
        workflow_path = registered[0]
        mesh_dir = (tmp_path / "build/.mesh").resolve()
        assert workflow_path.resolve().is_relative_to(mesh_dir)
        assert workflow_path.name == "intake-evil.workflow.yaml"
        assert not (tmp_path / "evil.workflow.yaml").exists()

    def test_brief_without_yaml_is_retryable(self, tmp_path: Path, monkeypatch: Any) -> None:
        monkeypatch.chdir(tmp_path)
        handler = ResearchHandler(
            invoke=lambda p, c: '{"questions": []}' if "clarifying" in p else "no yaml",
            register_epic=lambda p, r: {},
        )
        ctx, _ = _ctx({"idea": "x", "issue": 5})

        result = handler.handle(ctx)

        assert not result.ok
        assert result.error_kind == "NoWorkflow"
        assert result.retryable

    def test_assumed_answers_are_recorded(self, tmp_path: Path, monkeypatch: Any) -> None:
        monkeypatch.chdir(tmp_path)

        def invoke(prompt: str, ctx: TaskContext) -> str:
            if "clarifying" in prompt:
                return '{"questions": ["Q1?"]}'
            return BRIEF_WITH_YAML

        handler = ResearchHandler(invoke=invoke, register_epic=lambda p, r: {"epic": 1, "key": "k"})
        ctx, progressed = _ctx({"idea": "x", "issue": 5})
        ctx.answers = [InterviewAnswer(q_id="q1", answer="", channel="assumed")]

        result = handler.handle(ctx)

        assert result.ok
        assert any("proceeding on assumptions" in p for p in progressed)

"""Tests for hephaestus.automation.mesh.roles.chief_architect."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest import mock

from hephaestus.automation.mesh.config import MeshConfig
from hephaestus.automation.mesh.epic import EpicChild
from hephaestus.automation.mesh.roles.chief_architect import (
    ChiefArchitectHandler,
    build_brief,
    impl_entry,
)
from hephaestus.automation.mesh.worker import TaskContext

CFG = MeshConfig(domain="pipeline", role="chief-architect", agent_id="p-1", exec_host="h")

EPIC_BODY = "- [ ] #11\n- [ ] #12 (depends on: #11)\n"


@dataclass
class FakePlanResult:
    """Planner per-issue result double."""

    success: bool = True


class FakePlanner:
    """Planner double."""

    def __init__(self, results: dict[int, FakePlanResult]) -> None:
        self.results = results

    def run(self) -> dict[int, FakePlanResult]:
        return self.results


class FakeAgamemnon:
    """Records the submitted brief."""

    def __init__(self) -> None:
        self.briefs: list[dict[str, Any]] = []

    def submit_brief(self, brief: dict[str, Any]) -> dict[str, Any]:
        self.briefs.append(brief)
        return {"brief": {"id": "b-1"}, "tasks": []}


def _ctx(payload: dict[str, Any], aga: FakeAgamemnon) -> TaskContext:
    ctx = TaskContext(
        config=CFG,
        payload=payload,
        task_id="t-1",
        team_id="mesh",
        attempt=1,
        publisher=None,  # type: ignore[arg-type]
        agamemnon=aga,  # type: ignore[arg-type]
        deadline=float("inf"),
    )
    ctx.progress = lambda text: None  # type: ignore[method-assign]
    return ctx


class TestImplEntry:
    """Tests for L3 impl-string encoding."""

    def test_plain(self) -> None:
        assert impl_entry(EpicChild(number=11)) == "#11"

    def test_with_deps(self) -> None:
        assert impl_entry(EpicChild(number=12, depends_on=[11, 9])) == ("#12 (depends on: #11, #9)")


class TestBuildBrief:
    """Tests for TaskBrief construction."""

    def test_shape_matches_agamemnon_parser(self) -> None:
        children = [EpicChild(number=11), EpicChild(number=12, depends_on=[11])]
        brief = build_brief(
            {"repo": "HomericIntelligence/Odysseus", "issue": 5}, "Epic title", "body", children
        )
        repo = "HomericIntelligence/Odysseus"
        assert brief["title"] == "Epic title"
        assert brief["repos"] == [repo]
        assert brief["modules"] == {repo: ["epic-5"]}
        assert brief["impls"][repo]["epic-5"] == ["#11", "#12 (depends on: #11)"]


class TestChiefArchitectHandler:
    """Tests for the planner role."""

    def test_missing_epic_is_non_retryable(self) -> None:
        handler = ChiefArchitectHandler(planner_factory=lambda issues: FakePlanner({}))
        result = handler.handle(_ctx({}, FakeAgamemnon()))
        assert not result.ok
        assert result.error_kind == "BadDispatch"

    def test_happy_path_plans_children_and_submits_brief(self) -> None:
        planned: list[list[int]] = []

        def factory(issues: list[int]) -> FakePlanner:
            planned.append(issues)
            return FakePlanner({n: FakePlanResult() for n in issues})

        handler = ChiefArchitectHandler(planner_factory=factory)
        aga = FakeAgamemnon()
        with mock.patch(
            "hephaestus.automation.github_api.issues.gh_issue_json",
            return_value={"title": "Epic", "body": EPIC_BODY},
        ):
            result = handler.handle(_ctx({"epic": {"repo": "o/r", "issue": 5}}, aga))

        assert result.ok
        assert planned == [[11, 12]]
        assert aga.briefs[0]["impls"]["o/r"]["epic-5"] == ["#11", "#12 (depends on: #11)"]
        assert "b-1" in result.summary

    def test_empty_epic_is_non_retryable(self) -> None:
        handler = ChiefArchitectHandler(planner_factory=lambda issues: FakePlanner({}))
        with mock.patch(
            "hephaestus.automation.github_api.issues.gh_issue_json",
            return_value={"title": "Epic", "body": "no tasks here"},
        ):
            result = handler.handle(_ctx({"epic": {"repo": "o/r", "issue": 5}}, FakeAgamemnon()))
        assert not result.ok
        assert result.error_kind == "EmptyEpic"
        assert not result.retryable

    def test_plan_failure_is_retryable(self) -> None:
        handler = ChiefArchitectHandler(
            planner_factory=lambda issues: FakePlanner(
                {issues[0]: FakePlanResult(success=False), issues[1]: FakePlanResult()}
            )
        )
        with mock.patch(
            "hephaestus.automation.github_api.issues.gh_issue_json",
            return_value={"title": "Epic", "body": EPIC_BODY},
        ):
            result = handler.handle(_ctx({"epic": {"repo": "o/r", "issue": 5}}, FakeAgamemnon()))
        assert not result.ok
        assert result.error_kind == "PlanFailed"
        assert result.retryable

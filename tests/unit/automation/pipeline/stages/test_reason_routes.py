"""Lock: every FAIL_BACK reason a stage emits is covered by its ROUTES row (m6).

A FAIL_BACK reason string that ROUTES cannot resolve would fall through to
the coordinator's default handling (or crash it), silently rewriting the
architecture doc's routing vocabulary. This test AST-scans each stage module
for ``StageOutcome(Disposition.FAIL_BACK, "<literal>")`` calls and asserts
every collected literal is either an explicit ``fail_routes`` key or covered
by the row's ``"*"`` default. It also pins the expected reason sets so a new
reason cannot appear without a conscious routing decision.
"""

from __future__ import annotations

import ast
import inspect
from types import ModuleType

import pytest

from hephaestus.automation.pipeline.routing import ROUTES, StageName
from hephaestus.automation.pipeline.stages import (
    implementation,
    merge_wait,
    plan_review,
    planning,
    pr_review,
)

_STAGE_MODULES: dict[StageName, ModuleType] = {
    StageName.PLANNING: planning,
    StageName.PLAN_REVIEW: plan_review,
    StageName.IMPLEMENTATION: implementation,
    StageName.PR_REVIEW: pr_review,
    StageName.MERGE_WAIT: merge_wait,
}

#: Reasons each stage is EXPECTED to emit (lock: additions must edit this).
_EXPECTED_REASONS: dict[StageName, set[str]] = {
    StageName.PLANNING: set(),
    StageName.PLAN_REVIEW: {"nogo", "plan_cycles_exhausted"},
    StageName.IMPLEMENTATION: {"plan_not_go", "already_implementation_go_pr"},
    # human_blocked is emitted as FINISH_FAIL (terminal), not FAIL_BACK —
    # its ROUTES row entry (-> FINISHED) documents the same destination.
    StageName.PR_REVIEW: {"agent_error"},
    StageName.MERGE_WAIT: {
        "not_implementation_go",
        "reviewed_head_drift",
        "reviewed_head_missing",
    },
}


def _fail_back_reason_literals(module: ModuleType) -> set[str]:
    """Collect the string literals passed to StageOutcome(FAIL_BACK, ...)."""
    tree = ast.parse(inspect.getsource(module))
    reasons: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != "StageOutcome":
            continue
        args = node.args
        if not args:
            continue
        first = args[0]
        if not (isinstance(first, ast.Attribute) and first.attr == "FAIL_BACK"):
            continue
        note_nodes: list[ast.expr] = list(args[1:2])
        note_nodes.extend(kw.value for kw in node.keywords if kw.arg == "note")
        for note in note_nodes:
            if isinstance(note, ast.Constant) and isinstance(note.value, str):
                reasons.add(note.value)
    return reasons


@pytest.mark.parametrize("stage_name", list(_STAGE_MODULES), ids=lambda s: s.value)
def test_every_fail_back_reason_is_routes_covered(stage_name: StageName) -> None:
    """Each emitted FAIL_BACK reason literal resolves in the stage's row."""
    module = _STAGE_MODULES[stage_name]
    reasons = _fail_back_reason_literals(module)
    fail_routes = ROUTES[stage_name].fail_routes
    uncovered = {r for r in reasons if r not in fail_routes and "*" not in fail_routes}
    assert not uncovered, (
        f"{stage_name.value} emits FAIL_BACK reason(s) {sorted(uncovered)} that "
        f"ROUTES[{stage_name.value}].fail_routes cannot resolve"
    )


@pytest.mark.parametrize("stage_name", list(_STAGE_MODULES), ids=lambda s: s.value)
def test_emitted_reason_set_is_pinned(stage_name: StageName) -> None:
    """The set of emitted reasons matches the lock (no silent vocabulary drift)."""
    module = _STAGE_MODULES[stage_name]
    assert _fail_back_reason_literals(module) == _EXPECTED_REASONS[stage_name]


def test_scan_is_not_vacuous() -> None:
    """The AST scan finds real emissions (guards against a silent no-op scan)."""
    assert "agent_error" in _fail_back_reason_literals(pr_review)
    assert "plan_not_go" in _fail_back_reason_literals(implementation)
    assert "not_implementation_go" in _fail_back_reason_literals(merge_wait)


def test_named_reasons_route_where_the_doc_says() -> None:
    """The doc's key cross-stage arrows hold for the emitted vocabulary."""
    assert ROUTES[StageName.PR_REVIEW].fail_routes["agent_error"] == StageName.IMPLEMENTATION
    assert ROUTES[StageName.IMPLEMENTATION].fail_routes["plan_not_go"] == StageName.PLAN_REVIEW
    assert (
        ROUTES[StageName.IMPLEMENTATION].fail_routes["already_implementation_go_pr"]
        == StageName.MERGE_WAIT
    )
    assert ROUTES[StageName.MERGE_WAIT].fail_routes["not_implementation_go"] == StageName.PR_REVIEW
    assert ROUTES[StageName.MERGE_WAIT].fail_routes["reviewed_head_missing"] == StageName.PR_REVIEW
    assert ROUTES[StageName.MERGE_WAIT].fail_routes["reviewed_head_drift"] == StageName.PR_REVIEW
    assert ROUTES[StageName.MERGE_WAIT].fail_routes["closed"] == StageName.FINISHED

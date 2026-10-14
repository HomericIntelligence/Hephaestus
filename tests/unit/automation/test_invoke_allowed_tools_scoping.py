"""Test the explicit tool scope of the live agent contract fixture."""

from __future__ import annotations

import ast
import pathlib

CONTRACT_AGENT_TEST = (
    pathlib.Path(__file__).parents[3]
    / "tests"
    / "integration"
    / "contract"
    / "test_agent_contract.py"
)


def test_contract_agent_lane_has_explicit_zero_tool_noninteractive_scope() -> None:
    """The token-spending contract lane must not inherit interactive CLI defaults."""
    tree = ast.parse(CONTRACT_AGENT_TEST.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "attr", None) or getattr(node.func, "id", None))
        == "invoke_claude_with_session"
    ]

    assert len(calls) == 2, "the contract must preserve its create-and-resume invocations"
    for call in calls:
        kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}
        assert _string_literal(kwargs.get("allowed_tools")) == ""
        assert _string_literal(kwargs.get("permission_mode")) == "dontAsk"
        agent = kwargs.get("agent")
        assert isinstance(agent, ast.Name)
        assert agent.id == "AGENT_PLANNER"


def _string_literal(node: ast.AST | None) -> str | None:
    """Return a string literal value for focused call-site policy assertions."""
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

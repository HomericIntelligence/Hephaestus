"""Every invoke_claude_with_session call must pass an --allowedTools scope (#1082)."""

from __future__ import annotations

import ast
import pathlib

import pytest

AUTOMATION_DIR = pathlib.Path(__file__).parents[3] / "hephaestus" / "automation"
CONTRACT_AGENT_TEST = (
    pathlib.Path(__file__).parents[3]
    / "tests"
    / "integration"
    / "contract"
    / "test_agent_contract.py"
)

# (filename, minimum tools, gh_required)
# gh_required=True means the agent itself may shell to gh and so needs "Bash".
# False = orchestrator posts on the agent's behalf; agent is read-only analysis.
CALL_SITES = [
    ("pr_review_core.py", {"Read", "Glob", "Grep"}, False),
    ("plan_reviewer.py", {"Read", "Glob", "Grep"}, False),
    # planner.py was re-pointed at the queue-based pipeline (#1820): its
    # planning agent calls now go through the pipeline stages'
    # ``AgentJob``/worker pool, not a direct ``invoke_claude_with_session``
    # call site, so it is no longer scanned here.
    # ci_driver.py was re-pointed at the queue-based pipeline (#1822): its
    # remaining drive-green work now goes through the ``AgentJob``/worker pool
    # rather than a direct ``invoke_claude_with_session`` call site, so it is
    # no longer scanned here.
    # implementer.py was re-pointed at the queue-based pipeline (#1821): its
    # implementation agent calls now go through the pipeline stages'
    # ``AgentJob``/worker pool (the legacy per-issue phase runner was deleted),
    # not a direct ``invoke_claude_with_session`` call site, so it is no longer
    # scanned here.
    ("comment_difficulty.py", {"Read", "Glob", "Grep"}, False),
]


def _allowed_tools_kwargs(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text())
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        # Handle both forms: invoke_claude_with_session(...) and
        # _impl_mod.invoke_claude_with_session(...)
        name = getattr(fn, "attr", None) or getattr(fn, "id", None)
        if name != "invoke_claude_with_session":
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        if "allowed_tools" not in kwargs:
            pytest.fail(
                f"{path.name}: invoke_claude_with_session call missing allowed_tools= kwarg"
            )
        val = kwargs["allowed_tools"]
        if isinstance(val, ast.Constant) and isinstance(val.value, str):
            out.append(val.value)
            continue
        pytest.fail(
            f"{path.name}: allowed_tools must be a string literal (this test asserts that contract)"
        )
    return out


@pytest.mark.parametrize("filename, min_tools, gh_required", CALL_SITES)
def test_call_site_scope(filename: str, min_tools: set[str], gh_required: bool) -> None:
    """Verify each automation call site passes correct allowed_tools scope."""
    values = _allowed_tools_kwargs(AUTOMATION_DIR / filename)
    assert values, f"{filename}: no invoke_claude_with_session call found"
    for v in values:
        tools = {t.strip() for t in v.split(",") if t.strip()}
        missing = min_tools - tools
        assert not missing, f"{filename} scope {v!r} missing {missing}"
        if gh_required:
            assert "Bash" in tools, f"{filename} scope {v!r} must include Bash for gh CLI access"


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
        assert agent.id == "AGENT_ADVISE"


def _string_literal(node: ast.AST | None) -> str | None:
    """Return a string literal value for focused call-site policy assertions."""
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

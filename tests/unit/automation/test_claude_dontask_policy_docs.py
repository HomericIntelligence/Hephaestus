"""Document the retained queue's Claude tool grants and permission policy."""

from __future__ import annotations

import ast
from pathlib import Path

from hephaestus.automation.agent_config import AGENT_IMPLEMENTER
from hephaestus.automation.pipeline.tool_scopes import tool_scope_for

ROOT = Path(__file__).parents[3]
AUTOMATION = ROOT / "hephaestus" / "automation"

REVIEW_JOB_SITE = "pipeline/stages/pr_review_jobs.py:PrReviewJobs._submit_review_job"
REVIEW_TOOLS = "Read,Glob,Grep,Bash,Skill,Agent,WebFetch"


def _literal(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _documented_rows() -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in (ROOT / "AGENTS.md").read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 3 or not cells[0].startswith("`"):
            continue
        rows[cells[0].strip("`")] = cells[1].strip("`")
    return rows


def test_queue_claude_tool_grants_are_documented_in_agents_md() -> None:
    """The current review and implementation grants must match their policy rows."""
    tree = ast.parse((AUTOMATION / "pipeline/stages/pr_review_jobs.py").read_text(encoding="utf-8"))
    reviewer = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PrReviewJobs"
    )
    submit = next(
        node
        for node in reviewer.body
        if isinstance(node, ast.FunctionDef) and node.name == "_submit_review_job"
    )
    jobs = [
        node
        for node in ast.walk(submit)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "AgentJob"
    ]
    assert len(jobs) == 1
    kwargs = {kw.arg: kw.value for kw in jobs[0].keywords if kw.arg}
    assert _literal(kwargs.get("allowed_tools")) == REVIEW_TOOLS
    assert _literal(kwargs.get("sandbox")) == "read-only"
    agents_md = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "There is no OS-level seccomp, namespace, or chroot sandbox" in agents_md
    documented = _documented_rows()
    assert documented.get(REVIEW_JOB_SITE) == REVIEW_TOOLS
    implementation_scope = tool_scope_for(AGENT_IMPLEMENTER)
    assert implementation_scope.permission_mode == "dontAsk"
    assert documented.get("pipeline/stages/implementation.py") == implementation_scope.allowed_tools


def test_agents_md_has_design_philosophy_section() -> None:
    """AGENTS.md must document the agent-design philosophy (issue #2111)."""
    agents_md = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "## Design Philosophy" in agents_md
    # Heritage attribution required by the issue title.
    assert "ProjectOdyssey" in agents_md
    # Core principles the section is grounded in.
    for principle in ("KISS", "YAGNI", "SOLID", "POLA"):
        assert principle in agents_md, f"Design Philosophy missing {principle}"
    # Cross-link back to the canonical principle list in AGENTS.md itself.
    assert "#key-development-principles" in agents_md

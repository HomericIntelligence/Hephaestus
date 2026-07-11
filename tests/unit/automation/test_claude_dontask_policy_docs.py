"""#1482: documented policy for Claude permission_mode='dontAsk' call sites."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[3]
AUTOMATION = ROOT / "hephaestus" / "automation"

EXPECTED_DONTASK_CALLS = {
    "audit_reviewer.py:run_audit_coordinator": "Read,Glob,Grep",
    "review_validator.py:_run_validation_session": "Read,Glob,Grep",
    "comment_difficulty.py:_run_classifier_session": "Read,Glob,Grep",
    "pr_review_core.py:_invoke_and_parse_review_session": "Read,Glob,Grep",
    "address_review_core.py:run_address_fix_session": "Read,Write,Edit,Glob,Grep,Bash,Task,Skill",
    "_implement_phase.py:ImplementPhase._run_claude_impl_session": (
        "Read,Write,Edit,Glob,Grep,Bash"
    ),
    "_review_phase.py:ReviewPhase._resume_impl_with_feedback": "Read,Write,Edit,Glob,Grep,Bash",
}


def _literal(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


class _DontAskVisitor(ast.NodeVisitor):
    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.scope: list[str] = []
        self.calls: dict[str, str] = {}

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_Call(self, node: ast.Call) -> None:
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name == "invoke_claude_with_session":
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            if _literal(kwargs.get("permission_mode")) == "dontAsk":
                self.calls[f"{self.filename}:{'.'.join(self.scope)}"] = (
                    _literal(kwargs.get("allowed_tools")) or ""
                )
        self.generic_visit(node)


def _documented_rows() -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in (ROOT / "AGENTS.md").read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 3 or not cells[0].startswith("`"):
            continue
        rows[cells[0].strip("`")] = cells[1].strip("`")
    return rows


def test_issue_1482_dontask_sites_are_documented_in_agents_md() -> None:
    """Verify every issue-named dontAsk site has paired policy documentation."""
    actual: dict[str, str] = {}
    for key in EXPECTED_DONTASK_CALLS:
        filename = key.split(":", 1)[0]
        visitor = _DontAskVisitor(filename)
        visitor.visit(ast.parse((AUTOMATION / filename).read_text(encoding="utf-8")))
        actual.update(visitor.calls)

    assert actual == EXPECTED_DONTASK_CALLS

    agents_md = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "There is no OS-level seccomp, namespace, or chroot sandbox" in agents_md
    documented = _documented_rows()
    for call_key, tools in EXPECTED_DONTASK_CALLS.items():
        assert documented.get(call_key) == tools

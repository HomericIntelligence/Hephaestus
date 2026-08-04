"""Every pipeline ``AgentJob`` must declare an explicit tool scope.

The direct-call policy test validates ``invoke_claude_with_session`` call
sites, but queue-pipeline agents reach the worker pool through frozen
``AgentJob`` specifications. This test statically covers those stage paths so
an omitted, broadened, or newly added scope cannot bypass policy review.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

STAGES_DIR = Path(__file__).parents[4] / "hephaestus" / "automation" / "pipeline" / "stages"

READ_ONLY = "Read,Glob,Grep"
WRITE = "Read,Write,Edit,Glob,Grep,Bash"
ADDRESS = "Read,Write,Edit,Glob,Grep,Bash,Task,Skill"
PR_REVIEW = "Read,Glob,Grep,Bash,Skill,Agent,WebFetch"

# (stage filename, enclosing function, prompt_builder source) -> exact scope.
# The primary PR review retains the additional read-only helper capabilities
# required by the normal review workflow; it does not grant write tools.
EXPECTED_SCOPES = {
    ("planning.py", "step", "get_advise_prompt_builder(ctx.config.agent)"): READ_ONLY,
    ("planning.py", "step", "build_plan_prompt"): READ_ONLY,
    ("plan_review.py", "step", "get_plan_loop_review_prompt"): READ_ONLY,
    ("plan_review.py", "step", "build_amend_prompt"): READ_ONLY,
    ("plan_review.py", "step", "build_learn_prompt"): WRITE,
    (
        "implementation.py",
        "_dirty_decision_wait",
        "get_dirty_reused_worktree_decision_prompt",
    ): READ_ONLY,
    (
        "implementation.py",
        "_advise_wait",
        "get_advise_prompt_builder(ctx.config.agent)",
    ): READ_ONLY,
    ("implementation.py", "_implement_wait", "get_address_review_prompt"): ADDRESS,
    ("implementation.py", "_implement_wait", "build_implementation_prompt"): WRITE,
    ("implementation.py", "_testfix_wait", "build_test_fix_prompt"): WRITE,
    ("pr_review.py", "_submit_review_job", "get_pr_review_analysis_prompt"): PR_REVIEW,
    ("pr_review.py", "_validate_wait", "get_review_validation_prompt"): READ_ONLY,
    ("merge_wait.py", "_request_learn", "build_drive_green_learn_prompt"): WRITE,
}


def _discover_agent_jobs() -> dict[tuple[str, str, str], str]:
    """Discover stage ``AgentJob`` scopes keyed by their source location.

    The returned mapping uses the stage filename, enclosing function, and
    prompt-builder source as its key and the literal ``allowed_tools`` value as
    its value.
    The test fails if a constructor omits ``allowed_tools`` or uses a
    non-literal value that cannot be checked by this policy test.
    """
    discovered: dict[tuple[str, str, str], str] = {}

    for path in sorted(STAGES_DIR.glob("*.py")):
        tree = ast.parse(path.read_text())

        class _Visitor(ast.NodeVisitor):
            def __init__(self, filename: str) -> None:
                self.filename = filename
                self.function_stack: list[str] = []

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self.function_stack.append(node.name)
                self.generic_visit(node)
                self.function_stack.pop()

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self.function_stack.append(node.name)
                self.generic_visit(node)
                self.function_stack.pop()

            def visit_Call(self, node: ast.Call) -> None:
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name == "AgentJob":
                    self._record(node)
                self.generic_visit(node)

            def _record(self, node: ast.Call) -> None:
                function = self.function_stack[-1] if self.function_stack else "<module>"
                kwargs = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
                builder = kwargs.get("prompt_builder")
                builder_source = ast.unparse(builder) if builder is not None else "<none>"
                scope = kwargs.get("allowed_tools")
                if scope is None:
                    pytest.fail(
                        f"{self.filename}:{node.lineno}: AgentJob in {function}() "
                        f"({builder_source}) missing allowed_tools= kwarg"
                    )
                if not isinstance(scope, ast.Constant) or not isinstance(scope.value, str):
                    pytest.fail(
                        f"{self.filename}:{node.lineno}: AgentJob in {function}() "
                        f"({builder_source}) allowed_tools must be a string literal"
                    )
                key = (self.filename, function, builder_source)
                discovered[key] = scope.value

        _Visitor(path.name).visit(tree)

    return discovered


def test_every_pipeline_agent_job_matches_expected_scope() -> None:
    """Every current stage job is registered with its exact tool scope."""
    discovered = _discover_agent_jobs()

    missing = sorted(set(EXPECTED_SCOPES) - set(discovered))
    assert not missing, f"expected AgentJob call sites not found: {missing}"

    extra = sorted(set(discovered) - set(EXPECTED_SCOPES))
    assert not extra, f"unregistered pipeline AgentJob call sites: {extra}"

    mismatched = {
        key: (discovered[key], EXPECTED_SCOPES[key])
        for key in EXPECTED_SCOPES
        if discovered[key] != EXPECTED_SCOPES[key]
    }
    assert not mismatched, f"AgentJob scope drift (actual, expected): {mismatched}"

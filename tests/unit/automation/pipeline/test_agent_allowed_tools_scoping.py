"""Every pipeline ``AgentJob`` must declare an explicit tool scope.

The direct-call policy test validates ``invoke_claude_with_session`` call
sites, but queue-pipeline agents reach the worker pool through frozen
``AgentJob`` specifications. This test statically covers every production
pipeline module so an omitted, broadened, or newly added scope cannot bypass
policy review.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PIPELINE_DIR = Path(__file__).parents[4] / "hephaestus" / "automation" / "pipeline"

READ_ONLY = "Read,Glob,Grep"
WRITE = "Read,Write,Edit,Glob,Grep,Bash"
EDIT_ONLY = "Read,Write,Edit,Glob,Grep"
ADDRESS = "Read,Write,Edit,Glob,Grep,Bash,Task,Skill"
PR_REVIEW = "Read,Glob,Grep,Bash,Skill,Agent,WebFetch"
READ_ONLY_SCOPES = frozenset({READ_ONLY, PR_REVIEW})

# (pipeline-relative path, enclosing function, prompt_builder source) -> exact scope.
# The primary PR review retains the additional read-only helper capabilities
# required by the normal review workflow; it does not grant write tools.
EXPECTED_SCOPES = {
    (
        "stages/planning.py",
        "_requirements_recovery_step",
        "build_recovery_prompt",
    ): READ_ONLY,
    (
        "stages/planning.py",
        "_requirements_recovery_step",
        "build_recovery_review_prompt",
    ): READ_ONLY,
    ("stages/planning.py", "step", "build_plan_prompt"): READ_ONLY,
    ("stages/plan_review.py", "step", "get_plan_loop_review_prompt"): READ_ONLY,
    ("stages/plan_review.py", "step", "build_amend_prompt"): READ_ONLY,
    (
        "stages/implementation.py",
        "_dirty_decision_wait",
        "get_dirty_reused_worktree_decision_prompt",
    ): READ_ONLY,
    ("stages/implementation.py", "_implement_wait", "get_address_review_prompt"): ADDRESS,
    ("stages/implementation.py", "_implement_wait", "build_implementation_prompt"): WRITE,
    (
        "stages/implementation.py",
        "_rebase_conflict_wait",
        "build_implementation_prompt",
    ): EDIT_ONLY,
    ("stages/implementation.py", "_testfix_wait", "build_test_fix_prompt"): WRITE,
    ("stages/pr_review_jobs.py", "_submit_review_job", "get_pr_review_analysis_prompt"): PR_REVIEW,
    ("stages/pr_review_jobs.py", "_validate_wait", "get_review_validation_prompt"): READ_ONLY,
}

# Athena advise/learn calls are host-owned typed jobs, not prompt-only AgentJobs.
# They intentionally have no direct agent tool scope; the runtime adapter maps
# their closed request kind to the minimal provider-specific capability grants.
EXPECTED_ATHENA_SKILLS = {
    ("stages/planning.py", "step", "advise"),
    ("stages/implementation.py", "_advise_wait", "advise"),
    ("stages/learning.py", "step", "learn"),
}


def _discover_agent_jobs() -> dict[tuple[str, str, str], str]:
    """Discover pipeline ``AgentJob`` scopes keyed by their source location.

    The returned mapping uses the pipeline-relative path, enclosing function,
    and prompt-builder source as its key and the literal ``allowed_tools``
    value as its value.
    The test fails if a constructor omits ``allowed_tools`` or uses a
    non-literal value that cannot be checked by this policy test.
    """
    discovered: dict[tuple[str, str, str], str] = {}
    discovered_locations: dict[tuple[str, str, str], int] = {}

    for path in sorted(PIPELINE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text())

        class _Visitor(ast.NodeVisitor):
            def __init__(self, relative_path: str) -> None:
                self.relative_path = relative_path
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
                        f"{self.relative_path}:{node.lineno}: AgentJob in {function}() "
                        f"({builder_source}) missing allowed_tools= kwarg"
                    )
                if not isinstance(scope, ast.Constant) or not isinstance(scope.value, str):
                    pytest.fail(
                        f"{self.relative_path}:{node.lineno}: AgentJob in {function}() "
                        f"({builder_source}) allowed_tools must be a string literal"
                    )
                if not scope.value.strip():
                    pytest.fail(
                        f"{self.relative_path}:{node.lineno}: AgentJob in {function}() "
                        f"({builder_source}) allowed_tools must be non-empty"
                    )
                key = (self.relative_path, function, builder_source)
                if key in discovered:
                    pytest.fail(
                        f"{self.relative_path}:{node.lineno}: duplicate AgentJob policy key "
                        f"{key}; previous occurrence at line {discovered_locations[key]}. "
                        "Include a distinct prompt builder or extend the policy key before "
                        "adding another constructor with the same identity."
                    )
                discovered[key] = scope.value
                discovered_locations[key] = node.lineno

        _Visitor(path.relative_to(PIPELINE_DIR).as_posix()).visit(tree)

    return discovered


def _discover_agent_job_sandboxes() -> dict[tuple[str, str, str], str | None]:
    """Discover each pipeline ``AgentJob``'s explicit sandbox literal."""
    discovered: dict[tuple[str, str, str], str | None] = {}

    for path in sorted(PIPELINE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text())

        class _Visitor(ast.NodeVisitor):
            def __init__(self, relative_path: str) -> None:
                self.relative_path = relative_path
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
                sandbox = kwargs.get("sandbox")
                if sandbox is None:
                    sandbox_value: str | None = None
                elif isinstance(sandbox, ast.Constant) and isinstance(sandbox.value, str):
                    sandbox_value = sandbox.value
                else:
                    pytest.fail(
                        f"{self.relative_path}:{node.lineno}: AgentJob in {function}() "
                        f"({builder_source}) sandbox must be omitted or a string literal"
                    )
                discovered[(self.relative_path, function, builder_source)] = sandbox_value

        _Visitor(path.relative_to(PIPELINE_DIR).as_posix()).visit(tree)

    return discovered


def _discover_athena_skill_jobs() -> set[tuple[str, str, str]]:
    """Discover typed Athena skill jobs by source location and request kind."""
    discovered: set[tuple[str, str, str]] = set()

    for path in sorted(PIPELINE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text())

        class _Visitor(ast.NodeVisitor):
            def __init__(self, relative_path: str) -> None:
                self.relative_path = relative_path
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
                if name == "AthenaSkillJob":
                    self._record(node)
                self.generic_visit(node)

            def _record(self, node: ast.Call) -> None:
                function = self.function_stack[-1] if self.function_stack else "<module>"
                kwargs = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
                request = kwargs.get("request")
                if not isinstance(request, ast.Call):
                    pytest.fail(
                        f"{self.relative_path}:{node.lineno}: AthenaSkillJob in {function}() "
                        "must build an AthenaSkillRequest directly"
                    )
                request_kwargs = {
                    keyword.arg: keyword.value for keyword in request.keywords if keyword.arg
                }
                kind = request_kwargs.get("kind")
                if not isinstance(kind, ast.Constant) or kind.value not in {"advise", "learn"}:
                    pytest.fail(
                        f"{self.relative_path}:{node.lineno}: AthenaSkillJob in {function}() "
                        "must declare a literal advise/learn kind"
                    )
                discovered.add((self.relative_path, function, str(kind.value)))

        _Visitor(path.relative_to(PIPELINE_DIR).as_posix()).visit(tree)

    return discovered


def test_every_pipeline_agent_job_matches_expected_scope() -> None:
    """Every current pipeline job is registered with its exact tool scope."""
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


def test_every_pipeline_athena_skill_job_is_registered() -> None:
    """Typed Athena skill jobs must stay explicit and enumerable."""
    discovered = _discover_athena_skill_jobs()

    missing = sorted(EXPECTED_ATHENA_SKILLS - discovered)
    assert not missing, f"expected AthenaSkillJob call sites not found: {missing}"

    extra = sorted(discovered - EXPECTED_ATHENA_SKILLS)
    assert not extra, f"unregistered pipeline AthenaSkillJob call sites: {extra}"


def test_every_read_only_pipeline_agent_job_declares_read_only_sandbox() -> None:
    """Read-only tool scopes must also constrain direct providers like Codex."""
    sandboxes = _discover_agent_job_sandboxes()

    widened = {
        key: sandboxes.get(key)
        for key, scope in EXPECTED_SCOPES.items()
        if scope in READ_ONLY_SCOPES and sandboxes.get(key) != "read-only"
    }

    assert not widened, f"read-only AgentJob sandbox drift: {widened}"

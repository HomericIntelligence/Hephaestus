"""Every pipeline ``AgentJob`` must declare an explicit least-privilege scope.

The direct-call policy test (``test_invoke_allowed_tools_scoping.py``) validates
``invoke_claude_with_session`` call sites, but the queue-pipeline migration
(#1820-#1823) routes agent invocations through frozen ``AgentJob`` specs handed
to :class:`~hephaestus.automation.pipeline.worker_pool.WorkerPool`. Those paths
were previously unscanned, so a pipeline job could omit or broaden its scope —
or a worker-pool bypass could drop it entirely — undetected (#2162).

This test statically discovers every ``AgentJob`` constructor under
``pipeline/stages/`` and pins each one — keyed by (stage file, enclosing
function, prompt builder) — to an exact allowed-tools literal. It fails when a
job omits ``allowed_tools``, uses a computed (non-literal) value, changes an
expected scope, or introduces an unregistered pipeline call. The execution-level
proof that the worker forwards the scope lives in ``test_worker_pool.py``.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

STAGES_DIR = pathlib.Path(__file__).parents[4] / "hephaestus" / "automation" / "pipeline" / "stages"

READ_ONLY = "Read,Glob,Grep"
WRITE = "Read,Write,Edit,Glob,Grep,Bash"
ADDRESS = "Read,Write,Edit,Glob,Grep,Bash,Task,Skill"

# (stage filename, enclosing function, prompt_builder source) -> required scope.
# Analysis / planning / review / validation / classification / follow-up jobs
# are read-only; implementation / test-fix / CI-fix / learn jobs may write and
# shell out; only the existing-PR address-review coordinator gets Task,Skill.
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
    ("implementation.py", "_implement_wait", "build_implementation_prompt"): WRITE,
    ("implementation.py", "_testfix_wait", "build_test_fix_prompt"): WRITE,
    ("pr_review.py", "_review_wait", "get_pr_review_analysis_prompt"): READ_ONLY,
    ("pr_review.py", "_validate_wait", "get_review_validation_prompt"): READ_ONLY,
    ("pr_review.py", "_difficulty_wait", "get_comment_difficulty_prompt"): READ_ONLY,
    ("pr_review.py", "_followup_wait", "get_follow_up_prompt"): READ_ONLY,
    ("pr_review.py", "_address", "get_address_review_prompt"): ADDRESS,
    ("pr_review.py", "_address", "get_impl_resume_feedback_prompt"): WRITE,
    ("strict_review.py", "_review_wait", "build_strict_review_prompt"): READ_ONLY,
    ("ci.py", "_request_fix", "prompt_builder"): WRITE,
    ("merge_wait.py", "_request_learn", "build_drive_green_learn_prompt"): WRITE,
}


def _discover_agent_jobs() -> dict[tuple[str, str, str], str]:
    """Return {(filename, enclosing_func, prompt_builder_src): allowed_tools}.

    Walks every ``pipeline/stages/*.py`` module, finds each ``AgentJob(...)``
    constructor, and records its enclosing function name, the source text of its
    ``prompt_builder`` argument, and its ``allowed_tools`` literal. Fails the
    test (via :func:`pytest.fail`) when a job omits ``allowed_tools`` or passes a
    non-string-literal value, since the least-privilege contract cannot be
    verified statically in either case.
    """
    discovered: dict[tuple[str, str, str], str] = {}

    for path in sorted(STAGES_DIR.glob("*.py")):
        tree = ast.parse(path.read_text())

        class _Visitor(ast.NodeVisitor):
            def __init__(self, filename: str) -> None:
                self.filename = filename
                self.func_stack: list[str] = []

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                # Stage step functions are all synchronous defs; no async
                # handler is needed to reach the AgentJob call sites.
                self.func_stack.append(node.name)
                self.generic_visit(node)
                self.func_stack.pop()

            def visit_Call(self, node: ast.Call) -> None:
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name == "AgentJob":
                    self._record(node)
                self.generic_visit(node)

            def _record(self, node: ast.Call) -> None:
                func = self.func_stack[-1] if self.func_stack else "<module>"
                kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
                builder = kwargs.get("prompt_builder")
                builder_src = ast.unparse(builder) if builder is not None else "<none>"
                key = (self.filename, func, builder_src)

                scope = kwargs.get("allowed_tools")
                if scope is None:
                    pytest.fail(
                        f"{self.filename}:{node.lineno}: AgentJob in {func}() "
                        f"({builder_src}) missing allowed_tools= kwarg"
                    )
                if not (isinstance(scope, ast.Constant) and isinstance(scope.value, str)):
                    pytest.fail(
                        f"{self.filename}:{node.lineno}: AgentJob in {func}() "
                        f"({builder_src}) allowed_tools must be a string literal"
                    )
                discovered[key] = scope.value

        _Visitor(path.name).visit(tree)

    return discovered


def test_every_pipeline_agent_job_matches_expected_scope() -> None:
    """Discovered pipeline AgentJob scopes must match EXPECTED_SCOPES exactly.

    A mismatch on either side is a failure: an omitted/broadened scope, or a new
    unregistered pipeline call site that the policy map does not yet cover.
    """
    discovered = _discover_agent_jobs()

    missing = sorted(set(EXPECTED_SCOPES) - set(discovered))
    assert not missing, f"expected AgentJob call sites not found (moved/removed?): {missing}"

    extra = sorted(set(discovered) - set(EXPECTED_SCOPES))
    assert not extra, (
        "unregistered pipeline AgentJob call site(s) — add an explicit "
        f"least-privilege scope to EXPECTED_SCOPES: {extra}"
    )

    mismatched = {
        key: (discovered[key], EXPECTED_SCOPES[key])
        for key in EXPECTED_SCOPES
        if discovered[key] != EXPECTED_SCOPES[key]
    }
    assert not mismatched, f"AgentJob scope drift (actual, expected): {mismatched}"


@pytest.mark.parametrize("key, expected", sorted(EXPECTED_SCOPES.items()))
def test_pipeline_agent_job_scope(key: tuple[str, str, str], expected: str) -> None:
    """Each registered pipeline AgentJob declares its expected literal scope."""
    discovered = _discover_agent_jobs()
    assert key in discovered, f"AgentJob call site {key} not found"
    assert discovered[key] == expected

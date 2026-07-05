"""Guard that pipeline modules have zero I/O imports (ast-based, not text-scan)."""

import ast
from pathlib import Path

import hephaestus.automation.pipeline as pkg

_PIPELINE_DIR = Path(pkg.__file__).parent

# Modules whose mere import implies (or enables) I/O / shelling out.
_FORBIDDEN_MODULES = {
    "subprocess",
    "os",
    "socket",
    "shutil",
    "urllib",
    "http",
    "requests",
    "httpx",
    "asyncio",
    "pty",
    "fcntl",
    "tempfile",
}
_FORBIDDEN_PREFIXES = (
    "hephaestus.automation.github_api",
    "hephaestus.automation.claude_invoke",
    "hephaestus.github",
    "hephaestus.automation.git_utils",
    # Both wrap subprocess execution; importing them from the pure-data layer
    # would smuggle shell-out capability past the stdlib forbid list.
    "hephaestus.utils",
    "hephaestus.resilience",
)

# Modules exempt from the zero-I/O guard entirely.
# worker_pool.py is the ONLY place that executes jobs (agent, git, build/test),
# so it must import I/O modules; all other workers offload to it.
_ALLOWLIST = frozenset({"worker_pool.py"})

# Capability-scoped exemptions: seeding.py and admission.py are the sanctioned
# "thin fetch over github_api" layer (epic #1809 PR-4): they READ GitHub facts
# for the classifier/serializer but perform no mutations (the AST mutator guard
# in test_pipeline_architecture.py still applies to them). Their exemption is
# NOT module-wide — only the read-seam prefixes below are permitted, so a
# direct `import subprocess` / `import os` in either file still trips the
# guard.
_THIN_FETCH_PREFIXES = (
    "hephaestus.automation.github_api",
    "hephaestus.automation._review_utils",
    "hephaestus.automation.state_labels",
    "hephaestus.automation.dependency_resolver",
)
_CAPABILITY_EXEMPT: dict[str, tuple[str, ...]] = {
    "seeding.py": _THIN_FETCH_PREFIXES,
    "admission.py": _THIN_FETCH_PREFIXES,
    # stages/plan_review.py imports ONLY the pure verdict parser
    # (claude_invoke.parse_review_verdict / ReviewVerdict) to attach as
    # AgentJob.parse — the architecture doc's plan_review contract says the
    # "verdict [is] parsed in-worker by claude_invoke.parse_review_verdict"
    # (#1814). The exemption is prefix-scoped: any other I/O-flavored import
    # in the module still trips the guard via the remaining prefixes.
    "plan_review.py": ("hephaestus.automation.claude_invoke",),
}


def _forbidden(name: str) -> bool:
    """Check if a module name is forbidden."""
    root = name.split(".")[0]
    return root in _FORBIDDEN_MODULES or name.startswith(_FORBIDDEN_PREFIXES)


def _collect_violations(tree: ast.AST, filename: str, exempt: tuple[str, ...]) -> list[str]:
    """Walk *tree* and return forbidden-import violations, honoring *exempt* prefixes."""

    def _is_violation(module: str) -> bool:
        return _forbidden(module) and not module.startswith(exempt)

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_violation(alias.name):
                    violations.append(f"{filename}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if _is_violation(mod):
                violations.append(f"{filename}:{node.lineno}: from {mod} import ...")
    return violations


def test_pipeline_modules_have_zero_io_imports() -> None:
    """Verify pipeline modules do not import I/O-related modules.

    Uses AST parsing to detect imports anywhere in the module (including
    inside function bodies), not just at the top level. This catches
    conditional and lazy imports that a text-scan would miss.

    Exceptions: worker_pool.py is the only place that executes jobs
    (agent, git, build/test), so it must import I/O modules; seeding.py and
    admission.py get a capability-scoped exemption for their sanctioned
    read seams only (see ``_CAPABILITY_EXEMPT``).
    """
    violations: list[str] = []
    # rglob so future pipeline/ subpackages (e.g. stages/) stay guarded.
    for py in sorted(_PIPELINE_DIR.rglob("*.py")):
        if py.name in _ALLOWLIST:
            continue
        exempt = _CAPABILITY_EXEMPT.get(py.name, ())
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        violations.extend(_collect_violations(tree, py.name, exempt))
    assert not violations, "pipeline modules must do zero I/O imports:\n" + "\n".join(violations)


def test_capability_scope_still_blocks_io_in_seeding() -> None:
    """The seeding/admission exemption is capability-scoped, not module-wide.

    A synthetic seeding.py that imports subprocess/os alongside its sanctioned
    read seams must still be flagged for the I/O modules — only imports under
    the ``_THIN_FETCH_PREFIXES`` capability prefixes escape the guard.
    """
    synthetic_source = (
        "import subprocess\n"
        "import os\n"
        "from hephaestus.automation.github_api import fetch_issue_info\n"
        "from hephaestus.automation._review_utils import find_pr_for_issue\n"
        "from hephaestus.automation.state_labels import is_epic\n"
        "from hephaestus.automation.dependency_resolver import DependencyResolver\n"
        "import hephaestus.utils.helpers\n"  # NOT a sanctioned seam — must trip
    )
    tree = ast.parse(synthetic_source, filename="<synthetic-seeding>")
    violations = _collect_violations(tree, "seeding.py", _CAPABILITY_EXEMPT["seeding.py"])

    assert any("import subprocess" in v for v in violations)
    assert any("import os" in v for v in violations)
    assert any("hephaestus.utils.helpers" in v for v in violations)
    assert not any("github_api" in v for v in violations)
    assert not any("_review_utils" in v for v in violations)
    assert not any("state_labels" in v for v in violations)
    assert not any("dependency_resolver" in v for v in violations)


def test_forbidden_detects_synthetic_forbidden_import() -> None:
    """Negative test: the guard must actually flag forbidden imports.

    Without this test, a broken `_forbidden()` that always returns False
    would let `test_pipeline_modules_have_zero_io_imports` pass vacuously
    with an empty `violations` list. Here we parse synthetic source
    containing known-forbidden imports (stdlib module, forbidden prefix,
    and a from-import) through the same collector (no exemptions) and
    assert each is caught, plus that an allowed import is not.
    """
    synthetic_source = (
        "import subprocess\n"
        "import hephaestus.automation.git_utils\n"
        "from os import path\n"
        "import json\n"  # allowed stdlib module; must NOT be flagged
    )
    tree = ast.parse(synthetic_source, filename="<synthetic>")
    violations = _collect_violations(tree, "<synthetic>", ())

    assert any("import subprocess" in v for v in violations)
    assert any("import hephaestus.automation.git_utils" in v for v in violations)
    assert any("from os import ..." in v for v in violations)
    assert not any("json" in v for v in violations)


def test_forbidden_direct_cases() -> None:
    """Directly exercise `_forbidden()` for both branches of its predicate.

    Covers: a bare forbidden stdlib module, a submodule of a forbidden
    stdlib module (root-splitting), a forbidden dotted prefix, and an
    allowed module that must return False.
    """
    assert _forbidden("subprocess") is True
    assert _forbidden("os.path") is True
    assert _forbidden("hephaestus.automation.claude_invoke") is True
    assert _forbidden("hephaestus.automation.claude_invoke.helpers") is True
    assert _forbidden("json") is False
    assert _forbidden("hephaestus.automation.pipeline") is False

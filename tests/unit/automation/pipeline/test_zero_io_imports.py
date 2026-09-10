"""Guard that pipeline modules have zero I/O imports (ast-based, not text-scan)."""

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

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
# These closed worker-side modules execute I/O. The main pool owns general
# jobs. The auxiliary pool owns host learning and cleanup only. git_cleanup
# is the shared low-level implementation of its two accepted Git operations.
_ALLOWLIST = frozenset(
    {
        "auxiliary_worker_pool.py",
        "codex_worktree_boundary.py",
        "git_cleanup.py",
        "worker_pool.py",
    }
)

# The queue uses its repository adapter for reads. Only the existing pure
# dependency parser and named error classes need direct imports here.
# Each exception names one module and its permitted symbols. A module import
# or any other symbol remains forbidden.
_CAPABILITY_EXEMPT: dict[str, dict[str, frozenset[str]]] = {
    "admission.py": {"subprocess": frozenset({"SubprocessError"})},
    "coordinator_sources.py": {
        "hephaestus.automation.github_api.issues": frozenset({"parse_issue_dependencies"}),
    },
    "plan_review.py": {"subprocess": frozenset({"SubprocessError"})},
}


def _forbidden(name: str) -> bool:
    """Check if a module name is forbidden."""
    root = name.split(".")[0]
    return root in _FORBIDDEN_MODULES or name.startswith(_FORBIDDEN_PREFIXES)


def _import_violations(node: ast.Import, filename: str) -> list[str]:
    """Reject each direct import of a forbidden module."""
    return [
        f"{filename}:{node.lineno}: import {alias.name}"
        for alias in node.names
        if _forbidden(alias.name)
    ]


def _import_from_violations(
    node: ast.ImportFrom, filename: str, exempt: dict[str, frozenset[str]]
) -> list[str]:
    """Violations for a ``from X import Y`` statement (symbol scoping applies)."""
    mod = node.module or ""
    if not _forbidden(mod):
        return []
    if mod not in exempt:
        return [f"{filename}:{node.lineno}: from {mod} import ..."]
    allowed = exempt[mod]
    return [
        f"{filename}:{node.lineno}: from {mod} import {alias.name} "
        f"(symbol not in allowed set {sorted(allowed)})"
        for alias in node.names
        if alias.name not in allowed
    ]


def _collect_violations(
    tree: ast.AST, filename: str, exempt: dict[str, frozenset[str]]
) -> list[str]:
    """Return forbidden imports except for the specified module and symbol pairs.

    Direct imports of forbidden modules remain violations. Each permitted
    from-import must name the exact module and one allowed symbol.
    """
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            violations.extend(_import_violations(node, filename))
        elif isinstance(node, ast.ImportFrom):
            violations.extend(_import_from_violations(node, filename, exempt))
    return violations


def test_pipeline_modules_have_zero_io_imports() -> None:
    """Verify pipeline modules do not import I/O-related modules.

    Uses AST parsing to detect imports anywhere in the module (including
    inside function bodies), not just at the top level. This catches
    conditional and lazy imports that a text-scan would miss.

    The listed worker modules execute I/O. Other exceptions permit only
    named error classes and the existing pure dependency parser.
    """
    violations: list[str] = []
    # rglob so future pipeline/ subpackages (e.g. stages/) stay guarded.
    for py in sorted(_PIPELINE_DIR.rglob("*.py")):
        if py.name in _ALLOWLIST:
            continue
        exempt = _CAPABILITY_EXEMPT.get(py.name, {})
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        violations.extend(_collect_violations(tree, py.name, exempt))
    assert not violations, "pipeline modules must do zero I/O imports:\n" + "\n".join(violations)


def test_pipeline_package_import_stays_lazy() -> None:
    """Importing the package must not eagerly load coordinator I/O dependencies."""
    probe = (
        "import json, sys\n"
        "import hephaestus.automation.pipeline\n"
        "watched = [\n"
        "  'hephaestus.automation.pipeline.coordinator',\n"
        "  'hephaestus.automation.github_api',\n"
        "  'hephaestus.automation.claude_invoke',\n"
        "]\n"
        "print(json.dumps([name for name in watched if name in sys.modules]))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        check=True,
        text=True,
        capture_output=True,
    )
    assert json.loads(result.stdout) == []


def test_seeding_cannot_import_direct_io_helpers() -> None:
    """Seeding must use its repository adapter for external reads."""
    synthetic_source = (
        "import subprocess\n"
        "import os\n"
        "from hephaestus.automation.github_api import fetch_issue_info\n"
        "from hephaestus.automation.state_labels import is_epic\n"
        "from hephaestus.automation.dependency_resolver import DependencyResolver\n"
        "import hephaestus.utils.helpers\n"
    )
    tree = ast.parse(synthetic_source, filename="<synthetic-seeding>")
    violations = _collect_violations(tree, "seeding.py", _CAPABILITY_EXEMPT.get("seeding.py", {}))

    assert any("import subprocess" in v for v in violations)
    assert any("import os" in v for v in violations)
    assert any("hephaestus.utils.helpers" in v for v in violations)
    assert any("github_api" in v for v in violations)
    assert not any("state_labels" in v for v in violations)
    assert not any("dependency_resolver" in v for v in violations)


@pytest.mark.parametrize("filename", ["admission.py", "plan_review.py"])
def test_error_class_import_does_not_allow_process_execution(filename: str) -> None:
    """Permit the error class without permitting process runners."""
    tree = ast.parse(
        "from subprocess import SubprocessError\n"
        "import subprocess\n"
        "from subprocess import run, Popen\n"
        "from subprocess import *\n"
        "from hephaestus.automation.github_api import gh_call\n"
    )
    violations = _collect_violations(tree, filename, _CAPABILITY_EXEMPT[filename])

    assert len(violations) == 5
    assert not any("import SubprocessError" in violation for violation in violations)
    assert any("import subprocess" in violation for violation in violations)
    assert any("import run " in violation for violation in violations)
    assert any("import Popen " in violation for violation in violations)
    assert any("import * " in violation for violation in violations)
    assert any("github_api" in violation for violation in violations)


def test_dependency_parser_import_does_not_allow_github_io() -> None:
    """Permit dependency parsing without permitting GitHub readers or runners."""
    tree = ast.parse(
        "from hephaestus.automation.github_api.issues import parse_issue_dependencies\n"
        "import hephaestus.automation.github_api.issues\n"
        "from hephaestus.automation.github_api.issues import fetch_issue_info\n"
        "from hephaestus.automation.github_api.issues import *\n"
        "from hephaestus.automation.github_api import gh_call\n"
    )
    violations = _collect_violations(
        tree, "coordinator_sources.py", _CAPABILITY_EXEMPT["coordinator_sources.py"]
    )

    assert len(violations) == 4
    assert not any("import parse_issue_dependencies" in violation for violation in violations)
    assert any("import hephaestus.automation.github_api.issues" in v for v in violations)
    assert any("import fetch_issue_info " in violation for violation in violations)
    assert any("import * " in violation for violation in violations)
    assert any("from hephaestus.automation.github_api import" in v for v in violations)


def test_plan_review_rejects_direct_agent_runner_imports() -> None:
    """Pipeline stages cannot import the direct agent runner."""
    synthetic_source = (
        "from hephaestus.automation.claude_invoke import raise_for_error_envelope\n"
        "from hephaestus.automation.claude_invoke import invoke_claude_with_session\n"
        "import hephaestus.automation.claude_invoke\n"
    )
    tree = ast.parse(synthetic_source, filename="<synthetic-plan-review>")
    violations = _collect_violations(tree, "plan_review.py", {})

    assert len(violations) == 3, violations
    assert any(
        v.startswith("plan_review.py:1: from hephaestus.automation.claude_invoke")
        for v in violations
    )
    assert any(
        v.startswith("plan_review.py:2: from hephaestus.automation.claude_invoke")
        for v in violations
    )
    # A bare module import exposes the whole surface: always a violation
    # under a symbol-scoped exemption.
    assert any(
        v.startswith("plan_review.py:3: import hephaestus.automation.claude_invoke")
        for v in violations
    )


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
    violations = _collect_violations(tree, "<synthetic>", {})

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

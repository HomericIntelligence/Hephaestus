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

# Modules exempt from the zero-I/O guard.
# worker_pool.py is the ONLY place that executes jobs (agent, git, build/test),
# so it must import I/O modules; all other workers offload to it.
_ALLOWLIST = frozenset({"worker_pool.py"})


def _forbidden(name: str) -> bool:
    """Check if a module name is forbidden."""
    root = name.split(".")[0]
    return root in _FORBIDDEN_MODULES or name.startswith(_FORBIDDEN_PREFIXES)


def test_pipeline_modules_have_zero_io_imports() -> None:
    """Verify pipeline modules do not import I/O-related modules.

    Uses AST parsing to detect imports anywhere in the module (including
    inside function bodies), not just at the top level. This catches
    conditional and lazy imports that a text-scan would miss.

    Exceptions: worker_pool.py is the only place that executes jobs
    (agent, git, build/test), so it must import I/O modules.
    """
    violations: list[str] = []
    # rglob so future pipeline/ subpackages (e.g. stages/) stay guarded.
    for py in sorted(_PIPELINE_DIR.rglob("*.py")):
        if py.name in _ALLOWLIST:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _forbidden(alias.name):
                        violations.append(f"{py.name}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if _forbidden(mod):
                    violations.append(f"{py.name}:{node.lineno}: from {mod} import ...")
    assert not violations, "pipeline modules must do zero I/O imports:\n" + "\n".join(violations)


def test_forbidden_detects_synthetic_forbidden_import() -> None:
    """Negative test: the guard must actually flag forbidden imports.

    Without this test, a broken `_forbidden()` that always returns False
    would let `test_pipeline_modules_have_zero_io_imports` pass vacuously
    with an empty `violations` list. Here we parse synthetic source
    containing known-forbidden imports (stdlib module, forbidden prefix,
    and a from-import) through the same AST walk and assert each is
    caught, plus that an allowed import is not.
    """
    synthetic_source = (
        "import subprocess\n"
        "import hephaestus.automation.git_utils\n"
        "from os import path\n"
        "import json\n"  # allowed stdlib module; must NOT be flagged
    )
    tree = ast.parse(synthetic_source, filename="<synthetic>")

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _forbidden(alias.name):
                    violations.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if _forbidden(mod):
                violations.append(f"from {mod} import ...")

    assert "import subprocess" in violations
    assert "import hephaestus.automation.git_utils" in violations
    assert "from os import ..." in violations
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

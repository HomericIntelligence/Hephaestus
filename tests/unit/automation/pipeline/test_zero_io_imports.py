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


def _forbidden(name: str) -> bool:
    """Check if a module name is forbidden."""
    root = name.split(".")[0]
    return root in _FORBIDDEN_MODULES or name.startswith(_FORBIDDEN_PREFIXES)


def test_pipeline_modules_have_zero_io_imports() -> None:
    """Verify pipeline modules do not import I/O-related modules.

    Uses AST parsing to detect imports anywhere in the module (including
    inside function bodies), not just at the top level. This catches
    conditional and lazy imports that a text-scan would miss.
    """
    violations: list[str] = []
    # rglob so future pipeline/ subpackages (e.g. stages/) stay guarded.
    for py in sorted(_PIPELINE_DIR.rglob("*.py")):
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

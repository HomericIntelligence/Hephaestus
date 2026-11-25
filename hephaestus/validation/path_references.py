"""Validate dotted module references against the repository tree.

Many tests and tools refer to a module with a dotted string: unittest.mock
patch targets, monkeypatch.setattr targets, importlib.import_module
arguments, module-path string constants such as the environment-registry
readers, [project.scripts] entry points, and the pre-commit files: patterns
that name each subpackage. A string that points at a renamed or deleted
module fails silently or late, so this guard AST-walks the scanned sources
and resolves every such reference against the repository tree. The scanned
sources are never imported.

Mock-style references (patch, patch.object, monkeypatch.setattr, and the
environment-registry readers) treat the dotted part as module followed by
an attribute tail: the module is the longest importable prefix and the rest
resolves as attributes. When only the top-level package resolves
(``hephaestus.missing_mod.target``), the module cannot exist and the guard
reports a violation. import_module and console-script targets name the
module exactly, so those references must map to a real source file.

Constants occasionally carry a dotted name that is not a module at all: an
entry-point group installed by an external package, a JSON key, a topic
prefix. Such a constant carries the ``# path-reference-exempt`` marker
comment, on its own line above the constant or on the constant's own line,
and the guard skips it. The marker text names the contract the name
belongs to, so the triage stays visible next to the constant.

Usage::

    hephaestus-check-path-references
    hephaestus-check-path-references --json
"""

from __future__ import annotations

import ast
import json
import re
import sys
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from hephaestus.cli.localization import text
from hephaestus.cli.utils import create_validation_parser, resolve_repo_root
from hephaestus.errors import boundary_main

# This module lives at hephaestus/validation/, so the repo root is two parents up.
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]
_REPO_PREFIXES = ("hephaestus", "tests", "scripts")
_PY_PROJECT = "pyproject.toml"
_PRE_COMMIT_CONFIG = ".pre-commit-config.yaml"
_SCRIPTS_TABLE = ("project", "scripts")
# A source-marker comment on a dotted constant line: the constant is a
# declared non-module name (an entry-point group, a JSON key, a topic
# prefix). The guard skips marked constants so external contracts such as
# the isolation-adapter entry-point groups stay intact.
_EXEMPT_MARKER = "path-reference-exempt"


@dataclass(frozen=True)
class PathReferenceFinding:
    """A single unresolved module-reference violation."""

    # kind values:
    # "patch-target" | "module-attribute" | "module-string" |
    # "console-script" | "hook-coverage"
    kind: str
    detail: str


def _dotted_prefix(dotted: str) -> str:
    """Return the top-level repository prefix of *dotted*."""
    return dotted.split(".", 1)[0]


def dotted_to_file(repo_root: Path, dotted: str) -> Path | None:
    """Map a dotted module name to a repository source file exactly.

    Returns None when *dotted* is outside the repository or does not map to
    an existing ``<module>.py`` or ``<package>/__init__.py`` file.
    """
    parts = dotted.split(".")
    if not parts or parts[0] not in _REPO_PREFIXES:
        return None
    module_file = repo_root.joinpath(*parts[:-1], f"{parts[-1]}.py")
    if module_file.is_file():
        return module_file
    package_file = repo_root.joinpath(*parts) / "__init__.py"
    if package_file.is_file():
        return package_file
    return None


def resolve_module(repo_root: Path, dotted: str) -> str | None:
    """Return the longest prefix of *dotted* that maps to a source file.

    Reference strings carry attribute tails (``patch("pkg.mod.Class.method")``
    or a registry reader such as ``"hephaestus.config.child_environments.reader"``),
    so the module is the longest mappable prefix, never the whole string.
    Returns None when the string is external or no prefix maps to a file.
    """
    if dotted.split(".")[0] not in _REPO_PREFIXES:
        return None
    parts: list[str] = []
    resolved: str | None = None
    for part in dotted.split("."):
        parts.append(part)
        if dotted_to_file(repo_root, ".".join(parts)) is not None:
            resolved = ".".join(parts)
    return resolved


def module_cannot_exist(repo_root: Path, dotted: str) -> bool:
    """Return True when only the top-level prefix of *dotted* resolves.

    For mock-style references the module is the longest importable prefix.
    When that prefix is just the repository root (``hephaestus.missing_mod.target``
    resolves to ``hephaestus``), the module itself is absent and the
    reference cannot hold.
    """
    resolved = resolve_module(repo_root, dotted)
    if resolved is None:
        return True
    return resolved == _dotted_prefix(dotted)


def _has_top_level_attr(py_file: Path, name: str) -> bool:
    """Return True when *name* is defined at the top level of *py_file*."""
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return True
        elif isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                return True
        elif isinstance(node, ast.ImportFrom):
            # A re-export such as ``from .cli import main`` still binds the
            # name at package top level.
            if any(alias.name == name for alias in node.names):
                return True
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            return True
    return False


def _is_patch_call(func: ast.expr) -> bool:
    """Return True for patch or patch.object call functions."""
    if isinstance(func, ast.Name):
        return func.id == "patch"
    if isinstance(func, ast.Attribute):
        return func.attr == "patch" or (func.attr == "object" and _is_patch_call(func.value))
    return False


def _is_monkeypatch_setattr_call(func: ast.expr) -> bool:
    """Return True for monkeypatch.setattr call functions."""
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "setattr"
        and isinstance(func.value, ast.Name)
        and func.value.id == "monkeypatch"
    )


def _is_mock_call(func: ast.expr) -> bool:
    """Return True for patch, patch.object, and monkeypatch.setattr calls."""
    return _is_patch_call(func) or _is_monkeypatch_setattr_call(func)


def _is_import_module_call(func: ast.expr) -> bool:
    """Return True for import_module call functions."""
    return (isinstance(func, ast.Name) and func.id == "import_module") or (
        isinstance(func, ast.Attribute) and func.attr == "import_module"
    )


def _first_arg_strings(path: Path, predicate: Callable[[ast.expr], bool]) -> list[tuple[int, str]]:
    """Yield ``(line number, first-arg string)`` for call sites that match."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    report: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and predicate(node.func)
        ):
            report.append((node.lineno, node.args[0].value))
    return report


def _repo_string_constants(path: Path) -> list[tuple[int, str]]:
    """Yield repo-prefixed string constants, excluding the call targets above."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    excluded = {
        hit
        for predicate in (_is_mock_call, _is_import_module_call)
        for hit in _first_arg_strings(path, predicate)
    }
    report: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        value = node.value
        parts = value.split(".")
        if len(parts) > 1 and parts[0] in _REPO_PREFIXES and (node.lineno, value) not in excluded:
            report.append((node.lineno, value))
    return report


def _console_script_entries(repo_root: Path) -> list[str]:
    """Yield ``"module:attr"`` strings from the [project.scripts] table."""
    project = repo_root / _PY_PROJECT
    if not project.is_file():
        return []
    table = tomllib.loads(project.read_text(encoding="utf-8"))
    scripts = table.get(_SCRIPTS_TABLE[0], {}).get(_SCRIPTS_TABLE[1], {})
    return [entry for entry in scripts.values() if isinstance(entry, str) and ":" in entry]


def _console_script_violation(repo_root: Path, entry: str) -> str | None:
    """Return the violation string when a script entry does not resolve."""
    module, _, attr = entry.partition(":")
    module_file = dotted_to_file(repo_root, module)
    if module_file is None:
        return f"console script target module {module} has no source file"
    if not _has_top_level_attr(module_file, attr):
        return f"console script target {attr} is not defined in {module}"
    return None


def _pre_commit_files_patterns(repo_root: Path) -> list[re.Pattern[str]]:
    """Return compiled regex values of every ``files:`` line in the config."""
    config = repo_root / _PRE_COMMIT_CONFIG
    patterns: list[re.Pattern[str]] = []
    if not config.is_file():
        return patterns
    for line in config.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("files:"):
            value = stripped[len("files:") :].strip().strip("\"'")
            try:
                patterns.append(re.compile(value))
            except re.error:
                patterns.append(re.compile(re.escape(value)))
    return patterns


def _source_subpackages(repo_root: Path) -> list[str]:
    """Return public subpackage names of the hephaestus package."""
    package_root = repo_root / "hephaestus"
    if not package_root.is_dir():
        return []
    return sorted(
        path.name
        for path in package_root.iterdir()
        if path.is_dir()
        and not path.name.startswith((".", "_"))
        and (path / "__init__.py").is_file()
    )


def _hook_coverage_findings(repo_root: Path) -> list[PathReferenceFinding]:
    """Return a finding for each subpackage a pre-commit files: pattern misses."""
    patterns = _pre_commit_files_patterns(repo_root)
    return [
        PathReferenceFinding(
            "hook-coverage",
            f"hephaestus/{subpackage} has no covering pre-commit files: pattern",
        )
        for subpackage in _source_subpackages(repo_root)
        if not any(pattern.search(f"hephaestus/{subpackage}/mod.py") for pattern in patterns)
    ]


def _scan_call_strings(repo_root: Path, path: Path, relative: str) -> list[PathReferenceFinding]:
    """Return findings for mock and import_module string arguments.

    The wrong-prefix family (``patch("hephaestus.missing_mod.target")``)
    reports only when no module prefix beyond the top-level package resolves.
    An ``import_module`` literal names the module exactly, so it must map to
    a real source file.
    """
    findings: list[PathReferenceFinding] = []
    for lineno, target in _first_arg_strings(path, _is_mock_call):
        if _dotted_prefix(target) in _REPO_PREFIXES and module_cannot_exist(repo_root, target):
            findings.append(PathReferenceFinding("patch-target", f"{relative}:{lineno}: {target}"))
    for lineno, target in _first_arg_strings(path, _is_import_module_call):
        if _dotted_prefix(target) in _REPO_PREFIXES and dotted_to_file(repo_root, target) is None:
            findings.append(
                PathReferenceFinding("module-attribute", f"{relative}:{lineno}: {target}")
            )
    return findings


def _scan_source_strings(repo_root: Path) -> list[PathReferenceFinding]:
    """Return module-string findings for the hephaestus source tree only.

    Test files carry documentation strings that happen to start with a
    repository prefix, so the constant scan does not run there. A dotted
    constant with a ``# path-reference-exempt`` marker comment on its own
    line or on the line above is a declared non-module name and is skipped.
    """
    findings: list[PathReferenceFinding] = []
    source_root = repo_root / "hephaestus"
    if not source_root.is_dir():
        return findings
    for path in sorted(source_root.rglob("*.py")):
        relative = str(path.relative_to(repo_root))
        source_lines = path.read_text(encoding="utf-8").splitlines()
        exempt_lines = {
            lineno for lineno, line in enumerate(source_lines, start=1) if _EXEMPT_MARKER in line
        }
        for lineno, literal in _repo_string_constants(path):
            if (lineno - 1) in exempt_lines or lineno in exempt_lines:
                continue
            if module_cannot_exist(repo_root, literal):
                findings.append(
                    PathReferenceFinding("module-string", f"{relative}:{lineno}: {literal}")
                )
    return findings


def find_violations(repo_root: Path) -> list[PathReferenceFinding]:
    """Return one finding per unresolved dotted module reference.

    Checks patch/object and import_module string arguments in ``tests/``
    and ``hephaestus/``; scans repo-prefixed registry-style string constants
    in ``hephaestus/`` only, because test files carry documentation strings
    that happen to start with a repository prefix. Validates every
    ``[project.scripts]`` entry point, and checks that every subpackage is
    named by at least one pre-commit ``files:`` pattern.

    References whose first segment is not a repository prefix (``yaml``,
    ``builtins``, third-party names) are ignored: the guard only models
    what the repository itself provides.

    Args:
        repo_root: Repository root holding ``tests``, ``hephaestus``,
            ``pyproject.toml``, and ``.pre-commit-config.yaml``.

    Returns:
        Sorted findings. An empty list means every checked reference resolves.

    """
    findings: set[PathReferenceFinding] = set()
    for root in (repo_root / "tests", repo_root / "hephaestus"):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            findings.update(_scan_call_strings(repo_root, path, str(path.relative_to(repo_root))))
    findings.update(_scan_source_strings(repo_root))
    for entry in _console_script_entries(repo_root):
        violation = _console_script_violation(repo_root, entry)
        if violation is not None:
            findings.add(PathReferenceFinding("console-script", violation))
    findings.update(_hook_coverage_findings(repo_root))
    return sorted(findings, key=lambda finding: (finding.kind, finding.detail))


def format_report(findings: list[PathReferenceFinding]) -> str:
    """Render *findings* as a human-readable report."""
    if not findings:
        return text("OK: all repository module references resolve.")
    lines = [text("FAIL: %(count)d path-reference violation(s):", count=len(findings))]
    lines.extend(
        text("  [%(kind)s] %(detail)s", kind=finding.kind, detail=finding.detail)
        for finding in findings
    )
    return "\n".join(lines)


def format_json(findings: list[PathReferenceFinding]) -> str:
    """Render *findings* as a JSON string."""
    return json.dumps({"violations": [asdict(finding) for finding in findings]}, indent=2)


def main(argv: list[str] | None = None) -> int:
    """Run hephaestus-check-path-references through the shared console-script boundary.

    The boundary reports a failure with one exit code and one stderr line.
    A usage error still exits 2 from argparse.
    """
    return boundary_main(_run, argv)


def _run(argv: list[str] | None = None) -> int:
    """Entry point for ``hephaestus-check-path-references``."""
    parser = create_validation_parser(__doc__, prog="hephaestus-check-path-references")
    args = parser.parse_args(argv)
    repo_root = resolve_repo_root(args)
    findings = find_violations(repo_root)
    print(format_json(findings) if args.json else format_report(findings))
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())

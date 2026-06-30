"""Structural guard against bypassing the shared GitHub CLI adapter."""

from __future__ import annotations

import ast
import shlex
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
# Explicit allowlist of library modules that MUST route gh through gh_call —
# NOT the whole github/ package. tidy.py (interactive ``gh tidy``) and
# rate_limit.py (the rate-limit reset probe, which cannot re-enter gh_call while
# classifying a rate-limit error) are the documented sanctioned exceptions; see
# the hephaestus/github/client.py docstring. Adding them here would wrongly fail
# CI. severity_label.py is covered here per issue #1456.
_TARGETS = (
    _REPO_ROOT / "hephaestus" / "github" / "fleet_sync.py",
    _REPO_ROOT / "hephaestus" / "github" / "severity_label.py",
)
_RUNNERS = {"run", "Popen", "check_output", "check_call"}


def _subprocess_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    aliases[alias.asname or alias.name] = "subprocess"
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in _RUNNERS:
                    aliases[alias.asname or alias.name] = f"subprocess.{alias.name}"
    return aliases


def _assignments(tree: ast.AST) -> dict[str, ast.expr]:
    assignments: dict[str, ast.expr] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            assignments[node.target.id] = node.value
    return assignments


def _call_name(func: ast.expr, aliases: dict[str, str]) -> str | None:
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and aliases.get(func.value.id) == "subprocess"
        and func.attr in _RUNNERS
    ):
        return f"subprocess.{func.attr}"
    if isinstance(func, ast.Name):
        alias = aliases.get(func.id)
        if alias in {f"subprocess.{runner}" for runner in _RUNNERS}:
            return alias
    return None


def _literal_strings(node: ast.expr, assignments: dict[str, ast.expr]) -> list[str] | None:
    if isinstance(node, ast.Name):
        value = assignments.get(node.id)
        return _literal_strings(value, assignments) if value is not None else None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.List, ast.Tuple)):
        values: list[str] = []
        for element in node.elts:
            strings = _literal_strings(element, assignments)
            if strings is None:
                return None
            values.extend(strings)
        return values
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_strings(node.left, assignments)
        right = _literal_strings(node.right, assignments)
        if left is None or right is None:
            return None
        return [*left, *right]
    return None


def _first_executables(node: ast.expr, assignments: dict[str, ast.expr]) -> set[str]:
    strings = _literal_strings(node, assignments)
    if not strings:
        return set()

    first = strings[0]
    try:
        shell_words = shlex.split(first)
    except ValueError:
        shell_words = []
    return {shell_words[0] if shell_words else first}


def _has_shell_true(node: ast.Call) -> bool:
    return any(
        kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True
        for kw in node.keywords
    )


def _raw_gh_subprocess_violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    aliases = _subprocess_aliases(tree)
    assignments = _assignments(tree)
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call = _call_name(node.func, aliases)
        if call is None or not node.args:
            continue

        executables = _first_executables(node.args[0], assignments)
        shell_true = _has_shell_true(node)
        if "gh" in executables:
            violations.append(f"{path}:{node.lineno}: raw gh subprocess via {call}")
        elif shell_true and not executables:
            violations.append(f"{path}:{node.lineno}: unresolved shell=True subprocess")

    return violations


def test_target_modules_do_not_run_gh_via_subprocess() -> None:
    """fleet_sync and severity_label must use gh_call instead of raw subprocess."""
    violations: list[str] = []
    for path in _TARGETS:
        violations.extend(_raw_gh_subprocess_violations(path))
    assert violations == []


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess\nsubprocess.run(['gh', 'pr', 'list'])\n",
        "import subprocess as sp\nsp.Popen(('gh', 'api'))\n",
        "from subprocess import run\nrun(['gh'] + ['pr', 'list'])\n",
        "from subprocess import check_output as co\ncmd = ['gh', 'api']\nco(cmd)\n",
        "import subprocess\ncmd: list[str] = ['gh']\nsubprocess.check_call(cmd)\n",
        "import subprocess\nsubprocess.run('gh api repos/o/r', shell=True)\n",
    ],
)
def test_detector_finds_raw_gh_subprocess_forms(tmp_path: Path, source: str) -> None:
    """The structural guard covers aliases, command variables, and shell strings."""
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")

    assert _raw_gh_subprocess_violations(path)


def test_detector_allows_non_gh_subprocess(tmp_path: Path) -> None:
    """Non-GitHub subprocess calls remain valid for git/gpg operations."""
    path = tmp_path / "sample.py"
    path.write_text(
        "import subprocess\nsubprocess.run(['git', 'status'], check=True)\n",
        encoding="utf-8",
    )

    assert _raw_gh_subprocess_violations(path) == []

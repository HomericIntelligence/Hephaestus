"""Structural guard against bypassing the shared git adapter."""

from __future__ import annotations

import ast
import shlex
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TARGETS = (
    *_REPO_ROOT.joinpath("hephaestus", "github", "fleet_sync").glob("*.py"),
    _REPO_ROOT / "hephaestus" / "github" / "pr_merge.py",
    _REPO_ROOT / "hephaestus" / "github" / "tidy.py",
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


def _raw_git_subprocess_violations(path: Path) -> list[str]:
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
        if "git" in executables:
            violations.append(f"{path}:{node.lineno}: raw git subprocess via {call}")

    return violations


def test_github_modules_do_not_run_git_via_raw_subprocess() -> None:
    """GitHub modules must use the shared git adapter for git commands."""
    violations: list[str] = []
    for path in _TARGETS:
        violations.extend(_raw_git_subprocess_violations(path))
    assert violations == []


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess\nsubprocess.run(['git', 'status'])\n",
        "import subprocess as sp\nsp.Popen(('git', 'status'))\n",
        "from subprocess import run\nrun(['git'] + ['status'])\n",
        "from subprocess import check_output as co\ncmd = ['git', 'status']\nco(cmd)\n",
        "import subprocess\nsubprocess.run('git status', shell=True)\n",
    ],
)
def test_detector_finds_raw_git_subprocess_forms(tmp_path: Path, source: str) -> None:
    """The structural guard covers aliases, command variables, and shell strings."""
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")

    assert _raw_git_subprocess_violations(path)


def test_detector_allows_non_git_subprocess(tmp_path: Path) -> None:
    """Non-git subprocess calls, such as gpg, remain valid."""
    path = tmp_path / "sample.py"
    path.write_text(
        "import subprocess\nsubprocess.run(['gpg', '--list-keys'], check=False)\n",
        encoding="utf-8",
    )

    assert _raw_git_subprocess_violations(path) == []

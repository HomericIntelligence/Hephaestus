"""Guard rails for scripts that manipulate Git metadata."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCAN_PATHS = (
    ".github",
    "docs",
    "hephaestus",
    "scripts",
    "skills",
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "justfile",
)

FORBIDDEN_PATTERNS = {
    "sets-core-bare": re.compile(
        r"\bgit\s+config\b[^\n]*\bcore[.]bare\b[^\n]*(?:true|1|yes|on)\b",
        re.IGNORECASE,
    ),
    "uses-explicit-git-dir": re.compile(
        r'(?:\bgit\s+--git-dir(?:=|\s)|\["git",\s*"--git-dir")',
        re.IGNORECASE,
    ),
    "sets-git-dir-env": re.compile(
        r"(?:^[ \t]*|[;&|][ \t]*|\benv[ \t]+)(?:export[ \t]+)?GIT_DIR[ \t]*=",
        re.MULTILINE,
    ),
    "sets-git-work-tree-env": re.compile(
        r"(?:^[ \t]*|[;&|][ \t]*|\benv[ \t]+)(?:export[ \t]+)?GIT_WORK_TREE[ \t]*=",
        re.MULTILINE,
    ),
}


@pytest.mark.parametrize(
    ("rule", "snippet"),
    [
        ("sets-git-dir-env", "GIT_DIR=.git command"),
        ("sets-git-dir-env", "    export GIT_DIR=.git"),
        ("sets-git-dir-env", "command && GIT_DIR=.git next"),
        ("sets-git-dir-env", "env GIT_DIR=.git command"),
        ("sets-git-work-tree-env", "GIT_WORK_TREE=. command"),
        ("sets-git-work-tree-env", "    export GIT_WORK_TREE=."),
        ("sets-git-work-tree-env", "command; GIT_WORK_TREE=. next"),
        ("sets-git-work-tree-env", "env GIT_WORK_TREE=. command"),
    ],
)
def test_git_metadata_env_patterns_match_indented_assignments(
    rule: str,
    snippet: str,
) -> None:
    """Forbidden Git metadata env assignments are caught in common shell forms."""
    assert FORBIDDEN_PATTERNS[rule].search(snippet) is not None


def _tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", *SCAN_PATHS],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    return [REPO_ROOT / path.decode() for path in result.stdout.split(b"\0") if path]


def test_project_scripts_do_not_flip_repository_to_bare_mode() -> None:
    """Checked-in automation must not rewrite a normal checkout as bare."""
    violations: list[str] = []
    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        for rule, pattern in FORBIDDEN_PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                rel_path = path.relative_to(REPO_ROOT)
                violations.append(f"{rel_path}:{line}: {rule}: {match.group(0)!r}")

    assert violations == []

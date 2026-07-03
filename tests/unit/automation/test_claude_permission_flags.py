"""Regression guard for Claude permission bypass flags."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).parents[3]
AUTOMATION_DIR = REPO_ROOT / "hephaestus" / "automation"
DANGEROUS_PERMISSION_FLAG = "--dangerously-skip-permissions"


def test_automation_sources_do_not_pass_dangerously_skip_permissions() -> None:
    """Verify automation source does not bypass Claude permission prompts."""
    offenders: list[str] = []
    for path in sorted(AUTOMATION_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == DANGEROUS_PERMISSION_FLAG:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")

    assert not offenders, "Claude automation must not bypass permission prompts: " + ", ".join(
        offenders
    )

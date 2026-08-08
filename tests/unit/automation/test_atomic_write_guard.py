"""Guard automation state/log writes against raw Path.write_text()."""

from __future__ import annotations

import ast
from pathlib import Path


def _write_text_calls(root: Path) -> list[str]:
    """Return Python call sites that invoke an attribute named write_text."""
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "write_text"
            ):
                offenders.append(f"{path}:{node.lineno}")
    return offenders


def test_no_raw_write_text_calls_remain_in_automation() -> None:
    """Automation state, prompt, output, and log files use atomic write_secure."""
    offenders = _write_text_calls(Path("hephaestus/automation"))

    assert offenders == []

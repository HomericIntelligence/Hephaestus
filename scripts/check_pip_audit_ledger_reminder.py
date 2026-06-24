#!/usr/bin/env python3
"""Enforce that every pip-audit suppression carries a re-review trigger.

The pip-audit ledger in ``pixi.toml`` suppresses individual CVEs via
``--ignore-vuln <ID>``. Each suppression MUST be documented in the contiguous
comment region directly above the ``pip-audit =`` task line, with a
``Re-review:`` trigger so it is never silently permanent (issue #1550).

The region is the maximal run of comment lines (including bare ``#``
separators) ending on the line directly above ``pip-audit = "..."``. A
suppression is documented when its vuln ID appears in the region AND a
``Re-review:`` line appears at or after the ID's first mention. A multi-line
(triple-quoted) task value the single-line parser cannot read fails closed.

Usage:
    python scripts/check_pip_audit_ledger_reminder.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

IGNORE_VULN_RE = re.compile(r"--ignore-vuln\s+(\S+)")
# Match only task-definition lines (value starts with "pip-audit"), not dep-version lines.
PIP_AUDIT_TASK_RE = re.compile(r'^\s*pip-audit\s*=\s*"(?P<value>pip-audit[^"]*)"\s*$')


def get_repo_root() -> Path:
    """Return the repository root by walking up to the nearest ``pyproject.toml``."""
    path = Path(__file__).resolve().parent
    while path != path.parent:
        if (path / "pyproject.toml").exists():
            return path
        path = path.parent
    return Path(__file__).resolve().parent.parent


def _ledger_region(lines: list[str], task_index: int) -> list[str]:
    """Return the contiguous comment block ending directly above ``task_index``.

    Includes bare ``#`` separator lines so a blank-comment line inside the
    ledger does not truncate the region (the source of the prior false-positive).
    """
    region: list[str] = []
    i = task_index - 1
    while i >= 0 and lines[i].lstrip().startswith("#"):
        region.append(lines[i])
        i -= 1
    region.reverse()
    return region


def find_undocumented_suppressions(pixi_toml: Path) -> list[tuple[str, str]]:
    """Return (vuln_id_or_marker, problem) for each undocumented suppression.

    Fails closed: a ``pip-audit`` task the single-line regex cannot parse, yet
    which clearly contains ``--ignore-vuln`` in the raw text, yields a parser
    finding rather than a silent pass.
    """
    if not pixi_toml.exists():
        return []
    text = pixi_toml.read_text(encoding="utf-8")
    lines = text.splitlines()

    task_index = next((i for i, ln in enumerate(lines) if PIP_AUDIT_TASK_RE.match(ln)), None)
    problems: list[tuple[str, str]] = []

    if task_index is None:
        # No single-line task matched. If non-comment lines still contain
        # --ignore-vuln, the task is multi-line / unparseable — fail closed (Decision 4).
        non_comment_text = "\n".join(ln for ln in lines if not ln.lstrip().startswith("#"))
        if IGNORE_VULN_RE.search(non_comment_text):
            problems.append(
                ("<parser>", "pip-audit task is not a single-line string the checker can parse")
            )
        return problems

    task_value = PIP_AUDIT_TASK_RE.match(lines[task_index]).group("value")  # type: ignore[union-attr]
    parsed = IGNORE_VULN_RE.findall(task_value)
    # Count --ignore-vuln occurrences only in non-comment lines (to avoid matching
    # the phrase "--ignore-vuln below" in ledger documentation comments).
    non_comment_lines = [ln for ln in lines if not ln.lstrip().startswith("#")]
    raw = IGNORE_VULN_RE.findall("\n".join(non_comment_lines))
    if len(raw) > len(parsed):
        problems.append(
            ("<parser>", "pip-audit task is not a single-line string the checker can parse")
        )

    region = _ledger_region(lines, task_index)
    for vuln_id in parsed:
        id_line = next((j for j, ln in enumerate(region) if vuln_id in ln), None)
        if id_line is None:
            problems.append((vuln_id, "no ledger comment documents this suppression"))
            continue
        if not any("Re-review:" in ln for ln in region[id_line:]):
            problems.append((vuln_id, "ledger comment lacks a 'Re-review:' trigger"))
    return problems


def main() -> int:
    """Scan the pip-audit ledger and exit non-zero on an undocumented suppression."""
    if len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
        print(__doc__)
        return 0
    repo_root = get_repo_root()
    problems = find_undocumented_suppressions(repo_root / "pixi.toml")
    if problems:
        print("ERROR: pip-audit ledger has suppressions without a re-review trigger:")
        for marker, problem in problems:
            print(f"  {marker}: {problem}")
        print(
            "\nEvery '--ignore-vuln <ID>' MUST be documented in the comment region "
            "directly above the pip-audit task, with a 'Re-review:' line stating "
            "when to drop it. See issue #1550."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

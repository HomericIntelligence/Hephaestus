"""Enforce console-script stability-tier documentation.

Every entry in [project.scripts] in pyproject.toml MUST have a matching row in
the "Console-Script Stability Tiers" table in COMPATIBILITY.md. The CLI name
is the row key; the tier (Stable / Provisional / Internal) is the value.

If this hook misfires in a way you cannot fix locally, bypass it with
``SKIP=hephaestus-check-cli-tier-docs git commit -S ...`` (do NOT use
``--no-verify``, which skips ALL hooks including signing).

Usage::
    hephaestus-check-cli-tier-docs
    hephaestus-check-cli-tier-docs --json
    hephaestus-check-cli-tier-docs --repo-root /path/to/repo
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from hephaestus.cli.utils import add_json_arg, add_version_arg
from hephaestus.io.toml import import_tomllib
from hephaestus.utils.helpers import get_repo_root

VALID_TIERS: frozenset[str] = frozenset({"Stable", "Provisional", "Internal"})
_SECTION_HEADER_RE = re.compile(r"^##\s+Console-Script Stability Tiers", re.IGNORECASE)
_TABLE_HEADER_RE = re.compile(r"^\|\s*CLI\s*\|\s*Tier\s*\|", re.IGNORECASE)
_TABLE_SEPARATOR_RE = re.compile(r"^\|[\s\-:|]+\|?\s*$")
_TABLE_ROW_RE = re.compile(r"^\|\s*`?(hephaestus-[a-z0-9-]+)`?\s*\|\s*([A-Za-z]+)\s*\|")


@dataclass
class TierDocFinding:
    """A single violation found while cross-checking CLI tier documentation."""

    cli: str
    # kind values: "missing-from-docs" | "missing-from-pyproject"
    #              | "invalid-tier" | "parser-found-no-rows"
    kind: str
    detail: str


def load_pyproject_scripts(pyproject_path: Path) -> dict[str, str]:
    """Return the ``[project.scripts]`` mapping from *pyproject_path*.

    Raises:
        RuntimeError: When neither ``tomllib`` (Python 3.11+) nor the
            ``tomli`` backport is available.

    """
    tomllib = import_tomllib()
    if tomllib is None:
        raise RuntimeError(
            "tomllib (Python 3.11+) or the 'tomli' backport is required to parse pyproject.toml"
        )
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    return dict(data.get("project", {}).get("scripts", {}))


def load_documented_tiers(compatibility_path: Path) -> dict[str, str]:
    """Parse the Console-Script Stability Tiers table.

    Skips separator rows (``|---|---|``) and the header row. Stops at the
    next section heading or first non-table line after the table starts.
    """
    tiers: dict[str, str] = {}
    in_section = False
    in_table = False
    for line in compatibility_path.read_text(encoding="utf-8").splitlines():
        if _SECTION_HEADER_RE.match(line):
            in_section = True
            continue
        if in_section and line.startswith("## ") and not _SECTION_HEADER_RE.match(line):
            break  # next H2 ends the section
        if not in_section:
            continue
        if _TABLE_HEADER_RE.search(line):
            in_table = True
            continue
        if in_table:
            if _TABLE_SEPARATOR_RE.match(line):
                continue
            if not line.startswith("|"):
                in_table = False
                continue
            m = _TABLE_ROW_RE.match(line)
            if m:
                tiers[m.group(1)] = m.group(2)
    return tiers


def find_violations(scripts: dict[str, str], tiers: dict[str, str]) -> list[TierDocFinding]:
    """Cross-check *scripts* (from pyproject.toml) against *tiers* (from COMPATIBILITY.md).

    Returns a list of :class:`TierDocFinding` objects describing every
    discrepancy. An empty list means full alignment.
    """
    findings: list[TierDocFinding] = []
    # Guard against silent regex regression (Decision 4): if pyproject has
    # entries but the table parsed zero rows, fail loudly rather than reporting
    # all 44 CLIs as "missing-from-docs".
    if scripts and not tiers:
        findings.append(
            TierDocFinding(
                cli="<table>",
                kind="parser-found-no-rows",
                detail=(
                    "COMPATIBILITY.md has no parseable rows in the "
                    "'## Console-Script Stability Tiers' section. "
                    "Either the section is missing or the table format changed."
                ),
            )
        )
        return findings
    for cli in sorted(set(scripts) - set(tiers)):
        findings.append(
            TierDocFinding(
                cli,
                "missing-from-docs",
                f"{cli} is in pyproject.toml [project.scripts] but has no row in COMPATIBILITY.md",
            )
        )
    for cli in sorted(set(tiers) - set(scripts)):
        findings.append(
            TierDocFinding(
                cli,
                "missing-from-pyproject",
                f"{cli} has a tier row in COMPATIBILITY.md but is not in"
                f" pyproject.toml [project.scripts]",
            )
        )
    for cli in sorted(set(tiers) & set(scripts)):
        if tiers[cli] not in VALID_TIERS:
            findings.append(
                TierDocFinding(
                    cli,
                    "invalid-tier",
                    f"{cli} has tier '{tiers[cli]}'; must be one of {sorted(VALID_TIERS)}",
                )
            )
    return findings


def format_report(findings: list[TierDocFinding]) -> str:
    """Render *findings* as a human-readable text report."""
    if not findings:
        return "OK: every [project.scripts] entry has a documented tier in COMPATIBILITY.md."
    lines = [f"FAIL: {len(findings)} tier-doc violation(s):"]
    for f in findings:
        lines.append(f"  [{f.kind}] {f.detail}")
    return "\n".join(lines)


def format_json(findings: list[TierDocFinding]) -> str:
    """Render *findings* as a JSON string."""
    return json.dumps({"violations": [asdict(f) for f in findings]}, indent=2)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``hephaestus-check-cli-tier-docs``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=None)
    add_json_arg(parser)
    add_version_arg(parser)
    args = parser.parse_args(argv)
    repo_root = args.repo_root or get_repo_root()
    scripts = load_pyproject_scripts(repo_root / "pyproject.toml")
    tiers = load_documented_tiers(repo_root / "COMPATIBILITY.md")
    findings = find_violations(scripts, tiers)
    print(format_json(findings) if args.json else format_report(findings))
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())

"""Enforce console-script stability-tier documentation.

Every entry in [project.scripts] in pyproject.toml MUST have a matching row in
the "Console-Script Stability Tiers" table in COMPATIBILITY.md. The CLI name
is the row key; the tier (Stable / Provisional / Internal) is the value.

If this hook misfires in a way you cannot fix locally, bypass it with
``SKIP=hephaestus-check-cli-tier-docs git commit -S -s ...`` (do NOT use
``--no-verify``, which skips ALL hooks including signing).

Usage::
    hephaestus-check-cli-tier-docs
    hephaestus-check-cli-tier-docs --json
    hephaestus-check-cli-tier-docs --repo-root /path/to/repo
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from hephaestus.cli.utils import create_validation_parser, resolve_repo_root
from hephaestus.io.toml import import_tomllib

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
    #              | "duplicate-tier" | "conflicting-tier"
    #              | "duplicate-section"
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


def load_documented_tiers(
    compatibility_path: Path,
) -> tuple[dict[str, str], dict[str, list[str]], int]:
    """Parse the Console-Script Stability Tiers table.

    Skips separator rows (``|---|---|``) and the header row. Stops at the
    next section heading or first non-table line after the table starts.
    Accumulates rows from ALL ``## Console-Script Stability Tiers`` sections
    into the same ``occurrences`` dict so cross-section contradictions are
    detected.

    Returns:
        A ``(tiers, occurrences, section_count)`` triple. ``tiers`` is the
        flattened ``{cli: tier}`` mapping (last occurrence wins, used for the
        membership/valid-value checks). ``occurrences`` is
        ``{cli: [tier, tier, ...]}`` preserving EVERY parsed row so the
        caller can detect a CLI documented more than once. ``section_count``
        is the number of ``## Console-Script Stability Tiers`` headers found;
        >1 indicates a duplicate section.

    """
    occurrences: dict[str, list[str]] = {}
    in_section = False
    in_table = False
    section_count = 0
    for line in compatibility_path.read_text(encoding="utf-8").splitlines():
        if _SECTION_HEADER_RE.match(line):
            in_section = True
            in_table = False
            section_count += 1
            continue
        if in_section and line.startswith("## ") and not _SECTION_HEADER_RE.match(line):
            in_section = False  # exit section; keep scanning for another tier section
            in_table = False
            continue
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
                occurrences.setdefault(m.group(1), []).append(m.group(2))
    tiers = {cli: vals[-1] for cli, vals in occurrences.items()}
    return tiers, occurrences, section_count


def find_duplicate_tiers(occurrences: dict[str, list[str]]) -> list[TierDocFinding]:
    """Flag any CLI documented in more than one row of the tier table.

    A CLI with multiple rows of the SAME tier yields a ``duplicate-tier``
    finding; multiple rows with DIFFERENT tiers yield ``conflicting-tier``
    (the self-contradiction the validator exists to prevent). Returns an
    empty list when every CLI appears exactly once.
    """
    findings: list[TierDocFinding] = []
    for cli in sorted(occurrences):
        vals = occurrences[cli]
        if len(vals) < 2:
            continue
        distinct = sorted(set(vals))
        if len(distinct) > 1:
            findings.append(
                TierDocFinding(
                    cli,
                    "conflicting-tier",
                    f"{cli} is documented {len(vals)} times in COMPATIBILITY.md "
                    f"with conflicting tiers {distinct}; the table contradicts itself",
                )
            )
        else:
            findings.append(
                TierDocFinding(
                    cli,
                    "duplicate-tier",
                    f"{cli} is documented {len(vals)} times in COMPATIBILITY.md "
                    f"(all tier '{distinct[0]}'); remove the duplicate row",
                )
            )
    return findings


def find_violations(
    scripts: dict[str, str],
    tiers: dict[str, str],
    duplicates: list[TierDocFinding] | None = None,
) -> list[TierDocFinding]:
    """Cross-check *scripts* (from pyproject.toml) against *tiers* (from COMPATIBILITY.md).

    *duplicates* carries findings produced by :func:`find_duplicate_tiers`
    (a CLI documented more than once). They are surfaced even when the
    flattened *scripts*/*tiers* alignment is otherwise clean, because the
    contradiction lives entirely inside COMPATIBILITY.md.

    Returns a list of :class:`TierDocFinding` objects describing every
    discrepancy. An empty list means full alignment.
    """
    findings: list[TierDocFinding] = list(duplicates or [])
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
    parser = create_validation_parser(__doc__)
    args = parser.parse_args(argv)
    repo_root = resolve_repo_root(args)
    scripts = load_pyproject_scripts(repo_root / "pyproject.toml")
    tiers, occurrences, section_count = load_documented_tiers(repo_root / "COMPATIBILITY.md")
    duplicates = find_duplicate_tiers(occurrences)
    if section_count > 1:
        duplicates.append(
            TierDocFinding(
                cli="<section>",
                kind="duplicate-section",
                detail=(
                    f"COMPATIBILITY.md contains {section_count} "
                    "'## Console-Script Stability Tiers' sections; "
                    "merge them into one to prevent cross-section contradictions"
                ),
            )
        )
    findings = find_violations(scripts, tiers, duplicates)
    print(format_json(findings) if args.json else format_report(findings))
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())

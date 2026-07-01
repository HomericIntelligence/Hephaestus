"""Enforce per-symbol API stability-table documentation.

Every member of a documented subpackage's __all__ MUST have a matching row in
that subpackage's "### `hephaestus.<pkg>`" table in COMPATIBILITY.md, and every
documented row MUST correspond to a live __all__ member. Only TABLE MEMBERSHIP
is validated — the "Added" version column is a best-effort historical anchor
and is intentionally NOT asserted.

If this hook misfires in a way you cannot fix locally, bypass it with
``SKIP=hephaestus-check-api-table-docs git commit -S -s`` (do NOT use
``--no-verify``, which skips ALL hooks including signing).

Usage::
    hephaestus-check-api-table-docs
    hephaestus-check-api-table-docs --json
    hephaestus-check-api-table-docs --repo-root /path/to/repo
"""

from __future__ import annotations

import importlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from hephaestus.cli.utils import create_validation_parser, resolve_repo_root

# Subpackages whose API tables are completeness-guarded. Add a name here only
# after authoring its table in COMPATIBILITY.md.
GUARDED_PACKAGES: tuple[str, ...] = (
    "hephaestus.cli",
    "hephaestus.config",
    "hephaestus.system",
    "hephaestus.utils",
    "hephaestus.version",
)

_HEADER_RE = re.compile(r"^###\s+`(hephaestus\.[a-z_]+)`\s*$")
_SEPARATOR_RE = re.compile(r"^\|[\s\-:|]+\|?\s*$")
_ROW_RE = re.compile(r"^\|\s*`([A-Za-z_][A-Za-z0-9_]*)`\s*\|")


@dataclass
class ApiTableFinding:
    """A single violation found while cross-checking API table documentation."""

    package: str
    # kind values: "missing-from-docs" | "missing-from-all"
    #              | "table-not-found" | "parser-found-no-rows"
    kind: str
    detail: str


def load_documented_symbols(compatibility_path: Path) -> dict[str, set[str]]:
    """Return ``{package: {symbol, ...}}`` parsed from each ``### `hephaestus.<pkg>``` table.

    Scans all ``### `hephaestus.<pkg>``` headings, reading table rows until
    the next heading (``###`` or ``##``) or the end of the file. Separator
    rows and the column header row are skipped.

    Args:
        compatibility_path: Path to ``COMPATIBILITY.md``.

    Returns:
        Mapping from fully-qualified package name to the set of symbol names
        parsed from that package's table.

    """
    tables: dict[str, set[str]] = {}
    current: str | None = None
    text = compatibility_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        header = _HEADER_RE.match(line)
        if header:
            current = header.group(1)
            tables.setdefault(current, set())
            continue
        if line.startswith("### ") or line.startswith("## "):
            current = None
            continue
        if current is None:
            continue
        if _SEPARATOR_RE.match(line) or line.lower().startswith("| symbol"):
            continue
        m = _ROW_RE.match(line)
        if m:
            tables[current].add(m.group(1))
    return tables


def live_all(package: str) -> set[str]:
    """Return the live ``__all__`` of *package* as a set.

    Args:
        package: Fully-qualified package name (e.g. ``hephaestus.utils``).

    Returns:
        Set of public symbol names exported by the package.

    """
    mod = importlib.import_module(package)
    return set(getattr(mod, "__all__", ()))


def find_violations(
    documented: dict[str, set[str]],
    packages: tuple[str, ...] = GUARDED_PACKAGES,
) -> list[ApiTableFinding]:
    """Cross-check *packages* live ``__all__`` against *documented* table rows.

    For each package in *packages*:

    - ``table-not-found`` if the package has no ``### `hephaestus.<pkg>``` heading.
    - ``parser-found-no-rows`` if the heading exists but zero rows were parsed.
    - ``missing-from-docs`` for each ``__all__`` symbol with no table row.
    - ``missing-from-all`` for each table row whose symbol is not in ``__all__``.

    The "Added" version column is deliberately NOT validated — those values are
    best-effort historical anchors and asserting them would lend false authority
    to inferred data.

    Args:
        documented: Output of :func:`load_documented_symbols`.
        packages: Tuple of package names to check; defaults to
            :data:`GUARDED_PACKAGES`.

    Returns:
        List of :class:`ApiTableFinding` objects. Empty list means full alignment.

    """
    findings: list[ApiTableFinding] = []
    for pkg in packages:
        if pkg not in documented:
            findings.append(
                ApiTableFinding(
                    pkg,
                    "table-not-found",
                    f"No '### `{pkg}`' API table found in COMPATIBILITY.md",
                )
            )
            continue
        doc_syms = documented[pkg]
        all_syms = live_all(pkg)
        if all_syms and not doc_syms:
            findings.append(
                ApiTableFinding(
                    pkg,
                    "parser-found-no-rows",
                    f"'### `{pkg}`' table parsed zero rows; format may have changed",
                )
            )
            continue
        for sym in sorted(all_syms - doc_syms):
            findings.append(
                ApiTableFinding(
                    pkg,
                    "missing-from-docs",
                    f"{pkg}.{sym} is in __all__ but has no row in COMPATIBILITY.md",
                )
            )
        for sym in sorted(doc_syms - all_syms):
            findings.append(
                ApiTableFinding(
                    pkg,
                    "missing-from-all",
                    f"{pkg}: '{sym}' has a row in COMPATIBILITY.md but is not in __all__",
                )
            )
    return findings


def format_report(findings: list[ApiTableFinding]) -> str:
    """Render *findings* as a human-readable text report."""
    if not findings:
        return "OK: every guarded subpackage's __all__ is fully documented in COMPATIBILITY.md."
    lines = [f"FAIL: {len(findings)} API-table violation(s):"]
    lines.extend(f"  [{f.kind}] {f.detail}" for f in findings)
    return "\n".join(lines)


def format_json(findings: list[ApiTableFinding]) -> str:
    """Render *findings* as a JSON string."""
    return json.dumps({"violations": [asdict(f) for f in findings]}, indent=2)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``hephaestus-check-api-table-docs``."""
    parser = create_validation_parser(__doc__, prog="hephaestus-check-api-table-docs")
    args = parser.parse_args(argv)
    repo_root = resolve_repo_root(args)
    documented = load_documented_symbols(repo_root / "COMPATIBILITY.md")
    findings = find_violations(documented)
    print(format_json(findings) if args.json else format_report(findings))
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())

"""Repository-owned ADR rebase policy injected into the shared WorkerPool.

The shared ``WorkerPool`` executor is deliberately repository-agnostic.  The
ADR filename/section/README-index contract and the structural test argv below
are this repository's own policy and are injected by the host coordinator so
that another repository with a different valid ``docs/adr`` layout is
unaffected during rebase.
"""

from __future__ import annotations

import re
from pathlib import Path

from .job_results import JobResult

ADR_FILENAME_RE = re.compile(r"^(?P<number>[0-9]{4})-[a-z0-9-]+\.md$")
ADR_README_LINK_RE = re.compile(r"\(([0-9]{4}-[a-z0-9-]+\.md)\)")
ADR_REQUIRED_SECTIONS = (
    "## Context",
    "## Decision",
    "## Alternatives considered",
    "## Consequences",
)
REBASE_STRUCTURAL_TEST_PATH = "tests/unit/docs/test_adr_records.py"
REBASE_STRUCTURAL_TEST_ARGV = (
    "uv",
    "run",
    "pytest",
    "-o",
    "addopts=",
    REBASE_STRUCTURAL_TEST_PATH,
    "-q",
    "--tb=short",
)


def validate_rebased_adr_tree(cwd: Path) -> JobResult | None:
    """Reject known repository-structure damage before a rebase publish.

    Conflict resolution is intentionally limited to the files Git reports
    as conflicted.  That prevents an agent from editing unrelated files,
    but it cannot prove that the resulting tree is semantically valid.  A
    duplicate ADR number is the concrete failure this guard is designed to
    catch; the README check catches the corresponding stale/missing index
    entry.  Repositories without ``docs/adr`` are unaffected.
    """
    adr_dir = cwd / "docs" / "adr"
    if not adr_dir.is_dir():
        return None
    try:
        adr_files = sorted(path for path in adr_dir.glob("*.md") if path.name != "README.md")
        numbers: dict[str, list[str]] = {}
        for path in adr_files:
            match = ADR_FILENAME_RE.fullmatch(path.name)
            if match is None:
                return JobResult(
                    ok=False,
                    value={"failure_kind": "semantic_validation"},
                    error=(
                        f"rebase semantic validation failed: malformed ADR filename {path.name}"
                    ),
                )
            numbers.setdefault(match.group("number"), []).append(path.name)
        for number, names in sorted(numbers.items()):
            if len(names) > 1:
                return JobResult(
                    ok=False,
                    value={"failure_kind": "semantic_validation"},
                    error=(
                        f"rebase semantic validation failed: duplicate ADR number {number} "
                        f"({', '.join(names)})"
                    ),
                )
        for path in adr_files:
            text = path.read_text(encoding="utf-8")
            number = path.name[:4]
            if (
                re.search(rf"^# ADR-{number}:", text, re.MULTILINE) is None
                or "- Status:" not in text
                or "- Date:" not in text
                or any(section not in text for section in ADR_REQUIRED_SECTIONS)
            ):
                return JobResult(
                    ok=False,
                    value={"failure_kind": "semantic_validation"},
                    error=(f"rebase semantic validation failed: malformed ADR record {path.name}"),
                )

        readme = adr_dir / "README.md"
        if readme.is_file():
            linked = set(ADR_README_LINK_RE.findall(readme.read_text(encoding="utf-8")))
            on_disk = {path.name for path in adr_files}
            if linked != on_disk:
                missing = sorted(on_disk - linked)
                stale = sorted(linked - on_disk)
                return JobResult(
                    ok=False,
                    value={"failure_kind": "semantic_validation"},
                    error=(
                        "rebase semantic validation failed: ADR README index out of sync "
                        f"(missing={missing}, stale={stale})"
                    ),
                )
    except (OSError, UnicodeError) as exc:
        return JobResult(
            ok=False,
            value={"failure_kind": "semantic_validation"},
            error=f"rebase semantic validation failed: cannot inspect ADR records ({exc})",
        )
    return None

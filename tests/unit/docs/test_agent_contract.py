"""Functional contracts for the repository agent-guidance entry points.

These tests assert *functionality*, not document content: the legacy Claude
entry point must still route readers to the canonical contract, and no live
policy consumer may keep citing the deprecated pointer file. The prose inside
the documents is intentionally not pinned — behavior gates (pr-policy,
doc-config validator, link validation) own those guarantees.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

ALLOWED_CLAUDE_REFERENCE_LINES = {
    Path(".github/CODEOWNERS"): {"CLAUDE.md @mvillmow"},
    Path("docs/adr/0001-automation-library-boundary.md"): {
        "At decision time this guidance lived in `CLAUDE.md`; "
        "it is now consolidated in `AGENTS.md`."
    },
}

EXCLUDED_PARTS = {
    ".git",
    ".hypothesis",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tmp",
    ".venv",
    "build",
    "plugins",
}


def test_claude_md_routes_readers_to_the_canonical_contract() -> None:
    """The legacy entry point must link readers to the canonical AGENTS.md."""
    claude_md = REPO_ROOT / "CLAUDE.md"
    agents_md = REPO_ROOT / "AGENTS.md"
    assert agents_md.is_file(), "canonical contract must exist"
    text = claude_md.read_text(encoding="utf-8")
    assert "(AGENTS.md)" in text, "CLAUDE.md must reference AGENTS.md"
    # The relative link must resolve from CLAUDE.md's directory (repo root).
    assert (claude_md.parent / "AGENTS.md").resolve() == agents_md.resolve()


def test_hypothesis_cache_is_not_repository_source() -> None:
    """Property-test caches stay under build and outside policy scans."""
    assert ".hypothesis" in EXCLUDED_PARTS
    assert Path(os.environ["HYPOTHESIS_STORAGE_DIRECTORY"]) == (REPO_ROOT / "build" / ".hypothesis")


def test_only_explicit_compatibility_and_history_references_remain() -> None:
    """No live policy consumer may continue citing the legacy pointer."""
    test_file = Path(__file__).resolve()
    unexpected: list[str] = []

    for root, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [dirname for dirname in dirnames if dirname not in EXCLUDED_PARTS]
        for filename in filenames:
            path = Path(root) / filename
            if path.resolve() == test_file:
                continue
            relative = path.relative_to(REPO_ROOT)
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue

            allowed = ALLOWED_CLAUDE_REFERENCE_LINES.get(relative, set())
            for number, line in enumerate(lines, 1):
                if "CLAUDE.md" in line and line.strip() not in allowed:
                    unexpected.append(f"{relative}:{number}: {line.strip()}")

    assert unexpected == []

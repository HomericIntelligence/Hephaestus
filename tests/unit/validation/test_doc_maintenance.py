"""Tests for the normative documentation maintenance validator."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

from hephaestus.validation.doc_maintenance import (
    SourceContract,
    discover_normative_markdown,
    main,
    validate_documentation,
    validate_roadmap_maintenance,
    validate_source_contracts,
    validate_volatile_claims,
)


def test_specs_are_normative_and_test_fixtures_are_excluded(tmp_path: Path) -> None:
    """Nested specifications are scanned while nested fixtures are ignored."""
    spec = tmp_path / "docs" / "specs" / "nested" / "design.md"
    fixture = tmp_path / "tests" / "fixtures" / "docs" / "example.md"
    spec.parent.mkdir(parents=True)
    fixture.parent.mkdir(parents=True)
    spec.write_text("# Design\nCurrently inactive.\n", encoding="utf-8")
    fixture.write_text("# Fixture\nCurrently inactive.\n", encoding="utf-8")

    discovered = {
        path.relative_to(tmp_path).as_posix() for path in discover_normative_markdown(tmp_path)
    }

    assert "docs/specs/nested/design.md" in discovered
    assert "tests/fixtures/docs/example.md" not in discovered
    assert any(
        finding.file == "docs/specs/nested/design.md"
        for finding in validate_documentation(tmp_path)
    )


def test_historical_adr_and_release_note_bodies_are_excluded(tmp_path: Path) -> None:
    """Accepted records may retain point-in-time claims without being living docs."""
    accepted_adr = tmp_path / "docs" / "adr" / "0001-example.md"
    proposed_adr = tmp_path / "docs" / "adr" / "0002-proposed.md"
    draft_adr = tmp_path / "docs" / "adr" / "0003-draft.md"
    adr_index = tmp_path / "docs" / "adr" / "README.md"
    release_note = tmp_path / "docs" / "release-notes" / "v1.md"
    release_index = tmp_path / "docs" / "release-notes" / "README.md"
    release_index_alias = tmp_path / "docs" / "release-notes" / "index.md"
    for path in (adr_index, release_note, release_index, release_index_alias):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Record\nAs of 2026-01-01, issue #12 was closed.\n", encoding="utf-8")
    accepted_adr.parent.mkdir(parents=True, exist_ok=True)
    accepted_adr.write_text(
        "# Accepted ADR\n\n- Status: Accepted\n\nAs of 2026-01-01, issue #12 was closed.\n",
        encoding="utf-8",
    )
    proposed_adr.write_text(
        "# Proposed ADR\n\n- Status: Proposed\n\nCurrently inactive.\n", encoding="utf-8"
    )
    draft_adr.write_text(
        "# Draft ADR\n\n- Status: Draft\n\nCurrently inactive.\n", encoding="utf-8"
    )

    discovered = {
        path.relative_to(tmp_path).as_posix() for path in discover_normative_markdown(tmp_path)
    }

    assert "docs/adr/0001-example.md" not in discovered
    assert "docs/adr/0002-proposed.md" in discovered
    assert "docs/adr/0003-draft.md" in discovered
    assert "docs/release-notes/v1.md" not in discovered
    assert "docs/adr/README.md" in discovered
    assert "docs/release-notes/README.md" in discovered
    assert "docs/release-notes/index.md" in discovered


def test_volatile_claims_inside_fenced_examples_are_ignored(tmp_path: Path) -> None:
    """Examples can demonstrate stale prose without becoming normative claims."""
    document = tmp_path / "docs" / "example.md"
    document.parent.mkdir()
    document.write_text(
        "# Example\n\n"
        "```text\n"
        "The repository has 21 packages as of 2026-01-01.\n"
        "Currently inactive.\n"
        "```\n\n"
        "The repository has 21 packages as of 2026-01-01.\n",
        encoding="utf-8",
    )

    findings = validate_volatile_claims(document, tmp_path)

    assert len(findings) == 2
    assert all(finding.line == 8 for finding in findings)


def test_source_contracts_validate_local_links_and_semantic_selectors(tmp_path: Path) -> None:
    """A cited source must exist, be linked, and contain its selected symbol."""
    source = tmp_path / "src" / "routing.py"
    document = tmp_path / "docs" / "architecture.md"
    source.parent.mkdir(parents=True)
    document.parent.mkdir(parents=True)
    source.write_text("ROUTES = {'start': 'finish'}\n", encoding="utf-8")
    document.write_text("See [ROUTES](../src/routing.py).\n", encoding="utf-8")
    contract = SourceContract(
        document="docs/architecture.md",
        source="src/routing.py",
        selector="ROUTES",
    )

    assert validate_source_contracts(tmp_path, contracts=(contract,)) == []

    source.write_text("OTHER = {}\n", encoding="utf-8")
    document.write_text("See [ROUTES](../src/missing.py).\n", encoding="utf-8")
    findings = validate_source_contracts(tmp_path, contracts=(contract,))
    assert {finding.rule for finding in findings} == {"source-link", "source-selector"}


def test_roadmap_freshness_uses_injected_today(tmp_path: Path) -> None:
    """Roadmap freshness is deterministic and does not depend on wall-clock time."""
    roadmap = tmp_path / "docs" / "ROADMAP.md"
    roadmap.parent.mkdir(parents=True)
    roadmap.write_text(
        "# Roadmap\n\n"
        "## Current Focus (Q3 2026)\n\nFocus from open epics.\n\n"
        "## Updating This Roadmap\n\n"
        "**Trigger:** release. **Responsibility:** maintainer.\n"
        "Source: [RELEASING.md](RELEASING.md).\n\n"
        "Last updated: 2026-07-20\n",
        encoding="utf-8",
    )

    findings = validate_roadmap_maintenance(tmp_path, today=date(2026, 10, 1))

    assert any(finding.rule == "stale-current-focus" for finding in findings)


@pytest.mark.parametrize(
    ("content", "rule"),
    [
        ("## Current Focus (Q3 2026)\n\nLast updated: 2026-06-30\n", "last-updated-before-focus"),
        ("## Current Focus (Q3 2026)\n\nLast updated: 2026-08-01\n", "roadmap-ownership"),
    ],
)
def test_roadmap_contract_reports_deterministic_boundary_errors(
    tmp_path: Path, content: str, rule: str
) -> None:
    """Malformed roadmap metadata produces named findings rather than exceptions."""
    roadmap = tmp_path / "docs" / "ROADMAP.md"
    roadmap.parent.mkdir(parents=True)
    roadmap.write_text(content, encoding="utf-8")

    findings = validate_roadmap_maintenance(tmp_path, today=date(2026, 8, 3))

    assert any(finding.rule == rule for finding in findings)


def test_roadmap_cadence_is_validated_inside_update_section(tmp_path: Path) -> None:
    """Unrelated roadmap prose cannot satisfy the release-driven cadence guard."""
    roadmap = tmp_path / "docs" / "ROADMAP.md"
    roadmap.parent.mkdir(parents=True)
    roadmap.write_text(
        "# Roadmap\n\n"
        "## Current Focus (Q3 2026)\n\n"
        "The release-driven plan references Auto Tag Release and is not date-driven.\n\n"
        "## Notes\n\nTrigger and maintainer references may appear elsewhere.\n\n"
        "## Updating This Roadmap\n\nTypically monthly, when convenient.\n\n"
        "Last updated: 2026-07-20\n",
        encoding="utf-8",
    )

    findings = validate_roadmap_maintenance(tmp_path, today=date(2026, 8, 3))

    assert any(finding.rule == "roadmap-cadence" for finding in findings)


def test_json_output_is_machine_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The validator's CLI emits structured findings when requested."""
    document = tmp_path / "README.md"
    document.write_text("The repository has 21 packages as of 2026-01-01.\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["doc-maintenance", "--repo-root", str(tmp_path), "--json"],
    )

    assert main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is False
    assert payload["findings"][0]["rule"] in {"dated-state", "snapshot-metric"}


def test_current_repository_satisfies_documentation_contract() -> None:
    """The checked-in normative documentation has no maintenance findings."""
    repo_root = Path(__file__).resolve().parents[3]

    assert validate_documentation(repo_root) == []

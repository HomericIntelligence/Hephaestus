"""Tests for the living-document maintenance validator."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

from hephaestus.validation.doc_maintenance import (
    Finding,
    _selector_exists,
    discover_normative_markdown,
    format_json_report,
    scan_file,
    validate_roadmap_maintenance,
)


def test_specs_are_normative_and_test_fixtures_are_excluded(tmp_path: Path) -> None:
    """Nested specifications are scanned but nested fixtures are not."""
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


def test_historical_document_bodies_are_excluded_but_indexes_remain(tmp_path: Path) -> None:
    """Accepted ADR and release-note bodies are point-in-time records."""
    for relative in (
        "docs/adr/0001-example.md",
        "docs/release-notes/v1.md",
        "docs/adr/README.md",
        "docs/release-notes/README.md",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Record\nCurrently inactive.\n", encoding="utf-8")

    discovered = {
        path.relative_to(tmp_path).as_posix() for path in discover_normative_markdown(tmp_path)
    }

    assert discovered == {
        "docs/adr/README.md",
        "docs/release-notes/README.md",
    }


def test_scan_ignores_fenced_examples_and_reports_volatile_prose(tmp_path: Path) -> None:
    """Only prose outside fenced examples is subject to the volatile-claim guard."""
    document = tmp_path / "docs" / "example.md"
    document.parent.mkdir()
    document.write_text(
        "```text\n"
        "Currently inactive; 29 child issues\n"
        "```\n"
        "Currently inactive.\n"
        "The repository has 29 child issues.\n",
        encoding="utf-8",
    )

    findings = scan_file(document, tmp_path)

    assert [(finding.rule, finding.line) for finding in findings] == [
        ("temporary-issue-state", 4),
        ("repository-snapshot-metric", 5),
    ]


def test_semantic_selectors_cover_python_yaml_and_markdown(tmp_path: Path) -> None:
    """Source contracts can select symbols, YAML keys, and Markdown headings."""
    python_source = tmp_path / "routing.py"
    yaml_source = tmp_path / "workflow.yml"
    markdown_source = tmp_path / "release.md"
    python_source.write_text("ROUTES = {}\n\nclass PromptCatalog:\n    pass\n", encoding="utf-8")
    yaml_source.write_text("jobs:\n  lint:\n    runs-on: ubuntu-latest\n", encoding="utf-8")
    markdown_source.write_text("# Pre-Release Checklist\n", encoding="utf-8")

    assert _selector_exists(python_source, "ROUTES")
    assert _selector_exists(python_source, "PromptCatalog")
    assert _selector_exists(yaml_source, "jobs")
    assert _selector_exists(markdown_source, "Pre-Release Checklist")
    assert not _selector_exists(python_source, "MissingSymbol")


@pytest.fixture
def roadmap_repo(tmp_path: Path) -> Path:
    """Create a minimal repository containing a roadmap cadence contract."""
    roadmap = tmp_path / "docs" / "ROADMAP.md"
    roadmap.parent.mkdir()
    roadmap.write_text(
        "## Current Focus (Q3 2026)\n\n"
        "## Updating This Roadmap\n\n"
        "The maintainer reviews it at the Auto Tag Release trigger; releases are\n"
        "feature/fix-driven, not date-driven.\n\n"
        "Last updated: 2026-07-20\n",
        encoding="utf-8",
    )
    return tmp_path


def test_roadmap_freshness_uses_injected_today(roadmap_repo: Path) -> None:
    """Roadmap freshness is deterministic when the current date is injected."""
    findings = validate_roadmap_maintenance(
        roadmap_repo,
        today=date(2026, 10, 1),
    )

    assert any(finding.rule == "stale-current-focus" for finding in findings)


def test_roadmap_rejects_future_dates(roadmap_repo: Path) -> None:
    """Roadmap dates cannot be later than the injected current date."""
    findings = validate_roadmap_maintenance(roadmap_repo, today=date(2026, 7, 1))

    assert any(finding.rule == "future-last-updated" for finding in findings)


def test_roadmap_rejects_cross_quarter_dates(roadmap_repo: Path) -> None:
    """Roadmap dates must belong to the stated focus quarter."""
    roadmap = roadmap_repo / "docs" / "ROADMAP.md"
    roadmap.write_text(
        roadmap.read_text(encoding="utf-8").replace("2026-07-20", "2026-10-01"),
        encoding="utf-8",
    )

    findings = validate_roadmap_maintenance(roadmap_repo, today=date(2026, 10, 2))

    assert any(finding.rule == "last-updated-outside-focus-quarter" for finding in findings)


def test_json_report_is_machine_readable() -> None:
    """The CLI report includes findings and its exit status."""
    findings = [Finding("docs/example.md", 3, "dated-state", "not maintained")]

    report = json.loads(format_json_report(findings))

    assert report["exit_code"] == 1
    assert report["passed"] is False
    assert report["findings"][0]["rule"] == "dated-state"


def test_repository_corpus_passes_maintenance_contract() -> None:
    """The checked-in normative corpus satisfies the offline guard."""
    repo_root = Path(__file__).resolve().parents[3]
    from hephaestus.validation.doc_maintenance import validate_documentation

    assert validate_documentation(repo_root) == []


def test_cli_module_is_importable_without_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The validator exposes a module entry point without requiring a console script."""
    monkeypatch.setattr(sys, "argv", ["doc_maintenance", "--help"])
    with pytest.raises(SystemExit) as raised:
        from hephaestus.validation.doc_maintenance import main

        main()
    assert raised.value.code == 0

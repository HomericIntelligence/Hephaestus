"""Behavioral tests for normative-documentation maintenance validation."""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
import yaml


def _module() -> object:
    """Import the validation module after its availability assertion runs."""
    return importlib.import_module("hephaestus.validation.doc_maintenance")


def test_doc_maintenance_module_is_available() -> None:
    """The maintenance guard has a Python module entry point."""
    assert importlib.util.find_spec("hephaestus.validation.doc_maintenance") is not None


def test_discovery_includes_nested_specs_and_excludes_historical_records(tmp_path: Path) -> None:
    """Nested specifications stay normative while fixtures and records do not."""
    paths = {
        "docs/specs/nested/design.md": "# Design\n",
        "docs/adr/0001-decision.md": "# Decision\n",
        "docs/adr/README.md": "# Index\n",
        "docs/release-notes/release.md": "# Release\n",
        "docs/release-notes/README.md": "# Index\n",
        "tests/fixtures/docs/example.md": "# Fixture\n",
    }
    for relative_path, content in paths.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    module = _module()
    discovered = {
        path.relative_to(tmp_path).as_posix()
        for path in module.discover_normative_markdown(tmp_path)
    }

    assert "docs/specs/nested/design.md" in discovered
    assert "docs/adr/README.md" in discovered
    assert "docs/release-notes/README.md" in discovered
    assert "docs/adr/0001-decision.md" not in discovered
    assert "docs/release-notes/release.md" not in discovered
    assert "tests/fixtures/docs/example.md" not in discovered


def test_volatile_validation_ignores_fenced_examples(tmp_path: Path) -> None:
    """Only live prose containing an unsupported operational claim is reported."""
    document = tmp_path / "docs" / "guide.md"
    document.parent.mkdir(parents=True)
    document.write_text(
        "Currently inactive.\n\n```text\nCurrently inactive.\n```\n",
        encoding="utf-8",
    )

    findings = _module().validate_volatile_claims(document, repo_root=tmp_path)

    assert [(finding.rule, finding.line) for finding in findings] == [("temporary-state", 1)]


def test_source_contract_reports_missing_semantic_selector(tmp_path: Path) -> None:
    """A maintained source must contain the selector cited by its contract."""
    document = tmp_path / "docs" / "guide.md"
    source = tmp_path / "src.py"
    document.parent.mkdir(parents=True)
    document.write_text("# Guide\n", encoding="utf-8")
    source.write_text("class Actual:\n    pass\n", encoding="utf-8")

    module = _module()
    findings = module.validate_source_contracts(
        tmp_path,
        contracts=(module.SourceContract("docs/guide.md", "src.py", "Expected"),),
    )

    assert [finding.rule for finding in findings] == ["missing-semantic-selector"]


def test_roadmap_maintenance_uses_release_trigger_not_calendar_rollover(tmp_path: Path) -> None:
    """A release-driven roadmap remains valid after its focus quarter ends."""
    roadmap = tmp_path / "docs" / "ROADMAP.md"
    roadmap.parent.mkdir(parents=True)
    roadmap.write_text(
        "## Current Focus (Q3 2026)\n\n"
        "**Owner:** Release maintainer.\n\n"
        "**Trigger:** Pre-release review.\n\n"
        "**Maintained source:** Open epics.\n\n"
        "Last updated: 2026-07-20\n",
        encoding="utf-8",
    )

    findings = _module().validate_roadmap_maintenance(tmp_path)

    assert findings == []


@pytest.mark.parametrize(
    ("metadata", "rule"),
    (
        ("**Owner:** Release maintainer.\n", "missing-roadmap-owner"),
        ("**Trigger:** Pre-release review.\n", "missing-roadmap-trigger"),
        ("**Maintained source:** Open epics.\n", "missing-roadmap-source"),
    ),
)
def test_roadmap_maintenance_requires_declared_contract_fields(
    tmp_path: Path, metadata: str, rule: str
) -> None:
    """Each roadmap maintenance field is validated independently."""
    roadmap = tmp_path / "docs" / "ROADMAP.md"
    roadmap.parent.mkdir(parents=True)
    content = (
        "**Owner:** Release maintainer.\n"
        "**Trigger:** Pre-release review.\n"
        "**Maintained source:** Open epics.\n"
    )
    roadmap.write_text(
        content.replace(metadata, ""),
        encoding="utf-8",
    )

    findings = _module().validate_roadmap_maintenance(tmp_path)

    assert [finding.rule for finding in findings] == [rule]


@pytest.mark.parametrize(
    "changed_path",
    (
        "hephaestus/automation/pipeline/routing.py",
        "hephaestus/automation/pipeline/stages/implementation.py",
        ".github/workflows/_required.yml",
        ".github/workflows/test.yml",
    ),
)
def test_doc_maintenance_hook_covers_declared_source_changes(changed_path: str) -> None:
    """The maintenance guard runs when a maintained source changes."""
    repo_root = Path(__file__).resolve().parents[3]
    config = yaml.safe_load((repo_root / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hook = next(
        hook
        for repo in config["repos"]
        for hook in repo.get("hooks", ())
        if hook["id"] == "check-doc-maintenance"
    )

    assert re.search(hook["files"], changed_path)


def test_main_json_reports_repository_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI emits a stable JSON report for a repository with a finding."""
    document = tmp_path / "docs" / "guide.md"
    document.parent.mkdir(parents=True)
    document.write_text("Currently inactive.\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["hephaestus-check-doc-maintenance", "--repo-root", str(tmp_path), "--json"],
    )

    assert _module().main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is False
    assert report["findings"][0]["rule"] == "temporary-state"


def test_repository_documents_satisfy_maintenance_contract() -> None:
    """The checked-in normative-document corpus has no unsupported snapshots."""
    repo_root = Path(__file__).resolve().parents[3]
    assert _module().validate_documentation(repo_root) == []

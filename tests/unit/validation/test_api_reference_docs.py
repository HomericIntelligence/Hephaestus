"""Tests for the generated pdoc API reference guard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hephaestus.validation.api_reference import (
    DEFAULT_REPO_ROOT,
    ApiReferenceFinding,
    expected_pdoc_targets,
    find_violations,
    list_subpackage_pages,
    main,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _write_html(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<!doctype html><title>pdoc</title>\n", encoding="utf-8")


class TestPdocTargets:
    """Tests for the pdoc target list used by the docs task."""

    def test_default_repo_root_points_at_checkout_root(self) -> None:
        """The flattened module default resolves the repository root."""
        assert DEFAULT_REPO_ROOT == REPO_ROOT

    def test_expected_targets_include_every_direct_subpackage_except_automation(self) -> None:
        direct_subpackages = {
            f"./hephaestus/{path.name}"
            for path in (REPO_ROOT / "hephaestus").iterdir()
            if path.is_dir()
            and not path.name.startswith((".", "_"))
            and (path / "__init__.py").is_file()
            and path.name != "automation"
        }

        targets = expected_pdoc_targets(REPO_ROOT)

        assert targets[0] == "./hephaestus"
        assert set(targets[1:]) == direct_subpackages
        assert "./hephaestus/automation" not in targets

    def test_justfile_docs_recipe_matches_expected_targets(self) -> None:
        recipe = (REPO_ROOT / "justfile").read_text(encoding="utf-8")
        expected = (
            "uv run pdoc " + " ".join(expected_pdoc_targets(REPO_ROOT)) + " --output-dir docs/api"
        )
        assert expected in recipe

    def test_release_workflow_docs_recipe_matches_expected_targets(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        expected = (
            "uv run pdoc " + " ".join(expected_pdoc_targets(REPO_ROOT)) + " --output-dir docs/api"
        )

        assert expected in workflow


class TestFindViolations:
    """Tests for generated ``docs/api`` output validation."""

    def test_missing_docs_dir_is_reported(self, tmp_path: Path) -> None:
        findings = find_violations(tmp_path / "docs" / "api")

        assert findings == [
            ApiReferenceFinding(
                kind="missing-docs-dir",
                detail=f"{tmp_path / 'docs' / 'api'} does not exist",
            )
        ]

    def test_near_empty_pdoc_output_is_reported(self, tmp_path: Path) -> None:
        docs_dir = tmp_path / "docs" / "api"
        _write_html(docs_dir / "index.html")
        _write_html(docs_dir / "hephaestus.html")
        (docs_dir / "search.js").write_text("window.search = [];\n", encoding="utf-8")

        findings = find_violations(docs_dir, repo_root=REPO_ROOT)

        assert findings
        assert all(finding.kind == "missing-subpackage-page" for finding in findings)
        assert all("automation.html" not in finding.detail for finding in findings)

    def test_missing_expected_subpackage_pages_are_reported_individually(
        self,
        tmp_path: Path,
    ) -> None:
        docs_dir = tmp_path / "docs" / "api"
        expected_pages = {
            f"{Path(target).name}.html" for target in expected_pdoc_targets(REPO_ROOT)[1:]
        }
        missing_pages = {"io.html", "utils.html"} & expected_pages
        assert missing_pages == {"io.html", "utils.html"}

        _write_html(docs_dir / "hephaestus.html")
        for page_name in sorted(expected_pages - missing_pages):
            _write_html(docs_dir / "hephaestus" / page_name)
        _write_html(docs_dir / "hephaestus" / "automation.html")

        findings = find_violations(docs_dir, repo_root=REPO_ROOT)

        assert findings == [
            ApiReferenceFinding(
                kind="missing-subpackage-page",
                detail=f"missing generated page docs/api/hephaestus/{page_name}",
            )
            for page_name in sorted(missing_pages)
        ]

    def test_complete_expected_subpackage_pages_pass(self, tmp_path: Path) -> None:
        docs_dir = tmp_path / "docs" / "api"
        _write_html(docs_dir / "hephaestus.html")
        for target in expected_pdoc_targets(REPO_ROOT)[1:]:
            _write_html(docs_dir / "hephaestus" / f"{Path(target).name}.html")

        assert find_violations(docs_dir, repo_root=REPO_ROOT) == []

    def test_lists_only_direct_subpackage_pages(self, tmp_path: Path) -> None:
        docs_dir = tmp_path / "docs" / "api"
        _write_html(docs_dir / "hephaestus" / "utils.html")
        _write_html(docs_dir / "hephaestus" / "scripts_lib" / "helper.html")

        assert [path.name for path in list_subpackage_pages(docs_dir)] == ["utils.html"]


class TestMain:
    """Tests for the ``hephaestus-check-api-reference`` CLI entry point."""

    def test_main_returns_zero_for_complete_docs(self, tmp_path: Path) -> None:
        docs_dir = tmp_path / "docs" / "api"
        for target in expected_pdoc_targets(REPO_ROOT)[1:]:
            _write_html(docs_dir / "hephaestus" / f"{Path(target).name}.html")

        assert main(["--docs-dir", str(docs_dir)]) == 0

    def test_main_returns_one_for_near_empty_docs(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        docs_dir = tmp_path / "docs" / "api"
        _write_html(docs_dir / "hephaestus.html")

        assert main(["--docs-dir", str(docs_dir)]) == 1
        assert "missing-subpackage-page" in capsys.readouterr().out

    def test_main_json_output_is_valid_json(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        docs_dir = tmp_path / "docs" / "api"
        _write_html(docs_dir / "hephaestus.html")

        assert main(["--docs-dir", str(docs_dir), "--json"]) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["violations"][0]["kind"] == "missing-subpackage-page"

    def test_main_rejects_removed_min_subpackage_pages_flag(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["--min-subpackage-pages", "999"])

        assert exc_info.value.code == 2
        assert "unrecognized arguments: --min-subpackage-pages 999" in capsys.readouterr().err

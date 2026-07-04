"""Tests for the generated pdoc API reference guard."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from hephaestus.io.toml import import_tomllib
from hephaestus.validation.docs.api_reference import (
    DEFAULT_MIN_SUBPACKAGE_PAGES,
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

    def test_expected_targets_include_every_direct_subpackage(self) -> None:
        direct_subpackages = {
            f"./hephaestus/{path.name}"
            for path in (REPO_ROOT / "hephaestus").iterdir()
            if path.is_dir()
            and not path.name.startswith((".", "_"))
            and (path / "__init__.py").is_file()
        }

        targets = expected_pdoc_targets(REPO_ROOT)

        assert targets[0] == "./hephaestus"
        assert set(targets[1:]) == direct_subpackages

    def test_pixi_docs_task_matches_expected_targets(self) -> None:
        tomllib = import_tomllib()
        assert tomllib is not None
        pixi = tomllib.loads((REPO_ROOT / "pixi.toml").read_text(encoding="utf-8"))

        parts = shlex.split(pixi["tasks"]["docs"])
        output_flag = parts.index("--output-dir")

        assert parts[0] == "pdoc"
        assert tuple(parts[1:output_flag]) == expected_pdoc_targets(REPO_ROOT)
        assert parts[output_flag + 1 :] == ["docs/api"]


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

        findings = find_violations(docs_dir)

        assert findings == [
            ApiReferenceFinding(
                kind="too-few-subpackage-pages",
                detail=(
                    f"{docs_dir} has 0 hephaestus subpackage page(s); "
                    f"expected at least {DEFAULT_MIN_SUBPACKAGE_PAGES}"
                ),
            )
        ]

    def test_subpackage_pages_pass(self, tmp_path: Path) -> None:
        docs_dir = tmp_path / "docs" / "api"
        _write_html(docs_dir / "hephaestus.html")
        _write_html(docs_dir / "hephaestus" / "utils.html")
        _write_html(docs_dir / "hephaestus" / "io.html")

        assert find_violations(docs_dir) == []

    def test_lists_only_direct_subpackage_pages(self, tmp_path: Path) -> None:
        docs_dir = tmp_path / "docs" / "api"
        _write_html(docs_dir / "hephaestus" / "utils.html")
        _write_html(docs_dir / "hephaestus" / "scripts_lib" / "helper.html")

        assert [path.name for path in list_subpackage_pages(docs_dir)] == ["utils.html"]


class TestMain:
    """Tests for the ``hephaestus-check-api-reference`` CLI entry point."""

    def test_main_returns_zero_for_complete_docs(self, tmp_path: Path) -> None:
        docs_dir = tmp_path / "docs" / "api"
        _write_html(docs_dir / "hephaestus" / "utils.html")
        _write_html(docs_dir / "hephaestus" / "io.html")

        assert main(["--docs-dir", str(docs_dir)]) == 0

    def test_main_returns_one_for_near_empty_docs(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        docs_dir = tmp_path / "docs" / "api"
        _write_html(docs_dir / "hephaestus.html")

        assert main(["--docs-dir", str(docs_dir)]) == 1
        assert "too-few-subpackage-pages" in capsys.readouterr().out

    def test_main_json_output_is_valid_json(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        docs_dir = tmp_path / "docs" / "api"
        _write_html(docs_dir / "hephaestus.html")

        assert main(["--docs-dir", str(docs_dir), "--json"]) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["violations"][0]["kind"] == "too-few-subpackage-pages"

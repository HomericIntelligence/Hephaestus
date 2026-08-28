"""Distribution checks for the nested Hephaestus compatibility package."""

from __future__ import annotations

import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from markdown_it import MarkdownIt
from pathspec import PathSpec

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "hephaestus"

EXPECTED_RUNTIME_RESOURCE_PATHS = (
    ".codex-plugin/plugin.json",
    "assets/icon.svg",
    "skills/THIRD_PARTY_LICENSES.md",
    "skills/_repo_analyze_common/README.md",
    "skills/_repo_analyze_common/coverage_report_block.md",
    "skills/_repo_analyze_common/methodology_full.md",
    "skills/_repo_analyze_common/methodology_full_8.md",
    "skills/_repo_analyze_common/methodology_sampling.md",
    "skills/_repo_analyze_common/output_format_full.md",
    "skills/_repo_analyze_common/output_format_quick.md",
    "skills/_repo_analyze_common/principles.md",
    "skills/_repo_analyze_common/rubric_default.md",
    "skills/_repo_analyze_common/rubric_quick.md",
    "skills/_repo_analyze_common/rubric_strict.md",
    "skills/_repo_analyze_common/sections_15.md",
    "skills/_repo_analyze_common/sections_8.md",
    "skills/_repo_analyze_common/variants.yaml",
    "skills/_repo_analyze_common/templates/repo_analyze.md.tmpl",
    "skills/_repo_analyze_common/templates/repo_analyze_quick.md.tmpl",
    "skills/_repo_analyze_common/templates/repo_analyze_strict.md.tmpl",
    "skills/python-repo-modernization/references/notes.md",
)


def _read_ignore_spec(path: Path) -> PathSpec:
    """Return a GitWildMatch ignore spec from a package ``.codexignore`` file."""
    return PathSpec.from_lines("gitwildmatch", path.read_text(encoding="utf-8").splitlines())


def _match(spec: PathSpec, relpath: str, is_dir: bool = False) -> bool:
    """Return whether a relative path is ignored by a GitWildMatch spec."""
    probe = f"{relpath}/" if is_dir and not relpath.endswith("/") else relpath
    return spec.match_file(probe)


def _regular_files(root: Path) -> set[str]:
    """Return the relative paths of all regular files under *root*."""
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def _assemble_package(source_root: Path, destination_root: Path, spec: PathSpec) -> None:
    """Copy a package tree without ignored paths or symlinks."""
    destination_root.mkdir(parents=True, exist_ok=True)

    for dirpath, dirnames, filenames in os.walk(source_root, topdown=True, followlinks=False):
        current = Path(dirpath)

        kept_dirs: list[str] = []
        for dirname in dirnames:
            source_dir = current / dirname
            if source_dir.is_symlink():
                raise ValueError(f"symlink directory is not allowed: {source_dir}")
            rel_dir = source_dir.relative_to(source_root).as_posix()
            if not _match(spec, rel_dir, is_dir=True):
                kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in filenames:
            source_file = current / filename
            if source_file.is_symlink():
                raise ValueError(f"symlink file is not allowed: {source_file}")
            rel_file = source_file.relative_to(source_root).as_posix()
            if _match(spec, rel_file):
                continue
            destination_file = destination_root / rel_file
            destination_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, destination_file)


def _has_symlink_component(base_root: Path, reference: PurePosixPath) -> bool:
    """Return True when a reference path traverses any symlink component."""
    cursor = base_root
    for part in reference.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            return True
    return False


def _resolve_package_reference(base_root: Path, reference: str) -> Path:
    """Resolve a package resource reference and reject traversal or symlinks."""
    ref_path = PurePosixPath(reference)
    if ref_path.is_absolute() or ref_path.anchor or any(part == ".." for part in ref_path.parts):
        raise ValueError(f"illegal reference path: {reference}")

    candidate = base_root.joinpath(*ref_path.parts)
    if _has_symlink_component(base_root, ref_path):
        raise ValueError(f"symlink escape is not allowed: {reference}")

    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(base_root.resolve()):
        raise ValueError(f"reference escapes package root: {reference}")
    if not candidate.exists():
        raise FileNotFoundError(reference)
    return candidate


def _collect_markdown_references(text: str) -> set[str]:
    """Return link and image destinations outside code spans and fences."""
    references: set[str] = set()
    parser = MarkdownIt("commonmark")

    def visit(tokens: list[Any]) -> None:
        for token in tokens:
            if token.type == "link_open":
                href = token.attrGet("href")
                if href is not None:
                    references.add(href)
            elif token.type == "image":
                src = token.attrGet("src")
                if src is not None:
                    references.add(src)
            if token.children:
                visit(token.children)

    visit(parser.parse(text))
    return references


def test_codexignore_assembles_package_without_dropping_required_files(tmp_path: Path) -> None:
    """GitWildMatch assembly must keep the shipped compatibility payload."""
    spec = _read_ignore_spec(PLUGIN_ROOT / ".codexignore")
    assembled_root = tmp_path / "assembled"
    _assemble_package(PLUGIN_ROOT, assembled_root, spec)

    expected = {
        relpath
        for relpath in _regular_files(PLUGIN_ROOT)
        if not _match(spec, relpath)
    }
    actual = _regular_files(assembled_root)

    assert actual == expected
    assert not any(path.is_symlink() for path in PLUGIN_ROOT.rglob("*"))
    assert not any(path.is_symlink() for path in assembled_root.rglob("*"))
    assert set(EXPECTED_RUNTIME_RESOURCE_PATHS).issubset(actual)
    assert ".codexignore" in actual
    assert "README.md" in actual
    assert "LICENSE" in actual


def test_pathspec_semantics_cover_negation_anchoring_and_directories() -> None:
    """The package ignore model must keep GitWildMatch last-match semantics."""
    spec = PathSpec.from_lines(
        "gitwildmatch",
        [
            "*.log",
            "!keep.log",
            "/root.txt",
            "build/",
            "!build/keep.txt",
        ],
    )

    assert _match(spec, "debug.log")
    assert not _match(spec, "keep.log")
    assert _match(spec, "root.txt")
    assert not _match(spec, "nested/root.txt")
    assert _match(spec, "build", is_dir=True)
    assert _match(spec, "build/drop.txt")
    assert not _match(spec, "build/keep.txt")


def test_markdown_classification_ignores_code_examples_when_resolving_resources() -> None:
    """MarkdownIt must ignore illustrative links inside code examples."""
    text = (
        "See [third-party licenses](skills/THIRD_PARTY_LICENSES.md) and "
        "![icon](assets/icon.svg).\n\n"
        "Inline code: `[ignored](skills/missing.md)`.\n\n"
        "```markdown\n"
        "[ignored](../escape.md)\n"
        "```\n"
    )

    refs = _collect_markdown_references(text)

    assert refs == {
        "skills/THIRD_PARTY_LICENSES.md",
        "assets/icon.svg",
    }
    for ref in refs:
        assert _resolve_package_reference(PLUGIN_ROOT, ref).is_file()


def test_runtime_reference_resolution_rejects_escape_attempts(tmp_path: Path) -> None:
    """Declared runtime references must stay inside the package root."""
    sandbox_root = tmp_path / "sandbox"
    sandbox_root.mkdir()
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("outside", encoding="utf-8")
    escape_link = sandbox_root / "escape.txt"
    escape_link.symlink_to(outside_file)

    with pytest.raises(FileNotFoundError):
        _resolve_package_reference(PLUGIN_ROOT, "skills/missing.md")
    with pytest.raises(ValueError):
        _resolve_package_reference(PLUGIN_ROOT, "../README.md")
    with pytest.raises(ValueError):
        _resolve_package_reference(PLUGIN_ROOT, "/etc/passwd")
    with pytest.raises(ValueError):
        _resolve_package_reference(sandbox_root, "escape.txt")

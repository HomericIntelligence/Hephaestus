"""Tests for validation-style CLI parser helpers."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.cli.utils import create_validation_parser, resolve_repo_root

ISSUE_1418_FILES = [
    Path("hephaestus/validation/docstrings.py"),
    Path("hephaestus/validation/doc_config.py"),
    Path("hephaestus/validation/type_aliases.py"),
    Path("hephaestus/validation/complexity.py"),
    Path("hephaestus/validation/audit.py"),
    Path("hephaestus/validation/stale_scripts.py"),
    Path("hephaestus/validation/test_structure.py"),
    Path("hephaestus/validation/mypy_per_file.py"),
    Path("hephaestus/validation/cli_tier_docs.py"),
    Path("hephaestus/validation/coverage.py"),
    Path("hephaestus/validation/doc_policy.py"),
    Path("hephaestus/validation/python_version.py"),
    Path("hephaestus/validation/schema.py"),
    Path("hephaestus/validation/tier_labels.py"),
    Path("hephaestus/validation/markdown.py"),
    Path("hephaestus/version/consistency.py"),
]


def test_create_validation_parser_adds_standard_validation_flags() -> None:
    """Validation parsers include repo-root and JSON flags by default."""
    parser = create_validation_parser("check things")

    args = parser.parse_args(["--repo-root", "/tmp/project", "--json"])

    assert args.repo_root == Path("/tmp/project")
    assert args.json is True


def test_create_validation_parser_adds_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """Validation parsers include the shared version flag."""
    parser = create_validation_parser("check things", prog="demo")

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--version"])

    assert exc.value.code == 0
    assert "demo" in capsys.readouterr().out


def test_create_validation_parser_can_skip_repo_root() -> None:
    """No-repo-root validation CLIs can opt out of the standard root flag."""
    parser = create_validation_parser("check things", include_repo_root=False)

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--repo-root", "/tmp/project"])

    assert exc.value.code == 2


def test_create_validation_parser_preserves_parser_customization() -> None:
    """Call sites can preserve existing help metadata while sharing standard flags."""
    parser = create_validation_parser(
        "check things",
        prog="custom-check",
        usage="%(prog)s [flags] path",
        epilog="Example: %(prog)s path",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    help_text = parser.format_help()

    assert parser.prog == "custom-check"
    assert "usage: custom-check [flags] path" in help_text
    assert "Example: custom-check path" in help_text


def test_resolve_repo_root_prefers_explicit_path() -> None:
    """Explicit CLI roots take precedence over auto-detection."""
    explicit = Path("/tmp/project")

    with patch("hephaestus.cli.utils.get_repo_root") as mocked:
        assert resolve_repo_root(argparse.Namespace(repo_root=explicit)) == explicit

    mocked.assert_not_called()


def test_resolve_repo_root_auto_detects_when_missing() -> None:
    """Missing CLI roots fall back to the shared repository root detector."""
    detected = Path("/tmp/detected")

    with patch("hephaestus.cli.utils.get_repo_root", return_value=detected):
        assert resolve_repo_root(argparse.Namespace(repo_root=None)) == detected


def test_issue_1418_clis_use_canonical_validation_parser() -> None:
    """Issue-listed CLIs should not reintroduce parser boilerplate."""
    violations: list[str] = []
    for path in ISSUE_1418_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            func = node.func
            name = func.id if isinstance(func, ast.Name) else ""
            attr = func.attr if isinstance(func, ast.Attribute) else ""
            if name in {"add_json_arg", "add_version_arg"}:
                violations.append(f"{path}:{node.lineno}: direct {name} call")
            if name == "ArgumentParser" or attr == "ArgumentParser":
                violations.append(f"{path}:{node.lineno}: direct ArgumentParser call")
            if attr == "add_argument" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and first.value == "--repo-root":
                    violations.append(f"{path}:{node.lineno}: direct --repo-root registration")

    assert violations == []

"""Tests for hephaestus.agents.frontmatter."""

from __future__ import annotations

from pathlib import Path

from hephaestus.agents.frontmatter import (
    check_agent_file,
    extract_frontmatter_full,
    extract_frontmatter_parsed,
    extract_frontmatter_raw,
    extract_frontmatter_with_lines,
    validate_frontmatter,
)

_VALID_FM = (
    "---\nname: test-agent\ndescription: A test\ntools: Read,Write\nmodel: sonnet\n---\n# Body\n"
)
_NO_FM = "# Just markdown\n\nNo frontmatter here.\n"
_INVALID_YAML = "---\nkey: [unclosed\n---\n# Body\n"


class TestExtractFrontmatterRaw:
    """Tests for extract_frontmatter_raw()."""

    def test_extracts_content(self) -> None:
        result = extract_frontmatter_raw(_VALID_FM)
        assert result is not None
        assert "name: test-agent" in result

    def test_no_frontmatter_returns_none(self) -> None:
        assert extract_frontmatter_raw(_NO_FM) is None

    def test_empty_string(self) -> None:
        assert extract_frontmatter_raw("") is None


class TestExtractFrontmatterWithLines:
    """Tests for extract_frontmatter_with_lines()."""

    def test_returns_tuple(self) -> None:
        result = extract_frontmatter_with_lines(_VALID_FM)
        assert result is not None
        _fm_text, start, end = result
        assert start == 1
        assert end > start

    def test_no_frontmatter_returns_none(self) -> None:
        assert extract_frontmatter_with_lines(_NO_FM) is None


class TestExtractFrontmatterParsed:
    """Tests for extract_frontmatter_parsed()."""

    def test_parses_to_dict(self) -> None:
        result = extract_frontmatter_parsed(_VALID_FM)
        assert result is not None
        _text, data = result
        assert data["name"] == "test-agent"
        assert data["model"] == "sonnet"

    def test_no_frontmatter_returns_none(self) -> None:
        assert extract_frontmatter_parsed(_NO_FM) is None

    def test_invalid_yaml_returns_none(self) -> None:
        assert extract_frontmatter_parsed(_INVALID_YAML) is None


class TestExtractFrontmatterFull:
    """Tests for extract_frontmatter_full()."""

    def test_returns_four_tuple(self) -> None:
        result = extract_frontmatter_full(_VALID_FM)
        assert result is not None
        assert len(result) == 4
        _text, data, start, _end = result
        assert isinstance(data, dict)
        assert start == 1

    def test_no_frontmatter_returns_none(self) -> None:
        assert extract_frontmatter_full(_NO_FM) is None


class TestValidateFrontmatter:
    """Tests for validate_frontmatter()."""

    def test_valid_frontmatter(self) -> None:
        fm = {"name": "agent", "description": "desc", "tools": "Read", "model": "sonnet"}
        errors = validate_frontmatter(fm)
        assert errors == []

    def test_missing_required_field(self) -> None:
        fm = {"name": "agent", "description": "desc", "tools": "Read"}
        errors = validate_frontmatter(fm)
        assert any("model" in e for e in errors)

    def test_wrong_type(self) -> None:
        fm = {"name": 123, "description": "desc", "tools": "Read", "model": "sonnet"}
        errors = validate_frontmatter(fm)
        assert any("name" in e for e in errors)

    def test_optional_wrong_type(self) -> None:
        fm = {
            "name": "agent",
            "description": "desc",
            "tools": "Read",
            "model": "sonnet",
            "level": "not-an-int",
        }
        errors = validate_frontmatter(fm)
        assert any("level" in e for e in errors)

    def test_custom_required_fields(self) -> None:
        errors = validate_frontmatter(
            {"title": "Hello"},
            required_fields={"title": str},
            optional_fields={},
        )
        assert errors == []

    def test_empty_frontmatter_all_missing(self) -> None:
        errors = validate_frontmatter({})
        assert len(errors) == 4  # name, description, tools, model


class TestCheckAgentFile:
    """Tests for check_agent_file()."""

    def test_valid_file(self, tmp_path: Path) -> None:
        f = tmp_path / "agent.md"
        f.write_text(_VALID_FM)
        is_valid, errors = check_agent_file(f)
        assert is_valid is True
        assert errors == []

    def test_no_frontmatter(self, tmp_path: Path) -> None:
        f = tmp_path / "agent.md"
        f.write_text(_NO_FM)
        is_valid, errors = check_agent_file(f)
        assert is_valid is False
        assert errors

    def test_missing_file(self, tmp_path: Path) -> None:
        is_valid, errors = check_agent_file(tmp_path / "nonexistent.md")
        assert is_valid is False
        assert errors

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "agent.md"
        f.write_text(_INVALID_YAML)
        is_valid, errors = check_agent_file(f)
        assert is_valid is False
        assert errors

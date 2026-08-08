"""Tests for hephaestus.agents.frontmatter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hephaestus.agents.frontmatter import (
    check_agent_file,
    extract_frontmatter_full,
    extract_frontmatter_parsed,
    extract_frontmatter_raw,
    extract_frontmatter_with_lines,
    validate_agents_main,
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


class TestValidateAgentsMain:
    """Smoke tests for the validate_agents_main() CLI."""

    def test_text_success(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "a.md").write_text(_VALID_FM)
        rc = validate_agents_main(["--agents-dir", str(agents_dir)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "OK" in out
        assert "1/1 agents valid" in out

    def test_text_invalid_agent(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "bad.md").write_text(_NO_FM)
        rc = validate_agents_main(["--agents-dir", str(agents_dir)])
        assert rc == 1
        assert "FAIL" in capsys.readouterr().out

    def test_json_success(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "a.md").write_text(_VALID_FM)
        rc = validate_agents_main(["--agents-dir", str(agents_dir), "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["total"] == 1
        assert payload["invalid"] == 0

    def test_json_invalid_agent(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "bad.md").write_text(_NO_FM)
        rc = validate_agents_main(["--agents-dir", str(agents_dir), "--json"])
        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert payload["invalid"] == 1

    def test_json_dir_not_found(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        missing = tmp_path / "nope"
        rc = validate_agents_main(["--agents-dir", str(missing), "--json"])
        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert "not found" in payload["error"]

    def test_text_dir_not_found(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = validate_agents_main(["--agents-dir", str(tmp_path / "nope")])
        assert rc == 1
        assert "not found" in capsys.readouterr().err

    def test_json_no_files(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        rc = validate_agents_main(["--agents-dir", str(empty), "--json"])
        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert "no agent files" in payload["error"]

    def test_text_no_files(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        rc = validate_agents_main(["--agents-dir", str(empty)])
        assert rc == 1
        assert "No agent files" in capsys.readouterr().err

    def test_default_agents_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from hephaestus.utils import helpers

        agents_dir = tmp_path / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "a.md").write_text(_VALID_FM)
        monkeypatch.setattr(helpers, "get_repo_root", lambda: tmp_path)
        assert validate_agents_main([]) == 0

"""Tests for hephaestus.validation.schema."""

import json
import re
from pathlib import Path

import pytest

from hephaestus.validation.schema import (
    check_files,
    load_schema_map,
    resolve_schema,
    validate_file,
)


@pytest.fixture()
def simple_schema(tmp_path: Path) -> dict:
    """Create a simple JSON schema for testing."""
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "version": {"type": "integer"},
        },
        "required": ["name"],
    }
    schema_file = tmp_path / "schema.json"
    schema_file.write_text(json.dumps(schema))
    return schema


@pytest.fixture()
def schema_map(tmp_path: Path) -> list[tuple[re.Pattern, Path]]:
    """Create a simple schema mapping."""
    return [
        (re.compile(r"^config/.*\.yaml$"), tmp_path / "schema.json"),
    ]


class TestLoadSchemaMap:
    """Tests for load_schema_map()."""

    def test_loads_mapping(self, tmp_path: Path) -> None:
        """Loads pattern-to-schema mapping from JSON file."""
        map_file = tmp_path / "map.json"
        map_file.write_text(
            json.dumps(
                [
                    ["^config/.*\\.yaml$", "schemas/config.schema.json"],
                    ["^models/.*\\.yaml$", "schemas/model.schema.json"],
                ]
            )
        )
        result = load_schema_map(map_file)
        assert len(result) == 2
        assert result[0][1] == Path("schemas/config.schema.json")

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        """Missing file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_schema_map(tmp_path / "nonexistent.json")


class TestResolveSchema:
    """Tests for resolve_schema()."""

    def test_matches_pattern(self, tmp_path: Path, schema_map: list) -> None:
        """File matching a pattern returns the schema path."""
        config_file = tmp_path / "config" / "defaults.yaml"
        config_file.parent.mkdir(parents=True)
        config_file.touch()
        result = resolve_schema(config_file, tmp_path, schema_map)
        assert result == tmp_path / "schema.json"

    def test_no_match_returns_none(self, tmp_path: Path, schema_map: list) -> None:
        """File not matching any pattern returns None."""
        other_file = tmp_path / "other" / "file.yaml"
        other_file.parent.mkdir(parents=True)
        other_file.touch()
        result = resolve_schema(other_file, tmp_path, schema_map)
        assert result is None


class TestValidateFile:
    """Tests for validate_file()."""

    def test_valid_file(self, tmp_path: Path, simple_schema: dict) -> None:
        """Valid YAML passes validation."""
        pytest.importorskip("jsonschema")
        yaml_file = tmp_path / "valid.yaml"
        yaml_file.write_text("name: test\nversion: 1\n")
        errors = validate_file(yaml_file, simple_schema)
        assert errors == []

    def test_missing_required_field(self, tmp_path: Path, simple_schema: dict) -> None:
        """Missing required field is flagged."""
        pytest.importorskip("jsonschema")
        yaml_file = tmp_path / "invalid.yaml"
        yaml_file.write_text("version: 1\n")
        errors = validate_file(yaml_file, simple_schema)
        assert len(errors) >= 1
        assert any("name" in e for e in errors)

    def test_wrong_type(self, tmp_path: Path, simple_schema: dict) -> None:
        """Wrong type for a field is flagged."""
        pytest.importorskip("jsonschema")
        yaml_file = tmp_path / "wrong_type.yaml"
        yaml_file.write_text("name: test\nversion: not_a_number\n")
        errors = validate_file(yaml_file, simple_schema)
        assert len(errors) >= 1

    def test_missing_file(self, tmp_path: Path, simple_schema: dict) -> None:
        """Missing YAML file returns error about reading/parsing."""
        pytest.importorskip("jsonschema")
        errors = validate_file(tmp_path / "missing.yaml", simple_schema)
        assert len(errors) == 1
        assert "could not" in errors[0].lower()


class TestCheckFiles:
    """Tests for check_files()."""

    def test_valid_files_pass(self, tmp_path: Path) -> None:
        """Valid files return exit code 0."""
        pytest.importorskip("jsonschema")
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        schema_file = tmp_path / "schema.json"
        schema_file.write_text(json.dumps(schema))

        yaml_file = tmp_path / "config" / "test.yaml"
        yaml_file.parent.mkdir()
        yaml_file.write_text("name: hello\n")

        schema_map = [(re.compile(r"^config/.*\.yaml$"), schema_file)]
        exit_code, error_count = check_files([yaml_file], tmp_path, schema_map)
        assert exit_code == 0
        assert error_count == 0

    def test_dry_run_returns_zero(self, tmp_path: Path) -> None:
        """Dry run returns 0 even with errors."""
        pytest.importorskip("jsonschema")
        schema = {"type": "object", "required": ["name"]}
        schema_file = tmp_path / "schema.json"
        schema_file.write_text(json.dumps(schema))

        yaml_file = tmp_path / "config" / "bad.yaml"
        yaml_file.parent.mkdir()
        yaml_file.write_text("version: 1\n")

        schema_map = [(re.compile(r"^config/.*\.yaml$"), schema_file)]
        exit_code, error_count = check_files([yaml_file], tmp_path, schema_map, dry_run=True)
        assert exit_code == 0
        assert error_count >= 1

    def test_no_matching_schema_warns(self, tmp_path: Path) -> None:
        """File with no matching schema is warned, not failed."""
        yaml_file = tmp_path / "random.yaml"
        yaml_file.write_text("key: value\n")
        schema_map: list = []
        exit_code, _error_count = check_files([yaml_file], tmp_path, schema_map)
        assert exit_code == 0

    def test_empty_files_list(self, tmp_path: Path) -> None:
        """Empty files list returns 0."""
        exit_code, _error_count = check_files([], tmp_path, [])
        assert exit_code == 0


class TestMain:
    """Tests for hephaestus-validate-schemas CLI entry point (regression for #495)."""

    def test_empty_files_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No file arguments → no work → exit 0."""
        from hephaestus.validation.schema import main

        monkeypatch.setattr("sys.argv", ["hephaestus-validate-schemas"])
        assert main() == 0

    def test_missing_schema_map_returns_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Files passed without --schema-map exits 1 with a clear error."""
        from hephaestus.validation.schema import main

        f = tmp_path / "config.yaml"
        f.write_text("name: ok\n")
        monkeypatch.setattr("sys.argv", ["hephaestus-validate-schemas", str(f)])
        assert main() == 1
        assert "--schema-map is required" in capsys.readouterr().err

    def test_missing_schema_map_file_returns_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """A --schema-map pointing at a missing file exits 1."""
        from hephaestus.validation.schema import main

        target = tmp_path / "config.yaml"
        target.write_text("name: x\n")
        missing_map = tmp_path / "does-not-exist.json"
        monkeypatch.setattr(
            "sys.argv",
            ["hephaestus-validate-schemas", "--schema-map", str(missing_map), str(target)],
        )
        assert main() == 1
        assert "Could not load schema map" in capsys.readouterr().err

    def test_invalid_json_schema_map_returns_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """A malformed --schema-map JSON exits 1."""
        from hephaestus.validation.schema import main

        target = tmp_path / "config.yaml"
        target.write_text("name: x\n")
        bad_map = tmp_path / "bad.json"
        bad_map.write_text("{not valid json")
        monkeypatch.setattr(
            "sys.argv",
            ["hephaestus-validate-schemas", "--schema-map", str(bad_map), str(target)],
        )
        assert main() == 1
        assert "Could not load schema map" in capsys.readouterr().err

    def test_valid_file_returns_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A valid YAML file matching its schema exits 0."""
        pytest.importorskip("jsonschema")
        from hephaestus.validation.schema import main

        schema = tmp_path / "schema.json"
        schema.write_text(
            json.dumps(
                {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                }
            )
        )
        target_dir = tmp_path / "config"
        target_dir.mkdir()
        target = target_dir / "ok.yaml"
        target.write_text("name: alice\n")

        schema_map = tmp_path / "map.json"
        # The schema map JSON format is list[[pattern, schema_path]].
        schema_map.write_text(json.dumps([[r"^config/.*\.yaml$", str(schema)]]))

        monkeypatch.setattr(
            "sys.argv",
            [
                "hephaestus-validate-schemas",
                "--schema-map",
                str(schema_map),
                "--repo-root",
                str(tmp_path),
                str(target),
            ],
        )
        assert main() == 0

    def test_violating_file_returns_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file violating its schema exits 1."""
        pytest.importorskip("jsonschema")
        from hephaestus.validation.schema import main

        schema = tmp_path / "schema.json"
        schema.write_text(
            json.dumps(
                {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                }
            )
        )
        target_dir = tmp_path / "config"
        target_dir.mkdir()
        target = target_dir / "bad.yaml"
        target.write_text("notname: alice\n")  # missing required "name"

        schema_map = tmp_path / "map.json"
        schema_map.write_text(json.dumps([[r"^config/.*\.yaml$", str(schema)]]))

        monkeypatch.setattr(
            "sys.argv",
            [
                "hephaestus-validate-schemas",
                "--schema-map",
                str(schema_map),
                "--repo-root",
                str(tmp_path),
                str(target),
            ],
        )
        assert main() == 1

    def test_dry_run_returns_zero_even_on_violations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--dry-run reports errors but returns 0."""
        pytest.importorskip("jsonschema")
        from hephaestus.validation.schema import main

        schema = tmp_path / "schema.json"
        schema.write_text(
            json.dumps(
                {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                }
            )
        )
        target_dir = tmp_path / "config"
        target_dir.mkdir()
        target = target_dir / "bad.yaml"
        target.write_text("notname: alice\n")

        schema_map = tmp_path / "map.json"
        schema_map.write_text(json.dumps([[r"^config/.*\.yaml$", str(schema)]]))

        monkeypatch.setattr(
            "sys.argv",
            [
                "hephaestus-validate-schemas",
                "--schema-map",
                str(schema_map),
                "--repo-root",
                str(tmp_path),
                "--dry-run",
                str(target),
            ],
        )
        assert main() == 0

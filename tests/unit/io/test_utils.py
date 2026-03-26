#!/usr/bin/env python3
"""Tests for I/O utilities."""

import json
from pathlib import Path

import pytest

from hephaestus.io.utils import (
    _detect_format,
    ensure_directory,
    load_data,
    read_file,
    safe_write,
    save_data,
    write_file,
    write_secure,
)


class TestEnsureDirectory:
    """Tests for ensure_directory."""

    def test_creates_nested_directories(self, tmp_path: Path) -> None:
        """Nested directories are created."""
        target = tmp_path / "a" / "b" / "c"
        assert ensure_directory(target)
        assert target.exists()

    def test_existing_directory(self, tmp_path: Path) -> None:
        """Existing directory returns True without error."""
        assert ensure_directory(tmp_path)

    def test_string_path(self, tmp_path: Path) -> None:
        """Accepts string path."""
        target = str(tmp_path / "str_dir")
        assert ensure_directory(target)

    def test_raises_on_failure(self, tmp_path: Path) -> None:
        """Raises OSError when directory cannot be created."""
        # Create a file where a directory needs to exist
        blocker = tmp_path / "blocker"
        blocker.write_text("I am a file, not a dir")
        with pytest.raises(OSError):
            ensure_directory(blocker / "subdir")


class TestReadFile:
    """Tests for read_file."""

    def test_reads_text(self, tmp_path: Path) -> None:
        """Reads text file content."""
        f = tmp_path / "hello.txt"
        f.write_text("hello world")
        assert read_file(f) == "hello world"

    def test_reads_binary(self, tmp_path: Path) -> None:
        """Reads binary file content."""
        f = tmp_path / "data.bin"
        f.write_bytes(b"\x00\x01\x02")
        assert read_file(f, mode="rb") == b"\x00\x01\x02"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        """Raises FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError):
            read_file(tmp_path / "missing.txt")


class TestWriteFile:
    """Tests for write_file."""

    def test_writes_text(self, tmp_path: Path) -> None:
        """Writes text content."""
        f = tmp_path / "out.txt"
        assert write_file(f, "content")
        assert f.read_text() == "content"

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        """Creates missing parent directories."""
        f = tmp_path / "sub" / "file.txt"
        assert write_file(f, "hi")
        assert f.exists()


class TestSafeWrite:
    """Tests for safe_write."""

    def test_writes_text_content(self, tmp_path: Path) -> None:
        """Writes text content to file."""
        f = tmp_path / "test.txt"
        assert safe_write(f, "Hello World")
        assert f.read_text() == "Hello World"

    def test_writes_binary_content(self, tmp_path: Path) -> None:
        """Writes bytes content to file."""
        f = tmp_path / "test.bin"
        assert safe_write(f, b"\xff\xfe")
        assert f.read_bytes() == b"\xff\xfe"

    def test_backup_created(self, tmp_path: Path) -> None:
        """Backup file is created for existing files."""
        f = tmp_path / "test.txt"
        f.write_text("original")
        safe_write(f, "updated", backup=True)
        backup = f.with_suffix(".txt.bak")
        assert backup.exists()
        assert backup.read_text() == "original"

    def test_no_backup(self, tmp_path: Path) -> None:
        """No backup created when backup=False."""
        f = tmp_path / "test.txt"
        f.write_text("original")
        safe_write(f, "updated", backup=False)
        assert not f.with_suffix(".txt.bak").exists()


class TestWriteSecure:
    """Tests for write_secure."""

    def test_writes_content(self, tmp_path: Path) -> None:
        """Writes text content to file."""
        f = tmp_path / "secret.txt"
        write_secure(f, "sensitive data")
        assert f.read_text() == "sensitive data"

    def test_default_permissions(self, tmp_path: Path) -> None:
        """Default permissions are 0o600 (owner read/write only)."""
        f = tmp_path / "secret.txt"
        write_secure(f, "data")
        assert oct(f.stat().st_mode & 0o777) == oct(0o600)

    def test_custom_permissions(self, tmp_path: Path) -> None:
        """Custom permissions are applied."""
        f = tmp_path / "readable.txt"
        write_secure(f, "data", permissions=0o644)
        assert oct(f.stat().st_mode & 0o777) == oct(0o644)

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        """Creates missing parent directories."""
        f = tmp_path / "sub" / "dir" / "secret.txt"
        write_secure(f, "data")
        assert f.exists()

    def test_string_path(self, tmp_path: Path) -> None:
        """Accepts string path."""
        f = str(tmp_path / "secret.txt")
        write_secure(f, "data")
        assert Path(f).read_text() == "data"

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        """Overwrites existing file content."""
        f = tmp_path / "secret.txt"
        f.write_text("old")
        write_secure(f, "new")
        assert f.read_text() == "new"


class TestDetectFormat:
    """Tests for _detect_format helper."""

    def test_json_extension(self, tmp_path: Path) -> None:
        """JSON extension detected."""
        assert _detect_format(tmp_path / "f.json", None) == "json"

    def test_yaml_extension(self, tmp_path: Path) -> None:
        """Yaml extension detected."""
        assert _detect_format(tmp_path / "f.yaml", None) == "yaml"

    def test_yml_extension(self, tmp_path: Path) -> None:
        """Yml extension detected."""
        assert _detect_format(tmp_path / "f.yml", None) == "yaml"

    def test_pkl_extension(self, tmp_path: Path) -> None:
        """Pkl extension detected."""
        assert _detect_format(tmp_path / "f.pkl", None) == "pickle"

    def test_explicit_hint_overrides_extension(self, tmp_path: Path) -> None:
        """Explicit hint overrides file extension."""
        assert _detect_format(tmp_path / "f.txt", "json") == "json"

    def test_unknown_extension_raises(self, tmp_path: Path) -> None:
        """Unknown extension raises ValueError."""
        with pytest.raises(ValueError, match="Could not determine"):
            _detect_format(tmp_path / "f.xyz", None)

    def test_unsupported_format_hint_raises(self, tmp_path: Path) -> None:
        """Unsupported format_hint raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported format"):
            _detect_format(tmp_path / "f.txt", "csv")

    def test_unknown_format_hint_raises(self, tmp_path: Path) -> None:
        """Unknown format_hint raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported format"):
            _detect_format(tmp_path / "f.txt", "xml")


class TestLoadData:
    """Tests for load_data."""

    def test_load_json(self, tmp_path: Path) -> None:
        """Loads JSON data correctly."""
        f = tmp_path / "data.json"
        f.write_text(json.dumps({"key": "value"}))
        result = load_data(f)
        assert result == {"key": "value"}

    def test_load_yaml(self, tmp_path: Path) -> None:
        """Loads YAML data correctly."""
        f = tmp_path / "data.yaml"
        f.write_text("key: value\n")
        result = load_data(f)
        assert result == {"key": "value"}

    def test_unsafe_format_blocked_by_default(self, tmp_path: Path) -> None:
        """Loading unsafe format raises without explicit opt-in."""
        # Create a minimal valid pickle (empty dict) without importing pickle in prod path
        f = tmp_path / "data.pkl"
        f.write_bytes(b"\x80\x04\x95\x06\x00\x00\x00\x00\x00\x00\x00}\x94.")  # pickle.dumps({})
        with pytest.raises(ValueError, match="unsafe deserialization"):
            load_data(f)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        """Raises FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError):
            load_data(tmp_path / "missing.json")

    def test_format_hint_overrides_extension(self, tmp_path: Path) -> None:
        """format_hint takes precedence over file extension."""
        f = tmp_path / "data.txt"
        f.write_text(json.dumps({"a": 1}))
        result = load_data(f, format_hint="json")
        assert result == {"a": 1}

    def test_unsupported_format_hint_raises(self, tmp_path: Path) -> None:
        """Unsupported format_hint raises ValueError."""
        f = tmp_path / "data.json"
        f.write_text(json.dumps({"a": 1}))
        with pytest.raises(ValueError, match="Unsupported format"):
            load_data(f, format_hint="csv")


class TestSaveData:
    """Tests for save_data."""

    def test_save_json(self, tmp_path: Path) -> None:
        """Saves data as JSON."""
        f = tmp_path / "out.json"
        assert save_data({"k": 1}, f)
        assert json.loads(f.read_text()) == {"k": 1}

    def test_save_yaml(self, tmp_path: Path) -> None:
        """Saves data as YAML."""
        f = tmp_path / "out.yaml"
        assert save_data({"k": 1}, f)
        assert "k:" in f.read_text()

    def test_unsafe_format_blocked_by_default(self, tmp_path: Path) -> None:
        """Saving to unsafe format raises without explicit opt-in."""
        f = tmp_path / "out.pkl"
        with pytest.raises(ValueError, match="unsafe deserialization"):
            save_data({"x": 1}, f)

    def test_unknown_extension_without_hint_raises(self, tmp_path: Path) -> None:
        """Unknown extension without format_hint raises ValueError."""
        f = tmp_path / "out.dat"
        with pytest.raises(ValueError, match="Could not determine"):
            save_data({"x": 1}, f)

    def test_unsupported_format_hint_raises(self, tmp_path: Path) -> None:
        """Unsupported format_hint raises ValueError."""
        f = tmp_path / "out.csv"
        with pytest.raises(ValueError, match="Unsupported format"):
            save_data({"x": 1}, f, format_hint="csv")

    def test_unknown_format_hint_raises(self, tmp_path: Path) -> None:
        """Unknown format_hint raises ValueError."""
        f = tmp_path / "out.xml"
        with pytest.raises(ValueError, match="Unsupported format"):
            save_data({"x": 1}, f, format_hint="xml")

    def test_unsupported_format_no_file_written(self, tmp_path: Path) -> None:
        """No file is written when format_hint is unsupported."""
        f = tmp_path / "out.csv"
        with pytest.raises(ValueError):
            save_data({"x": 1}, f, format_hint="csv")
        assert not f.exists()

    def test_creates_parent_dirs_json(self, tmp_path: Path) -> None:
        """Creates missing parent directories for JSON output."""
        f = tmp_path / "sub" / "deep" / "out.json"
        assert save_data({"a": 1}, f)
        assert json.loads(f.read_text()) == {"a": 1}

    def test_creates_parent_dirs_yaml(self, tmp_path: Path) -> None:
        """Creates missing parent directories for YAML output."""
        f = tmp_path / "sub" / "deep" / "out.yaml"
        assert save_data({"a": 1}, f)
        assert "a:" in f.read_text()

    def test_creates_parent_dirs_default_format(self, tmp_path: Path) -> None:
        """Creates missing parent directories for unknown extension (defaults to JSON)."""
        f = tmp_path / "new_dir" / "out.dat"
        assert save_data({"a": 1}, f)
        assert json.loads(f.read_text()) == {"a": 1}

    def test_raises_on_io_error(self, tmp_path: Path) -> None:
        """IOError is raised (not silently swallowed) when write fails."""
        # Use a path inside a non-existent parent where we've put a file as blocker
        blocker = tmp_path / "blocker"
        blocker.write_text("file")
        with pytest.raises(OSError):
            save_data({"x": 1}, blocker / "out.json")

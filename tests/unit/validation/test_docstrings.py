"""Tests for hephaestus.validation.docstrings."""

from pathlib import Path

from hephaestus.validation.docstrings import (
    FragmentFinding,
    format_json,
    format_report,
    is_genuine_fragment,
    main,
    scan_directory,
    scan_file,
)


class TestIsGenuineFragment:
    """Tests for is_genuine_fragment()."""

    def test_continuation_word(self) -> None:
        """Docstring starting with 'and' is a fragment."""
        assert is_genuine_fragment("and then do something") is True

    def test_preposition_start(self) -> None:
        """Docstring starting with 'across' is a fragment."""
        assert is_genuine_fragment("across multiple servers") is True

    def test_normal_sentence(self) -> None:
        """Normal sentence is not a fragment."""
        assert is_genuine_fragment("Calculate the total value.") is False

    def test_noun_phrase(self) -> None:
        """Noun phrase summary is not a fragment."""
        assert is_genuine_fragment("Total value calculator.") is False

    def test_empty_docstring(self) -> None:
        """Empty docstring is not a fragment."""
        assert is_genuine_fragment("") is False

    def test_whitespace_only(self) -> None:
        """Whitespace-only docstring is not a fragment."""
        assert is_genuine_fragment("   \n  \n  ") is False

    def test_capitalized_continuation(self) -> None:
        """Capitalized word is not a fragment even if it's a continuation word."""
        assert is_genuine_fragment("And then do something") is False

    def test_technical_start(self) -> None:
        """Technical token at start is not a fragment."""
        assert is_genuine_fragment("HTTP response handler") is False

    def test_multiline_first_line_checked(self) -> None:
        """Only the first non-empty line matters."""
        assert is_genuine_fragment("\n  across servers\n  more text") is True

    def test_non_continuation_lowercase(self) -> None:
        """Lowercase word that is not a continuation starter passes."""
        assert is_genuine_fragment("calculate the value") is False


class TestScanFile:
    """Tests for scan_file()."""

    def test_detects_fragment_in_function(self, tmp_path: Path) -> None:
        """Detects fragment docstring in a function."""
        py_file = tmp_path / "example.py"
        py_file.write_text('def foo():\n    """and then does something."""\n    pass\n')
        findings = scan_file(py_file, tmp_path)
        assert len(findings) == 1
        assert findings[0].context == "def foo"

    def test_clean_file_no_findings(self, tmp_path: Path) -> None:
        """Clean file returns no findings."""
        py_file = tmp_path / "clean.py"
        py_file.write_text('def foo():\n    """Calculate the total."""\n    return 1\n')
        findings = scan_file(py_file, tmp_path)
        assert findings == []

    def test_detects_fragment_in_class(self, tmp_path: Path) -> None:
        """Detects fragment docstring in a class."""
        py_file = tmp_path / "example.py"
        py_file.write_text('class Foo:\n    """across multiple instances."""\n    pass\n')
        findings = scan_file(py_file, tmp_path)
        assert len(findings) == 1
        assert findings[0].context == "class Foo"

    def test_missing_file(self, tmp_path: Path) -> None:
        """Missing file returns empty list."""
        findings = scan_file(tmp_path / "missing.py", tmp_path)
        assert findings == []

    def test_syntax_error_file(self, tmp_path: Path) -> None:
        """File with syntax errors returns empty list."""
        py_file = tmp_path / "bad.py"
        py_file.write_text("def foo(:\n")
        findings = scan_file(py_file, tmp_path)
        assert findings == []

    def test_module_docstring(self, tmp_path: Path) -> None:
        """Detects fragment in module-level docstring."""
        py_file = tmp_path / "mod.py"
        py_file.write_text('"""and also provides utilities."""\nx = 1\n')
        findings = scan_file(py_file, tmp_path)
        assert len(findings) == 1
        assert findings[0].context == "module"


class TestScanDirectory:
    """Tests for scan_directory()."""

    def test_scans_directory(self, tmp_path: Path) -> None:
        """Scans all .py files in a directory."""
        (tmp_path / "a.py").write_text('def a():\n    """and stuff."""\n    pass\n')
        (tmp_path / "b.py").write_text('def b():\n    """Normal docstring."""\n    pass\n')
        findings = scan_directory(tmp_path, tmp_path)
        assert len(findings) == 1

    def test_empty_directory(self, tmp_path: Path) -> None:
        """Empty directory returns no findings."""
        findings = scan_directory(tmp_path, tmp_path)
        assert findings == []

    def test_subdirectories(self, tmp_path: Path) -> None:
        """Recursively scans subdirectories."""
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "mod.py").write_text('def foo():\n    """across items."""\n    pass\n')
        findings = scan_directory(tmp_path, tmp_path)
        assert len(findings) == 1


class TestFormatReport:
    """Tests for format_report()."""

    def test_no_findings(self) -> None:
        """No findings produces clean message."""
        report = format_report([])
        assert "No docstring fragment violations found" in report

    def test_with_findings(self) -> None:
        """Findings are included in the report."""
        findings = [FragmentFinding("file.py", 10, "and stuff", "def foo")]
        report = format_report(findings)
        assert "1 genuine docstring fragment" in report
        assert "file.py:10" in report


class TestFormatJson:
    """Tests for format_json()."""

    def test_empty_list(self) -> None:
        """Empty list produces valid JSON."""
        result = format_json([])
        assert result == "[]"

    def test_with_findings(self) -> None:
        """Findings are serialized to JSON."""
        import json

        findings = [FragmentFinding("file.py", 5, "and x", "def bar")]
        data = json.loads(format_json(findings))
        assert len(data) == 1
        assert data[0]["file"] == "file.py"
        assert data[0]["line"] == 5


class TestMain:
    """Tests for main() CLI entry point."""

    def test_clean_returns_zero(self, tmp_path: Path, monkeypatch) -> None:
        """Clean code exits 0."""
        (tmp_path / "clean.py").write_text('def f():\n    """Calculate total."""\n    pass\n')
        monkeypatch.setattr(
            "sys.argv",
            ["check-docstrings", "--directory", str(tmp_path), "--repo-root", str(tmp_path)],
        )
        assert main() == 0

    def test_violations_returns_one(self, tmp_path: Path, monkeypatch) -> None:
        """Violations exit 1."""
        (tmp_path / "bad.py").write_text('def f():\n    """and stuff."""\n    pass\n')
        monkeypatch.setattr(
            "sys.argv",
            ["check-docstrings", "--directory", str(tmp_path), "--repo-root", str(tmp_path)],
        )
        assert main() == 1

    def test_json_output(self, tmp_path: Path, monkeypatch) -> None:
        """JSON output flag works."""
        (tmp_path / "clean.py").write_text('def f():\n    """Good docstring."""\n    pass\n')
        monkeypatch.setattr(
            "sys.argv",
            [
                "check-docstrings",
                "--directory",
                str(tmp_path),
                "--repo-root",
                str(tmp_path),
                "--json",
            ],
        )
        assert main() == 0

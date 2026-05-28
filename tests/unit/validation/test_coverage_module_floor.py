"""Tests for per-module coverage floor enforcement."""

from pathlib import Path
from xml.etree.ElementTree import Element, ElementTree

import pytest

from hephaestus.validation.coverage import parse_module_coverage


@pytest.fixture
def mock_coverage_xml(tmp_path: Path) -> Path:
    """Create a mock Cobertura XML with two <class> elements."""
    root = Element("coverage")
    root.set("line-rate", "0.85")
    root.set("branch-rate", "0.82")

    # Class with good branch coverage (above floor)
    class1 = Element("class")
    class1.set("filename", "hephaestus/validation/schema.py")
    class1.set("line-rate", "0.90")
    class1.set("branch-rate", "0.88")
    root.append(class1)

    # Class with poor coverage (below typical floor)
    class2 = Element("class")
    class2.set("filename", "hephaestus/validation/poorly_tested.py")
    class2.set("line-rate", "0.50")
    class2.set("branch-rate", "0.45")
    root.append(class2)

    coverage_file = tmp_path / "coverage.xml"
    tree = ElementTree(root)
    tree.write(str(coverage_file))
    return coverage_file


class TestParseModuleCoverage:
    """Tests for parse_module_coverage function."""

    def test_parses_module_coverage_from_xml(self, mock_coverage_xml: Path) -> None:
        """parse_module_coverage extracts per-module rates from Cobertura XML."""
        result = parse_module_coverage(mock_coverage_xml)

        assert "hephaestus/validation/schema.py" in result
        line_rate, branch_rate = result["hephaestus/validation/schema.py"]
        assert line_rate == 90.0
        assert branch_rate == 88.0

    def test_parses_all_classes(self, mock_coverage_xml: Path) -> None:
        """parse_module_coverage returns all <class> elements."""
        result = parse_module_coverage(mock_coverage_xml)

        assert len(result) == 2
        assert "hephaestus/validation/poorly_tested.py" in result

    def test_raises_file_not_found_for_missing_file(self, tmp_path: Path) -> None:
        """parse_module_coverage raises FileNotFoundError if file doesn't exist."""
        missing_file = tmp_path / "missing.xml"

        with pytest.raises(FileNotFoundError):
            parse_module_coverage(missing_file)

    def test_raises_on_parse_error(self, tmp_path: Path) -> None:
        """parse_module_coverage raises RuntimeError on malformed XML."""
        bad_xml = tmp_path / "bad.xml"
        bad_xml.write_text("<coverage>unclosed tag")

        with pytest.raises(RuntimeError):
            parse_module_coverage(bad_xml)

    def test_handles_missing_rates(self, tmp_path: Path) -> None:
        """parse_module_coverage handles missing rate attributes gracefully."""
        root = Element("coverage")
        class_elem = Element("class")
        class_elem.set("filename", "module_with_no_rates.py")
        # Don't set line-rate or branch-rate
        root.append(class_elem)

        coverage_file = tmp_path / "coverage.xml"
        tree = ElementTree(root)
        tree.write(str(coverage_file))

        result = parse_module_coverage(coverage_file)
        assert "module_with_no_rates.py" in result
        line_rate, branch_rate = result["module_with_no_rates.py"]
        assert line_rate == 0.0
        assert branch_rate == 0.0

    def test_converts_rates_to_percentages(self, tmp_path: Path) -> None:
        """parse_module_coverage converts decimal rates to percentages."""
        root = Element("coverage")
        class_elem = Element("class")
        class_elem.set("filename", "test_module.py")
        class_elem.set("line-rate", "0.75")
        class_elem.set("branch-rate", "0.50")
        root.append(class_elem)

        coverage_file = tmp_path / "coverage.xml"
        tree = ElementTree(root)
        tree.write(str(coverage_file))

        result = parse_module_coverage(coverage_file)
        line_rate, branch_rate = result["test_module.py"]
        assert line_rate == 75.0
        assert branch_rate == 50.0

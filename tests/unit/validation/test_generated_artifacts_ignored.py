"""Regression guards for generated repository-root artifacts."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_root_coverage_report_is_ignored() -> None:
    """Coverage XML is generated output, never reviewable source."""
    patterns = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "coverage.xml" in patterns
    assert not (REPO_ROOT / "coverage.xml").exists()

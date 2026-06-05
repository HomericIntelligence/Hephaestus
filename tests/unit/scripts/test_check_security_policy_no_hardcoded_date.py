"""Tests for scripts/check_security_policy_no_hardcoded_date.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from check_security_policy_no_hardcoded_date import find_hardcoded_dates


class TestFindHardcodedDates:
    """Tests for the ``find_hardcoded_dates`` SECURITY.md scanner."""

    def test_returns_empty_when_no_date(self, tmp_path: Path) -> None:
        f = tmp_path / "SECURITY.md"
        f.write_text("# Security Policy\n\nAs of the 0.9.x release line.\n")
        assert find_hardcoded_dates(f) == []

    def test_flags_iso_date_in_as_of_line(self, tmp_path: Path) -> None:
        f = tmp_path / "SECURITY.md"
        f.write_text("# Security Policy\n\nAs of 2026-05-24. Python >= 3.10 required.\n")
        hits = find_hardcoded_dates(f)
        assert len(hits) == 1
        assert hits[0][0] == 3
        assert "2026-05-24" in hits[0][1]

    def test_ignores_dates_not_prefixed_by_as_of(self, tmp_path: Path) -> None:
        f = tmp_path / "SECURITY.md"
        f.write_text("Released 2026-05-24 — see release notes.\n")
        assert find_hardcoded_dates(f) == []

    def test_returns_empty_when_file_missing(self, tmp_path: Path) -> None:
        assert find_hardcoded_dates(tmp_path / "SECURITY.md") == []

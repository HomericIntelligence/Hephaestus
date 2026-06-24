"""Tests for scripts/check_pip_audit_ledger_reminder.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from check_pip_audit_ledger_reminder import find_undocumented_suppressions

# Mirrors the real ledger layout: a bare '#' separates the header from the
# per-vuln paragraph (pixi.toml:106). The walk must NOT stop at the bare '#'.
_LEDGER_REAL_SHAPE = """[feature.lint.tasks]
# pip-audit suppression ledger. Each --ignore-vuln below MUST carry a reason.
#
# PYSEC-2025-183: disputed, unfixable transitive pyjwt CVE.
#   Re-review: drop if pyjwt publishes a fixed release.
pip-audit = "pip-audit --ignore-vuln PYSEC-2025-183"
"""

_LEDGER_WITH_DEP_VERSION_AND_TASK = """[feature.shared.pypi-dependencies]
pip-audit = ">=2.7,<3"

[feature.lint.tasks]
# pip-audit suppression ledger. Each --ignore-vuln below MUST carry a reason.
#
# PYSEC-2025-183: disputed, unfixable transitive pyjwt CVE.
#   Re-review: drop if pyjwt publishes a fixed release.
pip-audit = "pip-audit --ignore-vuln PYSEC-2025-183"
"""

_LEDGER_NO_TRIGGER = """[feature.lint.tasks]
# PYSEC-2025-183: disputed, unfixable transitive pyjwt CVE.
pip-audit = "pip-audit --ignore-vuln PYSEC-2025-183"
"""

_LEDGER_MULTILINE = '''[feature.lint.tasks]
# PYSEC-2025-183 documented here.
#   Re-review: drop later.
pip-audit = """pip-audit --ignore-vuln PYSEC-2025-183"""
'''

_LEDGER_TWO_IDS_ONE_BAD = """[feature.lint.tasks]
# PYSEC-2025-183: disputed.
#   Re-review: drop if fixed.
# PYSEC-2025-999: accepted.
pip-audit = "pip-audit --ignore-vuln PYSEC-2025-183 --ignore-vuln PYSEC-2025-999"
"""


class TestFindUndocumentedSuppressions:
    """Tests for find_undocumented_suppressions()."""

    def test_empty_when_no_suppressions(self, tmp_path: Path) -> None:
        f = tmp_path / "pixi.toml"
        f.write_text('[feature.lint.tasks]\npip-audit = "pip-audit"\n')
        assert find_undocumented_suppressions(f) == []

    def test_empty_with_bare_hash_separator_in_region(self, tmp_path: Path) -> None:
        # Bare '#' separator must not truncate the region — the real-shape ledger
        # passes. This is the case the prior block-walk design false-positived on.
        f = tmp_path / "pixi.toml"
        f.write_text(_LEDGER_REAL_SHAPE)
        assert find_undocumented_suppressions(f) == []

    def test_ignores_pip_audit_dependency_version_line(self, tmp_path: Path) -> None:
        # Regression for issue #1587: the dependency declaration must not be
        # mistaken for the lint task line when both appear in pixi.toml.
        f = tmp_path / "pixi.toml"
        f.write_text(_LEDGER_WITH_DEP_VERSION_AND_TASK)
        assert find_undocumented_suppressions(f) == []

    def test_flags_suppression_without_trigger(self, tmp_path: Path) -> None:
        f = tmp_path / "pixi.toml"
        f.write_text(_LEDGER_NO_TRIGGER)
        problems = find_undocumented_suppressions(f)
        assert [p[0] for p in problems] == ["PYSEC-2025-183"]

    def test_flags_suppression_with_no_comment(self, tmp_path: Path) -> None:
        f = tmp_path / "pixi.toml"
        f.write_text('[feature.lint.tasks]\npip-audit = "pip-audit --ignore-vuln PYSEC-2025-999"\n')
        problems = find_undocumented_suppressions(f)
        assert problems and problems[0][0] == "PYSEC-2025-999"

    def test_fails_closed_on_multiline_task(self, tmp_path: Path) -> None:
        # A triple-quoted task the single-line parser can't read must fail loudly,
        # never silently pass (Decision 4 guard).
        f = tmp_path / "pixi.toml"
        f.write_text(_LEDGER_MULTILINE)
        problems = find_undocumented_suppressions(f)
        assert problems and problems[0][0] == "<parser>"

    def test_flags_only_the_undocumented_of_two_ids(self, tmp_path: Path) -> None:
        f = tmp_path / "pixi.toml"
        f.write_text(_LEDGER_TWO_IDS_ONE_BAD)
        problems = find_undocumented_suppressions(f)
        assert [p[0] for p in problems] == ["PYSEC-2025-999"]

    def test_empty_when_file_missing(self, tmp_path: Path) -> None:
        assert find_undocumented_suppressions(tmp_path / "pixi.toml") == []

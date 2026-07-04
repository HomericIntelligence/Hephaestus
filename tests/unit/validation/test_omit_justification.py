r"""Guard: every coverage-omitted automation module must be unit-tested.

The automation modules in pyproject.toml[tool.coverage.run].omit are excluded
from coverage because their orchestration loops need a live claude/gh CLI. The
contract (documented in CLAUDE.md and the pyproject comment) is that each
module's pure-function helpers ARE unit-tested in tests/unit/automation/.

test_omit_allowlist.py freezes the *membership* of that list; this module
enforces the *justification*: omitting a module without a backing unit-test
suite fails CI loudly, closing the one-line bypass described in issue #1422.

PROXY HONESTY: this guard proves a backing test file IMPORTS the module and
contains at least one ``def test_``. That is a necessary-but-insufficient proxy
for "the module's pure helpers are meaningfully exercised" — it does not assert
that every helper is tested, only that a referencing suite exists.

Import detection uses ``ast`` (not a regex substring scan) so it catches every
import form, including the parenthesized multi-line
``from hephaestus.automation import (\n    planner,\n)`` used in
tests/unit/automation/test_automation_parsers.py and test_options_contract.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.validation.coverage import (
    _defines_a_test,
    _imported_automation_modules,
    find_unjustified_coverage_omits,
)

_UNIT_TEST_DIR = "tests/unit/automation"


def _project_root() -> Path:
    """Walk up from this file to the dir containing pyproject.toml.

    Mirrors test_omit_allowlist.get_pyproject_toml_path() rather than importing
    it: a cross-test-module import would depend on pytest pythonpath / a
    tests/__init__.py that this repo does not guarantee.
    """
    current = Path(__file__).resolve()
    while current != current.parent:
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
    raise RuntimeError("Could not find pyproject.toml")


class TestOmitJustification:
    """Every coverage-omitted module must have a backing unit-test suite."""

    def test_every_omitted_module_has_backing_unit_tests(self) -> None:
        root = _project_root()
        unjustified = find_unjustified_coverage_omits(root)
        if unjustified:
            pytest.fail(
                "Coverage-omitted automation modules with NO backing unit test "
                f"(issue #1422 invariant): {unjustified}\n"
                f"Each entry in pyproject.toml[tool.coverage.run].omit under "
                "hephaestus/automation/ must be imported by a test_*.py in "
                f"{_UNIT_TEST_DIR}/ that defines at least one test_ function. "
                "Add the missing unit tests for the module's pure helpers, or "
                "do not omit the module from coverage."
            )

    # --- predicate unit tests (prove the guard bites WITHOUT mutating the real
    # --- omit list, which the frozen test_omit_allowlist.py would also trip) ---

    def test_imports_detects_parenthesized_multiline(self) -> None:
        src = "from hephaestus.automation import (\n    implementer,\n    planner,\n)\n"
        assert _imported_automation_modules(src) == {"implementer", "planner"}

    def test_imports_detects_single_line_comma(self) -> None:
        src = "from hephaestus.automation import implementer, planner\n"
        assert "planner" in _imported_automation_modules(src)

    def test_imports_detects_dotted_and_aliased(self) -> None:
        assert _imported_automation_modules(
            "from hephaestus.automation.ci_driver import CIDriver\n"
        ) == {"ci_driver"}
        assert _imported_automation_modules("import hephaestus.automation.github_api as g\n") == {
            "github_api"
        }

    def test_imports_ignores_unrelated(self) -> None:
        assert _imported_automation_modules("import os\nfrom pathlib import Path\n") == set()

    def test_defines_a_test_detects_sync_and_async(self) -> None:
        assert _defines_a_test("def test_x():\n    pass\n")
        assert _defines_a_test("async def test_y():\n    pass\n")
        assert _defines_a_test("class TestThing:\n    def test_method(self):\n        pass\n")
        assert not _defines_a_test("def helper():\n    pass\n")

    def test_defines_a_test_ignores_non_collectable_nested_tests(self) -> None:
        src = (
            "def helper():\n"
            "    def test_nested():\n"
            "        pass\n"
            "\n"
            "class Helper:\n"
            "    def test_method(self):\n"
            "        pass\n"
        )
        assert not _defines_a_test(src)

    def test_guard_bites_on_synthetic_unbacked_module(self, tmp_path: Path) -> None:
        # A fake automation module with a backer that imports it but defines NO
        # test, plus one with no backer at all → both must be reported unjustified.
        (tmp_path / "pyproject.toml").write_text(
            "[tool.coverage.run]\nomit = [\n"
            '  "hephaestus/automation/backed.py",\n'
            '  "hephaestus/automation/unbacked.py",\n'
            "]\n"
        )
        td = tmp_path / _UNIT_TEST_DIR
        td.mkdir(parents=True)
        # imports `backed` but has no test_ function → does NOT justify it
        (td / "test_no_func.py").write_text("from hephaestus.automation import backed\nX = 1\n")
        assert find_unjustified_coverage_omits(tmp_path) == ["backed", "unbacked"]

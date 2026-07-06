"""Unit tests for the drive-green failing-PR predicate (#819).

The ``CIDriver``-method discovery scenarios that used to live here were
absorbed by the pipeline conversion (#1822): the discovery logic now lives in
``PRDiscovery`` (covered by ``test_pr_discovery_helpers.py``) and the
failing-PR sweep is coordinator-owned. What remains here is the single
canonical ``_pr_is_failing`` predicate (still exported from ``ci_driver`` for
the loop runner's SKIP gate) and the DRY single-definition guard from #1345.
"""

from __future__ import annotations

import ast
from pathlib import Path

from hephaestus.automation.ci_driver import _pr_is_failing


class TestPrIsFailingPredicate:
    """Tests for the _pr_is_failing predicate filter."""

    def test_pr_is_failing_returns_true_for_failure_conclusion(self) -> None:
        """A PR with FAILURE conclusion is failing."""
        pr = {
            "isDraft": False,
            "statusCheckRollup": [{"conclusion": "FAILURE"}],
            "mergeStateStatus": "CLEAN",
        }
        assert _pr_is_failing(pr)

    def test_pr_is_failing_returns_true_for_cancelled_conclusion(self) -> None:
        """A PR with CANCELLED conclusion is failing."""
        pr = {
            "isDraft": False,
            "statusCheckRollup": [{"conclusion": "CANCELLED"}],
            "mergeStateStatus": "CLEAN",
        }
        assert _pr_is_failing(pr)

    def test_pr_is_failing_returns_true_for_timed_out_conclusion(self) -> None:
        """A PR with TIMED_OUT conclusion is failing."""
        pr = {
            "isDraft": False,
            "statusCheckRollup": [{"conclusion": "TIMED_OUT"}],
            "mergeStateStatus": "CLEAN",
        }
        assert _pr_is_failing(pr)

    def test_pr_is_failing_returns_true_for_blocked_merge_state(self) -> None:
        """A PR with BLOCKED merge state is failing."""
        pr = {
            "isDraft": False,
            "statusCheckRollup": [],
            "mergeStateStatus": "BLOCKED",
        }
        assert _pr_is_failing(pr)

    def test_pr_is_failing_returns_false_for_draft_pr(self) -> None:
        """Draft PRs are excluded."""
        pr = {
            "isDraft": True,
            "statusCheckRollup": [{"conclusion": "FAILURE"}],
            "mergeStateStatus": "CLEAN",
        }
        assert not _pr_is_failing(pr)

    def test_pr_is_failing_returns_false_for_success_conclusion(self) -> None:
        """SUCCESS conclusion is not failing."""
        pr = {
            "isDraft": False,
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            "mergeStateStatus": "CLEAN",
        }
        assert not _pr_is_failing(pr)

    def test_pr_is_failing_returns_false_for_pending_conclusion(self) -> None:
        """PENDING conclusion is not considered failing (waiting for terminal state)."""
        pr = {
            "isDraft": False,
            "statusCheckRollup": [{"conclusion": "PENDING"}],
            "mergeStateStatus": "CLEAN",
        }
        assert not _pr_is_failing(pr)

    def test_pr_is_failing_returns_false_for_clean_merge_state_no_failures(self) -> None:
        """CLEAN merge state with no failing conclusions is not failing."""
        pr = {
            "isDraft": False,
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            "mergeStateStatus": "CLEAN",
        }
        assert not _pr_is_failing(pr)

    def test_pr_is_failing_handles_missing_rollup(self) -> None:
        """Missing statusCheckRollup is treated as empty."""
        pr = {
            "isDraft": False,
            "mergeStateStatus": "CLEAN",
        }
        assert not _pr_is_failing(pr)

    def test_pr_is_failing_handles_mixed_conclusions(self) -> None:
        """One FAILURE among SUCCESS checks means PR is failing."""
        pr = {
            "isDraft": False,
            "statusCheckRollup": [
                {"conclusion": "SUCCESS"},
                {"conclusion": "FAILURE"},
                {"conclusion": "SUCCESS"},
            ],
            "mergeStateStatus": "CLEAN",
        }
        assert _pr_is_failing(pr)


class TestFailingCheckPredicateSingleDefinition:
    """Regression guard: FAILING_CHECK_CONCLUSIONS and _pr_is_failing must not be duplicated.

    The DRY goal is satisfied when there is exactly one assignment to
    FAILING_CHECK_CONCLUSIONS and exactly one function named _pr_is_failing
    across the entire automation package.  This test catches future drift that
    would re-introduce the three-copy situation described in issue #1345.
    """

    _AUTOMATION_DIR = Path(__file__).parents[3] / "hephaestus" / "automation"

    def _count_assignments(self, target: str) -> list[Path]:
        """Return paths of automation modules that assign to *target* at module level."""
        hits: list[Path] = []
        for py_file in self._AUTOMATION_DIR.rglob("*.py"):
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        if isinstance(t, ast.Name) and t.id == target:
                            hits.append(py_file)
                elif (
                    isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and node.target.id == target
                ):
                    hits.append(py_file)
        return hits

    def _count_function_defs(self, name: str) -> list[Path]:
        """Return paths of automation modules that define a function named *name*."""
        hits: list[Path] = []
        for py_file in self._AUTOMATION_DIR.rglob("*.py"):
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == name:
                    hits.append(py_file)
        return hits

    def test_failing_check_conclusions_defined_exactly_once(self) -> None:
        """FAILING_CHECK_CONCLUSIONS must have a single canonical definition.

        The canonical home moved from ci_driver.py to ci_check_inspector.py in
        the CIDriver decomposition (#1357 / refs #1179, #1289): the constant
        belongs to the check-inspector that queries CI check state. ci_driver.py
        re-exports it (an import, not an assignment) for backward compatibility,
        so the DRY guard from #1345 still holds — exactly one assignment, no
        drift across the automation package.
        """
        hits = self._count_assignments("FAILING_CHECK_CONCLUSIONS")
        assert len(hits) == 1, (
            f"Expected exactly 1 definition of FAILING_CHECK_CONCLUSIONS, "
            f"found {len(hits)}: {[str(p) for p in hits]}"
        )
        assert hits[0].name == "ci_check_inspector.py", (
            f"Canonical definition must be in ci_check_inspector.py, not {hits[0].name}"
        )

    def test_pr_is_failing_defined_exactly_once(self) -> None:
        """_pr_is_failing must have a single canonical definition."""
        hits = self._count_function_defs("_pr_is_failing")
        assert len(hits) == 1, (
            f"Expected exactly 1 definition of _pr_is_failing, "
            f"found {len(hits)}: {[str(p) for p in hits]}"
        )
        assert hits[0].name == "ci_driver.py", (
            f"Canonical definition must be in ci_driver.py, not {hits[0].name}"
        )

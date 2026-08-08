"""Tests for the shell-free immutable-review coverage runner."""

from __future__ import annotations

from unittest.mock import call, patch

from hephaestus.automation import host_coverage


def test_main_runs_policy_only_after_unit_coverage_passes() -> None:
    """The policy check consumes the report only after pytest creates it."""
    with patch.object(host_coverage, "_run", side_effect=[0, 0]) as run:
        assert host_coverage.main() == 0

    assert run.call_args_list == [
        call(host_coverage._UNIT_COVERAGE_ARGS),
        call(host_coverage._COVERAGE_POLICY_ARGS),
    ]


def test_main_stops_when_unit_coverage_fails() -> None:
    """A failed test command cannot be hidden by a later policy command."""
    with patch.object(host_coverage, "_run", return_value=7) as run:
        assert host_coverage.main() == 7

    run.assert_called_once_with(host_coverage._UNIT_COVERAGE_ARGS)

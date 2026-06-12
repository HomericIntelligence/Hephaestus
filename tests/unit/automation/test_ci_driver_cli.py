"""CLI-shape tests for ci_driver: optional --issues + no gate (#820)."""

from __future__ import annotations

import sys

import pytest

from hephaestus.automation import ci_driver


def test_parse_args_no_issues_flag_enters_discovery_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: no-arg invocation enters discovery mode (args.issues == [])."""
    monkeypatch.setattr(sys, "argv", ["drive_prs_green.py"])
    args = ci_driver._parse_args()
    assert args.issues == []


def test_parse_args_issues_scopes_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2: --issues 814 scopes to that issue."""
    monkeypatch.setattr(sys, "argv", ["drive_prs_green.py", "--issues", "814"])
    args = ci_driver._parse_args()
    assert args.issues == [814]


def test_parse_args_issues_multiple_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2: --issues accepts multiple values."""
    monkeypatch.setattr(sys, "argv", ["drive_prs_green.py", "--issues", "814", "815"])
    args = ci_driver._parse_args()
    assert args.issues == [814, 815]


def test_parse_args_issues_flag_without_values_exits_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5: --issues with NO numbers exits 2 (argparse error)."""
    monkeypatch.setattr(sys, "argv", ["drive_prs_green.py", "--issues"])
    with pytest.raises(SystemExit) as exc:
        ci_driver._parse_args()
    assert exc.value.code == 2


def test_parse_args_force_run_flag_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3: --force-run is gone."""
    monkeypatch.setattr(sys, "argv", ["drive_prs_green.py", "--force-run"])
    with pytest.raises(SystemExit) as exc:
        ci_driver._parse_args()
    assert exc.value.code == 2


def test_no_final_loop_gate_symbol() -> None:
    """AC3: gate function symbol deleted."""
    assert not hasattr(ci_driver, "_final_loop_gate_passes")


def test_help_omits_force_run_and_loop_env(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC3: help-text sweep confirms no gate references."""
    monkeypatch.setattr(sys, "argv", ["drive_prs_green.py", "--help"])
    with pytest.raises(SystemExit):
        ci_driver._parse_args()
    out = capsys.readouterr().out
    assert "--force-run" not in out
    assert "HEPH_LOOP_INDEX" not in out
    assert "HEPH_CI_DRIVER_FORCE" not in out
    # `--issues` line must not advertise itself as required
    issues_lines = [line for line in out.splitlines() if "--issues" in line]
    assert issues_lines, "no --issues line in help output"
    for line in issues_lines:
        assert "required" not in line.lower()

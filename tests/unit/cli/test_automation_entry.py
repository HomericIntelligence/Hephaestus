"""Behavior tests for the automation console-script shims (issue #3528).

On a base install the `[automation]` extra dependencies are absent. The shim
must report that as a reproducible boundary error instead of a raw
`ModuleNotFoundError` traceback. With the extra installed the shim delegates
to the real automation module unchanged.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from collections.abc import Callable
from importlib.machinery import ModuleSpec

import pytest

from hephaestus.cli import automation_entry
from hephaestus.errors import ExitCode

ENTRY_POINTS: dict[str, str] = {
    "automation_loop_main": "hephaestus.automation.loop_runner",
    "plan_issues_main": "hephaestus.automation.planner",
    "implement_issues_main": "hephaestus.automation.implementer",
    "review_prs_main": "hephaestus.automation.pr_reviewer",
    "ensure_state_labels_main": "hephaestus.automation.ensure_state_labels",
    "reconcile_implementation_labels_main": "hephaestus.automation.reconcile_implementation_labels",
}


def _entry(name: str) -> Callable[[list[str] | None], int]:
    fn = getattr(automation_entry, name)
    assert callable(fn)
    return fn


@pytest.mark.parametrize("entry_name", sorted(ENTRY_POINTS))
def test_missing_extra_reports_configuration_error(
    entry_name: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A base install gets exit code CONFIGURATION and a recovery, not a traceback."""
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, package: str | None = None) -> ModuleSpec | None:
        if name == "pydantic":
            return None
        return real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    result = _entry(entry_name)(None)

    captured = capsys.readouterr()
    assert result == int(ExitCode.CONFIGURATION)
    assert "ConfigurationError" in captured.err
    assert captured.err.count("\n") <= 2
    # The recovery must name the install step that resolves the failure.
    assert "pip install" in captured.err
    assert "ModuleNotFoundError" not in captured.err


@pytest.mark.parametrize(("entry_name", "module_name"), sorted(ENTRY_POINTS.items()))
def test_present_extra_delegates_to_automation_main(
    entry_name: str,
    module_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the extra, the shim calls the mapped module's main unchanged."""
    seen: dict[str, object] = {}
    fake = types.ModuleType(module_name)

    def fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 7

    fake.main = fake_main  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module_name, fake)

    result = _entry(entry_name)(None)

    assert result == 7
    assert seen["argv"] is None


@pytest.mark.parametrize("entry_name", sorted(ENTRY_POINTS))
def test_system_exit_passes_through(
    entry_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SystemExit from the target (argparse --version/--help) keeps its code."""
    module_name = ENTRY_POINTS[entry_name]
    fake = types.ModuleType(module_name)

    def fake_main(argv: list[str] | None = None) -> int:
        raise SystemExit(3)

    fake.main = fake_main  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module_name, fake)

    assert _entry(entry_name)(None) == 3

"""Regression tests for automation package import side effects."""

from __future__ import annotations

import subprocess
import sys

import pytest

_PHASE_ENTRYPOINTS = (
    "hephaestus.automation.address_review",
    "hephaestus.automation.ci_driver",
    "hephaestus.automation.implementer",
    "hephaestus.automation.plan_reviewer",
    "hephaestus.automation.planner",
    "hephaestus.automation.pr_reviewer",
)


def _run_python(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_package_import_does_not_preload_phase_entrypoints() -> None:
    """Parent package import must not preload modules executed via ``python -m``."""
    result = _run_python(
        f"""
import sys
import hephaestus.automation

loaded = [name for name in {_PHASE_ENTRYPOINTS!r} if name in sys.modules]
if loaded:
    raise SystemExit("preloaded phase entrypoints: " + ", ".join(loaded))
"""
    )

    assert result.returncode == 0, result.stderr + result.stdout


def test_package_lazy_exports_remain_importable() -> None:
    """Existing package-level class imports should keep working."""
    result = _run_python(
        """
import hephaestus.automation as automation

for name in automation.__all__:
    assert getattr(automation, name).__name__ == name
"""
    )

    assert result.returncode == 0, result.stderr + result.stdout


def test_public_surface_pins_expected_symbols() -> None:
    """Pin __all__ so silent omission of peer classes (e.g. issue #775) regresses loudly."""
    import hephaestus.automation as automation

    expected = {
        "AddressReviewOptions",
        "AddressReviewer",
        "CIDriver",
        "CIDriverOptions",
        "DependencyResolver",
        "ImplementerOptions",
        "IssueImplementer",
        "IssueInfo",
        "PlanReviewer",
        "PlanReviewerOptions",
        "Planner",
        "PlannerOptions",
        "PRReviewer",
        "ReviewerOptions",
    }
    assert set(automation.__all__) == expected
    assert set(automation.__all__) <= set(automation._LAZY_EXPORTS), (
        "every __all__ entry must have a _LAZY_EXPORTS row"
    )


@pytest.mark.parametrize("module_name", _PHASE_ENTRYPOINTS)
def test_phase_entrypoint_help_has_no_runpy_preload_warning(module_name: str) -> None:
    """Phase modules should run through ``python -m`` without preload warnings."""
    result = subprocess.run(
        [sys.executable, "-W", "error::RuntimeWarning", "-m", module_name, "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "found in sys.modules after import of package" not in result.stderr

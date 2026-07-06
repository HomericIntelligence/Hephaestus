"""Regression tests for automation package import side effects and public API contract.

This module tests both import-side-effect safety and the public re-export surface.
Issue #799: the package must re-export every reviewer class and option model at
the root, so consumers do not need to reach into private-looking submodules.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

import hephaestus.automation as automation

EXPECTED_PUBLIC_SYMBOLS = {
    "AddressReviewOptions": "hephaestus.automation.models",
    "AddressReviewer": "hephaestus.automation.address_review",
    "AuditReviewer": "hephaestus.automation.audit_reviewer",
    "CIDriver": "hephaestus.automation.ci_driver",
    "CIDriverOptions": "hephaestus.automation.models",
    "DependencyResolver": "hephaestus.automation.dependency_resolver",
    "ImplementerOptions": "hephaestus.automation.models",
    "IssueImplementer": "hephaestus.automation.implementer",
    "IssueInfo": "hephaestus.automation.models",
    "PRReviewer": "hephaestus.automation.pr_reviewer",
    "PlanReviewer": "hephaestus.automation.plan_reviewer",
    "PlanReviewerOptions": "hephaestus.automation.models",
    "PlannerOptions": "hephaestus.automation.models",
    "ReviewerOptions": "hephaestus.automation.models",
}

_PHASE_ENTRYPOINTS = (
    "hephaestus.automation.address_review",
    "hephaestus.automation.ci_driver",
    "hephaestus.automation.implementer",
    "hephaestus.automation.plan_reviewer",
    "hephaestus.automation.planner",
    "hephaestus.automation.pr_reviewer",
)

_AUTOMATION_ROOT = Path(__file__).resolve().parents[3] / "hephaestus" / "automation"


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


def test_all_matches_expected() -> None:
    """Assert __all__ exactly matches the expected public API."""
    assert set(automation.__all__) == set(EXPECTED_PUBLIC_SYMBOLS)


def test_every_all_entry_resolves_via_package_root() -> None:
    """Assert every export is accessible and points to the correct source module.

    This test is stronger than a simple __name__ check: it verifies that the
    re-exported object is the same object (same identity) as in the source module.
    This ensures there are no accidental copies or rewraps of public classes.
    """
    for name, source_module in EXPECTED_PUBLIC_SYMBOLS.items():
        value = getattr(automation, name)
        source = importlib.import_module(source_module)
        assert value is getattr(source, name), (
            f"hephaestus.automation.{name} is not the same object as {source_module}.{name}"
        )


def test_dir_includes_all_entries() -> None:
    """Assert dir() introspection includes all public exports."""
    listing = dir(automation)
    for name in EXPECTED_PUBLIC_SYMBOLS:
        assert name in listing, f"{name!r} missing from dir(hephaestus.automation)"


def test_public_surface_pins_expected_symbols() -> None:
    """Pin __all__ so silent omission of peer classes (e.g. issue #775) regresses loudly."""
    import hephaestus.automation as automation

    expected = {
        "AddressReviewOptions",
        "AddressReviewer",
        "AuditReviewer",
        "CIDriver",
        "CIDriverOptions",
        "DependencyResolver",
        "ImplementerOptions",
        "IssueImplementer",
        "IssueInfo",
        "PlanReviewer",
        "PlanReviewerOptions",
        "PlannerOptions",
        "PRReviewer",
        "ReviewerOptions",
    }
    assert set(automation.__all__) == expected
    assert set(automation.__all__) <= set(automation._LAZY_EXPORTS), (
        "every __all__ entry must have a _LAZY_EXPORTS row"
    )


def test_production_modules_import_timeout_config_from_agent_config() -> None:
    """Production automation modules must not depend on the timeout shim."""
    offenders = []
    for path in _AUTOMATION_ROOT.rglob("*.py"):
        if path.name == "claude_timeouts.py":
            continue
        text = path.read_text(encoding="utf-8")
        if (
            "from .claude_timeouts import" in text
            or "from hephaestus.automation.claude_timeouts import" in text
            or "import claude_timeouts" in text
        ):
            offenders.append(path.relative_to(_AUTOMATION_ROOT).as_posix())

    assert offenders == [], (
        "production automation modules must import timeout config from "
        f"agent_config, not claude_timeouts: {offenders}"
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

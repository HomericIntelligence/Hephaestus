"""Console-script entry shims for the opt-in automation product layer.

The automation queue console scripts are declared in base ``[project.scripts]``
because Python packaging cannot make a console script conditional on an
optional extra. On a base install the runtime dependencies of
``hephaestus.automation`` are absent, so the generated script used to fail
with a raw ``ModuleNotFoundError`` traceback (issue #3528).

Each shim checks for the extra's runtime modules first. When one is absent the
shim reports a ``ConfigurationError`` through ``hephaestus.errors`` so the
user gets one reproducible stderr line, exit code ``CONFIGURATION``, and a
recovery instruction. With the extra installed, the shim delegates to the
real automation module unchanged.
"""

from __future__ import annotations

import importlib
import importlib.util

from hephaestus.errors import ConfigurationError, boundary_main

__all__ = [
    "automation_loop_main",
    "ensure_state_labels_main",
    "implement_issues_main",
    "plan_issues_main",
    "reconcile_implementation_labels_main",
    "review_prs_main",
]

# Import names of the runtime modules that the [automation] extra provides.
_EXTRA_MODULES = ("markdown_it", "pydantic", "sigstore")


def _require_automation_extra() -> None:
    """Raise ConfigurationError when a module from the [automation] extra is absent."""
    missing = [name for name in _EXTRA_MODULES if importlib.util.find_spec(name) is None]
    if missing:
        raise ConfigurationError(
            "this command needs the automation product layer "
            f"(missing modules: {', '.join(sorted(missing))})",
            recovery="install it with: pip install 'HomericIntelligence-Hephaestus[automation]'",
        )


def _run(module_name: str, argv: list[str] | None) -> int:
    """Report one automation console-script invocation at the module boundary.

    Args:
        module_name: The automation module that owns ``main``.
        argv: Command-line arguments, or None to read ``sys.argv``.

    Returns:
        The process exit code.

    """

    def entry(args: list[str] | None) -> int:
        _require_automation_extra()
        main = importlib.import_module(module_name).main
        return int(main(args))

    return boundary_main(entry, argv)


def automation_loop_main(argv: list[str] | None = None) -> int:
    """Entry point for the ``hephaestus-automation-loop`` console script."""
    return _run("hephaestus.automation.loop_runner", argv)


def plan_issues_main(argv: list[str] | None = None) -> int:
    """Entry point for the ``hephaestus-plan-issues`` console script."""
    return _run("hephaestus.automation.planner", argv)


def implement_issues_main(argv: list[str] | None = None) -> int:
    """Entry point for the ``hephaestus-implement-issues`` console script."""
    return _run("hephaestus.automation.implementer", argv)


def review_prs_main(argv: list[str] | None = None) -> int:
    """Entry point for the ``hephaestus-review-prs`` console script."""
    return _run("hephaestus.automation.pr_reviewer", argv)


def ensure_state_labels_main(argv: list[str] | None = None) -> int:
    """Entry point for the ``hephaestus-ensure-state-labels`` console script."""
    return _run("hephaestus.automation.ensure_state_labels", argv)


def reconcile_implementation_labels_main(argv: list[str] | None = None) -> int:
    """Entry point for the ``hephaestus-reconcile-implementation-labels`` console script."""
    return _run("hephaestus.automation.reconcile_implementation_labels", argv)

"""Regression tests for centralized logging setup delegation."""

from __future__ import annotations

import logging
from contextlib import ExitStack
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from hephaestus.constants import AUTOMATION_LOG_FORMAT, LOG_DATEFMT


class _StopAfterLoggingError(RuntimeError):
    """Stop a command after its logging setup boundary."""


@pytest.mark.parametrize(
    ("module_name", "callable_name", "kwargs", "expected_level"),
    [
        ("hephaestus.cli.utils", "configure_cli_logging", {"verbose": False}, logging.INFO),
        ("hephaestus.cli.utils", "configure_cli_logging", {"verbose": True}, logging.DEBUG),
        (
            "hephaestus.cli.utils",
            "configure_cli_logging",
            {"verbose": False, "log_format": "json"},
            logging.INFO,
        ),
        (
            "hephaestus.automation._review_utils",
            "setup_review_logging",
            {"verbose": False},
            logging.INFO,
        ),
        (
            "hephaestus.automation._review_utils",
            "setup_review_logging",
            {"verbose": True},
            logging.DEBUG,
        ),
        ("hephaestus.automation.loop_runner", "_setup_logging", {"verbose": False}, logging.INFO),
    ],
)
def test_cli_logging_helpers_delegate_to_shared_helper(
    module_name: str,
    callable_name: str,
    kwargs: dict[str, object],
    expected_level: int,
) -> None:
    """Standard CLI logging helpers route through ``setup_logging``."""
    module = import_module(module_name)
    helper = getattr(module, callable_name)

    with patch("hephaestus.cli.utils.setup_logging") as setup:
        helper(**kwargs)

    setup.assert_called_once_with(
        level=expected_level,
        format_string=AUTOMATION_LOG_FORMAT,
        datefmt=LOG_DATEFMT,
        primary_stream="stderr",
        json_format=kwargs.get("log_format") == "json",
    )


def test_implementer_setup_logging_routes_log_dir_to_shared_helper(tmp_path: Path) -> None:
    """Implementer logging keeps the run.log file handler but delegates setup."""
    module = import_module("hephaestus.automation.implementer")
    log_dir = tmp_path / "state"

    with patch.object(module, "setup_logging", Mock()) as setup:
        module._setup_logging(verbose=True, log_dir=log_dir)

    assert log_dir.is_dir()
    setup.assert_called_once_with(
        level=logging.DEBUG,
        log_file=str(log_dir / "run.log"),
        format_string=AUTOMATION_LOG_FORMAT,
        datefmt=LOG_DATEFMT,
        primary_stream="stderr",
        json_format=False,
    )


def test_tidy_logging_delegates_to_shared_helper() -> None:
    """Tidy logging forwards the selected explicit format to the shared helper."""
    module = import_module("hephaestus.github.tidy")

    with patch.object(module, "configure_cli_logging", Mock()) as configure:
        module._configure_logging(verbose=False, log_format="json")

    configure.assert_called_once_with(verbose=False, log_format="json")


def test_fleet_sync_main_delegates_logging_to_shared_helper() -> None:
    """Fleet sync CLI uses stderr-safe shared logging setup."""
    module = import_module("hephaestus.github.fleet_sync.cli")

    with (
        patch.object(module, "configure_github_throttle_from_args") as throttle,
        patch.object(module, "resolve_agent", return_value="claude"),
        patch.object(module, "resolve_fleet_config", return_value=("Org", [])),
        patch.object(module, "configure_cli_logging", Mock()) as configure,
    ):
        rc = module.main(["--verbose", "--log-format", "json"])

    assert rc == 0
    throttle.assert_called_once()
    configure.assert_called_once_with(verbose=True, log_format="json")


@pytest.mark.parametrize(
    "module_name",
    [
        "hephaestus.automation.ci_driver",
        "hephaestus.automation.planner",
        "hephaestus.automation.pr_reviewer",
        "hephaestus.automation.plan_reviewer",
    ],
)
def test_affected_cli_mains_forward_logging_arguments(module_name: str) -> None:
    """Affected commands send parsed logging options to the canonical helper."""
    module = import_module(module_name)
    args = SimpleNamespace(
        verbose=True,
        log_format="json",
        agent=None,
        disable_pi_automation=False,
        auth_status_timeout=1,
        pi_isolation_adapter=None,
        pi_dir=None,
        model=None,
        planner_model=None,
        reviewer_model=None,
    )

    with ExitStack() as stack:
        stack.enter_context(patch.object(module, "_parse_args", return_value=args))
        stack.enter_context(
            patch.object(module, "resolve_agent", side_effect=_StopAfterLoggingError())
        )
        configure = stack.enter_context(patch.object(module, "configure_cli_logging", Mock()))
        if hasattr(module, "configure_github_throttle_from_args"):
            stack.enter_context(patch.object(module, "configure_github_throttle_from_args"))

        with pytest.raises(_StopAfterLoggingError):
            module.main()

    configure.assert_called_once_with(verbose=True, log_format="json")

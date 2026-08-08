"""Regression tests for centralized logging setup delegation."""

from __future__ import annotations

import logging
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from hephaestus.constants import AUTOMATION_LOG_FORMAT, LOG_DATEFMT


@pytest.mark.parametrize(
    ("module_name", "callable_name", "kwargs", "expected_level"),
    [
        ("hephaestus.cli.utils", "configure_cli_logging", {"verbose": False}, logging.INFO),
        ("hephaestus.cli.utils", "configure_cli_logging", {"verbose": True}, logging.DEBUG),
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
        ("hephaestus.automation.ci_driver", "_setup_logging", {"verbose": True}, logging.DEBUG),
        ("hephaestus.automation.loop_runner", "_setup_logging", {"verbose": False}, logging.INFO),
        ("hephaestus.automation.planner", "_setup_logging", {"verbose": False}, logging.INFO),
        ("hephaestus.automation.pr_reviewer", "_setup_logging", {"verbose": False}, logging.INFO),
        ("hephaestus.automation.plan_reviewer", "_setup_logging", {"verbose": True}, logging.DEBUG),
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
    )


def test_tidy_logging_delegates_to_shared_helper() -> None:
    """Tidy logging uses the shared helper with its compact human format."""
    module = import_module("hephaestus.github.tidy")

    with patch.object(module, "setup_logging", Mock()) as setup:
        module._configure_logging(verbose=False)

    setup.assert_called_once_with(
        level=logging.INFO,
        format_string="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        primary_stream="stderr",
    )


def test_fleet_sync_main_delegates_logging_to_shared_helper() -> None:
    """Fleet sync CLI uses stderr-safe shared logging setup."""
    module = import_module("hephaestus.github.fleet_sync.cli")

    with (
        patch.object(module, "configure_github_throttle_from_args") as throttle,
        patch.object(module, "resolve_agent", return_value="claude"),
        patch.object(module, "resolve_fleet_config", return_value=("Org", [])),
        patch.object(module, "setup_logging", Mock()) as setup,
    ):
        rc = module.main(["--verbose"])

    assert rc == 0
    throttle.assert_called_once()
    setup.assert_called_once_with(
        level=logging.DEBUG,
        format_string="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        primary_stream="stderr",
    )

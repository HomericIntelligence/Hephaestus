"""Behavioral checks for automation ``--dry-run`` parser flags."""

from __future__ import annotations

import argparse
import importlib
import pkgutil

import pytest

from hephaestus import automation
from hephaestus.cli.utils import add_dry_run_arg


def _discover_dry_run_parsers() -> list[tuple[str, argparse.ArgumentParser]]:
    """Discover importable automation parsers exposing ``--dry-run``."""
    discovered: list[tuple[str, argparse.ArgumentParser]] = []
    for module_info in pkgutil.iter_modules(
        automation.__path__,
        prefix=f"{automation.__name__}.",
    ):
        module = importlib.import_module(module_info.name)
        builder = getattr(module, "_build_parser", None)
        if not callable(builder):
            continue
        parser = builder()
        if any("--dry-run" in action.option_strings for action in parser._actions):
            discovered.append((module_info.name, parser))
    return discovered


def _required_arguments(parser: argparse.ArgumentParser) -> list[str]:
    """Build minimal option arguments needed to parse a discovered parser."""
    argv: list[str] = []
    for action in parser._actions:
        if action.required and action.option_strings:
            value = next(iter(action.choices)) if action.choices else "1"
            argv.extend([action.option_strings[0], str(value)])
    return argv


def test_dry_run_parser_discovery_is_non_empty() -> None:
    """At least one automation parser is covered by discovery."""
    assert _discover_dry_run_parsers()


def test_add_dry_run_arg_toggles_boolean_state() -> None:
    """The isolated shared option defaults false and becomes true when present."""
    parser = argparse.ArgumentParser()
    add_dry_run_arg(parser)

    assert parser.parse_args([]).dry_run is False
    assert parser.parse_args(["--dry-run"]).dry_run is True


@pytest.mark.parametrize(("module_name", "parser"), _discover_dry_run_parsers())
def test_discovered_dry_run_flags_toggle_boolean_state(
    module_name: str,
    parser: argparse.ArgumentParser,
) -> None:
    """Every discovered dry-run flag toggles only the boolean state."""
    required = _required_arguments(parser)

    assert parser.parse_args(required).dry_run is False, module_name
    assert parser.parse_args([*required, "--dry-run"]).dry_run is True, module_name

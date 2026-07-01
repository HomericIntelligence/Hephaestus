"""Fleet-sync package split and facade compatibility tests."""

from __future__ import annotations

import importlib


def test_fleet_sync_facade_preserves_public_imports() -> None:
    """The old hephaestus.github.fleet_sync import surface stays available."""
    module = importlib.import_module("hephaestus.github.fleet_sync")

    assert callable(module.main)
    assert callable(module.resolve_fleet_config)
    assert callable(module.process_repo)
    assert module.PRStatus.READY.name == "READY"


def test_fleet_sync_is_split_into_focused_modules() -> None:
    """The monolith is now a package with responsibility-focused submodules."""
    for name in (
        "models",
        "gpg",
        "config",
        "pr_api",
        "git_ops",
        "conflict_resolver",
        "sync_coordinator",
        "cli",
    ):
        assert importlib.import_module(f"hephaestus.github.fleet_sync.{name}") is not None

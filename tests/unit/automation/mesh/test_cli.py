"""Tests for hephaestus.automation.mesh.cli."""

from __future__ import annotations

from typing import Any

from hephaestus.automation.mesh import cli
from hephaestus.automation.mesh.roles import ROLE_HANDLERS, resolve_handler


class TestResolveHandler:
    """Tests for the (domain, role) registry."""

    def test_known_pairs_registered(self) -> None:
        assert ("pipeline", "task-agent") in ROLE_HANDLERS
        assert ("pipeline", "chief-architect") in ROLE_HANDLERS
        assert ("research", "chief-architect") in ROLE_HANDLERS

    def test_unknown_pair_raises_with_known_list(self) -> None:
        import pytest

        with pytest.raises(KeyError, match=r"pipeline\.task-agent"):
            resolve_handler("nope", "nothing")

    def test_resolve_returns_handler(self) -> None:
        handler = resolve_handler("pipeline", "task-agent")
        assert hasattr(handler, "handle")


class TestCliMain:
    """Tests for the entry point's failure paths."""

    def test_missing_env_returns_2(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("MESH_DOMAIN", raising=False)
        monkeypatch.delenv("MESH_ROLE", raising=False)
        assert cli.main([]) == 2

    def test_unknown_role_returns_2(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MESH_DOMAIN", "nope")
        monkeypatch.setenv("MESH_ROLE", "nothing")
        assert cli.main([]) == 2

    def test_flags_override_env(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MESH_DOMAIN", "pipeline")
        monkeypatch.setenv("MESH_ROLE", "task-agent")
        # Unknown override loses to the registry check → exit 2, proving the
        # flag reached the config.
        assert cli.main(["--domain", "nope", "--role", "nothing"]) == 2

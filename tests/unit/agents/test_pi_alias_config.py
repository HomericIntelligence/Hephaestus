"""Security and CLI contracts for operator-owned Pi alias configuration."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from hephaestus.agents import runtime as agent_runtime

_RESOLVE_AGENT = agent_runtime.resolve_agent


def _write_alias_config(
    path: Path,
    text: str = 'provider = "private-provider"\nmodel = "private-model"\n',
) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_load_pi_alias_config_accepts_exact_private_toml(tmp_path: Path) -> None:
    """The two aliases load only from an owner-only regular TOML file."""
    path = _write_alias_config(tmp_path / "aliases.toml")

    assert agent_runtime.load_pi_alias_config(path) == agent_runtime.PiAliasConfig(
        provider="private-provider",
        model="private-model",
    )


@pytest.mark.parametrize(
    "text",
    [
        'provider = "private-provider"\n',
        'model = "private-model"\n',
        'provider = "private-provider"\nmodel = "private-model"\nextra = "no"\n',
        'provider = ""\nmodel = "private-model"\n',
        'provider = "private-provider"\nmodel = "   "\n',
        'provider = 7\nmodel = "private-model"\n',
        'provider = "private-provider"\nmodel = ["private-model"]\n',
        'provider = "unterminated\nmodel = "private-model"\n',
    ],
)
def test_load_pi_alias_config_rejects_invalid_schema(tmp_path: Path, text: str) -> None:
    """Missing, unknown, blank, and non-string values fail closed."""
    path = _write_alias_config(tmp_path / "aliases.toml", text)

    with pytest.raises(ValueError, match="Pi alias config"):
        agent_runtime.load_pi_alias_config(path)


def test_load_pi_alias_config_rejects_symlink(tmp_path: Path) -> None:
    """An alias file cannot be redirected after operator review."""
    target = _write_alias_config(tmp_path / "target.toml")
    link = tmp_path / "aliases.toml"
    link.symlink_to(target)

    with pytest.raises(OSError, match="symlink"):
        agent_runtime.load_pi_alias_config(link)


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership is unavailable")
def test_load_pi_alias_config_rejects_wrong_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only a file owned by the invoking user may provide private aliases."""
    path = _write_alias_config(tmp_path / "aliases.toml")
    monkeypatch.setattr(os, "getuid", lambda: path.stat().st_uid + 1)

    with pytest.raises(OSError, match="owned by the current user"):
        agent_runtime.load_pi_alias_config(path)


@pytest.mark.parametrize("mode", [0o400, 0o640, 0o644, 0o660, 0o666])
def test_load_pi_alias_config_requires_mode_0600(tmp_path: Path, mode: int) -> None:
    """Readability by peers and weaker owner modes are both rejected."""
    path = _write_alias_config(tmp_path / "aliases.toml")
    path.chmod(mode)

    with pytest.raises(OSError, match="mode 0600"):
        agent_runtime.load_pi_alias_config(path)


def test_agent_parser_registers_explicit_pi_disable_switch() -> None:
    """Every agent CLI receives a typed emergency-stop option."""
    parser = argparse.ArgumentParser()
    agent_runtime.add_agent_argument(parser)

    defaults = parser.parse_args([])
    assert defaults.disable_pi_automation is False
    assert defaults.auth_status_timeout == 10
    assert parser.parse_args(["--disable-pi-automation"]).disable_pi_automation is True
    assert parser.parse_args(["--auth-status-timeout", "17"]).auth_status_timeout == 17
    with pytest.raises(SystemExit):
        parser.parse_args(["--auth-status-timeout", "0"])


def test_resolve_agent_disable_pi_policy_fails_before_preflight(tmp_path: Path) -> None:
    """The explicit policy switch prevents any Pi provider or preflight process."""
    with pytest.raises(agent_runtime.PiAutomationDisabledError, match="disabled by CLI policy"):
        _RESOLVE_AGENT("pi", cwd=tmp_path, disable_pi_automation=True)


def test_direct_agent_model_uses_only_explicit_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removed environment names cannot influence provider model selection."""
    monkeypatch.setenv("HEPH_PI_MODEL", "poison-pi")
    monkeypatch.setenv("HEPH_IMPLEMENTER_MODEL", "poison-phase")

    for agent in agent_runtime.AGENT_CHOICES:
        assert agent_runtime.direct_agent_model(agent, "explicit") == "explicit"
        assert agent_runtime.direct_agent_model(agent, "") == ""
        assert (
            agent_runtime.direct_agent_model(agent, None, codex_default="established")
            == "established"
        )


def test_removed_codex_grace_environment_has_no_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Final-message grace is an injectable internal value, not environment config."""
    monkeypatch.setenv("HEPH_CODEX_FINAL_MESSAGE_GRACE", "999")

    assert agent_runtime._codex_final_message_grace_seconds() == 5.0


def test_provider_child_environments_are_named_allowlists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider subprocesses receive no ambient auth or unrelated parent state."""
    expected = "test-value"
    monkeypatch.setenv("GH_TOKEN", expected)
    monkeypatch.setenv("OPENAI_API_KEY", expected)
    monkeypatch.setenv("ANTHROPIC_API_KEY", expected)
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")

    claude = agent_runtime._claude_child_env()
    codex = agent_runtime._codex_child_env()

    for name in ("GH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "UNRELATED_SECRET"):
        assert name not in claude
        assert name not in codex

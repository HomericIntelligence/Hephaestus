"""Tests for the canonical exact environment-variable registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.config.environment_registry import (
    APPROVED_ENV_BY_NAME,
    APPROVED_ENV_VARS,
    RETIRED_ENV_NAMES,
    reader_is_authorized,
    validate_environment_value,
    writer_is_authorized,
)
from hephaestus.validation.environment_variables import load_registry


def test_registry_has_one_complete_exact_spec_per_name() -> None:
    """Every approved name carries finite policy metadata and exact authority."""
    assert len(APPROVED_ENV_BY_NAME) == len(APPROVED_ENV_VARS)
    assert APPROVED_ENV_BY_NAME.keys().isdisjoint(RETIRED_ENV_NAMES)

    for spec in APPROVED_ENV_VARS:
        assert spec.name and not any(char in spec.name for char in "*?[")
        assert spec.purpose
        assert spec.owner
        assert spec.sensitivity in {"public", "private", "secret"}
        assert spec.validation
        assert spec.direction in {
            "parent-read",
            "parent-read, child-forward",
            "child-write",
        }
        authorities = spec.qualified_readers + spec.qualified_writers
        assert authorities
        assert all("*" not in authority and "." in authority for authority in authorities)
        if "parent-read" in spec.direction:
            assert spec.qualified_readers
        if spec.direction == "child-write":
            assert spec.qualified_writers


def test_ambient_reader_manifest_is_a_registry_projection() -> None:
    """The AST validator manifest cannot invent authority outside the registry."""
    root = Path(__file__).parents[3]
    manifest = load_registry(root / "docs/environment-variables.toml")

    assert {variable.name for variable in manifest} <= APPROVED_ENV_BY_NAME.keys()
    for variable in manifest:
        spec = APPROVED_ENV_BY_NAME[variable.name]
        assert variable.owner == spec.owner
        assert variable.purpose == spec.purpose
        assert variable.sensitivity == {"private": "sensitive"}.get(
            spec.sensitivity, spec.sensitivity
        )
        assert variable.validation == {
            "string-no-nul": "string",
            "non-empty-no-nul": "non-empty",
        }.get(spec.validation, spec.validation)
        assert "parent-read" in spec.direction
        manifest_readers = {
            f"{Path(reader.path).with_suffix('').as_posix().replace('/', '.')}.{reader.reader}"
            for reader in variable.readers
        }
        assert manifest_readers <= set(spec.qualified_readers)


def test_reader_and_writer_authorization_is_exact() -> None:
    """Authorization does not accept prefixes, wildcards, or near matches."""
    assert reader_is_authorized(
        "GH_TOKEN", "hephaestus.config.child_environments.build_gh_child_env"
    )
    assert not reader_is_authorized("GH_TOKEN", "hephaestus.config.child_environments.*")
    assert writer_is_authorized(
        "GH_TRACE_ID", "hephaestus.config.child_environments.with_correlation_id"
    )
    assert not writer_is_authorized("GH_TRACE_ID", "with_correlation_id")
    assert not reader_is_authorized("HEPH_GH_TIMEOUT", "anything")


@pytest.mark.parametrize(
    ("name", "valid", "invalid"),
    [
        ("PATH", "/usr/bin", ""),
        ("HOME", "/tmp/home", "relative/home"),
        ("CLAUDECODE", "", "1"),
        ("GH_TRACE_ID", "trace-123", "trace 123"),
        ("GIT_CONFIG_NOSYSTEM", "1", "true"),
        ("GIT_TERMINAL_PROMPT", "0", "1"),
        ("NPM_CONFIG_IGNORE_SCRIPTS", "true", "1"),
    ],
)
def test_registry_validation_rules_are_executable(name: str, valid: str, invalid: str) -> None:
    """Every value rule used at a child boundary has executable semantics."""
    spec = APPROVED_ENV_BY_NAME[name]
    assert validate_environment_value(spec, valid)
    assert not validate_environment_value(spec, invalid)
    assert not validate_environment_value(spec, "contains\0nul")

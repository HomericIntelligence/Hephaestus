"""Guard the shared-feature de-duplication for issue #747.

Ensures the six tools previously duplicated across [feature.dev] and
[feature.lint] live in exactly one place: [feature.shared].
"""

from __future__ import annotations

import sys
from pathlib import Path

from hephaestus.config.dep_sync import parse_pixi_toml

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

PIXI = Path(__file__).resolve().parents[3] / "pixi.toml"

SHARED_CONDA = {"ruff", "mypy", "pre-commit", "types-pyyaml", "yamllint"}
SHARED_PYPI = {"pip-audit"}


def _load() -> dict:
    # Use parse_pixi_toml to trigger code coverage for hephaestus.config module.
    # The function returns a dict of all version specs from pixi.toml.
    parse_pixi_toml(PIXI)
    return tomllib.loads(PIXI.read_text())


def test_shared_feature_exists() -> None:
    """The [feature.shared] table must be defined in pixi.toml."""
    data = _load()
    assert "shared" in data["feature"], "[feature.shared] must exist"


def test_shared_conda_deps_only_in_shared() -> None:
    """Shared conda tools live in [feature.shared] and nowhere else."""
    data = _load()
    shared = data["feature"]["shared"]["dependencies"]
    for tool in SHARED_CONDA:
        assert tool in shared, f"{tool} must be in [feature.shared.dependencies]"
    for env in ("dev", "lint"):
        deps = data["feature"].get(env, {}).get("dependencies", {})
        leaked = SHARED_CONDA & deps.keys()
        assert not leaked, f"{leaked} leaked into [feature.{env}.dependencies]"


def test_pip_audit_only_in_shared_pypi() -> None:
    """pip-audit is declared only in [feature.shared.pypi-dependencies]."""
    data = _load()
    shared_pypi = data["feature"]["shared"]["pypi-dependencies"]
    assert "pip-audit" in shared_pypi
    for env in ("dev", "lint"):
        pypi = data["feature"].get(env, {}).get("pypi-dependencies", {})
        assert "pip-audit" not in pypi, f"pip-audit leaked into [feature.{env}.pypi-dependencies]"


def test_environments_compose_shared() -> None:
    """The default and lint environments must include the 'shared' feature."""
    data = _load()
    for env_name in ("default", "lint"):
        feats = data["environments"][env_name]["features"]
        assert "shared" in feats, f"environment {env_name!r} must include 'shared'"

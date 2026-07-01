"""Regression test for ADR-0001 install-boundary resolution (issue #1458).

`pip install HomericIntelligence-Hephaestus` (no [automation]) must NOT pull
pydantic. pydantic now lives only in the [automation] extra; hephaestus.nats
uses stdlib dataclasses. Companion to tests/unit/validation/test_import_surface.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

_PYPROJECT = Path(__file__).resolve().parents[3] / "pyproject.toml"


def _data() -> dict:
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))


def _name(spec: str) -> str:
    for sep in ("<", ">", "=", "!", "~", ";", "["):
        spec = spec.split(sep)[0]
    return spec.strip()


def test_pydantic_not_a_base_dependency() -> None:
    """A base install (no [automation]) must not pull pydantic (issue #1458)."""
    base = _data()["project"]["dependencies"]
    offenders = [d for d in base if _name(d) == "pydantic"]
    assert offenders == [], (
        f"pydantic must not be a base dependency (ADR-0001 / issue #1458); found {offenders}"
    )


def test_pydantic_is_declared_in_automation_extra() -> None:
    """Pydantic must stay declared in the [automation] extra (load-bearing)."""
    extras = _data()["project"]["optional-dependencies"]["automation"]
    assert any(_name(d) == "pydantic" for d in extras), (
        "pydantic must remain declared in the [automation] extra (load-bearing per ADR-0001)"
    )

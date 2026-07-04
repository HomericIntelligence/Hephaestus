"""Regression tests for ADR-0001 install-boundary resolution.

`pip install HomericIntelligence-Hephaestus` (no [automation]) and
`pip install HomericIntelligence-Hephaestus[nats]` must NOT pull pydantic.
pydantic lives only in [automation]; hephaestus.nats uses stdlib dataclasses.
"""

from __future__ import annotations

import subprocess
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


def test_nats_extra_declares_nats_py_without_pydantic() -> None:
    """The [nats] extra must stay sufficient without reintroducing pydantic."""
    extras = _data()["project"]["optional-dependencies"]["nats"]
    names = {_name(d) for d in extras}

    assert "nats-py" in names, "nats-py must remain declared in the [nats] extra"
    assert "pydantic" not in names, (
        "pydantic must stay out of the [nats] extra; hephaestus.nats uses "
        "stdlib dataclasses and the [automation] extra owns pydantic"
    )


def test_nats_public_api_imports_with_pydantic_blocked() -> None:
    """The [nats] import contract must not require pydantic."""
    code = (
        "import importlib.abc\n"
        "import sys\n"
        "\n"
        "for name in list(sys.modules):\n"
        "    if name == 'pydantic' or name.startswith('pydantic.'):\n"
        "        del sys.modules[name]\n"
        "\n"
        "class BlockPydantic(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, fullname, path, target=None):\n"
        "        if fullname == 'pydantic' or fullname.startswith('pydantic.'):\n"
        "            raise ImportError('pydantic blocked for [nats] install-contract test')\n"
        "        return None\n"
        "\n"
        "sys.meta_path.insert(0, BlockPydantic())\n"
        "from hephaestus.nats import EventRouter, NATSConfig, NATSEvent\n"
        "from hephaestus.nats import NATSSubscriberThread, load_nats_config, parse_subject\n"
        "router = EventRouter()\n"
        "NATSSubscriberThread(config=NATSConfig(), handler=router.dispatch)\n"
        "load_nats_config({'url': 'nats://localhost:4222', 'unknown': 'ignored'})\n"
        "NATSEvent(subject='hi.tasks.team.1.created', data={}, timestamp='', sequence=0)\n"
        "parse_subject('hi.tasks.team.1.created')\n"
        "loaded = sorted(\n"
        "    m for m in sys.modules\n"
        "    if m == 'pydantic' or m.startswith('pydantic.')\n"
        ")\n"
        "print('PYDANTIC_LOADED:' + ','.join(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    loaded_line = next(
        line for line in result.stdout.splitlines() if line.startswith("PYDANTIC_LOADED:")
    )
    payload = loaded_line.removeprefix("PYDANTIC_LOADED:")
    loaded = payload.split(",") if payload else []
    assert loaded == [], f"hephaestus.nats imported pydantic modules: {loaded}"

"""Regression test for ADR-0001 import surface contract.

Issue #711 acceptance criterion 3: `import hephaestus` MUST NOT pull
`curses`, `pydantic`, or `hephaestus.automation.*` into
sys.modules. Subprocess isolation is required because pytest itself
loads pydantic — we need a clean interpreter to make the assertion
meaningful.
"""

from __future__ import annotations

import subprocess
import sys


def test_base_import_does_not_load_automation_or_heavy_deps() -> None:
    """Verify `import hephaestus` does not load forbidden modules.

    Note: fcntl may be loaded indirectly by the Python standard library's pathlib
    on POSIX systems. We only check for direct imports from hephaestus code,
    not transitive loads from stdlib.
    """
    code = (
        "import sys\n"
        "before = set(sys.modules)\n"
        "import hephaestus  # noqa: F401\n"
        "after = set(sys.modules)\n"
        "new = after - before\n"
        "# Only check for modules directly loaded by hephaestus code.\n"
        "# fcntl may be transitively loaded by stdlib (pathlib on POSIX)\n"
        "# so we don't check it here. The boundary contract is that\n"
        "# hephaestus itself must not directly import curses or pydantic.\n"
        "leaked = sorted(\n"
        "    m for m in new\n"
        "    if m == 'curses'\n"
        "    or m == 'pydantic' or m.startswith('pydantic.')\n"
        "    or m.startswith('hephaestus.automation')\n"
        ")\n"
        "print('LEAKED:' + ','.join(leaked))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    leaked_line = next(line for line in result.stdout.splitlines() if line.startswith("LEAKED:"))
    payload = leaked_line.removeprefix("LEAKED:")
    leaked = payload.split(",") if payload else []
    assert leaked == [], (
        f"forbidden modules loaded by `import hephaestus` (ADR-0001 / issue #711 AC#3): {leaked}"
    )


def test_validation_package_and_console_script_targets_import() -> None:
    """Validation package exports and declared validation CLIs stay importable."""
    code = (
        "import importlib\n"
        "from pathlib import Path\n"
        "from hephaestus.io.toml import import_tomllib\n"
        "import hephaestus.validation\n"
        "tomllib = import_tomllib()\n"
        "assert tomllib is not None\n"
        "pyproject = tomllib.loads(Path('pyproject.toml').read_text(encoding='utf-8'))\n"
        "targets = pyproject['project']['scripts'].values()\n"
        "for target in targets:\n"
        "    module_name, _, attr = target.partition(':')\n"
        "    if not module_name.startswith('hephaestus.validation.'):\n"
        "        continue\n"
        "    module = importlib.import_module(module_name)\n"
        "    getattr(module, attr)\n"
    )
    subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )

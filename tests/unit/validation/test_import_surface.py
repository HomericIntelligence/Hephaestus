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

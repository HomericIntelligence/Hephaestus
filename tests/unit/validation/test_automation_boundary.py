"""Regression test for ADR-0001 boundary contract.

Library subpackages of `hephaestus` MUST NOT import from
`hephaestus.automation`. The dependency arrow points only one way:
automation -> library.
"""

from __future__ import annotations

from pathlib import Path

import hephaestus

LIB_ROOT = Path(hephaestus.__file__).parent


def test_no_library_subpackage_imports_automation() -> None:
    """Verify library packages do not import from hephaestus.automation."""
    violations: list[str] = []
    for py in LIB_ROOT.rglob("*.py"):
        # Skip the product layer itself.
        if "automation" in py.relative_to(LIB_ROOT).parts:
            continue
        text = py.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("from hephaestus.automation", "import hephaestus.automation")):
                violations.append(f"{py.relative_to(LIB_ROOT)}:{lineno}: {stripped}")
    assert not violations, (
        "library subpackages must not import from hephaestus.automation:\n" + "\n".join(violations)
    )

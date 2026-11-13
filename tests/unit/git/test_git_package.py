"""Behavior tests for the hephaestus.git package skeleton.

Issue #3508: the skeleton must import, and the base ``import hephaestus``
must stay lazy — it must not load ``hephaestus.git``. Subprocess isolation
gives a clean interpreter for the lazy-import assertion because pytest
itself imports hephaestus.
"""

from __future__ import annotations

import subprocess
import sys


def test_git_package_imports() -> None:
    """Verify `import hephaestus.git` succeeds."""
    import hephaestus.git  # noqa: F401


def test_base_import_does_not_load_git_package() -> None:
    """Verify `import hephaestus` does not load `hephaestus.git` (issue #3508)."""
    code = (
        "import sys\n"
        "import hephaestus  # noqa: F401\n"
        "loaded = sorted(m for m in sys.modules if m == 'hephaestus.git' "
        "or m.startswith('hephaestus.git.'))\n"
        "print('LOADED:' + ','.join(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    loaded_line = next(line for line in result.stdout.splitlines() if line.startswith("LOADED:"))
    payload = loaded_line.removeprefix("LOADED:")
    loaded = payload.split(",") if payload else []
    assert loaded == [], f"`import hephaestus` loaded hephaestus.git modules: {loaded}"


def test_constants_home_loads_without_models_or_pydantic() -> None:
    """Verify `hephaestus.git.constants` resolves without the product layer (#3509)."""
    code = (
        "import sys\n"
        "from hephaestus.git.constants import DEFAULT_STATE_DIR\n"
        "assert DEFAULT_STATE_DIR == 'build/.issue_implementer'\n"
        "loaded = [m for m in sys.modules if m == 'pydantic' "
        "or m.startswith('pydantic.') or m == 'hephaestus.automation' "
        "or m.startswith('hephaestus.automation.')]\n"
        "print('LOADED:' + ','.join(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    loaded_line = next(line for line in result.stdout.splitlines() if line.startswith("LOADED:"))
    payload = loaded_line.removeprefix("LOADED:")
    loaded = payload.split(",") if payload else []
    assert loaded == [], f"`hephaestus.git.constants` loaded product modules: {loaded}"


def test_writer_home_loads_without_models_or_pydantic() -> None:
    """Verify `hephaestus.git.writer` resolves without the product layer (#3509)."""
    code = (
        "import sys\n"
        "from hephaestus.git.writer import ImplementationWriterHandoff\n"
        "assert ImplementationWriterHandoff is not None\n"
        "loaded = [m for m in sys.modules if m == 'pydantic' "
        "or m.startswith('pydantic.') or m == 'hephaestus.automation' "
        "or m.startswith('hephaestus.automation.')]\n"
        "print('LOADED:' + ','.join(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    loaded_line = next(line for line in result.stdout.splitlines() if line.startswith("LOADED:"))
    payload = loaded_line.removeprefix("LOADED:")
    loaded = payload.split(",") if payload else []
    assert loaded == [], f"`hephaestus.git.writer` loaded product modules: {loaded}"

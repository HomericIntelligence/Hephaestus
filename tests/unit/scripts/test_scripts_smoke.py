"""Smoke tests for every ``scripts/*.py``.

Two guarantees per script:

1. The module is importable as a file (catches missing imports / syntax errors
   that ruff/mypy don't always surface — e.g. circulars under runtime).
2. ``python scripts/<name>.py --help`` exits 0 within a short timeout.

Some scripts are demos that don't take ``--help`` as a no-op (they execute
their work regardless). Those are listed in ``HELP_RUNS_REAL_WORK`` so we
only assert exit-0, not ``--help``-style usage output.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit.scripts.conftest import REPO_ROOT

HELP_TIMEOUT_SECONDS = 30

# Scripts that execute real work even when invoked with ``--help`` — for these
# we only assert exit-0, not that argparse-style usage text is printed.
HELP_RUNS_REAL_WORK = {
    "run_tests.py",  # legacy demo runner; doesn't use argparse
    "example_usage.py",  # demo that performs I/O on import
}


def _import_by_path(path: Path):
    spec = importlib.util.spec_from_file_location(f"_scripts_{path.stem}", path)
    assert spec is not None and spec.loader is not None, f"no spec for {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_is_importable(script_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every ``scripts/*.py`` must import cleanly.

    We isolate sys.argv so demo scripts that read it at import time don't
    pick up pytest's args, and we capture SystemExit so wrappers that call
    ``sys.exit()`` at module level still pass.
    """
    monkeypatch.setattr(sys, "argv", [str(script_path), "--help"])
    try:
        _import_by_path(script_path)
    except SystemExit as exc:
        assert exc.code in (0, None), f"{script_path.name} exited with {exc.code}"


def test_script_help_exits_zero(script_path: Path) -> None:
    """``python scripts/<name>.py --help`` must succeed within the timeout."""
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(REPO_ROOT), *([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])]
        ),
    }
    proc = subprocess.run(
        [sys.executable, str(script_path), "--help"],
        capture_output=True,
        text=True,
        timeout=HELP_TIMEOUT_SECONDS,
        env=env,
    )
    assert proc.returncode == 0, (
        f"{script_path.name} --help exited {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

    if script_path.name not in HELP_RUNS_REAL_WORK:
        combined = proc.stdout + proc.stderr
        assert combined.strip(), f"{script_path.name} --help produced no output"

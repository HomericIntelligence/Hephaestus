#!/usr/bin/env python3
"""Scaffold a new hephaestus subpackage skeleton with matching test directory.

Usage:
    python scripts/scaffold_subpackage.py <name> [--with-cli] [--dry-run] [--json]
    python scripts/scaffold_subpackage.py --root /path/to/repo <name>

Exit codes:
    0  Files created successfully (or --dry-run completed).
    1  Invalid name, existing target, or filesystem error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import NamedTuple

from hephaestus.cli.utils import add_json_arg

_VALID_NAME = re.compile(r"^[a-z][a-z0-9_]*$")

_PKG_INIT = '''\
"""{title} utilities for ProjectHephaestus."""
'''

_PKG_MODULE = '''\
"""{title} implementation module."""

from __future__ import annotations


def placeholder() -> None:
    """Placeholder function — replace with real implementation.

    Args: none

    Returns:
        None

    """
'''

_TEST_INIT = ""

_TEST_MODULE = '''\
"""Tests for hephaestus.{name}.{name}."""

from __future__ import annotations

from hephaestus.{name}.{name} import placeholder


class TestPlaceholder:
    """Smoke tests for the placeholder stub."""

    def test_placeholder_returns_none(self) -> None:
        assert placeholder() is None
'''

_SCRIPT_SHIM = '''\
#!/usr/bin/env python3
"""CLI shim — implementation lives in ``hephaestus.scripts_lib.{name}``."""

from hephaestus.scripts_lib.{name} import main

if __name__ == "__main__":
    raise SystemExit(main())
'''


class _Plan(NamedTuple):
    files: list[tuple[Path, str]]
    hints: list[str]


def _build_plan(name: str, root: Path, *, with_cli: bool) -> _Plan:
    title = name.replace("_", " ").title()
    files: list[tuple[Path, str]] = [
        (root / "hephaestus" / name / "__init__.py", _PKG_INIT.format(title=title)),
        (root / "hephaestus" / name / f"{name}.py", _PKG_MODULE.format(title=title)),
        (root / "tests" / "unit" / name / "__init__.py", _TEST_INIT),
        (root / "tests" / "unit" / name / f"test_{name}.py", _TEST_MODULE.format(name=name)),
    ]
    if with_cli:
        files.append((root / "scripts" / f"{name}.py", _SCRIPT_SHIM.format(name=name)))

    hints: list[str] = [
        f"Next steps for '{name}':",
        f"  1. Add 'hephaestus/{name}/' to the directory tree in README.md",
        f"  2. Implement hephaestus/{name}/{name}.py",
        f"  3. Run: pixi run pytest tests/unit/{name}/ -v",
    ]
    if with_cli:
        cmd = name.replace("_", "-")
        hints += [
            "  4. Register the console script in pyproject.toml [project.scripts]:",
            f'       hephaestus-{cmd} = "hephaestus.scripts_lib.{name}:main"',
            "     Then run: pixi run dev-install",
            "  5. Add the command to the README CLI table",
        ]
    return _Plan(files=files, hints=hints)


def _validate_name(name: str) -> str | None:
    """Return an error message if name is invalid, else None."""
    if not name:
        return "Name must not be empty"
    if not _VALID_NAME.match(name):
        return (
            f"Invalid name {name!r}: must be lowercase snake_case starting with a letter "
            "(only a-z, 0-9, _)"
        )
    return None


def main(argv: list[str] | None = None) -> int:
    """Entry point for the scaffold-subpackage CLI.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        Exit code: 0 on success, 1 on error.

    """
    parser = argparse.ArgumentParser(
        prog="hephaestus-scaffold-subpackage",
        description="Scaffold a new hephaestus subpackage skeleton with matching test directory.",
    )
    parser.add_argument(
        "name",
        help="Subpackage name: lowercase snake_case (e.g. my_utils)",
    )
    parser.add_argument(
        "--with-cli",
        action="store_true",
        help="Also generate a scripts/ shim for a CLI entry point",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned paths without writing any files",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repository root (defaults to the repo root auto-detected from this file's location)",
    )
    add_json_arg(parser)
    args = parser.parse_args(argv)

    error = _validate_name(args.name)
    if error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    root = args.root if args.root is not None else _default_root()
    plan = _build_plan(args.name, root, with_cli=args.with_cli)

    # Refuse to overwrite an existing target
    pkg_dir = root / "hephaestus" / args.name
    if pkg_dir.exists():
        print(
            f"Error: target directory {pkg_dir} already exists; refusing to overwrite.",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        msg = f"[dry-run] Would create {len(plan.files)} file(s) for '{args.name}':"
        if args.json:
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "name": args.name,
                        "files_planned": [str(p) for p, _ in plan.files],
                    }
                )
            )
        else:
            print(msg)
            for path, _ in plan.files:
                print(f"  {path}")
        return 0

    created: list[str] = []
    for path, content in plan.files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        created.append(str(path))

    if args.json:
        print(json.dumps({"name": args.name, "files_created": created}))
    else:
        print(f"Created {len(created)} file(s) for '{args.name}':")
        for p in created:
            print(f"  {p}")
        print()
        print("\n".join(plan.hints))

    return 0


def _default_root() -> Path:
    return Path(__file__).resolve().parents[2]


if __name__ == "__main__":
    raise SystemExit(main())

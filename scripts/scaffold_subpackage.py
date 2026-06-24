#!/usr/bin/env python3
"""CLI shim — implementation lives in ``hephaestus.scripts_lib.scaffold_subpackage``."""

from hephaestus.scripts_lib.scaffold_subpackage import main

if __name__ == "__main__":
    raise SystemExit(main())

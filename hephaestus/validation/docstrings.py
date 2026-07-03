"""Backward-compatibility shim.

Canonical implementation: :mod:`hephaestus.validation.docs.docstrings`.
Retained so existing import sites and the ``hephaestus-check-docstrings``
console script need no changes.
"""

import sys

from hephaestus.validation.docs.docstrings import (
    FragmentFinding as FragmentFinding,
    format_json as format_json,
    format_report as format_report,
    is_genuine_fragment as is_genuine_fragment,
    main as main,
    scan_directory as scan_directory,
    scan_file as scan_file,
)

__all__ = [
    "FragmentFinding",
    "format_json",
    "format_report",
    "is_genuine_fragment",
    "main",
    "scan_directory",
    "scan_file",
]


if __name__ == "__main__":
    sys.exit(main())

"""Backward-compatibility shim.

Canonical implementation: :mod:`hephaestus.validation.docs.doc_policy`.
Retained so existing import sites and the ``hephaestus-audit-doc-policy``
console script need no changes.
"""

import sys

from hephaestus.validation.docs.doc_policy import (
    EXCLUDED_PREFIXES as EXCLUDED_PREFIXES,
    POLICY_RULES as POLICY_RULES,
    Finding as Finding,
    Severity as Severity,
    format_json_report as format_json_report,
    format_text_report as format_text_report,
    main as main,
    scan_file as scan_file,
    scan_repository as scan_repository,
)

__all__ = [
    "EXCLUDED_PREFIXES",
    "POLICY_RULES",
    "Finding",
    "Severity",
    "format_json_report",
    "format_text_report",
    "main",
    "scan_file",
    "scan_repository",
]


if __name__ == "__main__":
    sys.exit(main())

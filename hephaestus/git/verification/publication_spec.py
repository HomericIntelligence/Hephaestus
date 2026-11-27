"""Publication-focused view of the canonical host verification specs.

The canonical argv values live in :mod:`hephaestus.git.verification.specs`.
This module groups the values a publication boundary consumes and owns the
publication timeout policy.
"""

from __future__ import annotations

from .specs import (
    FIRST_PUBLICATION_CHECK as FIRST_PUBLICATION_CHECK,
    FIRST_PUBLICATION_CHECK_ARGV as FIRST_PUBLICATION_CHECK_ARGV,
    HEPHAESTUS_REQUIRED_CHECK as HEPHAESTUS_REQUIRED_CHECK,
    HEPHAESTUS_REQUIRED_CHECK_ARGV as HEPHAESTUS_REQUIRED_CHECK_ARGV,
)

__all__ = [
    "FIRST_PUBLICATION_CHECK",
    "FIRST_PUBLICATION_CHECK_ARGV",
    "HEPHAESTUS_REQUIRED_CHECK",
    "HEPHAESTUS_REQUIRED_CHECK_ARGV",
    "HEPHAESTUS_REQUIRED_CHECK_TIMEOUT_S",
]

#: The required suite runs CI's formerly parallel jobs serially on a local
#: host, so it needs a wider bound than one generic pytest invocation.
HEPHAESTUS_REQUIRED_CHECK_TIMEOUT_S = 7200

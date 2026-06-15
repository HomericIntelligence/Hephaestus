"""Declared abstraction contracts for the automation pipeline.

Import directly from this module — not via ``hephaestus.automation`` —
to avoid defeating the lazy-export design of the package __init__.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ReviewerProtocol(Protocol):
    """Structural contract satisfied by all four reviewer classes.

    Verified: PRReviewer.run (pr_reviewer.py:396),
              AddressReviewer.run (address_review.py:350),
              AuditReviewer.run (audit_reviewer.py:197),
              PlanReviewer.run (plan_reviewer.py:99).
    """

    def run(self) -> Any: ...

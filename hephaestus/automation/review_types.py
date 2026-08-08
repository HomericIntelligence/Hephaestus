"""Pure review result value objects shared by agent runners and pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewVerdict:
    """Parsed review grade, normalized verdict, and unmodified source text."""

    grade: str | None
    verdict: str
    raw: str

    @property
    def is_go(self) -> bool:
        """Return whether the result is an unambiguous GO."""
        return self.verdict == "GO"

    @property
    def is_error(self) -> bool:
        """Return whether reviewer infrastructure failed."""
        return self.verdict == "ERROR"

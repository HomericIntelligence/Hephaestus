"""Fleet-sync data models and display constants."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto


@dataclass(frozen=True)
class Symbols:
    """Glyphs used in user-facing log output. Frozen for safe sharing across calls."""

    banner: str
    check: str
    arrow: str
    dash: str


UNICODE_SYMBOLS = Symbols(banner="══", check="✓", arrow="→", dash="—")
ASCII_SYMBOLS = Symbols(banner="==", check="*", arrow="->", dash="--")


class PRStatus(Enum):
    """Readiness classification for a pull request."""

    READY = auto()  # CI green, no conflicts -> merge
    OUTDATED = auto()  # CI pending/green, behind base -> rebase + re-sign
    CONFLICTED = auto()  # Has merge conflicts -> agent resolution
    FAILING = auto()  # CI failing -> skip
    UNKNOWN = auto()  # Can't determine -> skip


@dataclass
class PRInfo:
    """All information needed to act on a single pull request."""

    repo: str
    number: int
    title: str
    head_ref: str
    base_ref: str
    head_sha: str
    mergeable: str  # MERGEABLE | CONFLICTING | UNKNOWN
    merge_state: str  # CLEAN | BEHIND | DIRTY | BLOCKED | UNKNOWN
    ci_state: str  # SUCCESS | FAILURE | PENDING | UNKNOWN
    status: PRStatus = PRStatus.UNKNOWN
    conflict_files: list[str] = field(default_factory=list)

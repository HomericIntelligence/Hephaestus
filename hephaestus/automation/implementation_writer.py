"""Private capabilities for implementation-writer ownership."""

from __future__ import annotations

from pathlib import Path

_CONSTRUCTION_TOKEN = object()


class ImplementationWriterHandoff:
    """Opaque capability created only by this module's private factory."""

    __slots__ = ("_active", "_item_number", "_repo_root")

    def __init__(self, repo_root: Path, item_number: int, *, token: object) -> None:
        """Initialize one capability after validating the private token."""
        if token is not _CONSTRUCTION_TOKEN:
            raise TypeError("implementation writer handoff requires the private factory")
        self._repo_root = repo_root
        self._item_number = item_number
        self._active = False

    def _validate(self, repo_root: Path, item_number: int) -> None:
        if (
            not self._active
            or self._repo_root != repo_root.resolve()
            or self._item_number != item_number
        ):
            raise RuntimeError("implementation writer handoff is missing or inactive")


def _new_implementation_writer_handoff(
    repo_root: Path, item_number: int
) -> ImplementationWriterHandoff:
    """Create one inactive handoff for a SourceWorkspaceManager context."""
    return ImplementationWriterHandoff(repo_root, item_number, token=_CONSTRUCTION_TOKEN)


def _set_implementation_writer_handoff_active(
    handoff: ImplementationWriterHandoff, active: bool
) -> None:
    """Set handoff activity from the owning SourceWorkspaceManager context."""
    handoff._active = active

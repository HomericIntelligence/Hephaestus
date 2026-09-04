"""Shared capabilities for implementation-writer ownership."""

from __future__ import annotations

from pathlib import Path


class ImplementationWriterHandoff:
    """Opaque capability for one active implementation-writer handoff."""

    __slots__ = ("_active", "_item_number", "_repo_root")

    def __init__(self, repo_root: Path, item_number: int) -> None:
        """Initialize one inactive handoff capability."""
        self._repo_root = repo_root
        self._item_number = item_number
        self._active = False

    def _activate(self) -> None:
        self._active = True

    def _deactivate(self) -> None:
        self._active = False

    def _validate(self, repo_root: Path, item_number: int) -> None:
        if (
            not self._active
            or self._repo_root != repo_root.resolve()
            or self._item_number != item_number
        ):
            raise RuntimeError("implementation writer handoff is missing or inactive")

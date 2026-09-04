"""Private capabilities for implementation-writer ownership."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast

from hephaestus.utils.file_lock import file_lock

if TYPE_CHECKING:

    class ImplementationWriterHandoff:
        """Static type for the opaque implementation-writer capability."""

        def _validate(self, repo_root: Path, item_number: int, lock_path: Path) -> None: ...

        def _arm_direct_transition(
            self,
            *,
            path: Path,
            predecessor_generation: int,
            predecessor_revision: str,
            branch: str,
            base_sha: str,
        ) -> None: ...

        def _validate_direct_transition(
            self,
            *,
            path: Path,
            predecessor_revision: str,
            branch: str,
            base_sha: str,
        ) -> None: ...

        def _consume_direct_transition(
            self,
            *,
            path: Path,
            predecessor_revision: str,
            branch: str,
            base_sha: str,
        ) -> object: ...

        def _validate_consumed_direct_transition(
            self,
            evidence: object,
            *,
            path: Path,
            predecessor_generation: int,
            predecessor_revision: str,
            branch: str,
        ) -> None: ...


def _build_implementation_writer_api() -> tuple[  # noqa: C901
    type[ImplementationWriterHandoff],
    Callable[[Path, int, Path], AbstractContextManager[ImplementationWriterHandoff]],
]:
    """Build the handoff API around a sentinel inaccessible to callers."""
    sentinel = object()

    class _DirectTransitionEvidence:
        """Private immutable facts for one detached-to-attached transition."""

        __slots__ = (
            "base_sha",
            "branch",
            "path",
            "predecessor_generation",
            "predecessor_revision",
        )

        def __init__(
            self,
            *,
            path: Path,
            predecessor_generation: int,
            predecessor_revision: str,
            branch: str,
            base_sha: str,
        ) -> None:
            self.path = path.resolve()
            self.predecessor_generation = predecessor_generation
            self.predecessor_revision = predecessor_revision
            self.branch = branch
            self.base_sha = base_sha

    class _ImplementationWriterHandoff:
        """Opaque capability issued only by the handoff context manager."""

        _active: bool
        _construction_token: object
        _item_number: int
        _lock_path: Path
        _repo_root: Path
        _direct_transition: _DirectTransitionEvidence | None
        _consumed_direct_transition: _DirectTransitionEvidence | None

        __slots__ = (
            "_active",
            "_construction_token",
            "_consumed_direct_transition",
            "_direct_transition",
            "_item_number",
            "_lock_path",
            "_repo_root",
        )

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            """Reject direct construction outside the issuer context manager."""
            raise TypeError("implementation writer handoff requires the issuer context manager")

        def _validate(self, repo_root: Path, item_number: int, lock_path: Path) -> None:
            if (
                not self._active
                or self._construction_token is not sentinel
                or self._repo_root != repo_root.resolve()
                or self._item_number != item_number
                or self._lock_path != lock_path.resolve()
            ):
                raise RuntimeError("implementation writer handoff is missing or inactive")

        def _arm_direct_transition(
            self,
            *,
            path: Path,
            predecessor_generation: int,
            predecessor_revision: str,
            branch: str,
            base_sha: str,
        ) -> None:
            if not self._active or self._direct_transition is not None:
                raise RuntimeError("implementation writer direct transition is unavailable")
            object.__setattr__(
                self,
                "_direct_transition",
                _DirectTransitionEvidence(
                    path=path,
                    predecessor_generation=predecessor_generation,
                    predecessor_revision=predecessor_revision,
                    branch=branch,
                    base_sha=base_sha,
                ),
            )

        def _validate_direct_transition(
            self,
            *,
            path: Path,
            predecessor_revision: str,
            branch: str,
            base_sha: str,
        ) -> None:
            evidence = self._direct_transition
            if (
                not self._active
                or evidence is None
                or evidence.path != path.resolve()
                or evidence.predecessor_revision != predecessor_revision
                or evidence.branch != branch
                or evidence.base_sha != base_sha
            ):
                raise RuntimeError("implementation writer direct transition is invalid")

        def _consume_direct_transition(
            self,
            *,
            path: Path,
            predecessor_revision: str,
            branch: str,
            base_sha: str,
        ) -> object:
            self._validate_direct_transition(
                path=path,
                predecessor_revision=predecessor_revision,
                branch=branch,
                base_sha=base_sha,
            )
            evidence = self._direct_transition
            if evidence is None:  # pragma: no cover - guarded above
                raise RuntimeError("implementation writer direct transition is invalid")
            object.__setattr__(self, "_direct_transition", None)
            object.__setattr__(self, "_consumed_direct_transition", evidence)
            return evidence

        def _validate_consumed_direct_transition(
            self,
            evidence: object,
            *,
            path: Path,
            predecessor_generation: int,
            predecessor_revision: str,
            branch: str,
        ) -> None:
            expected = self._consumed_direct_transition
            if (
                not self._active
                or evidence is not expected
                or expected is None
                or expected.path != path.resolve()
                or expected.predecessor_generation != predecessor_generation
                or expected.predecessor_revision != predecessor_revision
                or expected.branch != branch
            ):
                raise RuntimeError("implementation writer direct transition evidence is invalid")

    @contextmanager
    def implementation_writer_handoff(
        repo_root: Path, item_number: int, lock_path: Path
    ) -> Iterator[_ImplementationWriterHandoff]:
        """Acquire *lock_path* and issue one active implementation-writer handoff."""
        normalized_root = repo_root.resolve()
        normalized_lock_path = lock_path.resolve()
        handoff = object.__new__(_ImplementationWriterHandoff)
        object.__setattr__(handoff, "_active", False)
        object.__setattr__(handoff, "_construction_token", sentinel)
        object.__setattr__(handoff, "_item_number", item_number)
        object.__setattr__(handoff, "_lock_path", normalized_lock_path)
        object.__setattr__(handoff, "_repo_root", normalized_root)
        object.__setattr__(handoff, "_direct_transition", None)
        object.__setattr__(handoff, "_consumed_direct_transition", None)
        with file_lock(normalized_lock_path, require_exclusive=True):
            object.__setattr__(handoff, "_active", True)
            try:
                yield handoff
            finally:
                object.__setattr__(handoff, "_direct_transition", None)
                object.__setattr__(handoff, "_consumed_direct_transition", None)
                object.__setattr__(handoff, "_active", False)

    return (
        cast("type[ImplementationWriterHandoff]", _ImplementationWriterHandoff),
        cast(
            "Callable[[Path, int, Path], AbstractContextManager[ImplementationWriterHandoff]]",
            implementation_writer_handoff,
        ),
    )


_implementation_writer_handoff_class, implementation_writer_handoff = (
    _build_implementation_writer_api()
)
globals()["ImplementationWriterHandoff"] = _implementation_writer_handoff_class
del _implementation_writer_handoff_class
del _build_implementation_writer_api

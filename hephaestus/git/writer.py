"""Private capabilities for implementation-writer ownership."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Protocol, cast

from hephaestus.utils.file_lock import LockUnavailableError, file_lock

if TYPE_CHECKING:

    class _HandoffIssuer(Protocol):
        """Describe the private issuer's bounded lock contract."""

        def __call__(
            self,
            repo_root: Path,
            item_number: int,
            lock_path: Path,
            *,
            remaining_timeout: Callable[[], float] | None = None,
            shutdown: Event | None = None,
        ) -> AbstractContextManager[ImplementationWriterHandoff]: ...

    class ImplementationWriterHandoff:
        """Static type for the opaque implementation-writer capability."""

        def _validate(self, repo_root: Path, item_number: int, lock_path: Path) -> None:
            raise NotImplementedError

        def _arm_direct_transition(
            self,
            *,
            path: Path,
            predecessor_generation: int,
            predecessor_revision: str,
            predecessor_branch: str | None,
            branch: str,
            base_sha: str,
        ) -> None:
            raise NotImplementedError

        def _arm_writer_transition(
            self,
            *,
            path: Path,
            predecessor_generation: int,
            predecessor_revision: str,
            predecessor_detached: bool,
            predecessor_branch: str | None,
            successor_branch: str,
            successor_revision: str,
            transition: str,
            journal_digest: str,
            target_ref_revision: str | None,
            journal_validator: Callable[[str], None],
            phase_writer: Callable[[str, str], None],
            commit_writer: Callable[[str], None],
        ) -> None:
            raise NotImplementedError

        def _validate_direct_transition(
            self,
            *,
            path: Path,
            predecessor_revision: str,
            predecessor_branch: str | None,
            branch: str,
            base_sha: str,
            target_ref_revision: str | None = None,
        ) -> None:
            raise NotImplementedError

        def _validate_writer_transition(
            self,
            *,
            path: Path,
            predecessor_revision: str,
            predecessor_detached: bool,
            predecessor_branch: str | None,
            successor_branch: str,
            successor_revision: str,
            transition: str,
            target_ref_revision: str | None,
        ) -> None:
            raise NotImplementedError

        def _consume_direct_transition(
            self,
            *,
            path: Path,
            predecessor_revision: str,
            predecessor_branch: str | None,
            branch: str,
            base_sha: str,
            target_ref_revision: str | None = None,
        ) -> object:
            raise NotImplementedError

        def _consume_writer_transition(
            self,
            *,
            path: Path,
            predecessor_revision: str,
            predecessor_detached: bool,
            predecessor_branch: str | None,
            successor_branch: str,
            successor_revision: str,
            transition: str,
            target_ref_revision: str | None,
        ) -> object:
            raise NotImplementedError

        def _mark_transition_phase(self, phase: str) -> None:
            raise NotImplementedError

        def _complete_writer_transition(self) -> None:
            raise NotImplementedError

        def _validate_consumed_direct_transition(
            self,
            evidence: object,
            *,
            path: Path,
            predecessor_generation: int,
            predecessor_revision: str,
            predecessor_branch: str | None,
            branch: str,
        ) -> None:
            raise NotImplementedError

        def _validate_consumed_writer_transition(
            self,
            evidence: object,
            *,
            path: Path,
            predecessor_generation: int,
            predecessor_revision: str,
            predecessor_detached: bool,
            predecessor_branch: str | None,
            branch: str,
            successor_revision: str | None,
            transition: str,
            journal_digest: str | None,
        ) -> None:
            raise NotImplementedError


def _build_implementation_writer_api() -> tuple[  # noqa: C901
    type[ImplementationWriterHandoff],
    _HandoffIssuer,
]:
    """Build the handoff API around a sentinel inaccessible to callers."""
    sentinel = object()

    class _WriterTransitionEvidence:
        """Private immutable facts for one writer checkout transition."""

        __slots__ = (
            "base_sha",
            "branch",
            "journal_digest",
            "path",
            "predecessor_branch",
            "predecessor_detached",
            "predecessor_generation",
            "predecessor_revision",
            "successor_revision",
            "target_ref_revision",
            "transition",
        )

        def __init__(
            self,
            *,
            path: Path,
            predecessor_generation: int,
            predecessor_revision: str,
            predecessor_detached: bool,
            predecessor_branch: str | None,
            branch: str,
            base_sha: str,
            transition: str = "direct",
            journal_digest: str = "",
            target_ref_revision: str | None = None,
        ) -> None:
            self.path = path.resolve()
            self.predecessor_generation = predecessor_generation
            self.predecessor_revision = predecessor_revision
            self.predecessor_detached = predecessor_detached
            self.predecessor_branch = predecessor_branch
            self.branch = branch
            self.base_sha = base_sha
            self.successor_revision = base_sha
            self.transition = transition
            self.journal_digest = journal_digest
            self.target_ref_revision = target_ref_revision

    class _ImplementationWriterHandoff:
        """Opaque capability issued only by the handoff context manager."""

        _active: bool
        _construction_token: object
        _item_number: int
        _lock_path: Path
        _repo_root: Path
        _direct_transition: _WriterTransitionEvidence | None
        _consumed_direct_transition: _WriterTransitionEvidence | None
        _journal_validator: Callable[[str], None] | None
        _phase_writer: Callable[[str, str], None] | None
        _commit_writer: Callable[[str], None] | None

        __slots__ = (
            "_active",
            "_commit_writer",
            "_construction_token",
            "_consumed_direct_transition",
            "_direct_transition",
            "_item_number",
            "_journal_validator",
            "_lock_path",
            "_phase_writer",
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
            predecessor_branch: str | None,
            branch: str,
            base_sha: str,
        ) -> None:
            self._arm_writer_transition(
                path=path,
                predecessor_generation=predecessor_generation,
                predecessor_revision=predecessor_revision,
                predecessor_detached=predecessor_branch is None,
                predecessor_branch=(
                    predecessor_branch.removeprefix("refs/heads/")
                    if predecessor_branch is not None
                    else None
                ),
                successor_branch=branch,
                successor_revision=base_sha,
                transition="direct",
                journal_digest="",
                target_ref_revision=None,
                journal_validator=lambda _digest: None,
                phase_writer=lambda _digest, _phase: None,
                commit_writer=lambda _digest: None,
            )

        def _arm_writer_transition(
            self,
            *,
            path: Path,
            predecessor_generation: int,
            predecessor_revision: str,
            predecessor_detached: bool,
            predecessor_branch: str | None,
            successor_branch: str,
            successor_revision: str,
            transition: str,
            journal_digest: str,
            target_ref_revision: str | None,
            journal_validator: Callable[[str], None],
            phase_writer: Callable[[str, str], None],
            commit_writer: Callable[[str], None],
        ) -> None:
            if not self._active or self._direct_transition is not None:
                raise RuntimeError("implementation writer direct transition is unavailable")
            object.__setattr__(
                self,
                "_direct_transition",
                _WriterTransitionEvidence(
                    path=path,
                    predecessor_generation=predecessor_generation,
                    predecessor_revision=predecessor_revision,
                    predecessor_detached=predecessor_detached,
                    predecessor_branch=predecessor_branch,
                    branch=successor_branch,
                    base_sha=successor_revision,
                    transition=transition,
                    journal_digest=journal_digest,
                    target_ref_revision=target_ref_revision,
                ),
            )
            object.__setattr__(self, "_journal_validator", journal_validator)
            object.__setattr__(self, "_phase_writer", phase_writer)
            object.__setattr__(self, "_commit_writer", commit_writer)

        def _validate_direct_transition(
            self,
            *,
            path: Path,
            predecessor_revision: str,
            predecessor_branch: str | None,
            branch: str,
            base_sha: str,
            target_ref_revision: str | None = None,
        ) -> None:
            self._validate_writer_transition(
                path=path,
                predecessor_revision=predecessor_revision,
                predecessor_detached=predecessor_branch is None,
                predecessor_branch=(
                    predecessor_branch.removeprefix("refs/heads/")
                    if predecessor_branch is not None
                    else None
                ),
                successor_branch=branch,
                successor_revision=base_sha,
                transition="direct",
                target_ref_revision=target_ref_revision,
            )

        def _validate_writer_transition(
            self,
            *,
            path: Path,
            predecessor_revision: str,
            predecessor_detached: bool,
            predecessor_branch: str | None,
            successor_branch: str,
            successor_revision: str,
            transition: str,
            target_ref_revision: str | None,
        ) -> None:
            evidence = self._direct_transition
            if (
                not self._active
                or evidence is None
                or evidence.path != path.resolve()
                or evidence.predecessor_revision != predecessor_revision
                or evidence.predecessor_detached != predecessor_detached
                or evidence.predecessor_branch != predecessor_branch
                or evidence.branch != successor_branch
                or evidence.successor_revision != successor_revision
                or evidence.transition != transition
                or evidence.target_ref_revision != target_ref_revision
            ):
                message = (
                    "implementation writer direct transition is invalid"
                    if transition == "direct"
                    else "implementation writer transition is invalid"
                )
                raise RuntimeError(message)
            journal_validator = self._journal_validator
            if journal_validator is None:
                raise RuntimeError("implementation writer transition journal is unavailable")
            journal_validator(evidence.journal_digest)

        def _consume_direct_transition(
            self,
            *,
            path: Path,
            predecessor_revision: str,
            predecessor_branch: str | None,
            branch: str,
            base_sha: str,
            target_ref_revision: str | None = None,
        ) -> object:
            return self._consume_writer_transition(
                path=path,
                predecessor_revision=predecessor_revision,
                predecessor_detached=predecessor_branch is None,
                predecessor_branch=(
                    predecessor_branch.removeprefix("refs/heads/")
                    if predecessor_branch is not None
                    else None
                ),
                successor_branch=branch,
                successor_revision=base_sha,
                transition="direct",
                target_ref_revision=target_ref_revision,
            )

        def _consume_writer_transition(
            self,
            *,
            path: Path,
            predecessor_revision: str,
            predecessor_detached: bool,
            predecessor_branch: str | None,
            successor_branch: str,
            successor_revision: str,
            transition: str,
            target_ref_revision: str | None,
        ) -> object:
            self._validate_writer_transition(
                path=path,
                predecessor_revision=predecessor_revision,
                predecessor_detached=predecessor_detached,
                predecessor_branch=predecessor_branch,
                successor_branch=successor_branch,
                successor_revision=successor_revision,
                transition=transition,
                target_ref_revision=target_ref_revision,
            )
            phase_writer = self._phase_writer
            if phase_writer is None:
                raise RuntimeError("implementation writer transition journal is unavailable")
            evidence = self._direct_transition
            if evidence is None:  # pragma: no cover - guarded above
                raise RuntimeError("implementation writer transition is invalid")
            phase_writer(evidence.journal_digest, "predecessor_removing")
            object.__setattr__(self, "_direct_transition", None)
            object.__setattr__(self, "_consumed_direct_transition", evidence)
            return evidence

        def _mark_transition_phase(self, phase: str) -> None:
            if not self._active or (
                self._direct_transition is None and self._consumed_direct_transition is None
            ):
                raise RuntimeError("implementation writer transition is unavailable")
            phase_writer = self._phase_writer
            if phase_writer is None:
                raise RuntimeError("implementation writer transition journal is unavailable")
            evidence = self._direct_transition or self._consumed_direct_transition
            if evidence is None:  # pragma: no cover - guarded above
                raise RuntimeError("implementation writer transition is unavailable")
            phase_writer(evidence.journal_digest, phase)

        def _complete_writer_transition(self) -> None:
            if not self._active:
                raise RuntimeError("implementation writer transition is unavailable")
            commit_writer = self._commit_writer
            if commit_writer is None:
                raise RuntimeError("implementation writer transition journal is unavailable")
            evidence = self._direct_transition or self._consumed_direct_transition
            if evidence is None:
                raise RuntimeError("implementation writer transition is unavailable")
            commit_writer(evidence.journal_digest)
            object.__setattr__(self, "_direct_transition", None)
            object.__setattr__(self, "_consumed_direct_transition", None)
            object.__setattr__(self, "_journal_validator", None)
            object.__setattr__(self, "_phase_writer", None)
            object.__setattr__(self, "_commit_writer", None)

        def _validate_consumed_direct_transition(
            self,
            evidence: object,
            *,
            path: Path,
            predecessor_generation: int,
            predecessor_revision: str,
            predecessor_branch: str | None,
            branch: str,
        ) -> None:
            self._validate_consumed_writer_transition(
                evidence,
                path=path,
                predecessor_generation=predecessor_generation,
                predecessor_revision=predecessor_revision,
                predecessor_detached=predecessor_branch is None,
                predecessor_branch=predecessor_branch,
                branch=branch,
                successor_revision=None,
                transition="direct",
                journal_digest=None,
            )

        def _validate_consumed_writer_transition(
            self,
            evidence: object,
            *,
            path: Path,
            predecessor_generation: int,
            predecessor_revision: str,
            predecessor_detached: bool,
            predecessor_branch: str | None,
            branch: str,
            successor_revision: str | None,
            transition: str,
            journal_digest: str | None,
        ) -> None:
            expected = self._consumed_direct_transition
            if (
                not self._active
                or evidence is not expected
                or expected is None
                or expected.path != path.resolve()
                or expected.predecessor_generation != predecessor_generation
                or expected.predecessor_revision != predecessor_revision
                or expected.predecessor_detached != predecessor_detached
                or expected.predecessor_branch != predecessor_branch
                or expected.branch != branch
                or expected.transition != transition
                or (journal_digest is not None and expected.journal_digest != journal_digest)
                or (
                    successor_revision is not None
                    and expected.successor_revision != successor_revision
                )
            ):
                message = (
                    "implementation writer direct transition evidence is invalid"
                    if transition == "direct"
                    else "implementation writer transition evidence is invalid"
                )
                raise RuntimeError(message)
            journal_validator = self._journal_validator
            if journal_validator is None:
                raise RuntimeError("implementation writer transition journal is unavailable")
            journal_validator(expected.journal_digest)

    @contextmanager
    def implementation_writer_handoff(
        repo_root: Path,
        item_number: int,
        lock_path: Path,
        *,
        remaining_timeout: Callable[[], float] | None = None,
        shutdown: Event | None = None,
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
        object.__setattr__(handoff, "_journal_validator", None)
        object.__setattr__(handoff, "_phase_writer", None)
        object.__setattr__(handoff, "_commit_writer", None)
        with ExitStack() as stack:
            while True:
                if remaining_timeout is not None:
                    remaining_timeout()
                try:
                    stack.enter_context(
                        file_lock(
                            normalized_lock_path,
                            require_exclusive=True,
                            blocking=remaining_timeout is None,
                        )
                    )
                except LockUnavailableError:
                    if remaining_timeout is None:
                        raise
                    wait_s = min(0.1, remaining_timeout())
                    if shutdown is None:
                        time.sleep(wait_s)
                    else:
                        shutdown.wait(wait_s)
                    continue
                break
            if remaining_timeout is not None:
                remaining_timeout()
            object.__setattr__(handoff, "_active", True)
            try:
                yield handoff
            finally:
                object.__setattr__(handoff, "_direct_transition", None)
                object.__setattr__(handoff, "_consumed_direct_transition", None)
                object.__setattr__(handoff, "_journal_validator", None)
                object.__setattr__(handoff, "_phase_writer", None)
                object.__setattr__(handoff, "_commit_writer", None)
                object.__setattr__(handoff, "_active", False)

    return (
        cast("type[ImplementationWriterHandoff]", _ImplementationWriterHandoff),
        cast(
            "_HandoffIssuer",
            implementation_writer_handoff,
        ),
    )


_implementation_writer_handoff_class, implementation_writer_handoff = (
    _build_implementation_writer_api()
)
globals()["ImplementationWriterHandoff"] = _implementation_writer_handoff_class
del _implementation_writer_handoff_class
del _build_implementation_writer_api

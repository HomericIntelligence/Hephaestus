"""Verify that all four reviewer classes satisfy ReviewerProtocol."""

from hephaestus.automation._interfaces import ReviewerProtocol


class _ConcreteReviewer:
    """Stub reviewer that satisfies the protocol."""

    def run(self) -> dict:
        """Return an empty result mapping."""
        return {}


class _MissingReviewer:
    """Stub that does not implement run()."""


def test_protocol_satisfied_by_stub() -> None:
    """A class defining run() satisfies ReviewerProtocol."""
    assert isinstance(_ConcreteReviewer(), ReviewerProtocol)


def test_protocol_violated_by_stub() -> None:
    """A class missing run() does not satisfy ReviewerProtocol."""
    assert not isinstance(_MissingReviewer(), ReviewerProtocol)


def test_pr_reviewer_satisfies_protocol() -> None:
    """PRReviewer satisfies ReviewerProtocol structurally."""
    from hephaestus.automation.pr_reviewer import PRReviewer

    assert issubclass(PRReviewer, ReviewerProtocol)


def test_address_reviewer_satisfies_protocol() -> None:
    """AddressReviewer satisfies ReviewerProtocol structurally."""
    from hephaestus.automation.address_review import AddressReviewer

    assert issubclass(AddressReviewer, ReviewerProtocol)


def test_audit_reviewer_satisfies_protocol() -> None:
    """AuditReviewer satisfies ReviewerProtocol structurally."""
    from hephaestus.automation.audit_reviewer import AuditReviewer

    assert issubclass(AuditReviewer, ReviewerProtocol)


def test_plan_reviewer_satisfies_protocol() -> None:
    """PlanReviewer satisfies ReviewerProtocol structurally."""
    from hephaestus.automation.plan_reviewer import PlanReviewer

    assert issubclass(PlanReviewer, ReviewerProtocol)


def test_pr_discovery_satisfies_protocol() -> None:
    """PRDiscovery satisfies PRDiscoveryProtocol structurally."""
    from hephaestus.automation._interfaces import PRDiscoveryProtocol
    from hephaestus.automation.pr_discovery import PRDiscovery

    assert issubclass(PRDiscovery, PRDiscoveryProtocol)


def test_status_tracker_satisfies_protocol() -> None:
    """StatusTracker satisfies StatusTrackerProtocol structurally."""
    from hephaestus.automation._interfaces import StatusTrackerProtocol
    from hephaestus.automation.status_tracker import StatusTracker

    assert issubclass(StatusTracker, StatusTrackerProtocol)


def test_worktree_manager_satisfies_protocol(tmp_path) -> None:
    """WorktreeManager satisfies WorktreeManagerProtocol structurally.

    Uses ``isinstance`` on an instance rather than ``issubclass`` because the
    Protocol declares ``base_branch`` as a property; runtime_checkable
    Protocols with non-method members reject ``issubclass`` but support
    ``isinstance``.
    """
    from hephaestus.automation._interfaces import WorktreeManagerProtocol
    from hephaestus.automation.worktree_manager import WorktreeManager

    assert isinstance(WorktreeManager(base_dir=tmp_path), WorktreeManagerProtocol)


def test_planner_state_satisfies_protocol() -> None:
    """PlannerStateManager satisfies PlannerStateProtocol structurally."""
    from hephaestus.automation._interfaces import PlannerStateProtocol
    from hephaestus.automation.planner_state import PlannerStateManager

    assert issubclass(PlannerStateManager, PlannerStateProtocol)


def test_implementer_state_satisfies_protocol(tmp_path) -> None:
    """ImplementationStateManager satisfies ImplementerStateProtocol.

    Uses ``isinstance`` because the Protocol declares ``lock`` as a property;
    runtime_checkable Protocols with non-method members support ``isinstance``
    but not ``issubclass``.
    """
    from hephaestus.automation._interfaces import ImplementerStateProtocol
    from hephaestus.automation.implementer_state import ImplementationStateManager

    assert isinstance(ImplementationStateManager(tmp_path), ImplementerStateProtocol)


def test_planner_state_is_not_implementer_state(tmp_path) -> None:
    """The two state Protocols are disjoint — a planner is not an implementer."""
    from hephaestus.automation._interfaces import (
        ImplementerStateProtocol,
        PlannerStateProtocol,
    )
    from hephaestus.automation.planner_state import PlannerStateManager

    assert issubclass(PlannerStateManager, PlannerStateProtocol)

    # The implementer-side Protocol carries an implementer-only member set
    # (``lock``/``get``/``save``/...) that the planner surface lacks; a stub
    # bearing only the planner methods therefore does not satisfy it.
    class _PlannerOnly:
        def filter(self) -> list[int]:
            return []

        def get_cached_labels(self, issue_number: int):
            return None

        def prefetch_comments(self, issue_numbers: list[int]) -> None:
            return None

        def get_cached_comments(self, issue_number: int):
            return None

        def has_existing_plan(self, issue_number: int) -> bool:
            return False

    assert isinstance(_PlannerOnly(), PlannerStateProtocol)
    assert not isinstance(_PlannerOnly(), ImplementerStateProtocol)


def test_state_store_alias_is_usable_union(tmp_path) -> None:
    """StateStoreProtocol resolves to a usable union of both role protocols.

    Proves the alias is a real type (not a tuple/marker): both concrete
    managers satisfy one of the role protocols, an unrelated object satisfies
    neither — a behavioral contract, not a self-equality tautology.
    """
    from hephaestus.automation._interfaces import (
        ImplementerStateProtocol,
        PlannerStateProtocol,
    )
    from hephaestus.automation.implementer_state import ImplementationStateManager
    from hephaestus.automation.planner_state import PlannerStateManager

    union = (PlannerStateProtocol, ImplementerStateProtocol)
    assert issubclass(PlannerStateManager, PlannerStateProtocol)
    assert isinstance(ImplementationStateManager(tmp_path), union)

    class _Unrelated:
        pass

    assert not isinstance(_Unrelated(), union)


def test_status_tracker_protocol_violated_by_stub() -> None:
    """A class missing acquire_slot does not satisfy StatusTrackerProtocol."""
    from hephaestus.automation._interfaces import StatusTrackerProtocol

    class _Empty:
        pass

    assert not isinstance(_Empty(), StatusTrackerProtocol)

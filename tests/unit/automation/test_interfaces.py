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

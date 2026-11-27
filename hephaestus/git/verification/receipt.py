"""Bounded receipts for one host verification attempt.

A receipt records which canonical spec ran and what bounded outcome it had.
The diagnostic module uses the receipt fields without trusting the source.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["HostVerificationReceipt", "parse_host_verification_receipt"]

_ERROR_MAX = 500
_RECEIPT_KEYS = frozenset({"spec_descr", "ok", "argv", "error"})


@dataclass(frozen=True, slots=True)
class HostVerificationReceipt:
    """The bounded outcome of one fixed host check."""

    spec_descr: str
    ok: bool
    argv: tuple[str, ...]
    error: str | None = None

    def __post_init__(self) -> None:
        """Reject a malformed receipt at construction time."""
        if not isinstance(self.spec_descr, str) or not self.spec_descr:
            raise ValueError("receipt spec description must be nonempty")
        if type(self.ok) is not bool:
            raise ValueError("receipt outcome must be a bool")
        if (
            type(self.argv) is not tuple
            or not self.argv
            or any(type(part) is not str or not part for part in self.argv)
        ):
            raise ValueError("receipt argv must be a nonempty tuple of strings")
        if self.error is not None and (
            not isinstance(self.error, str) or len(self.error) > _ERROR_MAX
        ):
            raise ValueError("receipt error must be a bounded string")

    def to_diagnostic(self) -> dict[str, object]:
        """Return the bounded diagnostic fields this receipt contributes."""
        fields: dict[str, object] = {"receipt_id": self.spec_descr}
        if self.error:
            fields["error"] = self.error
        return fields


def parse_host_verification_receipt(value: object) -> HostVerificationReceipt | None:
    """Parse one host-verification receipt, or return None for invalid data."""
    if not isinstance(value, dict):
        return None
    if not _RECEIPT_KEYS.issuperset(value) or not {"spec_descr", "ok", "argv"}.issubset(value):
        return None
    raw_argv = value["argv"]
    if not isinstance(raw_argv, (list, tuple)):
        return None
    argv = tuple(str(part) for part in raw_argv)
    raw_error = value.get("error")
    try:
        return HostVerificationReceipt(
            spec_descr=str(value["spec_descr"]),
            ok=value["ok"] is True,
            argv=argv,
            error=None if raw_error is None else str(raw_error)[:_ERROR_MAX],
        )
    except ValueError:
        return None

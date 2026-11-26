"""Canonical host-verification specifications.

Hephaestus defines each fixed host-owned check once in this module. Pipeline
stages import the specifications instead of constructing pytest argv locally.
No specification derives from issue content or reviewer text.

This module belongs to the library layer. It imports no automation module.
"""

from __future__ import annotations

from dataclasses import dataclass

from .paths import safe_relative_path

__all__ = [
    "EMPTY_HOST_VERIFICATIONS_JSON",
    "EMPTY_HOST_VERIFICATION_BOOTSTRAP_JSON",
    "FIRST_PUBLICATION_CHECK",
    "FIRST_PUBLICATION_CHECK_ARGV",
    "HEPHAESTUS_REQUIRED_CHECK",
    "HEPHAESTUS_REQUIRED_CHECK_ARGV",
    "HOST_VERIFICATION_SPECS",
    "HOST_VERIFICATION_WORKSPACE_KEY",
    "PRE_PR_TEST",
    "PRE_PR_TEST_ARGV",
    "REBASE_STRUCTURAL_TEST",
    "REBASE_STRUCTURAL_TEST_ARGV",
    "REBASE_STRUCTURAL_TEST_PATH",
    "HostVerificationSpec",
    "focused_test_argv",
    "spec_for",
]

HOST_VERIFICATION_WORKSPACE_KEY = "host_verification_workspace"
EMPTY_HOST_VERIFICATIONS_JSON = "[]"
EMPTY_HOST_VERIFICATION_BOOTSTRAP_JSON = ""


@dataclass(frozen=True, slots=True)
class HostVerificationSpec:
    """One fixed host-owned command and its stable identifier."""

    descr: str
    argv: tuple[str, ...]
    changed_path: str | None = None

    def __post_init__(self) -> None:
        """Reject an empty identifier or an invalid command vector."""
        if not isinstance(self.descr, str) or not self.descr:
            raise ValueError("host verification description must be a nonempty string")
        if (
            type(self.argv) is not tuple
            or not self.argv
            or any(type(part) is not str or not part or "\x00" in part for part in self.argv)
        ):
            raise ValueError("host verification argv must be a nonempty tuple of strings")
        if self.changed_path is not None and safe_relative_path(self.changed_path) is None:
            raise ValueError("host verification changed path must be repository-relative")


PRE_PR_TEST = HostVerificationSpec(
    "implementation_pre_pr_test",
    ("uv", "run", "pytest", "tests", "-q", "--tb=short"),
)
HEPHAESTUS_REQUIRED_CHECK = HostVerificationSpec(
    "hephaestus_required_check",
    ("bash", "scripts/run_ci_local.sh", "all", "--rebuild"),
)
FIRST_PUBLICATION_CHECK = HostVerificationSpec(
    "first_publication_check",
    ("bash", "scripts/run_ci_local.sh", "all", "--check-only"),
)
REBASE_STRUCTURAL_TEST_PATH = "tests/unit/automation/pipeline/test_rebase_adr_policy.py"
REBASE_STRUCTURAL_TEST = HostVerificationSpec(
    "rebase_structural_test",
    (
        "uv",
        "run",
        "pytest",
        "-o",
        "addopts=",
        REBASE_STRUCTURAL_TEST_PATH,
        "-q",
        "--tb=short",
    ),
)

#: The single spec table. Every stage-owned argv is derived from these records.
HOST_VERIFICATION_SPECS: tuple[HostVerificationSpec, ...] = (
    PRE_PR_TEST,
    HEPHAESTUS_REQUIRED_CHECK,
    FIRST_PUBLICATION_CHECK,
    REBASE_STRUCTURAL_TEST,
)

PRE_PR_TEST_ARGV = PRE_PR_TEST.argv
HEPHAESTUS_REQUIRED_CHECK_ARGV = HEPHAESTUS_REQUIRED_CHECK.argv
FIRST_PUBLICATION_CHECK_ARGV = FIRST_PUBLICATION_CHECK.argv
REBASE_STRUCTURAL_TEST_ARGV = REBASE_STRUCTURAL_TEST.argv


def spec_for(descr: str) -> HostVerificationSpec | None:
    """Return the canonical spec with the exact identifier, or None."""
    for spec in HOST_VERIFICATION_SPECS:
        if spec.descr == descr:
            return spec
    return None


def focused_test_argv(path: str) -> tuple[str, ...]:
    """Return a focused pytest command for one trusted repository-relative path."""
    safe = safe_relative_path(path)
    if safe is None or not safe.endswith(".py"):
        raise ValueError("focused test path must be a repository-relative Python test file")
    return ("uv", "run", "pytest", "-o", "addopts=", safe, "-q", "--tb=short")

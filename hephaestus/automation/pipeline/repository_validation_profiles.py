"""Resolve installed repository-validation profiles by entry point.

Hephaestus owns the repository-validation mechanism. The mechanism ships no
profile; this distribution installs exactly one opt-in Comet sweep profile
(``hephaestus.automation.comet_profile_post2267``). An installed distribution
supplies one factory per repository in the
``hephaestus.repository_validation_profiles`` entry-point group. The factory
returns a :class:`RepositoryValidationProfile`. That object binds the reviewed
control inventory, the fixed command selection, and the provider CI
expectations for one repository.

This module does not read source, contact a provider, or execute a command.
The pipeline adapters in
:mod:`hephaestus.automation.pipeline_github_review_validation` own admission,
identity, deadlines, and transport limits.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from importlib.metadata import entry_points
from typing import Literal

from .repository_validation import (
    RepositoryValidationCheck,
    RepositoryValidationPlan,
    valid_repository_name,
)

PROFILE_ENTRY_POINT_GROUP = "hephaestus.repository_validation_profiles"
PROFILES_MAX = 32
CONTROLS_MAX = 256
CONTROL_BYTES_MAX = 16 * 1024 * 1024
HEADER_KEYS_MAX = 16
HEADER_VALUE_BYTES_MAX = 256
_NAME = r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_WORKFLOW_PATH = re.compile(r"\.github/workflows/[a-z0-9._-]+\.ya?ml")
_HEADER_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}")

type ValidationCheckStatus = Literal["success", "failed"]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def local_repository_name(repository: str) -> str:
    """Return the workspace-local name of one validated repository."""
    _require(valid_repository_name(repository), "The profile repository name is invalid.")
    return repository.split("/", 1)[1]


FAILED_CI_CONCLUSIONS = frozenset(
    {
        "failure",
        "cancelled",
        "timed_out",
        "action_required",
        "stale",
        "startup_failure",
    }
)


@dataclass(frozen=True, slots=True)
class ValidationCIStep:
    """Describe one provider job step without claiming source coverage."""

    number: int
    name: str
    status: str
    conclusion: str | None


@dataclass(frozen=True, slots=True)
class ValidationCIJob:
    """Describe one provider job without claiming source coverage."""

    identifier: int
    name: str
    status: str
    conclusion: str | None
    steps: tuple[ValidationCIStep, ...]


def validation_job_status(
    jobs: tuple[ValidationCIJob, ...], name: str, expected: tuple[tuple[str, str], ...]
) -> ValidationCheckStatus | None:
    """Match one job name and its ordered expected step conclusions.

    A successful aggregate job or a no-op job cannot replace the required
    command steps. The caller supplies the expected ordered step list.
    """
    matching = [job for job in jobs if job.name == name]
    if len(matching) > 1:
        raise ValueError("ci_job_ambiguous")
    if not matching:
        return None
    job = matching[0]
    names = {step for step, _ in expected}
    if job.conclusion in FAILED_CI_CONCLUSIONS or any(
        step.name in names and step.conclusion in FAILED_CI_CONCLUSIONS for step in job.steps
    ):
        return "failed"
    if (
        job.status == "completed"
        and job.conclusion == "success"
        and tuple((step.name, step.conclusion) for step in job.steps[: len(expected)]) == expected
        and all(step.status == "completed" for step in job.steps[: len(expected)])
        and all(sum(step.name == name for step in job.steps) == 1 for name in names)
    ):
        return "success"
    return None


@dataclass(frozen=True, slots=True)
class RepositoryValidationProfile:
    """Bind one repository to its reviewed controls, commands, and CI proof.

    The callables below are the complete profile contract. The pipeline owns
    admission, identity, deadlines, and transport limits. The profile owns the
    repository facts that only its own project can review.
    """

    repository: str
    profile_id: str
    profile_digest: str
    controls: tuple[tuple[str, int, int, str], ...]
    select: Callable[[tuple[tuple[str, str], ...]], tuple[RepositoryValidationCheck, ...]]
    admit_controls: Callable[[Mapping[str, tuple[int, bytes]], tuple[str, ...]], str]
    ci_covered_check_ids: Callable[[tuple[RepositoryValidationCheck, ...]], tuple[str, ...]]
    local_check_ids: Callable[[tuple[RepositoryValidationCheck, ...]], tuple[str, ...]]
    ci_workflow_path: str
    ci_referenced_workflows: frozenset[str]
    ci_receipt_status: Callable[
        [RepositoryValidationPlan, str, tuple[ValidationCIJob, ...]], ValidationCheckStatus | None
    ]
    runtime_slug: str
    runtime_header: tuple[tuple[str, str], ...]
    runtime_executables: frozenset[str]

    def __post_init__(self) -> None:
        """Reject a profile that cannot carry a complete reviewed identity."""
        _require(type(self) is RepositoryValidationProfile, "The profile type is invalid.")
        _require(valid_repository_name(self.repository), "The profile repository name is invalid.")
        _require(
            type(self.profile_id) is str and re.fullmatch(_NAME, self.profile_id) is not None,
            "The profile identifier is invalid.",
        )
        _require(
            type(self.profile_digest) is str and _SHA256.fullmatch(self.profile_digest) is not None,
            "The profile digest is invalid.",
        )
        _require(
            type(self.controls) is tuple and 0 < len(self.controls) <= CONTROLS_MAX,
            "The profile control inventory is invalid.",
        )
        paths: set[str] = set()
        for control in self.controls:
            _require(
                type(control) is tuple and len(control) == 4,
                "The profile control record is invalid.",
            )
            path, mode, size, digest = control
            _require(
                type(path) is str and bool(path) and path not in paths,
                "The profile control path is invalid.",
            )
            paths.add(path)
            _require(
                type(mode) is int
                and mode in {0o100644, 0o100755}
                and type(size) is int
                and 0 <= size <= CONTROL_BYTES_MAX
                and type(digest) is str
                and _SHA256.fullmatch(digest) is not None,
                "The profile control identity is invalid.",
            )
        for member in fields(self):
            if member.name in {
                "repository",
                "profile_id",
                "profile_digest",
                "controls",
                "ci_workflow_path",
                "ci_referenced_workflows",
                "runtime_slug",
                "runtime_header",
                "runtime_executables",
            }:
                # The members below hold data, not commands. Each one has its
                # own complete admission check after this loop.
                continue
            _require(callable(getattr(self, member.name)), "The profile contract is incomplete.")
        _require(
            type(self.ci_workflow_path) is str
            and _WORKFLOW_PATH.fullmatch(self.ci_workflow_path) is not None,
            "The profile workflow path is invalid.",
        )
        _require(
            type(self.ci_referenced_workflows) is frozenset
            and bool(self.ci_referenced_workflows)
            and all(
                type(path) is str and _WORKFLOW_PATH.fullmatch(path) is not None
                for path in self.ci_referenced_workflows
            ),
            "The profile referenced workflows are invalid.",
        )
        _require(
            type(self.runtime_slug) is str
            and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", self.runtime_slug)
            is not None,
            "The profile runtime slug is invalid.",
        )
        _require(
            type(self.runtime_header) is tuple
            and 0 < len(self.runtime_header) <= HEADER_KEYS_MAX
            and tuple(sorted(self.runtime_header)) == self.runtime_header
            and len({key for key, _ in self.runtime_header}) == len(self.runtime_header)
            and all(
                type(key) is str
                and _HEADER_KEY.fullmatch(key) is not None
                and key != "schema"
                and type(value) is str
                and 0 < len(value.encode("utf-8")) <= HEADER_VALUE_BYTES_MAX
                for key, value in self.runtime_header
            ),
            "The profile runtime header is invalid.",
        )
        _require(
            type(self.runtime_executables) is frozenset
            and bool(self.runtime_executables)
            and all(
                type(path) is str
                and re.fullmatch(r"bin/[a-z0-9][a-z0-9.+-]{0,62}", path) is not None
                for path in self.runtime_executables
            ),
            "The profile runtime programs are invalid.",
        )

    @property
    def local_name(self) -> str:
        """Return the workspace-local name of the profile repository."""
        return local_repository_name(self.repository)

    @property
    def control_paths(self) -> frozenset[str]:
        """Return every control path that the profile binds."""
        return frozenset(control[0] for control in self.controls)


def _load_profiles() -> dict[str, RepositoryValidationProfile]:
    """Load every installed profile and reject duplicate repository claims."""
    profiles: dict[str, RepositoryValidationProfile] = {}
    for entry in entry_points(group=PROFILE_ENTRY_POINT_GROUP):
        factory = entry.load()
        _require(callable(factory), "A profile factory is not callable.")
        profile = factory()
        _require(
            type(profile) is RepositoryValidationProfile,
            "A profile factory did not return a repository-validation profile.",
        )
        key = profile.repository.casefold()
        _require(key not in profiles, "Two profiles claim one repository.")
        profiles[key] = profile
    _require(len(profiles) <= PROFILES_MAX, "The installed profile count exceeds its limit.")
    return profiles


_PROFILES: dict[str, RepositoryValidationProfile] | None = None


def repository_validation_profiles() -> Mapping[str, RepositoryValidationProfile]:
    """Return every installed profile keyed by its casefolded repository."""
    global _PROFILES
    if _PROFILES is None:
        _PROFILES = _load_profiles()
    return _PROFILES


def repository_validation_profile(repository: object) -> RepositoryValidationProfile | None:
    """Return the installed profile that owns one repository or its local name."""
    if type(repository) is not str or not repository:
        return None
    profiles = repository_validation_profiles()
    found = profiles.get(repository.casefold())
    if found is not None:
        return found
    local = repository.rsplit("/", 1)[-1].casefold()
    matches = [profile for profile in profiles.values() if profile.local_name.casefold() == local]
    return matches[0] if len(matches) == 1 else None


def require_repository_validation_profile(repository: object) -> RepositoryValidationProfile:
    """Return the profile that owns one repository or reject the request."""
    profile = repository_validation_profile(repository)
    if profile is None:
        raise ValueError("The validation repository is unsupported.")
    return profile


def validation_checks_for_profile(
    profile: RepositoryValidationProfile, changes: tuple[tuple[str, str], ...]
) -> tuple[RepositoryValidationCheck, ...]:
    """Select every applicable repository check without executing the selector."""
    return profile.select(changes)


def ci_validation_check_ids(
    profile: RepositoryValidationProfile, checks: tuple[RepositoryValidationCheck, ...]
) -> tuple[str, ...]:
    """Return applicable checks that the ordinary pull-request workflow covers."""
    return profile.ci_covered_check_ids(checks)


def local_validation_check_ids(
    profile: RepositoryValidationProfile, checks: tuple[RepositoryValidationCheck, ...]
) -> tuple[str, ...]:
    """Return applicable checks that the sealed local runtime may execute."""
    return profile.local_check_ids(checks)


def plan_validation_profile(plan: RepositoryValidationPlan) -> RepositoryValidationProfile:
    """Return the profile that one frozen validation plan must keep."""
    profile = require_repository_validation_profile(plan.repository)
    _require(profile.profile_id == plan.profile_id, "The plan profile identifier does not match.")
    _require(
        profile.profile_digest == plan.profile_digest,
        "The plan profile digest does not match.",
    )
    return profile


def plan_validation_checks(plan: RepositoryValidationPlan) -> tuple[RepositoryValidationCheck, ...]:
    """Return the check selection that one frozen validation plan must keep."""
    return validation_checks_for_profile(plan_validation_profile(plan), plan.changes)

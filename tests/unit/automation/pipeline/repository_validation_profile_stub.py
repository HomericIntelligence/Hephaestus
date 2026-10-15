"""Build a minimal repository-validation profile for tests.

Hephaestus owns the repository-validation mechanism and ships no profile. An
installed distribution supplies one profile per repository through the
``hephaestus.repository_validation_profiles`` entry-point group. A test that
needs a concrete profile therefore builds one itself.

This module supplies one small, generic profile. Its repository name, control
inventory, command selection, and provider expectations are test data. They are
not a reviewed profile of any real repository.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hephaestus.automation.pipeline.repository_validation import (
    RepositoryValidationCheck,
    RepositoryValidationPlan,
)
from hephaestus.automation.pipeline.repository_validation_profiles import (
    RepositoryValidationProfile,
    ValidationCheckStatus,
    ValidationCIJob,
    validation_job_status,
)

REPOSITORY = "acme/ledger"
PROFILE_ID = "ledger-v1"
PROFILE_DIGEST = "1" * 64
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
RUNTIME_SLUG = "ledger"
RUNTIME_HEADER_KEYS = ("profile", "python_version", "repository", "uv_version")
NIGHTLY_CHECK_ID = "ledger.nightly.integration"
CONTROL_SOURCES = (
    (".github/workflows/ci.yml", b"name: ci\n"),
    ("pyproject.toml", b'[project]\nname = "ledger"\n'),
    ("uv.lock", b"version = 1\n"),
)
CONTROL_PATHS = tuple(path for path, _ in CONTROL_SOURCES)
CHECK_IDS = ("ledger.python.ruff-check", "ledger.python.ty-check")
CI_CHECK_IDS = frozenset(CHECK_IDS)
CHECKOUT_STEP = "Run actions/checkout@8ade135a41bc03ea155e62e844d188df9d189bef"
SETUP_UV_STEP = "Run astral-sh/setup-uv@e92bafb6253dcd438e0484186d7669ea7a8ca8e"


def sha256(content: bytes) -> str:
    """Return the SHA-256 digest of one control file."""
    return hashlib.sha256(content).hexdigest()


def source_digests() -> tuple[tuple[str, int, str], ...]:
    """Return the complete source-digest triple of one admitted check."""
    return tuple((path, len(content), sha256(content)) for path, content in CONTROL_SOURCES)


def _controls() -> tuple[tuple[str, int, int, str], ...]:
    """Return the profile control inventory for the fixed source bytes."""
    return tuple(
        (path, 0o100644, len(content), sha256(content)) for path, content in CONTROL_SOURCES
    )


def write_controls(root: Path) -> None:
    """Write the control bytes into one source workspace."""
    for path, content in CONTROL_SOURCES:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _argv(check_id: str) -> tuple[str, ...]:
    """Return the fixed command vector of one test check."""
    if check_id == "ledger.python.ruff-check":
        return ("uv", "run", "--locked", "--extra", "dev", "ruff", "check", ".")
    if check_id == NIGHTLY_CHECK_ID:
        return ("uv", "run", "--locked", "--extra", "dev", "pytest", "-q")
    return ("uv", "run", "--locked", "--extra", "dev", "ty", "check")


def ci_step_name(check_id: str) -> str:
    """Return the provider job step that proves one check."""
    return "Run uv run --locked --extra dev " + " ".join(_argv(check_id)[5:])


CI_JOB_NAME = "lint"
CI_JOB_STEPS = (
    "Set up job",
    CHECKOUT_STEP,
    SETUP_UV_STEP,
    ci_step_name("ledger.python.ty-check"),
    ci_step_name("ledger.python.ruff-check"),
)


def ci_expected_steps(check_id: str) -> tuple[tuple[str, str], ...]:
    """Return the ordered job-step prefix that the pipeline must match.

    Each check names an ordered prefix of the one provider job. The prefix ends
    at the step that runs the reviewed command. A profile that wants a later
    step must name every earlier step too.
    """
    steps = CI_JOB_STEPS[: CI_JOB_STEPS.index(ci_step_name(check_id)) + 1]
    return tuple((step, "success") for step in steps)


def ci_job(step_conclusions: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Return the provider job records for the fixed lint job."""
    conclusions = dict(step_conclusions or {})
    return [
        {
            "id": 1,
            "run_id": 123,
            "run_attempt": 2,
            "head_sha": "a" * 40,
            "name": CI_JOB_NAME,
            "status": "completed",
            "conclusion": "success",
            "steps": [
                {
                    "number": number + 1,
                    "name": step,
                    "status": "completed",
                    "conclusion": conclusions.get(step, "success"),
                }
                for number, step in enumerate(CI_JOB_STEPS)
            ],
        }
    ]


def _select(
    changes: tuple[tuple[str, str], ...], *, nightly: bool
) -> tuple[RepositoryValidationCheck, ...]:
    """Select every check for the fixed test control set."""
    digests = source_digests()
    checks = [
        RepositoryValidationCheck(check_id=check_id, argv=_argv(check_id), source_digests=digests)
        for check_id in CHECK_IDS
    ]
    if nightly and any(status == "M" and path.startswith("src/") for status, path in changes):
        checks.append(
            RepositoryValidationCheck(
                check_id=NIGHTLY_CHECK_ID, argv=_argv(NIGHTLY_CHECK_ID), source_digests=digests
            )
        )
    return tuple(checks)


RESERVED_CONTROL_NAMES = frozenset({"pyproject.toml", "uv.lock"})
RESERVED_CONTROL_PREFIXES = (".github/workflows/",)


def _is_control(path: str) -> bool:
    """Return True for a path the profile reserves as one reviewed control."""
    return path in RESERVED_CONTROL_NAMES or path.startswith(RESERVED_CONTROL_PREFIXES)


def _admit_controls(controls: Mapping[str, tuple[int, bytes]], discovered: tuple[str, ...]) -> str:
    """Admit only the complete unchanged control inventory."""
    expected = {path: (0o100644, content) for path, content in CONTROL_SOURCES}
    if set(controls) != set(expected) or any(
        controls[path] != value for path, value in expected.items()
    ):
        raise ValueError("The control inventory is not admitted.")
    if any(path not in discovered for path in expected):
        raise ValueError("The discovered inventory is incomplete.")
    if any(_is_control(path) and path not in expected for path in discovered):
        raise ValueError("The discovered inventory adds an unreviewed control.")
    return PROFILE_ID


def _ci_receipt_status(
    plan: RepositoryValidationPlan, check_id: str, jobs: tuple[ValidationCIJob, ...]
) -> ValidationCheckStatus | None:
    """Accept a complete successful provider observation of one check."""
    return validation_job_status(jobs, CI_JOB_NAME, ci_expected_steps(check_id))


def build_profile(
    repository: str = REPOSITORY, *, nightly: bool = False
) -> RepositoryValidationProfile:
    """Return one minimal generic profile for repository tests.

    Args:
        repository: The repository that this profile owns.
        nightly: Add one local-only check when a reviewed source path changes.
            The ordinary pull-request workflow never covers it, so provider CI
            evidence must not fill it.

    """
    return RepositoryValidationProfile(
        repository=repository,
        profile_id=PROFILE_ID,
        profile_digest=PROFILE_DIGEST,
        controls=_controls(),
        select=lambda changes: _select(changes, nightly=nightly),
        admit_controls=_admit_controls,
        ci_covered_check_ids=lambda checks: tuple(
            check.check_id for check in checks if check.check_id in CI_CHECK_IDS
        ),
        local_check_ids=lambda checks: tuple(
            check.check_id for check in checks if check.check_id in CI_CHECK_IDS
        ),
        ci_workflow_path=CI_WORKFLOW_PATH,
        ci_referenced_workflows=frozenset({CI_WORKFLOW_PATH}),
        ci_receipt_status=_ci_receipt_status,
        runtime_slug=RUNTIME_SLUG,
        runtime_header=(
            ("profile", "ledger"),
            ("python_version", "3.13"),
            ("repository", repository.casefold()),
            ("uv_version", "0.12.7"),
        ),
        runtime_executables=frozenset({"bin/python", "bin/ruff", "bin/ty", "bin/uv"}),
    )


def install_profile(
    monkeypatch: Any, repository: str = REPOSITORY, *, nightly: bool = False
) -> RepositoryValidationProfile:
    """Make one profile the installed profile for the current test."""
    profile = build_profile(repository, nightly=nightly)
    monkeypatch.setattr(
        "hephaestus.automation.pipeline.repository_validation_profiles._PROFILES",
        {profile.repository.casefold(): profile},
    )
    return profile

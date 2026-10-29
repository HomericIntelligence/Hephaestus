r"""Define the pinned Comet post-#2267 repository-validation profile.

This profile binds the GitHub project ``LLM360/comet`` at commit
``bc16c3ae6562799a229c54c82344e2fceb64ab9b`` (short identity ``bc16c3a``,
captured live on 2026-10-29). The ``repository`` field uses the lowercase
spelling ``llm360/comet`` because the repository-name contract admits
lowercase names only. Every consumer compares repository names without case,
and the provider API accepts the lowercase spelling. The byte-faithful copy
of every bound control is in
``tests/fixtures/comet-review-validation/bc16c3a/``. The capture table is in
``bc16c3a-manifest.json`` and the source inventory is in
``bc16c3a-paths.json`` next to it.

Comet pull request #2267 removed the pinned K2 affected-scope test policy
(``.github/ci/k2-affected-scope.json`` and ``scripts/k2_ci_policy.py``). This
profile does not admit that policy again: ``select`` returns one fixed full
check set for every change set, and ``.github/ci/`` stays a reserved control
namespace so a re-created policy file fails admission.

The profile digest is the SHA-256 of the canonical control-inventory
serialization: one ``<path>\t<mode>\t<size>\t<sha256>`` row per control in
manifest order, joined by ``\n``, with one trailing ``\n``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

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

PROFILE_ID = "comet-bc16c3a-v1"
PROFILE_DIGEST = "a573b9331d1d2a2ef391e9f03bf7825acbfc77b1ddf4be95a965f92bc0569c2e"

REPOSITORY = "llm360/comet"
RUNTIME_SLUG = "comet"
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
CI_REFERENCED_WORKFLOWS = frozenset(
    {
        ".github/workflows/build-image.yml",
        ".github/workflows/docs-check.yml",
        ".github/workflows/postgres-tests.yml",
        ".github/workflows/secrets.yml",
    }
)

CONTROLS: tuple[tuple[str, int, int, str], ...] = (
    (
        ".gitattributes",
        33188,
        115,
        "5a131fa6e6c16945dd1eb1dbc15327f37d2d88e2a5a2ccc179fa98877e98ef53",
    ),
    (
        ".github/workflows/bootstrap-correlation.yml",
        33188,
        1513,
        "51f75d512f1173a4ef47995f7b53c03a5141b6cd438ea0f6fb13659fc4083939",
    ),
    (
        ".github/workflows/build-image.yml",
        33188,
        1711,
        "fa6130b18190ab666f82fd3811ac29bf0bc2f7142b40d7582af2a77cb0135e4e",
    ),
    (
        ".github/workflows/ci.yml",
        33188,
        24355,
        "738f682246139354399baedaf6defe804cf5b8990e595138d892544f89fffd91",
    ),
    (
        ".github/workflows/docs-check.yml",
        33188,
        1518,
        "143a2a71ca24a5cf4750d3da476febbf0fc80f2410ec7dc375d9d4a8fb7eac0e",
    ),
    (
        ".github/workflows/docs.yml",
        33188,
        10703,
        "dae11a449af4daf75d0d0e9e4870318cd8ac1e5ff0f6282af5d4ddfe17c400d9",
    ),
    (
        ".github/workflows/hosted-scope-qualification.yml",
        33188,
        29817,
        "7f12756f46a55ddb76eb002500b2b1097efe898123cd252daeb09cb4f42013a5",
    ),
    (
        ".github/workflows/nightly.yml",
        33188,
        3629,
        "bbe593171b70515680a995d6e0f296a8d7415476e9029d3edccc69aeb12203c4",
    ),
    (
        ".github/workflows/postgres-rootless-validation.yml",
        33188,
        3339,
        "a95f596ff498cd76fff003da5110e525febfa808c4cbf2941f166f930e043d73",
    ),
    (
        ".github/workflows/postgres-tests.yml",
        33188,
        52669,
        "4e461b1f2aa2bb3835c5ae9dc5d9fc44e624c5356c137ed746699cf42ae77e97",
    ),
    (
        ".github/workflows/pr-provenance.yml",
        33188,
        1328,
        "4fd399cdd549eda0e8722fabddd843a074092577e438f5e771a2cca489f04515",
    ),
    (
        ".github/workflows/release-conformance.yml",
        33188,
        21528,
        "70fd752d34eead39a1bd214dd1ca5c2af3a9182ba8bdb180aebab5f7ad93ead4",
    ),
    (
        ".github/workflows/release-focused-validation.yml",
        33188,
        24108,
        "126c530d9e6ec0b8a377c2833a9af9f56ad1b1e5a8f209d38287685163a62c28",
    ),
    (
        ".github/workflows/release-live-validation.yml",
        33188,
        21982,
        "eed5ce30ac7b8ee096d697dc935a4a92d726d5595da7903c86776b212d2db906",
    ),
    (
        ".github/workflows/root-ownership-regressions.yml",
        33188,
        8811,
        "67c098a60037cac4fa0ce7f63920e0812b85d73233dc2925d68b1f7370db04da",
    ),
    (
        ".github/workflows/secrets.yml",
        33188,
        626,
        "a7316fa5457d822a07502cebc607295b862e8f608a9329b9d8cb06cda52b14bc",
    ),
    (
        ".github/workflows/test-profile.yml",
        33188,
        196138,
        "9a8bf17fa3854808dd0099c8057c77cd38003ca7cae0cd4abe549f4afc07cd32",
    ),
    (
        "pyproject.toml",
        33188,
        6246,
        "8bf43c013cf5b8dc470a12401611013b5afad33fe5b3f0de3bc1e6aad50c01b3",
    ),
    (
        "tests/promotion_quarantine.py",
        33188,
        1907,
        "0125698ecdd2cfe30a8ed96ab894d1a407b9f59fbf92f5ac427770c6f56869fe",
    ),
    (
        "tests/release_effect_guard.py",
        33188,
        3494,
        "d55dcd37e09092c4822d58b719792a54b26ba5a34e2ca14b694e60277ac0bc1d",
    ),
    (
        "tests/test-durations.json",
        33188,
        86210,
        "3ca65ad1a1e8532be0017da13c73315103b4036d7e15dd1544a7e9d1b2e3a91c",
    ),
    (
        "tests/test-tiers.yaml",
        33188,
        12973,
        "85fb7e4068cabcbd2361b435fecde1c60f74c018ab4c2d74c2d9e21e2511bd3c",
    ),
    (
        "uv.lock",
        33188,
        184600,
        "d834026644d17fa1fd50d1e68018d8027a936bfacddcb4833b6bb85f3ca94e5d",
    ),
)

RUFF_FORMAT_CHECK_ID = "comet.python.ruff-format"
RUFF_CHECK_ID = "comet.python.ruff-check"
TY_CHECK_ID = "comet.python.ty-check"
PR_TESTS_CHECK_ID = "comet.python.pr-tests"
CHECK_IDS = (RUFF_FORMAT_CHECK_ID, RUFF_CHECK_ID, TY_CHECK_ID, PR_TESTS_CHECK_ID)

_PREFIX = ("uv", "run", "--locked", "--extra", "dev")
_CHECK_ARGV: dict[str, tuple[str, ...]] = {
    RUFF_FORMAT_CHECK_ID: (*_PREFIX, "ruff", "format", "--check", ".", "--diff"),
    RUFF_CHECK_ID: (*_PREFIX, "ruff", "check", "."),
    TY_CHECK_ID: (*_PREFIX, "ty", "check"),
    PR_TESTS_CHECK_ID: (*_PREFIX, "python", "scripts/ci/run-test-tier.py", "pr"),
}

CHECKOUT_STEP = "Run actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_UV_STEP = "Run astral-sh/setup-uv@07a047e608db84b3d8c06cd9a7c7dcc9c355c18e"
LINT_JOB = "lint"
TYPECHECK_JOB = "typecheck"
TEST_SHARD_COUNT = 32
TEST_PYTHON_VERSION = "3.12"

_LINT_FORMAT_STEP = "Run uv run --locked --extra dev ruff format --check . --diff"
_LINT_CHECK_STEP = "Run uv run --locked --extra dev ruff check ."
_TYPECHECK_STEP = "Run uv run --locked --extra dev ty check"

_RESERVED_NAMES = frozenset(
    {
        ".gitattributes",
        "pyproject.toml",
        "uv.lock",
        "tests/promotion_quarantine.py",
        "tests/release_effect_guard.py",
        "tests/test-durations.json",
        "tests/test-tiers.yaml",
    }
)
_RESERVED_PREFIXES = (".github/ci/", ".github/workflows/")


def _is_control(path: str) -> bool:
    """Return True when one path belongs to a reserved control namespace."""
    return path in _RESERVED_NAMES or path.startswith(_RESERVED_PREFIXES)


def _source_digests() -> tuple[tuple[str, int, str], ...]:
    """Return the source-digest record shared by every selected check."""
    return tuple((path, size, digest) for path, _mode, size, digest in CONTROLS)


def _select(changes: tuple[tuple[str, str], ...]) -> tuple[RepositoryValidationCheck, ...]:
    """Return the fixed full check set. Comet runs the full profile always."""
    del changes
    digests = _source_digests()
    return tuple(
        RepositoryValidationCheck(
            check_id=check_id, argv=_CHECK_ARGV[check_id], source_digests=digests
        )
        for check_id in CHECK_IDS
    )


def _admit_controls(controls: Mapping[str, tuple[int, bytes]], discovered: tuple[str, ...]) -> str:
    """Admit the complete unchanged control inventory and return the profile identity."""
    expected = {path: (mode, size, digest) for path, mode, size, digest in CONTROLS}
    if not isinstance(controls, Mapping):
        raise ValueError("The control inventory is not a mapping.")
    if set(controls) != set(expected):
        raise ValueError("The control inventory does not match the profile.")
    for path in sorted(controls):
        record = controls[path]
        mode, size, digest = expected[path]
        if (
            type(record) is not tuple
            or len(record) != 2
            or type(record[0]) is not int
            or record[0] != mode
            or type(record[1]) is not bytes
            or len(record[1]) != size
        ):
            raise ValueError("The control record does not match the profile.")
        if hashlib.sha256(record[1]).hexdigest() != digest:
            raise ValueError("The control bytes do not match the profile.")
    if any(path not in discovered for path in expected):
        raise ValueError("The discovered inventory is incomplete.")
    if any(_is_control(path) and path not in expected for path in discovered):
        raise ValueError("The discovered inventory adds an unreviewed control.")
    return PROFILE_ID


def _lint_steps(check_id: str) -> tuple[tuple[str, str], ...]:
    """Return the ordered lint job-step prefix that proves one lint check."""
    steps = [
        ("Set up job", "success"),
        (CHECKOUT_STEP, "success"),
        (SETUP_UV_STEP, "success"),
        (_LINT_FORMAT_STEP, "success"),
    ]
    if check_id == RUFF_CHECK_ID:
        steps.append((_LINT_CHECK_STEP, "success"))
    return tuple(steps)


def _typecheck_steps() -> tuple[tuple[str, str], ...]:
    """Return the ordered typecheck job-step prefix that proves the check."""
    return (
        ("Set up job", "success"),
        (CHECKOUT_STEP, "success"),
        (SETUP_UV_STEP, "success"),
        (_TYPECHECK_STEP, "success"),
    )


def _shard_steps() -> tuple[tuple[str, str], ...]:
    """Return the ordered shard job-step prefix that proves pull-request tests."""
    return (
        ("Set up job", "success"),
        (CHECKOUT_STEP, "success"),
        (SETUP_UV_STEP, "success"),
        ("Require the test scope", "skipped"),
        ("Verify test source", "success"),
        ("Install Bubblewrap", "success"),
        ("Record the no-op product test decision", "skipped"),
        ("Run the ordinary pull-request profile", "success"),
    )


def _pr_tests_status(jobs: tuple[ValidationCIJob, ...]) -> ValidationCheckStatus | None:
    """Combine one status over the complete captured pull-request test shards."""
    statuses = [
        validation_job_status(jobs, f"test (3.12, shard {shard})", _shard_steps())
        for shard in range(TEST_SHARD_COUNT)
    ]
    if "failed" in statuses:
        return "failed"
    return "success" if all(status == "success" for status in statuses) else None


def _ci_receipt_status(
    plan: RepositoryValidationPlan, check_id: str, jobs: tuple[ValidationCIJob, ...]
) -> ValidationCheckStatus | None:
    """Accept one complete successful provider observation of a selected check."""
    del plan
    if check_id in {RUFF_FORMAT_CHECK_ID, RUFF_CHECK_ID}:
        return validation_job_status(jobs, LINT_JOB, _lint_steps(check_id))
    if check_id == TY_CHECK_ID:
        return validation_job_status(jobs, TYPECHECK_JOB, _typecheck_steps())
    if check_id == PR_TESTS_CHECK_ID:
        return _pr_tests_status(jobs)
    return None


def comet_post2267_profile() -> RepositoryValidationProfile:
    """Return the pinned Comet post-#2267 repository-validation profile."""
    return RepositoryValidationProfile(
        repository=REPOSITORY,
        profile_id=PROFILE_ID,
        profile_digest=PROFILE_DIGEST,
        controls=CONTROLS,
        select=_select,
        admit_controls=_admit_controls,
        ci_covered_check_ids=lambda checks: tuple(
            check.check_id for check in checks if check.check_id in CHECK_IDS
        ),
        local_check_ids=lambda checks: tuple(
            check.check_id for check in checks if check.check_id in CHECK_IDS
        ),
        ci_workflow_path=CI_WORKFLOW_PATH,
        ci_referenced_workflows=CI_REFERENCED_WORKFLOWS,
        ci_receipt_status=_ci_receipt_status,
        runtime_slug=RUNTIME_SLUG,
        runtime_header=(
            ("profile", "comet"),
            ("python_version", TEST_PYTHON_VERSION),
            ("repository", REPOSITORY.casefold()),
            ("uv_version", "0.10.8"),
        ),
        runtime_executables=frozenset({"bin/python", "bin/ruff", "bin/ty", "bin/uv"}),
    )

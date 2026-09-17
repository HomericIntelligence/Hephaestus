"""Host-owned verification plans and receipt checks for PR review."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ..repository_validation import (
    RepositoryValidationAttempt,
    RepositoryValidationCoverage,
    RepositoryValidationGap,
    validation_attempt_coverage,
)
from ..work_item import WorkItem
from .pr_review_verification_specs import (
    _FULL_UNIT_COVERAGE_SPEC as _FULL_UNIT_COVERAGE_SPEC,
    _NONHERMETIC_HOST_UNIT_TEST_PATHS as _NONHERMETIC_HOST_UNIT_TEST_PATHS,
    _PATH_HOST_VERIFICATION_SPECS as _PATH_HOST_VERIFICATION_SPECS,
    _PYTHON_HOST_VERIFICATION_SPECS as _PYTHON_HOST_VERIFICATION_SPECS,
    _PYTHON_VALIDATION_CONFIG_PATHS as _PYTHON_VALIDATION_CONFIG_PATHS,
    _HostPlan as _HostPlan,
    _HostVerificationSpec as _HostVerificationSpec,
)

HOST_VERIFICATION_TIMEOUT_S = 300
HOST_VERIFICATION_DIAGNOSTIC_MAX = 4_000
_REVIEW_CHANGED_PATH_MAX = 512
_REVIEW_CHANGED_PATH_BYTES_MAX = 64 * 1024


def _review_changed_paths(value: object) -> tuple[str, ...] | None:
    """Return bounded safe paths, or return None for invalid input."""
    if not isinstance(value, (list, tuple)) or len(value) > _REVIEW_CHANGED_PATH_MAX:
        return None
    paths: list[str] = []
    encoded_bytes = 0
    for path in value:
        if not isinstance(path, str):
            return None
        relative = Path(path)
        if (
            not path
            or "\x00" in path
            or relative.is_absolute()
            or relative.as_posix() != path
            or any(component in {"", ".", ".."} for component in relative.parts)
        ):
            return None
        encoded_bytes += (
            len(path.encode(sys.getfilesystemencoding(), sys.getfilesystemencodeerrors())) + 1
        )
        if encoded_bytes > _REVIEW_CHANGED_PATH_BYTES_MAX:
            return None
        paths.append(path)
    if len(set(paths)) != len(paths):
        return None
    return tuple(paths)


def _review_change_records(
    value: object, paths: tuple[str, ...]
) -> tuple[tuple[str, str], ...] | None:
    """Normalize status records that match the complete legacy path list."""
    if not isinstance(value, (list, tuple)) or len(value) != len(paths):
        return None
    records: list[tuple[str, str]] = []
    for record, path in zip(value, paths, strict=True):
        if (
            not isinstance(record, (list, tuple))
            or len(record) != 2
            or not isinstance(record[0], str)
            or record[0] not in {"A", "M", "D", "T"}
            or record[1] != path
        ):
            return None
        records.append((record[0], path))
    return tuple(records)


def _changed_unit_pytest_argv(target: str) -> tuple[str, ...]:
    """Return a focused unit-test command with host exclusions."""
    ignore_args = tuple(
        f"--ignore={path}"
        for path in sorted(_NONHERMETIC_HOST_UNIT_TEST_PATHS)
        if path.startswith(f"{target}/")
    )
    return (
        "uv",
        "run",
        "pytest",
        "-o",
        "addopts=",
        target,
        *ignore_args,
        "-q",
        "--tb=short",
    )


def _host_verification_specs(
    review_changed_paths: object,
    *,
    existing_changed_paths: object | None = None,
    profile: str | None = "hephaestus",
) -> _HostPlan:
    """Return the fixed host plan for checkout-derived changed paths."""
    normalized_paths = _review_changed_paths(review_changed_paths)
    if profile != "hephaestus" or normalized_paths is None:
        return ()
    changed_paths = frozenset(normalized_paths)
    normalized_existing_paths = _review_changed_paths(
        normalized_paths if existing_changed_paths is None else existing_changed_paths
    )
    if normalized_existing_paths is None or not set(normalized_existing_paths) <= changed_paths:
        return ()
    path_triggered_specs = tuple(
        spec
        for spec in _PATH_HOST_VERIFICATION_SPECS
        if spec.changed_path in changed_paths
        or bool(changed_paths.intersection(spec.additional_changed_paths))
    )
    coverage_specs = (
        (_FULL_UNIT_COVERAGE_SPEC,) if changed_paths & {"coverage.toml", "pyproject.toml"} else ()
    )
    if not any(
        path.endswith(".py") or path in _PYTHON_VALIDATION_CONFIG_PATHS for path in changed_paths
    ):
        return path_triggered_specs
    changed_unit_paths = tuple(
        sorted(
            path
            for path in normalized_existing_paths
            if path.startswith("tests/unit/")
            and path.endswith(".py")
            and path not in _NONHERMETIC_HOST_UNIT_TEST_PATHS
        )
    )
    changed_conftest_paths = tuple(
        (path.rsplit("/", 1)[0], path)
        for path in changed_unit_paths
        if path.rsplit("/", 1)[-1] == "conftest.py"
    )
    changed_conftest_directories = {
        directory: path
        for directory, path in changed_conftest_paths
        if not any(
            directory.startswith(f"{other_directory}/")
            for other_directory, _ in changed_conftest_paths
        )
    }
    changed_unit_targets = (
        *((path, directory) for directory, path in sorted(changed_conftest_directories.items())),
        *(
            (path, path)
            for path in changed_unit_paths
            if path.rsplit("/", 1)[-1] != "conftest.py"
            and not any(
                path.startswith(f"{directory}/") for directory in changed_conftest_directories
            )
        ),
    )
    changed_unit_tests = tuple(
        _HostVerificationSpec(
            changed_path=changed_path,
            argv=_changed_unit_pytest_argv(target),
            descr=f"review_changed_unit_test_{index}",
        )
        for index, (changed_path, target) in enumerate(changed_unit_targets)
    )
    return (
        *_PYTHON_HOST_VERIFICATION_SPECS,
        *changed_unit_tests,
        *path_triggered_specs,
        *coverage_specs,
    )


# fmt: off
__all__ = [
    'HOST_VERIFICATION_DIAGNOSTIC_MAX', 'HOST_VERIFICATION_TIMEOUT_S',
    '_NONHERMETIC_HOST_UNIT_TEST_PATHS', '_PATH_HOST_VERIFICATION_SPECS',
    '_PYTHON_HOST_VERIFICATION_SPECS', '_PYTHON_VALIDATION_CONFIG_PATHS', '_HostPlan',
    '_HostVerificationSpec', '_changed_unit_pytest_argv', '_host_verification_specs',
    '_review_changed_paths']
# fmt: on


def _repository_validation_coverage(item: WorkItem) -> RepositoryValidationCoverage:
    """Bind live attempt coverage to the current review identity."""
    attempt = item.payload.get("repository_validation_attempt")
    generation = item.payload.get("reviewed_pr_proof_generation")
    if (
        type(attempt) is not RepositoryValidationAttempt
        or type(generation) is not int
        or attempt.generation != generation
        or attempt.plan.repository.casefold() != f"llm360/{item.repo.casefold()}"
        or attempt.plan.pr_number != item.pr
        or attempt.plan.issue_number != item.issue
        or attempt.plan.reviewed_head != item.payload.get("pr_head_sha")
        or attempt.plan.reviewed_base != item.payload.get("reviewed_pr_base_sha")
        or item.payload.get("repository_validation_failure")
    ):
        return RepositoryValidationCoverage(
            "gap", gaps=(RepositoryValidationGap("*", "validation_stage_identity_invalid"),)
        )
    return validation_attempt_coverage(
        attempt,
        reviewed_head=str(item.payload.get("reviewed_pr_head_sha") or ""),
        reviewed_base=str(item.payload.get("pr_base_sha") or ""),
    )


def _repository_validation_required(item: WorkItem, org: str | None) -> bool:
    """Select the repository that requires a bound validation attempt."""
    return item.repo.casefold() == "comet" and (org is None or org.casefold() == "llm360")


def _repository_validation_complete(item: WorkItem, org: str | None) -> bool:
    """Require complete current coverage for the selected repository."""
    return not _repository_validation_required(item, org) or (
        _repository_validation_coverage(item).status == "complete"
    )


def _repository_validation_prompt_json(item: WorkItem, org: str | None) -> str:
    """Describe current admitted evidence without a second receipt inventory."""
    if not _repository_validation_required(item, org):
        return ""
    coverage = _repository_validation_coverage(item)
    summary: dict[str, object] = {
        "schema": "repository-validation-summary-v1",
        "status": coverage.status,
        "gaps": [{"check_id": gap.check_id, "reason": gap.reason} for gap in coverage.gaps],
        "uncovered_check_ids": list(coverage.uncovered_check_ids),
    }
    if coverage.status == "complete":
        attempt = item.payload["repository_validation_attempt"]
        plan = attempt.plan
        summary.update(
            {
                "repository": plan.repository,
                "pr_number": plan.pr_number,
                "reviewed_head": plan.reviewed_head,
                "reviewed_base": plan.reviewed_base,
                "plan_id": plan.plan_id,
                "profile_id": plan.profile_id,
                "profile_digest": plan.profile_digest,
                "generation": attempt.generation,
                "checks": [
                    {"check_id": check.check_id, "argv": list(check.argv)} for check in plan.checks
                ],
                "receipts": [
                    {
                        "check_id": receipt.check_id,
                        "receipt_id": receipt.receipt_id,
                        "evidence_kind": receipt.evidence_kind,
                        "status": receipt.status,
                    }
                    for receipt in coverage.receipts
                ],
            }
        )
    return json.dumps(summary, sort_keys=True, separators=(",", ":"))

#!/usr/bin/env python3
"""Validate production inputs and select independent CI checks.

The YAML inventory declares production candidates, policies, and validators.
Explicit exclusions identify non-production files in mixed directories.
Code-owned CI policies select repository contracts without selecting artifacts.

A policy exclusion overrides its inclusion. Policy order gives no precedence.
The classifier reads bounded NUL-delimited status and path pairs.
It rejects unsafe, duplicate, forbidden, and unclassified production paths.
It preserves first-seen policy order and sorts validator identifiers.
The selector emits separate artifact and repository-contract matrices.

Each empty matrix contains one successful no-op job.
The aggregate requires the exact job set and a `success` result for each job.
All invalid, missing, forbidden, or unsuccessful inputs fail closed.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import k2_ci_policy
import yaml
import yaml.resolver

# Inventory schema and bounded-input limits.
MAX_INVENTORY_BYTES = 1024 * 1024
MAX_CHANGE_BYTES = 16 * 1024 * 1024
MAX_PROMOTION_PATCH_BYTES = 32 * 1024 * 1024
MAX_REPOSITORY_PATH_BYTES = 64 * 1024 * 1024
MAX_CHANGES = 100_000
MAX_REPOSITORY_PATHS = 500_000
MAX_PATH_BYTES = 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
VALID_CHANGE_STATUSES = {"A", "D", "M", "T", "U"}
VALID_VALIDATOR_KINDS = {"artifact-fixture", "oci-image", "repository-contract"}
VALID_CONTRACT_TIERS = {"pr", "nightly"}
VALID_PROFILES = {"pr", "nightly", "promotion"}
TOP_LEVEL_FIELDS = {"policies", "production_candidates", "validators", "version"}
PRODUCTION_CANDIDATE_FIELDS = {"exclude", "files", "prefixes"}
VALIDATOR_FIELDS = {"check_name", "id", "kind", "tier"}
REQUIRED_VALIDATOR_FIELDS = {"check_name", "id", "kind"}
POLICY_FIELDS = {
    "component",
    "exclude",
    "forbidden",
    "id",
    "include",
    "validators",
}
REQUIRED_POLICY_FIELDS = {"component", "id", "include", "validators"}
RESERVED_VALIDATOR_IDS = {"noop"}
RESERVED_CHECK_NAMES = {"artifact-noop", "contract-noop"}

REPOSITORY_CONTRACT_VALIDATORS = (
    "schema-contracts",
    "control-deployment-contracts",
    "viewer-deployment-contracts",
    "workflow-contracts",
    "github-production-rules-contracts",
    "deployed-input-contracts",
    "sglang-k2-contracts",
    "sglang-glm53-image-contracts",
)
CI_POLICIES = (
    {
        "id": "ci-bootstrap",
        "include": (
            "deployment/deployed-inputs.yaml",
            "scripts/check_deployed_inputs.py",
            "scripts/k2_ci_policy.py",
            ".github/ci/k2-affected-scope.json",
            "scripts/deployed_artifact_validators.py",
            "tests/test_deployed_artifacts.py",
            "tests/test_deployed_inputs.py",
        ),
        "exclude": (),
        "validators": REPOSITORY_CONTRACT_VALIDATORS,
    },
    {
        "id": "sglang-k2-contracts",
        "include": (
            "docker/k2-sglang/**",
            "docs/k2-sglang-base-image.md",
            "scripts/build_k2_sglang_image.py",
            "scripts/probe_k2_sglang_image.py",
            "tests/test_k2_sglang_image.py",
        ),
        "exclude": (),
        "validators": ("sglang-k2-contracts",),
    },
    {
        "id": "sglang-glm53-image-contracts",
        "include": (
            "scripts/build-sglang-glm53-image.sh",
            "tests/test_glm53_image.py",
            "tests/test_runtime_artifact.py",
            "tests/test_sglang_redaction_images.py",
            "vendor/sglang-0515/apply_queue_patch.py",
            "vendor/sglang-glm53/**",
        ),
        "exclude": ("vendor/sglang-glm53/README.md",),
        "validators": ("sglang-glm53-image-contracts",),
    },
    {
        "id": "vllm-k2-contracts",
        "include": (
            "docker/vllm-k2/**",
            "docs/vllm-k2-wheel.md",
            "scripts/validate_vllm_k2_inputs.py",
            "scripts/build_vllm_k2_wheel.py",
            "scripts/vllm_k2_producer.py",
            "scripts/preflight_vllm_k2_wheel.py",
            "tests/test_vllm_k2_inputs.py",
            "tests/test_vllm_k2_wheel.py",
            "tests/test_vllm_k2_wheel_preflight.py",
        ),
        "exclude": (),
        "validators": ("deployed-input-contracts",),
    },
    {
        "id": "workflow-contracts",
        "include": (
            ".github/actions/**",
            ".github/workflows/**",
            ".gitleaks.toml",
            ".gitleaksignore",
            "scripts/check-recycling-coverage.py",
            "scripts/validate-pr-policy.sh",
            "scripts/emit-bootstrap-preparation.py",
            "scripts/emit-bootstrap-correlation.py",
            "scripts/emit-bootstrap-docs-build.py",
            "scripts/ci/**",
            "tests/test_gitleaks_policy.py",
            "tests/test_ci_workflows.py",
        ),
        "exclude": (),
        "validators": ("workflow-contracts",),
    },
    {
        "id": "github-production-rules-contracts",
        "include": (
            "scripts/ci/capture_github_production_readback.sh",
            "scripts/ci/verify_github_production_rules.py",
            "tests/test_github_production_rules.py",
            "tests/test_github_production_rules_pr.py",
        ),
        "exclude": (),
        "validators": ("github-production-rules-contracts",),
    },
    {
        "id": "schema-contracts",
        "include": (
            "schema/**",
            "scripts/export_schemas.py",
            "src/comet/image_probe.py",
            "tests/test_config.py",
            "tests/test_image_probe_contract.py",
            "tests/test_schema_export.py",
        ),
        "exclude": (),
        "validators": ("schema-contracts",),
    },
    {
        "id": "control-deployment-contracts",
        "include": ("tests/test_atomic_deploy.py",),
        "exclude": (),
        "validators": ("control-deployment-contracts",),
    },
    {
        "id": "viewer-deployment-contracts",
        "include": (
            "tests/test_grafana_provisioning.py",
            "tests/test_ingest.py",
            "tests/test_secondary_dashboard_rollups.py",
            "tests/test_telemetry_rollup_backfill.py",
        ),
        "exclude": (),
        "validators": ("viewer-deployment-contracts",),
    },
)
SGLANG_INPUT_BUILD_CONTRACT = {
    "docker/k2-sglang/Dockerfile": ("100644", "1e2a57deb81b2453658d7625dc7d6cc535fee134"),
    "scripts/build_k2_sglang_image.py": ("100644", "9d4868e43db842511b23b3e80110e780c82a2306"),
    "scripts/probe_k2_sglang_image.py": ("100644", "45dc5c94029581697e15a0e1689791fb60ec02af"),
}
CI_FORBIDDEN_PATHS = {
    ".gitmodules",
    "docker/sglang-faststart.Dockerfile",
}
RETIRED_REPOSITORY_PATHS = (
    "CHANGELOG.md",
    "changes/**",
    "schema/change-fragment.schema.json",
)


# Inventory parsing and repository path discovery.
class UniqueKeyLoader(yaml.SafeLoader):
    """Load safe YAML and reject duplicate mapping keys."""


def _construct_unique_mapping(
    loader: UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    """Construct one YAML mapping with unique keys."""
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise ValueError("YAML mapping keys must be scalar values") from error
        if duplicate:
            raise ValueError(f"duplicate YAML mapping key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class Change:
    """A changed repository path and its Git status."""

    status: str
    path: str


def load_inventory(path: Path) -> dict[str, Any]:
    """Read bounded regular YAML without symlinks or duplicate keys."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("this platform cannot reject inventory symlinks")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError("the deployed-input inventory must not be a symlink") from error
        raise
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("the deployed-input inventory must be a regular file")
        encoded = stream.read(MAX_INVENTORY_BYTES + 1)
    if len(encoded) > MAX_INVENTORY_BYTES:
        raise ValueError("the deployed-input inventory is too large")
    try:
        payload = yaml.load(encoded.decode("utf-8"), Loader=UniqueKeyLoader)
    except yaml.YAMLError as error:
        raise ValueError(f"invalid deployed-input YAML: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("the deployed-input inventory must be a mapping")
    if payload.get("version") != 1:
        raise ValueError("the deployed-input inventory version is not supported")
    return payload


def _pattern_matches(pattern: str, path: str) -> bool:
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-2])
    return path == pattern


def _is_forbidden_repository_path(path: str) -> bool:
    return path in CI_FORBIDDEN_PATHS or any(
        _pattern_matches(pattern, path) for pattern in RETIRED_REPOSITORY_PATHS
    )


def _policy_matches(policy: dict[str, Any], path: str) -> bool:
    included = any(_pattern_matches(pattern, path) for pattern in policy.get("include", []))
    excluded = any(_pattern_matches(pattern, path) for pattern in policy.get("exclude", []))
    return included and not excluded


def _is_production_candidate(inventory: dict[str, Any], path: str) -> bool:
    candidates = inventory["production_candidates"]
    included = path in candidates["files"] or any(
        path.startswith(prefix) for prefix in candidates["prefixes"]
    )
    excluded = any(_pattern_matches(pattern, path) for pattern in candidates["exclude"])
    return included and not excluded


def _is_production_exclusion(inventory: dict[str, Any], path: str) -> bool:
    candidates = inventory["production_candidates"]
    return any(_pattern_matches(pattern, path) for pattern in candidates["exclude"])


def _validate_path(path: str, *, field: str) -> None:
    if not path or path.startswith("/") or ".." in path.split("/") or "\0" in path:
        raise ValueError(f"invalid {field}: {path!r}")


def _validate_pattern(pattern: Any, *, field: str) -> str:
    if not isinstance(pattern, str):
        raise ValueError(f"{field} must contain strings")
    candidate = pattern[:-3] if pattern.endswith("/**") else pattern
    _validate_path(candidate, field=field)
    if any(character in candidate for character in "*?["):
        raise ValueError(f"invalid {field}: {pattern!r}")
    return pattern


def _string_list(value: Any, *, field: str, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    if not allow_empty and not value:
        raise ValueError(f"{field} must not be empty")
    if len(value) != len(set(value)):
        raise ValueError(f"{field} contains duplicate values")
    return value


def _require_fields(
    mapping: dict[Any, Any],
    *,
    allowed: set[str],
    required: set[str],
    field: str,
) -> None:
    keys = set(mapping)
    unknown = keys - allowed
    missing = required - keys
    if unknown:
        raise ValueError(f"{field} has unknown fields: {sorted(map(str, unknown))}")
    if missing:
        raise ValueError(f"{field} has missing required fields: {sorted(missing)}")


def repository_paths(repository_root: Path) -> list[str]:
    """Read a bounded NUL-delimited list of tracked and untracked files."""
    command = [
        "git",
        "-C",
        str(repository_root),
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if process.stdout is None:
        process.kill()
        process.wait()
        raise ValueError("Git did not provide a repository path stream")
    paths: list[str] = []
    pending = bytearray()
    total_bytes = 0
    try:
        while chunk := process.stdout.read(READ_CHUNK_BYTES):
            total_bytes += len(chunk)
            if total_bytes > MAX_REPOSITORY_PATH_BYTES:
                raise ValueError("the repository path list is too large")
            pending.extend(chunk)
            if len(pending) > MAX_PATH_BYTES and b"\0" not in pending:
                raise ValueError("a repository path is too large")
            while (separator := pending.find(b"\0")) >= 0:
                encoded_path = bytes(pending[:separator])
                del pending[: separator + 1]
                if not encoded_path:
                    raise ValueError("the repository path list contains an empty path")
                if len(encoded_path) > MAX_PATH_BYTES:
                    raise ValueError("a repository path is too large")
                path = encoded_path.decode("utf-8")
                if os.path.lexists(repository_root / path):
                    paths.append(path)
                    if len(paths) > MAX_REPOSITORY_PATHS:
                        raise ValueError("the repository path list contains too many paths")
        if pending:
            raise ValueError("the repository path list is not NUL-terminated")
        if process.wait() != 0:
            raise ValueError("Git could not list repository paths")
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise
    return paths


# Inventory validation and exact path ownership.
def validate_inventory(inventory: dict[str, Any], paths: list[str]) -> None:
    """Validate the schema, references, and exact policy ownership."""
    _require_fields(
        inventory,
        allowed=TOP_LEVEL_FIELDS,
        required=TOP_LEVEL_FIELDS,
        field="inventory",
    )
    candidates = inventory.get("production_candidates")
    if not isinstance(candidates, dict):
        raise ValueError("production_candidates must be a mapping")
    _require_fields(
        candidates,
        allowed=PRODUCTION_CANDIDATE_FIELDS,
        required=PRODUCTION_CANDIDATE_FIELDS,
        field="production_candidates",
    )
    prefixes = _string_list(candidates.get("prefixes"), field="production candidate prefixes")
    files = _string_list(candidates.get("files"), field="production candidate files")
    exclusions = _string_list(candidates.get("exclude"), field="production candidate exclusions")
    for prefix in prefixes:
        _validate_path(prefix, field="production candidate prefix")
        if not prefix.endswith("/"):
            raise ValueError(f"production candidate prefix must end with '/': {prefix}")
    for path in files:
        _validate_path(path, field="production candidate file")
    for pattern in exclusions:
        _validate_pattern(pattern, field="production candidate exclusion")

    validators = inventory.get("validators")
    if not isinstance(validators, list):
        raise ValueError("validators must be a list")
    validator_ids: set[str] = set()
    validator_kinds: dict[str, str] = {}
    check_names: set[str] = set()
    for validator in validators:
        if not isinstance(validator, dict):
            raise ValueError("each validator must be a mapping")
        _require_fields(
            validator,
            allowed=VALIDATOR_FIELDS,
            required=REQUIRED_VALIDATOR_FIELDS,
            field="validator",
        )
        validator_id = validator.get("id")
        check_name = validator.get("check_name")
        kind = validator.get("kind")
        if not isinstance(validator_id, str) or not IDENTIFIER_PATTERN.fullmatch(validator_id):
            raise ValueError(f"invalid validator id: {validator_id!r}")
        if validator_id in validator_ids:
            raise ValueError(f"duplicate validator id: {validator_id}")
        if validator_id in RESERVED_VALIDATOR_IDS:
            raise ValueError(f"reserved validator id: {validator_id}")
        validator_ids.add(validator_id)
        if not isinstance(check_name, str) or not IDENTIFIER_PATTERN.fullmatch(check_name):
            raise ValueError(f"invalid validator check name: {check_name!r}")
        if check_name in check_names:
            raise ValueError(f"duplicate validator check name: {check_name}")
        if check_name in RESERVED_CHECK_NAMES:
            raise ValueError(f"reserved validator check name: {check_name}")
        check_names.add(check_name)
        if kind not in VALID_VALIDATOR_KINDS:
            raise ValueError(f"invalid validator kind for {validator_id}: {kind!r}")
        tier = validator.get("tier")
        if kind == "repository-contract":
            if tier not in VALID_CONTRACT_TIERS:
                raise ValueError(f"invalid contract tier for {validator_id}: {tier!r}")
        elif tier is not None:
            raise ValueError(f"non-contract validator must not declare a tier: {validator_id}")
        validator_kinds[validator_id] = kind

    policies = inventory.get("policies")
    if not isinstance(policies, list):
        raise ValueError("policies must be a list")
    policy_ids: set[str] = set()
    used_validators: set[str] = set()
    for policy in policies:
        if not isinstance(policy, dict):
            raise ValueError("each policy must be a mapping")
        _require_fields(
            policy,
            allowed=POLICY_FIELDS,
            required=REQUIRED_POLICY_FIELDS,
            field="policy",
        )
        policy_id = policy.get("id")
        component = policy.get("component")
        if not isinstance(policy_id, str) or not IDENTIFIER_PATTERN.fullmatch(policy_id):
            raise ValueError(f"invalid policy id: {policy_id!r}")
        if policy_id in policy_ids:
            raise ValueError(f"duplicate policy id: {policy_id}")
        policy_ids.add(policy_id)
        if not isinstance(component, str) or not component:
            raise ValueError(f"invalid component for policy {policy_id}")
        include = _string_list(
            policy.get("include"), field=f"include patterns for {policy_id}", allow_empty=False
        )
        exclude = _string_list(policy.get("exclude", []), field=f"exclude patterns for {policy_id}")
        for pattern in [*include, *exclude]:
            _validate_pattern(pattern, field=f"pattern for {policy_id}")
        references = _string_list(policy["validators"], field=f"validators for {policy_id}")
        unknown = set(references) - validator_ids
        if unknown:
            raise ValueError(f"policy {policy_id} references unknown validators: {sorted(unknown)}")
        used_validators.update(references)
        if "forbidden" in policy and not isinstance(policy["forbidden"], bool):
            raise ValueError(f"forbidden for policy {policy_id} must be a Boolean")

    for ci_policy in CI_POLICIES:
        for pattern in (*ci_policy["include"], *ci_policy["exclude"]):
            _validate_pattern(pattern, field=f"CI pattern for {ci_policy['id']}")
        references = set(ci_policy["validators"])
        unknown = references - validator_ids
        if unknown:
            raise ValueError(
                f"CI policy {ci_policy['id']} references unknown validators: {sorted(unknown)}"
            )
        invalid_kinds = {
            validator_id
            for validator_id in references
            if validator_kinds[validator_id] != "repository-contract"
        }
        if invalid_kinds:
            raise ValueError(
                f"CI policy {ci_policy['id']} references artifact validators: "
                f"{sorted(invalid_kinds)}"
            )
        used_validators.update(references)

    unused_validators = validator_ids - used_validators
    if unused_validators:
        raise ValueError(f"unused validators: {sorted(unused_validators)}")

    for path in paths:
        _validate_path(path, field="repository path")
        matches = [policy for policy in policies if _policy_matches(policy, path)]
        is_candidate = _is_production_candidate(inventory, path)
        is_excluded = _is_production_exclusion(inventory, path)
        if is_candidate and len(matches) != 1:
            raise ValueError(
                f"production candidate must match exactly one policy: {path} "
                f"(matched {len(matches)})"
            )
        if is_excluded and matches:
            raise ValueError(f"production exclusion matches a deployment policy: {path}")
        if matches and not is_candidate:
            raise ValueError(f"deployment policy matches a non-production path: {path}")
        if len(matches) > 1:
            raise ValueError(
                f"repository path must match exactly one policy at most: {path} "
                f"(matched {len(matches)})"
            )
        if matches and matches[0].get("forbidden", False):
            raise ValueError(f"forbidden deployed input is present: {path}")
        if _is_forbidden_repository_path(path):
            raise ValueError(f"forbidden repository input is present: {path}")


# Policy selection and validator matrix generation.
def classify_paths(
    inventory: dict[str, Any],
    paths: list[str],
    *,
    deleted_paths: set[str] | None = None,
) -> dict[str, Any]:
    """Select production policies and validators for changed paths."""
    deleted_paths = deleted_paths or set()
    policy_ids: list[str] = []
    validator_ids: set[str] = set()
    for path in paths:
        _validate_path(path, field="changed path")
        if _is_forbidden_repository_path(path) and path not in deleted_paths:
            raise ValueError(f"forbidden repository input is present: {path}")
        matches = [
            policy for policy in inventory.get("policies", []) if _policy_matches(policy, path)
        ]
        is_candidate = _is_production_candidate(inventory, path)
        is_excluded = _is_production_exclusion(inventory, path)
        if is_candidate and len(matches) != 1:
            state = "unclassified" if not matches else "multiply classified"
            raise ValueError(f"production input is {state}: {path}")
        if is_excluded and matches:
            raise ValueError(f"production exclusion matches a deployment policy: {path}")
        if matches and not is_candidate:
            raise ValueError(f"deployment policy matches a non-production path: {path}")
        if len(matches) > 1:
            raise ValueError(f"changed path is multiply classified: {path}")
        for policy in matches:
            if policy.get("forbidden", False) and path not in deleted_paths:
                raise ValueError(f"forbidden deployed input is present: {path}")
            policy_id = str(policy["id"])
            if policy_id not in policy_ids:
                policy_ids.append(policy_id)
            validator_ids.update(policy.get("validators", []))
    return {
        "policies": policy_ids,
        "validators": sorted(validator_ids),
    }


def classify_ci_paths(
    inventory: dict[str, Any],
    paths: list[str],
    *,
    deleted_paths: set[str] | None = None,
) -> dict[str, Any]:
    """Select code-owned CI contracts without selecting production artifacts."""
    deleted_paths = deleted_paths or set()
    policy_ids: list[str] = []
    validator_ids: set[str] = set()
    declared = {validator["id"]: validator["kind"] for validator in inventory["validators"]}
    for path in paths:
        _validate_path(path, field="changed path")
        if _is_forbidden_repository_path(path) and path not in deleted_paths:
            raise ValueError(f"forbidden repository input is present: {path}")
        for policy in CI_POLICIES:
            if not _policy_matches(policy, path):
                continue
            policy_id = str(policy["id"])
            if policy_id not in policy_ids:
                policy_ids.append(policy_id)
            for validator_id in policy["validators"]:
                if declared.get(validator_id) != "repository-contract":
                    raise ValueError(
                        f"CI policy {policy_id} cannot select artifact validator {validator_id}"
                    )
                validator_ids.add(validator_id)
    return {"policies": policy_ids, "validators": sorted(validator_ids)}


def artifact_matrix(inventory: dict[str, Any], validator_ids: list[str]) -> dict[str, Any]:
    """Expand validators into fixed engine jobs or one successful no-op job."""
    validators = {validator["id"]: validator for validator in inventory.get("validators", [])}
    selected = [
        validator_id
        for validator_id in validator_ids
        if validators[validator_id]["kind"] in {"artifact-fixture", "oci-image"}
    ]
    if not selected:
        return {
            "include": [
                {
                    "id": "noop",
                    "check_name": "artifact-noop",
                    "engine": "none",
                    "kind": "noop",
                }
            ]
        }
    jobs: list[dict[str, str]] = []
    for validator_id in selected:
        validator = validators[validator_id]
        kind = validator["kind"]
        engines = ("docker", "podman") if kind == "oci-image" else ("podman",)
        for engine in engines:
            check_name = validator["check_name"]
            if kind == "oci-image":
                check_name = f"{check_name}-{engine}"
            jobs.append(
                {
                    "id": validator_id,
                    "check_name": check_name,
                    "engine": engine,
                    "kind": kind,
                }
            )
    return {"include": jobs}


def contract_matrix(inventory: dict[str, Any], validator_ids: list[str]) -> dict[str, Any]:
    """Expand repository contracts or return one successful no-op job."""
    validators = {validator["id"]: validator for validator in inventory.get("validators", [])}
    selected = [
        validator_id
        for validator_id in validator_ids
        if validators[validator_id]["kind"] == "repository-contract"
    ]
    if not selected:
        return {
            "include": [
                {
                    "id": "noop",
                    "check_name": "contract-noop",
                    "kind": "noop",
                }
            ]
        }
    return {
        "include": [
            {
                "id": validator_id,
                "check_name": validators[validator_id]["check_name"],
                "kind": "repository-contract",
            }
            for validator_id in selected
        ]
    }


# Changed-path and aggregate rules.
def read_changes0(path: Path) -> list[Change]:
    """Read bounded NUL-delimited status and path pairs.

    Reject rename and copy records so each change has one unambiguous path.
    """
    with path.open("rb") as stream:
        payload = stream.read(MAX_CHANGE_BYTES + 1)
    if len(payload) > MAX_CHANGE_BYTES:
        raise ValueError("the changed-path input is too large")
    if payload and not payload.endswith(b"\0"):
        raise ValueError("the changed-path input is not NUL-terminated")
    fields = payload.split(b"\0")[:-1] if payload else []
    if len(fields) % 2:
        raise ValueError("the changed-path input does not contain status and path pairs")
    if len(fields) // 2 > MAX_CHANGES:
        raise ValueError("the changed-path input contains too many changes")
    changes: list[Change] = []
    seen_paths: set[str] = set()
    for offset in range(0, len(fields), 2):
        if len(fields[offset + 1]) > MAX_PATH_BYTES:
            raise ValueError("the changed path is too long")
        try:
            status = fields[offset].decode("utf-8")
            changed_path = fields[offset + 1].decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("changed paths and statuses must use UTF-8") from error
        if status not in VALID_CHANGE_STATUSES:
            raise ValueError(f"unsupported Git change status: {status!r}")
        _validate_path(changed_path, field="changed path")
        if changed_path in seen_paths:
            raise ValueError(f"duplicate changed path: {changed_path}")
        seen_paths.add(changed_path)
        changes.append(Change(status=status, path=changed_path))
    return changes


def aggregate_succeeds(needs: dict[str, Any], required_jobs: list[str]) -> None:
    """Require exact job membership and a `success` result for every job."""
    if len(required_jobs) != len(set(required_jobs)):
        raise ValueError("the aggregate contains duplicate required jobs")
    expected = set(required_jobs)
    actual = set(needs)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"aggregate job membership differs: missing={missing}, extra={extra}")
    for job in required_jobs:
        state = needs[job]
        if not isinstance(state, dict) or state.get("result") != "success":
            result = state.get("result") if isinstance(state, dict) else None
            raise ValueError(f"required job did not succeed: {job} ({result!r})")


# GitHub output and command handlers.
def _write_github_output(path: Path, values: dict[str, str]) -> None:
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as output:
        for key, value in values.items():
            if not re.fullmatch(r"[a-z_]+", key) or "\n" in value or "\r" in value:
                raise ValueError("unsafe GitHub output")
            output.write(f"{key}={value}\n")


def _git_output(repository_root: Path, *arguments: str, limit: int = MAX_CHANGE_BYTES) -> bytes:
    """Read bounded output from one fixed Git operation."""
    process = subprocess.Popen(
        ["git", "--literal-pathspecs", "-C", str(repository_root), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        if process.stdout is None:
            raise ValueError("Git did not provide an output stream.")
        output = process.stdout.read(limit + 1)
        if len(output) > limit:
            raise ValueError("The Git source output is too large.")
        if process.wait(timeout=30) != 0:
            raise ValueError("Git could not read the specified source objects.")
        return output
    except subprocess.TimeoutExpired as error:
        raise ValueError("Git did not finish the source read in time.") from error
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()


def _patch_identity(patch: bytes, *, limit: int = MAX_CHANGE_BYTES) -> str:
    """Return one bounded, whitespace-preserving Git patch identity."""
    if len(patch) > limit:
        raise ValueError("The complete event patch is too large.")
    process = subprocess.Popen(
        ["git", "patch-id", "--verbatim"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    output = b""
    read_error: Exception | None = None
    write_error: Exception | None = None
    read_complete = threading.Event()

    def write_patch() -> None:
        nonlocal write_error
        try:
            if process.stdin is None:
                raise ValueError("Git did not provide an input stream.")
            process.stdin.write(patch)
        except Exception as error:
            write_error = error
        finally:
            if process.stdin is not None:
                process.stdin.close()

    def read_identity() -> None:
        nonlocal output, read_error
        try:
            if process.stdout is None:
                raise ValueError("Git did not provide an output stream.")
            output = process.stdout.read(MAX_CHANGE_BYTES + 1)
        except Exception as error:
            read_error = error
        finally:
            if process.stdout is not None:
                process.stdout.close()
            read_complete.set()

    writer = threading.Thread(target=write_patch, daemon=True)
    reader = threading.Thread(target=read_identity, daemon=True)
    started = time.monotonic()
    writer.start()
    reader.start()
    try:
        if not read_complete.wait(timeout=30):
            raise subprocess.TimeoutExpired(["git", "patch-id", "--verbatim"], 30)
        if len(output) > MAX_CHANGE_BYTES:
            raise ValueError("The patch identity output is too large.")
        remaining = max(0.0, 30 - (time.monotonic() - started))
        if process.wait(timeout=remaining) != 0:
            raise ValueError("Git could not calculate the patch identity.")
        writer.join(timeout=remaining)
        if writer.is_alive():
            raise subprocess.TimeoutExpired(["git", "patch-id", "--verbatim"], 30)
        if read_error is not None:
            raise ValueError("Git could not read the patch identity.") from read_error
        if write_error is not None:
            raise ValueError("Git could not read the complete event patch.") from write_error
        match = re.fullmatch(rb"([0-9a-f]{40}) ([0-9a-f]{40})\n?", output)
        if match is None:
            raise ValueError("Git did not return exactly one patch identity.")
        return match.group(1).decode("ascii")
    except subprocess.TimeoutExpired as error:
        raise ValueError("Git did not calculate the patch identity in time.") from error
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        writer.join(timeout=1)
        reader.join(timeout=1)


def _tree_identities(
    repository_root: Path, commit: str, paths: set[str]
) -> dict[str, tuple[str, str]]:
    output = _git_output(repository_root, "ls-tree", "-rz", commit, limit=MAX_REPOSITORY_PATH_BYTES)
    identities = {}
    for row in output.split(b"\0"):
        if not row:
            continue
        metadata, encoded_path = row.split(b"\t", 1)
        path = encoded_path.decode("utf-8")
        if path in paths:
            mode, _, object_id = metadata.decode("ascii").split()
            identities[path] = (mode, object_id)
    return identities


def _validate_sglang_build_contract(repository_root: Path, comparison_base: str, head: str) -> None:
    paths = set(SGLANG_INPUT_BUILD_CONTRACT)
    for commit in (comparison_base, head):
        if _tree_identities(repository_root, commit, paths) != SGLANG_INPUT_BUILD_CONTRACT:
            raise ValueError("The SGLang build contract differs from the fixed source.")


def _event_context(args: argparse.Namespace) -> dict[str, str]:
    fields = (args.event_name, args.run_id, args.run_attempt, args.base_sha, args.head_sha)
    if any(not isinstance(value, str) or not value for value in fields):
        raise ValueError("The scoped command must have the complete current event identity.")
    return {"name": args.event_name, "run_id": args.run_id, "run_attempt": args.run_attempt}


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"The JSON object contains a duplicate key: {key}")
        value[key] = item
    return value


def _scope_json(text: str) -> Any:
    if len(text.encode("utf-8")) > k2_ci_policy.MAX_SCOPE_BYTES:
        raise ValueError("The scope JSON is too large.")
    return json.loads(text, object_pairs_hook=_unique_json_object)


def _ordinary_scope(
    args: argparse.Namespace, changes: list[Change], *, promotion: bool
) -> dict[str, Any]:
    event = _event_context(args)
    root = args.repository_root
    for commit in (args.base_sha, args.head_sha):
        if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
            raise ValueError("The event source must use complete Git commit identities.")
        resolved = _git_output(root, "rev-parse", "--verify", f"{commit}^{{commit}}")
        if resolved.decode().strip() != commit:
            raise ValueError("The event object is not the specified commit.")
    checkout_head = _git_output(root, "rev-parse", "HEAD").decode().strip()
    if checkout_head != args.head_sha:
        raise ValueError("The checkout HEAD differs from the event head.")
    merge_base = args.base_sha
    if event["name"] == "pull_request":
        merge_base_output = _git_output(root, "merge-base", "--all", args.base_sha, args.head_sha)
        if re.fullmatch(rb"[0-9a-f]{40}\n?", merge_base_output) is None:
            raise ValueError("Git must return exactly one complete merge-base commit identity.")
        merge_base = merge_base_output.decode().strip()
    measured_diff = _git_output(
        root,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--name-status",
        "--no-renames",
        "-z",
        merge_base,
        args.head_sha,
        "--",
    )
    supplied_diff = b"".join(
        change.status.encode() + b"\0" + change.path.encode("utf-8") + b"\0" for change in changes
    )
    if supplied_diff != measured_diff:
        raise ValueError("The changed-path input differs from the complete event comparison.")
    patch_limit = MAX_PROMOTION_PATCH_BYTES if promotion else MAX_CHANGE_BYTES
    patch = _git_output(
        root,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--binary",
        merge_base,
        args.head_sha,
        "--",
        limit=patch_limit,
    )
    patch_id = _patch_identity(patch, limit=patch_limit)
    paths = {change.path for change in changes} | {k2_ci_policy.POLICY_PATH}
    before = _tree_identities(root, merge_base, paths)
    after = _tree_identities(root, args.head_sha, paths)
    records = []
    for change in changes:
        base_mode, base_blob = before.get(change.path, (None, None))
        head_mode, head_blob = after.get(change.path, (None, None))
        records.append(
            {
                "status": change.status,
                "path": change.path,
                "base_blob": base_blob,
                "head_blob": head_blob,
                "base_mode": base_mode,
                "head_mode": head_mode,
            }
        )
    policy_mode, policy_blob = after.get(k2_ci_policy.POLICY_PATH, (None, None))
    if policy_mode != "100644" or policy_blob is None:
        raise ValueError("The event head has no regular K2 policy module.")
    committed_policy = _git_output(root, "cat-file", "blob", policy_blob)
    policy_path = root / k2_ci_policy.POLICY_PATH
    if (
        policy_path.is_symlink()
        or policy_path.read_bytes() != committed_policy
        or Path(__file__).with_name("k2_ci_policy.py").read_bytes() != committed_policy
    ):
        raise ValueError("The current policy bytes differ from the committed event policy.")
    bootstrap = None
    if not promotion and any(change.path == k2_ci_policy.DECLARATION_PATH for change in changes):
        declaration = after.get(k2_ci_policy.DECLARATION_PATH)
        if declaration is None or declaration[0] != "100644":
            raise ValueError("The current bootstrap declaration must be a regular JSON file.")
        bootstrap = _scope_json(
            _git_output(
                root, "cat-file", "blob", declaration[1], limit=MAX_INVENTORY_BYTES
            ).decode()
        )
    scope = k2_ci_policy.build_scope(
        event=event,
        base=args.base_sha,
        head=args.head_sha,
        merge_base=merge_base,
        changes=records,
        patch_id=patch_id,
        policy_sha256=hashlib.sha256(committed_policy).hexdigest(),
        promotion=promotion,
        bootstrap=bootstrap,
    )
    if "k2-sglang-inputs-v1" in scope["rules"]:
        _validate_sglang_build_contract(root, merge_base, args.head_sha)
    if scope["route"] == "affected":
        objects = {
            cast("str", row[field])
            for row in records
            for field in ("base_blob", "head_blob")
            if row[field] is not None
        }
        for object_id in sorted(objects):
            _git_output(root, "cat-file", "-e", f"{object_id}^{{blob}}")
    return scope


def _validated_scope(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, str]]:
    event = _event_context(args)
    scope = k2_ci_policy.validate_scope(
        _scope_json(args.affected_scope_json),
        event=event,
        base=args.base_sha,
        head=args.head_sha,
        checkout_head=_git_output(args.repository_root, "rev-parse", "HEAD").decode().strip(),
        policy_sha256=hashlib.sha256(
            (args.repository_root / k2_ci_policy.POLICY_PATH).read_bytes()
        ).hexdigest(),
    )
    return scope, event


def _classify(args: argparse.Namespace) -> int:
    """Validate inputs and emit selector outputs."""
    inventory = load_inventory(args.inventory)
    validate_inventory(inventory, repository_paths(args.repository_root))
    changes = read_changes0(args.changes0)
    changed_paths = [change.path for change in changes]
    deleted_paths = {change.path for change in changes if change.status == "D"}
    selected = classify_paths(
        inventory,
        changed_paths,
        deleted_paths=deleted_paths,
    )
    ci_selected = classify_ci_paths(
        inventory,
        changed_paths,
        deleted_paths=deleted_paths,
    )
    validators = {validator["id"]: validator for validator in inventory["validators"]}
    profile = "promotion" if args.selection == "all" else args.profile
    if profile == "promotion":
        selected["validators"] = sorted(validator["id"] for validator in inventory["validators"])
        ci_selected = {"policies": [], "validators": []}
    elif profile == "nightly":
        selected["validators"] = []
        ci_selected = {
            "policies": [],
            "validators": sorted(
                validator_id
                for validator_id, validator in validators.items()
                if validator["kind"] == "repository-contract" and validator["tier"] == "nightly"
            ),
        }
    else:
        selected["validators"] = [
            validator_id
            for validator_id in selected["validators"]
            if validators[validator_id]["kind"] != "repository-contract"
        ]
        ci_selected["validators"] = [
            validator_id
            for validator_id in ci_selected["validators"]
            if validators[validator_id]["tier"] == "pr"
        ]
    ordinary_scope = None
    if args.ordinary_test_scope is not None:
        if profile == "nightly":
            raise ValueError("The K2 scope does not select the nightly profile.")
        ordinary_scope = _ordinary_scope(args, changes, promotion=profile == "promotion")
    result = {
        **selected,
        "ci_policies": ci_selected["policies"],
        "artifact_matrix": artifact_matrix(inventory, selected["validators"]),
        "contract_matrix": contract_matrix(
            inventory,
            sorted({*selected["validators"], *ci_selected["validators"]}),
        ),
    }
    if ordinary_scope is not None:
        result["ordinary_test_scope"] = ordinary_scope
    if args.github_output is not None:
        outputs = {
            name: json.dumps(result[name], sort_keys=True, separators=(",", ":"))
            for name in ("artifact_matrix", "contract_matrix")
        }
        if ordinary_scope is not None:
            outputs["ordinary_test_scope"] = json.dumps(
                ordinary_scope, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
        _write_github_output(args.github_output, outputs)
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


def _inventory_check(args: argparse.Namespace) -> int:
    inventory = load_inventory(args.inventory)
    paths = repository_paths(args.repository_root)
    validate_inventory(inventory, paths)
    candidate_count = sum(_is_production_candidate(inventory, path) for path in paths)
    sys.stdout.write(
        json.dumps({"production_candidate_paths": candidate_count}, sort_keys=True) + "\n"
    )
    return 0


def _aggregate(args: argparse.Namespace) -> int:
    needs = json.loads(args.needs_json)
    if not isinstance(needs, dict):
        raise ValueError("the aggregate needs value must be a mapping")
    aggregate_succeeds(needs, args.required_job)
    return 0


def _validate(args: argparse.Namespace) -> int:
    """Confirm registry parity and run one declared validator."""
    from deployed_artifact_validators import VALIDATOR_KINDS, run_validator

    inventory = load_inventory(args.inventory)
    validate_inventory(inventory, repository_paths(args.repository_root))
    declared = {item["id"]: item["kind"] for item in inventory["validators"]}
    if declared != VALIDATOR_KINDS:
        raise ValueError("the inventory and code-owned validator dispatch differ")
    scope_arguments: dict[str, Any] = {}
    if args.affected_scope_json is not None:
        scope, event = _validated_scope(args)
        scope_arguments = {
            "affected_scope": scope,
            "event": event,
            "base": args.base_sha,
            "head": args.head_sha,
        }
    result = run_validator(
        args.validator,
        args.repository_root,
        container_engine=args.container_engine,
        **scope_arguments,
    )
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


# Command-line interface.
def _parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory",
        type=Path,
        default=root / "deployment" / "deployed-inputs.yaml",
    )
    parser.add_argument("--repository-root", type=Path, default=root)
    commands = parser.add_subparsers(dest="command", required=True)

    inventory_check = commands.add_parser("inventory-check")
    inventory_check.set_defaults(handler=_inventory_check)

    classify = commands.add_parser("classify")
    classify.add_argument("--changes0", type=Path, required=True)
    classify.add_argument("--selection", choices=("all", "changed"), default="changed")
    classify.add_argument("--profile", choices=sorted(VALID_PROFILES), default="pr")
    classify.add_argument("--github-output", type=Path)
    classify.add_argument("--ordinary-test-scope", choices=(k2_ci_policy.SCOPE,))
    classify.set_defaults(handler=_classify)

    aggregate = commands.add_parser("aggregate")
    aggregate.add_argument("--needs-json", required=True)
    aggregate.add_argument("--required-job", action="append", default=[], required=True)
    aggregate.set_defaults(handler=_aggregate)

    validate = commands.add_parser("validate")
    validate.add_argument("--validator", required=True)
    validate.add_argument("--affected-scope-json")
    validate.add_argument(
        "--container-engine",
        choices=("docker", "none", "podman"),
        required=True,
    )
    validate.set_defaults(handler=_validate)
    for scoped_command in (classify, validate):
        scoped_command.add_argument("--event-name")
        scoped_command.add_argument("--run-id")
        scoped_command.add_argument("--run-attempt")
        scoped_command.add_argument("--base-sha")
        scoped_command.add_argument("--head-sha")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the selected policy command."""
    args = _parser().parse_args(argv)
    args.repository_root = args.repository_root.resolve(strict=True)
    args.inventory = args.inventory.absolute()
    return args.handler(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        RuntimeError,
        subprocess.CalledProcessError,
        UnicodeError,
        ValueError,
    ) as error:
        sys.stderr.write(f"deployed-input policy failed: {error}\n")
        raise SystemExit(1) from None

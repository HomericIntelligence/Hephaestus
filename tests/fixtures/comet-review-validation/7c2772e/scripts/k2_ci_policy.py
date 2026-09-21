"""Select fixed K2 CI cases from validated source records.

The classifier examines Git objects and declaration bindings. Consumers examine the
same-run record, current source, and allocation with this pure policy.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, cast

SCOPE = "k2-horizon-v1"
MAX_SCOPE_BYTES = 1024 * 1024
POLICY_PATH = "scripts/k2_ci_policy.py"
DECLARATION_PATH = ".github/ci/k2-affected-scope.json"
BOOTSTRAP_RULE = "k2-ci-amendment-v1"
JOB_IDS = (
    "test",
    "recycling-coverage",
    "schema-contracts",
    "control-deployment-contracts",
    "viewer-deployment-contracts",
    "workflow-contracts",
    "github-production-rules-contracts",
    "deployed-input-contracts",
    "sglang-k2-contracts",
    "sglang-glm53-image-contracts",
)
DOCUMENT_PATHS = frozenset(
    {
        "docs/k2-horizon-family.md",
        "docs/k2-sglang-base-image.md",
        "docs/k2-horizon/procedure.md",
        "docs/k2-horizon/0p9b.md",
        "docs/k2-horizon/375b-a23b.md",
        "docs/k2-horizon/7b-fp8.md",
        "docs/k2-horizon/32b-fp8.md",
        "docs/k2-horizon/375b-a23b-fp8.md",
        "docs/k2-horizon/mova-36b-a4b-fp8.md",
    }
)
BOOTSTRAP_PATHS = frozenset(
    {
        ".gitleaksignore",
        "tests/test_gitleaks_policy.py",
        ".github/workflows/ci.yml",
        ".github/workflows/contracts.yml",
        POLICY_PATH,
        "scripts/check_deployed_inputs.py",
        "scripts/ci/run-test-tier.py",
        "scripts/deployed_artifact_validators.py",
        "tests/test_deployed_inputs.py",
        "tests/test_deployed_artifacts.py",
        "tests/test_ci_workflows.py",
        "docs/process/testing.md",
        "deployment/deployed-inputs.yaml",
    }
)
CHANGE_FIELDS = {
    "status",
    "path",
    "base_blob",
    "head_blob",
    "base_mode",
    "head_mode",
}
SCOPE_FIELDS = {
    "version",
    "scope",
    "route",
    "event",
    "base",
    "head",
    "merge_base",
    "changes",
    "changes_sha256",
    "patch_id",
    "policy_sha256",
    "rules",
    "jobs",
}
OBJECT_ID = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
K2_TOKEN = re.compile(r"(?:^|[-_.])k2(?:$|[-_.])", re.IGNORECASE)
INPUT_RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "k2-913-context-v1",
        "owner": "test",
        "patch_id": "4b1208f5108c995062c34031f345f6391784661d",
        "changes": [
            {
                "status": "A",
                "path": "deployment/contexts/m2-k2-horizon-0p9b-autoscale.yaml",
                "base_blob": None,
                "head_blob": "61187218881911a06331893d102a6f5fd1392b85",
                "base_mode": None,
                "head_mode": "100644",
            },
            {
                "status": "M",
                "path": "deployment/contexts/registry.yaml",
                "base_blob": "c61cbc4a19e105a2df5936775f518465fe230dc8",
                "head_blob": "9c50f758ef6e190ba96853aa1a2c9bda7cce5367",
                "base_mode": "100644",
                "head_mode": "100644",
            },
            {
                "status": "M",
                "path": "schema/deployment-context.schema.json",
                "base_blob": "5700af924d335d366b5a577e8098d2bc74184ff7",
                "head_blob": "263e7717a3a753f808e5bf6476a20c2261933e05",
                "base_mode": "100644",
                "head_mode": "100644",
            },
            {
                "status": "M",
                "path": "src/comet/deployment_context.py",
                "base_blob": "5b89d2cc73c4b1f5ea12e8a7129f1c8286b2d50f",
                "head_blob": "19db94faedac6c9c5da1f81523f0eebaabc056f7",
                "base_mode": "100644",
                "head_mode": "100644",
            },
            {
                "status": "M",
                "path": "tests/test_deployment_context.py",
                "base_blob": "32e69933ddc182f6ee9d73c3abdd5ddbf1711cce",
                "head_blob": "9b1906021df8f99b89b0d68483c46282d3efb16f",
                "base_mode": "100644",
                "head_mode": "100644",
            },
            {
                "status": "M",
                "path": "tests/test_deployment_context_env.py",
                "base_blob": "545aa33afc327d92797f3ef0be26c2b3090005ad",
                "head_blob": "013f1fc0ff7bd4d89f93d98a8c790b655a06e77b",
                "base_mode": "100644",
                "head_mode": "100644",
            },
            {
                "status": "M",
                "path": "tests/test_schema_export.py",
                "base_blob": "5ae5ce9a95d16f34b5916ac57f71cf84f32826fe",
                "head_blob": "a2c6b4d882dc6597cb85a20ff98c065623b1eea6",
                "base_mode": "100644",
                "head_mode": "100644",
            },
        ],
        "selectors": (
            (
                "tests/test_deployment_context.py::"
                "test_k2_context_identity_keeps_the_complete_association"
            ),
            (
                "tests/test_deployment_context_env.py::"
                "test_k2_autoscale_context_is_the_only_changed_qualification_policy"
            ),
            "tests/test_schema_export.py::test_deployment_context_schema_is_exported_and_versioned",
        ),
    },
    {
        "id": "k2-sglang-inputs-v1",
        "owner": "sglang-k2-contracts",
        "changes": [
            {
                "status": "M",
                "path": "docker/k2-sglang/inputs.json",
                "base_blob": "c5ee4a522963ad1de870c579fc370d48bad43acb",
                "head_blob": "097d4955c23fc88502a216a7e05362969c71b9c6",
                "base_mode": "100644",
                "head_mode": "100644",
            },
            {
                "status": "M",
                "path": "docker/k2-sglang/requirements.lock",
                "base_blob": "4c1cba544983fa7fa76fccf2ee02c2d4438ebe43",
                "head_blob": "05eb008c2a6f16c643336ec90f38d7c66c159a71",
                "base_mode": "100644",
                "head_mode": "100644",
            },
        ],
        "selectors": (
            "tests/test_k2_sglang_image.py::test_input_record_is_complete",
            "tests/test_k2_sglang_image.py::test_input_identity_must_match_its_build_input[source]",
            "tests/test_k2_sglang_image.py::test_altered_local_lock_fails[requirements.lock]",
            "tests/test_deployed_inputs.py::test_sglang_k2_inputs_do_not_select_ci_image_build[docker/k2-sglang/inputs.json]",
            "tests/test_deployed_inputs.py::test_sglang_k2_inputs_do_not_select_ci_image_build[docker/k2-sglang/requirements.lock]",
        ),
    },
    {
        "id": "k2-p06a2-bootstrap-inputs-v1",
        "owner": "deployed-input-contracts",
        "changes": [
            {
                "status": "M",
                "path": "scripts/vllm_k2_producer.py",
                "base_blob": "0d71d65462df3714c0551dce502a5661972bebd7",
                "head_blob": "d94da74652b0ba614d38569822a37649aaa901d3",
                "base_mode": "100644",
                "head_mode": "100644",
            },
            {
                "status": "M",
                "path": "tests/test_vllm_k2_wheel.py",
                "base_blob": "62ba0e898ec4902968ca2223cfa915b1065ee063",
                "head_blob": "3b7205f3dba7094a13d2c26d260cb0cc6a1df32a",
                "base_mode": "100644",
                "head_mode": "100644",
            },
        ],
        "selectors": (
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_inputs[interpreter-link]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_inputs[interpreter-copy]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_inputs[uid]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_inputs[gid]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_inputs[version]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_inputs[platform]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_inputs[architecture]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_inputs[executable]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_inputs[record-type]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_inputs[requirements]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_inputs[duplicate-name]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_inputs[profile]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_inputs[pip-absent]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_inputs[pip-duplicate]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_inputs[pip-foreign]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_inputs[pip-code]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_inputs[unsafe-link]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_inputs[read]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_inputs[close]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_inputs[cancel-close]",
            "tests/test_vllm_k2_wheel.py::test_producer_manifest_uses_standard_library",
        ),
    },
    {
        "id": "k2-p06b1-fixed-requests-v1",
        "owner": "deployed-input-contracts",
        "changes": [
            {
                "status": "M",
                "path": "scripts/vllm_k2_producer.py",
                "base_blob": "d94da74652b0ba614d38569822a37649aaa901d3",
                "head_blob": "3c403ef664b14b7b6e3e6db3dfa5fd8af234ea2d",
                "base_mode": "100644",
                "head_mode": "100644",
            },
            {
                "status": "M",
                "path": "tests/test_vllm_k2_wheel.py",
                "base_blob": "3b7205f3dba7094a13d2c26d260cb0cc6a1df32a",
                "head_blob": "849de23ff15372a0810c5345c647a40191b15043",
                "base_mode": "100644",
                "head_mode": "100644",
            },
        ],
        "selectors": (
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[exact-request]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[invalid-step]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[timeout]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[stderr-limit]",
            "tests/test_vllm_k2_wheel.py::test_producer_manifest_uses_standard_library",
        ),
    },
    {
        "id": "k2-p06b2-strict-decoder-v1",
        "owner": "deployed-input-contracts",
        "changes": [
            {
                "status": "M",
                "path": "scripts/vllm_k2_producer.py",
                "base_blob": "3c403ef664b14b7b6e3e6db3dfa5fd8af234ea2d",
                "head_blob": "80effed9071a0cd3edfb25abf57202698e848e61",
                "base_mode": "100644",
                "head_mode": "100644",
            },
            {
                "status": "M",
                "path": "tests/test_vllm_k2_wheel.py",
                "base_blob": "849de23ff15372a0810c5345c647a40191b15043",
                "head_blob": "173b1f3a93c56a3d33653bcc03e63373aa101f00",
                "base_mode": "100644",
                "head_mode": "100644",
            },
        ],
        "selectors": (
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[exact-request]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[invalid-step]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[timeout]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[stderr-limit]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[capture-json]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[json-empty]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[json-duplicate]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[json-shape]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[json-trailing]",
        ),
    },
    {
        "id": "k2-p06b3-bootstrap-process-v1",
        "owner": "deployed-input-contracts",
        "changes": [
            {
                "status": "M",
                "path": "scripts/vllm_k2_producer.py",
                "base_blob": "80effed9071a0cd3edfb25abf57202698e848e61",
                "head_blob": "c9a92aa858c5bc887c3af4cae01e15e4037dec1a",
                "base_mode": "100644",
                "head_mode": "100644",
            },
            {
                "status": "M",
                "path": "tests/test_vllm_k2_wheel.py",
                "base_blob": "173b1f3a93c56a3d33653bcc03e63373aa101f00",
                "head_blob": "00a85c1b07687840a3158374613403e81d84fa24",
                "base_mode": "100644",
                "head_mode": "100644",
            },
        ],
        "selectors": (
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[capture-text]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[capture-json]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[launch]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[nonzero]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[timeout]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[stdout-limit]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[stderr-limit]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[read]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[json-empty]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[json-duplicate]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[json-shape]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[json-trailing]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[reaped]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[reap-failed]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[close]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[cancel]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[invalid-step]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_step[exact-request]",
            "tests/test_vllm_k2_wheel.py::test_producer_manifest_uses_standard_library",
        ),
    },
    {
        "id": "k2-p06c1a-bootstrap-work-v1",
        "owner": "deployed-input-contracts",
        "changes": [
            {
                "status": "M",
                "path": "scripts/vllm_k2_producer.py",
                "base_blob": "c9a92aa858c5bc887c3af4cae01e15e4037dec1a",
                "head_blob": "80b98276a62209612cceebd3805008fabb877998",
                "base_mode": "100644",
                "head_mode": "100644",
            },
            {
                "status": "M",
                "path": "tests/test_vllm_k2_wheel.py",
                "base_blob": "00a85c1b07687840a3158374613403e81d84fa24",
                "head_blob": "b46e6a94fc1cbb055480cfc8d753ebb12d113eb0",
                "base_mode": "100644",
                "head_mode": "100644",
            },
        ],
        "selectors": (
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_policy[ordinary-modules]",
            "tests/test_vllm_k2_wheel.py::test_producer_bootstrap_policy[pre-existing-target]",
        ),
    },
)

BOOTSTRAP_SELECTORS = {
    "deployed-input-contracts": (
        "tests/test_deployed_inputs.py::test_k2_ordinary_gitleaks_scope_is_bound[valid]",
        "tests/test_deployed_inputs.py::test_k2_ordinary_gitleaks_scope_is_bound[blob]",
        "tests/test_deployed_inputs.py::test_k2_ordinary_gitleaks_scope_is_bound[mode]",
        "tests/test_deployed_inputs.py::test_k2_ordinary_gitleaks_scope_is_bound[unknown]",
        "tests/test_deployed_inputs.py::test_k2_ordinary_docs_scope_is_bound[current]",
        "tests/test_deployed_inputs.py::test_k2_ordinary_input_scope_has_one_test_owner",
        "tests/test_deployed_inputs.py::test_k2_ordinary_mixed_unknown_scope_fails[unknown-source]",
        "tests/test_deployed_inputs.py::test_k2_ordinary_mixed_unknown_scope_fails[unknown-k2-page]",
        "tests/test_deployed_inputs.py::test_k2_ordinary_mixed_unknown_scope_fails[shared-fixture]",
        "tests/test_deployed_inputs.py::test_k2_ordinary_scope_preserves_other_routes[unrelated-pr]",
        "tests/test_deployed_inputs.py::test_k2_ordinary_scope_preserves_other_routes[unrelated-main]",
        "tests/test_deployed_inputs.py::test_k2_ordinary_scope_preserves_other_routes[promotion-pr]",
        "tests/test_deployed_inputs.py::test_k2_ordinary_scope_preserves_other_routes[prod-push]",
        "tests/test_deployed_inputs.py::test_k2_ordinary_scope_rejects_invalid_source[missing-base]",
        "tests/test_deployed_inputs.py::test_k2_ordinary_scope_rejects_invalid_source[wrong-head]",
        "tests/test_deployed_inputs.py::test_k2_ordinary_scope_rejects_invalid_source[type-change]",
        "tests/test_deployed_inputs.py::test_k2_ordinary_scope_rejects_invalid_source[unmerged]",
        "tests/test_deployed_inputs.py::test_k2_ordinary_scope_rejects_invalid_source[symlink]",
        "tests/test_deployed_inputs.py::test_k2_ordinary_scope_uses_merge_base_and_all_status_paths",
        "tests/test_deployed_inputs.py::test_k2_ordinary_scope_rejects_changed_decisions[policy-digest]",
        "tests/test_deployed_inputs.py::test_k2_ordinary_scope_rejects_changed_decisions[selector-id]",
        "tests/test_deployed_inputs.py::test_k2_ordinary_scope_rejects_changed_decisions[job-owner]",
        "tests/test_deployed_artifacts.py::test_k2_scoped_contract_runs_only_its_owned_nodes",
        "tests/test_deployed_artifacts.py::test_k2_scoped_docs_contract_records_a_noop",
        "tests/test_deployed_artifacts.py::test_k2_scoped_workflow_contract_keeps_actionlint",
        "tests/test_deployed_inputs.py::test_inventory_covers_each_production_candidate_path",
        "tests/test_deployed_inputs.py::test_unknown_production_input_fails_closed[scripts/future-deployment-recipe.sh]",
        "tests/test_deployed_inputs.py::test_ci_bootstrap_changes_select_all_contract_validators[deployment/deployed-inputs.yaml]",
        "tests/test_deployed_inputs.py::test_unaffected_path_emits_successful_noop_policy",
        "tests/test_deployed_inputs.py::test_all_selection_emits_every_validator_without_noop_entries",
        "tests/test_deployed_inputs.py::test_classification_writes_safe_github_outputs",
        "tests/test_deployed_artifacts.py::test_unknown_validator_fails_before_process_execution",
        "tests/test_deployed_artifacts.py::test_github_production_rule_validator_propagates_pytest_failure",
    ),
    "workflow-contracts": (
        "tests/test_gitleaks_policy.py::test_gitleaks_ignores_only_known_public_findings",
        "tests/test_ci_workflows.py::test_affected_tier_executes_exact_nodes",
        "tests/test_ci_workflows.py::test_affected_tier_rejects_incomplete_results[empty]",
        "tests/test_ci_workflows.py::test_affected_tier_rejects_incomplete_results[all-skipped]",
        "tests/test_ci_workflows.py::test_affected_tier_rejects_incomplete_results[failed]",
        "tests/test_ci_workflows.py::test_affected_tier_rejects_incomplete_results[wrong-node]",
        "tests/test_ci_workflows.py::test_k2_ordinary_jobs_require_a_valid_scope",
        "tests/test_ci_workflows.py::test_ci_selects_pull_request_and_promotion_test_profiles",
        "tests/test_ci_workflows.py::test_ci_installs_bubblewrap_before_each_test_profile",
        "tests/test_ci_workflows.py::test_pre_commit_excludes_automated_pytest_profiles",
        "tests/test_ci_workflows.py::test_deployment_policy_uses_immutable_status_aware_diff",
        "tests/test_ci_workflows.py::test_contract_workflow_installs_bubblewrap_only_for_workflow_contracts",
        "tests/test_ci_workflows.py::test_prod_push_selects_all_tests_and_validators",
        "tests/test_ci_workflows.py::test_ci_calls_each_reusable_gate",
        "tests/test_ci_workflows.py::test_test_tier_cli_rejects_the_removed_time_limit_option",
    ),
}


def _mapping(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"The {label} fields differ from the fixed schema.")
    return value


def _identity(value: Any, pattern: re.Pattern[str], label: str) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"The {label} identity is invalid.")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _changes(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("The change records must be a list.")
    paths: set[str] = set()
    for row in value:
        _mapping(row, CHANGE_FIELDS, "change record")
        path = row["path"]
        if (
            not isinstance(path, str)
            or not path
            or "\0" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or path in paths
        ):
            raise ValueError("The change path is invalid or duplicated.")
        paths.add(path)
        if not isinstance(row["status"], str) or row["status"] not in {"A", "D", "M", "T"}:
            raise ValueError("The change status is not supported.")
        for side in ("base", "head"):
            blob = row[f"{side}_blob"]
            mode = row[f"{side}_mode"]
            absent = (side == "base" and row["status"] == "A") or (
                side == "head" and row["status"] == "D"
            )
            if absent:
                if blob is not None or mode is not None:
                    raise ValueError("An absent change side has an object identity.")
            else:
                _identity(blob, OBJECT_ID, "change object")
                if not isinstance(mode, str) or mode not in {
                    "100644",
                    "100755",
                    "120000",
                    "160000",
                }:
                    raise ValueError("The Git mode is not supported.")
    return sorted(cast("list[dict[str, Any]]", value), key=lambda row: cast("str", row["path"]))


def _regular_change(row: dict[str, Any], *, addition: bool = False) -> None:
    expected = {"A", "M"} if addition else {"M"}
    if (
        row["status"] not in expected
        or row["head_mode"] != "100644"
        or row["base_mode"] != (None if row["status"] == "A" else "100644")
    ):
        raise ValueError("The affected rule does not admit this status or Git mode.")


def _bootstrap_rows(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in row.items() if key != "status"}
        for row in changes
        if row["path"] in BOOTSTRAP_PATHS
    ]


def _validate_bootstrap(bootstrap: Any, changes: list[dict[str, Any]]) -> None:
    """Examine declaration fields against the producer's measured change records."""
    _mapping(bootstrap, {"version", "rule", "files"}, "bootstrap declaration")
    if type(bootstrap["version"]) is not int or bootstrap["version"] != 1:
        raise ValueError("The bootstrap declaration version is not supported.")
    if bootstrap["rule"] != BOOTSTRAP_RULE or not isinstance(bootstrap["files"], list):
        raise ValueError("The bootstrap declaration rule or file list is invalid.")
    expected = _bootstrap_rows(changes)
    fields = CHANGE_FIELDS - {"status"}
    for row in bootstrap["files"]:
        _mapping(row, fields, "bootstrap file")
        if not isinstance(row["path"], str):
            raise ValueError("The bootstrap path must be a string.")
    actual = sorted(
        cast("list[dict[str, Any]]", bootstrap["files"]), key=lambda row: cast("str", row["path"])
    )
    if not expected or actual != expected:
        raise ValueError("The bootstrap file bindings differ from the complete change.")


def _allocation(
    changes: list[dict[str, Any]], *, patch_id: str, promotion: bool, bootstrap: Any
) -> tuple[str, list[str], dict[str, Any]]:
    paths = {row["path"] for row in changes}
    affected = DECLARATION_PATH in paths or any(
        K2_TOKEN.search(part) for path in paths for part in path.split("/")
    )
    route = "promotion" if promotion else "affected" if affected else "legacy"
    if route != "affected":
        if bootstrap is not None:
            raise ValueError("An unscoped route cannot use a bootstrap declaration.")
        return (
            route,
            [],
            {
                job: {
                    "decision": route,
                    "selectors": [],
                    "reason": f"The existing {route} profile applies.",
                }
                for job in JOB_IDS
            },
        )
    jobs: dict[str, dict[str, Any]] = {
        job: {
            "decision": "unaffected",
            "selectors": [],
            "reason": "This job has no affected pytest cases.",
        }
        for job in JOB_IDS
    }
    rules: list[str] = []
    unallocated = {row["path"]: row for row in changes}
    for path in paths & DOCUMENT_PATHS:
        _regular_change(unallocated.pop(path))
    if paths & DOCUMENT_PATHS:
        rules.append("k2-documents-v1")
    if DECLARATION_PATH in paths:
        _regular_change(unallocated.pop(DECLARATION_PATH), addition=True)
        _validate_bootstrap(bootstrap, changes)
        for path in paths & BOOTSTRAP_PATHS:
            _regular_change(unallocated.pop(path), addition=True)
        rules.append(BOOTSTRAP_RULE)
        for owner, nodes in BOOTSTRAP_SELECTORS.items():
            jobs[owner] = {
                "decision": "run",
                "selectors": list(nodes),
                "reason": "Run the fixed CI amendment cases.",
            }
        jobs["test"]["reason"] = "The contract jobs own the affected cases."
    elif bootstrap is not None:
        raise ValueError("The current change has no bootstrap declaration.")
    for rule in INPUT_RULES:
        expected = rule["changes"]
        candidate = sorted(unallocated.values(), key=lambda row: row["path"])
        if "patch_id" in rule:
            structural_fields = {"status", "path", "base_mode", "head_mode"}
            candidate_identity = [
                {field: row[field] for field in structural_fields} for row in candidate
            ]
            expected_identity = [
                {field: row[field] for field in structural_fields} for row in expected
            ]
            matches = patch_id == rule["patch_id"] and candidate_identity == expected_identity
        else:
            matches = candidate == expected
        if not matches:
            continue
        if rule["owner"] == "deployed-input-contracts" and len(changes) != len(expected):
            continue
        nodes = list(rule["selectors"])
        owner = rule["owner"]
        jobs[owner] = {
            "decision": "run",
            "selectors": nodes,
            "reason": "Run the fixed input cases.",
        }
        if BOOTSTRAP_RULE not in rules and owner != "test":
            jobs["test"] = {
                "decision": "delegated",
                "selectors": [],
                "owner": owner,
                "reason": "The contract job owns the affected cases.",
            }
        rules.append(rule["id"])
        unallocated.clear()
        break
    if unallocated:
        raise ValueError(f"No affected rule admits these paths: {sorted(unallocated)}")
    selected = [node for job in jobs.values() for node in job["selectors"]]
    if len(selected) != len(set(selected)):
        raise ValueError("The affected allocation contains duplicate cases.")
    return route, sorted(rules), jobs


def build_scope(
    *,
    event: dict[str, str],
    base: str,
    head: str,
    merge_base: str,
    changes: list[dict[str, Any]],
    patch_id: str,
    policy_sha256: str,
    promotion: bool = False,
    bootstrap: Any = None,
) -> dict[str, Any]:
    """Build one scope from the complete, validated producer comparison."""
    _mapping(event, {"name", "run_id", "run_attempt"}, "event")
    if not isinstance(event["name"], str) or event["name"] not in {"pull_request", "push"}:
        raise ValueError("The event name is not supported.")
    if not isinstance(promotion, bool):
        raise ValueError("The promotion selection must be a Boolean value.")
    if any(
        not isinstance(event[field], str) or re.fullmatch(r"[0-9]+", event[field]) is None
        for field in ("run_id", "run_attempt")
    ):
        raise ValueError("The current event identity is invalid.")
    for label, value in (("base", base), ("head", head), ("merge base", merge_base)):
        _identity(value, OBJECT_ID, label)
    _identity(policy_sha256, SHA256, "policy")
    _identity(patch_id, OBJECT_ID, "patch")
    if event["name"] == "push" and merge_base != base:
        raise ValueError("A push comparison must start at its base commit.")
    measured = _changes(changes)
    route, rules, jobs = _allocation(
        measured, patch_id=patch_id, promotion=promotion, bootstrap=bootstrap
    )
    result = {
        "version": 1,
        "scope": SCOPE,
        "route": route,
        "event": dict(event),
        "base": base,
        "head": head,
        "merge_base": merge_base,
        "changes": measured,
        "changes_sha256": hashlib.sha256(_canonical(measured)).hexdigest(),
        "patch_id": patch_id,
        "policy_sha256": policy_sha256,
        "rules": rules,
        "jobs": jobs,
    }
    if len(_canonical(result)) > MAX_SCOPE_BYTES:
        raise ValueError("The generated scope is too large.")
    return result


def validate_scope(
    scope: Any,
    *,
    event: dict[str, str],
    base: str,
    head: str,
    checkout_head: str,
    policy_sha256: str,
) -> dict[str, Any]:
    """Validate same-run scope data without reading Git or the filesystem."""
    _mapping(scope, SCOPE_FIELDS, "scope")
    if (
        type(scope["version"]) is not int
        or scope["version"] != 1
        or scope["scope"] != SCOPE
        or not isinstance(scope["route"], str)
        or scope["route"] not in {"legacy", "affected", "promotion"}
    ):
        raise ValueError("The scope version, name, or route is invalid.")
    if (
        scope["event"] != event
        or scope["base"] != base
        or scope["head"] != head
        or checkout_head != head
        or scope["policy_sha256"] != policy_sha256
    ):
        raise ValueError("The scope and current source identities differ.")
    measured = _changes(scope["changes"])
    bootstrap = None
    if scope["route"] == "affected" and any(row["path"] == DECLARATION_PATH for row in measured):
        # The same-run producer has examined the declaration against these objects.
        bootstrap = {
            "version": 1,
            "rule": BOOTSTRAP_RULE,
            "files": _bootstrap_rows(measured),
        }
    expected = build_scope(
        event=event,
        base=base,
        head=head,
        merge_base=scope["merge_base"],
        changes=measured,
        patch_id=scope["patch_id"],
        policy_sha256=policy_sha256,
        promotion=scope["route"] == "promotion",
        bootstrap=bootstrap,
    )
    if scope != expected:
        raise ValueError("The scope differs from its fixed rule and case allocation.")
    return scope

"""Check the closed controller build profile without granting execution."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast

Json = dict[str, Any]
MAX_INTEGER = 2**63 - 1


def encoded(value: Any) -> bytes:
    """Match the controller's compact sorted UTF-8 JSON, without a newline."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def digest(value: Any) -> str:
    """Hash actual canonical controller bytes."""
    return hashlib.sha256(encoded(value)).hexdigest()


def equal(left: Any, right: Any) -> bool:
    """Compare JSON identity without Python's bool/float integer coercion."""
    return encoded(left) == encoded(right)


def fields(value: Any, names: str) -> Json:
    """Require exactly the declared object fields."""
    if not isinstance(value, dict) or set(value) != set(names.split()):
        raise ValueError("build object has missing or unknown fields")
    return value


def identifier(value: Any) -> str:
    """Accept bounded opaque controller identifiers, never paths."""
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value) is None
    ):
        raise ValueError("invalid build identifier")
    return value


def integer(value: Any, *, minimum: int = 1) -> int:
    """Reject boolean, fractional, negative and oversized counters."""
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise ValueError("invalid build integer")
    return value


def sha(value: Any, length: int = 64) -> str:
    """Require a lowercase full digest."""
    if not isinstance(value, str) or re.fullmatch(f"[0-9a-f]{{{length}}}", value) is None:
        raise ValueError("invalid build digest")
    return value


def text(value: Any) -> str:
    """Validate retained public identity text without using it as a path."""
    if not isinstance(value, str) or not 1 <= len(value.encode()) <= 1024 or "\0" in value:
        raise ValueError("invalid build identity text")
    return value


def validate_policy(value: Any) -> Json:
    """Bind one operator policy to the first fixed, provider-free recipe."""
    policy = fields(value, "workspace recipe allocation")
    workspace = fields(policy["workspace"], "id repository parentWorkspace snapshotPolicyDigest")
    recipe = fields(
        policy["recipe"],
        "id repository argv parameters recipeDigest lockDigest platform "
        "imageDigest toolchainDigest resources",
    )
    allocation = fields(
        policy["allocation"],
        "id workerId generation authorityId platform imageDigest toolchainDigest "
        "qualificationReceiptDigest resources supervision",
    )
    for item in (workspace, recipe, allocation):
        identifier(item["id"])
    for name in ("workerId", "authorityId"):
        identifier(allocation[name])
    integer(allocation["generation"])
    for name in ("repository", "parentWorkspace"):
        text(workspace[name])
    sha(workspace["snapshotPolicyDigest"])
    sha(allocation["qualificationReceiptDigest"])
    for name in ("recipeDigest", "lockDigest"):
        sha(recipe[name])
    if (
        recipe["id"] != "hephaestus-test-unit-v1"
        or recipe["argv"] != ["just", "test-unit"]
        or recipe["parameters"] != {}
        or recipe["repository"] != workspace["repository"]
    ):
        raise ValueError("unsupported fixed build recipe")
    for item in (recipe, allocation):
        sha(item["toolchainDigest"])
        if (
            item["platform"] != "linux/aarch64"
            or not isinstance(item["imageDigest"], str)
            or not item["imageDigest"].startswith("sha256:")
        ):
            raise ValueError("build requires an immutable Linux image")
        sha(item["imageDigest"][7:])
    if any(
        recipe[name] != allocation[name] for name in ("platform", "imageDigest", "toolchainDigest")
    ):
        raise ValueError("build allocation differs from recipe")
    _validate_resources(recipe, allocation)
    return cast(Json, json.loads(encoded(policy)))


def _validate_resources(recipe: Json, allocation: Json) -> None:
    requested = fields(
        recipe["resources"],
        "cpus gpus memoryBytes diskBytes wallSeconds outputBytes artifactBytes "
        "snapshotBytes snapshotMembers",
    )
    available = fields(allocation["resources"], "cpus gpus memoryBytes diskBytes")
    reserve = fields(allocation["supervision"], "cpus memoryBytes")
    for resource in (requested, available, reserve):
        for name, quantity in resource.items():
            integer(quantity, minimum=0 if name == "gpus" else 1)
    if requested["gpus"] != 0 or available["gpus"] != 0:
        raise ValueError("first build profile has no GPU allocation")
    for name in ("cpus", "memoryBytes"):
        if requested[name] + reserve[name] > available[name]:
            raise ValueError("build exceeds allocation after supervision reserve")
    if (
        requested["diskBytes"] > available["diskBytes"]
        or requested["outputBytes"] > 64 * 1024 * 1024
    ):
        raise ValueError("build exceeds bounded capture or allocation")


def validate_command(value: Any, policy: Json) -> Json:
    """Accept an immutable start or its exact controller cancellation envelope."""
    command = fields(
        value,
        "schema targetKind targetId workerId generation operation commandId idempotencyKey payload",
    )
    if len(encoded(command)) > 32768:
        raise ValueError("build command exceeds its bound")
    for name in ("targetId", "workerId", "commandId", "idempotencyKey"):
        identifier(command[name])
    integer(command["generation"])
    if command["operation"] == "cancel":
        payload = fields(
            command["payload"],
            "schema attempt parent policy policyDigest parametersDigest snapshot "
            "snapshotWorkspace requiresRunGrant stopStartCommandId",
        )
        if (
            payload["stopStartCommandId"] != command["targetId"] + "-start"
            or command["commandId"] == payload["stopStartCommandId"]
        ):
            raise ValueError("build cancellation changed its start identity")
        validate_command(start_command(command), policy)
        return cast(Json, json.loads(encoded(command)))
    allocation = policy["allocation"]
    if (
        command["schema"] != "hi/fleet/v1"
        or command["targetKind"] != "build-jobs"
        or command["operation"] != "start"
        or command["workerId"] != allocation["workerId"]
        or command["generation"] != allocation["generation"]
        or command["commandId"] != command["targetId"] + "-start"
        or command["idempotencyKey"] != command["commandId"]
    ):
        raise ValueError("build command is not owned by this allocation")
    payload = fields(
        command["payload"],
        "schema attempt parent policy policyDigest parametersDigest snapshot "
        "snapshotWorkspace requiresRunGrant",
    )
    attempt = integer(payload["attempt"])
    if (
        payload["schema"] != "hi/fleet/build-command/v1"
        or payload["requiresRunGrant"] is not True
        or not equal(payload["policy"], policy)
        or payload["policyDigest"] != digest(policy)
        or payload["parametersDigest"] != digest({})
        or payload["snapshotWorkspace"] != f"{command['targetId']}-attempt-{attempt}"
    ):
        raise ValueError("build command policy or attempt changed")
    snapshot = fields(
        payload["snapshot"], "reference manifestDigest baseCommit members bytes policyDigest"
    )
    identifier(snapshot["reference"])
    sha(snapshot["manifestDigest"])
    sha(snapshot["baseCommit"], 40)
    limits = policy["recipe"]["resources"]
    if (
        integer(snapshot["members"]) > limits["snapshotMembers"]
        or integer(snapshot["bytes"]) > limits["snapshotBytes"]
        or snapshot["policyDigest"] != policy["workspace"]["snapshotPolicyDigest"]
    ):
        raise ValueError("build snapshot policy or limits changed")
    parent = fields(
        payload["parent"],
        "targetKind targetId sessionId executionId generation taskId agentId claim",
    )
    for name in ("targetId", "sessionId", "executionId", "taskId", "agentId"):
        text(parent[name])
    integer(parent["generation"])
    claim = fields(
        parent["claim"], "schema targetKind targetId workerId agentId generation workspace"
    )
    integer(claim["generation"])
    text(claim["workerId"])
    if (
        parent["targetKind"] not in {"sessions", "executions"}
        or claim["schema"] != "hi/fleet/claim/v1"
        or any(
            not equal(claim[name], parent[name])
            for name in ("targetKind", "targetId", "agentId", "generation")
        )
        or claim["workspace"] != policy["workspace"]["parentWorkspace"]
        or claim["workerId"] == allocation["workerId"]
    ):
        raise ValueError("build parent claim is inconsistent")
    target = parent["sessionId"] if parent["targetKind"] == "sessions" else parent["executionId"]
    if parent["targetId"] != target:
        raise ValueError("build parent target is inconsistent")
    return cast(Json, json.loads(encoded(command)))


def start_command(command: Json) -> Json:
    """Recover the immutable start identity from an already checked envelope."""
    result = cast(Json, json.loads(encoded(command)))
    if result["operation"] == "cancel":
        command_id = result["payload"].pop("stopStartCommandId")
        result.update(operation="start", commandId=command_id, idempotencyKey=command_id)
    return result


def validate_grant(response: Any, command: Json, claim: Json) -> Json:
    """Require the exact returned command, typed claim and controller grant digest."""
    fields(response, "command grant")
    grant = fields(response["grant"], "schema claim grantId authorizedAt")
    if (
        not equal(response["command"], command)
        or not equal(grant["claim"], claim)
        or grant["schema"] != "hi/fleet/build-grant/v1"
        or grant["grantId"] != digest({"buildId": command["targetId"], "claim": claim})
    ):
        raise ValueError("run grant does not match the persisted claim")
    text(grant["authorizedAt"])
    return cast(Json, json.loads(encoded(grant)))


def _validate_retained_claim(claim: Any, command: Json, policy: Json) -> Json:
    fields(claim, "schema workerId allocationId generation attempt commandId claimId")
    expected = {
        "schema": "hi/fleet/build-claim/v1",
        "workerId": policy["allocation"]["workerId"],
        "allocationId": policy["allocation"]["id"],
        "generation": policy["allocation"]["generation"],
        "attempt": command["payload"]["attempt"],
        "commandId": command["commandId"],
        "claimId": identifier(claim["claimId"]),
    }
    if not equal(claim, expected):
        raise ValueError("retained build claim identity changed")
    return expected


def _validate_retained_lease(lease: Any, command: Json, policy: Json, root: Path) -> None:
    fields(
        lease,
        "schema leaseId buildId attempt commandId workerId allocationId generation "
        "policyDigest snapshotDigest workspace policy",
    )
    allocation, payload = policy["allocation"], command["payload"]
    expected = {
        "schema": "hi/hephaestus/build-lease/v1",
        "leaseId": sha(lease["leaseId"], 32),
        "buildId": command["targetId"],
        "attempt": payload["attempt"],
        "commandId": command["commandId"],
        "workerId": allocation["workerId"],
        "allocationId": allocation["id"],
        "generation": allocation["generation"],
        "policyDigest": payload["policyDigest"],
        "snapshotDigest": payload["snapshot"]["manifestDigest"],
        "workspace": str(root / payload["snapshotWorkspace"]),
        "policy": policy,
    }
    if not equal(lease, expected):
        raise ValueError("retained build lease identity changed")


def _retained_result_references(fact: Json, lease: Json | None) -> tuple[Json | None, Json | None]:
    receipt, artifacts = fact["receipt"], fact["artifacts"]
    if (receipt is None) != (artifacts is None):
        raise ValueError("retained build result references are incomplete")
    if receipt is not None:
        if lease is None:
            raise ValueError("retained result reference has no execution lease")
        for reference in (receipt, artifacts):
            fields(reference, "reference digest")
            sha(reference["digest"])
            if reference["reference"] != "result-" + lease["leaseId"]:
                raise ValueError("retained result reference changed its owner")
        if artifacts["digest"] != digest([]):
            raise ValueError("retained result has undeclared artifact membership")
    return receipt, artifacts


def _validate_retained_terminal(state: Json, policy: Json) -> None:
    fact = fields(
        state["terminal"],
        "schema eventId workerId allocationId generation attempt commandId policyDigest "
        "parametersDigest snapshotDigest platform imageDigest toolchainDigest outcome "
        "exitCode cleanup startFenced receipt logs artifacts",
    )
    command = state["cancel"] or state["command"]
    allocation, payload = policy["allocation"], command["payload"]
    outcome, exit_code = fact["outcome"], fact["exitCode"]
    if outcome in ("completed", "failed"):
        if (
            type(exit_code) is not int
            or not -255 <= exit_code <= 255
            or (outcome == "completed") != (exit_code == 0)
        ):
            raise ValueError("retained build exit observation is invalid")
    elif outcome not in ("cancelled", "timed_out") or exit_code is not None:
        raise ValueError("retained build outcome is invalid")
    if (outcome == "cancelled") != (state["cancel"] is not None):
        raise ValueError("retained build outcome changed its cancellation identity")
    if outcome != "cancelled" and (state["grant"] is None or state["lease"] is None):
        raise ValueError("retained build terminal has no run ownership")
    logs = fact["logs"]
    if logs is not None:
        fields(logs, "reference digest")
        sha(logs["digest"])
        if state["lease"] is None or logs["reference"] != "output-" + state["lease"]["leaseId"]:
            raise ValueError("retained build logs changed their owner")
    elif outcome != "cancelled":
        raise ValueError("retained build terminal has no private output reference")
    receipt, artifacts = _retained_result_references(fact, state["lease"])
    expected = {
        "schema": "hi/fleet/build-fact/v1",
        "eventId": "build-"
        + digest({"claim": state["claim"], "commandId": command["commandId"], "outcome": outcome}),
        "workerId": allocation["workerId"],
        "allocationId": allocation["id"],
        "generation": allocation["generation"],
        "attempt": payload["attempt"],
        "commandId": command["commandId"],
        "policyDigest": payload["policyDigest"],
        "parametersDigest": payload["parametersDigest"],
        "snapshotDigest": payload["snapshot"]["manifestDigest"],
        "platform": allocation["platform"],
        "imageDigest": allocation["imageDigest"],
        "toolchainDigest": allocation["toolchainDigest"],
        "outcome": outcome,
        "exitCode": exit_code,
        "cleanup": "confirmed_empty",
        "startFenced": True,
        "receipt": receipt,
        "logs": logs,
        "artifacts": artifacts,
    }
    if not equal(fact, expected) or state["cleanup"] != "confirmed_empty":
        raise ValueError("retained build terminal identity changed")


def validate_state(value: Any, policy: Json, workspace_root: Path) -> Json:
    """Reject damaged typed ownership before a recovered owner can make effects."""
    names = "schema command claim grant lease terminal published cancel cleanup phase reason"
    schema = value.get("schema") if isinstance(value, dict) else None
    if schema == "hi/hephaestus/build-attempt/v2":
        names += " supersededTerminal"
    state = fields(value, names)
    if (
        schema not in ("hi/hephaestus/build-attempt/v1", "hi/hephaestus/build-attempt/v2")
        or type(state["published"]) is not bool
        or state["phase"] not in ("grant_pending", "granted", "preparing", "starting", "terminal")
        or state["cleanup"] not in (None, "confirmed_empty")
        or state["reason"]
        not in (
            "",
            "owned_execution_requires_reconciliation",
            "build_effect_requires_reconciliation",
            "terminal_cancellation_requires_controller_reconciliation",
            "owned_exit_requires_reconciliation",
            "owned_cleanup_requires_reconciliation",
        )
    ):
        raise ValueError("retained build state is invalid")
    command = validate_command(state["command"], policy)
    if command["operation"] != "start":
        raise ValueError("retained build command is not its canonical start")
    claim = _validate_retained_claim(state["claim"], command, policy)
    if state["grant"] is not None:
        validate_grant({"command": command, "grant": state["grant"]}, command, claim)
    if state["cancel"] is not None:
        cancel = validate_command(state["cancel"], policy)
        if cancel["operation"] != "cancel" or not equal(start_command(cancel), command):
            raise ValueError("retained build cancellation changed identity")
    if state["lease"] is not None:
        _validate_retained_lease(state["lease"], command, policy, workspace_root)
        if state["grant"] is None:
            raise ValueError("retained build lease has no grant")
    _validate_state_phase(state)
    if state["terminal"] is not None:
        _validate_retained_terminal(state, policy)
    if schema == "hi/hephaestus/build-attempt/v2":
        _validate_superseded_terminal(state, policy)
    return cast(Json, json.loads(encoded(state)))


def _validate_superseded_terminal(state: Json, policy: Json) -> None:
    if state["cancel"] is None or state["terminal"] is None:
        raise ValueError("retained terminal history has no accepted stop")
    if any(state["terminal"][name] is not None for name in ("receipt", "logs", "artifacts")):
        raise ValueError("cancellation cannot reuse superseded execution evidence")
    _validate_retained_terminal(
        {**state, "cancel": None, "terminal": state["supersededTerminal"]}, policy
    )


def validate_history_transition(
    previous: Any, current: Any, policy: Json, workspace_root: Path
) -> None:
    """Bind version 2 history to the exact preceding terminal and accepted stop."""
    version = "hi/hephaestus/build-attempt/v2"
    if not any(
        isinstance(state, dict) and state.get("schema") == version for state in (previous, current)
    ):
        return
    before = validate_state(previous, policy, workspace_root)
    after = validate_state(current, policy, workspace_root)
    if after["schema"] != version:
        raise ValueError("retained terminal history cannot downgrade its schema")
    immutable = ("command", "claim", "grant", "lease", "cleanup")
    if any(not equal(before[name], after[name]) for name in immutable):
        raise ValueError("retained terminal history changed its execution ownership")
    if before["schema"] == version:
        if any(
            not equal(before[name], after[name])
            for name in ("cancel", "terminal", "supersededTerminal")
        ) or (before["published"] and not after["published"]):
            raise ValueError("retained terminal history or accepted stop changed")
    elif (
        before["cancel"] is not None
        or before["published"]
        or not equal(before["terminal"], after["supersededTerminal"])
    ):
        raise ValueError("new terminal history differs from its unpublished predecessor")


def _validate_state_phase(state: Json) -> None:
    if (
        (state["phase"] == "terminal") != (state["terminal"] is not None)
        or (state["published"] and state["terminal"] is None)
        or (state["phase"] == "grant_pending" and state["grant"] is not None)
        or (state["phase"] in ("granted", "preparing", "starting") and state["grant"] is None)
        or (state["phase"] in ("preparing", "starting") and state["lease"] is None)
        or (state["phase"] in ("grant_pending", "granted") and state["lease"] is not None)
        or (
            state["phase"] in ("grant_pending", "granted")
            and state["cleanup"] == "confirmed_empty"
            and state["cancel"] is None
        )
    ):
        raise ValueError("retained build phase has inconsistent ownership")

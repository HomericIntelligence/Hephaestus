#!/usr/bin/env python3
"""Collect and render live Pi end-to-end evidence for GitHub issue #2519.

The collector records a private run manifest beneath ``build/pi-e2e-2519`` and
renders a reproducible report/runbook pair from that evidence. It keeps
operator-local aliases and prompts out of the committed artifacts by storing
prompt digests, raw provider-output digests, and private proxy logs only in the
owner-only run directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from hephaestus.agents.pi_plugins import inspect_pi_package_inventory, load_pi_package_catalog
from hephaestus.agents.runtime import pi_private_redaction_tokens, redact_pi_private_values
from hephaestus.automation.athena_contract import load_athena_contract_receipt
from hephaestus.automation.mnemosyne_binding import default_mnemosyne_root
from hephaestus.automation.mnemosyne_delivery import valid_delivery_receipt
from hephaestus.automation.pipeline_github import PipelineGitHub
from hephaestus.io.utils import write_secure
from hephaestus.utils.helpers import slugify

ISSUE_NUMBER = 2519
PROJECT_REPOSITORY = "HomericIntelligence/Hephaestus"
FIXTURE_TITLE = "fix(utils): reject negative byte sizes"
FIXTURE_SUMMARY = (
    "Exercise `hephaestus/utils/helpers.py` `human_readable_size` and "
    "`tests/unit/utils/test_general_utils.py` `TestHumanReadableSize` with a "
    "deterministic negative-size rejection."
)
RUN_ROOT_NAME = "pi-e2e-2519"
DEFAULT_REPORT_PATH = Path("docs/pi-e2e-2519-report.md")
DEFAULT_RUNBOOK_PATH = Path("docs/runbooks/pi-e2e-2519.md")
MANIFEST_NAME = "run.json"
PROXY_LOG_NAME = "provider-proxy.jsonl"
COMMANDS_DIR_NAME = "commands"
DEFECTS_DIR_NAME = "defects"
ARTIFACTS_DIR_NAME = "artifacts"
ATHENA_RECEIPTS_DIR_NAME = "athena-host-receipts"
PROXY_TOOL_NAMES = ("pi", "codex")
REQUIRED_SKILL_COMMANDS = ("skill:advise", "skill:learn", "skill:pr-review")
PIPELINE_CAPTURE_COMMANDS = {
    "discovery-plan": "hephaestus-plan-issues",
    "implementation-review-handoff": "hephaestus-automation-loop",
}
REQUIRED_E2E_STAGES = tuple(PIPELINE_CAPTURE_COMMANDS)
SKILL_COMMAND_RE = re.compile(r"(?:\$athena:|/athena:|skill:)[A-Za-z0-9._:/-]+")
FULL_COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
FOLLOW_UP_PARENT_RE = re.compile(rf"^Parent: #{ISSUE_NUMBER}[ \t]*$", re.MULTILINE)
DEFAULT_CAPTURE_TIMEOUT_SECONDS = 600
DEFAULT_INVENTORY_TIMEOUT_SECONDS = 30
PI_PROVIDER_NAME = "pi"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _run_root(repo_root: Path) -> Path:
    return repo_root / "build" / RUN_ROOT_NAME


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _ensure_owner_only_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _manifest_path(run_dir: Path) -> Path:
    return run_dir / MANIFEST_NAME


def _load_manifest(run_dir: Path) -> dict[str, Any]:
    manifest_path = _manifest_path(run_dir)
    try:
        return cast(dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8")))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"run manifest missing: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"run manifest is not valid JSON: {manifest_path}") from exc


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    write_secure(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _save_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    _write_json(_manifest_path(run_dir), manifest)


def _default_manifest(run_id: str, repo_root: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "issue_number": ISSUE_NUMBER,
        "fixture": {
            "title": FIXTURE_TITLE,
            "summary": FIXTURE_SUMMARY,
        },
        "run_id": run_id,
        "created_at": _utc_now(),
        "repo_root": str(repo_root),
        "pi": {},
        "inventory": {},
        "snapshots": [],
        "commands": [],
        "defects": [],
        "athena_host_receipts": [],
        "comparisons": [],
        "artifacts": {},
        "publication": {},
    }


def _resolve_run_dir(run_root: Path, run_id: str) -> Path:
    _validate_run_id(run_id)
    run_dir = run_root / run_id
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise FileNotFoundError(f"run directory does not exist: {run_dir}")
    return run_dir


def _validate_run_id(run_id: str) -> None:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id) is None:
        raise ValueError("run id must be one safe path component")


def _new_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{slugify(_random_token())}"


def _random_token() -> str:
    """Return a short random token for run identifiers and defect slugs."""
    return os.urandom(4).hex()


def _prepare_run_dir(run_root: Path, run_id: str, repo_root: Path) -> Path:
    _validate_run_id(run_id)
    _ensure_owner_only_dir(run_root)
    run_dir = run_root / run_id
    _ensure_owner_only_dir(run_dir)
    for subdir in (
        COMMANDS_DIR_NAME,
        DEFECTS_DIR_NAME,
        ARTIFACTS_DIR_NAME,
        ATHENA_RECEIPTS_DIR_NAME,
    ):
        _ensure_owner_only_dir(run_dir / subdir)
    if not _manifest_path(run_dir).exists():
        _save_manifest(run_dir, _default_manifest(run_id, repo_root))
    return run_dir


def _append_command_entry(run_dir: Path, entry: dict[str, Any]) -> dict[str, Any]:
    manifest = _load_manifest(run_dir)
    manifest["commands"].append(entry)
    _save_manifest(run_dir, manifest)
    return manifest


def _append_snapshot_entry(run_dir: Path, entry: dict[str, Any]) -> dict[str, Any]:
    manifest = _load_manifest(run_dir)
    manifest["snapshots"].append(entry)
    _save_manifest(run_dir, manifest)
    return manifest


def _append_defect_entry(run_dir: Path, entry: dict[str, Any]) -> dict[str, Any]:
    manifest = _load_manifest(run_dir)
    manifest["defects"].append(entry)
    _save_manifest(run_dir, manifest)
    return manifest


def _latest_snapshot_head(manifest: dict[str, Any]) -> str:
    for snapshot in reversed(manifest.get("snapshots", [])):
        if isinstance(snapshot, dict) and isinstance(snapshot.get("head"), str):
            return cast(str, snapshot["head"])
    return ""


def _comparison_payload(pi_entry: dict[str, Any], control_entry: dict[str, Any]) -> dict[str, Any]:
    """Return one paired, manifest-verifiable provider comparison."""
    keys = ("stage", "prompt_sha256", "revision")
    if any(not pi_entry.get(key) or pi_entry.get(key) != control_entry.get(key) for key in keys):
        raise ValueError("comparison requires the same stage, prompt, and revision")

    def outcome(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": entry.get("status"),
            "returncode": entry.get("returncode"),
            "timed_out": entry.get("timed_out"),
            "stdout_digest": entry.get("stdout_digest"),
            "stderr_digest": entry.get("stderr_digest"),
        }

    pi_outcome = outcome(pi_entry)
    control_outcome = outcome(control_entry)
    return {
        "pi_entry_id": pi_entry.get("id"),
        "control_entry_id": control_entry.get("id"),
        "stage": pi_entry["stage"],
        "prompt_sha256": pi_entry["prompt_sha256"],
        "revision": pi_entry["revision"],
        "pi_outcome": pi_outcome,
        "control_outcome": control_outcome,
        "outcomes_match": (
            pi_outcome["status"] == control_outcome["status"]
            and pi_outcome["returncode"] == control_outcome["returncode"]
            and pi_outcome["timed_out"] == control_outcome["timed_out"]
        ),
        "stdout_matches": pi_outcome["stdout_digest"] == control_outcome["stdout_digest"],
        "stderr_matches": pi_outcome["stderr_digest"] == control_outcome["stderr_digest"],
    }


def _refresh_comparisons(run_dir: Path) -> None:
    """Persist every newly available Pi/control pair exactly once."""
    manifest = _load_manifest(run_dir)
    captures = [entry for entry in manifest["commands"] if entry.get("kind") == "capture"]
    comparisons: list[dict[str, Any]] = []
    for pi_entry in captures:
        if pi_entry.get("provider") != PI_PROVIDER_NAME:
            continue
        for control_entry in captures:
            if control_entry.get("provider") in {None, PI_PROVIDER_NAME}:
                continue
            try:
                comparisons.append(_comparison_payload(pi_entry, control_entry))
            except ValueError:
                continue
    manifest["comparisons"] = comparisons
    _save_manifest(run_dir, manifest)


def _update_manifest(run_dir: Path, patch: dict[str, Any]) -> dict[str, Any]:
    manifest = _load_manifest(run_dir)
    manifest.update(patch)
    _save_manifest(run_dir, manifest)
    return manifest


def _prompt_digest(prompt: str) -> str:
    return _sha256_text(prompt)


def _load_prompt(args: argparse.Namespace) -> str:
    if getattr(args, "prompt", None):
        return cast(str, args.prompt)
    prompt_file = getattr(args, "prompt_file", None)
    if prompt_file is None:
        return ""
    return Path(prompt_file).read_text(encoding="utf-8")


def _normalize_command_argv(command_argv: Sequence[str]) -> list[str]:
    argv = list(command_argv)
    if argv and argv[0] == "--":
        return argv[1:]
    return argv


def _validate_pi_pipeline_command(stage: str, command_argv: Sequence[str]) -> None:
    """Require a Pi capture to observe one exact normal pipeline entry point."""
    expected_command = PIPELINE_CAPTURE_COMMANDS.get(stage)
    if expected_command is None:
        raise ValueError(f"unsupported Pi pipeline capture stage: {stage!r}")
    argv = list(command_argv)
    command_index = 2 if argv[:2] == ["uv", "run"] else 0
    if len(argv) <= command_index or argv[command_index] != expected_command:
        raise ValueError(
            f"Pi {stage} evidence must run {expected_command!r} through the normal pipeline"
        )
    try:
        agent_index = argv.index("--agent", command_index + 1)
        selected_agent = argv[agent_index + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError(
            "Pi pipeline evidence requires the literal arguments '--agent pi'"
        ) from exc
    if selected_agent != PI_PROVIDER_NAME:
        raise ValueError("Pi pipeline evidence requires the literal arguments '--agent pi'")
    try:
        issue_index = argv.index("--issues", command_index + 1)
        selected_issue = argv[issue_index + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError(f"Pi pipeline evidence must target issue #{ISSUE_NUMBER}") from exc
    if selected_issue != str(ISSUE_NUMBER):
        raise ValueError(f"Pi pipeline evidence must target issue #{ISSUE_NUMBER}")


def _positive_timeout(value: str) -> int:
    """Parse a capture timeout that guarantees bounded provider execution."""
    try:
        timeout = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a positive integer") from exc
    if timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be a positive integer")
    return timeout


def _timeout_output(value: str | bytes | None) -> str:
    """Normalize subprocess timeout output for the text artifacts."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _jsonl_objects(text: str) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(cast(dict[str, Any], value))
    return objects


def _session_ids_from_events(events: Iterable[dict[str, Any]]) -> tuple[str, ...]:
    session_ids: list[str] = []
    for event in events:
        if event.get("type") == "session" and isinstance(event.get("id"), str):
            session_ids.append(event["id"])
        if event.get("type") == "session_meta":
            payload = event.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("id"), str):
                session_ids.append(payload["id"])
        if event.get("type") == "response":
            payload = event.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("id"), str):
                session_ids.append(payload["id"])
    return tuple(dict.fromkeys(session_ids))


def _skill_calls_from_events(events: Iterable[dict[str, Any]]) -> tuple[str, ...]:
    matches: list[str] = []
    for event in events:
        rendered = json.dumps(event, sort_keys=True, ensure_ascii=False)
        for match in SKILL_COMMAND_RE.findall(rendered):
            matches.append(match)
    return tuple(dict.fromkeys(matches))


def _proxy_tool_scopes_from_argv(argv: Sequence[str]) -> tuple[str, ...]:
    scopes: list[str] = []
    for index, token in enumerate(argv):
        if token in {"--tools", "--allowedTools"}:
            if index + 1 < len(argv):
                scopes.extend(part for part in argv[index + 1].split(",") if part)
            continue
        for flag in ("--tools=", "--allowedTools="):
            if token.startswith(flag):
                scopes.extend(part for part in token.split("=", 1)[1].split(",") if part)
    return tuple(dict.fromkeys(scopes))


def _requested_skill_grants_from_argv(argv: Sequence[str]) -> tuple[str, ...]:
    grants: list[str] = []
    for index, argument in enumerate(argv):
        values: Iterable[str] = ()
        if argument == "--commands" and index + 1 < len(argv):
            values = argv[index + 1].split(",")
        elif argument.startswith("--commands="):
            values = argument.split("=", 1)[1].split(",")
        grants.extend(value for value in values if value.startswith("skill:"))
    return tuple(dict.fromkeys(grants))


def _proxy_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return _jsonl_objects(path.read_text(encoding="utf-8"))


def _proxy_invocations(events: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    invocations: list[dict[str, Any]] = []
    for event in events:
        if event.get("event") != "proxy-invocation":
            continue
        tool = event.get("tool")
        argv = event.get("argv")
        real_binary = event.get("real_binary")
        if (
            not isinstance(tool, str)
            or not isinstance(argv, list)
            or not all(isinstance(item, str) for item in argv)
        ):
            continue
        record: dict[str, Any] = {
            "tool": tool,
            "argv": list(argv),
            "real_binary": real_binary if isinstance(real_binary, str) else "",
            "cwd": event.get("cwd") if isinstance(event.get("cwd"), str) else "",
            "timestamp": event.get("timestamp") if isinstance(event.get("timestamp"), str) else "",
        }
        invocations.append(record)
    return tuple(invocations)


def _capture_analysis(
    stdout: str,
    stderr: str,
    proxy_log: Path,
    pipeline_receipts: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    stdout_events = _jsonl_objects(stdout)
    stderr_events = _jsonl_objects(stderr)
    all_events = stdout_events + stderr_events
    proxy_events = _proxy_events(proxy_log)
    proxy_invocations = _proxy_invocations(proxy_events)
    pi_agent_receipts = [
        {
            "claim_stage": receipt.get("claim_stage", ""),
            "ok": receipt.get("ok") is True,
            "session_id": receipt.get("session_id", ""),
            "tool_scopes": receipt.get("tool_scopes", []),
            "execution_request": receipt.get("execution_request"),
        }
        for receipt in pipeline_receipts
        if receipt.get("job_type") == "agent" and receipt.get("provider") == PI_PROVIDER_NAME
    ]
    session_ids = tuple(
        dict.fromkeys(
            [
                *_session_ids_from_events(all_events),
                *(
                    receipt["session_id"]
                    for receipt in pipeline_receipts
                    if receipt.get("job_type") == "agent"
                    and receipt.get("provider") == PI_PROVIDER_NAME
                    and receipt.get("ok") is True
                    and isinstance(receipt.get("session_id"), str)
                    and receipt["session_id"]
                ),
            ]
        )
    )
    provider_skill_mentions = _skill_calls_from_events(all_events)
    requested_skill_grants = tuple(
        dict.fromkeys(
            grant
            for invocation in proxy_invocations
            for grant in _requested_skill_grants_from_argv(invocation["argv"])
        )
    )
    tool_scopes = tuple(
        dict.fromkeys(
            [
                *(
                    match
                    for invocation in proxy_invocations
                    for match in _proxy_tool_scopes_from_argv(invocation["argv"])
                ),
                *(
                    scope
                    for receipt in pipeline_receipts
                    if receipt.get("job_type") == "agent"
                    and receipt.get("provider") == PI_PROVIDER_NAME
                    and receipt.get("ok") is True
                    and isinstance(receipt.get("tool_scopes"), list)
                    for scope in receipt["tool_scopes"]
                    if isinstance(scope, str) and scope
                ),
            ]
        )
    )
    return {
        "session_ids": list(session_ids),
        "observed_skill_invocations": [],
        "provider_skill_mentions": list(provider_skill_mentions),
        "requested_skill_grants": list(requested_skill_grants),
        "tool_scopes": list(tool_scopes),
        "proxy_invocations": list(proxy_invocations),
        "pipeline_receipt_count": len(pipeline_receipts),
        "pi_agent_receipts": pi_agent_receipts,
        "stdout_digest": _sha256_text(stdout),
        "stderr_digest": _sha256_text(stderr),
        "stdout_event_count": len(stdout_events),
        "stderr_event_count": len(stderr_events),
    }


def _with_evidence_receipt_dir(command_argv: Sequence[str], receipt_dir: Path) -> list[str]:
    """Enable the normal pipeline's private typed receipt sink."""
    if "--evidence-receipt-dir" in command_argv:
        raise ValueError("capture owns --evidence-receipt-dir")
    return [*command_argv, "--evidence-receipt-dir", str(receipt_dir)]


def _load_pipeline_receipts(receipt_dir: Path) -> list[dict[str, Any]]:
    """Load bounded regular JSON receipts produced by the queue workers."""
    receipts: list[dict[str, Any]] = []
    for path in sorted(receipt_dir.glob("*.json")):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            raise ValueError("pipeline evidence receipt is not a bounded regular file")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("pipeline evidence receipt is not valid JSON") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("pipeline evidence receipt has an unsupported schema")
        receipts.append(cast(dict[str, Any], payload))
    return receipts


def _store_generated_athena_receipts(
    run_dir: Path, pipeline_receipts: Sequence[dict[str, Any]]
) -> None:
    """Bind successful host-owned results emitted by this queue invocation."""
    manifest = _load_manifest(run_dir)
    descriptors = cast(list[dict[str, Any]], manifest.get("athena_host_receipts", []))
    by_kind = {entry.get("kind"): entry for entry in descriptors}
    changed = False
    for receipt in pipeline_receipts:
        payload = receipt.get("result")
        if (
            receipt.get("job_type") != "athena"
            or receipt.get("ok") is not True
            or not isinstance(payload, dict)
        ):
            continue
        kind = payload.get("kind")
        if kind not in {"advise", "learn"}:
            continue
        _validate_athena_host_receipt(cast(dict[str, Any], payload), kind)
        artifact = Path(ATHENA_RECEIPTS_DIR_NAME) / f"{kind}.json"
        _write_json(run_dir / artifact, cast(dict[str, Any], payload))
        by_kind[kind] = {
            "kind": kind,
            "source": "pipeline",
            "artifact": artifact.as_posix(),
            "sha256": _sha256_bytes((run_dir / artifact).read_bytes()),
            "recorded_at": _utc_now(),
        }
        changed = True
    if changed:
        manifest["athena_host_receipts"] = [
            by_kind[kind] for kind in ("advise", "learn") if kind in by_kind
        ]
        _save_manifest(run_dir, manifest)


def _write_proxy_wrapper(path: Path, real_env_var: str) -> None:
    content = textwrap.dedent(
        f"""\
        #!/usr/bin/env python3
        from __future__ import annotations

        import json
        import os
        import pathlib
        import sys
        import time

        def main() -> int:
            tool = pathlib.Path(sys.argv[0]).name
            real = os.environ.get({real_env_var!r}, "")
            if not real:
                print(f"error: {{tool}} proxy has no resolved real binary", file=sys.stderr)
                return 127
            log_path = os.environ.get("HEPH_PI_E2E_PROXY_LOG", "")
            if log_path:
                event = {{
                    "event": "proxy-invocation",
                    "tool": tool,
                    "real_binary": real,
                    "argv": sys.argv[1:],
                    "cwd": os.getcwd(),
                    "timestamp": time.time(),
                }}
                with open(log_path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, sort_keys=True) + "\\n")
            os.execv(real, [real, *sys.argv[1:]])

        if __name__ == "__main__":
            raise SystemExit(main())
        """
    )
    write_secure(path, content)
    path.chmod(0o700)


def _prepare_provider_proxy_dir(run_dir: Path) -> Path:
    proxy_dir = _ensure_owner_only_dir(run_dir / "provider-proxy")
    _write_proxy_wrapper(proxy_dir / "pi", "HEPH_PI_E2E_REAL_PI")
    _write_proxy_wrapper(proxy_dir / "codex", "HEPH_PI_E2E_REAL_CODEX")
    return proxy_dir


def _provider_proxy_env(proxy_dir: Path, log_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{proxy_dir}{os.pathsep}{env.get('PATH', os.defpath)}"
    env["HEPH_PI_E2E_PROXY_LOG"] = str(log_path)
    env["HEPH_PI_E2E_REAL_PI"] = shutil.which("pi") or ""
    env["HEPH_PI_E2E_REAL_CODEX"] = shutil.which("codex") or ""
    return env


def _probe_command_version(binary: str) -> tuple[str, bool]:
    resolved = shutil.which(binary)
    if resolved is None:
        return "", False
    try:
        result = subprocess.run(
            [resolved, "--version"],
            cwd=_repo_root(),
            capture_output=True,
            text=True,
            check=False,
            timeout=DEFAULT_INVENTORY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return "", True
    output = (result.stdout or result.stderr or "").strip()
    return output, False


def _record_inventory(run_dir: Path) -> int:
    repo_root = Path(_load_manifest(run_dir)["repo_root"])
    catalog = load_pi_package_catalog()
    pi_inventory = inspect_pi_package_inventory(repo_root, catalog)
    version, version_probe_timed_out = _probe_command_version("pi")
    ready = pi_inventory.ready and not version_probe_timed_out
    status = "version_probe_timeout" if version_probe_timed_out else pi_inventory.status
    detail = (
        f"Pi --version timed out after {DEFAULT_INVENTORY_TIMEOUT_SECONDS} seconds"
        if version_probe_timed_out
        else pi_inventory.detail
    )
    manifest = _load_manifest(run_dir)
    manifest["pi"] = {
        "version": version,
        "binary": shutil.which("pi") or "",
        "skill_commands": list(catalog.required_commands),
        "package_inventory": {
            "ready": ready,
            "status": status,
            "detail": detail,
            "roots": {key: str(value) for key, value in pi_inventory.roots.items()},
            "scopes": pi_inventory.scopes,
        },
    }
    _save_manifest(run_dir, manifest)
    _write_json(run_dir / ARTIFACTS_DIR_NAME / "inventory.json", manifest["pi"])
    _append_command_entry(
        run_dir,
        {
            "id": f"inventory-{len(manifest['commands']) + 1:02d}",
            "kind": "inventory",
            "status": "success" if ready else "failure",
            "returncode": 0 if ready else 1,
            "provider": "pi",
            "session_ids": [],
            "requested_skill_grants": list(catalog.required_commands),
            "tool_scopes": [],
            "prompt_sha256": "",
            "stdout_digest": "",
            "stderr_digest": "",
            "started_at": _utc_now(),
            "finished_at": _utc_now(),
            "artifacts": {"inventory": str(Path(ARTIFACTS_DIR_NAME) / "inventory.json")},
        },
    )
    return 0 if ready else 1


def _record_snapshot(run_dir: Path, label: str) -> int:
    repo_root = Path(_load_manifest(run_dir)["repo_root"])

    def _git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        return (result.stdout or result.stderr or "").strip()

    snapshot = {
        "label": label,
        "branch": _git("branch", "--show-current"),
        "head": _git("rev-parse", "HEAD"),
        "status": _git("status", "--short", "--untracked-files=all"),
    }
    snapshot_path = run_dir / ARTIFACTS_DIR_NAME / f"{slugify(label) or 'snapshot'}.json"
    _write_json(snapshot_path, snapshot)
    _append_snapshot_entry(
        run_dir,
        {
            "label": label,
            "artifact": str(snapshot_path.relative_to(run_dir)),
            "created_at": _utc_now(),
            "head": snapshot["head"],
            "branch": snapshot["branch"],
            "status": snapshot["status"],
        },
    )
    return 0


def _record_command(
    run_dir: Path,
    *,
    provider: str,
    stage: str,
    command_argv: Sequence[str],
    prompt: str,
    prompt_file: Path | None,
    timeout_seconds: int,
) -> int:
    if provider == PI_PROVIDER_NAME:
        _validate_pi_pipeline_command(stage, command_argv)
    proxy_dir = _prepare_provider_proxy_dir(run_dir)
    manifest = _load_manifest(run_dir)
    command_index = len(manifest["commands"]) + 1
    record_dir = _ensure_owner_only_dir(
        run_dir / COMMANDS_DIR_NAME / f"{command_index:02d}-{slugify(stage) or 'stage'}"
    )
    proxy_log = record_dir / PROXY_LOG_NAME
    env = _provider_proxy_env(proxy_dir, proxy_log)
    stdout_path = record_dir / "stdout.txt"
    stderr_path = record_dir / "stderr.txt"
    analysis_path = record_dir / "analysis.json"
    pipeline_receipt_dir = _ensure_owner_only_dir(record_dir / "pipeline-receipts")
    executed_argv = list(command_argv)
    if provider == PI_PROVIDER_NAME:
        executed_argv = _with_evidence_receipt_dir(executed_argv, pipeline_receipt_dir)
    if prompt_file is not None:
        prompt_copy = record_dir / "prompt.txt"
        write_secure(prompt_copy, prompt)
        prompt_copy.chmod(0o600)
    start = _utc_now()
    try:
        completed = subprocess.run(
            executed_argv,
            cwd=Path(manifest["repo_root"]),
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
        returncode = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        timed_out = False
    except subprocess.CalledProcessError as exc:
        returncode = exc.returncode
        stdout = _timeout_output(exc.stdout)
        stderr = _timeout_output(exc.stderr)
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        stdout = _timeout_output(exc.stdout)
        stderr = _timeout_output(exc.stderr)
        stderr += f"error: command timed out after {timeout_seconds} seconds\n"
        timed_out = True
    write_secure(stdout_path, stdout)
    write_secure(stderr_path, stderr)
    pipeline_receipts = _load_pipeline_receipts(pipeline_receipt_dir)
    analysis = _capture_analysis(stdout, stderr, proxy_log, pipeline_receipts)
    analysis.update(
        {
            "provider": provider,
            "stage": stage,
            "command": executed_argv,
            "returncode": returncode,
            "timeout_seconds": timeout_seconds,
            "timed_out": timed_out,
            "started_at": start,
            "finished_at": _utc_now(),
            "prompt_sha256": _prompt_digest(prompt),
            "stdout_path": str(stdout_path.relative_to(run_dir)),
            "stderr_path": str(stderr_path.relative_to(run_dir)),
            "proxy_log_path": str(proxy_log.relative_to(run_dir)),
            "outcome": "success" if returncode == 0 else "failure",
        }
    )
    write_secure(analysis_path, json.dumps(analysis, indent=2, sort_keys=True) + "\n")
    _store_generated_athena_receipts(run_dir, pipeline_receipts)
    entry = {
        "id": f"{command_index:02d}-{slugify(stage) or 'stage'}",
        "kind": "capture",
        "provider": provider,
        "stage": stage,
        "command": executed_argv,
        "status": "success" if returncode == 0 else "failure",
        "returncode": returncode,
        "timeout_seconds": timeout_seconds,
        "timed_out": timed_out,
        "prompt_sha256": _prompt_digest(prompt),
        "revision": _latest_snapshot_head(manifest),
        "session_ids": analysis["session_ids"],
        "observed_skill_invocations": analysis["observed_skill_invocations"],
        "provider_skill_mentions": analysis["provider_skill_mentions"],
        "requested_skill_grants": analysis["requested_skill_grants"],
        "tool_scopes": analysis["tool_scopes"],
        "stdout_digest": analysis["stdout_digest"],
        "stderr_digest": analysis["stderr_digest"],
        "stdout_event_count": analysis["stdout_event_count"],
        "stderr_event_count": analysis["stderr_event_count"],
        "artifacts": {
            "stdout": str(stdout_path.relative_to(run_dir)),
            "stderr": str(stderr_path.relative_to(run_dir)),
            "analysis": str(analysis_path.relative_to(run_dir)),
            "proxy_log": str(proxy_log.relative_to(run_dir)),
        },
        "provider_invocations": analysis["proxy_invocations"],
        "pi_agent_receipts": analysis["pi_agent_receipts"],
        "started_at": start,
        "finished_at": _utc_now(),
    }
    _append_command_entry(run_dir, entry)
    _refresh_comparisons(run_dir)
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return returncode


def _record_failure_probe(
    run_dir: Path,
    *,
    provider: str,
    stage: str,
    command_argv: Sequence[str],
    prompt: str,
    timeout_seconds: int,
) -> int:
    rc = _record_command(
        run_dir,
        provider=provider,
        stage=stage,
        command_argv=command_argv,
        prompt=prompt,
        prompt_file=None,
        timeout_seconds=timeout_seconds,
    )
    if rc == 0:
        manifest = _load_manifest(run_dir)
        manifest["commands"][-1]["kind"] = "failure_probe"
        manifest["commands"][-1]["status"] = "unexpected_success"
        _save_manifest(run_dir, manifest)
        _refresh_comparisons(run_dir)
        print("error: failure probe unexpectedly succeeded", file=sys.stderr)
        return 1
    manifest = _load_manifest(run_dir)
    probe = manifest["commands"][-1]
    probe["kind"] = "failure_probe"
    if probe.get("timed_out") is True:
        probe["status"] = "unexpected_timeout"
        _save_manifest(run_dir, manifest)
        _refresh_comparisons(run_dir)
        return 1
    probe["status"] = "expected_failure"
    _save_manifest(run_dir, manifest)
    _refresh_comparisons(run_dir)
    return 0


def _record_defect(
    run_dir: Path,
    *,
    summary: str,
    follow_up_issue: int,
    details: str,
    source_entry: str = "",
) -> int:
    manifest = _load_manifest(run_dir)
    if any(
        isinstance(entry, dict) and entry.get("follow_up_issue") == follow_up_issue
        for entry in manifest.get("defects", [])
    ):
        raise ValueError(f"follow-up issue #{follow_up_issue} is already recorded")
    github = _stable_live_issue(Path(manifest["repo_root"]), follow_up_issue)
    defect_id = (
        f"{_utc_now().replace(':', '').replace('-', '')}-{slugify(summary)[:48] or 'defect'}"
    )
    defect_path = run_dir / DEFECTS_DIR_NAME / f"{defect_id}.json"
    record = {
        "id": defect_id,
        "summary": summary,
        "follow_up_issue": follow_up_issue,
        "details": details,
        "source_entry": source_entry,
        "github": github,
        "created_at": _utc_now(),
    }
    _write_json(defect_path, record)
    _append_defect_entry(
        run_dir,
        {
            **record,
            "artifact": str(defect_path.relative_to(run_dir)),
        },
    )
    return 0


def _github(repo_root: Path) -> PipelineGitHub:
    return PipelineGitHub("HomericIntelligence", repo="Hephaestus", repo_root=repo_root)


def _stable_live_issue(repo_root: Path, issue_number: int) -> dict[str, Any]:
    """Return one stable, repository-scoped follow-up issue readback."""
    github = _github(repo_root)
    initial = github.gh_issue_json(issue_number)
    final = github.gh_issue_json(issue_number)
    if initial != final:
        raise ValueError(f"follow-up issue #{issue_number} changed during live readback")
    expected_url = f"https://github.com/{PROJECT_REPOSITORY}/issues/{issue_number}"
    if (
        final.get("number") != issue_number
        or final.get("url") != expected_url
        or final.get("state") != "OPEN"
        or not isinstance(final.get("id"), str)
        or not final["id"]
        or not isinstance(final.get("title"), str)
        or not isinstance(final.get("body"), str)
        or FOLLOW_UP_PARENT_RE.search(final["body"]) is None
    ):
        raise ValueError(
            f"follow-up issue #{issue_number} is not an open {PROJECT_REPOSITORY} defect for "
            f"issue #{ISSUE_NUMBER}"
        )
    return {
        "id": final["id"],
        "number": issue_number,
        "title": final["title"],
        "state": final["state"],
        "url": final["url"],
    }


def _stable_live_pull_request(repo_root: Path, pr_number: int) -> dict[str, Any]:
    """Return a stabilized live PR/head/review authority snapshot."""
    github = _github(repo_root)

    def read() -> dict[str, Any]:
        state = github.gh_pr_state(pr_number)
        context = github.pr_review_context(pr_number)
        labels = github.pr_has_implementation_state_label(pr_number)
        threads = github.list_unresolved_review_threads(pr_number)
        if (
            not isinstance(state, dict)
            or not isinstance(context, dict)
            or not isinstance(labels, tuple)
            or len(labels) != 2
            or any(not isinstance(value, bool) for value in labels)
            or not isinstance(threads, list)
        ):
            raise ValueError("live GitHub pull-request readback is incomplete")
        thread_ids = [thread.get("id") for thread in threads if isinstance(thread, dict)]
        if len(thread_ids) != len(threads) or any(
            not isinstance(thread_id, str) or not thread_id for thread_id in thread_ids
        ):
            raise ValueError("live GitHub review-thread readback is malformed")
        return {
            "repository": PROJECT_REPOSITORY,
            "number": pr_number,
            "url": f"https://github.com/{PROJECT_REPOSITORY}/pull/{pr_number}",
            "state": state.get("state"),
            "head_sha": state.get("headRefOid"),
            "base_branch": state.get("baseRefName"),
            "closes_issue": github.find_issue_for_pr(pr_number),
            "implementation_go": labels[0],
            "implementation_no_go": labels[1],
            "unresolved_thread_ids": sorted(cast(list[str], thread_ids)),
            "native_auto_merge": state.get("autoMergeRequest") is not None,
            "reviewed_head_sha": context.get("pr_head_sha"),
        }

    initial = read()
    final = read()
    if initial != final:
        raise ValueError("live GitHub pull-request readback did not stabilize")
    if (
        final["state"] != "OPEN"
        or not isinstance(final["head_sha"], str)
        or FULL_COMMIT_SHA_RE.fullmatch(final["head_sha"]) is None
        or final["reviewed_head_sha"] != final["head_sha"]
        or final["base_branch"] != "main"
        or final["closes_issue"] != ISSUE_NUMBER
        or final["implementation_go"] is not True
        or final["implementation_no_go"] is not False
        or final["unresolved_thread_ids"]
        or final["native_auto_merge"] is not False
    ):
        raise ValueError("live GitHub pull request is not exact-head review-complete evidence")
    return final


def _live_mnemosyne_head(root: Path) -> str:
    """Read the current immutable commit of the canonical Mnemosyne checkout."""
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        timeout=DEFAULT_INVENTORY_TIMEOUT_SECONDS,
    )
    head = result.stdout.strip()
    if result.returncode != 0 or FULL_COMMIT_SHA_RE.fullmatch(head) is None:
        raise ValueError("live Mnemosyne checkout head is unavailable")
    return head


def _verify_live_learn_delivery(receipt: dict[str, Any]) -> None:
    """Rebind a host learn result to its current Mnemosyne pull request head."""
    if not valid_delivery_receipt(receipt):
        raise ValueError("Athena learn receipt lacks PR-backed delivery evidence")
    if receipt.get("repository") != "HomericIntelligence/Mnemosyne":
        raise ValueError("Athena learn receipt targets the wrong repository")
    pr_number = receipt.get("pr_number")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
        raise ValueError("Athena learn receipt has an invalid pull request")
    github = PipelineGitHub(
        "HomericIntelligence",
        repo="Mnemosyne",
        repo_root=default_mnemosyne_root(),
    )
    initial = github.gh_pr_state(pr_number)
    final = github.gh_pr_state(pr_number)
    if initial != final or not isinstance(final, dict):
        raise ValueError("Mnemosyne learn pull-request readback did not stabilize")
    if (
        final.get("state") not in {"OPEN", "MERGED"}
        or final.get("headRefOid") != receipt.get("commit_sha")
        or final.get("baseRefName") != receipt.get("base_branch")
        or receipt.get("readback_head_sha") != final.get("headRefOid")
    ):
        raise ValueError("Mnemosyne learn receipt does not match its live pull request")


def _validate_athena_host_receipt(payload: dict[str, Any], kind: str) -> None:
    """Validate one typed host result against live Athena and Mnemosyne facts."""
    if payload.get("kind") != kind or payload.get("error") is not None:
        raise ValueError(f"Athena {kind} host receipt is not a successful typed result")
    receipt = payload.get("receipt")
    if not isinstance(receipt, dict):
        raise ValueError(f"Athena {kind} host receipt lacks its contract receipt")
    contract = load_athena_contract_receipt().to_dict()
    binding = receipt.get("binding")
    if receipt.get("contract") != contract or not isinstance(binding, dict):
        raise ValueError(f"Athena {kind} host receipt does not match the live contract")
    root = binding.get("root")
    commit = binding.get("commit_sha")
    if (
        not isinstance(root, str)
        or Path(root).resolve() != default_mnemosyne_root().resolve()
        or binding.get("repository") != "HomericIntelligence/Mnemosyne"
        or not isinstance(commit, str)
        or FULL_COMMIT_SHA_RE.fullmatch(commit) is None
        or binding.get("athena_contract") != contract
        or _live_mnemosyne_head(Path(root)) != commit
    ):
        raise ValueError(f"Athena {kind} host receipt lacks a live Mnemosyne binding")
    if kind == "advise":
        corpus = receipt.get("corpus")
        if (
            not isinstance(payload.get("context"), str)
            or not payload["context"]
            or not isinstance(corpus, dict)
            or corpus.get("repository") != binding["repository"]
            or corpus.get("commit_sha") != commit
            or corpus.get("athena_contract") != contract
        ):
            raise ValueError("Athena advise receipt lacks bound corpus evidence")
    else:
        delivery = payload.get("delivery_receipt")
        if not isinstance(delivery, dict):
            raise ValueError("Athena learn receipt lacks delivery evidence")
        _verify_live_learn_delivery(delivery)


def _verify_athena_host_receipts(manifest: dict[str, Any], run_dir: Path) -> None:
    """Re-read and freshness-check the required advise and learn receipts."""
    receipts = manifest.get("athena_host_receipts")
    if not isinstance(receipts, list) or {entry.get("kind") for entry in receipts} != {
        "advise",
        "learn",
    }:
        raise ValueError("typed Athena advise and learn host receipts are required")
    for descriptor in receipts:
        if descriptor.get("source") != "pipeline":
            raise ValueError("Athena host receipt was not emitted by the queue pipeline")
        artifact = descriptor.get("artifact")
        if not isinstance(artifact, str):
            raise ValueError("Athena host receipt artifact is invalid")
        path = run_dir / artifact
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ValueError("Athena host receipt artifact is unavailable") from exc
        if (
            path.is_symlink()
            or not resolved.is_relative_to(run_dir.resolve())
            or not resolved.is_file()
        ):
            raise ValueError("Athena host receipt artifact escapes the private run")
        path = resolved
        content = path.read_bytes()
        if _sha256_bytes(content) != descriptor.get("sha256"):
            raise ValueError("Athena host receipt artifact digest mismatch")
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise ValueError("Athena host receipt artifact is malformed")
        _validate_athena_host_receipt(cast(dict[str, Any], payload), descriptor["kind"])


def _render_report(manifest: dict[str, Any], report_path: Path, runbook_path: Path) -> str:
    pi = cast(dict[str, Any], manifest.get("pi", {}))
    inventory = cast(dict[str, Any], pi.get("package_inventory", {}))
    commands = [entry for entry in manifest.get("commands", []) if entry.get("kind") == "capture"]
    defects = cast(list[dict[str, Any]], manifest.get("defects", []))
    athena_receipts = cast(list[dict[str, Any]], manifest.get("athena_host_receipts", []))
    snapshots = cast(list[dict[str, Any]], manifest.get("snapshots", []))
    skill_commands = ", ".join(f"`{skill}`" for skill in pi.get("skill_commands", [])) or "n/a"
    lines = [
        "# Pi Issue 2519 Report",
        "",
        f"- Evidence status: `{_evidence_status(manifest)}`",
        f"- Fixture: `{manifest['fixture']['title']}`",
        f"- Run ID: `{manifest['run_id']}`",
        f"- Created: `{manifest['created_at']}`",
        f"- Pi version: `{pi.get('version', '')}`",
        "- Pi binary: recorded privately in the run manifest",
        f"- Skill commands: {skill_commands}",
        f"- Inventory status: `{inventory.get('status', '')}`",
        f"- Inventory ready: `{inventory.get('ready', False)}`",
        "",
        "## Captured Commands",
        "",
    ]
    if commands:
        lines.extend(
            [
                (
                    "| Stage | Provider | Status | Returncode | Session evidence | "
                    "Requested tool scopes | Requested skill grants |"
                ),
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for entry in commands:
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(entry.get("stage", "")),
                        str(entry.get("provider", "")),
                        str(entry.get("status", "")),
                        str(entry.get("returncode", "")),
                        ", ".join(f"`{session_id}`" for session_id in entry.get("session_ids", []))
                        or "none",
                        ", ".join(entry.get("tool_scopes", [])) or "n/a",
                        ", ".join(entry.get("requested_skill_grants", [])) or "n/a",
                    ]
                )
                + " |"
            )
    else:
        lines.append("_No captured commands recorded yet._")
    lines.extend(
        [
            "",
            "## Snapshots",
            "",
        ]
    )
    if snapshots:
        for snapshot in snapshots:
            lines.append(
                f"- `{snapshot.get('label', '')}` -> `{snapshot.get('branch', '')}` "
                f"`{snapshot.get('head', '')}`"
            )
    else:
        lines.append("_No repository snapshots recorded yet._")
    lines.extend(["", "## Athena Host Receipts", ""])
    if athena_receipts:
        lines.extend(
            f"- `{entry.get('kind', '')}`: typed host receipt recorded privately"
            for entry in athena_receipts
        )
    else:
        lines.append("_No typed Athena host receipts recorded yet._")
    lines.extend(
        [
            "",
            "## Defects",
            "",
        ]
    )
    if defects:
        for defect in defects:
            lines.append(
                f"- Follow-up issue #{defect.get('follow_up_issue')}: {defect.get('summary', '')}"
            )
    else:
        lines.append("_No follow-up defects recorded._")
    lines.extend(
        [
            "",
            "## Publication",
            "",
            f"- Runbook: `{DEFAULT_RUNBOOK_PATH}`",
            f"- Report: `{DEFAULT_REPORT_PATH}`",
            "",
        ]
    )
    rendered = "\n".join(lines)
    tokens = pi_private_redaction_tokens(
        Path(manifest["repo_root"]),
        "",
        additional_roots=(Path(manifest["repo_root"]),),
    )
    return redact_pi_private_values(rendered, tokens)


def _render_runbook(manifest: dict[str, Any], run_dir: Path, report_path: Path) -> str:
    pi = cast(dict[str, Any], manifest.get("pi", {}))
    inventory = cast(dict[str, Any], pi.get("package_inventory", {}))
    skill_commands = ", ".join(f"`{skill}`" for skill in pi.get("skill_commands", [])) or "n/a"
    required_skill_commands = ", ".join(f"`{skill}`" for skill in REQUIRED_SKILL_COMMANDS)
    script_name = Path(__file__).name
    rendered = "\n".join(
        [
            "# Pi Issue 2519 Runbook",
            "",
            "This runbook reproduces the live evidence collected for issue #2519.",
            "",
            f"- Evidence status: `{_evidence_status(manifest)}`",
            "- Exact local paths remain in the owner-only manifest.",
            "- Session identifiers are published in the report as required evidence.",
            "",
            "## Target Fixture",
            "",
            f"- `{FIXTURE_TITLE}`",
            f"- {FIXTURE_SUMMARY}",
            "",
            "## Evidence Root",
            "",
            f"- Run directory: `build/{RUN_ROOT_NAME}/<run-id>/`",
            f"- Manifest: `build/{RUN_ROOT_NAME}/<run-id>/{MANIFEST_NAME}`",
            f"- Report: `{DEFAULT_REPORT_PATH}`",
            "",
            "## Collection Steps",
            "",
            (
                "1. Install a reviewed external Pi isolation adapter and explicitly select its "
                "`hephaestus.pi_isolation_adapters` entry-point name with "
                '`HEPH_PI_ISOLATION_ADAPTER`. Stop if `resolve_agent("pi")` reports that the '
                "adapter is unavailable; direct Pi CLI execution is not workflow evidence."
            ),
            "2. Initialize the private run directory.",
            "3. Record the exact Pi inventory and command catalog.",
            "4. Run the normal planner and automation-loop commands through `capture`.",
            "5. Retain the typed Athena and provider receipts emitted by those queue runs.",
            "6. Capture any failure probes that demonstrate the stage boundary.",
            "7. Record defects as live-linked follow-up issues.",
            "8. Render the report and runbook from the manifest.",
            "9. Attest publication readiness against the live PR head.",
            "",
            "## Verification Matrix",
            "",
            f"- Pi version: `{pi.get('version', '')}`",
            f"- Inventory status: `{inventory.get('status', '')}`",
            f"- Skill commands: {skill_commands}",
            f"- Required skill commands: {required_skill_commands}",
            "",
            "## Direct Commands",
            "",
            "```bash",
            f"uv run python scripts/{script_name} init --run-id <run-id>",
            f"uv run python scripts/{script_name} inventory --run-id <run-id>",
            (
                f"uv run python scripts/{script_name} snapshot --run-id <run-id> "
                "--label repo-snapshot"
            ),
            (
                f"uv run python scripts/{script_name} capture --run-id <run-id> "
                "--stage discovery-plan --provider pi -- uv run hephaestus-plan-issues "
                f"--issues {ISSUE_NUMBER} --parallel 1 --agent pi --json"
            ),
            (
                f"uv run python scripts/{script_name} capture --run-id <run-id> "
                "--stage implementation-review-handoff --provider pi -- uv run "
                f"hephaestus-automation-loop --issues {ISSUE_NUMBER} --agent pi --json"
            ),
            (
                f"uv run python scripts/{script_name} failure-probe --run-id <run-id> "
                "--stage <stage> --provider codex -- <command...>"
            ),
            (
                f"uv run python scripts/{script_name} record-defect --run-id <run-id> "
                '--summary "<summary>" --follow-up-issue <issue-number>'
            ),
            f"uv run python scripts/{script_name} render --run-id <run-id>",
            (
                f"uv run python scripts/{script_name} verify --run-id <run-id> "
                f"--criterion completion --report {DEFAULT_REPORT_PATH} "
                f"--runbook {DEFAULT_RUNBOOK_PATH}"
            ),
            (
                f"uv run python scripts/{script_name} attest-publication --run-id <run-id> "
                "--repo HomericIntelligence/Hephaestus --ref <commit-sha> "
                "--pr <pr-number> --verify-defects"
            ),
            "```",
            "",
            "## Verification Criteria",
            "",
            "- `fixture` validates the deterministic fixture contract.",
            "- `workflow` requires inventory, command, and repository snapshot evidence.",
            "- `capture` requires private session identifiers and observed tool scopes.",
            "- `comparison` requires Pi and a distinct control provider.",
            "- `mnemosyne` revalidates typed host-owned advise and learn receipts.",
            "- `publication` requires deterministic manifest-to-document rendering.",
            (
                "- `completion` requires both normal pipeline captures to succeed plus all "
                "criteria above."
            ),
            "",
        ]
    )
    tokens = pi_private_redaction_tokens(
        Path(manifest["repo_root"]),
        "",
        additional_roots=(run_dir,),
    )
    return redact_pi_private_values(rendered, tokens)


def _render_artifacts(run_dir: Path, report_path: Path, runbook_path: Path) -> int:
    manifest = _load_manifest(run_dir)
    report_text = _render_report(manifest, report_path, runbook_path)
    runbook_text = _render_runbook(manifest, run_dir, report_path)
    write_secure(report_path, report_text)
    write_secure(runbook_path, runbook_text)
    _update_manifest(
        run_dir,
        {
            "artifacts": {
                "report": str(report_path),
                "runbook": str(runbook_path),
            },
        },
    )
    return 0


def _require_manifest_paths(report_path: Path, runbook_path: Path) -> None:
    if not report_path.exists():
        raise FileNotFoundError(f"missing report: {report_path}")
    if not runbook_path.exists():
        raise FileNotFoundError(f"missing runbook: {runbook_path}")


def _verify_fixture(manifest: dict[str, Any]) -> None:
    if manifest.get("issue_number") != ISSUE_NUMBER:
        raise ValueError("run manifest does not target issue #2519")
    fixture = cast(dict[str, Any], manifest.get("fixture", {}))
    if fixture.get("title") != FIXTURE_TITLE:
        raise ValueError("run manifest fixture title is invalid")
    if FIXTURE_SUMMARY not in fixture.get("summary", ""):
        raise ValueError("run manifest fixture summary is invalid")


def _verify_workflow(manifest: dict[str, Any]) -> None:
    if not manifest.get("commands"):
        raise ValueError("no commands were captured")
    if not manifest.get("snapshots"):
        raise ValueError("no repository snapshot was recorded")
    if not manifest.get("pi"):
        raise ValueError("no Pi inventory was recorded")


def _verify_capture(manifest: dict[str, Any]) -> None:
    capture_entries = [
        entry for entry in manifest.get("commands", []) if entry.get("kind") == "capture"
    ]
    if not capture_entries:
        raise ValueError("no captured command entries were recorded")
    pi_entries = [entry for entry in capture_entries if entry.get("provider") == PI_PROVIDER_NAME]
    if not pi_entries:
        raise ValueError("no Pi pipeline captures were recorded")
    for entry in pi_entries:
        stage = entry.get("stage", "unknown")
        if not entry.get("session_ids"):
            raise ValueError(f"Pi capture {stage} emitted no session id")
        if not entry.get("tool_scopes"):
            raise ValueError(f"Pi capture {stage} recorded no requested tool scopes")
        receipts = entry.get("pi_agent_receipts")
        if not isinstance(receipts, list) or not receipts:
            raise ValueError(f"Pi capture {stage} has no queue-worker agent receipts")
        if any(
            receipt.get("ok") is not True
            or not receipt.get("session_id")
            or not receipt.get("tool_scopes")
            or not isinstance(receipt.get("execution_request"), dict)
            for receipt in receipts
            if isinstance(receipt, dict)
        ) or any(not isinstance(receipt, dict) for receipt in receipts):
            raise ValueError(f"Pi capture {stage} has incomplete queue-worker agent receipts")


def _verify_comparison(manifest: dict[str, Any]) -> None:
    capture_entries = [
        entry for entry in manifest.get("commands", []) if entry.get("kind") == "capture"
    ]
    providers = {entry.get("provider") for entry in capture_entries if entry.get("provider")}
    if len(providers) < 2:
        raise ValueError("comparison requires at least two distinct provider runs")
    comparisons = manifest.get("comparisons")
    if not isinstance(comparisons, list) or not comparisons:
        raise ValueError("comparison requires a persisted Pi/control pair")
    by_id = {entry.get("id"): entry for entry in capture_entries}
    for comparison in comparisons:
        if not isinstance(comparison, dict):
            raise ValueError("comparison record is malformed")
        pi_entry = by_id.get(comparison.get("pi_entry_id"))
        control_entry = by_id.get(comparison.get("control_entry_id"))
        if pi_entry is None or control_entry is None:
            raise ValueError("comparison references a missing capture")
        if comparison != _comparison_payload(pi_entry, control_entry):
            raise ValueError("comparison no longer matches its paired captures")


def _verify_failure_probes(manifest: dict[str, Any]) -> None:
    for probe in manifest.get("commands", []):
        if probe.get("kind") != "failure_probe":
            continue
        if (
            probe.get("status") != "expected_failure"
            or probe.get("timed_out") is not False
            or not isinstance(probe.get("returncode"), int)
            or probe["returncode"] == 0
        ):
            raise ValueError("failure probe did not record its expected nonzero outcome")


def _verify_mnemosyne(manifest: dict[str, Any]) -> None:
    receipts = manifest.get("athena_host_receipts")
    if not isinstance(receipts, list) or {entry.get("kind") for entry in receipts} != {
        "advise",
        "learn",
    }:
        raise ValueError("typed Athena advise and learn host receipts are required")
    if any(entry.get("source") != "pipeline" for entry in receipts):
        raise ValueError("Athena host receipts must come from the queue pipeline")


def _verify_completion(manifest: dict[str, Any]) -> None:
    """Require the complete successful Pi workflow and its control evidence."""
    _verify_fixture(manifest)
    _verify_workflow(manifest)
    _verify_capture(manifest)
    _verify_comparison(manifest)
    _verify_failure_probes(manifest)
    _verify_mnemosyne(manifest)
    pi = cast(dict[str, Any], manifest.get("pi", {}))
    inventory = cast(dict[str, Any], pi.get("package_inventory", {}))
    if inventory.get("ready") is not True:
        raise ValueError("Pi package inventory is not ready")
    pi_captures = [
        entry
        for entry in manifest.get("commands", [])
        if entry.get("kind") == "capture" and entry.get("provider") == PI_PROVIDER_NAME
    ]
    failed = [
        entry.get("id", "unknown") for entry in pi_captures if entry.get("status") != "success"
    ]
    if failed:
        raise ValueError(f"Pi capture stages did not succeed: {', '.join(map(str, failed))}")
    observed_stages = {
        entry.get("stage") for entry in pi_captures if isinstance(entry.get("stage"), str)
    }
    missing_stages = [stage for stage in REQUIRED_E2E_STAGES if stage not in observed_stages]
    if missing_stages:
        raise ValueError(f"required Pi stages are missing: {', '.join(missing_stages)}")


def _evidence_status(manifest: dict[str, Any]) -> str:
    """Return the truthful publishable completion state for a private manifest."""
    try:
        _verify_completion(manifest)
    except (KeyError, TypeError, ValueError):
        return "incomplete"
    return "complete"


def _verify_publication(
    manifest: dict[str, Any],
    run_dir: Path,
    report_path: Path,
    runbook_path: Path,
) -> None:
    _require_manifest_paths(report_path, runbook_path)
    expected_report = _render_report(manifest, report_path, runbook_path)
    expected_runbook = _render_runbook(manifest, run_dir, report_path)
    if report_path.read_text(encoding="utf-8") != expected_report:
        raise ValueError("rendered report does not match the manifest")
    if runbook_path.read_text(encoding="utf-8") != expected_runbook:
        raise ValueError("rendered runbook does not match the manifest")
    publication = cast(dict[str, Any], manifest.get("publication", {}))
    if publication and publication.get("report") != str(report_path):
        raise ValueError("publication report path does not match the manifest")
    if publication and publication.get("runbook") != str(runbook_path):
        raise ValueError("publication runbook path does not match the manifest")


def _verify_run(
    run_dir: Path,
    *,
    criterion: str,
    report_path: Path | None = None,
    runbook_path: Path | None = None,
) -> int:
    manifest = _load_manifest(run_dir)
    if criterion == "fixture":
        _verify_fixture(manifest)
        return 0
    if criterion == "workflow":
        _verify_workflow(manifest)
        return 0
    if criterion == "capture":
        _verify_capture(manifest)
        return 0
    if criterion == "comparison":
        _verify_comparison(manifest)
        return 0
    if criterion == "mnemosyne":
        _verify_mnemosyne(manifest)
        _verify_athena_host_receipts(manifest, run_dir)
        return 0
    if criterion == "publication":
        if report_path is None or runbook_path is None:
            raise ValueError("publication verification requires report and runbook paths")
        _verify_publication(manifest, run_dir, report_path, runbook_path)
        return 0
    if criterion == "completion":
        if report_path is None or runbook_path is None:
            raise ValueError("completion verification requires report and runbook paths")
        _verify_completion(manifest)
        _verify_athena_host_receipts(manifest, run_dir)
        _verify_publication(manifest, run_dir, report_path, runbook_path)
        return 0
    raise ValueError(f"unsupported verification criterion: {criterion}")


def _attest_publication(
    run_dir: Path,
    *,
    repo: str,
    ref: str,
    pr_number: int,
    report_path: Path,
    runbook_path: Path,
    verify_defects: bool,
) -> int:
    manifest = _load_manifest(run_dir)
    _verify_completion(manifest)
    _verify_publication(manifest, run_dir, report_path, runbook_path)
    _verify_athena_host_receipts(manifest, run_dir)
    if repo != PROJECT_REPOSITORY:
        raise ValueError(f"publication repository must be {PROJECT_REPOSITORY}")
    if FULL_COMMIT_SHA_RE.fullmatch(ref) is None:
        raise ValueError("publication ref must be a full immutable commit SHA")
    snapshot_heads = {
        entry.get("head") for entry in manifest.get("snapshots", []) if isinstance(entry, dict)
    }
    if ref not in snapshot_heads:
        raise ValueError("publication ref must match a recorded repository snapshot")
    live_pr = _stable_live_pull_request(Path(manifest["repo_root"]), pr_number)
    if live_pr["head_sha"] != ref:
        raise ValueError("publication ref does not match the live pull-request head")
    if verify_defects:
        defects = cast(list[dict[str, Any]], manifest.get("defects", []))
        seen: set[int] = set()
        for entry in defects:
            issue_number = entry.get("follow_up_issue")
            if (
                isinstance(issue_number, bool)
                or not isinstance(issue_number, int)
                or issue_number <= 0
                or issue_number in seen
            ):
                raise ValueError("one or more defects lacks a unique follow-up issue")
            seen.add(issue_number)
            if entry.get("github") != _stable_live_issue(Path(manifest["repo_root"]), issue_number):
                raise ValueError(f"follow-up issue #{issue_number} no longer matches its receipt")
    publication = {
        "repo": repo,
        "ref": ref,
        "commit_sha": ref,
        "pull_request": live_pr,
        "report": str(report_path),
        "runbook": str(runbook_path),
        "verified_defects": verify_defects,
        "attested_at": _utc_now(),
    }
    _update_manifest(run_dir, {"publication": publication})
    _write_json(run_dir / ARTIFACTS_DIR_NAME / "publication.json", publication)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the issue-specific Pi evidence collector parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_repo_root())
    parser.add_argument("--run-root", type=Path, default=_run_root(_repo_root()))
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="create a new private run directory")
    init.add_argument("--run-id", default="")

    inventory = subparsers.add_parser("inventory", help="record exact Pi inventory")
    inventory.add_argument("--run-id", required=True)

    snapshot = subparsers.add_parser("snapshot", help="record a repository snapshot")
    snapshot.add_argument("--run-id", required=True)
    snapshot.add_argument("--label", default="repo-snapshot")

    capture = subparsers.add_parser("capture", help="capture a provider-backed command")
    capture.add_argument("--run-id", required=True)
    capture.add_argument("--stage", required=True)
    capture.add_argument("--provider", choices=("pi", "codex", "claude"), default="pi")
    capture.add_argument("--prompt", default="")
    capture.add_argument("--prompt-file", type=Path)
    capture.add_argument(
        "--timeout",
        type=_positive_timeout,
        default=DEFAULT_CAPTURE_TIMEOUT_SECONDS,
        help="maximum provider execution time in seconds (default: %(default)s)",
    )
    capture.add_argument("command_argv", nargs=argparse.REMAINDER)

    failure = subparsers.add_parser("failure-probe", help="capture a command expected to fail")
    failure.add_argument("--run-id", required=True)
    failure.add_argument("--stage", required=True)
    failure.add_argument("--provider", choices=("pi", "codex", "claude"), default="pi")
    failure.add_argument("--prompt", default="")
    failure.add_argument("--prompt-file", type=Path)
    failure.add_argument(
        "--timeout",
        type=_positive_timeout,
        default=DEFAULT_CAPTURE_TIMEOUT_SECONDS,
        help="maximum provider execution time in seconds (default: %(default)s)",
    )
    failure.add_argument("command_argv", nargs=argparse.REMAINDER)

    defect = subparsers.add_parser("record-defect", help="record a follow-up defect")
    defect.add_argument("--run-id", required=True)
    defect.add_argument("--summary", required=True)
    defect.add_argument("--follow-up-issue", type=int, required=True)
    defect.add_argument("--details", default="")
    defect.add_argument("--source-entry", default="")

    render = subparsers.add_parser("render", help="render the report and runbook")
    render.add_argument("--run-id", required=True)
    render.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    render.add_argument("--runbook", type=Path, default=DEFAULT_RUNBOOK_PATH)

    verify = subparsers.add_parser("verify", help="verify one evidence criterion")
    verify.add_argument("--run-id", required=True)
    verify.add_argument(
        "--criterion",
        choices=(
            "fixture",
            "workflow",
            "capture",
            "comparison",
            "mnemosyne",
            "publication",
            "completion",
        ),
        required=True,
    )
    verify.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    verify.add_argument("--runbook", type=Path, default=DEFAULT_RUNBOOK_PATH)

    attest = subparsers.add_parser("attest-publication", help="attest report/runbook publication")
    attest.add_argument("--run-id", required=True)
    attest.add_argument("--repo", required=True)
    attest.add_argument("--ref", required=True)
    attest.add_argument("--pr", type=int, required=True)
    attest.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    attest.add_argument("--runbook", type=Path, default=DEFAULT_RUNBOOK_PATH)
    attest.add_argument("--verify-defects", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the issue-specific collector CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root)
    run_root = Path(args.run_root)

    try:
        if args.command == "init":
            run_id = args.run_id or _new_run_id()
            run_dir = _prepare_run_dir(run_root, run_id, repo_root)
            print(run_dir)
            return 0

        run_dir = _resolve_run_dir(run_root, args.run_id)
        if args.command == "inventory":
            return _record_inventory(run_dir)
        if args.command == "snapshot":
            return _record_snapshot(run_dir, args.label)
        if args.command == "capture":
            prompt = args.prompt or _load_prompt(args)
            command_argv = _normalize_command_argv(args.command_argv)
            if not command_argv:
                raise ValueError("capture requires a command to execute")
            return _record_command(
                run_dir,
                provider=args.provider,
                stage=args.stage,
                command_argv=command_argv,
                prompt=prompt,
                prompt_file=args.prompt_file,
                timeout_seconds=args.timeout,
            )
        if args.command == "failure-probe":
            prompt = args.prompt or _load_prompt(args)
            command_argv = _normalize_command_argv(args.command_argv)
            if not command_argv:
                raise ValueError("failure-probe requires a command to execute")
            return _record_failure_probe(
                run_dir,
                provider=args.provider,
                stage=args.stage,
                command_argv=command_argv,
                prompt=prompt,
                timeout_seconds=args.timeout,
            )
        if args.command == "record-defect":
            return _record_defect(
                run_dir,
                summary=args.summary,
                follow_up_issue=args.follow_up_issue,
                details=args.details,
                source_entry=args.source_entry,
            )
        if args.command == "render":
            return _render_artifacts(run_dir, args.report, args.runbook)
        if args.command == "verify":
            return _verify_run(
                run_dir,
                criterion=args.criterion,
                report_path=args.report,
                runbook_path=args.runbook,
            )
        if args.command == "attest-publication":
            return _attest_publication(
                run_dir,
                repo=args.repo,
                ref=args.ref,
                pr_number=args.pr,
                report_path=args.report,
                runbook_path=args.runbook,
                verify_defects=args.verify_defects,
            )
        raise ValueError(f"unsupported command: {args.command}")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

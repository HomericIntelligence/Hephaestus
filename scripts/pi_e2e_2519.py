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
from pathlib import Path, PurePosixPath
from typing import Any, cast

from hephaestus.agents.execution_policy import (
    AgentOperation,
    AgentRole,
    ExecutionPolicy,
    ExecutionRequest,
    SessionLifecycle,
    resolve_policy,
)
from hephaestus.agents.pi_plugins import inspect_pi_package_inventory, load_pi_package_catalog
from hephaestus.agents.pi_session import AgentSessionBinding
from hephaestus.agents.runtime import (
    AgentRunResult,
    direct_agent_model,
    pi_private_redaction_tokens,
    redact_pi_private_values,
    resolve_agent,
    run_agent_session,
    run_agent_text,
)
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
ATHENA_SKILL_JOBS_DIR_NAME = "athena-skill-jobs"
DEFECTS_DIR_NAME = "defects"
ARTIFACTS_DIR_NAME = "artifacts"
PROXY_TOOL_NAMES = ("pi", "codex")
REQUIRED_SKILL_COMMANDS = ("skill:advise", "skill:learn", "skill:pr-review")
REQUIRED_E2E_STAGES = (
    "discovery",
    "planning",
    "implementation",
    "tests",
    "commit-pr",
    "review",
)
SKILL_COMMAND_RE = re.compile(r"(?:\$athena:|/athena:|skill:)[A-Za-z0-9._:/-]+")
SAFE_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
FULL_COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
FOLLOW_UP_PARENT_LINE_RE = re.compile(rf"^Parent: #{ISSUE_NUMBER}[ \t]*$", re.MULTILINE)
DEFAULT_CAPTURE_TIMEOUT_SECONDS = 600
DEFAULT_INVENTORY_TIMEOUT_SECONDS = 30
MAX_ATHENA_HOST_RECEIPT_BYTES = 64 * 1024
MAX_STAGE_RECEIPT_BYTES = 128 * 1024
PI_PROVIDER_NAME = "pi"
CONTROL_PROVIDER_NAME = "codex"
FAILURE_PROBE_KIND = "failure_probe"
FAILURE_PROBE_EVIDENCE_KIND = "expected_failure_probe"
EXPECTED_FAILURE_PROBE_OUTCOME = {
    "returncode": "nonzero",
    "timed_out": False,
}
FIXTURE_PATHS = (
    "hephaestus/utils/helpers.py",
    "tests/unit/utils/test_general_utils.py",
)
FIXTURE_TEST_ARGV = (
    "uv",
    "run",
    "pytest",
    "tests/unit/utils/test_general_utils.py::TestHumanReadableSize",
    "-q",
)
PI_EVIDENCE_STAGE_REQUESTS = {
    "discovery": ExecutionRequest(
        AgentRole.PLAN_REVIEWER,
        AgentOperation.PLAN_REVIEW,
        SessionLifecycle.ONE_SHOT,
    ),
    "planning": ExecutionRequest(
        AgentRole.PLANNER,
        AgentOperation.PLAN,
        SessionLifecycle.START_NEW,
    ),
    "implementation": ExecutionRequest(
        AgentRole.IMPLEMENTER,
        AgentOperation.IMPLEMENT,
        SessionLifecycle.START_NEW,
    ),
    "tests": ExecutionRequest(
        AgentRole.IMPLEMENTER,
        AgentOperation.TEST_FIX,
        SessionLifecycle.RESUME_REQUIRED,
    ),
    "commit-pr": ExecutionRequest(
        AgentRole.IMPLEMENTER,
        AgentOperation.GIT_MESSAGE,
        SessionLifecycle.ONE_SHOT,
    ),
    "review": ExecutionRequest(
        AgentRole.PR_REVIEWER,
        AgentOperation.PR_REVIEW,
        SessionLifecycle.ONE_SHOT,
    ),
}
ATHENA_SKILL_JOB_CORRELATIONS = {
    "planning": ("advise", AgentRole.PLANNER.value),
    "implementation": ("advise", AgentRole.IMPLEMENTER.value),
    "review": ("learn", AgentRole.PR_REVIEWER.value),
}
PI_STAGE_COORDINATOR_STAGES = {
    "discovery": "repo",
    "planning": "planning",
    "implementation": "implementation",
    "tests": "implementation",
    "commit-pr": "implementation",
    "review": "pr_review",
}
PI_STAGE_COORDINATOR_JOBS = {
    "discovery": "GitHubJob",
    "planning": "AgentJob",
    "implementation": "AgentJob",
    "tests": "BuildTestJob",
    "commit-pr": "GitJob",
    "review": "AgentJob",
}
PI_STAGE_LIFECYCLE_KINDS = {
    "discovery": "fixture-discovery",
    "planning": "fixture-plan",
    "implementation": "fixture-diff",
    "tests": "fixture-test",
    "commit-pr": "commit-pr-readback",
    "review": "review-readback",
}
PI_STAGE_COORDINATOR_FIELDS = frozenset(
    {
        "source",
        "pipeline_stage",
        "job_kind",
        "sequence",
        "outcome",
        "receipt_id",
        "worker_id",
        "completed_at",
    }
)
PI_STAGE_PROVIDER_EVIDENCE_FIELDS = frozenset(
    {
        "capture_id",
        "coordinator_receipt_id",
        "capture_analysis_sha256",
        "execution_policy",
        "tool_scopes",
        "skill_grants",
        "session_ids",
        "invocation_id",
        "stdout_sha256",
        "stderr_sha256",
        "session_binding_sha256",
        "resumed_from_capture_id",
    }
)
PI_STAGE_WORKTREE_FIELDS = frozenset(
    {
        "receipt_id",
        "capture_id",
        "coordinator_receipt_id",
        "root",
        "git_dir",
        "git_common_dir",
        "isolated",
        "branch",
        "head",
        "clean",
        "status_sha256",
        "observed_at",
    }
)
PI_STAGE_PULL_REQUEST_FIELDS = frozenset(
    {
        "receipt_id",
        "lifecycle_receipt_id",
        "repository",
        "number",
        "url",
        "state",
        "branch",
        "head_sha",
        "closes_issue",
    }
)
PI_STAGE_PULL_REQUEST_LIVE_FIELDS = frozenset(
    {
        "repository",
        "number",
        "url",
        "state",
        "branch",
        "head_sha",
        "closes_issue",
    }
)
LIVE_PR_READBACK_FIELDS = frozenset(
    {
        "repository",
        "number",
        "url",
        "state",
        "branch",
        "head_sha",
        "closes_issue",
        "implementation_go",
        "implementation_no_go",
        "unresolved_threads",
        "unresolved_thread_ids",
        "native_auto_merge",
    }
)
PI_STAGE_HANDOFF_FIELDS = frozenset(
    {
        "source",
        "destination_stage",
        "outcome",
        "receipt_id",
        "coordinator_receipt_id",
        "worktree_receipt_id",
        "provider_invocation_id",
        "review_receipt_id",
        "head_sha",
        "pr_number",
        "completed_at",
    }
)
_PI_STAGE_LIFECYCLE_BASE_FIELDS = frozenset(
    {
        "kind",
        "receipt_id",
        "capture_id",
        "coordinator_receipt_id",
        "worktree_receipt_id",
        "provider_invocation_id",
        "fixture_sha256",
        "head_sha",
        "success",
    }
)
PI_STAGE_LIFECYCLE_FIELDS = {
    "discovery": _PI_STAGE_LIFECYCLE_BASE_FIELDS | {"paths", "all_paths_found"},
    "planning": _PI_STAGE_LIFECYCLE_BASE_FIELDS | {"plan_sha256"},
    "implementation": _PI_STAGE_LIFECYCLE_BASE_FIELDS | {"changed_paths", "diff_sha256"},
    "tests": _PI_STAGE_LIFECYCLE_BASE_FIELDS | {"argv", "returncode", "timed_out", "result_sha256"},
    "commit-pr": _PI_STAGE_LIFECYCLE_BASE_FIELDS
    | {"pull_request", "commit_sha", "signed_commit", "dco_signed_off"},
    "review": _PI_STAGE_LIFECYCLE_BASE_FIELDS
    | {
        "pull_request",
        "reviewed_head_sha",
        "implementation_go",
        "implementation_no_go",
        "unresolved_threads",
        "native_auto_merge",
        "review_receipt_id",
        "handoff",
    },
}


_LIVE_READBACK_PROVENANCE = object()
_LIVE_FOLLOW_UP_PROVENANCE = object()


class _LivePullRequestReceipt:
    """Process-local proof that PR facts came from the live GitHub seam."""

    __slots__ = (
        "branch",
        "closes_issue",
        "head_sha",
        "implementation_go",
        "implementation_no_go",
        "native_auto_merge",
        "number",
        "provenance",
        "repository",
        "state",
        "unresolved_thread_ids",
        "url",
    )

    def __init__(
        self,
        repository: str,
        number: int,
        url: str,
        state: str,
        branch: str,
        head_sha: str,
        closes_issue: int,
        implementation_go: bool,
        implementation_no_go: bool,
        unresolved_thread_ids: tuple[str, ...],
        native_auto_merge: bool,
        provenance: object,
    ) -> None:
        self.repository = repository
        self.number = number
        self.url = url
        self.state = state
        self.branch = branch
        self.head_sha = head_sha
        self.closes_issue = closes_issue
        self.implementation_go = implementation_go
        self.implementation_no_go = implementation_no_go
        self.unresolved_thread_ids = unresolved_thread_ids
        self.native_auto_merge = native_auto_merge
        self.provenance = provenance


class _LiveFollowUpIssueReceipt:
    """Process-local proof that follow-up facts came from the scoped host seam."""

    __slots__ = (
        "node_id",
        "number",
        "parent_issue",
        "provenance",
        "repository",
        "state",
        "url",
    )

    def __init__(
        self,
        repository: str,
        node_id: str,
        number: int,
        url: str,
        state: str,
        parent_issue: int,
        provenance: object,
    ) -> None:
        self.repository = repository
        self.node_id = node_id
        self.number = number
        self.url = url
        self.state = state
        self.parent_issue = parent_issue
        self.provenance = provenance


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
        "athena_skill_jobs": [],
        "defects": [],
        "comparisons": [],
        "artifacts": {},
        "publication": {},
    }


def _validated_run_paths(run_root: Path, run_id: str) -> tuple[Path, Path]:
    """Return the resolved run root and a strictly contained run directory."""
    separators = tuple(separator for separator in (os.sep, os.altsep, "/", "\\") if separator)
    if (
        not run_id
        or run_id in {".", ".."}
        or Path(run_id).is_absolute()
        or PurePosixPath(run_id).is_absolute()
        or any(separator in run_id for separator in separators)
        or len(PurePosixPath(run_id).parts) != 1
        or not SAFE_RUN_ID_RE.fullmatch(run_id)
    ):
        raise ValueError("run ID must be one safe path component")

    resolved_run_root = run_root.resolve()
    run_dir = resolved_run_root / run_id
    resolved_run_dir = run_dir.resolve()
    try:
        relative_run_dir = resolved_run_dir.relative_to(resolved_run_root)
    except ValueError as exc:
        raise ValueError("run ID resolves outside the configured run root") from exc
    if not relative_run_dir.parts:
        raise ValueError("run ID resolves outside the configured run root")
    return resolved_run_root, resolved_run_dir


def _validated_run_dir(run_root: Path, run_id: str) -> Path:
    """Return a contained run directory for one safe run-id path component."""
    _, run_dir = _validated_run_paths(run_root, run_id)
    return run_dir


def _resolve_run_dir(run_root: Path, run_id: str) -> Path:
    run_dir = _validated_run_dir(run_root, run_id)
    if not run_dir.exists():
        raise FileNotFoundError(f"run directory does not exist: {run_dir}")
    return run_dir


def _new_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{slugify(_random_token())}"


def _random_token() -> str:
    """Return a short random token for run identifiers and defect slugs."""
    return os.urandom(4).hex()


def _prepare_run_dir(run_root: Path, run_id: str, repo_root: Path) -> Path:
    resolved_run_root, run_dir = _validated_run_paths(run_root, run_id)
    _ensure_owner_only_dir(resolved_run_root)
    _ensure_owner_only_dir(run_dir)
    for subdir in (
        COMMANDS_DIR_NAME,
        ATHENA_SKILL_JOBS_DIR_NAME,
        DEFECTS_DIR_NAME,
        ARTIFACTS_DIR_NAME,
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


def _append_comparison_entry(run_dir: Path, entry: dict[str, Any]) -> dict[str, Any]:
    manifest = _load_manifest(run_dir)
    manifest["comparisons"].append(entry)
    _save_manifest(run_dir, manifest)
    return manifest


def _update_manifest(run_dir: Path, patch: dict[str, Any]) -> dict[str, Any]:
    manifest = _load_manifest(run_dir)
    manifest.update(patch)
    _save_manifest(run_dir, manifest)
    return manifest


def _prompt_digest(prompt: str) -> str:
    return _sha256_text(prompt)


def _fixture_digest(manifest: dict[str, Any]) -> str:
    fixture = manifest.get("fixture")
    if not isinstance(fixture, dict):
        raise ValueError("run manifest fixture is invalid")
    serialized = json.dumps(fixture, sort_keys=True, separators=(",", ":"))
    return _sha256_text(serialized)


def _latest_snapshot_revision(manifest: dict[str, Any]) -> str:
    snapshots = manifest.get("snapshots", [])
    if not isinstance(snapshots, list):
        return ""
    for snapshot in reversed(snapshots):
        if isinstance(snapshot, dict) and isinstance(snapshot.get("head"), str):
            revision = snapshot["head"].strip()
            if revision:
                return revision
    return ""


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


def _skill_mentions_from_events(events: Iterable[dict[str, Any]]) -> tuple[str, ...]:
    """Return untrusted skill-name mentions found in provider-controlled events."""
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


def _proxy_requested_skill_grants_from_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """Return requested skill command grants without treating them as invocations."""
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
) -> dict[str, Any]:
    stdout_events = _jsonl_objects(stdout)
    stderr_events = _jsonl_objects(stderr)
    all_events = stdout_events + stderr_events
    proxy_events = _proxy_events(proxy_log)
    proxy_invocations = _proxy_invocations(proxy_events)
    session_ids = _session_ids_from_events(all_events)
    provider_skill_mentions = tuple(
        dict.fromkeys(
            [
                *(_skill_mentions_from_events(all_events)),
            ]
        )
    )
    requested_skill_grants = tuple(
        dict.fromkeys(
            grant
            for invocation in proxy_invocations
            for grant in _proxy_requested_skill_grants_from_argv(invocation["argv"])
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
            ]
        )
    )
    return {
        "session_ids": list(session_ids),
        # Provider text and requested command grants are intentionally not execution
        # evidence. Actual Athena/Mnemosyne execution is proven later by host receipts.
        "observed_skill_invocations": [],
        "provider_skill_mentions": list(provider_skill_mentions),
        "requested_skill_grants": list(requested_skill_grants),
        "tool_scopes": list(tool_scopes),
        "proxy_invocations": list(proxy_invocations),
        "stdout_digest": _sha256_text(stdout),
        "stderr_digest": _sha256_text(stderr),
        "stdout_event_count": len(stdout_events),
        "stderr_event_count": len(stderr_events),
    }


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
    """Return the command version and whether its bounded probe timed out."""
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
    inventory_ready = pi_inventory.ready and not version_probe_timed_out
    inventory_status = "version_probe_timeout" if version_probe_timed_out else pi_inventory.status
    inventory_detail = pi_inventory.detail
    if version_probe_timed_out:
        inventory_detail = (
            f"Pi --version timed out after {DEFAULT_INVENTORY_TIMEOUT_SECONDS} seconds"
        )
    manifest = _load_manifest(run_dir)
    manifest["pi"] = {
        "version": version,
        "binary": shutil.which("pi") or "",
        "skill_commands": list(catalog.required_commands),
        "package_inventory": {
            "ready": inventory_ready,
            "status": inventory_status,
            "detail": inventory_detail,
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
            "status": "success" if inventory_ready else "failure",
            "returncode": 0 if inventory_ready else 1,
            "provider": "pi",
            "session_ids": [],
            "available_skill_commands": list(catalog.required_commands),
            "observed_skill_invocations": [],
            "provider_skill_mentions": [],
            "requested_skill_grants": [],
            "tool_scopes": [],
            "prompt_sha256": "",
            "stdout_digest": "",
            "stderr_digest": "",
            "started_at": _utc_now(),
            "finished_at": _utc_now(),
            "artifacts": {"inventory": str(Path(ARTIFACTS_DIR_NAME) / "inventory.json")},
        },
    )
    return 0 if inventory_ready else 1


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


def _pi_stage_session_binding(
    run_dir: Path, request: ExecutionRequest
) -> AgentSessionBinding | None:
    if request.lifecycle is not SessionLifecycle.RESUME_REQUIRED:
        return None
    manifest = _load_manifest(run_dir)
    for entry in reversed(manifest.get("commands", [])):
        if (
            isinstance(entry, dict)
            and entry.get("kind") == "capture"
            and entry.get("provider") == PI_PROVIDER_NAME
            and entry.get("stage") == "implementation"
            and entry.get("status") == "success"
        ):
            artifacts = entry.get("artifacts")
            binding_path = artifacts.get("session_binding") if isinstance(artifacts, dict) else None
            if isinstance(binding_path, str) and binding_path:
                return AgentSessionBinding.from_json(
                    (run_dir / binding_path).read_text(encoding="utf-8")
                )
    raise ValueError("Pi tests capture requires a successful implementation session binding")


def _run_pi_stage(
    run_dir: Path,
    *,
    stage: str,
    prompt: str,
    cwd: Path,
    timeout_seconds: int,
) -> tuple[AgentRunResult, ExecutionPolicy, ExecutionRequest]:
    try:
        request = PI_EVIDENCE_STAGE_REQUESTS[stage]
    except KeyError as exc:
        raise ValueError(f"unsupported Pi evidence stage: {stage!r}") from exc

    agent = resolve_agent(PI_PROVIDER_NAME, cwd=cwd)
    policy = resolve_policy(request)
    model = direct_agent_model(agent)
    if request.lifecycle is SessionLifecycle.ONE_SHOT:
        completed = run_agent_text(
            agent,
            prompt,
            cwd=cwd,
            timeout=timeout_seconds,
            model=model,
            execution_request=request,
        )
        return (
            AgentRunResult(stdout=completed.stdout or "", stderr=completed.stderr or ""),
            policy,
            request,
        )

    result = run_agent_session(
        agent,
        prompt,
        cwd=cwd,
        timeout=timeout_seconds,
        model=model,
        execution_request=request,
        resume_binding=_pi_stage_session_binding(run_dir, request),
    )
    return result, policy, request


def _pi_policy_evidence(
    policy: ExecutionPolicy,
    request: ExecutionRequest,
) -> dict[str, Any]:
    return {
        "role": policy.role.value,
        "operation": policy.operation.value,
        "lifecycle": request.lifecycle.value,
        "filesystem": policy.filesystem.value,
        "network": policy.network.value,
        "tool_scopes": sorted(policy.builtins),
        "skill_grants": sorted(f"skill:{skill.split(':', 1)[1]}" for skill in policy.skills),
    }


def _record_command(
    run_dir: Path,
    *,
    provider: str,
    stage: str,
    command_argv: Sequence[str],
    prompt: str,
    prompt_file: Path | None,
    timeout_seconds: int,
    expected_failure_probe: bool = False,
) -> int:
    if provider == PI_PROVIDER_NAME and command_argv:
        raise ValueError(
            "Pi capture rejects direct command arguments; the admitted runtime owns provider "
            "execution"
        )
    manifest = _load_manifest(run_dir)
    command_index = len(manifest["commands"]) + 1
    record_dir = _ensure_owner_only_dir(
        run_dir / COMMANDS_DIR_NAME / f"{command_index:02d}-{slugify(stage) or 'stage'}"
    )
    proxy_log = record_dir / PROXY_LOG_NAME
    stdout_path = record_dir / "stdout.txt"
    stderr_path = record_dir / "stderr.txt"
    analysis_path = record_dir / "analysis.json"
    if prompt_file is not None:
        prompt_copy = record_dir / "prompt.txt"
        write_secure(prompt_copy, prompt)
        prompt_copy.chmod(0o600)
    start = _utc_now()
    pi_result: AgentRunResult | None = None
    pi_policy: ExecutionPolicy | None = None
    pi_request: ExecutionRequest | None = None
    if provider == PI_PROVIDER_NAME:
        write_secure(proxy_log, "")
    try:
        if provider == PI_PROVIDER_NAME:
            pi_result, pi_policy, pi_request = _run_pi_stage(
                run_dir,
                stage=stage,
                prompt=prompt,
                cwd=Path(manifest["repo_root"]),
                timeout_seconds=timeout_seconds,
            )
            returncode = 0
            stdout = pi_result.stdout
            stderr = pi_result.stderr
        else:
            proxy_dir = _prepare_provider_proxy_dir(run_dir)
            env = _provider_proxy_env(proxy_dir, proxy_log)
            completed = subprocess.run(
                list(command_argv),
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
    analysis = _capture_analysis(stdout, stderr, proxy_log)
    if pi_result is not None and pi_policy is not None and pi_request is not None:
        policy_evidence = _pi_policy_evidence(pi_policy, pi_request)
        if pi_result.session_id:
            analysis["session_ids"] = list(
                dict.fromkeys([*analysis["session_ids"], pi_result.session_id])
            )
        analysis["requested_skill_grants"] = list(
            dict.fromkeys([*analysis["requested_skill_grants"], *policy_evidence["skill_grants"]])
        )
        analysis["tool_scopes"] = policy_evidence["tool_scopes"]
        analysis["execution_policy"] = {
            key: value
            for key, value in policy_evidence.items()
            if key not in {"skill_grants", "tool_scopes"}
        }
    analysis.update(
        {
            "provider": provider,
            "stage": stage,
            "command": list(command_argv),
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
    session_binding_path: Path | None = None
    if pi_result is not None and pi_result.session_binding is not None:
        session_binding_path = record_dir / "session-binding.json"
        write_secure(session_binding_path, pi_result.session_binding.to_json() + "\n")
    entry = {
        "id": f"{command_index:02d}-{slugify(stage) or 'stage'}",
        "kind": FAILURE_PROBE_KIND if expected_failure_probe else "capture",
        "provider": provider,
        "stage": stage,
        "fixture_sha256": _fixture_digest(manifest),
        "revision": _latest_snapshot_revision(manifest),
        "status": "success" if returncode == 0 else "failure",
        "returncode": returncode,
        "timeout_seconds": timeout_seconds,
        "timed_out": timed_out,
        "prompt_sha256": _prompt_digest(prompt),
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
        "started_at": start,
        "finished_at": _utc_now(),
    }
    if expected_failure_probe:
        entry.update(_expected_failure_probe_fields(returncode, timed_out))
    if session_binding_path is not None:
        entry["artifacts"]["session_binding"] = str(session_binding_path.relative_to(run_dir))
    if pi_policy is not None:
        entry["execution_policy"] = analysis["execution_policy"]
    _append_command_entry(run_dir, entry)
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return returncode


def _failure_probe_result(returncode: int, timed_out: bool) -> str:
    if timed_out:
        return "unexpected_timeout"
    if returncode == 0:
        return "unexpected_success"
    return "matched_expected_nonzero"


def _expected_failure_probe_fields(returncode: int, timed_out: bool) -> dict[str, Any]:
    matches = returncode != 0 and not timed_out
    return {
        "evidence_kind": FAILURE_PROBE_EVIDENCE_KIND,
        "status": "expected_failure" if matches else _failure_probe_result(returncode, timed_out),
        "expected_outcome": dict(EXPECTED_FAILURE_PROBE_OUTCOME),
        "observed_outcome": {
            "returncode": returncode,
            "timed_out": timed_out,
        },
        "validation": {
            "matches_expectation": matches,
            "result": _failure_probe_result(returncode, timed_out),
        },
    }


def _record_failure_probe(
    run_dir: Path,
    *,
    provider: str,
    stage: str,
    command_argv: Sequence[str],
    prompt: str,
    timeout_seconds: int,
) -> int:
    _record_command(
        run_dir,
        provider=provider,
        stage=stage,
        command_argv=command_argv,
        prompt=prompt,
        prompt_file=None,
        timeout_seconds=timeout_seconds,
        expected_failure_probe=True,
    )
    manifest = _load_manifest(run_dir)
    try:
        entry = manifest["commands"][-1]
    except (IndexError, KeyError, TypeError) as exc:
        raise RuntimeError("failure probe did not record a command entry") from exc
    if not isinstance(entry, dict):
        raise RuntimeError("failure probe did not record a valid command entry")

    returncode = entry.get("returncode")
    timed_out = entry.get("timed_out")
    if (
        not isinstance(returncode, int)
        or isinstance(returncode, bool)
        or not isinstance(timed_out, bool)
    ):
        raise RuntimeError("failure probe did not record a valid command outcome")

    if entry.get("kind") != FAILURE_PROBE_KIND:
        raise RuntimeError("failure probe was not recorded as distinct evidence")
    probe_fields = _expected_failure_probe_fields(returncode, timed_out)
    for field, expected in probe_fields.items():
        if entry.get(field) != expected:
            raise RuntimeError("failure probe did not persist its expected outcome")
    if not probe_fields["validation"]["matches_expectation"]:
        print(f"error: failure probe {probe_fields['validation']['result']}", file=sys.stderr)
        return 1
    return 0


def _capture_entry_by_id(manifest: dict[str, Any], entry_id: str) -> dict[str, Any]:
    matches = [
        entry
        for entry in manifest.get("commands", [])
        if isinstance(entry, dict)
        and entry.get("kind") in {"capture", FAILURE_PROBE_KIND}
        and entry.get("id") == entry_id
    ]
    if len(matches) != 1:
        raise ValueError(f"comparison entry must identify one provider run: {entry_id}")
    return cast(dict[str, Any], matches[0])


def _capture_artifact_path(run_dir: Path, entry: dict[str, Any], stream: str) -> Path:
    """Resolve one capture-owned artifact and reject pooled artifact paths."""
    artifacts = entry.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError(f"capture {entry.get('id', 'unknown')} has no artifact manifest")
    artifact_value = artifacts.get(stream)
    if not isinstance(artifact_value, str) or not artifact_value:
        raise ValueError(f"capture {entry.get('id', 'unknown')} lacks a {stream} artifact")
    artifact = PurePosixPath(artifact_value)
    if artifact.is_absolute() or ".." in artifact.parts or not artifact.parts:
        raise ValueError(f"capture {entry.get('id', 'unknown')} {stream} artifact escapes run")
    entry_id = entry.get("id")
    if (
        not isinstance(entry_id, str)
        or not entry_id
        or artifact.parent != PurePosixPath(COMMANDS_DIR_NAME) / entry_id
    ):
        raise ValueError(
            f"capture {entry.get('id', 'unknown')} {stream} artifact is not capture-owned"
        )

    artifact_path = run_dir
    for part in artifact.parts:
        artifact_path /= part
        if artifact_path.is_symlink():
            raise ValueError(
                f"capture {entry.get('id', 'unknown')} {stream} artifact path uses a symlink"
            )
    try:
        artifact_path.resolve(strict=True).relative_to(run_dir.resolve(strict=True))
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(
            f"capture {entry.get('id', 'unknown')} {stream} artifact is unavailable"
        ) from exc
    if not artifact_path.is_file():
        raise ValueError(f"capture {entry.get('id', 'unknown')} {stream} artifact is invalid")
    return artifact_path


def _capture_artifact_sha256(run_dir: Path, entry: dict[str, Any], stream: str) -> str:
    artifact_path = _capture_artifact_path(run_dir, entry, stream)

    return _sha256_bytes(artifact_path.read_bytes())


def _capture_analysis_observation(
    run_dir: Path,
    entry: dict[str, Any],
    *,
    observed_stage: str,
) -> tuple[dict[str, Any], str]:
    """Load the collector-written provider observation for one exact capture.

    Manifest fields are convenient render inputs, but they are not independent
    provider evidence.  Completion therefore replays the private analysis
    artifact owned by this capture and requires every consumed mirror to match.
    """
    analysis_path = _capture_artifact_path(run_dir, entry, "analysis")
    analysis_sha256 = _sha256_bytes(analysis_path.read_bytes())
    try:
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Pi capture analysis is not valid JSON") from exc
    if not isinstance(analysis, dict):
        raise ValueError("Pi capture analysis must contain a JSON object")
    observation = cast(dict[str, Any], analysis)
    mirrored_fields = {
        "provider": "provider",
        "stage": "stage",
        "returncode": "returncode",
        "timed_out": "timed_out",
        "prompt_sha256": "prompt_sha256",
        "session_ids": "session_ids",
        "requested_skill_grants": "requested_skill_grants",
        "tool_scopes": "tool_scopes",
        "stdout_digest": "stdout_digest",
        "stderr_digest": "stderr_digest",
        "started_at": "started_at",
        "finished_at": "finished_at",
    }
    for analysis_field, entry_field in mirrored_fields.items():
        if observation.get(analysis_field) != entry.get(entry_field):
            raise ValueError(
                f"Pi capture analysis {analysis_field} is not bound to its manifest entry"
            )
    if observation.get("provider") != PI_PROVIDER_NAME:
        raise ValueError("Pi capture analysis does not contain Pi provider evidence")
    if observation.get("stage") != observed_stage:
        raise ValueError("caller stage label does not identify the coordinator-observed stage")
    expected_outcome = "success" if entry.get("returncode") == 0 else "failure"
    if observation.get("outcome") != expected_outcome:
        raise ValueError("Pi capture analysis has an invalid provider outcome")
    return observation, analysis_sha256


def _observed_provider_invocation_id(
    entry: dict[str, Any],
    analysis_sha256: str,
) -> str:
    """Derive a capture-specific provider invocation identity from host observations."""
    evidence = {
        "capture_id": entry.get("id"),
        "analysis_sha256": analysis_sha256,
        "prompt_sha256": entry.get("prompt_sha256"),
        "stdout_sha256": entry.get("stdout_digest"),
        "stderr_sha256": entry.get("stderr_digest"),
        "started_at": entry.get("started_at"),
        "finished_at": entry.get("finished_at"),
    }
    return _sha256_text(json.dumps(evidence, sort_keys=True, separators=(",", ":")))


def _capture_outcome(run_dir: Path, entry: dict[str, Any]) -> dict[str, Any]:
    kind = entry.get("kind")
    status = entry.get("status")
    returncode = entry.get("returncode")
    timed_out = entry.get("timed_out")
    stdout_digest = entry.get("stdout_digest")
    stderr_digest = entry.get("stderr_digest")
    if kind not in {"capture", FAILURE_PROBE_KIND}:
        raise ValueError(f"capture {entry.get('id', 'unknown')} has an invalid kind")
    if kind == "capture" and status not in {"success", "failure"}:
        raise ValueError(f"capture {entry.get('id', 'unknown')} has an invalid status")
    if not isinstance(returncode, int) or isinstance(returncode, bool):
        raise ValueError(f"capture {entry.get('id', 'unknown')} has no return code")
    if not isinstance(timed_out, bool):
        raise ValueError(f"capture {entry.get('id', 'unknown')} has no timeout outcome")
    expected_outcome: dict[str, Any] | None = None
    if kind == FAILURE_PROBE_KIND:
        expected_fields = _expected_failure_probe_fields(returncode, timed_out)
        for field in (
            "evidence_kind",
            "status",
            "expected_outcome",
            "observed_outcome",
            "validation",
        ):
            if entry.get(field) != expected_fields[field]:
                raise ValueError(
                    f"capture {entry.get('id', 'unknown')} has an invalid expected-failure outcome"
                )
        if expected_fields["validation"]["matches_expectation"] is not True:
            raise ValueError(
                f"capture {entry.get('id', 'unknown')} did not observe its expected failure"
            )
        expected_outcome = cast(dict[str, Any], expected_fields["expected_outcome"])
    for stream, digest in (("stdout", stdout_digest), ("stderr", stderr_digest)):
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError(
                f"capture {entry.get('id', 'unknown')} has no valid {stream} artifact digest"
            )
        artifact_digest = _capture_artifact_sha256(run_dir, entry, stream)
        if artifact_digest != digest:
            raise ValueError(
                f"capture {entry.get('id', 'unknown')} {stream} artifact digest mismatch"
            )
    outcome = {
        "kind": kind,
        "status": status,
        "returncode": returncode,
        "timed_out": timed_out,
        "stdout_sha256": stdout_digest,
        "stderr_sha256": stderr_digest,
    }
    if expected_outcome is not None:
        outcome["expected_outcome"] = expected_outcome
        outcome["matches_expectation"] = True
    return outcome


def _comparison_payload(
    manifest: dict[str, Any],
    run_dir: Path,
    *,
    pi_entry_id: str,
    control_entry_id: str,
) -> dict[str, Any]:
    pi_entry = _capture_entry_by_id(manifest, pi_entry_id)
    control_entry = _capture_entry_by_id(manifest, control_entry_id)
    if pi_entry.get("provider") != PI_PROVIDER_NAME:
        raise ValueError("comparison Pi entry is not a Pi capture")
    control_provider = control_entry.get("provider")
    if control_provider != CONTROL_PROVIDER_NAME:
        raise ValueError(f"comparison control entry must use the {CONTROL_PROVIDER_NAME} provider")

    entry_kind = pi_entry.get("kind")
    if entry_kind not in {"capture", FAILURE_PROBE_KIND} or control_entry.get("kind") != entry_kind:
        raise ValueError("comparison runs must use the same evidence kind")

    fixture_sha256 = _fixture_digest(manifest)
    if {
        pi_entry.get("fixture_sha256"),
        control_entry.get("fixture_sha256"),
    } != {fixture_sha256}:
        raise ValueError("comparison captures do not target the manifest fixture")

    stage = pi_entry.get("stage")
    if not isinstance(stage, str) or not stage or control_entry.get("stage") != stage:
        raise ValueError("comparison captures must target the same stage")
    revision = pi_entry.get("revision")
    snapshot_revisions = {
        snapshot.get("head")
        for snapshot in manifest.get("snapshots", [])
        if isinstance(snapshot, dict) and isinstance(snapshot.get("head"), str)
    }
    if (
        not isinstance(revision, str)
        or not revision
        or control_entry.get("revision") != revision
        or revision not in snapshot_revisions
    ):
        raise ValueError("comparison captures must target the same recorded revision")
    prompt_sha256 = pi_entry.get("prompt_sha256")
    if (
        not isinstance(prompt_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", prompt_sha256) is None
        or control_entry.get("prompt_sha256") != prompt_sha256
    ):
        raise ValueError("comparison captures must use the same fixture prompt")

    pi_outcome = _capture_outcome(run_dir, pi_entry)
    control_outcome = _capture_outcome(run_dir, control_entry)
    if entry_kind == FAILURE_PROBE_KIND:
        outcomes_match = (
            pi_outcome.get("matches_expectation") is True
            and control_outcome.get("matches_expectation") is True
            and pi_outcome.get("expected_outcome") == control_outcome.get("expected_outcome")
        )
    else:
        outcomes_match = (
            pi_outcome["status"] == control_outcome["status"]
            and pi_outcome["returncode"] == control_outcome["returncode"]
            and pi_outcome["timed_out"] == control_outcome["timed_out"]
        )
    return {
        "fixture_sha256": fixture_sha256,
        "stage": stage,
        "revision": revision,
        "prompt_sha256": prompt_sha256,
        "comparison_basis": (
            "artifacts"
            if pi_outcome["status"] == control_outcome["status"] == "success"
            else "failure_behavior"
        ),
        "pi_entry_id": pi_entry_id,
        "control_entry_id": control_entry_id,
        "pi_provider": PI_PROVIDER_NAME,
        "control_provider": control_provider,
        "pi_outcome": pi_outcome,
        "control_outcome": control_outcome,
        "outcomes_match": outcomes_match,
        "artifact_comparison": {
            "stdout_matches": (pi_outcome["stdout_sha256"] == control_outcome["stdout_sha256"]),
            "stderr_matches": (pi_outcome["stderr_sha256"] == control_outcome["stderr_sha256"]),
        },
    }


def _record_comparison(
    run_dir: Path,
    *,
    pi_entry_id: str,
    control_entry_id: str,
) -> int:
    manifest = _load_manifest(run_dir)
    payload = _comparison_payload(
        manifest,
        run_dir,
        pi_entry_id=pi_entry_id,
        control_entry_id=control_entry_id,
    )
    for comparison in manifest.get("comparisons", []):
        if not isinstance(comparison, dict):
            raise ValueError("run manifest contains an invalid comparison record")
        if (
            comparison.get("pi_entry_id") == pi_entry_id
            and comparison.get("control_entry_id") == control_entry_id
        ):
            for key, expected in payload.items():
                if comparison.get(key) != expected:
                    raise ValueError("existing comparison record does not match its captures")
            return 0
    _append_comparison_entry(
        run_dir,
        {
            "id": f"comparison-{len(manifest.get('comparisons', [])) + 1:02d}",
            **payload,
            "recorded_at": _utc_now(),
        },
    )
    return 0


def _receipt_payload_from_path(path: Path, *, label: str, size_limit: int) -> dict[str, Any]:
    """Load one bounded host-owned JSON receipt without following a final symlink."""
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular, non-symlink file")
    content = path.read_bytes()
    if not content or len(content) > size_limit:
        raise ValueError(f"{label} size is invalid")
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return cast(dict[str, Any], payload)


def _persist_receipt_payload(
    run_dir: Path,
    entry_id: str,
    filename: str,
    payload: dict[str, Any],
    *,
    collection_dir: str = COMMANDS_DIR_NAME,
) -> dict[str, str]:
    relative_path = Path(collection_dir) / entry_id / filename
    receipt_path = run_dir / relative_path
    _ensure_owner_only_dir(receipt_path.parent)
    _write_json(receipt_path, payload)
    return {
        "artifact": relative_path.as_posix(),
        "sha256": _sha256_bytes(receipt_path.read_bytes()),
    }


def _record_stage_receipt(
    run_dir: Path,
    *,
    capture_id: str,
    receipt_path: Path,
) -> int:
    """Ingest a coordinator receipt and bind it to one exact Pi capture."""
    manifest = _load_manifest(run_dir)
    entry = _capture_entry_by_id(manifest, capture_id)
    if entry.get("provider") != PI_PROVIDER_NAME:
        raise ValueError("stage receipts may be attached only to Pi captures")
    if "stage_receipt" in entry:
        raise ValueError("capture already has persisted coordinator receipt evidence")

    stage_payload = _receipt_payload_from_path(
        receipt_path,
        label="Pi stage coordinator receipt",
        size_limit=MAX_STAGE_RECEIPT_BYTES,
    )
    entry["stage_receipt"] = _persist_receipt_payload(
        run_dir,
        capture_id,
        "stage-receipt.json",
        stage_payload,
    )
    # Validate before making the descriptor durable in the manifest. The
    # persisted artifact remains private and can be replaced by a corrected retry.
    _verify_pi_stage_receipt(manifest, run_dir, entry)
    _save_manifest(run_dir, manifest)
    return 0


def _record_athena_host_receipt(
    run_dir: Path,
    *,
    kind: str,
    correlated_capture_id: str,
    receipt_path: Path,
) -> int:
    """Persist one independent AthenaSkillJob result correlated to a Pi agent job."""
    manifest = _load_manifest(run_dir)
    entry = _capture_entry_by_id(manifest, correlated_capture_id)
    stage = entry.get("stage")
    expected = ATHENA_SKILL_JOB_CORRELATIONS.get(stage) if isinstance(stage, str) else None
    if (
        entry.get("kind") != "capture"
        or entry.get("provider") != PI_PROVIDER_NAME
        or entry.get("status") != "success"
        or expected is None
        or expected[0] != kind
    ):
        raise ValueError(
            f"Athena {kind} receipt must correlate to a successful Pi-backed agent job"
        )
    host_jobs = manifest.get("athena_skill_jobs")
    if not isinstance(host_jobs, list):
        raise ValueError("manifest has invalid AthenaSkillJob receipts")
    if any(
        isinstance(job, dict)
        and job.get("kind") == kind
        and job.get("correlated_capture_id") == correlated_capture_id
        for job in host_jobs
    ):
        raise ValueError(f"Pi-backed job already has an Athena {kind} host receipt")

    payload = _receipt_payload_from_path(
        receipt_path,
        label=f"Athena {kind} host receipt",
        size_limit=MAX_ATHENA_HOST_RECEIPT_BYTES,
    )
    receipt_id = f"athena-skill-{len(host_jobs) + 1:02d}"
    host_job = {
        "id": receipt_id,
        "job_kind": "AthenaSkillJob",
        "kind": kind,
        "correlated_capture_id": correlated_capture_id,
        "correlated_stage": stage,
        "correlated_role": expected[1],
        "result": _persist_receipt_payload(
            run_dir,
            receipt_id,
            f"athena-{kind}-host-receipt.json",
            payload,
            collection_dir=ATHENA_SKILL_JOBS_DIR_NAME,
        ),
        "recorded_at": _utc_now(),
    }
    _verify_host_athena_receipt(run_dir, host_job, expected_kind=kind)
    _verify_athena_skill_job_correlation(manifest, host_job)
    host_jobs.append(host_job)
    _save_manifest(run_dir, manifest)
    return 0


def _record_defect(
    run_dir: Path,
    *,
    repo_root: Path,
    summary: str,
    follow_up_issue: int,
    details: str,
    source_entry: str = "",
) -> int:
    manifest = _load_manifest(run_dir)
    defects = manifest.get("defects")
    if not isinstance(defects, list):
        raise ValueError("run manifest defects are invalid")
    for entry in defects:
        if not isinstance(entry, dict):
            raise ValueError("run manifest contains an invalid defect record")
        if entry.get("follow_up_issue") == follow_up_issue:
            raise ValueError(f"duplicate follow-up issue #{follow_up_issue}")

    follow_up_receipt = _resolve_follow_up_issue_identity(repo_root, follow_up_issue)
    follow_up_identity = _follow_up_identity_from_live_receipt(follow_up_receipt)
    defect_id = (
        f"{_utc_now().replace(':', '').replace('-', '')}-{slugify(summary)[:48] or 'defect'}"
    )
    defect_path = run_dir / DEFECTS_DIR_NAME / f"{defect_id}.json"
    record = {
        "id": defect_id,
        "summary": summary,
        "follow_up_issue": follow_up_issue,
        "follow_up_issue_identity": follow_up_identity,
        "details": details,
        "source_entry": source_entry,
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


def _resolve_follow_up_issue_identity(
    repo_root: Path,
    issue_number: int,
) -> _LiveFollowUpIssueReceipt:
    """Resolve one open #2519 follow-up through the repository-scoped host seam."""
    owner, name = PROJECT_REPOSITORY.split("/", 1)
    try:
        github = PipelineGitHub(owner, repo=name, repo_root=repo_root)
        read_issue = github.gh_issue_json
    except (
        AttributeError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        TypeError,
        ValueError,
    ) as exc:
        raise ValueError(
            f"scoped GitHub host seam is unavailable for follow-up issue #{issue_number}"
        ) from exc
    identities: list[dict[str, Any]] = []
    for _read in range(2):
        try:
            issue = read_issue(issue_number)
        except (
            AttributeError,
            OSError,
            RuntimeError,
            subprocess.SubprocessError,
            TypeError,
            ValueError,
        ) as exc:
            raise ValueError(
                f"could not resolve follow-up issue #{issue_number} in {PROJECT_REPOSITORY}"
            ) from exc
        identities.append(_validated_follow_up_issue_identity(issue, issue_number))
    if identities[0] != identities[1]:
        raise ValueError(f"follow-up issue #{issue_number} live identity did not stabilize")
    return _provenance_bound_live_follow_up_receipt(identities[1])


def _validated_follow_up_issue_identity(
    issue: object,
    issue_number: int,
) -> dict[str, Any]:
    """Validate exact repository identity, open state, and #2519 linkage."""
    expected_url = f"https://github.com/{PROJECT_REPOSITORY}/issues/{issue_number}"
    if (
        not isinstance(issue, dict)
        or issue.get("number") != issue_number
        or issue.get("url") != expected_url
        or not isinstance(issue.get("id"), str)
        or not issue["id"]
    ):
        raise ValueError(f"follow-up issue #{issue_number} has mismatched GitHub identity")
    if issue.get("state") != "OPEN":
        raise ValueError(f"follow-up issue #{issue_number} must be open")
    body = issue.get("body")
    if not isinstance(body, str) or FOLLOW_UP_PARENT_LINE_RE.search(body) is None:
        raise ValueError(f"follow-up issue #{issue_number} must link to #{ISSUE_NUMBER}")
    return {
        "repository": PROJECT_REPOSITORY,
        "node_id": issue["id"],
        "number": issue_number,
        "url": expected_url,
        "state": "OPEN",
        "parent_issue": ISSUE_NUMBER,
    }


def _provenance_bound_live_follow_up_receipt(
    value: object,
) -> _LiveFollowUpIssueReceipt:
    """Convert one complete host issue readback into non-JSON process-local proof."""
    required_fields = {
        "repository",
        "node_id",
        "number",
        "url",
        "state",
        "parent_issue",
    }
    if not isinstance(value, dict) or set(value) != required_fields:
        raise ValueError("host-derived follow-up issue readback has an unsupported schema")
    identity = cast(dict[str, Any], value)
    issue_number = identity.get("number")
    node_id = identity.get("node_id")
    if (
        identity.get("repository") != PROJECT_REPOSITORY
        or isinstance(issue_number, bool)
        or not isinstance(issue_number, int)
        or issue_number <= 0
        or not isinstance(node_id, str)
        or not node_id
        or identity.get("url") != f"https://github.com/{PROJECT_REPOSITORY}/issues/{issue_number}"
        or identity.get("state") != "OPEN"
        or identity.get("parent_issue") != ISSUE_NUMBER
    ):
        raise ValueError("host-derived follow-up issue readback is incomplete")
    return _LiveFollowUpIssueReceipt(
        repository=PROJECT_REPOSITORY,
        node_id=node_id,
        number=issue_number,
        url=cast(str, identity["url"]),
        state="OPEN",
        parent_issue=ISSUE_NUMBER,
        provenance=_LIVE_FOLLOW_UP_PROVENANCE,
    )


def _follow_up_identity_from_live_receipt(
    receipt: object,
) -> dict[str, Any]:
    """Return serializable identity only from an authentic process-local receipt."""
    if (
        not isinstance(receipt, _LiveFollowUpIssueReceipt)
        or receipt.provenance is not _LIVE_FOLLOW_UP_PROVENANCE
    ):
        raise ValueError("follow-up issue lacks a host-derived live readback receipt")
    return {
        "repository": receipt.repository,
        "node_id": receipt.node_id,
        "number": receipt.number,
        "url": receipt.url,
        "state": receipt.state,
        "parent_issue": receipt.parent_issue,
    }


def _verify_defect_follow_ups(
    manifest: dict[str, Any],
    repo_root: Path,
) -> list[dict[str, Any]]:
    """Rebind every persisted defect to a unique current GitHub follow-up."""
    defects = manifest.get("defects")
    if not isinstance(defects, list):
        raise ValueError("run manifest defects are invalid")
    verified: list[dict[str, Any]] = []
    seen_numbers: set[int] = set()
    seen_urls: set[str] = set()
    seen_node_ids: set[str] = set()
    for entry in defects:
        if not isinstance(entry, dict):
            raise ValueError("run manifest contains an invalid defect record")
        issue_number = entry.get("follow_up_issue")
        if isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number <= 0:
            raise ValueError("one or more defects lacks a valid follow-up issue")
        if issue_number in seen_numbers:
            raise ValueError(f"duplicate follow-up issue #{issue_number}")
        live_receipt = _resolve_follow_up_issue_identity(repo_root, issue_number)
        live_identity = _follow_up_identity_from_live_receipt(live_receipt)
        stored_identity = entry.get("follow_up_issue_identity")
        if stored_identity != live_identity:
            raise ValueError(
                f"follow-up issue #{issue_number} identity or live state/linkage is stale"
            )
        issue_url = live_identity["url"]
        node_id = live_identity["node_id"]
        if issue_url in seen_urls:
            raise ValueError(f"duplicate follow-up issue URL: {issue_url}")
        if node_id in seen_node_ids:
            raise ValueError(f"duplicate follow-up issue identity: {node_id}")
        seen_numbers.add(issue_number)
        seen_urls.add(issue_url)
        seen_node_ids.add(node_id)
        verified.append(live_identity)
    return verified


def _render_report(
    manifest: dict[str, Any],
    run_dir: Path,
    report_path: Path,
    runbook_path: Path,
) -> str:
    pi = cast(dict[str, Any], manifest.get("pi", {}))
    inventory = cast(dict[str, Any], pi.get("package_inventory", {}))
    commands = [entry for entry in manifest.get("commands", []) if entry.get("kind") == "capture"]
    defects = cast(list[dict[str, Any]], manifest.get("defects", []))
    snapshots = cast(list[dict[str, Any]], manifest.get("snapshots", []))
    skill_commands = ", ".join(f"`{skill}`" for skill in pi.get("skill_commands", [])) or "n/a"
    evidence_status = _evidence_status(manifest, run_dir)
    lines = [
        "# Pi Issue 2519 Report",
        "",
        f"- Evidence status: `{evidence_status}`",
        f"- Fixture: `{manifest['fixture']['title']}`",
        f"- Run ID: `{manifest['run_id']}`",
        f"- Created: `{manifest['created_at']}`",
        f"- Pi version: `{pi.get('version', '')}`",
        "- Pi binary: recorded privately in the run manifest",
        f"- Skill commands: {skill_commands}",
        f"- Inventory status: `{inventory.get('status', '')}`",
        f"- Inventory ready: `{inventory.get('ready', False)}`",
        "",
    ]
    if evidence_status != "complete":
        lines.extend(
            [
                "## Verification Outcome",
                "",
                "This is an incomplete, unverified partial capture, not an end-to-end Pi "
                "workflow attestation. It is not closure evidence for #2519.",
                "",
                "The only captured Pi command failed during planning. No isolated Pi "
                "worktree, repository snapshot, successful test run, commit/PR creation, "
                "review, or handoff evidence has been recorded.",
                "",
                "Missing required acceptance evidence:",
                "",
                "- A repository snapshot bound to the Pi run.",
                "- Successful isolated Pi planning, implementation, tests, commit/PR creation, "
                "and review stage receipts, including host-owned advice and learning handoff.",
                "- Pi/control comparison evidence for the same fixture, prompt, recorded "
                "revision, and persisted success artifacts or failure behavior.",
                "- Typed Mnemosyne advise/learn host receipts correlated to the relevant Pi "
                "planning and review jobs.",
                "- Publication attestation for the rendered report and runbook.",
                "",
                "Host receipts or a control-provider run do not substitute for the missing "
                "isolated Pi workflow evidence.",
                "",
            ]
        )
    lines.extend(
        [
            "## Captured Commands",
            "",
        ]
    )
    if commands:
        lines.extend(
            [
                (
                    "| Stage | Provider | Status | Returncode | Session evidence | "
                    "Tool scopes | Requested skill grants | Provider skill mentions |"
                ),
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        has_unverified_control = False
        for entry in commands:
            status = str(entry.get("status", ""))
            returncode = str(entry.get("returncode", ""))
            if (
                entry.get("stage") == "control"
                and entry.get("status") == "success"
                and not entry.get("session_ids")
            ):
                status = "unverified / unproven"
                returncode = f"claimed `{returncode}` (private manifest only)"
                has_unverified_control = True
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(entry.get("stage", "")),
                        str(entry.get("provider", "")),
                        status,
                        returncode,
                        ", ".join(f"`{session_id}`" for session_id in entry.get("session_ids", []))
                        or "none",
                        ", ".join(entry.get("tool_scopes", [])) or "n/a",
                        ", ".join(entry.get("requested_skill_grants", [])) or "n/a",
                        ", ".join(entry.get("provider_skill_mentions", [])) or "n/a",
                    ]
                )
                + " |"
            )
        if has_unverified_control:
            lines.extend(
                [
                    "",
                    "The Codex control result is unverified: no committed, report-bound control "
                    "transcript exists for independent re-execution. The available host receipts "
                    "cover linting, type checking, and unit tests only; they do not establish this "
                    "Codex invocation. A committed transcript bound to this report and "
                    "reproducible by an independent reviewer is required before this row can be "
                    "marked successful.",
                ]
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
            f"- Evidence status: `{_evidence_status(manifest, run_dir)}`",
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
            "4. Capture Pi prompts through the admitted runtime or controls through the "
            "temporary provider proxy.",
            (
                "5. Export and attach each coordinator-owned stage receipt, including its "
                "capture-bound provider-analysis digest, distinct linked-worktree receipt, "
                "exact revision, stage-specific lifecycle, live GitHub PR readback where "
                "applicable, and final review-to-finished handoff receipt."
            ),
            (
                "6. Attach typed host-owned AthenaSkillJob advise/learn results independently "
                "and correlate them to the Pi planner, implementer, and reviewer captures."
            ),
            "7. Capture any failure probes that demonstrate the stage boundary.",
            "8. Record defects as follow-up issues.",
            "9. Render the report and runbook from the manifest.",
            "10. Attest publication readiness.",
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
                "--stage <stage> --provider pi --prompt-file <prompt-file>"
            ),
            (
                f"uv run python scripts/{script_name} capture --run-id <run-id> "
                "--stage <stage> --provider codex -- <command...>"
            ),
            (
                f"uv run python scripts/{script_name} failure-probe --run-id <run-id> "
                "--stage <stage> --provider codex -- <command...>"
            ),
            (
                f"uv run python scripts/{script_name} record-comparison --run-id <run-id> "
                "--pi-entry <pi-entry-id> --control-entry <control-entry-id>"
            ),
            (
                f"uv run python scripts/{script_name} record-stage-receipt --run-id <run-id> "
                "--capture-id <pi-entry-id> --receipt <coordinator-receipt.json>"
            ),
            (
                f"uv run python scripts/{script_name} record-athena-host-receipt "
                "--run-id <run-id> --kind <advise|learn> "
                "--correlated-capture-id <pi-entry-id> --receipt <athena-result.json>"
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
                "--repo HomericIntelligence/Hephaestus --ref <commit-sha> --verify-defects"
            ),
            "```",
            "",
            "## Verification Criteria",
            "",
            "- `fixture` validates the deterministic fixture contract.",
            "- `workflow` requires inventory, command, and repository snapshot evidence.",
            (
                "- `capture` requires each Pi stage's own host-observed provider invocation, "
                "session binding when stateful, and exact policy tool scopes."
            ),
            (
                "- `comparison` requires a persisted Pi/control pair for the same fixture, "
                "stage, prompt, and recorded revision, with matching outcomes plus "
                "matching success artifact digests or matching failure behavior."
            ),
            (
                "- `mnemosyne` requires typed host-owned Athena advise and learning receipts "
                "correlated to Pi planner, implementer, and reviewer jobs, including Mnemosyne "
                "PR readback."
            ),
            "- `publication` requires deterministic manifest-to-document rendering.",
            (
                "- `completion` requires a distinct coordinator receipt for every Pi stage, "
                "per-stage isolated worktree/revision evidence, fixture test and exact-head "
                "GitHub lifecycle readbacks, every expected failure probe, and all criteria above."
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
    report_text = _render_report(manifest, run_dir, report_path, runbook_path)
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
    if not any(entry.get("session_ids") for entry in capture_entries):
        raise ValueError("no captured command emitted a session id")
    if not any(entry.get("tool_scopes") for entry in capture_entries):
        raise ValueError("no captured command recorded tool scopes")


def _verify_failure_probes(manifest: dict[str, Any]) -> None:
    """Require every recorded failure probe to have observed its expected nonzero result."""
    for entry in manifest.get("commands", []):
        if not isinstance(entry, dict) or entry.get("kind") != FAILURE_PROBE_KIND:
            continue
        returncode = entry.get("returncode")
        timed_out = entry.get("timed_out")
        if (
            entry.get("evidence_kind") != FAILURE_PROBE_EVIDENCE_KIND
            or not isinstance(returncode, int)
            or isinstance(returncode, bool)
            or not isinstance(timed_out, bool)
        ):
            raise ValueError("failure probe has an invalid observed outcome")
        expected_fields = _expected_failure_probe_fields(returncode, timed_out)
        for field in ("expected_outcome", "observed_outcome", "validation"):
            if entry.get(field) != expected_fields[field]:
                raise ValueError("failure probe has an invalid expected-outcome record")
        if entry.get("status") != expected_fields["status"]:
            raise ValueError("failure probe status does not match its observed outcome")
        if expected_fields["validation"]["matches_expectation"] is not True:
            raise ValueError("failure probe did not match expected nonzero outcome")


def _verify_expected_failure_comparison(manifest: dict[str, Any]) -> None:
    """Require a validated Pi/control comparison of expected nonzero probes."""
    for comparison in manifest.get("comparisons", []):
        if not isinstance(comparison, dict) or comparison.get("outcomes_match") is not True:
            continue
        pi_outcome = comparison.get("pi_outcome")
        control_outcome = comparison.get("control_outcome")
        if (
            isinstance(pi_outcome, dict)
            and isinstance(control_outcome, dict)
            and pi_outcome.get("kind") == FAILURE_PROBE_KIND
            and control_outcome.get("kind") == FAILURE_PROBE_KIND
            and pi_outcome.get("matches_expectation") is True
            and control_outcome.get("matches_expectation") is True
        ):
            return
    raise ValueError("completion requires a paired expected failure probe comparison")


def _verify_comparison(manifest: dict[str, Any], run_dir: Path) -> None:
    comparisons = manifest.get("comparisons", [])
    if not isinstance(comparisons, list) or not comparisons:
        raise ValueError("comparison requires a persisted Pi/control pair")
    seen_pairs: set[tuple[str, str]] = set()
    for comparison in comparisons:
        if not isinstance(comparison, dict):
            raise ValueError("run manifest contains an invalid comparison record")
        pi_entry_id = comparison.get("pi_entry_id")
        control_entry_id = comparison.get("control_entry_id")
        if not isinstance(pi_entry_id, str) or not isinstance(control_entry_id, str):
            raise ValueError("comparison record does not identify both captures")
        pair = (pi_entry_id, control_entry_id)
        if pair in seen_pairs:
            raise ValueError("comparison record pair is duplicated")
        seen_pairs.add(pair)
        expected = _comparison_payload(
            manifest,
            run_dir,
            pi_entry_id=pi_entry_id,
            control_entry_id=control_entry_id,
        )
        for key, value in expected.items():
            if comparison.get(key) != value:
                raise ValueError("comparison record does not match its captured outcomes")
        if comparison.get("outcomes_match") is not True:
            raise ValueError("paired Pi and control outcomes do not match")
        if comparison.get("comparison_basis") == "artifacts" and comparison.get(
            "artifact_comparison"
        ) != {"stdout_matches": True, "stderr_matches": True}:
            raise ValueError("successful Pi/control comparison artifacts do not match")


def _receipt_dict(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Athena host receipt has invalid {field}")
    return cast(dict[str, Any], value)


def _receipt_string(data: dict[str, Any], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Athena host receipt lacks non-empty {field}")
    return value


def _receipt_sha(data: dict[str, Any], field: str, length: int) -> str:
    value = _receipt_string(data, field)
    if re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is None:
        raise ValueError(f"Athena host receipt has invalid {field}")
    return value


def _load_private_receipt(
    run_dir: Path,
    descriptor_value: object,
    *,
    label: str,
    size_limit: int,
) -> dict[str, Any]:
    descriptor = _receipt_dict(descriptor_value, f"{label} artifact descriptor")
    if set(descriptor) != {"artifact", "sha256"}:
        raise ValueError(f"{label} artifact descriptor is not canonical")
    artifact = PurePosixPath(_receipt_string(descriptor, "artifact"))
    if artifact.is_absolute() or ".." in artifact.parts or not artifact.parts:
        raise ValueError(f"{label} artifact path escapes the run directory")

    receipt_path = run_dir
    for part in artifact.parts:
        receipt_path /= part
        if receipt_path.is_symlink():
            raise ValueError(f"{label} artifact path must not contain symlinks")
    try:
        receipt_path.resolve(strict=True).relative_to(run_dir.resolve(strict=True))
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(f"{label} artifact is unavailable") from exc
    if not receipt_path.is_file():
        raise ValueError(f"{label} artifact is not a regular file")

    content = receipt_path.read_bytes()
    if not content or len(content) > size_limit:
        raise ValueError(f"{label} artifact size is invalid")
    expected_digest = _receipt_sha(descriptor, "sha256", 64)
    if _sha256_bytes(content) != expected_digest:
        raise ValueError(f"{label} artifact digest does not match")
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} artifact is not valid JSON") from exc
    return _receipt_dict(payload, f"{label} artifact payload")


def _load_athena_host_receipt(
    run_dir: Path,
    host_job: dict[str, Any],
) -> dict[str, Any]:
    return _load_private_receipt(
        run_dir,
        host_job.get("result"),
        label=f"Athena {host_job.get('kind', '')} host receipt",
        size_limit=MAX_ATHENA_HOST_RECEIPT_BYTES,
    )


def _verify_contract_and_binding(
    result_receipt: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    contract = _receipt_dict(result_receipt.get("contract"), "Athena contract")
    binding = _receipt_dict(result_receipt.get("binding"), "Mnemosyne binding")
    _receipt_string(contract, "athena_repository")
    _receipt_sha(contract, "athena_commit", 40)
    for field in ("advise_sha256", "learn_sha256", "dependency_resolution_sha256"):
        _receipt_sha(contract, field, 64)
    _receipt_string(contract, "trust_source")
    _receipt_string(binding, "root")
    _receipt_string(binding, "repository")
    _receipt_string(binding, "default_branch")
    _receipt_sha(binding, "commit_sha", 40)
    _receipt_string(binding, "trust_basis")
    if binding.get("athena_contract") != contract:
        raise ValueError("Mnemosyne binding is not bound to the Athena contract")
    return contract, binding


def _verify_advise_receipt(
    host_result: dict[str, Any],
    result_receipt: dict[str, Any],
    contract: dict[str, Any],
    binding: dict[str, Any],
) -> None:
    if host_result.get("delivery_receipt") is not None:
        raise ValueError("Athena advise receipt unexpectedly includes learning delivery")
    corpus = _receipt_dict(result_receipt.get("corpus"), "Mnemosyne corpus")
    if (
        corpus.get("repository") != binding["repository"]
        or corpus.get("commit_sha") != binding["commit_sha"]
        or corpus.get("athena_contract") != contract
    ):
        raise ValueError("Mnemosyne corpus receipt is not bound to its checkout")
    selected_paths = corpus.get("selected_paths")
    entry_count = corpus.get("entry_count")
    if (
        not isinstance(selected_paths, list)
        or len(selected_paths) > 5
        or isinstance(entry_count, bool)
        or not isinstance(entry_count, int)
        or entry_count != len(selected_paths)
    ):
        raise ValueError("Mnemosyne corpus selection receipt is invalid")
    for source in selected_paths:
        if not isinstance(source, str):
            raise ValueError("Mnemosyne corpus selection contains a non-string path")
        path = PurePosixPath(source)
        if (
            path.is_absolute()
            or ".." in path.parts
            or len(path.parts) != 2
            or path.parts[0] != "skills"
            or path.suffix != ".md"
            or path.name.endswith(".notes.md")
        ):
            raise ValueError("Mnemosyne corpus selection is outside the flat skill corpus")


def _verify_learn_delivery_receipt(
    host_result: dict[str, Any],
    binding: dict[str, Any],
) -> None:
    delivery = _receipt_dict(host_result.get("delivery_receipt"), "LearnDeliveryReceipt")
    if not valid_delivery_receipt(delivery):
        raise ValueError("LearnDeliveryReceipt lacks matching PR readback evidence")
    repository = _receipt_string(delivery, "repository")
    commit_sha = _receipt_sha(delivery, "commit_sha", 40)
    if (
        repository != binding["repository"]
        or delivery.get("base_branch") != binding["default_branch"]
        or delivery.get("readback_head_sha") != commit_sha
    ):
        raise ValueError("LearnDeliveryReceipt is not bound to the Mnemosyne checkout")
    branch = _receipt_string(delivery, "branch")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) or ".." in branch:
        raise ValueError("LearnDeliveryReceipt has an unsafe branch")
    pr_number = delivery.get("pr_number")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
        raise ValueError("LearnDeliveryReceipt has an invalid PR number")
    expected_url = f"https://github.com/{repository}/pull/{pr_number}"
    if delivery.get("pr_url") != expected_url:
        raise ValueError("LearnDeliveryReceipt PR URL does not match its repository and number")
    validation = delivery.get("validation_evidence")
    if (
        not isinstance(validation, list)
        or not validation
        or any(not isinstance(item, str) or not item for item in validation)
    ):
        raise ValueError("LearnDeliveryReceipt lacks validation evidence")
    _receipt_string(delivery, "final_disposition")
    if (
        delivery.get("local_only") is not False
        or delivery.get("signed_commit") is not True
        or delivery.get("dco_signed_off") is not True
    ):
        raise ValueError("LearnDeliveryReceipt lacks signed, DCO-attested durable delivery")


def _verify_host_athena_receipt(
    run_dir: Path,
    host_job: dict[str, Any],
    *,
    expected_kind: str,
) -> str:
    if host_job.get("job_kind") != "AthenaSkillJob" or host_job.get("kind") != expected_kind:
        raise ValueError("Athena host receipt is not a typed AthenaSkillJob result")
    payload = _load_athena_host_receipt(run_dir, host_job)
    if set(payload) != {"kind", "context", "receipt", "delivery_receipt", "error"}:
        raise ValueError("Athena host receipt is not a typed AthenaSkillResult")
    host_result = payload
    if (
        host_result.get("kind") != expected_kind
        or host_result.get("error") is not None
        or not isinstance(host_result.get("context", ""), str)
    ):
        raise ValueError("Athena host result is unsuccessful or has the wrong kind")
    result_receipt = _receipt_dict(host_result.get("receipt"), "Athena result receipt")
    contract, binding = _verify_contract_and_binding(result_receipt)
    if expected_kind == "advise":
        _verify_advise_receipt(host_result, result_receipt, contract, binding)
    else:
        _verify_learn_delivery_receipt(host_result, binding)
    return expected_kind


def _verify_athena_skill_job_correlation(
    manifest: dict[str, Any],
    host_job: dict[str, Any],
) -> dict[str, Any]:
    """Correlate host evidence without treating Pi policy or admission as its provenance."""
    stage = host_job.get("correlated_stage")
    expected = ATHENA_SKILL_JOB_CORRELATIONS.get(stage) if isinstance(stage, str) else None
    if (
        expected is None
        or host_job.get("kind") != expected[0]
        or host_job.get("correlated_role") != expected[1]
    ):
        raise ValueError("AthenaSkillJob receipt has an invalid agent-job correlation")
    capture_id = host_job.get("correlated_capture_id")
    if not isinstance(capture_id, str) or not capture_id:
        raise ValueError("AthenaSkillJob receipt lacks a correlated capture id")
    entry = _capture_entry_by_id(manifest, capture_id)
    if (
        entry.get("kind") != "capture"
        or entry.get("provider") != PI_PROVIDER_NAME
        or entry.get("status") != "success"
        or entry.get("stage") != stage
    ):
        raise ValueError("AthenaSkillJob receipt is not correlated to a Pi-backed agent job")
    return entry


def _verify_mnemosyne(manifest: dict[str, Any], run_dir: Path) -> None:
    host_jobs = manifest.get("athena_skill_jobs")
    if not isinstance(host_jobs, list):
        raise ValueError("manifest has invalid AthenaSkillJob receipts")
    verified: set[tuple[str, str]] = set()
    for value in host_jobs:
        if not isinstance(value, dict):
            raise ValueError("manifest contains an invalid AthenaSkillJob receipt")
        host_job = cast(dict[str, Any], value)
        kind = _stage_receipt_string(host_job, "kind")
        _verify_host_athena_receipt(run_dir, host_job, expected_kind=kind)
        entry = _verify_athena_skill_job_correlation(manifest, host_job)
        verified_receipt = _verify_pi_stage_receipt(manifest, run_dir, entry)
        stage = cast(str, verified_receipt["stage"])
        correlation = (stage, kind)
        if correlation in verified:
            raise ValueError(f"duplicate AthenaSkillJob receipt for Pi {stage} job")
        verified.add(correlation)

    required = {(stage, kind) for stage, (kind, _role) in ATHENA_SKILL_JOB_CORRELATIONS.items()}
    if verified != required:
        raise ValueError(
            "Mnemosyne verification requires independent typed AthenaSkillJob receipts "
            "correlated to Pi planner, implementer, and reviewer jobs"
        )


def _stage_receipt_dict(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Pi stage receipt has invalid {field}")
    return cast(dict[str, Any], value)


def _stage_receipt_string(data: dict[str, Any], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"Pi stage receipt lacks non-empty {field}")
    return value


def _stage_receipt_sha(data: dict[str, Any], field: str, length: int = 64) -> str:
    value = _stage_receipt_string(data, field)
    if re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is None:
        raise ValueError(f"Pi stage receipt has invalid {field}")
    return value


def _stage_receipt_positive_int(data: dict[str, Any], field: str) -> int:
    value = data.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Pi stage receipt has invalid {field}")
    return value


def _stage_receipt_string_list(data: dict[str, Any], field: str) -> list[str]:
    value = data.get(field)
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"Pi stage receipt has invalid {field}")
    return cast(list[str], value)


def _require_stage_receipt_schema(
    data: dict[str, Any],
    field: str,
    expected_fields: frozenset[str],
) -> None:
    """Reject generic or union-shaped receipt sections at stage boundaries."""
    if set(data) != expected_fields:
        raise ValueError(f"Pi stage receipt has unsupported {field} schema")


def _verify_stage_worktree(
    manifest: dict[str, Any],
    entry: dict[str, Any],
    worktree: dict[str, Any],
    *,
    coordinator_receipt_id: str,
) -> dict[str, Any]:
    _require_stage_receipt_schema(worktree, "worktree", PI_STAGE_WORKTREE_FIELDS)
    receipt_id = _stage_receipt_sha(worktree, "receipt_id")
    if (
        worktree.get("capture_id") != entry.get("id")
        or worktree.get("coordinator_receipt_id") != coordinator_receipt_id
    ):
        raise ValueError("Pi worktree receipt is not bound to its coordinator and capture")
    root = Path(_stage_receipt_string(worktree, "root"))
    git_dir = Path(_stage_receipt_string(worktree, "git_dir"))
    common_dir = Path(_stage_receipt_string(worktree, "git_common_dir"))
    if not root.is_absolute() or root.resolve() != Path(manifest["repo_root"]).resolve():
        raise ValueError("Pi stage receipt is not bound to the manifest worktree")
    if not git_dir.is_absolute() or not common_dir.is_absolute() or git_dir == common_dir:
        raise ValueError("Pi stage receipt lacks isolated linked-worktree evidence")
    try:
        git_dir_parts = git_dir.relative_to(common_dir).parts
    except ValueError as exc:
        raise ValueError("Pi stage receipt git directory is outside its common directory") from exc
    if len(git_dir_parts) < 2 or git_dir_parts[0] != "worktrees":
        raise ValueError("Pi stage receipt lacks linked-worktree metadata")
    if worktree.get("isolated") is not True:
        raise ValueError("Pi stage receipt worktree is not isolated")
    branch = _stage_receipt_string(worktree, "branch")
    if ".." in branch or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) is None:
        raise ValueError("Pi stage receipt has an unsafe worktree branch")
    head = _stage_receipt_sha(worktree, "head", 40)
    if entry.get("revision") != head:
        raise ValueError("Pi stage receipt head does not match its capture revision")
    if not isinstance(worktree.get("clean"), bool):
        raise ValueError("Pi stage receipt lacks a worktree cleanliness observation")
    _stage_receipt_sha(worktree, "status_sha256")
    _stage_receipt_string(worktree, "observed_at")
    return {
        "receipt_id": receipt_id,
        "root": str(root.resolve()),
        "branch": branch,
        "head": head,
        "clean": worktree["clean"],
    }


def _verify_stage_provider_evidence(
    entry: dict[str, Any],
    run_dir: Path,
    stage: str,
    worktree: dict[str, Any],
    provider_evidence: dict[str, Any],
    *,
    coordinator_receipt_id: str,
) -> dict[str, Any]:
    _require_stage_receipt_schema(
        provider_evidence,
        "provider_evidence",
        PI_STAGE_PROVIDER_EVIDENCE_FIELDS,
    )
    outcome = _capture_outcome(run_dir, entry)
    if outcome["status"] != "success":
        raise ValueError("Pi stage receipt is not bound to a successful provider capture")
    observation, analysis_sha256 = _capture_analysis_observation(
        run_dir,
        entry,
        observed_stage=stage,
    )
    if (
        provider_evidence.get("capture_id") != entry.get("id")
        or provider_evidence.get("coordinator_receipt_id") != coordinator_receipt_id
        or provider_evidence.get("capture_analysis_sha256") != analysis_sha256
    ):
        raise ValueError("Pi provider receipt is not bound to its own capture observation")
    request = PI_EVIDENCE_STAGE_REQUESTS[stage]
    expected_policy = _pi_policy_evidence(resolve_policy(request), request)
    observed_policy = _stage_receipt_dict(
        provider_evidence.get("execution_policy"), "provider execution_policy"
    )
    if observed_policy != expected_policy:
        raise ValueError("Pi stage receipt provider policy does not match the required stage")
    expected_entry_policy = {
        key: value
        for key, value in expected_policy.items()
        if key not in {"skill_grants", "tool_scopes"}
    }
    if observation.get("execution_policy") != expected_entry_policy or entry.get(
        "execution_policy"
    ) != observation.get("execution_policy"):
        raise ValueError("Pi capture policy is not bound to its stage receipt")

    tool_scopes = _stage_receipt_string_list(provider_evidence, "tool_scopes")
    if (
        tool_scopes != expected_policy["tool_scopes"]
        or observation.get("tool_scopes") != tool_scopes
        or entry.get("tool_scopes") != tool_scopes
    ):
        raise ValueError("Pi stage receipt lacks its own observed tool-scope evidence")
    skill_grants = _stage_receipt_string_list(provider_evidence, "skill_grants")
    if skill_grants != expected_policy["skill_grants"]:
        raise ValueError("Pi stage receipt skill grants do not match the host policy")
    if (
        observation.get("requested_skill_grants") != skill_grants
        or entry.get("requested_skill_grants") != skill_grants
    ):
        raise ValueError("Pi capture skill grants are not bound to its stage receipt")

    session_ids = _stage_receipt_string_list(provider_evidence, "session_ids")
    if not session_ids:
        raise ValueError("Pi stage receipt lacks observed session evidence")
    if observation.get("session_ids") != session_ids or entry.get("session_ids") != session_ids:
        raise ValueError("Pi stage receipt session evidence is not bound to its capture")
    invocation_id = _stage_receipt_sha(provider_evidence, "invocation_id")
    if invocation_id != _observed_provider_invocation_id(entry, analysis_sha256):
        raise ValueError("Pi stage receipt invocation is not host-observed for its capture")
    if (
        provider_evidence.get("stdout_sha256") != outcome["stdout_sha256"]
        or provider_evidence.get("stderr_sha256") != outcome["stderr_sha256"]
    ):
        raise ValueError("Pi stage receipt output evidence is not bound to its capture")

    binding_sha = provider_evidence.get("session_binding_sha256", "")
    resumed_from = provider_evidence.get("resumed_from_capture_id")
    if request.lifecycle is SessionLifecycle.ONE_SHOT:
        if binding_sha != "" or resumed_from is not None:
            raise ValueError("one-shot Pi stage receipt unexpectedly claims session state")
    else:
        if not session_ids:
            raise ValueError("stateful Pi stage receipt lacks observed session evidence")
        if not isinstance(binding_sha, str) or re.fullmatch(r"[0-9a-f]{64}", binding_sha) is None:
            raise ValueError("stateful Pi stage receipt lacks a session-binding digest")
        artifacts = _stage_receipt_dict(entry.get("artifacts"), "capture artifacts")
        binding_artifact = artifacts.get("session_binding")
        if not isinstance(binding_artifact, str) or not binding_artifact:
            raise ValueError("stateful Pi capture lacks a session-binding artifact")
        binding_payload = _load_private_receipt(
            run_dir,
            {"artifact": binding_artifact, "sha256": binding_sha},
            label="Pi session binding",
            size_limit=MAX_STAGE_RECEIPT_BYTES,
        )
        binding = AgentSessionBinding.from_json(
            json.dumps(binding_payload, sort_keys=True, separators=(",", ":"))
        )
        if (
            binding.session_id not in session_ids
            or binding.canonical_cwd != worktree["root"]
            or binding.role is not request.role
        ):
            raise ValueError("Pi session binding does not match its stage, session, and worktree")
        if request.lifecycle is SessionLifecycle.RESUME_REQUIRED:
            if not isinstance(resumed_from, str) or not resumed_from:
                raise ValueError("resumed Pi stage receipt lacks its source capture")
        elif resumed_from is not None:
            raise ValueError("new Pi stage receipt unexpectedly claims a resumed capture")
    return {
        "invocation_id": invocation_id,
        "capture_analysis_sha256": analysis_sha256,
        "session_ids": tuple(session_ids),
        "session_binding_sha256": binding_sha,
        "resumed_from_capture_id": resumed_from,
    }


def _validated_live_pr_state(value: object) -> dict[str, Any]:
    """Return one complete live PR-state response or fail closed."""
    if not isinstance(value, dict):
        raise ValueError("live GitHub pull-request state readback failed")
    state = cast(dict[str, Any], value)
    required_fields = {
        "state",
        "headRefOid",
        "mergedAt",
        "baseRefName",
        "autoMergeRequest",
    }
    auto_merge = state.get("autoMergeRequest")
    head = state.get("headRefOid")
    if (
        not required_fields.issubset(state)
        or not isinstance(state.get("state"), str)
        or not isinstance(head, str)
        or FULL_COMMIT_SHA_RE.fullmatch(head) is None
        or not isinstance(state.get("baseRefName"), str)
        or not state["baseRefName"]
        or (auto_merge is not None and not isinstance(auto_merge, dict))
    ):
        raise ValueError("live GitHub pull-request state readback is incomplete")
    return state


def _live_unresolved_thread_ids(threads: object) -> frozenset[str]:
    """Return unique IDs from one host-stabilized unresolved-thread read."""
    if not isinstance(threads, list):
        raise ValueError("live GitHub review-thread readback failed")
    ids: list[str] = []
    for thread in threads:
        if not isinstance(thread, dict):
            raise ValueError("live GitHub review-thread readback is malformed")
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("live GitHub review-thread readback is malformed")
        ids.append(thread_id)
    if len(set(ids)) != len(ids):
        raise ValueError("live GitHub review-thread readback contains duplicate IDs")
    return frozenset(ids)


def _validated_live_implementation_labels(value: object) -> tuple[bool, bool]:
    """Return the two exclusive implementation-label flags or fail closed."""
    if (
        not isinstance(value, tuple)
        or len(value) != 2
        or any(not isinstance(item, bool) for item in value)
    ):
        raise ValueError("live GitHub implementation-label readback failed")
    return cast(tuple[bool, bool], value)


def _live_pull_request_readback(
    repository: str,
    pr_number: int,
    repo_root: Path,
) -> dict[str, Any]:
    """Read and stabilize completion facts from the scoped GitHub host seam.

    The caller supplies only the target identity. All acceptance facts come
    from fresh GitHub reads, with PR state, labels, and unresolved-thread IDs
    repeated around the snapshot so an in-flight transition fails closed.
    """
    if repository != PROJECT_REPOSITORY:
        raise ValueError("live GitHub readback targets the wrong repository")
    owner, name = repository.split("/", 1)
    github = PipelineGitHub(owner, repo=name, repo_root=repo_root)

    initial_state = _validated_live_pr_state(github.gh_pr_state(pr_number))
    initial_context = github.pr_review_context(pr_number)
    if not isinstance(initial_context, dict):
        raise ValueError("live GitHub pull-request context readback failed")
    initial_branch = github.get_pr_head_branch(pr_number)
    if not isinstance(initial_branch, str) or not initial_branch:
        raise ValueError("live GitHub pull-request branch readback failed")
    initial_closes_issue = github.find_issue_for_pr(pr_number)

    initial_labels = _validated_live_implementation_labels(
        github.pr_has_implementation_state_label(pr_number)
    )
    initial_threads = _live_unresolved_thread_ids(github.list_unresolved_review_threads(pr_number))
    final_threads = _live_unresolved_thread_ids(github.list_unresolved_review_threads(pr_number))
    final_labels = _validated_live_implementation_labels(
        github.pr_has_implementation_state_label(pr_number)
    )
    final_branch = github.get_pr_head_branch(pr_number)
    final_closes_issue = github.find_issue_for_pr(pr_number)
    final_context = github.pr_review_context(pr_number)
    final_state = _validated_live_pr_state(github.gh_pr_state(pr_number))

    if (
        initial_state != final_state
        or initial_context != final_context
        or initial_branch != final_branch
        or initial_closes_issue != final_closes_issue
        or initial_labels != final_labels
        or initial_threads != final_threads
        or initial_context.get("pr_head_sha") != final_state["headRefOid"]
    ):
        raise ValueError("live GitHub pull-request readback did not stabilize")

    return {
        "repository": repository,
        "number": pr_number,
        "url": f"https://github.com/{repository}/pull/{pr_number}",
        "state": final_state["state"],
        "branch": final_branch,
        "head_sha": final_state["headRefOid"],
        "closes_issue": final_closes_issue,
        "implementation_go": final_labels[0],
        "implementation_no_go": final_labels[1],
        "unresolved_threads": len(final_threads),
        "unresolved_thread_ids": sorted(final_threads),
        "native_auto_merge": final_state["autoMergeRequest"] is not None,
    }


def _provenance_bound_live_pr_receipt(value: object) -> _LivePullRequestReceipt:
    """Convert one complete host readback into non-JSON process-local proof."""
    if not isinstance(value, dict) or set(value) != LIVE_PR_READBACK_FIELDS:
        raise ValueError("host-derived live PR readback has an unsupported schema")
    readback = cast(dict[str, Any], value)
    repository = readback.get("repository")
    number = readback.get("number")
    url = readback.get("url")
    branch = readback.get("branch")
    head_sha = readback.get("head_sha")
    unresolved_threads = readback.get("unresolved_threads")
    unresolved_thread_ids = readback.get("unresolved_thread_ids")
    if (
        repository != PROJECT_REPOSITORY
        or isinstance(number, bool)
        or not isinstance(number, int)
        or number <= 0
        or url != f"https://github.com/{PROJECT_REPOSITORY}/pull/{number}"
        or readback.get("state") != "OPEN"
        or not isinstance(branch, str)
        or not branch
        or not isinstance(head_sha, str)
        or FULL_COMMIT_SHA_RE.fullmatch(head_sha) is None
        or readback.get("closes_issue") != ISSUE_NUMBER
        or not isinstance(readback.get("implementation_go"), bool)
        or not isinstance(readback.get("implementation_no_go"), bool)
        or isinstance(unresolved_threads, bool)
        or not isinstance(unresolved_threads, int)
        or unresolved_threads < 0
        or not isinstance(unresolved_thread_ids, list)
        or any(
            not isinstance(thread_id, str) or not thread_id for thread_id in unresolved_thread_ids
        )
        or len(set(unresolved_thread_ids)) != len(unresolved_thread_ids)
        or unresolved_thread_ids != sorted(unresolved_thread_ids)
        or unresolved_threads != len(unresolved_thread_ids)
        or not isinstance(readback.get("native_auto_merge"), bool)
    ):
        raise ValueError("host-derived live PR readback is incomplete")
    return _LivePullRequestReceipt(
        repository=repository,
        number=number,
        url=url,
        state=cast(str, readback["state"]),
        branch=branch,
        head_sha=head_sha,
        closes_issue=cast(int, readback["closes_issue"]),
        implementation_go=cast(bool, readback["implementation_go"]),
        implementation_no_go=cast(bool, readback["implementation_no_go"]),
        unresolved_thread_ids=tuple(unresolved_thread_ids),
        native_auto_merge=cast(bool, readback["native_auto_merge"]),
        provenance=_LIVE_READBACK_PROVENANCE,
    )


def _verify_stage_pr_readback(
    lifecycle: dict[str, Any],
    *,
    expected_head: str,
    expected_lifecycle_receipt_id: str,
) -> tuple[int, str, str, str]:
    pull_request = _stage_receipt_dict(lifecycle.get("pull_request"), "pull_request")
    _require_stage_receipt_schema(
        pull_request,
        "pull_request",
        PI_STAGE_PULL_REQUEST_FIELDS,
    )
    receipt_id = _stage_receipt_sha(pull_request, "receipt_id")
    if pull_request.get("lifecycle_receipt_id") != expected_lifecycle_receipt_id:
        raise ValueError("Pi pull-request receipt is not bound to its stage lifecycle")
    repository = _stage_receipt_string(pull_request, "repository")
    if repository != PROJECT_REPOSITORY:
        raise ValueError("Pi stage receipt pull request targets the wrong repository")
    pr_number = _stage_receipt_positive_int(pull_request, "number")
    pr_url = _stage_receipt_string(pull_request, "url")
    if pr_url != f"https://github.com/{repository}/pull/{pr_number}":
        raise ValueError("Pi stage receipt pull-request URL does not match its identity")
    if (
        pull_request.get("state") != "OPEN"
        or pull_request.get("head_sha") != expected_head
        or pull_request.get("closes_issue") != ISSUE_NUMBER
    ):
        raise ValueError("Pi stage receipt lacks an exact open pull-request claim")
    branch = _stage_receipt_string(pull_request, "branch")
    return pr_number, pr_url, branch, receipt_id


def _verify_live_stage_pr_readback(
    manifest: dict[str, Any],
    lifecycle: dict[str, Any],
    stage: str,
    *,
    expected_head: str,
) -> _LivePullRequestReceipt:
    """Rebind a caller-provided stage receipt to current GitHub facts."""
    pull_request = _stage_receipt_dict(lifecycle.get("pull_request"), "pull_request")
    repository = _stage_receipt_string(pull_request, "repository")
    pr_number = _stage_receipt_positive_int(pull_request, "number")
    live = _live_pull_request_readback(
        repository,
        pr_number,
        Path(_stage_receipt_string(manifest, "repo_root")),
    )
    claimed_pr = {field: pull_request[field] for field in PI_STAGE_PULL_REQUEST_LIVE_FIELDS}
    live_pr = {field: live[field] for field in PI_STAGE_PULL_REQUEST_LIVE_FIELDS}
    if claimed_pr != live_pr or live["head_sha"] != expected_head:
        raise ValueError("Pi stage receipt does not match live GitHub pull-request readback")
    if stage == "review" and (
        lifecycle.get("reviewed_head_sha") != live["head_sha"]
        or lifecycle.get("implementation_go") is not live["implementation_go"]
        or lifecycle.get("implementation_no_go") is not live["implementation_no_go"]
        or lifecycle.get("unresolved_threads") != live["unresolved_threads"]
        or lifecycle.get("native_auto_merge") is not live["native_auto_merge"]
    ):
        raise ValueError("review receipt does not match live GitHub label and thread readback")
    return _provenance_bound_live_pr_receipt(live)


def _verify_stage_lifecycle(
    manifest: dict[str, Any],
    run_dir: Path,
    entry: dict[str, Any],
    stage: str,
    worktree: dict[str, Any],
    provider: dict[str, Any],
    lifecycle: dict[str, Any],
    *,
    coordinator_receipt_id: str,
) -> tuple[int, str, str, str] | None:
    _require_stage_receipt_schema(
        lifecycle,
        f"{stage} lifecycle",
        PI_STAGE_LIFECYCLE_FIELDS[stage],
    )
    if lifecycle.get("kind") != PI_STAGE_LIFECYCLE_KINDS[stage]:
        raise ValueError("Pi stage receipt has the wrong lifecycle evidence kind")
    lifecycle_receipt_id = _stage_receipt_sha(lifecycle, "receipt_id")
    if (
        lifecycle.get("capture_id") != entry.get("id")
        or lifecycle.get("coordinator_receipt_id") != coordinator_receipt_id
        or lifecycle.get("worktree_receipt_id") != worktree["receipt_id"]
        or lifecycle.get("provider_invocation_id") != provider["invocation_id"]
        or lifecycle.get("fixture_sha256") != _fixture_digest(manifest)
        or lifecycle.get("head_sha") != worktree["head"]
        or lifecycle.get("success") is not True
    ):
        raise ValueError("Pi stage lifecycle is not bound to the fixture and revision")

    if stage == "discovery":
        if (
            lifecycle.get("paths") != list(FIXTURE_PATHS)
            or lifecycle.get("all_paths_found") is not True
        ):
            raise ValueError("discovery lacks host-observed fixture paths")
    elif stage == "planning":
        if lifecycle.get("plan_sha256") != entry.get("stdout_digest"):
            raise ValueError("planning lifecycle is not bound to the captured plan artifact")
    elif stage == "implementation":
        changed_paths = _stage_receipt_string_list(lifecycle, "changed_paths")
        if set(changed_paths) != set(FIXTURE_PATHS):
            raise ValueError("implementation does not contain the exact fixture diff")
        _stage_receipt_sha(lifecycle, "diff_sha256")
    elif stage == "tests":
        if (
            lifecycle.get("argv") != list(FIXTURE_TEST_ARGV)
            or lifecycle.get("returncode") != 0
            or lifecycle.get("timed_out") is not False
        ):
            raise ValueError("tests stage lacks a passing host fixture-test receipt")
        _stage_receipt_sha(lifecycle, "result_sha256")
    elif stage == "commit-pr":
        if (
            lifecycle.get("commit_sha") != worktree["head"]
            or lifecycle.get("signed_commit") is not True
            or lifecycle.get("dco_signed_off") is not True
            or worktree["clean"] is not True
        ):
            raise ValueError("commit-pr lacks a clean signed DCO commit receipt")
        return _verify_stage_pr_readback(
            lifecycle,
            expected_head=worktree["head"],
            expected_lifecycle_receipt_id=lifecycle_receipt_id,
        )
    elif stage == "review":
        pr_identity = _verify_stage_pr_readback(
            lifecycle,
            expected_head=worktree["head"],
            expected_lifecycle_receipt_id=lifecycle_receipt_id,
        )
        review_receipt_id = _stage_receipt_sha(lifecycle, "review_receipt_id")
        handoff = _stage_receipt_dict(lifecycle.get("handoff"), "handoff")
        _require_stage_receipt_schema(handoff, "handoff", PI_STAGE_HANDOFF_FIELDS)
        if (
            lifecycle.get("reviewed_head_sha") != worktree["head"]
            or lifecycle.get("implementation_go") is not True
            or lifecycle.get("implementation_no_go") is not False
            or lifecycle.get("unresolved_threads") != 0
            or lifecycle.get("native_auto_merge") is not False
            or worktree["clean"] is not True
        ):
            raise ValueError(
                "review lacks exact-head review, label, and host learning handoff evidence"
            )
        if (
            handoff.get("source") != "hephaestus.automation.pipeline.coordinator"
            or handoff.get("destination_stage") != "finished"
            or handoff.get("outcome") != "accepted"
            or handoff.get("coordinator_receipt_id") != coordinator_receipt_id
            or handoff.get("worktree_receipt_id") != worktree["receipt_id"]
            or handoff.get("provider_invocation_id") != provider["invocation_id"]
            or handoff.get("review_receipt_id") != review_receipt_id
            or handoff.get("head_sha") != worktree["head"]
            or handoff.get("pr_number") != pr_identity[0]
        ):
            raise ValueError("review lacks a stage-bound finished handoff receipt")
        _stage_receipt_sha(handoff, "receipt_id")
        _stage_receipt_string(handoff, "completed_at")
        return pr_identity
    return None


def _coordinator_observed_stage(payload: dict[str, Any]) -> str:
    """Classify a receipt from stage-specific coordinator evidence, not labels."""
    coordinator = _stage_receipt_dict(payload.get("coordinator"), "coordinator")
    lifecycle = _stage_receipt_dict(payload.get("lifecycle"), "lifecycle")
    candidates = [
        stage
        for sequence, stage in enumerate(REQUIRED_E2E_STAGES, start=1)
        if coordinator.get("pipeline_stage") == PI_STAGE_COORDINATOR_STAGES[stage]
        and coordinator.get("job_kind") == PI_STAGE_COORDINATOR_JOBS[stage]
        and coordinator.get("sequence") == sequence
        and lifecycle.get("kind") == PI_STAGE_LIFECYCLE_KINDS[stage]
        and set(lifecycle) == PI_STAGE_LIFECYCLE_FIELDS[stage]
    ]
    if len(candidates) != 1:
        raise ValueError("Pi receipt lacks one stage-specific coordinator observation")
    return candidates[0]


def _verify_pi_stage_receipt(
    manifest: dict[str, Any],
    run_dir: Path,
    entry: dict[str, Any],
) -> dict[str, Any]:
    payload = _load_private_receipt(
        run_dir,
        entry.get("stage_receipt"),
        label="Pi stage coordinator receipt",
        size_limit=MAX_STAGE_RECEIPT_BYTES,
    )
    required_keys = {
        "schema_version",
        "kind",
        "run_id",
        "issue_number",
        "provider",
        "capture_id",
        "stage",
        "coordinator",
        "provider_evidence",
        "worktree",
        "lifecycle",
    }
    if set(payload) != required_keys:
        raise ValueError("Pi stage coordinator receipt has an unsupported schema")
    stage = _coordinator_observed_stage(payload)
    if (
        payload.get("schema_version") != 1
        or payload.get("kind") != "hephaestus-pi-e2e-stage"
        or payload.get("run_id") != manifest.get("run_id")
        or payload.get("issue_number") != ISSUE_NUMBER
        or payload.get("provider") != PI_PROVIDER_NAME
        or payload.get("capture_id") != entry.get("id")
        or entry.get("kind") != "capture"
        or entry.get("provider") != PI_PROVIDER_NAME
        or entry.get("status") != "success"
    ):
        raise ValueError("Pi stage coordinator receipt is not bound to its successful capture")
    if payload.get("stage") != stage or entry.get("stage") != stage:
        raise ValueError("caller stage label does not identify the coordinator-observed stage")

    coordinator = _stage_receipt_dict(payload.get("coordinator"), "coordinator")
    _require_stage_receipt_schema(
        coordinator,
        "coordinator",
        PI_STAGE_COORDINATOR_FIELDS,
    )
    sequence = REQUIRED_E2E_STAGES.index(stage) + 1
    if (
        coordinator.get("source") != "hephaestus.automation.pipeline.coordinator"
        or coordinator.get("pipeline_stage") != PI_STAGE_COORDINATOR_STAGES[stage]
        or coordinator.get("job_kind") != PI_STAGE_COORDINATOR_JOBS[stage]
        or coordinator.get("sequence") != sequence
        or coordinator.get("outcome") != "success"
    ):
        raise ValueError("Pi stage receipt lacks its required coordinator completion")
    receipt_id = _stage_receipt_sha(coordinator, "receipt_id")
    _stage_receipt_string(coordinator, "worker_id")
    _stage_receipt_string(coordinator, "completed_at")

    worktree = _verify_stage_worktree(
        manifest,
        entry,
        _stage_receipt_dict(payload.get("worktree"), "worktree"),
        coordinator_receipt_id=receipt_id,
    )
    provider = _verify_stage_provider_evidence(
        entry,
        run_dir,
        stage,
        worktree,
        _stage_receipt_dict(payload.get("provider_evidence"), "provider_evidence"),
        coordinator_receipt_id=receipt_id,
    )
    pr_identity = _verify_stage_lifecycle(
        manifest,
        run_dir,
        entry,
        stage,
        worktree,
        provider,
        _stage_receipt_dict(payload.get("lifecycle"), "lifecycle"),
        coordinator_receipt_id=receipt_id,
    )
    live_pr_readback: _LivePullRequestReceipt | None = None
    if pr_identity is not None:
        live_pr_readback = _verify_live_stage_pr_readback(
            manifest,
            _stage_receipt_dict(payload.get("lifecycle"), "lifecycle"),
            stage,
            expected_head=worktree["head"],
        )
        pr_identity = (
            live_pr_readback.number,
            live_pr_readback.url,
            live_pr_readback.branch,
            pr_identity[3],
        )
    descriptor = _receipt_dict(entry.get("stage_receipt"), "artifact descriptor")
    expected_artifact = (
        PurePosixPath(COMMANDS_DIR_NAME) / str(entry["id"]) / "stage-receipt.json"
    ).as_posix()
    if descriptor.get("artifact") != expected_artifact:
        raise ValueError("Pi stage receipt artifact is not owned by its exact capture")
    lifecycle = _stage_receipt_dict(payload.get("lifecycle"), "lifecycle")
    handoff = lifecycle.get("handoff")
    return {
        "capture_id": entry["id"],
        "stage": stage,
        "receipt_id": receipt_id,
        "artifact": descriptor["artifact"],
        "worktree": worktree,
        "provider": provider,
        "lifecycle_receipt_id": lifecycle["receipt_id"],
        "handoff_receipt_id": handoff.get("receipt_id") if isinstance(handoff, dict) else None,
        "pr_identity": pr_identity,
        "live_pr_readback": live_pr_readback,
    }


def _verify_stage_receipts(manifest: dict[str, Any], run_dir: Path) -> dict[str, dict[str, Any]]:
    capture_ids: set[str] = set()
    receipts: list[dict[str, Any]] = []
    for value in manifest.get("commands", []):
        if (
            not isinstance(value, dict)
            or value.get("kind") != "capture"
            or value.get("provider") != PI_PROVIDER_NAME
        ):
            continue
        if "stage_receipt" not in value:
            continue
        capture_id = value.get("id")
        if not isinstance(capture_id, str) or not capture_id:
            raise ValueError("Pi coordinator receipt has an invalid capture id")
        if capture_id in capture_ids:
            raise ValueError("required Pi stages reuse duplicate capture ids")
        capture_ids.add(capture_id)
        receipts.append(_verify_pi_stage_receipt(manifest, run_dir, cast(dict[str, Any], value)))

    by_stage: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        stage = receipt["stage"]
        if stage in by_stage:
            raise ValueError(f"required Pi stage has duplicate coordinator receipts: {stage}")
        by_stage[stage] = receipt
    missing = [stage for stage in REQUIRED_E2E_STAGES if stage not in by_stage]
    if missing:
        raise ValueError(f"required Pi stages are missing: {', '.join(missing)}")

    receipts = [by_stage[stage] for stage in REQUIRED_E2E_STAGES]
    for field in ("receipt_id", "artifact", "lifecycle_receipt_id"):
        values = [receipt[field] for receipt in receipts]
        if len(set(values)) != len(values):
            raise ValueError(f"Pi stages reuse pooled {field} evidence")
    worktree_receipt_ids = [receipt["worktree"]["receipt_id"] for receipt in receipts]
    if len(set(worktree_receipt_ids)) != len(worktree_receipt_ids):
        raise ValueError("Pi stages reuse pooled worktree receipt evidence")
    invocation_ids = [receipt["provider"]["invocation_id"] for receipt in receipts]
    if len(set(invocation_ids)) != len(invocation_ids):
        raise ValueError("Pi stages reuse pooled provider invocation evidence")
    session_owners: dict[str, str] = {}
    for receipt in receipts:
        stage = receipt["stage"]
        for session_id in receipt["provider"]["session_ids"]:
            owner = session_owners.get(session_id)
            if owner is None:
                session_owners[session_id] = stage
                continue
            if {owner, stage} != {"implementation", "tests"}:
                raise ValueError("independent Pi stages reuse pooled session evidence")
    roots = {receipt["worktree"]["root"] for receipt in receipts}
    if len(roots) != 1:
        raise ValueError("Pi stages are not bound to one isolated fixture worktree")

    implementation = by_stage["implementation"]
    tests = by_stage["tests"]
    if (
        tests["provider"]["resumed_from_capture_id"] != implementation["capture_id"]
        or tests["provider"]["session_binding_sha256"]
        != implementation["provider"]["session_binding_sha256"]
        or tests["provider"]["session_ids"] != implementation["provider"]["session_ids"]
    ):
        raise ValueError("tests stage is not bound to its implementation Pi session")

    new_session_stages = ("planning", "implementation")
    new_bindings = [
        by_stage[stage]["provider"]["session_binding_sha256"] for stage in new_session_stages
    ]
    if len(set(new_bindings)) != len(new_bindings):
        raise ValueError("independent Pi stages reuse pooled session bindings")

    pr_identities = [by_stage[stage]["pr_identity"] for stage in ("commit-pr", "review")]
    if (
        pr_identities[0] is None
        or pr_identities[1] is None
        or pr_identities[0][:3] != pr_identities[1][:3]
        or pr_identities[0][3] == pr_identities[1][3]
    ):
        raise ValueError("GitHub lifecycle receipts do not identify one exact pull request")
    committed_head = by_stage["commit-pr"]["worktree"]["head"]
    if by_stage["review"]["worktree"]["head"] != committed_head:
        raise ValueError("review handoff is not bound to the committed PR head")
    return by_stage


def _verify_completion(manifest: dict[str, Any], run_dir: Path) -> dict[str, dict[str, Any]]:
    """Require the complete successful Pi workflow and its control evidence."""
    _verify_fixture(manifest)
    _verify_workflow(manifest)
    _verify_capture(manifest)
    _verify_failure_probes(manifest)
    _verify_comparison(manifest, run_dir)
    _verify_expected_failure_comparison(manifest)
    stage_receipts = _verify_stage_receipts(manifest, run_dir)
    _verify_mnemosyne(manifest, run_dir)
    pi = cast(dict[str, Any], manifest.get("pi", {}))
    inventory = cast(dict[str, Any], pi.get("package_inventory", {}))
    if inventory.get("ready") is not True:
        raise ValueError("Pi package inventory is not ready")
    return stage_receipts


def _evidence_status(manifest: dict[str, Any], run_dir: Path) -> str:
    """Return the truthful publishable completion state for a private manifest."""
    try:
        _verify_completion(manifest, run_dir)
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
    expected_report = _render_report(manifest, run_dir, report_path, runbook_path)
    expected_runbook = _render_runbook(manifest, run_dir, report_path)
    if report_path.read_text(encoding="utf-8") != expected_report:
        raise ValueError("rendered report does not match the manifest")
    if runbook_path.read_text(encoding="utf-8") != expected_runbook:
        raise ValueError("rendered runbook does not match the manifest")
    publication_value = manifest.get("publication", {})
    if not isinstance(publication_value, dict):
        raise ValueError("publication record is invalid")
    publication = cast(dict[str, Any], publication_value)
    if not publication:
        return
    if publication.get("report") != str(report_path):
        raise ValueError("publication report path does not match the manifest")
    if publication.get("runbook") != str(runbook_path):
        raise ValueError("publication runbook path does not match the manifest")
    if publication.get("repo") != PROJECT_REPOSITORY:
        raise ValueError("publication repository does not match the configured repository")
    commit_sha = _full_commit_sha(publication.get("commit_sha"), label="publication commit")
    if publication.get("ref") != commit_sha:
        raise ValueError("publication ref does not match its immutable commit")
    evidence = _publication_attestation_evidence(
        manifest,
        _verify_stage_receipts(manifest, run_dir),
        commit_sha,
    )
    for field, expected in evidence.items():
        if publication.get(field) != expected:
            raise ValueError(f"publication {field} does not match its local evidence")
    verified_defects = publication.get("verified_defects")
    if not isinstance(verified_defects, bool):
        raise ValueError("publication verified_defects flag is invalid")
    verified_follow_ups = publication.get("verified_follow_up_issues")
    if verified_defects:
        live_follow_ups = _verify_defect_follow_ups(
            manifest,
            Path(_stage_receipt_string(manifest, "repo_root")),
        )
        if verified_follow_ups != live_follow_ups:
            raise ValueError("publication follow-up issue proof is stale")
    elif verified_follow_ups != []:
        raise ValueError("publication contains unverified follow-up issue proof")


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
        _verify_comparison(manifest, run_dir)
        return 0
    if criterion == "mnemosyne":
        _verify_mnemosyne(manifest, run_dir)
        return 0
    if criterion == "publication":
        if report_path is None or runbook_path is None:
            raise ValueError("publication verification requires report and runbook paths")
        _verify_publication(manifest, run_dir, report_path, runbook_path)
        return 0
    if criterion == "completion":
        if report_path is None or runbook_path is None:
            raise ValueError("completion verification requires report and runbook paths")
        _verify_completion(manifest, run_dir)
        _verify_publication(manifest, run_dir, report_path, runbook_path)
        return 0
    raise ValueError(f"unsupported verification criterion: {criterion}")


def _full_commit_sha(value: object, *, label: str) -> str:
    """Return a canonical immutable Git commit SHA or fail closed."""
    if not isinstance(value, str) or FULL_COMMIT_SHA_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a full immutable commit SHA")
    return value


def _github_repository_from_remote(value: str) -> str:
    """Return an owner/repository slug from a canonical GitHub remote URL."""
    normalized = value.strip().removesuffix("/").removesuffix(".git")
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if normalized.startswith(prefix):
            repository = normalized.removeprefix(prefix)
            if re.fullmatch(r"[A-Za-z0-9.-]+/[A-Za-z0-9._-]+", repository):
                return repository
    return ""


def _resolve_publication_commit(repo_root: Path, ref: str) -> str:
    """Resolve a publication ref and require it to be the immutable commit name."""
    requested_sha = _full_commit_sha(ref, label="publication ref")
    try:
        remote_result = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
        repository = _github_repository_from_remote(remote_result.stdout)
        if remote_result.returncode != 0 or repository.casefold() != PROJECT_REPOSITORY.casefold():
            raise ValueError(
                "publication checkout does not identify the configured GitHub repository"
            )
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{requested_sha}^{{commit}}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ValueError(
            "publication ref could not be resolved in the configured repository"
        ) from exc
    if result.returncode != 0:
        raise ValueError("publication ref does not resolve to an existing commit")
    resolved_sha = _full_commit_sha(result.stdout.strip(), label="resolved publication ref")
    if resolved_sha != requested_sha:
        raise ValueError("publication ref resolved to a different commit")
    return resolved_sha


def _publication_attestation_evidence(
    manifest: dict[str, Any],
    stage_receipts: dict[str, dict[str, Any]],
    commit_sha: str,
) -> dict[str, Any]:
    """Bind a publication to the captured local snapshot and workflow receipts."""
    snapshot_sha = _full_commit_sha(
        _latest_snapshot_revision(manifest), label="captured snapshot revision"
    )
    if snapshot_sha != commit_sha:
        raise ValueError("publication commit does not match the captured snapshot")

    workflow_receipts: list[dict[str, Any]] = []
    for stage in ("commit-pr", "review"):
        receipt = stage_receipts.get(stage)
        if not isinstance(receipt, dict) or receipt.get("stage") != stage:
            raise ValueError(f"publication lacks the required {stage} workflow receipt")
        worktree = receipt.get("worktree")
        if not isinstance(worktree, dict) or worktree.get("head") != commit_sha:
            raise ValueError(f"publication commit does not match the {stage} workflow receipt")
        pr_identity = receipt.get("pr_identity")
        if (
            not isinstance(pr_identity, tuple)
            or len(pr_identity) != 4
            or isinstance(pr_identity[0], bool)
            or not isinstance(pr_identity[0], int)
            or not isinstance(pr_identity[1], str)
            or not isinstance(pr_identity[2], str)
            or not isinstance(pr_identity[3], str)
        ):
            raise ValueError(f"publication lacks the required {stage} PR readback receipt")
        live = receipt.get("live_pr_readback")
        if (
            not isinstance(live, _LivePullRequestReceipt)
            or live.provenance is not _LIVE_READBACK_PROVENANCE
        ):
            raise ValueError(
                f"publication lacks the required {stage} host-derived live PR readback"
            )
        if (
            live.repository != PROJECT_REPOSITORY
            or live.head_sha != commit_sha
            or live.state != "OPEN"
            or live.closes_issue != ISSUE_NUMBER
            or pr_identity[:3] != (live.number, live.url, live.branch)
        ):
            raise ValueError(f"publication {stage} live PR readback does not match its receipt")
        if stage == "review" and (
            live.implementation_go is not True
            or live.implementation_no_go is not False
            or live.unresolved_thread_ids
            or live.native_auto_merge is not False
        ):
            raise ValueError("publication review live readback is not merge-safe")
        receipt_id = _stage_receipt_sha({"receipt_id": receipt.get("receipt_id")}, "receipt_id")
        lifecycle_receipt_id = _stage_receipt_sha(
            {"receipt_id": receipt.get("lifecycle_receipt_id")},
            "receipt_id",
        )
        pr_receipt_id = _stage_receipt_sha(
            {"receipt_id": pr_identity[3]},
            "receipt_id",
        )
        workflow_receipts.append(
            {
                "stage": stage,
                "capture_id": receipt["capture_id"],
                "receipt_id": receipt_id,
                "lifecycle_receipt_id": lifecycle_receipt_id,
                "pr_receipt_id": pr_receipt_id,
                "head_sha": commit_sha,
                "pr_number": pr_identity[0],
                "pr_url": pr_identity[1],
                "branch": pr_identity[2],
                "repository": live.repository,
                "pr_state": live.state,
                "closes_issue": live.closes_issue,
                "implementation_go": live.implementation_go,
                "implementation_no_go": live.implementation_no_go,
                "unresolved_thread_ids": list(live.unresolved_thread_ids),
                "native_auto_merge": live.native_auto_merge,
            }
        )
    return {"snapshot_sha": snapshot_sha, "workflow_receipts": workflow_receipts}


def _attest_publication(
    run_dir: Path,
    *,
    repo: str,
    ref: str,
    report_path: Path,
    runbook_path: Path,
    verify_defects: bool,
) -> int:
    manifest = _load_manifest(run_dir)
    stage_receipts = _verify_completion(manifest, run_dir)
    _verify_publication(manifest, run_dir, report_path, runbook_path)
    if repo != PROJECT_REPOSITORY:
        raise ValueError("publication repository does not match the configured repository")
    repo_root_value = manifest.get("repo_root")
    if not isinstance(repo_root_value, str) or not repo_root_value:
        raise ValueError("publication manifest lacks a configured repository path")
    commit_sha = _resolve_publication_commit(Path(repo_root_value), ref)
    verified_follow_ups = (
        _verify_defect_follow_ups(manifest, Path(repo_root_value)) if verify_defects else []
    )
    # Completion validation may precede local commit and defect checks by an
    # arbitrary interval. Re-consume every coordinator receipt and perform its
    # stabilized live GitHub rebind immediately before deriving the attestation;
    # caller-authored receipt claims are never the final PR/head/label/thread proof.
    stage_receipts = _verify_stage_receipts(manifest, run_dir)
    evidence = _publication_attestation_evidence(manifest, stage_receipts, commit_sha)
    publication = {
        "repo": repo,
        "ref": commit_sha,
        "commit_sha": commit_sha,
        **evidence,
        "report": str(report_path),
        "runbook": str(runbook_path),
        "verified_defects": verify_defects,
        "verified_follow_up_issues": verified_follow_ups,
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

    comparison = subparsers.add_parser(
        "record-comparison",
        help="persist a paired Pi/control comparison",
    )
    comparison.add_argument("--run-id", required=True)
    comparison.add_argument("--pi-entry", required=True)
    comparison.add_argument("--control-entry", required=True)

    stage_receipt = subparsers.add_parser(
        "record-stage-receipt",
        help="bind one host/coordinator receipt to an exact Pi capture",
    )
    stage_receipt.add_argument("--run-id", required=True)
    stage_receipt.add_argument("--capture-id", required=True)
    stage_receipt.add_argument("--receipt", type=Path, required=True)

    athena_receipt = subparsers.add_parser(
        "record-athena-host-receipt",
        help="persist a typed host receipt and correlate it to a Pi-backed job",
    )
    athena_receipt.add_argument("--run-id", required=True)
    athena_receipt.add_argument("--kind", choices=("advise", "learn"), required=True)
    athena_receipt.add_argument("--correlated-capture-id", required=True)
    athena_receipt.add_argument("--receipt", type=Path, required=True)

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
            if args.provider == PI_PROVIDER_NAME and command_argv:
                raise ValueError(
                    "Pi capture rejects direct command arguments; provide only a stage prompt so "
                    "the admitted runtime can apply its execution policy"
                )
            if args.provider != PI_PROVIDER_NAME and not command_argv:
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
            if args.provider == PI_PROVIDER_NAME and command_argv:
                raise ValueError(
                    "Pi failure probes reject direct command arguments; provide only a stage "
                    "prompt so the admitted runtime can apply its execution policy"
                )
            if args.provider != PI_PROVIDER_NAME and not command_argv:
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
                repo_root=repo_root,
                summary=args.summary,
                follow_up_issue=args.follow_up_issue,
                details=args.details,
                source_entry=args.source_entry,
            )
        if args.command == "record-comparison":
            return _record_comparison(
                run_dir,
                pi_entry_id=args.pi_entry,
                control_entry_id=args.control_entry,
            )
        if args.command == "record-stage-receipt":
            return _record_stage_receipt(
                run_dir,
                capture_id=args.capture_id,
                receipt_path=args.receipt,
            )
        if args.command == "record-athena-host-receipt":
            return _record_athena_host_receipt(
                run_dir,
                kind=args.kind,
                correlated_capture_id=args.correlated_capture_id,
                receipt_path=args.receipt,
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
                report_path=args.report,
                runbook_path=args.runbook,
                verify_defects=args.verify_defects,
            )
        raise ValueError(f"unsupported command: {args.command}")
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

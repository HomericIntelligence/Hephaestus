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
from hephaestus.io.utils import write_secure
from hephaestus.utils.helpers import slugify

ISSUE_NUMBER = 2519
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
PROXY_TOOL_NAMES = ("pi", "codex")
REQUIRED_SKILL_COMMANDS = ("skill:advise", "skill:learn", "skill:pr-review")
REQUIRED_E2E_STAGES = (
    "discovery",
    "advise",
    "planning",
    "implementation",
    "tests",
    "commit-pr",
    "review",
    "handoff",
)
SKILL_COMMAND_RE = re.compile(r"(?:\$athena:|/athena:|skill:)[A-Za-z0-9._:/-]+")
DEFAULT_CAPTURE_TIMEOUT_SECONDS = 600


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
        "comparisons": [],
        "artifacts": {},
        "publication": {},
    }


def _resolve_run_dir(run_root: Path, run_id: str) -> Path:
    run_dir = run_root / run_id
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
    _ensure_owner_only_dir(run_root)
    run_dir = run_root / run_id
    _ensure_owner_only_dir(run_dir)
    for subdir in (COMMANDS_DIR_NAME, DEFECTS_DIR_NAME, ARTIFACTS_DIR_NAME):
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
        if token in {"--tools", "--allowedTools", "--commands"}:
            if index + 1 < len(argv):
                scopes.extend(part for part in argv[index + 1].split(",") if part)
            continue
        for flag in ("--tools=", "--allowedTools=", "--commands="):
            if token.startswith(flag):
                scopes.extend(part for part in token.split("=", 1)[1].split(",") if part)
    return tuple(dict.fromkeys(scopes))


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
    skill_calls = tuple(
        dict.fromkeys(
            [
                *(_skill_calls_from_events(all_events)),
                *(
                    match
                    for invocation in proxy_invocations
                    for match in _proxy_tool_scopes_from_argv(invocation["argv"])
                    if match.startswith("skill:")
                ),
            ]
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
        "skill_calls": list(skill_calls),
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


def _probe_command_version(binary: str) -> str:
    resolved = shutil.which(binary)
    if resolved is None:
        return ""
    result = subprocess.run(
        [resolved, "--version"],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        check=False,
    )
    output = (result.stdout or result.stderr or "").strip()
    return output


def _record_inventory(run_dir: Path) -> int:
    repo_root = Path(_load_manifest(run_dir)["repo_root"])
    catalog = load_pi_package_catalog()
    pi_inventory = inspect_pi_package_inventory(repo_root, catalog)
    manifest = _load_manifest(run_dir)
    manifest["pi"] = {
        "version": _probe_command_version("pi"),
        "binary": shutil.which("pi") or "",
        "skill_commands": list(catalog.required_commands),
        "package_inventory": {
            "ready": pi_inventory.ready,
            "status": pi_inventory.status,
            "detail": pi_inventory.detail,
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
            "status": "success" if pi_inventory.ready else "failure",
            "returncode": 0 if pi_inventory.ready else 1,
            "provider": "pi",
            "session_ids": [],
            "skill_calls": list(catalog.required_commands),
            "tool_scopes": [],
            "prompt_sha256": "",
            "stdout_digest": "",
            "stderr_digest": "",
            "started_at": _utc_now(),
            "finished_at": _utc_now(),
            "artifacts": {"inventory": str(Path(ARTIFACTS_DIR_NAME) / "inventory.json")},
        },
    )
    return 0 if pi_inventory.ready else 1


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
    if prompt_file is not None:
        prompt_copy = record_dir / "prompt.txt"
        write_secure(prompt_copy, prompt)
        prompt_copy.chmod(0o600)
    start = _utc_now()
    try:
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
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        stdout = _timeout_output(exc.stdout)
        stderr = _timeout_output(exc.stderr)
        stderr += f"error: command timed out after {timeout_seconds} seconds\n"
        timed_out = True
    write_secure(stdout_path, stdout)
    write_secure(stderr_path, stderr)
    analysis = _capture_analysis(stdout, stderr, proxy_log)
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
    entry = {
        "id": f"{command_index:02d}-{slugify(stage) or 'stage'}",
        "kind": "capture",
        "provider": provider,
        "stage": stage,
        "status": "success" if returncode == 0 else "failure",
        "returncode": returncode,
        "timeout_seconds": timeout_seconds,
        "timed_out": timed_out,
        "prompt_sha256": _prompt_digest(prompt),
        "session_ids": analysis["session_ids"],
        "skill_calls": analysis["skill_calls"],
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
    _append_command_entry(run_dir, entry)
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
        print("error: failure probe unexpectedly succeeded", file=sys.stderr)
        return 1
    return 0


def _record_defect(
    run_dir: Path,
    *,
    summary: str,
    follow_up_issue: int,
    details: str,
    source_entry: str = "",
) -> int:
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


def _render_report(manifest: dict[str, Any], report_path: Path, runbook_path: Path) -> str:
    pi = cast(dict[str, Any], manifest.get("pi", {}))
    inventory = cast(dict[str, Any], pi.get("package_inventory", {}))
    commands = [entry for entry in manifest.get("commands", []) if entry.get("kind") == "capture"]
    defects = cast(list[dict[str, Any]], manifest.get("defects", []))
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
                    "Tool scopes | Skill calls |"
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
                        (
                            f"{len(entry.get('session_ids', []))} recorded privately"
                            if entry.get("session_ids")
                            else "none"
                        ),
                        ", ".join(entry.get("tool_scopes", [])) or "n/a",
                        ", ".join(entry.get("skill_calls", [])) or "n/a",
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
            "- Exact local paths and session identifiers remain in the owner-only manifest.",
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
            "1. Initialize the private run directory.",
            "2. Record the exact Pi inventory and command catalog.",
            "3. Capture Pi or Codex control runs through the temporary provider proxy.",
            "4. Capture any failure probes that demonstrate the stage boundary.",
            "5. Record defects as follow-up issues.",
            "6. Render the report and runbook from the manifest.",
            "7. Attest publication readiness.",
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
                "--stage <stage> --provider pi -- <command...>"
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
                "--repo HomericIntelligence/Hephaestus --ref <commit-sha> --verify-defects"
            ),
            "```",
            "",
            "## Verification Criteria",
            "",
            "- `fixture` validates the deterministic fixture contract.",
            "- `workflow` requires inventory, command, and repository snapshot evidence.",
            "- `capture` requires private session identifiers and observed tool scopes.",
            "- `comparison` requires Pi and a distinct control provider.",
            "- `mnemosyne` requires observed advise, learn, and review skill calls.",
            "- `publication` requires deterministic manifest-to-document rendering.",
            "- `completion` requires every Pi stage to succeed plus all criteria above.",
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
    if not any(entry.get("session_ids") for entry in capture_entries):
        raise ValueError("no captured command emitted a session id")
    if not any(entry.get("tool_scopes") for entry in capture_entries):
        raise ValueError("no captured command recorded tool scopes")


def _verify_comparison(manifest: dict[str, Any]) -> None:
    capture_entries = [
        entry for entry in manifest.get("commands", []) if entry.get("kind") == "capture"
    ]
    providers = {entry.get("provider") for entry in capture_entries if entry.get("provider")}
    if len(providers) < 2:
        raise ValueError("comparison requires at least two distinct provider runs")
    staged = {entry.get("stage") for entry in capture_entries if entry.get("stage")}
    if len(staged) < 1:
        raise ValueError("comparison requires at least one staged capture")


def _verify_mnemosyne(manifest: dict[str, Any]) -> None:
    capture_entries = [
        entry for entry in manifest.get("commands", []) if entry.get("kind") == "capture"
    ]
    observed = {
        skill
        for entry in capture_entries
        for skill in entry.get("skill_calls", [])
        if isinstance(skill, str)
    }
    if not set(REQUIRED_SKILL_COMMANDS).issubset(observed):
        raise ValueError(
            "recorded capture evidence does not include the required "
            "advise, learn, and review skill calls"
        )


def _verify_completion(manifest: dict[str, Any]) -> None:
    """Require the complete successful Pi workflow and its control evidence."""
    _verify_fixture(manifest)
    _verify_workflow(manifest)
    _verify_capture(manifest)
    _verify_comparison(manifest)
    _verify_mnemosyne(manifest)
    pi = cast(dict[str, Any], manifest.get("pi", {}))
    inventory = cast(dict[str, Any], pi.get("package_inventory", {}))
    if inventory.get("ready") is not True:
        raise ValueError("Pi package inventory is not ready")
    pi_captures = [
        entry
        for entry in manifest.get("commands", [])
        if entry.get("kind") == "capture" and entry.get("provider") == "pi"
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
        _verify_publication(manifest, run_dir, report_path, runbook_path)
        return 0
    raise ValueError(f"unsupported verification criterion: {criterion}")


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
    _verify_completion(manifest)
    _verify_publication(manifest, run_dir, report_path, runbook_path)
    if verify_defects:
        defects = cast(list[dict[str, Any]], manifest.get("defects", []))
        if not all(
            isinstance(entry.get("follow_up_issue"), int) and entry["follow_up_issue"] > 0
            for entry in defects
        ):
            raise ValueError("one or more defects lacks a valid follow-up issue")
    publication = {
        "repo": repo,
        "ref": ref,
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

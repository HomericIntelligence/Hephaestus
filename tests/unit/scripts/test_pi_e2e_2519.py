"""Tests for scripts/pi_e2e_2519.py."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "pi_e2e_2519.py"


def _load_module() -> ModuleType:
    assert SCRIPT.is_file(), "the issue-2519 collector must exist"
    spec = importlib.util.spec_from_file_location("pi_e2e_2519", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_provider(path: Path) -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "from __future__ import annotations\n"
        "\n"
        "import json\n"
        "import signal\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "\n"
        "def main() -> int:\n"
        "    argv_file = Path(sys.argv[1])\n"
        "    signal_file = Path(sys.argv[2])\n"
        "    mode = sys.argv[3]\n"
        "    argv_file.write_text(json.dumps(sys.argv[1:], indent=2), encoding='utf-8')\n"
        "    if mode == 'emit':\n"
        "        print(json.dumps({'type': 'session', 'id': 'session-123'}))\n"
        "        print(json.dumps({'type': 'session_meta', 'payload': {'id': 'session-123'}}))\n"
        "        print(json.dumps({'type': 'message_end', 'message': "
        "{'role': 'assistant', 'content': 'skill:advise skill:pr-review'}}))\n"
        "        return 0\n"
        "\n"
        "    def handler(signum: int, _frame: object) -> None:\n"
        "        signal_file.write_text(str(signum), encoding='utf-8')\n"
        "        raise SystemExit(128 + signum)\n"
        "\n"
        "    signal.signal(signal.SIGTERM, handler)\n"
        "    while True:\n"
        "        time.sleep(0.1)\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )
    path.chmod(0o700)


def _bootstrap_run(module: ModuleType, tmp_path: Path, run_id: str = "run-1") -> tuple[Path, Path]:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    run_root = tmp_path / "build" / "pi-e2e-2519"
    assert (
        module.main(
            [
                "--repo-root",
                str(repo_root),
                "--run-root",
                str(run_root),
                "init",
                "--run-id",
                run_id,
            ]
        )
        == 0
    )
    return repo_root, run_root / run_id


def _host_athena_receipt(
    *,
    capture_id: str,
    stage: str,
    readback_head_sha: str | None = None,
) -> dict[str, Any]:
    athena_commit = "a" * 40
    mnemosyne_commit = "b" * 40
    delivered_commit = "c" * 40
    contract = {
        "athena_repository": "github.com/HomericIntelligence/Athena",
        "athena_commit": athena_commit,
        "advise_sha256": "1" * 64,
        "learn_sha256": "2" * 64,
        "dependency_resolution_sha256": "3" * 64,
        "trust_source": "pi-package-catalog",
    }
    binding = {
        "root": "/host-owned/mnemosyne",
        "repository": "HomericIntelligence/Mnemosyne",
        "default_branch": "main",
        "commit_sha": mnemosyne_commit,
        "trust_basis": "canonical upstream",
        "athena_contract": contract,
    }
    kind = "advise" if stage == "advise" else "learn"
    delivery_receipt = None
    corpus: dict[str, Any] = {
        "repository": binding["repository"],
        "commit_sha": mnemosyne_commit,
        "selected_paths": ["skills/evidence-receipts.md"],
        "entry_count": 1,
        "athena_contract": contract,
    }
    if kind == "learn":
        corpus = {}
        delivery_receipt = {
            "repository": binding["repository"],
            "branch": "skill/evidence-receipts",
            "base_branch": binding["default_branch"],
            "commit_sha": delivered_commit,
            "pr_url": "https://github.com/HomericIntelligence/Mnemosyne/pull/17",
            "pr_number": 17,
            "readback_head_sha": readback_head_sha or delivered_commit,
            "validation_evidence": ["uv run pytest tests/unit"],
            "final_disposition": "amend",
            "local_only": False,
            "signed_commit": True,
            "dco_signed_off": True,
        }
    return {
        "schema_version": 1,
        "provider": "pi",
        "capture_id": capture_id,
        "stage": stage,
        "pi_command_receipt": {
            "command": f"skill:{kind}",
            "package_key": "athena",
            "package_root": "/host-owned/athena",
            "repository": contract["athena_repository"],
            "commit": athena_commit,
        },
        "host_result": {
            "kind": kind,
            "context": "Selected host-owned advice." if kind == "advise" else "",
            "receipt": {
                "contract": contract,
                "binding": binding,
                "corpus": corpus,
            },
            "delivery_receipt": delivery_receipt,
            "error": None,
        },
    }


def _attach_host_athena_receipt(
    module: ModuleType,
    run_dir: Path,
    entry: dict[str, Any],
    *,
    readback_head_sha: str | None = None,
) -> None:
    payload = _host_athena_receipt(
        capture_id=entry["id"],
        stage=entry["stage"],
        readback_head_sha=readback_head_sha,
    )
    relative_path = Path("commands") / entry["id"] / "athena-host-receipt.json"
    receipt_path = run_dir / relative_path
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    receipt_path.write_bytes(content)
    entry["athena_host_receipt"] = {
        "artifact": str(relative_path),
        "sha256": module._sha256_bytes(content),
    }


def _attach_capture_artifacts(
    module: ModuleType,
    run_dir: Path,
    entry: dict[str, Any],
    *,
    stdout: str | None = None,
    stderr: str | None = None,
) -> None:
    entry_id = str(entry["id"])
    stdout_text = f"{entry_id}-stdout" if stdout is None else stdout
    stderr_text = f"{entry_id}-stderr" if stderr is None else stderr
    artifact_dir = run_dir / "commands" / entry_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = artifact_dir / "stdout.txt"
    stderr_path = artifact_dir / "stderr.txt"
    stdout_path.write_text(stdout_text, encoding="utf-8")
    stderr_path.write_text(stderr_text, encoding="utf-8")
    entry.setdefault("artifacts", {})
    entry["artifacts"]["stdout"] = str(stdout_path.relative_to(run_dir))
    entry["artifacts"]["stderr"] = str(stderr_path.relative_to(run_dir))
    entry["stdout_digest"] = module._sha256_text(stdout_text)
    entry["stderr_digest"] = module._sha256_text(stderr_text)


def _attach_stage_receipt(
    module: ModuleType,
    run_dir: Path,
    manifest: dict[str, Any],
    entry: dict[str, Any],
) -> None:
    stage = entry["stage"]
    sequence = module.REQUIRED_E2E_STAGES.index(stage) + 1
    request = module.PI_EVIDENCE_STAGE_REQUESTS[stage]
    policy = module._pi_policy_evidence(module.resolve_policy(request), request)
    entry["execution_policy"] = {
        key: value for key, value in policy.items() if key not in {"skill_calls", "tool_scopes"}
    }
    entry["tool_scopes"] = policy["tool_scopes"]
    entry["skill_calls"] = policy["skill_calls"]
    entry.setdefault("artifacts", {})
    _attach_capture_artifacts(
        module,
        run_dir,
        entry,
        stdout=f"stdout-{stage}",
        stderr=f"stderr-{stage}",
    )

    session_ids = [f"private-session-{sequence}"]
    binding_sha = ""
    resumed_from: str | None = None
    if request.lifecycle is module.SessionLifecycle.RESUME_REQUIRED:
        implementation = next(
            value
            for value in manifest["commands"]
            if value.get("kind") == "capture"
            and value.get("provider") == "pi"
            and value.get("stage") == "implementation"
        )
        session_ids = implementation["session_ids"]
        binding_artifact = implementation["artifacts"]["session_binding"]
        entry["artifacts"]["session_binding"] = binding_artifact
        binding_sha = module._sha256_bytes((run_dir / binding_artifact).read_bytes())
        resumed_from = implementation["id"]
    elif request.lifecycle is module.SessionLifecycle.START_NEW:
        binding = module.AgentSessionBinding(
            session_id=session_ids[0],
            canonical_cwd=str(Path(manifest["repo_root"]).resolve()),
            role=request.role,
            model_fingerprint=module._sha256_text(f"{stage}-model"),
        )
        relative_binding = Path("commands") / entry["id"] / "session-binding.json"
        binding_path = run_dir / relative_binding
        binding_path.parent.mkdir(parents=True, exist_ok=True)
        binding_path.write_text(binding.to_json() + "\n", encoding="utf-8")
        entry["artifacts"]["session_binding"] = relative_binding.as_posix()
        binding_sha = module._sha256_bytes(binding_path.read_bytes())
    entry["session_ids"] = session_ids

    if stage in {"advise", "handoff"}:
        _attach_host_athena_receipt(module, run_dir, entry)

    head = entry["revision"]
    lifecycle: dict[str, Any] = {
        "kind": module.PI_STAGE_LIFECYCLE_KINDS[stage],
        "fixture_sha256": module._fixture_digest(manifest),
        "head_sha": head,
        "success": True,
    }
    if stage == "discovery":
        lifecycle.update(paths=list(module.FIXTURE_PATHS), all_paths_found=True)
    elif stage == "advise":
        lifecycle["athena_receipt_sha256"] = entry["athena_host_receipt"]["sha256"]
    elif stage == "planning":
        lifecycle["plan_sha256"] = entry["stdout_digest"]
    elif stage == "implementation":
        lifecycle.update(
            changed_paths=list(module.FIXTURE_PATHS),
            diff_sha256=module._sha256_text("fixture-diff"),
        )
    elif stage == "tests":
        lifecycle.update(
            argv=list(module.FIXTURE_TEST_ARGV),
            returncode=0,
            timed_out=False,
            result_sha256=module._sha256_text("fixture-test-result"),
        )
    else:
        lifecycle["pull_request"] = {
            "repository": module.PROJECT_REPOSITORY,
            "number": 2519,
            "url": f"https://github.com/{module.PROJECT_REPOSITORY}/pull/2519",
            "state": "OPEN",
            "branch": "2519-pi-e2e",
            "head_sha": head,
            "closes_issue": module.ISSUE_NUMBER,
            "readback_verified": True,
        }
        if stage == "commit-pr":
            lifecycle.update(commit_sha=head, signed_commit=True, dco_signed_off=True)
        elif stage == "review":
            lifecycle.update(
                reviewed_head_sha=head,
                implementation_go=True,
                implementation_no_go=False,
                unresolved_threads=0,
                native_auto_merge=False,
                review_receipt_id=module._sha256_text("review-receipt"),
            )
        else:
            lifecycle.update(
                learn_receipt_sha256=entry["athena_host_receipt"]["sha256"],
                finished_handoff=True,
            )

    payload = {
        "schema_version": 1,
        "kind": "hephaestus-pi-e2e-stage",
        "run_id": manifest["run_id"],
        "issue_number": module.ISSUE_NUMBER,
        "provider": "pi",
        "capture_id": entry["id"],
        "stage": stage,
        "coordinator": {
            "source": "hephaestus.automation.pipeline.coordinator",
            "pipeline_stage": module.PI_STAGE_COORDINATOR_STAGES[stage],
            "job_kind": module.PI_STAGE_COORDINATOR_JOBS[stage],
            "sequence": sequence,
            "outcome": "success",
            "receipt_id": module._sha256_text(f"coordinator-{stage}"),
            "worker_id": f"worker-{sequence}",
            "completed_at": f"2026-08-10T00:00:{sequence:02d}Z",
        },
        "provider_evidence": {
            "execution_policy": policy,
            "tool_scopes": policy["tool_scopes"],
            "skill_calls": policy["skill_calls"],
            "session_ids": session_ids,
            "invocation_id": module._sha256_text(f"invocation-{stage}"),
            "stdout_sha256": entry["stdout_digest"],
            "stderr_sha256": entry["stderr_digest"],
            "session_binding_sha256": binding_sha,
            "resumed_from_capture_id": resumed_from,
        },
        "worktree": {
            "root": str(Path(manifest["repo_root"]).resolve()),
            "git_dir": str(Path(manifest["repo_root"]).resolve() / ".git/worktrees/fixture"),
            "git_common_dir": str(Path(manifest["repo_root"]).resolve() / ".git"),
            "isolated": True,
            "branch": "2519-pi-e2e",
            "head": head,
            "clean": stage not in {"implementation", "tests"},
            "status_sha256": module._sha256_text(f"status-{stage}"),
            "observed_at": f"2026-08-10T00:00:{sequence:02d}Z",
        },
        "lifecycle": lifecycle,
    }
    relative_path = Path("commands") / entry["id"] / "stage-receipt.json"
    receipt_path = run_dir / relative_path
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    receipt_path.write_bytes(content)
    entry["stage_receipt"] = {
        "artifact": relative_path.as_posix(),
        "sha256": module._sha256_bytes(content),
    }


def _manifest_with_stage_receipts(
    module: ModuleType,
    tmp_path: Path,
) -> tuple[Path, dict[str, Any]]:
    _, run_dir = _bootstrap_run(module, tmp_path)
    manifest = module._load_manifest(run_dir)
    revision = "a" * 40
    manifest["commands"] = [
        {
            "id": f"{index:02d}-{stage}",
            "kind": "capture",
            "provider": "pi",
            "stage": stage,
            "fixture_sha256": module._fixture_digest(manifest),
            "revision": revision,
            "status": "success",
            "returncode": 0,
            "timed_out": False,
            "prompt_sha256": module._sha256_text(f"prompt-{stage}"),
            "session_ids": [],
            "skill_calls": [],
            "tool_scopes": [],
            "stdout_digest": module._sha256_text(f"stdout-{stage}"),
            "stderr_digest": module._sha256_text(f"stderr-{stage}"),
            "stdout_event_count": 1,
            "stderr_event_count": 0,
            "artifacts": {},
            "provider_invocations": [],
            "started_at": f"2026-08-10T00:00:{index:02d}Z",
            "finished_at": f"2026-08-10T00:01:{index:02d}Z",
        }
        for index, stage in enumerate(module.REQUIRED_E2E_STAGES, start=1)
    ]
    for entry in manifest["commands"]:
        _attach_stage_receipt(module, run_dir, manifest, entry)
    module._save_manifest(run_dir, manifest)
    return run_dir, manifest


def _manifest_with_completion_evidence(
    module: ModuleType,
    tmp_path: Path,
) -> tuple[Path, dict[str, Any]]:
    run_dir, manifest = _manifest_with_stage_receipts(module, tmp_path)
    revision = "a" * 40
    manifest["snapshots"] = [
        {
            "label": "fixture",
            "artifact": "artifacts/fixture.json",
            "created_at": "2026-08-10T00:00:00Z",
            "head": revision,
            "branch": "2519-pi-e2e",
            "status": "",
        }
    ]
    manifest["pi"] = {
        "version": "pi 0.80.2",
        "binary": "/admitted/pi",
        "skill_commands": list(module.REQUIRED_SKILL_COMMANDS),
        "package_inventory": {
            "ready": True,
            "status": "ready",
            "detail": "",
            "roots": {},
            "scopes": {},
        },
    }
    planning = next(entry for entry in manifest["commands"] if entry["stage"] == "planning")
    control = {
        **planning,
        "id": "09-control",
        "kind": "capture",
        "provider": "codex",
        "session_ids": ["private-control-session"],
        "skill_calls": [],
        "tool_scopes": ["read"],
        "provider_invocations": [],
    }
    control.pop("stage_receipt", None)
    control.pop("execution_policy", None)
    _attach_capture_artifacts(
        module,
        run_dir,
        control,
        stdout="stdout-planning",
        stderr="stderr-planning",
    )
    manifest["commands"].append(control)
    module._save_manifest(run_dir, manifest)
    assert (
        module._record_comparison(
            run_dir,
            pi_entry_id=planning["id"],
            control_entry_id=control["id"],
        )
        == 0
    )
    return run_dir, module._load_manifest(run_dir)


def _rewrite_stage_receipt(
    module: ModuleType,
    run_dir: Path,
    entry: dict[str, Any],
    mutate: Any,
) -> dict[str, Any]:
    receipt_path = run_dir / entry["stage_receipt"]["artifact"]
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    mutate(payload)
    content = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    receipt_path.write_bytes(content)
    entry["stage_receipt"]["sha256"] = module._sha256_bytes(content)
    return entry


def _rewrite_athena_host_receipt(
    module: ModuleType,
    run_dir: Path,
    entry: dict[str, Any],
    mutate: Any,
) -> None:
    receipt_path = run_dir / entry["athena_host_receipt"]["artifact"]
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    mutate(payload)
    content = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    receipt_path.write_bytes(content)
    entry["athena_host_receipt"]["sha256"] = module._sha256_bytes(content)
    stage = entry.get("stage")
    lifecycle_field = (
        {
            "advise": "athena_receipt_sha256",
            "handoff": "learn_receipt_sha256",
        }.get(stage)
        if isinstance(stage, str)
        else None
    )
    if lifecycle_field is not None and "stage_receipt" in entry:
        _rewrite_stage_receipt(
            module,
            run_dir,
            entry,
            lambda payload: payload["lifecycle"].__setitem__(
                lifecycle_field,
                entry["athena_host_receipt"]["sha256"],
            ),
        )


def test_init_creates_private_manifest_and_owner_only_directory(
    tmp_path: Path,
) -> None:
    """init() must create the run tree and a versioned run.json."""
    module = _load_module()
    repo_root, run_dir = _bootstrap_run(module, tmp_path)
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

    assert repo_root.exists()
    assert manifest["schema_version"] == 1
    assert manifest["issue_number"] == 2519
    assert manifest["fixture"]["title"] == "fix(utils): reject negative byte sizes"
    assert run_dir.name == "run-1"
    assert stat.S_IMODE(run_dir.stat().st_mode) & 0o077 == 0


@pytest.mark.parametrize("run_id_kind", ("absolute", "traversal"))
def test_init_rejects_unsafe_run_ids_before_creating_the_run_root(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    run_id_kind: str,
) -> None:
    """init() must not create or chmod paths outside its configured run root."""
    module = _load_module()
    run_root = tmp_path / "build" / "pi-e2e-2519"
    escaped_dir = tmp_path / "escaped-run"
    run_id = str(escaped_dir) if run_id_kind == "absolute" else "../escaped-run"

    assert (
        module.main(
            [
                "--repo-root",
                str(tmp_path / "repo"),
                "--run-root",
                str(run_root),
                "init",
                "--run-id",
                run_id,
            ]
        )
        == 1
    )

    assert "one safe path component" in capsys.readouterr().err
    assert not run_root.exists()
    assert not escaped_dir.exists()


@pytest.mark.parametrize("run_id", ("/outside-run", "../outside-run"))
def test_later_run_resolution_rejects_unsafe_run_ids(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    run_id: str,
) -> None:
    """Commands after init must enforce the same contained run-id contract."""
    module = _load_module()
    run_root = tmp_path / "build" / "pi-e2e-2519"
    _bootstrap_run(module, tmp_path)

    assert (
        module.main(
            [
                "--run-root",
                str(run_root),
                "inventory",
                "--run-id",
                run_id,
            ]
        )
        == 1
    )

    assert "one safe path component" in capsys.readouterr().err


def test_inventory_records_pi_version_and_skill_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """inventory() must capture the exact Pi version and catalogued skills."""
    module = _load_module()
    build_root = tmp_path / "build" / "pi-e2e-2519"
    _, run_dir = _bootstrap_run(module, tmp_path)
    fake_catalog = SimpleNamespace(
        required_commands=("skill:advise", "skill:learn", "skill:pr-review")
    )
    fake_inventory = SimpleNamespace(
        ready=True,
        status="ready",
        detail="",
        roots={"athena": tmp_path / "athena"},
        scopes={"athena": "user"},
    )
    monkeypatch.setattr(module, "load_pi_package_catalog", lambda: fake_catalog)
    monkeypatch.setattr(
        module,
        "inspect_pi_package_inventory",
        lambda *args, **kwargs: fake_inventory,
    )
    monkeypatch.setattr(module, "_probe_command_version", lambda binary: f"{binary} 0.80.2")

    assert (
        module.main(
            [
                "--repo-root",
                str(tmp_path / "repo"),
                "--run-root",
                str(build_root),
                "inventory",
                "--run-id",
                run_dir.name,
            ]
        )
        == 0
    )

    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert manifest["pi"]["version"] == "pi 0.80.2"
    assert manifest["pi"]["skill_commands"] == ["skill:advise", "skill:learn", "skill:pr-review"]
    assert manifest["pi"]["package_inventory"]["ready"] is True
    assert (run_dir / "artifacts" / "inventory.json").is_file()


def test_capture_rejects_generic_direct_pi_command(tmp_path: Path) -> None:
    """Pi evidence must never execute a caller-supplied command directly."""
    module = _load_module()
    repo_root, run_dir = _bootstrap_run(module, tmp_path)
    marker = tmp_path / "direct-pi-command-ran"

    rc = module.main(
        [
            "--repo-root",
            str(repo_root),
            "--run-root",
            str(tmp_path / "build" / "pi-e2e-2519"),
            "capture",
            "--run-id",
            run_dir.name,
            "--stage",
            "planning",
            "--provider",
            "pi",
            "--prompt",
            "plan the fixture",
            "--",
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch()",
        ]
    )

    assert rc == 1
    assert not marker.exists()
    assert module._load_manifest(run_dir)["commands"] == []


def test_failure_probe_rejects_generic_direct_pi_command(tmp_path: Path) -> None:
    """Failure probes share the Pi direct-command rejection boundary."""
    module = _load_module()
    repo_root, run_dir = _bootstrap_run(module, tmp_path)
    marker = tmp_path / "direct-pi-failure-probe-ran"

    rc = module.main(
        [
            "--repo-root",
            str(repo_root),
            "--run-root",
            str(tmp_path / "build" / "pi-e2e-2519"),
            "failure-probe",
            "--run-id",
            run_dir.name,
            "--stage",
            "planning",
            "--provider",
            "pi",
            "--prompt",
            "prove the fixture fails",
            "--",
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch()",
        ]
    )

    assert rc == 1
    assert not marker.exists()
    assert module._load_manifest(run_dir)["commands"] == []


def test_pi_one_shot_capture_routes_through_runtime_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """One-shot Pi captures must use resolve_agent and an ExecutionRequest."""
    module = _load_module()
    repo_root, run_dir = _bootstrap_run(module, tmp_path)
    runtime_calls: list[tuple[str, Any]] = []

    def _resolve_agent(agent: str, *, cwd: Path) -> str:
        assert cwd == repo_root
        runtime_calls.append(("resolve", agent))
        return agent

    def _run_agent_text(
        agent: str,
        prompt: str,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert agent == "pi"
        assert prompt == "advise on the fixture"
        runtime_calls.append(("text", kwargs["execution_request"]))
        return subprocess.CompletedProcess(
            args=["pi", "--mode", "json"],
            returncode=0,
            stdout=json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": "skill:advise",
                    },
                }
            )
            + "\n",
            stderr="",
        )

    def _reject_subprocess_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Pi capture must not execute subprocess.run directly")

    monkeypatch.setattr(module, "resolve_agent", _resolve_agent)
    monkeypatch.setattr(module, "run_agent_text", _run_agent_text)
    monkeypatch.setattr(module.subprocess, "run", _reject_subprocess_run)

    assert (
        module.main(
            [
                "--repo-root",
                str(repo_root),
                "--run-root",
                str(tmp_path / "build" / "pi-e2e-2519"),
                "capture",
                "--run-id",
                run_dir.name,
                "--stage",
                "advise",
                "--provider",
                "pi",
                "--prompt",
                "advise on the fixture",
            ]
        )
        == 0
    )

    assert runtime_calls[0] == ("resolve", "pi")
    request = runtime_calls[1][1]
    assert request.role.value == "advisor"
    assert request.operation.value == "advise"
    assert request.lifecycle.value == "one_shot"
    capture = module._load_manifest(run_dir)["commands"][0]
    analysis = json.loads((run_dir / capture["artifacts"]["analysis"]).read_text(encoding="utf-8"))
    assert analysis["command"] == []
    assert capture["execution_policy"] == {
        "role": "advisor",
        "operation": "advise",
        "lifecycle": "one_shot",
        "filesystem": "knowledge_ro",
        "network": "provider_relay",
    }
    assert capture["skill_calls"] == ["skill:advise"]


def test_capture_records_session_ids_tool_scopes_and_comparison_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """capture() must preserve provider output semantics and extract evidence."""
    module = _load_module()
    repo_root, run_dir = _bootstrap_run(module, tmp_path)
    provider_dir = tmp_path / "providers"
    provider_dir.mkdir()
    codex_real = provider_dir / "codex-real"
    _write_provider(codex_real)
    monkeypatch.setattr(
        module.shutil,
        "which",
        lambda name: str(codex_real) if name == "codex" else None,
    )
    runtime_calls: list[tuple[str, Any]] = []

    def _resolve_agent(agent: str, *, cwd: Path) -> str:
        assert cwd == repo_root
        runtime_calls.append(("resolve", agent))
        return agent

    def _run_agent_session(
        agent: str,
        prompt: str,
        **kwargs: object,
    ) -> object:
        assert agent == "pi"
        assert prompt == "capture prompt"
        runtime_calls.append(("session", kwargs["execution_request"]))
        stdout = (
            "\n".join(
                (
                    json.dumps({"type": "session", "id": "session-123"}),
                    json.dumps({"type": "session_meta", "payload": {"id": "session-123"}}),
                    json.dumps(
                        {
                            "type": "message_end",
                            "message": {
                                "role": "assistant",
                                "content": "skill:advise skill:pr-review",
                            },
                        }
                    ),
                )
            )
            + "\n"
        )
        return module.AgentRunResult(
            stdout=stdout,
            stderr="",
            session_id="session-123",
        )

    monkeypatch.setattr(module, "resolve_agent", _resolve_agent)
    monkeypatch.setattr(module, "run_agent_session", _run_agent_session)
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("capture prompt", encoding="utf-8")
    argv_file = tmp_path / "argv.json"
    signal_file = tmp_path / "signal.txt"
    base_args = [
        "--repo-root",
        str(repo_root),
        "--run-root",
        str(tmp_path / "build" / "pi-e2e-2519"),
    ]
    manifest = module._load_manifest(run_dir)
    manifest["snapshots"] = [
        {
            "label": "capture-revision",
            "head": "a" * 40,
        }
    ]
    module._save_manifest(run_dir, manifest)

    first = [
        *base_args,
        "capture",
        "--run-id",
        run_dir.name,
        "--stage",
        "planning",
        "--provider",
        "pi",
        "--prompt-file",
        str(prompt_file),
    ]
    assert module.main(first) == 0

    second = [
        *base_args,
        "capture",
        "--run-id",
        run_dir.name,
        "--stage",
        "planning",
        "--provider",
        "codex",
        "--prompt-file",
        str(prompt_file),
        "--",
        "codex",
        str(argv_file),
        str(signal_file),
        "emit",
        "--tools",
        "read,grep",
        "--commands",
        "skill:learn,skill:pr-review",
    ]
    assert module.main(second) == 0

    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    captures = [entry for entry in manifest["commands"] if entry["kind"] == "capture"]
    assert {entry["provider"] for entry in captures} == {"pi", "codex"}
    assert captures[0]["prompt_sha256"] == module._prompt_digest("capture prompt")
    assert "session-123" in captures[0]["session_ids"]
    assert "skill:advise" in captures[0]["skill_calls"]
    assert "skill:pr-review" in captures[0]["skill_calls"]
    assert "read" in captures[0]["tool_scopes"]
    assert "grep" in captures[0]["tool_scopes"]
    assert "skill:pr-review" in captures[1]["skill_calls"]
    assert captures[0]["provider_invocations"] == []
    assert captures[0]["execution_policy"] == {
        "role": "planner",
        "operation": "plan",
        "lifecycle": "start_new",
        "filesystem": "checkout_ro",
        "network": "provider_relay",
    }
    assert runtime_calls[0] == ("resolve", "pi")
    request = runtime_calls[1][1]
    assert request.role.value == "planner"
    assert request.operation.value == "plan"
    assert request.lifecycle.value == "start_new"
    assert (run_dir / "commands").is_dir()
    assert (run_dir / "commands" / "01-planning").exists()
    snapshot_results = iter(
        [
            subprocess.CompletedProcess([], 0, "main\n", ""),
            subprocess.CompletedProcess([], 0, f"{'a' * 40}\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
    )
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: next(snapshot_results))
    assert (
        module.main(
            [
                "--repo-root",
                str(repo_root),
                "--run-root",
                str(tmp_path / "build" / "pi-e2e-2519"),
                "snapshot",
                "--run-id",
                run_dir.name,
                "--label",
                "repo snapshot",
            ]
        )
        == 0
    )
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    manifest["pi"] = {
        "version": "pi 0.80.2",
        "binary": "/admitted/pi",
        "skill_commands": list(module.REQUIRED_SKILL_COMMANDS),
        "package_inventory": {
            "ready": True,
            "status": "ready",
            "detail": "",
            "roots": {},
            "scopes": {},
        },
    }
    module._save_manifest(run_dir, manifest)
    assert (
        module.main(
            [
                *base_args,
                "record-comparison",
                "--run-id",
                run_dir.name,
                "--pi-entry",
                captures[0]["id"],
                "--control-entry",
                captures[1]["id"],
            ]
        )
        == 0
    )
    manifest = module._load_manifest(run_dir)
    comparison = manifest["comparisons"][0]
    assert comparison["stage"] == "planning"
    assert comparison["revision"] == "a" * 40
    assert comparison["fixture_sha256"] == module._fixture_digest(manifest)
    assert comparison["pi_outcome"]["status"] == "success"
    assert comparison["control_outcome"]["status"] == "success"
    assert comparison["outcomes_match"] is True
    assert comparison["artifact_comparison"] == {
        "stdout_matches": True,
        "stderr_matches": True,
    }
    assert (
        module._record_comparison(
            run_dir,
            pi_entry_id=captures[0]["id"],
            control_entry_id=captures[1]["id"],
        )
        == 0
    )
    assert len(module._load_manifest(run_dir)["comparisons"]) == 1
    assert (
        module.main(
            [
                "--repo-root",
                str(repo_root),
                "--run-root",
                str(tmp_path / "build" / "pi-e2e-2519"),
                "verify",
                "--run-id",
                run_dir.name,
                "--criterion",
                "workflow",
            ]
        )
        == 0
    )
    assert (
        module.main(
            [
                "--repo-root",
                str(repo_root),
                "--run-root",
                str(tmp_path / "build" / "pi-e2e-2519"),
                "verify",
                "--run-id",
                run_dir.name,
                "--criterion",
                "mnemosyne",
            ]
        )
        == 1
    )
    assert (
        module.main(
            [
                "--repo-root",
                str(repo_root),
                "--run-root",
                str(tmp_path / "build" / "pi-e2e-2519"),
                "verify",
                "--run-id",
                run_dir.name,
                "--criterion",
                "comparison",
            ]
        )
        == 0
    )
    manifest = module._load_manifest(run_dir)
    pi_capture = next(entry for entry in manifest["commands"] if entry.get("provider") == "pi")
    pi_capture["returncode"] = 9
    pi_capture["status"] = "failure"
    module._save_manifest(run_dir, manifest)
    assert (
        module.main(
            [
                *base_args,
                "record-comparison",
                "--run-id",
                run_dir.name,
                "--pi-entry",
                captures[0]["id"],
                "--control-entry",
                captures[1]["id"],
            ]
        )
        == 1
    )
    assert (
        module.main(
            [
                *base_args,
                "verify",
                "--run-id",
                run_dir.name,
                "--criterion",
                "comparison",
            ]
        )
        == 1
    )


def test_mnemosyne_verification_ignores_inventory_catalog_skill_calls(
    tmp_path: Path,
) -> None:
    """Mnemosyne verification must use observed capture evidence only."""
    module = _load_module()
    _, run_dir = _bootstrap_run(module, tmp_path)
    manifest = module._load_manifest(run_dir)
    manifest["commands"] = [
        {
            "id": "inventory-01",
            "kind": "inventory",
            "provider": "pi",
            "skill_calls": list(module.REQUIRED_SKILL_COMMANDS),
        },
        {
            "id": "01-planning",
            "kind": "capture",
            "provider": "pi",
            "stage": "planning",
            "skill_calls": ["skill:advise", "skill:learn"],
        },
    ]
    module._save_manifest(run_dir, manifest)

    assert (
        module.main(
            [
                "--repo-root",
                str(tmp_path / "repo"),
                "--run-root",
                str(tmp_path / "build" / "pi-e2e-2519"),
                "verify",
                "--run-id",
                run_dir.name,
                "--criterion",
                "mnemosyne",
            ]
        )
        == 1
    )


def test_mnemosyne_verification_rejects_forged_provider_prose(
    tmp_path: Path,
) -> None:
    """Provider-controlled skill names are not Athena/Mnemosyne receipts."""
    module = _load_module()
    _, run_dir = _bootstrap_run(module, tmp_path)
    manifest = module._load_manifest(run_dir)
    manifest["commands"] = [
        {
            "id": "01-advise",
            "kind": "capture",
            "provider": "pi",
            "stage": "advise",
            "status": "success",
            "skill_calls": list(module.REQUIRED_SKILL_COMMANDS),
        },
        {
            "id": "02-review",
            "kind": "capture",
            "provider": "pi",
            "stage": "review",
            "status": "success",
            "skill_calls": ["skill:pr-review"],
        },
        {
            "id": "03-handoff",
            "kind": "capture",
            "provider": "pi",
            "stage": "handoff",
            "status": "success",
            "skill_calls": ["skill:learn"],
        },
    ]
    module._save_manifest(run_dir, manifest)

    assert (
        module.main(
            [
                "--repo-root",
                str(tmp_path / "repo"),
                "--run-root",
                str(tmp_path / "build" / "pi-e2e-2519"),
                "verify",
                "--run-id",
                run_dir.name,
                "--criterion",
                "mnemosyne",
            ]
        )
        == 1
    )


def test_mnemosyne_verification_rejects_requested_command_grants(
    tmp_path: Path,
) -> None:
    """Requested --commands grants are not Athena/Mnemosyne receipts."""
    module = _load_module()
    _, run_dir = _bootstrap_run(module, tmp_path)
    manifest = module._load_manifest(run_dir)
    manifest["commands"] = [
        {
            "id": "01-advise",
            "kind": "capture",
            "provider": "pi",
            "stage": "advise",
            "status": "success",
            "tool_scopes": list(module.REQUIRED_SKILL_COMMANDS),
            "provider_invocations": [
                {
                    "tool": "pi",
                    "argv": [
                        "pi",
                        "--commands",
                        ",".join(module.REQUIRED_SKILL_COMMANDS),
                    ],
                }
            ],
        },
        {
            "id": "02-review",
            "kind": "capture",
            "provider": "pi",
            "stage": "review",
            "status": "success",
            "tool_scopes": ["skill:pr-review"],
        },
        {
            "id": "03-handoff",
            "kind": "capture",
            "provider": "pi",
            "stage": "handoff",
            "status": "success",
            "tool_scopes": ["skill:learn"],
        },
    ]
    module._save_manifest(run_dir, manifest)

    assert (
        module.main(
            [
                "--repo-root",
                str(tmp_path / "repo"),
                "--run-root",
                str(tmp_path / "build" / "pi-e2e-2519"),
                "verify",
                "--run-id",
                run_dir.name,
                "--criterion",
                "mnemosyne",
            ]
        )
        == 1
    )


def test_mnemosyne_verification_accepts_pi_bound_host_and_stage_receipts(tmp_path: Path) -> None:
    """Canonical stage, advise, review, and PR-readback learn receipts are sufficient."""
    module = _load_module()
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    run_dir, manifest = _manifest_with_stage_receipts(module, evidence_root)

    assert (
        module.main(
            [
                "--repo-root",
                manifest["repo_root"],
                "--run-root",
                str(run_dir.parent),
                "verify",
                "--run-id",
                run_dir.name,
                "--criterion",
                "mnemosyne",
            ]
        )
        == 0
    )


def test_mnemosyne_verification_rejects_missing_review_stage_receipt(
    tmp_path: Path,
) -> None:
    """Advise and learn receipts cannot substitute for the Pi review receipt."""
    module = _load_module()
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    run_dir, manifest = _manifest_with_stage_receipts(module, evidence_root)
    review = next(entry for entry in manifest["commands"] if entry["stage"] == "review")
    review.pop("stage_receipt")
    module._save_manifest(run_dir, manifest)

    assert (
        module.main(
            [
                "--repo-root",
                manifest["repo_root"],
                "--run-root",
                str(run_dir.parent),
                "verify",
                "--run-id",
                run_dir.name,
                "--criterion",
                "mnemosyne",
            ]
        )
        == 1
    )


def test_mnemosyne_verification_rejects_learn_receipt_without_matching_readback(
    tmp_path: Path,
) -> None:
    """A PR-looking delivery receipt fails when host readback does not match."""
    module = _load_module()
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    run_dir, manifest = _manifest_with_stage_receipts(module, evidence_root)
    handoff = next(entry for entry in manifest["commands"] if entry["stage"] == "handoff")
    _rewrite_athena_host_receipt(
        module,
        run_dir,
        handoff,
        lambda payload: payload["host_result"]["delivery_receipt"].__setitem__(
            "readback_head_sha",
            "d" * 40,
        ),
    )
    module._save_manifest(run_dir, manifest)

    assert (
        module.main(
            [
                "--repo-root",
                manifest["repo_root"],
                "--run-root",
                str(run_dir.parent),
                "verify",
                "--run-id",
                run_dir.name,
                "--criterion",
                "mnemosyne",
            ]
        )
        == 1
    )


def test_stage_receipts_reject_noop_labels_and_pooled_evidence(tmp_path: Path) -> None:
    """Eight labels or one shared receipt cannot stand in for eight host observations."""
    module = _load_module()
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    run_dir, manifest = _manifest_with_stage_receipts(module, evidence_root)

    module._verify_stage_receipts(manifest, run_dir)

    labels_only = json.loads(json.dumps(manifest))
    for entry in labels_only["commands"]:
        entry.pop("stage_receipt", None)
    with pytest.raises(ValueError, match="artifact descriptor"):
        module._verify_stage_receipts(labels_only, run_dir)

    pooled = json.loads(json.dumps(manifest))
    pooled["commands"][1]["stage_receipt"] = pooled["commands"][0]["stage_receipt"]
    with pytest.raises(ValueError, match="not bound to its successful capture"):
        module._verify_stage_receipts(pooled, run_dir)


def test_stage_receipts_reject_duplicate_capture_ids_and_pooled_sessions(
    tmp_path: Path,
) -> None:
    """Required stages must be distinct captures with non-pooled provider sessions."""
    module = _load_module()
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    run_dir, manifest = _manifest_with_stage_receipts(module, evidence_root)

    duplicate_id = json.loads(json.dumps(manifest))
    duplicate_id["commands"][1]["id"] = duplicate_id["commands"][0]["id"]
    with pytest.raises(ValueError, match="duplicate capture ids"):
        module._verify_stage_receipts(duplicate_id, run_dir)

    discovery = next(entry for entry in manifest["commands"] if entry["stage"] == "discovery")
    review = next(entry for entry in manifest["commands"] if entry["stage"] == "review")
    review["session_ids"] = list(discovery["session_ids"])
    _rewrite_stage_receipt(
        module,
        run_dir,
        review,
        lambda payload: payload["provider_evidence"].__setitem__(
            "session_ids",
            list(discovery["session_ids"]),
        ),
    )
    with pytest.raises(ValueError, match="pooled session evidence"):
        module._verify_stage_receipts(manifest, run_dir)


def test_stage_receipts_require_per_stage_session_policy_and_artifact_readback(
    tmp_path: Path,
) -> None:
    """Provider receipt fields must be observed, policy-bound, and artifact-backed."""
    module = _load_module()
    scenarios = (
        (
            "missing-session",
            "discovery",
            lambda run_dir, entry: (
                entry.__setitem__("session_ids", []),
                _rewrite_stage_receipt(
                    module,
                    run_dir,
                    entry,
                    lambda payload: payload["provider_evidence"].__setitem__(
                        "session_ids",
                        [],
                    ),
                ),
            ),
            "observed session evidence",
        ),
        (
            "missing-policy",
            "planning",
            lambda run_dir, entry: entry.pop("execution_policy"),
            "capture policy",
        ),
        (
            "artifact-mismatch",
            "review",
            lambda run_dir, entry: (run_dir / entry["artifacts"]["stdout"]).write_text(
                "tampered stdout",
                encoding="utf-8",
            ),
            "artifact digest mismatch",
        ),
    )
    for name, stage, mutate, message in scenarios:
        evidence_root = tmp_path / name
        evidence_root.mkdir()
        run_dir, manifest = _manifest_with_stage_receipts(module, evidence_root)
        entry = next(value for value in manifest["commands"] if value["stage"] == stage)
        mutate(run_dir, entry)
        with pytest.raises(ValueError, match=message):
            module._verify_stage_receipts(manifest, run_dir)


def test_stage_receipts_ignore_failed_nonrequired_pi_captures(tmp_path: Path) -> None:
    """Stage verification requires green required stages, not every Pi evidence command."""
    module = _load_module()
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    run_dir, manifest = _manifest_with_stage_receipts(module, evidence_root)
    manifest["commands"].append(
        {
            "id": "09-boundary-probe-legacy-capture",
            "kind": "capture",
            "provider": "pi",
            "stage": "boundary-probe",
            "status": "failure",
            "returncode": 7,
            "timed_out": False,
        }
    )

    assert module._verify_stage_receipts(manifest, run_dir)


def test_record_stage_receipt_ingests_host_artifact_for_one_exact_capture(
    tmp_path: Path,
) -> None:
    """The collector can persist a coordinator export without manual manifest edits."""
    module = _load_module()
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    run_dir, manifest = _manifest_with_stage_receipts(module, evidence_root)
    planning = next(value for value in manifest["commands"] if value["stage"] == "planning")
    persisted_path = run_dir / planning["stage_receipt"]["artifact"]
    coordinator_export = tmp_path / "planning-stage-receipt.json"
    coordinator_export.write_bytes(persisted_path.read_bytes())
    planning.pop("stage_receipt")
    module._save_manifest(run_dir, manifest)

    assert (
        module.main(
            [
                "--repo-root",
                manifest["repo_root"],
                "--run-root",
                str(run_dir.parent),
                "record-stage-receipt",
                "--run-id",
                run_dir.name,
                "--capture-id",
                planning["id"],
                "--receipt",
                str(coordinator_export),
            ]
        )
        == 0
    )
    recorded = module._load_manifest(run_dir)
    recorded_planning = next(
        value for value in recorded["commands"] if value["stage"] == "planning"
    )
    assert recorded_planning["stage_receipt"]["artifact"].endswith("stage-receipt.json")
    module._verify_stage_receipts(recorded, run_dir)


def test_stage_receipts_require_worktree_test_github_and_handoff_readbacks(
    tmp_path: Path,
) -> None:
    """Completion-relevant host facts fail closed when any lifecycle receipt is absent."""
    module = _load_module()
    scenarios = (
        (
            "worktree",
            "planning",
            lambda payload: payload["worktree"].__setitem__("isolated", False),
            "worktree is not isolated",
        ),
        (
            "revision",
            "implementation",
            lambda payload: payload["worktree"].__setitem__("head", "b" * 40),
            "head does not match its capture revision",
        ),
        (
            "tests",
            "tests",
            lambda payload: payload["lifecycle"].__setitem__("returncode", 1),
            "passing host fixture-test receipt",
        ),
        (
            "github",
            "review",
            lambda payload: payload["lifecycle"]["pull_request"].__setitem__(
                "readback_verified", False
            ),
            "pull-request readback evidence",
        ),
        (
            "handoff",
            "handoff",
            lambda payload: payload["lifecycle"].__setitem__("finished_handoff", False),
            "learning delivery evidence",
        ),
    )
    for name, stage, mutate, message in scenarios:
        scenario_root = tmp_path / name
        scenario_root.mkdir()
        run_dir, manifest = _manifest_with_stage_receipts(module, scenario_root)
        entry = next(value for value in manifest["commands"] if value["stage"] == stage)
        _rewrite_stage_receipt(module, run_dir, entry, mutate)
        with pytest.raises(ValueError, match=message):
            module._verify_stage_receipts(manifest, run_dir)


def test_comparison_verification_rejects_unpaired_provider_labels(
    tmp_path: Path,
) -> None:
    """Provider labels alone must not count as paired comparison evidence."""
    module = _load_module()
    _, run_dir = _bootstrap_run(module, tmp_path)
    manifest = module._load_manifest(run_dir)
    manifest["commands"] = [
        {
            "id": "01-pi-planning",
            "kind": "capture",
            "provider": "pi",
            "stage": "planning",
            "status": "success",
        },
        {
            "id": "02-codex-review",
            "kind": "capture",
            "provider": "codex",
            "stage": "review",
            "status": "success",
        },
    ]
    module._save_manifest(run_dir, manifest)

    assert (
        module.main(
            [
                "--repo-root",
                str(tmp_path / "repo"),
                "--run-root",
                str(tmp_path / "build" / "pi-e2e-2519"),
                "verify",
                "--run-id",
                run_dir.name,
                "--criterion",
                "comparison",
            ]
        )
        == 1
    )


@pytest.mark.parametrize(
    ("field", "control_value"),
    [
        ("fixture_sha256", "0" * 64),
        ("stage", "review"),
        ("revision", "b" * 40),
        ("prompt_sha256", "c" * 64),
    ],
)
def test_record_comparison_rejects_unpaired_capture_metadata(
    tmp_path: Path,
    field: str,
    control_value: str,
) -> None:
    """A persisted pair must describe the same fixture, stage, revision, and prompt."""
    module = _load_module()
    _, run_dir = _bootstrap_run(module, tmp_path)
    manifest = module._load_manifest(run_dir)
    revision = "a" * 40
    manifest["snapshots"] = [{"label": "fixture", "head": revision}]
    capture = {
        "kind": "capture",
        "stage": "planning",
        "fixture_sha256": module._fixture_digest(manifest),
        "revision": revision,
        "status": "success",
        "returncode": 0,
        "timed_out": False,
        "prompt_sha256": module._prompt_digest("fixture prompt"),
        "stdout_digest": "a" * 64,
        "stderr_digest": "b" * 64,
    }
    control = {**capture, "id": "02-control", "provider": "codex"}
    control[field] = control_value
    manifest["commands"] = [
        {**capture, "id": "01-pi", "provider": "pi"},
        control,
    ]
    module._save_manifest(run_dir, manifest)

    assert (
        module.main(
            [
                "--repo-root",
                str(tmp_path / "repo"),
                "--run-root",
                str(tmp_path / "build" / "pi-e2e-2519"),
                "record-comparison",
                "--run-id",
                run_dir.name,
                "--pi-entry",
                "01-pi",
                "--control-entry",
                "02-control",
            ]
        )
        == 1
    )
    assert module._load_manifest(run_dir)["comparisons"] == []


@pytest.mark.parametrize(
    ("pi_provider", "control_provider"),
    [
        ("codex", "codex"),
        ("pi", "pi"),
    ],
)
def test_record_comparison_rejects_unpaired_provider_roles(
    tmp_path: Path,
    pi_provider: str,
    control_provider: str,
) -> None:
    """The persisted pair must identify exactly one Pi capture and one control capture."""
    module = _load_module()
    _, run_dir = _bootstrap_run(module, tmp_path)
    manifest = module._load_manifest(run_dir)
    revision = "a" * 40
    manifest["snapshots"] = [{"label": "fixture", "head": revision}]
    capture = {
        "kind": "capture",
        "stage": "planning",
        "fixture_sha256": module._fixture_digest(manifest),
        "revision": revision,
        "status": "success",
        "returncode": 0,
        "timed_out": False,
        "prompt_sha256": module._prompt_digest("fixture prompt"),
    }
    manifest["commands"] = [
        {**capture, "id": "01-pi", "provider": pi_provider},
        {**capture, "id": "02-control", "provider": control_provider},
    ]
    module._save_manifest(run_dir, manifest)

    assert (
        module.main(
            [
                "--repo-root",
                str(tmp_path / "repo"),
                "--run-root",
                str(tmp_path / "build" / "pi-e2e-2519"),
                "record-comparison",
                "--run-id",
                run_dir.name,
                "--pi-entry",
                "01-pi",
                "--control-entry",
                "02-control",
            ]
        )
        == 1
    )
    assert module._load_manifest(run_dir)["comparisons"] == []


def test_comparison_verification_rejects_different_outcomes(
    tmp_path: Path,
) -> None:
    """A recorded artifact comparison cannot certify divergent provider outcomes."""
    module = _load_module()
    _, run_dir = _bootstrap_run(module, tmp_path)
    manifest = module._load_manifest(run_dir)
    revision = "a" * 40
    manifest["snapshots"] = [{"label": "fixture", "head": revision}]
    capture = {
        "kind": "capture",
        "stage": "planning",
        "fixture_sha256": module._fixture_digest(manifest),
        "revision": revision,
        "timed_out": False,
        "prompt_sha256": module._prompt_digest("fixture prompt"),
        "stdout_digest": "a" * 64,
        "stderr_digest": "b" * 64,
    }
    manifest["commands"] = [
        {
            **capture,
            "id": "01-pi",
            "provider": "pi",
            "status": "success",
            "returncode": 0,
        },
        {
            **capture,
            "id": "02-control",
            "provider": "codex",
            "status": "failure",
            "returncode": 7,
        },
    ]
    for entry in manifest["commands"]:
        _attach_capture_artifacts(
            module,
            run_dir,
            entry,
            stdout="shared stdout",
            stderr="shared stderr",
        )
    module._save_manifest(run_dir, manifest)
    base_args = [
        "--repo-root",
        str(tmp_path / "repo"),
        "--run-root",
        str(tmp_path / "build" / "pi-e2e-2519"),
    ]

    assert (
        module.main(
            [
                *base_args,
                "record-comparison",
                "--run-id",
                run_dir.name,
                "--pi-entry",
                "01-pi",
                "--control-entry",
                "02-control",
            ]
        )
        == 0
    )
    comparison = module._load_manifest(run_dir)["comparisons"][0]
    assert comparison["pi_outcome"]["status"] == "success"
    assert comparison["control_outcome"]["status"] == "failure"
    assert comparison["outcomes_match"] is False
    assert (
        module.main(
            [
                *base_args,
                "verify",
                "--run-id",
                run_dir.name,
                "--criterion",
                "comparison",
            ]
        )
        == 1
    )


def test_comparison_verification_rejects_success_artifact_mismatch(
    tmp_path: Path,
) -> None:
    """Successful paired runs must compare artifact digests, not just outcomes."""
    module = _load_module()
    _, run_dir = _bootstrap_run(module, tmp_path)
    manifest = module._load_manifest(run_dir)
    revision = "a" * 40
    manifest["snapshots"] = [{"label": "fixture", "head": revision}]
    capture = {
        "kind": "capture",
        "stage": "planning",
        "fixture_sha256": module._fixture_digest(manifest),
        "revision": revision,
        "status": "success",
        "returncode": 0,
        "timed_out": False,
        "prompt_sha256": module._prompt_digest("fixture prompt"),
        "session_ids": [],
        "skill_calls": [],
        "tool_scopes": [],
    }
    manifest["commands"] = [
        {**capture, "id": "01-pi", "provider": "pi"},
        {**capture, "id": "02-control", "provider": "codex"},
    ]
    _attach_capture_artifacts(module, run_dir, manifest["commands"][0], stdout="pi stdout")
    _attach_capture_artifacts(module, run_dir, manifest["commands"][1], stdout="control stdout")
    module._save_manifest(run_dir, manifest)
    base_args = [
        "--repo-root",
        str(tmp_path / "repo"),
        "--run-root",
        str(tmp_path / "build" / "pi-e2e-2519"),
    ]

    assert (
        module.main(
            [
                *base_args,
                "record-comparison",
                "--run-id",
                run_dir.name,
                "--pi-entry",
                "01-pi",
                "--control-entry",
                "02-control",
            ]
        )
        == 0
    )
    comparison = module._load_manifest(run_dir)["comparisons"][0]
    assert comparison["outcomes_match"] is True
    assert comparison["artifact_comparison"]["stdout_matches"] is False
    assert (
        module.main(
            [
                *base_args,
                "verify",
                "--run-id",
                run_dir.name,
                "--criterion",
                "comparison",
            ]
        )
        == 1
    )


def test_record_comparison_rejects_tampered_artifact_digest(
    tmp_path: Path,
) -> None:
    """A stale comparison record cannot survive artifact-file tampering."""
    module = _load_module()
    _, run_dir = _bootstrap_run(module, tmp_path)
    manifest = module._load_manifest(run_dir)
    revision = "a" * 40
    manifest["snapshots"] = [{"label": "fixture", "head": revision}]
    capture = {
        "kind": "capture",
        "stage": "planning",
        "fixture_sha256": module._fixture_digest(manifest),
        "revision": revision,
        "status": "success",
        "returncode": 0,
        "timed_out": False,
        "prompt_sha256": module._prompt_digest("fixture prompt"),
        "session_ids": [],
        "skill_calls": [],
        "tool_scopes": [],
    }
    manifest["commands"] = [
        {**capture, "id": "01-pi", "provider": "pi"},
        {**capture, "id": "02-control", "provider": "codex"},
    ]
    for entry in manifest["commands"]:
        _attach_capture_artifacts(module, run_dir, entry, stdout="shared stdout")
    module._save_manifest(run_dir, manifest)
    base_args = [
        "--repo-root",
        str(tmp_path / "repo"),
        "--run-root",
        str(tmp_path / "build" / "pi-e2e-2519"),
    ]

    assert (
        module.main(
            [
                *base_args,
                "record-comparison",
                "--run-id",
                run_dir.name,
                "--pi-entry",
                "01-pi",
                "--control-entry",
                "02-control",
            ]
        )
        == 0
    )
    pi_stdout = run_dir / module._load_manifest(run_dir)["commands"][0]["artifacts"]["stdout"]
    pi_stdout.write_text("tampered stdout", encoding="utf-8")
    assert (
        module.main(
            [
                *base_args,
                "record-comparison",
                "--run-id",
                run_dir.name,
                "--pi-entry",
                "01-pi",
                "--control-entry",
                "02-control",
            ]
        )
        == 1
    )
    assert (
        module.main(
            [
                *base_args,
                "verify",
                "--run-id",
                run_dir.name,
                "--criterion",
                "comparison",
            ]
        )
        == 1
    )


def test_provider_proxy_preserves_argv_and_signal_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The generated proxy must exec the real provider and pass SIGTERM through."""
    module = _load_module()
    _, run_dir = _bootstrap_run(module, tmp_path)
    proxy_dir = module._prepare_provider_proxy_dir(run_dir)
    real_provider = tmp_path / "real-pi"
    _write_provider(real_provider)
    monkeypatch.setenv("HEPH_PI_E2E_REAL_PI", str(real_provider))
    monkeypatch.setenv("HEPH_PI_E2E_PROXY_LOG", str(proxy_dir / "provider-proxy.jsonl"))
    argv_file = tmp_path / "argv.json"
    signal_file = tmp_path / "signal.txt"

    proc = subprocess.Popen(
        [str(proxy_dir / "pi"), str(argv_file), str(signal_file), "wait"],
        cwd=tmp_path,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(100):
            if argv_file.exists():
                break
            time.sleep(0.05)
        assert argv_file.exists(), "the real provider did not receive argv"
        proc.terminate()
        assert proc.wait(timeout=10) == 128 + signal.SIGTERM
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    assert json.loads(argv_file.read_text(encoding="utf-8")) == [
        str(argv_file),
        str(signal_file),
        "wait",
    ]
    assert signal_file.read_text(encoding="utf-8") == str(signal.SIGTERM)
    proxy_log = proxy_dir / "provider-proxy.jsonl"
    proxy_events = json.loads(proxy_log.read_text(encoding="utf-8").splitlines()[0])
    assert proxy_events["tool"] == "pi"
    assert proxy_events["real_binary"] == str(real_provider)


def test_failure_probe_records_a_failure_as_a_successful_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """failure-probe() records expected nonzero evidence without becoming a capture."""
    module = _load_module()
    _, run_dir = _bootstrap_run(module, tmp_path)
    provider_dir = tmp_path / "providers"
    provider_dir.mkdir()
    failing_real = provider_dir / "codex-real"
    failing_real.write_text(
        (
            "#!/usr/bin/env python3\n"
            "from __future__ import annotations\n"
            "import sys\n"
            "raise SystemExit(7)\n"
        ),
        encoding="utf-8",
    )
    failing_real.chmod(0o700)

    def _which(name: str) -> str | None:
        return str(failing_real) if name == "codex" else None

    monkeypatch.setattr(module.shutil, "which", _which)

    rc = module.main(
        [
            "--repo-root",
            str(tmp_path / "repo"),
            "--run-root",
            str(tmp_path / "build" / "pi-e2e-2519"),
            "failure-probe",
            "--run-id",
            run_dir.name,
            "--stage",
            "review",
            "--provider",
            "codex",
            "--",
            "codex",
            str(tmp_path / "argv.json"),
            str(tmp_path / "signal.txt"),
            "emit",
        ]
    )

    assert rc == 0
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    probe = manifest["commands"][-1]
    assert probe["kind"] == "failure_probe"
    assert probe["evidence_kind"] == "expected_failure_probe"
    assert probe["status"] == "expected_failure"
    assert probe["returncode"] == 7
    assert probe["expected_outcome"] == {
        "returncode": "nonzero",
        "timed_out": False,
    }
    assert probe["observed_outcome"] == {
        "returncode": 7,
        "timed_out": False,
    }
    assert probe["validation"] == {
        "matches_expectation": True,
        "result": "matched_expected_nonzero",
    }


def test_failure_probe_verification_rejects_an_unexpected_success() -> None:
    """An unexpected probe success remains a fail-closed completion failure."""
    module = _load_module()
    manifest = {
        "commands": [
            {
                "id": "01-boundary-probe",
                "kind": "failure_probe",
                "evidence_kind": "expected_failure_probe",
                "provider": "pi",
                "status": "unexpected_success",
                "returncode": 0,
                "timed_out": False,
                "expected_outcome": {
                    "returncode": "nonzero",
                    "timed_out": False,
                },
                "observed_outcome": {
                    "returncode": 0,
                    "timed_out": False,
                },
                "validation": {
                    "matches_expectation": False,
                    "result": "unexpected_success",
                },
            }
        ]
    }

    with pytest.raises(ValueError, match="failure probe did not match expected nonzero outcome"):
        module._verify_failure_probes(manifest)


def test_completion_accepts_expected_failure_probe_as_distinct_evidence(tmp_path: Path) -> None:
    """A matched Pi failure probe must not be rejected as a failed workflow stage."""
    module = _load_module()
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    run_dir, manifest = _manifest_with_completion_evidence(module, evidence_root)
    manifest["commands"].append(
        {
            "id": "10-review-boundary-probe",
            "kind": "failure_probe",
            "evidence_kind": "expected_failure_probe",
            "provider": "pi",
            "stage": "review",
            "fixture_sha256": module._fixture_digest(manifest),
            "revision": "a" * 40,
            "status": "expected_failure",
            "returncode": 7,
            "timed_out": False,
            "expected_outcome": {
                "returncode": "nonzero",
                "timed_out": False,
            },
            "observed_outcome": {
                "returncode": 7,
                "timed_out": False,
            },
            "validation": {
                "matches_expectation": True,
                "result": "matched_expected_nonzero",
            },
        }
    )
    module._save_manifest(run_dir, manifest)

    assert module._verify_completion(module._load_manifest(run_dir), run_dir)


def test_completion_rejects_unexpected_successful_failure_probe(tmp_path: Path) -> None:
    """A failure probe that exits zero blocks completion even with all stages green."""
    module = _load_module()
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    run_dir, manifest = _manifest_with_completion_evidence(module, evidence_root)
    manifest["commands"].append(
        {
            "id": "10-review-boundary-probe",
            "kind": "failure_probe",
            "evidence_kind": "expected_failure_probe",
            "provider": "pi",
            "stage": "review",
            "fixture_sha256": module._fixture_digest(manifest),
            "revision": "a" * 40,
            "status": "unexpected_success",
            "returncode": 0,
            "timed_out": False,
            "expected_outcome": {
                "returncode": "nonzero",
                "timed_out": False,
            },
            "observed_outcome": {
                "returncode": 0,
                "timed_out": False,
            },
            "validation": {
                "matches_expectation": False,
                "result": "unexpected_success",
            },
        }
    )
    module._save_manifest(run_dir, manifest)

    with pytest.raises(ValueError, match="failure probe did not match expected nonzero outcome"):
        module._verify_completion(module._load_manifest(run_dir), run_dir)


def test_capture_timeout_records_a_bounded_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """capture() must record a timed-out provider stage as a failure."""
    module = _load_module()
    repo_root, run_dir = _bootstrap_run(module, tmp_path)
    provider_dir = tmp_path / "providers"
    provider_dir.mkdir()
    codex_real = provider_dir / "codex-real"
    _write_provider(codex_real)
    monkeypatch.setattr(
        module.shutil,
        "which",
        lambda name: str(codex_real) if name == "codex" else None,
    )

    assert (
        module.main(
            [
                "--repo-root",
                str(repo_root),
                "--run-root",
                str(tmp_path / "build" / "pi-e2e-2519"),
                "capture",
                "--run-id",
                run_dir.name,
                "--stage",
                "planning",
                "--provider",
                "codex",
                "--timeout",
                "1",
                "--",
                "codex",
                str(tmp_path / "argv.json"),
                str(tmp_path / "signal.txt"),
                "wait",
            ]
        )
        == 124
    )

    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    capture = manifest["commands"][-1]
    assert capture["status"] == "failure"
    assert capture["returncode"] == 124
    assert capture["timeout_seconds"] == 1
    assert capture["timed_out"] is True
    stderr_path = run_dir / capture["artifacts"]["stderr"]
    assert "command timed out after 1 seconds" in stderr_path.read_text(encoding="utf-8")


def test_record_defect_persists_a_follow_up_issue(
    tmp_path: Path,
) -> None:
    """record-defect() must write one private follow-up defect record."""
    module = _load_module()
    _, run_dir = _bootstrap_run(module, tmp_path)

    assert (
        module.main(
            [
                "--repo-root",
                str(tmp_path / "repo"),
                "--run-root",
                str(tmp_path / "build" / "pi-e2e-2519"),
                "record-defect",
                "--run-id",
                run_dir.name,
                "--summary",
                "missing learn evidence",
                "--follow-up-issue",
                "2600",
                "--details",
                "Add an explicit learn capture before publication.",
                "--source-entry",
                "01-planning",
            ]
        )
        == 0
    )
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    defect = manifest["defects"][0]
    assert defect["follow_up_issue"] == 2600
    assert defect["source_entry"] == "01-planning"
    assert list((run_dir / "defects").glob("*.json"))


def test_render_verify_and_publication_attestation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """render(), verify(), and attestation should round-trip the private manifest."""
    module = _load_module()
    repo_root, run_dir = _bootstrap_run(module, tmp_path)
    prompt = "capture prompt"
    manifest = module._load_manifest(run_dir)
    manifest["pi"] = {
        "version": "pi 0.80.2",
        "binary": "/usr/bin/pi",
        "skill_commands": list(module.REQUIRED_SKILL_COMMANDS),
        "package_inventory": {
            "ready": True,
            "status": "ready",
            "detail": "",
            "roots": {},
            "scopes": {},
        },
    }
    manifest["snapshots"] = [
        {
            "label": "repo-snapshot",
            "artifact": "artifacts/repo-snapshot.json",
            "created_at": "2026-08-10T00:00:00Z",
            "head": "a" * 40,
            "branch": "main",
            "status": "",
        }
    ]
    manifest["commands"] = [
        {
            "id": f"{index:02d}-{stage}",
            "kind": "capture",
            "provider": "pi",
            "stage": stage,
            "fixture_sha256": module._fixture_digest(manifest),
            "revision": "a" * 40,
            "status": "success",
            "returncode": 0,
            "timed_out": False,
            "prompt_sha256": module._prompt_digest(prompt),
            "session_ids": [f"private-session-{index}"],
            "skill_calls": (
                ["skill:advise"]
                if stage == "advise"
                else ["skill:pr-review"]
                if stage == "review"
                else ["skill:learn"]
                if stage == "handoff"
                else []
            ),
            "tool_scopes": ["read"],
            "stdout_digest": "a" * 64,
            "stderr_digest": "b" * 64,
            "stdout_event_count": 1,
            "stderr_event_count": 0,
            "artifacts": {},
            "provider_invocations": [],
            "execution_policy": (
                {"role": "advisor", "operation": "advise", "lifecycle": "one_shot"}
                if stage == "advise"
                else {"role": "learner", "operation": "learn", "lifecycle": "start_new"}
                if stage == "handoff"
                else {}
            ),
            "started_at": "2026-08-10T00:00:00Z",
            "finished_at": "2026-08-10T00:00:01Z",
        }
        for index, stage in enumerate(module.REQUIRED_E2E_STAGES, start=1)
    ]
    manifest["commands"].append(
        {
            "id": "10-control",
            "kind": "capture",
            "provider": "codex",
            "stage": "planning",
            "fixture_sha256": module._fixture_digest(manifest),
            "revision": "a" * 40,
            "status": "success",
            "returncode": 0,
            "timed_out": False,
            "prompt_sha256": module._prompt_digest(prompt),
            "session_ids": ["private-control-session"],
            "skill_calls": [],
            "tool_scopes": ["read"],
            "stdout_digest": "a" * 64,
            "stderr_digest": "b" * 64,
            "stdout_event_count": 1,
            "stderr_event_count": 0,
            "artifacts": {},
            "provider_invocations": [],
            "started_at": "2026-08-10T00:00:00Z",
            "finished_at": "2026-08-10T00:00:01Z",
        }
    )
    manifest["commands"].append(
        {
            **manifest["commands"][3],
            "id": "11-review-boundary-probe",
            "kind": "failure_probe",
            "evidence_kind": "expected_failure_probe",
            "stage": "review",
            "status": "expected_failure",
            "returncode": 7,
            "timed_out": False,
            "session_ids": ["private-probe-session"],
            "expected_outcome": {
                "returncode": "nonzero",
                "timed_out": False,
            },
            "observed_outcome": {
                "returncode": 7,
                "timed_out": False,
            },
            "validation": {
                "matches_expectation": True,
                "result": "matched_expected_nonzero",
            },
        }
    )
    for entry in manifest["commands"]:
        if (
            entry.get("kind") == "capture"
            and entry.get("provider") == "pi"
            and entry.get("stage") in module.REQUIRED_E2E_STAGES
        ):
            _attach_stage_receipt(module, run_dir, manifest, entry)
        elif entry.get("id") == "10-control":
            _attach_capture_artifacts(
                module,
                run_dir,
                entry,
                stdout="stdout-planning",
                stderr="stderr-planning",
            )
    manifest["defects"] = [
        {
            "id": "defect-1",
            "summary": "control run should be compared",
            "follow_up_issue": 2600,
            "details": "compare Pi and Codex outputs",
            "source_entry": "01-planning",
            "created_at": "2026-08-10T00:00:00Z",
            "artifact": "defects/defect-1.json",
        }
    ]
    module._save_manifest(run_dir, manifest)
    assert (
        module.main(
            [
                "--repo-root",
                str(repo_root),
                "--run-root",
                str(tmp_path / "build" / "pi-e2e-2519"),
                "record-comparison",
                "--run-id",
                run_dir.name,
                "--pi-entry",
                "03-planning",
                "--control-entry",
                "10-control",
            ]
        )
        == 0
    )
    report = tmp_path / "docs" / "pi-e2e-2519-report.md"
    runbook = tmp_path / "docs" / "runbooks" / "pi-e2e-2519.md"

    assert (
        module.main(
            [
                "--repo-root",
                str(repo_root),
                "--run-root",
                str(tmp_path / "build" / "pi-e2e-2519"),
                "render",
                "--run-id",
                run_dir.name,
                "--report",
                str(report),
                "--runbook",
                str(runbook),
            ]
        )
        == 0
    )
    report_text = report.read_text(encoding="utf-8")
    runbook_text = runbook.read_text(encoding="utf-8")
    assert "Pi Issue 2519 Report" in report_text
    assert "Evidence status: `complete`" in report_text
    assert "pi 0.80.2" in report_text
    assert "| 1 recorded privately |" not in report_text
    assert "private-session-1" in report_text
    assert str(repo_root) not in report_text
    assert "/usr/bin/pi" not in report_text
    assert "Pi Issue 2519 Runbook" in runbook_text
    assert "capture --run-id <run-id>" in runbook_text
    assert "--provider pi -- <command...>" not in runbook_text
    assert "HEPH_PI_ISOLATION_ADAPTER" in runbook_text
    assert "direct Pi CLI execution is not workflow evidence" in runbook_text
    assert "Session identifiers are published in the report" in runbook_text
    assert "session identifiers remain in the owner-only manifest" not in runbook_text
    assert str(repo_root) not in runbook_text
    assert str(run_dir) not in runbook_text

    assert (
        module.main(
            [
                "--repo-root",
                str(repo_root),
                "--run-root",
                str(tmp_path / "build" / "pi-e2e-2519"),
                "verify",
                "--run-id",
                run_dir.name,
                "--criterion",
                "publication",
                "--report",
                str(report),
                "--runbook",
                str(runbook),
            ]
        )
        == 0
    )
    report.write_text("stale report\n", encoding="utf-8")
    assert (
        module.main(
            [
                "--repo-root",
                str(repo_root),
                "--run-root",
                str(tmp_path / "build" / "pi-e2e-2519"),
                "verify",
                "--run-id",
                run_dir.name,
                "--criterion",
                "publication",
                "--report",
                str(report),
                "--runbook",
                str(runbook),
            ]
        )
        == 1
    )
    assert (
        module.main(
            [
                "--repo-root",
                str(repo_root),
                "--run-root",
                str(tmp_path / "build" / "pi-e2e-2519"),
                "render",
                "--run-id",
                run_dir.name,
                "--report",
                str(report),
                "--runbook",
                str(runbook),
            ]
        )
        == 0
    )
    publication_sha = "a" * 40
    resolved_publication_refs: list[str] = []

    def _resolve_publication_ref(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        command = args[0]
        assert command[:6] == [
            "git",
            "-C",
            str(repo_root),
            "rev-parse",
            "--verify",
            "--end-of-options",
        ]
        resolved_publication_refs.append(command[6])
        assert command[6] in {"main^{commit}", f"{publication_sha}^{{commit}}"}
        assert kwargs["check"] is False
        return subprocess.CompletedProcess(command, 0, f"{publication_sha}\n", "")

    monkeypatch.setattr(module.subprocess, "run", _resolve_publication_ref)
    assert (
        module.main(
            [
                "--repo-root",
                str(repo_root),
                "--run-root",
                str(tmp_path / "build" / "pi-e2e-2519"),
                "attest-publication",
                "--run-id",
                run_dir.name,
                "--repo",
                "HomericIntelligence/Hephaestus",
                "--ref",
                "main",
                "--report",
                str(report),
                "--runbook",
                str(runbook),
                "--verify-defects",
            ]
        )
        == 1
    )
    assert (
        module.main(
            [
                "--repo-root",
                str(repo_root),
                "--run-root",
                str(tmp_path / "build" / "pi-e2e-2519"),
                "attest-publication",
                "--run-id",
                run_dir.name,
                "--repo",
                "HomericIntelligence/Hephaestus",
                "--ref",
                publication_sha,
                "--report",
                str(report),
                "--runbook",
                str(runbook),
                "--verify-defects",
            ]
        )
        == 0
    )
    assert resolved_publication_refs == ["main^{commit}", f"{publication_sha}^{{commit}}"]
    publication_path = run_dir / "artifacts" / "publication.json"
    publication = json.loads(publication_path.read_text(encoding="utf-8"))
    assert publication["repo"] == "HomericIntelligence/Hephaestus"
    assert publication["ref"] == publication_sha
    assert publication["commit_sha"] == publication_sha
    assert publication["snapshot_sha"] == publication_sha
    assert [receipt["head_sha"] for receipt in publication["workflow_receipts"]] == [
        publication_sha,
        publication_sha,
        publication_sha,
    ]
    assert (
        module._verify_run(
            run_dir,
            criterion="publication",
            report_path=report,
            runbook_path=runbook,
        )
        == 0
    )

    publication["snapshot_sha"] = "b" * 40
    manifest = module._load_manifest(run_dir)
    manifest["publication"] = publication
    module._save_manifest(run_dir, manifest)
    with pytest.raises(
        ValueError,
        match="publication snapshot_sha does not match its local evidence",
    ):
        module._verify_run(
            run_dir,
            criterion="publication",
            report_path=report,
            runbook_path=runbook,
        )


def test_publication_commit_resolution_rejects_symbolic_unresolved_and_mismatched_refs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Publication must identify one existing immutable local commit exactly."""
    module = _load_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    requested_sha = "a" * 40
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, f"{requested_sha}\n", ""),
    )
    with pytest.raises(ValueError, match="resolved immutable commit SHA"):
        module._resolve_publication_commit(repo_root, "main")

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "unknown revision"),
    )
    with pytest.raises(ValueError, match="does not resolve to an existing commit"):
        module._resolve_publication_commit(repo_root, requested_sha)

    resolved_sha = "b" * 40
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, f"{resolved_sha}\n", ""),
    )
    with pytest.raises(ValueError, match="resolved to a different commit"):
        module._resolve_publication_commit(repo_root, requested_sha)


def test_publication_attestation_rejects_snapshot_and_workflow_receipt_mismatches() -> None:
    """Publication must bind the immutable commit to local snapshot and PR receipts."""
    module = _load_module()
    commit_sha = "a" * 40
    stage_receipts = {
        stage: {
            "capture_id": f"capture-{stage}",
            "receipt_id": "b" * 64,
            "worktree": {"head": commit_sha},
            "pr_identity": (
                2519,
                "https://github.com/HomericIntelligence/Hephaestus/pull/2519",
                "2519",
            ),
        }
        for stage in ("commit-pr", "review", "handoff")
    }
    manifest = {"snapshots": [{"head": "c" * 40}]}

    with pytest.raises(ValueError, match="does not match the captured snapshot"):
        module._publication_attestation_evidence(manifest, stage_receipts, commit_sha)

    manifest["snapshots"][0]["head"] = commit_sha
    stage_receipts["review"]["worktree"] = {"head": "d" * 40}
    with pytest.raises(ValueError, match="review workflow receipt"):
        module._publication_attestation_evidence(manifest, stage_receipts, commit_sha)


def test_incomplete_run_is_truthful_private_and_cannot_be_attested(
    tmp_path: Path,
) -> None:
    """A failed Pi capture must remain private and block completion attestation."""
    module = _load_module()
    repo_root, run_dir = _bootstrap_run(module, tmp_path, run_id="private-live-run")
    manifest = module._load_manifest(run_dir)
    private_binary = tmp_path / "private-toolchain" / "pi"
    private_session = "019feaa3-private-session"
    manifest["pi"] = {
        "version": "pi 0.80.2",
        "binary": str(private_binary),
        "skill_commands": list(module.REQUIRED_SKILL_COMMANDS),
        "package_inventory": {"ready": True, "status": "ready"},
    }
    manifest["commands"] = [
        {
            "id": "01-planning",
            "kind": "capture",
            "provider": "pi",
            "stage": "planning",
            "status": "failure",
            "returncode": 1,
            "session_ids": [private_session],
            "skill_calls": [],
            "tool_scopes": ["read", "grep"],
        },
        {
            "id": "02-control",
            "kind": "capture",
            "provider": "codex",
            "stage": "control",
            "status": "success",
            "returncode": 0,
            "session_ids": [],
            "skill_calls": [],
            "tool_scopes": [],
        },
    ]
    module._save_manifest(run_dir, manifest)
    report = tmp_path / "docs" / "pi-e2e-2519-report.md"
    runbook = tmp_path / "docs" / "runbooks" / "pi-e2e-2519.md"

    assert (
        module.main(
            [
                "--repo-root",
                str(repo_root),
                "--run-root",
                str(tmp_path / "build" / "pi-e2e-2519"),
                "render",
                "--run-id",
                run_dir.name,
                "--report",
                str(report),
                "--runbook",
                str(runbook),
            ]
        )
        == 0
    )

    published = report.read_text(encoding="utf-8") + runbook.read_text(encoding="utf-8")
    assert "Evidence status: `incomplete`" in published
    assert "not closure evidence for #2519" in published
    assert "The only captured Pi command failed during planning" in published
    assert "No isolated Pi worktree, repository snapshot, successful test run" in published
    assert "Missing required acceptance evidence:" in published
    assert "A repository snapshot bound to the Pi run." in published
    assert "Successful isolated Pi planning, implementation, tests, commit/PR creation" in published
    assert "persisted success artifacts or failure behavior" in published
    assert "Host receipts or a control-provider run do not substitute" in published
    assert (
        "| control | codex | unverified / unproven | claimed `0` (private manifest only) | "
        "none | n/a | n/a |"
    ) in published
    assert "The Codex control result is unverified" in published
    assert "no committed, report-bound control transcript exists" in published
    assert "they do not establish this Codex invocation" in published
    assert "failure" in published
    assert private_session in published
    assert "| 1 recorded privately |" not in published
    assert str(private_binary) not in published
    assert str(repo_root) not in published
    assert str(run_dir) not in published
    assert (
        module.main(
            [
                "--repo-root",
                str(repo_root),
                "--run-root",
                str(tmp_path / "build" / "pi-e2e-2519"),
                "attest-publication",
                "--run-id",
                run_dir.name,
                "--repo",
                "HomericIntelligence/Hephaestus",
                "--ref",
                "main",
                "--report",
                str(report),
                "--runbook",
                str(runbook),
            ]
        )
        == 1
    )

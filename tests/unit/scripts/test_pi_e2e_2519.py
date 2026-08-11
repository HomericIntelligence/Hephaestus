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


def _successful_live_pr_readback(
    repository: str,
    pr_number: int,
    _repo_root: Path,
) -> dict[str, Any]:
    """Return host-seam facts for tests that are not exercising live readback."""
    return {
        "repository": repository,
        "number": pr_number,
        "url": f"https://github.com/{repository}/pull/{pr_number}",
        "state": "OPEN",
        "branch": "2519-pi-e2e",
        "head_sha": "a" * 40,
        "closes_issue": 2519,
        "implementation_go": True,
        "implementation_no_go": False,
        "unresolved_threads": 0,
        "native_auto_merge": False,
    }


def _load_module(*, stub_live_readback: bool = True) -> ModuleType:
    assert SCRIPT.is_file(), "the issue-2519 collector must exist"
    spec = importlib.util.spec_from_file_location("pi_e2e_2519", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if stub_live_readback:
        module._live_pull_request_readback = _successful_live_pr_readback  # type: ignore[attr-defined]
    return module


def _follow_up_issue_payload(
    number: int = 2600,
    *,
    state: str = "OPEN",
    body: str = "Parent: #2519\n\n## Defect\n\nObserved failure.",
    url: str | None = None,
) -> dict[str, Any]:
    """Return one repository-scoped follow-up issue readback."""
    return {
        "id": f"I_kwDOQww0as5{number}",
        "number": number,
        "title": "Observed Pi defect",
        "state": state,
        "labels": [],
        "body": body,
        "url": url or f"https://github.com/HomericIntelligence/Hephaestus/issues/{number}",
    }


def _stub_follow_up_issue(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any] | Exception,
) -> list[int]:
    """Replace the repository GitHub seam with one controlled issue readback."""
    calls: list[int] = []

    class FakePipelineGitHub:
        def __init__(
            self,
            owner: str,
            *,
            repo: str,
            repo_root: Path,
        ) -> None:
            assert (owner, repo) == ("HomericIntelligence", "Hephaestus")
            assert isinstance(repo_root, Path)

        def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
            calls.append(issue_number)
            if isinstance(payload, Exception):
                raise payload
            return dict(payload)

    monkeypatch.setattr(module, "PipelineGitHub", FakePipelineGitHub)
    return calls


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
    kind: str,
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
        "trust_source": "packaged-athena-contract",
    }
    binding = {
        "root": "/host-owned/mnemosyne",
        "repository": "HomericIntelligence/Mnemosyne",
        "default_branch": "main",
        "commit_sha": mnemosyne_commit,
        "trust_basis": "canonical upstream",
        "athena_contract": contract,
    }
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
        "kind": kind,
        "context": "Selected host-owned advice." if kind == "advise" else "",
        "receipt": {
            "contract": contract,
            "binding": binding,
            "corpus": corpus,
        },
        "delivery_receipt": delivery_receipt,
        "error": None,
    }


def _attach_host_athena_receipt(
    module: ModuleType,
    run_dir: Path,
    entry: dict[str, Any],
    *,
    kind: str,
    readback_head_sha: str | None = None,
) -> None:
    payload = _host_athena_receipt(
        kind=kind,
        readback_head_sha=readback_head_sha,
    )
    relative_path = Path("commands") / entry["id"] / f"athena-{kind}-host-receipt.json"
    receipt_path = run_dir / relative_path
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    receipt_path.write_bytes(content)
    entry.setdefault("athena_host_receipts", {})[kind] = {
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
        key: value for key, value in policy.items() if key not in {"skill_grants", "tool_scopes"}
    }
    entry["tool_scopes"] = policy["tool_scopes"]
    entry["requested_skill_grants"] = policy["skill_grants"]
    entry["observed_skill_invocations"] = []
    entry["provider_skill_mentions"] = []
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

    if stage == "planning":
        _attach_host_athena_receipt(module, run_dir, entry, kind="advise")
    elif stage == "review":
        _attach_host_athena_receipt(module, run_dir, entry, kind="learn")

    head = entry["revision"]
    lifecycle: dict[str, Any] = {
        "kind": module.PI_STAGE_LIFECYCLE_KINDS[stage],
        "fixture_sha256": module._fixture_digest(manifest),
        "head_sha": head,
        "success": True,
    }
    if stage == "discovery":
        lifecycle.update(paths=list(module.FIXTURE_PATHS), all_paths_found=True)
    elif stage == "planning":
        lifecycle["athena_receipt_sha256"] = entry["athena_host_receipts"]["advise"]["sha256"]
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
                learn_receipt_sha256=entry["athena_host_receipts"]["learn"]["sha256"],
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
            "skill_grants": policy["skill_grants"],
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
    kind = {"planning": "advise", "review": "learn"}[entry["stage"]]
    descriptor = entry["athena_host_receipts"][kind]
    receipt_path = run_dir / descriptor["artifact"]
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    mutate(payload)
    content = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    receipt_path.write_bytes(content)
    descriptor["sha256"] = module._sha256_bytes(content)
    stage = entry.get("stage")
    lifecycle_field = (
        {
            "planning": "athena_receipt_sha256",
            "review": "learn_receipt_sha256",
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
                descriptor["sha256"],
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


@pytest.mark.parametrize(
    "run_id_kind",
    ("absolute", "traversal", "nested", "backslash-traversal", "dot"),
)
def test_init_rejects_unsafe_run_ids_before_creating_the_run_root(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    run_id_kind: str,
) -> None:
    """init() must not create or chmod paths outside its configured run root."""
    module = _load_module()
    run_root = tmp_path / "build" / "pi-e2e-2519"
    escaped_dir = tmp_path / "escaped-run"
    run_id = {
        "absolute": str(escaped_dir),
        "traversal": "../escaped-run",
        "nested": "nested/run",
        "backslash-traversal": r"..\escaped-run",
        "dot": ".",
    }[run_id_kind]

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


@pytest.mark.parametrize("symlink_target", ("outside", "run-root"))
def test_init_rejects_run_id_symlinks_that_are_not_strict_descendants(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    symlink_target: str,
) -> None:
    """init() must reject resolved run directories at or outside the run root."""
    module = _load_module()
    repo_root = tmp_path / "repo"
    run_root = tmp_path / "build" / "pi-e2e-2519"
    escaped_dir = tmp_path / "escaped-run"
    repo_root.mkdir()
    run_root.mkdir(parents=True)
    escaped_dir.mkdir()
    run_root.chmod(0o755)
    escaped_dir.chmod(0o755)
    target = escaped_dir if symlink_target == "outside" else run_root
    (run_root / "linked-run").symlink_to(target, target_is_directory=True)

    assert (
        module.main(
            [
                "--repo-root",
                str(repo_root),
                "--run-root",
                str(run_root),
                "init",
                "--run-id",
                "linked-run",
            ]
        )
        == 1
    )

    assert "outside the configured run root" in capsys.readouterr().err
    assert stat.S_IMODE(run_root.stat().st_mode) == 0o755
    assert stat.S_IMODE(escaped_dir.stat().st_mode) == 0o755
    assert not (run_root / "run.json").exists()
    assert not (escaped_dir / "run.json").exists()


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
    monkeypatch.setattr(
        module,
        "_probe_command_version",
        lambda binary: (f"{binary} 0.80.2", False),
    )

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


def test_inventory_records_timed_out_version_probe_as_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A non-terminating Pi --version probe cannot make inventory succeed."""
    module = _load_module()
    build_root = tmp_path / "build" / "pi-e2e-2519"
    _, run_dir = _bootstrap_run(module, tmp_path)
    pi = tmp_path / "pi"
    pi.write_text(
        "#!/usr/bin/env python3\nimport time\nwhile True:\n    time.sleep(1)\n",
        encoding="utf-8",
    )
    pi.chmod(0o700)
    fake_catalog = SimpleNamespace(required_commands=("skill:advise",))
    fake_inventory = SimpleNamespace(
        ready=True,
        status="ready",
        detail="",
        roots={"athena": tmp_path / "athena"},
        scopes={"athena": "user"},
    )
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(module, "DEFAULT_INVENTORY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(module, "load_pi_package_catalog", lambda: fake_catalog)
    monkeypatch.setattr(
        module,
        "inspect_pi_package_inventory",
        lambda *args, **kwargs: fake_inventory,
    )

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
        == 1
    )

    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    inventory = manifest["pi"]["package_inventory"]
    assert manifest["pi"]["version"] == ""
    assert inventory["ready"] is False
    assert inventory["status"] == "version_probe_timeout"
    assert inventory["detail"] == "Pi --version timed out after 0.05 seconds"
    assert manifest["commands"][-1]["status"] == "failure"


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


@pytest.mark.parametrize("stage", ("advise", "handoff"))
def test_host_owned_athena_stages_never_call_pi_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
) -> None:
    """Host-owned advise/learn work must never enter the Pi runtime."""
    module = _load_module()
    repo_root, run_dir = _bootstrap_run(module, tmp_path)
    runtime_calls: list[str] = []

    monkeypatch.setattr(
        module,
        "resolve_agent",
        lambda *_args, **_kwargs: runtime_calls.append("resolve_agent"),
    )
    monkeypatch.setattr(
        module,
        "run_agent_text",
        lambda *_args, **_kwargs: runtime_calls.append("run_agent_text"),
    )
    monkeypatch.setattr(
        module,
        "run_agent_session",
        lambda *_args, **_kwargs: runtime_calls.append("run_agent_session"),
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
                stage,
                "--provider",
                "pi",
                "--prompt",
                "host-owned Athena work",
            ]
        )
        == 1
    )

    assert runtime_calls == []
    assert module._load_manifest(run_dir)["commands"] == []


def test_typed_host_receipt_is_collected_independently_and_correlated(
    tmp_path: Path,
) -> None:
    """Host results correlate to real Pi jobs without claiming Pi execution provenance."""
    module = _load_module()
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    run_dir, manifest = _manifest_with_stage_receipts(module, evidence_root)
    planning = next(entry for entry in manifest["commands"] if entry["stage"] == "planning")
    planning.pop("athena_host_receipts")
    module._save_manifest(run_dir, manifest)
    receipt_path = tmp_path / "advise-result.json"
    receipt_path.write_text(
        json.dumps(_host_athena_receipt(kind="advise")),
        encoding="utf-8",
    )

    assert (
        module.main(
            [
                "--repo-root",
                manifest["repo_root"],
                "--run-root",
                str(run_dir.parent),
                "record-athena-host-receipt",
                "--run-id",
                run_dir.name,
                "--kind",
                "advise",
                "--correlated-capture-id",
                planning["id"],
                "--receipt",
                str(receipt_path),
            ]
        )
        == 0
    )

    recorded = module._load_manifest(run_dir)
    correlated = next(entry for entry in recorded["commands"] if entry["id"] == planning["id"])
    payload = module._load_athena_host_receipt(run_dir, correlated, "advise")
    assert set(payload) == {"kind", "context", "receipt", "delivery_receipt", "error"}
    assert "provider" not in payload
    assert "pi_command_receipt" not in payload
    assert (
        module._verify_host_athena_receipt(
            run_dir,
            correlated,
            expected_kind="advise",
        )
        == "advise"
    )


def test_capture_analysis_separates_provider_mentions_from_requested_skill_grants(
    tmp_path: Path,
) -> None:
    """Provider prose and command grants must not become observed skill invocations."""
    module = _load_module()
    proxy_log = tmp_path / "provider-proxy.jsonl"
    proxy_log.write_text(
        json.dumps(
            {
                "event": "proxy-invocation",
                "tool": "pi",
                "argv": [
                    "--commands",
                    "skill:learn,skill:pr-review",
                    "--tools",
                    "read,grep",
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    stdout = json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": "I ran skill:advise and skill:learn",
            },
        }
    )

    analysis = module._capture_analysis(stdout, "", proxy_log)

    assert analysis["observed_skill_invocations"] == []
    assert analysis["provider_skill_mentions"] == ["skill:advise", "skill:learn"]
    assert analysis["requested_skill_grants"] == ["skill:learn", "skill:pr-review"]
    assert analysis["tool_scopes"] == ["read", "grep"]


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
    assert captures[0]["observed_skill_invocations"] == []
    assert captures[0]["provider_skill_mentions"] == ["skill:advise", "skill:pr-review"]
    assert captures[0]["requested_skill_grants"] == []
    assert "read" in captures[0]["tool_scopes"]
    assert "grep" in captures[0]["tool_scopes"]
    assert captures[1]["observed_skill_invocations"] == []
    assert captures[1]["provider_skill_mentions"] == ["skill:advise", "skill:pr-review"]
    assert captures[1]["requested_skill_grants"] == ["skill:learn", "skill:pr-review"]
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
    assert comparison["comparison_basis"] == "artifacts"
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
    run_dir, manifest = _manifest_with_stage_receipts(module, tmp_path)
    for entry in manifest["commands"]:
        if entry["stage"] in {"planning", "review"}:
            entry.pop("athena_host_receipts")
        entry["provider_skill_mentions"] = list(module.REQUIRED_SKILL_COMMANDS)
        entry["observed_skill_invocations"] = list(module.REQUIRED_SKILL_COMMANDS)
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
    run_dir, manifest = _manifest_with_stage_receipts(module, tmp_path)
    for entry in manifest["commands"]:
        if entry["stage"] in {"planning", "review"}:
            entry.pop("athena_host_receipts")
        entry["provider_invocations"] = [
            {
                "tool": "pi",
                "argv": ["--commands", ",".join(module.REQUIRED_SKILL_COMMANDS)],
            }
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
    review = next(entry for entry in manifest["commands"] if entry["stage"] == "review")
    _rewrite_athena_host_receipt(
        module,
        run_dir,
        review,
        lambda payload: payload["delivery_receipt"].__setitem__(
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


@pytest.mark.parametrize(
    ("section", "unexpected_field"),
    (
        ("coordinator", "pooled_stage_names"),
        ("provider_evidence", "pooled_capture_ids"),
        ("worktree", "caller_attestation"),
    ),
)
def test_stage_receipts_reject_malformed_nested_receipt_schemas(
    tmp_path: Path,
    section: str,
    unexpected_field: str,
) -> None:
    """Caller-added fields cannot turn generic receipts into coordinator evidence."""
    module = _load_module()
    evidence_root = tmp_path / section
    evidence_root.mkdir()
    run_dir, manifest = _manifest_with_stage_receipts(module, evidence_root)
    discovery = next(entry for entry in manifest["commands"] if entry["stage"] == "discovery")
    _rewrite_stage_receipt(
        module,
        run_dir,
        discovery,
        lambda payload: payload[section].__setitem__(unexpected_field, True),
    )

    with pytest.raises(ValueError, match=f"unsupported {section} schema"):
        module._verify_stage_receipts(manifest, run_dir)


@pytest.mark.parametrize(
    ("target", "message"),
    (
        ("lifecycle", "unsupported review lifecycle schema"),
        ("pull_request", "unsupported pull_request schema"),
    ),
)
def test_stage_receipts_reject_pooled_lifecycle_and_github_schemas(
    tmp_path: Path,
    target: str,
    message: str,
) -> None:
    """A stage lifecycle or PR union receipt must not satisfy exact-stage completion."""
    module = _load_module()
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    run_dir, manifest = _manifest_with_stage_receipts(module, evidence_root)
    review = next(entry for entry in manifest["commands"] if entry["stage"] == "review")
    _rewrite_stage_receipt(
        module,
        run_dir,
        review,
        lambda payload: (
            payload["lifecycle"].__setitem__("commit_sha", review["revision"])
            if target == "lifecycle"
            else payload["lifecycle"]["pull_request"].__setitem__("pooled_readback", True)
        ),
    )

    with pytest.raises(ValueError, match=message):
        module._verify_stage_receipts(manifest, run_dir)


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


def test_record_stage_receipt_rejects_caller_authored_readback_boolean(
    tmp_path: Path,
) -> None:
    """A well-shaped JSON assertion cannot stand in for live GitHub evidence."""
    module = _load_module()
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    run_dir, manifest = _manifest_with_stage_receipts(module, evidence_root)
    review = next(value for value in manifest["commands"] if value["stage"] == "review")
    persisted_path = run_dir / review["stage_receipt"]["artifact"]
    payload = json.loads(persisted_path.read_text(encoding="utf-8"))
    payload["lifecycle"]["pull_request"]["readback_verified"] = True
    coordinator_export = tmp_path / "fabricated-review-receipt.json"
    coordinator_export.write_text(json.dumps(payload), encoding="utf-8")
    review.pop("stage_receipt")
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
                review["id"],
                "--receipt",
                str(coordinator_export),
            ]
        )
        == 1
    )
    recorded_review = next(
        value for value in module._load_manifest(run_dir)["commands"] if value["stage"] == "review"
    )
    assert "stage_receipt" not in recorded_review


def test_stage_receipt_rebinds_review_claims_to_live_github(
    tmp_path: Path,
) -> None:
    """Exact-head label and thread claims must match fresh host-owned facts."""
    module = _load_module()
    run_dir, manifest = _manifest_with_stage_receipts(module, tmp_path)
    live = _successful_live_pr_readback(
        module.PROJECT_REPOSITORY,
        2519,
        Path(manifest["repo_root"]),
    )
    live["implementation_go"] = False
    live["implementation_no_go"] = True
    live["unresolved_threads"] = 1
    module._live_pull_request_readback = lambda *_args: dict(live)  # type: ignore[attr-defined]

    with pytest.raises(ValueError, match="live GitHub label and thread readback"):
        module._verify_stage_receipts(manifest, run_dir)


def test_live_readback_uses_scoped_host_seam_and_stabilizes_all_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Production readback repeats mutable PR, label, and conversation facts."""
    module = _load_module(stub_live_readback=False)
    calls: dict[str, int] = {}

    def observed(name: str, value: Any) -> Any:
        calls[name] = calls.get(name, 0) + 1
        return value

    github = SimpleNamespace(
        gh_pr_state=lambda number: observed(
            "state",
            {
                "state": "OPEN",
                "headRefOid": "a" * 40,
                "mergedAt": None,
                "baseRefName": "main",
                "autoMergeRequest": None,
            },
        ),
        pr_review_context=lambda number: observed(
            "context",
            {
                "pr_title": "fixture",
                "pr_description": "Closes #2519",
                "pr_head_sha": "a" * 40,
                "pr_base_sha": "b" * 40,
                "pr_base_branch": "main",
            },
        ),
        get_pr_head_branch=lambda number: observed("branch", "2519-pi-e2e"),
        find_issue_for_pr=lambda number: observed("issue", 2519),
        pr_has_implementation_state_label=lambda number: observed("labels", (True, False)),
        list_unresolved_review_threads=lambda number: observed("threads", []),
    )

    def pipeline_github(owner: str, *, repo: str, repo_root: Path) -> Any:
        assert (owner, repo, repo_root) == (
            "HomericIntelligence",
            "Hephaestus",
            tmp_path,
        )
        return github

    monkeypatch.setattr(module, "PipelineGitHub", pipeline_github)

    assert module._live_pull_request_readback(
        module.PROJECT_REPOSITORY,
        2519,
        tmp_path,
    ) == _successful_live_pr_readback(module.PROJECT_REPOSITORY, 2519, tmp_path)
    assert calls == {
        "state": 2,
        "context": 2,
        "branch": 2,
        "issue": 2,
        "labels": 2,
        "threads": 2,
    }


def test_stage_receipt_verification_performs_a_final_live_rebind(
    tmp_path: Path,
) -> None:
    """A receipt accepted at ingestion cannot certify a later stale PR head."""
    module = _load_module()
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    run_dir, manifest = _manifest_with_stage_receipts(module, evidence_root)
    commit_pr = next(value for value in manifest["commands"] if value["stage"] == "commit-pr")
    persisted_path = run_dir / commit_pr["stage_receipt"]["artifact"]
    coordinator_export = tmp_path / "commit-pr-stage-receipt.json"
    coordinator_export.write_bytes(persisted_path.read_bytes())
    commit_pr.pop("stage_receipt")
    module._save_manifest(run_dir, manifest)

    live_reads = 0

    def moving_head(repository: str, pr_number: int, repo_root: Path) -> dict[str, Any]:
        nonlocal live_reads
        live_reads += 1
        readback = _successful_live_pr_readback(repository, pr_number, repo_root)
        if live_reads > 1:
            readback["head_sha"] = "b" * 40
        return readback

    module._live_pull_request_readback = moving_head  # type: ignore[attr-defined]
    assert (
        module._record_stage_receipt(
            run_dir,
            capture_id=commit_pr["id"],
            receipt_path=coordinator_export,
        )
        == 0
    )
    with pytest.raises(ValueError, match="does not match live GitHub"):
        module._verify_stage_receipts(module._load_manifest(run_dir), run_dir)
    assert live_reads >= 2


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
            lambda payload: payload["lifecycle"]["pull_request"].__setitem__("head_sha", "b" * 40),
            "open pull-request claim",
        ),
        (
            "handoff",
            "review",
            lambda payload: payload["lifecycle"].__setitem__("finished_handoff", False),
            "host learning handoff evidence",
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


def test_comparison_verification_accepts_paired_expected_failure_behavior(
    tmp_path: Path,
) -> None:
    """Expected-failure runs compare behavior while retaining their actual outcomes."""
    module = _load_module()
    _, run_dir = _bootstrap_run(module, tmp_path)
    manifest = module._load_manifest(run_dir)
    revision = "a" * 40
    manifest["snapshots"] = [{"label": "fixture", "head": revision}]
    base_probe = {
        "kind": "failure_probe",
        "evidence_kind": "expected_failure_probe",
        "stage": "review",
        "fixture_sha256": module._fixture_digest(manifest),
        "revision": revision,
        "timed_out": False,
        "prompt_sha256": module._prompt_digest("fixture failure prompt"),
    }
    manifest["commands"] = []
    for entry_id, provider, returncode in (
        ("01-pi-probe", "pi", 7),
        ("02-control-probe", "codex", 9),
    ):
        entry = {
            **base_probe,
            "id": entry_id,
            "provider": provider,
            "returncode": returncode,
            **module._expected_failure_probe_fields(returncode, False),
        }
        _attach_capture_artifacts(
            module,
            run_dir,
            entry,
            stdout=f"{provider} expected failure",
        )
        manifest["commands"].append(entry)
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
                "01-pi-probe",
                "--control-entry",
                "02-control-probe",
            ]
        )
        == 0
    )
    comparison = module._load_manifest(run_dir)["comparisons"][0]
    assert comparison["comparison_basis"] == "failure_behavior"
    assert comparison["pi_outcome"]["returncode"] == 7
    assert comparison["control_outcome"]["returncode"] == 9
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
        == 0
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


def test_failure_probe_persists_an_admitted_pi_runtime_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An admitted Pi nonzero result remains durable expected-failure evidence."""
    module = _load_module()
    repo_root, run_dir = _bootstrap_run(module, tmp_path)
    observed_requests: list[Any] = []

    monkeypatch.setattr(module, "resolve_agent", lambda _agent, *, cwd: "pi")

    def _failing_pi_session(_agent: str, _prompt: str, **kwargs: object) -> object:
        observed_requests.append(kwargs["execution_request"])
        raise subprocess.CalledProcessError(
            7,
            ["pi", "--mode", "json"],
            output="durable pi stdout\n",
            stderr="durable pi stderr\n",
        )

    monkeypatch.setattr(module, "run_agent_session", _failing_pi_session)

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
            "exercise the admitted failure boundary",
        ]
    )

    assert rc == 0
    assert len(observed_requests) == 1
    request = observed_requests[0]
    assert request.role.value == "planner"
    assert request.operation.value == "plan"
    manifest = module._load_manifest(run_dir)
    probe = manifest["commands"][-1]
    assert probe["kind"] == "failure_probe"
    assert probe["status"] == "expected_failure"
    assert probe["returncode"] == 7
    assert probe["timed_out"] is False
    assert probe["observed_outcome"] == {"returncode": 7, "timed_out": False}
    assert probe["validation"]["matches_expectation"] is True
    assert (run_dir / probe["artifacts"]["stdout"]).read_text(encoding="utf-8") == (
        "durable pi stdout\n"
    )
    assert (run_dir / probe["artifacts"]["stderr"]).read_text(encoding="utf-8") == (
        "durable pi stderr\n"
    )


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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """record-defect() must write one private follow-up defect record."""
    module = _load_module()
    _, run_dir = _bootstrap_run(module, tmp_path)
    calls = _stub_follow_up_issue(module, monkeypatch, _follow_up_issue_payload())

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
    assert defect["follow_up_issue_identity"] == {
        "repository": "HomericIntelligence/Hephaestus",
        "node_id": "I_kwDOQww0as52600",
        "number": 2600,
        "url": "https://github.com/HomericIntelligence/Hephaestus/issues/2600",
        "state": "OPEN",
        "parent_issue": 2519,
    }
    assert defect["source_entry"] == "01-planning"
    assert list((run_dir / "defects").glob("*.json"))
    assert calls == [2600, 2600]


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (RuntimeError("issue not found"), "could not resolve"),
        (_follow_up_issue_payload(999_999), "identity"),
        (
            _follow_up_issue_payload(url="https://github.com/Elsewhere/Other/issues/2600"),
            "identity",
        ),
        (_follow_up_issue_payload(state="CLOSED"), "must be open"),
        (_follow_up_issue_payload(body="Parent: #999\n"), "link to #2519"),
    ],
)
def test_record_defect_rejects_bogus_or_unrelated_follow_up_issue_numbers(
    payload: dict[str, Any] | Exception,
    error: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A positive integer is not follow-up coverage without live GitHub proof."""
    module = _load_module()
    _, run_dir = _bootstrap_run(module, tmp_path)
    _stub_follow_up_issue(module, monkeypatch, payload)

    result = module.main(
        [
            "--repo-root",
            str(tmp_path / "repo"),
            "--run-root",
            str(tmp_path / "build" / "pi-e2e-2519"),
            "record-defect",
            "--run-id",
            run_dir.name,
            "--summary",
            "unverified defect",
            "--follow-up-issue",
            "2600",
        ]
    )

    assert result == 1
    assert error in capsys.readouterr().err
    assert module._load_manifest(run_dir)["defects"] == []
    assert list((run_dir / "defects").glob("*.json")) == []


def test_record_defect_rejects_duplicate_follow_up_issue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One live follow-up issue cannot attest coverage for two defect records."""
    module = _load_module()
    _, run_dir = _bootstrap_run(module, tmp_path)
    _stub_follow_up_issue(module, monkeypatch, _follow_up_issue_payload())
    argv = [
        "--repo-root",
        str(tmp_path / "repo"),
        "--run-root",
        str(tmp_path / "build" / "pi-e2e-2519"),
        "record-defect",
        "--run-id",
        run_dir.name,
        "--summary",
        "first defect",
        "--follow-up-issue",
        "2600",
    ]

    assert module.main(argv) == 0
    argv[argv.index("first defect")] = "second defect"
    assert module.main(argv) == 1
    assert "duplicate follow-up issue" in capsys.readouterr().err
    assert len(module._load_manifest(run_dir)["defects"]) == 1


def test_record_defect_rejects_unstable_live_follow_up_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A state transition between live reads cannot produce durable defect proof."""
    module = _load_module()
    _, run_dir = _bootstrap_run(module, tmp_path)
    payloads = iter(
        [
            _follow_up_issue_payload(),
            _follow_up_issue_payload(state="CLOSED"),
        ]
    )

    class MovingPipelineGitHub:
        def __init__(self, owner: str, *, repo: str, repo_root: Path) -> None:
            assert (owner, repo) == ("HomericIntelligence", "Hephaestus")

        def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
            assert issue_number == 2600
            return next(payloads)

    monkeypatch.setattr(module, "PipelineGitHub", MovingPipelineGitHub)

    result = module.main(
        [
            "--repo-root",
            str(tmp_path / "repo"),
            "--run-root",
            str(tmp_path / "build" / "pi-e2e-2519"),
            "record-defect",
            "--run-id",
            run_dir.name,
            "--summary",
            "moving defect",
            "--follow-up-issue",
            "2600",
        ]
    )

    assert result == 1
    assert "must be open" in capsys.readouterr().err
    assert module._load_manifest(run_dir)["defects"] == []


@pytest.mark.parametrize(
    ("live_payload", "defect_count", "stored_node_id", "error"),
    [
        (
            _follow_up_issue_payload(state="CLOSED"),
            1,
            "I_kwDOQww0as52600",
            "must be open",
        ),
        (_follow_up_issue_payload(), 2, "I_kwDOQww0as52600", "duplicate follow-up issue"),
        (_follow_up_issue_payload(), 1, "I_stale_identity", "identity or live state/linkage"),
    ],
)
def test_publication_attestation_rejects_stale_or_duplicate_follow_up_proof(
    live_payload: dict[str, Any],
    defect_count: int,
    stored_node_id: str,
    error: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Attestation must freshly rebind unique defect records to live GitHub state."""
    module = _load_module()
    repo_root, run_dir = _bootstrap_run(module, tmp_path)
    identity = {
        "repository": "HomericIntelligence/Hephaestus",
        "node_id": stored_node_id,
        "number": 2600,
        "url": "https://github.com/HomericIntelligence/Hephaestus/issues/2600",
        "state": "OPEN",
        "parent_issue": 2519,
    }
    manifest = module._load_manifest(run_dir)
    manifest["defects"] = [
        {
            "id": f"defect-{index}",
            "summary": f"defect {index}",
            "follow_up_issue": 2600,
            "follow_up_issue_identity": identity,
        }
        for index in range(defect_count)
    ]
    module._save_manifest(run_dir, manifest)
    _stub_follow_up_issue(module, monkeypatch, live_payload)
    monkeypatch.setattr(module, "_verify_completion", lambda *_args: {})
    monkeypatch.setattr(module, "_verify_publication", lambda *_args: None)
    monkeypatch.setattr(module, "_resolve_publication_commit", lambda *_args: "a" * 40)
    monkeypatch.setattr(module, "_publication_attestation_evidence", lambda *_args: {})

    result = module.main(
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
            "a" * 40,
            "--report",
            str(tmp_path / "docs" / "pi-e2e-2519-report.md"),
            "--runbook",
            str(tmp_path / "docs" / "runbooks" / "pi-e2e-2519.md"),
            "--verify-defects",
        ]
    )

    assert result == 1
    assert error in capsys.readouterr().err
    assert module._load_manifest(run_dir)["publication"] == {}


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
            "follow_up_issue_identity": {
                "repository": "HomericIntelligence/Hephaestus",
                "node_id": "I_kwDOQww0as52600",
                "number": 2600,
                "url": "https://github.com/HomericIntelligence/Hephaestus/issues/2600",
                "state": "OPEN",
                "parent_issue": 2519,
            },
            "details": "compare Pi and Codex outputs",
            "source_entry": "01-planning",
            "created_at": "2026-08-10T00:00:00Z",
            "artifact": "defects/defect-1.json",
        }
    ]
    module._save_manifest(run_dir, manifest)
    _stub_follow_up_issue(module, monkeypatch, _follow_up_issue_payload())
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
                "02-planning",
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
        if command[-3:] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "https://github.com/HomericIntelligence/Hephaestus.git\n",
                "",
            )
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
    assert publication["verified_follow_up_issues"] == [
        {
            "repository": "HomericIntelligence/Hephaestus",
            "node_id": "I_kwDOQww0as52600",
            "number": 2600,
            "url": "https://github.com/HomericIntelligence/Hephaestus/issues/2600",
            "state": "OPEN",
            "parent_issue": 2519,
        }
    ]
    assert [receipt["head_sha"] for receipt in publication["workflow_receipts"]] == [
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

    def _resolve_as(
        command: list[str],
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> subprocess.CompletedProcess[str]:
        if command[-3:] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "git@github.com:HomericIntelligence/Hephaestus.git\n",
                "",
            )
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **kwargs: _resolve_as(command, stdout=f"{requested_sha}\n"),
    )
    with pytest.raises(ValueError, match="resolved immutable commit SHA"):
        module._resolve_publication_commit(repo_root, "main")

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **kwargs: _resolve_as(
            command,
            returncode=1,
            stderr="unknown revision",
        ),
    )
    with pytest.raises(ValueError, match="does not resolve to an existing commit"):
        module._resolve_publication_commit(repo_root, requested_sha)

    resolved_sha = "b" * 40
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **kwargs: _resolve_as(command, stdout=f"{resolved_sha}\n"),
    )
    with pytest.raises(ValueError, match="resolved to a different commit"):
        module._resolve_publication_commit(repo_root, requested_sha)


def test_publication_commit_resolution_rejects_wrong_repository_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A local object database cannot stand in for the configured repository."""
    module = _load_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    requested_sha = "a" * 40

    def _run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        if command[-3:] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "https://github.com/OtherOrg/OtherRepository.git\n",
                "",
            )
        return subprocess.CompletedProcess(command, 0, f"{requested_sha}\n", "")

    monkeypatch.setattr(module.subprocess, "run", _run)

    with pytest.raises(ValueError, match="configured GitHub repository"):
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
        for stage in ("commit-pr", "review")
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

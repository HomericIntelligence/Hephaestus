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


def test_run_id_cannot_escape_the_private_evidence_root(tmp_path: Path) -> None:
    """Manifest resolution rejects traversal even when the target exists."""
    module = _load_module()
    run_root = tmp_path / "runs"
    run_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(ValueError, match="run id"):
        module._resolve_run_dir(run_root, "../outside")


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


def test_inventory_records_a_bounded_version_probe_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A hung provider version probe becomes durable failed inventory evidence."""
    module = _load_module()
    _, run_dir = _bootstrap_run(module, tmp_path)
    monkeypatch.setattr(module.shutil, "which", lambda _name: "/usr/bin/pi")
    monkeypatch.setattr(
        module,
        "load_pi_package_catalog",
        lambda: SimpleNamespace(required_commands=()),
    )
    monkeypatch.setattr(
        module,
        "inspect_pi_package_inventory",
        lambda *_args, **_kwargs: SimpleNamespace(
            ready=True,
            status="ready",
            detail="",
            roots={},
            scopes={},
        ),
    )
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(["pi", "--version"], 1)
        ),
    )
    monkeypatch.setattr(module, "DEFAULT_INVENTORY_TIMEOUT_SECONDS", 1)

    rc = module.main(
        [
            "--repo-root",
            str(tmp_path / "repo"),
            "--run-root",
            str(tmp_path / "build" / "pi-e2e-2519"),
            "inventory",
            "--run-id",
            run_dir.name,
        ]
    )

    assert rc == 1
    manifest = module._load_manifest(run_dir)
    assert manifest["pi"]["package_inventory"]["status"] == "version_probe_timeout"
    assert manifest["commands"][-1]["status"] == "failure"


def test_capture_analysis_does_not_promote_grants_or_mentions_to_invocations(
    tmp_path: Path,
) -> None:
    """Requested policy and provider text are not observed skill execution."""
    module = _load_module()
    proxy_log = tmp_path / "proxy.jsonl"
    proxy_log.write_text(
        json.dumps(
            {
                "event": "proxy-invocation",
                "tool": "pi",
                "argv": ["--commands", "skill:advise", "--tools", "read"],
                "real_binary": "/usr/bin/pi",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    analysis = module._capture_analysis(
        '{"type":"message","text":"skill:advise"}\n',
        "",
        proxy_log,
    )

    assert analysis["observed_skill_invocations"] == []
    assert analysis["requested_skill_grants"] == ["skill:advise"]
    assert analysis["provider_skill_mentions"] == ["skill:advise"]


def test_mnemosyne_uses_independent_typed_host_receipts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Advise/learn evidence is host-owned and never inferred from Pi output."""
    module = _load_module()
    repo_root, run_dir = _bootstrap_run(module, tmp_path)
    contract = {
        "athena_repository": "HomericIntelligence/Mnemosyne",
        "athena_commit": "a" * 40,
        "advise_sha256": "b" * 64,
        "learn_sha256": "c" * 64,
        "dependency_resolution_sha256": "d" * 64,
        "trust_source": "packaged",
    }
    binding = {
        "root": str(tmp_path / "mnemosyne"),
        "repository": "HomericIntelligence/Mnemosyne",
        "default_branch": "main",
        "commit_sha": "e" * 40,
        "trust_basis": "canonical",
        "athena_contract": contract,
    }
    monkeypatch.setattr(
        module,
        "load_athena_contract_receipt",
        lambda: SimpleNamespace(to_dict=lambda: contract),
    )
    monkeypatch.setattr(module, "default_mnemosyne_root", lambda: tmp_path / "mnemosyne")
    monkeypatch.setattr(module, "_live_mnemosyne_head", lambda _root: "e" * 40)
    monkeypatch.setattr(module, "_verify_live_learn_delivery", lambda _receipt: None)

    advise_result = {
        "kind": "advise",
        "context": "selected guidance",
        "receipt": {
            "contract": contract,
            "binding": binding,
            "corpus": {
                "repository": binding["repository"],
                "commit_sha": binding["commit_sha"],
                "selected_paths": ["skills/pi.md"],
                "entry_count": 1,
                "athena_contract": contract,
            },
        },
        "delivery_receipt": None,
        "error": None,
    }
    learn_result = {
        "kind": "learn",
        "context": "",
        "receipt": {"contract": contract, "binding": binding},
        "delivery_receipt": {
            "repository": "HomericIntelligence/Mnemosyne",
            "branch": "2519-learning",
            "base_branch": "main",
            "commit_sha": "f" * 40,
            "pr_url": "https://github.com/HomericIntelligence/Mnemosyne/pull/42",
            "pr_number": 42,
            "readback_head_sha": "f" * 40,
            "validation_evidence": ["tests passed"],
            "final_disposition": "recorded",
            "local_only": False,
        },
        "error": None,
    }
    module._store_generated_athena_receipts(
        run_dir,
        [
            {"job_type": "athena", "ok": True, "result": advise_result},
            {"job_type": "athena", "ok": True, "result": learn_result},
        ],
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
        == 0
    )
    assert [entry["kind"] for entry in module._load_manifest(run_dir)["athena_host_receipts"]] == [
        "advise",
        "learn",
    ]


def test_pi_capture_runs_normal_pipeline_command_without_direct_runtime_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pi evidence observes the queue CLI instead of reimplementing its stages."""
    module = _load_module()
    repo_root, run_dir = _bootstrap_run(module, tmp_path)
    command = [
        "uv",
        "run",
        "hephaestus-plan-issues",
        "--issues",
        "2519",
        "--parallel",
        "1",
        "--agent",
        "pi",
        "--json",
    ]
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        receipt_dir = Path(argv[argv.index("--evidence-receipt-dir") + 1])
        receipt_dir.mkdir(parents=True, exist_ok=True)
        (receipt_dir / "agent.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "job_type": "agent",
                    "provider": "pi",
                    "ok": True,
                    "session_id": "pipeline-session",
                    "tool_scopes": ["find", "grep", "ls", "read"],
                    "execution_request": {
                        "role": "planner",
                        "operation": "plan",
                        "lifecycle": "start_new",
                    },
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout='{"status":"ok"}\n',
            stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

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
            "discovery-plan",
            "--provider",
            "pi",
            "--",
            *command,
        ]
    )

    assert rc == 0
    assert seen[0][:-2] == command
    assert seen[0][-2] == "--evidence-receipt-dir"
    entry = module._load_manifest(run_dir)["commands"][-1]
    assert entry["command"] == seen[0]
    assert entry["session_ids"] == ["pipeline-session"]
    assert entry["tool_scopes"] == ["find", "grep", "ls", "read"]


def test_queue_capture_imports_host_owned_athena_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The host receipt must come from the same observed queue invocation."""
    module = _load_module()
    _repo_root, run_dir = _bootstrap_run(module, tmp_path)
    command = [
        "uv",
        "run",
        "hephaestus-automation-loop",
        "--issues",
        "2519",
        "--agent",
        "pi",
        "--json",
    ]
    monkeypatch.setattr(module, "_validate_athena_host_receipt", lambda *_args: None)

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        receipt_dir = Path(argv[argv.index("--evidence-receipt-dir") + 1])
        receipt_dir.mkdir(parents=True, exist_ok=True)
        (receipt_dir / "advise.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "job_type": "athena",
                    "ok": True,
                    "result": {
                        "kind": "advise",
                        "context": "selected",
                        "receipt": {"bound": True},
                        "delivery_receipt": None,
                        "error": None,
                    },
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert (
        module._record_command(
            run_dir,
            provider="pi",
            stage="implementation-review-handoff",
            command_argv=command,
            prompt="",
            prompt_file=None,
            timeout_seconds=60,
        )
        == 0
    )
    receipt = module._load_manifest(run_dir)["athena_host_receipts"]
    assert [entry["kind"] for entry in receipt] == ["advise"]


def test_pi_capture_rejects_commands_outside_normal_pipeline(tmp_path: Path) -> None:
    """A caller cannot relabel an arbitrary provider command as queue evidence."""
    module = _load_module()
    repo_root, run_dir = _bootstrap_run(module, tmp_path)
    marker = tmp_path / "direct-command-ran"

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
            "discovery-plan",
            "--provider",
            "pi",
            "--",
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch()",
        ]
    )

    assert rc == 1
    assert not marker.exists()
    assert module._load_manifest(run_dir)["commands"] == []


def test_comparison_requires_distinct_provider_capture(tmp_path: Path) -> None:
    """Comparison evidence requires both Pi and a distinct control provider."""
    module = _load_module()
    _, run_dir = _bootstrap_run(module, tmp_path)
    manifest = module._load_manifest(run_dir)
    manifest["commands"] = [
        {"kind": "capture", "provider": "pi", "stage": "discovery-plan"},
        {"kind": "capture", "provider": "codex", "stage": "discovery-plan"},
    ]
    module._save_manifest(run_dir, manifest)

    module._verify_comparison(manifest)

    manifest["commands"].pop()
    with pytest.raises(ValueError, match="distinct provider"):
        module._verify_comparison(manifest)

    manifest["commands"] = [
        {
            "kind": "capture",
            "provider": "pi",
            "stage": "discovery-plan",
            "prompt_sha256": "a" * 64,
        },
        {
            "kind": "capture",
            "provider": "codex",
            "stage": "implementation-review-handoff",
            "prompt_sha256": "a" * 64,
        },
    ]
    with pytest.raises(ValueError, match="same stage and prompt"):
        module._verify_comparison(manifest)


def test_host_receipt_artifact_cannot_escape_run_directory(tmp_path: Path) -> None:
    """A tampered manifest cannot rebind a host receipt to an outside file."""
    module = _load_module()
    _, run_dir = _bootstrap_run(module, tmp_path)
    outside = run_dir.parent / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    manifest = module._load_manifest(run_dir)
    manifest["athena_host_receipts"] = [
        {
            "kind": "advise",
            "source": "pipeline",
            "artifact": "../outside.json",
            "sha256": module._sha256_bytes(outside.read_bytes()),
        },
        {
            "kind": "learn",
            "source": "pipeline",
            "artifact": "../outside.json",
            "sha256": module._sha256_bytes(outside.read_bytes()),
        },
    ]

    with pytest.raises(ValueError, match="artifact"):
        module._verify_athena_host_receipts(manifest, run_dir)


def test_capture_verification_requires_each_pi_pipeline_observation() -> None:
    """One well-observed Pi call cannot mask a second unobserved queue call."""
    module = _load_module()
    manifest = {
        "commands": [
            {
                "kind": "capture",
                "provider": "pi",
                "stage": "discovery-plan",
                "session_ids": ["session-1"],
                "tool_scopes": ["read"],
                "pi_agent_receipts": [
                    {
                        "ok": True,
                        "session_id": "session-1",
                        "tool_scopes": ["read"],
                        "execution_request": {"role": "planner"},
                    }
                ],
            },
            {
                "kind": "capture",
                "provider": "pi",
                "stage": "implementation-review-handoff",
                "session_ids": [],
                "tool_scopes": [],
                "pi_agent_receipts": [],
            },
        ]
    }

    with pytest.raises(ValueError, match="implementation-review-handoff"):
        module._verify_capture(manifest)


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


def test_pi_failure_probe_persists_called_process_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A nonzero admitted pipeline outcome remains usable failure evidence."""
    module = _load_module()
    repo_root, run_dir = _bootstrap_run(module, tmp_path)
    command = [
        "uv",
        "run",
        "hephaestus-plan-issues",
        "--issues",
        "2519",
        "--agent",
        "pi",
        "--json",
    ]
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(
                7,
                command,
                output="sanitized stdout",
                stderr="sanitized stderr",
            )
        ),
    )

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
            "discovery-plan",
            "--provider",
            "pi",
            "--",
            *command,
        ]
    )

    assert rc == 0
    probe = module._load_manifest(run_dir)["commands"][-1]
    assert probe["returncode"] == 7
    assert probe["timed_out"] is False
    assert (run_dir / probe["artifacts"]["stderr"]).read_text() == "sanitized stderr"


def test_capture_timeout_records_a_bounded_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """capture() records a bounded queue timeout as durable failure evidence."""
    module = _load_module()
    repo_root, run_dir = _bootstrap_run(module, tmp_path)
    command = [
        "uv",
        "run",
        "hephaestus-plan-issues",
        "--issues",
        "2519",
        "--agent",
        "pi",
        "--json",
    ]
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(command, 1)),
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
                "discovery-plan",
                "--provider",
                "pi",
                "--timeout",
                "1",
                "--",
                *command,
            ]
        )
        == 124
    )

    capture = module._load_manifest(run_dir)["commands"][-1]
    assert capture["status"] == "failure"
    assert capture["returncode"] == 124
    assert capture["timed_out"] is True


def test_record_defect_binds_a_stable_live_repository_issue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A positive integer alone is not follow-up issue evidence."""
    module = _load_module()
    _repo_root, run_dir = _bootstrap_run(module, tmp_path)
    calls: list[int] = []
    payload = {
        "id": "I_kwDOQww0as5live",
        "number": 2600,
        "title": "Observed Pi defect",
        "state": "OPEN",
        "labels": [],
        "body": "Parent: #2519\n\nObserved failure.",
        "url": "https://github.com/HomericIntelligence/Hephaestus/issues/2600",
    }

    class FakeGitHub:
        def __init__(self, owner: str, *, repo: str, repo_root: Path) -> None:
            assert (owner, repo) == ("HomericIntelligence", "Hephaestus")
            assert isinstance(repo_root, Path)

        def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
            calls.append(issue_number)
            return dict(payload)

    monkeypatch.setattr(module, "PipelineGitHub", FakeGitHub)

    assert (
        module._record_defect(
            run_dir,
            summary="Observed Pi defect",
            follow_up_issue=2600,
            details="provider exited unexpectedly",
        )
        == 0
    )

    defect = module._load_manifest(run_dir)["defects"][-1]
    assert calls == [2600, 2600]
    assert defect["github"]["id"] == payload["id"]
    assert defect["github"]["state"] == "OPEN"


def test_attestation_rebinds_exact_head_and_review_state_to_live_github(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Publication derives PR authority from fresh host reads, not receipt booleans."""
    module = _load_module()
    _, run_dir = _bootstrap_run(module, tmp_path)
    head = "a" * 40
    state_calls: list[int] = []

    class FakeGitHub:
        def __init__(self, owner: str, *, repo: str, repo_root: Path) -> None:
            assert (owner, repo) == ("HomericIntelligence", "Hephaestus")

        def gh_pr_state(self, pr_number: int) -> dict[str, Any]:
            state_calls.append(pr_number)
            return {
                "state": "OPEN",
                "headRefOid": head,
                "mergedAt": None,
                "baseRefName": "main",
                "autoMergeRequest": None,
            }

        def pr_review_context(self, _pr_number: int) -> dict[str, str]:
            return {
                "pr_title": "chore: validate Pi",
                "pr_description": "Closes #2519",
                "pr_head_sha": head,
                "pr_base_sha": "b" * 40,
                "pr_base_branch": "main",
            }

        def find_issue_for_pr(self, _pr_number: int) -> int:
            return 2519

        def pr_has_implementation_state_label(self, _pr_number: int) -> tuple[bool, bool]:
            return True, False

        def list_unresolved_review_threads(self, _pr_number: int) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr(module, "PipelineGitHub", FakeGitHub)
    monkeypatch.setattr(module, "_verify_completion", lambda _manifest: None)
    monkeypatch.setattr(module, "_verify_publication", lambda *_args: None)
    monkeypatch.setattr(module, "_verify_athena_host_receipts", lambda *_args: None)

    assert (
        module._attest_publication(
            run_dir,
            repo="HomericIntelligence/Hephaestus",
            ref=head,
            pr_number=2737,
            report_path=tmp_path / "report.md",
            runbook_path=tmp_path / "runbook.md",
            verify_defects=False,
        )
        == 0
    )

    publication = module._load_manifest(run_dir)["publication"]
    assert state_calls == [2737, 2737]
    assert publication["commit_sha"] == head
    assert publication["pull_request"]["number"] == 2737
    assert publication["pull_request"]["implementation_go"] is True


def test_render_verify_and_publication_attestation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """render(), verify(), and attestation should round-trip the private manifest."""
    module = _load_module()
    repo_root, run_dir = _bootstrap_run(module, tmp_path)
    prompt = "capture prompt"
    head = "a" * 40
    monkeypatch.setattr(module, "_verify_athena_host_receipts", lambda *_args: None)
    monkeypatch.setattr(
        module,
        "_stable_live_pull_request",
        lambda _root, number: {"number": number, "head_sha": head},
    )
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
    manifest["athena_host_receipts"] = [
        {"kind": "advise", "source": "pipeline"},
        {"kind": "learn", "source": "pipeline"},
    ]
    manifest["commands"] = [
        {
            "id": f"{index:02d}-{stage}",
            "kind": "capture",
            "provider": "pi",
            "stage": stage,
            "status": "success",
            "returncode": 0,
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
            "pi_agent_receipts": [
                {
                    "ok": True,
                    "session_id": f"private-session-{index}",
                    "tool_scopes": ["read"],
                    "execution_request": {"role": "pipeline"},
                }
            ],
            "stdout_digest": "a" * 64,
            "stderr_digest": "b" * 64,
            "stdout_event_count": 1,
            "stderr_event_count": 0,
            "artifacts": {},
            "provider_invocations": [],
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
            "stage": "discovery-plan",
            "status": "success",
            "returncode": 0,
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
                head,
                "--pr",
                "2737",
                "--report",
                str(report),
                "--runbook",
                str(runbook),
            ]
        )
        == 0
    )
    publication_path = run_dir / "artifacts" / "publication.json"
    publication = json.loads(publication_path.read_text(encoding="utf-8"))
    assert publication["repo"] == "HomericIntelligence/Hephaestus"
    assert publication["ref"] == head


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
                "--pr",
                "2737",
                "--report",
                str(report),
                "--runbook",
                str(runbook),
            ]
        )
        == 1
    )

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


def test_capture_records_session_ids_tool_scopes_and_comparison_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """capture() must preserve provider output semantics and extract evidence."""
    module = _load_module()
    repo_root, run_dir = _bootstrap_run(module, tmp_path)
    provider_dir = tmp_path / "providers"
    provider_dir.mkdir()
    pi_real = provider_dir / "pi-real"
    codex_real = provider_dir / "codex-real"
    _write_provider(pi_real)
    _write_provider(codex_real)
    monkeypatch.setattr(
        module.shutil,
        "which",
        lambda name: str(pi_real) if name == "pi" else str(codex_real) if name == "codex" else None,
    )
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
        "--",
        "pi",
        str(argv_file),
        str(signal_file),
        "emit",
        "--tools",
        "read,grep",
        "--commands",
        "skill:advise",
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
    assert captures[0]["provider_invocations"][0]["real_binary"] == str(pi_real)
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
        "binary": str(pi_real),
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
                "comparison",
            ]
        )
        == 0
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
    """failure-probe() should invert a non-zero command into a successful probe."""
    module = _load_module()
    _, run_dir = _bootstrap_run(module, tmp_path)
    provider_dir = tmp_path / "providers"
    provider_dir.mkdir()
    failing_real = provider_dir / "pi-real"
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
        return str(failing_real) if name == "pi" else None

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
            "pi",
            "--",
            "pi",
            str(tmp_path / "argv.json"),
            str(tmp_path / "signal.txt"),
            "emit",
        ]
    )

    assert rc == 0
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    probe = manifest["commands"][-1]
    assert probe["kind"] == "capture"
    assert probe["status"] == "failure"


def test_capture_timeout_records_a_bounded_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """capture() must record a timed-out provider stage as a failure."""
    module = _load_module()
    repo_root, run_dir = _bootstrap_run(module, tmp_path)
    provider_dir = tmp_path / "providers"
    provider_dir.mkdir()
    pi_real = provider_dir / "pi-real"
    _write_provider(pi_real)
    monkeypatch.setattr(
        module.shutil,
        "which",
        lambda name: str(pi_real) if name == "pi" else None,
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
                "pi",
                "--timeout",
                "1",
                "--",
                "pi",
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
            "stage": "control",
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
                "main",
                "--report",
                str(report),
                "--runbook",
                str(runbook),
                "--verify-defects",
            ]
        )
        == 0
    )
    publication_path = run_dir / "artifacts" / "publication.json"
    publication = json.loads(publication_path.read_text(encoding="utf-8"))
    assert publication["repo"] == "HomericIntelligence/Hephaestus"
    assert publication["ref"] == "main"


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
                "--report",
                str(report),
                "--runbook",
                str(runbook),
            ]
        )
        == 1
    )

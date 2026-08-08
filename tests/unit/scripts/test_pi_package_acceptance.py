"""Tests for the commit-pinned Athena Pi package acceptance collector."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "pi_package_acceptance.py"
ATHENA_REF = "496815b00f6fb4c8e97466489371b364d52588b5"


def _load_module() -> ModuleType:
    assert SCRIPT.is_file(), "the Athena Pi acceptance collector must exist"
    spec = importlib.util.spec_from_file_location("pi_package_acceptance", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def acceptance_module() -> ModuleType:
    """Load the standalone collector as an importable module."""
    return _load_module()


def _write_catalog(path: Path, *, ref: str = ATHENA_REF) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "package": {
                    "source": "git:github.com/HomericIntelligence/Athena",
                    "version": "v0.4.0",
                    "ref": ref,
                },
                "compatibility": {
                    "pi": "@earendil-works/pi-coding-agent@0.80.2",
                    "delegation": "pi-subagents@0.37.2",
                    "web_access": "pi-web-access@0.15.0",
                },
                "upstream": {
                    "issue": "https://github.com/HomericIntelligence/Athena/issues/61",
                    "pull_request": "https://github.com/HomericIntelligence/Athena/pull/62",
                    "release_tag": "v0.4.0",
                    "required_check": "package",
                },
            }
        ),
        encoding="utf-8",
    )


def test_catalog_requires_an_exact_commit_ref(
    acceptance_module: ModuleType, tmp_path: Path
) -> None:
    """Mutable tags and abbreviated SHAs are never installation authority."""
    catalog_path = tmp_path / "catalog.json"
    _write_catalog(catalog_path, ref="v0.4.0")

    with pytest.raises(ValueError, match="40-character lowercase commit"):
        acceptance_module.load_catalog(catalog_path)


def test_catalog_parses_the_pinned_contract(acceptance_module: ModuleType, tmp_path: Path) -> None:
    """The accepted source, release metadata, and companions remain explicit."""
    catalog_path = tmp_path / "catalog.json"
    _write_catalog(catalog_path)

    catalog = acceptance_module.load_catalog(catalog_path)

    assert catalog.package.ref == ATHENA_REF
    assert catalog.install_spec.endswith(f"@{ATHENA_REF}")
    assert catalog.compatibility.delegation == "pi-subagents@0.37.2"


def test_collector_consumes_the_packaged_catalog_authority(
    acceptance_module: ModuleType,
) -> None:
    """Acceptance and runtime bootstrap read the same distributable pin source."""
    expected = SCRIPT.parents[1] / "hephaestus" / "agents" / "pi_package_catalog.json"

    assert expected == acceptance_module.CATALOG_PATH
    assert not (SCRIPT.parents[1] / "docs" / "athena-pi-package.json").exists()
    catalog = acceptance_module.load_catalog(expected)
    assert catalog.package.ref == ATHENA_REF
    assert catalog.compatibility.pi == "@earendil-works/pi-coding-agent@0.80.2"


def test_checkout_validation_requires_remote_head_and_clean_tree(
    acceptance_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance binds a clean checkout to both repository identity and SHA."""
    checkout = tmp_path / "athena"
    checkout.mkdir()
    results = iter(
        [
            subprocess.CompletedProcess(
                [], 0, "https://github.com/HomericIntelligence/Athena.git\n", ""
            ),
            subprocess.CompletedProcess([], 0, f"{ATHENA_REF}\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
    )
    monkeypatch.setattr(acceptance_module, "_run_command", lambda *args, **kwargs: next(results))

    acceptance_module.validate_checkout(
        checkout,
        "https://github.com/HomericIntelligence/Athena.git",
        ATHENA_REF,
    )


def test_checkout_validation_rejects_dirty_tree(
    acceptance_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uncommitted source invalidates acceptance evidence."""
    checkout = tmp_path / "athena"
    checkout.mkdir()
    results = iter(
        [
            subprocess.CompletedProcess(
                [], 0, "https://github.com/HomericIntelligence/Athena.git\n", ""
            ),
            subprocess.CompletedProcess([], 0, f"{ATHENA_REF}\n", ""),
            subprocess.CompletedProcess([], 0, "?? unexpected.txt\n", ""),
        ]
    )
    monkeypatch.setattr(acceptance_module, "_run_command", lambda *args, **kwargs: next(results))

    with pytest.raises(ValueError, match="not clean"):
        acceptance_module.validate_checkout(
            checkout,
            "https://github.com/HomericIntelligence/Athena.git",
            ATHENA_REF,
        )


def test_rpc_discovery_requires_package_origin_and_exact_installed_head(
    acceptance_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovery accepts only package-origin commands from the pinned checkout."""
    catalog_path = tmp_path / "catalog.json"
    _write_catalog(catalog_path)
    catalog = acceptance_module.load_catalog(catalog_path)
    installed_root = tmp_path / "agent" / "git" / "github.com" / "HomericIntelligence" / "Athena"
    (installed_root / "skills").mkdir(parents=True)
    (installed_root / "package.json").write_text(
        json.dumps({"pi": {"skills": ["./skills"]}}), encoding="utf-8"
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        if command[1:2] == ["install"]:
            return subprocess.CompletedProcess(command, 0, "installed", "")
        if command[:3] == ["git", "-C", str(installed_root)]:
            return subprocess.CompletedProcess(command, 0, f"{ATHENA_REF}\n", "")
        commands = [
            {
                "name": f"skill:{name}",
                "source": "skill",
                "sourceInfo": {"origin": "package", "baseDir": str(installed_root)},
            }
            for name in ("advise", "learn", "pr-review")
        ]
        payload = {
            "id": "skills",
            "type": "response",
            "command": "get_commands",
            "success": True,
            "data": {"commands": commands},
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload) + "\n", "")

    monkeypatch.setattr(acceptance_module, "_run_command", run)
    monkeypatch.setattr(
        acceptance_module.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path / "agent")
    )

    discovered = acceptance_module.install_and_discover(catalog, Path("/usr/bin/pi"))

    assert discovered.installed_commit == ATHENA_REF
    assert discovered.commands == ("skill:advise", "skill:learn", "skill:pr-review")
    assert calls[0][0] == ["/usr/bin/pi", "install", catalog.install_spec, "--no-approve"]
    child_env = calls[0][1]["env"]
    assert child_env["PI_CODING_AGENT_DIR"].endswith("agent")
    assert child_env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GH_TOKEN" not in child_env
    assert "GITHUB_TOKEN" not in child_env


def _write_athena_archive(path: Path, manifest: dict[str, Any]) -> None:
    with tarfile.open(path, mode="w:gz") as archive:
        files = {
            "package.json": json.dumps(manifest).encode(),
            "skills/advise/SKILL.md": b"advise",
            "skills/learn/SKILL.md": b"learn",
            "skills/pr-review/SKILL.md": b"pr-review",
        }
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))


def test_archive_inspection_enforces_native_package_boundary(
    acceptance_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The downstream audit accepts resources only and rejects bundled dependencies."""
    checkout = tmp_path / "Athena"
    archive_path = checkout / "dist" / "athena-plugin-0.4.0.tar.gz"
    archive_path.parent.mkdir(parents=True)
    manifest = {
        "name": "@homericintelligence/athena",
        "version": "0.4.0",
        "pi": {"skills": ["./skills"]},
    }
    _write_athena_archive(archive_path, manifest)
    monkeypatch.setattr(
        acceptance_module,
        "_run_command",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )

    evidence = acceptance_module.inspect_athena_archive(checkout)

    assert evidence.members == 4
    assert len(evidence.sha256) == 64

    manifest["dependencies"] = {"pi-subagents": "0.37.2"}
    _write_athena_archive(archive_path, manifest)
    with pytest.raises(ValueError, match="forbidden fields"):
        acceptance_module.inspect_athena_archive(checkout)


def test_output_directory_must_remain_beneath_build_and_reject_symlinks(
    acceptance_module: ModuleType, tmp_path: Path
) -> None:
    """Generated evidence cannot escape build or follow a symlink component."""
    repo = tmp_path / "repo"
    build = repo / "build"
    build.mkdir(parents=True)

    with pytest.raises(ValueError, match="beneath build"):
        acceptance_module.prepare_output_directory(repo, repo / "outside")

    target = tmp_path / "target"
    target.mkdir()
    (build / "linked").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        acceptance_module.prepare_output_directory(repo, build / "linked" / "evidence")


def test_atomic_json_write_replaces_an_existing_artifact(
    acceptance_module: ModuleType, tmp_path: Path
) -> None:
    """Ordinary sibling-temp replacement updates complete JSON atomically."""
    destination = tmp_path / "acceptance.json"
    destination.write_text("old", encoding="utf-8")

    acceptance_module.atomic_write(destination, '{"schema_version":1}\n')

    assert json.loads(destination.read_text(encoding="utf-8")) == {"schema_version": 1}
    assert list(tmp_path.glob(".acceptance.json.*.tmp")) == []


class FakeTransport:
    """Small endpoint map used to validate collector API bindings."""

    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        self.responses = responses

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        del body
        return self.responses[(method, path)]


def test_remote_receipts_bind_issue_pr_tag_check_and_implementation(
    acceptance_module: ModuleType, tmp_path: Path
) -> None:
    """Every remote receipt must resolve to the exact accepted commits."""
    catalog_path = tmp_path / "catalog.json"
    _write_catalog(catalog_path)
    catalog = acceptance_module.load_catalog(catalog_path)
    implementation_head = "a" * 40
    transport = FakeTransport(
        {
            ("GET", "/repos/HomericIntelligence/Athena/issues/61"): {"state": "closed"},
            ("GET", "/repos/HomericIntelligence/Athena/pulls/62"): {
                "merged": True,
                "merge_commit_sha": ATHENA_REF,
            },
            ("GET", "/repos/HomericIntelligence/Athena/commits/v0.4.0"): {"sha": ATHENA_REF},
            (
                "GET",
                f"/repos/HomericIntelligence/Athena/commits/{ATHENA_REF}/check-runs?per_page=100",
            ): {
                "check_runs": [
                    {
                        "name": "package",
                        "head_sha": ATHENA_REF,
                        "status": "completed",
                        "conclusion": "success",
                        "html_url": "https://github.com/example/check/1",
                    }
                ]
            },
            ("GET", "/repos/HomericIntelligence/Hephaestus/pulls/77"): {
                "body": (
                    "Upstream: https://github.com/HomericIntelligence/Athena/issues/61"
                    "\n\nCloses #2515\n"
                ),
                "head": {"sha": implementation_head},
                "html_url": "https://github.com/HomericIntelligence/Hephaestus/pull/77",
            },
        }
    )

    receipts = acceptance_module.validate_remote_receipts(catalog, 77, transport)

    assert receipts.implementation_head == implementation_head
    assert receipts.check_url.endswith("/1")


def test_remote_receipts_reject_malformed_check_payload(
    acceptance_module: ModuleType, tmp_path: Path
) -> None:
    """Malformed GitHub data fails closed instead of manufacturing evidence."""
    catalog_path = tmp_path / "catalog.json"
    _write_catalog(catalog_path)
    catalog = acceptance_module.load_catalog(catalog_path)
    transport = FakeTransport(
        {
            ("GET", "/repos/HomericIntelligence/Athena/issues/61"): {"state": "closed"},
            ("GET", "/repos/HomericIntelligence/Athena/pulls/62"): {
                "merged": True,
                "merge_commit_sha": ATHENA_REF,
            },
            ("GET", "/repos/HomericIntelligence/Athena/commits/v0.4.0"): {"sha": ATHENA_REF},
            (
                "GET",
                f"/repos/HomericIntelligence/Athena/commits/{ATHENA_REF}/check-runs?per_page=100",
            ): [],
        }
    )

    with pytest.raises(ValueError, match="check-runs"):
        acceptance_module.validate_remote_receipts(catalog, 77, transport)

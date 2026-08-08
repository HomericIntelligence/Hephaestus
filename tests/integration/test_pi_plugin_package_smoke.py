"""Gated live smoke for the catalog-pinned Pi CLI and package set."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hephaestus.agents.pi_plugins import preflight_pi_environment


@pytest.mark.nightly
def test_catalog_pinned_packages_install_and_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compatible Pi preflight loads packages without ambient extensions."""
    if os.environ.get("HEPHAESTUS_REQUIRE_PI_PACKAGE_SMOKE") != "1":
        pytest.skip("set HEPHAESTUS_REQUIRE_PI_PACKAGE_SMOKE=1 for live package evidence")
    command = shutil.which("hephaestus-install-pi-plugins")
    assert command is not None, "installed console script is required"
    assert shutil.which("pi") is not None, "catalog-pinned Pi CLI is required"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    env = dict(os.environ)
    env["PI_CODING_AGENT_DIR"] = str(tmp_path / "pi-agent")

    result = subprocess.run(
        [
            command,
            "--global",
            "--yes",
            "--no-approve",
            "--timeout",
            "180",
            "--json",
        ],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=600,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["ready"] is True
    assert payload["status"] == "ready"

    pi_dir = Path(env["PI_CODING_AGENT_DIR"])
    sentinel = tmp_path / "compatible-sentinel-loaded"
    extension_dir = pi_dir / "extensions"
    extension_dir.mkdir(parents=True)
    (extension_dir / "sentinel.ts").write_text(
        (
            'import { writeFileSync } from "node:fs";\n'
            f'writeFileSync({json.dumps(str(sentinel))}, "loaded");\n'
            "export default function () {}\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_dir))

    preflight = preflight_pi_environment(cwd, pi_dir=pi_dir, timeout=180)

    assert preflight.ready is True, preflight.remediation_message()
    assert not sentinel.exists(), "compatible Pi loaded an ambient extension during preflight"


@pytest.mark.nightly
def test_incompatible_real_pi_fails_before_loading_extensions(tmp_path: Path) -> None:
    """A real wrong Pi release cannot execute even a configured sentinel extension."""
    if os.environ.get("HEPHAESTUS_REQUIRE_PI_PACKAGE_SMOKE") != "1":
        pytest.skip("set HEPHAESTUS_REQUIRE_PI_PACKAGE_SMOKE=1 for live package evidence")
    command = shutil.which("hephaestus-install-pi-plugins")
    npm = shutil.which("npm")
    assert command is not None, "installed console script is required"
    assert npm is not None, "npm is required for incompatible-CLI evidence"
    prefix = tmp_path / "npm-prefix"
    install = subprocess.run(
        [
            npm,
            "install",
            "--global",
            "--prefix",
            str(prefix),
            "--ignore-scripts",
            "@earendil-works/pi-coding-agent@0.80.1",
        ],
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
    )
    assert install.returncode == 0, install.stderr
    pi_dir = tmp_path / "pi-agent"
    sentinel = tmp_path / "sentinel-loaded"
    extension_dir = pi_dir / "extensions"
    extension_dir.mkdir(parents=True)
    (extension_dir / "sentinel.ts").write_text(
        (
            'import { writeFileSync } from "node:fs";\n'
            f'writeFileSync({json.dumps(str(sentinel))}, "loaded");\n'
            "export default function () {}\n"
        ),
        encoding="utf-8",
    )
    cwd = tmp_path / "repo"
    cwd.mkdir()
    env = dict(os.environ)
    env["PI_CODING_AGENT_DIR"] = str(pi_dir)
    env["PATH"] = str(prefix / "bin") + os.pathsep + env.get("PATH", "")

    result = subprocess.run(
        [command, "--global", "--yes", "--no-approve", "--json"],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 1, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "pi_cli_version_mismatch"
    assert not sentinel.exists(), "wrong Pi CLI loaded an extension before rejection"

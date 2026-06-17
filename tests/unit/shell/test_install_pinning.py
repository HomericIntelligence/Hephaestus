"""Regression tests for scripts/shell/install.sh installer pinning.

Closes #744.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SH = REPO_ROOT / "scripts" / "shell" / "install.sh"

PINNED_TOOLS = ["pixi", "dagger", "just"]  # tailscale uses apt path; no SHA needed
HEX64 = re.compile(r"^[0-9a-f]{64}$")


@pytest.fixture(scope="module")
def install_script() -> str:
    """Return the install.sh source text, asserting the script exists."""
    assert INSTALL_SH.exists(), f"install.sh missing at {INSTALL_SH}"
    return INSTALL_SH.read_text()


@pytest.mark.parametrize("tool", PINNED_TOOLS)
def test_pinned_tool_declares_version_constant(install_script: str, tool: str) -> None:
    """Each pinned tool must declare a readonly <TOOL>_VERSION constant."""
    pattern = rf'^readonly {tool.upper()}_VERSION="[^"]+"'
    assert re.search(pattern, install_script, re.MULTILINE), (
        f"{tool} must declare a readonly {tool.upper()}_VERSION constant"
    )


@pytest.mark.parametrize("tool", PINNED_TOOLS)
def test_pinned_tool_sha256_constants_are_real_hashes(install_script: str, tool: str) -> None:
    """SHA-256 constants must be 64-char lowercase hex — no placeholders."""
    sha_pattern = rf'^readonly {tool.upper()}_SHA256_[A-Z0-9_]+="([^"]+)"'
    matches = re.findall(sha_pattern, install_script, re.MULTILINE)
    assert matches, f"{tool} must declare at least one {tool.upper()}_SHA256_* constant"
    for value in matches:
        assert HEX64.match(value), (
            f"{tool.upper()}_SHA256_* contains non-hex or wrong-length value: {value!r}"
        )


@pytest.mark.parametrize(
    "fragment",
    [
        "pixi.sh/install.sh",
        "dl.dagger.io/dagger/install.sh",
        "just.systems/install.sh",
        "tailscale.com/install.sh",
    ],
)
def test_no_unverified_curl_bash_for_pinned_tools(install_script: str, fragment: str) -> None:
    """No pinned-tool installer may be piped straight from curl into a shell."""
    bad = rf"curl[^|\n]*{re.escape(fragment)}[^|\n]*\|\s*(?:bash|sh)"
    matches = re.findall(bad, install_script)
    assert not matches, f"Unverified curl|bash for {fragment}: {matches}"


def test_download_and_verify_helper_defined(install_script: str) -> None:
    """install.sh must define the download_and_verify() helper function."""
    assert "download_and_verify()" in install_script, (
        "install.sh must define download_and_verify() helper"
    )


def test_trust_model_documented_for_unpinned(install_script: str) -> None:
    """Unpinned installers (Homebrew, npm) must document their TRUST MODEL."""
    # Both Homebrew and npm/claude must carry a TRUST MODEL comment.
    assert install_script.count("TRUST MODEL") >= 2, (
        "install.sh must document TRUST MODEL for Homebrew AND npm sections"
    )


def test_podman_socket_uses_secure_runtime_directory(install_script: str) -> None:
    """Podman socket setup uses a hardened TMPDIR subtree."""
    assert "XDG_RUNTIME_DIR" not in install_script
    assert "/run/user" not in install_script
    assert 'INSTALL_TMP_ROOT="${TMPDIR:-/tmp}/hephaestus-$(id -u)"' in install_script
    assert "readonly INSTALL_TMP_ROOT" in install_script
    assert "make_secure_tmp_component()" in install_script
    assert 'local base="$INSTALL_TMP_ROOT"' in install_script
    assert 'chmod 700 "$base" "$base/$component"' in install_script
    assert 'PODMAN_SOCKET_DIR="$(make_secure_runtime_dir podman)"' in install_script


def test_download_and_verify_rejects_bad_hash(tmp_path: Path) -> None:
    """Functional test: helper must exit non-zero on hash mismatch.

    Sources install.sh in a subshell, calls download_and_verify with a
    deliberately wrong SHA against a small file served by `cat` over file://.
    Uses a known-content local file to avoid network dependency.
    """
    fixture = tmp_path / "payload.txt"
    fixture.write_text("hello\n")
    bad_hash = "0" * 64
    # Use file:// URL so curl can fetch without network.
    script = f"""
        set -e
        source {INSTALL_SH}
        download_and_verify {bad_hash} "file://{fixture}" "{tmp_path}/downloaded.txt"
    """
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode != 0, (
        f"download_and_verify should reject bad hash, got rc={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )

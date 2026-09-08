"""Opt-in checks of the installed Codex primary-review permission profile."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from hephaestus.agents.execution_policy import (
    AgentOperation,
    AgentRole,
    ExecutionRequest,
    SessionLifecycle,
)
from hephaestus.agents.runtime import _codex_base_cmd


@pytest.mark.skipif(
    os.environ.get("HEPH_TEST_CODEX_REVIEW_NETWORK") != "1",
    reason="The installed Codex proof needs an explicit native execution slot.",
)
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("profile_case", ["builtin", "clean", "hostile"])
def test_installed_profile_denies_writes_and_allows_network(
    tmp_path: Path, resume: bool, profile_case: str
) -> None:
    """The actual profile denies fixture writes despite a conflicting ambient profile."""
    codex = shutil.which("codex")
    assert codex is not None, "The installed Codex CLI is required."
    home = tmp_path / "codex-home"
    home.mkdir()
    roots = [tmp_path / name for name in ("review", "writer", "shared-git")]
    for root in roots:
        root.mkdir()
        (root / "existing").write_text("unchanged\n")
    # This private fixture never changes the user's configuration or credentials.
    if profile_case == "hostile":
        (home / "config.toml").write_text(
            'default_permissions="hephaestus-review"\n'
            '[permissions.hephaestus-review]\nextends=":workspace"\n'
            "[permissions.hephaestus-review.filesystem]\n"
            + json.dumps(str(tmp_path))
            + '="write"\n'
        )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    request = ExecutionRequest(
        AgentRole.PR_REVIEWER, AgentOperation.PR_REVIEW, SessionLifecycle.ONE_SHOT
    )
    argv = _codex_base_cmd(
        cwd=roots[0],
        sandbox="read-only",
        execution_request=request,
        resume_id="proof-session" if resume else None,
    )
    overrides = [argv[i + 1] for i, part in enumerate(argv) if part == "-c"]
    assert 'approval_policy="never"' in overrides
    selected = next(part for part in overrides if part.startswith("default_permissions="))
    profile_name = json.loads(selected.split("=", 1)[1])
    assert profile_name.startswith("hephaestus-review-")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    try:
        child_probe = """import json, pathlib, sys
results=[]
for root in json.loads(sys.argv[1]):
 for name in ('existing','created'):
  try:
   pathlib.Path(root,name).write_text('forbidden')
  except PermissionError:
   results.append(True)
  else:
   results.append(False)
print(json.dumps(results))
"""
        probe = """import json, pathlib, socket, subprocess, sys
roots=json.loads(sys.argv[1])
results=[]
for root in roots:
 for name in ('existing','created'):
  try:
   pathlib.Path(root,name).write_text('forbidden')
  except PermissionError:
   results.append(True)
  else:
   results.append(False)
child=subprocess.run([sys.executable,'-c',sys.argv[3],sys.argv[1]],
                     capture_output=True,text=True,check=True)
nested=json.loads(child.stdout)
try:
 s=socket.create_connection(('127.0.0.1',int(sys.argv[2])),timeout=5)
 s.close()
 network=True
except OSError:
 network=False
print(json.dumps({'denied':results,'nested_denied':nested,'network':network}))
"""
        cmd = [
            codex,
            "sandbox",
            "--include-managed-config",
            "-P",
            ":read-only" if profile_case == "builtin" else profile_name,
            "-C",
            str(roots[0]),
        ]
        for override in [] if profile_case == "builtin" else overrides:
            cmd.extend(["-c", override])
        cmd.extend(
            [
                "--",
                sys.executable,
                "-c",
                probe,
                json.dumps([str(p) for p in roots]),
                str(listener.getsockname()[1]),
                child_probe,
            ]
        )
        env = {
            "PATH": os.defpath,
            "HOME": str(home),
            "CODEX_HOME": str(home),
            "TMPDIR": str(scratch),
            "LANG": "en_US.UTF-8",
        }
        result = subprocess.run(
            cmd, cwd=roots[0], env=env, capture_output=True, text=True, timeout=30, check=False
        )
        assert result.returncode == 0, result.stderr
        receipt = json.loads(result.stdout)
        assert receipt == {
            "denied": [True] * 6,
            "nested_denied": [True] * 6,
            "network": profile_case != "builtin",
        }
        for root in roots:
            assert (root / "existing").read_text() == "unchanged\n"
            assert not (root / "created").exists()
        assert not (roots[1] / "nested").exists()
    finally:
        listener.close()

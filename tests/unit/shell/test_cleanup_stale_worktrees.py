"""Regression tests for scripts/shell/cleanup-stale-worktrees.sh."""

# The stale-script validator records the path relative to ``scripts/``.
# Keep that operator-facing path explicit while this test executes the script.
# shell/cleanup-stale-worktrees.sh

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CLEANUP_SH = REPO_ROOT / "scripts" / "shell" / "cleanup-stale-worktrees.sh"


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


def test_cleanup_stale_worktrees_shell_preserves_spaced_path(tmp_path: Path) -> None:
    """Executing the shell command keeps a spaced worktree path as one argv."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls_file = tmp_path / "calls.jsonl"
    repo_root = tmp_path / "repo"
    spaced_path = repo_root / ".worktrees" / "123 finished"
    repo_root.mkdir()
    spaced_path.mkdir(parents=True)

    _write_executable(
        fake_bin / "git",
        f"""#!{sys.executable}
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_CALLS_FILE"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({{"cmd": "git", "args": args}}) + "\\n")

repo_root = os.environ["FAKE_REPO_ROOT"]
spaced_path = os.path.join(repo_root, ".worktrees", "123 finished")

if args == ["rev-parse", "--show-toplevel"]:
    print(repo_root)
elif args == ["worktree", "list", "--porcelain", "-z"]:
    fields = [
        "worktree " + repo_root,
        "HEAD abcdef",
        "branch refs/heads/main",
        "",
        "worktree " + spaced_path,
        "HEAD 123456",
        "branch refs/heads/123-finished",
        "",
    ]
    sys.stdout.buffer.write("\\0".join(fields).encode())
elif args == ["merge-base", "--is-ancestor", "123-finished", "main"]:
    sys.exit(1)
elif args == ["-C", spaced_path, "status", "--porcelain"]:
    sys.exit(0)
elif args[:2] == ["worktree", "remove"]:
    raise SystemExit("dry-run must not remove worktrees")
elif args[:2] == ["branch", "-d"]:
    raise SystemExit("dry-run must not delete branches")
else:
    raise SystemExit(f"unexpected git args: {{args!r}}")
""",
    )
    _write_executable(
        fake_bin / "gh",
        f"""#!{sys.executable}
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_CALLS_FILE"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({{"cmd": "gh", "args": args}}) + "\\n")

if args == ["issue", "view", "123", "--json", "state", "--jq", ".state"]:
    print("CLOSED")
else:
    raise SystemExit(f"unexpected gh args: {{args!r}}")
""",
    )

    env = {
        **os.environ,
        "FAKE_CALLS_FILE": str(calls_file),
        "FAKE_REPO_ROOT": str(repo_root),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    result = subprocess.run(
        ["bash", str(CLEANUP_SH), "--dry-run", "--trunk", "main"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert f"Would remove stale worktree {spaced_path}" in result.stdout
    assert "branch 123-finished" in result.stdout

    calls = [json.loads(line) for line in calls_file.read_text(encoding="utf-8").splitlines()]
    assert {
        "cmd": "git",
        "args": ["-C", str(spaced_path), "status", "--porcelain"],
    } in calls
    assert not any(
        call["cmd"] == "git" and call["args"][:2] == ["worktree", "remove"] for call in calls
    )

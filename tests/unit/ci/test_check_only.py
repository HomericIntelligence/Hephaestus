"""Test the check-only command through isolated process boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK_REPO = "https://github.com/pre-commit/pre-commit-hooks"
HOOK_REV = "3e8a8703264a2f4a69428a0aa4dcb512790b2c8c"
MARKDOWN_REPO = "https://github.com/DavidAnson/markdownlint-cli2"
MARKDOWN_REV = "7339935a3036f096ee0d7f1f047a9ec41707d4c1"

# These executable fixtures replace external tools. They make real changes to
# their input files, so the test does not depend on a reported command alone.
TOOL_BODY = r"""
import json
import os
import stat
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["CHECK_ONLY_TOOL_LOG"], "a") as stream:
    stream.write(json.dumps({"name": name, "args": args, "cwd": os.getcwd()}) + "\n")
if os.environ.get("CHECK_ONLY_TOOL_FAIL") == name:
    print("fixture validator failure", file=sys.stderr)
    raise SystemExit(37)
if name == "uv":
    if args[:3] == ["run", "ruff", "format"] and "--check" not in args:
        Path(args[-1]).write_text("VALUE = 1\n")
    if args[:3] == ["run", "ruff", "check"] and "--no-fix" not in args:
        Path(args[-1]).write_text("VALUE = 1\n")
    raise SystemExit(0)
if name == "markdownlint-cli2":
    config = json.loads(Path(".markdownlint-cli2.jsonc").read_text())
    if "--fix" in args or config.get("fix"):
        target = Path(args[-1])
        target.write_bytes(target.read_bytes().rstrip() + b"\n")
    raise SystemExit(0)
if name == "trailing-whitespace-fixer":
    changed = False
    for value in args:
        target = Path(value)
        before = target.read_bytes()
        after = b"\n".join(line.rstrip(b" \t") for line in before.split(b"\n"))
        if after != before:
            target.write_bytes(after)
            target.chmod(stat.S_IRUSR | stat.S_IWUSR)
            changed = True
    raise SystemExit(int(changed))
raise SystemExit("unexpected fixture command")
"""


@dataclass
class Workspace:
    """Own private source, tool, cache, and process-output paths."""

    root: Path
    tools: Path
    cache: Path
    log: Path
    scratch: Path

    def git(self, *args: str) -> bytes:
        """Run Git only in the private fixture repository."""
        return subprocess.check_output(["git", *args], cwd=self.root)

    def write(self, name: str, value: str) -> Path:
        """Write a private fixture file."""
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def config(self, repos: list[dict[str, Any]], **options: Any) -> None:
        """Write the actual pre-commit configuration consumed by the CLI."""
        self.write(".pre-commit-config.yaml", yaml.safe_dump({**options, "repos": repos}))

    def stage(self) -> None:
        """Stage the private candidate, including new source files."""
        self.git("add", "--all")

    def tool(self, name: str, directory: Path | None = None) -> Path:
        """Create an executable tool fixture outside the candidate source."""
        path = (directory or self.tools) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!{sys.executable}\n" + TOOL_BODY)
        path.chmod(0o755)
        return path

    def events(self) -> list[dict[str, Any]]:
        """Read actual subprocess invocations recorded by the fixtures."""
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def environment(self, **overrides: str) -> dict[str, str]:
        """Set an isolated hook cache and prevent dependency refresh."""
        env = dict(os.environ)
        for key in tuple(env):
            if key.startswith("GIT_"):
                del env[key]
        env.update(
            PATH=str(self.tools) + os.pathsep + os.defpath,
            PYTHONPATH=str(REPO_ROOT),
            PYTHONDONTWRITEBYTECODE="1",
            PRE_COMMIT_HOME=str(self.cache),
            TMPDIR=str(self.scratch),
            UV_NO_SYNC="1",
            UV_OFFLINE="1",
            CHECK_ONLY_TOOL_LOG=str(self.log),
        )
        env.update(overrides)
        return env

    def run(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        """Run the public command and distinguish an absent CLI from a failure."""
        result = subprocess.run(
            [sys.executable, "-B", "-m", "hephaestus.ci.check_only"],
            cwd=self.root,
            env=self.environment(**overrides),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert "No module named hephaestus.ci.check_only" not in result.stderr, (
            "The check-only CLI is not implemented: " + result.stderr
        )
        assert "Traceback (most recent call last)" not in result.stderr, result.stderr
        return result


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    """Create a committed baseline without hooks or signing requirements."""
    root = tmp_path / "candidate"
    root.mkdir()
    value = Workspace(
        root, tmp_path / "tools", tmp_path / "cache", tmp_path / "tools.jsonl", tmp_path / "scratch"
    )
    value.tools.mkdir()
    (value.tools / "python3").symlink_to(sys.executable)
    value.scratch.mkdir()
    value.log.write_text("")
    value.git("init", "--quiet", "--template=")
    value.write(".gitignore", "build/\n.heph-private-denylist\n")
    value.write("README.md", "# Fixture\n")
    value.stage()
    value.git(
        "-c",
        "user.name=Check-only fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "--quiet",
        "-m",
        "fixture baseline",
    )
    value.tool("uv")
    return value


def _local(hook_id: str, entry: str, **options: Any) -> dict[str, Any]:
    return {"id": hook_id, "name": hook_id, "entry": entry, "language": "system", **options}


def _source_state(root: Path) -> dict[str, tuple[int, str]]:
    """Bind every original file, symlink, mode, and Git metadata file."""
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            data = os.fsencode(os.readlink(path))
        elif path.is_file():
            data = path.read_bytes()
        else:
            continue
        result[str(path.relative_to(root))] = (
            stat.S_IMODE(path.lstat().st_mode),
            hashlib.sha256(data).hexdigest(),
        )
    return result


def _cache_remote(
    workspace: Workspace, hook_id: str, entry: str, *, markdown: bool = False
) -> dict[str, Any]:
    """Prepare a cache fixture; the command must only read this cache."""
    repo, rev = (MARKDOWN_REPO, MARKDOWN_REV) if markdown else (HOOK_REPO, HOOK_REV)
    cached = workspace.cache / "pinned-repository"
    cached.mkdir(parents=True)
    manifest = [
        {"id": hook_id, "name": hook_id, "entry": entry, "language": "system", "types": ["text"]}
    ]
    (cached / ".pre-commit-hooks.yaml").write_text(yaml.safe_dump(manifest))
    with sqlite3.connect(workspace.cache / "db.db") as connection:
        connection.execute(
            "CREATE TABLE repos (repo TEXT, ref TEXT, path TEXT, PRIMARY KEY (repo, ref))"
        )
        connection.execute("INSERT INTO repos VALUES (?, ?, ?)", (repo, rev, str(cached)))
    workspace.tool(entry)
    return {"repo": repo, "rev": rev, "hooks": [{"id": hook_id}]}


def test_external_tool_fixture_reports_real_exit_and_source_change(workspace: Workspace) -> None:
    """Keep a passing control independent of the new product command."""
    target = workspace.write("sample.txt", "value  \n")
    tool = workspace.tool("trailing-whitespace-fixer")
    result = subprocess.run(
        [str(tool), str(target)], env=workspace.environment(), capture_output=True, check=False
    )
    assert result.returncode == 1
    assert target.read_bytes() == b"value\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert workspace.events()[0]["name"] == "trailing-whitespace-fixer"


def test_native_checks_keep_configuration_file_selection_and_source(workspace: Workspace) -> None:
    """Keep configured selectors while applying native check flags."""
    for name in ("src/keep.py", "src/hook_skip.py", "root_skip.py", "src/notes.txt"):
        workspace.write(name, "VALUE=1\n")
    workspace.write("src/new.py", "NEW=2\n")
    workspace.config(
        [
            {
                "repo": "local",
                "hooks": [
                    _local(
                        "ruff-format-python",
                        "uv run ruff format",
                        files=r"\.py$",
                        exclude="hook_skip",
                        types=["python"],
                    ),
                    _local(
                        "ruff-check-python",
                        "uv run ruff check --fix",
                        files=r"\.py$",
                        exclude="hook_skip",
                        types_or=["python", "markdown"],
                        exclude_types=["markdown"],
                    ),
                ],
            }
        ],
        files="^src/",
        exclude="root_skip",
    )
    workspace.stage()
    workspace.write("src/untracked.py", "PENDING=3\n")
    before = _source_state(workspace.root)
    result = workspace.run(SKIP="ruff-format-python,ruff-check-python")
    assert result.returncode == 0, result.stdout + result.stderr
    events = workspace.events()
    assert len(events) == 2
    for event in events:
        assert {arg for arg in event["args"] if arg.endswith(".py")} == {
            "src/keep.py",
            "src/new.py",
            "src/untracked.py",
        }
        assert event["cwd"] != str(workspace.root)
    assert "--check" in events[0]["args"]
    assert "--no-fix" in events[1]["args"]
    assert "--fix" not in events[1]["args"]
    assert _source_state(workspace.root) == before
    assert not workspace.cache.exists()


def test_whole_directory_always_run_and_hook_stage_are_preserved(workspace: Workspace) -> None:
    """Keep directory arguments, always-run checks, and stage boundaries."""
    workspace.write("src/item.py", "VALUE = 1\n")
    workspace.write("scripts/run_fast_tests.sh", "#!/bin/sh\nexec uv run fast-fixture\n")
    workspace.config(
        [
            {
                "repo": "local",
                "hooks": [
                    _local(
                        "mypy-check-python",
                        "uv run mypy hephaestus/ scripts/ tests/",
                        files=r"\.py$",
                        pass_filenames=False,
                    ),
                    _local(
                        "fast-tests",
                        "bash scripts/run_fast_tests.sh",
                        files="^absent$",
                        always_run=True,
                        pass_filenames=False,
                    ),
                    _local("pip-audit", "uv run pip-audit", always_run=True, stages=["manual"]),
                    _local(
                        "dco-signoff-msg",
                        "python3 scripts/check_dco_signoff.py",
                        always_run=True,
                        stages=["commit-msg"],
                    ),
                ],
            }
        ]
    )
    workspace.stage()
    result = workspace.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert [event["args"] for event in workspace.events()] == [
        ["run", "mypy", "hephaestus/", "scripts/", "tests/"],
        ["run", "fast-fixture"],
    ]


def test_hook_failure_is_not_reported_as_success(workspace: Workspace) -> None:
    """Report an external validator failure as a failed check."""
    workspace.write("sample.py", "VALUE = 1\n")
    workspace.config(
        [
            {
                "repo": "local",
                "hooks": [_local("ruff-format-python", "uv run ruff format", types=["python"])],
            }
        ]
    )
    workspace.stage()
    result = workspace.run(CHECK_ONLY_TOOL_FAIL="uv")
    assert result.returncode == 1
    assert "ruff-format-python" in result.stdout + result.stderr
    assert "fixture validator failure" in result.stdout + result.stderr


def test_pygrep_uses_configured_expression_and_exclusion(workspace: Workspace) -> None:
    """Run the configured expression against only the selected files."""
    workspace.write("scripts/allowed.sh", "echo acceptable\n")
    workspace.write("scripts/excluded.sh", "echo forbidden-marker\n")
    workspace.config(
        [
            {
                "repo": "local",
                "hooks": [
                    {
                        "id": "forbid-or-true",
                        "name": "rule",
                        "language": "pygrep",
                        "entry": "forbidden-marker",
                        "files": r"\.sh$",
                        "exclude": "excluded",
                    }
                ],
            }
        ]
    )
    workspace.stage()
    assert workspace.run().returncode == 0
    workspace.write("scripts/allowed.sh", "echo forbidden-marker\n")
    result = workspace.run()
    assert result.returncode == 1
    assert "scripts/allowed.sh" in result.stdout + result.stderr


def test_normalizer_detects_changes_without_changing_original_bytes_modes_or_index(
    workspace: Workspace,
) -> None:
    """Detect normalization changes while preserving source and cache state."""
    target = workspace.write("sample.txt", "value  \n")
    target.chmod(0o755)
    repo = _cache_remote(workspace, "trailing-whitespace", "trailing-whitespace-fixer")
    repo["hooks"][0]["files"] = "^sample.txt$"
    workspace.config([repo])
    workspace.stage()
    before = _source_state(workspace.root)
    cache_before = _source_state(workspace.cache)
    result = workspace.run()
    assert result.returncode == 1
    assert "trailing-whitespace" in result.stdout + result.stderr
    assert workspace.events()[0]["cwd"] != str(workspace.root)
    assert _source_state(workspace.root) == before
    assert _source_state(workspace.cache) == cache_before


def test_markdown_config_can_force_fix_but_original_source_stays_unchanged(
    workspace: Workspace,
) -> None:
    """Reject a configuration-driven fix even when the tool returns success."""
    workspace.write("README.md", "# Fixture  \n")
    workspace.write(".markdownlint.yaml", "default: true\n")
    workspace.write(".markdownlint-cli2.jsonc", '{"fix": true}\n')
    repo = _cache_remote(workspace, "markdownlint-cli2", "markdownlint-cli2", markdown=True)
    repo["hooks"][0].update(files=r"\.md$", args=["--config", ".markdownlint.yaml", "--fix"])
    workspace.config([repo])
    workspace.stage()
    before = _source_state(workspace.root)
    result = workspace.run()
    assert result.returncode == 1
    assert "markdownlint-cli2" in result.stdout + result.stderr
    assert workspace.events()[0]["cwd"] != str(workspace.root)
    assert _source_state(workspace.root) == before


def test_private_policy_and_candidate_git_state_reach_whole_tree_validator(
    workspace: Workspace,
) -> None:
    """Keep private policy input and staged source visible to Git-aware checks."""
    workspace.write(".heph-private-denylist", "fixture-policy-value\n")
    workspace.write("pending.txt", "candidate content\n")
    workspace.write(
        "scripts/check_private_denylist.py",
        (
            "from pathlib import Path\nimport subprocess\n"
            "assert Path('.heph-private-denylist').read_text() == 'fixture-policy-value\\n'\n"
            "assert subprocess.check_output(['git', 'show', ':pending.txt']) "
            "== b'candidate content\\n'\n"
            "assert subprocess.check_output(['git', 'rev-parse', 'HEAD']).strip()\n"
        ),
    )
    workspace.config(
        [
            {
                "repo": "local",
                "hooks": [
                    _local(
                        "check-private-denylist",
                        "python3 scripts/check_private_denylist.py --staged --tracked",
                        pass_filenames=False,
                        always_run=True,
                    )
                ],
            }
        ]
    )
    workspace.stage()
    before = _source_state(workspace.root)
    result = workspace.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert _source_state(workspace.root) == before


@pytest.mark.parametrize("missing", ["cache", "tool"])
def test_missing_prepared_dependency_fails_without_setup(
    workspace: Workspace, missing: str
) -> None:
    """Report missing prepared tools without changing source or cache state."""
    workspace.write("sample.txt", "value\n")
    if missing == "cache":
        repo = {"repo": HOOK_REPO, "rev": HOOK_REV, "hooks": [{"id": "trailing-whitespace"}]}
    else:
        repo = _cache_remote(workspace, "trailing-whitespace", "trailing-whitespace-fixer")
        (workspace.tools / "trailing-whitespace-fixer").unlink()
    workspace.config([repo])
    workspace.stage()
    before = _source_state(workspace.root)
    cache_before = _source_state(workspace.cache)
    result = workspace.run()
    assert result.returncode != 0
    assert "preparation" in (result.stdout + result.stderr).lower()
    missing_input = HOOK_REPO if missing == "cache" else "trailing-whitespace-fixer"
    assert missing_input in result.stderr
    assert workspace.events() == []
    assert _source_state(workspace.root) == before
    assert _source_state(workspace.cache) == cache_before


def test_unknown_hook_fails_before_any_hook_runs(workspace: Workspace) -> None:
    """Reject an unreviewed hook before executing the selected checks."""
    workspace.write("sample.py", "VALUE = 1\n")
    workspace.config(
        [
            {
                "repo": "local",
                "hooks": [
                    _local("ruff-format-python", "uv run ruff format", types=["python"]),
                    _local("unreviewed-new-hook", "uv run new-command", always_run=True),
                ],
            }
        ]
    )
    workspace.stage()
    result = workspace.run()
    assert result.returncode != 0
    assert "unreviewed-new-hook" in result.stdout + result.stderr
    assert workspace.events() == []


def test_changed_formatter_execution_contract_fails_before_execution(workspace: Workspace) -> None:
    """Reject a formatter command whose execution contract has changed."""
    workspace.write("sample.py", "VALUE = 1\n")
    workspace.config(
        [
            {
                "repo": "local",
                "hooks": [
                    _local("ruff-check-python", "uv run ruff check --fix-only", types=["python"]),
                ],
            }
        ]
    )
    workspace.stage()
    result = workspace.run()
    assert result.returncode != 0
    assert "ruff-check-python" in result.stdout + result.stderr
    assert workspace.events() == []


def test_later_hook_sees_original_candidate_after_normalizer_failure(workspace: Workspace) -> None:
    """Restore candidate input before the next configured check runs."""
    workspace.write("sample.txt", "value  \n")
    repo = _cache_remote(workspace, "trailing-whitespace", "trailing-whitespace-fixer")
    repo["hooks"][0]["files"] = "^sample.txt$"
    workspace.config(
        [
            repo,
            {
                "repo": "local",
                "hooks": [
                    {
                        "id": "forbid-or-true",
                        "name": "candidate whitespace rule",
                        "language": "pygrep",
                        "entry": "value  ",
                        "files": "^sample.txt$",
                    }
                ],
            },
        ]
    )
    workspace.stage()
    result = workspace.run()
    assert result.returncode == 1
    assert "trailing-whitespace" in result.stdout + result.stderr
    assert "sample.txt:1:value  " in result.stdout + result.stderr


def test_external_symlink_cannot_expose_original_source_to_a_mutating_tool(
    workspace: Workspace, tmp_path: Path
) -> None:
    """Reject links that could expose an external file to a normalizer."""
    outside = tmp_path / "outside.txt"
    outside.write_text("outside  \n")
    (workspace.root / "alias.txt").symlink_to(outside)
    repo = _cache_remote(workspace, "trailing-whitespace", "trailing-whitespace-fixer")
    workspace.config([repo])
    workspace.stage()
    result = workspace.run()
    assert result.returncode != 0
    assert "symlink" in (result.stdout + result.stderr).lower()
    assert outside.read_bytes() == b"outside  \n"
    assert workspace.events() == []

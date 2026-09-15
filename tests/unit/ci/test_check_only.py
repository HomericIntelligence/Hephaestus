"""Test the check-only command through isolated process boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
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
environment_names = (
    "AWS_SECRET_ACCESS_KEY",
    "GH_TOKEN",
    "PATH",
    "PRE_COMMIT_NO_CONCURRENCY",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONPATH",
    "SSH_AUTH_SOCK",
    "UV_NO_SYNC",
    "UV_OFFLINE",
    "UV_PROJECT_ENVIRONMENT",
)
tool_root = Path(sys.argv[0]).parent
with open(tool_root / ".events.jsonl", "a") as stream:
    stream.write(json.dumps({
        "name": name,
        "args": args,
        "cwd": os.getcwd(),
        "environment": {key: os.environ.get(key) for key in environment_names},
    }) + "\n")
failure_marker = tool_root / ".fail"
if failure_marker.is_file() and failure_marker.read_text() == name:
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
        )
        env.update(overrides)
        return env

    def run(
        self, *, catalog: dict[str, str] | None = None, **overrides: str
    ) -> subprocess.CompletedProcess[str]:
        """Run the public command and distinguish an absent CLI from a failure."""
        failure_marker = self.tools / ".fail"
        failure_marker.unlink(missing_ok=True)
        failure_name = overrides.pop("CHECK_ONLY_TOOL_FAIL", None)
        if failure_name is not None:
            failure_marker.write_text(failure_name)
        command = [sys.executable, "-B", "-m", "hephaestus.ci.check_only"]
        if catalog is not None:
            command = [
                sys.executable,
                "-B",
                "-c",
                "from hephaestus.cli.localization import using_localizer\n"
                "from hephaestus.ci.check_only import main\n"
                f"with using_localizer({catalog!r}):\n"
                "    raise SystemExit(main())\n",
            ]
        result = subprocess.run(
            command,
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
        root,
        tmp_path / "tools",
        tmp_path / "cache",
        tmp_path / "tools" / ".events.jsonl",
        tmp_path / "scratch",
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


def _configured_local_hook(hook_id: str) -> dict[str, Any]:
    """Return one local hook from the repository configuration."""
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text())
    for repository in config["repos"]:
        if repository["repo"] != "local":
            continue
        for hook in repository["hooks"]:
            if hook["id"] == hook_id:
                return dict(hook)
    raise AssertionError(f"Local hook is absent: {hook_id}")


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


def test_pygrep_uses_the_bound_expression(workspace: Workspace) -> None:
    """Run the reviewed expression against its selected files."""
    workspace.write("scripts/checked.sh", "echo acceptable\n")
    workspace.config(
        [
            {
                "repo": "local",
                "hooks": [_configured_local_hook("forbid-or-true")],
            }
        ]
    )
    workspace.stage()
    assert workspace.run().returncode == 0
    workspace.write("scripts/checked.sh", "command || true\n")
    result = workspace.run()
    assert result.returncode == 1
    assert "scripts/checked.sh" in result.stdout + result.stderr


@pytest.mark.parametrize(
    "hook_id",
    [
        "forbid-or-true",
        "forbid-continue-on-error",
        "forbid-advisory-warnings",
        "forbid-unwhitelisted-add-to-bashrc",
    ],
)
def test_pygrep_accepts_the_bound_contract(workspace: Workspace, hook_id: str) -> None:
    """Accept each reviewed policy hook without a contract change."""
    workspace.config([{"repo": "local", "hooks": [_configured_local_hook(hook_id)]}])
    workspace.stage()
    result = workspace.run()
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "hook_id",
    [
        "forbid-or-true",
        "forbid-continue-on-error",
        "forbid-advisory-warnings",
        "forbid-unwhitelisted-add-to-bashrc",
    ],
)
def test_pygrep_rejects_a_changed_expression(workspace: Workspace, hook_id: str) -> None:
    """Reject a known policy ID with a different expression."""
    hook = _configured_local_hook(hook_id)
    hook["entry"] = "candidate-weakened-expression"
    workspace.config([{"repo": "local", "hooks": [hook]}])
    workspace.stage()
    result = workspace.run()
    assert result.returncode == 2
    assert hook_id in result.stderr
    assert workspace.events() == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("args", ["--multiline"]),
        ("files", "^absent$"),
        ("exclude", ".*"),
        ("types", ["python"]),
        ("types_or", ["python"]),
        ("exclude_types", ["shell"]),
        ("always_run", True),
        ("pass_filenames", False),
        ("stages", ["manual"]),
    ],
)
def test_pygrep_rejects_changed_selection_or_arguments(
    workspace: Workspace, field: str, value: object
) -> None:
    """Reject a policy hook whose execution selection changed."""
    hook = _configured_local_hook("forbid-or-true")
    hook[field] = value
    workspace.config([{"repo": "local", "hooks": [hook]}])
    workspace.stage()
    result = workspace.run()
    assert result.returncode == 2
    assert "forbid-or-true" in result.stderr
    assert workspace.events() == []


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


@pytest.mark.parametrize("revision", ["main", "v6.0.0", "3e8a870"])
def test_symbolic_remote_revision_fails_with_a_prepared_cache(
    workspace: Workspace, revision: str
) -> None:
    """Reject a cached remote hook revision that is not a full object ID."""
    workspace.write("sample.txt", "value\n")
    repository = _cache_remote(workspace, "trailing-whitespace", "trailing-whitespace-fixer")
    with sqlite3.connect(workspace.cache / "db.db") as connection:
        connection.execute("UPDATE repos SET ref = ?", (revision,))
    repository["rev"] = revision
    workspace.config([repository])
    workspace.stage()
    result = workspace.run()
    assert result.returncode == 2
    assert "immutable" in result.stderr.lower()
    assert revision in result.stderr
    assert workspace.events() == []


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


def test_later_hook_sees_original_candidate_after_normalizer_failure(
    workspace: Workspace,
) -> None:
    """Restore candidate input before the next configured check runs."""
    workspace.write("sample.sh", "command || true  \n")
    repo = _cache_remote(workspace, "trailing-whitespace", "trailing-whitespace-fixer")
    repo["hooks"][0]["files"] = "^sample.sh$"
    workspace.config(
        [
            repo,
            {
                "repo": "local",
                "hooks": [_configured_local_hook("forbid-or-true")],
            },
        ]
    )
    workspace.stage()
    result = workspace.run()
    assert result.returncode == 1
    assert "trailing-whitespace" in result.stdout + result.stderr
    assert "sample.sh:1:command || true  \n" in result.stdout + result.stderr


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


def test_candidate_hooks_exclude_secrets_and_keep_required_environment(
    workspace: Workspace, tmp_path: Path
) -> None:
    """Exclude parent secrets and keep the private check environment."""
    workspace.write("sample.py", "VALUE = 1\n")
    workspace.config(
        [
            {
                "repo": "local",
                "hooks": [_local("ruff-check-python", "uv run ruff check --fix")],
            }
        ]
    )
    workspace.stage()

    aws_credential = "-".join(("test", "only", "aws", "credential"))
    github_credential = "-".join(("test", "only", "github", "credential"))
    result = workspace.run(
        AWS_SECRET_ACCESS_KEY=aws_credential,
        GH_TOKEN=github_credential,
        SSH_AUTH_SOCK=str(tmp_path / "test-agent.sock"),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    environment = workspace.events()[0]["environment"]
    assert all(
        environment[name] is None for name in ("AWS_SECRET_ACCESS_KEY", "GH_TOKEN", "SSH_AUTH_SOCK")
    )
    assert environment["PATH"]
    assert Path(environment["PYTHONPATH"]).name == "candidate"
    assert environment["UV_PROJECT_ENVIRONMENT"] == sys.prefix
    assert {
        name: environment[name]
        for name in (
            "PRE_COMMIT_NO_CONCURRENCY",
            "PYTHONDONTWRITEBYTECODE",
            "UV_NO_SYNC",
            "UV_OFFLINE",
        )
    } == {
        "PRE_COMMIT_NO_CONCURRENCY": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "UV_NO_SYNC": "1",
        "UV_OFFLINE": "1",
    }


def test_git_children_keep_private_candidate_and_exclude_ambient_values(
    workspace: Workspace, tmp_path: Path
) -> None:
    """Use the supplied index/object store without forwarding unrelated input."""
    workspace.write("selected.py", "BEFORE = 1\n")
    workspace.config(
        [
            {
                "repo": "local",
                "hooks": [_local("ruff-check-python", "uv run ruff check --fix")],
            }
        ]
    )
    workspace.stage()
    workspace.git(
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
        "tracked selection baseline",
    )
    workspace.write(".gitignore", "build/\n.heph-private-denylist\nselected.py\n")
    workspace.git("rm", "--cached", "--", "selected.py")
    workspace.write("selected.py", "AFTER = 2\n")
    workspace.stage()
    assert b"selected.py" not in workspace.git(
        "ls-files", "--cached", "--others", "--exclude-standard"
    )

    private = tmp_path / "candidate-git"
    private.mkdir()
    (private / "objects").mkdir()
    admitted = {
        "GIT_INDEX_FILE": str(private / "index"),
        "GIT_OBJECT_DIRECTORY": str(private / "objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(workspace.root / ".git" / "objects"),
    }
    for arguments in (("read-tree", "HEAD"), ("add", "--all", "--", ".")):
        subprocess.run(
            ["git", *arguments],
            cwd=workspace.root,
            env=workspace.environment(**admitted),
            capture_output=True,
            check=True,
            timeout=10,
        )
    private_contents = subprocess.check_output(
        ["git", "show", ":selected.py"],
        cwd=workspace.root,
        env=workspace.environment(**admitted),
        timeout=10,
    )
    assert private_contents == b"AFTER = 2\n"

    real_git = shutil.which("git", path=os.defpath)
    assert real_git is not None
    git_log = tmp_path / "git-children.jsonl"
    git_log.write_text("")
    wrapper = workspace.tools / "git"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"names = {tuple(admitted)!r}\n"
        "if sys.argv[1:2] == ['-C']:\n"
        f"    with open({str(git_log)!r}, 'a') as stream:\n"
        "        stream.write(json.dumps({'root': sys.argv[2],\n"
        "            'candidate': {name: os.environ.get(name) for name in names},\n"
        "            'sentinel': 'CHECK_ONLY_AMBIENT_SENTINEL' in os.environ,\n"
        "            'credential': 'GH_TOKEN' in os.environ}) + '\\n')\n"
        f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n"
    )
    wrapper.chmod(0o755)
    original = _source_state(workspace.root)
    private_original = _source_state(private)
    result = workspace.run(
        GIT_INDEX_FILE=admitted["GIT_INDEX_FILE"],
        GIT_OBJECT_DIRECTORY=admitted["GIT_OBJECT_DIRECTORY"],
        GIT_ALTERNATE_OBJECT_DIRECTORIES=admitted["GIT_ALTERNATE_OBJECT_DIRECTORIES"],
        CHECK_ONLY_AMBIENT_SENTINEL="fixture-only-unrelated-value",
        GH_TOKEN=str(tmp_path / "credential-presence-sentinel"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ruff-check-python: passed" in result.stdout
    assert "selected.py" in workspace.events()[0]["args"]
    events = [json.loads(line) for line in git_log.read_text().splitlines()]
    source_calls = [event for event in events if event["root"] == str(workspace.root)]
    candidate_calls = [event for event in events if event["root"] != str(workspace.root)]
    assert source_calls and candidate_calls
    assert all(event["candidate"] == admitted for event in source_calls)
    assert all(
        all(value is None for value in event["candidate"].values()) for event in candidate_calls
    )
    assert all(not event["sentinel"] and not event["credential"] for event in events)
    assert _source_state(workspace.root) == original
    assert _source_state(private) == private_original


@pytest.mark.parametrize("outcome", ["passed", "failed", "changed", "preparation-error"])
def test_public_output_localizes_templates_without_translating_runtime_values(
    workspace: Workspace, outcome: str
) -> None:
    """Translate authored messages while retaining IDs, diagnostics, and exits."""
    workspace.write("sample.py", "VALUE = 1\n")
    hook_id = "ruff-check-python"
    overrides = {}
    if outcome == "changed":
        hook_id = "trailing-whitespace"
        workspace.write("sample.txt", "value  \n")
        workspace.config([_cache_remote(workspace, hook_id, "trailing-whitespace-fixer")])
    else:
        workspace.config([{"repo": "local", "hooks": [_local(hook_id, "uv run ruff check --fix")]}])
        if outcome == "failed":
            overrides["CHECK_ONLY_TOOL_FAIL"] = "uv"
        elif outcome == "preparation-error":
            (workspace.tools / "uv").unlink()
    workspace.stage()
    original = _source_state(workspace.root)
    error = "Hook executable is not prepared: ruff-check-python: uv"
    catalog = {
        "%(hook_id)s: %(status)s": "Kontrolle %(hook_id)s: %(status)s",
        "passed": "bestanden",
        "FAILED": "FEHLER",
        "The hook changed its private candidate; original source is unchanged.": (
            "Private Kopie geaendert; Original unveraendert."
        ),
        "Check-only preparation failed: %(error)s": "Vorbereitung fehlgeschlagen: %(error)s",
        hook_id: "INCORRECTLY_TRANSLATED_HOOK_ID",
        "fixture validator failure": "INCORRECTLY_TRANSLATED_TOOL_OUTPUT",
        error: "INCORRECTLY_TRANSLATED_RUNTIME_ERROR",
    }
    result = workspace.run(catalog=catalog, **overrides)
    expected_exit = {"passed": 0, "failed": 1, "changed": 1, "preparation-error": 2}
    assert result.returncode == expected_exit[outcome], result.stdout + result.stderr
    if outcome == "preparation-error":
        assert f"Vorbereitung fehlgeschlagen: {error}" in result.stderr
    else:
        status = "bestanden" if outcome == "passed" else "FEHLER"
        assert f"Kontrolle {hook_id}: {status}" in result.stdout
        if outcome == "failed":
            assert "fixture validator failure" in result.stdout
        elif outcome == "changed":
            assert "Private Kopie geaendert; Original unveraendert." in result.stdout
    assert "INCORRECTLY_TRANSLATED" not in result.stdout + result.stderr
    assert _source_state(workspace.root) == original

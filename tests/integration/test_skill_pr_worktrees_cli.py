#!/usr/bin/env python3
"""Subprocess-level integration tests for the skill pull-request worktree CLIs.

Both console entry points run in their own interpreter process through their
complete argument parsers. GitHub REST boundaries are a caller-trusted fake
``gh`` executable under ``--gh-extra-path-root``. Remote Git transport is a
PATH-shim ``git`` executable that rewrites canonical https GitHub URLs onto
local bare fixture repositories and records every push argument.

Outcome boundaries covered end to end:

- preparation success with a receipt on disk,
- argument errors (exit 2) with a JSON envelope,
- contract rejection (exit 1) through a missing REST view,
- fork-head publication with the exact lease the contract owns,
- temporary failure (exit 75) when a preserved head blocks publication,
- unknown publication (exit 76) when the post-push readback refuses,
- reconciliation (exit 0) of an earlier publication,
- operator interruption (exit 130) before any remote write.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

OWNER = "octo"
REPOSITORY = "example"
PR_NUMBER = 514


def _resolve_binary(command: str, *, required: bool) -> str:
    """Return the console-script path, skipping — or failing — when absent."""
    binary = shutil.which(command)
    if binary is None:
        message = f"{command} not on PATH — install with `pip install -e .` or run with uv"
        if required:
            pytest.fail(f"{message} (skip forbidden: --require-cli)")
        pytest.skip(message)
    return binary


def _rest_view(number: int, *, base_sha: str, head_sha: str, fork: bool) -> str:
    """Return one bounded pull-request REST row with only its contract fields."""
    return json.dumps(
        {
            "number": number,
            "state": "OPEN",
            "html_url": f"https://github.com/octo/example/pull/{number}",
            "base": {"ref": "main", "sha": base_sha, "repo": {"full_name": "octo/example"}},
            "head": {
                "ref": "pr-head",
                "sha": head_sha,
                "repo": {"full_name": "octo-fork/example" if fork else "octo/example"},
            },
        }
    )


def _census_row(number: int, *, base_sha: str, head_sha: str, fork: bool) -> str:
    """Return one census row with only the fields the sibling fetch owns."""
    return json.dumps(
        {
            "number": number,
            "state": "OPEN",
            "html_url": f"https://github.com/octo/example/pull/{number}",
            "base": {"ref": "main", "sha": base_sha, "repo": {"full_name": "octo/example"}},
            "head": {
                "ref": "pr-head",
                "sha": head_sha,
                "repo": {"full_name": "octo-fork/example" if fork else "octo/example"},
            },
        }
    )


def _git(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run one local fixture Git command with deterministic identity."""
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        },
    )


def _init_fixture_bare(directory: Path) -> tuple[str, str]:
    """Create one bare fixture repository with real commits.

    Returns:
        The ``(base_sha, head_sha)`` tips of ``main`` and ``pr-head``.

    """
    directory.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "-q", str(directory)], check=True)
    seed = directory.parent / f"{directory.name}-seed"
    seed.mkdir()
    _git(seed.parent, "clone", "-q", str(directory), str(seed))
    (seed / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(seed, "add", "seed.txt")
    _git(seed, "commit", "-q", "-m", "seed commit")
    _git(seed, "branch", "-q", "-M", "main")
    _git(seed, "push", "-q", str(directory), "main")
    base_sha = _git(seed, "rev-parse", "main").stdout.strip()
    _git(seed, "checkout", "-q", "-b", "pr-head")
    (seed / "head.txt").write_text("head\n", encoding="utf-8")
    _git(seed, "add", "head.txt")
    _git(seed, "commit", "-q", "-m", "head commit")
    _git(seed, "push", "-q", str(directory), "pr-head")
    head_sha = _git(seed, "rev-parse", "pr-head").stdout.strip()
    shutil.rmtree(seed)
    return base_sha, head_sha


class _World:
    """One hermetic fixture world with fake REST views and URL-shimmed Git."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.remotes = root / "remotes"
        self.tools = root / "tools"
        self.shim = root / "shim"
        self.fixtures = root / "fixtures"
        self.flags = root / "flags"
        self.push_log = root / "push.log"
        self.pushed_marker = root / "pushed.marker"
        for directory in (self.remotes, self.tools / "bin", self.shim, self.fixtures):
            directory.mkdir(parents=True)
        self.push_log.touch()

    def set_flag(self, name: str) -> None:
        """Enable one runtime flag for the fixture executables."""
        lines = [
            line
            for line in self.flags.read_text(encoding="utf-8").splitlines()
            if line and line != name
        ]
        lines.append(name)
        self.flags.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def clear_flags(self) -> None:
        """Disable every runtime flag."""
        self.flags.write_text("", encoding="utf-8")

    def init_remote(self, full_name: str) -> tuple[str, str]:
        """Create one bare fixture repository named like the shim maps it.

        Returns:
            The ``(base_sha, head_sha)`` tips of ``main`` and ``pr-head``.

        """
        directory = self.remotes / (full_name.replace("/", "-") + ".git")
        return _init_fixture_bare(directory)

    def write_rest_view(self, number: int, *, base_sha: str, head_sha: str, fork: bool) -> None:
        """Write the bounded pull-request REST view fixture."""
        key = self._key(f"repos/{OWNER}/{REPOSITORY}/pulls/{number}")
        (self.fixtures / key).write_text(
            _rest_view(number, base_sha=base_sha, head_sha=head_sha, fork=fork),
            encoding="utf-8",
        )

    def write_census(self, rows: list[str]) -> None:
        """Write the first-page census fixture."""
        census = "[" + ",".join(rows) + "]"
        key = self._key(f"repos/{OWNER}/{REPOSITORY}/pulls?state=open&per_page=100&page=1")
        (self.fixtures / key).write_text(census, encoding="utf-8")

    def write_tools(self) -> None:
        """Write the caller-trusted fake gh executable with baked fixture paths."""
        gh = self.tools / "bin" / "gh"
        gh.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "auth" ]; then exit 0; fi\n'
            'if [ "$1" = "api" ]; then\n'
            "  key=\"$(printf '%s' \"$2\" | tr -c 'A-Za-z0-9' '_')\"\n"
            f'  if grep -q "SIGINT-{PR_NUMBER}$" "{self.flags}" 2>/dev/null && '
            'printf \'%s\' "$2" | grep -q "pulls/' + str(PR_NUMBER) + '$"; then\n'
            '    kill -INT "$PPID"\n'
            "    sleep 0.3\n"
            "  fi\n"
            f'  if [ ! -f "{self.fixtures}/$key" ]; then\n'
            '    echo "gh: Not Found (HTTP 404)" >&2\n'
            "    exit 1\n"
            "  fi\n"
            f'  exec cat "{self.fixtures}/$key"\n'
            "fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)

    def write_git_shim(self) -> None:
        """Write the PATH git shim that rewrites canonical GitHub URLs."""
        real_git = shutil.which("git")
        assert real_git is not None, "git must be on PATH"
        shim = self.shim / "git"
        shim.write_text(
            "#!/bin/bash\n"
            'subcommand=""\n'
            'prev=""\n'
            'for arg in "$@"; do\n'
            '  case "$prev" in\n'
            '    -c|-C|--config-env|--exec-path) prev=""; continue ;;\n'
            "  esac\n"
            '  case "$arg" in\n'
            '    -c|-C|--config-env|--exec-path) prev="$arg" ;;\n'
            '    -*) prev="" ;;\n'
            '    *) subcommand="$arg"; break ;;\n'
            "  esac\n"
            "done\n"
            f'if [ "$subcommand" = "push" ]; then printf \'%s\\n\' "$*" >> "{self.push_log}"; fi\n'
            'if grep -q "^FAIL-PUSH$" "'
            + str(self.flags)
            + '" 2>/dev/null && [ "$subcommand" = "push" ]; then\n'
            '  echo "push refused by fixture" >&2\n'
            "  exit 1\n"
            "fi\n"
            'if grep -q "^FAIL-LS-REMOTE$" "'
            + str(self.flags)
            + '" 2>/dev/null && [ "$subcommand" = "ls-remote" ] && '
            f'[ -f "{self.pushed_marker}" ]; then\n'
            '  echo "readback refused by fixture" >&2\n'
            "  exit 1\n"
            "fi\n"
            f'if [ "$subcommand" = "push" ]; then : > "{self.pushed_marker}"; fi\n'
            "args=()\n"
            'for arg in "$@"; do\n'
            '  case "$arg" in\n'
            "    https://github.com/*.git)\n"
            "      rest=${arg#https://github.com/}\n"
            '      name="${rest//\\//-}"\n'
            f'      args+=("{self.remotes}/$name")\n'
            "      ;;\n"
            "    *)\n"
            '      args+=("$arg")\n'
            "      ;;\n"
            "  esac\n"
            "done\n"
            f"exec '{real_git}' \"${{args[@]}}\"\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)

    @property
    def checkout(self) -> Path:
        """The source checkout path the fixture world owns."""
        return self.root / "checkout"

    @property
    def state_root(self) -> Path:
        """The state root prepare creates below the checkout's build directory."""
        return self.checkout / "build" / "pr-repairs"

    def base_checkout(self, name: str = "checkout") -> Path:
        """Create a source checkout bound to the canonical base origin URL."""
        checkout = self.root / name
        _git(self.root, "clone", "-q", str(self.remotes / "octo-example.git"), str(checkout))
        _git(
            checkout,
            "remote",
            "set-url",
            "origin",
            f"https://github.com/{OWNER}/{REPOSITORY}.git",
        )
        return checkout

    def environment(self) -> dict[str, str]:
        """Return the fixture environment with the shim first on PATH."""
        env = os.environ.copy()
        env["PATH"] = f"{self.shim}{os.pathsep}{env['PATH']}"
        return env

    @staticmethod
    def _key(path: str) -> str:
        """Return the fixture filename for one REST request path."""
        return "".join(character if character.isalnum() else "_" for character in path)


@pytest.fixture()
def world(tmp_path: Path) -> _World:
    """Provide one fixture world with tools and the Git shim installed."""
    fixture_world = _World(tmp_path / "world")
    fixture_world.write_tools()
    fixture_world.write_git_shim()
    fixture_world.clear_flags()
    return fixture_world


def _run(
    command: str,
    arguments: list[str],
    *,
    env: dict[str, str],
    require_cli: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run one console entry point in its own process."""
    binary = _resolve_binary(command, required=require_cli)
    return subprocess.run(
        [binary, *arguments],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def _json_outcome(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """Parse the single JSON envelope line from stdout."""
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected one envelope line, got: {result.stdout!r}"
    envelope: dict[str, Any] = json.loads(lines[0])
    return envelope


def _prepare_arguments(
    world: _World,
    checkout: Path,
    *,
    extra: list[str] | None = None,
    json_mode: bool = True,
) -> list[str]:
    """Return the complete preparation argument vector."""
    arguments = [
        "514",
        "--target-host",
        "github.com",
        "--target-repository",
        "octo/example",
        "--repository-root",
        str(checkout),
        "--state-root",
        str(checkout / "build" / "pr-repairs"),
        "--worktree-root",
        str(checkout / "build" / "pr-worktrees"),
        "--worktree",
        str(checkout / "build" / "pr-worktrees" / "pr-514"),
        "--branch",
        "hephaestus-repair-pr-514",
        "--mode",
        "in-place",
        "--gh-extra-path-root",
        str(world.tools),
    ]
    if json_mode:
        arguments.append("--json")
    return arguments + (extra or [])


def _publish_arguments(
    world: _World,
    checkout: Path,
    receipt: Path,
    *,
    json_mode: bool = True,
) -> list[str]:
    """Return the complete publication argument vector.

    The receipt owns the worktree, namespace, branch, and expected head; the
    publication grammar has no caller-supplied repository root.
    """
    del checkout  # publish must not trust a caller-supplied root
    arguments = [
        "514",
        "--target-host",
        "github.com",
        "--target-repository",
        "octo/example",
        "--state-root",
        str(world.state_root),
        "--receipt",
        str(receipt),
        "--gh-extra-path-root",
        str(world.tools),
    ]
    if json_mode:
        arguments.append("--json")
    return arguments


class TestPreparation:
    """End-to-end preparation through the console entry point."""

    def test_prepared_envelope_and_receipt(self, world: _World, tmp_path: Path) -> None:
        """A same-repository head prepares a worktree with a receipt."""
        base_sha, head_sha = world.init_remote("octo/example")
        world.write_rest_view(PR_NUMBER, base_sha=base_sha, head_sha=head_sha, fork=False)
        world.write_census(
            [_census_row(PR_NUMBER, base_sha=base_sha, head_sha=head_sha, fork=False)]
        )
        checkout = world.base_checkout()

        result = _run(
            "hephaestus-prepare-pr-worktree",
            _prepare_arguments(world, checkout),
            env=world.environment(),
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert result.stderr == ""
        envelope = _json_outcome(result)
        assert envelope["outcome"] == "prepared"
        receipt = Path(envelope["receipt"])
        assert receipt.is_file()
        assert Path(envelope["worktree"]).is_dir()

    def test_argument_error_envelope(self, world: _World, tmp_path: Path) -> None:
        """Missing required arguments exit 2 with a JSON envelope."""
        result = _run(
            "hephaestus-prepare-pr-worktree",
            ["514", "--json"],
            env=world.environment(),
        )
        assert result.returncode == 2, (result.stdout, result.stderr)
        assert result.stderr == ""
        envelope = _json_outcome(result)
        assert envelope["outcome"] == "argument-error"
        assert "required" in envelope["message"]

    def test_missing_rest_view_is_rejected(self, world: _World, tmp_path: Path) -> None:
        """A pull request without a REST view exits 1 with a human message."""
        world.init_remote("octo/example")
        checkout = world.base_checkout()

        result = _run(
            "hephaestus-prepare-pr-worktree",
            _prepare_arguments(world, checkout),
            env=world.environment(),
        )
        assert result.returncode == 1, (result.stdout, result.stderr)
        assert result.stderr == ""
        envelope = _json_outcome(result)
        assert envelope["outcome"] == "rejected"
        assert "GitHub request failed" in envelope["message"]

    def test_interrupted_preparation_exits_130(self, world: _World, tmp_path: Path) -> None:
        """An operator stop before any write exits 130 with the envelope."""
        base_sha, head_sha = world.init_remote("octo/example")
        world.write_rest_view(PR_NUMBER, base_sha=base_sha, head_sha=head_sha, fork=False)
        world.write_census(
            [_census_row(PR_NUMBER, base_sha=base_sha, head_sha=head_sha, fork=False)]
        )
        checkout = world.base_checkout()
        world.set_flag(f"SIGINT-{PR_NUMBER}")

        result = _run(
            "hephaestus-prepare-pr-worktree",
            _prepare_arguments(world, checkout),
            env=world.environment(),
        )
        assert result.returncode == 130, (result.stdout, result.stderr)
        envelope = _json_outcome(result)
        assert envelope["outcome"] == "interrupted"
        assert "before any remote write" in envelope["message"]


class TestPublication:
    """End-to-end publication through the console entry point."""

    def _prepared(self, world: _World, *, fork: bool) -> tuple[Path, Path, str, str]:
        """Prepare once and return the checkout, receipt, head tip, and candidate."""
        if fork:
            base_sha, _ = world.init_remote("octo/example")
            _, head_sha = world.init_remote("octo-fork/example")
        else:
            base_sha, head_sha = world.init_remote("octo/example")
        world.write_rest_view(PR_NUMBER, base_sha=base_sha, head_sha=head_sha, fork=fork)
        world.write_census(
            [_census_row(PR_NUMBER, base_sha=base_sha, head_sha=head_sha, fork=fork)]
        )
        checkout = world.base_checkout()
        result = _run(
            "hephaestus-prepare-pr-worktree",
            _prepare_arguments(world, checkout),
            env=world.environment(),
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        envelope = _json_outcome(result)
        receipt = Path(envelope["receipt"])
        worktree = Path(envelope["worktree"])
        (worktree / "candidate.txt").write_text("candidate\n", encoding="utf-8")
        _git(worktree, "add", "candidate.txt")
        _git(worktree, "commit", "-q", "-m", "candidate commit")
        candidate = _git(worktree, "rev-parse", "HEAD").stdout.strip()
        return checkout, receipt, head_sha, candidate

    def test_fork_head_published_with_exact_lease(self, world: _World, tmp_path: Path) -> None:
        """A fork-owned head publishes through its canonical fork URL."""
        checkout, receipt, head_sha, candidate = self._prepared(world, fork=True)

        result = _run(
            "hephaestus-publish-pr-worktree",
            _publish_arguments(world, checkout, receipt),
            env=world.environment(),
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert result.stderr == ""
        envelope = _json_outcome(result)
        assert envelope["outcome"] == "published"
        assert envelope["remote_head"] == candidate

        pushes = world.push_log.read_text(encoding="utf-8").splitlines()
        assert len(pushes) == 1, pushes
        assert "https://github.com/octo-fork/example.git" in pushes[0]
        assert f"--force-with-lease=refs/heads/pr-head:{head_sha}" in pushes[0]
        assert f"{candidate}:refs/heads/pr-head" in pushes[0]

        remote_head = _git(
            Path(world.remotes / "octo-fork-example.git"), "rev-parse", "pr-head"
        ).stdout.strip()
        assert remote_head == candidate

    def test_preserved_head_is_temporary_failure(self, world: _World, tmp_path: Path) -> None:
        """A preserved remote head exits 75 without moving it."""
        checkout, receipt, head_sha, _ = self._prepared(world, fork=False)
        world.set_flag("FAIL-PUSH")

        result = _run(
            "hephaestus-publish-pr-worktree",
            _publish_arguments(world, checkout, receipt),
            env=world.environment(),
        )
        assert result.returncode == 75, (result.stdout, result.stderr)
        envelope = _json_outcome(result)
        assert envelope["outcome"] == "temporary-failure"
        assert envelope["remote_head"] == head_sha
        assert world.push_log.read_text(encoding="utf-8") != ""

        remote_head = _git(
            Path(world.remotes / "octo-example.git"), "rev-parse", "pr-head"
        ).stdout.strip()
        assert remote_head == head_sha

    def test_refused_readback_is_unknown_then_reconciled(
        self, world: _World, tmp_path: Path
    ) -> None:
        """A refused post-push readback exits 76 and reconciles on the retry."""
        checkout, receipt, _, candidate = self._prepared(world, fork=False)
        world.set_flag("FAIL-LS-REMOTE")

        first = _run(
            "hephaestus-publish-pr-worktree",
            _publish_arguments(world, checkout, receipt),
            env=world.environment(),
        )
        assert first.returncode == 76, (first.stdout, first.stderr)
        envelope = _json_outcome(first)
        assert envelope["outcome"] == "unknown"
        assert envelope["remote_head"] is None

        remote_head = _git(
            Path(world.remotes / "octo-example.git"), "rev-parse", "pr-head"
        ).stdout.strip()
        assert remote_head == candidate, "the push must have completed"

        world.clear_flags()
        second = _run(
            "hephaestus-publish-pr-worktree",
            _publish_arguments(world, checkout, receipt),
            env=world.environment(),
        )
        assert second.returncode == 0, (second.stdout, second.stderr)
        reconciled = _json_outcome(second)
        assert reconciled["outcome"] == "reconciled"
        assert reconciled["remote_head"] == candidate

    def test_argument_error_envelope(self, world: _World, tmp_path: Path) -> None:
        """Missing required publication arguments exit 2 with the envelope."""
        result = _run(
            "hephaestus-publish-pr-worktree",
            ["--json"],
            env=world.environment(),
        )
        assert result.returncode == 2, (result.stdout, result.stderr)
        assert result.stderr == ""
        envelope = _json_outcome(result)
        assert envelope["outcome"] == "argument-error"
        assert "required" in envelope["message"]

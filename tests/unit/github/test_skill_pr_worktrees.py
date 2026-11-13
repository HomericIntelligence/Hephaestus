"""Behavior tests for pull-request-bound worktree preparation and publication.

The tests drive the complete prepare and publish contracts with real Git
repositories and controlled GitHub boundaries. The controlled remote executor
maps each canonical GitHub URL to a local bare repository, so fetch, exact
remote-head reads, and lease pushes run through real Git while the GitHub
REST reads are controlled fixtures.
"""

# ruff: noqa: D103
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import hephaestus.github.skill_pr_worktrees as module
from hephaestus.github.skill_pr_worktrees import (
    prepare_pr_worktree_main,
    publish_pr_worktree_main,
)

_BASE_REPOSITORY = "octo/example"
_BASE_URL = f"https://github.com/{_BASE_REPOSITORY}.git"


# ---------------------------------------------------------------------------
# Real Git helpers and fixtures
# ---------------------------------------------------------------------------


def _git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _commit_file(repository: Path, name: str, content: str, message: str) -> str:
    (repository / name).write_text(content, encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "--quiet", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _move_remote_branch(remote: Path, branch: str, oid: str) -> None:
    _git(remote, "update-ref", f"refs/heads/{branch}", oid)


class RemoteWorld:
    """One bare remote repository with a base and independent PR branches."""

    def __init__(self, tmp_path: Path) -> None:
        self.bare = tmp_path / "remotes" / "octo-example.git"
        self.bare.parent.mkdir(parents=True)
        seed = tmp_path / "seed"
        seed.mkdir()
        _git(seed, "init", "--quiet", "--initial-branch=main")
        _git(seed, "config", "user.email", "tests@example.invalid")
        _git(seed, "config", "user.name", "Hephaestus Tests")
        self.base_sha = _commit_file(seed, "base.txt", "base\n", "test: base")
        _git(seed, "clone", "--quiet", "--bare", str(seed), str(self.bare))
        self._scratch = tmp_path / "scratch"

    def branch_from(self, name: str, parent: str, count: int, prefix: str) -> str:
        """Advance one remote branch with ``count`` commits and return its tip."""
        work = self._scratch / name.replace("/", "-")
        work.parent.mkdir(parents=True, exist_ok=True)
        _git(work.parent, "clone", "--quiet", "--no-local", str(self.bare), str(work))
        _git(work, "config", "user.email", "tests@example.invalid")
        _git(work, "config", "user.name", "Hephaestus Tests")
        _git(work, "checkout", "--quiet", "-B", name, parent)
        for index in range(count):
            _commit_file(
                work, f"{prefix}-{index}.txt", f"{prefix} {index}\n", f"test: {prefix} {index}"
            )
        _git(work, "push", "--quiet", "origin", f"{name}:{name}")
        return _git(work, "rev-parse", name)


# ---------------------------------------------------------------------------
# Controlled GitHub boundaries
# ---------------------------------------------------------------------------


class ControlledRemote:
    """Map canonical GitHub URLs to local bare repositories and run real Git."""

    def __init__(self, mapping: dict[str, Path]) -> None:
        self.mapping = mapping
        self.calls: list[list[str]] = []
        self.pushes: list[list[str]] = []
        self.fail_push = False
        self.fail_readback = False

    def __call__(
        self,
        session: object,
        cwd: Path,
        arguments: list[str],
        *,
        pre_launch: Any = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(arguments))
        if pre_launch is not None:
            pre_launch()
        mapped = [self.mapping.get(argument, argument) for argument in arguments]
        if mapped[0] == "push":
            self.pushes.append(list(arguments))
            if self.fail_push:
                return subprocess.CompletedProcess(arguments, 1, "", "push rejected by fixture")
        result = subprocess.run(
            ["git", *mapped], cwd=cwd, capture_output=True, text=True, check=False
        )
        # Readback refusal models a remote that cannot answer after a possible
        # write; pre-push head reads stay servable so the lease path is reached.
        if mapped[0] == "ls-remote" and self.fail_readback and self.pushes:
            result = subprocess.CompletedProcess(arguments, 1, "", "readback refused")
        return subprocess.CompletedProcess(
            arguments, result.returncode, result.stdout, result.stderr
        )

    def pushes_to(self, repository_url: str) -> list[list[str]]:
        return [push for push in self.pushes if repository_url in push]

    def remote_head(self, repository_url: str, branch: str) -> str | None:
        result = subprocess.run(
            ["git", "rev-parse", f"refs/heads/{branch}"],
            cwd=self.mapping[repository_url],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None


class ControlledRest:
    """Serve controlled GitHub REST payloads keyed by their request URL."""

    def __init__(self) -> None:
        self.payloads: dict[str, Any] = {}
        self.calls: list[list[str]] = []

    def __call__(self, arguments: list[str], *, timeout_s: int, executable: str) -> str:
        self.calls.append(list(arguments))
        url = arguments[1]
        payload = self.payloads.get(url)
        if payload is None:
            raise AssertionError(f"unexpected GitHub request: {url}")
        return json.dumps(payload)

    def pr_reads(self, number: int) -> int:
        return sum(
            1 for call in self.calls if call[1] == f"repos/{_BASE_REPOSITORY}/pulls/{number}"
        )

    def replace_pull_request(self, view: dict[str, Any]) -> None:
        self.payloads[f"repos/{_BASE_REPOSITORY}/pulls/{view['number']}"] = view


def _rest_row(
    number: int,
    *,
    base_repository: str = _BASE_REPOSITORY,
    base_ref: str = "main",
    base_sha: str = "",
    head_repository: str = _BASE_REPOSITORY,
    head_ref: str = "feature",
    head_sha: str = "",
) -> dict[str, Any]:
    return {
        "number": number,
        "state": "OPEN",
        "html_url": f"https://github.com/{base_repository}/pull/{number}",
        "base": {"repo": {"full_name": base_repository}, "ref": base_ref, "sha": base_sha},
        "head": {"repo": {"full_name": head_repository}, "ref": head_ref, "sha": head_sha},
    }


# ---------------------------------------------------------------------------
# Environment fixture
# ---------------------------------------------------------------------------


class Environment:
    """One controlled prepare and publish environment."""

    def __init__(self, tmp_path: Path, world: RemoteWorld) -> None:
        self.tmp_path = tmp_path
        self.world = world
        self.rest = ControlledRest()
        self.executor = ControlledRemote({_BASE_URL: world.bare})
        self.repository_root = tmp_path / "source"
        _git(tmp_path, "clone", "--quiet", str(world.bare), str(self.repository_root))
        _git(self.repository_root, "remote", "set-url", "origin", _BASE_URL)
        _git(self.repository_root, "config", "user.email", "tests@example.invalid")
        _git(self.repository_root, "config", "user.name", "Hephaestus Tests")
        self.state_root = self.repository_root / "build" / "pr-repairs"
        self.worktree_root = self.repository_root / "build" / "pr-worktrees"

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(module, "_gh_api_read", self.rest)
        monkeypatch.setattr(module, "_remote_git_run", self.executor)
        monkeypatch.setattr(module, "trusted_gh_authenticated", lambda *_: True)

    def register_pull_request(self, view: dict[str, Any]) -> None:
        self.rest.payloads[f"repos/{_BASE_REPOSITORY}/pulls/{view['number']}"] = view

    def register_census(self, rows: list[dict[str, Any]]) -> None:
        self.rest.payloads[f"repos/{_BASE_REPOSITORY}/pulls?state=open&per_page=100&page=1"] = rows

    def prepare(
        self,
        capsys: pytest.CaptureFixture[str],
        *,
        number: int,
        mode: str = "in-place",
        branch: str | None = None,
        dependency: int | None = None,
        worktree: Path | None = None,
    ) -> tuple[int, dict[str, Any]]:
        arguments = [
            str(number),
            "--target-host",
            "github.com",
            "--target-repository",
            _BASE_REPOSITORY,
            "--repository-root",
            str(self.repository_root),
            "--state-root",
            str(self.state_root),
            "--worktree",
            str(worktree or self.worktree_root / f"pr-{number}"),
            "--worktree-root",
            str(self.worktree_root),
            "--branch",
            branch or f"hephaestus-repair-pr-{number}",
            "--mode",
            mode,
            "--json",
        ]
        if dependency is not None:
            arguments.extend(["--dependency-pr", str(dependency)])
        code = prepare_pr_worktree_main(arguments)
        return code, self._payload(capsys)

    def publish(
        self, capsys: pytest.CaptureFixture[str], *, number: int, receipt: str
    ) -> tuple[int, dict[str, Any]]:
        code = publish_pr_worktree_main(
            [
                str(number),
                "--target-host",
                "github.com",
                "--target-repository",
                _BASE_REPOSITORY,
                "--state-root",
                str(self.state_root),
                "--receipt",
                receipt,
                "--json",
            ]
        )
        return code, self._payload(capsys)

    @staticmethod
    def _payload(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
        out = capsys.readouterr()
        for stream in (out.out, out.err):
            text = stream.strip()
            if text.startswith("{"):
                return json.loads(text.splitlines()[0])
        return {"stdout": out.out, "stderr": out.err}

    def source_refs(self) -> list[str]:
        return _git(self.repository_root, "for-each-ref", "--format=%(refname)").splitlines()


@pytest.fixture()
def world(tmp_path: Path) -> RemoteWorld:
    return RemoteWorld(tmp_path)


# ---------------------------------------------------------------------------
# Preparation receipt binding
# ---------------------------------------------------------------------------


class TestPreparationReceipt:
    """The receipt binds every observed pull-request identity."""

    def test_receipt_binds_repository_number_base_head_start_and_mode(
        self, tmp_path: Path, world: RemoteWorld, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        parent_sha = world.branch_from("pr-766-head", world.base_sha, 2, "parent")
        head_sha = world.branch_from("pr-514-head", world.base_sha, 1, "repair")
        env = Environment(tmp_path, world)
        env.install(monkeypatch)
        env.register_pull_request(
            _rest_row(
                514,
                base_ref="main",
                base_sha=world.base_sha,
                head_ref="pr-514-head",
                head_sha=head_sha,
            )
        )
        env.register_census(
            [
                _rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha),
                _rest_row(
                    766, base_sha=world.base_sha, head_ref="pr-766-head", head_sha=parent_sha
                ),
            ]
        )

        code, payload = env.prepare(capsys, number=514)

        assert code == 0, payload
        assert payload["outcome"] == "prepared"
        receipt = json.loads(Path(payload["receipt"]).read_text(encoding="utf-8"))
        assert receipt["schema_version"] == 1
        assert receipt["state"] == "prepared"
        assert receipt["mode"] == "in-place"
        assert receipt["target"] == {
            "host": "github.com",
            "number": 514,
            "repository": _BASE_REPOSITORY,
            "url": f"https://github.com/{_BASE_REPOSITORY}/pull/514",
        }
        assert receipt["base"] == {
            "ref": "main",
            "repository": _BASE_REPOSITORY,
            "sha": world.base_sha,
        }
        assert receipt["head"] == {
            "ref": "pr-514-head",
            "repository": _BASE_REPOSITORY,
            "sha": head_sha,
        }
        assert receipt["start"] == {
            "object": head_sha,
            "ref": "pr-514-head",
            "repository": _BASE_REPOSITORY,
        }
        assert receipt["expected_remote_head"] == head_sha
        assert receipt["dependency"] is None
        assert set(receipt["census"]) == {"514", "766"}
        assert receipt["source"]["origin_fetch_url"] == _BASE_URL

    def test_receipt_digest_binds_the_file_name_and_rejects_tampering(
        self, tmp_path: Path, world: RemoteWorld, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        head_sha = world.branch_from("pr-514-head", world.base_sha, 1, "repair")
        env = Environment(tmp_path, world)
        env.install(monkeypatch)
        env.register_pull_request(
            _rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)
        )
        env.register_census(
            [_rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)]
        )
        code, payload = env.prepare(capsys, number=514)
        assert code == 0
        receipt_path = Path(payload["receipt"])
        document = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert document["receipt_digest"] == receipt_path.stem

        document["expected_remote_head"] = "0" * 40
        receipt_path.write_text(json.dumps(document, indent=2), encoding="utf-8")

        code, payload = env.publish(capsys, number=514, receipt=str(receipt_path))
        assert code == 1
        assert payload["outcome"] == "rejected"

    def test_clean_rebuild_starts_from_the_exact_base_tip(
        self, tmp_path: Path, world: RemoteWorld, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        parent_sha = world.branch_from("pr-766-head", world.base_sha, 1, "parent")
        head_sha = world.branch_from("pr-514-head", parent_sha, 1, "repair")
        env = Environment(tmp_path, world)
        env.install(monkeypatch)
        env.register_pull_request(
            _rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)
        )
        env.register_census(
            [_rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)]
        )

        code, payload = env.prepare(
            capsys, number=514, mode="clean-rebuild", branch="repair-rebuild"
        )
        assert code == 0
        receipt = json.loads(Path(payload["receipt"]).read_text(encoding="utf-8"))
        assert receipt["start"] == {
            "object": world.base_sha,
            "ref": "main",
            "repository": _BASE_REPOSITORY,
        }
        assert receipt["mode"] == "clean-rebuild"
        worktree = Path(payload["worktree"])
        assert _git(worktree, "rev-parse", "HEAD") == world.base_sha

    def test_stacked_starts_from_the_exact_dependency_head(
        self, tmp_path: Path, world: RemoteWorld, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        parent_sha = world.branch_from("pr-766-head", world.base_sha, 2, "parent")
        head_sha = world.branch_from("pr-999-head", parent_sha, 1, "stack")
        env = Environment(tmp_path, world)
        env.install(monkeypatch)
        env.register_pull_request(
            _rest_row(
                999,
                base_ref="pr-766-head",
                base_sha=parent_sha,
                head_ref="pr-999-head",
                head_sha=head_sha,
            )
        )
        env.register_pull_request(
            _rest_row(766, base_sha=world.base_sha, head_ref="pr-766-head", head_sha=parent_sha)
        )
        env.register_census(
            [
                _rest_row(
                    999,
                    base_ref="pr-766-head",
                    base_sha=parent_sha,
                    head_ref="pr-999-head",
                    head_sha=head_sha,
                ),
                _rest_row(
                    766, base_sha=world.base_sha, head_ref="pr-766-head", head_sha=parent_sha
                ),
            ]
        )

        code, payload = env.prepare(
            capsys, number=999, mode="stacked", dependency=766, branch="stacked-repair"
        )
        assert code == 0
        receipt = json.loads(Path(payload["receipt"]).read_text(encoding="utf-8"))
        assert receipt["dependency"]["number"] == 766
        assert receipt["start"]["object"] == parent_sha
        assert _git(Path(payload["worktree"]), "rev-parse", "HEAD") == parent_sha


# ---------------------------------------------------------------------------
# Namespace isolation and census validation
# ---------------------------------------------------------------------------


class TestNamespaceIsolation:
    """Preparation stays inside the receipt-owned namespace."""

    def test_preparation_does_not_change_source_refs(
        self, tmp_path: Path, world: RemoteWorld, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        head_sha = world.branch_from("pr-514-head", world.base_sha, 1, "repair")
        env = Environment(tmp_path, world)
        env.install(monkeypatch)
        env.register_pull_request(
            _rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)
        )
        env.register_census(
            [_rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)]
        )
        before = env.source_refs()

        code, payload = env.prepare(capsys, number=514)

        assert code == 0
        assert env.source_refs() == before
        namespace = Path(payload["namespace"])
        assert namespace.is_dir()
        assert _git(namespace, "rev-parse", "--is-bare-repository") == "true"

    def test_independent_pull_requests_get_distinct_namespaces_receipts_and_worktrees(
        self, tmp_path: Path, world: RemoteWorld, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        parent_sha = world.branch_from("pr-766-head", world.base_sha, 1, "parent")
        head_sha = world.branch_from("pr-514-head", world.base_sha, 1, "repair")
        env = Environment(tmp_path, world)
        env.install(monkeypatch)
        env.register_pull_request(
            _rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)
        )
        env.register_pull_request(
            _rest_row(766, base_sha=world.base_sha, head_ref="pr-766-head", head_sha=parent_sha)
        )
        env.register_census(
            [
                _rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha),
                _rest_row(
                    766, base_sha=world.base_sha, head_ref="pr-766-head", head_sha=parent_sha
                ),
            ]
        )

        code_514, payload_514 = env.prepare(capsys, number=514)
        code_766, payload_766 = env.prepare(capsys, number=766, branch="hephaestus-repair-pr-766")

        assert code_514 == code_766 == 0
        assert payload_514["namespace"] != payload_766["namespace"]
        assert payload_514["receipt"] != payload_766["receipt"]
        assert payload_514["worktree"] != payload_766["worktree"]

    def test_full_final_census_page_prevents_complete_traversal(
        self, tmp_path: Path, world: RemoteWorld, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        head_sha = world.branch_from("pr-514-head", world.base_sha, 1, "repair")
        env = Environment(tmp_path, world)
        env.install(monkeypatch)
        env.register_pull_request(
            _rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)
        )
        full_page = [
            _rest_row(
                number, base_sha=world.base_sha, head_ref=f"branch-{number}", head_sha="1" * 40
            )
            for number in range(1, 101)
        ]
        for page in range(1, 200):
            env.rest.payloads[
                f"repos/{_BASE_REPOSITORY}/pulls?state=open&per_page=100&page={page}"
            ] = full_page

        code, payload = env.prepare(capsys, number=514)

        assert code == 1
        assert payload["outcome"] == "rejected"
        assert "complete traversal" in payload["message"]

    def test_duplicate_census_rows_are_rejected(
        self, tmp_path: Path, world: RemoteWorld, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        head_sha = world.branch_from("pr-514-head", world.base_sha, 1, "repair")
        env = Environment(tmp_path, world)
        env.install(monkeypatch)
        env.register_pull_request(
            _rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)
        )
        row = _rest_row(700, base_sha=world.base_sha, head_ref="other", head_sha="2" * 40)
        env.register_census([row, row])

        code, payload = env.prepare(capsys, number=514)

        assert code == 1
        assert "duplicate" in payload["message"]


# ---------------------------------------------------------------------------
# Publication: modes, forks, and exact leases
# ---------------------------------------------------------------------------


class TestPublication:
    """Publication honors modes, forks, and exact leases."""

    def _prepared_in_place(
        self, env: Environment, capsys: pytest.CaptureFixture[str]
    ) -> dict[str, Any]:
        code, payload = env.prepare(capsys, number=514)
        assert code == 0
        return payload

    def test_in_place_repair_publishes_as_a_fast_forward(
        self, tmp_path: Path, world: RemoteWorld, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        head_sha = world.branch_from("pr-514-head", world.base_sha, 1, "repair")
        env = Environment(tmp_path, world)
        env.install(monkeypatch)
        env.register_pull_request(
            _rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)
        )
        env.register_census(
            [_rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)]
        )
        payload = self._prepared_in_place(env, capsys)
        worktree = Path(payload["worktree"])
        candidate = _commit_file(worktree, "fix.txt", "fix\n", "test: cli repair")

        code, outcome = env.publish(capsys, number=514, receipt=payload["receipt"])

        assert code == 0, outcome
        assert outcome["outcome"] == "published"
        assert outcome["candidate_object"] == candidate
        assert outcome["remote_head"] == candidate
        assert self.executor_remote(env).remote_head(_BASE_URL, "pr-514-head") == candidate
        push = self.executor_remote(env).pushes_to(_BASE_URL)[0]
        assert f"--force-with-lease=refs/heads/pr-514-head:{head_sha}" in push
        assert push[-1] == f"{candidate}:refs/heads/pr-514-head"

    def executor_remote(self, env: Environment) -> ControlledRemote:
        return env.executor

    def test_clean_rebuild_publishes_over_the_reviewed_head_with_the_exact_lease(
        self, tmp_path: Path, world: RemoteWorld, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        head_sha = world.branch_from("pr-514-head", world.base_sha, 3, "repair")
        env = Environment(tmp_path, world)
        env.install(monkeypatch)
        env.register_pull_request(
            _rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)
        )
        env.register_census(
            [_rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)]
        )
        code, payload = env.prepare(capsys, number=514, mode="clean-rebuild", branch="rebuild")
        assert code == 0
        candidate = _commit_file(Path(payload["worktree"]), "fix.txt", "fix\n", "test: rebuild")

        code, outcome = env.publish(capsys, number=514, receipt=payload["receipt"])

        assert code == 0, outcome
        assert outcome["outcome"] == "published"
        push = env.executor.pushes_to(_BASE_URL)[0]
        assert f"--force-with-lease=refs/heads/pr-514-head:{head_sha}" in push
        assert env.executor.remote_head(_BASE_URL, "pr-514-head") == candidate

    def test_declared_stack_publishes_when_the_live_base_matches_the_dependency(
        self, tmp_path: Path, world: RemoteWorld, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        parent_sha = world.branch_from("pr-766-head", world.base_sha, 2, "parent")
        head_sha = world.branch_from("pr-999-head", parent_sha, 1, "stack")
        env = Environment(tmp_path, world)
        env.install(monkeypatch)
        env.register_pull_request(
            _rest_row(
                999,
                base_ref="pr-766-head",
                base_sha=parent_sha,
                head_ref="pr-999-head",
                head_sha=head_sha,
            )
        )
        env.register_pull_request(
            _rest_row(766, base_sha=world.base_sha, head_ref="pr-766-head", head_sha=parent_sha)
        )
        env.register_census(
            [
                _rest_row(
                    999,
                    base_ref="pr-766-head",
                    base_sha=parent_sha,
                    head_ref="pr-999-head",
                    head_sha=head_sha,
                ),
                _rest_row(
                    766, base_sha=world.base_sha, head_ref="pr-766-head", head_sha=parent_sha
                ),
            ]
        )
        code, payload = env.prepare(
            capsys, number=999, mode="stacked", dependency=766, branch="stacked-repair"
        )
        assert code == 0
        candidate = _commit_file(
            Path(payload["worktree"]), "fix.txt", "fix\n", "test: stack repair"
        )

        code, outcome = env.publish(capsys, number=999, receipt=payload["receipt"])

        assert code == 0, outcome
        assert outcome["outcome"] == "published"
        assert env.executor.remote_head(_BASE_URL, "pr-999-head") == candidate

    def test_fork_owned_head_publishes_to_the_head_repository(
        self, tmp_path: Path, world: RemoteWorld, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        fork_url = "https://github.com/octo-fork/example.git"
        env = Environment(tmp_path, world)
        env.executor.mapping[fork_url] = world.bare
        env.install(monkeypatch)
        head_sha = world.branch_from("pr-514-head", world.base_sha, 1, "repair")
        env.register_pull_request(
            _rest_row(
                514,
                base_sha=world.base_sha,
                head_repository="octo-fork/example",
                head_ref="pr-514-head",
                head_sha=head_sha,
            )
        )
        env.register_census(
            [
                _rest_row(
                    514,
                    base_sha=world.base_sha,
                    head_repository="octo-fork/example",
                    head_ref="pr-514-head",
                    head_sha=head_sha,
                )
            ]
        )
        code, payload = env.prepare(capsys, number=514)
        assert code == 0
        candidate = _commit_file(Path(payload["worktree"]), "fix.txt", "fix\n", "test: fork repair")

        code, outcome = env.publish(capsys, number=514, receipt=payload["receipt"])

        assert code == 0, outcome
        push = env.executor.pushes_to(fork_url)[0]
        assert "https://github.com/octo-fork/example.git" in push
        assert env.executor.remote_head(fork_url, "pr-514-head") == candidate

    def test_moved_remote_head_is_refused_without_a_push(
        self, tmp_path: Path, world: RemoteWorld, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        head_sha = world.branch_from("pr-514-head", world.base_sha, 1, "repair")
        env = Environment(tmp_path, world)
        env.install(monkeypatch)
        env.register_pull_request(
            _rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)
        )
        env.register_census(
            [_rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)]
        )
        payload = self._prepared_in_place(env, capsys)
        _commit_file(Path(payload["worktree"]), "fix.txt", "fix\n", "test: repair")
        moved_sha = world.branch_from("unrelated-move", world.base_sha, 1, "moved")
        _move_remote_branch(world.bare, "pr-514-head", moved_sha)

        code, outcome = env.publish(capsys, number=514, receipt=payload["receipt"])

        assert code == 75
        assert outcome["outcome"] == "temporary-failure"
        assert env.executor.pushes == []
        assert env.executor.remote_head(_BASE_URL, "pr-514-head") == moved_sha

    def test_failed_push_that_preserves_the_head_is_a_temporary_failure(
        self, tmp_path: Path, world: RemoteWorld, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        head_sha = world.branch_from("pr-514-head", world.base_sha, 1, "repair")
        env = Environment(tmp_path, world)
        env.install(monkeypatch)
        env.register_pull_request(
            _rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)
        )
        env.register_census(
            [_rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)]
        )
        payload = self._prepared_in_place(env, capsys)
        candidate = _commit_file(Path(payload["worktree"]), "fix.txt", "fix\n", "test: repair")
        env.executor.fail_push = True

        code, outcome = env.publish(capsys, number=514, receipt=payload["receipt"])

        assert code == 75
        assert outcome["outcome"] == "temporary-failure"
        assert env.executor.remote_head(_BASE_URL, "pr-514-head") == head_sha
        assert outcome["candidate_object"] == candidate

    def test_failed_push_with_unreadable_remote_is_unknown_and_reconciles_later(
        self, tmp_path: Path, world: RemoteWorld, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        head_sha = world.branch_from("pr-514-head", world.base_sha, 1, "repair")
        env = Environment(tmp_path, world)
        env.install(monkeypatch)
        env.register_pull_request(
            _rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)
        )
        env.register_census(
            [_rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)]
        )
        payload = self._prepared_in_place(env, capsys)
        candidate = _commit_file(Path(payload["worktree"]), "fix.txt", "fix\n", "test: repair")
        env.executor.fail_readback = True

        code, outcome = env.publish(capsys, number=514, receipt=payload["receipt"])

        assert code == 76
        assert outcome["outcome"] == "unknown"

        env.executor.fail_readback = False
        code, outcome = env.publish(capsys, number=514, receipt=payload["receipt"])

        assert code == 0, outcome
        assert outcome["outcome"] == "reconciled"
        assert env.executor.remote_head(_BASE_URL, "pr-514-head") == candidate


# ---------------------------------------------------------------------------
# The 105-file parent regression and late-drift rejection
# ---------------------------------------------------------------------------


class TestContaminatedParentRegression:
    """The 105-file parent incident cannot reproduce through publication."""

    def test_unrelated_105_file_parent_cannot_publish_four_file_repair(
        self, tmp_path: Path, world: RemoteWorld, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """Reproduce the incident: a repair built on another PR's 105-file head."""
        parent_sha = world.branch_from("pr-766-head", world.base_sha, 105, "parent")
        head_sha = world.branch_from("pr-514-head", world.base_sha, 1, "repair")
        env = Environment(tmp_path, world)
        env.install(monkeypatch)
        env.register_pull_request(
            _rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)
        )
        env.register_pull_request(
            _rest_row(766, base_sha=world.base_sha, head_ref="pr-766-head", head_sha=parent_sha)
        )
        env.register_census(
            [
                _rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha),
                _rest_row(
                    766, base_sha=world.base_sha, head_ref="pr-766-head", head_sha=parent_sha
                ),
            ]
        )
        code, payload = env.prepare(capsys, number=514)
        assert code == 0
        worktree = Path(payload["worktree"])
        # The external swarm based its repair on the sibling pull request head.
        _git(worktree, "fetch", "--quiet", str(world.bare), "pr-766-head")
        _git(worktree, "merge", "--quiet", "--no-edit", "FETCH_HEAD")
        candidate = _commit_file(worktree, "fix.txt", "fix\n", "test: cli repair")
        assert int(_git(worktree, "rev-list", "--count", f"{parent_sha}..{candidate}")) >= 1

        code, outcome = env.publish(capsys, number=514, receipt=payload["receipt"])

        assert code == 1
        assert outcome["outcome"] == "rejected"
        assert "766" in outcome["message"]
        assert env.executor.pushes == []
        assert env.executor.remote_head(_BASE_URL, "pr-514-head") == head_sha

    def test_rejection_occurs_before_push_and_preserves_remote_head(
        self, tmp_path: Path, world: RemoteWorld, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        parent_sha = world.branch_from("pr-766-head", world.base_sha, 2, "parent")
        head_sha = world.branch_from("pr-514-head", world.base_sha, 1, "repair")
        env = Environment(tmp_path, world)
        env.install(monkeypatch)
        env.register_pull_request(
            _rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)
        )
        env.register_census(
            [
                _rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha),
                _rest_row(
                    766, base_sha=world.base_sha, head_ref="pr-766-head", head_sha=parent_sha
                ),
            ]
        )
        code, payload = env.prepare(capsys, number=514)
        assert code == 0
        worktree = Path(payload["worktree"])
        _git(worktree, "fetch", "--quiet", str(world.bare), "pr-766-head")
        _git(worktree, "merge", "--quiet", "--no-edit", "FETCH_HEAD")

        rest_calls_before = len(env.rest.calls)
        code, _outcome = env.publish(capsys, number=514, receipt=payload["receipt"])

        assert code == 1
        assert env.executor.pushes == []
        # The fresh census and sibling reads ran, and the remote ref never moved.
        assert len(env.rest.calls) > rest_calls_before
        assert env.executor.remote_head(_BASE_URL, "pr-514-head") == head_sha

    def test_sibling_head_added_after_preparation_is_rejected_by_the_fresh_census(
        self, tmp_path: Path, world: RemoteWorld, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        head_sha = world.branch_from("pr-514-head", world.base_sha, 1, "repair")
        env = Environment(tmp_path, world)
        env.install(monkeypatch)
        env.register_pull_request(
            _rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)
        )
        env.register_census(
            [_rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)]
        )
        code, payload = env.prepare(capsys, number=514)
        assert code == 0
        worktree = Path(payload["worktree"])
        sibling_sha = world.branch_from("pr-800-head", world.base_sha, 1, "sibling")
        _git(worktree, "fetch", "--quiet", str(world.bare), "pr-800-head")
        _git(worktree, "merge", "--quiet", "--no-edit", "FETCH_HEAD")
        _commit_file(worktree, "fix.txt", "fix\n", "test: repair")
        env.register_pull_request(
            _rest_row(800, base_sha=world.base_sha, head_ref="pr-800-head", head_sha=sibling_sha)
        )
        env.register_census(
            [
                _rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha),
                _rest_row(
                    800, base_sha=world.base_sha, head_ref="pr-800-head", head_sha=sibling_sha
                ),
            ]
        )

        code, outcome = env.publish(capsys, number=514, receipt=payload["receipt"])

        assert code == 1
        assert "800" in outcome["message"]
        assert env.executor.pushes == []


class TestFinalLivePublicationBinding:
    """Late live drift is rejected before the push."""

    def test_target_base_drift_during_census_is_rejected_before_push(
        self, tmp_path: Path, world: RemoteWorld, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        self._assert_late_drift_rejected(monkeypatch, capsys, world, tmp_path, field="base_sha")

    def test_target_base_branch_drift_during_census_is_rejected_before_push(
        self, tmp_path: Path, world: RemoteWorld, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        self._assert_late_drift_rejected(monkeypatch, capsys, world, tmp_path, field="base_ref")

    def test_late_base_drift_preserves_remote_head(
        self, tmp_path: Path, world: RemoteWorld, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        self._assert_late_drift_rejected(monkeypatch, capsys, world, tmp_path, field="base_sha")

    def _assert_late_drift_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        world: RemoteWorld,
        tmp_path: Path,
        *,
        field: str,
    ) -> None:
        head_sha = world.branch_from("pr-514-head", world.base_sha, 1, "repair")
        env = Environment(tmp_path, world)
        env.install(monkeypatch)
        env.register_pull_request(
            _rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)
        )
        env.register_census(
            [_rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)]
        )
        code, payload = env.prepare(capsys, number=514)
        assert code == 0
        _commit_file(Path(payload["worktree"]), "fix.txt", "fix\n", "test: repair")

        original = env.rest.__call__

        def drifting_rest(arguments: list[str], **kwargs: Any) -> str:
            result = original(arguments, **kwargs)
            url = arguments[1]
            if url == f"repos/{_BASE_REPOSITORY}/pulls/514":
                drifted = dict(env.rest.payloads[url])
                if field == "base_sha":
                    drifted["base"]["sha"] = "f" * 40
                else:
                    drifted["base"]["ref"] = "release"
                env.rest.payloads[url] = drifted
            return result

        monkeypatch.setattr(module, "_gh_api_read", drifting_rest)
        census_url = f"repos/{_BASE_REPOSITORY}/pulls?state=open&per_page=100&page=1"
        moved = False

        def counting_rest(arguments: list[str], **kwargs: Any) -> str:
            nonlocal moved
            result = drifting_rest(arguments, **kwargs)
            if arguments[1] == census_url:
                moved = True
            return result

        monkeypatch.setattr(module, "_gh_api_read", counting_rest)

        code, outcome = env.publish(capsys, number=514, receipt=payload["receipt"])

        assert code == 1
        assert outcome["outcome"] == "rejected"
        assert moved, "the drift must occur after the first live read, during the census"
        assert env.executor.pushes == []
        assert env.executor.remote_head(_BASE_URL, "pr-514-head") == head_sha

    def test_stacked_dependency_drift_is_rejected_by_the_final_dependency_read(
        self, tmp_path: Path, world: RemoteWorld, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        parent_sha = world.branch_from("pr-766-head", world.base_sha, 2, "parent")
        head_sha = world.branch_from("pr-999-head", parent_sha, 1, "stack")
        env = Environment(tmp_path, world)
        env.install(monkeypatch)
        env.register_pull_request(
            _rest_row(
                999,
                base_ref="pr-766-head",
                base_sha=parent_sha,
                head_ref="pr-999-head",
                head_sha=head_sha,
            )
        )
        env.register_pull_request(
            _rest_row(766, base_sha=world.base_sha, head_ref="pr-766-head", head_sha=parent_sha)
        )
        env.register_census(
            [
                _rest_row(
                    999,
                    base_ref="pr-766-head",
                    base_sha=parent_sha,
                    head_ref="pr-999-head",
                    head_sha=head_sha,
                ),
                _rest_row(
                    766, base_sha=world.base_sha, head_ref="pr-766-head", head_sha=parent_sha
                ),
            ]
        )
        code, payload = env.prepare(
            capsys, number=999, mode="stacked", dependency=766, branch="stacked-repair"
        )
        assert code == 0
        _commit_file(Path(payload["worktree"]), "fix.txt", "fix\n", "test: stack repair")

        original = env.rest.__call__
        dependency_url = f"repos/{_BASE_REPOSITORY}/pulls/766"

        def drifting_rest(arguments: list[str], **kwargs: Any) -> str:
            result = original(arguments, **kwargs)
            if arguments[1] == dependency_url:
                drifted = dict(env.rest.payloads[dependency_url])
                drifted["head"]["sha"] = "e" * 40
                env.rest.payloads[dependency_url] = drifted
            return result

        monkeypatch.setattr(module, "_gh_api_read", drifting_rest)

        code, outcome = env.publish(capsys, number=999, receipt=payload["receipt"])

        assert code == 1
        assert outcome["outcome"] == "rejected"
        assert env.executor.pushes == []
        assert env.executor.remote_head(_BASE_URL, "pr-999-head") == head_sha


# ---------------------------------------------------------------------------
# Argument errors
# ---------------------------------------------------------------------------


class TestArgumentGrammar:
    """Argument errors follow the shared outcome envelope."""

    def test_missing_required_option_reports_argument_error_with_exit_2(
        self, tmp_path: Path, capsys
    ) -> None:
        code = prepare_pr_worktree_main(["514", "--json"])

        assert code == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["outcome"] == "argument-error"
        assert payload["exit_code"] == 2
        assert payload["receipt"] is None

    def test_argument_error_without_the_flag_reports_human_output(
        self, tmp_path: Path, capsys
    ) -> None:
        code = prepare_pr_worktree_main([])

        assert code == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "required" in captured.err

    def test_dependency_pr_outside_stacked_mode_is_rejected(
        self, tmp_path: Path, world: RemoteWorld, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        head_sha = world.branch_from("pr-514-head", world.base_sha, 1, "repair")
        env = Environment(tmp_path, world)
        env.install(monkeypatch)
        env.register_pull_request(
            _rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)
        )
        env.register_census(
            [_rest_row(514, base_sha=world.base_sha, head_ref="pr-514-head", head_sha=head_sha)]
        )

        code, outcome = env.prepare(capsys, number=514, dependency=766)

        assert code == 1
        assert outcome["outcome"] == "rejected"

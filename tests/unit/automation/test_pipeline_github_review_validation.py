"""Check bounded provider CI admission for the generic validation capability.

Hephaestus owns the mechanism and ships no repository profile. These tests
build a minimal generic profile through
``tests.unit.automation.pipeline.repository_validation_profile_stub`` and
exercise the pipeline behavior that Hephaestus does own: the read-only
transport, the immutable identity, the source-control admission, and the
two-observation collection rule.

Repository-specific facts, such as the exact command vectors of one
repository or one repository version selector, belong to the installed
profile distribution and are not tested here.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import subprocess
import threading
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from hephaestus.agents.workspace import SourceLane, WorkspaceBinding
from hephaestus.automation.pipeline.repository_validation import (
    RepositoryValidationInvocation,
    RepositoryValidationPlan,
)
from tests.unit.automation.pipeline.repository_validation_profile_stub import (
    CI_WORKFLOW_PATH,
    CONTROL_PATHS,
    CONTROL_SOURCES,
    NIGHTLY_CHECK_ID,
    REPOSITORY,
    build_profile,
    ci_job,
    install_profile,
    write_controls,
)

PR_NUMBER = 1733
ISSUE_NUMBER = 1623
HEAD = "a" * 40
BASE = "b" * 40
TREE = "d" * 40
MERGE = "c" * 40
HEAD_BRANCH = "codex/repair"
RUN_ID = 123
ATTEMPT = 2
REPO_ID = 1308303366
CONTROLS = {path: (0o100644, content) for path, content in CONTROL_SOURCES}


def _api() -> ModuleType:
    name = "hephaestus.automation.pipeline_github_review_validation"
    assert importlib.util.find_spec(name) is not None, "The validation adapter is not available."
    return importlib.import_module(name)


def _git(root: Path, *args: str) -> str:
    """Run one fixed Git command in the fixture repository."""
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=Validation fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    ).stdout.strip()


def _checkout(root: Path) -> tuple[str, str]:
    """Create a source workspace whose commits bind the stub control bytes."""
    write_controls(root)
    package = root / "src/ledger"
    package.mkdir(parents=True)
    (package / "__init__.py").write_bytes(b"")
    payload = package / "example.py"
    payload.write_bytes(b"VALUE = 1\n")
    _git(root, "init", "--initial-branch=main")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Create the source fixture.")
    base = _git(root, "rev-parse", "HEAD")
    payload.write_bytes(b"VALUE = 2\n")
    _git(root, "add", "src/ledger/example.py")
    _git(root, "commit", "-m", "Change the reviewed source.")
    head = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "--detach", head)
    return base, head


def _workspace(root: Path, repository: str = REPOSITORY, revision: str | None = None):
    """Bind one detached review workspace to the fixture source."""
    return WorkspaceBinding.source(
        cwd=root,
        reusable_root=root.parent,
        repository=repository,
        ownership_key="ledger-review-1623",
        item_number=ISSUE_NUMBER,
        lane=SourceLane.REVIEW,
        revision=revision if revision is not None else _git(root, "rev-parse", "HEAD"),
        generation=1,
        detached=True,
    )


# --------------------------------------------------------------------------
# Immutable source plan
# --------------------------------------------------------------------------


def _source_plan_api() -> Any:
    api = _api()
    assert hasattr(api, "validation_plan_for_workspace"), "Source collection is missing."
    return api.validation_plan_for_workspace


def _source_fixture(tmp_path: Path) -> tuple[Any, str, str]:
    root = tmp_path / "source"
    base, head = _checkout(root)
    return _workspace(root, revision=head), base, head


def test_source_plan_reads_committed_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use immutable controls even when the worktree copy differs."""
    install_profile(monkeypatch)
    workspace, base, head = _source_fixture(tmp_path)
    (workspace.cwd / "pyproject.toml").write_bytes(b"uncommitted replacement\n")
    plan = _source_plan_api()(
        workspace,
        repository=REPOSITORY,
        issue_number=ISSUE_NUMBER,
        pr_number=ISSUE_NUMBER,
        reviewed_base=base,
        timeout_s=30,
    )
    assert plan.reviewed_head == head
    assert plan.reviewed_base == base
    assert plan.source_workspace == workspace
    assert plan.changes == (("M", "src/ledger/example.py"),)
    assert plan.execution_allowed
    assert {check.check_id for check in plan.checks} == {
        "ledger.python.ruff-check",
        "ledger.python.ty-check",
    }
    assert all(len(check.source_digests) == len(CONTROL_PATHS) for check in plan.checks)


def test_source_plan_retains_target_base_and_real_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exclude target-only changes and preserve additions and deletions."""
    from dataclasses import replace

    install_profile(monkeypatch)
    workspace, branchpoint, _head = _source_fixture(tmp_path)
    root = workspace.cwd
    (root / "src/ledger/added.py").write_bytes(b"VALUE = 3\n")
    (root / "src/ledger/example.py").unlink()
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "Add and delete reviewed files.")
    changed = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "--detach", branchpoint)
    (root / "src/ledger/target_only.py").write_bytes(b"VALUE = 4\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "Advance the target branch.")
    target = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "--detach", changed)
    plan = _source_plan_api()(
        replace(workspace, revision=changed),
        repository=REPOSITORY,
        issue_number=ISSUE_NUMBER,
        pr_number=ISSUE_NUMBER,
        reviewed_base=target,
        timeout_s=30,
    )
    assert plan.reviewed_base == target != branchpoint
    assert plan.diff_base_sha == branchpoint
    assert plan.changes == (("A", "src/ledger/added.py"), ("D", "src/ledger/example.py"))


@pytest.mark.parametrize("revision", ["head", "base"])
def test_source_plan_rejects_changed_committed_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: str
) -> None:
    """Reject a changed control in either admitted commit."""
    from dataclasses import replace

    install_profile(monkeypatch)
    workspace, base, head = _source_fixture(tmp_path)
    root = workspace.cwd
    if revision == "base":
        _git(root, "checkout", "--detach", base)
    with (root / "pyproject.toml").open("ab") as stream:
        stream.write(b"\n# Changed committed control.\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "Change a control.")
    changed = _git(root, "rev-parse", "HEAD")
    if revision == "base":
        base = changed
        _git(root, "checkout", "--detach", head)
    else:
        workspace = replace(workspace, revision=changed)
    with pytest.raises(ValueError, match=r"control|profile"):
        _source_plan_api()(
            workspace,
            repository=REPOSITORY,
            issue_number=ISSUE_NUMBER,
            pr_number=ISSUE_NUMBER,
            reviewed_base=base,
            timeout_s=30,
        )


def test_source_plan_rejects_unsupported_repository_before_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a repository that has no installed profile before reading source."""
    from dataclasses import replace
    from unittest.mock import patch

    install_profile(monkeypatch)
    workspace, base, _ = _source_fixture(tmp_path)
    with patch("subprocess.Popen", side_effect=AssertionError("Git must not run.")):
        with pytest.raises(ValueError, match=r"binding|repository|unsupported"):
            _source_plan_api()(
                replace(workspace, repository="Other/ledger"),
                repository=REPOSITORY,
                issue_number=ISSUE_NUMBER,
                pr_number=ISSUE_NUMBER,
                reviewed_base=base,
                timeout_s=30,
            )


def test_source_plan_rejects_a_shallow_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Require a complete history for immutable source inspection."""
    install_profile(monkeypatch)
    workspace, base, head = _source_fixture(tmp_path)
    (workspace.cwd / ".git" / "shallow").write_text(f"{head}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="complete Git history"):
        _source_plan_api()(
            workspace,
            repository=REPOSITORY,
            issue_number=ISSUE_NUMBER,
            pr_number=ISSUE_NUMBER,
            reviewed_base=base,
            timeout_s=30,
        )


# --------------------------------------------------------------------------
# Bounded read-only transport
# --------------------------------------------------------------------------


def _reader(profile: Any | None = None, **kwargs: Any) -> Any:
    """Return a bounded reader bound to the profile repository."""
    api = _api()
    profile = profile if profile is not None else build_profile()
    return api.ValidationCIReader(
        profile.repository, profile.ci_workflow_path, deadline_s=kwargs.pop("deadline_s"), **kwargs
    )


def test_ci_reader_uses_one_deadline_and_fixed_read_only_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep every page inside the original deadline and output cap."""
    api = _api()
    clock = [10.0]
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(api, "trusted_gh_executable", lambda: "/trusted/gh")

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, '{"ok":true}', "")

    monkeypatch.setattr(api, "run_subprocess", run)
    reader = _reader(deadline_s=1000)
    assert reader.read(f"repos/{REPOSITORY}/pulls/{PR_NUMBER}") == {"ok": True}
    clock[0] = 30
    assert reader.read(f"repos/{REPOSITORY}/actions/runs/{RUN_ID}") == {"ok": True}
    assert [call[1]["timeout"] for call in calls] == [120, 100]
    assert calls[0][0] == [
        "/trusted/gh",
        "api",
        "--hostname",
        "github.com",
        "--method",
        "GET",
        f"repos/{REPOSITORY}/pulls/{PR_NUMBER}",
    ]
    assert all(call[1]["max_output_bytes"] == 4 * 1024 * 1024 for call in calls)
    assert all(call[1]["track_process_group"] and not call[1]["log_on_error"] for call in calls)
    clock[0] = 131
    with pytest.raises(api.ValidationCIReadError, match="deadline"):
        reader.read(f"repos/{REPOSITORY}/pulls/{PR_NUMBER}")
    assert len(calls) == 2


@pytest.mark.parametrize(
    "endpoint",
    [
        "repos/other/ledger/pulls/1",
        f"https://example.invalid/repos/{REPOSITORY}/pulls/1",
        f"repos/{REPOSITORY}/actions/runs/1/rerun",
        f"repos/{REPOSITORY}/pulls/1/comments",
        f"repos/{REPOSITORY}/git/trees/main?recursive=1",
        f"repos/{REPOSITORY}/actions/runs/1/attempts/1/jobs?per_page=100&page=11",
    ],
)
def test_ci_reader_rejects_nonprofile_endpoints(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    """Reject writes, foreign targets, mutable refs, and excess pages before execution."""
    api = _api()
    monkeypatch.setattr(api, "trusted_gh_executable", lambda: "/trusted/gh")
    calls: list[object] = []
    monkeypatch.setattr(api, "run_subprocess", lambda *args, **kwargs: calls.append(args))
    reader = _reader(deadline_s=api.time.monotonic() + 60)
    with pytest.raises(api.ValidationCIReadError):
        reader.read(endpoint)
    assert calls == []


@pytest.mark.parametrize(
    "body", ['{"id":1,"id":2}', '{"value":NaN}', '{"value":Infinity}', "{} trailing"]
)
def test_ci_reader_rejects_ambiguous_json(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """Reject duplicate members, nonfinite numbers, and incomplete JSON."""
    api = _api()
    monkeypatch.setattr(api, "trusted_gh_executable", lambda: "/trusted/gh")
    monkeypatch.setattr(
        api, "run_subprocess", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, body, "")
    )
    reader = _reader(deadline_s=api.time.monotonic() + 60)
    with pytest.raises(api.ValidationCIReadError, match="json"):
        reader.read(f"repos/{REPOSITORY}/pulls/{PR_NUMBER}")


def test_ci_reader_aggregate_limit_spans_all_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Accept the exact JSON budget and reject another read before execution."""
    api = _api()
    monkeypatch.setattr(api, "trusted_gh_executable", lambda: "/trusted/gh")
    calls: list[int] = []
    body = "{}" + " " * (4 * 1024 * 1024 - 2)

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(kwargs["max_output_bytes"])
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(api, "run_subprocess", run)
    reader = _reader(deadline_s=api.time.monotonic() + 60)
    for _ in range(8):
        assert reader.read(f"repos/{REPOSITORY}/pulls/{PR_NUMBER}") == {}
    with pytest.raises(api.ValidationCIReadError, match="aggregate"):
        reader.read(f"repos/{REPOSITORY}/pulls/{PR_NUMBER}")
    assert len(calls) == 8


@pytest.mark.parametrize(
    ("count", "kind"),
    [(600, "pull"), (512, "blob")],
)
def test_ci_reader_enforces_request_and_object_budgets(
    monkeypatch: pytest.MonkeyPatch, count: int, kind: str
) -> None:
    """Stop before a request would exceed its complete-attempt budget."""
    api = _api()
    monkeypatch.setattr(api, "trusted_gh_executable", lambda: "/trusted/gh")
    calls: list[str] = []
    endpoint = (
        f"repos/{REPOSITORY}/pulls/{PR_NUMBER}"
        if kind == "pull"
        else f"repos/{REPOSITORY}/git/blobs/" + "a" * 40
    )

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(api, "run_subprocess", run)
    reader = _reader(deadline_s=api.time.monotonic() + 60)
    for _ in range(count):
        reader.read(endpoint)
    with pytest.raises(api.ValidationCIReadError, match="limit"):
        reader.read(endpoint)
    assert len(calls) == count


def test_ci_reader_cancelled_request_starts_no_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply cancellation before process creation."""
    api = _api()
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(api, "trusted_gh_executable", lambda: "/trusted/gh")
    calls: list[object] = []
    monkeypatch.setattr(api, "run_subprocess", lambda *args, **kwargs: calls.append(args))
    reader = _reader(deadline_s=api.time.monotonic() + 60, shutdown=stop)
    with pytest.raises(api.ValidationCIReadError, match="cancelled"):
        reader.read(f"repos/{REPOSITORY}/pulls/{PR_NUMBER}")
    assert calls == []


@pytest.mark.skipif(os.name != "posix", reason="This test checks POSIX process cleanup.")
def test_ci_reader_terminates_a_child_that_exceeds_output_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Prove the reader terminates excess output during capture."""
    import sys
    import time

    api = _api()
    actual_run = api.run_subprocess
    pid_file = tmp_path / "reader-child.pid"
    program = (
        "import os,sys,time; from pathlib import Path; "
        "Path(sys.argv[1]).write_text(str(os.getpid())); "
        "sys.stdout.buffer.write(b'x' * (4 * 1024 * 1024 + 1)); "
        "sys.stdout.buffer.flush(); time.sleep(30)"
    )

    def run(argv: list[str], **kwargs: Any) -> Any:
        return actual_run([sys.executable, "-I", "-c", program, str(pid_file)], **kwargs)

    monkeypatch.setattr(api, "trusted_gh_executable", lambda: "/trusted/gh")
    monkeypatch.setattr(api, "run_subprocess", run)
    reader = _reader(deadline_s=time.monotonic() + 10)
    with pytest.raises(api.ValidationCIReadError, match="response_byte_limit"):
        reader.read(f"repos/{REPOSITORY}/pulls/{PR_NUMBER}")
    pid = int(pid_file.read_text())
    expires = time.monotonic() + 2
    while time.monotonic() < expires:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("The provider child remains alive after the output limit.")


@pytest.mark.parametrize("interruption", ["deadline", "cancelled"])
def test_ci_reader_rejects_interruption_during_json_validation(
    monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    """Reject evidence if parsing consumes the deadline or receives cancellation."""
    api = _api()
    now = [100.0]
    stop = threading.Event()
    monkeypatch.setattr(api.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(api, "trusted_gh_executable", lambda: "/trusted/gh")
    calls: list[str] = []

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, '{"id":1733}', "")

    validate = api._ci_json_text_is_valid

    def interrupt(value: object) -> None:
        validate(value)
        if interruption == "deadline":
            now[0] = 160.0
        else:
            stop.set()

    monkeypatch.setattr(api, "run_subprocess", run)
    monkeypatch.setattr(api, "_ci_json_text_is_valid", interrupt)
    reader = _reader(deadline_s=160.0, shutdown=stop)
    with pytest.raises(api.ValidationCIReadError, match=interruption):
        reader.read(f"repos/{REPOSITORY}/pulls/{PR_NUMBER}")
    stop.clear()
    now[0] = 100.0
    with pytest.raises(api.ValidationCIReadError, match=interruption):
        reader.read(f"repos/{REPOSITORY}/pulls/{PR_NUMBER}")
    assert len(calls) == 1


# --------------------------------------------------------------------------
# Immutable provider identity
# --------------------------------------------------------------------------


def _identity_fixture() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Make synthetic provider data for identity checks, without source evidence."""
    repo = {"id": REPO_ID, "full_name": REPOSITORY, "fork": False}
    pr = {
        "number": PR_NUMBER,
        "state": "open",
        "head": {"sha": HEAD, "ref": HEAD_BRANCH, "repo": deepcopy(repo)},
        "base": {"sha": BASE, "ref": "main", "repo": deepcopy(repo)},
    }
    run = {
        "id": RUN_ID,
        "run_attempt": ATTEMPT,
        "repository": deepcopy(repo),
        "head_repository": deepcopy(repo),
        "head_sha": HEAD,
        "head_branch": HEAD_BRANCH,
        "path": CI_WORKFLOW_PATH,
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "referenced_workflows": [
            {
                "path": f"{REPOSITORY}/{CI_WORKFLOW_PATH}@{MERGE}",
                "sha": MERGE,
                "ref": f"refs/pull/{PR_NUMBER}/merge",
            }
        ],
    }
    merge = {"sha": MERGE, "parents": [{"sha": BASE}, {"sha": HEAD}], "tree": {"sha": TREE}}
    return pr, run, merge


def _identity(api: ModuleType, data: tuple[dict[str, Any], ...], profile: Any) -> Any:
    return api.validation_ci_identity(
        profile,
        *data,
        pr_number=PR_NUMBER,
        reviewed_head=HEAD,
        reviewed_base=BASE,
        head_branch=HEAD_BRANCH,
    )


def test_ci_identity_binds_merge_parents_without_run_pull_requests() -> None:
    """Bind the immutable merge witness without inferring a PR from the run list."""
    api = _api()
    profile = build_profile()
    data = _identity_fixture()
    identity = _identity(api, data, profile)
    assert identity.repository_id == REPO_ID
    assert (identity.run_id, identity.run_attempt) == (RUN_ID, ATTEMPT)
    assert identity.merge_sha == MERGE
    assert identity.merge_tree == TREE
    data[1]["pull_requests"] = []
    assert _identity(api, data, profile) == identity
    data[1]["run_attempt"] = 3
    assert _identity(api, data, profile) != identity


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ((0, "number"), PR_NUMBER + 1),
        ((0, "number"), True),
        ((0, "state"), "closed"),
        ((0, "head", "sha"), "e" * 40),
        ((0, "base", "sha"), "e" * 40),
        ((0, "head", "ref"), "other"),
        ((0, "base", "ref"), "production"),
        ((0, "head", "repo", "id"), 9),
        ((0, "base", "repo", "id"), True),
        ((0, "head", "repo", "fork"), True),
        ((0, "base", "repo", "fork"), True),
        ((0, "head", "repo", "full_name"), "other/ledger"),
        ((1, "repository", "id"), 9),
        ((1, "head_repository", "id"), 9),
        ((1, "repository", "full_name"), "other/ledger"),
        ((1, "head_repository", "fork"), True),
        ((1, "id"), 0),
        ((1, "run_attempt"), True),
        ((1, "head_sha"), "e" * 40),
        ((1, "head_branch"), "other"),
        ((1, "path"), ".github/workflows/other.yml"),
        ((1, "event"), "pull_request_target"),
        ((1, "status"), "unknown"),
        ((1, "conclusion"), "unknown"),
        ((1, "referenced_workflows", 0, "sha"), "e" * 40),
        ((1, "referenced_workflows", 0, "ref"), f"refs/pull/{PR_NUMBER + 1}/merge"),
        (
            (1, "referenced_workflows", 0, "path"),
            f"other/ledger/{CI_WORKFLOW_PATH}@{MERGE}",
        ),
        ((2, "sha"), "e" * 40),
        ((2, "tree", "sha"), "short"),
        ((2, "parents"), [{"sha": HEAD}, {"sha": BASE}]),
        ((2, "parents"), [{"sha": BASE}]),
        ((2, "parents", 1, "sha"), "e" * 40),
    ],
)
def test_ci_identity_rejects_mismatched_provider_facts(path: tuple[Any, ...], value: Any) -> None:
    """Reject a changed PR, run, repository, or immutable merge fact."""
    api = _api()
    profile = build_profile()
    data = _identity_fixture()
    owner: Any = data
    for key in path[:-1]:
        owner = owner[key]
    owner[path[-1]] = value
    with pytest.raises(api.ValidationCIReadError):
        _identity(api, data, profile)


@pytest.mark.parametrize("change", ["missing", "duplicate", "extra", "mixed-sha"])
def test_ci_identity_requires_exact_workflow_witnesses(change: str) -> None:
    """Require one witness per local workflow and one shared immutable commit."""
    api = _api()
    profile = build_profile()
    data = _identity_fixture()
    witnesses = data[1]["referenced_workflows"]
    if change == "missing":
        witnesses.pop()
    elif change == "duplicate":
        witnesses.append(deepcopy(witnesses[0]))
    elif change == "extra":
        witnesses.append({**deepcopy(witnesses[0]), "path": f"{REPOSITORY}/other.yml@{MERGE}"})
    else:
        witnesses[0]["sha"] = "e" * 40
        witnesses[0]["path"] = witnesses[0]["path"].split("@")[0] + "@" + "e" * 40
    with pytest.raises(api.ValidationCIReadError):
        _identity(api, data, profile)


def test_ci_identity_retains_failure_and_incomplete_run_facts() -> None:
    """Keep failed and incomplete runs distinct from successful runs."""
    api = _api()
    profile = build_profile()
    data = _identity_fixture()
    success = _identity(api, data, profile)
    data[1]["conclusion"] = "failure"
    failed = _identity(api, data, profile)
    assert failed != success
    assert failed.conclusion == "failure"
    data[1]["status"] = "in_progress"
    data[1]["conclusion"] = None
    pending = _identity(api, data, profile)
    assert pending != failed
    assert pending.status == "in_progress"
    assert pending.conclusion is None


# --------------------------------------------------------------------------
# Immutable source-control admission
# --------------------------------------------------------------------------


def _control_graph(
    controls: dict[str, tuple[int, bytes]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Make an immutable API graph from the stub control bytes."""
    import base64
    import hashlib

    graph: dict[str, dict[str, Any]] = {}
    entries: list[dict[str, Any]] = []
    directories: set[str] = set()
    for path, (mode, content) in (CONTROLS if controls is None else controls).items():
        sha = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
        entries.append(
            {"path": path, "mode": f"{mode:o}", "type": "blob", "sha": sha, "size": len(content)}
        )
        for parent in Path(path).parents:
            if str(parent) != ".":
                directories.add(parent.as_posix())
        graph[f"repos/{REPOSITORY}/git/blobs/{sha}"] = {
            "sha": sha,
            "size": len(content),
            "encoding": "base64",
            "content": base64.b64encode(content).decode(),
        }
    entries.extend(
        {"path": path, "mode": "040000", "type": "tree", "sha": "e" * 40}
        for path in sorted(directories)
    )
    graph[f"repos/{REPOSITORY}/git/trees/{TREE}?recursive=1"] = {
        "sha": TREE,
        "truncated": False,
        "tree": entries,
    }
    for sha in ("a", "b", "c"):
        graph[f"repos/{REPOSITORY}/git/commits/{sha * 40}"] = {
            "sha": sha * 40,
            "tree": {"sha": TREE},
        }
    return graph


class _ControlGraphReader:
    def __init__(self, graph: dict[str, dict[str, Any]]) -> None:
        self.graph = graph
        self.calls: list[str] = []

    def _remaining(self) -> float:
        return 60.0

    def read(self, endpoint: str) -> dict[str, Any]:
        self.calls.append(endpoint)
        return deepcopy(self.graph[endpoint])


def _controls_admit(api: ModuleType, profile: Any, reader: Any, **kwargs: Any) -> str:
    return api.ValidationCISourceControls(reader, profile).admit(kwargs.pop("sha"), **kwargs)


def test_ci_controls_admit_head_base_and_merge_with_request_local_cache() -> None:
    """Read immutable source controls at all three commits without mutable refs."""
    api = _api()
    profile = build_profile()
    reader = _ControlGraphReader(_control_graph())
    controls = api.ValidationCISourceControls(reader, profile)
    for sha in ("a", "b", "c"):
        assert controls.admit(sha * 40, expected_tree=TREE) == profile.profile_id
    assert len(reader.calls) == 3 + 1 + len(CONTROL_PATHS)
    before = list(reader.calls)
    assert controls.admit("c" * 40, expected_tree=TREE) == profile.profile_id
    assert reader.calls == before
    fresh = _ControlGraphReader(_control_graph())
    api.ValidationCISourceControls(fresh, profile).admit("c" * 40, expected_tree=TREE)
    assert len(fresh.calls) == 2 + len(CONTROL_PATHS)


@pytest.mark.parametrize(
    "change",
    [
        "commit-sha",
        "tree-sha",
        "expected-tree",
        "truncated",
        "missing-control",
        "duplicate-path",
        "unsafe-path",
        "nonregular",
        "new-control",
        "blob-sha",
        "blob-size",
        "blob-encoding",
        "blob-content",
        "blob-digest",
        "tree-size",
    ],
)
def test_ci_controls_reject_incomplete_or_changed_immutable_graph(change: str) -> None:
    """Reject malformed object graphs and changed controls before CI coverage."""
    api = _api()
    profile = build_profile()
    graph = _control_graph()
    commit = graph[f"repos/{REPOSITORY}/git/commits/" + "a" * 40]
    tree = graph[f"repos/{REPOSITORY}/git/trees/{TREE}?recursive=1"]
    entry = tree["tree"][0]
    blob = graph[f"repos/{REPOSITORY}/git/blobs/{entry['sha']}"]
    expected_tree = "f" * 40 if change == "expected-tree" else TREE
    mutations = {
        "commit-sha": (commit, "sha", "f" * 40),
        "tree-sha": (tree, "sha", "f" * 40),
        "truncated": (tree, "truncated", True),
        "missing-control": (tree, "tree", tree["tree"][1:]),
        "duplicate-path": (tree, "tree", [*tree["tree"], dict(entry)]),
        "unsafe-path": (entry, "path", "../outside"),
        "nonregular": (entry, "mode", "120000"),
        "new-control": (
            tree,
            "tree",
            [*tree["tree"], {**entry, "path": ".github/workflows/extra.yml"}],
        ),
        "tree-size": (entry, "size", entry["size"] + 1),
        "blob-sha": (blob, "sha", "f" * 40),
        "blob-size": (blob, "size", blob["size"] + 1),
        "blob-encoding": (blob, "encoding", "utf-8"),
        "blob-content": (blob, "content", "invalid!"),
    }
    if change in mutations:
        target, key, value = mutations[change]
        target[key] = value
    elif change == "blob-digest":
        import base64

        content = base64.b64decode(blob["content"])
        blob["content"] = base64.b64encode(bytes([content[0] ^ 1]) + content[1:]).decode()
    with pytest.raises(api.ValidationCIReadError):
        _controls_admit(
            api, profile, _ControlGraphReader(graph), sha="a" * 40, expected_tree=expected_tree
        )


# --------------------------------------------------------------------------
# Two-observation collection
# --------------------------------------------------------------------------


def _ci_collection_fixture(
    tmp_path: Path, changes: tuple[tuple[str, str], ...] = (("M", "src/ledger/example.py"),)
) -> tuple[Any, _ControlGraphReader]:
    """Make a complete synthetic CI observation with the stub control bytes."""
    profile = build_profile(nightly=True)
    checks = profile.select(changes)
    workspace = WorkspaceBinding.source(
        cwd=tmp_path,
        reusable_root=tmp_path.parent,
        repository=REPOSITORY,
        ownership_key="ci-fixture",
        item_number=1700,
        lane=SourceLane.REVIEW,
        revision=HEAD,
        generation=1,
        detached=True,
    )
    plan = RepositoryValidationPlan(
        repository=REPOSITORY,
        issue_number=1700,
        pr_number=PR_NUMBER,
        reviewed_head=HEAD,
        reviewed_base=BASE,
        diff_base_sha=BASE,
        source_workspace=workspace,
        changes=changes,
        profile_id=profile.profile_id,
        profile_digest=profile.profile_digest,
        checks=checks,
    )
    ci_ids = tuple(
        check.check_id for check in checks if check.check_id in profile.ci_covered_check_ids(checks)
    )
    invocation = RepositoryValidationInvocation(plan, 1, "f" * 32, "ci", ci_ids)
    graph = _control_graph()
    pr, run, merge = _identity_fixture()
    graph[f"repos/{REPOSITORY}/pulls/{PR_NUMBER}"] = pr
    graph[f"repos/{REPOSITORY}/actions/runs/{RUN_ID}"] = run
    graph[f"repos/{REPOSITORY}/git/commits/{MERGE}"] = merge
    graph[
        f"repos/{REPOSITORY}/actions/workflows/ci.yml/runs?event=pull_request&head_sha="
        f"{HEAD}&per_page=100&page=1"
    ] = {"total_count": 1, "workflow_runs": [run]}
    jobs = list(ci_job())
    jobs_endpoint = (
        f"repos/{REPOSITORY}/actions/runs/{RUN_ID}/attempts/{ATTEMPT}/jobs?per_page=100&page=1"
    )
    graph[jobs_endpoint] = {"total_count": len(jobs), "jobs": jobs}
    return invocation, _ControlGraphReader(graph)


def _install_nightly_profile(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install the stub profile whose local-only nightly check is selected."""
    return install_profile(monkeypatch, nightly=True)


def _collect_ci(api: ModuleType, invocation: Any, reader: Any) -> Any:
    return api.collect_repository_validation_ci(invocation, head_branch=HEAD_BRANCH, reader=reader)


def test_ci_collection_requires_two_complete_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admit selected command evidence from stable jobs and immutable sources."""
    api = _api()
    _install_nightly_profile(monkeypatch)
    invocation, reader = _ci_collection_fixture(tmp_path)
    result = _collect_ci(api, invocation, reader)
    assert not result.gaps
    assert {receipt.check_id for receipt in result.receipts} == set(invocation.check_ids)
    assert all(
        receipt.status == "success" and receipt.plan_id == invocation.plan.plan_id
        for receipt in result.receipts
    )
    assert sum("/attempts/2/jobs?" in call for call in reader.calls) == 2
    assert reader.calls.count(f"repos/{REPOSITORY}/actions/runs/{RUN_ID}") == 4
    assert all("refs/" not in call for call in reader.calls)


def test_ci_collection_rejects_an_unbound_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Return a typed gap when no installed profile owns the plan repository."""
    api = _api()
    install_profile(monkeypatch, "other/ledger")
    invocation, reader = _ci_collection_fixture(tmp_path)
    result = _collect_ci(api, invocation, reader)
    assert result.receipts == ()
    assert [gap.reason for gap in result.gaps] == ["ci_plan_invalid"]
    assert reader.calls == []


@pytest.mark.parametrize(
    "change",
    [
        "second-job-missing",
        "second-step-missing",
        "second-attempt",
        "second-base",
        "second-status",
        "second-step-status",
    ],
)
def test_ci_collection_rejects_changed_second_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """Never combine a first-read success with changed second-read evidence."""
    api = _api()
    _install_nightly_profile(monkeypatch)
    invocation, reader = _ci_collection_fixture(tmp_path)
    original = reader.read
    count: dict[str, int] = {}

    def read(endpoint: str) -> dict[str, Any]:
        value = original(endpoint)
        count[endpoint] = count.get(endpoint, 0) + 1
        if "/jobs?" in endpoint and count[endpoint] == 2:
            if change == "second-job-missing":
                value["jobs"].pop()
                value["total_count"] -= 1
            elif change == "second-step-missing":
                value["jobs"][0]["steps"].pop()
            elif change == "second-step-status":
                value["jobs"][0]["steps"][-1]["conclusion"] = "failure"
        if endpoint.endswith(f"/runs/{RUN_ID}") and count[endpoint] >= 3:
            if change == "second-attempt":
                value["run_attempt"] = 3
            elif change == "second-status":
                value["conclusion"] = "failure"
        if (
            endpoint.endswith(f"/pulls/{PR_NUMBER}")
            and count[endpoint] == 2
            and change == "second-base"
        ):
            value["base"]["sha"] = "e" * 40
        return value

    reader.read = read  # type: ignore[method-assign]
    result = _collect_ci(api, invocation, reader)
    assert result.receipts == ()
    assert result.gaps


@pytest.mark.parametrize(
    "change",
    [
        "failed",
        "skipped",
        "noop",
        "missing-step",
        "missing-checkout",
        "duplicate-job",
        "wrong-job-head",
        "wrong-job-attempt",
        "duplicate-step",
        "too-many-steps",
        "short-page",
        "wrong-total",
    ],
)
def test_ci_collection_does_not_fill_missing_or_failed_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """Keep failed results and reject incomplete, unrelated, or ambiguous jobs."""
    api = _api()
    _install_nightly_profile(monkeypatch)
    invocation, reader = _ci_collection_fixture(tmp_path)
    page = reader.graph[
        f"repos/{REPOSITORY}/actions/runs/{RUN_ID}/attempts/{ATTEMPT}/jobs?per_page=100&page=1"
    ]
    job = page["jobs"][0]
    mutations = {
        "failed": (job, "conclusion", "failure"),
        "skipped": (job["steps"][-1], "conclusion", "skipped"),
        "noop": (job, "name", "contracts / contract-noop"),
        "missing-step": (job, "steps", job["steps"][:-1]),
        "missing-checkout": (
            job,
            "steps",
            [step for step in job["steps"] if "actions/checkout" not in step["name"]],
        ),
        "wrong-job-head": (job, "head_sha", "e" * 40),
        "wrong-job-attempt": (job, "run_attempt", 3),
        "duplicate-step": (job, "steps", [*job["steps"], deepcopy(job["steps"][0])]),
        "too-many-steps": (job, "steps", job["steps"] * 21),
        "short-page": (page, "total_count", 101),
        "wrong-total": (page, "total_count", 0),
    }
    if change == "duplicate-job":
        page["jobs"].append({**deepcopy(job), "id": 999})
        page["total_count"] += 1
    else:
        target, key, value = mutations[change]
        target[key] = value
    result = _collect_ci(api, invocation, reader)
    check_id = "ledger.python.ruff-check"
    assert not any(
        receipt.check_id == check_id and receipt.status == "success" for receipt in result.receipts
    )
    if change == "failed":
        assert any(
            receipt.check_id == check_id and receipt.status == "failed"
            for receipt in result.receipts
        )
    elif change not in {"skipped", "noop", "missing-step", "missing-checkout"}:
        assert result.gaps


def test_ci_collection_absent_run_leaves_local_checks_uncovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Return no CI coverage when the bounded run list is empty."""
    api = _api()
    _install_nightly_profile(monkeypatch)
    invocation, reader = _ci_collection_fixture(tmp_path)
    listing = next(value for key, value in reader.graph.items() if "/workflows/ci.yml/runs?" in key)
    listing.update(total_count=0, workflow_runs=[])
    result = _collect_ci(api, invocation, reader)
    assert result.receipts == ()
    assert result.gaps == ()


def test_ci_collection_preserves_nightly_checks_for_local_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordinary PR evidence must not cover a check the profile marks as local-only."""
    api = _api()
    _install_nightly_profile(monkeypatch)
    invocation, reader = _ci_collection_fixture(tmp_path)
    result = _collect_ci(api, invocation, reader)
    assert not result.gaps
    assert len(invocation.plan.checks) > len(invocation.check_ids)
    assert {receipt.check_id for receipt in result.receipts} == set(invocation.check_ids)
    assert NIGHTLY_CHECK_ID in {check.check_id for check in invocation.plan.checks}
    assert NIGHTLY_CHECK_ID not in {receipt.check_id for receipt in result.receipts}


@pytest.mark.parametrize(
    ("kind", "count", "accepted"),
    [
        ("runs", 100, True),
        ("runs", 101, True),
        ("runs", 500, True),
        ("runs", 501, False),
        ("jobs", 100, True),
        ("jobs", 101, True),
        ("jobs", 1000, True),
        ("jobs", 1001, False),
    ],
)
def test_ci_collection_pagination_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, count: int, accepted: bool
) -> None:
    """Exhaust each bounded list without accepting a prefix or requesting excess pages."""
    api = _api()
    _install_nightly_profile(monkeypatch)
    invocation, reader = _ci_collection_fixture(tmp_path)
    run_key = next(key for key in reader.graph if "/workflows/ci.yml/runs?" in key)
    jobs_key = (
        f"repos/{REPOSITORY}/actions/runs/{RUN_ID}/attempts/{ATTEMPT}/jobs?per_page=100&page=1"
    )
    if kind == "runs":
        records: list[dict[str, Any]] = [
            {"id": number + 1, "run_attempt": ATTEMPT} for number in range(count)
        ]
        selected = count
        reader.graph[f"repos/{REPOSITORY}/actions/runs/{selected}"] = {
            **reader.graph[f"repos/{REPOSITORY}/actions/runs/{RUN_ID}"],
            "id": selected,
        }
        new_jobs = deepcopy(reader.graph[jobs_key])
        for job in new_jobs["jobs"]:
            job["run_id"] = selected
        reader.graph[
            f"repos/{REPOSITORY}/actions/runs/{selected}/attempts/{ATTEMPT}/jobs?per_page=100&page=1"
        ] = new_jobs
        key, member, limit = run_key, "workflow_runs", 5
    else:
        records = deepcopy(reader.graph[jobs_key]["jobs"])
        records.extend(
            {**deepcopy(records[-1]), "id": number + 1, "name": f"unselected-{number}"}
            for number in range(len(records), count)
        )
        key, member, limit = jobs_key, "jobs", 10
    base = key.rsplit("=", 1)[0]
    for offset in range(0, count, 100):
        reader.graph[f"{base}={offset // 100 + 1}"] = {
            "total_count": count,
            member: records[offset : offset + 100],
        }
    result = _collect_ci(api, invocation, reader)
    if accepted:
        assert not result.gaps
        assert {receipt.check_id for receipt in result.receipts} == set(invocation.check_ids)
        pages = [call for call in reader.calls if call.startswith(base)]
        assert len(pages) == ((count + 99) // 100) * (1 if kind == "runs" else 2)
    else:
        assert result.gaps and not result.receipts
    assert all(
        int(call.rsplit("=", 1)[-1]) <= limit for call in reader.calls if call.startswith(base)
    )


@pytest.mark.parametrize(
    "code",
    ["ci_request_deadline", "ci_request_cancelled", "ci_rate_limit", "ci_response_byte_limit"],
)
def test_ci_collection_retains_provider_failures_as_terminal_gaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    """Return a typed gap without retrying a failed provider read."""
    api = _api()
    _install_nightly_profile(monkeypatch)
    invocation, reader = _ci_collection_fixture(tmp_path)
    calls: list[str] = []

    def fail(endpoint: str) -> dict[str, Any]:
        calls.append(endpoint)
        raise api.ValidationCIReadError(code)

    reader.read = fail  # type: ignore[method-assign]
    result = _collect_ci(api, invocation, reader)
    assert result.receipts == ()
    assert len(result.gaps) == 1 and result.gaps[0].reason == code
    assert len(calls) == 1


# --------------------------------------------------------------------------
# Registry admission
# --------------------------------------------------------------------------


def test_registry_ships_no_profile() -> None:
    """The base package supplies no repository profile."""
    from hephaestus.automation.pipeline.repository_validation_profiles import (
        PROFILE_ENTRY_POINT_GROUP,
        repository_validation_profiles,
    )

    assert PROFILE_ENTRY_POINT_GROUP == "hephaestus.repository_validation_profiles"
    assert dict(repository_validation_profiles()) == {}


def test_require_profile_rejects_an_unsupported_repository() -> None:
    """Reject a repository that no installed profile owns."""
    from hephaestus.automation.pipeline.repository_validation_profiles import (
        require_repository_validation_profile,
    )

    with pytest.raises(ValueError, match="unsupported"):
        require_repository_validation_profile(REPOSITORY)


def test_profile_rejects_an_incomplete_contract() -> None:
    """Reject a profile that cannot carry a complete reviewed identity."""
    from dataclasses import replace

    profile = build_profile()
    with pytest.raises(ValueError, match="incomplete"):
        object.__setattr__(profile, "local_check_ids", "not callable")
        replace(profile)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("repository", "not-a-repository", "repository name"),
        ("profile_id", "Not Valid", "profile identifier"),
        ("profile_digest", "short", "profile digest"),
        ("ci_workflow_path", "scripts/run.yml", "workflow path"),
        ("ci_referenced_workflows", frozenset(), "referenced workflows"),
        ("runtime_slug", "Not Valid", "runtime slug"),
        ("runtime_executables", frozenset({"tool/ruff"}), "runtime programs"),
        ("controls", (), "control inventory"),
    ],
)
def test_profile_rejects_each_invalid_member(field: str, value: Any, message: str) -> None:
    """Reject one malformed profile member at construction."""
    from dataclasses import replace

    profile = build_profile()
    with pytest.raises(ValueError, match=message):
        replace(profile, **{field: value})


def test_plan_profile_binding_requires_an_identical_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bind a frozen plan to the exact installed profile identity."""
    from hephaestus.automation.pipeline.repository_validation_profiles import (
        plan_validation_checks,
        plan_validation_profile,
    )

    profile = _install_nightly_profile(monkeypatch)
    plan = RepositoryValidationPlan(
        repository=REPOSITORY,
        issue_number=ISSUE_NUMBER,
        pr_number=ISSUE_NUMBER,
        reviewed_head=HEAD,
        reviewed_base=BASE,
        diff_base_sha=BASE,
        source_workspace=_fixture_workspace(),
        changes=(("M", "src/ledger/example.py"),),
        profile_id=profile.profile_id,
        profile_digest=profile.profile_digest,
        checks=profile.select((("M", "src/ledger/example.py"),)),
    )
    assert plan_validation_profile(plan) is profile
    assert plan_validation_checks(plan) == plan.checks
    from dataclasses import replace

    with pytest.raises(ValueError, match="digest"):
        plan_validation_profile(replace(plan, profile_digest="f" * 64))


def _fixture_workspace() -> Any:
    """Return a workspace binding that needs no filesystem for plan binding."""
    return WorkspaceBinding.source(
        cwd=Path("/nonexistent/ledger"),
        reusable_root=Path("/nonexistent"),
        repository=REPOSITORY,
        ownership_key="ledger-review-1",
        item_number=ISSUE_NUMBER,
        lane=SourceLane.REVIEW,
        revision=HEAD,
        generation=1,
        detached=True,
    )


def test_module_is_importable_without_a_profile() -> None:
    """Import the adapter with no installed profile."""
    module = importlib.import_module("hephaestus.automation.pipeline_github_review_validation")
    assert hasattr(module, "collect_repository_validation_ci")
    assert hasattr(module, "validation_plan_for_workspace")
    assert hasattr(module, "ValidationCIReader")
    assert hasattr(module, "ValidationCICollection")

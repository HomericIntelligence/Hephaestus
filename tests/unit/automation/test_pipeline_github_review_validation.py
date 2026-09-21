"""Check fixed Comet commands and rejection of changed controls."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import runpy
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/comet-review-validation"
MANIFEST = json.loads((FIXTURES / "current-manifest.json").read_text())
CONTROL_PATHS = tuple(entry["path"] for entry in MANIFEST["entries"])


def _api() -> ModuleType:
    name = "hephaestus.automation.pipeline_github_review_validation"
    assert importlib.util.find_spec(name) is not None, "The Comet source profile is not available."
    return importlib.import_module(name)


def _controls() -> dict[str, tuple[int, bytes]]:
    return {
        entry["path"]: (
            int(entry["mode"], 8),
            (FIXTURES / "current" / entry["path"]).read_bytes(),
        )
        for entry in MANIFEST["entries"]
    }


def test_admit_complete_current_controls() -> None:
    """Select the exact four Python command vectors."""
    api = _api()
    profile = api.admit_comet_controls(_controls(), CONTROL_PATHS)
    checks = api.comet_validation_checks(profile, (("M", "tests/test_pool.py"),))
    assert {check.check_id: check.argv for check in checks} == {
        "comet.python.ruff-format": (
            "uv",
            "run",
            "--locked",
            "--extra",
            "dev",
            "ruff",
            "format",
            "--check",
            ".",
        ),
        "comet.python.ruff-check": (
            "uv",
            "run",
            "--locked",
            "--extra",
            "dev",
            "ruff",
            "check",
            ".",
        ),
        "comet.python.ty-check": ("uv", "run", "--locked", "--extra", "dev", "ty", "check"),
        "comet.python.pr-tests": (
            "uv",
            "run",
            "--locked",
            "--extra",
            "dev",
            "python",
            "scripts/ci/run-test-tier.py",
            "pr",
        ),
    }
    assert all(len(check.source_digests) == len(CONTROL_PATHS) for check in checks)


def test_admit_controls_with_the_complete_current_path_inventory() -> None:
    """Admit existing configuration from the full tracked inventory."""
    api = _api()
    paths = tuple(json.loads((FIXTURES / "current-paths.json").read_text()))
    assert len(paths) > len(CONTROL_PATHS)
    assert api.admit_comet_controls(_controls(), paths) == api.COMET_PROFILE_ID


@pytest.mark.parametrize("path", CONTROL_PATHS)
def test_reject_each_changed_control(path: str) -> None:
    """Reject one changed byte in each admitted control."""
    api = _api()
    controls = _controls()
    mode, data = controls[path]
    controls[path] = (mode, data + b"\n")
    with pytest.raises(ValueError, match="control"):
        api.admit_comet_controls(controls, CONTROL_PATHS)


@pytest.mark.parametrize("mode", [0o120000, 0o160000, 0o040000, 0o100755])
def test_reject_wrong_control_mode(mode: int) -> None:
    """Reject a control with a different Git file mode."""
    api = _api()
    controls = _controls()
    controls["pyproject.toml"] = (mode, controls["pyproject.toml"][1])
    with pytest.raises(ValueError, match="control"):
        api.admit_comet_controls(controls, CONTROL_PATHS)


@pytest.mark.parametrize(
    "path",
    [
        "ruff.toml",
        "src/pyproject.toml",
        "tests/unit/conftest.py",
        "sitecustomize.py",
        "scripts/yaml.py",
        "scripts/ci/yaml.py",
        "scripts/ci/json.py",
        "scripts/ci/json/__init__.py",
        "scripts/ci/json.cpython-312-x86_64-linux-gnu.so",
        "scripts/ci/sqlite3.py",
        "scripts/pydantic/__init__.py",
        "src/pytest/__init__.py",
        ".github/actions/new/action.yml",
    ],
)
def test_reject_new_discovery_control(path: str) -> None:
    """Reject new configuration and modules that can replace tools."""
    api = _api()
    with pytest.raises(ValueError, match="control"):
        api.admit_comet_controls(_controls(), (*CONTROL_PATHS, path))


def test_payload_changes_do_not_require_a_new_profile() -> None:
    """Keep ordinary source and test payloads outside the frozen controls."""
    api = _api()
    expected = api.admit_comet_controls(_controls(), CONTROL_PATHS)
    actual = api.admit_comet_controls(
        _controls(), (*CONTROL_PATHS, "src/comet/pool.py", "tests/test_pool.py", "docs/new.md")
    )
    assert actual == expected


@pytest.mark.parametrize(
    ("path", "required", "ci_absent", "local_absent"),
    [
        (
            "src/comet/pool.py",
            {"control-deployment-contracts", "viewer-deployment-contracts"},
            {"control-deployment-contracts", "viewer-deployment-contracts"},
            set(),
        ),
        ("tests/test_ci_workflows.py", {"workflow-contracts"}, set(), {"workflow-contracts"}),
        ("tests/test_glm53_image.py", {"sglang-glm53-image-contracts"}, set(), set()),
        (
            "tests/test_atomic_deploy.py",
            {"control-deployment-contracts"},
            {"control-deployment-contracts"},
            set(),
        ),
    ],
)
def test_keep_applicable_checks_when_one_route_cannot_cover_them(
    path: str, required: set[str], ci_absent: set[str], local_absent: set[str]
) -> None:
    """Retain applicable checks when CI or local execution cannot cover them."""
    api = _api()
    profile = api.admit_comet_controls(_controls(), CONTROL_PATHS)
    checks = api.comet_validation_checks(profile, (("M", path),))
    selected = {check.check_id for check in checks}
    assert {f"comet.contract.{name}" for name in required} <= selected
    assert set(api.comet_ci_check_ids(checks)) == selected - {
        f"comet.contract.{name}" for name in ci_absent
    }
    assert set(api.comet_local_check_ids(checks)) == selected - {
        f"comet.contract.{name}" for name in local_absent
    }


@pytest.mark.parametrize(
    "changes",
    [
        (("R", "tests/test_pool.py"),),
        (("M", "../pyproject.toml"),),
        (("M", "tests/test_pool.py"), ("D", "tests/test_pool.py")),
        (("A", ".gitmodules"),),
        (("A", "CHANGELOG.md"),),
    ],
)
def test_reject_unsafe_change_selection(changes: Any) -> None:
    """Reject unsafe records before selecting a command."""
    api = _api()
    profile = api.admit_comet_controls(_controls(), CONTROL_PATHS)
    with pytest.raises(ValueError):
        api.comet_validation_checks(profile, changes)


def test_deletion_of_a_forbidden_path_is_not_an_admission_error() -> None:
    """Permit removal of a forbidden input without inventing a check."""
    api = _api()
    profile = api.admit_comet_controls(_controls(), CONTROL_PATHS)
    assert api.comet_validation_checks(profile, (("D", ".gitmodules"),)) == ()


def _frozen_selector(root: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Load the frozen selector with its own policy dependency."""
    import importlib.util
    import sys

    policy_path = root / "scripts/k2_ci_policy.py"
    if policy_path.is_file():
        spec = importlib.util.spec_from_file_location("k2_ci_policy", policy_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, "k2_ci_policy", module)
        spec.loader.exec_module(module)
    return runpy.run_path(str(root / "scripts/check_deployed_inputs.py"))


@pytest.mark.parametrize("version", ["current", "historical", "fe5a67d", "7c2772e"])
def test_contract_selection_matches_the_frozen_repository_selector(
    version: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compare tracked paths and policy edges with each frozen source selector."""
    api = _api()
    controls = {
        "current": _controls,
        "historical": lambda: _historical_controls()[0],
        "fe5a67d": _fe5a67d_controls,
        "7c2772e": _7c2772e_controls,
    }[version]()
    profile = api.admit_comet_controls(controls, tuple(controls))
    root = FIXTURES / ("historical/controls" if version == "historical" else version)
    source = _frozen_selector(root, monkeypatch)
    inventory = yaml.safe_load((root / "deployment/deployed-inputs.yaml").read_text())
    contracts = {
        entry["id"] for entry in inventory["validators"] if entry["kind"] == "repository-contract"
    }
    inventory_version = "current" if version == "historical" else version
    paths = set(json.loads((FIXTURES / f"{inventory_version}-paths.json").read_text()))
    for policy in [*inventory["policies"], *source["CI_POLICIES"]]:
        for pattern in [*policy["include"], *policy.get("exclude", [])]:
            paths.add(
                pattern.removesuffix("**") + "fixture.py" if pattern.endswith("/**") else pattern
            )
    paths.update(source["CI_FORBIDDEN_PATHS"])
    paths.update({"CHANGELOG.md", "changes/fixture.md", "schema/change-fragment.schema.json"})
    for status in ("M", "D"):
        for path in sorted(paths):
            deleted = {path} if status == "D" else set()
            try:
                production = source["classify_paths"](inventory, [path], deleted_paths=deleted)
                ci = source["classify_ci_paths"](inventory, [path], deleted_paths=deleted)
            except ValueError:
                with pytest.raises(ValueError):
                    api.comet_validation_checks(profile, ((status, path),))
                continue
            expected = (set(production["validators"]) | set(ci["validators"])) & contracts
            actual = {
                check.check_id.removeprefix("comet.contract.")
                for check in api.comet_validation_checks(profile, ((status, path),))
                if check.check_id.startswith("comet.contract.")
            }
            assert actual == expected, (status, path)


def _source_plan_api() -> Any:
    api = _api()
    assert hasattr(api, "comet_plan_for_workspace"), "Immutable Comet source collection is missing."
    return api.comet_plan_for_workspace


def _source_fixture(tmp_path: Path) -> tuple[Any, str, str]:
    from hephaestus.agents.workspace import SourceLane, WorkspaceBinding
    from tests.unit.automation.pipeline.stages.test_pr_review_comet_validation import (
        _comet_checkout,
    )

    root = tmp_path / "source"
    base, head = _comet_checkout(root)
    workspace = WorkspaceBinding.source(
        cwd=root,
        reusable_root=tmp_path,
        repository="LLM360/comet",
        ownership_key="comet-review-1200",
        item_number=1200,
        lane=SourceLane.REVIEW,
        revision=head,
        generation=1,
        detached=True,
    )
    return workspace, base, head


def test_source_plan_reads_committed_controls(tmp_path: Path) -> None:
    """Use immutable controls even when worktree copies differ."""
    workspace, base, head = _source_fixture(tmp_path)
    (workspace.cwd / "pyproject.toml").write_text("uncommitted replacement\n")
    plan = _source_plan_api()(
        workspace, issue_number=1200, pr_number=1200, reviewed_base=base, timeout_s=30
    )
    assert plan.reviewed_head == head
    assert plan.reviewed_base == base
    assert plan.source_workspace == workspace
    assert plan.changes == (("M", "src/comet/example.py"),)
    assert {check.check_id for check in plan.checks} >= {
        "comet.python.ruff-format",
        "comet.python.ruff-check",
        "comet.python.ty-check",
        "comet.python.pr-tests",
    }


def test_source_plan_retains_target_base_and_real_statuses(tmp_path: Path) -> None:
    """Exclude target-only changes and preserve additions and deletions."""
    from dataclasses import replace

    from tests.unit.automation.pipeline.stages.test_pr_review_comet_validation import _git

    workspace, branchpoint, _ = _source_fixture(tmp_path)
    root = workspace.cwd
    (root / "src/comet/added.py").write_text("VALUE = 3\n")
    (root / "src/comet/example.py").unlink()
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "Add and delete reviewed files.")
    head = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "--detach", branchpoint)
    (root / "src/comet/target_only.py").write_text("VALUE = 4\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "Advance the target branch.")
    target = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "--detach", head)
    plan = _source_plan_api()(
        replace(workspace, revision=head),
        issue_number=1200,
        pr_number=1200,
        reviewed_base=target,
        timeout_s=30,
    )
    assert plan.reviewed_base == target != branchpoint
    assert getattr(plan, "diff_base_sha", None) == branchpoint
    assert plan.changes == (("A", "src/comet/added.py"), ("D", "src/comet/example.py"))


@pytest.mark.parametrize("revision", ["head", "base"])
def test_source_plan_rejects_changed_committed_control(tmp_path: Path, revision: str) -> None:
    """Reject a changed control in either admitted commit."""
    from dataclasses import replace

    from tests.unit.automation.pipeline.stages.test_pr_review_comet_validation import _git

    workspace, base, head = _source_fixture(tmp_path)
    root = workspace.cwd
    if revision == "base":
        _git(root, "checkout", "--detach", base)
    with (root / "pyproject.toml").open("a") as stream:
        stream.write("\n# Changed committed control.\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "Change a control.")
    changed = _git(root, "rev-parse", "HEAD")
    if revision == "base":
        base = changed
        _git(root, "checkout", "--detach", head)
    else:
        workspace = replace(workspace, revision=changed)
    collect = _source_plan_api()
    with pytest.raises(ValueError, match="control"):
        collect(workspace, issue_number=1200, pr_number=1200, reviewed_base=base, timeout_s=30)


def test_source_plan_rejects_wrong_repository_before_git(tmp_path: Path) -> None:
    """Reject a foreign binding without reading its source."""
    from dataclasses import replace
    from unittest.mock import patch

    workspace, base, _ = _source_fixture(tmp_path)
    collect = _source_plan_api()
    with patch("subprocess.Popen", side_effect=AssertionError("Git must not run.")):
        with pytest.raises(ValueError, match=r"binding|repository"):
            collect(
                replace(workspace, repository="Other/comet"),
                issue_number=1200,
                pr_number=1200,
                reviewed_base=base,
                timeout_s=30,
            )


def _reader_api() -> ModuleType:
    api = _api()
    assert hasattr(api, "CometCIReader"), "The bounded Comet CI reader is missing."
    return api


def test_ci_reader_uses_one_deadline_and_fixed_read_only_command(monkeypatch: Any) -> None:
    """Keep every page inside the original deadline and output cap."""
    import subprocess

    api = _reader_api()
    clock = [10.0]
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(api, "trusted_gh_executable", lambda: "/trusted/gh")

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, '{"ok":true}', "")

    monkeypatch.setattr(api, "run_subprocess", run)
    reader = api.CometCIReader(deadline_s=1000)
    assert reader.read("repos/LLM360/comet/pulls/1733") == {"ok": True}
    clock[0] = 30
    assert reader.read("repos/LLM360/comet/actions/runs/123") == {"ok": True}
    assert [call[1]["timeout"] for call in calls] == [120, 100]
    assert calls[0][0] == [
        "/trusted/gh",
        "api",
        "--hostname",
        "github.com",
        "--method",
        "GET",
        "repos/LLM360/comet/pulls/1733",
    ]
    assert all(call[1]["max_output_bytes"] == 4 * 1024 * 1024 for call in calls)
    assert all(call[1]["track_process_group"] and not call[1]["log_on_error"] for call in calls)
    clock[0] = 131
    with pytest.raises(api.CometCIReadError, match="deadline"):
        reader.read("repos/LLM360/comet/pulls/1733")
    assert len(calls) == 2


@pytest.mark.parametrize(
    "endpoint",
    [
        "repos/other/comet/pulls/1",
        "https://example.invalid/repos/LLM360/comet/pulls/1",
        "repos/LLM360/comet/actions/runs/1/rerun",
        "repos/LLM360/comet/pulls/1/comments",
        "repos/LLM360/comet/git/trees/main?recursive=1",
        "repos/LLM360/comet/actions/runs/1/attempts/1/jobs?per_page=100&page=11",
    ],
)
def test_ci_reader_rejects_nonprofile_endpoints(monkeypatch: Any, endpoint: str) -> None:
    """Reject writes, foreign targets, mutable refs, and excess pages before execution."""
    api = _reader_api()
    monkeypatch.setattr(api, "trusted_gh_executable", lambda: "/trusted/gh")
    calls: list[object] = []
    monkeypatch.setattr(api, "run_subprocess", lambda *args, **kwargs: calls.append(args))
    reader = api.CometCIReader(deadline_s=api.time.monotonic() + 60)
    with pytest.raises(api.CometCIReadError):
        reader.read(endpoint)
    assert calls == []


@pytest.mark.parametrize(
    "body", ['{"id":1,"id":2}', '{"value":NaN}', '{"value":Infinity}', "{} trailing"]
)
def test_ci_reader_rejects_ambiguous_json(monkeypatch: Any, body: str) -> None:
    """Reject duplicate members, nonfinite numbers, and incomplete JSON."""
    import subprocess

    api = _reader_api()
    monkeypatch.setattr(api, "trusted_gh_executable", lambda: "/trusted/gh")
    monkeypatch.setattr(
        api, "run_subprocess", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, body, "")
    )
    reader = api.CometCIReader(deadline_s=api.time.monotonic() + 60)
    with pytest.raises(api.CometCIReadError, match="json"):
        reader.read("repos/LLM360/comet/pulls/1733")


def test_ci_reader_aggregate_limit_spans_all_reads(monkeypatch: Any) -> None:
    """Accept the exact JSON budget and reject another read before execution."""
    import subprocess

    api = _reader_api()
    monkeypatch.setattr(api, "trusted_gh_executable", lambda: "/trusted/gh")
    calls: list[int] = []
    body = "{}" + " " * (4 * 1024 * 1024 - 2)

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(kwargs["max_output_bytes"])
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(api, "run_subprocess", run)
    reader = api.CometCIReader(deadline_s=api.time.monotonic() + 60)
    for _ in range(8):
        assert reader.read("repos/LLM360/comet/pulls/1733") == {}
    with pytest.raises(api.CometCIReadError, match="aggregate"):
        reader.read("repos/LLM360/comet/pulls/1733")
    assert len(calls) == 8


@pytest.mark.parametrize(
    ("count", "endpoint"),
    [(600, "repos/LLM360/comet/pulls/1733"), (512, "repos/LLM360/comet/git/blobs/" + "a" * 40)],
)
def test_ci_reader_enforces_request_and_object_budgets(
    monkeypatch: Any, count: int, endpoint: str
) -> None:
    """Stop before a request would exceed its complete-attempt budget."""
    import subprocess

    api = _reader_api()
    monkeypatch.setattr(api, "trusted_gh_executable", lambda: "/trusted/gh")
    calls: list[str] = []

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(api, "run_subprocess", run)
    reader = api.CometCIReader(deadline_s=api.time.monotonic() + 60)
    for _ in range(count):
        reader.read(endpoint)
    with pytest.raises(api.CometCIReadError, match="limit"):
        reader.read(endpoint)
    assert len(calls) == count


def test_ci_reader_cancelled_request_starts_no_child(monkeypatch: Any) -> None:
    """Apply cancellation before process creation."""
    import threading

    api = _reader_api()
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(api, "trusted_gh_executable", lambda: "/trusted/gh")
    calls: list[object] = []
    monkeypatch.setattr(api, "run_subprocess", lambda *args, **kwargs: calls.append(args))
    reader = api.CometCIReader(deadline_s=api.time.monotonic() + 60, shutdown=stop)
    with pytest.raises(api.CometCIReadError, match="cancelled"):
        reader.read("repos/LLM360/comet/pulls/1733")
    assert calls == []


@pytest.mark.skipif(os.name != "posix", reason="This test checks POSIX process cleanup.")
def test_ci_reader_terminates_a_child_that_exceeds_output_limit(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Prove the reader terminates excess output during capture."""
    import sys
    import time

    api = _reader_api()
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
    reader = api.CometCIReader(deadline_s=time.monotonic() + 10)
    with pytest.raises(api.CometCIReadError, match="response_byte_limit"):
        reader.read("repos/LLM360/comet/pulls/1733")
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
    monkeypatch: Any, interruption: str
) -> None:
    """Reject evidence if parsing consumes the deadline or receives cancellation."""
    import subprocess
    import threading

    api = _reader_api()
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
    reader = api.CometCIReader(deadline_s=160.0, shutdown=stop)
    with pytest.raises(api.CometCIReadError, match=interruption):
        reader.read("repos/LLM360/comet/pulls/1733")
    stop.clear()
    now[0] = 100.0
    with pytest.raises(api.CometCIReadError, match=interruption):
        reader.read("repos/LLM360/comet/pulls/1733")
    assert len(calls) == 1


def _ci_identity_fixture() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Make synthetic provider data for identity checks, without source evidence."""
    from copy import deepcopy

    repo = {"id": 1308303366, "full_name": "LLM360/comet", "fork": False}
    pr = {
        "number": 1733,
        "state": "open",
        "head": {"sha": "a" * 40, "ref": "codex/repair", "repo": deepcopy(repo)},
        "base": {"sha": "b" * 40, "ref": "main", "repo": deepcopy(repo)},
    }
    run = {
        "id": 123,
        "run_attempt": 2,
        "repository": deepcopy(repo),
        "head_repository": deepcopy(repo),
        "head_sha": "a" * 40,
        "head_branch": "codex/repair",
        "path": ".github/workflows/ci.yml",
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "referenced_workflows": [
            {
                "path": f"LLM360/comet/.github/workflows/{name}.yml@" + "c" * 40,
                "sha": "c" * 40,
                "ref": "refs/pull/1733/merge",
            }
            for name in ("contracts", "secrets", "docs-check", "postgres-tests", "build-image")
        ],
    }
    merge = {
        "sha": "c" * 40,
        "parents": [{"sha": "b" * 40}, {"sha": "a" * 40}],
        "tree": {"sha": "d" * 40},
    }
    return pr, run, merge


def _ci_identity(api: ModuleType, data: tuple[dict[str, Any], ...]) -> Any:
    return api.comet_ci_identity(
        *data,
        pr_number=1733,
        reviewed_head="a" * 40,
        reviewed_base="b" * 40,
        head_branch="codex/repair",
    )


def test_ci_identity_binds_merge_parents_without_run_pull_requests() -> None:
    """Bind the immutable merge witness without inferring a PR from the run list."""
    api = _api()
    data = _ci_identity_fixture()
    identity = _ci_identity(api, data)
    assert identity.repository_id == 1308303366
    assert (identity.run_id, identity.run_attempt) == (123, 2)
    assert identity.merge_sha == "c" * 40
    assert identity.merge_tree == "d" * 40
    data[1]["referenced_workflows"].reverse()
    data[1]["pull_requests"] = []
    assert _ci_identity(api, data) == identity
    data[1]["run_attempt"] = 3
    assert _ci_identity(api, data) != identity


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ((0, "number"), 1734),
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
        ((0, "head", "repo", "full_name"), "other/comet"),
        ((1, "repository", "id"), 9),
        ((1, "head_repository", "id"), 9),
        ((1, "repository", "full_name"), "other/comet"),
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
        ((1, "referenced_workflows", 0, "ref"), "refs/pull/1734/merge"),
        (
            (1, "referenced_workflows", 0, "path"),
            "other/comet/.github/workflows/contracts.yml@" + "c" * 40,
        ),
        ((2, "sha"), "e" * 40),
        ((2, "tree", "sha"), "short"),
        ((2, "parents"), [{"sha": "a" * 40}, {"sha": "b" * 40}]),
        ((2, "parents"), [{"sha": "b" * 40}]),
        ((2, "parents", 1, "sha"), "e" * 40),
    ],
)
def test_ci_identity_rejects_mismatched_provider_facts(path: tuple[Any, ...], value: Any) -> None:
    """Reject a changed PR, run, repository, or immutable merge fact."""
    api = _api()
    data = _ci_identity_fixture()
    owner: Any = data
    for key in path[:-1]:
        owner = owner[key]
    owner[path[-1]] = value
    with pytest.raises(api.CometCIReadError):
        _ci_identity(api, data)


@pytest.mark.parametrize("change", ["missing", "duplicate", "extra", "mixed-sha"])
def test_ci_identity_requires_exact_workflow_witnesses(change: str) -> None:
    """Require one witness per local workflow and one shared immutable commit."""
    from copy import deepcopy

    api = _api()
    data = _ci_identity_fixture()
    witnesses = data[1]["referenced_workflows"]
    if change == "missing":
        witnesses.pop()
    elif change == "duplicate":
        witnesses[-1] = deepcopy(witnesses[0])
    elif change == "extra":
        witnesses.append(deepcopy(witnesses[0]))
    else:
        witnesses[0]["sha"] = "e" * 40
        witnesses[0]["path"] = witnesses[0]["path"].split("@")[0] + "@" + "e" * 40
    with pytest.raises(api.CometCIReadError):
        _ci_identity(api, data)


def test_ci_identity_retains_failure_and_incomplete_run_facts() -> None:
    """Keep failed and incomplete runs distinct from successful runs."""
    api = _api()
    data = _ci_identity_fixture()
    success = _ci_identity(api, data)
    data[1]["conclusion"] = "failure"
    failed = _ci_identity(api, data)
    assert failed != success
    assert failed.conclusion == "failure"
    data[1]["status"] = "in_progress"
    data[1]["conclusion"] = None
    pending = _ci_identity(api, data)
    assert pending != failed
    assert pending.status == "in_progress"
    assert pending.conclusion is None


def _ci_control_graph(
    controls: dict[str, tuple[int, bytes]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Make an immutable API graph from the exact current control fixture."""
    import base64
    import hashlib

    graph: dict[str, dict[str, Any]] = {}
    entries: list[dict[str, Any]] = []
    directories: set[str] = set()
    for path, (mode, content) in (_controls() if controls is None else controls).items():
        sha = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
        entries.append(
            {"path": path, "mode": f"{mode:o}", "type": "blob", "sha": sha, "size": len(content)}
        )
        for parent in Path(path).parents:
            if str(parent) != ".":
                directories.add(parent.as_posix())
        graph[f"repos/LLM360/comet/git/blobs/{sha}"] = {
            "sha": sha,
            "size": len(content),
            "encoding": "base64",
            "content": base64.b64encode(content).decode(),
        }
    entries.extend(
        {"path": path, "mode": "040000", "type": "tree", "sha": "e" * 40}
        for path in sorted(directories)
    )
    graph["repos/LLM360/comet/git/trees/" + "d" * 40 + "?recursive=1"] = {
        "sha": "d" * 40,
        "truncated": False,
        "tree": entries,
    }
    for sha in ("a", "b", "c"):
        graph["repos/LLM360/comet/git/commits/" + sha * 40] = {
            "sha": sha * 40,
            "tree": {"sha": "d" * 40},
        }
    return graph


class _ControlGraphReader:
    def __init__(self, graph: dict[str, dict[str, Any]]) -> None:
        self.graph = graph
        self.calls: list[str] = []

    def _remaining(self) -> float:
        return 60.0

    def read(self, endpoint: str) -> dict[str, Any]:
        from copy import deepcopy

        self.calls.append(endpoint)
        return deepcopy(self.graph[endpoint])


def test_ci_controls_admit_head_base_and_merge_with_request_local_cache() -> None:
    """Read immutable source controls at all three commits without mutable refs."""
    api = _api()
    reader = _ControlGraphReader(_ci_control_graph())
    proof = api.CometCIControls(reader)
    for sha in ("a", "b", "c"):
        assert proof.admit(sha * 40, expected_tree="d" * 40) == api.COMET_PROFILE_ID
    assert len(reader.calls) == 3 + 1 + len(CONTROL_PATHS)
    before = list(reader.calls)
    assert proof.admit("c" * 40, expected_tree="d" * 40) == api.COMET_PROFILE_ID
    assert reader.calls == before
    fresh = _ControlGraphReader(_ci_control_graph())
    api.CometCIControls(fresh).admit("c" * 40, expected_tree="d" * 40)
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
    graph = _ci_control_graph()
    commit = graph["repos/LLM360/comet/git/commits/" + "a" * 40]
    tree = graph["repos/LLM360/comet/git/trees/" + "d" * 40 + "?recursive=1"]
    entry = tree["tree"][0]
    blob = graph["repos/LLM360/comet/git/blobs/" + entry["sha"]]
    expected_tree = "f" * 40 if change == "expected-tree" else "d" * 40
    mutations = {
        "commit-sha": (commit, "sha", "f" * 40),
        "tree-sha": (tree, "sha", "f" * 40),
        "truncated": (tree, "truncated", True),
        "missing-control": (tree, "tree", tree["tree"][1:]),
        "duplicate-path": (tree, "tree", [*tree["tree"], dict(entry)]),
        "unsafe-path": (entry, "path", "../outside"),
        "nonregular": (entry, "mode", "120000"),
        "new-control": (tree, "tree", [*tree["tree"], {**entry, "path": "sitecustomize.py"}]),
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
    with pytest.raises(api.CometCIReadError):
        api.CometCIControls(_ControlGraphReader(graph)).admit("a" * 40, expected_tree=expected_tree)


def _ci_collection_fixture(
    tmp_path: Path, changes: tuple[tuple[str, str], ...] = (("M", "tests/test_pool.py"),)
) -> tuple[Any, _ControlGraphReader]:
    """Make a complete synthetic CI observation with current control bytes."""
    from hephaestus.agents.workspace import SourceLane, WorkspaceBinding
    from hephaestus.automation.pipeline.repository_validation import (
        RepositoryValidationInvocation,
        RepositoryValidationPlan,
    )

    api = _api()
    checks = api.comet_validation_checks(api.COMET_PROFILE_ID, changes)
    workspace = WorkspaceBinding.source(
        cwd=tmp_path,
        reusable_root=tmp_path.parent,
        repository="LLM360/comet",
        ownership_key="ci-fixture",
        item_number=1700,
        lane=SourceLane.REVIEW,
        revision="a" * 40,
        generation=1,
        detached=True,
    )
    plan = RepositoryValidationPlan(
        repository="LLM360/comet",
        issue_number=1700,
        pr_number=1733,
        reviewed_head="a" * 40,
        reviewed_base="b" * 40,
        diff_base_sha="b" * 40,
        source_workspace=workspace,
        changes=changes,
        profile_id=api.COMET_PROFILE_ID,
        profile_digest=api.COMET_PROFILE_DIGEST,
        checks=checks,
    )
    invocation = RepositoryValidationInvocation(
        plan, 1, "f" * 32, "ci", api.comet_ci_check_ids(checks)
    )
    graph = _ci_control_graph()
    pr, run, merge = _ci_identity_fixture()
    graph["repos/LLM360/comet/pulls/1733"] = pr
    graph["repos/LLM360/comet/actions/runs/123"] = run
    graph["repos/LLM360/comet/git/commits/" + "c" * 40] = merge
    graph[
        "repos/LLM360/comet/actions/workflows/ci.yml/runs?event=pull_request&head_sha="
        + "a" * 40
        + "&per_page=100&page=1"
    ] = {"total_count": 1, "workflow_runs": [run]}
    checkout = "Run actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    setup_uv = "Run astral-sh/setup-uv@bec219d24cd3e171d82865faccec33120bb574f4"
    definitions = [
        (
            "lint",
            [
                checkout,
                setup_uv,
                "Run uv run --locked --extra dev ruff format --check .",
                "Run uv run --locked --extra dev ruff check .",
            ],
        ),
        ("typecheck", [checkout, setup_uv, "Run uv run --locked --extra dev ty check"]),
        (
            "test (3.12)",
            [checkout, setup_uv, "Install Bubblewrap", "Run the ordinary pull-request profile"],
        ),
        ("docs / docs-strict", [checkout, setup_uv, "Build site in strict mode"]),
    ]
    jobs = [
        {
            "id": index + 1,
            "run_id": 123,
            "run_attempt": 2,
            "head_sha": "a" * 40,
            "name": name,
            "status": "completed",
            "conclusion": "success",
            "steps": [
                {"number": number + 1, "name": step, "status": "completed", "conclusion": "success"}
                for number, step in enumerate(["Set up job", *steps])
            ],
        }
        for index, (name, steps) in enumerate(definitions)
    ]
    graph["repos/LLM360/comet/actions/runs/123/attempts/2/jobs?per_page=100&page=1"] = {
        "total_count": len(jobs),
        "jobs": jobs,
    }
    return invocation, _ControlGraphReader(graph)


def _collect_ci(api: ModuleType, invocation: Any, reader: Any) -> Any:
    return api.collect_comet_ci(invocation, head_branch="codex/repair", reader=reader)


def test_ci_collection_requires_two_complete_observations(tmp_path: Path) -> None:
    """Admit selected command evidence from stable jobs and immutable sources."""
    api = _api()
    invocation, reader = _ci_collection_fixture(tmp_path)
    result = _collect_ci(api, invocation, reader)
    assert not result.gaps
    assert {r.check_id for r in result.receipts} == set(invocation.check_ids)
    assert all(
        r.status == "success" and r.plan_id == invocation.plan.plan_id for r in result.receipts
    )
    assert sum("/attempts/2/jobs?" in call for call in reader.calls) == 2
    assert reader.calls.count("repos/LLM360/comet/actions/runs/123") == 4
    assert all("refs/" not in call for call in reader.calls)


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
def test_ci_collection_rejects_changed_second_observation(tmp_path: Path, change: str) -> None:
    """Never combine a first-read success with changed second-read evidence."""
    api = _api()
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
        if endpoint.endswith("/runs/123") and count[endpoint] >= 3:
            if change == "second-attempt":
                value["run_attempt"] = 3
            elif change == "second-status":
                value["conclusion"] = "failure"
        if endpoint.endswith("/pulls/1733") and count[endpoint] == 2 and change == "second-base":
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
def test_ci_collection_does_not_fill_missing_or_failed_checks(tmp_path: Path, change: str) -> None:
    """Keep failed results and reject incomplete, unrelated, or ambiguous jobs."""
    from copy import deepcopy

    api = _api()
    invocation, reader = _ci_collection_fixture(tmp_path)
    page = reader.graph["repos/LLM360/comet/actions/runs/123/attempts/2/jobs?per_page=100&page=1"]
    job = page["jobs"][0]
    mutations = {
        "failed": (job, "conclusion", "failure"),
        "skipped": (job["steps"][-1], "conclusion", "skipped"),
        "noop": (job, "name", "contracts / contract-noop"),
        "missing-step": (job, "steps", job["steps"][:-1]),
        "missing-checkout": (
            job,
            "steps",
            [s for s in job["steps"] if "actions/checkout" not in s["name"]],
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
    check_id = "comet.python.ruff-check"
    assert not any(r.check_id == check_id and r.status == "success" for r in result.receipts)
    if change == "failed":
        assert any(r.check_id == check_id and r.status == "failed" for r in result.receipts)
    elif change not in {"skipped", "noop", "missing-step", "missing-checkout"}:
        assert result.gaps


def test_ci_collection_absent_run_leaves_local_checks_uncovered(tmp_path: Path) -> None:
    """Return no CI coverage when the bounded run list is empty."""
    api = _api()
    invocation, reader = _ci_collection_fixture(tmp_path)
    listing = next(value for key, value in reader.graph.items() if "/workflows/ci.yml/runs?" in key)
    listing.update(total_count=0, workflow_runs=[])
    result = _collect_ci(api, invocation, reader)
    assert result.receipts == ()
    assert result.gaps == ()


def test_ci_collection_preserves_nightly_checks_for_local_validation(tmp_path: Path) -> None:
    """Ordinary PR evidence must not cover selected nightly contracts."""
    api = _api()
    invocation, reader = _ci_collection_fixture(tmp_path, (("M", "src/comet/pool.py"),))
    result = _collect_ci(api, invocation, reader)
    assert not result.gaps
    assert len(invocation.plan.checks) > len(invocation.check_ids)
    assert {r.check_id for r in result.receipts} == set(invocation.check_ids)
    assert all("control" not in r.check_id and "viewer" not in r.check_id for r in result.receipts)


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
    tmp_path: Path, kind: str, count: int, accepted: bool
) -> None:
    """Exhaust each bounded list without accepting a prefix or requesting excess pages."""
    from copy import deepcopy

    api = _api()
    invocation, reader = _ci_collection_fixture(tmp_path)
    run_key = next(key for key in reader.graph if "/workflows/ci.yml/runs?" in key)
    jobs_key = "repos/LLM360/comet/actions/runs/123/attempts/2/jobs?per_page=100&page=1"
    if kind == "runs":
        records: list[dict[str, Any]] = [
            {"id": number + 1, "run_attempt": 2} for number in range(count)
        ]
        selected = count
        reader.graph[f"repos/LLM360/comet/actions/runs/{selected}"] = {
            **reader.graph["repos/LLM360/comet/actions/runs/123"],
            "id": selected,
        }
        new_jobs = deepcopy(reader.graph[jobs_key])
        for job in new_jobs["jobs"]:
            job["run_id"] = selected
        reader.graph[
            f"repos/LLM360/comet/actions/runs/{selected}/attempts/2/jobs?per_page=100&page=1"
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
        assert {r.check_id for r in result.receipts} == set(invocation.check_ids)
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
    tmp_path: Path, code: str
) -> None:
    """Return a typed gap without retrying a failed provider read."""
    api = _api()
    invocation, reader = _ci_collection_fixture(tmp_path)
    calls: list[str] = []

    def fail(endpoint: str) -> dict[str, Any]:
        calls.append(endpoint)
        raise api.CometCIReadError(code)

    reader.read = fail  # type: ignore[method-assign]
    result = _collect_ci(api, invocation, reader)
    assert result.receipts == ()
    assert len(result.gaps) == 1 and result.gaps[0].reason == code
    assert len(calls) == 1


@pytest.mark.parametrize("path", ["tests/test_ci_workflows.py", "tests/test_schema_export.py"])
def test_ci_collection_covers_docs_and_selected_contract_steps(tmp_path: Path, path: str) -> None:
    """Require the selected contract step and the fixed docs build step."""
    api = _api()
    invocation, reader = _ci_collection_fixture(tmp_path, (("M", path), ("M", "docs/index.md")))
    page = reader.graph["repos/LLM360/comet/actions/runs/123/attempts/2/jobs?per_page=100&page=1"]
    validator = "workflow-contracts" if path == "tests/test_ci_workflows.py" else "schema-contracts"
    steps = [
        ("Set up job", "success"),
        ("Run actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", "success"),
        ("Verify the checked-out commit", "success"),
        ("Run astral-sh/setup-uv@bec219d24cd3e171d82865faccec33120bb574f4", "success"),
        ("Install Bubblewrap", "success" if validator == "workflow-contracts" else "skipped"),
        ("Successful no-op", "skipped"),
        ("Validate selected contract", "success"),
    ]
    page["jobs"].append(
        {
            "id": 20,
            "run_id": 123,
            "run_attempt": 2,
            "head_sha": "a" * 40,
            "name": f"contracts / {validator}",
            "status": "completed",
            "conclusion": "success",
            "steps": [
                {"number": n + 1, "name": name, "status": "completed", "conclusion": conclusion}
                for n, (name, conclusion) in enumerate(steps)
            ],
        }
    )
    page["total_count"] += 1
    result = _collect_ci(api, invocation, reader)
    assert not result.gaps
    assert {r.check_id for r in result.receipts} == set(invocation.check_ids)
    assert all(r.status == "success" for r in result.receipts)
    assert "comet.docs.strict" in invocation.check_ids
    assert f"comet.contract.{validator}" in invocation.check_ids


def _historical_controls() -> tuple[dict[str, tuple[int, bytes]], dict[str, Any]]:
    """Read retained historical control bytes and their immutable identities."""
    root = FIXTURES / "historical"
    manifest = json.loads((root / "manifest.json").read_text())
    controls = {}
    for entry in manifest["entries"]:
        path = entry["path"]
        source = root / "controls" / path
        if not source.exists():
            source = FIXTURES / "current" / path
        controls[path] = (int(entry["mode"], 8), source.read_bytes())
    return controls, manifest


def test_historical_provider_run_proves_its_exact_profile(tmp_path: Path) -> None:
    """Accept real retained jobs only with their historical controls and PR identity."""
    from hephaestus.agents.workspace import SourceLane, WorkspaceBinding
    from hephaestus.automation.pipeline.repository_validation import (
        RepositoryValidationInvocation,
        RepositoryValidationPlan,
    )

    api = _api()
    root = FIXTURES / "historical"
    controls, manifest = _historical_controls()
    graph = json.loads((root / "source-graph.json").read_text())
    run = json.loads((root / "run.json").read_text())
    pull = json.loads((root / "pull.json").read_text())
    head, base = manifest["commit"], manifest["base"]
    inventory = tuple(
        row["path"]
        for row in graph[
            "repos/LLM360/comet/git/trees/"
            + graph["repos/LLM360/comet/git/commits/" + head]["tree"]["sha"]
            + "?recursive=1"
        ]["tree"]
        if row["type"] == "blob"
    )
    profile = api.admit_comet_controls(controls, inventory)
    assert profile != api.COMET_PROFILE_ID
    changes = tuple(tuple(row) for row in manifest["changes"])
    checks = api.comet_validation_checks(profile, changes)
    expected = {
        "comet.python.ruff-format",
        "comet.python.ruff-check",
        "comet.python.ty-check",
        "comet.python.pr-tests",
        "comet.docs.strict",
        "comet.contract.workflow-contracts",
    }
    assert {check.check_id for check in checks} == expected
    workspace = WorkspaceBinding.source(
        cwd=tmp_path,
        reusable_root=tmp_path.parent,
        repository="LLM360/comet",
        ownership_key="historical-ci-fixture",
        item_number=1487,
        lane=SourceLane.REVIEW,
        revision=head,
        generation=1,
        detached=True,
    )
    plan = RepositoryValidationPlan(
        repository="LLM360/comet",
        issue_number=1487,
        pr_number=1719,
        reviewed_head=head,
        reviewed_base=base,
        diff_base_sha=base,
        source_workspace=workspace,
        changes=changes,
        profile_id=profile,
        profile_digest=api.comet_profile_digest(profile),
        checks=checks,
    )
    invocation = RepositoryValidationInvocation(plan, 1, "f" * 32, "ci", tuple(sorted(expected)))
    run_id = run["id"]
    graph["repos/LLM360/comet/pulls/1719"] = pull
    graph[f"repos/LLM360/comet/actions/runs/{run_id}"] = run
    graph[
        f"repos/LLM360/comet/actions/workflows/ci.yml/runs?event=pull_request&head_sha={head}&per_page=100&page=1"
    ] = {"total_count": 1, "workflow_runs": [run]}
    graph[f"repos/LLM360/comet/actions/runs/{run_id}/attempts/1/jobs?per_page=100&page=1"] = (
        json.loads((root / "jobs.json").read_text())
    )
    reader = _ControlGraphReader(graph)
    result = api.collect_comet_ci(invocation, head_branch=pull["head"]["ref"], reader=reader)
    assert not result.gaps
    assert {receipt.check_id for receipt in result.receipts} == expected
    assert all(receipt.status == "success" for receipt in result.receipts)
    assert run["pull_requests"] == []
    assert sum("/attempts/1/jobs?" in call for call in reader.calls) == 2
    assert all("refs/" not in call for call in reader.calls)


@pytest.mark.parametrize(
    "path",
    [
        "tests/test-tiers.yaml",
        "deployment/deployed-inputs.yaml",
        "scripts/deployed_artifact_validators.py",
        "scripts/check_deployed_inputs.py",
    ],
)
def test_historical_profile_rejects_mixed_control_versions(path: str) -> None:
    """Do not admit a mixture of current and historical control bytes."""
    api = _api()
    controls, _ = _historical_controls()
    assert api.admit_comet_controls(controls, tuple(controls)) != api.COMET_PROFILE_ID
    controls[path] = _controls()[path]
    with pytest.raises(ValueError, match="control"):
        api.admit_comet_controls(controls, tuple(controls))


def test_source_plan_rejects_different_admitted_profile_versions(tmp_path: Path) -> None:
    """Reject historical head controls when the PR base has current controls."""
    from dataclasses import replace

    from tests.unit.automation.pipeline.stages.test_pr_review_comet_validation import _git

    workspace, base, _ = _source_fixture(tmp_path)
    controls, _ = _historical_controls()
    for path, (_, data) in controls.items():
        (workspace.cwd / path).write_bytes(data)
    _git(workspace.cwd, "add", ".")
    _git(workspace.cwd, "commit", "-m", "Select historical control bytes.")
    head = _git(workspace.cwd, "rev-parse", "HEAD")
    workspace = replace(workspace, revision=head)
    with pytest.raises(ValueError, match="different control profiles"):
        _source_plan_api()(
            workspace, issue_number=1200, pr_number=1200, reviewed_base=base, timeout_s=30
        )


def test_historical_profile_identity_covers_its_frozen_policy() -> None:
    """Bind historical commands and selection policy to the published profile digest."""
    import hashlib

    controls, manifest = _historical_controls()
    root = FIXTURES / "historical/controls"
    source = runpy.run_path(str(root / "scripts/check_deployed_inputs.py"))
    inventory = yaml.safe_load((root / "deployment/deployed-inputs.yaml").read_text())
    selection = {
        "candidates": inventory["production_candidates"],
        "policies": inventory["policies"],
        "contracts": {
            row["id"]: row.get("tier", "pr")
            for row in inventory["validators"]
            if row["kind"] == "repository-contract"
        },
        "ci_policies": source["CI_POLICIES"],
        "forbidden": tuple(sorted(source["CI_FORBIDDEN_PATHS"])),
        "retired": source["RETIRED_REPOSITORY_PATHS"],
    }
    records = [
        (row["path"], int(row["mode"], 8), row["size"], row["sha256"])
        for row in manifest["entries"]
    ]
    canonical = json.dumps(
        {"controls": records, "selection": selection}, sort_keys=True, separators=(",", ":")
    )
    api = _api()
    profile = api.admit_comet_controls(controls, tuple(controls))
    assert api.comet_profile_digest(profile) == hashlib.sha256(canonical.encode()).hexdigest()
    _, admitted_controls, admitted_selection = api._PROFILES[profile]
    admitted = json.dumps(
        {"controls": admitted_controls, "selection": admitted_selection},
        sort_keys=True,
        separators=(",", ":"),
    )
    assert admitted == canonical
    # The supplied manifests must describe the bytes used by the test.
    for path, _, size, digest in records:
        assert len(controls[path][1]) == size
        assert hashlib.sha256(controls[path][1]).hexdigest() == digest


def _fe5a67d_controls() -> dict[str, tuple[int, bytes]]:
    manifest = json.loads((FIXTURES / "fe5a67d-manifest.json").read_text())
    return {
        row["path"]: (int(row["mode"], 8), (FIXTURES / "fe5a67d" / row["path"]).read_bytes())
        for row in manifest["entries"]
    }


def test_admit_fe5a67d_complete_source_controls() -> None:
    """Admit the current source and select the added support script contract."""
    api = _api()
    paths = tuple(json.loads((FIXTURES / "fe5a67d-paths.json").read_text()))
    profile = api.admit_comet_controls(_fe5a67d_controls(), paths)
    assert profile == "comet-fe5a67d-v1"
    checks = api.comet_validation_checks(
        profile, (("M", "scripts/build_public_access_application.py"),)
    )
    assert {check.check_id for check in checks} == {
        "comet.python.ruff-format",
        "comet.python.ruff-check",
        "comet.python.ty-check",
        "comet.python.pr-tests",
        "comet.contract.control-deployment-contracts",
    }

    assert "comet.contract.control-deployment-contracts" not in api.comet_ci_check_ids(checks)


@pytest.mark.parametrize("path", CONTROL_PATHS)
@pytest.mark.parametrize("mutation", ["bytes", "mode", "missing"])
def test_fe5a67d_rejects_changed_controls(path: str, mutation: str) -> None:
    """Reject changed bytes, modes, and missing controls."""
    controls = _fe5a67d_controls()
    mode, data = controls[path]
    if mutation == "bytes":
        controls[path] = (mode, data + b"\n")
    elif mutation == "mode":
        controls[path] = (mode ^ 0o111, data)
    else:
        del controls[path]
    with pytest.raises(ValueError, match="control"):
        _api().admit_comet_controls(controls, CONTROL_PATHS)


def test_fe5a67d_rejects_unknown_control() -> None:
    """Reject a new control in the source inventory."""
    with pytest.raises(ValueError, match="control"):
        _api().admit_comet_controls(_fe5a67d_controls(), (*CONTROL_PATHS, "ruff.toml"))


@pytest.mark.parametrize(
    "tier,modules",
    [
        (
            "pr_modules",
            {
                "tests/test_admin_root_locator_cli.py",
                "tests/test_context_migration.py",
                "tests/test_public_access.py",
                "tests/test_public_access_installation.py",
                "tests/test_public_context.py",
                "tests/test_root_locator_publication.py",
                "tests/test_root_operations.py",
                "tests/test_root_operator_service.py",
            },
        ),
        ("nightly_modules", {"tests/test_domain_drain.py", "tests/test_root_locator.py"}),
    ],
)
def test_fe5a67d_tier_additions(tier: str, modules: set[str]) -> None:
    """Keep the new PR and nightly test modules in their source tiers."""
    previous = yaml.safe_load(_controls()["tests/test-tiers.yaml"][1])
    current = yaml.safe_load(_fe5a67d_controls()["tests/test-tiers.yaml"][1])
    assert set(current[tier]) - set(previous[tier]) == modules
    assert not set(previous[tier]) - set(current[tier])


@pytest.mark.parametrize("version", ["fe5a67d", "7c2772e"])
def test_profile_identity_matches_source_policy(
    version: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bind all control bytes and source selection to the profile digest."""
    import hashlib

    controls = {"fe5a67d": _fe5a67d_controls, "7c2772e": _7c2772e_controls}[version]()
    manifest = json.loads((FIXTURES / f"{version}-manifest.json").read_text())
    root = FIXTURES / version
    source = _frozen_selector(root, monkeypatch)
    inventory = yaml.safe_load(controls["deployment/deployed-inputs.yaml"][1])
    selection = {
        "candidates": inventory["production_candidates"],
        "policies": inventory["policies"],
        "contracts": {
            row["id"]: row.get("tier", "pr")
            for row in inventory["validators"]
            if row["kind"] == "repository-contract"
        },
        "ci_policies": source["CI_POLICIES"],
        "forbidden": tuple(sorted(source["CI_FORBIDDEN_PATHS"])),
        "retired": source["RETIRED_REPOSITORY_PATHS"],
    }
    records = [
        (row["path"], int(row["mode"], 8), row["size"], row["sha256"])
        for row in manifest["entries"]
    ]
    for path, _, size, digest in records:
        assert len(controls[path][1]) == size
        assert hashlib.sha256(controls[path][1]).hexdigest() == digest
    canonical = json.dumps(
        {"controls": records, "selection": selection}, sort_keys=True, separators=(",", ":")
    )
    api = _api()
    profile = api.admit_comet_controls(controls, tuple(controls))
    assert api.comet_profile_digest(profile) == hashlib.sha256(canonical.encode()).hexdigest()


def test_admit_7c2772e_complete_source_controls() -> None:
    """Admit the pinned source and retain the full ordinary test command."""
    manifest = json.loads((FIXTURES / "7c2772e-manifest.json").read_text())
    controls = {
        row["path"]: (int(row["mode"], 8), (FIXTURES / "7c2772e" / row["path"]).read_bytes())
        for row in manifest["entries"]
    }
    paths = tuple(json.loads((FIXTURES / "7c2772e-paths.json").read_text()))
    api = _api()
    profile = api.admit_comet_controls(controls, paths)
    assert profile == "comet-7c2772e-v1"
    checks = api.comet_validation_checks(profile, (("M", "tests/test_key_expiration.py"),))
    commands = {check.check_id: check.argv for check in checks}
    assert commands["comet.python.pr-tests"] == (
        "uv",
        "run",
        "--locked",
        "--extra",
        "dev",
        "python",
        "scripts/ci/run-test-tier.py",
        "pr",
    )


def test_7c2772e_format_command_matches_its_workflow() -> None:
    """Keep the command identity from the pinned workflow."""
    checks = _api().comet_validation_checks(
        "comet-7c2772e-v1", (("M", "tests/test_key_expiration.py"),)
    )
    commands = {check.check_id: check.argv for check in checks}
    assert commands["comet.python.ruff-format"] == (
        "uv",
        "run",
        "--locked",
        "--extra",
        "dev",
        "ruff",
        "format",
        "--check",
        ".",
        "--diff",
    )


@pytest.mark.parametrize("check_id", ["comet.python.pr-tests", "comet.contract.workflow-contracts"])
def test_7c2772e_ci_does_not_claim_unproved_full_test_coverage(
    tmp_path: Path, check_id: str
) -> None:
    """Reject a green job without proof of its full validation scope."""
    from dataclasses import replace

    api = _api()
    invocation, _reader = _ci_collection_fixture(tmp_path)
    checks = api.comet_validation_checks(
        "comet-7c2772e-v1", (("M", "scripts/check_deployed_inputs.py"),)
    )
    plan = replace(
        invocation.plan,
        profile_id="comet-7c2772e-v1",
        profile_digest=api.comet_profile_digest("comet-7c2772e-v1"),
        changes=(("M", "scripts/check_deployed_inputs.py"),),
        checks=checks,
    )
    check = next(check for check in checks if check.check_id == check_id)
    steps = [
        ("Set up job", "success"),
        ("Run actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", "success"),
        ("Run astral-sh/setup-uv@bec219d24cd3e171d82865faccec33120bb574f4", "success"),
    ]
    if check_id == "comet.python.pr-tests":
        name = "test (3.12)"
        steps.extend(
            [
                ("Install Bubblewrap", "success"),
                ("Run the ordinary pull-request profile", "success"),
            ]
        )
    else:
        name = "contracts / workflow-contracts"
        steps.insert(2, ("Verify the checked-out commit", "success"))
        steps.extend(
            [
                ("Install Bubblewrap", "success"),
                ("Successful no-op", "skipped"),
                ("Validate selected contract", "success"),
            ]
        )
    job = api._CometCIJob(
        1,
        name,
        "completed",
        "success",
        tuple(
            api._CometCIStep(i + 1, step, "completed", result)
            for i, (step, result) in enumerate(steps)
        ),
    )
    assert api._ci_check_receipt(plan, check, (job,)) is None


def _7c2772e_receipt_fixture(tmp_path: Path, check_id: str) -> tuple[Any, Any, tuple[Any, ...]]:
    from dataclasses import replace

    api = _api()
    invocation, _reader = _ci_collection_fixture(tmp_path)
    changes = (("M", "scripts/check_deployed_inputs.py"),)
    checks = api.comet_validation_checks("comet-7c2772e-v1", changes)
    plan = replace(
        invocation.plan,
        profile_id="comet-7c2772e-v1",
        profile_digest=api.comet_profile_digest("comet-7c2772e-v1"),
        changes=changes,
        checks=checks,
    )
    check = next(check for check in checks if check.check_id == check_id)
    prefix = [
        ("Set up job", "success"),
        ("Run actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", "success"),
        ("Run astral-sh/setup-uv@bec219d24cd3e171d82865faccec33120bb574f4", "success"),
    ]
    definitions = [
        (
            "deployment-policy",
            [
                *prefix,
                ("Initialize safe downstream selections", "success"),
                ("Collect immutable changed-path records", "success"),
                ("Validate inventory and classify inputs", "success"),
            ],
        )
    ]
    if check_id == "comet.python.pr-tests":
        definitions.extend(
            (
                f"test (3.12, shard {index})",
                [
                    *prefix,
                    ("Require the test scope", "skipped"),
                    ("Verify test source", "success"),
                    ("Install Bubblewrap", "success"),
                    ("Run the ordinary pull-request profile", "success"),
                    ("Run the promotion profile", "skipped"),
                ],
            )
            for index in range(32)
        )
    else:
        steps = list(prefix)
        steps.insert(2, ("Verify the checked-out commit", "success"))
        steps.extend(
            [
                (
                    "Install Bubblewrap",
                    "success" if check_id.endswith("workflow-contracts") else "skipped",
                ),
                ("Successful no-op", "skipped"),
                ("Validate selected contract", "success"),
            ]
        )
        definitions.append(("contracts / " + check_id.removeprefix("comet.contract."), steps))
    jobs = tuple(
        api._CometCIJob(
            job_index + 1,
            name,
            "completed",
            "success",
            tuple(
                api._CometCIStep(i + 1, step, "completed", result)
                for i, (step, result) in enumerate(steps)
            ),
        )
        for job_index, (name, steps) in enumerate(definitions)
    )
    return plan, check, jobs


@pytest.mark.parametrize(
    "check_id",
    [
        "comet.python.pr-tests",
        "comet.contract.workflow-contracts",
        "comet.contract.github-production-rules-contracts",
    ],
)
def test_7c2772e_ci_proves_full_legacy_coverage(tmp_path: Path, check_id: str) -> None:
    """Require the scope producer and complete consumer coverage."""
    api = _api()
    plan, check, jobs = _7c2772e_receipt_fixture(tmp_path, check_id)
    assert check_id in api.comet_ci_check_ids(plan.checks, profile=plan.profile_id)
    receipt = api._ci_check_receipt(plan, check, jobs)
    assert receipt is not None
    assert receipt.status == "success"
    assert receipt.argv == check.argv
    assert receipt.source_digests == check.source_digests


@pytest.mark.parametrize(
    "path",
    [
        "docs/K2-notes.md",
        "scripts/k2_ci_policy.py",
        "cookbooks/k2.yaml",
        ".github/ci/k2-affected-scope.json",
    ],
)
@pytest.mark.parametrize("status", ["A", "M", "D"])
def test_7c2772e_ci_rejects_affected_coverage(tmp_path: Path, path: str, status: str) -> None:
    """A green affected job does not prove the full ordinary test command."""
    from dataclasses import replace

    api = _api()
    plan, check, jobs = _7c2772e_receipt_fixture(tmp_path, "comet.python.pr-tests")
    plan = replace(plan, changes=(*plan.changes, (status, path)))
    assert api._ci_check_receipt(plan, check, jobs) is None


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-classifier",
        "missing-shard",
        "skipped-source",
        "skipped-test",
        "duplicate-shard",
        "extra-shard",
        "failed-shard",
    ],
)
def test_7c2772e_ci_rejects_incomplete_shard_proof(tmp_path: Path, mutation: str) -> None:
    """Reject incomplete, ambiguous, or unsuccessful shard evidence."""
    from dataclasses import replace

    api = _api()
    plan, check, jobs = _7c2772e_receipt_fixture(tmp_path, "comet.python.pr-tests")
    if mutation == "missing-classifier":
        jobs = jobs[1:]
    elif mutation == "missing-shard":
        jobs = jobs[:-1]
    elif mutation in {"skipped-source", "skipped-test"}:
        target = (
            "Verify test source"
            if mutation == "skipped-source"
            else "Run the ordinary pull-request profile"
        )
        changed = replace(
            jobs[-1],
            steps=tuple(
                replace(step, conclusion="skipped") if step.name == target else step
                for step in jobs[-1].steps
            ),
        )
        jobs = (*jobs[:-1], changed)
    elif mutation == "duplicate-shard":
        jobs = (*jobs, replace(jobs[-1], identifier=100))
    elif mutation == "extra-shard":
        jobs = (*jobs, replace(jobs[-1], identifier=100, name="test (3.12, shard 32)"))
    else:
        jobs = (*jobs[:-1], replace(jobs[-1], conclusion="failure"))
    if mutation in {"duplicate-shard", "extra-shard"}:
        with pytest.raises(api.CometCIReadError, match="ci_job_ambiguous"):
            api._ci_check_receipt(plan, check, jobs)
    else:
        receipt = api._ci_check_receipt(plan, check, jobs)
        assert receipt is None or receipt.status == "failed"


def _7c2772e_controls() -> dict[str, tuple[int, bytes]]:
    manifest = json.loads((FIXTURES / "7c2772e-manifest.json").read_text())
    return {
        row["path"]: (int(row["mode"], 8), (FIXTURES / "7c2772e" / row["path"]).read_bytes())
        for row in manifest["entries"]
    }


def test_7c2772e_source_plan_reads_all_registered_controls(tmp_path: Path) -> None:
    """Bind the larger profile to immutable source commits."""
    from dataclasses import replace

    from tests.unit.automation.pipeline.stages.test_pr_review_comet_validation import _git

    workspace, _old_base, _old_head = _source_fixture(tmp_path)
    for path, (mode, data) in _7c2772e_controls().items():
        target = workspace.cwd / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode & 0o777)
    _git(workspace.cwd, "add", ".")
    _git(workspace.cwd, "commit", "-m", "Use the pinned validation controls.")
    base = _git(workspace.cwd, "rev-parse", "HEAD")
    (workspace.cwd / "src/comet/example.py").write_text("VALUE = 3319\n")
    _git(workspace.cwd, "add", ".")
    _git(workspace.cwd, "commit", "-m", "Change the selected source.")
    head = _git(workspace.cwd, "rev-parse", "HEAD")
    plan = _source_plan_api()(
        replace(workspace, revision=head),
        issue_number=1200,
        pr_number=1200,
        reviewed_base=base,
        timeout_s=30,
    )
    assert plan.profile_id == "comet-7c2772e-v1"
    assert plan.changes == (("M", "src/comet/example.py"),)
    assert all(len(check.source_digests) == 47 for check in plan.checks)


def test_7c2772e_ci_collection_binds_all_shards_and_source_controls(tmp_path: Path) -> None:
    """Read stable same-run evidence through the complete CI collector."""
    from dataclasses import replace

    api = _api()
    invocation, reader = _ci_collection_fixture(tmp_path)
    plan, check, jobs = _7c2772e_receipt_fixture(tmp_path, "comet.python.pr-tests")
    invocation = replace(invocation, plan=plan, check_ids=(check.check_id,))
    reader.graph.update(_ci_control_graph(_7c2772e_controls()))
    reader.graph["repos/LLM360/comet/git/commits/" + "c" * 40] = _ci_identity_fixture()[2]
    reader.graph["repos/LLM360/comet/actions/runs/123/attempts/2/jobs?per_page=100&page=1"] = {
        "total_count": len(jobs),
        "jobs": [
            {
                "id": job.identifier,
                "run_id": 123,
                "run_attempt": 2,
                "head_sha": "a" * 40,
                "name": job.name,
                "status": job.status,
                "conclusion": job.conclusion,
                "steps": [
                    {
                        "number": step.number,
                        "name": step.name,
                        "status": step.status,
                        "conclusion": step.conclusion,
                    }
                    for step in job.steps
                ],
            }
            for job in jobs
        ],
    }
    result = _collect_ci(api, invocation, reader)
    assert not result.gaps
    assert len(result.receipts) == 1
    assert result.receipts[0].status == "success"
    assert result.receipts[0].source_digests == check.source_digests


@pytest.mark.parametrize(
    "path",
    [
        row["path"]
        for row in json.loads((FIXTURES / "7c2772e-manifest.json").read_text())["entries"]
    ],
)
@pytest.mark.parametrize("mutation", ["bytes", "mode", "missing"])
def test_7c2772e_rejects_changed_controls(path: str, mutation: str) -> None:
    """Reject changed bytes, modes, and missing controls from the current profile."""
    controls = _7c2772e_controls()
    mode, data = controls[path]
    if mutation == "bytes":
        controls[path] = (mode, data + b"\n")
    elif mutation == "mode":
        controls[path] = (mode ^ 0o111, data)
    else:
        del controls[path]
    paths = tuple(json.loads((FIXTURES / "7c2772e-paths.json").read_text()))
    with pytest.raises(ValueError, match="control"):
        _api().admit_comet_controls(controls, paths)


@pytest.mark.parametrize(
    "path", ["k2_ci_policy.py", "src/k2_ci_policy.py", "scripts/ci/k2_ci_policy.py", "ruff.toml"]
)
def test_7c2772e_rejects_new_validation_controls(path: str) -> None:
    """Reject unregistered configuration and modules that can replace the policy."""
    paths = tuple(json.loads((FIXTURES / "7c2772e-paths.json").read_text()))
    with pytest.raises(ValueError, match="control"):
        _api().admit_comet_controls(_7c2772e_controls(), (*paths, path))

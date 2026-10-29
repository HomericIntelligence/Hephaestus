"""Check the pinned Comet post-#2267 repository-validation profile."""

from __future__ import annotations

import hashlib
import importlib
import json
import stat
import tomllib
from pathlib import Path
from typing import Any

import pytest

from hephaestus.agents.workspace import SourceLane, WorkspaceBinding
from hephaestus.automation.comet_profile_post2267 import (
    CHECK_IDS,
    CHECKOUT_STEP,
    CI_REFERENCED_WORKFLOWS,
    CI_WORKFLOW_PATH,
    CONTROLS,
    PR_TESTS_CHECK_ID,
    PROFILE_DIGEST,
    PROFILE_ID,
    REPOSITORY,
    RUFF_CHECK_ID,
    RUFF_FORMAT_CHECK_ID,
    SETUP_UV_STEP,
    TY_CHECK_ID,
    comet_post2267_profile,
)
from hephaestus.automation.pipeline import repository_validation_profiles as profiles_api
from hephaestus.automation.pipeline.repository_validation import (
    RepositoryValidationCheck,
    RepositoryValidationPlan,
)
from hephaestus.automation.pipeline.repository_validation_profiles import (
    PROFILE_ENTRY_POINT_GROUP,
    RepositoryValidationProfile,
    ValidationCIJob,
    ValidationCIStep,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "comet-review-validation"
CAPTURE_DIR = FIXTURE_ROOT / "bc16c3a"
MANIFEST_PATH = FIXTURE_ROOT / "bc16c3a-manifest.json"
PATHS_PATH = FIXTURE_ROOT / "bc16c3a-paths.json"
CAPTURED_COMMIT = "bc16c3ae6562799a229c54c82344e2fceb64ab9b"
TOMBSTONE_PATH = ".github/ci/k2-affected-scope.json"

HEAD = "a" * 40
BASE = "b" * 40


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _fixture_controls() -> dict[str, tuple[int, bytes]]:
    """Build the control inventory from the captured fixture bytes."""
    return {path: (mode, (CAPTURE_DIR / path).read_bytes()) for path, mode, _size, _d in CONTROLS}


def _fixture_inventory() -> tuple[str, ...]:
    """Read the captured source inventory."""
    return tuple(json.loads(PATHS_PATH.read_text(encoding="utf-8")))


def _step(number: int, name: str, conclusion: str = "success") -> ValidationCIStep:
    return ValidationCIStep(number=number, name=name, status="completed", conclusion=conclusion)


def _job(
    identifier: int, name: str, steps: list[ValidationCIStep], **override: Any
) -> ValidationCIJob:
    return ValidationCIJob(
        identifier=identifier,
        name=name,
        status=override.get("status", "completed"),
        conclusion=override.get("conclusion", "success"),
        steps=tuple(steps),
    )


def _lint_steps(**override: Any) -> list[ValidationCIStep]:
    format_step = override.get("format", "success")
    check_step = override.get("check", "success")
    return [
        _step(1, "Set up job"),
        _step(2, CHECKOUT_STEP),
        _step(3, SETUP_UV_STEP),
        _step(4, "Run uv run --locked --extra dev ruff format --check . --diff", format_step),
        _step(5, "Run uv run --locked --extra dev ruff check .", check_step),
        _step(6, "Run uv run --locked --extra dev ruff check --diff .", "skipped"),
        _step(7, "Run uv run --locked --extra dev pre-commit run --all-files"),
        _step(8, "Check import layers"),
    ]


def _typecheck_steps() -> list[ValidationCIStep]:
    return [
        _step(1, "Set up job"),
        _step(2, CHECKOUT_STEP),
        _step(3, SETUP_UV_STEP),
        _step(4, "Run uv run --locked --extra dev ty check"),
    ]


def _shard_steps(shard: int, **override: Any) -> list[ValidationCIStep]:
    return [
        _step(1, "Set up job"),
        _step(2, CHECKOUT_STEP),
        _step(3, SETUP_UV_STEP),
        _step(4, "Require the test scope", "skipped"),
        _step(5, "Verify test source", override.get("source", "success")),
        _step(6, "Install Bubblewrap", override.get("bubblewrap", "success")),
        _step(7, "Record the no-op product test decision", "skipped"),
        _step(8, "Run the ordinary pull-request profile", override.get("profile", "success")),
        _step(9, "Run the promotion profile", "skipped"),
        _step(10, "Keep test results"),
    ]


def _ci_jobs(*, shards: set[int] | None = None) -> tuple[ValidationCIJob, ...]:
    """Build the captured pull-request job set: lint, typecheck, and every shard."""
    jobs = [
        _job(1, "lint", _lint_steps()),
        _job(2, "typecheck", _typecheck_steps()),
    ]
    wanted = set(range(32)) if shards is None else shards
    jobs.extend(
        _job(100 + shard, f"test (3.12, shard {shard})", _shard_steps(shard))
        for shard in sorted(wanted)
    )
    return tuple(jobs)


def _plan(
    tmp_path: Path, checks: tuple[RepositoryValidationCheck, ...]
) -> RepositoryValidationPlan:
    """Build one minimal valid plan bound to the profile identity."""
    workspace = WorkspaceBinding.source(
        cwd=tmp_path,
        reusable_root=tmp_path.parent,
        repository="comet",
        ownership_key="comet-review-3437",
        item_number=3437,
        lane=SourceLane.REVIEW,
        revision=HEAD,
        generation=1,
        detached=True,
    )
    return RepositoryValidationPlan(
        repository=REPOSITORY,
        issue_number=3437,
        pr_number=2300,
        reviewed_head=HEAD,
        reviewed_base=BASE,
        diff_base_sha=BASE,
        source_workspace=workspace,
        changes=(("M", "src/comet/app.py"),),
        profile_id=PROFILE_ID,
        profile_digest=PROFILE_DIGEST,
        checks=checks,
    )


def test_factory_returns_a_complete_valid_profile() -> None:
    """The factory builds one profile that passes every admission check."""
    profile = comet_post2267_profile()

    assert type(profile) is RepositoryValidationProfile
    # The repository-name contract admits lowercase names only.
    assert profile.repository == REPOSITORY == "llm360/comet"
    assert profile.local_name == "comet"
    assert profile.profile_id == PROFILE_ID
    assert profile.profile_digest == PROFILE_DIGEST
    assert profile.ci_workflow_path == CI_WORKFLOW_PATH
    assert profile.control_paths == frozenset(path for path, _mode, _size, _digest in CONTROLS)
    assert profile.ci_referenced_workflows == CI_REFERENCED_WORKFLOWS


def test_controls_follow_the_manifest_order_and_digest() -> None:
    """The profile digest re-derives from the capture manifest rows."""
    manifest = _manifest()
    # The capture table keeps the canonical GitHub spelling of the project.
    assert manifest["repository"] == "LLM360/comet"
    assert manifest["repository"].casefold() == REPOSITORY
    assert manifest["commit"] == CAPTURED_COMMIT
    rows = [
        f"{entry['path']}\t{entry['mode']}\t{entry['size']}\t{entry['sha256']}"
        for entry in manifest["entries"]
    ]
    derived = hashlib.sha256(("\n".join(rows) + "\n").encode("utf-8")).hexdigest()

    assert derived == PROFILE_DIGEST
    assert len(CONTROLS) == len(manifest["entries"]) == 23
    assert [control[0] for control in CONTROLS] == [entry["path"] for entry in manifest["entries"]]


def test_every_control_row_matches_its_fixture_bytes() -> None:
    """Each bound control row agrees with the captured fixture bytes."""
    assert sorted(control[0] for control in CONTROLS) == [control[0] for control in CONTROLS]
    for path, mode, size, digest in CONTROLS:
        fixture = CAPTURE_DIR / path
        assert fixture.is_file(), f"missing fixture for control {path}"
        assert stat.S_IMODE(fixture.stat().st_mode) == mode & 0o777
        data = fixture.read_bytes()
        assert len(data) == size, f"size drift for {path}"
        assert hashlib.sha256(data).hexdigest() == digest, f"content drift for {path}"


def test_select_returns_one_full_check_set_for_every_change_set() -> None:
    """The fixed selection never re-admits the removed K2 affected-scope policy."""
    profile = comet_post2267_profile()
    empty = profile.select(())
    for changes in (
        (("M", "src/comet/app.py"),),
        (("M", "mkdocs.yml"), ("A", "docs/runbooks/x.md")),
        (("A", "scripts/k2_ci_policy.py"),),
        (("M", ".github/ci/k2-affected-scope.json"),),
    ):
        assert profile.select(changes) == empty
    assert tuple(check.check_id for check in empty) == CHECK_IDS
    digests = tuple((path, size, digest) for path, _mode, size, digest in CONTROLS)
    for check in empty:
        assert check.source_digests == digests
    assert profile.ci_covered_check_ids(empty) == CHECK_IDS
    assert profile.local_check_ids(empty) == CHECK_IDS


def test_admit_controls_accepts_the_captured_inventory() -> None:
    """The captured control inventory and source paths admit the profile."""
    result = comet_post2267_profile().admit_controls(_fixture_controls(), _fixture_inventory())
    assert result == PROFILE_ID


def test_admit_controls_rejects_the_removed_k2_control_tombstone() -> None:
    """A re-created K2 affected-scope file is an unreviewed control."""
    controls = _fixture_controls()
    controls[TOMBSTONE_PATH] = (0o100644, b'{"affected_scope": []}\n')
    with pytest.raises(ValueError, match="does not match"):
        comet_post2267_profile().admit_controls(controls, _fixture_inventory())
    discovered = (*_fixture_inventory(), TOMBSTONE_PATH)
    with pytest.raises(ValueError, match="unreviewed control"):
        comet_post2267_profile().admit_controls(_fixture_controls(), discovered)


def test_admit_controls_rejects_changed_missing_or_incomplete_inventories() -> None:
    """Byte, membership, and discovery changes all fail admission."""
    profile = comet_post2267_profile()
    controls = _fixture_controls()
    changed = dict(controls)
    changed["uv.lock"] = (0o100644, changed["uv.lock"][1] + b"\n")
    with pytest.raises(ValueError, match="does not match"):
        profile.admit_controls(changed, _fixture_inventory())
    missing = {path: record for path, record in controls.items() if path != "pyproject.toml"}
    with pytest.raises(ValueError, match="does not match"):
        profile.admit_controls(missing, _fixture_inventory())
    incomplete = tuple(path for path in _fixture_inventory() if path != "uv.lock")
    with pytest.raises(ValueError, match="incomplete"):
        profile.admit_controls(controls, incomplete)


@pytest.mark.parametrize(
    "check_id", [RUFF_FORMAT_CHECK_ID, RUFF_CHECK_ID, TY_CHECK_ID, PR_TESTS_CHECK_ID]
)
def test_ci_receipt_status_accepts_complete_successful_jobs(tmp_path: Path, check_id: str) -> None:
    """A complete provider observation of the captured jobs proves every check."""
    profile = comet_post2267_profile()
    plan = _plan(tmp_path, profile.select(()))
    assert profile.ci_receipt_status(plan, check_id, _ci_jobs()) == "success"


def test_ci_receipt_status_reports_failed_jobs(tmp_path: Path) -> None:
    """A failed captured job step reports a failed status."""
    profile = comet_post2267_profile()
    plan = _plan(tmp_path, profile.select(()))
    jobs = (
        _job(1, "lint", _lint_steps(check="failure"), conclusion="failure"),
        _job(2, "typecheck", _typecheck_steps(), conclusion="failure"),
    )
    assert profile.ci_receipt_status(plan, RUFF_CHECK_ID, jobs) == "failed"
    assert profile.ci_receipt_status(plan, TY_CHECK_ID, jobs) == "failed"
    shard_jobs = tuple(
        _job(100 + shard, f"test (3.12, shard {shard})", _shard_steps(shard), conclusion="failure")
        for shard in range(32)
    )
    assert profile.ci_receipt_status(plan, PR_TESTS_CHECK_ID, shard_jobs) == "failed"


def test_ci_receipt_status_waits_for_absent_jobs(tmp_path: Path) -> None:
    """A missing captured job keeps the status undecided."""
    profile = comet_post2267_profile()
    plan = _plan(tmp_path, profile.select(()))
    only_lint = (_job(1, "lint", _lint_steps()),)
    assert profile.ci_receipt_status(plan, RUFF_FORMAT_CHECK_ID, only_lint) == "success"
    assert profile.ci_receipt_status(plan, TY_CHECK_ID, only_lint) is None
    without_shard_seven = _ci_jobs(shards=set(range(32)) - {7})
    assert profile.ci_receipt_status(plan, PR_TESTS_CHECK_ID, without_shard_seven) is None
    assert profile.ci_receipt_status(plan, "comet.python.unknown", _ci_jobs()) is None


def test_entry_point_registration_discovers_the_comet_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pyproject entry-point table and the loader both bind the profile."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    table = pyproject["project"]["entry-points"][PROFILE_ENTRY_POINT_GROUP]
    target = "hephaestus.automation.comet_profile_post2267:comet_post2267_profile"
    assert table == {"comet-post2267": target}
    module_name, _, factory_name = target.partition(":")
    factory = getattr(importlib.import_module(module_name), factory_name)
    assert factory is comet_post2267_profile
    assert factory().profile_id == PROFILE_ID

    recorded: list[str] = []

    class _Entry:
        def load(self) -> Any:
            return factory

    def fake_entry_points(*, group: str) -> tuple[Any, ...]:
        recorded.append(group)
        return (_Entry(),) if group == PROFILE_ENTRY_POINT_GROUP else ()

    monkeypatch.setattr(profiles_api, "entry_points", fake_entry_points)
    monkeypatch.setattr(profiles_api, "_PROFILES", None)

    profile = profiles_api.repository_validation_profile("LLM360/comet")
    assert profile is not None
    assert profile.profile_id == PROFILE_ID
    # The canonical GitHub spelling and the lowercase binding are one identity.
    assert profile.repository == "llm360/comet"
    assert profiles_api.repository_validation_profile("comet") is profile
    assert profiles_api.repository_validation_profile("foreign/comet") is profile
    assert recorded == [PROFILE_ENTRY_POINT_GROUP]

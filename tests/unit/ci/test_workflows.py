"""Tests for hephaestus.ci.workflows."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 has no stdlib tomllib
    import tomli as tomllib

from hephaestus.ci.workflows import (
    Violation,
    _is_checkout_step,
    _is_local_reference_step,
    check_inventory,
    collect_workflow_files,
    collect_yml_files,
    parse_readme_table,
    validate_workflow,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
AUTO_TAG_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "auto-tag.yml"
AUTO_MERGE_ON_GO_WORKFLOW = (
    REPO_ROOT / ".github" / "workflows" / "enable-auto-merge-on-implementation-go.yml"
)
REQUIRED_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "_required.yml"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
SECURITY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "security.yml"
TEST_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"
PERFORMANCE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "performance.yml"
PERFORMANCE_DOC = REPO_ROOT / "docs" / "performance-testing.md"
CONTRACT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "contract.yml"
CONTRACT_DOC = REPO_ROOT / "docs" / "contract-testing.md"
SETUP_PI_ACTION = REPO_ROOT / ".github" / "actions" / "setup-pi-cli" / "action.yml"


class TestCollectYmlFiles:
    """Tests for collect_yml_files()."""

    def test_finds_yml_files(self, tmp_path: Path) -> None:
        workflows = tmp_path / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("name: CI")
        (workflows / "release.yml").write_text("name: Release")
        result = collect_yml_files(tmp_path)
        assert "ci.yml" in result
        assert "release.yml" in result

    def test_no_workflows_dir(self, tmp_path: Path) -> None:
        assert collect_yml_files(tmp_path) == set()

    def test_excludes_worktrees(self, tmp_path: Path) -> None:
        workflows = tmp_path / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("name: CI")
        # Worktree path — create a worktrees subdir
        worktree_wf = tmp_path / "worktrees" / "branch" / ".github" / "workflows"
        worktree_wf.mkdir(parents=True)
        (worktree_wf / "ci.yml").write_text("name: CI (worktree copy)")
        result = collect_yml_files(tmp_path)
        # Only one ci.yml should appear (from main .github/workflows/)
        assert "ci.yml" in result
        assert len([f for f in result if f == "ci.yml"]) == 1


class TestParseReadmeTable:
    """Tests for parse_readme_table()."""

    def test_parses_plain_filename(self, tmp_path: Path) -> None:
        readme = tmp_path / "README.md"
        readme.write_text("| ci.yml | Runs tests |\n")
        result = parse_readme_table(readme)
        assert "ci.yml" in result

    def test_parses_linked_filename(self, tmp_path: Path) -> None:
        readme = tmp_path / "README.md"
        readme.write_text("| [release.yml](#release) | Creates releases |\n")
        result = parse_readme_table(readme)
        assert "release.yml" in result

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        result = parse_readme_table(tmp_path / "nonexistent.md")
        assert result == set()

    def test_ignores_non_table_lines(self, tmp_path: Path) -> None:
        readme = tmp_path / "README.md"
        readme.write_text("# Workflows\n\nThis repo uses ci.yml for testing.\n")
        result = parse_readme_table(readme)
        assert "ci.yml" not in result


class TestCheckInventory:
    """Tests for check_inventory()."""

    def _setup(self, tmp_path: Path, on_disk: list[str], in_readme: list[str]) -> None:
        workflows = tmp_path / ".github" / "workflows"
        workflows.mkdir(parents=True)
        for name in on_disk:
            (workflows / name).write_text(f"name: {name}")
        readme = workflows / "README.md"
        table_rows = "\n".join(f"| {name} | desc |" for name in in_readme)
        readme.write_text(f"# Workflows\n\n{table_rows}\n")

    def test_in_sync(self, tmp_path: Path) -> None:
        self._setup(tmp_path, ["ci.yml"], ["ci.yml"])
        undoc, missing = check_inventory(tmp_path)
        assert undoc == []
        assert missing == []

    def test_undocumented_file(self, tmp_path: Path) -> None:
        self._setup(tmp_path, ["ci.yml", "new.yml"], ["ci.yml"])
        undoc, _missing = check_inventory(tmp_path)
        assert "new.yml" in undoc

    def test_missing_file(self, tmp_path: Path) -> None:
        self._setup(tmp_path, ["ci.yml"], ["ci.yml", "phantom.yml"])
        _, missing = check_inventory(tmp_path)
        assert "phantom.yml" in missing


class TestWorkflowInventoryLiveTree:
    """Live-tree regression tests for workflow inventory enforcement."""

    def test_real_repo_workflow_inventory_is_in_sync(self) -> None:
        undocumented, missing = check_inventory(REPO_ROOT)
        assert undocumented == []
        assert missing == []

    def test_public_workflow_references_resolve_to_existing_files(self) -> None:
        """Public badges and workflow docs cannot advertise deleted workflows."""
        pattern = re.compile(r"(?:actions/)?workflows/([A-Za-z0-9_.-]+\.ya?ml)")
        workflow_dir = REPO_ROOT / ".github" / "workflows"

        for document in (REPO_ROOT / "README.md", REPO_ROOT / ".github" / "README.md"):
            references = pattern.findall(document.read_text(encoding="utf-8"))
            missing = [name for name in references if not (workflow_dir / name).is_file()]
            assert missing == [], (
                f"{document.relative_to(REPO_ROOT)} references missing workflows: {missing}"
            )

    def test_workflow_inventory_hook_is_wired_in_precommit(self) -> None:
        from hephaestus.ci.precommit import load_precommit_config

        repos = load_precommit_config(REPO_ROOT / ".pre-commit-config.yaml")
        hook = None
        for repo in repos:
            hooks = repo.get("hooks")
            if not isinstance(hooks, list):
                continue
            for candidate in hooks:
                if isinstance(candidate, dict) and candidate.get("id") == (
                    "hephaestus-check-workflow-inventory"
                ):
                    hook = candidate
                    break
            if hook is not None:
                break

        assert hook is not None
        assert hook["entry"] == "uv run hephaestus-check-workflow-inventory"
        assert hook["pass_filenames"] is False
        assert hook["always_run"] is True
        assert (
            hook["files"]
            == r"^(\.pre-commit-config\.yaml|\.github/workflows/(README\.md|.*\.yml))$"
        )


class TestPerformanceWorkflow:
    """Contracts for the bounded worker-pool performance lane."""

    def _load(self) -> dict[str, Any]:
        workflow: dict[str, Any] = yaml.load(
            PERFORMANCE_WORKFLOW.read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        return workflow

    def test_lane_is_scheduled_manual_and_bounded(self) -> None:
        """The lane has only the approved triggers and fixed safety limits."""
        workflow = self._load()
        assert set(workflow["on"]) == {"schedule", "workflow_dispatch"}

        job = workflow["jobs"]["worker-pool-load"]
        assert int(job["timeout-minutes"]) <= 10
        run = next(
            step["run"]
            for step in job["steps"]
            if step.get("name") == "Run bounded worker-pool load tests"
        )
        for argument in (
            "--load-duration-s=30",
            "--load-max-jobs=50000",
            "--load-workers=8",
            "--load-max-in-flight=64",
            "--load-p95-budget-ms=500",
        ):
            assert argument in run

    def test_lane_collects_before_running_the_bounded_profile(self) -> None:
        """The workflow proves collection before it evaluates runtime limits."""
        steps = self._load()["jobs"]["worker-pool-load"]["steps"]
        collect = next(
            step["run"]
            for step in steps
            if step.get("name") == "Verify performance suite collection"
        )

        assert "python -m pytest tests/performance" in collect
        assert "--collect-only" in collect
        assert '--override-ini="addopts="' in collect

    def test_lane_uploads_runtime_report(self) -> None:
        """The report is retained as an artifact even when a gate fails."""
        steps = self._load()["jobs"]["worker-pool-load"]["steps"]
        upload = next(
            step
            for step in steps
            if str(step.get("uses", "")).startswith("actions/upload-artifact@")
        )
        assert upload["if"] == "${{ always() }}"
        assert upload["with"]["path"] == "build/performance/worker-pool.json"

    def test_performance_strategy_is_documented(self) -> None:
        """The public docs index links to the performance strategy."""
        index = (REPO_ROOT / "docs" / "index.md").read_text(encoding="utf-8")
        assert PERFORMANCE_DOC.is_file()
        assert "(performance-testing.md)" in index

    def test_default_pytest_options_deselect_performance_tests(self) -> None:
        """Normal test runs do not accidentally execute the stress lane."""
        config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        addopts = config["tool"]["pytest"]["ini_options"]["addopts"]
        assert "-m" in addopts
        assert "not performance" in addopts


class TestContractWorkflow:
    """Contracts for the opt-in external-integration contract lane (issue #2146)."""

    def _load(self) -> dict[str, Any]:
        workflow: dict[str, Any] = yaml.load(
            CONTRACT_WORKFLOW.read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        return workflow

    def _contract_step(self) -> dict[str, Any]:
        steps = self._load()["jobs"]["github-contract"]["steps"]
        return next(
            step for step in steps if str(step.get("run", "")).strip().startswith("uv run pytest")
        )

    def test_trigger_is_dispatch_only(self) -> None:
        """The lane must never run on pull_request/push — dispatch only."""
        workflow = self._load()
        assert set(workflow["on"]) == {"workflow_dispatch"}

    def test_permissions_are_read_only(self) -> None:
        """The lane requests only read scopes."""
        permissions = self._load()["permissions"]
        assert permissions["contents"] == "read"
        assert permissions["issues"] == "read"

    def test_pytest_step_opts_in_and_is_tokened(self) -> None:
        """The pytest step sets the opt-in gate and a GH token, and targets the lane."""
        step = self._contract_step()
        env = step["env"]
        assert env["HEPHAESTUS_CONTRACT_TESTS"] == "1"
        assert env["GH_TOKEN"] == "${{ github.token }}"  # noqa: S105
        assert env["HEPHAESTUS_CONTRACT_REPO"] == "${{ github.repository }}"
        assert "tests/integration/contract" in step["run"]
        assert '--override-ini="addopts="' in step["run"]

    def test_agent_lane_is_not_opted_in(self) -> None:
        """CI must not spend model tokens: the agent gate stays unset."""
        env = self._contract_step().get("env", {})
        assert "HEPHAESTUS_CONTRACT_AGENT" not in env

    def test_contract_lane_is_documented(self) -> None:
        """The public docs index links to the contract-testing guide."""
        index = (REPO_ROOT / "docs" / "index.md").read_text(encoding="utf-8")
        assert CONTRACT_DOC.is_file()
        assert "(contract-testing.md)" in index


class TestIsCheckoutStep:
    """Tests for _is_checkout_step()."""

    def test_checkout_step(self) -> None:
        assert _is_checkout_step({"uses": "actions/checkout@v4"}) is True

    def test_checkout_without_version(self) -> None:
        assert _is_checkout_step({"uses": "actions/checkout"}) is True

    def test_non_checkout(self) -> None:
        assert _is_checkout_step({"uses": "actions/setup-python@v4"}) is False

    def test_not_dict(self) -> None:
        assert _is_checkout_step("not a dict") is False

    def test_no_uses_key(self) -> None:
        assert _is_checkout_step({"run": "echo hello"}) is False


class TestIsLocalReferenceStep:
    """Tests for _is_local_reference_step()."""

    def test_local_action(self) -> None:
        assert _is_local_reference_step({"uses": "./.github/actions/setup"}) is True

    def test_local_workflow(self) -> None:
        assert _is_local_reference_step({"uses": "./.github/workflows/reusable.yml"}) is True

    def test_external_action(self) -> None:
        assert _is_local_reference_step({"uses": "actions/checkout@v4"}) is False

    def test_not_dict(self) -> None:
        assert _is_local_reference_step("str") is False

    def test_no_uses_key(self) -> None:
        assert _is_local_reference_step({"run": "echo hi"}) is False


class TestValidateWorkflow:
    """Tests for validate_workflow()."""

    def _write_workflow(self, path: Path, content: str) -> Path:
        path.write_text(content)
        return path

    def test_valid_checkout_first(self, tmp_path: Path) -> None:
        wf = self._write_workflow(
            tmp_path / "ci.yml",
            """
jobs:
  build:
    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/actions/setup
""",
        )
        assert validate_workflow(wf) == []

    def test_checkout_missing_violation(self, tmp_path: Path) -> None:
        wf = self._write_workflow(
            tmp_path / "ci.yml",
            """
jobs:
  build:
    steps:
      - uses: ./.github/actions/setup
""",
        )
        violations = validate_workflow(wf)
        assert len(violations) == 1
        assert isinstance(violations[0], Violation)
        assert violations[0].job_name == "build"

    def test_no_jobs(self, tmp_path: Path) -> None:
        wf = self._write_workflow(tmp_path / "ci.yml", "name: empty\n")
        assert validate_workflow(wf) == []

    def test_large_file_skipped(self, tmp_path: Path) -> None:
        wf = tmp_path / "big.yml"
        wf.write_bytes(b"x" * (1_048_576 + 1))
        assert validate_workflow(wf) == []


class TestStrictGateWorkflow:
    """Regression tests for queue-owned strict-review auto-merge policy."""

    def test_label_triggered_auto_merge_workflow_is_removed(self) -> None:
        """No privileged label-event workflow can bypass the strict gate."""
        assert not AUTO_MERGE_ON_GO_WORKFLOW.exists()

    def test_advisory_policy_reports_without_authorizing_an_arm(self) -> None:
        """The workflow reports state; the queue remains the sole armer."""
        text = REQUIRED_WORKFLOW.read_text(encoding="utf-8")
        assert "auto-merge-policy" in text
        assert "queue-owned strict-review and merge-wait controls are authoritative" in text
        assert "auto-merge is currently disabled" in text
        assert "Waiting for label-triggered auto-merge workflow" not in text
        assert "gh pr merge $PR_NUMBER --auto --squash" not in text
        assert "sleep 10" not in text

    def test_auto_merge_policy_treats_merged_prs_as_terminal(self) -> None:
        """GitHub clears autoMergeRequest after merge, so merged PRs still pass."""
        text = REQUIRED_WORKFLOW.read_text(encoding="utf-8")
        assert "--json autoMergeRequest,labels,state" in text
        assert "Incomplete PR state response" in text
        assert 'has("state")' in text
        assert 'has("autoMergeRequest")' in text
        assert '.autoMergeRequest == null or (.autoMergeRequest | type == "object")' in text
        assert "auto-merge policy is terminal" in text

    def test_pr_policy_remains_independent_from_auto_merge_state(self) -> None:
        """The hard PR policy keeps its body-only metadata check."""
        text = REQUIRED_WORKFLOW.read_text(encoding="utf-8")
        assert "--json body\n" in text or "--json body \\" in text


class TestRequiredUvLockCheckWorkflow:
    """Regression tests for the required UV lockfile check."""

    def test_uv_lock_check_is_a_required_job(self) -> None:
        with open(REQUIRED_WORKFLOW, encoding="utf-8") as f:
            workflow = yaml.safe_load(f)
        steps = workflow["jobs"]["uv-lock-check"]["steps"]
        assert any(step.get("run") == "uv lock --check" for step in steps)


class TestGitleaksSecretsScan:
    """Regression tests for the required gitleaks secrets scan.

    gitleaks runs as a digest-addressed container image via `docker run`.
    This avoids a dependency on a transient GitHub Releases tarball. `docker
    run` (not a job-level `container:`) keeps
    actions/checkout running on the host runner, since the gitleaks image is
    musl-based Alpine with no Node.js runtime.
    """

    def _gitleaks_step(self) -> dict[str, Any]:
        with open(REQUIRED_WORKFLOW, encoding="utf-8") as f:
            workflow = yaml.safe_load(f)
        steps = workflow["jobs"]["security-secrets-scan"]["steps"]
        return next(step for step in steps if step.get("name") == "Run Gitleaks")

    def test_secrets_scan_uses_digest_pinned_image(self) -> None:
        step = self._gitleaks_step()
        image = step["env"]["GITLEAKS_IMAGE"]

        assert image.startswith("ghcr.io/gitleaks/gitleaks:v8.30.0@sha256:")
        digest = image.split("@sha256:", 1)[1]
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_secrets_scan_runs_gitleaks_via_docker_run(self) -> None:
        run_script = self._gitleaks_step()["run"]

        assert "args=(detect --source=. --verbose --exit-code=1)" in run_script
        assert "args+=(--config=.gitleaks.toml)" in run_script
        assert 'docker run --rm -v "$PWD:/repo" -w /repo "$GITLEAKS_IMAGE"' in run_script

    def test_secrets_scan_does_not_download_gitleaks_release_tarball(self) -> None:
        run_script = self._gitleaks_step()["run"]

        assert "github.com/gitleaks/gitleaks/releases" not in run_script
        assert "wget" not in run_script
        assert "sha256sum --check" not in run_script
        assert "tar -xzf" not in run_script
        assert "./gitleaks" not in run_script


class TestPiCliSetup:
    """Regression tests for installing the real Pi CLI in test environments."""

    def test_required_unit_tests_install_real_pi_cli(self) -> None:
        text = REQUIRED_WORKFLOW.read_text(encoding="utf-8")
        assert "uses: ./.github/actions/setup-pi-cli" in text
        unit_section = text[text.index("  unit-tests:") : text.index("  integration-tests:")]
        assert "Install Pi CLI" in unit_section
        assert "Run unit tests" in unit_section
        assert unit_section.index("Install Pi CLI") < unit_section.index("Run unit tests")

    def test_matrix_unit_tests_install_real_pi_cli(self) -> None:
        text = TEST_WORKFLOW.read_text(encoding="utf-8")
        assert "uses: ./.github/actions/setup-pi-cli" in text
        assert text.index("uses: ./.github/actions/setup-pi-cli") < text.index("Run unit tests")

    def test_setup_pi_action_pins_real_npm_package(self) -> None:
        text = SETUP_PI_ACTION.read_text(encoding="utf-8")
        assert "actions/setup-node@" in text
        assert "node-version: 22.19.0" in text
        assert "npm install -g --ignore-scripts @earendil-works/pi-coding-agent@0.80.2" in text
        assert "pi --version" in text


class TestAutoTagReleaseDispatch:
    """Regression tests for the one-click release workflow chain."""

    def _workflow(self) -> dict[object, Any]:
        return yaml.safe_load(AUTO_TAG_WORKFLOW.read_text(encoding="utf-8"))

    def _steps(self) -> list[dict[str, Any]]:
        return self._workflow()["jobs"]["auto-tag"]["steps"]

    def test_bump_kind_input_is_restricted_choice(self) -> None:
        """Manual dispatch must expose only the supported semantic-version bumps."""
        workflow = self._workflow()
        # PyYAML 1.1 parses the unquoted GitHub Actions `on` key as boolean True.
        bump_kind = workflow[True]["workflow_dispatch"]["inputs"]["bump_kind"]

        assert bump_kind["type"] == "choice"
        assert bump_kind["options"] == ["patch", "minor", "major"]
        assert bump_kind["default"] == "patch"

    def test_unknown_bump_kind_is_rejected(self) -> None:
        """Non-UI dispatch callers must not turn an unknown value into a patch bump."""
        compute_step = next(
            step
            for step in self._steps()
            if step.get("name") == "Compute next version and push tag"
        )
        run = compute_step["run"]

        assert "patch|*)" not in run
        assert re.search(r"(?m)^\s+patch\)\s*$", run) is not None

        invalid_branch = re.search(
            r"(?ms)^\s+\*\)\s*$\n(?P<body>.*?)(?=^\s+;;\s*$)",
            run,
        )
        assert invalid_branch is not None
        body = invalid_branch.group("body")
        assert "::error::Invalid bump_kind" in body
        assert "exit 1" in body

    def test_auto_tag_can_dispatch_workflows(self) -> None:
        """The release dispatch API requires the workflow actions permission."""
        workflow = self._workflow()
        assert workflow["permissions"]["contents"] == "write"
        assert workflow["permissions"]["actions"] == "write"

    def test_auto_tag_dispatches_release_workflow_with_computed_tag(self) -> None:
        """GITHUB_TOKEN tag pushes do not trigger release.yml; dispatch it explicitly."""
        steps = self._steps()
        names = [step.get("name") for step in steps]

        compute_index = names.index("Compute next version and push tag")
        dispatch_index = names.index("Dispatch release workflow")
        assert compute_index < dispatch_index

        compute_step = steps[compute_index]
        assert compute_step["id"] == "tag"
        assert 'echo "tag=${TAG}" >> "$GITHUB_OUTPUT"' in compute_step["run"]

        dispatch_step = steps[dispatch_index]
        assert dispatch_step["if"] == "steps.tag.outputs.tag != ''"
        assert dispatch_step["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"  # noqa: S105
        assert dispatch_step["env"]["TAG"] == "${{ steps.tag.outputs.tag }}"
        assert 'gh workflow run release.yml --repo "${GITHUB_REPOSITORY}"' in dispatch_step["run"]
        assert '-f tag="${TAG}"' in dispatch_step["run"]

    def _dispatch_step(self) -> dict[str, Any]:
        steps = self._steps()
        return next(step for step in steps if step.get("name") == "Dispatch release workflow")

    def test_dispatch_failure_emits_stranded_tag_recovery_error(self) -> None:
        """A failed dispatch strands the already-pushed tag; the error must say how to recover.

        Re-running Auto Tag Release would compute and push a NEW tag rather than
        rescue the stranded one, so the ``::error::`` annotation must warn against
        a re-run, give the exact recovery command with the explicit tag, and point
        at the recovery section in docs/RELEASING.md.
        """
        run = self._dispatch_step()["run"]
        assert "::error::" in run
        assert "exit 1" in run
        assert "Do NOT re-run Auto Tag Release" in run
        assert "gh workflow run release.yml -f tag=${TAG}" in run
        assert "docs/RELEASING.md" in run
        assert "Dispatch failed after tag push" in run

    def test_releasing_doc_has_stranded_tag_recovery_section(self) -> None:
        """The recovery section the ::error:: annotation points at must exist."""
        doc = (REPO_ROOT / "docs" / "RELEASING.md").read_text(encoding="utf-8")
        assert "### Dispatch failed after tag push" in doc
        assert "gh workflow run release.yml -f tag=vX.Y.Z" in doc

    def test_release_concurrency_keys_on_resolved_tag(self) -> None:
        """Dispatched and tag-push release runs must serialize per tag, not per branch.

        With ``release-${{ github.ref }}`` every workflow_dispatch run grouped on
        the branch ref, so two dispatched runs for DIFFERENT tags serialized while
        a dispatched and a tag-push run of the SAME tag did not share a group.
        Keying on ``inputs.tag`` first makes dispatched runs group per tag.
        """
        workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        concurrency = workflow["concurrency"]
        assert concurrency["group"] == "release-${{ inputs.tag || github.ref }}"
        assert concurrency["cancel-in-progress"] is False


class TestReleaseAttestations:
    """Regression tests for PEP 740 build attestations in the release workflow."""

    def _publish_step(self) -> dict:
        """Return the parsed ``gh-action-pypi-publish`` step from release.yml."""
        workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        steps = workflow["jobs"]["build-and-publish"]["steps"]
        publish = [s for s in steps if "gh-action-pypi-publish" in str(s.get("uses", ""))]
        assert len(publish) == 1, "expected exactly one PyPI publish step"
        return publish[0]

    def test_publish_step_generates_attestations(self) -> None:
        """The PyPI publish step must opt into PEP 740 / Sigstore provenance.

        Parses the workflow and asserts the flag lives under the publish step's
        ``with:`` block, not merely somewhere in the file text — a comment or
        unrelated step containing the string would otherwise pass a substring
        check while leaving attestations disabled.
        """
        assert self._publish_step()["with"]["generate_attestations"] is True

    def test_id_token_write_permission_present(self) -> None:
        """Attestation generation requires the ``id-token: write`` permission."""
        workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        assert workflow["permissions"]["id-token"] == "write"


class TestAutomationRuntimeInstall:
    """Regression tests for the UV-managed automation runtime in CI test jobs."""

    def test_matrix_tests_sync_the_locked_project_environment(self) -> None:
        text = TEST_WORKFLOW.read_text(encoding="utf-8")
        assert "uv sync --all-groups --all-extras --locked" in text

    def test_required_tests_sync_the_locked_project_environment(self) -> None:
        text = REQUIRED_WORKFLOW.read_text(encoding="utf-8")
        unit_section = text[text.index("  unit-tests:") : text.index("  integration-tests:")]
        integration_section = text[
            text.index("  integration-tests:") : text.index("  shell-tests:")
        ]
        assert "uv sync --all-groups --all-extras --locked" in unit_section
        assert "uv sync --all-groups --all-extras --locked" in integration_section

    def test_shell_tests_install_just_before_running_bats(self) -> None:
        """The BATS suite exercises ``just --list`` on a bare runner."""
        workflow = yaml.safe_load(REQUIRED_WORKFLOW.read_text(encoding="utf-8"))
        steps = workflow["jobs"]["shell-tests"]["steps"]
        bats_index = next(
            index for index, step in enumerate(steps) if step.get("name") == "Run bats shell tests"
        )
        just_index, just_step = next(
            (index, step)
            for index, step in enumerate(steps)
            if step.get("uses", "").startswith("extractions/setup-just@")
        )

        assert just_index < bats_index
        assert just_step["with"]["just-version"] == "1.36.0"


class TestDependencyAuditEnvironment:
    """Dependency scans must inspect the complete locked dependency graph."""

    @pytest.mark.parametrize(
        ("workflow_path", "job_name"),
        (
            (REQUIRED_WORKFLOW, "security-dependency-scan"),
            (SECURITY_WORKFLOW, "pip-audit"),
        ),
    )
    def test_pip_audit_syncs_all_locked_groups_and_extras(
        self, workflow_path: Path, job_name: str
    ) -> None:
        """pip-audit cannot omit optional dependencies represented in uv.lock."""
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        steps = workflow["jobs"][job_name]["steps"]

        audit_index = next(
            index for index, step in enumerate(steps) if step.get("name") == "Run pip-audit"
        )
        install_index, install_step = next(
            (index, step)
            for index, step in enumerate(steps)
            if step.get("name") == "Install locked dependency audit environment"
        )

        assert install_index < audit_index
        assert install_step["run"] == "uv sync --all-groups --all-extras --locked"


class TestCollectWorkflowFiles:
    """Tests for collect_workflow_files()."""

    def test_finds_file(self, tmp_path: Path) -> None:
        f = tmp_path / "ci.yml"
        f.write_text("name: CI")
        result = collect_workflow_files([str(f)])
        assert f in result

    def test_finds_directory(self, tmp_path: Path) -> None:
        (tmp_path / "ci.yml").write_text("name: CI")
        (tmp_path / "release.yaml").write_text("name: Release")
        result = collect_workflow_files([str(tmp_path)])
        names = [p.name for p in result]
        assert "ci.yml" in names
        assert "release.yaml" in names

    def test_deduplicates(self, tmp_path: Path) -> None:
        f = tmp_path / "ci.yml"
        f.write_text("name: CI")
        result = collect_workflow_files([str(f), str(f)])
        assert len(result) == 1

    def test_missing_path_warns(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        collect_workflow_files([str(tmp_path / "nonexistent.yml")])
        captured = capsys.readouterr()
        assert "WARNING" in captured.err


class TestCLIEntryPoints:
    """Tests for check_workflow_inventory_main() and validate_workflow_checkout_main()."""

    def test_inventory_in_sync(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from hephaestus.ci.workflows import check_workflow_inventory_main

        workflows = tmp_path / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("name: CI")
        (workflows / "README.md").write_text("| ci.yml | CI workflow |\n")
        monkeypatch.setattr(
            "sys.argv", ["hephaestus-check-workflow-inventory", "--repo-root", str(tmp_path)]
        )
        assert check_workflow_inventory_main() == 0

    def test_inventory_drift(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from hephaestus.ci.workflows import check_workflow_inventory_main

        workflows = tmp_path / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("name: CI")
        (workflows / "README.md").write_text("| other.yml | Other |\n")
        monkeypatch.setattr(
            "sys.argv", ["hephaestus-check-workflow-inventory", "--repo-root", str(tmp_path)]
        )
        assert check_workflow_inventory_main() == 1

    def test_inventory_default_repo_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hephaestus.ci.workflows import check_workflow_inventory_main

        workflows = tmp_path / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("name: CI")
        (workflows / "README.md").write_text("| ci.yml | CI workflow |\n")

        monkeypatch.setattr("hephaestus.utils.helpers.get_repo_root", lambda: tmp_path)
        monkeypatch.setattr("sys.argv", ["hephaestus-check-workflow-inventory"])

        assert check_workflow_inventory_main() == 0

    def test_checkout_valid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from hephaestus.ci.workflows import validate_workflow_checkout_main

        wf = tmp_path / "ci.yml"
        wf.write_text(
            "jobs:\n  build:\n    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "      - uses: ./.github/actions/setup\n"
        )
        monkeypatch.setattr("sys.argv", ["hephaestus-validate-workflow-checkout", str(wf)])
        assert validate_workflow_checkout_main() == 0

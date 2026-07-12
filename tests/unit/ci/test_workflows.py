"""Tests for hephaestus.ci.workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

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
TEST_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"
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

    def test_workflow_inventory_hook_is_wired_in_precommit(self) -> None:
        from hephaestus.ci.precommit import load_precommit_config

        repos = load_precommit_config(REPO_ROOT / ".pre-commit-config.yaml")
        hook = next(
            (
                hook
                for repo in repos
                for hook in repo.get("hooks", [])
                if hook.get("id") == "hephaestus-check-workflow-inventory"
            ),
            None,
        )

        assert hook is not None
        assert hook["entry"] == "pixi run --environment default hephaestus-check-workflow-inventory"
        assert hook["pass_filenames"] is False
        assert hook["always_run"] is True
        assert (
            hook["files"]
            == r"^(\.pre-commit-config\.yaml|\.github/workflows/(README\.md|.*\.yml))$"
        )


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


class TestEnableAutoMergeOnImplementationGoWorkflow:
    """Regression tests for the implementation-GO auto-merge workflow."""

    def _workflow_text(self) -> str:
        return AUTO_MERGE_ON_GO_WORKFLOW.read_text(encoding="utf-8")

    def test_runs_only_on_pull_request_target_label_events(self) -> None:
        """Privileged workflow is limited to PR label events."""
        text = self._workflow_text()
        assert "pull_request_target:" in text
        assert "types: [labeled]" in text
        assert "state:implementation-go" in text

    def test_does_not_checkout_pr_controlled_code(self) -> None:
        """pull_request_target workflow must not execute code from the PR."""
        text = self._workflow_text()
        assert "actions/checkout" not in text
        assert "github.event.pull_request.head" not in text
        assert validate_workflow(AUTO_MERGE_ON_GO_WORKFLOW) == []

    def test_enables_squash_auto_merge_with_numeric_pr_guard(self) -> None:
        """The workflow validates PR_NUMBER and uses repo-required squash auto-merge."""
        text = self._workflow_text()
        assert "''|*[!0-9]*)" in text
        assert 'gh pr merge "$PR_NUMBER" --repo "$REPO" --auto --squash' in text

    def test_skips_if_auto_merge_is_already_armed(self) -> None:
        """Re-labeling an already armed PR is a no-op."""
        text = self._workflow_text()
        assert "autoMergeRequest" in text
        assert 'if [ "$auto_merge" != "null" ] && [ -n "$auto_merge" ]; then' in text
        assert "already has auto-merge enabled" in text

    def test_auto_merge_policy_waits_for_label_workflow_to_converge(self) -> None:
        """The auto-merge-policy job must not race the label-triggered workflow.

        The auto-merge ↔ implementation-go state machine was split out of the
        required ``pr-policy`` gate into the advisory ``auto-merge-policy`` job
        (#1080); the convergence behavior moved with it.
        """
        text = REQUIRED_WORKFLOW.read_text(encoding="utf-8")
        assert "auto-merge-policy" in text
        assert "Waiting for label-triggered auto-merge workflow" in text
        assert 'gh pr view "$PR_NUMBER" --repo "$GITHUB_REPOSITORY"' in text
        assert "--json autoMergeRequest,labels,state" in text
        assert "sleep 10" in text

    def test_auto_merge_policy_treats_merged_prs_as_terminal(self) -> None:
        """GitHub clears autoMergeRequest after merge, so merged PRs must pass."""
        text = REQUIRED_WORKFLOW.read_text(encoding="utf-8")
        assert "--json autoMergeRequest,labels,state" in text
        assert "pr_state=$(jq -r '.state // \"\"' pr.json)" in text
        assert "auto-merge policy is terminal" in text

    def test_pr_policy_no_longer_blocks_on_auto_merge_state(self) -> None:
        """pr-policy keeps only the hard gates; auto-merge moved to its own job.

        The split (#1080) makes the auto-merge verdict non-blocking: pr-policy
        fetches only the body it needs and no longer reads auto-merge/labels.
        """
        text = REQUIRED_WORKFLOW.read_text(encoding="utf-8")
        # pr-policy's own metadata fetch is body-only now.
        assert "--json body\n" in text or "--json body \\" in text
        # The auto-merge error lives in the advisory job, which is present.
        assert "Auto-merge is enabled before implementation review GO." in text


class TestRequiredPixiCheckWorkflow:
    """Regression tests for the required pixi-check workflow job."""

    def _pixi_install_script(self) -> str:
        with open(REQUIRED_WORKFLOW, encoding="utf-8") as f:
            workflow = yaml.safe_load(f)
        steps = workflow["jobs"]["pixi-check"]["steps"]
        for step in steps:
            if step.get("name") == "pixi install (locked)":
                return str(step["run"])
        raise AssertionError("pixi-check job must define a 'pixi install (locked)' step")

    def test_missing_pixi_lock_fails_without_unlocked_install(self) -> None:
        """pixi-check must fail fast if the lockfile is missing."""
        script = self._pixi_install_script()
        lines = {line.strip() for line in script.splitlines()}

        assert "pixi install --locked" in lines
        assert "exit 1" in lines
        assert "pixi install" not in lines
        assert "running unlocked" not in script


class TestGitleaksSecretsScan:
    """Regression tests for the required gitleaks secrets scan.

    gitleaks is not published on conda-forge or PyPI, so it cannot be a
    Pixi-managed dependency in this repo (channels are pinned to
    conda-forge). The job instead runs a digest-addressed container image
    via `docker run`, which removes the dependency on a transient GitHub
    Releases tarball URL (audit #1483) without requiring an unlockable Pixi
    package. `docker run` (not a job-level `container:`) keeps
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

    def _workflow(self) -> dict[str, Any]:
        return yaml.safe_load(AUTO_TAG_WORKFLOW.read_text(encoding="utf-8"))

    def _steps(self) -> list[dict[str, Any]]:
        return self._workflow()["jobs"]["auto-tag"]["steps"]

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


class TestAutomationExtraInstall:
    """Regression tests for the automation extra in pip-based CI test jobs."""

    def test_matrix_tests_install_automation_extra(self) -> None:
        text = TEST_WORKFLOW.read_text(encoding="utf-8")
        assert 'run: pip install -e ".[dev,schema,automation]"' in text

    def test_required_tests_install_automation_extra(self) -> None:
        text = REQUIRED_WORKFLOW.read_text(encoding="utf-8")
        unit_section = text[text.index("  unit-tests:") : text.index("  integration-tests:")]
        integration_section = text[
            text.index("  integration-tests:") : text.index("  shell-tests:")
        ]
        assert 'run: pip install -e ".[dev,schema,automation]"' in unit_section
        assert 'run: pip install -e ".[dev,automation]"' in integration_section


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

"""Guard-only smoke tests for omitted orchestration modules.

These tests validate that the orchestration modules omitted from coverage
(derived from ``pyproject.toml[tool.coverage.run].omit``) remain importable and
that their console entry points still work correctly.

This suite proves the omitted modules still import, their ``--help`` paths stay
wired up, and a representative set of orchestration entry points can reach
their mocked hand-off seams without touching live ``gh``/agent subprocesses.
It does not claim full orchestration coverage and is not a substitute for the
per-entry-point mocked-subprocess coverage required before issue #1422 can
shrink the coverage omit list.
"""

import contextlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def _omitted_modules() -> list[str]:
    """Derive omitted-module import paths from pyproject.toml (no drift)."""
    root = Path(__file__).resolve().parents[2]
    with open(root / "pyproject.toml", "rb") as f:
        omit = tomllib.load(f)["tool"]["coverage"]["run"]["omit"]
    prefix, suffix = "hephaestus/automation/", ".py"
    return sorted(
        entry[: -len(suffix)].replace("/", ".")
        for entry in omit
        if entry.startswith(prefix) and entry.endswith(suffix)
    )


# All omitted orchestration modules (derived from pyproject.toml omit list).
OMITTED_MODULES = _omitted_modules()

# Modules with console scripts (run --help to verify entry point works)
CONSOLE_SCRIPTS = [
    ("hephaestus-implement-issues", "hephaestus.automation.implementer"),
    ("hephaestus-plan-issues", "hephaestus.automation.planner"),
    ("hephaestus-automation-loop", "hephaestus.automation.loop_runner"),
    ("hephaestus-review-prs", "hephaestus.automation.pr_reviewer"),
    ("hephaestus-audit-prs", "hephaestus.automation.audit_reviewer"),
    ("hephaestus-agent-stage", "hephaestus.automation.agent_stage"),
]

# Modules with main() but no console script of their own.
# ``implementer.main()`` backs the ``hephaestus-implement-issues`` script and is
# covered by CONSOLE_SCRIPTS; since #714, ``implementer_cli`` holds only the
# argument-parsing / logging helpers and no longer defines main(). These entry
# points still deserve a smoke execution path under mocks below so the test does
# more than assert the symbol exists.
MAIN_ONLY_MODULES = [
    "hephaestus.automation.address_review",
    "hephaestus.automation.ci_driver",
]


@pytest.mark.integration
class TestOrchestrationsImportable:
    """All omitted modules must remain importable."""

    @pytest.mark.parametrize("module_name", OMITTED_MODULES)
    def test_module_importable(self, module_name: str) -> None:
        """Verify module can be imported without errors."""
        try:
            __import__(module_name)
        except ImportError as e:
            pytest.fail(f"Module {module_name} failed to import: {e}")


@pytest.mark.integration
class TestConsoleScriptsWork:
    """Console scripts must respond to --help without live session."""

    @pytest.mark.parametrize("script_name,module_name", CONSOLE_SCRIPTS)
    def test_console_script_help(self, script_name: str, module_name: str) -> None:
        """Verify console script exits 0 on --help.

        Invokes via ``python -c`` with ``sys.argv`` manipulation so the test
        works without a dev-install (``pip install -e .``) that registers
        console entry-points on PATH.
        """
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    f"import sys; sys.argv = ['{script_name}', '--help']; "
                    f"from {module_name} import main; raise SystemExit(main())"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )

        output = result.stdout + result.stderr
        assert result.returncode == 0, (
            f"Script {script_name} ({module_name}) exited with {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

        # Should print usage text (argparse default)
        assert "usage:" in output.lower(), (
            f"Script {script_name} did not print usage text\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


@pytest.mark.integration
class TestConsoleEntryPointsUnderMocks:
    """Representative console entry points should hand off under mocks."""

    def test_planner_main_executes_under_mocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``planner.main()`` should parse args and invoke ``Planner.run()``."""
        from hephaestus.automation import planner
        from hephaestus.automation.models import PlanResult

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "hephaestus-plan-issues",
                "--issues",
                "1422",
                "--dry-run",
                "--no-advise",
                "--agent",
                "claude",
            ],
        )

        with (
            patch.object(planner, "configure_github_throttle_from_args"),
            patch.object(planner, "resolve_agent", return_value="claude"),
            patch.object(planner, "Planner") as mock_planner_class,
        ):
            mock_planner = mock_planner_class.return_value
            mock_planner.run.return_value = {
                1422: PlanResult(issue_number=1422, success=True),
            }

            rc = planner.main()

        assert rc == 0
        options = mock_planner_class.call_args.args[0]
        assert options.issues == [1422]
        assert options.dry_run is True
        assert options.enable_advise is False
        assert options.agent == "claude"

    def test_implementer_main_executes_under_mocks(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """``implementer.main()`` should parse args and invoke ``IssueImplementer.run()``."""
        from hephaestus.automation import implementer
        from hephaestus.automation.models import WorkerResult

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "hephaestus-implement-issues",
                "--issues",
                "1422",
                "--dry-run",
                "--no-ui",
                "--no-advise",
                "--no-learn",
                "--agent",
                "claude",
            ],
        )

        with (
            patch("hephaestus.cli.utils.configure_github_throttle_from_args"),
            patch("hephaestus.agents.runtime.resolve_agent", return_value="claude"),
            patch(
                "hephaestus.utils.terminal.terminal_guard",
                return_value=contextlib.nullcontext(),
            ),
            patch.object(implementer, "get_repo_root", return_value=tmp_path),
            patch.object(implementer, "ensure_state_dir", return_value=tmp_path),
            patch.object(implementer, "IssueImplementer") as mock_implementer_class,
        ):
            mock_implementer = mock_implementer_class.return_value
            mock_implementer.run.return_value = {
                1422: WorkerResult(issue_number=1422, success=True, pr_number=1827),
            }

            rc = implementer.main()

        assert rc == 0
        options = mock_implementer_class.call_args.args[0]
        assert options.issues == [1422]
        assert options.dry_run is True
        assert options.enable_ui is False
        assert options.enable_advise is False
        assert options.enable_learn is False
        assert options.agent == "claude"

    def test_pr_reviewer_main_executes_under_mocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``pr_reviewer.main()`` should parse args and invoke ``PRReviewer.run()``."""
        from hephaestus.automation import pr_reviewer
        from hephaestus.automation.models import WorkerResult

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "hephaestus-review-prs",
                "--issues",
                "1422",
                "--dry-run",
                "--no-ui",
                "--agent",
                "claude",
            ],
        )

        with (
            patch.object(pr_reviewer, "configure_github_throttle_from_args"),
            patch.object(pr_reviewer, "resolve_agent", return_value="claude"),
            patch(
                "hephaestus.utils.terminal.terminal_guard",
                return_value=contextlib.nullcontext(),
            ),
            patch.object(pr_reviewer, "PRReviewer") as mock_reviewer_class,
        ):
            mock_reviewer = mock_reviewer_class.return_value
            mock_reviewer.run.return_value = {
                1422: WorkerResult(issue_number=1422, success=True, pr_number=1827),
            }

            rc = pr_reviewer.main()

        assert rc == 0
        options = mock_reviewer_class.call_args.args[0]
        assert options.issues == [1422]
        assert options.dry_run is True
        assert options.enable_ui is False
        assert options.agent == "claude"

    def test_audit_reviewer_main_executes_under_mocks(self) -> None:
        """``audit_reviewer.main()`` should parse args and invoke ``AuditReviewer.run()``."""
        from hephaestus.automation import audit_reviewer

        with (
            patch.object(audit_reviewer, "configure_github_throttle_from_args"),
            patch.object(audit_reviewer, "AuditReviewer") as mock_reviewer_class,
        ):
            mock_reviewer = mock_reviewer_class.return_value
            mock_reviewer.run.return_value = (0, [])

            rc = audit_reviewer.main(["--pr-numbers", "1827", "--dry-run", "--agent", "claude"])

        assert rc == 0
        assert mock_reviewer_class.call_args.kwargs == {
            "agent": "claude",
            "pr_numbers": [1827],
            "dry_run": True,
        }


@pytest.mark.integration
class TestMainCallable:
    """Modules with main() must have a callable main function."""

    @pytest.mark.parametrize("module_name", MAIN_ONLY_MODULES)
    def test_main_is_callable(self, module_name: str) -> None:
        """Verify module has a callable main() function."""
        module = __import__(module_name, fromlist=["main"])
        assert hasattr(module, "main"), f"Module {module_name} does not have main()"
        assert callable(module.main), f"Module {module_name}.main is not callable"


@pytest.mark.integration
class TestMainEntryPointsUnderMocks:
    """Main-only entry points should keep their mocked dry-run seams wired."""

    def test_address_review_main_executes_under_mocks(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``address_review.main()`` should parse args and return success under mocks."""
        from hephaestus.automation import address_review
        from hephaestus.automation.models import WorkerResult

        captured = {}
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "address_review",
                "--issues",
                "1422",
                "--dry-run",
                "--no-ui",
                "--agent",
                "claude",
            ],
        )

        def fake_init(self, options) -> None:
            captured["options"] = options

        with (
            patch("hephaestus.cli.utils.configure_github_throttle_from_args"),
            patch(
                "hephaestus.utils.terminal.terminal_guard",
                return_value=contextlib.nullcontext(),
            ),
            patch.object(address_review, "resolve_agent", return_value="claude"),
            patch.object(address_review.AddressReviewer, "__init__", fake_init),
            patch.object(
                address_review.AddressReviewer,
                "run",
                return_value={1422: WorkerResult(issue_number=1422, success=True, pr_number=1827)},
            ),
        ):
            rc = address_review.main()

        assert rc == 0
        assert captured["options"].issues == [1422]
        assert captured["options"].dry_run is True
        assert captured["options"].enable_ui is False

    def test_ci_driver_main_executes_under_mocks(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``ci_driver.main()`` should build driver options and return evaluator status."""
        from hephaestus.automation import ci_driver

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "ci_driver",
                "--prs",
                "1827",
                "--dry-run",
                "--no-ui",
                "--agent",
                "claude",
            ],
        )

        with (
            patch.object(ci_driver, "configure_github_throttle_from_args"),
            patch.object(ci_driver, "resolve_agent", return_value="claude"),
            patch.object(ci_driver, "_evaluate_run_result", return_value=0) as mock_evaluate,
            patch.object(ci_driver, "CIDriver") as mock_driver_class,
        ):
            mock_driver = mock_driver_class.return_value
            mock_driver.run.return_value = {}
            mock_driver.open_prs_remaining = []

            rc = ci_driver.main()

        assert rc == 0
        options = mock_driver_class.call_args.args[0]
        assert options.prs == [1827]
        assert options.dry_run is True
        assert options.enable_ui is False
        mock_evaluate.assert_called_once_with(
            {},
            [],
            issues=[],
            as_json=False,
        )


@pytest.mark.integration
class TestOrchestrationSubprocessBoundaries:
    """Selected subprocess seams should stay reachable under mocks."""

    def test_agent_stage_main_invokes_direct_agent_under_mocks(
        self,
        tmp_path: Path,
    ) -> None:
        """``agent_stage.main()`` should persist agent output without a live agent."""
        from hephaestus.agents.runtime import AgentRunResult
        from hephaestus.automation import agent_stage

        prompt_file = tmp_path / "prompt.md"
        output_file = tmp_path / "out.txt"
        log_file = tmp_path / "agent.log"
        prompt_file.write_text("implement issue #1422", encoding="utf-8")

        with (
            patch.object(agent_stage, "resolve_agent", return_value="codex"),
            patch.object(agent_stage, "uses_direct_agent_runner", return_value=True),
            patch.object(
                agent_stage,
                "run_agent_session",
                return_value=AgentRunResult(
                    stdout="mocked implementation result",
                    stderr="",
                    session_id="sess-1422",
                ),
            ) as mock_run_agent_session,
        ):
            rc = agent_stage.main(
                [
                    "--prompt-file",
                    str(prompt_file),
                    "--repo-root",
                    str(tmp_path),
                    "--stage",
                    "implement",
                    "--output",
                    str(output_file),
                    "--log-file",
                    str(log_file),
                    "--agent",
                    "codex",
                    "--timeout",
                    "12",
                    "--sandbox",
                    "read-only",
                    "--approval",
                    "never",
                ]
            )

        assert rc == 0
        assert output_file.read_text(encoding="utf-8") == "mocked implementation result"
        assert "SESSION_ID: sess-1422" in log_file.read_text(encoding="utf-8")
        mock_run_agent_session.assert_called_once()
        call_kwargs = mock_run_agent_session.call_args.kwargs
        assert call_kwargs["agent"] == "codex"
        assert call_kwargs["cwd"] == tmp_path
        assert call_kwargs["timeout"] == 12
        assert call_kwargs["sandbox"] == "read-only"
        assert call_kwargs["approval"] == "never"

    def test_loop_runner_run_phase_invokes_phase_subprocess_under_mocks(
        self,
        tmp_path: Path,
    ) -> None:
        """``loop_runner.run_phase()`` should launch a phase subprocess with scoped args."""
        from hephaestus.automation import loop_runner

        repo_dir = tmp_path / "ProjectHephaestus"
        repo_dir.mkdir()
        completed = subprocess.CompletedProcess(args=["hephaestus-plan-issues"], returncode=0)

        def fake_subprocess_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            env = kwargs["env"]
            assert isinstance(env, dict)
            work_report = Path(str(env["HEPH_WORK_REPORT"]))
            work_report.write_text("1\n", encoding="utf-8")
            return completed

        cfg = loop_runner.LoopConfig(
            max_workers=2,
            agent="codex",
            dry_run=True,
            no_advise=True,
            gh_global_rate=3.0,
            gh_global_burst=4.0,
            phase_timeout_s=9,
        )

        with (
            patch.object(
                loop_runner,
                "_resolve_phase_bin",
                return_value=("hephaestus-plan-issues", []),
            ),
            patch.object(
                loop_runner.subprocess, "run", side_effect=fake_subprocess_run
            ) as mock_run,
        ):
            result = loop_runner.run_phase(
                repo="ProjectHephaestus",
                repo_dir=repo_dir,
                phase="plan",
                cfg=cfg,
                loop_idx=1,
                open_issues=[1422],
                trunk_sha="abc123",
            )

        assert result.name == "plan"
        assert result.rc == 0
        assert result.work_units == 1
        mock_run.assert_called_once()
        argv = mock_run.call_args.args[0]
        kwargs = mock_run.call_args.kwargs
        assert argv[:3] == ["hephaestus-plan-issues", "-v", "--agent"]
        assert "codex" in argv
        assert "--dry-run" in argv
        assert "--no-advise" in argv
        assert "--issues" in argv
        assert "1422" in argv
        assert kwargs["cwd"] == str(repo_dir)
        assert kwargs["timeout"] == 9
        assert kwargs["check"] is False

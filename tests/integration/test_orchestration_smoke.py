"""Smoke tests for omitted orchestration modules — integration backstop.

These tests validate that the 11 automation modules omitted from coverage
(per pyproject.toml[tool.coverage.run].omit) remain importable and their
console entry points work correctly.

Module enumeration and entry-point discovery verified at plan time:
- All 11 modules are importable (guards against import regressions)
- 4 modules have console scripts: implementer, planner, loop_runner, pr_reviewer
- 3 modules are script-less but have main(): address_review, ci_driver,
    implementer_cli (its main() backs the implementer console script via re-export)
- 4 modules lack main() entirely: implementer_phase_runner, implementer_summary,
    curses_ui, github_api
"""

import subprocess

import pytest

# All 11 omitted orchestration modules
OMITTED_MODULES = [
    "hephaestus.automation.implementer",
    "hephaestus.automation.implementer_cli",
    "hephaestus.automation.implementer_phase_runner",
    "hephaestus.automation.implementer_summary",
    "hephaestus.automation.planner",
    "hephaestus.automation.address_review",
    "hephaestus.automation.ci_driver",
    "hephaestus.automation.loop_runner",
    "hephaestus.automation.curses_ui",
    "hephaestus.automation.github_api",
    "hephaestus.automation.pr_reviewer",
]

# Modules with console scripts (run --help to verify entry point works)
CONSOLE_SCRIPTS = [
    ("hephaestus-implement-issues", "hephaestus.automation.implementer"),
    ("hephaestus-plan-issues", "hephaestus.automation.planner"),
    ("hephaestus-automation-loop", "hephaestus.automation.loop_runner"),
    ("hephaestus-review-prs", "hephaestus.automation.pr_reviewer"),
]

# Modules with main() but no console script of their own.
# implementer_cli.main() is exposed as the ``hephaestus-implement-issues`` script
# via re-export from ``hephaestus.automation.implementer`` — it has no separate
# entry point, so it is verified here for callability rather than via --help.
MAIN_ONLY_MODULES = [
    "hephaestus.automation.address_review",
    "hephaestus.automation.ci_driver",
    "hephaestus.automation.implementer_cli",
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
        """Verify console script exits 0 on --help."""
        result = subprocess.run(
            [script_name, "--help"],
            capture_output=True,
            text=True,
            timeout=5,
        )

        assert result.returncode == 0, (
            f"Script {script_name} exited with {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

        # Should print usage text (argparse default)
        assert "usage:" in result.stdout.lower() or "usage:" in result.stderr.lower(), (
            f"Script {script_name} did not print usage text\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


@pytest.mark.integration
class TestMainCallable:
    """Modules with main() must have a callable main function."""

    @pytest.mark.parametrize("module_name", MAIN_ONLY_MODULES)
    def test_main_is_callable(self, module_name: str) -> None:
        """Verify module has a callable main() function."""
        module = __import__(module_name, fromlist=["main"])
        assert hasattr(module, "main"), f"Module {module_name} does not have main()"
        assert callable(module.main), f"Module {module_name}.main is not callable"

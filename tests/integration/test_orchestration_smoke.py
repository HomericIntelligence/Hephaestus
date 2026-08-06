"""Smoke tests for public orchestration CLI entry points."""

import subprocess
import sys

import pytest

# Modules with console scripts (run --help to verify entry point works)
CONSOLE_SCRIPTS = [
    ("hephaestus-implement-issues", "hephaestus.automation.implementer"),
    ("hephaestus-plan-issues", "hephaestus.automation.planner"),
    ("hephaestus-automation-loop", "hephaestus.automation.loop_runner"),
    ("hephaestus-review-prs", "hephaestus.automation.pr_reviewer"),
    ("hephaestus-audit-prs", "hephaestus.automation.audit_reviewer"),
]

# Modules with main() but no console script of their own.
# ``implementer.main()`` backs the ``hephaestus-implement-issues`` script and is
# covered by CONSOLE_SCRIPTS.
MAIN_ONLY_MODULES = ["hephaestus.automation.ci_driver"]


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
class TestMainCallable:
    """Modules with main() must have a callable main function."""

    @pytest.mark.parametrize("module_name", MAIN_ONLY_MODULES)
    def test_main_is_callable(self, module_name: str) -> None:
        """Verify module has a callable main() function."""
        module = __import__(module_name, fromlist=["main"])
        assert hasattr(module, "main"), f"Module {module_name} does not have main()"
        assert callable(module.main), f"Module {module_name}.main is not callable"

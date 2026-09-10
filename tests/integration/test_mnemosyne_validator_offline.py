"""Native proof for the fixed offline learning validator."""

from __future__ import annotations

import json
import platform
import socket
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.mnemosyne_delivery import LearnDeliveryError
from hephaestus.automation.mnemosyne_learning_preparation import MnemosynePluginValidator
from hephaestus.automation.mnemosyne_validator_dependencies import (
    _digests,
    bound_inputs,
    run_learning_subprocess,
)


@pytest.mark.parametrize("parent_project", [False, True], ids=["standalone", "nested-project"])
def test_locked_wheel_and_descendant_network_denial(tmp_path: Path, parent_project: bool) -> None:
    """A locked wheel loads, but the validator and its child cannot connect."""
    if platform.system() != "Darwin":
        pytest.skip("The learning network boundary requires macOS")
    probe = subprocess.run(
        [
            "/usr/bin/sandbox-exec",
            "-p",
            "(version 1)(allow default)(deny network*)",
            "/usr/bin/true",
        ],
        capture_output=True,
    )
    if probe.returncode:
        pytest.skip("The enclosing sandbox does not admit the native macOS boundary")
    parent = tmp_path / "parent"
    source = parent / "build" / "delivery" if parent_project else tmp_path / "delivery"
    source.mkdir(parents=True)
    wheels = source / "wheels"
    wheels.mkdir()
    wheel = wheels / "locked_fixture-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("locked_fixture.py", "VALUE = 42\n")
        archive.writestr(
            "locked_fixture-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: locked-fixture\nVersion: 1.0\n",
        )
        archive.writestr(
            "locked_fixture-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr("locked_fixture-1.0.dist-info/RECORD", "")
    (source / "pyproject.toml").write_text(
        '[project]\nname = "validator-fixture"\nversion = "1.0"\nrequires-python = ">=3.13"\n'
        '[dependency-groups]\ndev = ["locked-fixture"]\n'
        "[tool.uv.sources]\nlocked-fixture = "
        '{ path = "wheels/locked_fixture-1.0-py3-none-any.whl" }\n'
    )
    (source / ".markdownlint.yaml").write_text("{}\n")
    skills = source / "skills"
    skills.mkdir()
    lesson = skills / "lesson.md"
    lesson.write_text("# Lesson\n")
    scripts = source / "scripts"
    scripts.mkdir()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.2)
        port = listener.getsockname()[1]
        connection = (
            "import socket\n"
            "try:\n"
            f"    socket.create_connection(('127.0.0.1', {port}), timeout=1)\n"
            "except OSError:\n"
            "    pass\n"
            "else:\n"
            "    raise SystemExit('network was admitted')\n"
        )
        (scripts / "validate_plugins.py").write_text(
            "import json, os, pathlib, subprocess, sys\nimport locked_fixture\n"
            "assert locked_fixture.VALUE == 42\n"
            "assert sys.prefix == os.environ['UV_PROJECT_ENVIRONMENT']\n"
            "for target in [pathlib.Path.cwd() / 'forbidden-write', "
            "pathlib.Path(sys.prefix) / 'forbidden-write']:\n"
            "    try:\n"
            "        target.write_text('forbidden')\n"
            "    except PermissionError:\n"
            "        pass\n"
            "    else:\n"
            "        raise AssertionError('write was admitted')\n"
            + (
                f"try:\n    pathlib.Path({str(parent / 'pyproject.toml')!r}).read_text()\n"
                "except PermissionError:\n    pass\n"
                "else:\n    raise AssertionError('parent read was admitted')\n"
                if parent_project
                else ""
            )
            + connection
            + f"subprocess.run([sys.executable, '-c', {connection!r}], check=True)\n"
            "pathlib.Path(os.environ['TMPDIR'], 'validator-sentinel').write_text(\n"
            "    json.dumps({'wheel': locked_fixture.VALUE, 'prefix': sys.prefix}))\n"
        )
        subprocess.run(
            ["uv", "lock", "--offline", "--python", sys.executable],
            cwd=source,
            check=True,
            capture_output=True,
        )
        if parent_project:
            (parent / "pyproject.toml").write_text(
                '[project]\nname = "parent-project"\nversion = "1.0"\nrequires-python = ">=3.13"\n'
            )
        subprocess.run(["git", "init"], cwd=source, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=source, check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "-m",
                "fixture",
            ],
            cwd=source,
            check=True,
            capture_output=True,
        )

        initial_inputs = bound_inputs(source)
        commands: list[list[str]] = []

        def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            commands.append(argv)
            environment = Path(kwargs["env"]["UV_PROJECT_ENVIRONMENT"])
            validator_argv = [str(environment / "bin/python"), "scripts/validate_plugins.py"]
            validator_run = argv[0] == "/usr/bin/sandbox-exec" and argv[3] == validator_argv[0]
            artifacts = (
                _digests(environment.parent, Path(sys.base_prefix).resolve())
                if validator_run
                else ()
            )
            result = run_learning_subprocess(argv, **kwargs)
            if validator_run and result.returncode:
                assert not (Path(kwargs["env"]["TMPDIR"]) / "validator-sentinel").exists()
            assert result.returncode == 0, result.stderr
            if validator_run:
                assert argv[3:] == validator_argv
                sentinel = Path(kwargs["env"]["TMPDIR"]) / "validator-sentinel"
                assert json.loads(sentinel.read_text()) == {"wheel": 42, "prefix": str(environment)}
                assert _digests(environment.parent, Path(sys.base_prefix).resolve()) == artifacts
                assert bound_inputs(source) == initial_inputs
            return result

        evidence = MnemosynePluginValidator(runner=runner).validate(source)
        assert evidence == (
            " ".join(commands[-2][3:]),
            " ".join(commands[-1][3:]),
        )
        lesson.write_text("This text has no heading.\n")
        try:
            with pytest.raises(LearnDeliveryError, match="learning markdownlint failed"):
                MnemosynePluginValidator().validate(source)
        finally:
            lesson.write_text("# Lesson\n")
        assert subprocess.check_output(["git", "status", "--porcelain"], cwd=source) == b""
        with pytest.raises(TimeoutError):
            listener.accept()

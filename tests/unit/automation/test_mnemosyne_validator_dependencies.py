"""Tests for bound learning dependency preparation."""

from __future__ import annotations

import subprocess
from contextlib import suppress
from pathlib import Path

import pytest

from hephaestus.automation.mnemosyne_delivery import LearnDeliveryError
from hephaestus.automation.mnemosyne_validator_dependencies import (
    bound_inputs,
    prepare_dependencies,
)


def _source(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    (path / "uv.lock").write_text('version = 1\n[[package]]\nname = "fixture"\nversion = "1.0"\n')
    (path / "pyproject.toml").write_text('[project]\nname = "fixture"\nversion = "1.0"\n')
    for args in (
        ("add", "."),
        (
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
        ),
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    return path


def _runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    env = kwargs["env"]
    assert isinstance(env, dict)
    environment = Path(env["UV_PROJECT_ENVIRONMENT"])
    environment.mkdir(exist_ok=True)
    (environment / "artifact").write_text("sealed")
    return subprocess.CompletedProcess(argv, 0, stdout="uv fixture")


def test_preparation_is_external_and_rejects_changed_artifact(tmp_path: Path) -> None:
    """Prepared files stay outside delivery and cannot change after sealing."""
    source = _source(tmp_path / "source")
    before = bound_inputs(source)
    with prepare_dependencies(source, _runner) as prepared:
        root = prepared.root
        assert not root.is_relative_to(source)
        prepared.verify(source)
        (prepared.environment / "artifact").write_text("changed")
        with pytest.raises(LearnDeliveryError, match="artifact changed"):
            prepared.verify(source)
    assert not root.exists()
    assert before == bound_inputs(source)
    assert subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"]) == b""


@pytest.mark.parametrize("name", ["uv.lock", "pyproject.toml"])
def test_changed_dependency_input_is_rejected(tmp_path: Path, name: str) -> None:
    """Modified dependency files cannot control preparation."""
    source = _source(tmp_path / "source")
    (source / name).write_text("changed")
    with pytest.raises(LearnDeliveryError, match="dependency input differs"):
        with prepare_dependencies(source, _runner):
            pytest.fail("changed input admitted")


def test_preparation_keeps_only_locked_package_cause(tmp_path: Path) -> None:
    """Preparation errors retain the locked cause without paths or credentials."""
    source = _source(tmp_path / "source")

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "sync" in argv:
            return subprocess.CompletedProcess(
                argv, 1, stderr="fixture==1.0 https://secret@host /private/key"
            )
        return _runner(argv, **kwargs)

    with pytest.raises(LearnDeliveryError) as error:
        with prepare_dependencies(source, runner):
            pytest.fail("failed preparation admitted")
    assert str(error.value) == "learning dependency preparation failed: fixture==1.0"


def test_escaping_artifact_link_is_rejected(tmp_path: Path) -> None:
    """The prepared environment cannot refer to an ambient artifact."""
    source = _source(tmp_path / "source")

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        result = _runner(argv, **kwargs)
        env = kwargs["env"]
        assert isinstance(env, dict)
        link = Path(env["UV_PROJECT_ENVIRONMENT"]) / "outside"
        if not link.is_symlink():
            link.symlink_to(tmp_path / "ambient")
        return result

    with pytest.raises(LearnDeliveryError, match="escapes"):
        with prepare_dependencies(source, runner):
            pytest.fail("external link admitted")


@pytest.mark.parametrize("failure", ["timeout", "launch"])
def test_preparation_failure_preserves_delivery(tmp_path: Path, failure: str) -> None:
    """Process failures leave delivery inputs unchanged and omit exception text."""
    source = _source(tmp_path / "source")
    roots: list[Path] = []

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = kwargs["env"]
        assert isinstance(env, dict)
        roots.append(Path(env["HOME"]))
        if failure == "timeout":
            raise subprocess.TimeoutExpired("secret /private/path", 120)
        raise OSError("secret /private/path")

    with pytest.raises(LearnDeliveryError) as error:
        with prepare_dependencies(source, runner):
            pytest.fail("failed preparation admitted")
    assert "secret" not in str(error.value)
    assert "/private" not in str(error.value)
    assert all(not root.exists() for root in roots)
    assert subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"]) == b""


def test_symlink_dependency_input_is_rejected(tmp_path: Path) -> None:
    """A link cannot replace the committed lock file."""
    source = _source(tmp_path / "source")
    lock = source / "uv.lock"
    target = tmp_path / "lock"
    lock.rename(target)
    lock.symlink_to(target)
    with pytest.raises(LearnDeliveryError, match="regular tracked file"):
        bound_inputs(source)


def test_successful_parent_does_not_leave_a_writer(tmp_path: Path) -> None:
    """A child with closed pipes cannot write after its parent returns."""
    import os
    import sys
    import time

    from hephaestus.automation import mnemosyne_validator_dependencies as dependencies

    marker = tmp_path / "late-write"
    child = f"import pathlib,time; time.sleep(0.3); pathlib.Path({str(marker)!r}).touch()"
    parent = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
    )
    result = dependencies.run_learning_subprocess(
        [sys.executable, "-c", parent],
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=5,
        check=False,
        log_on_error=False,
        track_process_group=True,
    )
    assert result.returncode == 0
    time.sleep(0.5)
    assert not marker.exists()


def test_timeout_drain_is_bounded_for_an_escaped_pipe(tmp_path: Path) -> None:
    """An escaped child cannot keep timeout pipe draining open indefinitely."""
    import os
    import signal
    import sys
    import time

    from hephaestus.automation.mnemosyne_validator_dependencies import run_learning_subprocess

    identity = tmp_path / "child-pid"
    child = (
        f"import os,pathlib,time; pathlib.Path({str(identity)!r}).write_text(str(os.getpid())); "
        "time.sleep(4)"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True); "
        "time.sleep(5)"
    )
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            run_learning_subprocess(
                [sys.executable, "-c", parent],
                cwd=tmp_path,
                env=dict(os.environ),
                timeout=0.5,
            )
        assert time.monotonic() - started < 3
    finally:
        if identity.exists():
            with suppress(ProcessLookupError):
                os.kill(int(identity.read_text()), signal.SIGKILL)


def test_dangling_optional_python_input_is_rejected(tmp_path: Path) -> None:
    """A dangling Python version link is not an absent optional input."""
    source = _source(tmp_path / "source")
    (source / ".python-version").symlink_to(tmp_path / "missing-version")
    with pytest.raises(LearnDeliveryError, match="regular tracked file"):
        bound_inputs(source)

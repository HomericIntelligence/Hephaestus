"""Prepare locked dependencies outside a learning delivery tree."""

from __future__ import annotations

import io
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from hephaestus.automation.mnemosyne_delivery import LearnDeliveryError
from hephaestus.config.child_environments import build_git_child_env
from hephaestus.io.utils import write_secure
from hephaestus.utils.subprocess_registry import track_process_group as register_process_group

Runner = Callable[..., subprocess.CompletedProcess[str]]
_INPUTS = ("uv.lock", "pyproject.toml", ".python-version")


def run_learning_subprocess(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    check: bool = False,
    log_on_error: bool = False,
    track_process_group: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Stop the owned process group before private resources are released."""
    del log_on_error, track_process_group
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    with register_process_group(process.pid):
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        finally:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
                process.wait(timeout=1)
    result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    if check:
        result.check_returncode()
    return result


def _git(path: Path, *args: str) -> bytes:
    """Read source metadata without a shell."""
    try:
        return subprocess.check_output(
            ["git", "-C", str(path), *args],
            stderr=subprocess.DEVNULL,
            env=build_git_child_env(),
        )
    except (OSError, subprocess.SubprocessError):
        raise LearnDeliveryError("learning dependency input is not bound") from None


def bound_inputs(path: Path) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Require dependency inputs to equal regular files in the source commit."""
    commit = _git(path, "rev-parse", "HEAD").decode().strip()
    inputs: list[tuple[str, str]] = []
    for name in _INPUTS:
        entry = _git(path, "ls-tree", commit, "--", name).decode()
        target = path / name
        if (
            not entry
            and name == ".python-version"
            and not target.exists()
            and not target.is_symlink()
        ):
            continue
        if not entry.startswith("100644 ") and not entry.startswith("100755 "):
            raise LearnDeliveryError("learning dependency input is not a regular tracked file")
        if target.is_symlink() or not target.is_file():
            raise LearnDeliveryError("learning dependency input is not a regular tracked file")
        content = _git(path, "show", f"{commit}:{name}")
        if target.read_bytes() != content:
            raise LearnDeliveryError("learning dependency input differs from the source commit")
        inputs.append((name, sha256(content).hexdigest()))
    return commit, tuple(inputs)


def _digests(root: Path, runtime: Path) -> tuple[tuple[str, str], ...]:
    """Bind artifacts and reject links outside the runtime closure."""
    values: list[tuple[str, str]] = []
    for file in sorted(root.rglob("*")):
        if file.is_symlink():
            resolved = file.resolve()
            if not resolved.is_relative_to(root) and not resolved.is_relative_to(runtime):
                raise LearnDeliveryError("learning dependency artifact escapes the runtime")
            values.append((str(file.relative_to(root)), "link:" + str(resolved)))
        elif file.is_file():
            values.append((str(file.relative_to(root)), sha256(file.read_bytes()).hexdigest()))
    return tuple(values)


@dataclass(frozen=True)
class PreparedDependencies:
    """Keep immutable local evidence for one prepared environment."""

    root: Path
    environment: Path
    cache: Path
    runtime: Path
    uv: Path
    source: tuple[str, tuple[tuple[str, str], ...]]
    artifacts: tuple[tuple[str, str], ...]
    executables: tuple[tuple[str, str], ...]

    def verify(self, delivery: Path) -> None:
        """Reject changes to bound inputs or prepared artifacts."""
        if bound_inputs(delivery) != self.source:
            raise LearnDeliveryError("learning dependency input changed during preparation")
        if any(
            sha256(Path(path).read_bytes()).hexdigest() != digest
            for path, digest in self.executables
        ):
            raise LearnDeliveryError("learning dependency runtime changed after preparation")
        if _digests(self.root, self.runtime) != self.artifacts:
            raise LearnDeliveryError("learning dependency artifact changed after preparation")


def _run(
    runner: Runner,
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    packages: tuple[tuple[str, str], ...] = (),
) -> str:
    try:
        result = runner(
            argv,
            cwd=cwd,
            env=env,
            timeout=120,
            check=False,
            log_on_error=False,
            track_process_group=True,
        )
    except subprocess.TimeoutExpired:
        raise LearnDeliveryError("learning dependency preparation timed out") from None
    except OSError:
        raise LearnDeliveryError("learning dependency preparation could not start") from None
    if result.returncode:
        detail = result.stderr or result.stdout or ""
        cause = next(
            (f"{name}=={version}" for name, version in packages if f"{name}=={version}" in detail),
            "",
        )
        suffix = f": {cause}" if cause else ""
        raise LearnDeliveryError("learning dependency preparation failed" + suffix)
    return result.stdout or ""


@contextmanager
def _prepare_dependencies(
    delivery: Path, runner: Runner = run_learning_subprocess
) -> Iterator[PreparedDependencies]:
    """Synchronize the committed lock into a private external environment."""
    source = bound_inputs(delivery)
    uv_name = shutil.which("uv")
    if uv_name is None or sys.version_info[:2] != (3, 13):
        raise LearnDeliveryError("learning dependency runtime is unavailable")
    uv = Path(uv_name).resolve()
    python = Path(sys.executable).resolve()
    runtime = Path(sys.base_prefix).resolve()
    with tempfile.TemporaryDirectory(prefix="hephaestus-learning-deps-") as temporary:
        root = Path(temporary).resolve()
        clean = root / "source"
        clean.mkdir()
        # Git writes only the committed tree, without the delivery artifact.
        with tarfile.open(
            fileobj=io.BytesIO(_git(delivery, "archive", "--format=tar", source[0]))
        ) as archive:
            archive.extractall(clean, filter="data")
        environment, cache = root / "environment", root / "cache"
        env = {
            "PATH": os.defpath,
            "HOME": str(root),
            "UV_CACHE_DIR": str(cache),
            "UV_PROJECT_ENVIRONMENT": str(environment),
            "UV_PYTHON_DOWNLOADS": "never",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        lock = tomllib.loads((clean / "uv.lock").read_text())
        packages = tuple(
            (entry["name"], entry["version"])
            for entry in lock.get("package", [])
            if isinstance(entry.get("name"), str)
            and isinstance(entry.get("version"), str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", entry["name"])
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}", entry["version"])
        )
        version = _run(runner, [str(uv), "--version"], cwd=clean, env=env).strip()
        _run(
            runner,
            [str(uv), "sync", "--frozen", "--no-editable", "--python", str(python)],
            cwd=clean,
            env=env,
            packages=packages,
        )
        _run(
            runner,
            [str(uv), "pip", "check", "--python", str(environment / "bin/python")],
            cwd=clean,
            env=env,
            packages=packages,
        )
        for name, digest in source[1]:
            if sha256((clean / name).read_bytes()).hexdigest() != digest:
                raise LearnDeliveryError("learning dependency input changed during preparation")
        executables = tuple(
            (str(file), sha256(file.read_bytes()).hexdigest()) for file in (uv, python)
        )
        manifest = {
            "source": source,
            "groups": "default",
            "python": platform.python_version(),
            "platform": platform.platform(),
            "uv": str(uv),
            "uv_version": version,
            "executables": executables,
            "artifacts": _digests(root, runtime),
        }
        write_secure(root / "manifest.json", json.dumps(manifest, sort_keys=True))
        prepared = PreparedDependencies(
            root, environment, cache, runtime, uv, source, _digests(root, runtime), executables
        )
        prepared.verify(delivery)
        yield prepared


@contextmanager
def prepare_dependencies(
    delivery: Path, runner: Runner = run_learning_subprocess
) -> Iterator[PreparedDependencies]:
    """Keep dependency preparation failures safe at the host boundary."""
    try:
        with _prepare_dependencies(delivery, runner) as prepared:
            yield prepared
    except (OSError, ValueError, tarfile.TarError):
        raise LearnDeliveryError("learning dependency preparation input failed") from None

"""Build the host-owned launcher for a candidate pre-PR runner."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from hephaestus.utils.git import _is_full_commit_sha

__all__ = ["build_verified_runner_argv"]

# The launcher reads each candidate file through one no-follow descriptor walk.
# It runs only the anonymous snapshots that it verifies. The fixed read bound
# limits candidate-controlled input before the runner starts.
_VERIFIED_RUNNER_LAUNCHER = r"""
import hashlib
import os
import stat
import subprocess
import sys
import tempfile

_MAX_SNAPSHOT_BYTES = 1024 * 1024
_INSTALL_HELPERS_PATH = "scripts/shell/lib/install_helpers.sh"
_REJECTED_RUNNER = "The candidate CI runner cannot authorize native fallback."


def _read_file(path):
    parts = path.split("/")
    if not parts or path.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise OSError("unsafe runner path")
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    if not no_follow or not directory:
        raise OSError("no-follow path walk is unavailable")
    descriptors = [os.open(".", os.O_RDONLY | directory | close_on_exec)]
    try:
        for part in parts[:-1]:
            descriptors.append(
                os.open(
                    part,
                    os.O_RDONLY | directory | no_follow | close_on_exec,
                    dir_fd=descriptors[-1],
                )
            )
        file_descriptor = os.open(
            parts[-1],
            os.O_RDONLY | os.O_NONBLOCK | no_follow | close_on_exec,
            dir_fd=descriptors[-1],
        )
        descriptors.append(file_descriptor)
        before = os.fstat(file_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("snapshot source is not a regular file")
        os.set_blocking(file_descriptor, True)
        chunks = []
        size = 0
        while True:
            chunk = os.read(
                file_descriptor,
                min(65536, _MAX_SNAPSHOT_BYTES + 1 - size),
            )
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > _MAX_SNAPSHOT_BYTES:
                raise OSError("file exceeds the snapshot size bound")
        after = os.fstat(file_descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            stat.S_IMODE(before.st_mode),
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            stat.S_IMODE(after.st_mode),
        )
        return b"".join(chunks), before_identity == after_identity, bool(after.st_mode & 0o111)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _trusted_blob(revision, path, expected_mode, git_executable):
    if not revision:
        return ""
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
    )
    result = subprocess.run(
        (git_executable, "ls-tree", "-z", revision, "--", path),
        env=environment,
        capture_output=True,
        timeout=30,
        check=False,
    )
    suffix = b"\t" + path.encode("utf-8") + b"\0"
    prefix = expected_mode.encode("ascii") + b" blob "
    if (
        result.returncode
        or not result.stdout.startswith(prefix)
        or not result.stdout.endswith(suffix)
    ):
        return ""
    blob = result.stdout[len(prefix) : -len(suffix)]
    if len(blob) not in {40, 64} or any(byte not in b"0123456789abcdef" for byte in blob):
        return ""
    return blob.decode("ascii")


def _blob_id(content, expected):
    algorithm = "sha1" if len(expected) == 40 else "sha256"
    digest = hashlib.new(algorithm)
    digest.update(f"blob {len(content)}\0".encode("ascii"))
    digest.update(content)
    return digest.hexdigest()


(
    launcher_name,
    trusted_revision,
    git_executable,
    runner_shell_executable,
    runner_shell,
    runner_path,
    *runner_args,
) = sys.argv[1:]
if (
    launcher_name != "hephaestus-required-check"
    or not os.path.isabs(git_executable)
    or not os.path.isabs(runner_shell_executable)
    or os.path.basename(runner_shell_executable) != runner_shell
):
    print(_REJECTED_RUNNER, file=sys.stderr)
    raise SystemExit(1)
try:
    runner_bytes, stable_runner, executable_runner = _read_file(runner_path)
    helper_bytes, stable_helper, _executable_helper = _read_file(_INSTALL_HELPERS_PATH)
except OSError:
    print(_REJECTED_RUNNER, file=sys.stderr)
    raise SystemExit(1)

try:
    trusted_blob = _trusted_blob(trusted_revision, runner_path, "100755", git_executable)
    trusted_helper_blob = _trusted_blob(
        trusted_revision,
        _INSTALL_HELPERS_PATH,
        "100644",
        git_executable,
    )
except (OSError, subprocess.SubprocessError):
    trusted_blob = ""
    trusted_helper_blob = ""
trusted_runner = bool(
    trusted_blob
    and trusted_helper_blob
    and stable_runner
    and stable_helper
    and executable_runner
    and _blob_id(runner_bytes, trusted_blob) == trusted_blob
    and _blob_id(helper_bytes, trusted_helper_blob) == trusted_helper_blob
)

with (
    tempfile.TemporaryFile(prefix="hephaestus-required-check-runner-") as runner_snapshot,
    tempfile.TemporaryFile(prefix="hephaestus-required-check-helper-") as helper_snapshot,
):
    runner_snapshot.write(runner_bytes)
    runner_snapshot.flush()
    os.fchmod(runner_snapshot.fileno(), 0o500)
    runner_snapshot.seek(0)
    helper_snapshot.write(helper_bytes)
    helper_snapshot.flush()
    os.fchmod(helper_snapshot.fileno(), 0o400)
    helper_snapshot.seek(0)
    environment = dict(os.environ)
    environment["HEPHAESTUS_VERIFIED_RUNNER_ROOT"] = os.getcwd()
    environment["HEPHAESTUS_VERIFIED_RUNNER_FD"] = str(runner_snapshot.fileno())
    environment["HEPHAESTUS_VERIFIED_INSTALL_HELPERS_FD"] = str(helper_snapshot.fileno())
    result = subprocess.run(
        (
            runner_shell_executable,
            f"/dev/fd/{runner_snapshot.fileno()}",
            *runner_args,
        ),
        env=environment,
        pass_fds=(runner_snapshot.fileno(), helper_snapshot.fileno()),
        check=False,
    )

if result.returncode == 75 and not trusted_runner:
    print(_REJECTED_RUNNER, file=sys.stderr)
    raise SystemExit(1)
raise SystemExit(result.returncode)
""".strip()


def _trusted_host_executable(name: str) -> str:
    """Return a system executable path or an empty fail-closed value."""
    if Path(name).name != name:
        return ""
    return shutil.which(name, path=os.defpath) or ""


def build_verified_runner_argv(
    candidate_argv: tuple[str, ...], source_revision: str
) -> tuple[str, ...]:
    """Return a launcher that executes a verified candidate snapshot."""
    if not candidate_argv:
        raise ValueError("the verified runner command is empty")
    trusted_revision = source_revision if _is_full_commit_sha(source_revision) else ""
    launcher_python = str(Path(sys.executable).resolve(strict=True))
    git_executable = _trusted_host_executable("git")
    runner_shell = candidate_argv[0]
    runner_shell_executable = _trusted_host_executable(runner_shell)
    return (
        launcher_python,
        "-I",
        "-c",
        _VERIFIED_RUNNER_LAUNCHER,
        "hephaestus-required-check",
        trusted_revision,
        git_executable,
        runner_shell_executable,
        *candidate_argv,
    )

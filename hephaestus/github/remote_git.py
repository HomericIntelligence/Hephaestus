"""Trusted, isolated remote Git and GitHub CLI helpers.

The library layer owns this module so both the library and the automation
product layer share one trusted transport. It provides:

- trusted ``gh`` executable discovery with the explicit ``--gh-extra-path-root``
  override rule (only ``ROOT/bin/gh``);
- isolated GitHub HTTPS and SSH transport configuration;
- the trusted-authentication probe; and
- the shared ``--gh-extra-path-root`` argument parser helper.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from hephaestus.cli.localization import text


def trusted_gh_executable(
    extra_path_root: Path | None = None,
    *,
    path: str | None = None,
) -> str | None:
    """Return the ``gh`` executable to run, or None when it is not found.

    The operator override is tested first. Otherwise the lookup uses ``PATH``,
    the same way every other command lookup on the host works. The earlier
    version accepted only three hard-coded absolute paths and refused ``PATH``,
    so a correct ``gh`` on a normal system was reported as missing.

    Args:
        extra_path_root: Operator root from ``--gh-extra-path-root``. When it
            holds ``ROOT/bin/gh``, that file wins.
        path: Search path for the fallback lookup. Defaults to the process
            ``PATH``.

    Returns:
        An absolute path to ``gh``, or None when no candidate exists.

    """
    if extra_path_root is not None:
        override = extra_path_root / "bin" / "gh"
        if override.is_file() and os.access(override, os.X_OK):
            return str(override.resolve())
    return shutil.which("gh", path=path)


def trusted_remote_git_config(gh_command: str) -> tuple[str, ...] | None:
    """Return isolated GitHub HTTPS and SSH transport configuration."""
    executable = shutil.which("ssh", path=os.defpath)
    if executable is None:
        return None
    ssh_command = str(Path(executable).resolve())
    ssh_config = " ".join(
        (
            shlex.quote(ssh_command),
            "-F",
            shlex.quote(os.devnull),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
        )
    )
    return (
        "-c",
        f"core.sshCommand={ssh_config}",
        "-c",
        "credential.helper=",
        "-c",
        f"credential.helper=!{shlex.quote(gh_command)} auth git-credential",
        "-c",
        "core.askPass=",
        "-c",
        "http.sslVerify=true",
    )


def trusted_gh_authenticated(command: str, timeout_s: int) -> bool:
    """Check the trusted executable with the approved credential bridges."""
    from hephaestus.config.child_environments import build_gh_child_env
    from hephaestus.utils.helpers import run_subprocess

    try:
        result = run_subprocess(
            [command, "auth", "status", "--hostname", "github.com"],
            env=build_gh_child_env(),
            check=False,
            timeout=timeout_s,
            track_process_group=True,
            log_on_error=False,
        )
    except (OSError, RuntimeError, UnicodeError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


class TrustedRemoteGit:
    """Resolve authentication for each bounded remote Git operation."""

    def __init__(self, extra_path_root: Path | None = None) -> None:
        """Keep only the host-selected executable root."""
        self.extra_path_root = extra_path_root

    def __call__(
        self, cwd: Path, argv: tuple[str, ...], timeout_s: int
    ) -> subprocess.CompletedProcess[str]:
        """Run one remote command and discard failure diagnostics."""
        from hephaestus.config.child_environments import build_remote_git_env
        from hephaestus.utils.helpers import run_subprocess

        command = trusted_gh_executable(self.extra_path_root)
        if command is None or not trusted_gh_authenticated(command, timeout_s):
            raise RuntimeError("remote Git authentication unavailable")
        config = trusted_remote_git_config(command)
        if config is None:
            raise RuntimeError("remote Git authentication unavailable")
        try:
            result = run_subprocess(
                ["git", *config, *argv],
                env=build_remote_git_env(),
                cwd=cwd,
                check=False,
                timeout=timeout_s,
                track_process_group=True,
                log_on_error=False,
            )
        except (OSError, RuntimeError, UnicodeError, subprocess.SubprocessError):
            raise RuntimeError("remote Git transport failed") from None
        if result.returncode != 0:
            return subprocess.CompletedProcess(
                [], result.returncode, "", "remote Git transport failed"
            )
        return result


def parse_gh_extra_path_root(value: str) -> Path:
    """Validate an explicit root whose only admitted executable is ``bin/gh``."""
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise argparse.ArgumentTypeError(
            text("--gh-extra-path-root must be an absolute path")
        )
    try:
        resolved_root = root.resolve(strict=True)
        executable = (resolved_root / "bin" / "gh").resolve(strict=True)
    except OSError as exc:
        raise argparse.ArgumentTypeError(
            text("--gh-extra-path-root must contain an executable bin/gh")
        ) from exc
    if (
        not resolved_root.is_dir()
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
        or not executable.is_relative_to(resolved_root)
    ):
        raise argparse.ArgumentTypeError(
            text(
                "--gh-extra-path-root must contain an executable bin/gh "
                "without symlink escapes"
            )
        )
    return resolved_root


def add_gh_extra_path_root_arg(parser: argparse.ArgumentParser) -> None:
    """Add the explicit, narrowly validated ``gh`` root argument to ``parser``."""
    parser.add_argument(
        "--gh-extra-path-root",
        type=parse_gh_extra_path_root,
        default=None,
        metavar="ROOT",
        help=text(
            "Explicitly allow only ROOT/bin/gh in addition to system gh locations. "
            "ROOT must be absolute and contain an executable bin/gh that does not "
            "escape ROOT."
        ),
    )


__all__ = [
    "TrustedRemoteGit",
    "add_gh_extra_path_root_arg",
    "parse_gh_extra_path_root",
    "trusted_gh_authenticated",
    "trusted_gh_executable",
    "trusted_remote_git_config",
]


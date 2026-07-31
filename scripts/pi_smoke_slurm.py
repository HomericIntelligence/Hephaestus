#!/usr/bin/env python3
"""Submit the sanitized Pi smoke Slurm template without exposing alias values."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from hephaestus.agents.runtime import (
    REQUIRED_ALIAS_ENVS,
    missing_pi_alias_env,
    pi_private_redaction_tokens,
    prepare_pi_private_log_dir,
    redact_pi_private_values,
)

DEFAULT_LOG_DIR = Path("pi-smoke-logs")
DEFAULT_TEMPLATE = Path("scripts/slurm/pi_smoke.sbatch")
LOG_DIR_ENV = "HEPH_PI_SMOKE_LOG_DIR"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXPORT_NAMES = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TMP",
    "TEMP",
    *REQUIRED_ALIAS_ENVS,
    LOG_DIR_ENV,
)


def _private_smoke_log_permissions_supported() -> bool:
    """Return whether this platform can establish the required private log mode."""
    return os.name != "nt"


def _prepare_private_log_dir(log_dir: Path) -> Path:
    """Create or tighten the owner-only directory used for Slurm artifacts."""
    if not _private_smoke_log_permissions_supported():
        raise OSError("Pi smoke requires user-only log permissions on this platform")
    return prepare_pi_private_log_dir(log_dir)


def _submission_env(log_dir: Path) -> dict[str, str]:
    """Return the minimal environment needed by the local ``sbatch`` process."""
    env = {name: value for name in EXPORT_NAMES if (value := os.environ.get(name))}
    env.setdefault("PATH", os.defpath)
    env[LOG_DIR_ENV] = str(log_dir)
    return env


def build_parser() -> argparse.ArgumentParser:
    """Build the Pi Slurm smoke submission parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--sbatch", default="sbatch")
    return parser


def build_sbatch_cmd(args: argparse.Namespace) -> list[str]:
    """Build an sbatch command that exports alias env var names only."""
    log_dir: Path = args.log_dir
    return [
        args.sbatch,
        f"--export={','.join(EXPORT_NAMES)}",
        f"--output={log_dir / 'pi-smoke-%j.out'}",
        f"--error={log_dir / 'pi-smoke-%j.err'}",
        str(args.template),
    ]


def main(argv: list[str] | None = None) -> int:
    """Submit the Pi smoke Slurm template after validating alias env vars."""
    args = build_parser().parse_args(argv)
    missing = missing_pi_alias_env()
    if missing:
        print(f"ERROR: missing required env vars: {', '.join(missing)}", file=sys.stderr)
        return 2

    try:
        redaction_tokens = pi_private_redaction_tokens(
            Path.cwd(),
            additional_roots=(REPOSITORY_ROOT,),
            require_readable=True,
        )
    except OSError:
        print("ERROR: unable to load Pi private denylist safely", file=sys.stderr)
        return 1
    try:
        private_log_dir = _prepare_private_log_dir(args.log_dir)
    except OSError as exc:
        detail = redact_pi_private_values(str(exc), redaction_tokens)
        print(f"ERROR: {detail}", file=sys.stderr)
        return 1
    args.log_dir = private_log_dir
    cmd = build_sbatch_cmd(args)
    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            env=_submission_env(private_log_dir),
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr or exc.stdout or str(exc)
        print(redact_pi_private_values(detail, redaction_tokens), file=sys.stderr)
        return exc.returncode
    except OSError as exc:
        detail = redact_pi_private_values(str(exc), redaction_tokens)
        print(f"ERROR: Pi smoke Slurm submission could not start: {detail}", file=sys.stderr)
        return 1

    if result.stdout:
        print(redact_pi_private_values(result.stdout, redaction_tokens), end="")
    if result.stderr:
        print(redact_pi_private_values(result.stderr, redaction_tokens), end="", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

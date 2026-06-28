#!/usr/bin/env python3
"""Submit the sanitized Pi smoke Slurm template without exposing alias values."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from hephaestus.agents.runtime import REQUIRED_ALIAS_ENVS, missing_pi_alias_env

DEFAULT_LOG_DIR = Path("pi-smoke-logs")
DEFAULT_TEMPLATE = Path("scripts/slurm/pi_smoke.sbatch")
LOG_DIR_ENV = "HEPH_PI_SMOKE_LOG_DIR"
EXPORT_NAMES = ("ALL", *REQUIRED_ALIAS_ENVS, LOG_DIR_ENV)


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

    args.log_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env[LOG_DIR_ENV] = str(args.log_dir)
    cmd = build_sbatch_cmd(args)
    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        print(exc.stderr or exc.stdout or str(exc), file=sys.stderr)
        return exc.returncode

    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

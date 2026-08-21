#!/usr/bin/env python3
"""Run a sanitized Pi smoke prompt against an operator-owned alias file."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path

from hephaestus.agents.runtime import (
    AgentRunResult,
    load_pi_alias_config,
    pi_private_redaction_tokens,
    prepare_pi_private_log_dir,
    redact_pi_private_values,
    run_pi_smoke_session,
)

DEFAULT_PROMPT = "Reply with exactly: OK"
DEFAULT_LOG_DIR = Path("pi-smoke-logs")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _private_smoke_log_permissions_supported() -> bool:
    """Return whether this platform can establish the required private log mode."""
    return os.name != "nt"


def _require_private_smoke_log_permissions() -> None:
    """Fail closed where this script cannot establish a user-only log ACL."""
    if not _private_smoke_log_permissions_supported():
        raise OSError("Pi smoke requires user-only log permissions on this platform")


def _prepare_private_log_dir(log_dir: Path) -> Path:
    """Create a unique private directory for this smoke run's artifact."""
    _require_private_smoke_log_permissions()
    return prepare_pi_private_log_dir(log_dir)


def build_parser() -> argparse.ArgumentParser:
    """Build the Pi smoke parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Tool-free smoke prompt")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="Working directory for Pi")
    parser.add_argument("--timeout", type=int, default=300, help="Pi subprocess timeout")
    parser.add_argument(
        "--pi-alias-config",
        type=Path,
        help="Owner-only mode-0600 TOML file containing provider and model aliases",
    )
    parser.add_argument(
        "--pi-dir",
        type=Path,
        default=None,
        help="Explicit Pi coding-agent configuration directory",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help="Local directory for untracked smoke validation logs",
    )
    return parser


def _write_smoke_log(
    log_dir: Path,
    result: AgentRunResult,
    redaction_tokens: Iterable[str],
) -> Path:
    """Write the local Pi smoke result log and return its path.

    Each run produces a distinct artifact named with a nanosecond epoch suffix
    so consecutive smoke runs never silently overwrite one another.
    """
    epoch_ns = time.time_ns()
    log_path = log_dir / f"pi-smoke-local-{epoch_ns}.log"
    display_path = redact_pi_private_values(str(log_path), redaction_tokens)
    print(f"NOTE: writing smoke log to {display_path}", file=sys.stderr)
    lines = [
        f"stdout: {redact_pi_private_values(result.stdout, redaction_tokens)}",
        f"stderr: {redact_pi_private_values(result.stderr, redaction_tokens)}",
        "",
    ]
    file_descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(file_descriptor, "w", encoding="utf-8") as log_file:
        log_file.write("\n".join(lines))
    return log_path


def main(argv: list[str] | None = None) -> int:
    """Run the smoke prompt against aliases in an operator-owned TOML file."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.pi_alias_config is None:
        print("ERROR: --pi-alias-config is required", file=sys.stderr)
        return 2
    try:
        aliases = load_pi_alias_config(args.pi_alias_config)
    except (OSError, ValueError):
        print("ERROR: unable to load Pi alias config safely", file=sys.stderr)
        return 2
    try:
        redaction_tokens = pi_private_redaction_tokens(
            args.cwd,
            aliases.model,
            provider=aliases.provider,
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
    try:
        if args.pi_dir is None:
            result = run_pi_smoke_session(
                args.prompt,
                cwd=args.cwd,
                timeout=args.timeout,
                model=aliases.model,
                provider=aliases.provider,
            )
        else:
            result = run_pi_smoke_session(
                args.prompt,
                cwd=args.cwd,
                timeout=args.timeout,
                model=aliases.model,
                provider=aliases.provider,
                pi_dir=args.pi_dir,
            )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr or exc.stdout or f"Pi smoke failed with exit {exc.returncode}"
        print(redact_pi_private_values(detail, redaction_tokens), file=sys.stderr)
        return exc.returncode
    except subprocess.TimeoutExpired as exc:
        print(f"ERROR: Pi smoke timed out after {exc.timeout}s", file=sys.stderr)
        return 124
    except RuntimeError as exc:
        print(f"ERROR: {redact_pi_private_values(str(exc), redaction_tokens)}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError) as exc:
        detail = redact_pi_private_values(str(exc), redaction_tokens)
        print(f"ERROR: Pi smoke could not start: {detail}", file=sys.stderr)
        return 1
    try:
        log_path = _write_smoke_log(private_log_dir, result, redaction_tokens)
    except OSError as exc:
        detail = redact_pi_private_values(str(exc), redaction_tokens)
        print(f"ERROR: could not write Pi smoke log: {detail}", file=sys.stderr)
        return 1
    print(redact_pi_private_values(result.stdout, redaction_tokens))
    display_log_path = redact_pi_private_values(str(log_path), redaction_tokens)
    print(f"LOG_FILE={display_log_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

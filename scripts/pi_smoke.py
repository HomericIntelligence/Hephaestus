#!/usr/bin/env python3
"""Run a sanitized Pi smoke prompt against operator-local Pi aliases.

The model/provider configuration must live outside the repository in Pi's local
configuration. This script accepts provider and model aliases only from
``HEPH_PI_PROVIDER`` and ``HEPH_PI_MODEL`` and never stores private endpoints,
hostnames, or model identifiers in source.

Usage:
    HEPH_PI_PROVIDER=<provider-alias> HEPH_PI_MODEL=<model-alias> python scripts/pi_smoke.py
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from hephaestus.agents.runtime import (
    PI_MODEL_ENV,
    REQUIRED_ALIAS_ENVS,
    AgentRunResult,
    missing_pi_alias_env,
    pi_private_redaction_tokens,
    redact_pi_private_values,
    run_pi_session,
)

DEFAULT_PROMPT = "Reply with exactly: OK"
DEFAULT_LOG_DIR = Path("pi-smoke-logs")


def build_parser() -> argparse.ArgumentParser:
    """Build the Pi smoke parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Read-only smoke prompt")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="Working directory for Pi")
    parser.add_argument("--timeout", type=int, default=300, help="Pi subprocess timeout")
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help="Local directory for untracked smoke validation logs",
    )
    return parser


def _redact_alias_values(text: str) -> str:
    """Replace operator-local alias values before writing smoke logs."""
    redacted = text
    for name in REQUIRED_ALIAS_ENVS:
        value = os.environ.get(name, "").strip()
        if value:
            redacted = redacted.replace(value, f"<redacted:{name}>")
    return redacted


def _write_smoke_log(log_dir: Path, result: AgentRunResult) -> Path:
    """Write the local Pi smoke result log and return its path.

    Each run produces a distinct artifact named with a nanosecond epoch suffix
    so consecutive smoke runs never silently overwrite one another.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    epoch_ns = time.time_ns()
    log_path = log_dir / f"pi-smoke-local-{epoch_ns}.log"
    print(f"NOTE: writing smoke log to {log_path}", file=sys.stderr)
    lines = [
        f"session_id: {result.session_id or ''}",
        f"stdout: {_redact_alias_values(result.stdout)}",
        f"stderr: {_redact_alias_values(result.stderr)}",
        "",
    ]
    log_path.write_text("\n".join(lines), encoding="utf-8")
    return log_path


def main(argv: list[str] | None = None) -> int:
    """Run the smoke prompt against aliases in operator-local env vars."""
    parser = build_parser()
    args = parser.parse_args(argv)
    missing = missing_pi_alias_env()
    if missing:
        print(f"ERROR: missing required env vars: {', '.join(missing)}", file=sys.stderr)
        return 2
    model = os.environ.get(PI_MODEL_ENV, "").strip()
    redaction_tokens = pi_private_redaction_tokens(args.cwd, model)
    try:
        result = run_pi_session(
            args.prompt,
            cwd=args.cwd,
            timeout=args.timeout,
            model=model,
            sandbox="read-only",
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr or exc.stdout or f"Pi smoke failed with exit {exc.returncode}"
        print(redact_pi_private_values(detail, redaction_tokens), file=sys.stderr)
        return exc.returncode
    except subprocess.TimeoutExpired as exc:
        print(f"ERROR: Pi smoke timed out after {exc.timeout}s", file=sys.stderr)
        return 124
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    log_path = _write_smoke_log(args.log_dir, result)
    print(redact_pi_private_values(result.stdout, redaction_tokens))
    if result.session_id:
        print(f"SESSION_ID={result.session_id}", file=sys.stderr)
    print(f"LOG_FILE={log_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

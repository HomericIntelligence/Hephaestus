#!/usr/bin/env python3
"""Run a sanitized Pi smoke prompt against an operator-local model alias.

The model/provider configuration must live outside the repository in Pi's local
configuration. This script accepts only a model alias from ``HEPH_PI_MODEL`` and
never stores private endpoints, hostnames, or model identifiers in source.

Usage:
    HEPH_PI_MODEL=<operator-local-alias> python scripts/pi_smoke.py
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from hephaestus.agents.runtime import (
    pi_private_redaction_tokens,
    redact_pi_private_values,
    run_pi_session,
)

DEFAULT_PROMPT = "Reply with exactly: OK"


def build_parser() -> argparse.ArgumentParser:
    """Build the Pi smoke parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Read-only smoke prompt")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="Working directory for Pi")
    parser.add_argument("--timeout", type=int, default=300, help="Pi subprocess timeout")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the smoke prompt against the model alias in ``HEPH_PI_MODEL``."""
    parser = build_parser()
    args = parser.parse_args(argv)
    model = os.environ.get("HEPH_PI_MODEL", "").strip()
    if not model:
        print("ERROR: HEPH_PI_MODEL must name an operator-local Pi model alias.", file=sys.stderr)
        return 2
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
    print(redact_pi_private_values(result.stdout, redaction_tokens))
    if result.session_id:
        print(f"SESSION_ID={result.session_id}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

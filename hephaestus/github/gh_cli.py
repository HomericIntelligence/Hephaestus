"""Shell-facing wrapper around the shared ``gh_call`` adapter."""

from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Any

from hephaestus.cli.utils import (
    add_github_throttle_args,
    add_json_arg,
    add_version_arg,
    configure_github_throttle_from_args,
    emit_json_status,
)
from hephaestus.github.client import (
    ClaudeUsageCapError,
    GitHubRateLimitError,
    GitHubUnavailableError,
    gh_call,
)


def _as_text(value: Any) -> str:
    """Return subprocess output as text for pass-through and JSON envelopes."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def build_parser() -> argparse.ArgumentParser:
    """Build the parser for ``hephaestus-gh``."""
    parser = argparse.ArgumentParser(
        prog="hephaestus-gh",
        description="Run gh through Hephaestus's retry, circuit-breaker, and throttle adapter.",
        allow_abbrev=False,
    )
    add_github_throttle_args(parser)
    add_json_arg(parser)
    add_version_arg(parser)
    parser.add_argument(
        "gh_args",
        nargs=argparse.REMAINDER,
        metavar="GH_ARG",
        help="Arguments passed through to gh. Prefix with -- if they start with a dash.",
    )
    return parser


def _write_streams(stdout: str, stderr: str) -> None:
    """Forward subprocess streams to this process."""
    if stdout:
        sys.stdout.write(stdout)
    if stderr:
        sys.stderr.write(stderr)


def _handle_called_process_error(exc: subprocess.CalledProcessError, *, json_out: bool) -> int:
    """Render a nonzero gh exit without adding a Python traceback."""
    stdout = _as_text(exc.stdout)
    stderr = _as_text(exc.stderr)
    if json_out:
        emit_json_status(exc.returncode, stdout=stdout, stderr=stderr)
    else:
        _write_streams(stdout, stderr)
    return exc.returncode


def _handle_timeout(
    exc: subprocess.TimeoutExpired,
    *,
    gh_args: list[str],
    json_out: bool,
) -> int:
    """Render a timed-out gh invocation."""
    stdout = _as_text(getattr(exc, "stdout", None) or getattr(exc, "output", None))
    stderr = _as_text(exc.stderr)
    message = f"gh {' '.join(gh_args)} timed out after {exc.timeout}s"
    if json_out:
        emit_json_status(124, message, stdout=stdout, stderr=stderr)
    else:
        _write_streams(stdout, stderr)
        print(message, file=sys.stderr)
    return 124


def _handle_adapter_error(exc: Exception, *, json_out: bool) -> int:
    """Render adapter-level failures such as breaker or rate-limit errors."""
    message = str(exc)
    extra: dict[str, Any] = {"stdout": "", "stderr": message}
    reset_epoch = getattr(exc, "reset_epoch", None)
    if reset_epoch is not None:
        extra["reset_epoch"] = reset_epoch
    if json_out:
        emit_json_status(1, message, **extra)
    else:
        print(message, file=sys.stderr)
    return 1


def _handle_success(result: subprocess.CompletedProcess[str], *, json_out: bool) -> int:
    """Render a successful gh invocation."""
    if json_out:
        emit_json_status(
            result.returncode,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )
    else:
        _write_streams(result.stdout or "", result.stderr or "")
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    """Run ``gh`` via :func:`hephaestus.github.client.gh_call`."""
    parser = build_parser()
    args = parser.parse_args(argv)
    gh_args = list(args.gh_args)
    if gh_args and gh_args[0] == "--":
        gh_args = gh_args[1:]
    if not gh_args:
        parser.error("missing gh arguments")

    configure_github_throttle_from_args(args)
    try:
        result = gh_call(gh_args)
    except subprocess.CalledProcessError as exc:
        return _handle_called_process_error(exc, json_out=args.json)
    except subprocess.TimeoutExpired as exc:
        return _handle_timeout(exc, gh_args=gh_args, json_out=args.json)
    except (
        GitHubRateLimitError,
        GitHubUnavailableError,
        ClaudeUsageCapError,
        RuntimeError,
        OSError,
    ) as exc:
        return _handle_adapter_error(exc, json_out=args.json)

    return _handle_success(result, json_out=args.json)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

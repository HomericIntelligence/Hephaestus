"""Provider-selectable CLI stage runner for Hephaestus automation workflows."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from hephaestus.agents.runtime import (
    add_agent_argument,
    resolve_agent,
    run_claude_text,
    run_codex_session,
)
from hephaestus.cli.utils import add_json_arg, emit_json_status


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the agent stage runner."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-file", required=True, help="Prompt file to send to the agent")
    parser.add_argument("--repo-root", required=True, help="Repository root for the agent")
    parser.add_argument("--stage", required=True, help="Human-readable automation stage name")
    parser.add_argument("--output", required=True, help="Where to write the agent's final response")
    parser.add_argument("--log-file", help="Where to write combined agent stdout/stderr")
    parser.add_argument("--skill-file", help="Optional skill instructions to prepend to the prompt")
    add_agent_argument(parser)
    parser.add_argument("--model", default="", help="Optional agent model override")
    parser.add_argument(
        "--sandbox",
        choices=["read-only", "workspace-write", "danger-full-access"],
        default="workspace-write",
        help="Sandbox mode for agents that support it",
    )
    parser.add_argument(
        "--approval",
        choices=["untrusted", "on-request", "never"],
        default="never",
        help="Approval policy for agents that support it",
    )
    parser.add_argument("--timeout", type=int, default=1800, help="Subprocess timeout in seconds")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print the agent command before running",
    )
    add_json_arg(parser)
    return parser


def read_prompt(prompt_file: Path, skill_file: Path | None, stage: str) -> str:
    """Read the stage prompt and optionally prepend skill instructions."""
    prompt = prompt_file.read_text(encoding="utf-8")
    if skill_file is None:
        return prompt
    skill_text = skill_file.read_text(encoding="utf-8")
    return (
        f"You are running ProjectHephaestus agent stage `{stage}`.\n\n"
        "Use these skill instructions as authoritative context for this stage:\n\n"
        f"{skill_text}\n\n"
        "---\n\n"
        f"{prompt}"
    )


def write_log(log_file: Path | None, text: str) -> None:
    """Write a subprocess log when a log path was provided."""
    if log_file is not None:
        log_file.write_text(text, encoding="utf-8")


def run_claude(
    args: argparse.Namespace,
    prompt: str,
    repo_root: Path,
    output_file: Path,
    log_file: Path | None,
) -> int:
    """Run one stage with Claude Code, the default Hephaestus agent."""
    if args.debug:
        print("Running: claude --print", file=sys.stderr)

    try:
        result = run_claude_text(
            prompt,
            cwd=repo_root,
            timeout=args.timeout,
            model=args.model,
            sandbox=args.sandbox,
        )
    except subprocess.TimeoutExpired as exc:
        write_log(log_file, str(exc))
        return 124

    output_file.write_text(result.stdout or "", encoding="utf-8")
    write_log(log_file, result.stdout or "")
    return result.returncode


def run_codex(
    args: argparse.Namespace,
    prompt: str,
    repo_root: Path,
    output_file: Path,
    log_file: Path | None,
) -> int:
    """Run one stage with Codex."""
    if args.debug:
        print("Running: codex exec", file=sys.stderr)

    try:
        result = run_codex_session(
            prompt,
            cwd=repo_root,
            timeout=args.timeout,
            model=args.model,
            sandbox=args.sandbox,
            approval=args.approval,
        )
    except subprocess.TimeoutExpired as exc:
        write_log(log_file, str(exc))
        return 124
    except subprocess.CalledProcessError as exc:
        log_text = (
            f"EXIT CODE: {exc.returncode}\n\n"
            f"STDOUT:\n{exc.stdout or ''}\n\n"
            f"STDERR:\n{exc.stderr or ''}"
        )
        write_log(log_file, log_text)
        return exc.returncode

    output_file.write_text(result.stdout, encoding="utf-8")
    log = result.stdout
    if result.session_id:
        log = f"SESSION_ID: {result.session_id}\n\n{log}"
    write_log(log_file, log)
    return 0


# Flag values that silently no-op when --agent=claude is selected.
# - `approval` is not a parameter of run_claude_text at all, so any
#   non-default value (i.e. != "never") is a no-op.
# - `sandbox="read-only"` IS honored (run_claude_text:125 gates
#   --permission-mode on it), so only `danger-full-access` is a no-op.
# See issue #773.
_CLAUDE_NOOP_VALUES: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("approval", "--approval", frozenset({"untrusted", "on-request"})),
    ("sandbox", "--sandbox", frozenset({"danger-full-access"})),
)


def validate_agent_flags(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject flag values that the selected agent does not honor.

    Only fires when the operator EXPLICITLY passed ``--agent=claude``. When
    ``--agent`` is omitted, ``resolve_agent`` will auto-detect at run time and
    that path keeps its existing semantics. See issue #773.
    """
    if args.agent != "claude":
        return
    offending: list[str] = []
    for attr, flag, noop_values in _CLAUDE_NOOP_VALUES:
        value = getattr(args, attr)
        if value in noop_values:
            offending.append(f"{flag}={value}")
    if offending:
        parser.error(
            "--agent=claude does not honor "
            + ", ".join(offending)
            + " (these flag values are not supported by the claude agent)"
        )


def run_agent(args: argparse.Namespace) -> int:
    """Run one provider-selected automation stage and persist its output/log files."""
    repo_root = Path(args.repo_root).expanduser().resolve()
    prompt_file = Path(args.prompt_file).expanduser().resolve()
    output_file = Path(args.output).expanduser().resolve()
    log_file = Path(args.log_file).expanduser().resolve() if args.log_file else None
    skill_file = Path(args.skill_file).expanduser().resolve() if args.skill_file else None

    prompt = read_prompt(prompt_file, skill_file, args.stage)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)

    agent = resolve_agent(args.agent)
    args.agent = agent

    if agent == "claude":
        return run_claude(args, prompt, repo_root, output_file, log_file)
    if agent == "codex":
        return run_codex(args, prompt, repo_root, output_file, log_file)
    raise ValueError(f"Unsupported agent: {agent}")


def main(argv: list[str] | None = None) -> int:
    """Run the agent stage command-line interface."""
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_agent_flags(parser, args)
    exit_code = run_agent(args)
    if args.json:
        emit_json_status(exit_code, stage=args.stage, agent=args.agent)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

"""Provider-selectable CLI stage runner for Hephaestus automation workflows."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from hephaestus.agents.runtime import (
    add_agent_argument,
    resolve_agent,
    run_agent_session,
    run_claude_text,
    uses_direct_agent_runner,
)
from hephaestus.automation.agent_config import normalize_claude_model
from hephaestus.cli.utils import add_json_arg, add_version_arg, emit_json_status
from hephaestus.io.utils import write_secure
from hephaestus.prompts import PromptCatalog, add_prompt_dir_argument
from hephaestus.utils.terminal import install_sigtstp_only, terminal_guard


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
    add_prompt_dir_argument(parser)
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
    add_version_arg(parser)
    return parser


def read_prompt(prompt_file: Path, skill_file: Path | None, stage: str) -> str:
    """Read the stage prompt and optionally prepend skill instructions."""
    prompt = prompt_file.read_text(encoding="utf-8")
    if skill_file is None:
        return prompt
    skill_text = skill_file.read_text(encoding="utf-8")
    return PromptCatalog.current().render(
        "agent_stage/skill_prefix.j2", stage=stage, skill_text=skill_text, prompt=prompt
    )


def write_log(log_file: Path | None, text: str) -> None:
    """Write a subprocess log when a log path was provided."""
    if log_file is not None:
        write_secure(log_file, text)


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
            model=normalize_claude_model(args.model),
            sandbox=args.sandbox,
        )
    except subprocess.TimeoutExpired as exc:
        write_log(log_file, str(exc))
        return 124

    write_secure(output_file, result.stdout or "")
    write_log(log_file, result.stdout or "")
    return result.returncode


def run_direct_agent(
    args: argparse.Namespace,
    prompt: str,
    repo_root: Path,
    output_file: Path,
    log_file: Path | None,
) -> int:
    """Run one stage with a provider-neutral direct agent."""
    if args.debug:
        print(f"Running: {args.agent} direct session", file=sys.stderr)

    try:
        result = run_agent_session(
            agent=args.agent,
            prompt=prompt,
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

    write_secure(output_file, result.stdout)
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
_PI_NOOP_VALUES: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("approval", "--approval", frozenset({"untrusted", "on-request"})),
    ("sandbox", "--sandbox", frozenset({"danger-full-access"})),
)
_NOOP_VALUES_BY_AGENT: dict[str, tuple[tuple[str, str, frozenset[str]], ...]] = {
    "claude": _CLAUDE_NOOP_VALUES,
    "pi": _PI_NOOP_VALUES,
}


def validate_agent_flags(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject flag values that the selected agent does not honor.

    Only fires when the operator EXPLICITLY passed ``--agent=claude``. When
    ``--agent`` is omitted, ``resolve_agent`` will auto-detect at run time and
    that path keeps its existing semantics. See issue #773.
    """
    noop_values = _NOOP_VALUES_BY_AGENT.get(args.agent)
    if not noop_values:
        return
    offending: list[str] = []
    for attr, flag, noops in noop_values:
        value = getattr(args, attr)
        if value in noops:
            offending.append(f"{flag}={value}")
    if offending:
        parser.error(
            f"--agent={args.agent} does not honor "
            + ", ".join(offending)
            + f" (these flag values are not supported by the {args.agent} agent)"
        )


def validate_input_files(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject missing prompt/skill files with a CLI diagnostic instead of a traceback.

    ``run_agent`` reads these paths with a bare ``read_text`` (:func:`read_prompt`);
    without this gate a nonexistent ``--prompt-file`` (or ``--skill-file``) surfaces
    as an uncaught ``FileNotFoundError``, and a directory passed as a prompt raises
    ``IsADirectoryError``. ``.is_file()`` converts both into a controlled
    ``parser.error`` diagnostic (usage line + ``error: ...`` on stderr, exit code 2).
    See issue #2171.
    """
    prompt_file = Path(args.prompt_file).expanduser().resolve()
    if not prompt_file.is_file():
        parser.error(f"--prompt-file does not exist or is not a file: {prompt_file}")
    if args.skill_file:
        skill_file = Path(args.skill_file).expanduser().resolve()
        if not skill_file.is_file():
            parser.error(f"--skill-file does not exist or is not a file: {skill_file}")


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
    if uses_direct_agent_runner(agent):
        return run_direct_agent(args, prompt, repo_root, output_file, log_file)
    raise ValueError(f"Unsupported agent: {agent}")


def main(argv: list[str] | None = None) -> int:
    """Run the agent stage command-line interface.

    This entrypoint makes a single blocking agent call with no internal
    polling loop, so it deliberately does NOT install the cooperative
    double-Ctrl+C handler (:func:`install_signal_handlers`) — that would
    replace Python's default SIGINT disposition (instant ``KeyboardInterrupt``)
    with a handler whose first press only sets a flag, silently absorbing a
    single Ctrl+C instead of killing the blocking ``claude``/``gh`` subprocess.
    Only SIGTSTP (Ctrl+Z) is wired here, plus bare terminal restoration.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_agent_flags(parser, args)
    validate_input_files(parser, args)

    install_sigtstp_only()
    with terminal_guard():
        exit_code = run_agent(args)
    if args.json:
        emit_json_status(exit_code, stage=args.stage, agent=args.agent)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

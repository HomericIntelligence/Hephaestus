"""Codex CLI stage runner for Hephaestus automation workflows."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the Codex stage runner."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-file", required=True, help="Prompt file to send to Codex")
    parser.add_argument("--repo-root", required=True, help="Repository root for Codex --cd")
    parser.add_argument("--stage", required=True, help="Human-readable automation stage name")
    parser.add_argument("--output", required=True, help="Where to write Codex's final response")
    parser.add_argument("--log-file", help="Where to write combined Codex stdout/stderr")
    parser.add_argument("--skill-file", help="Optional skill instructions to prepend to the prompt")
    parser.add_argument("--model", default="", help="Optional Codex model override")
    parser.add_argument(
        "--sandbox",
        choices=["read-only", "workspace-write", "danger-full-access"],
        default="workspace-write",
        help="Codex sandbox mode",
    )
    parser.add_argument(
        "--approval",
        choices=["untrusted", "on-request", "never"],
        default="never",
        help="Codex approval policy",
    )
    parser.add_argument("--timeout", type=int, default=1800, help="Subprocess timeout in seconds")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print the Codex command before running",
    )
    return parser


def read_prompt(prompt_file: Path, skill_file: Path | None, stage: str) -> str:
    """Read the stage prompt and optionally prepend skill instructions."""
    prompt = prompt_file.read_text(encoding="utf-8")
    if skill_file is None:
        return prompt
    skill_text = skill_file.read_text(encoding="utf-8")
    return (
        f"You are running ProjectHephaestus Codex stage `{stage}`.\n\n"
        "Use these skill instructions as authoritative context for this stage:\n\n"
        f"{skill_text}\n\n"
        "---\n\n"
        f"{prompt}"
    )


def codex_approval_args(approval: str) -> list[str]:
    """Return approval arguments supported by the installed Codex CLI."""
    try:
        result = subprocess.run(
            ["codex", "exec", "--help"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    help_text = result.stdout or ""
    if "--approval-policy" in help_text:
        return ["--approval-policy", approval]
    if "--ask-for-approval" in help_text:
        return ["--ask-for-approval", approval]
    return []


def run_codex(args: argparse.Namespace) -> int:
    """Run Codex for one automation stage and persist its output/log files."""
    repo_root = Path(args.repo_root).expanduser().resolve()
    prompt_file = Path(args.prompt_file).expanduser().resolve()
    output_file = Path(args.output).expanduser().resolve()
    log_file = Path(args.log_file).expanduser().resolve() if args.log_file else None
    skill_file = Path(args.skill_file).expanduser().resolve() if args.skill_file else None

    prompt = read_prompt(prompt_file, skill_file, args.stage)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "codex",
        "exec",
        "--cd",
        str(repo_root),
        "--sandbox",
        args.sandbox,
        "--output-last-message",
        str(output_file),
        "-",
    ]
    cmd[8:8] = codex_approval_args(args.approval)
    if args.model:
        cmd[2:2] = ["--model", args.model]

    if args.debug:
        print("Running:", " ".join(cmd), file=sys.stderr)

    env = os.environ.copy()
    env.setdefault("CODEX_HOME", str(Path.home() / ".codex"))
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            cwd=repo_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=args.timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        if log_file is not None:
            log_file.write_text(str(exc), encoding="utf-8")
        return 124

    if log_file is not None:
        log_file.write_text(result.stdout or "", encoding="utf-8")
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    """Run the Codex stage command-line interface."""
    return run_codex(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())

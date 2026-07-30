"""Reproducible recent-change and source-pattern evidence collection."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence

from hephaestus.cli.utils import add_json_arg, create_parser, emit_json_status
from hephaestus.github.git_ops import run_git

_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _git_output(*arguments: str, accepted_codes: tuple[int, ...] = (0,)) -> str:
    """Run Git through the shared adapter and return stdout."""
    result = run_git(list(arguments), check=False, log_on_error=False)
    if result.returncode not in accepted_codes:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result.stdout


def repository_evidence_main(argv: Sequence[str] | None = None) -> int:
    """Collect reproducible recent-change and source-pattern evidence."""
    parser = create_parser("hephaestus-repository-evidence", description=__doc__)
    parser.add_argument("pattern")
    parser.add_argument("--source-root", default=".")
    add_json_arg(parser)
    arguments = parser.parse_args(argv)
    try:
        try:
            recent_revisions = _git_output("rev-list", "--max-count=10", "HEAD").splitlines()
        except RuntimeError as error:
            raise RuntimeError(f"cannot resolve HEAD: {error}") from error
        if not recent_revisions:
            raise RuntimeError("cannot resolve HEAD: repository has no commits")
        recent_commits = _git_output("log", "--oneline", "-10")
        oldest_parent = _git_output(
            "rev-parse",
            "--verify",
            f"{recent_revisions[-1]}^",
            accepted_codes=(0, 128),
        ).strip()
        recent_range = f"{oldest_parent or _EMPTY_TREE}..HEAD"
        recent_diff = _git_output("diff", "--stat", recent_range)
        pattern_matches = _git_output(
            "grep",
            "--line-number",
            "-e",
            arguments.pattern,
            "--",
            arguments.source_root,
            accepted_codes=(0, 1),
        )
    except (FileNotFoundError, RuntimeError, subprocess.SubprocessError) as error:
        if arguments.json:
            emit_json_status(1, str(error))
        else:
            print(error, file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "pattern_matches": pattern_matches,
                "recent_commits": recent_commits,
                "recent_diff": recent_diff,
                "recent_range": recent_range,
            },
            sort_keys=True,
        )
    )
    return 0

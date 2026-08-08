"""Portable worktree helpers used by host-neutral workflow skills.

The helpers in this module deliberately use the shared Git adapter instead of
the automation product layer.  They are suitable for a caller working in an
arbitrary Git repository and preserve the conservative safety checks required
for creating, auditing, and removing worktrees.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from hephaestus.cli.utils import add_json_arg, create_parser, emit_json_status
from hephaestus.github.git_ops import run_git


def _git_output(cwd: Path, *arguments: str, accepted_codes: tuple[int, ...] = (0,)) -> str:
    """Run Git and return stdout, raising a concise operational error."""
    result = run_git(
        list(arguments),
        cwd=cwd,
        check=False,
        log_on_error=False,
    )
    if result.returncode not in accepted_codes:
        message = result.stderr.strip() or f"git {' '.join(arguments)} failed"
        raise RuntimeError(message)
    return result.stdout


def reject_symlinks_below(trust_root: Path, target: Path) -> None:
    """Reject symlinks in the caller-controlled path below a trusted root."""
    lexical_root = trust_root.absolute()
    lexical_target = target.absolute()
    try:
        lexical_target.relative_to(lexical_root)
    except ValueError as error:
        raise RuntimeError(f"worktree path escapes trusted root {lexical_root}") from error
    for component in (*reversed(lexical_target.parents), lexical_target):
        if component.is_symlink():
            raise RuntimeError(f"worktree path component is a symlink: {component}")
    resolved_root = lexical_root.resolve()
    resolved_target = lexical_target.resolve()
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as error:
        raise RuntimeError(f"worktree path escapes trusted root {resolved_root}") from error


def select_worktree_path(
    root: Path,
    branch: str,
    requested_directory: Path | None,
    exact_path: Path | None,
    path_root: Path | None,
) -> tuple[Path, bool]:
    """Return the requested safe target path and whether it is project-local."""
    if exact_path is not None:
        path = exact_path if exact_path.is_absolute() else root / exact_path
        trust_root = path_root if path_root is not None else path.parent
        if not trust_root.is_absolute():
            trust_root = root / trust_root
        reject_symlinks_below(trust_root, path)
        resolved_path = path.resolve()
        return resolved_path, resolved_path.is_relative_to(root)
    if requested_directory is not None:
        base = (
            requested_directory if requested_directory.is_absolute() else root / requested_directory
        )
        path = base / branch
        reject_symlinks_below(base, path)
        resolved_path = path.resolve()
        return resolved_path, resolved_path.is_relative_to(root)
    for directory_name in (".worktrees", "worktrees"):
        directory = root / directory_name
        if directory.is_dir():
            if directory.is_symlink():
                raise RuntimeError(f"project-local worktree directory is a symlink: {directory}")
            path = directory / branch
            reject_symlinks_below(directory, path)
            resolved_path = path.resolve()
            return resolved_path, resolved_path.is_relative_to(root)
    temporary_root = Path(tempfile.gettempdir())
    path = temporary_root / f"{root.name}-{branch}"
    reject_symlinks_below(temporary_root, path)
    resolved_path = path.resolve()
    return resolved_path, resolved_path.is_relative_to(root)


def verify_ignored(root: Path, path: Path) -> None:
    """Require a project-local worktree directory to be ignored by Git."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return
    probe = relative.parent / ".hephaestus-ignore-probe"
    result = run_git(
        ["check-ignore", "-q", "--", str(probe)],
        cwd=root,
        check=False,
        log_on_error=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"project-local worktree directory {relative.parent} is not ignored")


def worktree_status(path: Path) -> str:
    """Return every local worktree change, including ignored user data."""
    return _git_output(path, "status", "--short", "--untracked-files=all", "--ignored=matching")


def prepare_worktree_main(argv: Sequence[str] | None = None) -> int:
    """Select, validate, and optionally create an isolated Git worktree."""
    parser = create_parser("hephaestus-prepare-worktree", description=__doc__)
    parser.add_argument("branch")
    path_selection = parser.add_mutually_exclusive_group()
    path_selection.add_argument("--directory", type=Path)
    path_selection.add_argument("--path", type=Path)
    parser.add_argument("--path-root", type=Path)
    parser.add_argument("--start-point", required=True)
    parser.add_argument("--dry-run", action="store_true")
    add_json_arg(parser)
    arguments = parser.parse_args(argv)
    try:
        root = Path(_git_output(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        branch_check = run_git(
            ["check-ref-format", "--branch", arguments.branch],
            cwd=root,
            check=False,
            log_on_error=False,
        )
        if branch_check.returncode != 0:
            raise RuntimeError(f"invalid branch name: {arguments.branch}")
        if (arguments.path is None) != (arguments.path_root is None):
            raise RuntimeError("--path and --path-root must be provided together")
        path, project_local = select_worktree_path(
            root,
            arguments.branch,
            arguments.directory,
            arguments.path,
            arguments.path_root,
        )
        start_sha = _git_output(
            root,
            "rev-parse",
            "--verify",
            f"{arguments.start_point}^{{commit}}",
        ).strip()
        if project_local:
            verify_ignored(root, path)
        if path.exists():
            raise RuntimeError(f"worktree path already exists: {path}")
        if not arguments.dry_run:
            _git_output(root, "worktree", "add", str(path), "-b", arguments.branch, start_sha)
    except (FileNotFoundError, RuntimeError, subprocess.SubprocessError) as error:
        if arguments.json:
            emit_json_status(1, str(error))
        else:
            print(error, file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "branch": arguments.branch,
                "created": not arguments.dry_run,
                "path": str(path),
                "start_sha": start_sha,
            },
            sort_keys=True,
        )
    )
    return 0


def parse_worktree_porcelain(output: str) -> list[dict[str, Any]]:
    """Parse ``git worktree list --porcelain`` records."""
    records: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in [*output.splitlines(), ""]:
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value if value else True
    return records


def audit_worktrees_main(argv: Sequence[str] | None = None) -> int:
    """Emit a read-only, machine-readable inventory of registered worktrees."""
    parser = create_parser("hephaestus-audit-worktrees", description=__doc__)
    add_json_arg(parser)
    arguments = parser.parse_args(argv)
    try:
        root = Path(_git_output(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        records = parse_worktree_porcelain(_git_output(root, "worktree", "list", "--porcelain"))
        for record in records:
            path = Path(str(record["worktree"]))
            record["path"] = str(path)
            record["exists"] = path.is_dir()
            porcelain_head = str(record.pop("HEAD", ""))
            record.pop("worktree", None)
            if not record["exists"]:
                record["clean"] = False
                record["status"] = []
                record["recent_commits"] = []
                record["head"] = porcelain_head
                continue
            status = worktree_status(path)
            record["clean"] = not bool(status.strip())
            record["status"] = status.splitlines()
            record["recent_commits"] = _git_output(
                path, "log", "--oneline", "--decorate", "-5"
            ).splitlines()
            record["head"] = _git_output(path, "rev-parse", "--verify", "HEAD").strip()
    except (FileNotFoundError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        if arguments.json:
            emit_json_status(1, str(error))
        else:
            print(error, file=sys.stderr)
        return 1
    print(json.dumps(records, indent=2, sort_keys=True))
    return 0


def remove_worktree_main(argv: Sequence[str] | None = None) -> int:
    """Safely remove one clean, non-current registered worktree after approval."""
    parser = create_parser("hephaestus-remove-worktree", description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--expected-head", required=True)
    add_json_arg(parser)
    arguments = parser.parse_args(argv)
    try:
        root = Path(_git_output(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        target = arguments.path.resolve()
        current = Path.cwd().resolve()
        if current == target or target in current.parents:
            raise RuntimeError("refusing to remove the current worktree")
        registered = {
            Path(line.removeprefix("worktree ")).resolve()
            for line in _git_output(root, "worktree", "list", "--porcelain").splitlines()
            if line.startswith("worktree ")
        }
        if target not in registered:
            raise RuntimeError(f"not a registered worktree: {target}")
        if worktree_status(target).strip():
            raise RuntimeError(f"worktree is not clean: {target}")
        head = _git_output(target, "rev-parse", "--verify", "HEAD").strip()
        if head != arguments.expected_head:
            raise RuntimeError(
                f"worktree HEAD changed: expected {arguments.expected_head}, found {head}"
            )
        _git_output(root, "worktree", "remove", str(target))
    except (FileNotFoundError, RuntimeError, subprocess.SubprocessError) as error:
        if arguments.json:
            emit_json_status(1, str(error))
        else:
            print(error, file=sys.stderr)
        return 1
    if arguments.json:
        emit_json_status(0, f"removed {target} at {head}", path=str(target), head=head)
    else:
        print(f"removed {target} at {head}")
    return 0

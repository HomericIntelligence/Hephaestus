"""Run configured PR hooks against a private candidate without setup or fixes.

Use the prepared CI image: ``python -B -m hephaestus.ci.check_only``.
The pre-commit library supplies configuration, manifests, and file selection.
This command does not call its run command or install hook environments.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pre_commit.all_languages import languages
from pre_commit.clientlib import load_config
from pre_commit.commands.run import Classifier
from pre_commit.envcontext import UNSET, envcontext
from pre_commit.hook import Hook
from pre_commit.repository import _hook_installed, all_hooks
from pre_commit.store import Store

from hephaestus.cli.localization import text
from hephaestus.config.child_environments import build_check_only_git_env


class PreparationError(RuntimeError):
    """The selected checks cannot run with the prepared inputs."""


# These are execution contracts, not copies of the hook file selectors.
# A new command requires review before it can run in this verification mode.
_LOCAL_ENTRIES = {
    "ruff-format-python": "uv run ruff format",
    "ruff-check-python": "uv run ruff check --fix",
    "mypy-check-python": "uv run mypy hephaestus/ scripts/ tests/",
    "fast-tests": "bash scripts/run_fast_tests.sh",
    "bandit": "uv run bandit -c pyproject.toml -r hephaestus scripts --severity-level medium",
    "zizmor": (
        "uv run zizmor --no-online-audits --min-severity medium .github/workflows/ .github/actions/"
    ),
    "check-no-unlinked-todo": "uv run python -m hephaestus.validation.unlinked_todo",
    "check-environment-variables": "uv run python -m hephaestus.validation.environment_variables",
    "check-cli-table-sync": "uv run python -m hephaestus.scripts_lib.check_cli_table_sync",
    "check-doc-maintenance": "uv run python -m hephaestus.validation.doc_maintenance",
    "check-doc-config": "uv run hephaestus-check-doc-config --repo-root . --skip-test-count",
    "check-settings-permission-paths": (
        "uv run python -m hephaestus.scripts_lib.check_settings_permission_paths"
    ),
    "check-unit-test-structure": "uv run hephaestus-check-test-structure",
    "hephaestus-check-cli-tier-docs": "uv run hephaestus-check-cli-tier-docs",
    "hephaestus-check-api-table-docs": "uv run hephaestus-check-api-table-docs",
    "ruff-check-complexity": "uv run ruff check --select C901 hephaestus/",
    "check-python-version-consistency": "uv run hephaestus-check-python-version",
    "hephaestus-check-workflow-inventory": "uv run hephaestus-check-workflow-inventory",
    "check-version-single-source": (
        "uv run python -m hephaestus.scripts_lib.check_version_single_source"
    ),
    "check-security-policy-no-hardcoded-date": (
        "python3 scripts/check_security_policy_no_hardcoded_date.py"
    ),
    "check-build-dir-untracked": "python3 scripts/check_build_dir_untracked.py",
    "check-repo-local-skill-surface": "python3 scripts/check_repo_local_skill_surface.py",
    "check-private-denylist": "python3 scripts/check_private_denylist.py --staged --tracked",
    "shellcheck": "shellcheck",
}
_PYGREP_CONTRACTS = {
    "forbid-or-true": (
        r"\|\|\s*true(\s*$|\s+#)",
        r"\.(sh|bash|yml|yaml|hcl)$|(^|/)Dockerfile[^/]*$|(^|/)[Jj]ustfile$",
        "^$",
        ("text",),
    ),
    "forbid-continue-on-error": (
        r"^\s*continue-on-error:\s*true\s*$",
        r"^\.github/workflows/.*\.ya?ml$",
        "^$",
        ("file",),
    ),
    "forbid-advisory-warnings": (
        "::warning::",
        r"^\.github/workflows/.*\.ya?ml$",
        r"^\.github/workflows/_required\.yml$",
        ("file",),
    ),
    "forbid-unwhitelisted-add-to-bashrc": (
        r'add_to_bashrc\s+"(?!(?:eval \\"\\\$\(/[A-Za-z0-9._/\-]+ shellenv\)\\"|'
        r'export PATH=\\\$PATH:[A-Za-z0-9._/\-$]+)")[^"]*"',
        r"^scripts/shell/install\.sh$",
        "^$",
        ("file",),
    ),
}
_REMOTE_ENTRIES = {
    "https://github.com/pre-commit/pre-commit-hooks": {
        "trailing-whitespace": "trailing-whitespace-fixer",
        "end-of-file-fixer": "end-of-file-fixer",
        "check-yaml": "check-yaml",
        "check-toml": "check-toml",
        "check-added-large-files": "check-added-large-files",
        "mixed-line-ending": "mixed-line-ending",
        "check-merge-conflict": "check-merge-conflict",
        "debug-statements": "debug-statement-hook",
        "detect-private-key": "detect-private-key",
        "detect-aws-credentials": "detect-aws-credentials",
    },
    "https://github.com/DavidAnson/markdownlint-cli2": {"markdownlint-cli2": "markdownlint-cli2"},
    "https://github.com/adrienverge/yamllint": {"yamllint": "yamllint"},
    "https://github.com/astral-sh/uv-pre-commit": {"uv-lock": "uv lock"},
    "https://github.com/gitleaks/gitleaks": {
        "gitleaks": "gitleaks git --pre-commit --redact --staged --verbose"
    },
}
_PRIVATE_GIT = (
    ("GIT_DIR", UNSET),
    ("GIT_COMMON_DIR", UNSET),
    ("GIT_WORK_TREE", UNSET),
    ("GIT_INDEX_FILE", UNSET),
    ("GIT_OBJECT_DIRECTORY", UNSET),
    ("GIT_ALTERNATE_OBJECT_DIRECTORIES", UNSET),
    ("GIT_CONFIG_GLOBAL", os.devnull),
    ("GIT_CONFIG_NOSYSTEM", "1"),
    ("GIT_NO_REPLACE_OBJECTS", "1"),
    ("GIT_OPTIONAL_LOCKS", "0"),
)


class PreparedStore(Store):
    """Read existing cache records without creating or repairing a store."""

    def __init__(self) -> None:
        """Bind the existing cache path without initializing it."""
        self.directory = self.get_default_directory()
        self.db_path = str(Path(self.directory) / "db.db")

    def clone(self, repo: str, ref: str, deps: Sequence[str] = ()) -> str:
        """Resolve an existing pinned manifest; never clone a repository."""
        if re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", ref) is None:
            raise PreparationError(
                f"Hook repository revision is not an immutable object ID: {repo} at {ref}"
            )
        database = Path(self.db_path)
        if not database.is_file():
            raise PreparationError(f"Prepared hook cache is absent: {repo}")
        try:
            with contextlib.closing(
                sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            ) as conn:
                row = conn.execute(
                    "SELECT path FROM repos WHERE repo = ? AND ref = ?",
                    (self.db_repo_name(repo, deps), ref),
                ).fetchone()
        except sqlite3.Error as error:
            raise PreparationError(f"Cannot read the prepared hook cache: {repo}") from error
        if row is None or not Path(row[0]).is_dir():
            raise PreparationError(f"Pinned hook repository is not prepared: {repo} at {ref}")
        return str(row[0])

    def make_local(self, deps: Sequence[str]) -> str:
        """Reject local hooks that require an unreviewed environment."""
        raise PreparationError("Local hooks must use their prepared system tools")


def _git(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        env=build_check_only_git_env(),
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise PreparationError("Cannot prepare or inspect candidate Git state")
    return result.stdout


def _paths(root: Path) -> tuple[str, ...]:
    output = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return tuple(sorted({os.fsdecode(name) for name in output.split(b"\0") if name}))


def _copy_file(source: Path, target: Path, root: Path) -> None:
    if not source.exists() and not source.is_symlink():
        return
    if source.is_symlink():
        link = os.readlink(source)
        if Path(link).is_absolute() or not source.resolve().is_relative_to(root):
            raise PreparationError(
                f"Candidate symlink leaves the private tree: {source.relative_to(root)}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(link)
    elif source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    else:
        raise PreparationError(f"Candidate path is not a source file: {source.relative_to(root)}")


def _prepare_candidate(source: Path, target: Path) -> tuple[str, ...]:
    names = _paths(source)
    for name in names:
        _copy_file(source / name, target / name, source)
    policy = source / ".heph-private-denylist"
    if policy.exists() and ".heph-private-denylist" not in names:
        _copy_file(policy, target / policy.name, source)
    head = _git(source, "rev-parse", "--verify", "HEAD").strip()
    object_format = _git(source, "rev-parse", "--show-object-format").decode().strip()
    objects = (
        _git(source, "rev-parse", "--path-format=absolute", "--git-path", "objects")
        .decode()
        .strip()
    )
    alternates = [
        line.removeprefix("alternate: ")
        for line in _git(source, "count-objects", "-v").decode().splitlines()
        if line.startswith("alternate: ")
    ]
    exclude = Path(
        _git(source, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude")
        .decode()
        .strip()
    )
    with envcontext(_PRIVATE_GIT):
        _git(target, "init", "--quiet", "--template=", f"--object-format={object_format}")
        (target / ".git/HEAD").write_bytes(head + b"\n")
        (target / ".git/objects/info/alternates").write_text(
            "\n".join([objects, *alternates]) + "\n"
        )
        if exclude.is_file():
            (target / ".git/info").mkdir(exist_ok=True)
            shutil.copy2(exclude, target / ".git/info/exclude")
        _git(target, "read-tree", "HEAD")
        _git(target, "add", "--all", "--", ".")
    return names


def _file_state(path: Path) -> tuple[int, bytes] | None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(mode):
        content = os.fsencode(os.readlink(path))
    elif stat.S_ISREG(mode):
        content = path.read_bytes()
    else:
        content = b""
    return mode, hashlib.sha256(content).digest()


def _tree_state(root: Path) -> dict[str, tuple[int, bytes] | None]:
    return {
        str(path.relative_to(root)): _file_state(path)
        for path in root.rglob("*")
        if not path.is_dir() or path.is_symlink()
    }


def _adapt(hook: Hook) -> Hook:
    if hook.src == "local" and hook.id in _PYGREP_CONTRACTS:
        expected_entry, expected_files, expected_exclude, expected_types = _PYGREP_CONTRACTS[
            hook.id
        ]
        contract = (
            hook.entry,
            hook.files,
            hook.exclude,
            tuple(hook.types),
            tuple(hook.types_or),
            tuple(hook.exclude_types),
            tuple(hook.args),
            hook.always_run,
            hook.pass_filenames,
        )
        expected = (
            expected_entry,
            expected_files,
            expected_exclude,
            expected_types,
            (),
            (),
            (),
            False,
            True,
        )
        if hook.language != "pygrep" or contract != expected or "pre-commit" not in hook.stages:
            raise PreparationError(f"Unsupported hook execution contract: {hook.id}")
        return hook
    entries = _LOCAL_ENTRIES if hook.src == "local" else _REMOTE_ENTRIES.get(hook.src, {})
    # Pinned pre-commit normalizes the configured `system` language to this name.
    if entries.get(hook.id) != hook.entry or hook.language not in {
        "unsupported",
        "python",
        "node",
        "golang",
    }:
        raise PreparationError(f"Unsupported hook execution contract: {hook.id}")
    if hook.src == "local" and hook.language != "unsupported":
        raise PreparationError(f"Unsupported local hook language: {hook.id}")
    if hook.id == "ruff-format-python":
        return hook._replace(args=(*hook.args, "--check"))
    if hook.id in {"ruff-check-python", "ruff-check-complexity"}:
        entry = shlex.join(arg for arg in shlex.split(hook.entry) if arg != "--fix")
        return hook._replace(entry=entry, args=(*hook.args, "--no-fix"))
    if hook.id == "markdownlint-cli2":
        return hook._replace(args=tuple(arg for arg in hook.args if arg != "--fix"))
    if hook.id == "mixed-line-ending":
        if hook.args:
            raise PreparationError("Unsupported mixed-line-ending repair options")
        return hook._replace(args=("--fix=no",))
    if hook.id == "uv-lock" and tuple(hook.args) != ("--check",):
        raise PreparationError("uv-lock must retain its --check argument")
    return hook


def _ready(hook: Hook) -> None:
    if not _hook_installed(hook):
        raise PreparationError(f"Hook environment is not prepared: {hook.id}")
    if hook.language == "pygrep":
        return
    language = languages[hook.language]
    with language.in_env(hook.prefix, hook.language_version):
        executable = shlex.split(hook.entry)[0]
        if shutil.which(executable) is None:
            raise PreparationError(f"Hook executable is not prepared: {hook.id}: {executable}")


def _selected(
    config: dict[str, Any], names: tuple[str, ...]
) -> tuple[tuple[Hook, tuple[str, ...]], ...]:
    classifier = Classifier.from_config(names, config["files"], config["exclude"])
    selected = []
    for configured in all_hooks(config, PreparedStore()):
        if configured.src == "local" and configured.id in _PYGREP_CONTRACTS:
            hook = _adapt(configured)
        else:
            if "pre-commit" not in configured.stages:
                continue
            hook = _adapt(configured)
        if "pre-commit" not in hook.stages:
            continue
        files = tuple(classifier.filenames_for_hook(hook))
        if files or hook.always_run:
            _ready(hook)
            selected.append((hook, files if hook.pass_filenames else ()))
    return tuple(selected)


def _restore(candidate: Path, baseline: Path, names: tuple[str, ...]) -> bool:
    """Detect source changes and restore the input for the next hook."""
    changed = False
    original_names = {*names, ".heph-private-denylist"}
    for name in sorted(original_names | set(_paths(candidate))):
        before, after = baseline / name, candidate / name
        if _file_state(before) == _file_state(after):
            continue
        changed = True
        if after.is_dir() and not after.is_symlink():
            shutil.rmtree(after)
        elif after.exists() or after.is_symlink():
            after.unlink()
        _copy_file(before, after, baseline)
    if _tree_state(candidate / ".git") != _tree_state(baseline / ".git"):
        changed = True
        shutil.rmtree(candidate / ".git")
        shutil.copytree(baseline / ".git", candidate / ".git")
    return changed


def _run(source: Path, temporary: Path) -> int:
    candidate = temporary / "candidate"
    candidate.mkdir()
    names = _prepare_candidate(source, candidate)
    baseline = temporary / "baseline"
    shutil.copytree(candidate, baseline, symlinks=True)
    scratch = temporary / "cache"
    scratch.mkdir()
    patches = (
        *_PRIVATE_GIT,
        ("UV_NO_SYNC", "1"),
        ("UV_OFFLINE", "1"),
        ("UV_PROJECT_ENVIRONMENT", sys.prefix),
        ("UV_CACHE_DIR", str(scratch / "uv")),
        ("RUFF_CACHE_DIR", str(scratch / "ruff")),
        ("MYPY_CACHE_DIR", str(scratch / "mypy")),
        ("COVERAGE_FILE", str(scratch / ".coverage")),
        ("PYTHONPATH", str(candidate)),
        ("PYTHONDONTWRITEBYTECODE", "1"),
        ("PRE_COMMIT_NO_CONCURRENCY", "1"),
    )
    failed = False
    with contextlib.chdir(candidate), envcontext(patches):
        config = load_config(".pre-commit-config.yaml")
        selected = _selected(config, names)
        for hook, filenames in selected:
            language = languages[hook.language]
            with language.in_env(hook.prefix, hook.language_version):
                status, output = language.run_hook(
                    hook.prefix,
                    hook.entry,
                    hook.args,
                    filenames,
                    is_local=hook.src == "local",
                    require_serial=hook.require_serial,
                    color=False,
                )
            changed = _restore(candidate, baseline, names)
            rejected = status != 0 or changed
            failed |= rejected
            print(
                text(
                    "%(hook_id)s: %(status)s",
                    hook_id=hook.id,
                    status=text("FAILED") if rejected else text("passed"),
                ),
                flush=True,
            )
            if output:
                sys.stdout.buffer.write(output)
                sys.stdout.buffer.flush()
            if changed:
                print(
                    text("The hook changed its private candidate; original source is unchanged."),
                    flush=True,
                )
            if rejected and (hook.fail_fast or config["fail_fast"]):
                break
    return int(failed)


def main() -> int:
    """Return failure for a rejected check or missing prepared input."""
    try:
        source = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
        with tempfile.TemporaryDirectory(prefix="hephaestus-check-only-") as temporary:
            return _run(source, Path(temporary))
    except (PreparationError, OSError, ValueError) as error:
        print(text("Check-only preparation failed: %(error)s", error=error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

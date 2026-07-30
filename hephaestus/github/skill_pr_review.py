"""Portable pull-request evidence helpers for host-neutral workflow skills."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

from hephaestus.cli.utils import (
    add_github_throttle_args,
    add_json_arg,
    configure_github_throttle_from_args,
    create_parser,
    emit_json_status,
)
from hephaestus.github.client import gh_call
from hephaestus.github.git_ops import run_git

_PR_URL = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/[1-9][0-9]*/?")
_RESOLVE_FIELDS = "number,url,state,headRefName,baseRefName,headRefOid,baseRefOid"
_EVIDENCE_FIELDS = (
    "number,title,body,state,isDraft,author,baseRefName,headRefName,"
    "reviews,statusCheckRollup,closingIssuesReferences,url"
)


def validate_pr_identifier(value: str) -> None:
    """Require a positive PR number or canonical GitHub pull-request URL."""
    if (value.isascii() and value.isdigit() and int(value) > 0) or _PR_URL.fullmatch(value):
        return
    raise RuntimeError(f"invalid pull-request identifier: {value!r}")


def repository_from_pr_url(url: str, number: int) -> str:
    """Return owner/repository after validating a canonical PR URL and number."""
    parsed_url = urlparse(url)
    path_parts = parsed_url.path.strip("/").split("/")
    if (
        parsed_url.hostname != "github.com"
        or len(path_parts) != 4
        or path_parts[2] != "pull"
        or path_parts[3] != str(number)
    ):
        raise RuntimeError(f"GitHub returned invalid pull-request URL: {url}")
    return "/".join(path_parts[:2])


def _command_error(result: subprocess.CompletedProcess[str], command: str) -> RuntimeError:
    return RuntimeError(result.stderr.strip() or f"{command} failed")


def _gh_output(*arguments: str, accepted_codes: tuple[int, ...] = (0,)) -> str:
    """Run ``gh`` through the shared adapter and return stdout."""
    result = gh_call(list(arguments), check=False)
    if result.returncode not in accepted_codes:
        raise _command_error(result, f"gh {' '.join(arguments)}")
    return result.stdout


def _git_output(*arguments: str) -> str:
    """Run Git through the shared adapter and return stdout."""
    result = run_git(list(arguments), check=False, log_on_error=False)
    if result.returncode != 0:
        raise _command_error(result, f"git {' '.join(arguments)}")
    return result.stdout


def _load_object(output: str) -> dict[str, Any]:
    """Decode one GitHub JSON object or reject malformed results."""
    value: Any = json.loads(output)
    if not isinstance(value, dict):
        raise RuntimeError("GitHub returned an invalid pull-request object")
    return value


def _resolve_open_pr(identifier: str) -> dict[str, Any]:
    """Return metadata for one explicitly identified open PR."""
    validate_pr_identifier(identifier)
    pull_request = _load_object(_gh_output("pr", "view", identifier, "--json", _RESOLVE_FIELDS))
    if pull_request.get("state") != "OPEN":
        raise RuntimeError(f"pull request {identifier} is not open")
    return pull_request


def _validate_repository_identity(pull_request: dict[str, Any]) -> None:
    """Reject a PR URL that belongs to a different repository than the checkout."""
    number = pull_request.get("number")
    url = pull_request.get("url")
    if not isinstance(number, int) or not isinstance(url, str):
        raise RuntimeError("GitHub returned incomplete pull-request identity")
    repository = _load_object(_gh_output("repo", "view", "--json", "nameWithOwner"))
    current = repository.get("nameWithOwner")
    if not isinstance(current, str) or not current:
        raise RuntimeError("GitHub returned incomplete repository identity")
    pull_repository = repository_from_pr_url(url, number)
    if pull_repository.casefold() != current.casefold():
        raise RuntimeError(f"pull request {url} does not belong to current repository {current}")


def resolve_pull_request(explicit: str | None) -> dict[str, Any]:
    """Resolve an explicit PR or the sole open PR associated with this branch."""
    if explicit:
        return _resolve_open_pr(explicit)

    branch = _git_output("branch", "--show-current").strip()
    if not branch:
        raise RuntimeError("current checkout is detached; provide a PR number or URL")
    candidates_value: Any = json.loads(
        _gh_output(
            "pr",
            "list",
            "--state",
            "open",
            "--head",
            branch,
            "--json",
            _RESOLVE_FIELDS,
            "--limit",
            "2",
        )
    )
    if not isinstance(candidates_value, list):
        raise RuntimeError("GitHub returned an invalid pull-request list")
    candidates = [item for item in candidates_value if isinstance(item, dict)]
    if len(candidates) == 1:
        number = candidates[0].get("number")
        if not isinstance(number, int) or number < 1:
            raise RuntimeError("GitHub returned an invalid pull-request candidate")
        return _resolve_open_pr(str(number))
    if not candidates:
        raise LookupError(f"no open pull request found for branch {branch!r}")
    rendered = "\n".join(
        f"  #{candidate.get('number')}: {candidate.get('url')}" for candidate in candidates
    )
    raise ValueError(f"multiple open pull requests found for {branch!r}:\n{rendered}")


def _print_error(exit_code: int, error: Exception, json_output: bool) -> int:
    """Emit a conventional CLI error response."""
    if json_output:
        emit_json_status(exit_code, str(error))
    else:
        print(error, file=sys.stderr)
    return exit_code


def resolve_pr_main(argv: Sequence[str] | None = None) -> int:
    """Resolve an explicit PR or the sole open PR for the current branch."""
    parser = create_parser("hephaestus-resolve-pr", description=__doc__)
    parser.add_argument("pull_request", nargs="?", metavar="PR_NUMBER_OR_URL")
    add_github_throttle_args(parser)
    add_json_arg(parser)
    arguments = parser.parse_args(argv)
    configure_github_throttle_from_args(arguments)
    try:
        pull_request = resolve_pull_request(arguments.pull_request)
        _validate_repository_identity(pull_request)
    except json.JSONDecodeError as error:
        return _print_error(1, error, arguments.json)
    except LookupError as error:
        return _print_error(2, error, arguments.json)
    except ValueError as error:
        return _print_error(3, error, arguments.json)
    except (FileNotFoundError, RuntimeError, subprocess.SubprocessError) as error:
        return _print_error(1, error, arguments.json)
    print(json.dumps(pull_request, sort_keys=True))
    return 0


def metadata_error(metadata: object) -> str | None:
    """Return a diagnostic when GitHub returns partial PR metadata."""
    if not isinstance(metadata, dict):
        return "PR metadata must be a JSON object"
    required_types: dict[str, type[object]] = {
        "number": int,
        "title": str,
        "author": dict,
        "baseRefName": str,
        "headRefName": str,
        "statusCheckRollup": list,
        "url": str,
    }
    invalid = [
        field
        for field, expected_type in required_types.items()
        if not isinstance(metadata.get(field), expected_type)
    ]
    author = metadata.get("author")
    if not isinstance(author, dict) or not isinstance(author.get("login"), str):
        invalid.append("author.login")
    if invalid:
        return "GitHub returned incomplete or invalid PR metadata fields: " + ", ".join(
            sorted(set(invalid))
        )
    return None


def collect_pr_evidence_main(argv: Sequence[str] | None = None) -> int:
    """Collect GitHub PR metadata, changed paths, and current check output."""
    parser = create_parser("hephaestus-collect-pr-evidence", description=__doc__)
    parser.add_argument("pull_request", metavar="PR_NUMBER_OR_URL")
    add_github_throttle_args(parser)
    add_json_arg(parser)
    arguments = parser.parse_args(argv)
    configure_github_throttle_from_args(arguments)
    pull_request = arguments.pull_request
    try:
        validate_pr_identifier(pull_request)
        metadata: Any = json.loads(
            _gh_output("pr", "view", pull_request, "--json", _EVIDENCE_FIELDS)
        )
        metadata_problem = metadata_error(metadata)
        if metadata_problem:
            print(
                json.dumps(
                    {"error": "incomplete PR metadata", "details": metadata_problem},
                    sort_keys=True,
                )
            )
            return 1
        repository_data = _load_object(_gh_output("repo", "view", "--json", "nameWithOwner"))
        repository = repository_data.get("nameWithOwner")
        number = metadata.get("number")
        url = metadata.get("url")
        if (
            not isinstance(repository, str)
            or not isinstance(number, int)
            or not isinstance(url, str)
        ):
            raise RuntimeError("GitHub returned incomplete repository or PR identity")
        pull_repository = repository_from_pr_url(url, number)
        if pull_repository.casefold() != repository.casefold():
            raise RuntimeError(
                f"pull request {url} does not belong to current repository {repository}"
            )
        changed_files = [
            line
            for line in _gh_output(
                "api",
                "--paginate",
                f"repos/{repository}/pulls/{number}/files",
                "--jq",
                ".[].filename",
            ).splitlines()
            if line
        ]
        checks: Any = json.loads(
            _gh_output(
                "pr",
                "checks",
                pull_request,
                "--json",
                "name,state,startedAt,completedAt,link,workflow",
                accepted_codes=(0, 1, 8),
            )
        )
        if not isinstance(checks, list):
            raise RuntimeError("GitHub returned invalid check evidence")
    except (
        FileNotFoundError,
        RuntimeError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as error:
        return _print_error(1, error, arguments.json)
    print(
        json.dumps(
            {
                "changed_files": changed_files,
                "changed_paths": changed_files,
                "checks": checks,
                "pull_request": metadata,
            },
            sort_keys=True,
        )
    )
    return 0


def pr_diff_context_main(argv: Sequence[str] | None = None) -> int:
    """Compute the two required pull-request diff lenses."""
    parser = create_parser("hephaestus-pr-diff-context", description=__doc__)
    parser.add_argument("base_ref", metavar="BASE_REF")
    parser.add_argument("head_ref", metavar="HEAD_REF")
    add_json_arg(parser)
    arguments = parser.parse_args(argv)
    base_ref, head_ref = arguments.base_ref, arguments.head_ref
    try:
        for label, value in (("base ref", base_ref), ("head ref", head_ref)):
            if value.startswith("-"):
                raise RuntimeError(f"{label} must not begin with '-': {value!r}")
        _git_output("rev-parse", "--verify", f"{base_ref}^{{commit}}")
        _git_output("rev-parse", "--verify", f"{head_ref}^{{commit}}")
        merge_base = _git_output("merge-base", base_ref, head_ref).strip()
        behind_count = int(_git_output("rev-list", "--count", f"{head_ref}..{base_ref}").strip())
    except (FileNotFoundError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        return _print_error(1, error, arguments.json)
    print(
        json.dumps(
            {
                "base_ref": base_ref,
                "head_ref": head_ref,
                "merge_base": merge_base,
                "behind_count": behind_count,
                "author_intent_range": f"{merge_base}...{head_ref}",
                "current_base_range": f"{base_ref}..{head_ref}",
            },
            sort_keys=True,
        )
    )
    return 0

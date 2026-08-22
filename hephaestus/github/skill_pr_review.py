"""Portable pull-request evidence helpers for host-neutral workflow skills."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from hephaestus.cli.utils import (
    add_github_throttle_args,
    add_json_arg,
    configure_github_throttle_from_args,
    create_parser,
    emit_json_status,
)
from hephaestus.config.child_environments import build_git_child_env
from hephaestus.github.client import gh_call
from hephaestus.github.git_ops import run_git

_PR_URL = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/[1-9][0-9]*")
_COMMIT_OID = re.compile(r"[0-9a-f]{40}\Z")
_GITHUB_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+\Z")
_RESOLVE_FIELDS = "number,url,state,headRefName,baseRefName,headRefOid,baseRefOid"
_EVIDENCE_FIELDS = (
    "number,title,body,state,isDraft,author,baseRefName,headRefName,"
    "reviews,statusCheckRollup,closingIssuesReferences,url"
)
_GIT_READ_ARGUMENTS = ("-c", "core.commitGraph=false", "--no-replace-objects")


@dataclass(frozen=True)
class RepositoryTarget:
    """An explicit GitHub target that never relies on checkout inference."""

    host: str
    repository: str

    def repository_argument(self) -> str:
        """Return the fully qualified repository argument accepted by ``gh``."""
        return f"{self.host}/{self.repository}"


def validate_pr_identifier(value: str) -> None:
    """Require a positive PR number or canonical GitHub pull-request URL."""
    if (value.isascii() and value.isdigit() and int(value) > 0) or _PR_URL.fullmatch(value):
        return
    raise RuntimeError(f"invalid pull-request identifier: {value!r}")


def _pull_request_number(value: str) -> int:
    validate_pr_identifier(value)
    if value.isdigit():
        return int(value)
    return int(urlparse(value).path.rsplit("/", maxsplit=1)[-1])


def _require_commit_oid(value: object, label: str) -> str:
    if not isinstance(value, str) or _COMMIT_OID.fullmatch(value) is None:
        raise RuntimeError(f"{label} must be a lowercase 40-hex Git commit OID")
    return value


def _require_repository(value: object, label: str) -> str:
    if not isinstance(value, str) or _GITHUB_REPOSITORY.fullmatch(value) is None:
        raise RuntimeError(f"{label} must be a canonical GitHub owner/repository")
    return value


def _canonical_pull_request_url(repository: str, number: int) -> str:
    return f"https://github.com/{_require_repository(repository, 'repository')}/pull/{number}"


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
    repository = "/".join(path_parts[:2])
    if url != _canonical_pull_request_url(repository, number):
        raise RuntimeError(f"GitHub returned invalid pull-request URL: {url}")
    return repository


def _command_error(result: subprocess.CompletedProcess[str], command: str) -> RuntimeError:
    return RuntimeError(result.stderr.strip() or f"{command} failed")


def _gh_output(*arguments: str, accepted_codes: tuple[int, ...] = (0,)) -> str:
    """Run ``gh`` through the shared adapter and return stdout."""
    result = gh_call(list(arguments), check=False)
    if result.returncode not in accepted_codes:
        raise _command_error(result, f"gh {' '.join(arguments)}")
    return result.stdout


def _git_output(*arguments: str) -> str:
    """Run an immutable Git read through the shared adapter and return stdout."""
    result = run_git(
        [*_GIT_READ_ARGUMENTS, *arguments],
        check=False,
        env=_git_read_environment(),
        log_on_error=False,
    )
    if result.returncode != 0:
        raise _command_error(result, f"git {' '.join(arguments)}")
    return result.stdout


def _git_read_environment() -> dict[str, str]:
    """Return a hermetic environment for non-interactive immutable Git reads."""
    environment = build_git_child_env()
    environment.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_GRAFT_FILE": os.devnull,
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _require_complete_git_history() -> None:
    """Reject shallow repositories, which cannot provide a complete diff lens."""
    if _git_output("rev-parse", "--is-shallow-repository").strip() != "false":
        raise RuntimeError("repository history is shallow; immutable diff evidence is incomplete")


def _unambiguous_merge_base(base_oid: str, head_oid: str) -> str:
    """Return the only merge base shared by the two immutable revisions."""
    merge_bases = _git_output("merge-base", "--all", base_oid, head_oid).splitlines()
    if len(merge_bases) != 1:
        raise RuntimeError("immutable diff lenses require exactly one merge base")
    return _require_commit_oid(merge_bases[0], "merge base")


def _load_object(output: str) -> dict[str, Any]:
    """Decode one GitHub JSON object or reject malformed results."""
    value: Any = json.loads(output)
    if not isinstance(value, dict):
        raise RuntimeError("GitHub returned an invalid pull-request object")
    return value


def _target_from_arguments(
    parser: Any,
    identifier: str | None,
    host: str | None,
    repository: str | None,
) -> RepositoryTarget:
    """Bind an explicit target, or derive it only from a direct canonical URL."""
    if (host is None) != (repository is None):
        parser.error("--target-host and --target-repository must be supplied together")
    if host is not None and repository is not None:
        if host != "github.com":
            parser.error("--target-host must be github.com")
        try:
            target = RepositoryTarget(
                host=host,
                repository=_require_repository(repository, "--target-repository"),
            )
        except RuntimeError as error:
            parser.error(str(error))
        if identifier is not None and identifier.startswith("https://"):
            try:
                supplied = repository_from_pr_url(identifier, _pull_request_number(identifier))
            except RuntimeError as error:
                parser.error(str(error))
            if supplied.casefold() != target.repository.casefold():
                parser.error("pull-request URL does not match --target-repository")
        return target
    if identifier is not None and identifier.startswith("https://"):
        try:
            return RepositoryTarget(
                host="github.com",
                repository=repository_from_pr_url(identifier, _pull_request_number(identifier)),
            )
        except RuntimeError as error:
            parser.error(str(error))
    parser.error(
        "numeric pull requests and branch discovery require --target-host and --target-repository"
    )
    raise AssertionError("argument parser returned after a target error")


def _resolve_open_pr(identifier: str, target: RepositoryTarget) -> dict[str, Any]:
    """Return metadata for one explicitly identified open PR."""
    number = _pull_request_number(identifier)
    if identifier.startswith("https://"):
        supplied = repository_from_pr_url(identifier, number)
        if supplied.casefold() != target.repository.casefold():
            raise RuntimeError("pull-request URL does not match the retained target")
    pull_request = _load_object(
        _gh_output(
            "pr",
            "view",
            str(number),
            "--repo",
            target.repository_argument(),
            "--json",
            _RESOLVE_FIELDS,
        )
    )
    metadata_problem = resolve_metadata_error(pull_request)
    if metadata_problem:
        raise RuntimeError(metadata_problem)
    if pull_request.get("state") != "OPEN":
        raise RuntimeError(f"pull request {identifier} is not open")
    if pull_request.get("number") != number:
        raise RuntimeError("GitHub returned a pull request different from the request")
    for field in ("baseRefOid", "headRefOid"):
        _require_commit_oid(pull_request.get(field), f"GitHub immutable PR revision {field}")
    return pull_request


def _validate_repository_identity(pull_request: dict[str, Any], target: RepositoryTarget) -> None:
    """Reject a PR URL that differs from the retained explicit forge target."""
    number = pull_request.get("number")
    url = pull_request.get("url")
    if not isinstance(number, int) or not isinstance(url, str):
        raise RuntimeError("GitHub returned incomplete pull-request identity")
    pull_repository = repository_from_pr_url(url, number)
    if pull_repository.casefold() != target.repository.casefold():
        raise RuntimeError(
            f"pull request {url} does not belong to target repository {target.repository}"
        )
    pull_request["review_target"] = {
        "host": target.host,
        "kind": "github",
        "number": number,
        "repository": target.repository,
        "url": url,
    }


def resolve_pull_request(explicit: str | None, target: RepositoryTarget) -> dict[str, Any]:
    """Resolve an explicit PR or the sole open PR associated with this branch."""
    if explicit:
        return _resolve_open_pr(explicit, target)

    branch = _git_output("branch", "--show-current").strip()
    if not branch:
        raise RuntimeError("current checkout is detached; provide a PR number or URL")
    candidates_value: Any = json.loads(
        _gh_output(
            "pr",
            "list",
            "--repo",
            target.repository_argument(),
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
        return _resolve_open_pr(str(number), target)
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
    parser.add_argument("--target-host", metavar="HOST")
    parser.add_argument("--target-repository", metavar="OWNER/REPOSITORY")
    parser.add_argument("pull_request", nargs="?", metavar="PR_NUMBER_OR_URL")
    add_github_throttle_args(parser)
    add_json_arg(parser)
    arguments = parser.parse_args(argv)
    configure_github_throttle_from_args(arguments)
    target = _target_from_arguments(
        parser, arguments.pull_request, arguments.target_host, arguments.target_repository
    )
    try:
        pull_request = resolve_pull_request(arguments.pull_request, target)
        _validate_repository_identity(pull_request, target)
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


def _missing_metadata_fields(
    metadata: object,
    required_types: dict[str, type[object]],
    label: str,
    nonempty_fields: frozenset[str] = frozenset(),
) -> str | None:
    """Return a diagnostic when GitHub omits a required typed field."""
    if not isinstance(metadata, dict):
        return f"{label} must be a JSON object"
    invalid = [
        field
        for field, expected_type in required_types.items()
        if not isinstance(metadata.get(field), expected_type)
        or (field in nonempty_fields and not metadata[field].strip())
    ]
    if invalid:
        return f"GitHub returned incomplete or invalid {label} fields: " + ", ".join(
            sorted(set(invalid))
        )
    return None


def resolve_metadata_error(metadata: object) -> str | None:
    """Return a diagnostic when PR resolution lacks immutable references."""
    return _missing_metadata_fields(
        metadata,
        {
            "number": int,
            "url": str,
            "state": str,
            "headRefName": str,
            "baseRefName": str,
            "headRefOid": str,
            "baseRefOid": str,
        },
        "pull-request resolution metadata",
        frozenset({"url", "state", "headRefName", "baseRefName", "headRefOid", "baseRefOid"}),
    )


def metadata_error(metadata: object) -> str | None:
    """Return a diagnostic when GitHub returns partial PR metadata."""
    if not isinstance(metadata, dict):
        return "GitHub returned PR metadata that is not a JSON object"
    required_types: dict[str, type[object]] = {
        "number": int,
        "title": str,
        "body": str,
        "state": str,
        "isDraft": bool,
        "author": dict,
        "baseRefName": str,
        "headRefName": str,
        "reviews": list,
        "statusCheckRollup": list,
        "closingIssuesReferences": list,
        "url": str,
    }
    problem = _missing_metadata_fields(metadata, required_types, "PR metadata")
    if problem:
        return problem
    invalid: list[str] = []
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
        changed_pages: Any = json.loads(
            _gh_output(
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repository}/pulls/{number}/files",
            )
        )
        if not isinstance(changed_pages, list) or any(
            not isinstance(page, list) for page in changed_pages
        ):
            raise RuntimeError("GitHub returned invalid changed-file evidence")
        changed_files = []
        for page in changed_pages:
            for changed_file in page:
                if not isinstance(changed_file, dict):
                    raise RuntimeError("GitHub returned invalid changed-file evidence")
                filename = changed_file.get("filename")
                if not isinstance(filename, str):
                    raise RuntimeError("GitHub returned invalid changed-file evidence")
                changed_files.append(filename)
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
        base_ref = _require_commit_oid(base_ref, "base OID")
        head_ref = _require_commit_oid(head_ref, "head OID")
        _require_complete_git_history()
        _git_output("rev-parse", "--verify", f"{base_ref}^{{commit}}")
        _git_output("rev-parse", "--verify", f"{head_ref}^{{commit}}")
        merge_base = _unambiguous_merge_base(base_ref, head_ref)
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

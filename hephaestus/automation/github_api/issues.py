"""GitHub issue helpers."""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import hephaestus.automation.github_api as _api
from hephaestus.utils.helpers import strip_null_bytes

from ..models import IssueInfo, IssueState


@contextlib.contextmanager
def _body_file(body: str) -> Iterator[str]:
    """Yield a path to a temporary file containing *body*, deleted on exit.

    Use with ``gh <subcmd> --body-file <path>`` instead of ``--body <body>`` so
    large bodies (e.g. multi-KB implementation plans) don't bloat error logs or
    risk hitting argv-size limits. The AGENTS.md convention says temporary
    files belong under ``build/`` of the current repo; if that directory
    exists we use it, otherwise we fall back to the system tempdir.
    """
    build_dir = Path.cwd() / "build"
    tmp_dir = str(build_dir) if build_dir.is_dir() else None
    fd, path = tempfile.mkstemp(
        prefix="gh-body-",
        suffix=".md",
        dir=tmp_dir,
    )
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        yield path
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _check_graphql_errors(data: dict[str, Any], context: str) -> None:
    """Raise RuntimeError if a GraphQL response carries an ``errors`` array.

    The GitHub GraphQL API returns HTTP 200 with ``{"errors": [...]}`` for
    permission, validation, and visibility failures. ``gh`` exits 0 in that
    case, so a plain exit-code check sees success while the operation has
    actually failed. This helper is the single place we surface those.

    ``context`` appears verbatim in the error message so logs identify which
    operation failed.
    """
    errors = data.get("errors")
    if not errors:
        return

    for err in errors:
        msg = err.get("message", "") if isinstance(err, dict) else ""
        err_type = err.get("type", "") if isinstance(err, dict) else ""
        if err_type == "RATE_LIMITED" or "rate limit" in msg.lower():
            reset = _api.gh_rate_limit_reset_epoch() or 0
            raise _api.GitHubRateLimitError(
                f"GraphQL {context} rate-limited: {msg}", reset_epoch=reset
            )

    raise RuntimeError(f"GraphQL {context} failed: {errors!r}")


def gh_issue_json(issue_number: int) -> dict[str, Any]:
    """Fetch issue data as JSON.

    Args:
        issue_number: GitHub issue number

    Returns:
        Issue data dictionary

    Raises:
        RuntimeError: If issue fetch fails

    """
    try:
        result = _api._gh_call(
            ["issue", "view", str(issue_number), "--json", "number,title,state,labels,body"],
        )
        data = cast(dict[str, Any], json.loads(result.stdout))
        # Strip stray NUL bytes at the source so downstream prompt assembly never
        # feeds an embedded null into a subprocess argv (#1661). Title/body are the
        # free-text fields consumed by the planner/implementer prompts; warn on a
        # strip so the (rare) mutation of a user-visible field is never silent.
        for field in ("title", "body"):
            value = data.get(field)
            if isinstance(value, str):
                cleaned = strip_null_bytes(value)
                if cleaned != value:
                    _api.logger.warning(
                        "Stripped NUL byte(s) from issue #%s %s field", issue_number, field
                    )
                    data[field] = cleaned
        return data
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to fetch issue #{issue_number}: {e}") from e


def _repo_args(repo: tuple[str, str] | None) -> list[str]:
    """Return the explicit ``--repo owner/name`` selector args when given."""
    if repo is None:
        return []
    owner, name = repo
    return ["--repo", f"{owner}/{name}"]


def gh_issue_comment(issue_number: int, body: str, repo: tuple[str, str] | None = None) -> None:
    """Post a comment to an issue.

    Args:
        issue_number: GitHub issue number
        body: Comment body text
        repo: ``(owner, name)`` owning *issue_number*; ambient cwd when omitted
            (correct only for single-repo callers, #2245/#2256)

    Raises:
        RuntimeError: If comment post fails

    """
    try:
        with _body_file(body) as path:
            _api._gh_call(
                ["issue", "comment", str(issue_number), "--body-file", path, *_repo_args(repo)]
            )
        _api.logger.info("Posted comment to issue #%s", issue_number)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to post comment to issue #{issue_number}: {e}") from e


def _fetch_issue_comment_ids(
    issue_number: int, repo: tuple[str, str] | None = None
) -> list[dict[str, Any]]:
    """Return up to 100 most-recent issue comments as ``{databaseId, body}`` dicts.

    Unlike :func:`_fetch_issue_comments_graphql` in ``review_state`` (which
    fetches ``body``/``url`` for verdict parsing), this also requests
    ``databaseId`` — the integer id required by the REST update/delete
    endpoints (``/repos/{o}/{r}/issues/comments/{id}``). Newest-first from
    GraphQL is reversed to chronological order so "last match wins" matches
    the rest of the pipeline.

    Args:
        issue_number: GitHub issue number, meaningful only within *repo*.
        repo: ``(owner, name)`` of the repository owning *issue_number*. When
            omitted the repo is resolved from the ambient working directory,
            which is correct only for single-repo callers. The multi-repo loop
            MUST pass this explicitly: an issue number carries no repo, so
            ambient resolution sent cross-repo lookups to the CWD repo,
            404-ing (or, on number collision, silently reading an unrelated
            issue) and tripping the ``github-api`` circuit breaker (#1795).

    Returns an empty list on any failure (callers then fall back to create).

    """
    owner, name = repo if repo is not None else _api.get_repo_info()
    query = (
        "query($owner:String!,$name:String!,$number:Int!){"
        "  repository(owner:$owner,name:$name){"
        "    issue(number:$number){"
        "      comments(last: 100, orderBy: {field: UPDATED_AT, direction: DESC}){"
        "        nodes{ databaseId body }"
        "      }"
        "    }"
        "  }"
        "}"
    )
    try:
        result = _api._gh_call(
            [
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-F",
                f"number={issue_number}",
            ],
        )
        data = json.loads(result.stdout)
        nodes = (
            data.get("data", {})
            .get("repository", {})
            .get("issue", {})
            .get("comments", {})
            .get("nodes", [])
        )
        return list(reversed(nodes))
    except Exception as exc:
        _api.logger.warning("Failed to fetch comment ids for issue #%s: %s", issue_number, exc)
        return []


def gh_issue_delete_comment(comment_id: int) -> None:
    """Delete a single issue comment by its REST database id.

    Args:
        comment_id: The integer ``databaseId`` of the issue comment.

    Raises:
        RuntimeError: If the delete call fails.

    """
    owner, name = _api.get_repo_info()
    try:
        _api._gh_call(
            [
                "api",
                "--method",
                "DELETE",
                f"/repos/{owner}/{name}/issues/comments/{comment_id}",
            ],
        )
        _api.logger.info("Deleted issue comment %s", comment_id)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to delete issue comment {comment_id}: {e}") from e


def gh_issue_upsert_comment(
    issue_number: int,
    marker_prefix: str,
    body: str,
    repo: tuple[str, str] | None = None,
) -> int | None:
    """Create-or-update the single issue comment whose body starts with ``marker_prefix``.

    Enforces the "one comment per role" invariant: the pipeline must keep at
    most one PLAN comment (``# Implementation Plan``) and one REVIEW comment
    (``## 🔍 Plan Review``) per issue, updated in place rather than appended.

    Behaviour:
    - Find every existing comment whose body ``startswith(marker_prefix)``.
    - If none: create a new comment.
    - If one or more: PATCH the newest matching comment with ``body`` and
      DELETE the older duplicates (one-time convergence for issues that
      already accumulated multiple plan/review comments).

    Args:
        issue_number: GitHub issue number.
        marker_prefix: The role marker the comment body must start with.
        body: The full new comment body (should itself start with the marker).

    Returns:
        The ``databaseId`` of the upserted comment, or ``None`` if a fresh
        comment was created via ``gh issue comment`` (whose id we do not parse).

    Raises:
        RuntimeError: If a create/update/delete call fails.

    """
    comments = _api._fetch_issue_comment_ids(issue_number, repo)
    matching = [
        c
        for c in comments
        if str(c.get("body", "")).startswith(marker_prefix) and c.get("databaseId") is not None
    ]

    if not matching:
        # No existing comment with this marker — create a fresh one.
        _api.gh_issue_comment(issue_number, body, repo=repo)
        return None

    # Newest matching comment wins (list is chronological → last element).
    target = matching[-1]
    target_id = int(target["databaseId"])
    owner, name = repo if repo is not None else _api.get_repo_info()

    # Delete older duplicates so only one comment with this marker remains.
    for dup in matching[:-1]:
        dup_id = dup.get("databaseId")
        if dup_id is not None:
            _api.gh_issue_delete_comment(int(dup_id))

    try:
        with _body_file(body) as path:
            _api._gh_call(
                [
                    "api",
                    "--method",
                    "PATCH",
                    f"/repos/{owner}/{name}/issues/comments/{target_id}",
                    "-F",
                    f"body=@{path}",
                ],
            )
        _api.logger.info("Updated issue comment %s (marker %r)", target_id, marker_prefix)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Failed to update issue comment {target_id} on #{issue_number}: {e}"
        ) from e
    return target_id


def _parse_issue_number(output: str) -> int:
    """Extract issue number from gh issue create output (URL or bare number)."""
    match = re.search(r"/issues/(\d+)", output)
    if match:
        return int(match.group(1))
    return int(output.split("/")[-1])


def gh_issue_create(title: str, body: str, labels: list[str] | None = None) -> int:
    """Create a new GitHub issue, auto-creating any missing labels.

    Args:
        title: Issue title
        body: Issue body/description
        labels: Optional list of label names to apply. Missing labels are
            created automatically before the issue is filed.

    Returns:
        Created issue number

    Raises:
        RuntimeError: If issue creation fails

    """
    try:
        if labels:
            _api._ensure_labels_exist(labels)

        with _body_file(body) as body_path:
            # Title is a direct argv element; a stray NUL would make gh's
            # subprocess invocation raise ``ValueError: embedded null byte`` (#1661).
            cmd = ["issue", "create", "--title", strip_null_bytes(title), "--body-file", body_path]
            if labels:
                for label in labels:
                    cmd.extend(["--label", label])

            try:
                result = _api._gh_call(cmd)
            except subprocess.CalledProcessError as e:
                # On a label-not-found error (race or cache miss), create the label and retry once.
                stderr = e.stderr if e.stderr else ""
                m = re.search(
                    r"could not add label:\s*'([^']+)'\s*not found", stderr, re.IGNORECASE
                )
                if m and labels:
                    missing_label = m.group(1)
                    _api.logger.warning(
                        "Label '%s' not found after pre-create; recreating and retrying",
                        missing_label,
                    )
                    _api.gh_create_label(missing_label)
                    result = _api._gh_call(cmd)
                else:
                    raise

        output = result.stdout.strip()
        try:
            issue_number = _parse_issue_number(output)
        except (ValueError, IndexError) as e:
            raise RuntimeError(f"Failed to parse issue number from output: {output}") from e

        _api.logger.info("Created issue #%s", issue_number)
        return issue_number

    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to create issue: {e}") from e


def gh_list_open_issues(limit: int = 500) -> list[int]:
    """Return issue numbers for all open issues in the current repository, ascending.

    Args:
        limit: Maximum number of issues to fetch (default 500).

    Returns:
        Sorted list of open issue numbers.

    Raises:
        RuntimeError: If fetching issues fails.

    """
    try:
        result = _api._gh_call(
            [
                "issue",
                "list",
                "--state",
                "open",
                "--limit",
                str(limit),
                "--json",
                "number",
            ]
        )
        data = json.loads(result.stdout)
        return sorted(item["number"] for item in data)
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as e:
        raise RuntimeError(f"Failed to list open issues: {e}") from e


_CLOSES_LINE_RE = re.compile(r"^Closes #\d+\s*$", re.MULTILINE)


def _assert_body_has_closes(body: str) -> None:
    """Raise if *body* does not contain a 'Closes #N' line.

    Hard-fail rather than auto-fix: a missing closes line means the caller did
    not link a tracking issue, and silently appending one would just hide the
    bug. See repo PR policy.
    """
    if not _CLOSES_LINE_RE.search(body):
        raise ValueError(
            "PR body must contain a 'Closes #N' line per repo policy "
            "(must match ^Closes #\\d+$ on its own line, case-sensitive)"
        )


def is_issue_closed(issue_number: int, cached_states: dict[int, IssueState] | None = None) -> bool:
    """Check if an issue is closed.

    Args:
        issue_number: GitHub issue number
        cached_states: Optional pre-fetched states cache

    Returns:
        True if issue is closed

    """
    if cached_states and issue_number in cached_states:
        # A merged PR referenced as a dependency is terminal too, so treat
        # MERGED as "closed" for skip purposes.
        return cached_states[issue_number].is_done

    try:
        issue_data = _api.gh_issue_json(issue_number)
        return issue_data["state"] in ("CLOSED", "MERGED")
    except Exception as e:
        _api.logger.warning("Failed to check if issue #%s is closed: %s", issue_number, e)
        return False


def parse_issue_dependencies(issue_body: str) -> list[int]:
    """Parse issue dependencies from issue body.

    Looks for patterns like:
    - Depends on #123
    - Depends: #123, #456
    - Blocked by #789

    Args:
        issue_body: Issue body text

    Returns:
        List of dependency issue numbers

    """
    dependencies = []

    # Pattern 1: Find all #numbers after dependency keywords on the same
    # line. Only refs at-or-after the keyword match are harvested — a #N
    # that precedes the keyword (e.g. "Part of epic #1809. Depends on
    # #1811.") must not be treated as a dependency, and a line that merely
    # *mentions* the keyword with no following ref (e.g. "no dependencies.")
    # must yield nothing (#1830).
    dep_keywords = r"(?:depends on|blocked by|requires|dependencies?:?)"
    keyword_re = re.compile(dep_keywords, re.IGNORECASE)
    for line in issue_body.split("\n"):
        keyword_match = keyword_re.search(line)
        if keyword_match:
            for match in re.finditer(r"#(\d+)", line[keyword_match.start() :]):
                dependencies.append(int(match.group(1)))

    # Pattern 2: Find issue references in lists under Dependencies heading
    # Look for a "Dependencies" section and extract list items from it
    dep_section_match = re.search(
        r"##\s*Dependencies.*?\n(.*?)(?=##|\Z)", issue_body, re.IGNORECASE | re.DOTALL
    )
    if dep_section_match:
        dep_section = dep_section_match.group(1)
        list_pattern = r"^\s*[-*]\s*#(\d+)"
        for match in re.finditer(list_pattern, dep_section, re.MULTILINE):
            dependencies.append(int(match.group(1)))

    return list(set(dependencies))  # Remove duplicates


def fetch_issue_info(issue_number: int) -> IssueInfo:
    """Fetch complete issue information.

    Args:
        issue_number: GitHub issue number

    Returns:
        IssueInfo instance

    Raises:
        RuntimeError: If fetch fails

    """
    issue_data = _api.gh_issue_json(issue_number)

    return IssueInfo(
        number=issue_data["number"],
        title=issue_data["title"],
        body=issue_data.get("body", ""),
        state=IssueState(issue_data["state"]),
        labels=[label["name"] for label in issue_data.get("labels", [])],
        dependencies=_api.parse_issue_dependencies(issue_data.get("body", "")),
    )

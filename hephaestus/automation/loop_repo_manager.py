"""Read bounded repository and issue sources for the queue."""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Iterator
from pathlib import Path
from threading import Event
from typing import Any
from urllib.parse import urlparse

from hephaestus.automation.github_api import gh_call
from hephaestus.automation.operation_deadlines import operation_deadline_after
from hephaestus.config.child_environments import build_git_signing_env
from hephaestus.utils.helpers import METADATA_TIMEOUT, NETWORK_TIMEOUT

LOG = logging.getLogger(__name__)


def _detect_cwd_repo(*, metadata_timeout: int = METADATA_TIMEOUT) -> tuple[str | None, str | None]:
    """Return the owner and repository name for the current checkout.

    For a GitHub remote, read both names from the origin URL. For another
    remote, return the local directory name and no owner. Return two None
    values when the directory is not a Git checkout.
    """
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
            timeout=metadata_timeout,
            env=build_git_signing_env(),
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return (None, None)
    repo: str | None = Path(top).name or None

    org: str | None = None
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
            timeout=metadata_timeout,
            env=build_git_signing_env(),
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        url = ""

    host = ""
    path = ""
    parsed = urlparse(url)
    if parsed.scheme:
        host = (parsed.hostname or "").rstrip(".").lower()
        path = parsed.path.lstrip("/")
    elif "@" in url and ":" in url:
        # SCP-like git remote, e.g. git@github.com:org/repo.git
        after_at = url.split("@", 1)[1]
        host_part, path_part = after_at.split(":", 1)
        host = host_part.rstrip(".").lower()
        path = path_part.lstrip("/")

    if host == "github.com":
        parts = path.strip("/").split("/", 1)
        if len(parts) == 2:
            org = parts[0] or None
            remote_repo = parts[1].removesuffix(".git")
            repo = remote_repo or repo

    return (org, repo)


def _iter_gh_repos(
    org: str, *, shutdown: Event, network_timeout: float = NETWORK_TIMEOUT
) -> Iterator[str]:
    """Read organization repositories one REST page at a time.

    Exclude archived repositories and forks. Reject malformed flags before
    a repository reaches the queue. Read the next page only after the caller
    consumes the current page.

    Args:
        org: Organization name.
        shutdown: Stop pending reads when this event is set.
        network_timeout: Time limit for each page read, in seconds.

    Yields:
        Repository names from the current page.

    """
    page = 1
    while not shutdown.is_set():
        try:
            out = gh_call(
                [
                    "api",
                    (
                        f"/orgs/{org}/repos?per_page=100&type=all&sort=full_name"
                        f"&direction=asc&page={page}"
                    ),
                ],
                timeout=network_timeout,
                deadline_s=operation_deadline_after(network_timeout),
                shutdown=shutdown,
                max_retries=1,
                retry_on_rate_limit=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"gh repo list {org} timed out after {exc.timeout}s") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"gh repo list {org} failed (rc={exc.returncode}): {(exc.stderr or '').strip()}"
            ) from exc
        try:
            entries = json.loads(out.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"gh repo list returned invalid JSON: {exc}") from exc
        if not isinstance(entries, list):
            raise RuntimeError("gh repo list returned a JSON value other than an array")

        for entry in entries:
            if not isinstance(entry, dict):
                raise RuntimeError("gh repo list returned a malformed repository entry")
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                raise RuntimeError("gh repo list returned a repository entry without a name")
            archived = entry.get("archived")
            fork = entry.get("fork")
            if not isinstance(archived, bool) or not isinstance(fork, bool):
                raise RuntimeError("gh repo list returned a repository entry with malformed flags")
            if not archived and not fork:
                yield name

        if len(entries) < 100:
            return
        page += 1


def _iter_open_issue_meta(
    org: str, repo: str, *, shutdown: Event, network_timeout: float = NETWORK_TIMEOUT
) -> Iterator[dict[str, Any]]:
    """Read open issues one REST page at a time.

    Exclude PR rows from the issues endpoint. Retain at most one page until
    the caller requests more issues. Reject malformed rows and pages.

    Raises:
        RuntimeError: The API call fails or returns malformed data.

    """
    page = 1
    while not shutdown.is_set():
        try:
            out = gh_call(
                [
                    "api",
                    f"/repos/{org}/{repo}/issues?state=open&per_page=100"
                    f"&sort=created&direction=asc&page={page}",
                ],
                timeout=network_timeout,
                deadline_s=operation_deadline_after(network_timeout),
                shutdown=shutdown,
                max_retries=1,
                retry_on_rate_limit=False,
            )
            entries = json.loads(out.stdout or "[]")
            if not isinstance(entries, list):
                raise ValueError("expected an issue-list page")
        except (
            subprocess.SubprocessError,
            RuntimeError,
            OSError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise RuntimeError(f"failed to list open issues for {org}/{repo}: {exc}") from exc

        for entry in entries:
            if not isinstance(entry, dict):
                raise RuntimeError(
                    f"failed to list open issues for {org}/{repo}: malformed issue row"
                )
            # REST's /issues includes PRs.  They are not issue-source rows.
            if "pull_request" in entry:
                continue
            number = entry.get("number")
            title = entry.get("title", "")
            labels = entry.get("labels", [])
            if not isinstance(number, int) or number <= 0:
                raise RuntimeError(
                    f"failed to list open issues for {org}/{repo}: malformed issue number"
                )
            if not isinstance(title, str) or not isinstance(labels, list):
                raise RuntimeError(
                    f"failed to list open issues for {org}/{repo}: malformed issue metadata"
                )
            label_names: list[str] = []
            for label in labels:
                if not isinstance(label, dict) or not isinstance(label.get("name"), str):
                    raise RuntimeError(
                        f"failed to list open issues for {org}/{repo}: malformed issue label"
                    )
                label_names.append(label["name"])
            yield {"number": number, "labels": label_names, "title": title}

        # A short response is the terminal cursor.  An exact 100-row page is
        # followed by one inexpensive empty-page probe, avoiding a hard cap.
        if len(entries) < 100:
            return
        page += 1

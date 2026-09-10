"""Pull-request lifecycle helpers."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from concurrent.futures import CancelledError
from typing import Any, cast

import hephaestus.automation.github_api as _api

_ACCEPTABLE_SIG_STATUSES = frozenset({"G", "U"})
_PULL_REQUEST_STATES = frozenset({"OPEN", "CLOSED", "MERGED"})


class OpenPrDiscoveryIncompleteError(RuntimeError):
    """A head lookup that retained known PRs but could not prove completeness."""

    def __init__(self, branch: str, open_prs: list[tuple[int, str]], reason: str) -> None:
        """Record known PRs and the reason the lookup is incomplete."""
        super().__init__(f"could not verify existing PR state for head {branch!r}: {reason}")
        self.open_prs = open_prs


def _gh_commit_is_verified(
    oid: str,
    *,
    repository: tuple[str, str],
    run_gh: Callable[[list[str]], subprocess.CompletedProcess[str]],
) -> bool:
    """Read GitHub signature evidence for the explicitly selected repository.

    Local Git cannot verify some SSH signatures without an allowed-signers
    file. A verified GitHub signature can satisfy that local check. The caller
    supplies the bounded command runner. An ordinary lookup failure returns
    False; cancellation and timeout stop the operation.
    """
    owner, name = repository
    try:
        result = run_gh(
            [
                "api",
                f"repos/{owner}/{name}/commits/{oid}",
                "--jq",
                ".commit.verification.verified",
            ]
        )
        return (result.stdout or "").strip().lower() == "true"
    except (CancelledError, subprocess.TimeoutExpired):
        raise
    except Exception as exc:
        _api.logger.warning("Could not confirm GitHub signature for %s: %s", oid[:10], exc)
        return False


def _assert_branch_commits_signed(
    branch: str,
    *,
    base: str,
    run_git: Callable[[list[str]], subprocess.CompletedProcess[str]],
    verify_commit: Callable[[str], bool],
) -> None:
    """Reject commits that fail both local and GitHub signature checks.

    The caller supplies repository-bound callbacks with one operation budget.
    A failed base fetch can use an existing local ref. Try each supported
    local or remote ref range before deferring an unresolved range to GitHub.
    Cancellation and timeout stop the sequence.
    """
    try:
        run_git(["git", "fetch", "origin", base, "--quiet"])
    except (CancelledError, subprocess.TimeoutExpired):
        raise
    except Exception as exc:
        _api.logger.warning(
            "Could not refresh base %r before commit signature verification: %s",
            base,
            exc,
        )

    candidate_ranges = (
        f"origin/{base}..origin/{branch}",
        f"origin/{base}..{branch}",
        f"{base}..origin/{branch}",
        f"{base}..{branch}",
    )
    result = None
    for rev_range in candidate_ranges:
        attempt = run_git(["git", "log", "--format=%H %G?", rev_range])
        if attempt.returncode == 0:
            result = attempt
            break

    if result is None:
        raise ValueError(
            f"Could not resolve commits for branch {branch!r} against base {base!r}; "
            "commit signatures cannot be verified"
        )

    bad: list[tuple[str, str]] = []
    for line in (result.stdout or "").splitlines():
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        oid, status = parts[0], parts[1].strip()
        if status not in _ACCEPTABLE_SIG_STATUSES:
            if verify_commit(oid):
                continue
            bad.append((oid, status))

    if bad:
        bad_str = ", ".join(f"{oid[:10]}={status!r}" for oid, status in bad)
        raise ValueError(
            f"Unsigned or invalid commits on branch {branch!r} (vs {base}): {bad_str}. "
            "Every commit MUST be cryptographically signed per repo policy."
        )


def _find_open_prs_for_head(
    branch: str,
    run_gh: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> list[tuple[int, str]]:
    """Return every validated OPEN PR number and base branch for ``branch``.

    The queue uses this lookup before it creates or reuses a PR. A query
    or parse failure raises. An incomplete response must not become an
    empty result that permits duplicate PR creation.

    Args:
        branch: Head branch name to look up.
        run_gh: Optional repo-scoped ``gh`` runner. Defaults to the shared
            GitHub API runner.

    Returns:
        Every open PR as ``(number, base_ref_name)``.

    """
    try:
        runner = run_gh or _api._gh_call
        result = runner(
            [
                "pr",
                "list",
                "--head",
                branch,
                "--json",
                "number,state,baseRefName",
                "--limit",
                "1000",
            ]
        )
        stdout = result.stdout
        if not isinstance(stdout, str) or not stdout.strip():
            raise ValueError("existing PR lookup returned empty output")
        prs = json.loads(stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError, TypeError, ValueError) as e:
        raise RuntimeError(f"could not verify existing PR state for head {branch!r}") from e
    if not isinstance(prs, list):
        raise RuntimeError(f"could not verify existing PR state for head {branch!r}")
    open_prs: list[tuple[int, str]] = []
    incomplete_reason = "head lookup reached the 1000-PR cap" if len(prs) >= 1000 else ""
    for pr in prs:
        if not isinstance(pr, dict):
            incomplete_reason = incomplete_reason or "head lookup returned a malformed PR row"
            continue
        state = pr.get("state")
        base_ref_name = pr.get("baseRefName")
        if not isinstance(state, str) or not isinstance(base_ref_name, str):
            incomplete_reason = incomplete_reason or "head lookup returned an incomplete PR row"
            continue
        base_ref_name = base_ref_name.strip()
        if not base_ref_name:
            incomplete_reason = incomplete_reason or "head lookup returned a blank base branch"
            continue
        state = state.upper()
        if state not in _PULL_REQUEST_STATES:
            incomplete_reason = incomplete_reason or "head lookup returned an unknown PR state"
            continue
        if state == "OPEN":
            number = pr.get("number")
            if not isinstance(number, int) or number <= 0:
                incomplete_reason = incomplete_reason or "head lookup returned an invalid PR number"
                continue
            open_prs.append((number, base_ref_name))
    if incomplete_reason:
        raise OpenPrDiscoveryIncompleteError(branch, open_prs, incomplete_reason)
    return open_prs


def _select_open_pr_for_base(open_prs: list[tuple[int, str]], base: str) -> int | None:
    """Return the single open PR targeting ``base`` or fail on ambiguity."""
    matching_numbers = [number for number, base_ref_name in open_prs if base_ref_name == base]
    if len(matching_numbers) > 1:
        raise RuntimeError(f"could not verify existing PR state for base {base!r}")
    return matching_numbers[0] if matching_numbers else None


def gh_pr_label_names(pr_number: int) -> list[str]:
    """Return the label names on a PR by number, best-effort (read-only).

    Fetches ``gh pr view <n> --json labels`` and normalizes the ``labels``
    array (each entry is a ``{"name": ...}`` dict) to a flat list of names.
    Any subprocess or JSON failure yields an empty list so callers can treat a
    fetch error as "no labels" without raising — mirroring
    ``pr_manager._pr_label_names`` (the ``_review_existing_pr`` seam) so
    pipeline seeding's ``--prs`` mapping shares its semantics without
    importing the ``pr_manager`` product module.

    Args:
        pr_number: GitHub PR number.

    Returns:
        The PR's label names, or an empty list on any fetch failure.

    """
    try:
        result = _api._gh_call(["pr", "view", str(pr_number), "--json", "labels"], check=False)
        pr = cast(dict[str, Any], json.loads(result.stdout or "{}"))
    except (json.JSONDecodeError, OSError, subprocess.SubprocessError) as exc:
        _api.logger.warning("Could not fetch PR #%s labels: %s", pr_number, exc)
        return []
    labels = pr.get("labels")
    if not isinstance(labels, list):
        return []
    names: list[str] = []
    for label in labels:
        if isinstance(label, str):
            names.append(label)
        elif isinstance(label, dict):
            name = label.get("name")
            if isinstance(name, str):
                names.append(name)
    return names


def gh_pr_state(pr_number: int) -> dict[str, Any] | None:
    """Return a PR's lifecycle state by number, best-effort (read-only).

    Fetches ``gh pr view <n> --json state,mergedAt`` so callers can
    distinguish merged/closed/open without importing the stage-runtime
    ``PipelineGitHub.gh_pr_state``. Mirrors that method's read exactly so
    pipeline seeding's ``--prs`` mapping shares its terminal-state semantics
    (see ``_terminal_pr_outcome``, ``pipeline/stages/base.py``).

    Args:
        pr_number: GitHub PR number.

    Returns:
        ``{"state": ..., "mergedAt": ...}`` on success, ``None`` on any
        fetch failure.

    """
    try:
        result = _api._gh_call(
            ["pr", "view", str(pr_number), "--json", "state,mergedAt"], check=False
        )
        data = cast(dict[str, Any], json.loads(result.stdout or "{}"))
    except (json.JSONDecodeError, OSError, subprocess.SubprocessError) as exc:
        _api.logger.warning("Could not fetch PR #%s state: %s", pr_number, exc)
        return None
    return data if isinstance(data, dict) else None


def gh_current_login(*, timeout: int | None = None) -> str | None:
    """Return the authenticated GitHub login for the current ``gh`` token."""
    try:
        result = _api._gh_call(["api", "user", "--jq", ".login"], check=False, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        _api.logger.warning("Could not determine current GitHub login: %s", exc)
        return None
    if result.returncode != 0:
        _api.logger.warning("Could not determine current GitHub login: %s", result.stderr or "")
        return None
    login = (result.stdout or "").strip()
    return login or None

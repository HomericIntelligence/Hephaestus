"""Batch GitHub issue-state helpers."""

from __future__ import annotations

import json
import re
import subprocess

import hephaestus.automation.github_api as _api

from ..models import IssueState


def _fetch_batch_states(batch: list[int], owner: str, repo: str) -> dict[int, IssueState]:
    """Fetch issue states for a single batch via GraphQL with individual fallback.

    Args:
        batch: Issue numbers to fetch.
        owner: Repository owner.
        repo: Repository name.

    Returns:
        Mapping of issue number to IssueState for the batch.

    """
    # GraphQL cannot index a list variable to build per-element aliases, so we
    # declare one $nN:Int! per issue and bind each via -F nN=<int>. The f-string
    # interpolates only range(len(batch))-derived fragment indices (query structure),
    # never user data. This was smoke-tested against the live GitHub endpoint.
    var_decls = ",".join(f"$n{idx}:Int!" for idx in range(len(batch)))
    fragments = " ".join(
        f"issue{idx}: issue(number:$n{idx}){{ number state }}" for idx in range(len(batch))
    )
    query = (
        f"query($owner:String!,$name:String!,{var_decls})"
        f"{{repository(owner:$owner,name:$name){{{fragments}}}}}"
    )
    args = ["api", "graphql", "-f", f"query={query}", "-F", f"owner={owner}", "-F", f"name={repo}"]
    for idx, num in enumerate(batch):
        args.extend(["-F", f"n{idx}={int(num)}"])

    states: dict[int, IssueState] = {}
    try:
        result = _api._gh_call(args)
        data = json.loads(result.stdout)
        _api._check_graphql_errors(data, "prefetch_issue_states")
        repo_data = data.get("data", {}).get("repository", {})
        for key, issue_data in repo_data.items():
            if key.startswith("issue") and issue_data:
                states[issue_data["number"]] = IssueState(issue_data["state"])
        _api.logger.debug("Fetched states for %s issues", len(batch))
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as e:
        _api.logger.warning("Failed to batch fetch issue states: %s", e)
        for num in batch:
            try:
                issue_data = _api.gh_issue_json(num)
                states[num] = IssueState(issue_data["state"])
            except Exception as e2:
                _api.logger.warning("Failed to fetch state for issue #%s: %s", num, e2)
    return states


def prefetch_issue_states(
    issue_numbers: list[int], *, refresh: bool = False
) -> dict[int, IssueState]:
    """Batch fetch issue states using GraphQL, memoized in-process (#1587).

    Results are cached per process in :data:`_api._issue_state_cache`, so repeated
    calls within one process only query the numbers not already seen. The gh
    GraphQL round-trip is the most expensive of the loop's repeated lookups and
    previously had no caching at all (it ran once per phase-subprocess AND twice
    in the parent's closed-filter).

    Args:
        issue_numbers: List of issue numbers.
        refresh: When True, ignore the cache and re-query every number (and
            update the cache with fresh values). Use when a state may have
            changed mid-process and a stale read is unacceptable.

    Returns:
        Dictionary mapping issue number to state (only the requested numbers).

    """
    if not issue_numbers:
        return {}

    if not refresh:
        missing = [n for n in issue_numbers if n not in _api._issue_state_cache]
    else:
        missing = list(issue_numbers)
    if not missing:
        return {
            n: _api._issue_state_cache[n] for n in issue_numbers if n in _api._issue_state_cache
        }

    try:
        owner, repo = _api.get_repo_info()
    except RuntimeError as e:
        _api.logger.warning("Failed to get repo info: %s", e)
        return {
            n: _api._issue_state_cache[n] for n in issue_numbers if n in _api._issue_state_cache
        }

    # Sanitize owner and repo to prevent GraphQL injection
    # Owner and repo should be alphanumeric with hyphens/underscores
    if not re.match(r"^[a-zA-Z0-9_-]+$", owner) or not re.match(r"^[a-zA-Z0-9_-]+$", repo):
        _api.logger.error("Invalid owner/repo format: %s/%s", owner, repo)
        return {
            n: _api._issue_state_cache[n] for n in issue_numbers if n in _api._issue_state_cache
        }

    batch_size = 100
    for i in range(0, len(missing), batch_size):
        batch = missing[i : i + batch_size]
        _api._issue_state_cache.update(_api._fetch_batch_states(batch, owner, repo))

    # Return only the requested numbers (those that resolved); a number that
    # failed to fetch is simply absent, matching the prior contract.
    return {n: _api._issue_state_cache[n] for n in issue_numbers if n in _api._issue_state_cache}

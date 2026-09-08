"""Batch GitHub issue-state helpers."""

from __future__ import annotations

import re

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
    variables: dict[str, int | str] = {"owner": owner, "name": repo}
    for idx, num in enumerate(batch):
        variables[f"n{idx}"] = int(num)

    states: dict[int, IssueState] = {}
    try:
        raw_states = _api.run_graphql(
            _api.batch_issue_states_query(batch, owner, repo),
            variables,
        )
        states = {number: IssueState(state) for number, state in raw_states.items()}
        _api.logger.debug("Fetched states for %s issues", len(batch))
    except _api.GraphQLDeterministicError as e:
        _api.logger.warning("Deterministic batch state failure: %s", e)
    for num in batch:
        if num in states:
            continue
        try:
            issue_data = _api.gh_issue_json(num, repo=(owner, repo))
            states[num] = IssueState(issue_data["state"])
        except Exception as e2:
            _api.logger.warning("Failed to fetch state for issue #%s: %s", num, e2)
    return states


def prefetch_issue_states(
    issue_numbers: list[int], *, refresh: bool = False, repo: tuple[str, str] | None = None
) -> dict[int, IssueState]:
    """Batch fetch issue states using GraphQL, memoized in-process (#1587).

    Cache entries use the repository owner, repository name, and issue number.
    Calls query only numbers without a cache entry for that repository.
    The gh GraphQL round-trip is the most expensive of the loop's repeated lookups and
    previously had no caching at all (it ran once per phase-subprocess AND twice
    in the parent's closed-filter).

    Args:
        issue_numbers: List of issue numbers.
        refresh: When True, ignore the cache and re-query every number (and
            update the cache with fresh values). Use when a state may have
            changed mid-process and a stale read is unacceptable.
        repo: Repository owner and name. If you do not supply repo, use the current checkout.

    Returns:
        Dictionary mapping issue number to state (only the requested numbers).

    """
    if not issue_numbers:
        return {}

    try:
        owner, name = repo if repo is not None else _api.get_repo_info()
    except RuntimeError as e:
        _api.logger.warning("Failed to get repo info: %s", e)
        return {}

    # Sanitize owner and repo to prevent GraphQL injection
    # Owner and repo should be alphanumeric with hyphens/underscores
    if not re.match(r"^[a-zA-Z0-9_-]+$", owner) or not re.match(r"^[a-zA-Z0-9_-]+$", name):
        _api.logger.error("Invalid owner/repo format: %s/%s", owner, name)
        return {}

    missing = [
        number
        for number in issue_numbers
        if refresh or (owner, name, number) not in _api._issue_state_cache
    ]
    if refresh:
        for number in missing:
            _api._issue_state_cache.pop((owner, name, number), None)

    batch_size = 100
    for i in range(0, len(missing), batch_size):
        batch = missing[i : i + batch_size]
        states = _api._fetch_batch_states(batch, owner, name)
        _api._issue_state_cache.update(
            {(owner, name, number): state for number, state in states.items()}
        )

    # Return only the requested numbers (those that resolved); a number that
    # failed to fetch is simply absent, matching the prior contract.
    return {
        number: _api._issue_state_cache[(owner, name, number)]
        for number in issue_numbers
        if (owner, name, number) in _api._issue_state_cache
    }

"""GitHub label helpers."""

from __future__ import annotations

import json

import hephaestus.automation.github_api as _api

from ..state_labels import STATE_SKIP, is_skipped


def _label_cache_key(repo: tuple[str, str] | None = None) -> str:
    """Repo slug ("owner/name") keying the label cache, or "" if unresolved.

    ``get_repo_info`` is memoized per repo root (git_utils._repo_info_cache),
    so this is a cached dict lookup after the first call, not a git subprocess.

    Args:
        repo: Explicit ``(owner, name)`` owning the labels. When omitted the
            slug is resolved from the ambient working directory.

    """
    if repo is not None:
        owner, name = repo
        return f"{owner}/{name}"
    try:
        owner, name = _api.get_repo_info()
    except RuntimeError:
        return ""
    return f"{owner}/{name}"


def _with_repo(cmd: list[str], repo: tuple[str, str] | None) -> list[str]:
    """Append an explicit ``--repo owner/name`` selector when *repo* is given."""
    if repo is None:
        return cmd
    owner, name = repo
    return [*cmd, "--repo", f"{owner}/{name}"]


def gh_list_labels(
    refresh: bool = False,
    *,
    raise_on_error: bool = False,
    repo: tuple[str, str] | None = None,
) -> set[str]:
    """Return the set of label names that exist in the current repository.

    Cached per repo slug under a lock (ThreadSafeCache) so concurrent per-repo
    pipeline threads never observe another repo's label set (#1858). Returns a
    defensive copy; callers must not mutate the cache.

    Args:
        refresh: If True, bypass the in-process cache for the current repo and re-fetch.
        raise_on_error: If True, propagate label-list failures instead of
            returning an empty set.
        repo: ``(owner, name)`` of the repository to list. When omitted the
            repo is resolved from the ambient working directory, which is
            correct only for single-repo callers (#2245).

    Returns:
        Set of existing label names.

    """
    key = _label_cache_key(repo)

    def _fetch() -> set[str]:
        cmd = _with_repo(["label", "list", "--json", "name", "--limit", "200"], repo)
        result = _api._gh_call(cmd)
        data = json.loads(result.stdout)
        return {item["name"] for item in data}

    if refresh:
        _api._label_cache.remove(key)
    try:
        return set(_api._label_cache.get_or_compute(key, _fetch))
    except Exception as e:
        _api.logger.warning("Could not fetch label list: %s; proceeding without validation", e)
        if raise_on_error:
            raise RuntimeError("Could not fetch label list") from e
        return set()


def gh_create_label(
    name: str,
    color: str = "ededed",
    description: str = "",
    *,
    repo: tuple[str, str] | None = None,
) -> None:
    """Create a GitHub label, updating it if it already exists.

    Args:
        name: Label name
        color: Hex color without leading ``#`` (default: neutral grey)
        description: Optional short description
        repo: ``(owner, name)`` of the repository to create the label in.
            When omitted the repo is resolved from the ambient working
            directory (#2245).

    """
    cmd = ["label", "create", name, "--color", color, "--force"]
    if description:
        cmd.extend(["--description", description])
    _api._gh_call(_with_repo(cmd, repo))
    _api._label_cache.add_to_entry(_label_cache_key(repo), name)
    _api.logger.info("Created missing label '%s'", name)


def gh_issue_add_labels(
    issue_number: int, labels: list[str], repo: tuple[str, str] | None = None
) -> None:
    """Add labels to an existing issue, auto-creating any that don't exist yet.

    Idempotent: applying a label the issue already has is a no-op from
    GitHub's perspective. Missing repo-level labels are created on demand via
    :func:`gh_create_label`, which is what the state-label rollout relies on
    (a repo that hasn't run ``hephaestus-ensure-state-labels`` yet will still
    work — the first reviewer pass creates the labels).

    Args:
        issue_number: Issue to label, meaningful only within *repo*.
        labels: Label names to add. Empty list is a no-op.
        repo: ``(owner, name)`` of the repository owning *issue_number*. When
            omitted the repo is resolved from the ambient working directory,
            which is correct only for single-repo callers. The multi-repo
            loop MUST pass this explicitly: an issue number carries no repo,
            so ambient resolution wrote other repos' epic ``state:skip`` tags
            onto whatever repo the loop was launched from — silently, since
            a colliding issue number returns 200, not 404 (#2245).

    """
    if not labels:
        return
    existing = _api.gh_list_labels(repo=repo)
    for label in labels:
        if label not in existing:
            _api.gh_create_label(label, repo=repo)
    cmd = ["issue", "edit", str(issue_number)]
    for label in labels:
        cmd += ["--add-label", label]
    _api._gh_call(_with_repo(cmd, repo))
    _api.logger.info("Added labels %s to issue #%s", labels, issue_number)


def skip_epics(epics_labels: dict[int, list[str]], repo: tuple[str, str] | None = None) -> None:
    """Tag excluded epic/roadmap issues with ``state:skip``, idempotently.

    Called by the discovery chokepoints after :func:`~hephaestus.automation.
    state_labels.partition_epics` separates the epics out. Applies the
    ``state:skip`` override so dashboards and other tooling see the epic as
    intentionally bypassed and the loop never re-attempts it. An epic that
    already carries ``state:skip`` is left untouched — no redundant API write
    each loop.

    Args:
        epics_labels: Mapping of epic issue number → its current label names.
        repo: ``(owner, name)`` of the repository owning the epics. Required
            for multi-repo callers; when omitted the write resolves against
            the ambient working directory (#2245).

    """
    for number, labels in epics_labels.items():
        if is_skipped(labels):
            continue
        _api.gh_issue_add_labels(number, [STATE_SKIP], repo=repo)
        _api.logger.info("Issue #%s is an epic/roadmap tracking issue; tagged state:skip", number)


def gh_issue_remove_labels(issue_number: int, labels: list[str]) -> None:
    """Remove labels from an existing issue.

    Tolerant of labels the issue does not actually carry, and of mutually
    exclusive state labels that have not been created in the repository yet.
    Used to keep the ``state:*`` family mutually-exclusive (apply one, remove
    the other two).

    Args:
        issue_number: Issue to modify.
        labels: Label names to remove. Empty list is a no-op.

    """
    if not labels:
        return
    try:
        existing = _api.gh_list_labels(raise_on_error=True)
    except RuntimeError as exc:
        _api.logger.warning(
            "Could not validate repo labels before removing from issue #%s; "
            "attempting requested removals without filtering: %s",
            issue_number,
            exc,
        )
        labels_to_remove = list(labels)
    else:
        labels_to_remove = [label for label in labels if label in existing]
        missing = sorted(set(labels) - existing)
        if missing:
            _api.logger.debug(
                "Skipping removal of repo labels that do not exist for issue #%s: %s",
                issue_number,
                missing,
            )
    if not labels_to_remove:
        return
    cmd = ["issue", "edit", str(issue_number)]
    for label in labels_to_remove:
        cmd += ["--remove-label", label]
    _api._gh_call(cmd)
    _api.logger.info("Removed labels %s from issue #%s", labels_to_remove, issue_number)


def _ensure_labels_exist(labels: list[str]) -> None:
    """Create any labels in *labels* that do not yet exist in the repository."""
    existing = _api.gh_list_labels()
    for label in labels:
        if label not in existing:
            _api.gh_create_label(label)

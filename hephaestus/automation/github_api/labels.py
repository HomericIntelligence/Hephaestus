"""GitHub label helpers."""

from __future__ import annotations

import json

import hephaestus.automation.github_api as _api

from ..state_labels import STATE_SKIP, is_skipped


def gh_list_labels(refresh: bool = False, *, raise_on_error: bool = False) -> set[str]:
    """Return the set of label names that exist in the current repository.

    Args:
        refresh: If True, bypass the in-process cache and re-fetch.
        raise_on_error: If True, propagate label-list failures instead of
            returning an empty set.

    Returns:
        Set of existing label names.

    """
    if _api._label_cache is not None and not refresh:
        return _api._label_cache

    try:
        result = _api._gh_call(["label", "list", "--json", "name", "--limit", "200"])
        data = json.loads(result.stdout)
        _api._label_cache = {item["name"] for item in data}
        return _api._label_cache
    except Exception as e:
        _api.logger.warning("Could not fetch label list: %s; proceeding without validation", e)
        if raise_on_error:
            raise RuntimeError("Could not fetch label list") from e
        return set()


def gh_create_label(name: str, color: str = "ededed", description: str = "") -> None:
    """Create a GitHub label, updating it if it already exists.

    Args:
        name: Label name
        color: Hex color without leading ``#`` (default: neutral grey)
        description: Optional short description

    """
    cmd = ["label", "create", name, "--color", color, "--force"]
    if description:
        cmd.extend(["--description", description])
    _api._gh_call(cmd)
    if _api._label_cache is not None:
        _api._label_cache.add(name)
    _api.logger.info("Created missing label '%s'", name)


def gh_issue_add_labels(issue_number: int, labels: list[str]) -> None:
    """Add labels to an existing issue, auto-creating any that don't exist yet.

    Idempotent: applying a label the issue already has is a no-op from
    GitHub's perspective. Missing repo-level labels are created on demand via
    :func:`gh_create_label`, which is what the state-label rollout relies on
    (a repo that hasn't run ``hephaestus-ensure-state-labels`` yet will still
    work — the first reviewer pass creates the labels).

    Args:
        issue_number: Issue to label.
        labels: Label names to add. Empty list is a no-op.

    """
    if not labels:
        return
    existing = _api.gh_list_labels()
    for label in labels:
        if label not in existing:
            _api.gh_create_label(label)
    cmd = ["issue", "edit", str(issue_number)]
    for label in labels:
        cmd += ["--add-label", label]
    _api._gh_call(cmd)
    _api.logger.info("Added labels %s to issue #%s", labels, issue_number)


def skip_epics(epics_labels: dict[int, list[str]]) -> None:
    """Tag excluded epic/roadmap issues with ``state:skip``, idempotently.

    Called by the discovery chokepoints after :func:`~hephaestus.automation.
    state_labels.partition_epics` separates the epics out. Applies the
    ``state:skip`` override so dashboards and other tooling see the epic as
    intentionally bypassed and the loop never re-attempts it. An epic that
    already carries ``state:skip`` is left untouched — no redundant API write
    each loop.

    Args:
        epics_labels: Mapping of epic issue number → its current label names.

    """
    for number, labels in epics_labels.items():
        if is_skipped(labels):
            continue
        _api.gh_issue_add_labels(number, [STATE_SKIP])
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

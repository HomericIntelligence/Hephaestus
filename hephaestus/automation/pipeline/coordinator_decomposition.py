"""Coordinator-owned decomposition child admission (issue #2982).

The coordinator owns every child queue push. Admission re-reads the parent's
durable GitHub records (seal, bindings, transition) and proves each child
issue exists with the exact sealed title and body and carries its confirmed
planning labels before the coordinator queues it. Root nodes queue together;
a later layer queues only after all of its direct prerequisites finish
successfully, and a failed prerequisite blocks its descendants.
"""

from __future__ import annotations

import logging
from typing import Any

import hephaestus.automation.pipeline.coordinator_types as ct
import hephaestus.automation.pipeline.plan_journal as plan_journal_mod
from hephaestus.automation.dependency_parser import parse_issue_dependencies
from hephaestus.automation.pipeline.plan_journal import (
    recover_decomposition_bindings,
    recover_decomposition_seal,
    recover_decomposition_transition,
)
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from hephaestus.automation.plan_decomposition import (
    child_recovery_marker,
    normalize_work_tree,
    render_child_body,
    topological_layers,
)
from hephaestus.automation.state_labels import STATE_NEEDS_PLAN

from .coordinator_contract import _CoordinatorHost

logger = logging.getLogger(__name__)

#: The payload key the plan-review stage sets right before its terminal
#: outcome carries the complete child manifest.
_DECOMP_CHILDREN_PAYLOAD = "decomposition_children"


def _label_names(ctx: Any, issue_number: int) -> set[str]:
    """Return one fresh label-name set for an issue."""
    payload = ctx.github.gh_issue_json(issue_number)
    raw = payload.get("labels")
    return {
        str(entry.get("name") if isinstance(entry, dict) else entry)
        for entry in (raw if isinstance(raw, list) else [])
    }


class DecompositionCoordinator(_CoordinatorHost):
    """Own child verification, topological admission, and dependent release."""

    # Instance state lives on the concrete Coordinator (one set of maps per
    # run). The declarations keep the mixin type-safe.
    _decomposition_graph: dict[tuple[str, int], dict[int, dict[str, Any]]]
    _decomposition_done: dict[tuple[str, int], set[int]]
    _decomposition_queued: set[tuple[str, int]]

    def _admit_decomposition_children(self, item: WorkItem) -> None:
        """Verify every bound child, then queue its ready topological layer.

        Any verification gap finishes the parent failed and queues no child:
        the host refuses admission instead of guessing (#2982).
        """
        assert item.issue is not None  # noqa: S101 - stage work items carry an issue
        repo = item.repo
        parent = item.issue
        ctx = self._ctx_for_repo(repo)
        epoch = plan_journal_mod.requirements_epoch(str(item.payload.get("issue_body") or ""))
        comments = ctx.github.issue_comments(parent)
        seal = recover_decomposition_seal(comments, epoch)
        bindings = recover_decomposition_bindings(comments, epoch)
        transition = recover_decomposition_transition(comments, epoch)
        children_payload = item.payload.get(_DECOMP_CHILDREN_PAYLOAD)
        if (
            seal is None
            or bindings is None
            or bindings.seal_digest != seal.seal_digest
            or transition is None
            or not isinstance(children_payload, list)
        ):
            self._finish(
                item,
                passed=False,
                reason="decomposition records are incomplete; child admission refused",
            )
            return
        units = normalize_work_tree(seal.nodes)
        layers = topological_layers(units) if units is not None else None
        if units is None or layers is None:
            self._finish(item, passed=False, reason="sealed tree is unreadable")
            return
        layer_of = {node_id: layer for layer, nodes in enumerate(layers) for node_id in nodes}
        intent = plan_journal_mod.recover_decomposition_intent(comments, epoch)
        source_revision = intent.source_plan_revision if intent is not None else 1
        bound_issues = dict(bindings.bindings)
        parent_labels = _label_names(ctx, parent)
        inherited_severity = next(
            (name for name in parent_labels if name.startswith("severity:")), None
        )
        graph = self._decomposition_graph.setdefault((repo, parent), {})
        for entry in children_payload:
            node_id = str(entry.get("node_id") or "")
            issue_number = entry.get("issue")
            unit = next((unit for unit in units if unit.node_id == node_id), None)
            if unit is None or not isinstance(issue_number, int) or issue_number <= 0:
                self._finish(
                    item,
                    passed=False,
                    reason=f"child {node_id or '?'} is not bound in the seal",
                )
                return
            expected_body = render_child_body(
                parent_issue=parent,
                unit=unit,
                prerequisite_issues={
                    prerequisite: issue
                    for prerequisite in unit.prerequisites
                    if (issue := bound_issues.get(prerequisite)) is not None
                },
                plan_revision=source_revision,
                requirements_epoch=epoch,
                marker=child_recovery_marker(
                    repository=f"{self.config.org}/{repo}".casefold(),
                    parent_issue=parent,
                    requirements_epoch=epoch,
                    generation=1,
                    node_id=node_id,
                ),
            )
            try:
                payload = ctx.github.gh_issue_json(issue_number)
                labels = _label_names(ctx, issue_number)
            except Exception as error:
                self._finish(item, passed=False, reason=f"child read failed: {error}")
                return
            required = (
                {STATE_NEEDS_PLAN, inherited_severity}
                if inherited_severity is not None
                else {STATE_NEEDS_PLAN}
            )
            if (
                str(payload.get("title") or "") != str(unit.title)
                or str(payload.get("body") or "") != expected_body
            ):
                self._finish(
                    item,
                    passed=False,
                    reason=f"child #{issue_number} does not match the sealed specification",
                )
                return
            if not required.issubset(labels):
                self._finish(
                    item,
                    passed=False,
                    reason=f"child #{issue_number} planning labels are not confirmed",
                )
                return
            if issue_number not in graph:
                graph[issue_number] = {
                    "node_id": node_id,
                    "layer": layer_of.get(node_id, 0),
                    "deps": {
                        bound_issues[prerequisite]
                        for prerequisite in unit.prerequisites
                        if prerequisite in bound_issues
                    },
                }
        self._queue_ready_children(repo, parent)

    def _queue_ready_children(self, repo: str, parent: int) -> None:
        """Queue each ready child exactly once and record its generated identity."""
        graph = self._decomposition_graph.get((repo, parent))
        if graph is None:
            return
        done = self._decomposition_done.setdefault((repo, parent), set())
        ctx = self._ctx_for_repo(repo)
        for issue_number in sorted(graph):
            facts = graph[issue_number]
            if issue_number in done or (repo, issue_number) in self._decomposition_queued:
                continue
            if facts["layer"] > 0 and not facts["deps"].issubset(done):
                logger.info(
                    "decomposition %s#%d: child #%d waits for prerequisites",
                    repo,
                    parent,
                    issue_number,
                )
                continue
            child = self._child_work_item(repo, parent, issue_number, facts)
            if child is None:
                continue
            if self._push_item(child, StageName.PLANNING, enter=True, defer_if_full=True):
                self._decomposition_queued.add((repo, issue_number))
                # The shared coordinator-owned set survives in every stage
                # context of this run and guards the recursion (#2982).
                ctx.generated_issue_ids.add(issue_number)
                logger.info(
                    "decomposition %s#%d: admitted child #%d (node %s, layer %d)",
                    repo,
                    parent,
                    issue_number,
                    facts["node_id"],
                    facts["layer"],
                )

    def _child_work_item(
        self, repo: str, parent: int, issue_number: int, facts: dict[str, Any]
    ) -> WorkItem | None:
        """Materialize one queue-ready child work item from live GitHub facts."""
        ctx = self._ctx_for_repo(repo)
        try:
            payload = ctx.github.gh_issue_json(issue_number)
        except Exception as error:
            logger.warning("decomposition child #%d read failed: %s", issue_number, error)
            return None
        child = WorkItem(
            repo=repo, kind=ItemKind.ISSUE, issue=issue_number, stage=StageName.PLANNING
        )
        child.payload["issue_title"] = str(payload.get("title") or "")
        child.payload["issue_body"] = str(payload.get("body") or "")
        child.payload["entry_reason"] = f"decomposition child of epic #{parent}"
        child.payload["decomposition_parent"] = parent
        child.payload["decomposition_node_id"] = facts["node_id"]
        child.payload["dependencies"] = parse_issue_dependencies(str(payload.get("body") or ""))
        child.state = "ENTER"
        return child

    def _release_decomposition_dependents(self, item: WorkItem, *, passed: bool) -> None:
        """Release or block descendants after one child finished."""
        issue_number = item.issue
        if issue_number is None:
            return
        for (repo, parent), graph in list(self._decomposition_graph.items()):
            if issue_number not in graph:
                continue
            if passed:
                self._decomposition_done.setdefault((repo, parent), set()).add(issue_number)
                graph.pop(issue_number, None)
                logger.info(
                    "decomposition %s#%d: child #%d finished; releasing dependents",
                    repo,
                    parent,
                    issue_number,
                )
            else:
                self._block_decomposition_descendants(repo, parent, {issue_number})
            self._queue_ready_children(repo, parent)

    def _block_decomposition_descendants(self, repo: str, parent: int, failed: set[int]) -> None:
        """Finish every not-yet-queued descendant of a failed child failed."""
        graph = self._decomposition_graph.get((repo, parent))
        if graph is None:
            return
        blocked: set[int] = set()
        pending = set(failed)
        while pending:
            pending.pop()
            for issue_number, facts in graph.items():
                if issue_number in blocked or (repo, issue_number) in self._decomposition_queued:
                    continue
                if not facts["deps"].intersection(failed | blocked):
                    continue
                blocked.add(issue_number)
                pending.add(issue_number)
        for issue_number in sorted(blocked):
            if graph.pop(issue_number, None) is None:
                continue
            self._decomposition_queued.add((repo, issue_number))
            finished = WorkItem(
                repo=repo,
                kind=ItemKind.ISSUE,
                issue=issue_number,
                stage=StageName.FINISHED,
            )
            finished.result = ct.ItemResult(
                passed=False,
                reason="decomposition prerequisite failed",
                final_stage=StageName.FINISHED,
            )
            finished.payload["_recorded"] = True
            self.ledger.append(finished.result)
            self._record_terminal_result(finished)
            logger.warning(
                "decomposition %s#%d: blocked child #%d after a failed prerequisite",
                repo,
                parent,
                issue_number,
            )

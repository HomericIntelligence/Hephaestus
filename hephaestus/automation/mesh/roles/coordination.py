"""Coordination role: acknowledge non-leaf HMAS nodes so delegation flows.

L0–L2 nodes of an ingested brief are coordination points, not implementation
work — the dependency graph and wake-up bursts live in Agamemnon (ADR-013
§10: parent nodes are parked in Agamemnon, never held by a worker). A
coordination myrmidon claims its layer's node, publishes the started fact
(assignment record), and completes it immediately so
``delegate_unblocked_children`` can delegate the layer below.

No LLM work happens here; these workers are lightweight.
"""

from __future__ import annotations

import logging

from hephaestus.automation.mesh.worker import RoleResult, TaskContext

logger = logging.getLogger(__name__)


class CoordinationHandler:
    """Acknowledges a coordination node and hands control back to Agamemnon."""

    def handle(self, ctx: TaskContext) -> RoleResult:
        """Complete the node; Agamemnon's graph walk delegates the children."""
        subject = str(ctx.payload.get("subject", ""))
        layer = str(ctx.payload.get("layer", ctx.config.role))
        logger.info(
            "coordination node acknowledged: task=%s layer=%s subject=%s",
            ctx.task_id,
            layer,
            subject,
        )
        return RoleResult(
            ok=True,
            summary=f"coordination node acknowledged ({layer}: {subject or ctx.task_id})",
        )

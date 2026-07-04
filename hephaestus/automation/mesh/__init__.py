"""HMAS mesh worker library (Odysseus ADR-013).

Implements the wire contracts for the HomericIntelligence mesh pipeline:
role-addressed dispatch consumption, task state events, the worker claim
loop with leases/heartbeats and the overrun checkpoint/split handler, the
interview relay, epic task-list conventions, and a small Agamemnon REST
client.

Requires the ``mesh`` optional dependency group
(``pip install HomericIntelligence-Hephaestus[mesh]``).
"""

from hephaestus.automation.mesh.config import MeshConfig, envelope
from hephaestus.automation.mesh.worker import MeshWorker, TaskContext

__all__ = ["MeshConfig", "MeshWorker", "TaskContext", "envelope"]

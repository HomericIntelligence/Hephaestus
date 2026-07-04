"""HMAS mesh worker library (Odysseus ADR-013).

Implements the wire contracts for the HomericIntelligence mesh pipeline:
role-addressed dispatch consumption, task state events, the worker claim
loop with leases/heartbeats and the overrun checkpoint/split handler.
"""

from hephaestus.automation.mesh.config import MeshConfig, envelope
from hephaestus.automation.mesh.worker import MeshWorker, TaskContext

__all__ = ["MeshConfig", "MeshWorker", "TaskContext", "envelope"]

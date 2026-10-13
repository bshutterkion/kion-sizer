"""What a deployment target must provide.

A target answers three questions for a given Recommendation: which database
tiers are candidates, what provisionable compute shape holds the poller's
computed heap requirement, and what those cost. All AWS/Azure/GCP I/O lives
behind this protocol so model.py stays a pure function of its inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..config import InstanceTier


class TargetError(Exception):
    pass


@dataclass
class ComputeShape:
    """A provisionable size that holds a computed (mem_gib, vcpu) requirement.

    `kind` selects which optional fields are populated: "fargate_task" fills
    cpu_units; "k8s_pod" fills the cpu_millicores and node_* fields. The
    requirement itself is not stored here — it stays on the Recommendation, so
    render can show requirement and provisioned size side by side.
    """

    kind: str
    vcpu: float
    mem_gib: float
    exceeds: bool = False
    # fargate_task
    cpu_units: int | None = None
    # k8s_pod
    cpu_millicores: int | None = None
    node_machine_type: str | None = None
    node_mem_gib: float | None = None
    node_allocatable_gib: float | None = None
    node_count: int | None = None
    values_snippet: str | None = None


class TargetAdapter(Protocol):
    name: str

    def db_tiers(
        self, cfg, *, live: bool, region: str, engine: str
    ) -> tuple[list[InstanceTier], str]:
        """Candidate DB tiers ascending by RAM, plus a provenance label.

        `live=False` forces the built-in ladder from config. A live lookup that
        fails must fall back to that ladder and say so in the label rather than
        raising.
        """
        ...

    def snap_compute(self, mem_gib: float, vcpu: int) -> ComputeShape:
        """Snap a computed requirement to the smallest shape that holds it."""
        ...

    def price_table(self, region: str, db_class: str, engine: str):
        """A price table exposing the interface model.cost() consumes."""
        ...

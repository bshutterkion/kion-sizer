"""The AWS deployment target: RDS tier ladder, Fargate task sizing, pricing.

Build the RDS tier ladder from what AWS actually offers in a region.

Enumerating `describe-orderable-db-instance-options` for an engine returns
thousands of rows (class × version × AZ) — slow enough to hang. Instead we probe
a small curated candidate ladder: for each RAM tier, a preference-ordered list of
DB classes (newest generation first). AWS is the source of truth for two things —
whether a class is *orderable* (a targeted, tiny `describe-orderable` query per
class) and its *RAM* (`ec2 describe-instance-types`, since the RDS API omits it).

The EC2 call runs first as a reachability probe; any AWS error propagates so the
CLI falls back to the built-in tiers rather than looping on timeouts. All I/O is
isolated here so model.py stays pure. Tested with injected fake clients.
"""

from __future__ import annotations

import math

from ..config import InstanceTier
from .base import ComputeShape


class RDSCatalogError(Exception):
    pass


# Candidate DB classes per RAM tier, ascending, newest generation first within a
# tier. AWS confirms which are actually orderable + their real RAM; the first
# orderable class in each row wins that tier.
_CANDIDATES: list[tuple[str, ...]] = [
    ("db.t4g.medium", "db.t3.medium"),  # ~4 GiB
    ("db.t4g.large", "db.t3.large"),  # ~8 GiB
    ("db.r8g.large", "db.r7g.large", "db.r6g.large"),  # 16 GiB
    ("db.r8g.xlarge", "db.r7g.xlarge", "db.r6g.xlarge"),  # 32 GiB
    ("db.r8g.2xlarge", "db.r7g.2xlarge", "db.r6g.2xlarge"),  # 64 GiB
    ("db.r8g.4xlarge", "db.r7g.4xlarge", "db.r6g.4xlarge"),  # 128 GiB
    ("db.r8g.8xlarge", "db.r7g.8xlarge", "db.r6g.8xlarge"),  # 256 GiB
]


def db_to_ec2(db_class: str) -> str | None:
    """`db.r6g.large` -> `r6g.large`; None for non-standard classes (db.serverless)."""
    if not db_class.startswith("db."):
        return None
    rest = db_class[3:]
    return rest if "." in rest else None


def orderable_tiers(
    region: str | None,
    engine: str = "mysql",
    rds_client=None,
    ec2_client=None,
) -> list[InstanceTier]:
    """Return the orderable candidate DB classes for `engine` in `region` as
    InstanceTiers (name, ram_gib) ascending by RAM. Raises RDSCatalogError (or
    lets boto3 errors propagate) so the CLI can fall back to the built-in tiers.
    """
    if rds_client is None or ec2_client is None:
        import boto3
        from botocore.config import Config

        # Hard timeouts + bounded retries so a stalled AWS call can't hang the tool.
        cfg = Config(
            connect_timeout=5,
            read_timeout=10,
            retries={"max_attempts": 2, "mode": "standard"},
        )
        rds_client = rds_client or boto3.client("rds", region_name=region, config=cfg)
        ec2_client = ec2_client or boto3.client("ec2", region_name=region, config=cfg)

    all_classes = [c for row in _CANDIDATES for c in row]
    ec2_types = [e for e in (db_to_ec2(c) for c in all_classes) if e]
    # RAM first — also the "is AWS reachable / fast" probe. Errors -> fallback.
    ram = _instance_ram_gib(ec2_client, ec2_types)  # ec2_type -> ram_gib

    tiers: list[InstanceTier] = []
    seen_ram: set[float] = set()
    for row in _CANDIDATES:
        for db_class in row:
            e = db_to_ec2(db_class)
            gib = ram.get(e) if e else None
            if gib is None:
                continue
            if _is_orderable(rds_client, engine, db_class):
                if gib not in seen_ram:
                    tiers.append(InstanceTier(name=db_class, ram_gib=gib))
                    seen_ram.add(gib)
                break  # first orderable class in this tier wins
    if not tiers:
        raise RDSCatalogError(
            f"no candidate db classes orderable for engine {engine!r} "
            f"in region {region!r}"
        )
    tiers.sort(key=lambda t: t.ram_gib)
    return tiers


def _is_orderable(rds_client, engine: str, db_class: str) -> bool:
    """True if `db_class` is orderable for `engine`. A targeted query (one class)
    returns a handful of rows, not the full catalog. Errors propagate — the CLI
    falls back rather than looping through timeouts.
    """
    resp = rds_client.describe_orderable_db_instance_options(
        Engine=engine, DBInstanceClass=db_class, MaxRecords=20
    )
    return len(resp.get("OrderableDBInstanceOptions", [])) > 0


def _instance_ram_gib(ec2_client, ec2_types) -> dict[str, float]:
    """RAM (GiB) for just the EC2 types we need. Uses an instance-type *filter*
    (not an explicit InstanceTypes list) so a type that doesn't exist in the
    region is silently absent rather than raising InvalidInstanceType.
    """
    types = sorted(set(ec2_types))
    specs: dict[str, float] = {}
    paginator = ec2_client.get_paginator("describe_instance_types")
    for i in range(0, len(types), 190):  # Values filter caps ~200 per call
        batch = types[i : i + 190]
        pages = paginator.paginate(Filters=[{"Name": "instance-type", "Values": batch}])
        for page in pages:
            for it in page.get("InstanceTypes", []):
                name = it.get("InstanceType")
                mib = it.get("MemoryInfo", {}).get("SizeInMiB")
                if name and mib:
                    specs[name] = mib / 1024
    return specs


def _mib_range(lo: int, hi: int, step: int) -> list[int]:
    return list(range(lo, hi + 1, step))


# AWS Fargate valid task sizes (fixed platform constants — AWS exposes no API
# for these, and they don't vary by region): cpu units -> the discrete list of
# valid memory values (MiB). The poller's 4 GiB floor keeps us at 1 vCPU (1024)
# or above, so the sub-1-vCPU tiers are omitted.
_FARGATE_TASKS: list[tuple[int, list[int]]] = [
    (1024, _mib_range(2048, 8192, 1024)),  # 1 vCPU:  2-8 GB /1
    (2048, _mib_range(4096, 16384, 1024)),  # 2 vCPU:  4-16 GB /1
    (4096, _mib_range(8192, 30720, 1024)),  # 4 vCPU:  8-30 GB /1
    (8192, _mib_range(16384, 61440, 4096)),  # 8 vCPU:  16-60 GB /4
    (16384, _mib_range(32768, 122880, 8192)),  # 16 vCPU: 32-120 GB /8
    (32768, [61440, 122880, 249856]),  # 32 vCPU: 60, 120, 244 GB (irregular)
]


def fargate_task(mem_gib: float, vcpu: int) -> tuple[int, int, bool]:
    """Snap a computed (mem_gib, vcpu) requirement to the smallest valid Fargate
    task that provides at least that CPU and memory. Returns
    (cpu_units, mem_mib, exceeds_largest_task).
    """
    req_cpu = vcpu * 1024
    req_mem = math.ceil(mem_gib * 1024)
    for cpu, mems in _FARGATE_TASKS:
        if cpu < req_cpu:
            continue
        for mem in mems:  # ascending
            if mem >= req_mem:
                return cpu, mem, False
        # memory doesn't fit this tier's max; escalate to the next (larger) CPU.
    cpu, mems = _FARGATE_TASKS[-1]
    return cpu, mems[-1], True


class AWSTarget:
    """RDS instance classes plus ECS Fargate tasks."""

    name = "aws"

    def db_tiers(self, cfg, *, live: bool, region: str, engine: str):
        builtin = cfg.target("aws").db_tiers
        if not live:
            return builtin, ""
        try:
            return orderable_tiers(region, engine), f"orderable in {region} ({engine})"
        except Exception as e:  # noqa: BLE001 — resilience: fall back on any AWS failure
            return builtin, f"built-in defaults (AWS lookup failed: {e})"

    def snap_compute(self, mem_gib: float, vcpu: int) -> ComputeShape:
        cpu_units, mem_mib, exceeds = fargate_task(mem_gib, vcpu)
        return ComputeShape(
            kind="fargate_task",
            vcpu=cpu_units // 1024,
            mem_gib=mem_mib / 1024,
            cpu_units=cpu_units,
            exceeds=exceeds,
        )

    def price_table(self, region: str, db_class: str, engine: str):
        from .. import pricing

        return pricing.build_price_table(region, db_class, engine)

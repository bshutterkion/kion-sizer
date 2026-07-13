"""Discover the DB instance classes actually orderable in a region, with RAM.

`rds describe-orderable-db-instance-options` returns class *names* but no
memory; `ec2 describe-instance-types` supplies RAM/vCPU (a `db.<type>` class maps
to the EC2 `<type>`). This module isolates that I/O so model.py stays a pure
function — the CLI fetches tiers here and overrides Config.rds_tiers.

Tested with injected fake rds/ec2 clients (see tests/test_rds_catalog.py); no
live AWS in tests.
"""

from __future__ import annotations

from .config import InstanceTier


class RDSCatalogError(Exception):
    pass


# Families kion-sizer sizes on: burstable (small/cheap end) + memory-optimized
# (the buffer pool is RAM-bound, so these fit a DB best). Value = preference when
# several orderable classes share a RAM size (higher wins → newer/mem-optimized).
_FAMILY_PREF = {
    # memory-optimized
    "r8g": 96,
    "r7g": 95,
    "r7i": 94,
    "r6g": 93,
    "r6i": 92,
    "r5": 91,
    "r5b": 90,
    # burstable (only place the 2–8 GiB low end comes from)
    "t4g": 60,
    "t3": 59,
    "t3a": 58,
}


def _family(ec2_type: str) -> str:
    return ec2_type.split(".", 1)[0]


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
    """Return the orderable DB instance classes for `engine` in `region`, as
    InstanceTiers (name, ram_gib) ascending by RAM. Raises RDSCatalogError if
    nothing sizeable resolves; boto3/ClientError propagate to the caller.
    """
    if rds_client is None or ec2_client is None:
        import boto3

        rds_client = rds_client or boto3.client("rds", region_name=region)
        ec2_client = ec2_client or boto3.client("ec2", region_name=region)

    classes = _orderable_classes(rds_client, engine)

    # Keep only classes in a family we size on and that map to an EC2 type.
    wanted: dict[str, str] = {}  # db_class -> ec2_type
    for c in classes:
        e = db_to_ec2(c)
        if e and _family(e) in _FAMILY_PREF:
            wanted[c] = e
    if not wanted:
        raise RDSCatalogError(
            f"no burstable/memory-optimized db classes orderable for "
            f"engine {engine!r} in region {region!r}"
        )

    specs = _instance_ram_gib(ec2_client)  # ec2_type -> ram_gib

    # Dedupe by RAM, keeping the most-preferred family at each size.
    best: dict[float, tuple[int, str]] = {}  # ram_gib -> (pref, db_class)
    for db_class, e in wanted.items():
        ram = specs.get(e)
        if ram is None:
            continue
        pref = _FAMILY_PREF[_family(e)]
        cur = best.get(ram)
        if cur is None or pref > cur[0]:
            best[ram] = (pref, db_class)
    if not best:
        raise RDSCatalogError("resolved no RAM specs for orderable db classes")

    return [InstanceTier(name=best[r][1], ram_gib=r) for r in sorted(best)]


def _orderable_classes(rds_client, engine: str) -> set[str]:
    out: set[str] = set()
    paginator = rds_client.get_paginator("describe_orderable_db_instance_options")
    for page in paginator.paginate(Engine=engine):
        for o in page.get("OrderableDBInstanceOptions", []):
            c = o.get("DBInstanceClass")
            if c:
                out.add(c)
    return out


def _instance_ram_gib(ec2_client) -> dict[str, float]:
    """All EC2 instance types -> RAM in GiB. Fetching the full catalog (rather
    than a specific list) avoids InvalidInstanceType when a db class maps to a
    type that doesn't exist as a standalone EC2 offering.
    """
    specs: dict[str, float] = {}
    paginator = ec2_client.get_paginator("describe_instance_types")
    for page in paginator.paginate():
        for it in page.get("InstanceTypes", []):
            name = it.get("InstanceType")
            mib = it.get("MemoryInfo", {}).get("SizeInMiB")
            if name and mib:
                specs[name] = mib / 1024
    return specs

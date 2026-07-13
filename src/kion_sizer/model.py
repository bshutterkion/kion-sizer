"""Maps a CURProfile + calibration Config to a sizing Recommendation.

Pure function of (profile, config, accounts); no I/O. Mirrors internal/model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .config import Config, InstanceTier, ServiceBand

_BYTES_PER_GIB = 1 << 30


@dataclass
class Recommendation:
    profile: object
    est_rows: int = 0
    est_basis: str = ""
    raw_line_items: int = 0
    shard_gib: float = 0.0
    rds: Optional[InstanceTier] = None
    rds_exceeds_tiers: bool = False
    rds_tier_source: str = ""
    poller_mem_gib: float = 0.0
    poller_vcpu: int = 0
    # Nearest valid AWS Fargate task the computed heap maps onto (the raw
    # poller_mem_gib above is a requirement, not a provisionable size).
    poller_task_mem_gib: float = 0.0
    poller_task_vcpu: int = 0
    poller_task_cpu_units: int = 0
    poller_task_exceeds: bool = False
    accounts: int = 0
    services: Optional[ServiceBand] = None
    have_services: bool = False
    calibration_version: str = ""


def round_half_away(x: float) -> float:
    """Round half away from zero, matching Go's math.Round."""
    if x >= 0:
        return math.floor(x + 0.5)
    return math.ceil(x - 0.5)


def recommend(p, c: Config, accounts: int) -> Recommendation:
    rec = Recommendation(
        profile=p, accounts=accounts, calibration_version=c.calibration_version
    )

    cur_gib = p.compressed_bytes / _BYTES_PER_GIB
    if p.have_raw_rows and p.raw_line_items > 0:
        rec.raw_line_items = p.raw_line_items
        rec.est_rows = int(p.raw_line_items / c.raw_to_aggregated_ratio)
        rec.est_basis = "line items / aggregation ratio"
    else:
        rec.est_rows = int(c.rows_per_gib * cur_gib)
        if p.has_csv:
            rec.est_basis = (
                "compressed bytes (WARN: rows_per_gib is parquet-calibrated; "
                "CSV estimate is rough — sampling did not run, e.g. parquet+CSV mixed)"
            )
        else:
            rec.est_basis = "compressed bytes"

    rec.shard_gib = rec.est_rows * c.bytes_per_row / _BYTES_PER_GIB
    headroom = max(
        c.buffer_pool_headroom_floor, c.buffer_pool_headroom_frac * rec.shard_gib
    )
    required_ram = (rec.shard_gib + headroom) / c.buffer_pool_fraction
    rec.rds, rec.rds_exceeds_tiers = pick_tier(c.rds_tiers, required_ram)

    mrows = rec.est_rows / 1_000_000
    poller_mem = (c.poller_base_gib + c.poller_heap_gib_per_mrow * mrows) * (
        1 + c.poller_headroom_frac
    )
    poller_mem = max(c.poller_floor_gib, poller_mem)
    rec.poller_mem_gib = round_half_away(poller_mem * 10) / 10
    rec.poller_vcpu = poller_vcpu(rec.poller_mem_gib)

    cpu_units, mem_mib, exceeds = fargate_task(rec.poller_mem_gib, rec.poller_vcpu)
    rec.poller_task_cpu_units = cpu_units
    rec.poller_task_mem_gib = mem_mib / 1024
    rec.poller_task_vcpu = cpu_units // 1024
    rec.poller_task_exceeds = exceeds

    if accounts > 0:
        rec.services = pick_band(c.service_bands, accounts)
        rec.have_services = True
    return rec


def pick_tier(tiers: list[InstanceTier], need: float) -> tuple[InstanceTier, bool]:
    for t in tiers:
        if t.ram_gib >= need:
            return t, False
    return tiers[-1], True


def pick_band(bands: list[ServiceBand], accounts: int) -> ServiceBand:
    for b in bands:
        if accounts <= b.max_accounts:
            return b
    # Go's pickBand returns a zero-value ServiceBand for an empty list (render
    # then shows zeros), not nil — match it so behavior stays byte-for-byte.
    return bands[-1] if bands else ServiceBand(0, 0, 0, 0, 0, 0, 0)


def poller_vcpu(mem_gib: float) -> int:
    if mem_gib <= 8:
        return 1
    if mem_gib <= 16:
        return 2
    if mem_gib <= 32:
        return 4
    return 8


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

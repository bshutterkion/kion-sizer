"""Maps a CURProfile + calibration Config to a sizing Recommendation.

Pure function of (profile, config, accounts); no I/O. Mirrors internal/model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
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
    # Populated by cost() when --cost is set; None otherwise (render stays quiet).
    cost: Optional["CostReport"] = None


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


# --- cost estimation --------------------------------------------------------
# Pure functions of (Recommendation, price table). The price table is any object
# exposing ec2_hr(name), rds_hr(class), .fargate, .hours_per_month, .ec2_specs,
# .region_label, .source (see pricing.PriceTable) — model stays I/O-free.

# When the cheapest fitting EC2 instance's memory overshoots the requirement by
# more than this, we also surface the "if less memory is enough" alternatives —
# the requirement landed in a dead zone between instance-memory tiers.
_DEADZONE_OVERSHOOT = 1.1


@dataclass
class EC2Option:
    name: str
    vcpu: int
    mem_gib: float
    arch: str
    usd_mo: Optional[float] = None
    cheapest: bool = False


@dataclass
class CostReport:
    region_label: str = ""
    source: str = ""
    hours_per_month: int = 730
    rds_name: str = ""
    rds_usd_mo: Optional[float] = None
    poller_task_vcpu: int = 0
    poller_task_mem_gib: float = 0.0
    poller_fargate_x86_usd_mo: float = 0.0
    poller_fargate_arm_usd_mo: float = 0.0
    poller_fargate_x86_hr: float = 0.0
    poller_fargate_arm_hr: float = 0.0
    ec2_req_vcpu: int = 0
    ec2_req_mem_gib: float = 0.0
    ec2_primary: list[EC2Option] = field(default_factory=list)
    ec2_under: list[EC2Option] = field(default_factory=list)
    ec2_exceeds: bool = False
    show_under: bool = False
    services_usd_mo: Optional[float] = None
    total_usd_mo: float = 0.0
    total_note: str = ""


def _family_token(name: str) -> str:
    """`r8g.2xlarge` -> `r8g` (the instance family, distinguishing x2gd vs x8g)."""
    return name.split(".", 1)[0]


def _family_class(name: str) -> str:
    """`r8g.2xlarge` -> `r` (general m / memory-optimized r / high-memory x)."""
    return name[0]


def _generation(name: str) -> int:
    """`r8g.2xlarge` -> 8; `x2gd.4xlarge` -> 2. The generation digit run."""
    tok = _family_token(name)
    digits = ""
    for ch in tok:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    return int(digits) if digits else 0


def select_ec2_alternatives(req_vcpu: int, req_mem_gib: float, specs):
    """Given the poller heap requirement and the candidate EC2 specs, return
    (primary, under, exceeds):

      primary  smallest instance per family that holds (>=req_vcpu, >=req_mem) at
               the least vCPU tier able to hold the memory (so vCPU isn't inflated
               the way Fargate's discrete tiers force). One row per family.
      under    if the fit overshoots memory (dead zone), the closest instances
               *below* the memory requirement at that same vCPU, one per
               (family-class, arch), newest generation.
      exceeds  no candidate can hold the requirement at all.

    Prices are applied later in cost(); selection is by spec only.
    """
    fits = [s for s in specs if s.vcpu >= req_vcpu and s.mem_gib >= req_mem_gib]
    if not fits:
        return [], [], True

    v = min(s.vcpu for s in fits)
    at_v = [s for s in specs if s.vcpu == v]

    # primary: one per family token, the smallest fitting memory for that family.
    prim_by_family: dict[str, object] = {}
    for s in (x for x in at_v if x.mem_gib >= req_mem_gib):
        cur = prim_by_family.get(_family_token(s.name))
        if cur is None or s.mem_gib < cur.mem_gib:
            prim_by_family[_family_token(s.name)] = s
    primary = [
        EC2Option(s.name, s.vcpu, s.mem_gib, s.arch) for s in prim_by_family.values()
    ]

    # under: the closest memory tier below the requirement, one per (class, arch),
    # newest generation (redundant older/pricier same-size gens are dropped).
    under_pool = [s for s in at_v if s.mem_gib < req_mem_gib]
    under: list[EC2Option] = []
    if under_pool:
        top = max(s.mem_gib for s in under_pool)
        best: dict[tuple, object] = {}
        for s in (x for x in under_pool if x.mem_gib == top):
            key = (_family_class(s.name), s.arch)
            cur = best.get(key)
            if cur is None or _generation(s.name) > _generation(cur.name):
                best[key] = s
        under = [EC2Option(s.name, s.vcpu, s.mem_gib, s.arch) for s in best.values()]

    return primary, under, False


def _fargate_hr(vcpu: int, mem_gib: float, far: dict, arch: str) -> float:
    if arch == "arm":
        return vcpu * far["arm_vcpu"] + mem_gib * far["arm_gb"]
    return vcpu * far["x86_vcpu"] + mem_gib * far["x86_gb"]


def _service_band_usd_mo(band: ServiceBand, far: dict, hpm: int) -> float:
    total = 0.0
    for tasks, cpu, mem_mib in (
        (band.core_tasks, band.core_cpu, band.core_mem_mib),
        (band.compliance_tasks, band.compliance_cpu, band.compliance_mem_mib),
    ):
        per_task = _fargate_hr(cpu / 1024, mem_mib / 1024, far, "x86")
        total += tasks * per_task * hpm
    return round(total, 2)


def cost(rec: Recommendation, prices) -> CostReport:
    """Price a Recommendation. Pure: all AWS I/O already happened in `prices`."""
    hpm = prices.hours_per_month
    far = prices.fargate
    r = CostReport(
        region_label=prices.region_label,
        source=prices.source,
        hours_per_month=hpm,
        rds_name=rec.rds.name,
    )

    rds_hr = prices.rds_hr(rec.rds.name)
    r.rds_usd_mo = round(rds_hr * hpm, 2) if rds_hr else None

    # Poller as deployed on Fargate (the discrete task size).
    r.poller_task_vcpu = rec.poller_task_vcpu
    r.poller_task_mem_gib = rec.poller_task_mem_gib
    r.poller_fargate_x86_hr = _fargate_hr(
        rec.poller_task_vcpu, rec.poller_task_mem_gib, far, "x86"
    )
    r.poller_fargate_arm_hr = _fargate_hr(
        rec.poller_task_vcpu, rec.poller_task_mem_gib, far, "arm"
    )
    r.poller_fargate_x86_usd_mo = round(r.poller_fargate_x86_hr * hpm, 2)
    r.poller_fargate_arm_usd_mo = round(r.poller_fargate_arm_hr * hpm, 2)

    # EC2 alternative anchored on the heap requirement (not the inflated task).
    r.ec2_req_vcpu = rec.poller_vcpu
    r.ec2_req_mem_gib = rec.poller_mem_gib
    primary, under, exceeds = select_ec2_alternatives(
        rec.poller_vcpu, rec.poller_mem_gib, prices.ec2_specs
    )
    r.ec2_exceeds = exceeds
    _price_options(primary, prices, hpm)
    _price_options(under, prices, hpm)
    if primary:
        cheapest = min(
            (o for o in primary if o.usd_mo is not None),
            key=lambda o: o.usd_mo,
            default=None,
        )
        if cheapest is not None:
            cheapest.cheapest = True
        # Show the "if less memory is enough" set only on a real dead-zone jump.
        best_mem = min(o.mem_gib for o in primary)
        r.show_under = (
            bool(under) and best_mem > _DEADZONE_OVERSHOOT * rec.poller_mem_gib
        )
    r.ec2_primary = primary
    r.ec2_under = under

    if rec.have_services and rec.services is not None:
        r.services_usd_mo = _service_band_usd_mo(rec.services, far, hpm)

    total = r.poller_fargate_x86_usd_mo
    total += r.rds_usd_mo or 0.0
    total += r.services_usd_mo or 0.0
    r.total_usd_mo = round(total, 2)
    omitted = []
    if r.rds_usd_mo is None:
        omitted.append("RDS (class not priced)")
    note = "RDS + poller Fargate x86_64" + (" + services" if r.services_usd_mo else "")
    if omitted:
        note += "; excludes " + ", ".join(omitted)
    r.total_note = note
    return r


def _price_options(opts: list[EC2Option], prices, hpm: int) -> None:
    for o in opts:
        hr = prices.ec2_hr(o.name)
        o.usd_mo = round(hr * hpm, 2) if hr else None
    opts.sort(key=lambda o: (o.usd_mo is None, o.usd_mo or 0.0))

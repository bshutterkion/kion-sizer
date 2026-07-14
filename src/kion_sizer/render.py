"""Formats a Recommendation for humans or machines. Mirrors internal/render.

Output is byte-for-byte identical to the Go version: Go sorts top-level JSON map
keys but keeps struct (services) keys in field order, and renders whole floats
without a trailing .0.
"""

from __future__ import annotations

import json

from .model import Recommendation

_BYTES_PER_GIB = 1 << 30


def _go_float(f):
    """Render floats the way Go's encoding/json does: whole values lose the .0."""
    if isinstance(f, float) and f.is_integer():
        return int(f)
    return f


def render_text(r: Recommendation) -> str:
    b = []
    b.append("kion-sizer recommendation\n")
    b.append(f"  calibration: {r.calibration_version}\n\n")

    b.append("CUR profile:\n")
    b.append(f"  files:            {r.profile.file_count}\n")
    b.append(
        f"  compressed bytes: {r.profile.compressed_bytes / _BYTES_PER_GIB:.1f} GiB\n"
    )
    if r.raw_line_items > 0:
        src = r.profile.sample_note if r.profile.sample_note else "from parquet footers"
        b.append(f"  CUR line items:   {r.raw_line_items} (raw, {src})\n")
    b.append(f"  aggregated PUC rows: {r.est_rows} (est. from {r.est_basis})\n")
    b.append(f"  peak shard:       {r.shard_gib:.1f} GiB\n")
    if r.raw_line_items > 0:
        b.append(
            "  NOTE: aggregated rows derived via a customer-variable raw→aggregated "
            "ratio; cross-check against the bytes-only estimate.\n"
        )
    if r.profile.granularity == "hourly":
        b.append(
            "  WARNING: hourly CUR detected — confirm Kion ingest granularity "
            "(hourly ~24x rows of daily)\n"
        )

    b.append("\nRecommended starting sizes (AWS):\n")
    rds = r.rds.name
    if r.rds_exceeds_tiers:
        rds += "  (!! exceeds known tiers — needs custom sizing/sharding review)"
    b.append(f"  RDS:              {rds} ({r.rds.ram_gib:.0f} GiB RAM)\n")
    if r.rds_tier_source:
        b.append(f"    instance classes: {r.rds_tier_source}\n")
    b.append(
        f"  financials-poller heap requirement: {r.poller_mem_gib:.1f} GiB mem, {r.poller_vcpu} vCPU\n"
    )
    task = ""
    if r.poller_task_exceeds:
        task = "  (!! exceeds largest Fargate task — needs custom/EC2 sizing)"
    b.append(
        f"  financials-poller Fargate task: {r.poller_task_mem_gib:.0f} GiB mem, "
        f"{r.poller_task_vcpu} vCPU ({r.poller_task_cpu_units} CPU units){task}\n"
    )
    if r.have_services:
        s = r.services
        b.append(
            f"  core services:    {s.core_tasks} task(s), {s.core_cpu} cpu / {s.core_mem_mib} MiB\n"
        )
        b.append(
            f"  compliance:       {s.compliance_tasks} task(s), {s.compliance_cpu} cpu / {s.compliance_mem_mib} MiB\n"
        )
    else:
        b.append("  core/compliance/cost: pass --accounts N for service bands\n")

    if r.cost is not None:
        b.append(_render_cost(r.cost))

    return "".join(b)


def _usd_mo(v) -> str:
    return f"${v:,.0f}/mo" if v is not None else "n/a"


def _render_cost(c) -> str:
    b = ["\n"]
    b.append(f"Estimated monthly cost ({c.source} · {c.hours_per_month} h/mo):\n\n")

    b.append(f"  RDS  {c.rds_name:<20} {_usd_mo(c.rds_usd_mo):>12}")
    b.append("   (instance compute only — storage & Multi-AZ not sized)\n\n")

    b.append(
        f"  financials-poller — as deployed on Fargate "
        f"({c.poller_task_vcpu} vCPU / {c.poller_task_mem_gib:.0f} GiB):\n"
    )
    b.append(
        f"    x86_64            {_usd_mo(c.poller_fargate_x86_usd_mo):>12}"
        f"   (${c.poller_fargate_x86_hr:.3f}/hr)\n"
    )
    b.append(
        f"    arm64 (Graviton)  {_usd_mo(c.poller_fargate_arm_usd_mo):>12}"
        f"   (${c.poller_fargate_arm_hr:.3f}/hr)\n"
    )

    if c.ec2_exceeds:
        b.append(
            f"  financials-poller — EC2 alternative: no candidate holds "
            f"{c.ec2_req_vcpu} vCPU / {c.ec2_req_mem_gib:.1f} GiB "
            "(needs custom sizing)\n"
        )
    elif c.ec2_primary:
        b.append(
            f"  financials-poller — EC2 alternative (holds the "
            f"{c.ec2_req_vcpu} vCPU / {c.ec2_req_mem_gib:.1f} GiB heap req):\n"
        )
        b.append(f"    {'instance':<15}{'vCPU':>5}{'mem':>9}  {'arch':<9}{'$/mo':>9}\n")
        for o in c.ec2_primary:
            b.append(_ec2_row(o))
        if c.show_under:
            b.append(
                "    note: at this vCPU the memory-optimized (r) family can't hold "
                "the requirement,\n"
                "          so it jumps to the high-memory (x) family. "
                "If less memory is enough:\n"
            )
            for o in c.ec2_under:
                b.append(_ec2_row(o))

    if c.services_usd_mo is not None:
        b.append(
            f"\n  service bands            {_usd_mo(c.services_usd_mo):>12}"
            "   (core + compliance Fargate tasks, x86_64)\n"
        )

    b.append("  " + "-" * 46 + "\n")
    b.append(f"  TOTAL ({c.total_note})   {_usd_mo(c.total_usd_mo)}\n")
    b.append("  on-demand rates; RIs / Savings Plans / Spot can cut this ~30-55%.\n")
    return "".join(b)


def _ec2_row(o) -> str:
    star = "  <- cheapest fit" if o.cheapest else ""
    price = f"${o.usd_mo:,.0f}" if o.usd_mo is not None else "n/a"
    return (
        f"    {o.name:<15}{o.vcpu:>5}{o.mem_gib:>6.0f} GiB  "
        f"{o.arch:<9}{price:>9}{star}\n"
    )


def render_json(r: Recommendation) -> str:
    # Keys inserted in alphabetical order to match Go's sorted map marshaling.
    out = {
        "accounts": r.accounts,
        "calibration_version": r.calibration_version,
        "est_basis": r.est_basis,
        "est_rows": r.est_rows,
        "poller_mem_gib": _go_float(r.poller_mem_gib),
        "poller_vcpu": r.poller_vcpu,
        "poller_task_mem_gib": _go_float(r.poller_task_mem_gib),
        "poller_task_vcpu": r.poller_task_vcpu,
        "poller_task_cpu_units": r.poller_task_cpu_units,
        "poller_task_exceeds": r.poller_task_exceeds,
        "raw_line_items": r.raw_line_items,
        "rds_exceeds_tiers": r.rds_exceeds_tiers,
        "rds_instance": r.rds.name,
        "rds_ram_gib": _go_float(r.rds.ram_gib),
        "rds_tier_source": r.rds_tier_source,
        "shard_gib": _go_float(r.shard_gib),
    }
    if r.have_services:
        s = r.services
        out["services"] = {
            "max_accounts": s.max_accounts,
            "core_tasks": s.core_tasks,
            "core_cpu": s.core_cpu,
            "core_mem_mib": s.core_mem_mib,
            "compliance_tasks": s.compliance_tasks,
            "compliance_cpu": s.compliance_cpu,
            "compliance_mem_mib": s.compliance_mem_mib,
        }
    if r.cost is not None:
        out["cost"] = _cost_json(r.cost)
    # Go json.MarshalIndent re-sorts ALL map keys; re-sort the top level by key
    # while leaving the (dict) services/cost values in field order.
    ordered = {k: out[k] for k in sorted(out.keys())}
    return json.dumps(ordered, indent=2)


def _cost_json(c) -> dict:
    def opts(rows):
        return [
            {
                "instance": o.name,
                "vcpu": o.vcpu,
                "mem_gib": _go_float(o.mem_gib),
                "arch": o.arch,
                "usd_mo": o.usd_mo,
                "cheapest": o.cheapest,
            }
            for o in rows
        ]

    return {
        "source": c.source,
        "region": c.region_label,
        "hours_per_month": c.hours_per_month,
        "rds_instance": c.rds_name,
        "rds_usd_mo": c.rds_usd_mo,
        "poller_fargate": {
            "vcpu": c.poller_task_vcpu,
            "mem_gib": _go_float(c.poller_task_mem_gib),
            "x86_64_usd_mo": c.poller_fargate_x86_usd_mo,
            "arm64_usd_mo": c.poller_fargate_arm_usd_mo,
        },
        "poller_ec2_req_vcpu": c.ec2_req_vcpu,
        "poller_ec2_req_mem_gib": _go_float(c.ec2_req_mem_gib),
        "poller_ec2_exceeds": c.ec2_exceeds,
        "poller_ec2_primary": opts(c.ec2_primary),
        "poller_ec2_under": opts(c.ec2_under) if c.show_under else [],
        "services_usd_mo": c.services_usd_mo,
        "total_usd_mo": c.total_usd_mo,
        "total_note": c.total_note,
    }

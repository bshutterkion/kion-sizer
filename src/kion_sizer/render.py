"""Formats a Recommendation for humans or machines.

JSON nests under `source` and `target` so the two axes stay legible when they
differ. Whole floats render without a trailing .0. Warnings appear near the top
of the text output, not the tail: an uncalibrated source or a pod that will not
schedule are the two things a field engineer most needs to see first.
"""

from __future__ import annotations

import json

from .model import Recommendation

_BYTES_PER_GIB = 1 << 30


def _go_float(f):
    """Emit a whole float as an int, so JSON shows 4 rather than 4.0."""
    if isinstance(f, float) and f.is_integer():
        return int(f)
    return f


def render_text(r: Recommendation) -> str:
    b = []
    b.append("kion-sizer recommendation\n")
    # The pair that was actually applied, not the file-level version: an Azure
    # run calibrated against Azure data should not print the AWS reference.
    b.append(f"  calibration: {r.source_calibration or r.calibration_version}\n")
    b.append(
        f"  source: {r.source_cloud.upper()}    "
        f"target: {r.target_cloud.upper()}\n"
    )
    if r.warnings:
        b.append("\n")
        for w in r.warnings:
            b.append(f"  !! {w}\n")
    b.append("\n")

    b.append("Billing profile:\n")
    unit = getattr(r.profile, "unit_label", "files")
    byte_label = getattr(r.profile, "byte_label", "compressed bytes")
    b.append(f"  {unit + ':':<18}{r.profile.file_count}\n")
    b.append(
        f"  {byte_label + ':':<18}"
        f"{r.profile.compressed_bytes / _BYTES_PER_GIB:.1f} GiB\n"
    )
    if r.raw_line_items > 0:
        src = r.profile.sample_note if r.profile.sample_note else "from parquet footers"
        b.append(f"  billing line items: {r.raw_line_items} (raw, {src})\n")
    b.append(f"  aggregated PUC rows: {r.est_rows} (est. from {r.est_basis})\n")
    b.append(f"  peak shard:       {r.shard_gib:.1f} GiB\n")
    if getattr(r.profile, "have_accounts", False):
        lb = "+ (lower bound)" if r.profile.account_lower_bound else ""
        b.append(
            f"  member accounts:  {r.profile.account_count}{lb} "
            f"(auto-detected from {r.profile.account_source})\n"
        )
    note = getattr(r.profile, "volume_note", "")
    if note:
        b.append(f"  NOTE: {note}\n")
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

    b.append(f"\nRecommended starting sizes ({r.target_cloud.upper()}):\n")
    db = r.db.name
    if r.db_exceeds_tiers:
        db += "  (!! exceeds known tiers — needs custom sizing/sharding review)"
    b.append(f"  database:         {db} ({r.db.ram_gib:.0f} GiB RAM)\n")
    if r.db_tier_source:
        b.append(f"    instance classes: {r.db_tier_source}\n")
    b.append(
        f"  financials-poller heap requirement: {r.poller_mem_gib:.1f} GiB mem, {r.poller_vcpu} vCPU\n"
    )
    b.append(_render_compute(r.compute))
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


def _render_compute(c) -> str:
    """Render the provisionable shape. Branches on kind so a Kubernetes target
    describes a pod plus the node pool that can actually schedule it."""
    if c.kind == "fargate_task":
        note = (
            "  (!! exceeds largest Fargate task — needs custom/EC2 sizing)"
            if c.exceeds
            else ""
        )
        return (
            f"  financials-poller Fargate task: {c.mem_gib:.0f} GiB mem, "
            f"{c.vcpu:.0f} vCPU ({c.cpu_units} CPU units){note}\n"
        )
    note = "  (!! no candidate node holds this pod)" if c.exceeds else ""
    out = (
        f"  financials-poller pod: {c.mem_gib:.1f} GiB mem, "
        f"{c.cpu_millicores}m CPU{note}\n"
    )
    if c.node_machine_type:
        out += (
            f"  node pool:        {c.node_count} x {c.node_machine_type} "
            f"({c.node_allocatable_gib:.1f} GiB allocatable each)\n"
        )
    return out


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
        b.append(
            "    NOTE: this instance replaces the financials-poller only — RDS and\n"
            "          the core/compliance/cost microservices run separately and are\n"
            "          not covered by this box (see the RDS + service-band lines).\n"
        )

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
    p = r.profile
    out = {
        "calibration_version": r.calibration_version,
        "source": {
            "cloud": r.source_cloud,
            "calibration": r.source_calibration,
            "calibration_inherited": r.calibration_inherited,
            "location": getattr(p, "source", ""),
            "unit_label": getattr(p, "unit_label", "files"),
            "unit_count": p.file_count,
            "bytes": p.compressed_bytes,
            "byte_label": getattr(p, "byte_label", "compressed bytes"),
            "granularity": p.granularity,
            "raw_line_items": r.raw_line_items,
            "est_rows": r.est_rows,
            "est_basis": r.est_basis,
            "shard_gib": _go_float(r.shard_gib),
            "volume_note": getattr(p, "volume_note", ""),
        },
        "target": {
            "cloud": r.target_cloud,
            "database": {
                "name": r.db.name,
                "ram_gib": _go_float(r.db.ram_gib),
                "source": r.db_tier_source,
                "exceeds_tiers": r.db_exceeds_tiers,
            },
            "poller_requirement": {
                "mem_gib": _go_float(r.poller_mem_gib),
                "vcpu": r.poller_vcpu,
            },
            "compute": _compute_json(r.compute),
        },
        "accounts": r.accounts,
        "warnings": list(r.warnings),
    }
    if getattr(p, "have_accounts", False):
        out["source"]["accounts"] = {
            "count": p.account_count,
            "lower_bound": p.account_lower_bound,
            "detected_from": p.account_source,
        }
    if r.have_services:
        s = r.services
        out["target"]["services"] = {
            "max_accounts": s.max_accounts,
            "core_tasks": s.core_tasks,
            "core_cpu": s.core_cpu,
            "core_mem_mib": s.core_mem_mib,
            "compliance_tasks": s.compliance_tasks,
            "compliance_cpu": s.compliance_cpu,
            "compliance_mem_mib": s.compliance_mem_mib,
        }
    if r.cost is not None:
        out["target"]["cost"] = _cost_json(r.cost)
    return json.dumps(out, indent=2)


def _compute_json(c) -> dict:
    """Emit only the fields this shape's kind populates."""
    out = {
        "kind": c.kind,
        "vcpu": _go_float(c.vcpu),
        "mem_gib": _go_float(c.mem_gib),
        "exceeds": c.exceeds,
    }
    if c.kind == "fargate_task":
        out["cpu_units"] = c.cpu_units
    else:
        out["cpu_millicores"] = c.cpu_millicores
        out["node_machine_type"] = c.node_machine_type
        out["node_mem_gib"] = _go_float(c.node_mem_gib)
        out["node_allocatable_gib"] = _go_float(c.node_allocatable_gib)
        out["node_count"] = c.node_count
    return out


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

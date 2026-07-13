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
    b.append(
        f"  financials-poller ECS task: {r.poller_mem_gib:.1f} GiB mem, {r.poller_vcpu} vCPU\n"
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

    return "".join(b)


def render_json(r: Recommendation) -> str:
    # Keys inserted in alphabetical order to match Go's sorted map marshaling.
    out = {
        "accounts": r.accounts,
        "calibration_version": r.calibration_version,
        "est_basis": r.est_basis,
        "est_rows": r.est_rows,
        "poller_mem_gib": _go_float(r.poller_mem_gib),
        "poller_vcpu": r.poller_vcpu,
        "raw_line_items": r.raw_line_items,
        "rds_exceeds_tiers": r.rds_exceeds_tiers,
        "rds_instance": r.rds.name,
        "rds_ram_gib": _go_float(r.rds.ram_gib),
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
    # Go json.MarshalIndent re-sorts ALL map keys; re-sort the top level by key
    # while leaving the (dict) services value in field order.
    ordered = {k: out[k] for k in sorted(out.keys())}
    return json.dumps(ordered, indent=2)

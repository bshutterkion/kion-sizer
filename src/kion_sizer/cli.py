"""CLI: parses flags, runs the profile→model→render pipeline.

run(args, out) returns an exit code and writes to an injected writer so it is
testable; main() wires it to argv/stdout.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import config, model, render, sources, targets
from .sources.base import ProfileError
from .targets.base import TargetError


class _UsageError(Exception):
    pass


class _SilentParser(argparse.ArgumentParser):
    """Routes argparse errors through our own exit-code convention (usage → 2)."""

    def __init__(self, out, **kw):
        self._out = out
        super().__init__(**kw)

    def error(self, message):
        print(f"error: {message}", file=self._out)
        raise _UsageError()

    def exit(self, status=0, message=None):
        if message:
            self._out.write(message)
        raise _UsageError()


def run(args: list[str], out) -> int:
    parser = _SilentParser(out, prog="kion-sizer", add_help=True, allow_abbrev=False)
    parser.add_argument("--dir", default="")
    parser.add_argument("--s3", dest="s3", default="")
    parser.add_argument(
        "--gcp-table",
        dest="gcp_table",
        default="",
        help="BigQuery billing export table as PROJECT.DATASET.TABLE",
    )
    parser.add_argument(
        "--azure-container",
        dest="azure_container",
        default="",
        help="Azure Cost Management export location "
        "(https://<account>.blob.core.windows.net/<container>/<path>)",
    )
    parser.add_argument(
        "--azure-month",
        dest="azure_month",
        default="",
        help="billing month YYYYMM to size (default: the peak period by bytes)",
    )
    parser.add_argument(
        "--oci-bucket",
        dest="oci_bucket",
        default="",
        help="OCI cost reports "
        "(https://objectstorage.<region>.oraclecloud.com/n/<ns>/b/<bucket>/o/<prefix>)",
    )
    parser.add_argument(
        "--custom-source",
        dest="custom_source",
        default="",
        help="custom billing source location (s3://bucket/prefix/, or an Azure "
        "blob URL). Custom sources are always FOCUS-formatted.",
    )
    parser.add_argument(
        "--gcp-month",
        dest="gcp_month",
        default="",
        help="billing month YYYYMM to size (default: the peak month by rows)",
    )
    parser.add_argument("--config", dest="config", default="")
    parser.add_argument("--accounts", type=int, default=0)
    parser.add_argument("--granularity", default="unknown")
    parser.add_argument(
        "--source",
        choices=("aws", "azure", "gcp", "oci", "custom"),
        default="",
        help="billing data source (default: inferred from the location flag, else aws)",
    )
    parser.add_argument(
        "--target",
        choices=("aws", "azure", "gcp"),
        default="aws",
        help="deployment target to size for (default: aws). GCP is a billing "
        "source only — Kion is not deployed on GCP.",
    )
    parser.add_argument(
        "--read-footers",
        dest="read_footers",
        action="store_true",
        help="exact raw line-item counts from parquet footers (local --dir or --s3)",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=20,
        help="CSV files to sample per format for row estimation (default 20)",
    )
    parser.add_argument(
        "--all-files",
        dest="all_files",
        action="store_true",
        help="read every CSV file instead of a sample — exact row count and exact "
        "account count, but much slower on a large CUR (ignores --sample)",
    )
    parser.add_argument(
        "--detect-accounts",
        dest="detect_accounts",
        action="store_true",
        help="auto-detect the member-account count from the CUR "
        "(exact for parquet; a lower bound from the CSV sample)",
    )
    parser.add_argument("--json", dest="as_json", action="store_true")
    parser.add_argument(
        "--rds-from-aws",
        dest="rds_from_aws",
        action="store_true",
        help="use the DB instance classes actually orderable in --region (needs AWS creds)",
    )
    parser.add_argument("--region", default="")
    parser.add_argument("--rds-engine", dest="rds_engine", default="mysql")
    parser.add_argument(
        "--cost",
        dest="cost",
        action="store_true",
        help="estimate monthly cost + EC2-equivalent for the poller (live AWS "
        "Pricing API, falls back to an embedded snapshot)",
    )
    try:
        ns = parser.parse_args(args)
    except _UsageError:
        return 2

    # Exactly one location flag, and each implies (or leaves open) a source.
    locations = [
        ("--dir", ns.dir, ""),          # ambiguous: any cloud's export downloads locally
        ("--s3", ns.s3, "aws"),
        ("--gcp-table", ns.gcp_table, "gcp"),
        ("--azure-container", ns.azure_container, "azure"),
        ("--oci-bucket", ns.oci_bucket, "oci"),
        ("--custom-source", ns.custom_source, "custom"),
    ]
    given = [(flag, val, imp) for flag, val, imp in locations if val]
    if len(given) != 1:
        print(
            "error: exactly one of --dir, --s3, --gcp-table, --azure-container, "
            "--oci-bucket, or --custom-source is required",
            file=out,
        )
        return 2
    loc_flag, location, implied = given[0]

    # An explicit --source wins, but may not contradict the location flag.
    if ns.source and implied and ns.source != implied:
        print(
            f"error: --source {ns.source} contradicts {loc_flag}, which implies "
            f"{implied}",
            file=out,
        )
        return 2
    source_cloud = ns.source or implied or "aws"

    # Flags that only mean something to a file-scanning source. Warn rather than
    # silently ignoring them, so a wrong invocation is visible.
    if source_cloud == "gcp":
        noop = [
            name
            for name, val in (
                ("--read-footers", ns.read_footers),
                ("--all-files", ns.all_files),
                ("--sample", ns.sample != 20),
            )
            if val
        ]
        if noop:
            print(
                f"warning: {', '.join(noop)} ignored for --source gcp "
                "(row counts come from BigQuery metadata and are already exact)",
                file=out,
            )

    # Resolve the target before profiling, so an unusable target fails
    # immediately rather than after a long CUR scan. A GCP target is a usage
    # error (there is no such deployment), not a runtime one.
    if ns.target in targets.SOURCE_ONLY:
        print(
            f"error: Kion is not deployed on {ns.target}; {ns.target} is supported "
            f"as a billing source only (--source {ns.target}, with --target aws "
            "or azure)",
            file=out,
        )
        return 2
    try:
        target = targets.get(ns.target)
    except TargetError as e:
        print(f"error: {e}", file=out)
        return 1

    try:
        cfg = config.default() if ns.config == "" else config.load(ns.config)
    except config.ConfigError as e:
        print(f"error: {e}", file=out)
        return 1

    try:
        p = _build_profile(
            source_cloud,
            location,
            ns.granularity,
            ns.sample,
            ns.read_footers,
            ns.detect_accounts,
            ns.all_files,
            ns.gcp_month or ns.azure_month,
        )
    except (ProfileError, OSError) as e:
        print(f"error: {e}", file=out)
        return 1

    # An explicit --accounts always wins; otherwise use the auto-detected count.
    accounts = ns.accounts
    if accounts == 0 and p.have_accounts:
        accounts = p.account_count

    # Both --rds-from-aws and --cost need a region. Fall back to the env, then to
    # us-east-1 (the Pricing API's home + a safe default), so a session without an
    # AWS_REGION set doesn't hard-fail the RDS lookup the way region=None does.
    region = (
        ns.region
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    )

    tiers, tier_source = target.db_tiers(
        cfg, live=ns.rds_from_aws, region=region, engine=ns.rds_engine
    )
    cfg.target(ns.target).db_tiers = tiers

    rec = model.recommend(p, cfg, accounts, target, source_cloud=source_cloud)
    rec.db_tier_source = tier_source

    if ns.cost:
        # Any pricing failure degrades to the embedded snapshot inside the
        # adapter; this try only guards truly unexpected errors so a cost
        # lookup can never fail the sizing run.
        try:
            prices = target.price_table(region, rec.db.name, ns.rds_engine)
            rec.cost = model.cost(rec, prices)
        except Exception as e:  # noqa: BLE001 — cost is best-effort
            print(f"warning: cost estimation skipped: {e}", file=out)

    if ns.as_json:
        print(render.render_json(rec), file=out)
    else:
        out.write(render.render_text(rec))
    return 0


def _build_profile(
    cloud: str,
    location: str,
    gran: str,
    sample: int,
    read_footers: bool,
    detect_accounts: bool,
    all_files: bool,
    month: str = "",
):
    # The source module owns the footer/sampling/account passes (that's where
    # the I/O and progress bars live); this only picks the source and sets
    # granularity.
    p = sources.load(
        cloud,
        location,
        sample=sample,
        read_footers=read_footers,
        detect_accounts=detect_accounts,
        all_files=all_files,
        month=month,
    )
    if gran in ("daily", "hourly"):
        p.granularity = gran
    return p


def main() -> None:
    sys.exit(run(sys.argv[1:], sys.stdout))

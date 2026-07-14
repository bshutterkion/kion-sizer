"""CLI: parses flags, runs the profile→model→render pipeline. Mirrors cmd/kion-sizer.

run(args, out) returns an exit code and writes to an injected writer so it is
testable; main() wires it to argv/stdout.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import config, model, pricing, profile, rds_catalog, render


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
    parser.add_argument("--config", dest="config", default="")
    parser.add_argument("--accounts", type=int, default=0)
    parser.add_argument("--granularity", default="unknown")
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

    if (ns.dir == "") == (ns.s3 == ""):
        print("error: exactly one of --dir or --s3 is required", file=out)
        return 2

    try:
        cfg = config.default() if ns.config == "" else config.load(ns.config)
    except config.ConfigError as e:
        print(f"error: {e}", file=out)
        return 1

    try:
        p = _build_profile(
            ns.dir,
            ns.s3,
            ns.granularity,
            ns.sample,
            ns.read_footers,
            ns.detect_accounts,
        )
    except (profile.ProfileError, OSError) as e:
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

    tier_source = ""
    if ns.rds_from_aws:
        try:
            cfg.rds_tiers = rds_catalog.orderable_tiers(region, ns.rds_engine)
            tier_source = f"orderable in {region} ({ns.rds_engine})"
        except Exception as e:  # noqa: BLE001 — resilience: fall back on any AWS failure
            tier_source = f"built-in defaults (AWS lookup failed: {e})"

    rec = model.recommend(p, cfg, accounts)
    rec.rds_tier_source = tier_source

    if ns.cost:
        # Any pricing failure degrades to the embedded snapshot inside
        # build_price_table; this try only guards truly unexpected errors so a
        # cost lookup can never fail the sizing run.
        try:
            prices = pricing.build_price_table(region, rec.rds.name, ns.rds_engine)
            rec.cost = model.cost(rec, prices)
        except Exception as e:  # noqa: BLE001 — cost is best-effort
            print(f"warning: cost estimation skipped: {e}", file=out)

    if ns.as_json:
        print(render.render_json(rec), file=out)
    else:
        out.write(render.render_text(rec))
    return 0


def _build_profile(
    dir: str,
    s3uri: str,
    gran: str,
    sample: int,
    read_footers: bool,
    detect_accounts: bool,
):
    # from_dir/from_s3 own the footer/sampling/account passes (that's where the
    # I/O and progress bars live); this only picks the source and sets granularity.
    if dir != "":
        p = profile.from_dir(
            dir,
            sample=sample,
            read_footers=read_footers,
            detect_accounts=detect_accounts,
        )
    else:
        p = profile.from_s3(
            s3uri,
            sample=sample,
            read_footers=read_footers,
            detect_accounts=detect_accounts,
        )

    if gran in ("daily", "hourly"):
        p.granularity = gran
    return p


def main() -> None:
    sys.exit(run(sys.argv[1:], sys.stdout))

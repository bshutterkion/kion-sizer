"""CLI: parses flags, runs the profile→model→render pipeline. Mirrors cmd/kion-sizer.

run(args, out) returns an exit code and writes to an injected writer so it is
testable; main() wires it to argv/stdout.
"""

from __future__ import annotations

import argparse
import sys

from . import config, model, profile, rds_catalog, render


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
    parser.add_argument("--read-footers", dest="read_footers", action="store_true")
    parser.add_argument("--json", dest="as_json", action="store_true")
    parser.add_argument(
        "--rds-from-aws",
        dest="rds_from_aws",
        action="store_true",
        help="use the DB instance classes actually orderable in --region (needs AWS creds)",
    )
    parser.add_argument("--region", default="")
    parser.add_argument("--rds-engine", dest="rds_engine", default="mysql")
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
        p = _build_profile(ns.dir, ns.s3, ns.granularity, ns.read_footers)
    except (profile.ProfileError, OSError) as e:
        print(f"error: {e}", file=out)
        return 1

    tier_source = ""
    if ns.rds_from_aws:
        region = ns.region or None
        try:
            cfg.rds_tiers = rds_catalog.orderable_tiers(region, ns.rds_engine)
            tier_source = (
                f"orderable in {ns.region or 'default region'} ({ns.rds_engine})"
            )
        except Exception as e:  # noqa: BLE001 — resilience: fall back on any AWS failure
            tier_source = f"built-in defaults (AWS lookup failed: {e})"

    rec = model.recommend(p, cfg, ns.accounts)
    rec.rds_tier_source = tier_source
    if ns.as_json:
        print(render.render_json(rec), file=out)
    else:
        out.write(render.render_text(rec))
    return 0


def _build_profile(dir: str, s3uri: str, gran: str, read_footers: bool):
    if dir != "":
        p = profile.from_dir(dir)
    else:
        p = profile.from_s3(s3uri)

    if read_footers and dir != "":
        p.raw_line_items = profile.read_footer_rows(dir)
        p.have_raw_rows = True

    if p.has_csv and not p.has_parquet and dir != "":
        est, sampled, total = profile.sample_dir_raw_rows(dir, 3)
        p.raw_line_items, p.have_raw_rows = est, True
        p.sample_note = (
            f"ESTIMATED by sampling {sampled} of {total} files "
            "(extrapolated by byte share)"
        )

    if gran in ("daily", "hourly"):
        p.granularity = gran
    return p


def main() -> None:
    sys.exit(run(sys.argv[1:], sys.stdout))

#!/usr/bin/env bash
#
# test_cloudshell.sh — hermetic tests for scripts/cloudshell.sh CUR discovery.
#
# Puts a fake `aws` on PATH (driven by $SCENARIO) and asserts that
# `cloudshell.sh --dry-run` resolves the correct `uv run kion-sizer …` command.
# No real AWS, no uv, no Docker — pure discovery-logic coverage.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../scripts/cloudshell.sh"
SHIMDIR="$(mktemp -d)"
trap 'rm -rf "$SHIMDIR"' EXIT

# --- fake aws ---------------------------------------------------------------
cat > "$SHIMDIR/aws" <<'FAKE'
#!/usr/bin/env bash
# Dispatch on the AWS subcommand + $SCENARIO. Prints canned JSON / listings.
svc="$1"; sub="${2:-}"
case "$svc/$sub" in
  cur/describe-report-definitions)
    case "$SCENARIO" in
      legacy_hourly|passthrough)
        echo '{"ReportDefinitions":[{"ReportName":"hourly-cur","TimeUnit":"HOURLY","Format":"Parquet","AdditionalArtifacts":["ATHENA"],"S3Bucket":"cur-bucket","S3Prefix":"reports"}]}' ;;
      multi_report)
        echo '{"ReportDefinitions":[{"ReportName":"daily","TimeUnit":"DAILY","Format":"textORcsv","S3Bucket":"daily-bkt","S3Prefix":"r"},{"ReportName":"hourly","TimeUnit":"HOURLY","Format":"Parquet","S3Bucket":"hourly-bkt","S3Prefix":"r"}]}' ;;
      *) echo '{"ReportDefinitions":[]}' ;;
    esac ;;
  bcm-data-exports/list-exports)
    case "$SCENARIO" in
      data_exports) echo '{"Exports":[{"ExportName":"cur2","ExportArn":"arn:aws:bcm:us-east-1:1:export/cur2"}]}' ;;
      *)            echo '{"Exports":[]}' ;;
    esac ;;
  bcm-data-exports/get-export)
    echo '{"Export":{"Name":"cur2","DestinationConfigurations":{"S3Destination":{"S3Bucket":"cur2-bkt","S3Prefix":"exports"}}}}' ;;
  s3/ls)
    # `aws s3 ls` (bucket list) has no s3:// arg; `aws s3 ls s3://… --recursive` does.
    if [ "${3:-}" = "--recursive" ] || [[ "${3:-}" == s3://* ]]; then
      path="$3"
      case "$SCENARIO" in
        data_exports)
          echo "2026-06-01 00:00:00     50 exports/cur2/data/BILLING_PERIOD=2026-05/f.parquet"
          echo "2026-07-01 00:00:00    900 exports/cur2/data/BILLING_PERIOD=2026-06/f.parquet" ;;
        bucketname)
          echo "2026-06-01 00:00:00    100 cur/yearMonth=202605/f.parquet"
          echo "2026-07-01 00:00:00    800 cur/yearMonth=202606/f.parquet" ;;
        named_bucket)
          echo "2026-06-01 00:00:00    100 reports/parquet/yearMonth=202605/f.parquet"
          echo "2026-07-01 00:00:00    700 reports/parquet/yearMonth=202606/f.parquet" ;;
        multi_report)
          echo "2026-06-01 00:00:00    100 r/hourly/yearMonth=202605/f.parquet"
          echo "2026-07-01 00:00:00    999 r/hourly/yearMonth=202606/f.parquet" ;;
        *)  # legacy_hourly / passthrough: 202606 is the peak by bytes
          echo "2026-06-01 00:00:00    100 reports/hourly-cur/yearMonth=202605/a.parquet"
          echo "2026-07-01 00:00:00    500 reports/hourly-cur/yearMonth=202606/b.parquet"
          echo "2026-07-01 00:00:00    400 reports/hourly-cur/yearMonth=202606/c.parquet" ;;
      esac
    else
      # bucket list (heuristic tier)
      case "$SCENARIO" in
        bucketname) echo "2026-01-01 00:00:00 my-cur-bucket" ;;
        *) : ;;  # no buckets -> heuristic finds nothing
      esac
    fi ;;
  *) echo "fake aws: unhandled $svc/$sub" >&2; exit 3 ;;
esac
FAKE
chmod +x "$SHIMDIR/aws"

# Clear region envs so --region isn't appended (keeps expectations deterministic).
run() { PATH="$SHIMDIR:$PATH" SCENARIO="$1" AWS_REGION= AWS_DEFAULT_REGION= bash "$SCRIPT" "${@:2}" 2>/dev/null; }

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL %s\n     expected: %s\n     got:      %s\n' "$1" "$2" "$3"; }

expect() { # name scenario  expected-stdout  [extra args...]
  local name="$1" scen="$2" want="$3"; shift 3
  local got; got="$(run "$scen" --dry-run "$@")"
  [ "$got" = "$want" ] && ok "$name" || bad "$name" "$want" "$got"
}

expect_fail() { # name scenario  — asserts nonzero exit and no stdout
  local name="$1" scen="$2"
  local got rc
  got="$(run "$scen" --dry-run 2>/dev/null)"; rc=$?
  { [ "$rc" -ne 0 ] && [ -z "$got" ]; } && ok "$name" || bad "$name" "nonzero exit, empty stdout" "rc=$rc out=$got"
}

echo "cloudshell.sh discovery tests:"

expect "legacy hourly parquet, peak month" legacy_hourly \
  "uv run kion-sizer --s3 s3://cur-bucket/reports/hourly-cur/yearMonth=202606/ --granularity hourly --rds-from-aws"

expect "prefers HOURLY+parquet among reports" multi_report \
  "uv run kion-sizer --s3 s3://hourly-bkt/r/hourly/yearMonth=202606/ --granularity hourly --rds-from-aws"

expect "CUR 2.0 data-exports fallback (BILLING_PERIOD)" data_exports \
  "uv run kion-sizer --s3 s3://cur2-bkt/exports/cur2/data/BILLING_PERIOD=2026-06/ --rds-from-aws"

expect "bucket-name heuristic fallback" bucketname \
  "uv run kion-sizer --s3 s3://my-cur-bucket/cur/yearMonth=202606/ --rds-from-aws"

# --bucket picks the peak month in a named bucket, skipping the discovery tiers.
expect "--bucket picks peak month in named bucket" named_bucket \
  "uv run kion-sizer --s3 s3://named-bkt/reports/parquet/yearMonth=202606/ --rds-from-aws" \
  --bucket named-bkt

# A pasted s3:// scheme and trailing slash on --bucket resolve identically.
expect "--bucket tolerates s3:// scheme and trailing slash" named_bucket \
  "uv run kion-sizer --s3 s3://named-bkt/reports/parquet/yearMonth=202606/ --rds-from-aws" \
  --bucket s3://named-bkt/

# --s3 wins when both --s3 and --bucket are given.
expect "--s3 takes precedence over --bucket" named_bucket \
  "uv run kion-sizer --s3 s3://manual/pfx/ --rds-from-aws" \
  --s3 s3://manual/pfx/ --bucket named-bkt

expect "accounts + json passthrough" passthrough \
  "uv run kion-sizer --s3 s3://cur-bucket/reports/hourly-cur/yearMonth=202606/ --granularity hourly --accounts 150 --json --rds-from-aws" \
  --accounts 150 --json

# --s3 override skips discovery entirely (no --granularity inferred).
got="$(PATH="$SHIMDIR:$PATH" SCENARIO=none AWS_REGION= AWS_DEFAULT_REGION= bash "$SCRIPT" --dry-run --s3 s3://manual/pfx/ 2>/dev/null)"
[ "$got" = "uv run kion-sizer --s3 s3://manual/pfx/ --rds-from-aws" ] \
  && ok "--s3 override skips discovery" \
  || bad "--s3 override skips discovery" "uv run kion-sizer --s3 s3://manual/pfx/ --rds-from-aws" "$got"

expect_fail "no CUR anywhere -> error" none

echo "  ---- $PASS passed, $FAIL failed ----"
[ "$FAIL" -eq 0 ]

#!/usr/bin/env bash
#
# cloudshell.sh — size a Kion deployment from THIS account's CUR, in AWS CloudShell.
#
# Run it inside an AWS CloudShell session that is signed into the account whose
# Cost and Usage Report you want to size. It needs no account flag, no --profile,
# and no S3 URI: it uses CloudShell's ambient credentials, discovers the account's
# CUR automatically, picks the peak month, and runs kion-sizer against it.
#
# What it does, in order:
#   1. Bootstrap the runtime — installs `uv` if missing (which provisions Python
#      3.12 + the tool's deps in your home dir; nothing needs sudo).
#   2. Discover the CUR — legacy CUR (describe-report-definitions), then CUR 2.0 /
#      BCM Data Exports, then a bucket-name heuristic. Picks the peak month by bytes.
#   3. Size it — runs `kion-sizer --s3 <discovered> --granularity <hourly|daily>`.
#
# Usage:
#   bash scripts/cloudshell.sh                  # discover + size (human-readable)
#   bash scripts/cloudshell.sh --accounts 150   # add per-service starting bands
#   bash scripts/cloudshell.sh --json           # machine-readable output
#   bash scripts/cloudshell.sh --s3 s3://b/pfx/ # skip discovery, size this prefix
#   bash scripts/cloudshell.sh --dry-run        # print the resolved command, don't run
#
# Requires (all preinstalled in CloudShell): aws, python3, curl, git.

set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# This script is runnable (`bash cloudshell.sh …`) and sourceable: sourcing it
# defines the functions (bootstrap_uv, discover_cur, …) without running main, so
# other scripts and tests can reuse the pieces.

log() { printf '%s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# --- CUR discovery ----------------------------------------------------------
# The discovery functions each echo two lines on success:
#   GRAN=<hourly|daily|>            (empty when the source doesn't declare it)
#   S3_URI=s3://<bucket>/<prefix>/  (the peak-by-bytes month)
# They run inside command substitution, so results MUST be printed, not set as
# shell vars (a var set in a $(...) subshell would not survive).

# Given a bucket and a base key prefix, list objects, group by billing-month
# token, and echo "S3_URI=s3://<bucket>/<peak-month-prefix>/" for the month with
# the most bytes. Returns nonzero if the listing has no parquet/csv data.
_peak_month_uri() {
  local bucket="$1" base="$2"
  aws s3 ls "s3://${bucket}/${base}" --recursive 2>/dev/null | BKT="$bucket" python3 -c '
import sys, os, re, collections
bkt = os.environ["BKT"]
tok = re.compile(r"yearMonth=\d{6}|BILLING_PERIOD=\d{4}-\d{2}|\d{8}-\d{8}")
sizes = collections.Counter()
sample = {}
def is_data(k):
    k = k.lower()
    return k.endswith((".parquet", ".csv", ".csv.gz"))
for line in sys.stdin:
    parts = line.split(None, 3)
    if len(parts) < 4:
        continue
    key = parts[3].strip()
    if not is_data(key):
        continue
    try:
        size = int(parts[2])
    except ValueError:
        continue
    m = tok.search(key)
    grp = m.group(0) if m else key.rsplit("/", 1)[0]
    sizes[grp] += size
    sample.setdefault(grp, key)
if not sizes:
    sys.exit(4)
top = sizes.most_common(1)[0][0]
key = sample[top]
i = key.find(top)
prefix = key[: i + len(top)]
if not prefix.endswith("/"):
    prefix += "/"
print("S3_URI=s3://%s/%s" % (bkt, prefix))
'
}

# Tier 1: legacy CUR report definitions (the CUR API lives only in us-east-1).
_discover_legacy_cur() {
  local json parsed base gran uri
  local BUCKET="" PREFIX="" NAME="" TIMEUNIT=""
  json="$(aws cur describe-report-definitions --region us-east-1 --output json 2>/dev/null)" || return 1
  parsed="$(printf '%s' "$json" | python3 -c '
import sys, json
data = json.load(sys.stdin)
reports = data.get("ReportDefinitions", [])
if not reports:
    sys.exit(3)
def score(r):
    hourly = 1 if r.get("TimeUnit", "").upper() == "HOURLY" else 0
    blob = (r.get("Format", "") + " " + " ".join(r.get("AdditionalArtifacts", []) or [])).lower()
    parquet = 1 if "parquet" in blob else 0
    return (hourly, parquet)
r = max(reports, key=score)
print("BUCKET=%s" % r["S3Bucket"])
print("PREFIX=%s" % r.get("S3Prefix", ""))
print("NAME=%s" % r["ReportName"])
print("TIMEUNIT=%s" % r.get("TimeUnit", ""))
' 2>/dev/null)" || return 1
  [ -n "$parsed" ] || return 1
  eval "$parsed"
  base="${PREFIX:+$PREFIX/}${NAME}/"
  gran="$(printf '%s' "$TIMEUNIT" | tr '[:upper:]' '[:lower:]')"
  uri="$(_peak_month_uri "$BUCKET" "$base")" || return 1
  [ -n "$uri" ] || return 1
  printf 'GRAN=%s\n%s\n' "$gran" "$uri"
}

# Tier 2: CUR 2.0 / BCM Data Exports.
_discover_data_exports() {
  local list arn parsed base uri
  local BUCKET="" PREFIX="" NAME=""
  list="$(aws bcm-data-exports list-exports --region us-east-1 --output json 2>/dev/null)" || return 1
  arn="$(printf '%s' "$list" | python3 -c '
import sys, json
data = json.load(sys.stdin)
exports = data.get("Exports", [])
if not exports:
    sys.exit(3)
print(exports[0].get("ExportArn", ""))
' 2>/dev/null)" || return 1
  [ -n "$arn" ] || return 1
  parsed="$(aws bcm-data-exports get-export --export-arn "$arn" --region us-east-1 --output json 2>/dev/null | python3 -c '
import sys, json
data = json.load(sys.stdin)
exp = data.get("Export", {})
dest = exp.get("DestinationConfigurations", {}).get("S3Destination", {})
bucket = dest.get("S3Bucket", "")
if not bucket:
    sys.exit(3)
print("BUCKET=%s" % bucket)
print("PREFIX=%s" % dest.get("S3Prefix", ""))
print("NAME=%s" % exp.get("Name", ""))
' 2>/dev/null)" || return 1
  [ -n "$parsed" ] || return 1
  eval "$parsed"
  base="${PREFIX:+$PREFIX/}${NAME:+$NAME/}"
  # CUR 2.0 granularity is defined in the export's SQL query; leave for --granularity.
  uri="$(_peak_month_uri "$BUCKET" "$base")" || return 1
  [ -n "$uri" ] || return 1
  printf 'GRAN=\n%s\n' "$uri"
}

# Tier 3: bucket-name heuristic (Kion-style / default CUR bucket naming).
_discover_by_bucket_name() {
  local bucket uri
  bucket="$(aws s3 ls 2>/dev/null | awk '{print $3}' | grep -iE -- '-hourly$|-billing-data$|cur|cost.*usage' | head -1)"
  [ -n "$bucket" ] || return 1
  uri="$(_peak_month_uri "$bucket" "")" || return 1
  [ -n "$uri" ] || return 1
  printf 'GRAN=\n%s\n' "$uri"
}

discover_cur() {
  local out
  if out="$(_discover_legacy_cur)"; then
    log "# discovered CUR via cur:DescribeReportDefinitions"
    printf '%s\n' "$out"; return 0
  fi
  if out="$(_discover_data_exports)"; then
    log "# discovered CUR via bcm-data-exports (CUR 2.0)"
    printf '%s\n' "$out"; return 0
  fi
  if out="$(_discover_by_bucket_name)"; then
    log "# discovered CUR via bucket-name heuristic"
    printf '%s\n' "$out"; return 0
  fi
  return 1
}

# --- runtime bootstrap ------------------------------------------------------
bootstrap_uv() {
  if ! command -v uv >/dev/null 2>&1; then
    log "# installing uv (provisions Python 3.12 + deps in your home dir)…"
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 \
      || die "uv install failed"
    export PATH="$HOME/.local/bin:$PATH"
  fi
  command -v uv >/dev/null 2>&1 || die "uv not on PATH after install"
  log "# syncing runtime (uv sync)…"
  ( cd "$REPO" && uv sync ) >/dev/null 2>&1 || die "uv sync failed"
}

# --- main -------------------------------------------------------------------
main() {
  local ACCOUNTS="" AS_JSON=0 S3_OVERRIDE="" DRY_RUN=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --accounts)   ACCOUNTS="${2:-}"; shift 2 ;;
      --accounts=*) ACCOUNTS="${1#*=}"; shift ;;
      --json)       AS_JSON=1; shift ;;
      --s3)         S3_OVERRIDE="${2:-}"; shift 2 ;;
      --s3=*)       S3_OVERRIDE="${1#*=}"; shift ;;
      --dry-run)    DRY_RUN=1; shift ;;
      -h|--help)    grep '^#' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; return 0 ;;
      *) echo "unknown arg: $1" >&2; return 2 ;;
    esac
  done

  local GRAN="" S3_URI="" out
  if [ -n "$S3_OVERRIDE" ]; then
    S3_URI="$S3_OVERRIDE"
  else
    out="$(discover_cur)" || die "no CUR found in this account (no report definition, no data export, no matching bucket). Pass --s3 s3://bucket/prefix/ to size a specific location."
    eval "$out"   # sets GRAN and S3_URI
    [ -n "$S3_URI" ] || die "CUR discovery returned no location"
    log "# peak-month CUR: $S3_URI  (granularity: ${GRAN:-unknown})"
  fi

  local ARGS=(--s3 "$S3_URI")
  case "$GRAN" in hourly|daily) ARGS+=(--granularity "$GRAN") ;; esac
  [ -n "$ACCOUNTS" ] && ARGS+=(--accounts "$ACCOUNTS")
  [ "$AS_JSON" = 1 ] && ARGS+=(--json)
  # CloudShell has creds — size the RDS tier against the classes actually
  # orderable in this region (falls back to built-in tiers if the lookup fails).
  ARGS+=(--rds-from-aws)
  local region="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
  [ -n "$region" ] && ARGS+=(--region "$region")

  if [ "$DRY_RUN" = 1 ]; then
    printf 'uv run kion-sizer'
    printf ' %s' "${ARGS[@]}"
    printf '\n'
    return 0
  fi

  bootstrap_uv
  log "# sizing… (querying RDS classes orderable in ${region:-the default region}; falls back to built-in tiers if AWS is slow)"
  ( cd "$REPO" && uv run kion-sizer "${ARGS[@]}" )
}

# Run main only when executed directly; sourcing just loads the functions.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main "$@"
fi

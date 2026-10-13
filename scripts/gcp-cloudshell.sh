#!/usr/bin/env bash
#
# gcp-cloudshell.sh — size a Kion deployment from a GCP billing export, in Google
# Cloud Shell.
#
# GCP is a billing SOURCE only: Kion is not deployed on GCP. This sizes the AWS
# (or, once it exists, Azure) deployment that will import this GCP spend.
#
# Run it inside a Cloud Shell session signed in as someone who can read the
# billing export dataset. It finds the BigQuery billing export table itself, so
# no table name is needed in the common case.
#
# What it does, in order:
#   1. Discover the export — looks for gcp_billing_export_* tables in the current
#      project, then in every project you can list (capped by --max-projects).
#      When several are found it prefers the detailed (resource-level) export,
#      then standard, then FOCUS, and the largest table within a kind. Detailed
#      is preferred because it is the conservative choice: for the same spend it
#      produces ~16x the database rows of a standard export, and under-sizing is
#      the failure that matters. If Kion is configured to ingest a different
#      export, name it with --table; every candidate found is logged.
#   2. Bootstrap the runtime — installs `uv` if missing and syncs only the GCP
#      extra (Cloud Shell's $HOME is a fixed 5 GB).
#   3. Size it — runs `kion-sizer --gcp-table <table> --target aws --cost`. Row
#      and project counts come from BigQuery metadata and are exact. --cost uses
#      the embedded AWS price snapshot, since there are no AWS credentials here.
#
# Usage:
#   bash scripts/gcp-cloudshell.sh                        # discover + size
#   bash scripts/gcp-cloudshell.sh --accounts 150         # override the project count
#   bash scripts/gcp-cloudshell.sh --json                 # machine-readable output
#   bash scripts/gcp-cloudshell.sh --table P.DS.TABLE     # skip discovery
#   bash scripts/gcp-cloudshell.sh --project P            # search this project first
#   bash scripts/gcp-cloudshell.sh --month YYYYMM         # size this billing month
#   bash scripts/gcp-cloudshell.sh --target azure         # size an Azure deployment
#   bash scripts/gcp-cloudshell.sh --max-projects 200     # widen the project sweep
#   bash scripts/gcp-cloudshell.sh --dry-run              # print the command only
#
# Requires (all preinstalled in Cloud Shell): gcloud, bq, python3, curl.
# Permissions: BigQuery Data Viewer on the export dataset, and BigQuery Job User
# in the project the queries bill to.

set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Runnable and sourceable, like cloudshell.sh: sourcing defines the functions
# without running main, which is how the hermetic tests drive it.

log() { printf '%s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# --- export discovery -------------------------------------------------------
# Candidates are printed one per line as "<kind-rank>\t<rows>\t<project.dataset.table>",
# rank 0 = detailed, 1 = standard, 2 = FOCUS. The pricing export
# (cloud_pricing_export) is not a billing export and is never a candidate.

# Echo every billing-export table in one project.
_tables_in_project() {
  local project="$1" ds
  local datasets
  datasets="$(bq ls --format=json --max_results=1000 --project_id="$project" 2>/dev/null | python3 -c '
import sys, json
try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
for d in data or []:
    ref = d.get("datasetReference", {})
    if ref.get("datasetId"):
        print(ref["datasetId"])
' 2>/dev/null)" || return 0
  for ds in $datasets; do
    bq ls --format=json --max_results=10000 "${project}:${ds}" 2>/dev/null | P="$project" D="$ds" python3 -c '
import sys, json, os, re
try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
kinds = (
    (re.compile(r"^gcp_billing_export_resource_v1_"), 0),
    (re.compile(r"^gcp_billing_export_v1_"), 1),
    (re.compile(r"^gcp_billing_export_focus_"), 2),
)
for t in data or []:
    name = t.get("tableReference", {}).get("tableId", "")
    for pat, rank in kinds:
        if pat.match(name):
            print("%d\t%s.%s.%s" % (rank, os.environ["P"], os.environ["D"], name))
            break
' 2>/dev/null
  done
}

# Row count for one table, for picking the largest within a kind. 0 if unknown.
_table_rows() {
  local ref="$1" proj rest
  proj="${ref%%.*}"; rest="${ref#*.}"
  bq show --format=json "${proj}:${rest}" 2>/dev/null | python3 -c '
import sys, json
try:
    print(int(json.load(sys.stdin).get("numRows", 0)))
except (ValueError, TypeError):
    print(0)
' 2>/dev/null || echo 0
}

# Echo "TABLE=<project.dataset.table>" for the preferred export, logging every
# candidate. Searches `first` (if given), then every listable project.
discover_export() {
  local first="$1" max="$2" found="" p cand rank ref rows
  if [ -n "$first" ]; then
    found="$(_tables_in_project "$first")"
    [ -n "$found" ] && log "# found billing export(s) in project $first"
  fi
  if [ -z "$found" ]; then
    log "# no export in ${first:-the current project}; searching up to $max projects…"
    for p in $(gcloud projects list --format='value(projectId)' --limit="$max" 2>/dev/null); do
      [ "$p" = "$first" ] && continue
      cand="$(_tables_in_project "$p")"
      [ -n "$cand" ] && found="${found:+$found
}$cand"
    done
  fi
  [ -n "$found" ] || return 1

  local ranked=""
  while IFS=$'\t' read -r rank ref; do
    [ -n "$ref" ] || continue
    rows="$(_table_rows "$ref")"
    ranked="${ranked:+$ranked
}${rank}	${rows}	${ref}"
  done <<< "$found"

  # Best kind first, then most rows.
  ranked="$(printf '%s\n' "$ranked" | sort -t $'\t' -k1,1n -k2,2nr)"
  log "# billing export candidates (kind: 0=detailed 1=standard 2=FOCUS):"
  printf '%s\n' "$ranked" | while IFS=$'\t' read -r rank rows ref; do
    log "#   [$rank] $ref  ($rows rows)"
  done
  ref="$(printf '%s\n' "$ranked" | head -1 | cut -f3)"
  [ -n "$ref" ] || return 1
  printf 'TABLE=%s\n' "$ref"
}

# --- runtime bootstrap ------------------------------------------------------
# Run a command quietly, but replay everything it printed if it fails, so a
# network error or resolver conflict is visible instead of a bare "failed".
_quiet() {
  local out rc
  out="$(mktemp)"
  "$@" >"$out" 2>&1
  rc=$?
  [ "$rc" -ne 0 ] && cat "$out" >&2
  rm -f "$out"
  return "$rc"
}

bootstrap_uv() {
  if ! command -v uv >/dev/null 2>&1; then
    log "# installing uv (provisions Python 3.12 + deps in your home dir)…"
    _quiet bash -c 'set -o pipefail; curl -LsSf https://astral.sh/uv/install.sh | sh' \
      || die "uv install failed (output above)"
    export PATH="$HOME/.local/bin:$PATH"
  fi
  command -v uv >/dev/null 2>&1 || die "uv not on PATH after install"
  log "# syncing runtime (uv sync --extra gcp)…"
  ( cd "$REPO" && _quiet uv sync --extra gcp ) \
    || die "uv sync failed (output above)"
}

# --- main -------------------------------------------------------------------
main() {
  local ACCOUNTS="" AS_JSON=0 TABLE="" PROJECT="" MONTH="" TARGET="aws"
  local MAX_PROJECTS=50 DRY_RUN=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --accounts)       ACCOUNTS="${2:-}"; shift 2 ;;
      --accounts=*)     ACCOUNTS="${1#*=}"; shift ;;
      --json)           AS_JSON=1; shift ;;
      --table)          TABLE="${2:-}"; shift 2 ;;
      --table=*)        TABLE="${1#*=}"; shift ;;
      --project)        PROJECT="${2:-}"; shift 2 ;;
      --project=*)      PROJECT="${1#*=}"; shift ;;
      --month)          MONTH="${2:-}"; shift 2 ;;
      --month=*)        MONTH="${1#*=}"; shift ;;
      --target)         TARGET="${2:-}"; shift 2 ;;
      --target=*)       TARGET="${1#*=}"; shift ;;
      --max-projects)   MAX_PROJECTS="${2:-}"; shift 2 ;;
      --max-projects=*) MAX_PROJECTS="${1#*=}"; shift ;;
      --dry-run)        DRY_RUN=1; shift ;;
      -h|--help)        grep '^#' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; return 0 ;;
      *) echo "unknown arg: $1" >&2; return 2 ;;
    esac
  done

  if [ -z "$TABLE" ]; then
    [ -n "$PROJECT" ] || PROJECT="$(gcloud config get-value project 2>/dev/null)"
    local out
    out="$(discover_export "$PROJECT" "$MAX_PROJECTS")" \
      || die "no GCP billing export table found (searched ${PROJECT:-no current project} and up to $MAX_PROJECTS projects). Pass --table PROJECT.DATASET.TABLE."
    eval "$out"   # sets TABLE
    [ -n "$TABLE" ] || die "export discovery returned no table"
    log "# sizing billing export: $TABLE"
  fi

  local ARGS=(--gcp-table "$TABLE" --target "$TARGET")
  [ -n "$MONTH" ] && ARGS+=(--gcp-month "$MONTH")
  [ -n "$ACCOUNTS" ] && ARGS+=(--accounts "$ACCOUNTS")
  [ "$AS_JSON" = 1 ] && ARGS+=(--json)
  # No AWS credentials in Cloud Shell, so --cost prices from the embedded
  # snapshot (the output says so). --rds-from-aws would only warn and fall back.
  ARGS+=(--cost)

  if [ "$DRY_RUN" = 1 ]; then
    printf 'uv run kion-sizer'
    printf ' %s' "${ARGS[@]}"
    printf '\n'
    return 0
  fi

  bootstrap_uv
  log "# sizing… (BigQuery metadata + one aggregate query; no row data is read)"
  ( cd "$REPO" && uv run kion-sizer "${ARGS[@]}" )
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main "$@"
fi

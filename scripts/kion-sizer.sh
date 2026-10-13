#!/usr/bin/env bash
#
# kion-sizer.sh — run kion-sizer with any flags, from any shell.
#
# The per-cloud scripts (cloudshell.sh, azure-cloudshell.sh, gcp-cloudshell.sh)
# discover the billing data for you and are the place to start. Use this one
# when you already know where the data is, or for a source with no discovery
# script: a downloaded CUR directory, OCI, or a custom FOCUS source.
#
# It installs the runtime if needed (uv, which provisions Python 3.12 + the
# tool's dependencies in your home dir; no sudo), syncs only the cloud SDK the
# location flag needs, and passes every argument straight through.
#
# Usage:
#   bash scripts/kion-sizer.sh --dir /path/to/cur-month/ --read-footers --accounts 150
#   bash scripts/kion-sizer.sh --s3 s3://bucket/report/month/ --json
#   bash scripts/kion-sizer.sh --azure-container https://<acct>.blob.core.windows.net/<container>/<path>
#   bash scripts/kion-sizer.sh --gcp-table PROJECT.DATASET.TABLE
#   bash scripts/kion-sizer.sh --oci-bucket https://objectstorage.<region>.oraclecloud.com/n/<ns>/b/<bucket>/o/reports/cost-csv
#   bash scripts/kion-sizer.sh --custom-source s3://bucket/prefix/
#   bash scripts/kion-sizer.sh --help             # every flag the tool takes
#   bash scripts/kion-sizer.sh --dry-run ...      # print what would run, don't run
#
# Requires: bash, curl, python3.

set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf '%s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# The SDK extra a run needs, from its location flag. A local --dir may hold
# parquet, which needs pyarrow (in the aws extra); a custom source lives in S3
# or Azure blob storage. OCI needs no extra: its client is not wired in yet.
extra_for() {
  local a next=""
  for a in "$@"; do
    if [ -n "$next" ]; then
      case "$next" in
        custom) case "$a" in s3://*) echo aws ;; *) echo azure ;; esac ;;
        *) echo "$next" ;;
      esac
      return 0
    fi
    case "$a" in
      --dir|--s3)          next=aws ;;
      --dir=*|--s3=*)      echo aws; return 0 ;;
      --azure-container)   next=azure ;;
      --azure-container=*) echo azure; return 0 ;;
      --gcp-table)         next=gcp ;;
      --gcp-table=*)       echo gcp; return 0 ;;
      --custom-source)     next=custom ;;
      --custom-source=*)   case "${a#*=}" in s3://*) echo aws ;; *) echo azure ;; esac; return 0 ;;
    esac
  done
}

bootstrap_uv() {
  local extra="$1"
  if ! command -v uv >/dev/null 2>&1; then
    log "# installing uv (provisions Python 3.12 + deps in your home dir)…"
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 \
      || die "uv install failed"
    export PATH="$HOME/.local/bin:$PATH"
  fi
  command -v uv >/dev/null 2>&1 || die "uv not on PATH after install"
  log "# syncing runtime${extra:+ ($extra extra)}…"
  ( cd "$REPO" && uv sync ${extra:+--extra "$extra"} ) >/dev/null 2>&1 \
    || die "uv sync failed"
}

main() {
  local DRY_RUN=0 args=() a
  for a in "$@"; do
    [ "$a" = "--dry-run" ] && DRY_RUN=1 || args+=("$a")
  done
  local extra
  extra="$(extra_for ${args[@]+"${args[@]}"})"

  if [ "$DRY_RUN" = 1 ]; then
    printf 'extra=%s\nkion-sizer' "${extra:-none}"
    printf ' %s' ${args[@]+"${args[@]}"}
    printf '\n'
    return 0
  fi

  bootstrap_uv "$extra"
  ( cd "$REPO" && uv run kion-sizer ${args[@]+"${args[@]}"} )
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main "$@"
fi

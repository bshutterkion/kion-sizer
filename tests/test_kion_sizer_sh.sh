#!/usr/bin/env bash
#
# test_kion_sizer_sh.sh — the general wrapper picks the right SDK extra from
# the location flag and passes every argument through unchanged.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../scripts/kion-sizer.sh"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     expected: %s\n     got:      %s\n' "$1" "$2" "$3"; }
expect() { # name expected args...
  local name="$1" want="$2"; shift 2
  local got; got="$(bash "$SCRIPT" --dry-run "$@" 2>/dev/null | tr '\n' '|')"
  [ "$got" = "$want" ] && ok "$name" || bad "$name" "$want" "$got"
}

echo "kion-sizer.sh wrapper tests:"
expect "local dir -> aws extra (parquet needs pyarrow)" \
  "extra=aws|kion-sizer --dir /data/cur --read-footers|" --dir /data/cur --read-footers
expect "s3 -> aws" "extra=aws|kion-sizer --s3 s3://b/p/ --json|" --s3 s3://b/p/ --json
expect "azure container -> azure" \
  "extra=azure|kion-sizer --azure-container https://a.blob.core.windows.net/c/p|" \
  --azure-container https://a.blob.core.windows.net/c/p
expect "gcp table, = form -> gcp" "extra=gcp|kion-sizer --gcp-table=p.d.t|" --gcp-table=p.d.t
expect "custom in s3 -> aws" "extra=aws|kion-sizer --custom-source s3://b/p/|" --custom-source s3://b/p/
expect "custom in blob -> azure" \
  "extra=azure|kion-sizer --custom-source https://a.blob.core.windows.net/c|" \
  --custom-source https://a.blob.core.windows.net/c
expect "oci -> no extra" "extra=none|kion-sizer --oci-bucket https://x|" --oci-bucket https://x
expect "arguments with spaces survive" "extra=aws|kion-sizer --dir /my dir/cur|" --dir "/my dir/cur"
expect "no location (e.g. --help) -> no extra" "extra=none|kion-sizer --help|" --help

echo "  ---- $PASS passed, $FAIL failed ----"
[ "$FAIL" -eq 0 ]

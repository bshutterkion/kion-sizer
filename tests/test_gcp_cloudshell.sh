#!/usr/bin/env bash
#
# test_gcp_cloudshell.sh — hermetic tests for scripts/gcp-cloudshell.sh export
# discovery. Fake `gcloud` and `bq` on PATH, driven by $SCENARIO; asserts on the
# `--dry-run` command. No real GCP, no uv.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../scripts/gcp-cloudshell.sh"
SHIMDIR="$(mktemp -d)"
trap 'rm -rf "$SHIMDIR"' EXIT

cat > "$SHIMDIR/gcloud" <<'FAKE'
#!/usr/bin/env bash
case "$1/$2" in
  config/get-value) echo "home-proj" ;;
  projects/list)
    case "$SCENARIO" in
      elsewhere|none) printf 'home-proj\nbilling-proj\nother-proj\n' ;;
      *) echo "home-proj" ;;
    esac ;;
  *) echo "fake gcloud: unhandled $*" >&2; exit 3 ;;
esac
FAKE

cat > "$SHIMDIR/bq" <<'FAKE'
#!/usr/bin/env bash
# bq ls --format=json --max_results=N --project_id=P   -> datasets of P
# bq ls --format=json --max_results=N P:DS              -> tables of P:DS
# bq show --format=json P:DS.T                          -> table metadata
cmd="$1"; shift
last="${@: -1}"
tables() { # project dataset name...
  local p="$1" d="$2"; shift 2
  printf '['
  local sep=""
  for t in "$@"; do
    printf '%s{"tableReference":{"projectId":"%s","datasetId":"%s","tableId":"%s"}}' "$sep" "$p" "$d" "$t"
    sep=","
  done
  printf ']\n'
}
case "$cmd" in
  ls)
    case "$last" in
      --project_id=*)
        p="${last#--project_id=}"
        case "$SCENARIO/$p" in
          home/home-proj|both/home-proj|override/home-proj) echo '[{"datasetReference":{"datasetId":"billing"}}]' ;;
          elsewhere/billing-proj) echo '[{"datasetReference":{"datasetId":"exports"}}]' ;;
          elsewhere/other-proj) echo 'BigQuery API has not been used in project other-proj' >&2; exit 1 ;;
          *) echo '[]' ;;
        esac ;;
      home-proj:billing)
        case "$SCENARIO" in
          home) tables home-proj billing gcp_billing_export_v1_AAA cloud_pricing_export unrelated ;;
          both|override) tables home-proj billing gcp_billing_export_v1_AAA gcp_billing_export_resource_v1_AAA gcp_billing_export_focus_AAA ;;
        esac ;;
      billing-proj:exports) tables billing-proj exports gcp_billing_export_v1_BBB gcp_billing_export_v1_CCC ;;
      *) echo '[]' ;;
    esac ;;
  show)
    case "$last" in
      *CCC) echo '{"numRows":"900"}' ;;
      *BBB) echo '{"numRows":"100"}' ;;
      *)    echo '{"numRows":"50"}' ;;
    esac ;;
  *) echo "fake bq: unhandled $cmd" >&2; exit 3 ;;
esac
FAKE
chmod +x "$SHIMDIR/gcloud" "$SHIMDIR/bq"

run() { PATH="$SHIMDIR:$PATH" SCENARIO="$1" bash "$SCRIPT" "${@:2}" 2>/dev/null; }

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     expected: %s\n     got:      %s\n' "$1" "$2" "$3"; }
expect() { # name scenario expected [extra args...]
  local name="$1" scen="$2" want="$3"; shift 3
  local got; got="$(run "$scen" --dry-run "$@")"
  [ "$got" = "$want" ] && ok "$name" || bad "$name" "$want" "$got"
}

echo "gcp-cloudshell.sh discovery tests:"

expect "standard export in the current project; pricing export ignored" home \
  "uv run kion-sizer --gcp-table home-proj.billing.gcp_billing_export_v1_AAA --target aws --cost"

expect "detailed preferred over standard and FOCUS" both \
  "uv run kion-sizer --gcp-table home-proj.billing.gcp_billing_export_resource_v1_AAA --target aws --cost"

expect "searches other projects; largest table within a kind wins" elsewhere \
  "uv run kion-sizer --gcp-table billing-proj.exports.gcp_billing_export_v1_CCC --target aws --cost"

expect "--table skips discovery" none \
  "uv run kion-sizer --gcp-table p.d.gcp_billing_export_v1_X --target aws --cost" \
  --table p.d.gcp_billing_export_v1_X

expect "month, accounts, json and target pass through" home \
  "uv run kion-sizer --gcp-table home-proj.billing.gcp_billing_export_v1_AAA --target azure --gcp-month 202606 --accounts 40 --json --cost" \
  --month 202606 --accounts 40 --json --target azure

got="$(run none --dry-run)"; rc=$?
{ [ "$rc" -ne 0 ] && [ -z "$got" ]; } && ok "no export anywhere -> error" \
  || bad "no export anywhere -> error" "nonzero exit, empty stdout" "rc=$rc out=$got"

echo "  ---- $PASS passed, $FAIL failed ----"
[ "$FAIL" -eq 0 ]

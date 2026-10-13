#!/usr/bin/env bash
#
# test_azure_cloudshell.sh — hermetic tests for scripts/azure-cloudshell.sh
# export discovery. A fake `az` on PATH, driven by $SCENARIO; asserts on the
# `--dry-run` command. No real Azure, no uv.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../scripts/azure-cloudshell.sh"
SHIMDIR="$(mktemp -d)"
trap 'rm -rf "$SHIMDIR"' EXIT

cat > "$SHIMDIR/az" <<'FAKE'
#!/usr/bin/env bash
args="$*"
export_json() { # name type account container root
  printf '{"name":"%s","properties":{"definition":{"type":"%s"},"deliveryInfo":{"destination":{"resourceId":"/subscriptions/s/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/%s","container":"%s","rootFolderPath":"%s"}}}}' "$@"
}
case "$args" in
  "cloud show --query endpoints.resourceManager -o tsv")
    [ "$SCENARIO" = gov ] && echo "https://management.usgovcloudapi.net/" || echo "https://management.azure.com/" ;;
  "cloud show --query suffixes.storageEndpoint -o tsv")
    [ "$SCENARIO" = gov ] && echo "core.usgovcloudapi.net" || echo "core.windows.net" ;;
  account\ list*) printf 'sub-1\nsub-2\n' ;;
  *billingAccounts\?api-version*)
    case "$SCENARIO" in
      ea|gov|mixed) echo "/providers/Microsoft.Billing/billingAccounts/12345" ;;
      *) : ;;
    esac ;;
  *billingAccounts/12345/providers/Microsoft.CostManagement/exports*)
    case "$SCENARIO" in
      ea|gov) printf '{"value":[%s]}' "$(export_json KionBillingExport ActualCost billacct billing exports)" ;;
      mixed)  printf '{"value":[%s,%s]}' \
                "$(export_json amortized AmortizedCost billacct billing amort)" \
                "$(export_json focus FocusCost billacct billing focus)" ;;
      *) echo '{"value":[]}' ;;
    esac ;;
  *subscriptions/sub-2/providers/Microsoft.CostManagement/exports*)
    case "$SCENARIO" in
      sub)   printf '{"value":[%s]}' "$(export_json daily Usage subacct costs "")" ;;
      mixed) printf '{"value":[%s,{"name":"sas","properties":{"definition":{"type":"ActualCost"},"deliveryInfo":{"destination":{"container":"c"}}}}]}' \
               "$(export_json actual ActualCost subacct costs root)" ;;
      *) echo '{"value":[]}' ;;
    esac ;;
  *CostManagement/exports*) echo '{"value":[]}' ;;
  *) echo "fake az: unhandled $args" >&2; exit 3 ;;
esac
FAKE
chmod +x "$SHIMDIR/az"

run() { PATH="$SHIMDIR:$PATH" SCENARIO="$1" bash "$SCRIPT" "${@:2}" 2>/dev/null; }

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     expected: %s\n     got:      %s\n' "$1" "$2" "$3"; }
expect() { # name scenario expected [extra args...]
  local name="$1" scen="$2" want="$3"; shift 3
  local got; got="$(run "$scen" --dry-run "$@")"
  [ "$got" = "$want" ] && ok "$name" || bad "$name" "$want" "$got"
}
expect_fail() { # name scenario [extra args...]
  local name="$1" scen="$2"; shift 2
  local got rc; got="$(run "$scen" --dry-run "$@")"; rc=$?
  { [ "$rc" -ne 0 ] && [ -z "$got" ]; } && ok "$name" \
    || bad "$name" "nonzero exit, empty stdout" "rc=$rc out=$got"
}

echo "azure-cloudshell.sh discovery tests:"

expect "EA export at the billing-account scope" ea \
  "uv run kion-sizer --azure-container https://billacct.blob.core.windows.net/billing/exports/KionBillingExport --target aws --detect-accounts --cost"

expect "Government cloud endpoints" gov \
  "uv run kion-sizer --azure-container https://billacct.blob.core.usgovcloudapi.net/billing/exports/KionBillingExport --target aws --detect-accounts --cost"

expect "subscription-scope export with no root folder" sub \
  "uv run kion-sizer --azure-container https://subacct.blob.core.windows.net/costs/daily --target aws --detect-accounts --cost"

expect "actual cost preferred over FOCUS and amortized; SAS destinations skipped" mixed \
  "uv run kion-sizer --azure-container https://subacct.blob.core.windows.net/costs/root/actual --target aws --detect-accounts --cost"

expect "--export picks a named export" mixed \
  "uv run kion-sizer --azure-container https://billacct.blob.core.windows.net/billing/focus/focus --target aws --detect-accounts --cost" \
  --export focus

expect "--container skips discovery" none \
  "uv run kion-sizer --azure-container https://a.blob.core.windows.net/c/p --target aws --detect-accounts --cost" \
  --container https://a.blob.core.windows.net/c/p

expect "month, accounts, json and target pass through" ea \
  "uv run kion-sizer --azure-container https://billacct.blob.core.windows.net/billing/exports/KionBillingExport --target azure --azure-month 202606 --accounts 12 --json --detect-accounts --cost" \
  --month 202606 --accounts 12 --json --target azure

expect_fail "no export anywhere -> error" none
expect_fail "unknown --export name -> error" ea --export nope

echo "  ---- $PASS passed, $FAIL failed ----"
[ "$FAIL" -eq 0 ]

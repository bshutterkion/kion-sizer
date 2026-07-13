#!/usr/bin/env bash
#
# run_env_test.sh — environment-fidelity check, meant to run INSIDE an
# amazonlinux:2023 container (AWS CloudShell's OS). Proves the whole runtime
# chain works on a clean AL2023 box:
#   * cloudshell.sh's bootstrap_uv installs uv and provisions Python 3.12,
#   * the pyarrow + pyyaml + boto3 wheels actually install there,
#   * CUR discovery parses real-shaped AWS output using the system python3,
#   * the full script executes end-to-end (--dry-run), and
#   * `uv run kion-sizer` produces a recommendation.
#
# No real AWS: a fake `aws` is put on PATH. The sizing step runs against a local
# --dir fixture (the S3 path is covered hermetically by the Python + shell unit
# tests). Exits nonzero on the first failure.

set -euo pipefail
REPO="${1:-/work}"
cd "$REPO"

step() { printf '\n=== %s ===\n' "$*"; }

# --- fake aws (discovery only; legacy hourly parquet, two months) -----------
SHIM="$(mktemp -d)"
cat > "$SHIM/aws" <<'FAKE'
#!/usr/bin/env bash
case "$1/${2:-}" in
  cur/describe-report-definitions)
    echo '{"ReportDefinitions":[{"ReportName":"hourly-cur","TimeUnit":"HOURLY","Format":"Parquet","S3Bucket":"cur-bucket","S3Prefix":"reports"}]}' ;;
  s3/ls)
    if [[ "${3:-}" == s3://* ]]; then
      echo "2026-06-01 00:00:00    100 reports/hourly-cur/yearMonth=202605/a.parquet"
      echo "2026-07-01 00:00:00    900 reports/hourly-cur/yearMonth=202606/b.parquet"
    fi ;;
  *) exit 0 ;;
esac
FAKE
chmod +x "$SHIM/aws"
export PATH="$SHIM:$PATH"

step "OS identity (expect Amazon Linux 2023)"
grep PRETTY_NAME /etc/os-release || true
echo "system python3: $(python3 --version 2>&1)"

step "discovery parses on AL2023 system python3"
# shellcheck disable=SC1091
source scripts/cloudshell.sh
out="$(discover_cur)"
echo "$out"
echo "$out" | grep -q 'S3_URI=s3://cur-bucket/reports/hourly-cur/yearMonth=202606/' \
  || { echo "FAIL: discovery did not resolve peak month"; exit 1; }

step "full script executes end-to-end (--dry-run)"
got="$(bash scripts/cloudshell.sh --dry-run --accounts 150)"
echo "$got"
[ "$got" = "uv run kion-sizer --s3 s3://cur-bucket/reports/hourly-cur/yearMonth=202606/ --granularity hourly --accounts 150" ] \
  || { echo "FAIL: unexpected resolved command"; exit 1; }

step "bootstrap_uv: install uv + provision Python (>=3.12) + deps"
bootstrap_uv
export PATH="$HOME/.local/bin:$PATH"
uv run python -c 'import sys; assert sys.version_info[:2]>=(3,12), sys.version; print("python", sys.version.split()[0])'
uv run python -c 'import pyarrow, yaml, boto3; print("pyarrow", pyarrow.__version__, "| pyyaml + boto3 OK")'

step "kion-sizer runs and produces a recommendation"
mkdir -p /tmp/fix
python3 -c "open('/tmp/fix/x.parquet','wb').truncate(1<<30)"   # 1 GiB sparse parquet
rec="$(uv run kion-sizer --dir /tmp/fix --accounts 150)"
echo "$rec"
echo "$rec" | grep -q 'kion-sizer recommendation' || { echo "FAIL: no recommendation header"; exit 1; }
echo "$rec" | grep -q 'RDS:' || { echo "FAIL: no RDS line"; exit 1; }
echo "$rec" | grep -q 'financials-poller ECS task' || { echo "FAIL: no poller line"; exit 1; }

step "JSON output is valid + carries the neutral calibration marker, no caveat"
js="$(uv run kion-sizer --dir /tmp/fix --accounts 150 --json)"
echo "$js" | uv run python -c 'import sys, json
d = json.load(sys.stdin)
assert d["calibration_version"] == "reference-cur-2026-03 / aws-parquet", d["calibration_version"]
assert "caveat" not in d
print("valid JSON; neutral calibration; no caveat")'

printf '\n*** ENV FIDELITY PASSED on %s ***\n' "$(uname -m)"

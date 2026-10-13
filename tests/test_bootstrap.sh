#!/usr/bin/env bash
#
# test_bootstrap.sh — every script's bootstrap_uv stays quiet on success and,
# on failure, replays what uv or the installer printed instead of a bare
# "failed". Sources each script (which defines its functions without running
# main) against a fake `uv` / `curl` on PATH. No network, no real uv.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS=(cloudshell.sh azure-cloudshell.sh gcp-cloudshell.sh kion-sizer.sh)
SHIMDIR="$(mktemp -d)"; NOUV="$(mktemp -d)"; FAKEHOME="$(mktemp -d)"
trap 'rm -rf "$SHIMDIR" "$NOUV" "$FAKEHOME"' EXIT

cat > "$SHIMDIR/uv" <<'FAKE'
#!/usr/bin/env bash
case "$UV_MODE" in
  fail) echo "  x No solution found when resolving dependencies: boom-conflict" >&2; exit 1 ;;
  ok)   echo "Resolved 30 packages in 12ms" >&2; exit 0 ;;
esac
FAKE
cat > "$NOUV/curl" <<'FAKE'
#!/usr/bin/env bash
echo "curl: (6) Could not resolve host: astral.sh" >&2; exit 6
FAKE
chmod +x "$SHIMDIR/uv" "$NOUV/curl"
BASE_PATH="/usr/bin:/bin"   # no real uv here

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "$2"; }

# Sets $rc and $err. Not called in $(...): a subshell would lose $rc.
boot() { # script path-prefix uv-mode
  local errfile="$FAKEHOME/stderr"
  HOME="$FAKEHOME" PATH="$2:$BASE_PATH" UV_MODE="$3" \
    bash -c 'source "$1"; bootstrap_uv aws' _ "$HERE/../scripts/$1" >/dev/null 2>"$errfile"
  rc=$?
  err="$(cat "$errfile")"
}

echo "bootstrap diagnostics tests:"
for s in "${SCRIPTS[@]}"; do
  boot "$s" "$SHIMDIR" fail
  if [ "$rc" -ne 0 ] && [[ "$err" == *boom-conflict* ]] && [[ "$err" == *"uv sync failed"* ]]; then
    ok "$s: failed sync replays uv's own error"
  else bad "$s: failed sync replays uv's own error" "rc=$rc err=$err"; fi

  boot "$s" "$SHIMDIR" ok
  if [ "$rc" -eq 0 ] && [[ "$err" != *Resolved* ]]; then
    ok "$s: successful sync stays quiet"
  else bad "$s: successful sync stays quiet" "rc=$rc err=$err"; fi

  boot "$s" "$NOUV" ok
  if [ "$rc" -ne 0 ] && [[ "$err" == *"Could not resolve host"* ]] && [[ "$err" == *"uv install failed"* ]]; then
    ok "$s: failed install replays the installer's error"
  else bad "$s: failed install replays the installer's error" "rc=$rc err=$err"; fi
done

echo "  ---- $PASS passed, $FAIL failed ----"
[ "$FAIL" -eq 0 ]

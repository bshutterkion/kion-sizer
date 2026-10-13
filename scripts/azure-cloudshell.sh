#!/usr/bin/env bash
#
# azure-cloudshell.sh — size a Kion deployment from an Azure Cost Management
# export, in Azure Cloud Shell.
#
# Run it inside a Cloud Shell session signed in as someone who can list the
# Cost Management exports and read the storage container they write to. It
# finds the export itself, so no storage URL is needed in the common case.
# Commercial, Government and China clouds all work: endpoints come from
# `az cloud show`, so whichever cloud the session is set to is the one searched.
#
# What it does, in order:
#   1. Discover the export, in two tiers:
#      a. Cost Management exports at every billing account and every enabled
#         subscription you can see. When several are found it prefers
#         actual-cost/usage exports, then FOCUS, then amortized, and logs every
#         candidate. If Kion ingests a different export, name it with --export.
#      b. If none are visible — common, because EA/MCA exports are defined at
#         the enrollment scope and an operator with storage access often cannot
#         see it — storage accounts with billing-like names are searched for
#         blobs laid out the way Cost Management writes them, largest wins.
#      Skip discovery entirely with --container.
#   2. Check blob access. The tool reads as your identity, which needs Storage
#      Blob Data Reader. If you lack it but can read the account key
#      (Contributor), a 2-hour read/list SAS token for that one container is
#      minted and handed to the tool through KION_SIZER_AZURE_SAS — never on
#      the command line. --no-sas turns this off.
#   3. Bootstrap the runtime — installs `uv` if missing and syncs only the Azure
#      extra.
#   4. Size it — runs `kion-sizer --azure-container <url> --target aws
#      --detect-accounts --cost`. The tool picks the peak period and counts only
#      the newest copy of it (exports accumulate one copy per run). --cost uses
#      the embedded AWS price snapshot, since there are no AWS credentials here.
#
# Usage:
#   bash scripts/azure-cloudshell.sh                     # discover + size
#   bash scripts/azure-cloudshell.sh --accounts 150      # override the subscription count
#   bash scripts/azure-cloudshell.sh --json              # machine-readable output
#   bash scripts/azure-cloudshell.sh --export NAME       # pick this export by name
#   bash scripts/azure-cloudshell.sh --container URL     # skip discovery
#   bash scripts/azure-cloudshell.sh --month YYYYMM      # size this billing period
#   bash scripts/azure-cloudshell.sh --target azure      # size an Azure deployment
#   bash scripts/azure-cloudshell.sh --no-sas            # never fall back to a key-minted SAS
#   bash scripts/azure-cloudshell.sh --dry-run           # print the command only
#
# Requires (all preinstalled in Cloud Shell): az, python3, curl.
# Permissions: Cost Management Reader (or billing reader) at the export's scope
# to discover it, and Storage Blob Data Reader on the container to read it —
# or Contributor on the storage account, for the SAS fallback. Owner/Contributor
# alone does NOT grant blob data access to your identity.

set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf '%s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

EXPORTS_API="2023-07-01-preview"
BILLING_API="2020-05-01"

# --- export discovery -------------------------------------------------------
# Candidates are printed one per line as
#   "<rank>\t<name>\t<type>\t<storage-account>\t<container>\t<root-folder>"
# rank 0 = ActualCost/Usage, 1 = FocusCost, 2 = AmortizedCost, 3 = anything else.

# Every scope that can hold an export: billing accounts, then subscriptions.
_scopes() {
  local arm="$1"
  az rest --method get \
    --url "${arm}providers/Microsoft.Billing/billingAccounts?api-version=${BILLING_API}" \
    --query 'value[].id' -o tsv 2>/dev/null
  az account list --query "[?state=='Enabled'].id" -o tsv 2>/dev/null \
    | sed 's#^#/subscriptions/#; s#^/subscriptions//subscriptions/#/subscriptions/#'
}

# Echo the candidate lines for the exports defined at one scope.
_exports_at() {
  local arm="$1" scope="$2"
  az rest --method get \
    --url "${arm}${scope#/}/providers/Microsoft.CostManagement/exports?api-version=${EXPORTS_API}" \
    -o json 2>/dev/null | python3 -c '
import sys, json
try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
rank = {"actualcost": 0, "usage": 0, "focuscost": 1, "amortizedcost": 2}
for e in data.get("value", []) or []:
    props = e.get("properties", {}) or {}
    dest = (props.get("deliveryInfo", {}) or {}).get("destination", {}) or {}
    rid = dest.get("resourceId", "") or ""
    acct = rid.rstrip("/").rsplit("/", 1)[-1] if "/storageAccounts/" in rid else ""
    if not acct or not dest.get("container"):
        continue  # SAS-token destinations carry no storage account we can name
    kind = (props.get("definition", {}) or {}).get("type", "") or ""
    print("\t".join([
        str(rank.get(kind.lower(), 3)),
        e.get("name", ""),
        kind,
        acct,
        dest["container"],
        (dest.get("rootFolderPath", "") or "").strip("/"),
    ]))
' 2>/dev/null
}

# Echo "CONTAINER_URL=<url>" for the preferred export, logging every candidate.
discover_export() {
  local want="$1" arm suffix scope found="" line
  arm="$(az cloud show --query endpoints.resourceManager -o tsv 2>/dev/null)"
  suffix="$(az cloud show --query suffixes.storageEndpoint -o tsv 2>/dev/null)"
  [ -n "$arm" ] && [ -n "$suffix" ] || return 1
  case "$arm" in */) ;; *) arm="$arm/" ;; esac

  for scope in $(_scopes "$arm"); do
    line="$(_exports_at "$arm" "$scope")"
    [ -n "$line" ] && found="${found:+$found
}$line"
  done
  [ -n "$found" ] || return 1

  if [ -n "$want" ]; then
    found="$(printf '%s\n' "$found" | awk -F'\t' -v n="$want" '$2 == n')"
    [ -n "$found" ] || { log "# no export named '$want'"; return 1; }
  fi

  found="$(printf '%s\n' "$found" | sort -t $'\t' -k1,1n -k2,2 | uniq)"
  log "# Cost Management export candidates:"
  printf '%s\n' "$found" | while IFS=$'\t' read -r rank name kind acct container root; do
    log "#   $name  ($kind)  -> $acct/$container${root:+/$root}"
  done

  IFS=$'\t' read -r rank name kind acct container root <<< "$(printf '%s\n' "$found" | head -1)"
  [ -n "$acct" ] || return 1
  # Azure writes under <root>/<export-name>/<period>/…; scoping to the export
  # name keeps two exports sharing a root folder from being blended.
  printf 'CONTAINER_URL=https://%s.blob.%s/%s/%s%s\n' \
    "$acct" "$suffix" "$container" "${root:+$root/}" "$name"
}

# Run an `az storage` data-plane command as yourself, falling back to the
# account key (which needs Contributor, not a data role).
_az_storage() {
  az storage "$@" --auth-mode login 2>/dev/null \
    || az storage "$@" --auth-mode key 2>/dev/null
}

# Tier b: storage accounts with billing-like names, searched for export-shaped
# period folders. Echo "CONTAINER_URL=<endpoint><container>/<prefix>" for the
# prefix holding the most bytes under a period folder.
discover_by_storage() {
  local acct endpoint container best="" cand
  while IFS=$'\t' read -r acct endpoint; do
    [ -n "$acct" ] || continue
    for container in $(_az_storage container list --account-name "$acct" --query '[].name' -o tsv); do
      cand="$(_az_storage blob list --account-name "$acct" -c "$container" --num-results 5000 \
                --query '[].[name, properties.contentLength]' -o tsv \
              | EP="$endpoint" C="$container" python3 -c '
import sys, os, re, collections
# Only the two layouts Cost Management itself writes; a period folder alone is
# not enough, because tools that emit export-shaped output copy that much.
#   unpartitioned: <pre>/<export>/<period>/<export>_<uuid>.csv[.gz]
#   partitioned:   <pre>/<period>/<uuid>/manifest.json | part_*
uuid = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
layouts = (
    re.compile(r"^(?P<pre>(?:.*/)?(?P<exp>[^/]+))/\d{8}-\d{8}/(?P=exp)_" + uuid
               + r"\.csv(?:\.gz)?$", re.I),
    re.compile(r"^(?P<pre>.*?)/?\d{8}-\d{8}/" + uuid
               + r"/(?:manifest\.json|part_[^/]+)$", re.I),
)
sizes = collections.Counter()
for line in sys.stdin:
    parts = line.rstrip("\n").split("\t")
    if len(parts) != 2:
        continue
    name, size = parts
    for pat in layouts:
        m = pat.match(name)
        if m:
            try:
                sizes[m.group("pre")] += int(size)
            except ValueError:
                pass
            break
if sizes:
    prefix, total = sizes.most_common(1)[0]
    url = "%s%s" % (os.environ["EP"], os.environ["C"]) + ("/" + prefix if prefix else "")
    print("%d\t%s" % (total, url))
' 2>/dev/null)"
      [ -n "$cand" ] && best="${best:+$best
}$cand"
    done
  done < <(az storage account list --query '[].[name, primaryEndpoints.blob]' -o tsv 2>/dev/null \
             | grep -iE '^[^[:space:]]*(bill|cost|export|focus|usage)')
  [ -n "$best" ] || return 1
  best="$(printf '%s\n' "$best" | sort -t $'\t' -k1,1nr)"
  log "# storage locations holding export-shaped period folders:"
  printf '%s\n' "$best" | while IFS=$'\t' read -r total url; do
    log "#   $url  ($total bytes)"
  done
  printf 'CONTAINER_URL=%s\n' "$(printf '%s\n' "$best" | head -1 | cut -f2)"
}

# Make sure the tool will be able to read the container. Echo nothing when your
# identity already can; otherwise echo "SAS=<token>" minted from the account key.
# Returns nonzero when neither works.
ensure_blob_access() {
  local url="$1" allow_sas="$2" rest acct container expiry sas
  rest="${url#https://}"
  acct="${rest%%.*}"
  rest="${rest#*/}"
  container="${rest%%/*}"
  if az storage blob list --account-name "$acct" -c "$container" --num-results 1 \
       --auth-mode login -o none 2>/dev/null; then
    log "# blob access: your identity can read $acct/$container"
    return 0
  fi
  [ "$allow_sas" = 1 ] || return 1
  expiry="$(python3 -c 'import datetime as d; print((d.datetime.now(d.timezone.utc)+d.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%MZ"))')"
  sas="$(az storage container generate-sas --account-name "$acct" --name "$container" \
           --permissions rl --expiry "$expiry" --https-only --auth-mode key -o tsv 2>/dev/null)"
  [ -n "$sas" ] || return 1
  log "# blob access: no data role on $acct/$container; using a read/list SAS from the account key (expires $expiry)"
  printf 'SAS=%q\n' "$sas"  # %q: a SAS is full of & and =, unsafe to eval raw
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
  log "# syncing runtime (uv sync --extra azure)…"
  ( cd "$REPO" && _quiet uv sync --extra azure ) \
    || die "uv sync failed (output above)"
}

# --- main -------------------------------------------------------------------
main() {
  local ACCOUNTS="" AS_JSON=0 CONTAINER_URL="" EXPORT="" MONTH="" TARGET="aws"
  local DRY_RUN=0 ALLOW_SAS=1
  while [ $# -gt 0 ]; do
    case "$1" in
      --accounts)    ACCOUNTS="${2:-}"; shift 2 ;;
      --accounts=*)  ACCOUNTS="${1#*=}"; shift ;;
      --json)        AS_JSON=1; shift ;;
      --container)   CONTAINER_URL="${2:-}"; shift 2 ;;
      --container=*) CONTAINER_URL="${1#*=}"; shift ;;
      --export)      EXPORT="${2:-}"; shift 2 ;;
      --export=*)    EXPORT="${1#*=}"; shift ;;
      --month)       MONTH="${2:-}"; shift 2 ;;
      --month=*)     MONTH="${1#*=}"; shift ;;
      --target)      TARGET="${2:-}"; shift 2 ;;
      --target=*)    TARGET="${1#*=}"; shift ;;
      --no-sas)      ALLOW_SAS=0; shift ;;
      --dry-run)     DRY_RUN=1; shift ;;
      -h|--help)     grep '^#' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; return 0 ;;
      *) echo "unknown arg: $1" >&2; return 2 ;;
    esac
  done

  if [ -z "$CONTAINER_URL" ]; then
    local out
    if out="$(discover_export "$EXPORT")"; then
      log "# discovered export via Cost Management"
    elif [ -z "$EXPORT" ] && out="$(discover_by_storage)"; then
      log "# discovered export via storage-account search (no export definition visible)"
    else
      if [ -n "$EXPORT" ]; then
        die "no Cost Management export named '$EXPORT' at any billing account or subscription you can see. Pass --container https://<account>.blob.<suffix>/<container>/<path>."
      fi
      die "no Cost Management export visible at any billing account or subscription, and no billing-like storage account holds export-shaped folders. Pass --container https://<account>.blob.<suffix>/<container>/<path>."
    fi
    eval "$out"   # sets CONTAINER_URL
    [ -n "$CONTAINER_URL" ] || die "export discovery returned no location"
    log "# sizing export at: $CONTAINER_URL"
  fi

  local ARGS=(--azure-container "$CONTAINER_URL" --target "$TARGET")
  [ -n "$MONTH" ] && ARGS+=(--azure-month "$MONTH")
  [ -n "$ACCOUNTS" ] && ARGS+=(--accounts "$ACCOUNTS")
  [ "$AS_JSON" = 1 ] && ARGS+=(--json)
  # --detect-accounts: the subscription count, from the export's own rows.
  # --cost: prices from the embedded snapshot (no AWS credentials here; the
  # output says so).
  ARGS+=(--detect-accounts --cost)

  if [ "$DRY_RUN" = 1 ]; then
    printf 'uv run kion-sizer'
    printf ' %s' "${ARGS[@]}"
    printf '\n'
    return 0
  fi

  local SAS="" access
  access="$(ensure_blob_access "$CONTAINER_URL" "$ALLOW_SAS")" \
    || die "cannot read $CONTAINER_URL: you need Storage Blob Data Reader on the container, or Contributor on the storage account for the SAS fallback (disabled by --no-sas)."
  [ -n "$access" ] && eval "$access"   # sets SAS when one was minted

  bootstrap_uv
  log "# sizing…"
  ( cd "$REPO" && KION_SIZER_AZURE_SAS="$SAS" uv run kion-sizer "${ARGS[@]}" )
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main "$@"
fi

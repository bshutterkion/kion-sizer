# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`kion-sizer` reads a customer's cloud billing data and recommends starting sizing
for a Kion deployment: the database instance class, the financials-poller compute
shape, coarse per-service bands, and (with `--cost`) a monthly cost estimate.

It profiles every bulk billing source Kion ingests (AWS, Azure, GCP, OCI, custom
FOCUS) and sizes an AWS deployment; an Azure target is designed but not built.
Primary distribution is each cloud's own shell: `scripts/cloudshell.sh` (AWS),
`scripts/azure-cloudshell.sh` and `scripts/gcp-cloudshell.sh`, with
`scripts/kion-sizer.sh` as a pass-through for everything else (a local CUR
directory, OCI, custom sources, explicit locations).

**User-facing instructions are always a bash script, never `uv`.** README and
any external docs tell people to clone and run a script; `uv` is an
implementation detail the scripts bootstrap. Only the Development section, which
is for contributors, uses `uv` directly.

**This repository is published publicly.** Design documents, calibration
provenance naming real accounts, and references to Kion's internal source live
in the internal twin repo, not here. See "Staying publishable" below.

## Commands

```sh
uv sync --extra dev                      # install deps (uv is the only runtime manager)
uv run pytest -q                         # full suite (unit + golden + shell discovery)
uv run pytest tests/test_model.py -q     # one file
uv run pytest -k fargate -q              # one test / pattern
uv run kion-sizer --dir path/ --json     # run the tool locally

bash tests/test_cloudshell.sh            # cloudshell.sh discovery tests standalone (also run under pytest)
bash tests/test_azure_cloudshell.sh      # azure-cloudshell.sh, same pattern with a fake `az`
bash tests/test_gcp_cloudshell.sh        # gcp-cloudshell.sh, fake `gcloud` + `bq`
bash tests/test_kion_sizer_sh.sh         # kion-sizer.sh wrapper: extra inference, arg passthrough
bash tests/env/run.sh                    # AL2023 container fidelity test (needs Docker; opt-in)
RUN_ENV_TEST=1 uv run pytest tests/test_env_fidelity.py   # same test via pytest
```

Regenerate golden files after an **intentional** output/calibration change, then
eyeball the diff (they are human-readable and committed):

```sh
KION_SIZER_UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py
```

## Architecture

Pipeline, in `cli.run()`: **source → (target: tiers / pricing) → model → render**.

The tool has **two independent axes**: `--source` (where the billing data comes
from) and `--target` (where Kion is deployed). Any pairing is valid — profiling
an Azure export while sizing an AWS deployment is a real case. **The axes are not symmetric.** Kion is deployed on AWS and Azure only — there is
no GCP deployment target. GCP is a billing *source* only, for customers whose
spend is in GCP but whose Kion runs on AWS or Azure, so `--target gcp` exits 2
with a message pointing at `--source gcp`. Implemented today: every source,
and the `aws` target; `--target azure` reports "not yet implemented".

| Module | Role |
|---|---|
| `sources/base.py` | Cloud-agnostic profiling machinery: the `_CsvRec` abstraction, stratified sampling, parallel scan, account detection, parquet footer/column helpers. Produces a `BillingProfile`. |
| `sources/aws.py` | `from_dir` / `from_s3` — the dir and S3 record factories. |
| `sources/oci.py` | OCI cost reports in Object Storage; compartments count as member accounts. |
| `sources/schema.py` | Cross-cloud column identity. FOCUS names are identical whoever emitted them. |
| `sources/azure.py` | Cost Management exports: manifest-driven, one run folder per period, FOCUS or usage-details. |
| `sources/custom.py` | FOCUS files, delegating to the S3 or Azure blob backend. |
| `sources/gcp.py` | BigQuery billing export. Metadata + aggregate queries only; exact rows and accounts, no scanning. |
| `sources/__init__.py` | `load(cloud, location, **kw)` — the source registry. |
| `targets/base.py` | `TargetAdapter` protocol and `ComputeShape`. |
| `targets/aws.py` | `AWSTarget`: orderable-DB-class lookup, Fargate task snapping, pricing delegation. |
| `targets/__init__.py` | `get(cloud)` — the target registry. |
| `config.py` | Shared Kion-side constants plus per-source calibration and per-target tier ladders, validated on parse. |
| `pricing.py` | AWS Pricing API lookup, with the embedded `prices.json` snapshot as fallback. |
| `model.py` | **Pure** — `recommend(profile, config, accounts, target)` and `cost(rec, prices)`. No I/O, no cloud imports. |
| `render.py` | Text and JSON formatting of a `Recommendation`. |
| `progress.py` | tqdm bars, stderr only. |

One invariant holds this shape together: **`model.py` stays I/O-free.** Every
cloud call lives behind a source module or a `TargetAdapter`, which is why those
are all testable with injected fake clients (`FakeRDS`, `FakePricing`,
`FakeS3Client`, `FakeLister` in `tests/`). Adding a boto3 import to `model.py`
breaks the design — and so does calling a target adapter's concrete class
directly instead of going through the protocol.

**Adding a cloud** is therefore one new file under `sources/` or `targets/` plus
a registry entry. A source produces a `BillingProfile`; a target implements
`db_tiers`, `snap_compute`, and `price_table`. `ComputeShape` is deliberately
general: on AWS it is a Fargate task, and on a Kubernetes target it is a pod plus
the node pool that can schedule it.

### Graceful degradation is deliberate

Every optional AWS-backed enhancement must degrade instead of failing the run:
`--rds-from-aws` falls back to the built-in `default.yaml` tiers, `--cost` falls
back to the embedded us-east-1 snapshot, footer reads and account detection warn
and fall back to the bytes-only estimate. The broad `except Exception` blocks
carry `# noqa: BLE001` plus a reason comment; keep that pattern. The provenance
of what actually happened is surfaced in the output (`db_tier_source`,
`cost.source`, `sample_note`, `account_source`) — a fallback must stay visible to
the user, never silent.

Timeouts matter for the same reason: AWS clients in `targets/aws.py` and
`pricing.py` set explicit connect/read timeouts and bounded retries so a slow
account can't hang the tool. `targets/aws.py` probes a curated candidate ladder
with targeted orderability queries rather than enumerating the full catalog (a
previous full enumeration hung).

### Row counting

GCP is exact by construction — `INFORMATION_SCHEMA.PARTITIONS` plus a
`GROUP BY invoice.month` give rows, bytes, and `COUNT(DISTINCT project.id)`
without reading row data, so the file-scan flags (`--read-footers`,
`--all-files`, `--sample`) are warned about and ignored for that source.

For file-based sources, three paths in decreasing accuracy: parquet footers (exact, reads footer bytes
only), full CSV read (`--all-files`, exact), stratified CSV sample (default —
files spread across the size distribution, extrapolated by byte share; account
counts from a sample are a **lower bound** and labeled as such). CSV files are
read exactly once even when both rows and account IDs are needed, in a bounded
thread pool (`_CSV_SCAN_WORKERS`) so buffered gzip can't exhaust CloudShell RAM.

### stdout / stderr split

Progress bars, warnings, and `cloudshell.sh` logs go to **stderr**; only the
recommendation goes to stdout, so `--json` stays pipeable. `progress.py`
auto-disables when stderr isn't a TTY (tests and CI stay silent);
`KION_SIZER_NO_PROGRESS` forces it off.

### `cli.run(args, out)`

Takes an argv list and a writer, returns an exit code (`0` ok, `1` runtime error,
`2` usage) — tests call it directly with a `StringIO`. `main()` only wires it to
`sys.argv`/`sys.stdout`. Keep new code inside `run()` rather than in `main()`.

### `scripts/cloudshell.sh`

Runnable *and* sourceable — sourcing defines `bootstrap_uv`, `discover_cur`,
`_peak_month_uri`, etc. without executing `main`, which is how the hermetic tests
drive it. Discovery is three tiers (legacy `cur:DescribeReportDefinitions` → BCM
Data Exports → bucket-name heuristic), each echoing `GRAN=` / `S3_URI=` lines
because they run inside command substitution (a variable set in a subshell would
not survive). It passes `--rds-from-aws --cost --read-footers --detect-accounts`
by default. Its tests (`tests/test_cloudshell.sh`) shim a fake `aws` on `PATH`
and assert on `--dry-run` output — no real AWS, no uv, no Docker.

### `scripts/azure-cloudshell.sh` and `scripts/gcp-cloudshell.sh`

Same shape as `cloudshell.sh`: runnable and sourceable, discovery functions echo
`KEY=value` lines for `eval`, `--dry-run` prints the resolved command, and each
syncs only its own extra (`uv sync --extra azure` / `--extra gcp`). Neither shell
has AWS credentials, so both pass `--cost` (embedded snapshot) but not
`--rds-from-aws`.

Azure discovery is two tiers: Cost Management exports listed through `az rest` at
billing-account and subscription scopes, then — because EA/MCA exports usually
live at an enrollment scope the operator cannot see — storage accounts with
billing-like names, matched on the two blob layouts Cost Management itself
writes (`<export>/<period>/<export>_<uuid>.csv`, and `<period>/<uuid>/manifest.json`
+ `part_*`). A bare period folder is not enough: FOCUS-conversion tooling emits
export-shaped folders too, and the largest such location in a real tenant was one.
When the operator's identity has no blob data role, the script mints a short
read/list SAS from the account key and passes it via `KION_SIZER_AZURE_SAS`,
which `sources/azure.py` prefers over `DefaultAzureCredential`. The SAS is
`printf %q`-quoted before `eval` — it is full of `&`.

GCP discovery walks `bq ls` over the current project, then every listable project
(capped by `--max-projects`), and ranks detailed > standard > FOCUS, then rows.

## Billing sources

Kion ingests seven billing sources, and the report
format is **orthogonal** to the source: `UseFOCUSReports` and
`UseProprietaryReports` are independent flags, FOCUS is tried first, and both can
be set. So a source is a **(provider, report format)** pair.

| Provider | Proprietary | FOCUS | Sizing |
|---|---|---|---|
| AWS | CUR, DBR | yes | ✅ all three |
| Azure | billing export (EA/MCA) | yes | ✅ both |
| GCP | standard, detailed | yes | ✅ all three |
| OCI | Object Storage `.csv.gz` | yes | ✅ both |
| Custom | none — FOCUS only | yes | ✅ S3 and Azure blob |
| Anthropic, OpenAI | API | no | excluded — no bulk export, negligible rows |

FOCUS columns are identical across providers, so column resolution lives in
`sources/schema.py`, not per-cloud. Calibration resolves most-specific-first —
`sources["<provider>/<format>"]` → `sources["<provider>"]` → `sources["default"]`
— so only measured pairs need an entry. All eleven bulk pairs are implemented; three are *measured* (`aws/cur`,
`gcp/standard`, `gcp/detailed`).

A ratio can be measured without touching a Kion database: the aggregation is a
deterministic collapse on a known hash key, so replicating that key against the
billing data (one `COUNT(DISTINCT …)` in BigQuery) gives the same count Kion
would store. That is how the two GCP pairs were calibrated, and it is the
preferred way to calibrate a new pair.
Everything else inherits `default` and every such run says UNCALIBRATED out loud,
naming the pair.

Two format quirks are load-bearing rather than cosmetic. **AWS DBR** files carry
`AccountTotal` / `InvoiceTotal` / `StatementTotal` / `Rounding` rows that Kion
does not ingest; counting them would inflate the estimate,
so `dbr_row_filter` excludes exactly those. **Azure exports** leave several
run-id folders per period because overwrite is on by default; only the newest is
counted, or the estimate multiplies by the number of runs.

## Calibration

`src/kion_sizer/default.yaml` holds the constants derived from a reference CUR,
split into shared Kion-side values, a `sources.<cloud>` block per billing source,
and a `targets.<cloud>` block per deployment target. A source block carries
`inherited: true` when its numbers were copied from the AWS reference rather than
measured — the output then warns that the estimate is uncalibrated. Legacy flat
configs (top-level `rds_tiers`, `rows_per_gib`, …) still load and map onto
`sources.aws` / `targets.aws`. `calibration_version` is stamped into every run. Changing any constant changes
golden output — regenerate goldens and review the diff. Output must stay
customer-neutral: `test_golden.py::test_output_is_neutral` asserts the neutral
calibration marker and the absence of a caveat block.

`src/kion_sizer/prices.json` is the embedded us-east-1 price/spec snapshot (also
the source of EC2 vcpu/mem/arch specs even on the live path, so selection needs
no `describe-instance-types` call — the only IAM surface `--cost` adds is
`pricing:GetProducts`). Refresh it from the AWS Pricing API when it goes stale.

## Staying publishable

This repo is mirrored to a public GitHub remote, so everything in it is
effectively published. Its internal twin holds what cannot be.

Keep out of this repo:

- **Design docs and plans.** They cite internal repositories and unfixed
  findings in Kion's deployment tooling. They live in the internal twin under
  `docs/superpowers/`.
- **References into Kion's private source or tracker** — a Go file-and-line
  citation, a portal domain type, an internal issue number. Describe the
  *behaviour* being matched instead; that is what a reader of this repo needs
  anyway, and it does not rot when the private source moves.
- **Real account, tenant or storage identifiers**, including in comments.

The last one bites hardest in `default.yaml`, because a `calibration:` marker is
printed in the `calibration` field of **every run**. A marker naming the account
it was measured against would show one customer another customer's billing
account id. Markers follow `reference-<what>-<YYYY-MM> / <pair>`: dated and
specific, never identifying.

`tests/test_publishable.py` enforces all of this — a tree-wide scan plus a check
of every calibration marker. It is the guard to extend when a new category of
internal reference shows up, rather than something to add an exemption to.

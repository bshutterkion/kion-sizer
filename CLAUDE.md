# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`kion-sizer` reads a customer's AWS Cost and Usage Report (CUR) — local dir or S3 —
and recommends starting AWS sizing for a Kion deployment: the RDS instance class,
the financials-poller ECS/Fargate task, coarse per-service bands, and (with
`--cost`) a monthly cost estimate. Primary distribution is AWS CloudShell via
`scripts/cloudshell.sh`.

## Commands

```sh
uv sync --extra dev                      # install deps (uv is the only runtime manager)
uv run pytest -q                         # full suite (unit + golden + shell discovery)
uv run pytest tests/test_model.py -q     # one file
uv run pytest -k fargate -q              # one test / pattern
uv run kion-sizer --dir path/ --json     # run the tool locally

bash tests/test_cloudshell.sh            # cloudshell.sh discovery tests standalone (also run under pytest)
bash tests/env/run.sh                    # AL2023 container fidelity test (needs Docker; opt-in)
RUN_ENV_TEST=1 uv run pytest tests/test_env_fidelity.py   # same test via pytest
```

Regenerate golden files after an **intentional** output/calibration change, then
eyeball the diff (they are human-readable and committed):

```sh
KION_SIZER_UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py
```

## Architecture

Pipeline, in `cli.run()`: **profile → (rds_catalog / pricing) → model → render**.

| Module | Role |
|---|---|
| `profile.py` | All CUR I/O. Walks a dir or lists S3, sums bytes, counts rows (parquet footers or CSV sampling), detects account IDs. Produces a `CURProfile`. |
| `config.py` | Calibration constants + tier/band tables, loaded from packaged `default.yaml` (or `--config`), validated on parse. |
| `rds_catalog.py` | AWS lookup of *orderable* DB classes + their RAM (`--rds-from-aws`). |
| `pricing.py` | AWS Pricing API lookup, with the embedded `prices.json` snapshot as fallback. Builds a `PriceTable`. |
| `model.py` | **Pure** — `recommend(profile, config, accounts)` and `cost(rec, prices)`. No I/O, no AWS imports. |
| `render.py` | Text and JSON formatting of a `Recommendation`. |
| `progress.py` | tqdm bars, stderr only. |

One invariant holds this shape together: **`model.py` stays I/O-free.** Every AWS
call lives in `profile`/`rds_catalog`/`pricing`, which is why those are all
testable with injected fake clients (`FakeRDS`, `FakePricing`, `FakeS3Client`,
`FakeLister` in `tests/`). Adding a boto3 import to `model.py` breaks the design.

### Graceful degradation is deliberate

Every optional AWS-backed enhancement must degrade instead of failing the run:
`--rds-from-aws` falls back to the built-in `default.yaml` tiers, `--cost` falls
back to the embedded us-east-1 snapshot, footer reads and account detection warn
and fall back to the bytes-only estimate. The broad `except Exception` blocks
carry `# noqa: BLE001` plus a reason comment; keep that pattern. The provenance
of what actually happened is surfaced in the output (`rds_tier_source`,
`cost.source`, `sample_note`, `account_source`) — a fallback must stay visible to
the user, never silent.

Timeouts matter for the same reason: AWS clients in `rds_catalog`/`pricing` set
explicit connect/read timeouts and bounded retries so a slow account can't hang
the tool. `rds_catalog` probes a curated candidate ladder with targeted
orderability queries rather than enumerating the full catalog (a previous full
enumeration hung).

### Row counting

Three paths, in decreasing accuracy: parquet footers (exact, reads footer bytes
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

## Calibration

`src/kion_sizer/default.yaml` holds the constants derived from a reference CUR;
`calibration_version` is stamped into every run. Changing any constant changes
golden output — regenerate goldens and review the diff. Output must stay
customer-neutral: `test_golden.py::test_output_is_neutral` asserts the neutral
calibration marker and the absence of a caveat block.

`src/kion_sizer/prices.json` is the embedded us-east-1 price/spec snapshot (also
the source of EC2 vcpu/mem/arch specs even on the live path, so selection needs
no `describe-instance-types` call — the only IAM surface `--cost` adds is
`pricing:GetProducts`). Refresh it from the AWS Pricing API when it goes stale.

# kion-sizer

Recommend out-of-the-box AWS sizing for a [Kion](https://kion.io) deployment
from a customer's **Cost and Usage Report (CUR)** — the one artifact reliably
available *before* a deployment exists.

It sizes the two components that matter most at scale — the **RDS instance** and
the **financials-poller ECS task** — plus coarse per-service starting bands.

> These are **starting-point** recommendations. Size against the customer's
> **peak** month of CUR data. Every run stamps the `calibration_version` used.

## Quickstart — AWS CloudShell (recommended)

Open **AWS CloudShell** in the account whose CUR you want to size, then:

```sh
git clone https://github.com/bshutterkion/kion-sizer.git
cd kion-sizer
bash scripts/cloudshell.sh
```

That's it — no `--profile`, no account flag, no S3 URI. The script:

1. **Bootstraps the runtime** — installs [`uv`](https://docs.astral.sh/uv/) if
   missing, which provisions Python 3.12+ and the dependencies in your home
   directory (no `sudo`, fits CloudShell's storage).
2. **Discovers the CUR** — via `cur:DescribeReportDefinitions`, then CUR 2.0 /
   BCM Data Exports, then a bucket-name heuristic. Picks the **peak month** by
   bytes.
3. **Sizes it** — runs the tool against the discovered report.

### Options

```sh
bash scripts/cloudshell.sh --accounts 150      # add per-service starting bands
bash scripts/cloudshell.sh --json              # machine-readable output
bash scripts/cloudshell.sh --s3 s3://b/prefix/ # skip discovery; size this prefix
bash scripts/cloudshell.sh --bucket NAME       # pick the peak month in a named bucket
bash scripts/cloudshell.sh --dry-run           # print the resolved command only
```

Use `--bucket` when the account has more than one CUR bucket and auto-discovery
picks the wrong one — it runs the same peak-by-bytes month picker, scoped to the
bucket you name. `--s3` takes precedence over `--bucket` when both are given.

Uses the CloudShell session's ambient credentials. The CUR's S3 bucket region is
auto-resolved (customer buckets are not always `us-east-1`).

## Running the tool directly

If you already have a checkout and `uv`:

```sh
uv run kion-sizer --s3 s3://bucket/report/month/ --granularity hourly --accounts 700
uv run kion-sizer --dir /path/to/cur-month/ --accounts 150 --read-footers --json
```

| Flag | Meaning |
|------|---------|
| `--s3 s3://…/` | Size a CUR month in S3 (auto-resolves bucket region). |
| `--dir PATH` | Size a locally-downloaded CUR month. |
| `--accounts N` | Add core/compliance service starting bands for N AWS accounts. |
| `--granularity hourly\|daily` | CUR granularity (hourly ≈ 24× the rows of daily). |
| `--read-footers` | Exact parquet row counts from footers (local `--dir` only). |
| `--json` | Machine-readable output. |
| `--rds-from-aws` | Size the RDS tier against the DB instance classes actually **orderable** in `--region` (via `describe-orderable-db-instance-options` + `describe-instance-types`); falls back to the built-in tiers if AWS is unreachable. `cloudshell.sh` enables this by default. |
| `--cost` | Estimate **monthly cost** (RDS + poller Fargate + service bands, with a total) and the **EC2-equivalent** instances that hold the poller's CPU/memory. Live AWS Pricing API (needs `pricing:GetProducts`), falling back to an embedded us-east-1 snapshot. `cloudshell.sh` enables this by default. |
| `--region R` | Region for `--rds-from-aws` and `--cost` pricing (defaults to the AWS session region). |
| `--rds-engine E` | RDS engine for orderability + pricing (default `mysql`). |
| `--config FILE` | Override the calibration constants (`default.yaml`). |

## What it reports

- **RDS instance class** — smallest tier whose InnoDB buffer pool holds the
  largest payer-month shard plus headroom. With `--rds-from-aws` the candidate
  classes come from what's actually orderable in the region.
- **financials-poller** — the raw heap requirement (memory + vCPU) *and* the
  nearest valid AWS Fargate task size to provision (Fargate only accepts
  discrete CPU/memory combinations).
- **core / compliance service bands** — starting ECS task counts + CPU/memory by
  account count (with `--accounts`).
- **estimated monthly cost** (with `--cost`) — RDS + poller Fargate (x86_64 and
  arm64) + service bands, with a stack total; plus an **EC2-equivalent menu** for
  the poller: the smallest instance per family/architecture that holds the poller's
  heap requirement, cheapest marked. When a memory requirement lands in a dead zone
  between instance tiers, it also shows the closest smaller-memory options. Prices
  are on-demand (RIs / Savings Plans / Spot can cut ~30-55%); RDS is compute-only
  (storage and Multi-AZ are not sized). Refresh the embedded snapshot with the
  values in `src/kion_sizer/prices.json` (pulled from the AWS Pricing API).

Parquet (CUR 2.0) and legacy CSV (`.csv` / `.csv.gz`) are both supported; legacy
CSV row counts are estimated by sampling the largest files and extrapolating by
byte share.

## Development

```sh
uv sync --extra dev
uv run pytest -q                 # Python unit + golden + discovery tests
bash tests/test_cloudshell.sh    # cloudshell.sh discovery tests (also under pytest)
bash tests/env/run.sh            # amazonlinux:2023 environment-fidelity test (needs Docker)
```

## License

MIT — see [LICENSE](LICENSE).

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

`cloudshell.sh` runs `--rds-from-aws`, `--cost`, `--read-footers`, and
`--detect-accounts` **by default**, so every run already includes the
region-orderable RDS tier, the monthly cost estimate + EC2-equivalent, exact
parquet row counts, and the auto-detected account count — no extra flags needed.
(Each degrades gracefully if the underlying AWS/parquet call is unavailable.) The
slower AWS/parquet passes stream a progress bar to stderr, so a large CUR shows
live progress. `--json` emits the whole thing, cost included, as JSON.

Uses the CloudShell session's ambient credentials. The CUR's S3 bucket region is
auto-resolved (customer buckets are not always `us-east-1`).

## Quickstart — Azure Cloud Shell

Kion runs on Azure as well as AWS, and Azure billing can be sized for either.
Open **Azure Cloud Shell** (Bash) signed into the tenant whose Cost Management
export you want to size, then:

```sh
git clone https://github.com/bshutterkion/kion-sizer.git
cd kion-sizer
bash scripts/azure-cloudshell.sh
```

The script:

1. **Discovers the export** — Cost Management exports at every billing account
   and enabled subscription you can see, preferring actual-cost exports, then
   FOCUS, then amortized. EA and MCA exports are usually defined at the
   enrollment scope, which an operator with storage access often cannot see, so
   when no export definition is visible it searches storage accounts with
   billing-like names for blobs laid out the way Cost Management writes them.
   Government and China clouds work: endpoints come from `az cloud show`.
2. **Checks blob access** — the tool reads as your identity, which needs
   **Storage Blob Data Reader** on the container. Owner or Contributor does not
   grant that. If you lack it but can read the account key, the script mints a
   2-hour read/list SAS token for that one container and hands it to the tool
   through `KION_SIZER_AZURE_SAS`, never on the command line (`--no-sas` turns
   this off).
3. **Sizes it** — the peak billing period, counting only the newest copy of it
   (exports accumulate one copy per run).

```sh
bash scripts/azure-cloudshell.sh --export NAME     # pick an export by name
bash scripts/azure-cloudshell.sh --container URL   # skip discovery
bash scripts/azure-cloudshell.sh --month 202606    # size this billing period
bash scripts/azure-cloudshell.sh --accounts 150 --json
bash scripts/azure-cloudshell.sh --dry-run         # print the resolved command only
```

## Quickstart — Google Cloud Shell

Kion is not deployed on GCP, so GCP is a billing **source** only: this sizes the
AWS deployment that will import the GCP spend. Open **Google Cloud Shell**, then:

```sh
git clone https://github.com/bshutterkion/kion-sizer.git
cd kion-sizer
bash scripts/gcp-cloudshell.sh
```

The script finds the BigQuery billing export table — in the current project, then
in every project you can list (`--max-projects`, default 50). When it finds more
than one it prefers the **detailed** export, then standard, then FOCUS, and logs
every candidate. Detailed is the conservative choice: for the same spend it
produces roughly 16× the database rows of a standard export. If Kion is set up to
ingest a different export, name it with `--table`.

Row and project counts come from BigQuery metadata plus one aggregate query, so
they are exact and no row data is read. Needs **BigQuery Data Viewer** on the
export dataset and **BigQuery Job User** in the billing project.

```sh
bash scripts/gcp-cloudshell.sh --table PROJECT.DATASET.TABLE   # skip discovery
bash scripts/gcp-cloudshell.sh --project PROJECT               # search here first
bash scripts/gcp-cloudshell.sh --month 202606                  # size this billing month
bash scripts/gcp-cloudshell.sh --accounts 150 --json
bash scripts/gcp-cloudshell.sh --dry-run
```

Neither Azure nor GCP Cloud Shell has AWS credentials, so `--cost` prices from
the embedded us-east-1 snapshot and the output says so.

## Running from any shell

The Cloud Shell scripts above discover the billing data for you. When you already
know where it is — or the source has no discovery script (a downloaded CUR
directory, OCI, a custom FOCUS source) — use `scripts/kion-sizer.sh`. It installs
the runtime the same way, syncs only the cloud SDK the location needs, and passes
every flag through to the tool:

```sh
bash scripts/kion-sizer.sh --dir /path/to/cur-month/ --accounts 150 --read-footers --json
bash scripts/kion-sizer.sh --s3 s3://bucket/report/month/ --granularity hourly --accounts 700
bash scripts/kion-sizer.sh --custom-source s3://bucket/prefix/
bash scripts/kion-sizer.sh --help        # every flag below
```

| Flag | Meaning |
|------|---------|
| `--source aws\|azure\|gcp\|oci\|custom` | Billing data source. Inferred from the location flag; `--dir` defaults to `aws`. |
| `--target aws\|azure` | Deployment target to size for. Defaults to `aws`. Only `aws` is implemented so far; `gcp` is rejected because Kion is not deployed there. |
| `--s3 s3://…/` | Size a CUR month in S3 (auto-resolves bucket region). |
| `--gcp-table P.D.T` | Size a GCP BigQuery billing export (`PROJECT.DATASET.TABLE`). Implies `--source gcp`. |
| `--azure-container URL` | Size an Azure Cost Management export (`https://<acct>.blob.core.windows.net/<container>/<path>`). |
| `--azure-month YYYYMM` | Billing period to size (default: the peak period by bytes). |
| `--oci-bucket URL` | Size OCI cost reports (`https://objectstorage.<region>.oraclecloud.com/n/<ns>/b/<bucket>/o/<prefix>`). |
| `--custom-source LOC` | Size a custom billing source (`s3://bucket/prefix/`, or an Azure blob URL). Always FOCUS-formatted. |
| `--gcp-month YYYYMM` | Billing month to size (default: the peak month by row count). |
| `--dir PATH` | Size a locally-downloaded CUR month. |
| `--accounts N` | Core/compliance service bands for N AWS accounts. Overrides `--detect-accounts`. |
| `--detect-accounts` | Auto-detect the member-account count from the CUR's `line_item_usage_account_id` column — **exact** for parquet, a labeled **lower bound** from the CSV sample. Feeds the service bands when `--accounts` isn't given. `cloudshell.sh` enables this by default. |
| `--granularity hourly\|daily` | CUR granularity (hourly ≈ 24× the rows of daily). |
| `--read-footers` | Exact raw line-item counts from parquet footers — now works on **`--s3`** as well as `--dir` (footer bytes only, never row data). `cloudshell.sh` enables this by default. |
| `--sample N` | CSV files to sample per format for row estimation (default 20, spread across the size distribution). Sampled files are read in parallel. |
| `--all-files` | Read **every** CSV file instead of a sample — exact row count and an **exact** account count (not a lower bound), at the cost of downloading + parsing the whole CUR. Much slower on a large CUR; ignores `--sample`. Not enabled by `cloudshell.sh`. |
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

- **member accounts** (with `--detect-accounts`) — distinct
  `line_item_usage_account_id`s in the CUR, used to pick the service bands.

Parquet (CUR 2.0) and legacy CSV (`.csv` / `.csv.gz`) are both supported. Parquet
row counts are exact from footers (`--read-footers`), and the parquet account count
is exact (columnar — only that one column is read). Legacy CSV row counts are
estimated by sampling files spread across the size distribution (`--sample N`, read
in parallel) and extrapolating by byte share, and the CSV account count from a
sample is a lower bound — pass `--all-files` to read the whole CUR for exact CSV
counts. Because the estimate only picks an instance *tier* (which is quantized),
the sample is almost always enough; `--all-files` is there for when you want the
exact numbers and don't mind the wait.

## Billing sources

Every billing source Kion ingests in bulk can be profiled, and the report format
is detected from the data rather than declared:

| Source | Flag | Formats |
|---|---|---|
| AWS | `--dir` / `--s3` | CUR, legacy DBR, FOCUS |
| Azure | `--azure-container` | usage details (EA/MCA), FOCUS |
| GCP | `--gcp-table` | standard, detailed, FOCUS |
| OCI | `--oci-bucket` | cost reports, FOCUS |
| Custom | `--custom-source` | FOCUS (in S3 or Azure blob) |

Kion is deployed on AWS and Azure only, so GCP and OCI customers pair their
billing source with `--target aws` or `--target azure`.

## Sizing GCP spend

Kion is not deployed on GCP, so a GCP-billed customer runs Kion on AWS (or, once
supported, Azure) and imports that spend. That is `--source gcp` with an AWS
target:

```sh
bash scripts/gcp-cloudshell.sh                                  # discover the export
bash scripts/gcp-cloudshell.sh --table my-proj.billing.gcp_billing_export_resource_v1_01ABCD --json
```

Row counts and the member-account count come straight from BigQuery metadata, so
they are **exact** — no sampling, and `--read-footers` / `--all-files` /
`--sample` do not apply. The peak billing month is chosen by `invoice.month`
row count unless `--gcp-month` names one.

The standard and detailed exports are calibrated separately, because they differ
by an order of magnitude: for the same spend a detailed export produces roughly
16× the database rows. The GCP FOCUS export is not yet calibrated, and runs
against it say so.

## Development

For contributors working on the tool itself, not for running it:

```sh
uv sync --extra dev          # dev pulls every implemented cloud
uv run pytest -q                 # Python unit + golden + discovery tests
bash tests/test_cloudshell.sh    # cloudshell.sh discovery tests (also under pytest)
bash tests/test_azure_cloudshell.sh  # azure-cloudshell.sh discovery tests
bash tests/test_gcp_cloudshell.sh    # gcp-cloudshell.sh discovery tests
bash tests/test_kion_sizer_sh.sh     # kion-sizer.sh wrapper tests
bash tests/env/run.sh            # amazonlinux:2023 environment-fidelity test (needs Docker)
```

## License

MIT — see [LICENSE](LICENSE).

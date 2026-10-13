"""GCP billing source: a BigQuery billing-export table.

Unlike the file-based sources, nothing here scans data. Two queries answer the
whole profile:

  1. INFORMATION_SCHEMA.PARTITIONS  -> table-level rows and logical bytes
  2. GROUP BY invoice.month         -> exact rows and distinct projects per
                                       billing month

So GCP row counts are exact and cheap: the sampling, footer, and byte-estimate
machinery is never used. Kion ingests GCP spend into a deployment running on AWS
or Azure — there is no GCP deployment target.
"""

from __future__ import annotations

import re

from . import schema
from .base import BillingProfile, ProfileError, missing_sdk

STANDARD_PREFIX = "gcp_billing_export_v1_"
DETAILED_PREFIX = "gcp_billing_export_resource_v1_"
FOCUS_PREFIX = "gcp_billing_export_focus_"
PRICING_TABLE = "cloud_pricing_export"


# BigQuery parameters bind values, never identifiers, so a table reference has
# to be interpolated into the SQL. Legacy domain-scoped project ids
# ("example.com:proj") are not supported -- a dot-split cannot represent them,
# and no billing export observed here uses one. These are the character sets BigQuery itself
# permits, so anything that could terminate a quote or a backtick is rejected
# before it reaches a query rather than producing a syntax error at run time.
_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,61}[A-Za-z0-9]$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,1024}$")


def parse_table_ref(ref: str) -> tuple[str, str, str]:
    """`proj.ds.table` -> its three parts, validated as safe identifiers."""
    parts = ref.strip().strip("`").split(".")
    if len(parts) != 3 or not all(parts):
        raise ProfileError(
            f'not a BigQuery table reference: "{ref}" '
            "(expected PROJECT.DATASET.TABLE)"
        )
    project, dataset, table = parts
    if not _PROJECT_RE.match(project):
        raise ProfileError(f'not a valid BigQuery project id: "{project}"')
    for label, name in (("dataset", dataset), ("table", table)):
        if not _NAME_RE.match(name):
            raise ProfileError(
                f'not a valid BigQuery {label} name: "{name}" '
                "(letters, digits and underscores only)"
            )
    return project, dataset, table


def export_kind(table: str) -> str:
    """Which billing export this table is.

    GCP emits three usage exports, and they are not interchangeable:

      standard  gcp_billing_export_v1_*            account-level rows
      detailed  gcp_billing_export_resource_v1_*   adds resource-level rows, so
                                                   dramatically more of them
      focus     gcp_billing_export_focus_*         FOCUS 1.x columns, in a
                                                   Google-managed immutable
                                                   dataset

    Detailed vs standard is GCP's equivalent of hourly-vs-daily CUR. FOCUS
    matters for a different reason: its columns are entirely different, so the
    queries must change, not just the expected volume.
    """
    if table.startswith(FOCUS_PREFIX):
        return "focus"
    if table.startswith(DETAILED_PREFIX):
        return "detailed"
    if table.startswith(STANDARD_PREFIX):
        return "standard"
    if table == PRICING_TABLE or table.startswith(PRICING_TABLE):
        return "pricing"
    return "unknown"


def _schema_for(kind: str) -> str:
    return schema.FOCUS if kind == "focus" else schema.GCP_NATIVE


def _make_client(project: str):
    try:
        from google.cloud import bigquery
    except ImportError:
        missing_sdk("gcp", "google-cloud-bigquery")
    try:
        return bigquery.Client(project=project)
    except Exception as e:  # noqa: BLE001 — surface any auth/config failure as ours
        raise ProfileError(
            f"could not create a BigQuery client for project {project!r}: {e}. "
            "Check that credentials are available (gcloud auth application-default "
            "login, or a service account)."
        ) from e


def _table_param(table: str):
    """Bind `table` as a query parameter rather than splicing it into the SQL."""
    try:
        from google.cloud import bigquery as bq
    except ImportError:  # pragma: no cover - the fake client needs no config
        return None
    return bq.QueryJobConfig(
        query_parameters=[bq.ScalarQueryParameter("table", "STRING", table)]
    )


def _rows(client, sql, job_config=None):
    """Run one query, turning any BigQuery failure into a ProfileError.

    Without this a missing credential or a denied dataset surfaces as an
    unhandled google.api_core exception and a traceback; the CLI only knows how
    to report ProfileError cleanly.
    """
    try:
        if job_config is not None:
            return list(client.query(sql, job_config=job_config).result())
        return list(client.query(sql).result())
    except ProfileError:
        raise
    except Exception as e:  # noqa: BLE001 — any query failure is fatal for profiling
        raise ProfileError(f"BigQuery query failed: {e}") from e


def _table_totals(client, project: str, dataset: str, table: str):
    """Table-level rows and logical bytes from partition metadata.

    Summed across partitions on purpose: __UNPARTITIONED__ can appear more than
    once when active and long-term storage tiers mix. TABLES.row_count is
    documented as approximate, so it is not used.
    """
    sql = f"""
        SELECT total_rows, total_logical_bytes
        FROM `{project}.{dataset}.INFORMATION_SCHEMA.PARTITIONS`
        WHERE table_name = @table
    """
    rows = _rows(client, sql, job_config=_table_param(table))
    total_rows = sum(int(r.total_rows or 0) for r in rows)
    total_bytes = sum(int(r.total_logical_bytes or 0) for r in rows)
    return total_rows, total_bytes


def _month_expr(kind: str) -> str:
    """SQL producing a YYYYMM billing month for this export kind.

    Native exports carry invoice.month as a YYYYMM string already. FOCUS carries
    BillingPeriodStart as a timestamp, so it is formatted; DATE() accepts both a
    TIMESTAMP and a DATE, which keeps this working if the column type differs.
    """
    if kind == "focus":
        return "FORMAT_DATE('%Y%m', DATE(BillingPeriodStart))"
    return "invoice.month"


def _account_expr(kind: str) -> str:
    return "SubAccountId" if kind == "focus" else "project.id"


def _months(client, project: str, dataset: str, table: str, kind: str):
    """Exact rows and distinct member accounts per billing month.

    Grouped by the billing month rather than the partition date: partitions are
    by usage day, and rows belonging to a billing month keep landing after that
    month has ended, so partition date picks the wrong month.
    """
    sql = f"""
        SELECT {_month_expr(kind)} AS month,
               COUNT(*) AS row_count,
               COUNT(DISTINCT {_account_expr(kind)}) AS accounts
        FROM `{project}.{dataset}.{table}`
        GROUP BY month
        ORDER BY month
    """
    return [
        (str(r.month), int(r.row_count), int(r.accounts)) for r in _rows(client, sql)
    ]


def from_bigquery(
    table_ref: str,
    *,
    month: str = "",
    client=None,
    **_ignored,
) -> BillingProfile:
    """Profile one billing month of a BigQuery billing export.

    Picks the peak month by row count unless `month` (YYYYMM) is given.
    `_ignored` absorbs the file-scan options (sample/read_footers/all_files),
    which have no meaning here; the CLI warns when they are passed.
    """
    project, dataset, table = parse_table_ref(table_ref)
    kind = export_kind(table)
    if kind == "pricing":
        raise ProfileError(
            f"{table_ref} is the SKU pricing export, not a usage export. Point "
            "--gcp-table at gcp_billing_export_v1_*, "
            "gcp_billing_export_resource_v1_*, or gcp_billing_export_focus_*."
        )
    client = client or _make_client(project)

    months = _months(client, project, dataset, table, kind)
    if not months:
        raise ProfileError(f"no billing rows found in {table_ref}")

    if month:
        picked = next((m for m in months if m[0] == month), None)
        if picked is None:
            seen = ", ".join(m[0] for m in months) or "none"
            raise ProfileError(
                f"no billing rows for month {month} in {table_ref} (months present: {seen})"
            )
    else:
        picked = max(months, key=lambda m: m[1])

    picked_month, rows, accounts = picked
    total_rows, total_bytes = _table_totals(client, project, dataset, table)

    # BigQuery reports bytes per partition, not per invoice month, so the
    # month's share is apportioned by row share. Only ever used for display and
    # the bytes-estimate fallback, which this source never takes.
    if total_rows > 0:
        month_bytes = int(total_bytes * rows / total_rows)
    else:
        month_bytes = 0

    note = f"billing month {picked_month} of {len(months)} present; {kind} export"
    if kind == "detailed":
        note += " (resource-level: substantially more rows than a standard export)"
    elif kind == "focus":
        note += " (FOCUS 1.x columns; volume differs from the native exports)"
    elif kind == "unknown":
        note += " (unrecognized table name; assuming native columns)"
    note += "; month bytes apportioned from the table total by row share"

    p = BillingProfile(
        source=table_ref,
        cloud="gcp",
        unit_label="billing months",
        byte_label="logical bytes",
        volume_note=note,
        file_count=len(months),
        compressed_bytes=month_bytes,
        raw_line_items=rows,
        have_raw_rows=True,
        granularity="unknown",
        format=f"bigquery/{kind}",
        sample_note=f"exact, from a BigQuery COUNT over the {kind} export",
    )
    p.account_count = accounts
    p.have_accounts = True
    p.account_lower_bound = False  # columnar COUNT(DISTINCT): exact, never a bound
    p.account_source = f"BigQuery COUNT(DISTINCT {_account_expr(kind)}) (exact)"
    p.report_format = kind
    return p

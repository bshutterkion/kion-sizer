"""OCI billing source: cost reports in Oracle Object Storage.

Oracle writes cost and usage reports as gzipped CSV into a tenancy-owned bucket
in the `bling` namespace, under `reports/cost-csv/`. Kion reads them from there,
and can also read an OCI FOCUS export; which one is present is told apart by the
columns, as everywhere else.

Kion is not deployed on OCI, so this is a source only: an OCI-billed customer
runs Kion on AWS or Azure and imports the spend.

Object access goes through an injectable client with two methods, `list_objects`
and `get_object`, so the profiling logic is testable without the Oracle SDK.
"""

from __future__ import annotations

import gzip
import io
import posixpath
import re
from dataclasses import dataclass

from . import schema
from .base import (
    BillingProfile,
    ProfileError,
    _apply_accounts,
    _CsvRec,
    _warn,
    data_ext,
    merge_format,
    missing_sdk,
    scan_csv,
)

BLING_NAMESPACE = "bling"
COST_PREFIX = "reports/cost-csv/"

# Account identity, matching what Kion records as the account number.
TENANCY_COLUMN = "lineItem/tenantId"
COMPARTMENT_COLUMN = "product/compartmentId"

# .../o/reports/cost-csv/0001000001652459-00001.csv.gz -> the YYYY-MM is not in
# the name, so periods come from the report's own date column when needed. What
# the name does carry is a monotonically increasing sequence per report run.
_URL_RE = re.compile(
    r"^https://objectstorage\.(?P<region>[^.]+)\.oraclecloud\.com"
    r"/n/(?P<ns>[^/]+)/b/(?P<bucket>[^/]+)(?:/o/(?P<prefix>.*))?$"
)


@dataclass
class ObjRec:
    name: str
    size: int


def parse_object_url(url: str) -> tuple[str, str, str, str]:
    """Split an OCI Object Storage URL into (region, namespace, bucket, prefix)."""
    m = _URL_RE.match(url.rstrip("/"))
    if not m:
        raise ProfileError(
            f'not an OCI Object Storage URL: "{url}" (expected https://objectstorage'
            ".<region>.oraclecloud.com/n/<namespace>/b/<bucket>/o/<prefix>)"
        )
    return (
        m.group("region"),
        m.group("ns"),
        m.group("bucket"),
        (m.group("prefix") or COST_PREFIX).strip("/") + "/",
    )


def detect_format(columns) -> str:
    """"focus" or "cost-csv", from the column names present."""
    if schema.pick(schema.ACCOUNT_COLUMNS[schema.FOCUS], columns):
        return "focus"
    lowered = {str(c).lower() for c in columns}
    if TENANCY_COLUMN.lower() in lowered or COMPARTMENT_COLUMN.lower() in lowered:
        return "cost-csv"
    return "unknown"


def _account_col(columns) -> str | None:
    """Which column identifies a member account.

    Kion maps an OCI compartment to an account, falling back to the tenancy for
    a single-tenancy report (usage_parser.go:131-132).
    """
    lowered = {str(c).lower(): str(c) for c in columns}
    for want in (COMPARTMENT_COLUMN, TENANCY_COLUMN):
        if want.lower() in lowered:
            return lowered[want.lower()]
    return schema.pick(schema.ACCOUNT_COLUMNS[schema.FOCUS], columns)


def _make_client(region: str, namespace: str):
    try:
        import oci  # noqa: F401
    except ImportError:
        missing_sdk("oci", "oci")
    raise ProfileError(
        "OCI credentials are not configured in this build; pass an injected "
        "client, or run against a downloaded report with --dir"
    )


def _open_lines(client, namespace, bucket, name):
    data = client.get_object(namespace, bucket, name)
    if name.lower().endswith(".gz"):
        return gzip.GzipFile(fileobj=io.BytesIO(data))
    return io.BytesIO(data)


def _header_of(recs) -> list:
    import csv as _csv

    for rec in recs:
        line = next(iter(rec.open_lines()), b"")
        text = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
        cols = next(_csv.reader([text]), [])
        if cols:
            return cols
    return []


def from_object_storage(
    url: str,
    *,
    client=None,
    sample: int = 20,
    detect_accounts: bool = False,
    all_files: bool = False,
    **_ignored,
) -> BillingProfile:
    """Profile an OCI cost-report bucket."""
    region, namespace, bucket, prefix = parse_object_url(url)
    if client is None:
        client = _make_client(region, namespace)

    try:
        objs = [
            ObjRec(name=o.name, size=int(o.size or 0))
            for o in client.list_objects(namespace, bucket, prefix)
        ]
    except ProfileError:
        raise
    except Exception as e:  # noqa: BLE001 — a failed listing is fatal for profiling
        raise ProfileError(f"could not list OCI objects under {prefix!r}: {e}") from e

    parts = [o for o in objs if data_ext(posixpath.basename(o.name)) != ""]
    if not parts:
        raise ProfileError(f"no .csv/.csv.gz cost reports found under {url}")

    p = BillingProfile(
        source=url,
        cloud="oci",
        unit_label="reports",
        byte_label="compressed bytes",
        granularity="unknown",
    )
    for o in parts:
        ext = data_ext(posixpath.basename(o.name))
        p.file_count += 1
        p.compressed_bytes += o.size
        p.format = merge_format(p.format, ext)
        p.set_format_flags(ext)

    recs = [
        _CsvRec(
            ext=data_ext(posixpath.basename(o.name)),
            size=o.size,
            open_lines=(lambda n=o.name: _open_lines(client, namespace, bucket, n)),
        )
        for o in parts
        if data_ext(posixpath.basename(o.name)) in ("csv", "csv.gz")
    ]
    if not recs:
        raise ProfileError(f"no readable CSV cost reports under {url}")

    columns = _header_of(recs)
    p.report_format = detect_format(columns)

    est, sampled, total, csv_accounts = scan_csv(
        recs, sample, detect_accounts, all_files, unit="cost reports"
    )
    # A sample that reached every file is a full read: exact, not an estimate.
    all_files = all_files or sampled == total
    p.raw_line_items, p.have_raw_rows = est, True
    p.sample_note = (
        f"exact count from all {total} reports"
        if all_files
        else f"ESTIMATED by sampling {sampled} of {total} reports "
        "(extrapolated by byte share)"
    )

    if detect_accounts:
        try:
            _apply_accounts(p, sample, [], recs, csv_accounts, None, all_files)
            col = _account_col(columns)
            if p.have_accounts and col:
                p.account_source = f"OCI report column {col}" + (
                    "" if all_files else ", sampled"
                )
        except Exception as e:  # noqa: BLE001 — account detection is best-effort
            _warn(f"account detection failed ({e}); skipping")

    note = f"{p.report_format} report"
    if p.report_format == "cost-csv":
        note += "; member accounts are OCI compartments"
    elif p.report_format == "unknown":
        note += "; no recognized account column, confirm this is a cost report"
    p.volume_note = note
    return p


__all__ = [
    "ObjRec",
    "COST_PREFIX",
    "BLING_NAMESPACE",
    "detect_format",
    "from_object_storage",
    "parse_object_url",
]

"""AWS billing sources: a local directory or an S3 prefix.

Three report formats can appear here and are told apart by their columns, not by
the flag the user passed:

  cur    Cost and Usage Report -- parquet or CSV
  dbr    the legacy Detailed Billing Report -- CSV inside .csv.zip
  focus  FOCUS 1.x, via Data Exports

DBR needs more than a different column map. Its files carry AccountTotal,
InvoiceTotal, StatementTotal and Rounding rows alongside real line items, and
Kion skips those on ingest. Counting them would inflate the
row estimate and over-size the database, so the same rows are excluded here.

Both entry points produce the same BillingProfile; they differ only in how they
enumerate objects and open bytes. The scanning itself lives in base.py.
"""

from __future__ import annotations

import gzip
import io
import os
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
    parquet_footer_rows,
    scan_csv,
)


def from_dir(
    dir: str,
    *,
    sample: int = 20,
    read_footers: bool = False,
    detect_accounts: bool = False,
    all_files: bool = False,
    **_ignored,
) -> BillingProfile:
    p = BillingProfile(source=dir, granularity="unknown", cloud="aws",
                       report_format="cur")
    for root, _, files in os.walk(dir):
        for name in files:
            ext = data_ext(name)
            if ext == "":
                continue
            size = os.path.getsize(os.path.join(root, name))
            p.file_count += 1
            p.compressed_bytes += size
            p.format = merge_format(p.format, ext)
            p.set_format_flags(ext)
    if p.file_count == 0:
        raise ProfileError(f'no .parquet/.csv/.csv.gz files found under "{dir}"')

    csv_recs = _dir_csv_recs(dir)
    p.report_format = detect_format(_first_header(csv_recs, _dir_parquet_paths(dir)))
    row_filter = dbr_row_filter if p.report_format == "dbr" else None
    csv_accounts = None
    if read_footers and p.has_parquet and not p.has_csv:
        try:
            p.raw_line_items = read_footer_rows(dir)
            p.have_raw_rows = True
        except Exception as e:  # noqa: BLE001 — degrade to the bytes-only estimate
            _warn(f"parquet footer read failed ({e}); using bytes estimate")
    elif p.has_csv and not p.has_parquet:
        est, sampled, total, csv_accounts = scan_csv(
            csv_recs, sample, detect_accounts, all_files, row_filter
        )
        # A sample that reached every file is a full read: exact, not an estimate.
        all_files = all_files or sampled == total
        p.raw_line_items, p.have_raw_rows = est, True
        p.sample_note = (
            f"exact count from all {total} files"
            if all_files
            else f"ESTIMATED by sampling {sampled} of {total} files "
            "(extrapolated by byte share)"
        )
    if p.report_format == "dbr":
        p.volume_note = (
            "legacy DBR; total/rounding rows excluded, matching what Kion ingests"
        )
    elif p.report_format == "focus":
        p.volume_note = "FOCUS export"
    if detect_accounts:
        try:
            _apply_accounts(
                p,
                sample,
                _dir_parquet_paths(dir),
                csv_recs,
                csv_accounts,
                None,
                all_files,
            )
        except Exception as e:  # noqa: BLE001 — account detection is best-effort
            _warn(f"account detection failed ({e}); skipping")
    return p

def sample_dir_raw_rows(dir: str, max_sample: int) -> tuple[int, int, int]:
    est, sampled, total, _ = scan_csv(_dir_csv_recs(dir), max_sample)
    return est, sampled, total

def _dir_parquet_paths(dir: str) -> list[str]:
    out = []
    for root, _, files in os.walk(dir):
        for name in files:
            if name.lower().endswith(".parquet"):
                out.append(os.path.join(root, name))
    return out

def read_footer_rows(dir: str) -> int:
    """Sum num_rows from every *.parquet footer under a local dir."""
    return parquet_footer_rows(_dir_parquet_paths(dir))

def _dir_csv_recs(dir: str) -> list:
    recs = []
    for root, _, files in os.walk(dir):
        for name in files:
            ext = data_ext(name)
            if ext not in ("csv", "csv.gz", "csv.zip"):
                continue
            path = os.path.join(root, name)
            recs.append(
                _CsvRec(
                    ext=ext,
                    size=os.path.getsize(path),
                    open_lines=(lambda p=path: _open_local_lines(p)),
                )
            )
    return recs

def _open_local_lines(path: str):
    if path.lower().endswith(".gz"):
        return gzip.open(path, "rb")
    if path.lower().endswith(".zip"):
        return _unzip_lines(open(path, "rb").read())
    return open(path, "rb")


def _unzip_lines(data: bytes):
    """First member of a zip archive, as a line iterator.

    AWS DBR ships one CSV per .csv.zip.
    """
    import zipfile

    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    if not names:
        raise ProfileError("empty zip archive in DBR report")
    return zf.open(names[0])

@dataclass
class S3Obj:
    key: str
    size: int

def base_name(key: str) -> str:
    idx = key.rfind("/")
    return key[idx + 1 :] if idx >= 0 else key

def parse_s3_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ProfileError(f'not an s3 uri: "{uri}"')
    rest = uri[len("s3://") :]
    bucket, _, prefix = rest.partition("/")
    if bucket == "":
        raise ProfileError(f'missing bucket in "{uri}"')
    return bucket, prefix

def profile_from_objs(objs: list, source: str) -> BillingProfile:
    p = BillingProfile(source=source, granularity="unknown", cloud="aws",
                       report_format="cur")
    for o in objs:
        ext = data_ext(base_name(o.key))
        if ext == "":
            continue
        p.file_count += 1
        p.compressed_bytes += o.size
        p.format = merge_format(p.format, ext)
        p.set_format_flags(ext)
    if p.file_count == 0:
        raise ProfileError(f"no .parquet/.csv/.csv.gz objects in {source}")
    return p

def profile_from_lister(lister, bucket: str, prefix: str, source: str) -> BillingProfile:
    objs = lister.list(bucket, prefix)
    return profile_from_objs(objs, source)

def _aws_list(client, bucket: str, prefix: str) -> list:
    out = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            out.append(S3Obj(o["Key"], o["Size"]))
    return out

def _resolve_region(client, bucket: str) -> str:
    # GetBucketLocation returns None/"" for us-east-1.
    resp = client.get_bucket_location(Bucket=bucket)
    return resp.get("LocationConstraint") or "us-east-1"

def _sample_s3_raw_rows(client, bucket: str, objs: list, max_sample: int):
    est, sampled, total, _ = scan_csv(_s3_csv_recs(client, bucket, objs), max_sample)
    return est, sampled, total

def _s3_csv_lines(client, bucket: str, key: str):
    body = client.get_object(Bucket=bucket, Key=key)["Body"]
    if key.lower().endswith(".zip"):
        return _unzip_lines(body.read())
    if key.lower().endswith(".gz"):
        return gzip.GzipFile(fileobj=io.BytesIO(body.read()))
    # StreamingBody iterates 1 KiB chunks, not lines; iter_lines() yields lines.
    return body.iter_lines()

def _s3_csv_recs(client, bucket: str, objs: list) -> list:
    recs = []
    for o in objs:
        ext = data_ext(base_name(o.key))
        if ext not in ("csv", "csv.gz", "csv.zip"):
            continue
        recs.append(
            _CsvRec(
                ext=ext,
                size=o.size,
                open_lines=(lambda k=o.key: _s3_csv_lines(client, bucket, k)),
            )
        )
    return recs

def _s3_filesystem(region: str):
    import pyarrow.fs as pafs

    return pafs.S3FileSystem(region=region)

def from_s3(
    uri: str,
    *,
    sample: int = 20,
    read_footers: bool = False,
    detect_accounts: bool = False,
    all_files: bool = False,
    **_ignored,
) -> BillingProfile:
    import boto3

    bucket, prefix = parse_s3_uri(uri)
    loc_client = boto3.client("s3", region_name="us-east-1")
    region = _resolve_region(loc_client, bucket)
    client = boto3.client("s3", region_name=region)

    objs = _aws_list(client, bucket, prefix)
    p = profile_from_objs(objs, uri)

    parquet_keys = [
        f"{bucket}/{o.key}" for o in objs if data_ext(base_name(o.key)) == "parquet"
    ]
    csv_recs = _s3_csv_recs(client, bucket, objs)
    p.report_format = detect_format(
        _first_header(csv_recs, parquet_keys, _s3_filesystem(region) if p.has_parquet else None)
    )
    row_filter = dbr_row_filter if p.report_format == "dbr" else None
    csv_accounts = None
    if read_footers and p.has_parquet and not p.has_csv:
        try:
            p.raw_line_items = parquet_footer_rows(parquet_keys, _s3_filesystem(region))
            p.have_raw_rows = True
        except Exception as e:  # noqa: BLE001 — degrade to the bytes-only estimate
            _warn(f"S3 parquet footer read failed ({e}); using bytes estimate")
    elif p.has_csv and not p.has_parquet:
        raw, sampled, total, csv_accounts = scan_csv(
            csv_recs, sample, detect_accounts, all_files, row_filter
        )
        # A sample that reached every file is a full read: exact, not an estimate.
        all_files = all_files or sampled == total
        p.raw_line_items, p.have_raw_rows = raw, True
        p.sample_note = (
            f"exact count from all {total} S3 files"
            if all_files
            else f"ESTIMATED by sampling {sampled} of {total} S3 files "
            "(extrapolated by byte share)"
        )
    if detect_accounts:
        try:
            fs = _s3_filesystem(region) if p.has_parquet else None
            _apply_accounts(
                p, sample, parquet_keys, csv_recs, csv_accounts, fs, all_files
            )
        except Exception as e:  # noqa: BLE001 — account detection is best-effort
            _warn(f"account detection failed ({e}); skipping")
    return p



# --- report-format detection ------------------------------------------------
# Rows the DBR carries but Kion does not ingest.
DBR_SKIPPED_RECORD_TYPES = frozenset(
    {"StatementTotal", "InvoiceTotal", "AccountTotal", "Rounding", "SavingsPlanNegation"}
)


def detect_format(columns) -> str:
    """"cur", "dbr", or "focus", from the column names present."""
    lowered = {str(c).lower() for c in columns}
    if "recordtype" in lowered and "linkedaccountid" in lowered:
        return "dbr"
    if schema.pick(schema.ACCOUNT_COLUMNS[schema.FOCUS], columns):
        return "focus"
    return "cur"


def dbr_row_filter(header, row) -> bool:
    """Keep only the rows Kion would ingest from a DBR file."""
    try:
        idx = [h.lower() for h in header].index("recordtype")
    except ValueError:
        return True
    return idx >= len(row) or row[idx] not in DBR_SKIPPED_RECORD_TYPES


def _first_header(recs: list, parquet_paths: list, filesystem=None) -> list:
    """Column names from the first readable part, for format detection."""
    for rec in recs:
        import csv as _csv

        reader = rec.open_lines()
        line = next(iter(reader), b"")
        text = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
        cols = next(_csv.reader([text]), [])
        if cols:
            return cols
    if parquet_paths:
        try:
            import pyarrow.parquet as pq

            return list(pq.read_schema(parquet_paths[0], filesystem=filesystem).names)
        except Exception:  # noqa: BLE001 — detection is best-effort; default to cur
            return []
    return []

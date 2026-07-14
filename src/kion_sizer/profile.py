"""Turns a customer CUR (local dir or S3) into a CURProfile.

Mirrors internal/profile/{profile,s3,footers,sample}.go.
"""

from __future__ import annotations

import gzip
import io
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from . import progress


class ProfileError(Exception):
    pass


def _warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


@dataclass
class CURProfile:
    source: str = ""
    file_count: int = 0
    compressed_bytes: int = 0
    raw_line_items: int = 0
    have_raw_rows: bool = False
    granularity: str = "unknown"
    format: str = ""
    has_parquet: bool = False
    has_csv: bool = False
    sample_note: str = ""
    # Account auto-detection (--detect-accounts): distinct usage-account IDs.
    account_count: int = 0
    have_accounts: bool = False
    account_lower_bound: bool = False  # True when only a CSV sample was read
    account_source: str = ""

    def set_format_flags(self, ext: str) -> None:
        if ext == "parquet":
            self.has_parquet = True
        elif ext in ("csv", "csv.gz"):
            self.has_csv = True


def data_ext(name: str) -> str:
    n = name.lower()
    if n.endswith(".parquet"):
        return "parquet"
    if n.endswith(".csv.gz"):
        return "csv.gz"
    if n.endswith(".csv"):
        return "csv"
    return ""


def merge_format(cur: str, nxt: str) -> str:
    if cur == "" or cur == nxt:
        return nxt
    return "mixed"


def from_dir(
    dir: str,
    *,
    sample: int = 20,
    read_footers: bool = False,
    detect_accounts: bool = False,
    all_files: bool = False,
) -> CURProfile:
    p = CURProfile(source=dir, granularity="unknown")
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
    csv_accounts = None
    if read_footers and p.has_parquet and not p.has_csv:
        try:
            p.raw_line_items = read_footer_rows(dir)
            p.have_raw_rows = True
        except Exception as e:  # noqa: BLE001 — degrade to the bytes-only estimate
            _warn(f"parquet footer read failed ({e}); using bytes estimate")
    elif p.has_csv and not p.has_parquet:
        est, sampled, total, csv_accounts = scan_csv(
            csv_recs, sample, detect_accounts, all_files
        )
        p.raw_line_items, p.have_raw_rows = est, True
        p.sample_note = (
            f"exact count from all {total} files"
            if all_files
            else f"ESTIMATED by sampling {sampled} of {total} files "
            "(extrapolated by byte share)"
        )
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


# --- CSV sampling (Task 5) -------------------------------------------------


def count_rows_from_reader(r) -> int:
    """Count CSV data rows (excluding the header line) from a binary reader.

    Streams line by line; never buffers the whole input.
    """
    lines = 0
    for _ in r:
        lines += 1
    if lines > 0:
        lines -= 1  # header
    return lines


def count_data_rows(path: str) -> int:
    if path.lower().endswith(".gz"):
        with gzip.open(path, "rb") as f:
            return count_rows_from_reader(f)
    with open(path, "rb") as f:
        return count_rows_from_reader(f)


def _stratified(items: list, n: int) -> list:
    """Pick up to n items spread evenly across an already size-sorted list.

    Sampling only the largest files biases rows-per-byte when large and small
    files differ in density; a spread from largest to smallest is representative.
    """
    c = len(items)
    if n >= c:
        return list(items)
    if n <= 1:
        return [items[0]]
    idxs = sorted({round(i * (c - 1) / (n - 1)) for i in range(n)})
    return [items[i] for i in idxs]


def _scan_one(rec: "_CsvRec", detect_accounts: bool) -> tuple[int, set | None]:
    """Read one CSV file exactly once. Returns (data_row_count, account_ids).

    account_ids is None when detection is off (the fast line-count path); when on,
    a single csv parse yields both the row count and the account column values.
    """
    reader = rec.open_lines()
    if not detect_accounts:
        return count_rows_from_reader(reader), None
    import csv

    r = csv.reader(ln.decode("utf-8", "replace") for ln in reader)
    try:
        header = next(r)
    except StopIteration:
        return 0, set()
    col = _pick_account_col(header)
    idx = header.index(col) if col is not None else None
    rows = 0
    ids: set = set()
    for row in r:
        rows += 1
        if idx is not None and idx < len(row):
            ids.add(row[idx])
    return rows, ids


# Bounded so a fleet of large gzip files (each buffered compressed in memory) can't
# blow CloudShell's RAM; still enough to overlap S3 download + decompress + parse.
_CSV_SCAN_WORKERS = 8


def scan_csv(
    recs: list,
    max_sample: int,
    detect_accounts: bool = False,
    all_files: bool = False,
) -> tuple[int, int, int, set | None]:
    """Read CSV files (each exactly once, in parallel) to estimate total raw rows
    by byte share and, when detect_accounts is set, collect distinct account IDs
    from the files read. With all_files the whole set is read (exact rows, and an
    exact account count); otherwise a stratified sample per format is read and the
    result is an estimate / account lower bound. Returns
    (est_rows, sampled, total, accounts_or_None).
    """
    if max_sample <= 0:
        max_sample = 3
    total = len(recs)
    if total == 0:
        raise ProfileError("no csv/csv.gz files to sample")
    groups: dict[str, list] = {}
    for r in recs:
        groups.setdefault(r.ext, []).append(r)
    plan = []  # (group_bytes, [selected recs])
    for items in groups.values():
        group_bytes = sum(it.size for it in items)
        if group_bytes == 0:
            continue
        ordered = sorted(items, key=lambda it: it.size, reverse=True)
        sel = ordered if all_files else _stratified(ordered, max_sample)
        plan.append((group_bytes, sel))
    selected = [r for _, sel in plan for r in sel]

    desc = "reading all CUR files" if all_files else "sampling CUR files"
    results = _parallel(
        lambda rec: _scan_one(rec, detect_accounts),
        selected,
        desc,
        workers=_CSV_SCAN_WORKERS,
    )
    rows_by = {}
    accounts: set | None = set() if detect_accounts else None
    for rec, (rows, ids) in zip(selected, results):
        rows_by[id(rec)] = rows
        if ids is not None:
            accounts |= ids

    est = 0
    sampled = 0
    for group_bytes, sel in plan:
        s_rows = sum(rows_by[id(r)] for r in sel)
        s_bytes = sum(r.size for r in sel)
        sampled += len(sel)
        if s_bytes > 0:
            est += int(s_rows / s_bytes * group_bytes)
    return est, sampled, total, accounts


def sample_dir_raw_rows(dir: str, max_sample: int) -> tuple[int, int, int]:
    est, sampled, total, _ = scan_csv(_dir_csv_recs(dir), max_sample)
    return est, sampled, total


# --- parquet footers (Task 6) ----------------------------------------------


def _dir_parquet_paths(dir: str) -> list[str]:
    out = []
    for root, _, files in os.walk(dir):
        for name in files:
            if name.lower().endswith(".parquet"):
                out.append(os.path.join(root, name))
    return out


def _parallel(fn: Callable, items: list, desc: str, workers: int = 16) -> list:
    """Run fn over items concurrently, drawing a progress bar. Order preserved."""
    items = list(items)
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        return list(
            progress.track(pool.map(fn, items), desc, total=len(items), unit="file")
        )


def parquet_footer_rows(paths: list[str], filesystem=None) -> int:
    """Sum num_rows from each parquet footer (RAW CUR line items). Reads only the
    file metadata (footer bytes), never row data — over local paths or, with an
    S3 filesystem, over S3 keys ("bucket/key")."""
    import pyarrow.parquet as pq

    def one(path):
        return pq.read_metadata(path, filesystem=filesystem).num_rows

    return sum(_parallel(one, paths, "reading parquet footers"))


def read_footer_rows(dir: str) -> int:
    """Sum num_rows from every *.parquet footer under a local dir."""
    return parquet_footer_rows(_dir_parquet_paths(dir))


# --- account auto-detection ------------------------------------------------
# Distinct line-item usage-account IDs = the deployment's member-account count.
_ACCOUNT_COLUMNS = ("line_item_usage_account_id", "lineItem/UsageAccountId")


def _pick_account_col(names) -> str | None:
    for c in _ACCOUNT_COLUMNS:
        if c in names:
            return c
    target = "lineitemusageaccountid"
    for n in names:
        if n.lower().replace("/", "").replace("_", "") == target:
            return n
    return None


def parquet_account_ids(paths: list[str], filesystem=None) -> set:
    """Exact distinct usage-account IDs by reading only that one column from each
    parquet file (local paths, or S3 keys with an S3 filesystem)."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    def one(path):
        col = _pick_account_col(pq.read_schema(path, filesystem=filesystem).names)
        if not col:
            return set()
        table = pq.read_table(path, columns=[col], filesystem=filesystem)
        return {str(v) for v in pc.unique(table.column(0)).to_pylist() if v is not None}

    ids: set = set()
    for s in _parallel(one, paths, "reading account ids"):
        ids |= s
    return ids


def _csv_account_ids(line_iter) -> set:
    """Distinct account IDs from one CSV reader (bytes lines, header first)."""
    import csv

    r = csv.reader(ln.decode("utf-8", "replace") for ln in line_iter)
    try:
        header = next(r)
    except StopIteration:
        return set()
    col = _pick_account_col(header)
    if col is None:
        return set()
    idx = header.index(col)
    ids = set()
    for row in r:
        if idx < len(row):
            ids.add(row[idx])
    return ids


@dataclass
class _CsvRec:
    ext: str
    size: int
    open_lines: Callable[[], object]


def _dir_csv_recs(dir: str) -> list:
    recs = []
    for root, _, files in os.walk(dir):
        for name in files:
            ext = data_ext(name)
            if ext not in ("csv", "csv.gz"):
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
    return open(path, "rb")


def _apply_accounts(
    p: CURProfile,
    sample: int,
    parquet_paths: list,
    csv_recs: list,
    csv_accounts: set | None,
    filesystem,
    all_files: bool = False,
) -> None:
    """Populate p.account_* from parquet (exact) and/or CSV sources.

    csv_accounts, if provided, is the set already collected during the CSV row
    scan — reused so a CSV CUR is read only once. It is None only when rows took a
    non-CSV path (pure parquet, or a mixed CUR), in which case the CSV files are
    scanned here. With all_files the CSV count is exact (not a lower bound).
    """
    ids: set = set()
    parts = []
    if p.has_parquet and parquet_paths:
        ids |= parquet_account_ids(parquet_paths, filesystem=filesystem)
        parts.append("parquet (exact)")
    if p.has_csv and csv_recs:
        if csv_accounts is None:
            _, _, _, csv_accounts = scan_csv(csv_recs, sample, True, all_files)
        ids |= csv_accounts or set()
        parts.append("CSV (exact)" if all_files else "CSV sample")
    if not parts:
        return
    p.account_count = len(ids)
    p.have_accounts = True
    # A sampled CSV only sees some files → lower bound; all_files (or parquet) exact.
    p.account_lower_bound = p.has_csv and not all_files
    p.account_source = " + ".join(parts)


# --- S3 (Task 7) -----------------------------------------------------------


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


def profile_from_objs(objs: list, source: str) -> CURProfile:
    p = CURProfile(source=source, granularity="unknown")
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


def profile_from_lister(lister, bucket: str, prefix: str, source: str) -> CURProfile:
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
    if key.lower().endswith(".gz"):
        return gzip.GzipFile(fileobj=io.BytesIO(body.read()))
    # StreamingBody iterates 1 KiB chunks, not lines; iter_lines() yields lines.
    return body.iter_lines()


def _s3_csv_recs(client, bucket: str, objs: list) -> list:
    recs = []
    for o in objs:
        ext = data_ext(base_name(o.key))
        if ext not in ("csv", "csv.gz"):
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
) -> CURProfile:
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
    csv_accounts = None
    if read_footers and p.has_parquet and not p.has_csv:
        try:
            p.raw_line_items = parquet_footer_rows(parquet_keys, _s3_filesystem(region))
            p.have_raw_rows = True
        except Exception as e:  # noqa: BLE001 — degrade to the bytes-only estimate
            _warn(f"S3 parquet footer read failed ({e}); using bytes estimate")
    elif p.has_csv and not p.has_parquet:
        raw, sampled, total, csv_accounts = scan_csv(
            csv_recs, sample, detect_accounts, all_files
        )
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

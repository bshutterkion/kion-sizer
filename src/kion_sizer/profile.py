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

    if read_footers and p.has_parquet and not p.has_csv:
        try:
            p.raw_line_items = read_footer_rows(dir)
            p.have_raw_rows = True
        except Exception as e:  # noqa: BLE001 — degrade to the bytes-only estimate
            _warn(f"parquet footer read failed ({e}); using bytes estimate")
    elif p.has_csv and not p.has_parquet:
        est, sampled, total = sample_dir_raw_rows(dir, sample)
        p.raw_line_items, p.have_raw_rows = est, True
        p.sample_note = (
            f"ESTIMATED by sampling {sampled} of {total} files "
            "(extrapolated by byte share)"
        )
    if detect_accounts:
        try:
            _detect_accounts(
                p, sample, _dir_parquet_paths(dir), _dir_csv_recs(dir), None
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


@dataclass
class _SampleItem:
    size: int
    count: Callable[[], int]


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


def extrapolate_raw_rows(groups: dict, max_sample: int) -> tuple[int, int, int]:
    """Estimate total raw CSV line items across format groups.

    Samples max_sample files per group — spread across the size distribution,
    not just the largest — and extrapolates by that group's byte share, then sums
    groups. Grouping keeps compressed vs uncompressed rows/byte from blending.
    Returns (est, sampled, total).
    """
    if max_sample <= 0:
        max_sample = 3
    total = sum(len(items) for items in groups.values())
    if total == 0:
        raise ProfileError("no csv/csv.gz files to sample")
    plan = []  # (group_bytes, [selected items])
    for items in groups.values():
        group_bytes = sum(it.size for it in items)
        if group_bytes == 0:
            continue
        ordered = sorted(items, key=lambda it: it.size, reverse=True)
        plan.append((group_bytes, _stratified(ordered, max_sample)))
    selected = [it for _, sel in plan for it in sel]
    counts = {}
    for it in progress.track(selected, "sampling CUR rows", unit="file"):
        counts[id(it)] = it.count()
    est = 0
    sampled = 0
    for group_bytes, sel in plan:
        s_rows = sum(counts[id(it)] for it in sel)
        s_bytes = sum(it.size for it in sel)
        sampled += len(sel)
        if s_bytes > 0:
            est += int(s_rows / s_bytes * group_bytes)
    return est, sampled, total


def sample_dir_raw_rows(dir: str, max_sample: int) -> tuple[int, int, int]:
    groups: dict[str, list[_SampleItem]] = {}
    for root, _, files in os.walk(dir):
        for name in files:
            ext = data_ext(name)
            if ext not in ("csv", "csv.gz"):
                continue
            path = os.path.join(root, name)
            size = os.path.getsize(path)
            groups.setdefault(ext, []).append(
                _SampleItem(size=size, count=(lambda p=path: count_data_rows(p)))
            )
    return extrapolate_raw_rows(groups, max_sample)


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


def _select_csv_recs(recs: list, sample: int) -> list:
    groups: dict[str, list] = {}
    for r in recs:
        groups.setdefault(r.ext, []).append(r)
    out = []
    for items in groups.values():
        out += _stratified(sorted(items, key=lambda r: r.size, reverse=True), sample)
    return out


def csv_account_ids(recs: list, sample: int) -> set:
    """Distinct account IDs from the same stratified CSV sample used for rows —
    a LOWER BOUND (accounts only in unsampled files are missed)."""
    ids: set = set()
    for rec in progress.track(
        _select_csv_recs(recs, sample), "reading account ids", unit="file"
    ):
        ids |= _csv_account_ids(rec.open_lines())
    return ids


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


def _detect_accounts(
    p: CURProfile, sample: int, parquet_paths: list, csv_recs: list, filesystem
) -> None:
    """Populate p.account_* from parquet (exact) and/or CSV (sampled) sources."""
    ids: set = set()
    parts = []
    if p.has_parquet and parquet_paths:
        ids |= parquet_account_ids(parquet_paths, filesystem=filesystem)
        parts.append("parquet (exact)")
    if p.has_csv and csv_recs:
        ids |= csv_account_ids(csv_recs, sample)
        parts.append("CSV sample")
    if not parts:
        return
    p.account_count = len(ids)
    p.have_accounts = True
    p.account_lower_bound = p.has_csv  # CSV path only sees sampled files
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
    groups: dict[str, list[_SampleItem]] = {}
    for o in objs:
        ext = data_ext(base_name(o.key))
        if ext not in ("csv", "csv.gz"):
            continue
        key = o.key
        is_gz = key.lower().endswith(".gz")

        def _count(k=key, gz=is_gz) -> int:
            body = client.get_object(Bucket=bucket, Key=k)["Body"]
            if gz:
                with gzip.GzipFile(fileobj=io.BytesIO(body.read())) as g:
                    return count_rows_from_reader(g)
            # boto3's StreamingBody iterates by 1 KiB chunks, not lines, so feed
            # iter_lines() to count_rows_from_reader (which counts one per item).
            return count_rows_from_reader(body.iter_lines())

        groups.setdefault(ext, []).append(_SampleItem(size=o.size, count=_count))
    return extrapolate_raw_rows(groups, max_sample)


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
    if read_footers and p.has_parquet and not p.has_csv:
        try:
            p.raw_line_items = parquet_footer_rows(parquet_keys, _s3_filesystem(region))
            p.have_raw_rows = True
        except Exception as e:  # noqa: BLE001 — degrade to the bytes-only estimate
            _warn(f"S3 parquet footer read failed ({e}); using bytes estimate")
    elif p.has_csv and not p.has_parquet:
        raw, sampled, total = _sample_s3_raw_rows(client, bucket, objs, sample)
        p.raw_line_items, p.have_raw_rows = raw, True
        p.sample_note = (
            f"ESTIMATED by sampling {sampled} of {total} S3 files "
            "(extrapolated by byte share)"
        )
    if detect_accounts:
        try:
            fs = _s3_filesystem(region) if p.has_parquet else None
            _detect_accounts(
                p, sample, parquet_keys, _s3_csv_recs(client, bucket, objs), fs
            )
        except Exception as e:  # noqa: BLE001 — account detection is best-effort
            _warn(f"account detection failed ({e}); skipping")
    return p

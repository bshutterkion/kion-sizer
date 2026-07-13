"""Turns a customer CUR (local dir or S3) into a CURProfile.

Mirrors internal/profile/{profile,s3,footers,sample}.go.
"""

from __future__ import annotations

import gzip
import io
import os
from dataclasses import dataclass
from typing import Callable


class ProfileError(Exception):
    pass


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


def from_dir(dir: str) -> CURProfile:
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


def extrapolate_raw_rows(groups: dict, max_sample: int) -> tuple[int, int, int]:
    """Estimate total raw CSV line items across format groups.

    Samples the largest max_sample files per group and extrapolates by that
    group's byte share, then sums groups. Grouping keeps compressed vs
    uncompressed rows/byte from blending. Returns (est, sampled, total).
    """
    if max_sample <= 0:
        max_sample = 3
    total = sum(len(items) for items in groups.values())
    if total == 0:
        raise ProfileError("no csv/csv.gz files to sample")
    est = 0
    sampled = 0
    for items in groups.values():
        group_bytes = sum(it.size for it in items)
        if group_bytes == 0:
            continue
        items = sorted(items, key=lambda it: it.size, reverse=True)
        n = min(max_sample, len(items))
        s_rows = 0
        s_bytes = 0
        for i in range(n):
            s_rows += items[i].count()
            s_bytes += items[i].size
        sampled += n
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


def read_footer_rows(dir: str) -> int:
    """Sum num_rows from every *.parquet footer under dir (RAW CUR line items).

    Reads only the file metadata (footer), never row data.
    """
    import pyarrow.parquet as pq

    total = 0
    for root, _, files in os.walk(dir):
        for name in files:
            if not name.lower().endswith(".parquet"):
                continue
            path = os.path.join(root, name)
            total += pq.read_metadata(path).num_rows
    return total


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


def from_s3(uri: str) -> CURProfile:
    import boto3

    bucket, prefix = parse_s3_uri(uri)
    loc_client = boto3.client("s3", region_name="us-east-1")
    region = _resolve_region(loc_client, bucket)
    client = boto3.client("s3", region_name=region)

    objs = _aws_list(client, bucket, prefix)
    p = profile_from_objs(objs, uri)
    if p.has_csv and not p.has_parquet:
        raw, sampled, total = _sample_s3_raw_rows(client, bucket, objs, 3)
        p.raw_line_items, p.have_raw_rows = raw, True
        p.sample_note = (
            f"ESTIMATED by sampling {sampled} of {total} S3 files "
            "(extrapolated by byte share)"
        )
    return p

"""Cloud-agnostic billing-profile machinery.

Everything here works on any source that can hand back byte-sized records
with a line iterator (_CsvRec) or a list of parquet paths: stratified
sampling, the parallel scan, account detection, and footer reads. The
per-cloud modules beside this one supply those records.
"""

from __future__ import annotations

import gzip
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from .. import progress


class ProfileError(Exception):
    pass


def _warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


def missing_sdk(cloud: str, package: str) -> None:
    """Raise a ProfileError telling the user exactly how to install the SDK.

    An ImportError traceback from deep inside a lazy import tells a field
    engineer nothing; this names the extra to install.
    """
    raise ProfileError(
        f"the {cloud} source needs the {package!r} package, which is not "
        f"installed. Install it with: uv sync --extra {cloud}"
    )


@dataclass
class BillingProfile:
    source: str = ""
    # Which cloud this billing data came from, and how to describe its units.
    # Set by the source module; render uses them so a non-file source is not
    # described as "files".
    cloud: str = "aws"
    unit_label: str = "files"
    byte_label: str = "compressed bytes"
    volume_note: str = ""
    # Which report format this is: cur / dbr / focus / standard / detailed /
    # usage-details / ... Row volume depends on the format as much as on the
    # provider, so calibration resolves on the (cloud, report_format) pair.
    report_format: str = ""
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
        elif ext in ("csv", "csv.gz", "csv.zip"):
            self.has_csv = True


def data_ext(name: str) -> str:
    n = name.lower()
    if n.endswith(".parquet"):
        return "parquet"
    if n.endswith(".csv.gz"):
        return "csv.gz"
    if n.endswith(".csv.zip"):
        return "csv.zip"  # AWS DBR ships this way
    if n.endswith(".csv"):
        return "csv"
    return ""


def merge_format(cur: str, nxt: str) -> str:
    if cur == "" or cur == nxt:
        return nxt
    return "mixed"




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


def _scan_one(
    rec: "_CsvRec", detect_accounts: bool, row_filter=None
) -> tuple[int, set | None]:
    """Read one CSV file exactly once. Returns (data_row_count, account_ids).

    account_ids is None when detection is off (the fast line-count path); when on,
    a single csv parse yields both the row count and the account column values.

    `row_filter(header, row) -> bool` drops rows the product would not ingest --
    AWS DBR files carry AccountTotal / InvoiceTotal / StatementTotal / Rounding
    rows alongside real line items, and counting those would over-size. Supplying
    a filter forces the parsing path even when accounts are not wanted, because
    lines cannot be filtered without being parsed.
    """
    reader = rec.open_lines()
    if not detect_accounts and row_filter is None:
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
        if row_filter is not None and not row_filter(header, row):
            continue
        rows += 1
        if idx is not None and idx < len(row):
            ids.add(row[idx])
    return rows, (ids if detect_accounts else None)


# Bounded so a fleet of large gzip files (each buffered compressed in memory) can't
# blow CloudShell's RAM; still enough to overlap S3 download + decompress + parse.
_CSV_SCAN_WORKERS = 8


def scan_csv(
    recs: list,
    max_sample: int,
    detect_accounts: bool = False,
    all_files: bool = False,
    row_filter=None,
    unit: str = "CUR files",
) -> tuple[int, int, int, set | None]:
    """Read CSV files (each exactly once, in parallel) to estimate total raw rows
    by byte share and, when detect_accounts is set, collect distinct account IDs
    from the files read. With all_files the whole set is read (exact rows, and an
    exact account count); otherwise a stratified sample per format is read and the
    result is an estimate / account lower bound. Returns
    (est_rows, sampled, total, accounts_or_None). `unit` names what is being
    read, for the progress bar only.
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

    # `unit` matches the profile's unit_label, so an Azure run says "blobs" and
    # an OCI run says "reports" rather than claiming to read a CUR.
    desc = f"reading all {unit}" if all_files else f"sampling {unit}"
    results = _parallel(
        lambda rec: _scan_one(rec, detect_accounts, row_filter),
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




# --- parquet footers (Task 6) ----------------------------------------------




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




# --- account auto-detection ------------------------------------------------
# Distinct line-item usage-account IDs = the deployment's member-account count.
# Every account-identity column we may meet, in preference order. A single file
# only ever carries one of these, so trying them all lets one scanner serve AWS
# CUR, Azure usage details (EA and MCA spell it differently), and FOCUS from any
# provider. This mirrors how Kion's own parser resolves aliases: case-insensitive,
# first match wins.
_ACCOUNT_COLUMNS = (
    "line_item_usage_account_id",   # AWS CUR (parquet)
    "lineItem/UsageAccountId",      # AWS CUR (CSV)
    "SubAccountId",                 # FOCUS, any provider
    "SubscriptionId",               # Azure usage details (EA)
    "SubscriptionGuid",             # Azure usage details (MCA)
    "LinkedAccountId",              # AWS DBR (legacy)
    "product/compartmentId",        # OCI cost report -- Kion maps compartment to account
    "lineItem/tenantId",            # OCI cost report -- single-tenancy fallback
)


def _pick_account_col(names) -> str | None:
    for c in _ACCOUNT_COLUMNS:
        if c in names:
            return c
    # Fall back to a separator-insensitive comparison, so lineItem/UsageAccountId
    # and line_item_usage_account_id match the same target.
    def norm(s):
        return s.lower().replace("/", "").replace("_", "")

    targets = {norm(c) for c in _ACCOUNT_COLUMNS}
    for n in names:
        if norm(n) in targets:
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






def _apply_accounts(
    p: BillingProfile,
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

























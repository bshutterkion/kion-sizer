"""Azure billing source: Cost Management exports in a storage container.

Exports land as

    {RootFolder}/{ExportName}/{YYYYMMDD-YYYYMMDD}/{run-id}/
        manifest.json
        part_0_0001.csv[.gz] | part_0_0001.parquet

and carry either the proprietary usage-details schema (whose column names differ
between EA and MCA billing accounts) or FOCUS. Which one is detected from the
column headers, because the customer should not have to know.

Two export shapes exist and Kion reads both (matching how Kion selects one):
a *partitioned* export with a manifest inside a run-id folder, and an
*unpartitioned* one that drops a whole-month CSV straight into the period folder.

Three hazards drive the design:

  * Partitioned: overwrite is on by default, so several run-id folders coexist
    for one period. Summing across them double-counts, so one run is selected.
  * Unpartitioned: every export run leaves another whole-month CSV as a
    *sibling* with a fresh GUID, and they accumulate indefinitely. A live Gov
    tenant was observed holding 36 copies of one month. Grouping by directory
    sees them as a single run, so counting them all would have multiplied that
    month by 36. Kion processes exactly one of them,
    and so does this module.
  * Commercial and GovCloud manifests have different shapes. Kion's own reader
    carries this warning. Only blobs[].blobName and blobs[].byteCount are read,
    unknown fields are ignored, and an unparseable manifest degrades to listing
    the run folder rather than failing the run.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import posixpath
import re
from dataclasses import dataclass

from . import schema
from .base import (
    BillingProfile,
    ProfileError,
    _apply_accounts,
    _CsvRec,
    _pick_account_col,
    _warn,
    data_ext,
    merge_format,
    missing_sdk,
    scan_csv,
)

MANIFEST_NAME = "manifest.json"

# A billing period folder: 20260601-20260630.
_PERIOD_RE = re.compile(r"(\d{8})-(\d{8})")


@dataclass
class BlobRec:
    name: str
    size: int
    last_modified: object = None


def parse_container_url(url: str) -> tuple[str, str, str]:
    """`https://acct.blob.core.windows.net/container/path` -> (account_url, container, prefix)."""
    if not url.startswith("https://"):
        raise ProfileError(
            f'not an Azure blob URL: "{url}" '
            "(expected https://<account>.blob.core.windows.net/<container>/<path>)"
        )
    rest = url[len("https://") :]
    host, _, tail = rest.partition("/")
    if not host or not tail:
        raise ProfileError(f'missing container in Azure blob URL: "{url}"')
    container, _, prefix = tail.partition("/")
    if not container:
        raise ProfileError(f'missing container in Azure blob URL: "{url}"')
    return f"https://{host}", container, prefix.strip("/")


def period_of(blob_name: str) -> str:
    """The billing period folder token in a blob path, or ""."""
    m = _PERIOD_RE.search(blob_name)
    return m.group(0) if m else ""


def run_of(blob_name: str) -> str:
    """The run-id folder: the directory holding the manifest / parts."""
    return posixpath.dirname(blob_name)


def group_runs(blobs: list) -> dict:
    """Group blobs into {period: {run_dir: [BlobRec]}}."""
    out: dict = {}
    for b in blobs:
        period = period_of(b.name)
        if not period:
            continue
        out.setdefault(period, {}).setdefault(run_of(b.name), []).append(b)
    return out


def pick_run(runs: dict) -> tuple[str, list]:
    """Choose exactly one run folder for a period: the most recently written.

    Ranked by the newest last_modified in the folder, so a re-run supersedes the
    run it replaced. Summing across runs would double-count.
    """
    def freshness(item):
        _, blobs = item
        stamps = [b.last_modified for b in blobs if b.last_modified is not None]
        return max(stamps) if stamps else None

    ranked = [r for r in runs.items() if freshness(r) is not None]
    if not ranked:
        # No timestamps (a fake, or an odd listing): fall back to name order so
        # the choice is at least deterministic.
        run_dir = sorted(runs)[-1]
        return run_dir, runs[run_dir]
    return max(ranked, key=freshness)


def parse_manifest(raw: bytes) -> list:
    """Blob names and byte counts from an export manifest.

    Only the two fields we need are read, so a commercial/GovCloud shape
    difference elsewhere in the document does not matter.
    """
    doc = json.loads(raw)
    blobs = doc.get("blobs") or doc.get("Blobs") or []
    out = []
    for b in blobs:
        name = b.get("blobName") or b.get("BlobName")
        if not name:
            continue
        size = b.get("byteCount") or b.get("ByteCount") or 0
        out.append(BlobRec(name=name.lstrip("/"), size=int(size)))
    return out


class _BlobFile(io.RawIOBase):
    """Seekable file-like view of a blob, backed by ranged reads.

    Lets pyarrow read a parquet footer without downloading the whole object,
    preserving the "footer bytes only" promise the AWS path makes.
    """

    def __init__(self, container, name: str, size: int):
        self._c, self._name, self._size, self._pos = container, name, size, 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._pos

    def seek(self, offset, whence=0):
        try:
            base = {0: 0, 1: self._pos, 2: self._size}[whence]
        except KeyError:
            # io.IOBase requires ValueError here, not KeyError.
            raise ValueError(f"invalid whence value: {whence!r}") from None
        self._pos = max(0, min(self._size, base + offset))
        return self._pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self._size - self._pos
        n = min(n, self._size - self._pos)
        if n <= 0:
            return b""
        data = self._c.download_blob(self._name, offset=self._pos, length=n).readall()
        self._pos += len(data)
        return data


def _open_lines(container, name: str):
    data = container.download_blob(name).readall()
    if name.lower().endswith(".gz"):
        return gzip.GzipFile(fileobj=io.BytesIO(data))
    return io.BytesIO(data)


def _header_of(container, blobs: list) -> list:
    """Column names from the first readable part, for format detection."""
    for b in blobs:
        ext = data_ext(posixpath.basename(b.name))
        if ext in ("csv", "csv.gz"):
            import csv as _csv

            reader = _open_lines(container, b.name)
            line = next(iter(reader), b"")
            text = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
            return next(_csv.reader([text]), [])
        if ext == "parquet":
            try:
                import pyarrow.parquet as pq
            except ImportError:
                missing_sdk("azure", "pyarrow")
            return list(pq.read_schema(_BlobFile(container, b.name, b.size)).names)
    return []


def detect_format(columns: list) -> str:
    """"focus" or "usage-details", from the column names present."""
    if schema.pick(schema.ACCOUNT_COLUMNS[schema.FOCUS], columns):
        return "focus"
    if schema.pick(schema.ACCOUNT_COLUMNS[schema.AZURE_NATIVE], columns):
        return "usage-details"
    return "unknown"


# A read/list SAS token for the container, taken from the environment rather
# than a flag so it never lands in shell history or a process listing. It covers
# the common case of an operator who can read the storage account's keys
# (Contributor) but holds no blob data role, which DefaultAzureCredential needs.
SAS_ENV = "KION_SIZER_AZURE_SAS"


def _credential():
    sas = os.environ.get(SAS_ENV, "").strip().lstrip("?")
    if sas:
        return sas
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()


def _make_container(account_url: str, container: str):
    try:
        import azure.identity  # noqa: F401
        from azure.storage.blob import ContainerClient
    except ImportError:
        missing_sdk("azure", "azure-storage-blob")
    try:
        return ContainerClient(
            account_url=account_url,
            container_name=container,
            credential=_credential(),
        )
    except Exception as e:  # noqa: BLE001 — surface auth/config failure as ours
        raise ProfileError(
            f"could not open Azure container {container!r}: {e}. Check that "
            "credentials are available (az login with Storage Blob Data Reader, "
            f"a service principal, or a container SAS token in {SAS_ENV})."
        ) from e


def _list(container, prefix: str) -> list:
    try:
        return [
            BlobRec(name=b.name, size=int(b.size or 0), last_modified=b.last_modified)
            for b in container.list_blobs(name_starts_with=prefix)
        ]
    except Exception as e:  # noqa: BLE001 — a failed listing is fatal for profiling
        raise ProfileError(
            f"could not list Azure blobs under {prefix!r}: {e}. Reading an export "
            "needs Storage Blob Data Reader on the container, or a read/list SAS "
            f"token in {SAS_ENV}."
        ) from e


def from_container(
    url: str,
    *,
    month: str = "",
    client=None,
    sample: int = 20,
    detect_accounts: bool = False,
    all_files: bool = False,
    **_ignored,
) -> BillingProfile:
    """Profile one billing period of an Azure Cost Management export."""
    account_url, container_name, prefix = parse_container_url(url)
    container = client or _make_container(account_url, container_name)

    all_blobs = _list(container, prefix)
    periods = group_runs(all_blobs)
    if not periods:
        raise ProfileError(
            f"no billing-period folders (YYYYMMDD-YYYYMMDD) found under {url}"
        )

    if month:
        matching = {p: r for p, r in periods.items() if p.startswith(month)}
        if not matching:
            raise ProfileError(
                f"no export for month {month} under {url} "
                f"(periods present: {', '.join(sorted(periods))})"
            )
        period = sorted(matching)[-1]
    else:
        # Peak period by total bytes, matching how the AWS discovery picks a month.
        period = max(
            periods,
            key=lambda p: sum(b.size for runs in [periods[p]] for r in runs.values() for b in r),
        )

    period_blobs = [b for runs in [periods[period]] for r in runs.values() for b in r]
    data_blobs = [b for b in period_blobs if data_ext(posixpath.basename(b.name)) != ""]

    # Two export shapes exist, and Kion handles both
    # (matching how Kion selects one):
    #
    #   partitioned    {period}/{run-id}/manifest.json + parts
    #   unpartitioned  {period}/Export_<guid>.csv -- one whole month per file,
    #                  accumulating a new GUID on every export run
    #
    # The unpartitioned shape is the dangerous one. Every run drops another
    # full-month CSV as a *sibling*, so grouping by directory sees a single
    # "run" holding N complete copies of the month. Kion processes exactly one
    # (a single path when not partitioned), and so
    # must we, or the row count multiplies by how many times the export has run.
    has_manifest = any(
        posixpath.basename(b.name) == MANIFEST_NAME for b in period_blobs
    )
    note_extra = ""

    if has_manifest:
        run_dir, run_blobs = pick_run(periods[period])
        run_count = len(periods[period])
        parts = []
        manifest_blob = next(
            (b for b in run_blobs if posixpath.basename(b.name) == MANIFEST_NAME), None
        )
        if manifest_blob is not None:
            try:
                parts = parse_manifest(
                    container.download_blob(manifest_blob.name).readall()
                )
            except Exception as e:  # noqa: BLE001 — degrade to the listing
                _warn(
                    f"Azure manifest unreadable ({e}); falling back to the blob listing"
                )
        if not parts:
            parts = [b for b in run_blobs if data_ext(posixpath.basename(b.name)) != ""]
        parts = [p for p in parts if data_ext(posixpath.basename(p.name)) != ""]
        if not parts:
            raise ProfileError(f"no data parts found in {run_dir}")
        if run_count > 1:
            note_extra = (
                f"{run_count} run folders present, counted only the newest "
                f"({posixpath.basename(run_dir)})"
            )
    else:
        if not data_blobs:
            raise ProfileError(f"no data parts found for period {period}")
        newest = max(
            data_blobs,
            key=lambda b: (b.last_modified is not None, b.last_modified, b.name),
        )
        parts = [newest]
        note_extra = "unpartitioned export"
        if len(data_blobs) > 1:
            note_extra = (
                f"unpartitioned export; {len(data_blobs)} full-month files present, "
                f"counted only the newest ({posixpath.basename(newest.name)})"
            )

    # Sizes come from the manifest when it had them, else from the listing.
    sizes = {b.name: b.size for b in period_blobs}
    for p in parts:
        if not p.size:
            p.size = sizes.get(p.name, 0)

    fmt = detect_format(_header_of(container, parts))

    p = BillingProfile(
        source=f"{url.rstrip('/')} [{period}]",
        cloud="azure",
        unit_label="blobs",
        byte_label="compressed bytes",
        granularity="unknown",
    )
    p.report_format = fmt
    for part in parts:
        ext = data_ext(posixpath.basename(part.name))
        p.file_count += 1
        p.compressed_bytes += part.size
        p.format = merge_format(p.format, ext)
        p.set_format_flags(ext)

    csv_recs = [
        _CsvRec(
            ext=data_ext(posixpath.basename(b.name)),
            size=b.size,
            open_lines=(lambda n=b.name: _open_lines(container, n)),
        )
        for b in parts
        if data_ext(posixpath.basename(b.name)) in ("csv", "csv.gz")
    ]
    parquet_parts = [b for b in parts if data_ext(posixpath.basename(b.name)) == "parquet"]

    csv_accounts = None
    if parquet_parts and not csv_recs:
        try:
            import pyarrow.parquet as pq

            p.raw_line_items = sum(
                pq.read_metadata(_BlobFile(container, b.name, b.size)).num_rows
                for b in parquet_parts
            )
            p.have_raw_rows = True
        except Exception as e:  # noqa: BLE001 — degrade to the bytes-only estimate
            _warn(f"Azure parquet footer read failed ({e}); using bytes estimate")
    elif csv_recs:
        est, sampled, total, csv_accounts = scan_csv(
            csv_recs, sample, detect_accounts, all_files, unit="blobs"
        )
        # A sample that reached every file is a full read: exact, not an estimate.
        all_files = all_files or sampled == total
        p.raw_line_items, p.have_raw_rows = est, True
        p.sample_note = (
            f"exact count from all {total} blobs"
            if all_files
            else f"ESTIMATED by sampling {sampled} of {total} blobs "
            "(extrapolated by byte share)"
        )

    if detect_accounts:
        try:
            _apply_accounts(p, sample, [], csv_recs, csv_accounts, None, all_files)
            if p.have_accounts:
                p.account_source = f"Azure export ({fmt} columns)" + (
                    "" if all_files else ", sampled"
                )
        except Exception as e:  # noqa: BLE001 — account detection is best-effort
            _warn(f"account detection failed ({e}); skipping")

    note = f"period {period}; {fmt} export"
    if note_extra:
        note += f"; {note_extra}"
    if fmt == "focus":
        note += "; FOCUS folds actual and amortized cost into one dataset"
    elif fmt == "unknown":
        note += "; no recognized account column, confirm this is a billing export"
    p.volume_note = note
    return p


__all__ = [
    "BlobRec",
    "detect_format",
    "from_container",
    "group_runs",
    "parse_container_url",
    "parse_manifest",
    "period_of",
    "pick_run",
]

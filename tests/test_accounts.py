"""Account auto-detection + parquet footer/column reads over a filesystem.

The S3 paths funnel through pyarrow's filesystem abstraction, so a LocalFileSystem
exercises the same reader code the S3FileSystem would — no real AWS needed.
"""

import gzip

import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq

from kion_sizer import profile


def _write_parquet(path, accounts, n_extra=0):
    cols = {"line_item_usage_account_id": accounts, "x": list(range(len(accounts)))}
    pq.write_table(pa.table(cols), path)


# --- column picking ---------------------------------------------------------
def test_pick_account_col_snake_and_camel():
    assert profile._pick_account_col(["a", "line_item_usage_account_id"]) == (
        "line_item_usage_account_id"
    )
    assert profile._pick_account_col(["lineItem/UsageAccountId", "b"]) == (
        "lineItem/UsageAccountId"
    )
    # normalized fallback for odd casing/separators
    assert profile._pick_account_col(["LineItem_UsageAccountID"]) == (
        "LineItem_UsageAccountID"
    )
    assert profile._pick_account_col(["unrelated"]) is None


# --- parquet footer + column reads via a filesystem -------------------------
def test_parquet_footer_rows_via_filesystem(tmp_path):
    _write_parquet(str(tmp_path / "a.parquet"), ["1", "1", "2"])
    _write_parquet(str(tmp_path / "b.parquet"), ["3", "4"])
    fs = pafs.LocalFileSystem()
    paths = [str(tmp_path / "a.parquet"), str(tmp_path / "b.parquet")]
    assert profile.parquet_footer_rows(paths, filesystem=fs) == 5


def test_parquet_account_ids_exact(tmp_path):
    _write_parquet(str(tmp_path / "a.parquet"), ["100", "100", "200"])
    _write_parquet(str(tmp_path / "b.parquet"), ["200", "300"])
    fs = pafs.LocalFileSystem()
    paths = [str(tmp_path / "a.parquet"), str(tmp_path / "b.parquet")]
    assert profile.parquet_account_ids(paths, filesystem=fs) == {"100", "200", "300"}


def test_parquet_account_ids_missing_column(tmp_path):
    pq.write_table(pa.table({"other": [1, 2]}), str(tmp_path / "a.parquet"))
    assert profile.parquet_account_ids([str(tmp_path / "a.parquet")]) == set()


# --- CSV account reads ------------------------------------------------------
def test_csv_account_ids_reader():
    data = b"lineItem/UsageAccountId,cost\n100,1\n100,2\n200,3\n"
    assert profile._csv_account_ids(iter(data.split(b"\n"))) == {"100", "200"}


def test_csv_account_ids_no_column():
    data = b"foo,bar\n1,2\n"
    assert profile._csv_account_ids(iter(data.split(b"\n"))) == set()


# --- from_dir integration ---------------------------------------------------
def test_from_dir_detect_accounts_parquet_exact(tmp_path):
    _write_parquet(str(tmp_path / "a.parquet"), ["100", "100", "200"])
    _write_parquet(str(tmp_path / "b.parquet"), ["300"])
    p = profile.from_dir(str(tmp_path), read_footers=True, detect_accounts=True)
    assert p.raw_line_items == 4  # exact footer rows
    assert p.account_count == 3  # {100,200,300}
    assert p.account_lower_bound is False
    assert p.account_source == "parquet (exact)"


def test_scan_csv_reads_each_file_once_when_detecting():
    # Rows AND accounts must come from a single read per file (no double I/O).
    opens = {"a": 0, "b": 0}

    def make(name, body):
        def open_lines(n=name, b=body):
            opens[n] += 1
            return iter(b.split(b"\n"))

        return profile._CsvRec(ext="csv", size=len(body), open_lines=open_lines)

    recs = [
        make("a", b"lineItem/UsageAccountId,c\n100,1\n200,2\n"),
        make("b", b"lineItem/UsageAccountId,c\n300,3\n"),
    ]
    est, sampled, total, accounts = profile.scan_csv(recs, 10, detect_accounts=True)
    assert accounts == {"100", "200", "300"}
    assert sampled == 2 and total == 2
    assert opens == {"a": 1, "b": 1}  # exactly one read each, not two


def test_from_dir_detect_accounts_csv_lower_bound(tmp_path):
    (tmp_path / "a.csv").write_bytes(b"lineItem/UsageAccountId,c\n100,1\n200,2\n")
    with gzip.open(tmp_path / "b.csv.gz", "wb") as w:
        w.write(b"lineItem/UsageAccountId,c\n300,9\n")
    p = profile.from_dir(str(tmp_path), detect_accounts=True)
    assert p.account_count == 3
    assert p.account_lower_bound is True
    assert p.account_source == "CSV sample"


def test_all_files_reads_everything_exact(tmp_path):
    # 30 CSV files; --sample would read 20, but --all-files reads all 30 → exact
    # rows and an exact (not lower-bound) account count.
    for i in range(30):
        (tmp_path / f"f{i:02d}.csv").write_bytes(
            f"lineItem/UsageAccountId,c\n{1000 + i},1\n".encode()
        )
    p = profile.from_dir(str(tmp_path), sample=20, detect_accounts=True, all_files=True)
    assert p.account_count == 30  # every distinct account, not a sampled subset
    assert p.account_lower_bound is False
    assert p.account_source == "CSV (exact)"
    assert "exact count from all 30 files" in p.sample_note


def test_scan_csv_parallel_results_match_sequential(tmp_path):
    # Parallel execution must not scramble the per-file row accounting.
    recs = []
    for i in range(12):
        body = b"h1,h2\n" + b"x,y\n" * (i + 1)
        # rstrip so the fake reader yields lines like a real file (no trailing "")
        recs.append(
            profile._CsvRec(
                ext="csv",
                size=len(body),
                open_lines=(lambda b=body: iter(b.rstrip(b"\n").split(b"\n"))),
            )
        )
    est, sampled, total, _ = profile.scan_csv(recs, 12)  # sample all 12
    assert total == 12 and sampled == 12
    assert est == sum(range(1, 13))  # 1+2+...+12, exact since all read


def test_detect_accounts_off_by_default(tmp_path):
    _write_parquet(str(tmp_path / "a.parquet"), ["100"])
    p = profile.from_dir(str(tmp_path))
    assert p.have_accounts is False
    assert p.account_count == 0


def test_corrupt_parquet_degrades_without_crashing(tmp_path, capsys):
    # A non-parquet file with a .parquet name must not blow up the run: footers
    # fall back to the bytes estimate and account detection is skipped.
    (tmp_path / "bad.parquet").write_bytes(b"not a parquet file")
    p = profile.from_dir(str(tmp_path), read_footers=True, detect_accounts=True)
    assert p.have_raw_rows is False  # degraded to bytes path
    assert p.have_accounts is False  # detection skipped
    assert "failed" in capsys.readouterr().err  # warned on stderr

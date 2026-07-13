import pyarrow as pa
import pyarrow.parquet as pq

from kion_sizer import profile


def _write_parquet(path, n):
    table = pa.table({"a": list(range(n))})
    pq.write_table(table, path)


def test_read_footer_rows_sums_num_rows(tmp_path):
    _write_parquet(str(tmp_path / "a.parquet"), 100)
    _write_parquet(str(tmp_path / "b.parquet"), 50)
    assert profile.read_footer_rows(str(tmp_path)) == 150

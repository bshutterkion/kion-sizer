import gzip
import io

from kion_sizer import profile


def test_count_rows_from_reader_excludes_header():
    n = profile.count_rows_from_reader(io.BytesIO(b"h1,h2\na,b\nc,d\ne,f\n"))
    assert n == 3


def test_count_data_rows_gz(tmp_path):
    p = tmp_path / "f.csv.gz"
    with gzip.open(p, "wb") as w:
        w.write(b"col1,col2\n" + b"a,b\n" * 100)
    assert profile.count_data_rows(str(p)) == 100


class _Item:
    def __init__(self, size):
        self.size = size


def test_stratified_spreads_across_size_range():
    items = [_Item(s) for s in range(10)]  # sizes 0..9
    ordered = sorted(items, key=lambda i: i.size, reverse=True)  # 9..0
    picked = [i.size for i in profile._stratified(ordered, 4)]
    assert picked == [9, 6, 3, 0]  # endpoints + evenly spread, not just the top 4


def test_stratified_returns_all_when_n_ge_count():
    items = [_Item(s) for s in (5, 3, 8)]
    assert len(profile._stratified(items, 10)) == 3


def test_sample_more_files_when_available(tmp_path):
    # 30 files of varying size; --sample 10 should read 10 spread across sizes.
    for i in range(30):
        (tmp_path / f"f{i:02d}.csv").write_bytes(b"h1,h2\n" + b"x,y\n" * (i + 1))
    est, sampled, total = profile.sample_dir_raw_rows(str(tmp_path), 10)
    assert total == 30
    assert sampled == 10
    assert est > 0


def test_sample_dir_mixed_formats_exact(tmp_path):
    (tmp_path / "a.csv").write_bytes(b"h1,h2\n" + b"x,y\n" * 50)
    (tmp_path / "b.csv").write_bytes(b"h1,h2\n" + b"x,y\n" * 30)
    with gzip.open(tmp_path / "c.csv.gz", "wb") as w:
        w.write(b"col1,col2\n" + b"a,b\n" * 100)
    with gzip.open(tmp_path / "d.csv.gz", "wb") as w:
        w.write(b"col1,col2\n" + b"a,b\n" * 20)
    est, sampled, total = profile.sample_dir_raw_rows(str(tmp_path), 3)
    assert est == 200
    assert total == 4
    assert sampled == 4

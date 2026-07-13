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

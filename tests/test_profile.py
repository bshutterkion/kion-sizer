import pytest

from kion_sizer import profile


def test_data_ext_classification():
    assert profile.data_ext("a.parquet") == "parquet"
    assert profile.data_ext("a.CSV.GZ") == "csv.gz"
    assert profile.data_ext("a.csv") == "csv"
    assert profile.data_ext("Manifest.json") == ""


def test_merge_format():
    assert profile.merge_format("", "parquet") == "parquet"
    assert profile.merge_format("parquet", "parquet") == "parquet"
    assert profile.merge_format("parquet", "csv") == "mixed"


def test_from_dir_sums_parquet(tmp_path):
    (tmp_path / "a.parquet").write_bytes(b"x" * 1000)
    (tmp_path / "b.parquet").write_bytes(b"y" * 2000)
    (tmp_path / "Manifest.json").write_bytes(b"{}")
    p = profile.from_dir(str(tmp_path))
    assert p.file_count == 2
    assert p.compressed_bytes == 3000
    assert p.format == "parquet"
    assert p.has_parquet and not p.has_csv


def test_from_dir_no_files_errors(tmp_path):
    with pytest.raises(
        profile.ProfileError, match="no .parquet/.csv/.csv.gz files found"
    ):
        profile.from_dir(str(tmp_path))

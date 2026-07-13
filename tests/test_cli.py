import io

from kion_sizer import cli


def test_run_dir_human_output(tmp_path):
    f = tmp_path / "x.parquet"
    with open(f, "wb") as fh:
        fh.truncate(1 << 30)  # sparse 1 GiB; profiler only reads size
    out = io.StringIO()
    code = cli.run(["--dir", str(tmp_path), "--accounts", "40"], out)
    assert code == 0, out.getvalue()
    assert "RDS:" in out.getvalue()


def test_run_requires_source():
    out = io.StringIO()
    code = cli.run([], out)
    assert code != 0
    assert "exactly one of --dir or --s3" in out.getvalue()


def test_run_both_sources_errors(tmp_path):
    out = io.StringIO()
    code = cli.run(["--dir", str(tmp_path), "--s3", "s3://b/p/"], out)
    assert code == 2

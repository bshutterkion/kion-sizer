import io

from kion_sizer import cli, pricing


def test_run_dir_human_output(tmp_path):
    f = tmp_path / "x.parquet"
    with open(f, "wb") as fh:
        fh.truncate(1 << 30)  # sparse 1 GiB; profiler only reads size
    out = io.StringIO()
    code = cli.run(["--dir", str(tmp_path), "--accounts", "40"], out)
    assert code == 0, out.getvalue()
    assert "RDS:" in out.getvalue()


def test_run_cost_flag_appends_cost_block(tmp_path, monkeypatch):
    # Force the offline snapshot so the test never touches AWS.
    orig = pricing.build_price_table
    monkeypatch.setattr(
        pricing,
        "build_price_table",
        lambda region, rds_class, engine="mysql", **kw: orig(
            region, rds_class, engine, live=False
        ),
    )
    f = tmp_path / "x.parquet"
    with open(f, "wb") as fh:
        fh.truncate(1 << 30)
    out = io.StringIO()
    code = cli.run(["--dir", str(tmp_path), "--cost"], out)
    assert code == 0, out.getvalue()
    assert "Estimated monthly cost" in out.getvalue()
    assert "EC2 alternative" in out.getvalue()


def test_run_no_cost_flag_no_cost_block(tmp_path):
    f = tmp_path / "x.parquet"
    with open(f, "wb") as fh:
        fh.truncate(1 << 30)
    out = io.StringIO()
    code = cli.run(["--dir", str(tmp_path)], out)
    assert code == 0
    assert "Estimated monthly cost" not in out.getvalue()


def test_run_requires_source():
    out = io.StringIO()
    code = cli.run([], out)
    assert code != 0
    assert "exactly one of --dir or --s3" in out.getvalue()


def test_run_both_sources_errors(tmp_path):
    out = io.StringIO()
    code = cli.run(["--dir", str(tmp_path), "--s3", "s3://b/p/"], out)
    assert code == 2

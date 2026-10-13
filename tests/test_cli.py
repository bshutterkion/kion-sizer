import io
import json

from kion_sizer import cli, pricing


def test_run_dir_human_output(tmp_path):
    f = tmp_path / "x.parquet"
    with open(f, "wb") as fh:
        fh.truncate(1 << 30)  # sparse 1 GiB; profiler only reads size
    out = io.StringIO()
    code = cli.run(["--dir", str(tmp_path), "--accounts", "40"], out)
    assert code == 0, out.getvalue()
    assert "database:" in out.getvalue()


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


def test_run_detect_accounts_drives_service_bands(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    accts = [str(100 + i) for i in range(120)]  # 120 distinct member accounts
    pq.write_table(
        pa.table({"line_item_usage_account_id": accts, "x": list(range(len(accts)))}),
        str(tmp_path / "a.parquet"),
    )
    out = io.StringIO()
    code = cli.run(["--dir", str(tmp_path), "--detect-accounts"], out)
    assert code == 0, out.getvalue()
    text = out.getvalue()
    assert "member accounts:  120" in text
    # 120 accounts auto-fed the service bands (no --accounts passed)
    assert "core services:" in text


def test_run_explicit_accounts_overrides_detection(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    pq.write_table(
        pa.table({"line_item_usage_account_id": ["1", "2", "3"]}),
        str(tmp_path / "a.parquet"),
    )
    out = io.StringIO()
    code = cli.run(
        ["--dir", str(tmp_path), "--detect-accounts", "--accounts", "500"], out
    )
    assert code == 0, out.getvalue()
    # detection still reported, but --accounts 500 drives the bands
    assert "member accounts:  3" in out.getvalue()


def test_run_no_cost_flag_no_cost_block(tmp_path):
    f = tmp_path / "x.parquet"
    with open(f, "wb") as fh:
        fh.truncate(1 << 30)
    out = io.StringIO()
    code = cli.run(["--dir", str(tmp_path)], out)
    assert code == 0
    assert "Estimated monthly cost" not in out.getvalue()


def test_rds_from_aws_defaults_region_when_unset(tmp_path, monkeypatch):
    # With no --region and no AWS_REGION, the RDS lookup must get us-east-1, not
    # None (which boto3 rejects with "You must specify a region").
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    seen = {}

    def fake_orderable(region, engine):
        seen["region"] = region
        from kion_sizer import config

        return config.default().target("aws").db_tiers

    monkeypatch.setattr("kion_sizer.targets.aws.orderable_tiers", fake_orderable)
    f = tmp_path / "x.parquet"
    with open(f, "wb") as fh:
        fh.truncate(1 << 30)
    out = io.StringIO()
    code = cli.run(["--dir", str(tmp_path), "--rds-from-aws"], out)
    assert code == 0, out.getvalue()
    assert seen["region"] == "us-east-1"
    assert "orderable in us-east-1" in out.getvalue()


def test_run_requires_source():
    out = io.StringIO()
    code = cli.run([], out)
    assert code != 0
    assert "exactly one of --dir, --s3, --gcp-table, --azure-container" in out.getvalue()


def test_run_both_sources_errors(tmp_path):
    out = io.StringIO()
    code = cli.run(["--dir", str(tmp_path), "--s3", "s3://b/p/"], out)
    assert code == 2


def test_source_and_target_default_to_aws(tmp_path):
    (tmp_path / "x.parquet").write_bytes(b"\0" * 1024)
    out = io.StringIO()
    assert cli.run(["--dir", str(tmp_path), "--json"], out) == 0
    obj = json.loads(out.getvalue())
    assert obj["source"]["cloud"] == "aws"
    assert obj["target"]["cloud"] == "aws"


def test_unimplemented_target_is_a_clean_error(tmp_path):
    (tmp_path / "x.parquet").write_bytes(b"\0" * 1024)
    out = io.StringIO()
    code = cli.run(["--dir", str(tmp_path), "--target", "azure"], out)
    assert code == 1
    assert "not yet implemented" in out.getvalue()


def test_gcp_target_is_a_usage_error_pointing_at_source(tmp_path):
    (tmp_path / "x.parquet").write_bytes(b"\0" * 1024)
    out = io.StringIO()
    code = cli.run(["--dir", str(tmp_path), "--target", "gcp"], out)
    assert code == 2
    assert "billing source only" in out.getvalue()


def test_source_that_cannot_read_the_location_fails_cleanly(tmp_path):
    (tmp_path / "x.parquet").write_bytes(b"\0" * 1024)
    out = io.StringIO()
    code = cli.run(["--dir", str(tmp_path), "--source", "oci"], out)
    assert code == 1
    assert "error:" in out.getvalue()


def test_source_contradicting_the_location_flag_is_a_usage_error():
    out = io.StringIO()
    code = cli.run(["--s3", "s3://b/p/", "--source", "gcp"], out)
    assert code == 2
    assert "contradicts" in out.getvalue()


def test_invalid_source_choice_is_a_usage_error():
    out = io.StringIO()
    assert cli.run(["--dir", "/tmp", "--source", "aliyun"], out) == 2


def test_gcp_table_implies_the_gcp_source(monkeypatch):
    # Patch client creation so the test never reaches the network; what matters
    # is that resolution routed to the gcp source at all.
    from kion_sizer.sources import base

    def no_client(project):
        raise base.ProfileError("no credentials (test)")

    monkeypatch.setattr("kion_sizer.sources.gcp._make_client", no_client)
    out = io.StringIO()
    code = cli.run(["--gcp-table", "proj-1234.billing_export.gcp_billing_export_v1_ABC", "--json"], out)
    assert code == 1
    assert "no credentials (test)" in out.getvalue()
    assert "exactly one of" not in out.getvalue()


def test_gcp_table_contradicting_source_is_a_usage_error():
    out = io.StringIO()
    code = cli.run(["--gcp-table", "proj-1234.billing_export.gcp_billing_export_v1_ABC", "--source", "aws"], out)
    assert code == 2
    assert "contradicts" in out.getvalue()


def test_two_location_flags_is_a_usage_error(tmp_path):
    out = io.StringIO()
    code = cli.run(["--dir", str(tmp_path), "--gcp-table", "proj-1234.billing_export.gcp_billing_export_v1_ABC"], out)
    assert code == 2
    assert "exactly one of" in out.getvalue()


def test_file_scan_flags_warn_on_a_non_file_source(monkeypatch):
    from kion_sizer.sources import base

    monkeypatch.setattr(
        "kion_sizer.sources.gcp._make_client",
        lambda project: (_ for _ in ()).throw(base.ProfileError("stop")),
    )
    out = io.StringIO()
    cli.run(["--gcp-table", "proj-1234.billing_export.gcp_billing_export_v1_ABC", "--read-footers", "--all-files"], out)
    assert "ignored for --source gcp" in out.getvalue()

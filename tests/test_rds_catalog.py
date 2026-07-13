"""rds_catalog tests: build tiers from orderable classes + EC2 RAM, plus the
CLI fallback path. No live AWS — fake rds/ec2 clients implement the boto3
paginator interface.
"""

import io
import json

import pytest

from kion_sizer import cli, config, rds_catalog


class _FakePaginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kw):
        return iter(self._pages)


class FakeRDS:
    def __init__(self, classes):
        self._classes = classes

    def get_paginator(self, name):
        assert name == "describe_orderable_db_instance_options"
        opts = [{"DBInstanceClass": c} for c in self._classes]
        return _FakePaginator([{"OrderableDBInstanceOptions": opts}])


class FakeEC2:
    def __init__(self, specs_mib):
        self._specs = specs_mib  # ec2_type -> MiB

    def get_paginator(self, name):
        assert name == "describe_instance_types"
        items = [
            {"InstanceType": t, "MemoryInfo": {"SizeInMiB": m}}
            for t, m in self._specs.items()
        ]
        return _FakePaginator([{"InstanceTypes": items}])


def test_db_to_ec2_mapping():
    assert rds_catalog.db_to_ec2("db.r6g.large") == "r6g.large"
    assert rds_catalog.db_to_ec2("db.t3.medium") == "t3.medium"
    assert rds_catalog.db_to_ec2("db.serverless") is None
    assert rds_catalog.db_to_ec2("weird") is None


def test_orderable_tiers_builds_ascending_and_drops_unsizeable():
    rds = FakeRDS(
        [
            "db.t3.medium",
            "db.t3.large",
            "db.r6g.large",
            "db.r6g.xlarge",
            "db.serverless",
        ]
    )
    ec2 = FakeEC2(
        {"t3.medium": 4096, "t3.large": 8192, "r6g.large": 16384, "r6g.xlarge": 32768}
    )
    tiers = rds_catalog.orderable_tiers("us-east-1", "mysql", rds, ec2)
    assert [(t.name, t.ram_gib) for t in tiers] == [
        ("db.t3.medium", 4.0),
        ("db.t3.large", 8.0),
        ("db.r6g.large", 16.0),
        ("db.r6g.xlarge", 32.0),
    ]


def test_dedupe_prefers_memory_optimized_at_same_ram():
    # t3.xlarge and r6g.large are both 16 GiB; memory-optimized wins.
    rds = FakeRDS(["db.t3.xlarge", "db.r6g.large"])
    ec2 = FakeEC2({"t3.xlarge": 16384, "r6g.large": 16384})
    tiers = rds_catalog.orderable_tiers("r", "mysql", rds, ec2)
    assert [(t.name, t.ram_gib) for t in tiers] == [("db.r6g.large", 16.0)]


def test_dedupe_prefers_newer_generation():
    rds = FakeRDS(["db.r6g.large", "db.r7g.large"])
    ec2 = FakeEC2({"r6g.large": 16384, "r7g.large": 16384})
    tiers = rds_catalog.orderable_tiers("r", "mysql", rds, ec2)
    assert tiers[0].name == "db.r7g.large"


def test_general_purpose_family_excluded():
    rds = FakeRDS(["db.m5.large", "db.m6i.large"])  # general purpose: not sized on
    ec2 = FakeEC2({"m5.large": 8192, "m6i.large": 8192})
    with pytest.raises(rds_catalog.RDSCatalogError):
        rds_catalog.orderable_tiers("r", "mysql", rds, ec2)


# --- CLI integration (monkeypatched catalog; no AWS) -----------------------


def _sparse(tmp_path, size=1 << 30):
    with open(tmp_path / "x.parquet", "wb") as f:
        f.truncate(size)
    return str(tmp_path)


def test_cli_uses_aws_tiers(monkeypatch, tmp_path):
    monkeypatch.setattr(
        rds_catalog,
        "orderable_tiers",
        lambda *a, **k: [config.InstanceTier("db.r7g.large", 16.0)],
    )
    out = io.StringIO()
    code = cli.run(
        ["--dir", _sparse(tmp_path), "--rds-from-aws", "--region", "us-west-2"], out
    )
    assert code == 0
    text = out.getvalue()
    assert "db.r7g.large" in text
    assert "instance classes: orderable in us-west-2 (mysql)" in text


def test_cli_falls_back_when_aws_unavailable(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise rds_catalog.RDSCatalogError("no creds")

    monkeypatch.setattr(rds_catalog, "orderable_tiers", boom)
    out = io.StringIO()
    code = cli.run(["--dir", _sparse(tmp_path), "--rds-from-aws", "--json"], out)
    assert code == 0
    d = json.loads(out.getvalue())
    assert d["rds_tier_source"].startswith("built-in defaults (AWS lookup failed")
    assert d["rds_instance"] == "db.t3.medium"  # static tiers still applied

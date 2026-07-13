"""rds_catalog tests: build the tier ladder from AWS-orderable candidate classes
+ EC2 RAM, plus the CLI fallback path. No live AWS — fake rds/ec2 clients.
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
    """describe_orderable_db_instance_options returns rows only for classes in
    `orderable` (a targeted per-class query, like the real API)."""

    def __init__(self, orderable):
        self._orderable = set(orderable)

    def describe_orderable_db_instance_options(
        self, Engine, DBInstanceClass=None, **kw
    ):
        if DBInstanceClass in self._orderable:
            return {
                "OrderableDBInstanceOptions": [{"DBInstanceClass": DBInstanceClass}]
            }
        return {"OrderableDBInstanceOptions": []}


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


# RAM (MiB) for every candidate EC2 type, so the fake EC2 can answer any of them.
_ALL_EC2_RAM = {
    "t4g.medium": 4096,
    "t3.medium": 4096,
    "t4g.large": 8192,
    "t3.large": 8192,
    "r8g.large": 16384,
    "r7g.large": 16384,
    "r6g.large": 16384,
    "r8g.xlarge": 32768,
    "r7g.xlarge": 32768,
    "r6g.xlarge": 32768,
    "r8g.2xlarge": 65536,
    "r7g.2xlarge": 65536,
    "r6g.2xlarge": 65536,
    "r8g.4xlarge": 131072,
    "r7g.4xlarge": 131072,
    "r6g.4xlarge": 131072,
    "r8g.8xlarge": 262144,
    "r7g.8xlarge": 262144,
    "r6g.8xlarge": 262144,
}


def test_db_to_ec2_mapping():
    assert rds_catalog.db_to_ec2("db.r6g.large") == "r6g.large"
    assert rds_catalog.db_to_ec2("db.t3.medium") == "t3.medium"
    assert rds_catalog.db_to_ec2("db.serverless") is None
    assert rds_catalog.db_to_ec2("weird") is None


def test_builds_ladder_preferring_newest_generation():
    # Everything orderable -> newest gen per tier: t4g, then r8g.
    rds = FakeRDS([c for row in rds_catalog._CANDIDATES for c in row])
    ec2 = FakeEC2(_ALL_EC2_RAM)
    tiers = rds_catalog.orderable_tiers("us-east-1", "mysql", rds, ec2)
    got = [(t.name, t.ram_gib) for t in tiers]
    assert got[:4] == [
        ("db.t4g.medium", 4.0),
        ("db.t4g.large", 8.0),
        ("db.r8g.large", 16.0),
        ("db.r8g.xlarge", 32.0),
    ]
    # ascending by RAM, no duplicate RAM sizes
    rams = [t.ram_gib for t in tiers]
    assert rams == sorted(rams) and len(rams) == len(set(rams))


def test_falls_through_to_next_orderable_class_in_tier():
    # r8g/r7g not offered at 16 GiB -> tier uses r6g.large; t4g not offered -> t3.
    orderable = {"db.t3.medium", "db.t3.large", "db.r6g.large", "db.r6g.xlarge"}
    rds = FakeRDS(orderable)
    ec2 = FakeEC2(_ALL_EC2_RAM)
    tiers = rds_catalog.orderable_tiers("r", "mysql", rds, ec2)
    got = [(t.name, t.ram_gib) for t in tiers]
    assert got == [
        ("db.t3.medium", 4.0),
        ("db.t3.large", 8.0),
        ("db.r6g.large", 16.0),
        ("db.r6g.xlarge", 32.0),
    ]


def test_raises_when_nothing_orderable():
    rds = FakeRDS(set())  # nothing orderable
    ec2 = FakeEC2(_ALL_EC2_RAM)
    with pytest.raises(rds_catalog.RDSCatalogError):
        rds_catalog.orderable_tiers("r", "mysql", rds, ec2)


def test_rds_api_error_propagates_for_fallback():
    class BoomRDS:
        def describe_orderable_db_instance_options(self, **kw):
            raise RuntimeError("throttled")

    ec2 = FakeEC2(_ALL_EC2_RAM)
    with pytest.raises(RuntimeError):
        rds_catalog.orderable_tiers("r", "mysql", BoomRDS(), ec2)


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

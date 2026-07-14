"""Tests for pricing.py — snapshot fallback, live parsing, and resilience.

The live path is exercised through an injected fake pricing client (a stand-in
for boto3's pricing client), never real AWS — same convention as test_s3.py.
"""

import json

import pytest

from kion_sizer import pricing


# --- fake pricing client ----------------------------------------------------
def _price_item(usd, product_attrs=None):
    return json.dumps(
        {
            "product": {"attributes": product_attrs or {}},
            "terms": {
                "OnDemand": {
                    "x": {"priceDimensions": {"y": {"pricePerUnit": {"USD": str(usd)}}}}
                }
            },
        }
    )


class FakePricing:
    """Returns canned PriceList items by ServiceCode + the exact filter fields."""

    def __init__(self, ec2=None, rds=None, fargate=None, fail=False):
        self.ec2 = ec2 or {}
        self.rds = rds or {}
        self.fargate = fargate or {}
        self.fail = fail
        self.calls = 0

    def get_products(self, **kw):
        self.calls += 1
        if self.fail:
            raise RuntimeError("AccessDenied: pricing:GetProducts")
        svc = kw["ServiceCode"]
        f = {d["Field"]: d["Value"] for d in kw["Filters"]}
        if svc == "AmazonEC2":
            usd = self.ec2.get(f["instanceType"])
            return {"PriceList": [_price_item(usd)] if usd is not None else []}
        if svc == "AmazonRDS":
            usd = self.rds.get(f["instanceType"])
            return {"PriceList": [_price_item(usd)] if usd is not None else []}
        if svc == "AmazonECS":
            items = [
                _price_item(usd, {"usagetype": f"USE1-{ut}"})
                for ut, usd in self.fargate.items()
            ]
            return {"PriceList": items}
        return {"PriceList": []}


_FARGATE = {
    "Fargate-vCPU-Hours:perCPU": 0.04048,
    "Fargate-GB-Hours": 0.004445,
    "Fargate-ARM-vCPU-Hours:perCPU": 0.03238,
    "Fargate-ARM-GB-Hours": 0.00356,
}


# --- snapshot ---------------------------------------------------------------
def test_snapshot_loads_and_prices():
    pt = pricing.build_price_table("us-east-1", "db.r8g.8xlarge", live=False)
    assert "embedded us-east-1 snapshot" in pt.source
    assert pt.hours_per_month == 730
    assert pt.rds_hr("db.r8g.8xlarge") == 3.824
    assert pt.ec2_hr("x2gd.2xlarge") == 0.668
    assert len(pt.ec2_specs) == 22
    assert pt.fargate["arm_vcpu"] == 0.03238


def test_snapshot_covers_builtin_and_orderable_rds_tiers():
    pt = pricing.build_price_table(None, "x", live=False)
    for cls in ("db.t3.medium", "db.r6g.xlarge", "db.r7g.xlarge", "db.r8g.8xlarge"):
        assert pt.rds_hr(cls) is not None, cls


def test_non_useast_snapshot_flags_approximation():
    pt = pricing.build_price_table("eu-west-1", "db.r6g.xlarge", live=False)
    assert "us-east-1 approximation" in pt.source


# --- live path (fake client) ------------------------------------------------
def test_live_path_uses_fetched_prices():
    fake = FakePricing(
        ec2={"x2gd.2xlarge": 0.70, "r8g.large": 0.12},
        rds={"db.r8g.8xlarge": 4.0},
        fargate=_FARGATE,
    )
    pt = pricing.build_price_table("us-east-1", "db.r8g.8xlarge", client=fake)
    assert pt.source == "live us-east-1 on-demand"
    assert pt.ec2_hr("x2gd.2xlarge") == 0.70  # live value, not snapshot 0.668
    assert pt.rds_hr("db.r8g.8xlarge") == 4.0
    assert pt.fargate["arm_gb"] == 0.00356


def test_live_missing_ec2_item_falls_back_per_instance():
    # An instance not offered in-region returns []; that one keeps its snapshot
    # price while overall provenance stays "live".
    fake = FakePricing(ec2={}, rds={"db.r8g.8xlarge": 4.0}, fargate=_FARGATE)
    pt = pricing.build_price_table("us-east-1", "db.r8g.8xlarge", client=fake)
    assert pt.source == "live us-east-1 on-demand"
    assert pt.ec2_hr("x2gd.2xlarge") == 0.668  # snapshot fallback for this item


def test_client_error_degrades_to_snapshot():
    fake = FakePricing(fail=True)
    pt = pricing.build_price_table("us-east-1", "db.r8g.8xlarge", client=fake)
    assert "embedded us-east-1 snapshot" in pt.source
    assert "live pricing unavailable" in pt.source
    assert pt.ec2_hr("x2gd.2xlarge") == 0.668


def test_fargate_arm_and_x86_suffixes_not_confused():
    fake = FakePricing(ec2={}, rds={}, fargate=_FARGATE)
    rates = pricing._fetch_fargate(fake, "us-east-1")
    assert rates == {
        "x86_vcpu": 0.04048,
        "x86_gb": 0.004445,
        "arm_vcpu": 0.03238,
        "arm_gb": 0.00356,
    }


def test_fargate_incomplete_returns_none():
    fake = FakePricing(fargate={"Fargate-vCPU-Hours:perCPU": 0.04})
    assert pricing._fetch_fargate(fake, "us-east-1") is None


@pytest.mark.parametrize(
    "engine,expected",
    [("mysql", "MySQL"), ("postgres", "PostgreSQL"), ("unknown-x", "MySQL")],
)
def test_rds_engine_mapping(engine, expected):
    seen = {}

    class Cap(FakePricing):
        def get_products(self, **kw):
            if kw["ServiceCode"] == "AmazonRDS":
                seen["engine"] = {d["Field"]: d["Value"] for d in kw["Filters"]}[
                    "databaseEngine"
                ]
            return super().get_products(**kw)

    fake = Cap(rds={"db.r8g.8xlarge": 4.0}, fargate=_FARGATE)
    pricing.build_price_table("us-east-1", "db.r8g.8xlarge", engine=engine, client=fake)
    assert seen["engine"] == expected

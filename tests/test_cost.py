"""Tests for model.cost() and select_ec2_alternatives() — pure, snapshot-priced.

Golden numbers are the real customer run (256 GiB CUR, us-east-1) that motivated
the feature: RDS db.r8g.8xlarge $2,791.52/mo, Fargate 16 vCPU/80 GiB arm $586.10 /
x86 $732.39, EC2 alt x2gd.2xlarge $487.64 (cheapest) / r8g.2xlarge $344.03.
"""

from kion_sizer import config, model, pricing


def _snapshot_prices(rds_class):
    return pricing.build_price_table(None, rds_class, live=False)


def _rec(rds_name, ram, poller_vcpu, poller_mem, task_vcpu, task_mem, accounts=0):
    class P:
        pass

    rec = model.Recommendation(profile=P())
    rec.rds = config.InstanceTier(rds_name, ram)
    rec.poller_vcpu = poller_vcpu
    rec.poller_mem_gib = poller_mem
    rec.poller_task_vcpu = task_vcpu
    rec.poller_task_mem_gib = task_mem
    if accounts:
        cfg = config.default()
        rec.services = model.pick_band(cfg.service_bands, accounts)
        rec.have_services = True
    return rec


def test_cost_matches_customer_run():
    rec = _rec("db.r8g.8xlarge", 256, 8, 76.5, 16, 80)
    r = model.cost(rec, _snapshot_prices("db.r8g.8xlarge"))

    assert r.rds_usd_mo == 2791.52
    assert r.poller_fargate_arm_usd_mo == 586.1
    assert r.poller_fargate_x86_usd_mo == 732.39

    prim = {o.name: o for o in r.ec2_primary}
    assert set(prim) == {"x2gd.2xlarge", "x8g.2xlarge", "x8i.2xlarge"}
    assert prim["x2gd.2xlarge"].usd_mo == 487.64
    assert prim["x2gd.2xlarge"].cheapest is True
    assert prim["x8i.2xlarge"].cheapest is False

    assert r.show_under is True
    under = {o.name: o for o in r.ec2_under}
    assert set(under) == {"r8g.2xlarge", "r8i.2xlarge"}
    assert under["r8g.2xlarge"].usd_mo == 344.03

    # EC2 alternative is NOT in the total; total = RDS + poller x86 Fargate.
    assert r.total_usd_mo == round(2791.52 + 732.39, 2)


def test_small_requirement_no_deadzone():
    rec = _rec("db.r6g.xlarge", 32, 2, 13.6, 2, 14)
    r = model.cost(rec, _snapshot_prices("db.r6g.xlarge"))
    assert r.ec2_exceeds is False
    assert r.show_under is False
    assert r.ec2_under == []
    names = {o.name for o in r.ec2_primary}
    assert names == {"r8g.large", "r8i.large"}  # 2 vCPU / 16 GiB holds 13.6
    cheapest = next(o for o in r.ec2_primary if o.cheapest)
    assert cheapest.name == "r8g.large"


def test_requirement_exceeds_candidates():
    rec = _rec("db.r8g.8xlarge", 256, 8, 900, 16, 120)
    r = model.cost(rec, _snapshot_prices("db.r8g.8xlarge"))
    assert r.ec2_exceeds is True
    assert r.ec2_primary == []


def test_services_and_total():
    rec = _rec("db.r6g.xlarge", 32, 2, 13.6, 2, 14, accounts=150)
    r = model.cost(rec, _snapshot_prices("db.r6g.xlarge"))
    assert r.services_usd_mo is not None and r.services_usd_mo > 0
    expected = round(r.rds_usd_mo + r.poller_fargate_x86_usd_mo + r.services_usd_mo, 2)
    assert r.total_usd_mo == expected
    assert "services" in r.total_note


def test_unpriced_rds_class_is_na_not_crash():
    # A class absent from the snapshot (no live pricing) -> None, total excludes it.
    rec = _rec("db.m5.legacy", 32, 2, 13.6, 2, 14)
    r = model.cost(rec, _snapshot_prices("db.m5.legacy"))
    assert r.rds_usd_mo is None
    assert "RDS (class not priced)" in r.total_note
    assert r.total_usd_mo == r.poller_fargate_x86_usd_mo


def test_generation_and_family_helpers():
    assert model._generation("r8g.2xlarge") == 8
    assert model._generation("x2gd.4xlarge") == 2
    assert model._family_token("r8g.2xlarge") == "r8g"
    assert model._family_class("x8i.2xlarge") == "x"

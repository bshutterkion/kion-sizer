import json
from dataclasses import dataclass

from kion_sizer import config, model, render


@dataclass
class FakeProfile:
    file_count: int = 1
    compressed_bytes: int = 1 << 30
    have_raw_rows: bool = False
    raw_line_items: int = 0
    has_csv: bool = False
    sample_note: str = ""
    granularity: str = "unknown"


def _rec(accounts=40):
    return model.recommend(FakeProfile(), config.default(), accounts)


def test_text_contains_key_lines():
    out = render.render_text(_rec())
    assert "RDS:              db.t3.medium (4 GiB RAM)" in out
    assert "financials-poller heap requirement: 4.0 GiB mem, 1 vCPU" in out
    assert "financials-poller Fargate task: 4 GiB mem, 1 vCPU (1024 CPU units)" in out
    assert out.endswith("\n")
    assert "peak shard:       0.8 GiB" in out


def test_json_key_order_and_whole_floats():
    out = render.render_json(_rec())
    # whole floats render without .0
    assert '"rds_ram_gib": 4' in out
    assert '"poller_mem_gib": 4' in out
    assert '"shard_gib": 0.7631964981555939' in out
    # top-level keys alphabetical; services nested keys in field order
    obj = json.loads(out)
    assert list(obj.keys()) == sorted(obj.keys())
    assert list(obj["services"].keys())[0] == "max_accounts"
    assert list(obj["services"].keys())[1] == "core_tasks"


def test_json_omits_services_without_accounts():
    out = render.render_json(_rec(accounts=0))
    assert "services" not in json.loads(out)


# --- cost block (flag-gated) ------------------------------------------------
def _rec_with_cost(accounts=0):
    from kion_sizer import pricing

    rec = _rec(accounts)
    rec.cost = model.cost(
        rec, pricing.build_price_table(None, rec.rds.name, live=False)
    )
    return rec


def test_cost_block_absent_without_cost():
    out = render.render_text(_rec())
    assert "Estimated monthly cost" not in out
    assert "cost" not in json.loads(render.render_json(_rec()))


def test_cost_block_renders_text():
    out = render.render_text(_rec_with_cost())
    assert "Estimated monthly cost" in out
    assert "EC2 alternative" in out
    assert "TOTAL (" in out
    assert "/mo" in out


def test_cost_present_in_json():
    obj = json.loads(render.render_json(_rec_with_cost(accounts=150)))
    assert "cost" in obj
    c = obj["cost"]
    assert list(c.keys())[0] == "source"  # cost sub-keys stay in field order
    assert c["total_usd_mo"] > 0
    assert isinstance(c["poller_ec2_primary"], list)
    assert obj["cost"]["poller_fargate"]["vcpu"] >= 1

import json
from dataclasses import dataclass

from kion_sizer import config, model, render
from kion_sizer.targets.aws import AWSTarget


def _aws_target():
    return AWSTarget()


@dataclass
class FakeProfile:
    cloud: str = "aws"
    report_format: str = "cur"
    file_count: int = 1
    compressed_bytes: int = 1 << 30
    have_raw_rows: bool = False
    raw_line_items: int = 0
    has_csv: bool = False
    sample_note: str = ""
    granularity: str = "unknown"


def _rec(accounts=40):
    return model.recommend(FakeProfile(), config.default(), accounts, _aws_target())


def test_text_contains_key_lines():
    out = render.render_text(_rec())
    assert "database:         db.t3.medium (4 GiB RAM)" in out
    assert "financials-poller heap requirement: 4.0 GiB mem, 1 vCPU" in out
    assert "financials-poller Fargate task: 4 GiB mem, 1 vCPU (1024 CPU units)" in out
    assert out.endswith("\n")
    assert "peak shard:       0.8 GiB" in out
    assert "source: AWS    target: AWS" in out


def test_json_whole_floats_and_nested_key_order():
    out = render.render_json(_rec())
    # whole floats render without .0
    assert '"ram_gib": 4' in out
    assert '"mem_gib": 4' in out
    assert '"shard_gib": 0.7631964981555939' in out
    obj = json.loads(out)
    # services keys stay in field order, not alphabetical
    svc = obj["target"]["services"]
    assert list(svc.keys())[0] == "max_accounts"
    assert list(svc.keys())[1] == "core_tasks"


def test_json_omits_services_without_accounts():
    out = render.render_json(_rec(accounts=0))
    assert "services" not in json.loads(out)


# --- cost block (flag-gated) ------------------------------------------------
def _rec_with_cost(accounts=0):
    from kion_sizer import pricing

    rec = _rec(accounts)
    rec.cost = model.cost(
        rec, pricing.build_price_table(None, rec.db.name, live=False)
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
    # the EC2 box is the poller only — must say the other services aren't covered
    assert "replaces the financials-poller only" in out
    assert "microservices run separately" in out


def test_cost_present_in_json():
    obj = json.loads(render.render_json(_rec_with_cost(accounts=150)))
    assert "cost" in obj["target"]
    c = obj["target"]["cost"]
    assert list(c.keys())[0] == "source"  # cost sub-keys stay in field order
    assert c["total_usd_mo"] > 0
    assert isinstance(c["poller_ec2_primary"], list)
    assert c["poller_fargate"]["vcpu"] >= 1


def test_json_nests_source_and_target():
    obj = json.loads(render.render_json(_rec()))
    assert obj["source"]["cloud"] == "aws"
    assert obj["target"]["cloud"] == "aws"
    assert obj["target"]["database"]["name"].startswith("db.")
    assert obj["target"]["compute"]["kind"] == "fargate_task"
    assert obj["warnings"] == []


def test_text_shows_warnings_near_the_top():
    rec = _rec()
    rec.warnings.append("UNCALIBRATED SOURCE: azure constants are inherited")
    text = render.render_text(rec)
    assert "UNCALIBRATED SOURCE" in text
    # Warnings must precede the recommendation, not trail it.
    assert text.index("UNCALIBRATED SOURCE") < text.index("Recommended starting sizes")

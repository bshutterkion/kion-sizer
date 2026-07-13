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
    assert "financials-poller ECS task: 4.0 GiB mem, 1 vCPU" in out
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

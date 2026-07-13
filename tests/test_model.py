from dataclasses import dataclass

from kion_sizer import config, model


@dataclass
class FakeProfile:
    compressed_bytes: int = 0
    have_raw_rows: bool = False
    raw_line_items: int = 0
    has_csv: bool = False
    sample_note: str = ""
    granularity: str = "unknown"


def test_round_half_away_matches_go():
    # Go math.Round rounds halves away from zero; Python round() would give 0.
    assert model.round_half_away(0.5) == 1
    assert model.round_half_away(2.5) == 3
    assert model.round_half_away(2.4) == 2


def test_bytes_path_one_gib():
    c = config.default()
    p = FakeProfile(compressed_bytes=1 << 30)
    r = model.recommend(p, c, 40)
    assert r.est_rows == 791000
    assert r.est_basis == "compressed bytes"
    assert r.rds.name == "db.t3.medium"
    assert r.rds.ram_gib == 4
    assert r.poller_mem_gib == 4.0
    assert r.poller_vcpu == 1
    assert r.have_services is True
    assert r.services.max_accounts == 50


def test_no_accounts_omits_services():
    c = config.default()
    r = model.recommend(FakeProfile(compressed_bytes=1 << 30), c, 0)
    assert r.have_services is False
    assert r.services is None


def test_footer_path_uses_ratio():
    c = config.default()
    p = FakeProfile(compressed_bytes=1 << 30, have_raw_rows=True, raw_line_items=8440)
    r = model.recommend(p, c, 0)
    assert r.raw_line_items == 8440
    assert r.est_rows == int(8440 / c.raw_to_aggregated_ratio)
    assert r.est_basis == "line items / aggregation ratio"


def test_fargate_task_rounds_mem_up_to_valid_size():
    # 5.3 GiB / 1 vCPU -> Fargate 1 vCPU tier (2-8 GB /1) rounds up to 6 GiB.
    cpu, mem_mib, exceeds = model.fargate_task(5.3, 1)
    assert (cpu, mem_mib, exceeds) == (1024, 6144, False)


def test_fargate_task_exact_value_unchanged():
    cpu, mem_mib, exceeds = model.fargate_task(4.0, 1)
    assert (cpu, mem_mib, exceeds) == (1024, 4096, False)


def test_fargate_task_escalates_cpu_when_mem_exceeds_tier():
    # 10 GiB needs > the 1 vCPU max (8 GB); even at vcpu=1 it must move to 2 vCPU.
    cpu, mem_mib, exceeds = model.fargate_task(10.0, 1)
    assert cpu == 2048 and mem_mib == 10240 and exceeds is False


def test_fargate_task_uses_32vcpu_tier():
    # 200 GiB fits the 32 vCPU tier's 244 GB (249856 MiB) top size.
    cpu, mem_mib, exceeds = model.fargate_task(200.0, 8)
    assert cpu == 32768 and mem_mib == 249856 and exceeds is False


def test_fargate_task_exceeds_largest():
    # Past 244 GB there is no larger Fargate task.
    cpu, mem_mib, exceeds = model.fargate_task(300.0, 8)
    assert cpu == 32768 and mem_mib == 249856 and exceeds is True


def test_recommend_sets_valid_fargate_task():
    c = config.default()
    p = FakeProfile(compressed_bytes=1 << 30)
    r = model.recommend(p, c, 0)
    # poller floor is 4 GiB -> valid Fargate task 4 GiB / 1 vCPU.
    assert r.poller_task_mem_gib == 4.0
    assert r.poller_task_vcpu == 1
    assert r.poller_task_cpu_units == 1024
    assert r.poller_task_exceeds is False


def test_pick_tier_exceeds():
    c = config.default()
    tier, exceeds = model.pick_tier(c.rds_tiers, 10_000)
    assert exceeds is True
    assert tier.name == "db.r6g.4xlarge"


def test_pick_band_empty_returns_zero_band():
    # Go's pickBand returns a zero-value ServiceBand (not nil) for an empty list,
    # so render shows zeros instead of crashing. Match that.
    band = model.pick_band([], 40)
    assert band == config.ServiceBand(0, 0, 0, 0, 0, 0, 0)


def test_recommend_empty_service_bands_does_not_crash():
    c = config.default()
    c.service_bands = []  # Go's validate() allows this (no non-empty check)
    r = model.recommend(FakeProfile(compressed_bytes=1 << 30), c, 40)
    assert r.have_services is True
    assert r.services == config.ServiceBand(0, 0, 0, 0, 0, 0, 0)

import pytest
from kion_sizer import config


def test_default_loads_and_validates():
    c = config.default()
    assert c.rows_per_gib == 791000
    assert c.rds_tiers[0].name == "db.t3.medium"
    assert c.rds_tiers[0].ram_gib == 4
    assert len(c.service_bands) == 3
    assert c.service_bands[0].max_accounts == 50


def test_validate_rejects_nonpositive_rows_per_gib():
    c = config.default()
    c.rows_per_gib = 0
    with pytest.raises(config.ConfigError, match="rows_per_gib must be positive"):
        c.validate()


def test_validate_rejects_buffer_pool_fraction_out_of_range():
    c = config.default()
    c.buffer_pool_fraction = 1.5
    with pytest.raises(
        config.ConfigError, match=r"buffer_pool_fraction must be in \(0,1\]"
    ):
        c.validate()


def test_validate_rejects_unsorted_rds_tiers():
    c = config.default()
    c.rds_tiers = list(reversed(c.rds_tiers))
    with pytest.raises(config.ConfigError, match="rds_tiers must be ascending"):
        c.validate()


def test_validate_rejects_unsorted_service_bands():
    c = config.default()
    c.service_bands = list(reversed(c.service_bands))
    with pytest.raises(config.ConfigError, match="service_bands must be ascending"):
        c.validate()

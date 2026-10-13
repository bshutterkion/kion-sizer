import pytest

from kion_sizer import config


def test_default_loads_and_validates():
    c = config.default()
    assert c.source("aws").rows_per_gib == 791000
    assert c.target("aws").db_tiers[0].name == "db.t3.medium"
    assert c.target("aws").db_tiers[0].ram_gib == 4
    assert len(c.service_bands) == 3
    assert c.service_bands[0].max_accounts == 50


def test_validate_rejects_nonpositive_rows_per_gib():
    c = config.default()
    c.source("aws").rows_per_gib = 0
    with pytest.raises(config.ConfigError, match="rows_per_gib must be positive"):
        c.validate()


def test_validate_rejects_buffer_pool_fraction_out_of_range():
    c = config.default()
    c.target("aws").buffer_pool_fraction = 1.5
    with pytest.raises(
        config.ConfigError, match=r"buffer_pool_fraction must be in \(0,1\]"
    ):
        c.validate()


def test_validate_rejects_unsorted_rds_tiers():
    c = config.default()
    tgt = c.target("aws")
    tgt.db_tiers = list(reversed(tgt.db_tiers))
    with pytest.raises(config.ConfigError, match="db_tiers must be ascending"):
        c.validate()


def test_validate_rejects_unsorted_service_bands():
    c = config.default()
    c.service_bands = list(reversed(c.service_bands))
    with pytest.raises(config.ConfigError, match="service_bands must be ascending"):
        c.validate()


def test_gcp_exports_do_not_share_a_ratio():
    # Standard collapses hourly rows to daily (~26x); detailed keys on the
    # resource and barely collapses (~2.6x). Sharing a constant would mis-size
    # one of them by an order of magnitude.
    c = config.default()
    std = c.source("gcp", "standard").raw_to_aggregated_ratio
    det = c.source("gcp", "detailed").raw_to_aggregated_ratio
    assert std > 20 and det < 5
    assert std / det > 5


def test_calibration_resolves_most_specific_first():
    c = config.default()
    # Measured entry wins for its exact pair.
    assert c.source("aws", "cur").inherited is False
    # Anything unmeasured falls through to `default` and is flagged.
    assert c.source("aws", "dbr").inherited is True
    assert c.source("gcp", "detailed").inherited is False   # measured 2026-08-25
    assert c.source("oci", "focus").inherited is True
    assert c.source("azure", "usage-details").inherited is False  # measured 2026-08-25
    # An unknown provider still resolves, rather than failing a sizing run.
    assert c.source("nonesuch", "whatever").inherited is True


def test_targets_only_the_two_we_deploy_on():
    # Kion is deployed on AWS and Azure; GCP and OCI are billing sources only.
    c = config.default()
    assert set(c.targets) == {"aws", "azure"}


def test_missing_default_entry_is_a_clear_error():
    c = config.default()
    c.sources = {"aws/cur": c.sources["aws/cur"]}
    with pytest.raises(config.ConfigError, match="no 'default' entry"):
        c.source("azure", "focus")


def test_only_the_measured_pair_is_calibrated():
    c = config.default()
    assert c.source("aws", "cur").inherited is False
    assert "UNCALIBRATED" in c.source("azure", "focus").calibration
    assert c.source("azure", "usage-details").inherited is False


def test_unknown_target_raises_but_an_unknown_source_inherits():
    # A target must exist -- we cannot size a deployment we know nothing about.
    # A source need not: an unmeasured billing format still has a row count, so
    # it inherits the default calibration and says the estimate is uncalibrated.
    c = config.default()
    with pytest.raises(config.ConfigError, match="no calibration for target"):
        c.target("aliyun")
    assert c.source("aliyun", "whatever").inherited is True


def test_legacy_flat_config_maps_onto_aws(tmp_path):
    # A config written against the pre-two-axis schema must keep working.
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text(
        "calibration_version: legacy-test\n"
        "rows_per_gib: 500000\n"
        "raw_to_aggregated_ratio: 7.0\n"
        "bytes_per_row: 1000\n"
        "buffer_pool_fraction: 0.75\n"
        "buffer_pool_headroom_fraction: 0.40\n"
        "buffer_pool_headroom_floor_gib: 1\n"
        "poller_base_gib: 1.0\n"
        "poller_heap_gib_per_mrow: 0.85\n"
        "poller_headroom_fraction: 0.25\n"
        "poller_floor_gib: 4\n"
        "rds_tiers:\n"
        "  - {name: db.t3.medium, ram_gib: 4}\n"
        "  - {name: db.r6g.large, ram_gib: 16}\n"
        "service_bands:\n"
        "  - {max_accounts: 50, core_tasks: 1, core_cpu: 1024, core_mem_mib: 2048,"
        " compliance_tasks: 4, compliance_cpu: 1024, compliance_mem_mib: 2048}\n"
    )
    c = config.load(str(legacy))
    assert c.source("aws").rows_per_gib == 500000
    assert c.source("aws").raw_to_aggregated_ratio == 7.0
    assert c.target("aws").buffer_pool_fraction == 0.75
    assert [t.name for t in c.target("aws").db_tiers] == ["db.t3.medium", "db.r6g.large"]

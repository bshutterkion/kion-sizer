"""The sources package: profile shape and the source registry."""

import pytest

from kion_sizer.sources import base, load


def test_billing_profile_carries_cloud_and_labels():
    p = base.BillingProfile(source="/tmp/cur")
    assert p.cloud == "aws"
    assert p.unit_label == "files"
    assert p.byte_label == "compressed bytes"
    assert p.volume_note == ""


def test_every_bulk_provider_is_routable():
    # Each of these must reach its source module rather than the "unknown"
    # branch. They fail on the location, which is the point: routing happened.
    from kion_sizer import sources

    for cloud in ("aws", "azure", "gcp", "oci", "custom"):
        with pytest.raises(base.ProfileError) as e:
            sources.load(cloud, "obviously-not-a-valid-location")
        assert "unknown source" not in str(e.value)


def test_registry_rejects_unknown_cloud():
    with pytest.raises(base.ProfileError, match="unknown source"):
        load("aliyun", "wherever")

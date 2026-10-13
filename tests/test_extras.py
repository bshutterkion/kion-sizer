"""Optional-dependency handling."""

import pytest

from kion_sizer.sources import base


def test_missing_sdk_names_the_extra_and_the_package():
    with pytest.raises(base.ProfileError, match=r"--extra gcp"):
        base.missing_sdk("gcp", "google-cloud-bigquery")
    with pytest.raises(base.ProfileError, match="google-cloud-bigquery"):
        base.missing_sdk("gcp", "google-cloud-bigquery")

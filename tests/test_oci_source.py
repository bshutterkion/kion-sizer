"""OCI cost-report profiling, against a fake object store."""

import gzip
import io

import pytest

from kion_sizer.sources import base, oci

URL = "https://objectstorage.us-ashburn-1.oraclecloud.com/n/bling/b/tenancy-ocid/o/reports/cost-csv"

NATIVE = (
    b"lineItem/tenantId,product/compartmentId,lineItem/intervalUsageStart,cost/myCost\n"
    + b"ocid1.tenancy.aaa,ocid1.compartment.c1,2026-06-01T00:00Z,1.0\n" * 40
    + b"ocid1.tenancy.aaa,ocid1.compartment.c2,2026-06-01T00:00Z,2.0\n" * 10
)
FOCUS = (
    b"ChargePeriodStart,SubAccountId,BilledCost\n"
    + b"2026-06-01,ocid1.compartment.c1,1.0\n" * 25
)


class FakeOCI:
    def __init__(self, objs):
        self._o = objs  # {name: bytes}

    def list_objects(self, namespace, bucket, prefix):
        class O:
            def __init__(self, name, size):
                self.name, self.size = name, size

        return [O(n, len(d)) for n, d in sorted(self._o.items()) if n.startswith(prefix)]

    def get_object(self, namespace, bucket, name):
        return self._o[name]


def _client(body=NATIVE, gz=True, n=2):
    objs = {}
    for i in range(n):
        name = f"reports/cost-csv/000100000165245{i}-0000{i}.csv"
        if gz:
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb") as w:
                w.write(body)
            objs[name + ".gz"] = buf.getvalue()
        else:
            objs[name] = body
    return FakeOCI(objs)


def test_parse_object_url():
    assert oci.parse_object_url(URL) == (
        "us-ashburn-1",
        "bling",
        "tenancy-ocid",
        "reports/cost-csv/",
    )


def test_parse_object_url_defaults_the_cost_prefix():
    bare = "https://objectstorage.us-ashburn-1.oraclecloud.com/n/bling/b/tenancy-ocid"
    assert oci.parse_object_url(bare)[3] == oci.COST_PREFIX


def test_parse_object_url_rejects_other_urls():
    with pytest.raises(base.ProfileError, match="not an OCI Object Storage URL"):
        oci.parse_object_url("s3://bucket/prefix/")


def test_detect_format_native_and_focus():
    assert oci.detect_format(["lineItem/tenantId", "cost/myCost"]) == "cost-csv"
    assert oci.detect_format(["SubAccountId", "BilledCost"]) == "focus"
    assert oci.detect_format(["nothing", "useful"]) == "unknown"


def test_native_report_counts_rows_and_compartments():
    p = oci.from_object_storage(
        URL, client=_client(), detect_accounts=True, all_files=True
    )
    assert p.cloud == "oci"
    assert p.report_format == "cost-csv"
    assert p.raw_line_items == 100          # 50 rows x 2 reports
    assert p.account_count == 2             # two compartments
    assert "compartments" in p.volume_note


def test_focus_report_is_detected():
    p = oci.from_object_storage(
        URL, client=_client(body=FOCUS), detect_accounts=True, all_files=True
    )
    assert p.report_format == "focus"
    assert p.raw_line_items == 50           # 25 x 2
    assert p.account_count == 1


def test_empty_bucket_is_a_clear_error():
    with pytest.raises(base.ProfileError, match="no .csv"):
        oci.from_object_storage(URL, client=FakeOCI({}))


def test_registry_routes_oci():
    from kion_sizer import sources

    p = sources.load("oci", URL, client=_client())
    assert p.cloud == "oci"

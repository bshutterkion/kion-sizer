"""Azure Cost Management export profiling, against a fake container."""

import gzip
import io
import json

import pytest

from kion_sizer.sources import azure, base

FOCUS_CSV = (
    b"ChargePeriodStart,SubAccountId,BilledCost\n"
    + b"2026-06-01,sub-a,1.0\n" * 30
    + b"2026-06-01,sub-b,2.0\n" * 20
)
NATIVE_CSV = (
    b"Date,SubscriptionId,CostInBillingCurrency\n"
    + b"06/01/2026,sub-a,1.0\n" * 10
    + b"06/01/2026,sub-c,2.0\n" * 10
    + b"06/01/2026,sub-d,3.0\n" * 5
)


class _Download:
    def __init__(self, data):
        self._d = data

    def readall(self):
        return self._d


class FakeContainer:
    """Stand-in for azure.storage.blob.ContainerClient."""

    def __init__(self, blobs):
        # blobs: {name: (bytes, last_modified)}
        self._b = blobs

    def list_blobs(self, name_starts_with=""):
        class P:
            def __init__(self, name, size, lm):
                self.name, self.size, self.last_modified = name, size, lm

        return [
            P(n, len(d), lm)
            for n, (d, lm) in sorted(self._b.items())
            if n.startswith(name_starts_with or "")
        ]

    def download_blob(self, name, offset=None, length=None):
        data = self._b[name][0]
        if offset is not None:
            data = data[offset : offset + (length or len(data))]
        return _Download(data)


def _manifest(names):
    return json.dumps(
        {"blobs": [{"blobName": n, "byteCount": 0} for n in names]}
    ).encode()


def _container(csv=FOCUS_CSV, runs=1):
    """One period with `runs` run folders; the last written is the freshest."""
    blobs = {}
    for i in range(runs):
        run = f"exports/daily/20260601-20260630/run-{i}"
        blobs[f"{run}/manifest.json"] = (_manifest([f"{run}/part_0.csv"]), 100 + i)
        blobs[f"{run}/part_0.csv"] = (csv, 100 + i)
    return FakeContainer(blobs)


URL = "https://acct.blob.core.windows.net/billing/exports"


def test_parse_container_url_splits_account_container_prefix():
    assert azure.parse_container_url(
        "https://acct.blob.core.windows.net/billing/exports/daily"
    ) == ("https://acct.blob.core.windows.net", "billing", "exports/daily")


def test_parse_container_url_rejects_non_azure():
    with pytest.raises(base.ProfileError, match="not an Azure blob URL"):
        azure.parse_container_url("s3://bucket/prefix/")
    with pytest.raises(base.ProfileError, match="missing container"):
        azure.parse_container_url("https://acct.blob.core.windows.net")


def test_period_and_run_extraction():
    n = "exports/daily/20260601-20260630/run-7/part_0.csv"
    assert azure.period_of(n) == "20260601-20260630"
    assert azure.run_of(n) == "exports/daily/20260601-20260630/run-7"


def test_parse_manifest_reads_only_the_fields_we_need():
    raw = json.dumps(
        {
            "manifestVersion": "2024-01-01",
            "byteCount": 999,
            "blobs": [
                {"blobName": "/a/part_0.csv", "byteCount": 12, "somethingElse": 1}
            ],
            "unknownTopLevel": {"x": 1},
        }
    ).encode()
    parts = azure.parse_manifest(raw)
    assert [(p.name, p.size) for p in parts] == [("a/part_0.csv", 12)]


def test_parse_manifest_tolerates_govcloud_capitalization():
    # Commercial and GovCloud manifests differ; Kion's own poller warns about it.
    raw = json.dumps({"Blobs": [{"BlobName": "a/p.csv", "ByteCount": 5}]}).encode()
    assert [(p.name, p.size) for p in azure.parse_manifest(raw)] == [("a/p.csv", 5)]


def test_only_the_newest_run_folder_is_counted():
    # Export overwrite is on by default, so several runs coexist for one period.
    # Summing across them would double-count -- here, triple.
    c = _container(runs=3)
    p = azure.from_container(URL, client=c)
    assert p.file_count == 1  # one part, not three
    assert "3 run folders present" in p.volume_note
    assert "run-2" in p.volume_note  # the freshest


def test_single_run_says_nothing_about_run_folders():
    p = azure.from_container(URL, client=_container(runs=1))
    assert "run folders present" not in p.volume_note


def test_focus_format_is_detected_and_flagged():
    p = azure.from_container(URL, client=_container(csv=FOCUS_CSV))
    assert p.report_format == "focus"
    assert p.cloud == "azure"
    assert "FOCUS folds actual and amortized" in p.volume_note


def test_native_usage_details_format_is_detected():
    p = azure.from_container(URL, client=_container(csv=NATIVE_CSV))
    assert p.report_format == "usage-details"


def test_rows_and_accounts_are_counted_for_focus():
    p = azure.from_container(
        URL, client=_container(csv=FOCUS_CSV), detect_accounts=True, all_files=True
    )
    assert p.raw_line_items == 50
    assert p.account_count == 2  # sub-a, sub-b


def test_rows_and_accounts_are_counted_for_native():
    p = azure.from_container(
        URL, client=_container(csv=NATIVE_CSV), detect_accounts=True, all_files=True
    )
    assert p.raw_line_items == 25
    assert p.account_count == 3  # sub-a, sub-c, sub-d


def test_unreadable_manifest_degrades_to_the_listing():
    blobs = {
        "exports/20260601-20260630/run-0/manifest.json": (b"{not json", 1),
        "exports/20260601-20260630/run-0/part_0.csv": (FOCUS_CSV, 1),
    }
    p = azure.from_container(URL, client=FakeContainer(blobs))
    assert p.file_count == 1  # found the part anyway


def test_no_period_folders_is_a_clear_error():
    c = FakeContainer({"exports/readme.txt": (b"hi", 1)})
    with pytest.raises(base.ProfileError, match="no billing-period folders"):
        azure.from_container(URL, client=c)


def test_explicit_month_selects_its_period():
    blobs = {}
    for period, n in (("20260501-20260531", 1), ("20260601-20260630", 9)):
        run = f"exports/{period}/run-0"
        blobs[f"{run}/manifest.json"] = (_manifest([f"{run}/part_0.csv"]), 1)
        blobs[f"{run}/part_0.csv"] = (FOCUS_CSV * n, 1)
    c = FakeContainer(blobs)
    assert "20260501" in azure.from_container(URL, month="202605", client=c).source
    # Default picks the peak period by bytes.
    assert "20260601" in azure.from_container(URL, client=c).source


def test_unknown_month_lists_what_is_present():
    with pytest.raises(base.ProfileError, match="periods present"):
        azure.from_container(URL, month="209901", client=_container())


def test_registry_routes_azure():
    from kion_sizer import sources

    p = sources.load("azure", URL, client=_container())
    assert p.cloud == "azure"


# --- unpartitioned exports --------------------------------------------------
# The legacy shape, and the one live Gov tenants actually use: one whole-month
# CSV per export run, accumulating as siblings in the period folder with no
# manifest. Counting them all multiplies the month by the number of runs.


def _unpartitioned(n=3):
    blobs = {}
    for i in range(n):
        blobs[f"exports/KionBillingExport/20260601-20260630/Export_{i}.csv"] = (
            FOCUS_CSV,
            100 + i,
        )
    return FakeContainer(blobs)


def test_unpartitioned_counts_only_the_newest_file():
    p = azure.from_container(URL, client=_unpartitioned(n=3))
    assert p.file_count == 1                    # not 3
    assert "3 full-month files present" in p.volume_note
    assert "Export_2" in p.volume_note          # the newest


def test_unpartitioned_rows_are_one_months_worth_not_n_months():
    one = azure.from_container(
        URL, client=_unpartitioned(n=1), detect_accounts=True, all_files=True
    )
    many = azure.from_container(
        URL, client=_unpartitioned(n=5), detect_accounts=True, all_files=True
    )
    assert many.raw_line_items == one.raw_line_items
    assert many.account_count == one.account_count


def test_single_unpartitioned_file_says_so_without_alarming():
    p = azure.from_container(URL, client=_unpartitioned(n=1))
    assert "unpartitioned export" in p.volume_note
    assert "full-month files present" not in p.volume_note


def test_partitioned_still_prefers_the_manifest():
    p = azure.from_container(URL, client=_container(runs=3))
    assert "3 run folders present" in p.volume_note


# --- credentials --------------------------------------------------------------


def test_sas_token_from_the_environment_is_used_verbatim(monkeypatch):
    monkeypatch.setenv(azure.SAS_ENV, "?sv=2024&sp=rl&sig=abc")
    assert azure._credential() == "sv=2024&sp=rl&sig=abc"  # leading ? dropped


def test_without_a_sas_token_the_default_credential_is_used(monkeypatch):
    pytest.importorskip("azure.identity")
    monkeypatch.delenv(azure.SAS_ENV, raising=False)
    from azure.identity import DefaultAzureCredential

    assert isinstance(azure._credential(), DefaultAzureCredential)


def test_a_failed_listing_points_at_both_ways_to_get_access():
    class Denied:
        def list_blobs(self, name_starts_with=""):
            raise RuntimeError("AuthorizationPermissionMismatch")

    with pytest.raises(base.ProfileError, match="Storage Blob Data Reader") as e:
        azure.from_container(URL, client=Denied())
    assert azure.SAS_ENV in str(e.value)

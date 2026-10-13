"""Custom billing sources: FOCUS files hosted in S3 or Azure blob storage."""

import gzip
import io

import pytest

from kion_sizer.sources import base, custom


def test_storage_kind_detects_both_backends():
    assert custom.storage_kind("s3://bucket/prefix/") == "aws"
    assert custom.storage_kind(
        "https://acct.blob.core.windows.net/exports/focus/"
    ) == "azure"
    assert custom.storage_kind("/local/path") == "unknown"


def test_azure_hosted_custom_source_delegates_to_the_azure_backend():
    import sys
    sys.path.insert(0, "tests")
    from test_azure_source import _container

    p = custom.from_location(
        "https://acct.blob.core.windows.net/billing/exports", client=_container()
    )
    assert p.cloud == "custom"
    assert p.report_format == "focus"
    assert "hosted in azure" in p.volume_note


def test_unrecognized_location_is_a_clear_error():
    with pytest.raises(base.ProfileError, match="cannot tell where"):
        custom.from_location("/some/local/dir")


def test_focus_account_column_is_detected():
    # A custom source carries FOCUS columns, not AWS CUR ones. The scanner must
    # find SubAccountId or the member-account count silently comes back zero.
    header = ["ChargePeriodStart", "SubAccountId", "BilledCost"]
    assert base._pick_account_col(header) == "SubAccountId"


def test_azure_account_columns_are_detected():
    # EA and MCA spell it differently; Kion's parser accepts both.
    assert base._pick_account_col(["Date", "SubscriptionId"]) == "SubscriptionId"
    assert base._pick_account_col(["Date", "SubscriptionGuid"]) == "SubscriptionGuid"


def test_aws_cur_columns_still_detected():
    assert (
        base._pick_account_col(["line_item_usage_account_id"])
        == "line_item_usage_account_id"
    )
    assert (
        base._pick_account_col(["lineItem/UsageAccountId"]) == "lineItem/UsageAccountId"
    )


def test_focus_rows_are_counted_and_accounts_detected(tmp_path):
    # End-to-end over the shared CSV scanner, using a FOCUS-shaped file.
    focus_csv = b"ChargePeriodStart,SubAccountId,BilledCost\n" + b"2026-06-01,sub-a,1.0\n" * 30 + b"2026-06-01,sub-b,2.0\n" * 20
    (tmp_path / "part0.csv").write_bytes(focus_csv)
    with gzip.open(tmp_path / "part1.csv.gz", "wb") as w:
        w.write(focus_csv)

    from kion_sizer.sources import aws

    p = aws.from_dir(str(tmp_path), detect_accounts=True, all_files=True)
    assert p.raw_line_items == 100          # 50 rows x 2 files
    assert p.account_count == 2             # sub-a and sub-b
    assert p.have_accounts is True


def test_sovereign_azure_endpoints_are_recognized():
    # Gov and China spell the blob host differently from commercial. Matching
    # only ".blob.core.windows.net" would strand the Gov tenant whose export
    # this tool has actually been run against.
    for host in (
        "acct.blob.core.windows.net",        # commercial
        "acct.blob.core.usgovcloudapi.net",  # US Gov
        "acct.blob.core.chinacloudapi.cn",   # China
    ):
        assert custom.storage_kind(f"https://{host}/billing/exports") == "azure"


def test_non_azure_https_is_not_claimed_as_azure():
    # Previously any https:// URL was routed to the Azure backend and failed
    # there with a confusing "not an Azure blob URL" message.
    for url in (
        "https://storage.googleapis.com/bucket/prefix",
        "https://example.com/whatever",
    ):
        assert custom.storage_kind(url) == "unknown"
        with pytest.raises(base.ProfileError, match="cannot tell where"):
            custom.from_location(url)

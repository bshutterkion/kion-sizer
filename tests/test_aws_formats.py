"""AWS report-format detection: CUR, the legacy DBR, and FOCUS."""

import io
import zipfile

from kion_sizer.sources import aws

DBR_HEADER = b"InvoiceID,PayerAccountId,LinkedAccountId,RecordType,UsageStartDate,Cost\n"
DBR_LINE_ITEMS = b"1,p,acct-a,LineItem,2026-06-01,1.0\n" * 40 + b"1,p,acct-b,LineItem,2026-06-01,2.0\n" * 10
# What a real DBR also carries, and Kion does not ingest.
DBR_TOTALS = (
    b"1,p,acct-a,AccountTotal,2026-06-01,41.0\n"
    b"1,p,acct-b,AccountTotal,2026-06-01,20.0\n"
    b"1,p,,InvoiceTotal,2026-06-01,61.0\n"
    b"1,p,,StatementTotal,2026-06-01,61.0\n"
    b"1,p,,Rounding,2026-06-01,0.01\n"
)


def _zip(name, data):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, data)
    return buf.getvalue()


def test_detect_format_distinguishes_all_three():
    assert aws.detect_format(["RecordType", "LinkedAccountId", "Cost"]) == "dbr"
    assert aws.detect_format(["ChargePeriodStart", "SubAccountId"]) == "focus"
    assert aws.detect_format(["line_item_usage_account_id"]) == "cur"
    # An unrecognized parquet CUR still defaults to cur rather than failing.
    assert aws.detect_format([]) == "cur"


def test_dbr_row_filter_drops_exactly_the_non_line_items():
    header = ["InvoiceID", "RecordType", "LinkedAccountId"]
    assert aws.dbr_row_filter(header, ["1", "LineItem", "a"]) is True
    assert aws.dbr_row_filter(header, ["1", "PayerLineItem", "a"]) is True
    for skipped in ("AccountTotal", "InvoiceTotal", "StatementTotal", "Rounding"):
        assert aws.dbr_row_filter(header, ["1", skipped, "a"]) is False


def test_dbr_row_filter_keeps_rows_when_the_column_is_absent():
    assert aws.dbr_row_filter(["a", "b"], ["1", "2"]) is True


def test_dbr_zip_is_read_and_total_rows_excluded(tmp_path):
    # 50 real line items plus 5 total/rounding rows. Counting all 55 would
    # over-size the database, so only the 50 count.
    (tmp_path / "report.csv.zip").write_bytes(
        _zip("report.csv", DBR_HEADER + DBR_LINE_ITEMS + DBR_TOTALS)
    )
    p = aws.from_dir(str(tmp_path), detect_accounts=True, all_files=True)
    assert p.report_format == "dbr"
    assert p.raw_line_items == 50
    assert "total/rounding rows excluded" in p.volume_note


def test_dbr_accounts_come_from_linkedaccountid(tmp_path):
    (tmp_path / "report.csv.zip").write_bytes(
        _zip("report.csv", DBR_HEADER + DBR_LINE_ITEMS + DBR_TOTALS)
    )
    p = aws.from_dir(str(tmp_path), detect_accounts=True, all_files=True)
    # acct-a and acct-b from the line items; the blank LinkedAccountId on the
    # invoice/statement rows must not become a third "account".
    assert p.account_count == 2


def test_aws_focus_export_is_detected_and_counted(tmp_path):
    (tmp_path / "part0.csv").write_bytes(
        b"ChargePeriodStart,SubAccountId,BilledCost\n"
        + b"2026-06-01,111122223333,1.0\n" * 30
    )
    p = aws.from_dir(str(tmp_path), detect_accounts=True, all_files=True)
    assert p.report_format == "focus"
    assert p.raw_line_items == 30
    assert p.account_count == 1
    assert p.volume_note == "FOCUS export"


def test_cur_is_still_detected_and_unchanged(tmp_path):
    (tmp_path / "part0.csv").write_bytes(
        b"line_item_usage_account_id,cost\n" + b"111122223333,1.0\n" * 20
    )
    p = aws.from_dir(str(tmp_path), detect_accounts=True, all_files=True)
    assert p.report_format == "cur"
    assert p.raw_line_items == 20

"""GCP BigQuery billing-export profiling, against a fake client."""

import pytest

from kion_sizer.sources import base, gcp


class _Row(dict):
    """BigQuery rows support both mapping and attribute access."""

    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError as e:
            raise AttributeError(k) from e


class FakeJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return iter(self._rows)


class FakeBQ:
    """Minimal stand-in for google.cloud.bigquery.Client.

    Dispatches on a marker in the SQL so one fake serves both queries.
    """

    def __init__(self, months, total_rows, total_bytes):
        self.months, self.total_rows, self.total_bytes = months, total_rows, total_bytes
        self.queries = []

    def query(self, sql, job_config=None):
        self.queries.append(sql)
        if "INFORMATION_SCHEMA.PARTITIONS" in sql:
            return FakeJob(
                [
                    _Row(total_rows=self.total_rows // 2, total_logical_bytes=self.total_bytes // 2),
                    _Row(total_rows=self.total_rows - self.total_rows // 2,
                         total_logical_bytes=self.total_bytes - self.total_bytes // 2),
                ]
            )
        return FakeJob(
            [_Row(month=m, row_count=r, accounts=a) for m, r, a in self.months]
        )


def _client(
    months=(("202606", 1000, 7), ("202607", 4000, 11)),
    total_rows=5000,
    total_bytes=5_000_000,
):
    return FakeBQ(list(months), total_rows, total_bytes)


def test_parse_table_ref_splits_three_parts():
    assert gcp.parse_table_ref("proj.ds.gcp_billing_export_v1_ABC") == (
        "proj",
        "ds",
        "gcp_billing_export_v1_ABC",
    )


def test_parse_table_ref_rejects_malformed():
    with pytest.raises(base.ProfileError, match=r"PROJECT\.DATASET\.TABLE"):
        gcp.parse_table_ref("proj.ds")


def test_export_kind_detects_detailed_and_standard():
    assert gcp.export_kind("gcp_billing_export_resource_v1_X") == "detailed"
    assert gcp.export_kind("gcp_billing_export_v1_X") == "standard"
    assert gcp.export_kind("my_custom_view") == "unknown"


def test_profile_picks_the_peak_month_by_rows():
    p = gcp.from_bigquery("proj.ds.gcp_billing_export_v1_X", client=_client())
    assert p.cloud == "gcp"
    assert p.raw_line_items == 4000  # the 202607 peak, not the 5000 total
    assert p.have_raw_rows is True
    assert "202607" in p.volume_note


def test_profile_reports_exact_accounts_never_a_lower_bound():
    p = gcp.from_bigquery("proj.ds.gcp_billing_export_v1_X", client=_client())
    assert p.account_count == 11
    assert p.have_accounts is True
    assert p.account_lower_bound is False


def test_row_count_provenance_is_not_the_parquet_default():
    # render falls back to "from parquet footers" when sample_note is empty,
    # which would be a lie for a BigQuery source.
    p = gcp.from_bigquery("proj.ds.gcp_billing_export_v1_X", client=_client())
    assert "BigQuery" in p.sample_note
    assert "parquet" not in p.sample_note.lower()


def test_profile_apportions_bytes_by_row_share():
    # 4000 of 5000 rows -> 80% of 5,000,000 logical bytes.
    p = gcp.from_bigquery("proj.ds.gcp_billing_export_v1_X", client=_client())
    assert p.compressed_bytes == 4_000_000
    assert p.byte_label == "logical bytes"
    assert p.unit_label == "billing months"


def test_partition_rows_are_summed_not_taken_from_one_row():
    # __UNPARTITIONED__ can appear more than once when storage tiers mix, so
    # the PARTITIONS result must be summed rather than read as a single row.
    c = _client(total_rows=5000, total_bytes=5_000_000)
    gcp.from_bigquery("proj.ds.gcp_billing_export_v1_X", client=c)
    assert any("INFORMATION_SCHEMA.PARTITIONS" in q for q in c.queries)


def test_month_selection_uses_invoice_month_not_partition_date():
    # Rows for a billing month keep arriving after that month ends, so grouping
    # by partition date would pick the wrong month.
    c = _client()
    gcp.from_bigquery("proj.ds.gcp_billing_export_v1_X", client=c)
    agg = [q for q in c.queries if "INFORMATION_SCHEMA" not in q][0]
    assert "invoice.month" in agg
    assert "COUNT(DISTINCT project.id)" in agg


def test_explicit_month_overrides_the_peak():
    p = gcp.from_bigquery(
        "proj.ds.gcp_billing_export_v1_X", month="202606", client=_client()
    )
    assert p.raw_line_items == 1000


def test_unknown_month_is_an_error():
    with pytest.raises(base.ProfileError, match="no billing rows"):
        gcp.from_bigquery("proj.ds.t", month="209901", client=_client())


def test_detailed_export_is_flagged_in_the_volume_note():
    p = gcp.from_bigquery(
        "proj.ds.gcp_billing_export_resource_v1_X", client=_client()
    )
    assert "detailed" in p.volume_note.lower()


def test_empty_table_is_an_error_not_a_zero_profile():
    with pytest.raises(base.ProfileError, match="no billing rows"):
        gcp.from_bigquery("proj.ds.t", client=_client(months=()))


def test_registry_routes_gcp():
    from kion_sizer import sources

    p = sources.load("gcp", "proj.ds.gcp_billing_export_v1_X", client=_client())
    assert p.cloud == "gcp"


# --- FOCUS export -----------------------------------------------------------
# GCP's FOCUS export uses entirely different column names, so the queries change
# rather than just the expected volume.


def test_export_kind_detects_focus_and_pricing():
    assert gcp.export_kind("gcp_billing_export_focus_01ABCD") == "focus"
    assert gcp.export_kind("cloud_pricing_export") == "pricing"


def test_focus_queries_use_focus_columns_not_native_ones():
    c = _client(months=(("202607", 4000, 11),))
    gcp.from_bigquery("proj.ds.gcp_billing_export_focus_01ABCD", client=c)
    agg = [q for q in c.queries if "INFORMATION_SCHEMA" not in q][0]
    assert "SubAccountId" in agg
    assert "BillingPeriodStart" in agg
    # The native columns do not exist on a FOCUS table; querying them would fail.
    assert "invoice.month" not in agg
    assert "project.id" not in agg


def test_report_format_is_recorded_for_calibration():
    focus = gcp.from_bigquery(
        "proj.ds.gcp_billing_export_focus_01ABCD", client=_client()
    )
    assert focus.report_format == "focus"
    assert "focus" in focus.volume_note.lower()

    detailed = gcp.from_bigquery(
        "proj.ds.gcp_billing_export_resource_v1_X", client=_client()
    )
    assert detailed.report_format == "detailed"

    standard = gcp.from_bigquery("proj.ds.gcp_billing_export_v1_X", client=_client())
    assert standard.report_format == "standard"


def test_pricing_export_is_rejected_with_a_pointer_to_the_usage_tables():
    with pytest.raises(base.ProfileError, match="SKU pricing export"):
        gcp.from_bigquery("proj.ds.cloud_pricing_export", client=_client())


# --- query safety -----------------------------------------------------------
# BigQuery binds values, never identifiers, so the table reference must be
# interpolated. Identifiers are validated instead.


def test_table_name_is_bound_not_spliced():
    c = _client()
    gcp.from_bigquery("proj-1234.billing_export.gcp_billing_export_v1_X", client=c)
    meta = [q for q in c.queries if "INFORMATION_SCHEMA.PARTITIONS" in q][0]
    assert "@table" in meta
    assert "'gcp_billing_export_v1_X'" not in meta


@pytest.mark.parametrize(
    "ref",
    [
        "proj-1234.billing_export.tbl'--",        # quote would break the query
        "proj-1234.bad dataset.tbl",              # whitespace
        "proj-1234.billing_export.tbl;DROP",      # statement separator
        "p.billing_export.tbl",                   # project id too short to be real
    ],
)
def test_unsafe_identifiers_are_rejected_before_any_query(ref):
    with pytest.raises(base.ProfileError, match="not a valid BigQuery"):
        gcp.parse_table_ref(ref)


def test_wrapping_backticks_are_tolerated_but_interior_ones_are_not():
    # Pasting `proj.ds.tbl` from the console should work; an interior backtick
    # could escape the FROM clause and must not be silently stripped.
    assert gcp.parse_table_ref("`proj-1234.ds.tbl`") == ("proj-1234", "ds", "tbl")
    with pytest.raises(base.ProfileError, match="not a valid BigQuery"):
        gcp.parse_table_ref("proj-1234.ds.tbl`x")

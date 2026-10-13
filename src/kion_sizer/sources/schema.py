"""Billing-schema column resolution, shared across clouds.

FOCUS (the FinOps Open Cost and Usage Specification) is a cross-cloud format:
Azure Cost Management and GCP Cloud Billing can both emit it, and their FOCUS
columns are the same. The native formats differ per cloud, and Azure's differ
again between EA and MCA billing accounts — hence the alias lists.

Kion's own poller resolves these the same way (case-insensitively, first match
wins), so the sizer sees the same account identity the product will.
"""

from __future__ import annotations

# One entry per billing schema we can profile. `month` and `account` name the
# columns used for peak-month selection and member-account counting; the rest of
# a billing row is irrelevant to sizing.
FOCUS = "focus"
GCP_NATIVE = "gcp-native"
AZURE_NATIVE = "azure-native"
AWS_CUR = "aws-cur"
AWS_DBR = "aws-dbr"
OCI_NATIVE = "oci-native"

# Account-identity columns, in preference order. Lower-cased on comparison.
ACCOUNT_COLUMNS = {
    FOCUS: ("subaccountid",),
    GCP_NATIVE: ("project.id",),
    # EA exports use SubscriptionId; MCA uses SubscriptionGuid.
    AZURE_NATIVE: ("subscriptionid", "subscriptionguid"),
    AWS_CUR: ("line_item_usage_account_id", "lineitem/usageaccountid"),
    AWS_DBR: ("linkedaccountid",),
    # Kion maps an OCI compartment to an account, falling back to the tenancy.
    OCI_NATIVE: ("product/compartmentid", "lineitem/tenantid"),
}

# Billing-month columns. GCP native exposes a struct field; FOCUS a timestamp.
MONTH_COLUMNS = {
    FOCUS: ("billingperiodstart", "chargeperiodstart"),
    GCP_NATIVE: ("invoice.month",),
    AZURE_NATIVE: ("billingperiodstartdate", "date"),
    AWS_CUR: ("bill_billing_period_start_date",),
    AWS_DBR: ("usagestartdate",),
    OCI_NATIVE: ("lineitem/intervalusagestart",),
}


def pick(candidates: tuple, available) -> str | None:
    """First candidate present in `available`, compared case-insensitively.

    `available` may contain qualified names ("project.id"); comparison is on the
    lower-cased full name, and separators are not normalized away, because
    "project.id" and "project_id" are different columns.
    """
    lowered = {str(a).lower(): str(a) for a in available}
    for c in candidates:
        if c in lowered:
            return lowered[c]
    return None

"""Custom billing sources: FOCUS files in S3 or Azure blob storage.

A custom billing source is always FOCUS-formatted — the portal has no
proprietary reader for it (`getProprietaryService` has no IsCustom branch, and
custom/custom.go parses FOCUS rows directly). What varies is where the files
live: `StorageCloudProviderTypeID` selects an AWS or an Azure connection.

So this module is thin. It reuses whichever storage backend the location names
and stamps the profile as FOCUS.
"""

from __future__ import annotations

from .base import BillingProfile, ProfileError

# Every sovereign cloud spells the blob endpoint differently --
# .blob.core.windows.net (commercial), .blob.core.usgovcloudapi.net (US Gov),
# .blob.core.chinacloudapi.cn (China) -- but all share this stem. Matching the
# stem keeps Gov and China working; matching bare https:// would claim every
# HTTPS URL, including a GCS one, and fail later with a confusing message.
AZURE_HOST_HINT = ".blob.core."


def storage_kind(location: str) -> str:
    """Which storage backend hosts this custom source."""
    if location.startswith("s3://"):
        return "aws"
    if AZURE_HOST_HINT in location:
        return "azure"
    return "unknown"


def from_location(location: str, **kw) -> BillingProfile:
    """Profile a custom FOCUS billing source in S3 or Azure blob storage."""
    kind = storage_kind(location)
    if kind == "aws":
        from . import aws

        p = aws.from_s3(location, **kw)
    elif kind == "azure":
        from . import azure

        p = azure.from_container(location, **kw)
    else:
        raise ProfileError(
            f'cannot tell where the custom billing source "{location}" is hosted '
            "(expected an s3:// URI or an Azure blob URL)"
        )

    # Re-stamp the AWS-shaped profile as what it actually is: a custom source
    # carrying FOCUS rows, which calibrates separately from an AWS CUR.
    p.cloud = "custom"
    p.report_format = "focus"
    p.volume_note = (
        f"custom billing source hosted in {kind}; FOCUS format "
        "(custom sources have no proprietary format)"
    )
    return p

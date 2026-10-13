"""Source registry: maps a --source name to a profile loader."""

from __future__ import annotations

from .base import BillingProfile, ProfileError

__all__ = ["BillingProfile", "ProfileError", "load"]

_NOT_YET = ()


def load(cloud: str, location: str, **kw) -> BillingProfile:
    """Profile `location` using the loader for `cloud`.

    Every provider Kion ingests in bulk is implemented here; an unknown name is
    a clear error rather than a KeyError.
    """
    if cloud == "aws":
        from . import aws

        if location.startswith("s3://"):
            return aws.from_s3(location, **kw)
        return aws.from_dir(location, **kw)
    if cloud == "oci":
        from . import oci

        return oci.from_object_storage(location, **kw)
    if cloud == "azure":
        from . import azure

        return azure.from_container(location, **kw)
    if cloud == "custom":
        from . import custom

        return custom.from_location(location, **kw)
    if cloud == "gcp":
        from . import gcp

        return gcp.from_bigquery(location, **kw)
    if cloud in _NOT_YET:
        raise ProfileError(f"source {cloud!r} is not yet implemented")
    raise ProfileError(f"unknown source {cloud!r}")

"""Target registry: maps a --target name to its adapter."""

from __future__ import annotations

from .base import ComputeShape, TargetAdapter, TargetError

__all__ = ["ComputeShape", "SOURCE_ONLY", "TargetAdapter", "TargetError", "get"]

# Kion deploys on AWS and Azure. GCP is a billing *source* only — there is no
# GCP deployment target, so it is rejected here rather than listed as pending.
_NOT_YET = ("azure",)
_SOURCE_ONLY = SOURCE_ONLY = ("gcp",)


def get(cloud: str) -> TargetAdapter:
    if cloud == "aws":
        from .aws import AWSTarget

        return AWSTarget()
    if cloud in _NOT_YET:
        raise TargetError(f"target {cloud!r} is not yet implemented")
    if cloud in _SOURCE_ONLY:
        raise TargetError(
            f"Kion is not deployed on {cloud}; {cloud} is supported as a billing "
            f"source only (--source {cloud}, with --target aws or azure)"
        )
    raise TargetError(f"unknown target {cloud!r}")

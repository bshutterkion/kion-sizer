"""The target registry and the ComputeShape contract."""

import pytest

from kion_sizer import targets
from kion_sizer.targets.base import ComputeShape, TargetError


def test_compute_shape_defaults_are_none_for_other_kinds():
    # A Fargate shape leaves the k8s fields unset, and vice versa, so render
    # can branch on `kind` without guessing.
    s = ComputeShape(kind="fargate_task", vcpu=2, mem_gib=8.0, cpu_units=2048)
    assert s.node_machine_type is None
    assert s.cpu_millicores is None
    assert s.exceeds is False


def test_registry_rejects_unimplemented_targets():
    with pytest.raises(TargetError, match="not yet implemented"):
        targets.get("azure")


def test_registry_rejects_gcp_as_a_target_with_a_source_only_message():
    # Kion is not deployed on GCP, so this is a different error from "pending":
    # the message must point the user at --source instead.
    with pytest.raises(TargetError, match="billing source only"):
        targets.get("gcp")


def test_registry_rejects_unknown_target():
    with pytest.raises(TargetError, match="unknown target"):
        targets.get("aliyun")


def test_registry_returns_the_aws_adapter():
    t = targets.get("aws")
    assert t.name == "aws"


def test_aws_snap_compute_returns_a_fargate_shape():
    from kion_sizer.targets.aws import AWSTarget

    # 6.0 GiB / 1 vCPU is the ECS default poller size; it snaps to the 1 vCPU
    # (1024 units) task with 6144 MiB.
    s = AWSTarget().snap_compute(6.0, 1)
    assert s.kind == "fargate_task"
    assert s.cpu_units == 1024
    assert s.vcpu == 1
    assert s.mem_gib == 6.0
    assert s.exceeds is False
    assert s.node_machine_type is None


def test_aws_db_tiers_offline_uses_the_config_ladder():
    from kion_sizer import config
    from kion_sizer.targets.aws import AWSTarget

    cfg = config.default()
    tiers, label = AWSTarget().db_tiers(
        cfg, live=False, region="us-east-1", engine="mysql"
    )
    assert [t.name for t in tiers] == [t.name for t in cfg.target("aws").db_tiers]
    assert label == ""

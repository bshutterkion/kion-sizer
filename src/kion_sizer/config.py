"""Calibration constants and instance lookup tables.

Constants split three ways. Kion-side values (PUC row size, poller heap
behavior) are shared, because they describe Kion itself rather than any cloud.
Row-volume constants are per-source, because how billing line items collapse
into aggregated rows depends on the billing format. The buffer-pool fraction
and the tier ladder are per-target, because they describe the database the
deployment runs on.

The default config is shipped as package data so the tool is self-contained;
callers may override it with a file via load().
"""

from __future__ import annotations

import importlib.resources
from dataclasses import dataclass, field


class ConfigError(Exception):
    pass


@dataclass
class InstanceTier:
    name: str
    ram_gib: float


@dataclass
class ServiceBand:
    max_accounts: int
    core_tasks: int
    core_cpu: int
    core_mem_mib: int
    compliance_tasks: int
    compliance_cpu: int
    compliance_mem_mib: int


@dataclass
class SourceCal:
    """How one billing source's volume converts to aggregated PUC rows.

    `inherited` means these numbers were copied from the AWS reference rather
    than measured on that cloud. Render says so out loud; see model.recommend.
    """

    calibration: str
    inherited: bool
    rows_per_gib: float
    raw_to_aggregated_ratio: float


@dataclass
class TargetCal:
    """Database sizing inputs for one deployment target."""

    buffer_pool_fraction: float
    db_tiers: list[InstanceTier] = field(default_factory=list)


@dataclass
class Config:
    calibration_version: str
    bytes_per_row: float
    buffer_pool_headroom_frac: float
    buffer_pool_headroom_floor: float
    poller_base_gib: float
    poller_heap_gib_per_mrow: float
    poller_headroom_frac: float
    poller_floor_gib: float
    sources: dict = field(default_factory=dict)
    targets: dict = field(default_factory=dict)
    service_bands: list[ServiceBand] = field(default_factory=list)

    def source(self, cloud: str, report_format: str = "") -> SourceCal:
        """Calibration for a (provider, report format) pair.

        Resolves most-specific-first:

            sources["<provider>/<format>"] -> sources["<provider>"] -> "default"

        so only measured combinations need an explicit entry and everything else
        inherits the (uncalibrated) default rather than failing.
        """
        for key in (f"{cloud}/{report_format}" if report_format else None, cloud, "default"):
            if key and key in self.sources:
                return self.sources[key]
        raise ConfigError(
            f"no calibration for source {cloud!r} (format {report_format!r}) "
            "and no 'default' entry"
        )

    def target(self, cloud: str) -> TargetCal:
        try:
            return self.targets[cloud]
        except KeyError:
            raise ConfigError(f"no calibration for target {cloud!r}") from None

    def validate(self) -> None:
        for name, val in (
            ("bytes_per_row", self.bytes_per_row),
            ("poller_base_gib", self.poller_base_gib),
            ("poller_heap_gib_per_mrow", self.poller_heap_gib_per_mrow),
        ):
            if val <= 0:
                raise ConfigError(f"{name} must be positive, got {_gv(val)}")

        if not self.sources:
            raise ConfigError("sources must not be empty")
        for cloud, s in self.sources.items():
            for name, val in (
                ("rows_per_gib", s.rows_per_gib),
                ("raw_to_aggregated_ratio", s.raw_to_aggregated_ratio),
            ):
                if val <= 0:
                    raise ConfigError(
                        f"sources.{cloud}.{name} must be positive, got {_gv(val)}"
                    )

        if not self.targets:
            raise ConfigError("targets must not be empty")
        for cloud, t in self.targets.items():
            f = t.buffer_pool_fraction
            if f <= 0 or f > 1:
                raise ConfigError(
                    f"targets.{cloud}.buffer_pool_fraction must be in (0,1], "
                    f"got {_gv(f)}"
                )
            # An empty ladder is legal: a target that is declared but not yet
            # implemented ships one, and the registry rejects that target
            # before any ladder is consulted.
            rams = [x.ram_gib for x in t.db_tiers]
            if any(b < a for a, b in zip(rams, rams[1:])):
                raise ConfigError(
                    f"targets.{cloud}.db_tiers must be ascending by ram_gib"
                )

        for name, val in (
            ("buffer_pool_headroom_fraction", self.buffer_pool_headroom_frac),
            ("poller_headroom_fraction", self.poller_headroom_frac),
        ):
            if val < 0 or val > 1:
                raise ConfigError(f"{name} must be in [0,1], got {_gv(val)}")

        for name, val in (
            ("buffer_pool_headroom_floor_gib", self.buffer_pool_headroom_floor),
            ("poller_floor_gib", self.poller_floor_gib),
        ):
            if val < 0:
                raise ConfigError(f"{name} must not be negative, got {_gv(val)}")

        accts = [b.max_accounts for b in self.service_bands]
        if any(b < a for a, b in zip(accts, accts[1:])):
            raise ConfigError("service_bands must be ascending by max_accounts")


def _gv(v: float) -> str:
    """Render a number for an error message, dropping a whole float's .0."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def default() -> Config:
    text = importlib.resources.files("kion_sizer").joinpath("default.yaml").read_text()
    return _parse(text)


def load(path: str) -> Config:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise ConfigError(f'read config "{path}": {e}') from e
    return _parse(text)


def _parse(text: str) -> Config:
    import yaml

    raw = yaml.safe_load(text)

    # Legacy (pre-two-axis) configs put the AWS values at the top level. Map
    # them onto sources.aws / targets.aws so existing --config files keep
    # working unchanged.
    src_raw = raw.get("sources")
    if src_raw is None:
        src_raw = {
            "aws": {
                "calibration": raw["calibration_version"],
                "inherited": False,
                "rows_per_gib": raw["rows_per_gib"],
                "raw_to_aggregated_ratio": raw["raw_to_aggregated_ratio"],
            }
        }
    tgt_raw = raw.get("targets")
    if tgt_raw is None:
        tgt_raw = {
            "aws": {
                "buffer_pool_fraction": raw["buffer_pool_fraction"],
                "db_tiers": raw["rds_tiers"],
            }
        }

    c = Config(
        calibration_version=raw["calibration_version"],
        bytes_per_row=raw["bytes_per_row"],
        buffer_pool_headroom_frac=raw["buffer_pool_headroom_fraction"],
        buffer_pool_headroom_floor=raw["buffer_pool_headroom_floor_gib"],
        poller_base_gib=raw["poller_base_gib"],
        poller_heap_gib_per_mrow=raw["poller_heap_gib_per_mrow"],
        poller_headroom_frac=raw["poller_headroom_fraction"],
        poller_floor_gib=raw["poller_floor_gib"],
        sources={
            k: SourceCal(
                calibration=v["calibration"],
                inherited=bool(v["inherited"]),
                rows_per_gib=v["rows_per_gib"],
                raw_to_aggregated_ratio=v["raw_to_aggregated_ratio"],
            )
            for k, v in src_raw.items()
        },
        targets={
            k: TargetCal(
                buffer_pool_fraction=v["buffer_pool_fraction"],
                db_tiers=[InstanceTier(t["name"], t["ram_gib"]) for t in v["db_tiers"]],
            )
            for k, v in tgt_raw.items()
        },
        service_bands=[ServiceBand(**b) for b in raw["service_bands"]],
    )
    c.validate()
    return c

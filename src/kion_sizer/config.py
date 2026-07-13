"""Calibration constants and instance lookup tables.

The default config is shipped as package data so the tool is self-contained;
callers may override it with a file via load(). Mirrors internal/config.
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
class Config:
    calibration_version: str
    rows_per_gib: float
    raw_to_aggregated_ratio: float
    bytes_per_row: float
    buffer_pool_fraction: float
    buffer_pool_headroom_frac: float
    buffer_pool_headroom_floor: float
    poller_base_gib: float
    poller_heap_gib_per_mrow: float
    poller_headroom_frac: float
    poller_floor_gib: float
    rds_tiers: list[InstanceTier] = field(default_factory=list)
    service_bands: list[ServiceBand] = field(default_factory=list)

    def validate(self) -> None:
        for name, val in (
            ("rows_per_gib", self.rows_per_gib),
            ("bytes_per_row", self.bytes_per_row),
            ("raw_to_aggregated_ratio", self.raw_to_aggregated_ratio),
            ("poller_base_gib", self.poller_base_gib),
            ("poller_heap_gib_per_mrow", self.poller_heap_gib_per_mrow),
        ):
            if val <= 0:
                raise ConfigError(f"{name} must be positive, got {_gv(val)}")

        if self.buffer_pool_fraction <= 0 or self.buffer_pool_fraction > 1:
            raise ConfigError(
                f"buffer_pool_fraction must be in (0,1], got {_gv(self.buffer_pool_fraction)}"
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

        if not self.rds_tiers:
            raise ConfigError("rds_tiers must not be empty")
        rams = [t.ram_gib for t in self.rds_tiers]
        if any(b < a for a, b in zip(rams, rams[1:])):
            raise ConfigError("rds_tiers must be ascending by ram_gib")
        accts = [b.max_accounts for b in self.service_bands]
        if any(b < a for a, b in zip(accts, accts[1:])):
            raise ConfigError("service_bands must be ascending by max_accounts")


def _gv(v: float) -> str:
    """Render a number the way Go's %v does (whole floats without a trailing .0)."""
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
    c = Config(
        calibration_version=raw["calibration_version"],
        rows_per_gib=raw["rows_per_gib"],
        raw_to_aggregated_ratio=raw["raw_to_aggregated_ratio"],
        bytes_per_row=raw["bytes_per_row"],
        buffer_pool_fraction=raw["buffer_pool_fraction"],
        buffer_pool_headroom_frac=raw["buffer_pool_headroom_fraction"],
        buffer_pool_headroom_floor=raw["buffer_pool_headroom_floor_gib"],
        poller_base_gib=raw["poller_base_gib"],
        poller_heap_gib_per_mrow=raw["poller_heap_gib_per_mrow"],
        poller_headroom_frac=raw["poller_headroom_fraction"],
        poller_floor_gib=raw["poller_floor_gib"],
        rds_tiers=[InstanceTier(t["name"], t["ram_gib"]) for t in raw["rds_tiers"]],
        service_bands=[ServiceBand(**b) for b in raw["service_bands"]],
    )
    c.validate()
    return c

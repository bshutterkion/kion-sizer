"""Build a price table for the sizing recommendation.

Isolates all AWS Pricing-API I/O so model.py stays a pure function of its inputs
(same boundary as rds_catalog.py). Prices come from the live Pricing API when
reachable; on any failure — no creds, pricing:GetProducts denied, timeout — we
fall back to the embedded us-east-1 snapshot (prices.json) and say so in the
output. Tested with an injected fake pricing client.

Instance specs (vcpu/mem/arch) always come from the snapshot: they're
region-independent and let selection run without a describe-instance-types call
(so the only IAM surface this adds is pricing:GetProducts). Only usd_hr is fetched
live and varies by region.
"""

from __future__ import annotations

import importlib.resources
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import progress

# The Pricing API is only served from a few endpoints; us-east-1 always works.
# The deployment region is selected with the regionCode *filter*, not the client.
_PRICING_ENDPOINT_REGION = "us-east-1"

# databaseEngine values the Pricing API expects, keyed by our --rds-engine flag.
_RDS_ENGINE_NAMES = {
    "mysql": "MySQL",
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "mariadb": "MariaDB",
    "aurora-mysql": "Aurora MySQL",
    "aurora-postgresql": "Aurora PostgreSQL",
}


@dataclass(frozen=True)
class EC2Spec:
    name: str
    vcpu: int
    mem_gib: float
    arch: str  # "arm64" | "x86_64"


@dataclass
class PriceTable:
    region_label: str  # region these prices are for, for display
    source: str  # provenance line shown in the render
    hours_per_month: int
    fargate: dict  # {x86_vcpu, x86_gb, arm_vcpu, arm_gb} $/unit-hr
    ec2_specs: list[EC2Spec]
    _ec2_hr: dict  # instance name -> $/hr
    _rds_hr: dict  # db class -> $/hr

    def ec2_hr(self, name: str):
        return self._ec2_hr.get(name)

    def rds_hr(self, name: str):
        return self._rds_hr.get(name)


def load_snapshot() -> dict:
    text = importlib.resources.files("kion_sizer").joinpath("prices.json").read_text()
    return json.loads(text)


def _specs_from_snapshot(snap: dict) -> list[EC2Spec]:
    return [
        EC2Spec(name, d["vcpu"], float(d["mem_gib"]), d["arch"])
        for name, d in snap["ec2"].items()
    ]


def build_price_table(
    region: str | None,
    rds_class: str,
    engine: str = "mysql",
    *,
    client=None,
    live: bool = True,
    snapshot: dict | None = None,
) -> PriceTable:
    """Return a PriceTable for `region`. Tries the live Pricing API (or an
    injected `client`); on any failure returns the embedded snapshot table,
    annotated. `live=False` forces the snapshot (offline/tests).
    """
    snap = snapshot or load_snapshot()
    specs = _specs_from_snapshot(snap)
    fb_ec2 = {n: float(d["usd_hr"]) for n, d in snap["ec2"].items()}
    fb_rds = dict(snap["rds_mysql_single_az"])
    fb_far = dict(snap["fargate"])
    hpm = int(snap["hours_per_month"])
    region_code = region or snap["snapshot_region"]

    def snapshot_table(reason: str | None) -> PriceTable:
        src = f"embedded us-east-1 snapshot {snap['snapshot_date']}"
        if reason:
            src += f" (live pricing unavailable: {reason})"
        if region_code != snap["snapshot_region"]:
            src += "; us-east-1 approximation"
        return PriceTable(region_code, src, hpm, fb_far, specs, fb_ec2, fb_rds)

    if not live:
        return snapshot_table(None)

    try:
        cl = client or _make_client()
        # RDS first as a reachability/permission probe — any error propagates and
        # we degrade to the snapshot rather than half-populating.
        rds_hr = dict(fb_rds)
        live_rds = _fetch_rds(cl, region_code, engine, rds_class)
        if live_rds:
            rds_hr[rds_class] = live_rds
        far = _fetch_fargate(cl, region_code) or fb_far
        ec2_hr = _fetch_ec2_prices(cl, region_code, list(fb_ec2), fb_ec2)
        return PriceTable(
            region_code,
            f"live {region_code} on-demand",
            hpm,
            far,
            specs,
            ec2_hr,
            rds_hr,
        )
    except Exception as e:  # noqa: BLE001 — resilience: never fail the run over pricing
        return snapshot_table(str(e))


def _make_client():
    import boto3
    from botocore.config import Config

    cfg = Config(
        connect_timeout=5,
        read_timeout=10,
        retries={"max_attempts": 2, "mode": "standard"},
    )
    return boto3.client("pricing", region_name=_PRICING_ENDPOINT_REGION, config=cfg)


def _ondemand_usd(price_item: str):
    """Parse the first positive OnDemand USD/hr from a Pricing API PriceList JSON."""
    p = json.loads(price_item)
    for term in p.get("terms", {}).get("OnDemand", {}).values():
        for dim in term.get("priceDimensions", {}).values():
            usd = dim.get("pricePerUnit", {}).get("USD")
            if usd and float(usd) > 0:
                return float(usd)
    return None


def _get_products(client, service: str, filters: dict) -> list[str]:
    f = [{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in filters.items()]
    out: list[str] = []
    token = None
    while True:
        params = {"ServiceCode": service, "Filters": f, "MaxResults": 100}
        if token:
            params["NextToken"] = token
        resp = client.get_products(**params)
        out.extend(resp.get("PriceList", []))
        token = resp.get("NextToken")
        if not token:
            return out


def _fetch_rds(client, region_code: str, engine: str, db_class: str):
    db_engine = _RDS_ENGINE_NAMES.get(engine.lower(), "MySQL")
    items = _get_products(
        client,
        "AmazonRDS",
        {
            "regionCode": region_code,
            "instanceType": db_class,
            "databaseEngine": db_engine,
            "deploymentOption": "Single-AZ",
        },
    )
    for it in items:
        usd = _ondemand_usd(it)
        if usd:
            return usd
    return None


def _fetch_ec2_prices(
    client, region_code: str, names: list[str], fallback: dict
) -> dict:
    """Price every candidate concurrently. A per-instance miss (e.g. a type not
    offered in the region) falls back to the snapshot value for that one type;
    it does not change the overall live provenance.
    """

    def one(name: str):
        items = _get_products(
            client,
            "AmazonEC2",
            {
                "regionCode": region_code,
                "instanceType": name,
                "operatingSystem": "Linux",
                "preInstalledSw": "NA",
                "tenancy": "Shared",
                "capacitystatus": "Used",
                "marketoption": "OnDemand",
            },
        )
        for it in items:
            usd = _ondemand_usd(it)
            if usd:
                return usd
        return None

    prices = dict(fallback)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = progress.track(
            pool.map(one, names),
            "pricing EC2 candidates",
            total=len(names),
            unit="type",
        )
        for name, usd in zip(names, results):
            if usd:
                prices[name] = usd
    return prices


def _fetch_fargate(client, region_code: str):
    """Match Fargate's four rate products by usagetype suffix (the usagetype
    carries a regional prefix like 'USE1-' outside us-east-1)."""
    items = _get_products(client, "AmazonECS", {"regionCode": region_code})
    suffixes = {
        "Fargate-vCPU-Hours:perCPU": "x86_vcpu",
        "Fargate-GB-Hours": "x86_gb",
        "Fargate-ARM-vCPU-Hours:perCPU": "arm_vcpu",
        "Fargate-ARM-GB-Hours": "arm_gb",
    }
    rates: dict[str, float] = {}
    for it in items:
        p = json.loads(it)
        usagetype = p.get("product", {}).get("attributes", {}).get("usagetype", "")
        for suf, key in suffixes.items():
            if usagetype.endswith(suf):
                usd = _ondemand_usd(it)
                if usd:
                    rates[key] = usd
    # ARM suffixes also end with the x86 suffix? No — "...ARM-GB-Hours" does not
    # end with "Fargate-GB-Hours" (the "ARM-" breaks it), so keys stay distinct.
    return rates if len(rates) == 4 else None

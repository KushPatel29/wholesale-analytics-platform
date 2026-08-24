"""
The Marketing page payload.

Like the Finance page, this is entity-level and takes no operational filters: a
campaign has no department category. And like Finance, every number comes out of
`app.services.metrics`.

The one number it does not own is CLV. The Customers page already computes a
governed 12-month, discounted, gross-profit CLV, and the CLV:CAC ratio here
reads that same figure rather than deriving a second one - the whole point of
the metric layer is that two pages cannot disagree about what a customer is
worth.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from app.services import metrics


logger = logging.getLogger(__name__)

DATASET_DIR = Path("cache") / "marketing" / "fact_dataset"
FINANCE_DIR = Path("cache") / "finance" / "fact_dataset"
MANIFEST_NAME = "_manifest.json"

_cache_lock = threading.RLock()
_cache: dict[str, Any] = {"mtime": None, "frame": None, "manifest": None}


class MarketingDatasetNotBuiltError(RuntimeError):
    """Raised when the seeded campaign layer is missing."""


def _manifest_path() -> Path:
    return DATASET_DIR / MANIFEST_NAME


def dataset_available() -> bool:
    return DATASET_DIR.exists() and any(DATASET_DIR.glob("*.parquet"))


def _load() -> tuple[pd.DataFrame, dict[str, Any]]:
    if not dataset_available():
        raise MarketingDatasetNotBuiltError(
            f"Marketing dataset not found at {DATASET_DIR.as_posix()}. "
            "Run `python -m seed.generate_synthetic_marketing`."
        )
    try:
        mtime = _manifest_path().stat().st_mtime
    except FileNotFoundError:
        mtime = None
    with _cache_lock:
        if _cache["frame"] is not None and _cache["mtime"] == mtime:
            return _cache["frame"], _cache["manifest"]
        frame = pd.concat(
            [pd.read_parquet(path) for path in sorted(DATASET_DIR.glob("*.parquet"))],
            ignore_index=True,
        )
        frame["month"] = pd.to_datetime(frame["month"]).dt.date
        frame = frame.sort_values(["month", "channel"]).reset_index(drop=True)
        manifest: dict[str, Any] = {}
        if _manifest_path().exists():
            try:
                manifest = json.loads(_manifest_path().read_text(encoding="utf-8"))
            except Exception:
                logger.debug("marketing_bundle.manifest_unreadable", exc_info=True)
        _cache.update({"mtime": mtime, "frame": frame, "manifest": manifest})
        return frame, manifest


def _f(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sum(frame: pd.DataFrame, column: str) -> Optional[float]:
    if column not in frame.columns:
        return None
    return _f(frame[column].sum(min_count=1))


def available_fiscal_years() -> list[dict[str, Any]]:
    frame, _ = _load()
    years: list[dict[str, Any]] = []
    for year in sorted({int(v) for v in frame["fiscal_year"]}):
        scoped = frame[frame["fiscal_year"] == year]
        months = int(scoped["month"].nunique())
        years.append(
            {
                "year": year,
                "label": f"FY{year}",
                "months": months,
                "complete": months >= 12,
            }
        )
    return years


def default_fiscal_year() -> int:
    years = available_fiscal_years()
    complete = [y for y in years if y["complete"]]
    return (complete or years)[-1]["year"]


def _average_clv() -> tuple[Optional[float], str]:
    """
    The Customers page's governed CLV, or an honest absence.

    Returning None rather than a fallback matters: a CLV:CAC ratio computed
    against a number this module invented would be exactly the page-local
    recomputation the metric layer exists to prevent.
    """
    try:
        from app.services import bundle_service

        payload = bundle_service.bundle("customers", {"scope": "current_fy", "_sections": "clv"})
        cards = ((payload or {}).get("clv") or {}).get("cards") or {}
        value = _f(cards.get("avg_clv_12m"))
        if value and value > 0:
            return value, "12-month discounted gross-profit CLV, read from the Customers page"
    except Exception:
        logger.debug("marketing_bundle.clv_unavailable", exc_info=True)
    return None, "CLV unavailable, so the ratio is not stated"


def _by_channel(frame: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for channel, group in frame.groupby("channel", sort=False):
        spend = _f(group["spend"].sum())
        leads = _f(group["leads"].sum())
        qualified = _f(group["qualified_leads"].sum())
        won = _f(group["new_customers"].sum())
        rows.append(
            {
                "channel": str(channel),
                "spend": spend,
                "leads": leads,
                "qualified_leads": qualified,
                "new_customers": won,
                "cpl": metrics.cost_per_lead(spend, leads),
                # Channel CAC is marketing spend only. The selling-compensation
                # share is a company-level split with no channel attribution
                # behind it, and spreading it across channels would invent one.
                "cost_per_acquisition": metrics.cost_per_lead(spend, won),
                "qualification_rate_pct": metrics.share_pct(qualified, leads),
                "win_rate_pct": metrics.share_pct(won, qualified),
            }
        )
    return sorted(rows, key=lambda r: (r["spend"] or 0.0), reverse=True)


def _monthly(frame: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for month, group in frame.groupby("month", sort=True):
        spend = _f(group["spend"].sum())
        leads = _f(group["leads"].sum())
        won = _f(group["new_customers"].sum())
        # One row per campaign carries the same month-level selling figure, so
        # take it once rather than summing six copies of it.
        acquisition_selling = _f(group["acquisition_selling_spend_month"].iloc[0])
        total_spend = None if spend is None or acquisition_selling is None else spend + acquisition_selling
        rows.append(
            {
                "month": str(month),
                "label": str(group["month_label"].iloc[0]),
                "spend": spend,
                "acquisition_selling_spend": acquisition_selling,
                "total_acquisition_spend": total_spend,
                "leads": leads,
                "qualified_leads": _f(group["qualified_leads"].sum()),
                "new_customers": won,
                "cac": metrics.customer_acquisition_cost(total_spend, won),
                "cpl": metrics.cost_per_lead(spend, leads),
            }
        )
    return rows


def build_marketing_bundle(fiscal_year: Optional[int] = None) -> dict[str, Any]:
    """Entity-level acquisition performance for one fiscal year."""
    frame, manifest = _load()
    years = available_fiscal_years()
    known = {entry["year"] for entry in years}
    year = int(fiscal_year) if fiscal_year is not None and int(fiscal_year) in known else default_fiscal_year()
    selected = next(entry for entry in years if entry["year"] == year)

    scoped = frame[frame["fiscal_year"] == year].reset_index(drop=True)
    if scoped.empty:
        raise MarketingDatasetNotBuiltError(f"No marketing rows for FY{year}")

    marketing_spend = _sum(scoped, "spend")
    leads = _sum(scoped, "leads")
    qualified = _sum(scoped, "qualified_leads")
    new_customers = _sum(scoped, "new_customers")
    acquisition_selling = _f(
        scoped.groupby("month")["acquisition_selling_spend_month"].first().sum()
    )
    total_acquisition_spend = (
        None
        if marketing_spend is None or acquisition_selling is None
        else marketing_spend + acquisition_selling
    )

    channels = _by_channel(scoped)
    monthly = _monthly(scoped)

    cac = metrics.customer_acquisition_cost(total_acquisition_spend, new_customers)
    cpl = metrics.cost_per_lead(marketing_spend, leads)
    average_clv, clv_basis = _average_clv()
    kpis = [
        {
            "key": "cac",
            "label": "Customer acquisition cost",
            "value": cac,
            "unit": "currency",
            "formula": "Total marketing and sales spend / New customers",
            "basis": (
                f"Marketing spend plus an {int(round((manifest.get('acquisition_share_of_selling') or 0) * 100))}% "
                "acquisition share of selling compensation"
            ),
            "note": None,
        },
        {
            "key": "cpl",
            "label": "Cost per lead",
            "value": cpl,
            "unit": "currency",
            "formula": "Total marketing spend / New leads",
            "basis": "Marketing spend only",
            "note": None,
        },
        {
            "key": "clv_cac",
            "label": "CLV:CAC",
            "value": metrics.clv_to_cac_ratio(average_clv, cac),
            "unit": "multiple",
            "formula": "CLV / CAC",
            "basis": clv_basis,
            "note": "First-year basis, not the lifetime basis the 3:1 benchmark assumes",
        },
        {
            "key": "cac_payback",
            "label": "CAC payback",
            "value": metrics.cac_payback_months(cac, average_clv),
            "unit": "months",
            "formula": "CAC / (Annual customer value / 12)",
            "basis": clv_basis,
            "note": None,
        },
    ]

    return {
        "available": True,
        "generated_from": manifest.get("source"),
        "dataset_version": manifest.get("dataset_version"),
        "fiscal_years": years,
        "selected": selected,
        "basis": {
            "grain": manifest.get("grain"),
            "scope": (
                "Entity-level acquisition performance. Operational filters do not apply - a campaign "
                "has no department or region dimension."
            ),
            "new_customer_basis": manifest.get("new_customer_basis"),
            "spend_basis": manifest.get("spend_basis"),
            "modelled": (
                "Channel mix, lead counts and campaign attribution are modelled. Spend and new-customer "
                "counts are read from the finance layer and the sales fact."
            ),
            "excluded_reason": manifest.get("exclusion_reason"),
            "acquisition_share_of_selling": manifest.get("acquisition_share_of_selling"),
        },
        "totals": {
            "marketing_spend": marketing_spend,
            "acquisition_selling_spend": acquisition_selling,
            "total_acquisition_spend": total_acquisition_spend,
            "leads": leads,
            "qualified_leads": qualified,
            "new_customers": new_customers,
            "average_clv": average_clv,
        },
        "kpis": kpis,
        "channels": channels,
        "monthly": monthly,
        "charts": {
            "spend_vs_acquisitions": {
                "labels": [row["label"] for row in monthly],
                "spend": [row["total_acquisition_spend"] for row in monthly],
                "new_customers": [row["new_customers"] for row in monthly],
                "cac": [row["cac"] for row in monthly],
            },
            "channel_spend": {
                "labels": [row["channel"] for row in channels],
                "spend": [row["spend"] for row in channels],
                "new_customers": [row["new_customers"] for row in channels],
            },
            "funnel": {
                "labels": ["Leads", "Qualified", "Won"],
                "values": [leads, qualified, new_customers],
            },
        },
    }

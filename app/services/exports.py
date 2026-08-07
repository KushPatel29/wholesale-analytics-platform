from __future__ import annotations

from typing import Any, Dict, Iterable, List

import pandas as pd

TOP_PRODUCT_COLUMNS: List[str] = [
    "product_id",
    "sku",
    "desc",
    "category",
    "supplier",
    "uom",
    "revenue",
    "qty",
    "avg_price",
    "margin",
    "margin_pct",
    "first_sold",
    "last_sold",
]


def _ensure_columns(frame: pd.DataFrame, columns: Iterable[str]) -> pd.DataFrame:
    df = frame.copy()
    for col in columns:
        if col not in df.columns:
            df[col] = None
    return df[list(columns)]


def overview_frames_from_payload(payload: Dict[str, Any]) -> Dict[str, pd.DataFrame]:
    """Build export-ready DataFrames from the products overview payload."""
    frames: Dict[str, pd.DataFrame] = {}

    top_products = payload.get("top_products") or []
    top_df = pd.DataFrame(top_products)
    if top_df.empty:
        top_df = pd.DataFrame(columns=TOP_PRODUCT_COLUMNS)
    else:
        top_df = _ensure_columns(top_df, TOP_PRODUCT_COLUMNS)
    frames["Top Products"] = top_df

    trend = payload.get("trend") or []
    trend_df = pd.DataFrame(trend)
    if trend_df.empty:
        trend_df = pd.DataFrame(columns=["Period", "Revenue"])
    else:
        trend_df = trend_df.rename(columns={"period": "Period", "revenue": "Revenue"})
    frames["Revenue Trend"] = trend_df

    movers = payload.get("top_movers") or []
    movers_df = pd.DataFrame(movers)
    if movers_df.empty:
        movers_df = pd.DataFrame(columns=["SKU", "Description", "DeltaRevenue"])
    else:
        movers_df = movers_df.rename(columns={"sku": "SKU", "desc": "Description", "delta_rev": "DeltaRevenue"})
    frames["Top Movers"] = movers_df

    breakdowns = payload.get("breakdowns") or {}
    if isinstance(breakdowns, dict):
        for key, values in breakdowns.items():
            label = key.replace("by_", "").replace("_", " ").title()
            df = pd.DataFrame(values)
            if df.empty:
                df = pd.DataFrame(columns=["Key", "Revenue"])
            else:
                df = df.rename(columns={"key": "Key", "revenue": "Revenue"})
            frames[f"Breakdown - {label}"] = df

    pareto = payload.get("pareto") or []
    pareto_df = pd.DataFrame(pareto)
    if pareto_df.empty:
        pareto_df = pd.DataFrame(columns=["Rank", "SKU", "Revenue", "Cumulative%"])
    else:
        pareto_df = pareto_df.rename(columns={"rank": "Rank", "sku": "SKU", "revenue": "Revenue", "cum_pct": "Cumulative%"})
    frames["Pareto"] = pareto_df

    return frames

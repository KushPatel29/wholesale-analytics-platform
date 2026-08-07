from __future__ import annotations

"""
Small service to load the analytics dataframe and normalize/canonicalize
columns the UI depends on, bridging loader schema changes.

This module now uses centralized analytics_utils for consistency.
"""

from typing import Any, Optional
import pandas as pd
import numpy as np

from . import analytics_utils as au
import data_loader as loader


def _looks_like_name(val: Any) -> bool:
    if val is None:
        return False
    s = str(val).strip()
    if not s:
        return False
    return any(ch.isalpha() for ch in s)


def _to_string(series: pd.Series, *, strip: bool = False, drop_empty: bool = False) -> pd.Series:
    """
    Convert a column to pandas' nullable string dtype without eagerly copying
    more than necessary. Optionally strip whitespace and drop empty strings.
    """
    if not isinstance(series.dtype, pd.StringDtype):
        series = series.astype("string", copy=False)
    else:
        series = series.astype("string")

    if strip:
        series = series.str.strip()
    if drop_empty:
        series = series.where(series.str.len() > 0)
    return series


def canonicalize(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    # Date (prefer DateExpected → DateShipped → DateOrdered)
    date_sources = (
        "DateExpected_line",
        "DateExpected_order",
        "DateExpected",
        "DateShipped_line",
        "DateShipped_order",
        "DateShipped",
        "DateOrdered_line",
        "DateOrdered_order",
        "DateOrdered",
    )
    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    else:
        date_series = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
        for column in date_sources:
            if column not in df.columns:
                continue
            candidate = pd.to_datetime(df[column], errors="coerce")
            if candidate.notna().any():
                date_series = date_series.fillna(candidate)
            if date_series.notna().all():
                break
        df["Date"] = date_series

    if "Date" in df.columns:
        try:
            df["Date"] = df["Date"].dt.tz_localize(None)
        except Exception:
            pass

    # OrderId string
    if "OrderId" in df.columns:
        df["OrderId"] = _to_string(df["OrderId"])

    # Names
    if "CustomerName" not in df.columns and "Name" in df.columns:
        df["CustomerName"] = _to_string(df["Name"], drop_empty=True)
    if "RegionName" not in df.columns:
        if "Region_Name" in df.columns:
            df["RegionName"] = _to_string(df["Region_Name"], drop_empty=True)
        elif "Region" in df.columns:
            df["RegionName"] = _to_string(df["Region"], drop_empty=True)
    if "ShipperName" not in df.columns and "Shipper_Name" in df.columns:
        df["ShipperName"] = _to_string(df["Shipper_Name"], drop_empty=True)
    if "ShippingMethodLabel" in df.columns:
        df["ShippingMethodLabel"] = _to_string(df["ShippingMethodLabel"], strip=True, drop_empty=True)
    else:
        sm = None
        if "ShippingMethodName" in df.columns:
            sm = _to_string(df["ShippingMethodName"], strip=True, drop_empty=True)
        elif "ShipMethod_Name" in df.columns:
            sm = _to_string(df["ShipMethod_Name"], strip=True, drop_empty=True)
        elif "ShippingMethodRequested" in df.columns:
            raw = _to_string(df["ShippingMethodRequested"], strip=True)
            mask = raw.str.contains(r"[A-Za-z]", na=False)
            sm = raw.where(mask)
        if sm is not None:
            df["ShippingMethodLabel"] = sm
    if "ShippingMethodName" not in df.columns and "ShippingMethodLabel" in df.columns:
        df["ShippingMethodName"] = df["ShippingMethodLabel"]

    if "SkuName" not in df.columns and "SKU" in df.columns:
        df["SkuName"] = _to_string(df["SKU"], drop_empty=True)

    # Revenue/Cost/Profit using centralized utilities
    if "Revenue" not in df.columns:
        rev_col = au.revenue_column(df)
        if rev_col and rev_col in df.columns:
            df["Revenue"] = au.to_numeric_safe(df[rev_col])

    if "Cost" not in df.columns:
        cost_col = au.cost_column(df)
        if cost_col and cost_col in df.columns:
            df["Cost"] = au.to_numeric_safe(df[cost_col])

    if "Profit" not in df.columns:
        rev_col = au.revenue_column(df)
        cost_col = au.cost_column(df)
        profit = au.calculate_profit(df, rev_col, cost_col)
        if not profit.empty:
            df["Profit"] = profit

    # Pack metrics
    if "WeightLb" not in df.columns and "pack_weight_lb_sum" in df.columns:
        df["WeightLb"] = pd.to_numeric(df["pack_weight_lb_sum"], errors="coerce")
    if "ItemCount" not in df.columns and "pack_item_count_sum" in df.columns:
        df["ItemCount"] = pd.to_numeric(df["pack_item_count_sum"], errors="coerce")

    # ShipDate convenience if present
    if "ShipDate" not in df.columns:
        for c in ("DateShipped_line", "DateShipped_order"):
            if c in df.columns:
                df["ShipDate"] = pd.to_datetime(df[c], errors="coerce")
                break

    # strings for UI safety
    for c in ("CustomerId", "OrderId", "ProductId", "SupplierId"):
        if c in df.columns:
            df[c] = _to_string(df[c])

    try:
        df.reset_index(drop=True, inplace=True)
    except Exception:
        pass
    return df


def load_canonical_df(columns: Optional[list[str]] = None) -> pd.DataFrame:
    """
    Load the latest snapshot and canonicalize columns for analytics.
    Optional `columns` allows light loads for performance-sensitive callers.
    """
    try:
        df = loader.load_snapshot(columns=columns)
    except TypeError:
        df = loader.load_snapshot()  # backward-compatible signature
    if df is None or df.empty:
        return pd.DataFrame()
    return canonicalize(df)

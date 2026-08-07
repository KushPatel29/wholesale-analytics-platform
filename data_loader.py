# data_loader.py
from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sys
import threading
import time
from dataclasses import dataclass
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional, Dict, Any, Tuple, List, Iterable, Set, cast
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus
import re

import numpy as np
import pandas as pd
import requests
from sqlalchemy import create_engine, text as _sa_text, event

from app.services import analytics_utils as au

# ─────────────────────────────────────────────────────────────────────────────
# Logging / .env
# ─────────────────────────────────────────────────────────────────────────────

def _get_logger() -> logging.Logger:
    logger = logging.getLogger("data_loader")
    if logger.handlers:
        return logger
    level = os.getenv("LOADER_LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, level, logging.INFO)
    logger.setLevel(log_level)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    ch = logging.StreamHandler(stream=sys.stdout)
    ch.setLevel(log_level)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    try:
        logs_dir = Path("logs")
        logs_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            logs_dir / "data_loader.log",
            maxBytes=5_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        fh.setLevel(log_level)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception as e:
        logger.warning("Failed to set up file logging: %s", e)

    return logger

log = _get_logger()

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_PARQUET_PATH = (PROJECT_ROOT / "cache" / "fact_analytics.parquet").resolve()
DateLike = date | datetime | str

# WA_IGNORE_DOTENV lets the test suite run against the shipped defaults rather
# than whatever .env the developer happens to have in place - see conftest.py.
if os.getenv("WA_IGNORE_DOTENV", "").strip().lower() in {"1", "true", "yes", "on"}:
    log.debug("Skipping .env files (WA_IGNORE_DOTENV set)")
else:
    try:
        from dotenv import load_dotenv
        dev_path = PROJECT_ROOT / ".env.dev"
        if dev_path.exists():
            load_dotenv(dotenv_path=dev_path, override=True)
        load_dotenv(override=False)
    except Exception as e:
        log.warning("Failed to load .env files: %s", e)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    return v.strip().lower() in {"1", "true", "yes", "on"} if v else default

def _get_list(name: str, default: Optional[List[str]] = None) -> List[str]:
    v = os.getenv(name)
    return [x.strip() for x in v.split(",") if x.strip()] if v else list(default or [])

def _get_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if not v or not str(v).strip():
        return default
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        log.warning("Invalid %s=%s; falling back to %s", name, v, default)
        return default

def _initial_start_date(default: str = "2017-01-01") -> str:
    return os.getenv("INITIAL_START_DATE") or os.getenv("DATA_START_DATE") or default

def _date_range_from_env() -> Tuple[Optional[str], Optional[str]]:
    return os.getenv("DATA_START_DATE"), os.getenv("DATA_END_DATE")

def _default_date_window(months: Optional[int] = None) -> Tuple[str, str]:
    try:
        months_val = int(months) if months is not None else _get_int("DEFAULT_MONTH_WINDOW", 12)
    except Exception:
        months_val = 12
    months_val = max(1, months_val)
    end_ts = pd.Timestamp.utcnow().normalize()
    start_ts = (end_ts - pd.DateOffset(months=months_val)).normalize()
    return start_ts.date().isoformat(), end_ts.date().isoformat()


def _compose_date_filter(
    effective_expr: str,
    change_expr: Optional[str],
    *,
    start: Optional[str],
    end_plus_1: Optional[str],
    min_updated_at: Optional[str],
    include_null_effective: bool = False,
) -> Tuple[str, List[str]]:
    parts: List[str] = []
    if start and end_plus_1:
        base = f"{effective_expr} >= :start AND {effective_expr} < :end_plus_1"
        if include_null_effective:
            parts.append(f"(({base}) OR {effective_expr} IS NULL)")
        else:
            parts.append(base)
    elif include_null_effective:
        parts.append(f"{effective_expr} IS NULL")

    if change_expr and min_updated_at:
        parts.append(f"{change_expr} >= :min_updated_at")

    if not parts:
        return "", []

    predicate = " AND (" + " OR ".join(f"({part})" for part in parts) + ")"
    return predicate, parts

def _concat_frames(frames: List[pd.DataFrame], *, columns_if_empty: Optional[Iterable[str]] = None) -> pd.DataFrame:
    valid: List[pd.DataFrame] = []
    ordered_columns: List[str] = []
    seen_cols: Set[str] = set()
    for f in frames:
        if not isinstance(f, pd.DataFrame) or f.empty:
            continue
        if f.dropna(how="all").empty:
            continue
        for col in f.columns:
            if col not in seen_cols:
                seen_cols.add(col)
                ordered_columns.append(col)
        # Select columns that contain at least one non-null value. Avoid using
        # DataFrame-wide boolean masks which can have dtype edge-cases across
        # pandas/numpy versions — iterate columns explicitly for robustness.
        cols = [col for col in f.columns if f[col].notna().any()]
        if not cols:
            continue
        clean = f.loc[:, cols]
        valid.append(clean)
    if columns_if_empty:
        for col in columns_if_empty:
            if col not in seen_cols:
                seen_cols.add(col)
                ordered_columns.append(col)
    if not valid:
        cols = list(columns_if_empty) if columns_if_empty else ordered_columns
        return pd.DataFrame(columns=cols)
    result = pd.concat(valid, ignore_index=True, sort=False, copy=False)
    if ordered_columns:
        missing = [c for c in ordered_columns if c not in result.columns]
        for col in missing:
            result[col] = pd.NA
        result = result.reindex(columns=ordered_columns)
    return result

def _coalesce_numeric(df: pd.DataFrame, candidates: Iterable[str]) -> pd.Series:
    """Coalesce numeric columns in order, preferring the first non-null/non-zero candidate."""
    if df is None or df.empty:
        return pd.Series(dtype="float64")
    idx = df.index
    out = pd.Series(np.nan, index=idx, dtype="float64")
    for cand in candidates:
        if cand not in df.columns:
            continue
        series = pd.to_numeric(df[cand], errors="coerce")
        missing = out.isna() | (out == 0)
        fill_mask = missing & series.notna() & (series != 0)
        out = out.where(~fill_mask, series)
    return out


def _clean_text_dimension(series: Optional[pd.Series], index: pd.Index) -> pd.Series:
    if series is None:
        return pd.Series(pd.NA, index=index, dtype="string")
    text = series.astype("string").str.strip()
    text = text.where(text.notna() & text.ne(""))
    return text.astype("string")


def _normalize_product_family_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    out = df.copy()
    idx = out.index

    def _first_text(*names: str) -> pd.Series:
        merged = pd.Series(pd.NA, index=idx, dtype="string")
        for name in names:
            if name not in out.columns:
                continue
            merged = merged.fillna(_clean_text_dimension(out[name], idx))
        return merged

    out["Protein"] = _first_text("Protein", "ProteinType", "ProteinName")
    out["ProteinType"] = _first_text("ProteinType", "ProteinName", "Protein", "Category", "ProductCategory")
    out["ProteinName"] = _first_text("ProteinName", "ProteinType", "Protein", "Category", "ProductCategory")
    out["Category"] = _first_text("Category", "ProductCategory", "ProteinType", "ProteinName", "Protein")
    out["ProductCategory"] = _first_text("ProductCategory", "Category", "ProteinType", "ProteinName", "Protein")
    return out

def _effective_date_series(df: pd.DataFrame) -> pd.Series:
    """Deterministic EffectiveDate for audits and filtering."""
    if df is None or df.empty:
        return pd.Series(dtype="datetime64[ns]")
    candidates = [
        "EffectiveDate",
        "Date",
        "DateExpected",
        "DateOrdered",
        "DateShipped",
        "DateExpected_line",
        "DateExpected_order",
        "DateOrdered_line",
        "DateOrdered_order",
        "DateShipped_line",
        "DateShipped_order",
        "CreatedAt",
        "UpdatedAt",
    ]
    eff = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    for col in candidates:
        if col not in df.columns:
            continue
        src = pd.to_datetime(df[col], errors="coerce")
        if src.notna().any():
            eff = eff.fillna(src)
    return _coerce_datetime_naive(eff)


def _cost_source_coverage(df: pd.DataFrame) -> Dict[str, float]:
    if df is None or df.empty:
        return {"pack": 0.0, "orderline": 0.0, "product": 0.0, "null": 0.0}
    _total = len(df)
    def _pct(series: pd.Series) -> float:
        if series is None or series.empty:
            return 0.0
        return float(series.notna().mean()) * 100.0

    pack_pct = _pct(df.get("pack_unit_cost_effective", pd.Series(dtype="float64"))) or _pct(df.get("pack_cost_total", pd.Series(dtype="float64")))
    line_pct = _pct(df.get("CostPrice_orderline", pd.Series(dtype="float64"))) or _pct(df.get("CostPrice", pd.Series(dtype="float64"))) or _pct(df.get("CostPerUnit", pd.Series(dtype="float64")))
    product_pct = _pct(df.get("CostPrice_product", pd.Series(dtype="float64"))) or _pct(df.get("StandardCost", pd.Series(dtype="float64"))) or _pct(df.get("LastCost", pd.Series(dtype="float64")))
    cost_series = pd.to_numeric(df.get("Cost", pd.Series(dtype="float64")), errors="coerce")
    null_pct = float(cost_series.isna().mean() * 100.0) if len(cost_series) else 0.0
    return {
        "pack": round(min(pack_pct, 100.0), 4),
        "orderline": round(min(line_pct, 100.0), 4),
        "product": round(min(product_pct, 100.0), 4),
        "null": round(null_pct, 4),
    }

def _cost_coverage_details(
    df: pd.DataFrame,
    *,
    cost_col: str = "Cost",
    revenue_col: str = "Revenue",
    product_col: str = "ProductId",
    product_name_col: str = "ProductName",
    limit: int = 25,
) -> Dict[str, Any]:
    """Return compact diagnostics for cost coverage by product/revenue."""
    if df is None or df.empty:
        return {
            "rows": 0,
            "missing_pct": 0.0,
            "zero_pct": 0.0,
            "missing_rows": 0,
            "zero_rows": 0,
            "top_missing_by_revenue": [],
            "product_cost_summary": [],
        }
    cost_series = pd.to_numeric(df.get(cost_col, pd.Series(dtype="float64")), errors="coerce")
    revenue_series = pd.to_numeric(df.get(revenue_col, pd.Series(dtype="float64")), errors="coerce")
    missing_mask = cost_series.isna() | (cost_series == 0)
    zero_mask = cost_series == 0
    payload: Dict[str, Any] = {
        "rows": int(len(df)),
        "missing_pct": float(missing_mask.mean()) if len(cost_series) else 0.0,
        "zero_pct": float(zero_mask.mean()) if len(cost_series) else 0.0,
        "missing_rows": int(missing_mask.sum()),
        "zero_rows": int(zero_mask.sum()),
    }
    if product_col in df.columns:
        prod_series = df[product_col]
        name_series = df.get(product_name_col)
        base = pd.DataFrame(
            {
                "ProductId": prod_series,
                "ProductName": name_series if name_series is not None else pd.Series(pd.NA, index=df.index, dtype="string"),
                "Revenue": revenue_series,
                "Cost": cost_series,
                "missing": missing_mask,
            }
        )
        missing_by_product = (
            base.loc[base["missing"]]
            .groupby("ProductId", dropna=False, observed=True)
            .agg(
                revenue_missing=("Revenue", "sum"),
                rows=("missing", "size"),
                product_name=("ProductName", lambda s: s.dropna().astype("string").head(1).iloc[0] if s.notna().any() else None),
            )
            .sort_values("revenue_missing", ascending=False)
        )
        payload["top_missing_by_revenue"] = missing_by_product.head(limit).reset_index().to_dict(orient="records")

        coverage = (
            base.groupby("ProductId", dropna=False, observed=True)
            .agg(
                rows=("Cost", "size"),
                with_cost=("Cost", lambda s: pd.to_numeric(s, errors="coerce").gt(0).sum()),
                cost_min=("Cost", lambda s: pd.to_numeric(s, errors="coerce").replace(0, np.nan).min()),
                cost_max=("Cost", lambda s: pd.to_numeric(s, errors="coerce").replace(0, np.nan).max()),
                revenue_sum=("Revenue", "sum"),
                product_name=("ProductName", lambda s: s.dropna().astype("string").head(1).iloc[0] if s.notna().any() else None),
            )
        )
        coverage["coverage_pct"] = (coverage["with_cost"] / coverage["rows"] * 100.0).round(2)
        payload["product_cost_summary"] = (
            coverage.sort_values("revenue_sum", ascending=False).head(limit).reset_index().to_dict(orient="records")
        )
    return payload

def _compute_qty_price_coverage(
    df: pd.DataFrame,
) -> Tuple[pd.Series, pd.Series, Dict[str, Any], Dict[str, pd.Series]]:
    """
    Resolve billed quantity with pack-first, orderline fallback semantics.

    Returns (qty_billed, price_series, coverage_metrics, components).
    coverage_metrics include pack/revenue coverage plus missing-qty rates.
    """
    if df is None or df.empty:
        empty_series = pd.Series(dtype="float64")
        coverage = {
            "matched_lines": 0,
            "pack_rows": 0,
            "total_lines": 0,
            "pack_match_rate": 0.0,
            "pack_match_rate_pct": 0.0,
            "pack_row_rate": 0.0,
            "pack_row_rate_pct": 0.0,
            "revenue_from_packs": 0.0,
            "revenue_from_fallback_qty": 0.0,
            "revenue_missing_qty": 0.0,
            "revenue_from_packs_pct": 0.0,
            "revenue_from_fallback_pct": 0.0,
            "revenue_missing_qty_pct": 0.0,
            "revenue_total": 0.0,
            "missing_qty_rate": 0.0,
            "missing_qty_pct": 0.0,
            "missing_qty_rows": 0,
        }
        components = {
            "pack_qty_used": empty_series,
            "fallback_qty_used": empty_series,
            "missing_qty_mask": pd.Series(dtype=bool),
        }
        return empty_series, empty_series, coverage, components

    idx = df.index
    price = pd.Series(np.nan, index=idx, dtype="float64")
    if "Price" in df.columns:
        price = pd.to_numeric(df.get("Price"), errors="coerce")

    pack_weight = pd.Series(np.nan, index=idx, dtype="float64")
    if "pack_weight_lb_sum" in df.columns:
        pack_weight = pd.to_numeric(df.get("pack_weight_lb_sum"), errors="coerce")
    pack_items = pd.Series(np.nan, index=idx, dtype="float64")
    if "pack_item_count_sum" in df.columns:
        pack_items = pd.to_numeric(df.get("pack_item_count_sum"), errors="coerce")
    pack_counts = pd.Series(np.nan, index=idx, dtype="float64")
    if "pack_count" in df.columns:
        pack_counts = pd.to_numeric(df.get("pack_count"), errors="coerce")

    uob_col = au.best_column(df, ("UnitOfBillingId", "UnitOfBillingId_x", "UnitOfBillingId_y"))
    unit_of_billing = pd.to_numeric(df.get(uob_col), errors="coerce") if uob_col else pd.Series(np.nan, index=idx, dtype="float64")
    uom_col = au.best_column(df, ("UOM_UOMShortName", "UOM_UOMName", "UOMName", "UnitOfMeasure", "UnitOfBillingId"))
    uom_series = df[uom_col] if uom_col and uom_col in df.columns else None

    weight_mask = unit_of_billing == 3
    if uom_series is not None:
        uom_norm = pd.Series(uom_series, index=idx).astype("string").str.strip().str.lower()
        unit_uom_mask = uom_norm.str.contains("ea|each|case|cs|unit|pack|pkg", na=False)
        weight_mask = weight_mask | (
            (uom_norm.isin({"3", "lb", "lbs", "pound", "pounds", "weight"}) | uom_norm.str.contains("lb|pound|weight", na=False))
            & ~unit_uom_mask
        )

    qty_candidates: List[str] = []
    for cand in ("QuantityShipped", "QtyShipped", "QuantityOrdered", "QtyOrdered", "Units", "UnitQty", "Quantity", "Qty", "ItemCount"):
        if cand in df.columns:
            qty_candidates.append(cand)
    if not qty_candidates:
        try:
            log.warning("orderline.qty_fallback_missing", extra={"available_columns": list(df.columns)})
        except Exception:
            pass
    order_qty = _coalesce_numeric(df, qty_candidates) if qty_candidates else pd.Series(np.nan, index=idx, dtype="float64")

    weight_candidates = [c for c in ("WeightLb", "Weight", "ShippedLb", "ShipLb", "WeightOrdered", "OrderedWeight") if c in df.columns and c != "pack_weight_lb_sum"]
    weight_fallback = _coalesce_numeric(df, weight_candidates) if weight_candidates else pd.Series(np.nan, index=idx, dtype="float64")
    if weight_fallback.empty:
        weight_fallback = pd.Series(np.nan, index=idx, dtype="float64")
    weight_fallback = weight_fallback.combine_first(order_qty)

    pack_qty_base = pd.Series(np.where(weight_mask, pack_weight, pack_items), index=idx, dtype="float64")
    fallback_qty_base = pd.Series(np.where(weight_mask, weight_fallback, order_qty), index=idx, dtype="float64")

    pack_qty_used = pack_qty_base.where(pack_qty_base > 0)
    fallback_qty_used = fallback_qty_base.where(fallback_qty_base > 0)
    fallback_only_qty = fallback_qty_used.where(pack_qty_used.isna())

    qty_billed = pack_qty_used.combine_first(fallback_qty_used)
    missing_qty_mask = qty_billed.isna()
    qty_billed = qty_billed.fillna(0.0)

    revenue_from_packs = float((price * pack_qty_used.fillna(0.0)).sum())
    revenue_from_fallback_qty = float((price * fallback_only_qty.fillna(0.0)).sum())
    revenue_total = float((price * qty_billed).sum())
    revenue_missing_qty = float(max(0.0, revenue_total - revenue_from_packs - revenue_from_fallback_qty))

    total_lines = int(len(df))
    if pack_counts.notna().any():
        matched_lines = int(pack_counts.fillna(0).gt(0).sum())
        pack_rows = int(pack_counts.fillna(0).sum())
    else:
        matched_lines = int(pack_qty_used.fillna(0).gt(0).sum())
        pack_rows = matched_lines
    pack_match_rate = float(matched_lines / total_lines) if total_lines else 0.0
    pack_row_rate = float(pack_rows / total_lines) if total_lines else 0.0

    missing_qty_rate = float(missing_qty_mask.mean()) if len(missing_qty_mask) else 0.0
    revenue_from_packs_pct = float((revenue_from_packs / revenue_total) * 100.0) if revenue_total else 0.0
    revenue_from_fallback_pct = float((revenue_from_fallback_qty / revenue_total) * 100.0) if revenue_total else 0.0
    revenue_missing_pct = float((revenue_missing_qty / revenue_total) * 100.0) if revenue_total else 0.0

    coverage = {
        "matched_lines": matched_lines,
        "pack_rows": pack_rows,
        "total_lines": total_lines,
        "pack_match_rate": pack_match_rate,
        "pack_match_rate_pct": pack_match_rate * 100.0,
        "pack_row_rate": pack_row_rate,
        "pack_row_rate_pct": pack_row_rate * 100.0,
        "revenue_from_packs": revenue_from_packs,
        "revenue_from_fallback_qty": revenue_from_fallback_qty,
        "revenue_missing_qty": revenue_missing_qty,
        "revenue_from_packs_pct": revenue_from_packs_pct,
        "revenue_from_fallback_pct": revenue_from_fallback_pct,
        "revenue_missing_qty_pct": revenue_missing_pct,
        "revenue_total": revenue_total,
        "missing_qty_rate": missing_qty_rate,
        "missing_qty_pct": missing_qty_rate * 100.0,
        "missing_qty_rows": int(missing_qty_mask.sum()),
    }
    components = {
        "pack_qty_used": pack_qty_used,
        "fallback_qty_used": fallback_only_qty,
        "missing_qty_mask": missing_qty_mask,
    }
    return qty_billed, price, coverage, components


def _stage_metrics(df: Optional[pd.DataFrame]) -> Dict[str, Any]:
    if df is None or not isinstance(df, pd.DataFrame):
        return {
            "rows": 0,
            "order_ids": 0,
            "orderline_ids": 0,
            "product_ids": 0,
            "rev_sum": 0.0,
            "cost_sum": 0.0,
            "effective_date_null_rate": 0.0,
            "pack_match_rate": 0.0,
            "pack_match_rate_pct": 0.0,
            "pack_row_rate": 0.0,
            "pack_row_rate_pct": 0.0,
            "pack_rows": 0,
            "product_match_rate": 0.0,
            "cost_null_rate": 0.0,
            "cost_source_pack_pct": 0.0,
            "cost_source_orderline_pct": 0.0,
            "cost_source_product_pct": 0.0,
            "revenue_from_packs": 0.0,
            "revenue_from_fallback_qty": 0.0,
            "revenue_missing_qty": 0.0,
            "revenue_from_packs_pct": 0.0,
            "revenue_from_fallback_pct": 0.0,
            "revenue_missing_qty_pct": 0.0,
            "missing_qty_pct": 0.0,
        }

    metrics: Dict[str, Any] = {}
    metrics["rows"] = int(len(df))
    metrics["order_ids"] = int(df["OrderId"].nunique()) if "OrderId" in df.columns else 0
    metrics["orderline_ids"] = int(df["OrderLineId"].nunique()) if "OrderLineId" in df.columns else 0
    metrics["product_ids"] = int(df["ProductId"].nunique()) if "ProductId" in df.columns else 0

    qty_billed, price_series, cov, _ = _compute_qty_price_coverage(df)

    rev_col = "Revenue" if "Revenue" in df.columns else None
    cost_col = "Cost" if "Cost" in df.columns else None
    metrics["rev_sum"] = float(pd.to_numeric(df[rev_col], errors="coerce").fillna(0.0).sum()) if rev_col else float(
        (price_series * qty_billed).fillna(0.0).sum()
    )
    metrics["cost_sum"] = float(pd.to_numeric(df[cost_col], errors="coerce").fillna(0.0).sum()) if cost_col else 0.0

    eff = _effective_date_series(df)
    metrics["effective_date_null_rate"] = float(eff.isna().mean() * 100.0) if len(eff) else 0.0

    metrics["pack_match_rate"] = float(cov.get("pack_match_rate", 0.0))
    metrics["pack_match_rate_pct"] = float(cov.get("pack_match_rate_pct", metrics["pack_match_rate"] * 100.0))
    metrics["pack_row_rate"] = float(cov.get("pack_row_rate", 0.0))
    metrics["pack_row_rate_pct"] = float(cov.get("pack_row_rate_pct", metrics["pack_row_rate"] * 100.0))
    metrics["pack_rows"] = int(cov.get("pack_rows", 0))
    metrics["matched_lines"] = int(cov.get("matched_lines", 0))
    metrics["missing_qty_pct"] = float(cov.get("missing_qty_pct", 0.0))
    metrics["revenue_from_packs"] = float(cov.get("revenue_from_packs", 0.0))
    metrics["revenue_from_fallback_qty"] = float(cov.get("revenue_from_fallback_qty", 0.0))
    metrics["revenue_missing_qty"] = float(cov.get("revenue_missing_qty", 0.0))
    metrics["revenue_from_packs_pct"] = float(cov.get("revenue_from_packs_pct", 0.0))
    metrics["revenue_from_fallback_pct"] = float(cov.get("revenue_from_fallback_pct", 0.0))
    metrics["revenue_missing_qty_pct"] = float(cov.get("revenue_missing_qty_pct", 0.0))

    prod_mask = df["ProductId"].notna() if "ProductId" in df.columns else pd.Series(False, index=df.index)
    metrics["product_match_rate"] = float(prod_mask.mean() * 100.0) if len(prod_mask) else 0.0

    cost_series = pd.to_numeric(df.get("Cost", pd.Series(dtype="float64")), errors="coerce")
    metrics["cost_null_rate"] = float(cost_series.isna().mean() * 100.0) if len(cost_series) else 0.0

    cost_cov = _cost_source_coverage(df)
    metrics["cost_source_pack_pct"] = cost_cov.get("pack", 0.0)
    metrics["cost_source_orderline_pct"] = cost_cov.get("orderline", 0.0)
    metrics["cost_source_product_pct"] = cost_cov.get("product", 0.0)
    metrics["cost_source_null_pct"] = cost_cov.get("null", 0.0)
    return metrics


def _log_stage_audit(stage: str, df: Optional[pd.DataFrame], extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Structured audit log for pipeline stages with coverage and cost lineage."""
    payload = _stage_metrics(df)
    payload["stage"] = stage
    if extra:
        payload.update({k: v for k, v in extra.items() if v is not None})
    try:
        log.info("stage.audit", extra=payload)
    except Exception:
        log.debug("stage.audit.log_failed", exc_info=True)
    return payload

def _sort_descending(df: pd.DataFrame, preferred_columns: Iterable[str]) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    cols = [c for c in preferred_columns if c in df.columns]
    if not cols:
        return df
    asc = [False] * len(cols)
    try:
        return df.sort_values(by=cols, ascending=asc, kind="mergesort", na_position="last", ignore_index=True)
    except Exception:
        return df

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LoaderConfig:
    server: str
    database: str
    user: Optional[str] = None
    password: Optional[str] = None
    trusted: bool = False
    driver: str = os.getenv("MSSQL_ODBC_DRIVER", "ODBC Driver 18 for SQL Server")
    trust_cert: bool = True
    parquet_path: str = os.getenv("PARQUET_PATH", DEFAULT_PARQUET_PATH.as_posix())
    order_statuses: List[str] = None  # type: ignore
    # New: direct-SQL mode (no parquet by default)
    direct_sql_only: bool = _get_bool("DIRECT_SQL_ONLY", False)

    def __post_init__(self):
        test_mode = bool(os.getenv("PYTEST_CURRENT_TEST"))
        require_sql = self.direct_sql_only or _get_bool("FORCE_SQL_REFRESH", False)
        if require_sql and not self.server and not test_mode:
            raise ValueError("MSSQL_SERVER is required")
        if require_sql and not self.database and not test_mode:
            raise ValueError("MSSQL_DB is required")
        if require_sql and (not self.trusted and (not self.user or not self.password)) and not test_mode:
            raise ValueError("MSSQL_USER and MSSQL_PASSWORD are required for SQL auth")
        # Default to packed-only for production safety; allow override via env/CLI.
        if self.order_statuses is None:
            self.order_statuses = _get_list("ORDER_STATUSES", ["packed", "invoiced", "shipped", "delivered"])

def get_config() -> LoaderConfig:
    return LoaderConfig(
        server=os.getenv("MSSQL_SERVER", ""),
        database=os.getenv("MSSQL_DB", "Wholesale Analytics"),
        user=os.getenv("MSSQL_USER"),
        password=os.getenv("MSSQL_PASSWORD") or os.getenv("MSSQL_PASS"),
        trusted=_get_bool("MSSQL_TRUSTED", False),
        driver=os.getenv("MSSQL_ODBC_DRIVER", "ODBC Driver 18 for SQL Server"),
        trust_cert=True,
        parquet_path=os.getenv("PARQUET_PATH", DEFAULT_PARQUET_PATH.as_posix()),
        direct_sql_only=_get_bool("DIRECT_SQL_ONLY", False),
    )

# ─────────────────────────────────────────────────────────────────────────────
# DB Engine
# ─────────────────────────────────────────────────────────────────────────────

def create_mssql_engine(cfg: LoaderConfig):
    login_timeout = _get_int("DB_LOGIN_TIMEOUT", 10)
    mars = "Yes" if _get_bool("DB_MARS", True) else "No"
    odbc = (
        f"DRIVER={{{cfg.driver}}};SERVER={cfg.server};DATABASE={cfg.database};"
        f"{'Trusted_Connection=yes' if cfg.trusted else f'UID={cfg.user};PWD={cfg.password}'};"
        f"Encrypt=yes;TrustServerCertificate={'yes' if cfg.trust_cert else 'no'};"
        f"LoginTimeout={login_timeout};Connection Timeout={login_timeout};"
        f"MARS_Connection={mars};Application Name=Wholesale Analytics-DataLoader"
    )
    params = quote_plus(odbc)
    url = f"mssql+pyodbc:///?odbc_connect={params}"
    log.info("Creating MSSQL engine (trusted=%s, direct_sql_only=%s)", cfg.trusted, cfg.direct_sql_only)

    eng = create_engine(
        url,
        fast_executemany=True,
        pool_pre_ping=True,
        future=True,
        pool_size=_get_int("DB_POOL_SIZE", 10),
        max_overflow=_get_int("DB_MAX_OVERFLOW", 10),
        pool_timeout=_get_int("DB_POOL_TIMEOUT", 30),
        pool_recycle=_get_int("DB_POOL_RECYCLE", 1800),
    )

    lock_timeout = os.getenv("DB_LOCK_TIMEOUT_MS")
    read_uncommitted = _get_bool("DB_READ_UNCOMMITTED", True)  # default-on for read-only analytics

    @event.listens_for(eng, "connect")
    def _on_connect(dbapi_connection, connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        try:
            if lock_timeout:
                try:
                    lv = int(lock_timeout)
                    if lv > 0:
                        cursor.execute(f"SET LOCK_TIMEOUT {lv}")
                except ValueError:
                    pass
            if read_uncommitted:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
        finally:
            cursor.close()

    return eng

# ─────────────────────────────────────────────────────────────────────────────
# SQL helpers
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_date_value(value: Optional[DateLike]) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        ts = pd.to_datetime(value, errors="coerce")
    except Exception:
        return None
    if pd.isna(ts):
        return None
    return ts.date()

def _normalize_datetime_value(value: Optional[DateLike]) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, datetime.min.time())
    else:
        ts = pd.to_datetime(value, errors="coerce", utc=True)
        if pd.isna(ts):
            return None
        try:
            dt = ts.to_pydatetime()
        except Exception:
            dt = datetime.fromtimestamp(ts.timestamp(), tz=timezone.utc)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=None)

def _half_open_dates(start: Optional[DateLike], end: Optional[DateLike]) -> Tuple[Optional[date], Optional[date]]:
    s = _normalize_date_value(start)
    e = _normalize_date_value(end)
    if not e:
        return s, None
    return s, e + timedelta(days=1)

def _build_status_params(statuses: List[str]) -> Tuple[str, Dict[str, Any]]:
    placeholders = [f":s{i}" for i, _ in enumerate(statuses)]
    params = {f"s{i}": s for i, s in enumerate(statuses)}
    return ", ".join(placeholders), params

def _is_sqlserver_datetime_conversion_error(exc: Exception) -> bool:
    def _matches(text: str) -> bool:
        lowered = text.lower()
        if "conversion failed when converting date" in lowered and ("241" in lowered or "22007" in lowered):
            return True
        return "22007" in lowered and "241" in lowered

    if _matches(str(exc)):
        return True
    orig = getattr(exc, "orig", None)
    if orig is not None:
        try:
            args_text = " ".join(str(a) for a in getattr(orig, "args", []) if a is not None)
        except Exception:
            args_text = ""
        if _matches(args_text):
            return True
    return False

def _can_disable_updated_after(sql: str, params: Dict[str, Any]) -> bool:
    if not params or "updated_after" not in params:
        return False
    if params.get("updated_after") is None:
        return False
    return "updated_after" in sql.lower()

def _read_sql(engine, sql: str, params: Dict[str, Any], *, chunksize: Optional[int] = None) -> pd.DataFrame:
    max_retries = _get_int("DB_MAX_RETRIES", 2)
    base_ms = _get_int("DB_RETRY_BASE_MS", 250)
    attempt = 0
    retry_without_updated_after = False
    while True:
        try:
            with engine.connect() as conn:
                if chunksize and chunksize > 0:
                    frames = [c for c in pd.read_sql(_sa_text(sql), conn, params=params, chunksize=chunksize) if not c.empty]
                    return _concat_frames(frames)
                return pd.read_sql(_sa_text(sql), conn, params=params)
        except Exception as exc:
            if (
                not retry_without_updated_after
                and _is_sqlserver_datetime_conversion_error(exc)
                and _can_disable_updated_after(sql, params)
            ):
                prior_updated_after = params.get("updated_after")
                log.warning(
                    "SQL read conversion error with updated_after; retrying without updated_after",
                    extra={"updated_after": prior_updated_after},
                )
                params = dict(params)
                params["updated_after"] = None
                retry_without_updated_after = True
                continue
            if attempt >= max_retries:
                log.error("SQL read failed after %d attempts: %s", attempt + 1, exc)
                raise
            sleep_ms = base_ms * (2 ** attempt)
            log.warning("SQL read failed (attempt %d/%d): %s; retrying in %d ms",
                        attempt + 1, max_retries + 1, exc, sleep_ms)
            time.sleep(sleep_ms / 1000.0)
            attempt += 1


def _normalize_status_list(statuses: Optional[Any], default: Optional[List[str]] = None) -> List[str]:
    """
    Normalize status arguments from CLI/env into a clean list.
    Accepts comma/semicolon delimited strings or iterables; falls back to `default`.
    """
    if statuses is None:
        return list(default or [])
    if isinstance(statuses, str):
        raw = statuses.replace(";", ",").split(",")
        return [s.strip() for s in raw if s and s.strip()]
    try:
        return [str(s).strip() for s in statuses if s is not None and str(s).strip()]
    except Exception:
        return list(default or [])

def _read_by_ids(
    engine,
    *,
    table: str,
    id_col: str,
    ids: Iterable[Optional[Any]],
    select_cols: Iterable[str] | None = None,
    chunk_size: int = 900,
) -> pd.DataFrame:
    id_list = [x for x in pd.Series(list(ids)).dropna().unique().tolist() if x is not None]
    if not id_list:
        return pd.DataFrame(columns=list(select_cols) if select_cols else [])
    cols = ", ".join(select_cols) if select_cols else "*"
    frames: List[pd.DataFrame] = []
    for i in range(0, len(id_list), chunk_size):
        chunk = id_list[i : i + chunk_size]
        ph = ", ".join([f":id{i}" for i in range(len(chunk))])
        sql = f"SELECT {cols} FROM {table} WHERE {id_col} IN ({ph})"
        params = {f"id{i}": v for i, v in enumerate(chunk)}
        frames.append(_read_sql(engine, sql, params))
    return _concat_frames(frames, columns_if_empty=select_cols)

def _table_columns(engine, table: str) -> Set[str]:
    """Return lowercased column names for a table (best-effort; empty on failure)."""
    try:
        df = _read_sql(
            engine,
            """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = :table
            """,
            {"table": table},
        )
        if df.empty or "COLUMN_NAME" not in df.columns:
            return set()
        return {str(c).lower() for c in df["COLUMN_NAME"].tolist()}
    except Exception as exc:
        log.warning("column_inspect.failed", extra={"table": table, "error": str(exc)})
        return set()


def sql_truth(
    start: Optional[DateLike],
    end: Optional[DateLike],
    status: Any = "packed",
    *,
    engine=None,
) -> Dict[str, Any]:
    """
    Return SQL truth metrics for a date window using the canonical revenue and cost formulas.
    Window is half-open: start <= o.DateExpected < end_plus_1.
    """
    cfg = get_config()
    statuses = _normalize_status_list(status, default=cfg.order_statuses)
    if not statuses:
        statuses = cfg.order_statuses
    eng = engine or create_mssql_engine(cfg)
    s, e_plus_1 = _half_open_dates(start, end)
    placeholders, sparams = _build_status_params(statuses)

    effective_expr = "o.DateExpected"
    where_parts = [f"o.OrderStatus IN ({placeholders})"]
    params: Dict[str, Any] = dict(sparams)
    params["raw_start"] = start
    params["raw_end"] = end
    if s:
        where_parts.append(f"{effective_expr} >= :start")
        params["start"] = s
    if e_plus_1:
        where_parts.append(f"{effective_expr} < :end_plus_1")
        params["end_plus_1"] = e_plus_1
    where = " AND ".join(where_parts)

    pack_cols = _table_columns(eng, "Packs")
    orderline_cols = _table_columns(eng, "OrderLines")
    product_cols = _table_columns(eng, "Products")

    def _pick(cols: Set[str], candidates: List[str]) -> Optional[str]:
        for cand in candidates:
            if cand.lower() in cols:
                return cand
        return None

    pack_cost_total_col = _pick(pack_cols, ["costtotal", "totalcost", "cost", "packcost"])
    pack_cost_per_lb_col = _pick(pack_cols, ["costperlb", "costlb", "cost_per_lb"])
    pack_cost_per_unit_col = _pick(pack_cols, ["costprice", "costperunit", "unitcost", "unit_cost"])
    line_cost_rate_col = _pick(orderline_cols, ["costprice", "unitcost", "costperunit", "cost"])
    product_cost_rate_col = _pick(product_cols, ["costprice", "standardcost", "lastcost", "cost"])

    pack_selects = []
    if pack_cost_total_col:
        pack_selects.append(f", pa.[{pack_cost_total_col}] AS PackCostTotal")
    if pack_cost_per_lb_col:
        pack_selects.append(f", pa.[{pack_cost_per_lb_col}] AS PackCostPerLb")
    if pack_cost_per_unit_col:
        pack_selects.append(f", pa.[{pack_cost_per_unit_col}] AS PackCostPerUnit")
    line_cost_select = f", ol.[{line_cost_rate_col}] AS LineCostRate" if line_cost_rate_col else ""
    product_cost_select = f", pr.[{product_cost_rate_col}] AS ProductCostRate" if product_cost_rate_col else ""

    qty_expr = "CASE WHEN COALESCE(ProductUnitOfBillingId, LineUnitOfBillingId, 0) = 3 THEN COALESCE(WeightLb, QuantityShipped, QuantityOrdered, 0) ELSE COALESCE(ItemCount, QuantityShipped, QuantityOrdered, 0) END"
    weight_expr = "COALESCE(WeightLb, QuantityShipped, QuantityOrdered, 0)"
    item_expr = "COALESCE(ItemCount, QuantityShipped, QuantityOrdered, 0)"

    cost_terms: List[str] = []
    if pack_cost_total_col:
        cost_terms.append("NULLIF(PackCostTotal, 0)")
    if pack_cost_per_lb_col:
        cost_terms.append(f"CASE WHEN PackCostPerLb IS NOT NULL THEN {weight_expr} * PackCostPerLb END")
    if pack_cost_per_unit_col:
        cost_terms.append(f"CASE WHEN PackCostPerUnit IS NOT NULL THEN {item_expr} * PackCostPerUnit END")
    if line_cost_rate_col:
        cost_terms.append(f"CASE WHEN LineCostRate IS NOT NULL THEN {qty_expr} * LineCostRate END")
    if product_cost_rate_col:
        cost_terms.append(f"CASE WHEN ProductCostRate IS NOT NULL THEN {qty_expr} * ProductCostRate END")

    cost_available = bool(cost_terms)
    cost_expr = "COALESCE(" + ", ".join(cost_terms) + ")" if cost_terms else "NULL"

    qty_pack_only_expr = "CASE WHEN COALESCE(ProductUnitOfBillingId, LineUnitOfBillingId, 0) = 3 THEN COALESCE(WeightLb, 0) ELSE COALESCE(ItemCount, 0) END"
    qty_with_fallback_expr = "CASE WHEN COALESCE(ProductUnitOfBillingId, LineUnitOfBillingId, 0) = 3 THEN COALESCE(WeightLb, QuantityShipped, QuantityOrdered, 0) ELSE COALESCE(ItemCount, QuantityShipped, QuantityOrdered, 0) END"

    def _run_truth(join_type: str) -> Dict[str, Any]:
        pack_join = "JOIN" if join_type == "inner" else "LEFT JOIN"
        sql = f"""
        WITH base AS (
            SELECT
                o.OrderId,
                ol.OrderLineId,
                ol.ProductId,
                ol.Price,
                ol.QuantityShipped,
                ol.QuantityOrdered,
                ol.UnitOfBillingId as LineUnitOfBillingId,
                pr.UnitOfBillingId as ProductUnitOfBillingId,
                pa.PackId,
                pa.WeightLb,
                pa.ItemCount
                {''.join(pack_selects)}
                {line_cost_select}
                {product_cost_select}
            FROM dbo.Orders o
            JOIN dbo.OrderLines ol ON ol.OrderId = o.OrderId
            {pack_join} dbo.Packs pa ON pa.PickedForOrderLine = ol.OrderLineId
            LEFT JOIN dbo.Products pr ON pr.ProductId = ol.ProductId
            WHERE {where}
        )
        SELECT
            COUNT(DISTINCT OrderId) AS orders,
            COUNT(DISTINCT OrderLineId) AS order_lines,
            COUNT(DISTINCT ProductId) AS products,
            COUNT(CASE WHEN PackId IS NOT NULL THEN 1 END) AS pack_rows,
            COUNT(DISTINCT CASE WHEN PackId IS NOT NULL THEN OrderLineId END) AS matched_lines,
            SUM({qty_pack_only_expr} * Price) AS revenue_pack_only,
            SUM({qty_with_fallback_expr} * Price) AS revenue_with_fallback,
            SUM({cost_expr}) AS cost
        FROM base
        """
        df = _read_sql(eng, sql, params)
        if df.empty:
            return {
                "orders": 0,
                "order_lines": 0,
                "products": 0,
                "pack_rows": 0,
                "matched_lines": 0,
                "revenue_pack_only": 0.0,
                "revenue_with_fallback": 0.0,
                "cost": 0.0,
            }
        row = df.iloc[0].fillna(0)
        return {
            "orders": int(row.get("orders", 0) or 0),
            "order_lines": int(row.get("order_lines", 0) or 0),
            "products": int(row.get("products", 0) or 0),
            "pack_rows": int(row.get("pack_rows", 0) or 0),
            "matched_lines": int(row.get("matched_lines", 0) or 0),
            "revenue_pack_only": float(row.get("revenue_pack_only", 0.0) or 0.0),
            "revenue_with_fallback": float(row.get("revenue_with_fallback", 0.0) or 0.0),
            "cost": float(row.get("cost", 0.0) or 0.0),
        }

    cost_sources = {
        "pack_total": pack_cost_total_col,
        "pack_per_lb": pack_cost_per_lb_col,
        "pack_per_unit": pack_cost_per_unit_col,
        "orderline_rate": line_cost_rate_col,
        "product_rate": product_cost_rate_col,
    }

    pack_only = _run_truth("inner")
    fallback_truth = _run_truth("left")
    payload = {
        "start": start,
        "end": end,
        "statuses": statuses,
        "orders": fallback_truth["orders"],
        "order_lines": fallback_truth["order_lines"],
        "pack_rows": fallback_truth["pack_rows"],
        "matched_lines": fallback_truth["matched_lines"],
        "products": fallback_truth["products"],
        "revenue": fallback_truth["revenue_with_fallback"],
        "revenue_with_fallback": fallback_truth["revenue_with_fallback"],
        "revenue_pack_only": pack_only["revenue_pack_only"],
        "cost": fallback_truth["cost"],
        "cost_available_in_db": cost_available,
        "cost_not_available_in_db": not cost_available,
        "cost_sources": cost_sources,
    }
    payload["distinct_products"] = payload["products"]
    payload["pack_match_rate"] = float(payload["matched_lines"] / payload["order_lines"]) if payload["order_lines"] else 0.0
    payload["pack_row_rate"] = float(payload["pack_rows"] / payload["order_lines"]) if payload["order_lines"] else 0.0
    log.info("sql_truth", extra={k: v for k, v in payload.items() if k not in {"statuses"}} | {"statuses": ",".join(statuses)})
    return payload

# ─────────────────────────────────────────────────────────────────────────────
# Type / misc utilities
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_int(df: pd.DataFrame, col: str) -> None:
    if col not in df.columns:
        return
    s = df[col]
    try:
        if pd.api.types.is_integer_dtype(s):
            df[col] = s.astype("Int64")
            return
        cleaned = s.astype("string").str.strip()
        cleaned = cleaned.mask(cleaned == "")
        numeric = pd.to_numeric(cleaned, errors="coerce")
        if numeric.dropna().empty:
            return
        if (numeric.dropna() % 1).abs().le(np.finfo(float).eps).all():
            df[col] = numeric.round().astype("Int64")
    except Exception:
        pass

def _align_merge_key(left: pd.DataFrame, right: pd.DataFrame, key: str) -> None:
    if key not in left.columns:
        left[key] = pd.Series(pd.NA, index=left.index, dtype="string")
    if key not in right.columns:
        right[key] = pd.Series(pd.NA, index=right.index, dtype="string")
    _ensure_int(left, key)
    _ensure_int(right, key)
    l_dtype = left[key].dtype
    r_dtype = right[key].dtype
    if pd.api.types.is_integer_dtype(l_dtype) and pd.api.types.is_integer_dtype(r_dtype):
        return
    left[key] = left[key].astype("string")
    right[key] = right[key].astype("string")


def _ensure_string_key(df: pd.DataFrame, col: str) -> None:
    """Normalize a column to pandas' string dtype, trimming blanks to NA."""
    if col not in df.columns:
        df[col] = pd.Series(pd.NA, index=df.index, dtype="string")
        return
    series = pd.Series(df[col], index=df.index, dtype="string")
    series = series.str.strip()
    series = series.mask(series == "")
    df[col] = series


def _safe_merge_on_key(left: pd.DataFrame, right: pd.DataFrame, key: str, *, how: str = "left") -> pd.DataFrame:
    """
    Merge two frames on key, retrying with object dtype when pandas refuses due to dtype mismatch.
    """
    try:
        return left.merge(right, on=key, how=how)
    except ValueError as exc:
        message = str(exc)
        if key not in left.columns or key not in right.columns or "merge on" not in message:
            raise
        log.warning(
            "merge.type_mismatch",
            extra={"key": key, "left_dtype": str(left[key].dtype), "right_dtype": str(right[key].dtype)},
        )
        left[key] = left[key].astype(object)
        right[key] = right[key].astype(object)
        return left.merge(right, on=key, how=how)


def _collapse_column_variants(df: pd.DataFrame, base: str) -> None:
    """
    Merge duplicated columns produced by pandas merge suffixes (e.g. RegionId_x, RegionId_y) into `base`.
    Prefers the unsuffixed column, then right-hand (`_y`) values, keeping the first non-null per row.
    """
    base_lower = base.lower()
    pattern = re.compile(rf"^{re.escape(base_lower)}(?:_|$)")
    matches = [col for col in df.columns if pattern.match(col.lower())]
    if not matches:
        return

    def _sort_key(col: str) -> tuple[int, int]:
        lower = col.lower()
        if lower == base_lower:
            priority = 0
        elif lower.endswith("_y"):
            priority = 1
        elif lower.endswith("_x"):
            priority = 2
        else:
            priority = 3
        return priority, len(col)

    ordered = sorted(matches, key=_sort_key)
    combined = df[ordered].bfill(axis=1).iloc[:, 0]
    df[base] = combined
    drop_cols = matches if base not in matches else [col for col in matches if col != base]
    if drop_cols:
        df.drop(columns=drop_cols, inplace=True, errors="ignore")


def _merge_region_dimension(fact: pd.DataFrame, regions: pd.DataFrame) -> pd.DataFrame:
    _ensure_string_key(fact, "RegionId")
    _ensure_string_key(regions, "RegionId")
    return _safe_merge_on_key(fact, regions, "RegionId", how="left")

def _coerce_datetime_naive(series: pd.Series) -> pd.Series:
    s = pd.to_datetime(series, errors="coerce")
    try:
        if isinstance(s.dtype, pd.DatetimeTZDtype):
            return s.dt.tz_convert(None)
    except Exception:
        pass
    try:
        return s.dt.tz_localize(None)
    except Exception:
        return s

def _safe_div(a, b):
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where((b == 0) | pd.isna(b), np.nan, a / b)

# ─────────────────────────────────────────────────────────────────────────────
# User-scope helpers (no RBAC import to avoid cycles)
# ─────────────────────────────────────────────────────────────────────────────

_ALL_SENTINELS = {"all", "*", "__all__"}



def _normalize_scope_tokens(raw: Any) -> List[str]:
    """Return unique, normalized scope tokens from strings or iterables, ignoring 'all' sentinels."""

    if raw in (None, "", [], (), set()):

        return []



    def _iter_values(value: Any):

        if value is None:

            return ()

        if isinstance(value, str):

            return value.replace(";", ",").replace("|", ",").split(",")

        if isinstance(value, Iterable):

            items = []

            for item in value:

                if isinstance(item, str):

                    items.extend(item.replace(";", ",").replace("|", ",").split(","))

                else:

                    items.append(item)

            return items

        return (value,)



    tokens: List[str] = []

    seen: Set[str] = set()

    for item in _iter_values(raw):

        candidate = str(item).strip()

        if not candidate:

            continue

        lowered = candidate.lower()

        if lowered in _ALL_SENTINELS:

            continue

        if lowered in seen:

            continue

        seen.add(lowered)

        tokens.append(candidate)

    return tokens





def _build_scope_sql(

    *,

    is_super_user: bool,

    user_role: Optional[str],

    user_sales_rep_id: Optional[str],

    region_ids: Optional[List[str]],

    sales_rep_override: Any = None,

) -> Tuple[str, Dict[str, Any]]:
    """

    Return SQL fragment (without leading AND) and params.

    - Super users: unrestricted unless override is supplied.

    - Sales: Orders where SalesRepId or Customers.PrimarySalesRepId match the scoped reps.

    - Sales manager: if regions provided, filter by those Regions; otherwise fall back to sales rep scope.

    """

    role = (user_role or "").strip().lower()

    regions = _normalize_scope_tokens(region_ids or [])

    override_tokens = _normalize_scope_tokens(sales_rep_override)

    rep_tokens = override_tokens or _normalize_scope_tokens(user_sales_rep_id)



    if role == "sales_manager" and regions and not override_tokens:

        rep_tokens = []



    params: Dict[str, Any] = {}

    clauses: List[str] = []



    if regions:

        region_placeholders = ", ".join(f":region{i}" for i in range(len(regions)))

        clauses.append(f"(c.RegionId IN ({region_placeholders}))")

        params.update({f"region{i}": value for i, value in enumerate(regions)})



    if rep_tokens:

        rep_placeholders = ", ".join(f":rep{i}" for i in range(len(rep_tokens)))

        clauses.append(

            "("

            f" o.SalesRepId IN ({rep_placeholders})"

            " OR EXISTS (SELECT 1 FROM dbo.Customers cx WHERE cx.CustomerId = o.CustomerId"

            f" AND cx.PrimarySalesRepId IN ({rep_placeholders}))"

            ")"

        )

        params.update({f"rep{i}": value for i, value in enumerate(rep_tokens)})



    if is_super_user and not clauses:

        return "", {}



    if not clauses:

        return "", {}



    joined = " AND ".join(clauses)

    return f" ({joined}) ", params

# Extraction (direct SQL-first; still reuses Python merges where helpful)
# ─────────────────────────────────────────────────────────────────────────────

def extract_all(
    engine,
    start: Optional[DateLike],
    end: Optional[DateLike],
    statuses: List[str],
    *,
    scope_sql: str = "",
    scope_params: Optional[Dict[str, Any]] = None,
    updated_after: Optional[DateLike] = None,
) -> Dict[str, pd.DataFrame]:
    """Pull Orders & OrderLines with filters, then only needed dims."""
    s, e_plus_1 = _half_open_dates(start, end)
    placeholders, sparams = _build_status_params(statuses)
    updated_after_param = _normalize_datetime_value(updated_after)

    # Date predicates: canonical analytics window uses Orders.DateExpected only (half-open [start, end))
    order_date_expr = "o.DateExpected"
    line_date_expr = "o.DateExpected"

    # Base WHERE for status/scope on orders
    where_parts = [f"o.OrderStatus IN ({placeholders})"]
    if scope_sql.strip():
        where_parts.append(scope_sql.strip())
    if s:
        where_parts.append(f"{order_date_expr} >= :start")
        sparams["start"] = s
    if e_plus_1:
        where_parts.append(f"{order_date_expr} < :end_plus_1")
        sparams["end_plus_1"] = e_plus_1
    if updated_after_param is not None:
        where_parts.append(
            "("
            ":updated_after IS NULL OR "
            "(CASE WHEN ISDATE(o.UpdatedAt) = 1 THEN CONVERT(datetime, o.UpdatedAt) END) >= :updated_after OR "
            "(CASE WHEN ISDATE(o.CreatedAt) = 1 THEN CONVERT(datetime, o.CreatedAt) END) >= :updated_after"
            ")"
        )
    where = "WHERE " + " AND ".join(where_parts)

    line_where_parts: List[str] = []
    if s:
        line_where_parts.append(f"{line_date_expr} >= :start")
    if e_plus_1:
        line_where_parts.append(f"{line_date_expr} < :end_plus_1")
    if updated_after_param is not None:
        line_where_parts.append(
            "("
            ":updated_after IS NULL OR "
            "(CASE WHEN ISDATE(ol.UpdatedAt) = 1 THEN CONVERT(datetime, ol.UpdatedAt) END) >= :updated_after OR "
            "(CASE WHEN ISDATE(ol.CreatedAt) = 1 THEN CONVERT(datetime, ol.CreatedAt) END) >= :updated_after"
            ")"
        )
    line_where = f"WHERE {' AND '.join(line_where_parts)}" if line_where_parts else ""

    # Attach user scope (SQL-level) for less data transfer
    params = dict(sparams)
    params.update(scope_params or {})
    if updated_after_param is not None:
        params["updated_after"] = updated_after_param
    chunksize = _get_int("DB_CHUNKSIZE", 0) or None

    # Join Customers to Orders in the base pull so we can filter/label by Region/Primary rep names client-side
    orders_sql = f"""
        SELECT
            o.OrderId, o.OrderStatus, o.CustomerId, o.SalesRepId,
            o.CreatedAt, o.UpdatedAt, o.DateOrdered, o.DateExpected, o.DateShipped,
            o.ApprovedAt, o.ApprovedBy, o.ShippingMethodRequested, o.ShippingCharge,
            o.LinesTotalPrice, o.OrderTotalPrice, o.WarehouseId, o.PoReference,
            o.Instructions, o.ProductionSheetId, o.SubmittedAt, o.SubmittedBy,
            c.RegionId, c.PrimarySalesRepId
        FROM dbo.Orders o
        LEFT JOIN dbo.Customers c ON c.CustomerId = o.CustomerId
        {where}
    """

    # Order lines tied to filtered Orders
    orderline_cols = _table_columns(engine, "OrderLines")
    ol_cost_candidates = [
        "CostPrice", "CostPerUnit", "UnitCost", "Cost", "LineCost", "TotalCost", "ExtendedCost", "ExtCost"
    ]
    ol_select_extra = [c for c in ol_cost_candidates if c.lower() in orderline_cols and c != "CostPrice"]
    order_lines_sql = f"""
        SELECT
            ol.OrderLineId, ol.OrderId, ol.CreatedAt, ol.UpdatedAt,
            ol.LineNumber, ol.DateOrdered, ol.DateExpected, ol.DateShipped,
            ol.Description, ol.IsProductionNote, ol.ProductId, ol.QuantityOrdered,
            ol.OrderedUnitsOfMeasureId, ol.QuantityShipped, ol.ShipperId,
            ol.Price, ol.BasePrice, ol.ListPrice, ol.CostPrice
            {"," if ol_select_extra else ""}{", ".join(f"ol.{c}" for c in ol_select_extra)}
        FROM dbo.OrderLines ol
        JOIN ({orders_sql}) o ON o.OrderId = ol.OrderId
        {line_where}
    """

    log.info("Extracting Orders/OrderLines (statuses=%s start=%s end=%s; scoped=%s)", statuses, start, end, bool(scope_sql))
    orders = _read_sql(engine, orders_sql, params, chunksize=chunksize)
    lines  = _read_sql(engine, order_lines_sql, params, chunksize=chunksize)
    _log_stage_audit("extract.orders", orders, {"start": start, "end": end, "statuses": statuses})
    _log_stage_audit("extract.order_lines", lines, {"start": start, "end": end, "statuses": statuses})
    orders = _sort_descending(orders, ["DateExpected", "DateOrdered", "CreatedAt", "OrderId"])
    lines = _sort_descending(lines, ["DateExpected", "DateOrdered", "CreatedAt", "OrderLineId"])
    log.info("Pulled Orders=%d, OrderLines=%d", len(orders), len(lines))

    # Early exit if empty
    if lines.empty or orders.empty:
        dims = ["Customers","Regions","Products","Suppliers","UOM","Shippers","ShipMethods","Packs","PAD",
                "Batches","BatchTypes","PurchaseOrders","PurchaseOrderLines","IncomingShipments","UsersNames"]
        return {"Orders": orders, "OrderLines": lines, **{k: pd.DataFrame() for k in dims}}

    # IDs for dim fetch
    cid: Set[Any]       = set(pd.Series(orders["CustomerId"]).dropna().unique().tolist())
    rid: Set[Any]
    if "RegionId" in orders.columns:
        rid_series = pd.Series(orders["RegionId"]).dropna()
        rid = {v for v in rid_series.tolist() if str(v).strip()}
    else:
        rid = set()
    prod_id: Set[Any]   = set(pd.Series(lines["ProductId"]).dropna().unique().tolist())
    uom_id: Set[Any]    = set(pd.Series(lines["OrderedUnitsOfMeasureId"]).dropna().unique().tolist())
    _shipper_id: Set[Any]= set(pd.Series(lines["ShipperId"]).dropna().unique().tolist())
    _rep_ids: Set[Any]   = set(pd.Series(orders["SalesRepId"]).dropna().unique().tolist()) | set(pd.Series(orders["PrimarySalesRepId"]).dropna().unique().tolist())

    # Pull UsersNames once (for SalesRepName, PrimarySalesRepName)
    users = _read_sql(engine, """
        SELECT UserId, FirstName, LastName FROM dbo.UsersNames
    """, {})
    users["UserId"] = users["UserId"].astype("string").str.strip()
    users["FullName"] = (
        users["FirstName"].astype("string").str.strip() + " " + users["LastName"].astype("string").str.strip()
    ).str.strip()

    from concurrent.futures import ThreadPoolExecutor, as_completed
    conc = max(1, _get_int("LOADER_CONCURRENCY", 4))
    results: Dict[str, pd.DataFrame] = {"Orders": orders, "OrderLines": lines, "UsersNames": users}

    def fetch_customers():
        select_cols = [
            "CustomerId","IsActive",
            "Name as CustomerName","PrimarySalesRepId","PriceListId","RegionId",
            "Address1","Address2","City","Province","PostalCode","Phone","Email",
            "DefaultShippingMethodId","PackLabelId","LabelPriceMargin","SageId",
            "MetricBilling","OnHold","IsRetail","ShippingChargePriceOverride",
            "DeliveryLat","DeliveryLong"
        ]

        def _ensure_default_shipping_column(frame: pd.DataFrame) -> None:
            if "DefaultShippingMethodId" not in frame.columns:
                frame["DefaultShippingMethodId"] = pd.Series(
                    pd.NA, index=frame.index, dtype="Int64"
                )

        try:
            df = _read_by_ids(
                engine,
                table="dbo.Customers",
                id_col="CustomerId",
                ids=cid,
                select_cols=select_cols,
            )
        except Exception as exc:
            message = str(exc)
            if "Invalid column name" in message and "DefaultShippingMethodId" in message:
                log.warning(
                    "Customers.DefaultShippingMethodId missing; retrying without the column"
                )
                trimmed_cols = [c for c in select_cols if c != "DefaultShippingMethodId"]
                df = _read_by_ids(
                    engine,
                    table="dbo.Customers",
                    id_col="CustomerId",
                    ids=cid,
                    select_cols=trimmed_cols,
                )
                _ensure_default_shipping_column(df)
            else:
                raise
        else:
            _ensure_default_shipping_column(df)
        return "Customers", df

    def fetch_products():
        prod_cols = _table_columns(engine, "Products")
        base_cols = [
            "ProductId","IsActive","Name as ProductName","Description as ProductDescription","PackDetails",
            "SKU","SKUAlt1","UnitOfBillingId","SupplierId","BasePrice","ListPrice","Protein","LeadTime",
            "IsProduction","LastImport","IsInternalProduct","CostPrice",
        ]
        optional_costs = [("StandardCost", "standardcost"), ("LastCost", "lastcost")]
        optional_attributes = [
            ("Category", "category"),
            ("ProductCategory", "productcategory"),
            ("ProteinType", "proteintype"),
            ("ProteinName", "proteinname"),
        ]
        dynamic_cols = list(base_cols)
        for col, norm in optional_costs:
            if norm in prod_cols:
                dynamic_cols.append(col)
        for col, norm in optional_attributes:
            if norm in prod_cols:
                dynamic_cols.append(col)

        def _read_products(select_cols: List[str]) -> pd.DataFrame:
            return _read_by_ids(
                engine,
                table="dbo.Products",
                id_col="ProductId",
                ids=prod_id,
                select_cols=select_cols,
            )

        try:
            df = _read_products(dynamic_cols)
        except Exception as exc:
            message = str(exc)
            missing_opts: List[str] = []
            for col, _ in optional_costs:
                if f"Invalid column name '{col}'" in message or f"'{col}'" in message:
                    missing_opts.append(col)
            if missing_opts:
                trimmed = [c for c in dynamic_cols if c not in missing_opts]
                log.warning(
                    "Products optional cost columns missing; retrying without them",
                    extra={"missing": missing_opts},
                )
                df = _read_products(trimmed)
            else:
                raise
        # Create a combined SkuName if ProductName and SKU exist
        if not df.empty and "ProductName" in df.columns and "SKU" in df.columns:
            df["SkuName"] = df["ProductName"].astype(str) + " (" + df["SKU"].astype(str) + ")"
        elif not df.empty and "ProductName" in df.columns: # Fallback if only ProductName exists
            df["SkuName"] = df["ProductName"].astype(str)
        elif not df.empty and "SKU" in df.columns: # Fallback if only SKU exists
            df["SkuName"] = df["SKU"].astype(str)
        else:
            df["SkuName"] = pd.Series(pd.NA, index=df.index, dtype="string")
        df = _normalize_product_family_columns(df)
        return "Products", df

    def fetch_uom():
        df = _read_by_ids(engine, table="dbo.UnitsOfMeasure", id_col="UnitOfMeasureId", ids=uom_id,
                           select_cols=["UnitOfMeasureId","IsActive as UOM_IsActive","Name as UOMName","ShortName as UOMShortName","Description as UOMDescription","Fractional"])
        return "UOM", df

    def fetch_shippers():
        return "Shippers", _read_sql(engine, """
            SELECT ShipperId, IsActive as ShipperIsActive, CreatedAt as ShipperCreatedAt, UpdatedAt as ShipperUpdatedAt,
                   Name as ShipperName, Description as ShipperDescription
            FROM dbo.Shippers
        """, {})

    def fetch_shipmethods():
        return "ShipMethods", _read_sql(engine, """
            SELECT ShippingMethodId, ShipperId, IsActive as ShipMethodIsActive, CreatedAt as ShipMethodCreatedAt,
                   UpdatedAt as ShipMethodUpdatedAt, Name as ShippingMethodName, Description as ShippingMethodDescription
            FROM dbo.ShippingMethods
        """, {})

    with ThreadPoolExecutor(max_workers=conc) as ex:
        futs = [ex.submit(fetch_customers), ex.submit(fetch_products), ex.submit(fetch_uom), ex.submit(fetch_shippers), ex.submit(fetch_shipmethods)]
        for f in as_completed(futs):
            name, df = f.result()
            results[name] = df

    # Regions/Suppliers driven by Customers/Products
    sup_id: Set[Any] = set(pd.Series(results["Products"]["SupplierId"]).dropna().unique().tolist()) if not results["Products"].empty else set()

    def fetch_regions():
        df = _read_sql(
            engine,
            """
            SELECT RegionId, Name AS RegionName
            FROM dbo.Regions
            """,
            {},
        )
        if rid:
            keep = {str(v).strip() for v in rid if str(v).strip()}
            if keep:
                df = df[df["RegionId"].astype("string").str.strip().isin(keep)]
        return "Regions", df

    def fetch_suppliers():
        return "Suppliers", _read_by_ids(engine, table="dbo.Suppliers", id_col="SupplierId", ids=sup_id,
                                         select_cols=[
                                             "SupplierId","IsActive","Name as SupplierName","ShortName",
                                             "Description as SupplierDescription","LogoUrl","ShipVia",
                                             "IncoTerms","PaymentTerms","AddressLine1 as SupplierAddress1",
                                             "AddressLine2 as SupplierAddress2","City as SupplierCity",
                                             "StateProvinceID as SupplierStateProvinceID",
                                             "PostalCode as SupplierPostalCode","IncotermsLocation"
                                         ])

    with ThreadPoolExecutor(max_workers=conc) as ex:
        futs = [ex.submit(fetch_regions), ex.submit(fetch_suppliers)]
        for f in as_completed(futs):
            name, df = f.result()
            results[name] = df

    # Packs and PAD related to these orders (server-filtered by subquery of OrderLines)
    where_for_sub = where  # already includes scope/date/status
    sparams_for_sub = params

    def fetch_packs():
        pack_cols = _table_columns(engine, "Packs")
        base_cols = [
            "PackId", "ProductId", "ProductNotes", "Barcode", "WeightLb", "PieceCount", "ItemCount",
            "CreatedAt", "CreatedBy", "UpdatedAt", "UpdatedBy", "BatchId",
            "CommitedToOrderLine", "PickedAt", "PickedBy", "PickedForOrderLine", "PackedToBoxId",
            "ShippedAt", "UsedInBatchId", "UsedInBatchAt", "SupplierLotId", "UsedOtherwiseAt",
        ]
        optional_cost_cols = ["CostPrice", "CostPerUnit", "UnitCost", "CostPerLb", "CostLb", "Cost"]
        selected_cols: List[str] = []
        missing_base: List[str] = []
        for col in base_cols:
            if col.lower() in pack_cols:
                selected_cols.append(col)
            else:
                missing_base.append(col)
        for col in optional_cost_cols:
            if col.lower() in pack_cols:
                selected_cols.append(col)
        if missing_base:
            log.warning("packs.missing_base_columns", extra={"missing": missing_base})
        if not selected_cols:
            return "Packs", pd.DataFrame(columns=[])

        cols_sql = ", ".join(selected_cols)
        sql = f"""
            SELECT {cols_sql}
            FROM dbo.Packs
            WHERE (PickedForOrderLine IN (
                SELECT ol.OrderLineId FROM dbo.OrderLines ol JOIN dbo.Orders o ON o.OrderId=ol.OrderId
                LEFT JOIN dbo.Customers c ON c.CustomerId = o.CustomerId
                {where_for_sub}
            )) OR (CommitedToOrderLine IN (
                SELECT ol.OrderLineId FROM dbo.OrderLines ol JOIN dbo.Orders o ON o.OrderId=ol.OrderId
                LEFT JOIN dbo.Customers c ON c.CustomerId = o.CustomerId
                {where_for_sub}
            ))
        """

        try:
            df = _read_sql(engine, sql, sparams_for_sub)
        except Exception as exc:
            message = str(exc)
            missing_cols = [
                col
                for col in optional_cost_cols + base_cols
                if f"Invalid column name '{col}'" in message
            ]
            if missing_cols:
                trimmed = [c for c in selected_cols if c not in missing_cols]
                log.warning("packs.optional_columns_missing_retry", extra={"missing": missing_cols})
                if not trimmed:
                    return "Packs", pd.DataFrame(columns=[])
                df = _read_sql(engine, sql.replace(cols_sql, ", ".join(trimmed)), sparams_for_sub)
            else:
                raise
        return "Packs", df

    def fetch_pad():
        return "PAD", _read_sql(
            engine,
            f"""
            SELECT d.PackId, t.TypeName, d.AdditionalDate
            FROM dbo.PackAdditionalDates d
            LEFT JOIN dbo.PackAdditionalDateTypes t
              ON t.PackAdditionalDateTypeId = d.PackAdditionalDateTypeId
            WHERE d.PackId IN (
                SELECT p.PackId FROM dbo.Packs p
                WHERE (p.PickedForOrderLine IN (
                    SELECT ol.OrderLineId FROM dbo.OrderLines ol JOIN dbo.Orders o ON o.OrderId=ol.OrderId
                    LEFT JOIN dbo.Customers c ON c.CustomerId = o.CustomerId
                    {where_for_sub}
                )) OR (p.CommitedToOrderLine IN (
                    SELECT ol.OrderLineId FROM dbo.OrderLines ol JOIN dbo.Orders o ON o.OrderId=ol.OrderId
                    LEFT JOIN dbo.Customers c ON c.CustomerId = o.CustomerId
                    {where_for_sub}
                ))
            )
            """,
            sparams_for_sub
        )

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=min(2, conc)) as ex:
        futs = [ex.submit(fetch_packs), ex.submit(fetch_pad)]
        for f in as_completed(futs):
            name, df = f.result()
            results[name] = df

    batch_ids = set(pd.Series(results["Packs"].get("BatchId")).dropna().unique().tolist()) if not results["Packs"].empty else set()

    def fetch_batches():
        return "Batches", _read_by_ids(engine, table="dbo.Batches", id_col="BatchId", ids=batch_ids,
                                       select_cols=["BatchId","BatchTag","CreatedAt as BatchCreatedAt","CreatedBy as BatchCreatedBy",
                                                    "ClosedAt as BatchClosedAt","ClosedBy as BatchClosedBy","ProductionLocationId","BatchType"])

    def fetch_batchtypes():
        return "BatchTypes", _read_sql(engine, "SELECT BatchTypeId, TypeName as BatchTypeName FROM dbo.BatchTypes", {})

    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(fetch_batches), ex.submit(fetch_batchtypes)]
        for f in as_completed(futs):
            name, df = f.result()
            results[name] = df

    def fetch_pol():
        return "PurchaseOrderLines", _read_by_ids(
            engine,
            table="dbo.PurchaseOrderLines",
            id_col="ProductId",
            ids=prod_id,
            select_cols=[
                "PurchaseOrderLineId","PurchaseOrderId","ProductId","QuantityOrdered",
                "OrderedUnitsOfMeasureId","AgreedPrice","Note",
                "CreatedAt as POL_CreatedAt","CreatedBy as POL_CreatedBy",
                "UpdatedAt as POL_UpdatedAt","UpdatedBy as POL_UpdatedBy","InternalNote"
            ],
        )

    def fetch_pos(pol_df: pd.DataFrame):
        po_ids = set(pd.Series(pol_df["PurchaseOrderId"]).dropna().unique().tolist())
        df = _read_by_ids(
            engine,
            table="dbo.PurchaseOrders",
            id_col="PurchaseOrderId",
            ids=po_ids,
            select_cols=[
                "PurchaseOrderId","SupplierId","OrderDate","DeliveryDate",
                "SubmittedAt as PO_SubmittedAt","SubmittedBy as PO_SubmittedBy",
                "ReconciledAt","ReconciledBy","PoStatus",
                "UpdatedAt as PO_UpdatedAt","UpdatedBy as PO_UpdatedBy",
                "ReceivingInstructions","ShipVia as PO_ShipVia","IncoTerms as PO_IncoTerms",
                "PaymentTerms as PO_PaymentTerms","SpecialInstructions",
                "AddressLine1 as PO_Address1","AddressLine2 as PO_Address2","City as PO_City",
                "StateProvinceID as PO_StateProvinceID","PostalCode as PO_PostalCode","IncotermsLocation as PO_IncotermsLocation"
            ],
        )
        return df

    pol = fetch_pol()[1]
    pos = fetch_pos(pol) if not pol.empty else pd.DataFrame()
    results["PurchaseOrderLines"] = pol
    results["PurchaseOrders"] = pos

    def fetch_incoming():
        frames: List[pd.DataFrame] = []
        sup_id = set(pd.Series(results["Products"]["SupplierId"]).dropna().unique().tolist()) if not results["Products"].empty else set()
        if sup_id:
            frames.append(
                _read_by_ids(engine, table="dbo.IncomingShipments", id_col="SupplierId", ids=sup_id,
                             select_cols=[
                                 "IncomingShipmentId","CreatedAt as IS_CreatedAt","CreatedBy as IS_CreatedBy",
                                 "ReceivedAt","ReceivedBy","ShipmentInformation","SupplierId",
                                 "ClosedAt as IS_ClosedAt","ClosedBy as IS_ClosedBy","BatchId","PurchaseOrderId"
                             ])
            )
        if batch_ids:
            frames.append(
                _read_by_ids(engine, table="dbo.IncomingShipments", id_col="BatchId", ids=batch_ids,
                             select_cols=[
                                 "IncomingShipmentId","CreatedAt as IS_CreatedAt","CreatedBy as IS_CreatedBy",
                                 "ReceivedAt","ReceivedBy","ShipmentInformation","SupplierId",
                                 "ClosedAt as IS_ClosedAt","ClosedBy as IS_ClosedBy","BatchId","PurchaseOrderId"
                             ])
            )
        out = _concat_frames(frames)
        return "IncomingShipments", (out.drop_duplicates() if not out.empty else out)

    results["IncomingShipments"] = fetch_incoming()[1]
    return results

# ─────────────────────────────────────────────────────────────────────────────
# Transformations (unchanged core, plus user names enrichment)
# ─────────────────────────────────────────────────────────────────────────────

def _aggregate_packs(packs: pd.DataFrame, pad: pd.DataFrame, *, log_prefix: str = "packs") -> pd.DataFrame:
    if packs.empty:
        return pd.DataFrame(columns=[
            "OrderLineId","pack_weight_lb_sum","pack_item_count_sum","pack_piece_count_sum","pack_count",
            "pack_first_picked_at","pack_last_picked_at","pack_first_shipped_at","pack_last_shipped_at","pack_cost_total"
        ])
    p = packs.copy()
    p["OrderLineId"] = p.get("PickedForOrderLine")
    _ensure_int(p, "OrderLineId")
    missing_picked = int(p["OrderLineId"].isna().sum())
    committed_rows = int(p.get("CommitedToOrderLine", pd.Series(dtype="Int64")).notna().sum()) if "CommitedToOrderLine" in p.columns else 0
    cost_cols = [c for c in p.columns if "cost" in str(c).lower()]
    log.info(
        f"{log_prefix}.aggregate",
        extra={
            "rows": len(p),
            "orderline_dtype": str(p["OrderLineId"].dtype),
            "missing_picked": missing_picked,
            "commited_rows": committed_rows,
            "cost_cols_detected": cost_cols,
        },
    )

    # Derive pack-level cost totals if any cost fields are present
    p["_pack_cost_total"] = pd.Series(np.nan, index=p.index, dtype="float64")
    if cost_cols:
        per_lb_cols = [c for c in cost_cols if "lb" in str(c).lower()]
        per_unit_cols = [c for c in cost_cols if any(token in str(c).lower() for token in ["unit", "perunit", "piece", "each"])]
        total_cols = [c for c in cost_cols if c not in per_lb_cols + per_unit_cols]

        weight_series = pd.to_numeric(p.get("WeightLb"), errors="coerce")
        unit_series = pd.to_numeric(p.get("ItemCount"), errors="coerce")
        cost_total_series = _coalesce_numeric(p, total_cols) if total_cols else pd.Series(np.nan, index=p.index, dtype="float64")
        cost_per_lb_series = _coalesce_numeric(p, per_lb_cols) if per_lb_cols else pd.Series(np.nan, index=p.index, dtype="float64")
        cost_per_unit_series = _coalesce_numeric(p, per_unit_cols) if per_unit_cols else pd.Series(np.nan, index=p.index, dtype="float64")

        p["_pack_cost_total"] = cost_total_series
        if not cost_per_lb_series.empty:
            calc_lb = cost_per_lb_series * weight_series
            p["_pack_cost_total"] = p["_pack_cost_total"].combine_first(calc_lb)
        if not cost_per_unit_series.empty:
            calc_unit = cost_per_unit_series * unit_series
            p["_pack_cost_total"] = p["_pack_cost_total"].combine_first(calc_unit)

    grp = p.groupby("OrderLineId", dropna=False, observed=True)
    agg = grp.agg(
        pack_count=("PackId","count"),
        pack_piece_count_sum=("PieceCount","sum"),
        pack_item_count_sum=("ItemCount","sum"),
        pack_weight_lb_sum=("WeightLb","sum"),
        pack_first_picked_at=("PickedAt","min"),
        pack_last_picked_at=("PickedAt","max"),
        pack_first_shipped_at=("ShippedAt","min"),
        pack_last_shipped_at=("ShippedAt","max"),
    ).reset_index()
    try:
        cost_sum = grp["_pack_cost_total"].sum(min_count=1).reset_index(name="pack_cost_total")
        agg = agg.merge(cost_sum, on="OrderLineId", how="left")
    except Exception:
        pass
    if "pack_cost_total" not in agg.columns:
        agg["pack_cost_total"] = pd.Series(np.nan, index=agg.index, dtype="float64")

    if not pad.empty:
        pad = pad.copy()
        pad["AdditionalDate"] = pd.to_datetime(pad["AdditionalDate"], errors="coerce")
        min_df = pad.pivot_table(index="PackId", columns="TypeName", values="AdditionalDate", aggfunc="min")
        max_df = pad.pivot_table(index="PackId", columns="TypeName", values="AdditionalDate", aggfunc="max")
        min_df = min_df.add_prefix("pad_min_")
        max_df = max_df.add_prefix("pad_max_")
        wide = pd.concat([min_df, max_df], axis=1).reset_index()
        p2 = p[["PackId","OrderLineId"]].merge(wide, on="PackId", how="left")
        pad_agg = p2.groupby("OrderLineId", dropna=False).agg(["min","max"]).reset_index()
        pad_agg.columns = [("OrderLineId" if c[0]=="OrderLineId" else f"{c[0]}__{c[1]}") for c in pad_agg.columns]
        agg = agg.merge(pad_agg, on="OrderLineId", how="left")

    return agg


def _pack_merge_stats(
    fact: pd.DataFrame,
    aggregated_packs: pd.DataFrame,
    *,
    engine=None,
    sample_limit: int = 50,
    log_prefix: str = "pack_merge",
) -> Dict[str, Any]:
    stats: Dict[str, Any] = {
        "total_order_lines": 0,
        "matched_lines": 0,
        "unmatched_lines": 0,
        "pack_match_rate": 0.0,
        "pack_rows": 0,
        "pack_row_rate": 0.0,
    }
    if fact is None or aggregated_packs is None or fact.empty:
        return stats

    _ensure_int(fact, "OrderLineId")
    _ensure_int(aggregated_packs, "OrderLineId")
    fact_ids = {int(v) for v in pd.Series(fact.get("OrderLineId")).dropna().unique().tolist() if pd.notna(v)}
    pack_ids = {int(v) for v in pd.Series(aggregated_packs.get("OrderLineId")).dropna().unique().tolist() if pd.notna(v)}
    total = len(fact_ids)
    matched = len(fact_ids & pack_ids)
    unmatched_ids = sorted(list(fact_ids - pack_ids))
    pack_rows_val = int(pd.to_numeric(aggregated_packs.get("pack_count"), errors="coerce").fillna(0).sum())
    stats.update(
        {
            "total_order_lines": total,
            "matched_lines": matched,
            "unmatched_lines": total - matched,
            "pack_match_rate": float(matched / total) if total else 0.0,
            "pack_rows": pack_rows_val,
            "pack_row_rate": float(pack_rows_val / total) if total else 0.0,
        }
    )
    log.info(
        f"{log_prefix}.stats",
        extra={
            "total": total,
            "matched": matched,
            "unmatched": stats["unmatched_lines"],
            "pack_rows": stats["pack_rows"],
            "match_rate": stats["pack_match_rate"],
            "pack_row_rate": stats["pack_row_rate"],
        },
    )
    if stats["unmatched_lines"] > 0:
        sample = [int(v) for v in unmatched_ids[:sample_limit]]
        log.warning(f"{log_prefix}.unmatched_orderlines", extra={"examples": sample, "total_unmatched": stats["unmatched_lines"]})
        if engine is not None and sample:
            ph = ", ".join(f":ol{i}" for i in range(len(sample)))
            params = {f"ol{i}": v for i, v in enumerate(sample)}
            try:
                db_rows = _read_sql(
                    engine,
                    f"""
                    SELECT PickedForOrderLine AS OrderLineId, COUNT(*) AS pack_rows
                    FROM dbo.Packs
                    WHERE PickedForOrderLine IN ({ph})
                    GROUP BY PickedForOrderLine
                    """,
                    params,
                )
                log.info(f"{log_prefix}.db_probe", extra={"found": db_rows.to_dict(orient="records")})
            except Exception as exc:
                log.warning(f"{log_prefix}.db_probe_failed", extra={"error": str(exc)})
    return stats

def _looks_like_name(value: Any) -> bool:
    if value is None:
        return False
    s = str(value).strip()
    return bool(s and any(ch.isalpha() for ch in s))

def _resolve_shipping(fact: pd.DataFrame, ship_methods: pd.DataFrame, shippers: pd.DataFrame) -> pd.DataFrame:
    f = fact.copy()
    sm = ship_methods.copy()
    sh = shippers.copy()
    _ensure_int(sm, "ShippingMethodId")
    _ensure_int(sm, "ShipperId")
    _ensure_int(sh, "ShipperId")

    req_num = pd.to_numeric(f.get("ShippingMethodRequested"), errors="coerce").astype("Int64")
    f["_req_num"] = req_num
    f = f.merge(sm.add_prefix("SM_"), left_on="_req_num", right_on="SM_ShippingMethodId", how="left")

    unresolved = f["SM_ShippingMethodId"].isna()
    if unresolved.any():
        f["_req_name"] = f.get("ShippingMethodRequested").astype("string").str.strip()
        sm_map = sm[["ShippingMethodId","ShippingMethodName"]].copy()
        sm_map["SM_NameNorm"] = sm_map["ShippingMethodName"].astype("string").str.strip()
        sm_map.rename(columns={"ShippingMethodId":"SM2_Id"}, inplace=True)
        left = f.loc[unresolved, ["_req_name"]].reset_index()
        by_name = left.merge(sm_map[["SM2_Id","SM_NameNorm"]], left_on="_req_name", right_on="SM_NameNorm", how="left")
        if not by_name.empty:
            f.loc[by_name["index"], "SM_ShippingMethodId"] = by_name["SM2_Id"].values

    if "DefaultShippingMethodId" in f.columns:
        still = f["SM_ShippingMethodId"].isna()
        f.loc[still, "SM_ShippingMethodId"] = f.loc[still, "DefaultShippingMethodId"]

    f = f.merge(sm.add_prefix("SM2_"), left_on="SM_ShippingMethodId", right_on="SM2_ShippingMethodId", how="left")
    f = f.merge(sh.add_prefix("SH_"), left_on="SM2_ShipperId", right_on="SH_ShipperId", how="left")
    shipper_fallback = f["SH_ShipperName"].isna() & f.get("ShipperId").notna()
    if shipper_fallback.any():
        f.loc[shipper_fallback, ["SH_ShipperName"]] = (
            f.loc[shipper_fallback, ["ShipperId"]]
            .merge(sh[["ShipperId","ShipperName"]], on="ShipperId", how="left")["ShipperName"].values
        )

    ship_name = f.get("SM2_ShippingMethodName")
    label = ship_name.astype("string").str.strip().where(ship_name.notna() & (ship_name.str.len() > 0)) if ship_name is not None else pd.Series(pd.NA, index=f.index, dtype="string")
    requested = f.get("ShippingMethodRequested")
    if requested is not None:
        req = requested.astype("string").str.strip()
        req = req.where(req.apply(_looks_like_name))
        label = label.where(label.notna(), req)

    f["ShippingMethodName"] = label
    f["ShippingMethodLabel"] = label.fillna("Unknown")
    f["ShipperName"] = f["SH_ShipperName"]
    f.drop(columns=[c for c in f.columns if c.startswith(("SM_","SM2_","SH_"))] + ["_req_num","_req_name"], inplace=True, errors="ignore")
    return f

def derive_cost(
    fact: pd.DataFrame,
    *,
    products_df: Optional[pd.DataFrame] = None,
    packs_df: Optional[pd.DataFrame] = None,
    orderlines_df: Optional[pd.DataFrame] = None,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Derive Cost/Revenue/Profit with explicit pack -> order line -> product precedence.
    Cost definition: billed quantity (pack-first, weight-aware) multiplied by the best available
    unit cost (OrderLine cost price preferred, Product cost as fallback). No rows are dropped.
    Returns the mutated frame plus simple stats for logging.
    """
    if fact is None:
        return pd.DataFrame(), {"rows": 0}
    work = fact.copy()
    idx = work.index

    _ = packs_df  # reserved for potential pack-level cost enrichment

    # Map cost price from OrderLines (per-unit)
    if orderlines_df is not None and isinstance(orderlines_df, pd.DataFrame) and not orderlines_df.empty:
        ol = orderlines_df.copy()
        _ensure_int(ol, "OrderLineId")
        ol_cost_col = au.best_column(ol, ("CostPrice", "CostPerUnit", "UnitCost", "Cost"))
        if ol_cost_col:
            ol_map = pd.Series(pd.to_numeric(ol[ol_cost_col], errors="coerce").values, index=ol["OrderLineId"].astype("string"))
            work["CostPrice_orderline"] = pd.Series(work.get("OrderLineId"), dtype="string").map(ol_map)

    # Map cost price from Products (default/fallback)
    if products_df is not None and isinstance(products_df, pd.DataFrame) and not products_df.empty:
        prod = products_df.copy()
        pid_col = au.best_column(prod, ("ProductId", "product_id"))
        prod_cost_col = au.best_column(prod, ("CostPrice", "StandardCost", "LastCost", "Cost", "UnitCost", "CostPerUnit"))
        if pid_col and prod_cost_col:
            prod_map = pd.Series(pd.to_numeric(prod[prod_cost_col], errors="coerce").values, index=prod[pid_col].astype("string"))
            work["CostPrice_product"] = pd.Series(work.get("ProductId"), dtype="string").map(prod_map)

    # Quantity basis (pack-first with order line fallback for billing/revenue)
    weight_col = au.weight_column(work)
    uom_col = au.best_column(work, ("UOM_UOMShortName", "UOM_UOMName", "UOMName", "UnitOfMeasure", "UnitOfBillingId"))
    qty_for_cost, price_series, revenue_cov, _ = _compute_qty_price_coverage(work)
    work["_qty_for_cost"] = qty_for_cost
    if revenue_cov:
        try:
            log.info(
                "revenue.coverage",
                extra={
                    "pack_match_rate": revenue_cov.get("pack_match_rate"),
                    "pack_row_rate": revenue_cov.get("pack_row_rate"),
                    "revenue_from_packs": revenue_cov.get("revenue_from_packs"),
                    "revenue_from_fallback_qty": revenue_cov.get("revenue_from_fallback_qty"),
                    "revenue_missing_qty": revenue_cov.get("revenue_missing_qty"),
                    "missing_qty_pct": revenue_cov.get("missing_qty_pct"),
                },
            )
        except Exception:
            pass

    # Pack-level unit cost derived from total when available (helps coalescing/lineage)
    pack_cost_total_col = au.best_column(work, ("pack_cost_total",))
    if pack_cost_total_col and pack_cost_total_col in work.columns:
        pack_cost_total_series = pd.to_numeric(work[pack_cost_total_col], errors="coerce")
        with np.errstate(divide="ignore", invalid="ignore"):
            work["pack_unit_cost_effective"] = pack_cost_total_series / qty_for_cost.replace({0: np.nan})

    # Build cost candidates honoring precedence
    total_cost_candidates: List[str] = []
    if pack_cost_total_col and pack_cost_total_col in work.columns:
        total_cost_candidates.append(pack_cost_total_col)
    for cand in ["Cost", "ExtCost", "ExtendedCost", "TotalCost", "LineCost", "cost_ordered", "cost_shipped"]:
        if cand in work.columns and cand not in total_cost_candidates:
            total_cost_candidates.append(cand)
    cost_col = total_cost_candidates[0] if total_cost_candidates else None
    cost_rate_candidates: List[str] = []
    cost_per_lb_candidates: List[str] = []

    for cand in ["pack_unit_cost_effective"]:
        if cand in work.columns and cand not in cost_rate_candidates:
            cost_rate_candidates.append(cand)
    for cand in ["CostPrice_orderline", "CostPrice", "CostPrice_x", "CostPrice_line", "CostPerUnit", "UnitCost"]:
        if cand in work.columns and cand not in cost_rate_candidates:
            cost_rate_candidates.append(cand)
    for cand in ["CostPrice_product", "CostPrice_y", "StandardCost", "LastCost", "UnitCost_product", "CostPerUnit_product"]:
        if cand in work.columns and cand not in cost_rate_candidates:
            cost_rate_candidates.append(cand)
    for cand in ["CostPerLb", "CostLb", "CostPerLB", "LandedCostPerLb", "LandedCostPerLB"]:
        if cand in work.columns and cand not in cost_per_lb_candidates:
            cost_per_lb_candidates.append(cand)

    cost_series = au.resolve_cost(
        work,
        cost_col=cost_col,
        cost_rate_cols=cost_rate_candidates,
        cost_per_lb_cols=cost_per_lb_candidates,
        units_col="_qty_for_cost",
        weight_col=weight_col,
        uom_col=uom_col,
        preserve_units=True,
    )

    revenue_series = qty_for_cost * price_series

    work["Revenue"] = revenue_series.round(2)
    work["Cost"] = pd.to_numeric(cost_series, errors="coerce").round(2)
    work["Profit"] = (work["Revenue"] - work["Cost"]).round(2)
    work["MarginPct"] = np.where(work["Revenue"] > 0, work["Profit"] / work["Revenue"], np.nan)
    work["ROIPct"] = np.where(work["Cost"] > 0, work["Profit"] / work["Cost"], np.nan)
    work.drop(columns=["_qty_for_cost"], inplace=True, errors="ignore")

    unit_cost_candidates = [
        cand
        for cand in [
            "pack_unit_cost_effective",
            "CostPrice_orderline",
            "CostPrice",
            "CostPrice_x",
            "CostPrice_product",
            "CostPrice_y",
            "StandardCost",
            "LastCost",
        ]
        if cand in work.columns
    ]
    if unit_cost_candidates:
        work["unit_cost_effective"] = _coalesce_numeric(work, unit_cost_candidates)

    for col in ["CostPrice_orderline", "CostPrice_product", "CostPrice_po", "unit_cost_effective"]:
        if col not in work.columns:
            work[col] = pd.Series(np.nan, index=idx, dtype="float64")

    cost_numeric = pd.to_numeric(work.get("Cost"), errors="coerce")
    revenue_numeric = pd.to_numeric(work.get("Revenue"), errors="coerce")
    active_mask = revenue_numeric > 0
    cost_missing_mask = cost_numeric.isna() | (cost_numeric == 0)
    cost_zero_mask = cost_numeric == 0
    coverage = _cost_coverage_details(work, limit=25)
    coverage["rows_before_cost"] = int(len(idx))
    coverage["rows_after_cost"] = int(len(work))
    try:
        log.info(
            "cost.diagnostics",
            extra={
                "rows_before": len(idx),
                "rows_after": len(work),
                "missing_pct": coverage.get("missing_pct", 0.0),
                "zero_pct": coverage.get("zero_pct", 0.0),
                "missing_active_pct": float((cost_missing_mask & active_mask).mean()) if len(cost_missing_mask) else 0.0,
                "top_missing_by_revenue": coverage.get("top_missing_by_revenue", [])[:5],
            },
        )
        if len(idx) != len(work):
            log.warning("cost.cardinality_changed", extra={"before": len(idx), "after": len(work)})
        missing_rate = float((cost_missing_mask & active_mask).mean()) if len(cost_missing_mask) else 0.0
        if missing_rate > 0.02:
            log.warning("cost.coverage_low", extra={"missing_pct": missing_rate, "limit_pct": 0.02})
    except Exception:
        pass

    cost_stats = {
        "rows": len(work),
        "cost_missing_rate": float(cost_missing_mask.mean()) if len(cost_missing_mask) else 0.0,
        "cost_zero_rate": float(cost_zero_mask.mean()) if len(cost_zero_mask) else 0.0,
        "rows_before_cost": int(len(idx)),
        "rows_after_cost": int(len(work)),
        "cost_missing_active_rate": float((cost_missing_mask & active_mask).mean()) if len(cost_missing_mask) else 0.0,
    }
    cost_stats.update(
        {
            "pack_match_rate": revenue_cov.get("pack_match_rate", 0.0),
            "pack_row_rate": revenue_cov.get("pack_row_rate", 0.0),
            "revenue_from_packs": revenue_cov.get("revenue_from_packs", 0.0),
            "revenue_from_fallback_qty": revenue_cov.get("revenue_from_fallback_qty", 0.0),
            "revenue_missing_qty": revenue_cov.get("revenue_missing_qty", 0.0),
            "missing_qty_pct": revenue_cov.get("missing_qty_pct", 0.0),
        }
    )
    cost_stats.update({k: v for k, v in coverage.items() if k not in cost_stats})
    return work, cost_stats

def canonicalize_columns(df: pd.DataFrame, *, best_effort: bool = False) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame):
        raise TypeError("canonicalize_columns expects a pandas DataFrame")
    out = _normalize_product_family_columns(df.copy())
    idx = out.index

    def _first_series(*names):
        for name in names:
            if name in out.columns:
                s = out[name]
                if isinstance(s, pd.Series):
                    return s
        return None

    def _ensure_string(column, *candidates):
        src = _first_series(column, *candidates)
        out[column] = src.astype("string") if src is not None else pd.Series(pd.NA, index=idx, dtype="string")

    def _ensure_float(column, *candidates, default=0.0, fill: bool = True):
        src = _first_series(column, *candidates)
        values = pd.to_numeric(src, errors="coerce") if src is not None else pd.Series(np.nan, index=idx, dtype="float64")
        if fill:
            values = values.fillna(default)
        out[column] = values.astype("float64").round(2)
        return values

    # Dates (EffectiveDate first, then canonical fallbacks)
    date_candidates = [
        "EffectiveDate",
        "DateExpected","DateExpected_line","DateExpected_order",
        "DateOrdered","DateOrdered_line","DateOrdered_order",
        "DateShipped","DateShipped_line","DateShipped_order",
        "CreatedAt","UpdatedAt",
    ]
    date_values = pd.Series(pd.NaT, index=idx, dtype="datetime64[ns]")
    for c in date_candidates:
        src = _first_series(c)
        if src is not None:
            date_values = date_values.fillna(pd.to_datetime(src, errors="coerce"))
    date_values = _coerce_datetime_naive(date_values)
    out["EffectiveDate"] = date_values
    out["Date"] = date_values

    ship_candidates = ["DateShipped_line","DateShipped_order","DateShipped"]
    ship_vals = pd.Series(pd.NaT, index=idx, dtype="datetime64[ns]")
    for c in ship_candidates:
        src = _first_series(c)
        if src is not None:
            ship_vals = ship_vals.fillna(pd.to_datetime(src, errors="coerce"))
    out["ShipDate"] = _coerce_datetime_naive(ship_vals)

    # IDs/names
    _ensure_string("CustomerId","customer_id")
    _ensure_string("CustomerName","Name")
    _ensure_string("RegionName","Region")
    _ensure_string("ProductId","product_id")
    _ensure_string("ProductName","Product_Name")
    _ensure_string("SupplierId","supplier_id")
    _ensure_string("SupplierName","Supplier_Name")
    _ensure_string("OrderId","order_id")
    _ensure_string("OrderLineId","orderline_id")
    _ensure_string("ShipperName","Carrier")
    _ensure_string("Protein","Protein","ProteinType","ProteinName")
    _ensure_string("ProteinType","ProteinType","ProteinName","Protein","Category","ProductCategory")
    _ensure_string("ProteinName","ProteinName","ProteinType","Protein","Category","ProductCategory")
    _ensure_string("Category","Category","ProductCategory","ProteinType","ProteinName","Protein")
    _ensure_string("ProductCategory","ProductCategory","Category","ProteinType","ProteinName","Protein")

    # Shipping method label
    ship_method = pd.Series(pd.NA, index=idx, dtype="string")
    for candidate in ["ShippingMethodName","ShipMethod_Name","SM_ShippingMethodName","ShippingMethodLabel","ShippingMethodRequested"]:
        src = _first_series(candidate)
        if src is not None:
            s = src.astype("string").str.strip()
            ship_method = ship_method.fillna(s.where(s.str.len() > 0))
    shipper_series = out.get("ShipperName")
    if isinstance(shipper_series, pd.Series):
        ship_method = ship_method.fillna(shipper_series.astype("string").str.strip())
    out["ShippingMethodName"] = ship_method.astype("string")

    # Numerics (non-financial)
    _ensure_float("WeightLb","pack_weight_lb_sum")
    _ensure_float("ItemCount","pack_item_count_sum","QuantityShipped")
    _ensure_float("Price","Price")
    _ensure_float("CostPrice","CostPrice","unit_cost_effective", default=np.nan, fill=best_effort)
    _ensure_float("PricePerUnit","PricePerUnit","Price")
    _ensure_float("CostPerUnit","CostPerUnit","CostPrice", default=np.nan, fill=best_effort)

    # Financial columns: coerce only, do not recompute
    for column in ["Revenue","Cost","Profit","MarginPct","ROIPct"]:
        if column in out.columns:
            series = pd.to_numeric(out[column], errors="coerce")
            if best_effort:
                series = series.fillna(0.0)
            out[column] = series.round(4 if column.endswith("Pct") else 2)

    for column in [
        "WeightLb","ItemCount","QuantityOrdered","QuantityShipped","Price","PricePerUnit","BasePrice","ListPrice",
        "CostPrice","revenue_ordered","cost_ordered","gross_margin_ordered","revenue_shipped","cost_shipped",
        "gross_margin_shipped","LeadTime","pack_count","pack_piece_count_sum","pack_item_count_sum",
        "pack_weight_lb_sum","ShippingCharge","LinesTotalPrice","OrderTotalPrice"
    ]:
        if column in {"Revenue","Cost","Profit","MarginPct","ROIPct"}:
            continue
        if column in out.columns:
            series = pd.to_numeric(out[column], errors="coerce")
            if best_effort:
                series = series.fillna(0.0)
            out[column] = series.round(2)

    for column in [
        "CustomerId","CustomerName","RegionName","ProductId","ProductName","SupplierId","SupplierName",
        "ShipperName","ShippingMethodName","OrderId","OrderLineId","Protein","ProteinType","ProteinName",
        "Category","ProductCategory",
    ]:
        if column in out.columns:
            out[column] = out[column].astype("string")

    return out

# ─────────────────────────────────────────────────────────────────────────────
# Build fact (live, with user scoping)
# ─────────────────────────────────────────────────────────────────────────────

def determine_date_range(start: Optional[DateLike], end: Optional[DateLike]) -> Tuple[Optional[DateLike], Optional[DateLike]]:
    """Determine the effective start and end dates for a query."""
    env_start, env_end = _date_range_from_env()
    start = start or env_start
    end = end or env_end
    if start is None and end is None:
        return _default_date_window()
    if start is None:
        start, _ = _default_date_window()
    # If only start is provided, end should be None to query up to the latest data.
    return start, end

def merge_dimensions(
    t: Dict[str, pd.DataFrame],
    *,
    engine=None,
    return_stats: bool = False,
) -> pd.DataFrame | Tuple[pd.DataFrame, Dict[str, Any]]:
    """Merge all dimension tables into the fact table."""
    fact = t["OrderLines"].merge(t["Orders"], on="OrderId", how="left", suffixes=("_line", "_order"))
    _log_stage_audit("merge.orders_orderlines", fact)
    fact = fact.merge(t["Customers"], on="CustomerId", how="left")

    _collapse_column_variants(fact, "RegionId")
    _collapse_column_variants(fact, "RegionName")
    fact = _merge_region_dimension(fact, t["Regions"])
    
    right_region = fact.pop("RegionName_y") if "RegionName_y" in fact.columns else pd.Series(dtype='object')
    left_region = fact.pop("RegionName_x") if "RegionName_x" in fact.columns else pd.Series(dtype='object')
    base_region = fact.pop("RegionName") if "RegionName" in fact.columns else pd.Series(dtype='object')

    fact["RegionName"] = right_region.combine_first(left_region).combine_first(base_region).fillna("Unknown Region")

    _align_merge_key(fact, t["Products"], "ProductId")
    fact = fact.merge(t["Products"], on="ProductId", how="left")
    fact = fact.merge(t["Suppliers"], on="SupplierId", how="left")
    fact = fact.merge(t["UOM"].add_prefix("UOM_"), left_on="OrderedUnitsOfMeasureId", right_on="UOM_UnitOfMeasureId", how="left")
    fact = _resolve_shipping(fact, t["ShipMethods"], t["Shippers"])
    _log_stage_audit("merge.products", fact)
    
    aggregated_packs = _aggregate_packs(t["Packs"], t["PAD"])
    fact["OrderLineId"] = pd.to_numeric(fact["OrderLineId"], errors="coerce").astype("Int64")
    _align_merge_key(fact, aggregated_packs, "OrderLineId")
    log.info(
        "pack_merge.dtypes",
        extra={
            "fact_orderline_dtype": str(fact["OrderLineId"].dtype),
            "packs_orderline_dtype": str(aggregated_packs["OrderLineId"].dtype),
        },
    )
    pack_stats = _pack_merge_stats(fact, aggregated_packs, engine=engine, log_prefix="pack_merge")
    fact = fact.merge(aggregated_packs, on="OrderLineId", how="left")
    _log_stage_audit("merge.packs_joined", fact, pack_stats)
    
    _log_stage_audit("merge.products_joined", fact)
    fact_out = fact.reset_index(drop=True).copy()
    if return_stats:
        return fact_out, pack_stats
    return fact_out

def enrich_data(fact: pd.DataFrame, t: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Enrich the fact table with derived columns and additional data."""
    # Batches per order line
    if not t["Packs"].empty:
        pb = t["Packs"][["PackId", "BatchId", "PickedForOrderLine", "CommitedToOrderLine"]].copy()
        pb["OrderLineId"] = pb["PickedForOrderLine"].fillna(pb["CommitedToOrderLine"])
        _ensure_int(pb, "OrderLineId")
        pb = pb.merge(t["Batches"], on="BatchId", how="left")
        pb = pb.merge(t["BatchTypes"].add_prefix("BT_"), left_on="BatchType", right_on="BT_BatchTypeId", how="left")
        pb_grp = pb.groupby("OrderLineId", dropna=False, observed=True).agg(
            pack_batches_count=("BatchId", "nunique"),
            batch_tags_concat=("BatchTag", lambda s: ",".join(sorted({str(x) for x in s.dropna()}))),
        ).reset_index()
        fact = fact.merge(pb_grp, on="OrderLineId", how="left")

    # Enrich with rep names
    if not t["UsersNames"].empty:
        u = t["UsersNames"][["UserId", "FullName"]].copy()
        u["UserId"] = u["UserId"].astype("string").str.strip()
        for key, outcol in [("SalesRepId", "SalesRepName"), ("PrimarySalesRepId", "PrimarySalesRepName")]:
            if key in fact.columns:
                fact[key] = fact[key].astype("string").str.strip()
                fact = fact.merge(u.rename(columns={"UserId": key, "FullName": outcol}), on=key, how="left")

    return fact

def finalize_dataframe(
    fact: pd.DataFrame,
    *,
    products_df: Optional[pd.DataFrame] = None,
    packs_df: Optional[pd.DataFrame] = None,
    orderlines_df: Optional[pd.DataFrame] = None,
    best_effort: bool = False,
) -> pd.DataFrame:
    """Perform final cleaning, casting, and column selection."""
    for c in fact.select_dtypes(include=["datetime64[ns, UTC]"]).columns:
        fact[c] = _coerce_datetime_naive(fact[c])

    # Ensure a unified UpdatedAt column exists for downstream incremental logic
    updated_candidates = [c for c in ["UpdatedAt", "UpdatedAt_line", "UpdatedAt_order", "CreatedAt"] if c in fact.columns]
    if updated_candidates:
        merged_updated = pd.Series(pd.NaT, index=fact.index, dtype="datetime64[ns]")
        for col in updated_candidates:
            merged_updated = merged_updated.fillna(pd.to_datetime(fact[col], errors="coerce"))
        fact["UpdatedAt"] = _coerce_datetime_naive(merged_updated)

    fact_with_cost, cost_stats = derive_cost(
        fact, products_df=products_df, packs_df=packs_df, orderlines_df=orderlines_df
    )
    log.info("cost.derived", extra=cost_stats)
    _log_stage_audit("finalize.derived", fact_with_cost)
    if fact_with_cost.empty:
        final_empty = canonicalize_columns(fact_with_cost, best_effort=best_effort)
        _log_stage_audit("finalize.canonicalize", final_empty)
        return final_empty

    has_cost_col = "Cost" in fact_with_cost.columns
    cost_series = pd.to_numeric(fact_with_cost["Cost"], errors="coerce") if has_cost_col else pd.Series(
        np.nan, index=fact_with_cost.index, dtype="float64"
    )
    cost_missing_rate = float(cost_series.isna().mean()) if len(cost_series) else 0.0
    critical_numeric = ["Revenue", "Cost", "Profit", "MarginPct", "ROIPct"]
    missing_columns = [c for c in critical_numeric if c not in fact_with_cost.columns]

    if (not has_cost_col) or (len(cost_series) and cost_series.isna().all()):
        if best_effort:
            log.warning(
                "finalize.best_effort_fill",
                extra={
                    "stage": "finalize",
                    "missing_columns": ["Cost"],
                    "cost_missing_rate": cost_missing_rate,
                    "rows": len(fact_with_cost),
                },
            )
            fact_with_cost["Cost"] = cost_series.fillna(0.0)
        else:
            raise ValueError(f"Cost missing after enrichment/finalize; columns_present={list(fact_with_cost.columns)}")

    if best_effort and missing_columns:
        log.warning(
            "finalize.best_effort_fill",
            extra={
                "stage": "finalize",
                "missing_columns": missing_columns,
                "cost_missing_rate": cost_missing_rate,
                "rows": len(fact_with_cost),
            },
        )
        for col in missing_columns:
            fact_with_cost[col] = 0.0

    if best_effort:
        for col in critical_numeric:
            if col in fact_with_cost.columns:
                fact_with_cost[col] = pd.to_numeric(fact_with_cost[col], errors="coerce").fillna(0.0).round(
                    4 if col.endswith("Pct") else 2
                )

    # Deterministic EffectiveDate used for all downstream filtering/audits
    try:
        fact_with_cost["EffectiveDate"] = _effective_date_series(fact_with_cost)
    except Exception:
        fact_with_cost["EffectiveDate"] = pd.to_datetime(fact_with_cost.get("Date"), errors="coerce")

    fact_with_cost = _normalize_product_family_columns(fact_with_cost)

    # Preserve legacy casting for a few known columns
    num_cols = [
        "WeightLb","ItemCount","QuantityOrdered","QuantityShipped","Price","PricePerUnit","BasePrice","ListPrice",
        "CostPrice","revenue_ordered","cost_ordered","gross_margin_ordered","revenue_shipped","cost_shipped",
        "gross_margin_shipped","LeadTime","pack_count","pack_piece_count_sum","pack_item_count_sum",
        "pack_weight_lb_sum","ShippingCharge","LinesTotalPrice","OrderTotalPrice"
    ]
    str_cols = [
        "CustomerId","OrderId","ProductId","SupplierId","SalesRepId","PrimarySalesRepId","SKU","CustomerName",
        "RegionId","RegionName","Address1","City","Province","PostalCode","Phone","Email","ProductName","ProductDescription",
        "SupplierName","ShippingMethodName","ShipperName","OrderStatus","WarehouseId",
        "PoReference","Instructions","ProductionSheetId","UOM_UOMName","UOM_UOMShortName","batch_tags_concat",
        "production_location_ids","batch_type_names","SalesRepName","PrimarySalesRepName","Protein","ProteinType",
        "ProteinName","Category","ProductCategory"
    ]

    for c in num_cols:
        if c in fact_with_cost.columns:
            fact_with_cost[c] = pd.to_numeric(fact_with_cost[c], errors="coerce").round(2)

    for c in str_cols:
        if c in fact_with_cost.columns:
            fact_with_cost[c] = fact_with_cost[c].astype("string")

    final_df = canonicalize_columns(fact_with_cost, best_effort=best_effort)
    _log_stage_audit("finalize.canonicalize", final_df)
    _log_stage_audit("finalize.complete", final_df)
    return final_df

def build_fact(
    engine,
    start: Optional[DateLike] = None,
    end: Optional[DateLike] = None,
    statuses: Optional[List[str]] = None,
    *,
    is_super_user: bool = False,
    user_role: Optional[str] = None,
    user_sales_rep_id: Optional[str] = None,
    region_ids: Optional[List[str]] = None,
    sales_rep_override: Any = None,
    window_days: Optional[int] = None,  # reserved
    best_effort: bool = False,
    updated_after: Optional[DateLike] = None,
) -> pd.DataFrame:
    """Builds the denormalized 'fact' table from various source tables."""
    statuses = statuses or get_config().order_statuses
    if updated_after is None:
        start, end = determine_date_range(start, end)

    log.info("Building fact dataframe (start=%s end=%s statuses=%s, super=%s role=%s)", start, end, statuses, is_super_user, user_role)

    scope_sql, scope_params = _build_scope_sql(
        is_super_user=is_super_user,
        user_role=user_role,
        user_sales_rep_id=user_sales_rep_id,
        region_ids=region_ids,
        sales_rep_override=sales_rep_override,
    )

    # 1. Extraction
    tables = extract_all(
        engine,
        start,
        end,
        statuses,
        scope_sql=scope_sql,
        scope_params=scope_params,
        updated_after=updated_after,
    )
    if tables["OrderLines"].empty:
        log.warning("No order lines found for the given scope and date range.")
        return pd.DataFrame()
    _log_stage_audit("extract.order_lines", tables.get("OrderLines"))

    # 2. Merging
    merged = merge_dimensions(tables, engine=engine, return_stats=True)
    if isinstance(merged, tuple):
        fact_df, pack_stats = merged
    else:
        fact_df = merged
        pack_stats = {}
    if pack_stats:
        log.info("pack_merge.summary", extra=pack_stats)
    _log_stage_audit("merge_dimensions", fact_df)

    # 3. Enrichment
    enriched_df = enrich_data(fact_df, tables)
    _log_stage_audit("enrich_data", enriched_df)

    # 4. Finalization
    final_df = finalize_dataframe(
        enriched_df,
        products_df=tables.get("Products"),
        packs_df=tables.get("Packs"),
        orderlines_df=tables.get("OrderLines"),
        best_effort=best_effort,
    )

    log.info("Final fact shape: rows=%d cols=%d", len(final_df), len(final_df.columns))
    return final_df


def _audit_fact_metrics(
    order_lines: pd.DataFrame,
    *,
    packs: Optional[pd.DataFrame] = None,
    products: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    """
    Compute parity-friendly metrics (rows, product count, revenue, pack match rate)
    from an order-line frame with optional pack/product context.
    """
    metrics: Dict[str, Any] = {
        "rows": 0,
        "distinct_products": 0,
        "revenue": 0.0,
        "pack_match_rate": 0.0,
        "matched_lines": 0,
        "unmatched_lines": 0,
        "pack_rows": 0,
    }
    if order_lines is None or order_lines.empty:
        return metrics

    work = order_lines.copy()
    _ensure_int(work, "OrderLineId")
    metrics["rows"] = int(len(work))
    if "ProductId" in work.columns:
        metrics["distinct_products"] = int(pd.Series(work["ProductId"]).dropna().nunique())

    # Build aggregate pack view either from existing columns or raw packs
    if {"pack_weight_lb_sum", "pack_item_count_sum"}.issubset(work.columns):
        agg = work[["OrderLineId", "pack_weight_lb_sum", "pack_item_count_sum", "pack_count"]].copy()
    else:
        agg = pd.DataFrame(columns=["OrderLineId", "pack_weight_lb_sum", "pack_item_count_sum", "pack_count"])
        if packs is not None and isinstance(packs, pd.DataFrame) and not packs.empty:
            p = packs.copy()
            p["OrderLineId"] = p.get("PickedForOrderLine")
            _ensure_int(p, "OrderLineId")
            agg = (
                p.groupby("OrderLineId", dropna=False, observed=True)
                .agg(
                    pack_count=("PackId", "count"),
                    pack_item_count_sum=("ItemCount", "sum"),
                    pack_weight_lb_sum=("WeightLb", "sum"),
                )
                .reset_index()
            )

    merged = work.merge(agg, on="OrderLineId", how="left")

    qty_billed, price_series, cov, _ = _compute_qty_price_coverage(merged)
    revenue_series = price_series * qty_billed
    metrics["revenue"] = float(pd.to_numeric(revenue_series, errors="coerce").fillna(0.0).sum())
    metrics["matched_lines"] = int(cov.get("matched_lines", 0))
    metrics["unmatched_lines"] = max(0, int(cov.get("total_lines", metrics["rows"])) - metrics["matched_lines"])
    metrics["pack_match_rate"] = float(cov.get("pack_match_rate", 0.0))
    metrics["pack_row_rate"] = float(cov.get("pack_row_rate", 0.0))
    metrics["pack_rows"] = int(cov.get("pack_rows", 0))
    metrics["missing_qty_pct"] = float(cov.get("missing_qty_pct", 0.0))
    metrics["revenue_from_packs"] = float(cov.get("revenue_from_packs", 0.0))
    metrics["revenue_from_fallback_qty"] = float(cov.get("revenue_from_fallback_qty", 0.0))
    metrics["revenue_missing_qty"] = float(cov.get("revenue_missing_qty", 0.0))
    metrics["revenue_from_packs_pct"] = float(cov.get("revenue_from_packs_pct", 0.0))
    metrics["revenue_from_fallback_pct"] = float(cov.get("revenue_from_fallback_pct", 0.0))
    if "Cost" in merged.columns:
        cost_series = pd.to_numeric(merged["Cost"], errors="coerce")
        metrics["cost"] = float(cost_series.fillna(0.0).sum())
        metrics["cost_missing_rate"] = float(cost_series.isna().mean()) if len(cost_series) else 0.0
    else:
        metrics["cost_missing_rate"] = 1.0 if metrics["rows"] else 0.0

    return metrics


def _apply_window_filters(
    df: pd.DataFrame,
    start: Optional[DateLike],
    end: Optional[DateLike],
    statuses: Optional[List[str]],
) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    s, e_plus_1 = _half_open_dates(start, end)
    work = df.copy()
    dates = _effective_date_series(work)
    if not dates.empty:
        if s:
            try:
                s_ts = pd.to_datetime(s)
                work = work.loc[dates >= s_ts]
            except Exception:
                pass
        if e_plus_1:
            try:
                e_ts = pd.to_datetime(e_plus_1)
                work = work.loc[dates < e_ts]
            except Exception:
                pass
    if statuses and "OrderStatus" in work.columns:
        status_norm = {str(val).strip().lower() for val in statuses if str(val).strip()}
        work = work.loc[work["OrderStatus"].astype("string").str.lower().isin(status_norm)]
    return work


def _load_persisted_fact(columns: Optional[List[str]] = None) -> pd.DataFrame:
    try:
        from app.services import fact_store  # type: ignore

        return fact_store.get_sales_fact(columns=columns)
    except Exception as exc:
        try:
            return load_snapshot(columns=columns)
        except Exception:
            log.warning("persisted.load_failed", extra={"error": str(exc)})
            return pd.DataFrame()

# ─────────────────────────────────────────────────────────────────────────────
# Parquet (still available; disabled by default with DIRECT_SQL_ONLY=True)
# ─────────────────────────────────────────────────────────────────────────────

def _detect_parquet_engine() -> str:
    try:
        import pyarrow  # noqa: F401
        return "pyarrow"
    except ImportError:
        try:
            import fastparquet  # noqa: F401
            return "fastparquet"
        except ImportError:
            raise RuntimeError("No parquet engine available (pyarrow or fastparquet required)") from None

def _parquet_columns(path: Path, engine: str) -> set[str]:
    try:
        if engine == "pyarrow":
            import pyarrow.parquet as pq  # type: ignore
            return set(pq.ParquetFile(path.as_posix()).schema.names)
        if engine == "fastparquet":
            from fastparquet import ParquetFile  # type: ignore
            return set(ParquetFile(path.as_posix()).columns)
    except Exception as exc:
        log.warning("Failed to inspect parquet schema for %s: %s", path.as_posix(), exc)
    return set()


def _persist_signature_payload(df: pd.DataFrame, path: Path) -> Dict[str, Any]:
    metrics = _audit_fact_metrics(df)
    date_cols = [c for c in ("Date", "DateExpected", "DateOrdered", "DateShipped") if c in df.columns]
    date_min = date_max = None
    for col in date_cols:
        try:
            dates = pd.to_datetime(df[col], errors="coerce")
            if dates.notna().any():
                date_min = dates.min()
                date_max = dates.max()
                break
        except Exception:
            continue
    payload = {
        "path": path.as_posix(),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "rows": metrics.get("rows", 0),
        "distinct_products": metrics.get("distinct_products", 0),
        "revenue": metrics.get("revenue", 0.0),
        "pack_match_rate": metrics.get("pack_match_rate", 0.0),
        "date_min": date_min.isoformat() if hasattr(date_min, "isoformat") else None,
        "date_max": date_max.isoformat() if hasattr(date_max, "isoformat") else None,
    }
    return payload


def _write_persisted_signature(df: pd.DataFrame, path: Path) -> Path:
    sig_path = path.parent / "persisted_signature.json"
    try:
        payload = _persist_signature_payload(df, path)
        sig_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    except Exception as exc:
        log.warning("persisted.signature_write_failed", extra={"error": str(exc), "path": sig_path.as_posix()})
    return sig_path

def write_parquet_atomic(df: pd.DataFrame, path: str) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / f".{p.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
    engine = _detect_parquet_engine()
    try:
        _log_stage_audit("persist.write.prepare", df, {"path": p.as_posix()})
        log.info("Writing parquet (engine=%s) to %s", engine, tmp.as_posix())
        df.to_parquet(tmp.as_posix(), engine=engine, index=False)
        os.replace(tmp, p)
        log.info("Parquet write committed to %s", p.as_posix())
        # Read back for verification and signature
        try:
            reloaded = pd.read_parquet(p.as_posix(), engine=engine)
        except Exception as exc:
            log.warning("persisted.readback_failed", extra={"error": str(exc), "path": p.as_posix()})
            reloaded = pd.DataFrame()
        _log_stage_audit("persist.write.committed", df, {"path": p.as_posix()})
        _log_stage_audit("persist.readback", reloaded, {"path": p.as_posix()})
        try:
            in_metrics = _audit_fact_metrics(df)
            out_metrics = _audit_fact_metrics(reloaded)
            if in_metrics.get("rows") != out_metrics.get("rows") or abs(in_metrics.get("revenue", 0.0) - out_metrics.get("revenue", 0.0)) > 0.01:
                log.warning(
                    "persisted.mismatch",
                    extra={
                        "in_rows": in_metrics.get("rows"),
                        "out_rows": out_metrics.get("rows"),
                        "in_revenue": in_metrics.get("revenue"),
                        "out_revenue": out_metrics.get("revenue"),
                    },
                )
        except Exception:
            log.debug("persisted.metric_compare_failed", exc_info=True)
        _write_persisted_signature(reloaded if not reloaded.empty else df, p)
        return p.as_posix()
    except Exception as exc:
        log.error("Failed writing parquet with engine=%s: %s", engine, exc)
        if tmp.exists():
            try: tmp.unlink()
            except Exception: pass
        raise RuntimeError("Unable to write parquet") from exc

def _manifest_path(parquet_path: Optional[str] = None) -> Path:
    if parquet_path is None:
        parquet_path = get_config().parquet_path
    return Path(parquet_path).resolve().parent / "manifest.json"

def _format_date_bounds(df: pd.DataFrame) -> Tuple[Optional[str], Optional[str]]:
    if "Date" not in df.columns:
        return None, None
    series = pd.to_datetime(df["Date"], errors="coerce").dropna()
    if series.empty:
        return None, None
    # Use .dt.date to get python date objects robustly across pandas versions
    try:
        dates = series.dt.date
        if dates.empty:
            return None, None
        date_min = min(dates)
        date_max = max(dates)
        return (date_min.isoformat(), date_max.isoformat())
    except Exception:
        # Fallback: coerce via Timestamp for maximum compatibility
        try:
            min_ts = pd.to_datetime(series.min(), errors="coerce")
            max_ts = pd.to_datetime(series.max(), errors="coerce")
            if pd.isna(min_ts) or pd.isna(max_ts):
                return None, None
            return (min_ts.date().isoformat(), max_ts.date().isoformat())
        except Exception:
            return None, None

def _read_existing_parquet(path: Path, columns: Optional[List[str]] = None) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    engine = _detect_parquet_engine()
    read_kwargs: Dict[str, Any] = {}
    missing: List[str] = []

    if columns:
        available = _parquet_columns(path, engine)
        if available:
            present = [c for c in columns if c in available]
            missing = [c for c in columns if c not in available]
            if present:
                read_kwargs["columns"] = present
        else:
            read_kwargs["columns"] = list(columns)

    try:
        df = pd.read_parquet(path.as_posix(), engine=engine, **read_kwargs)
    except KeyError as exc:
        log.warning("Parquet projection failed (%s); retrying full read", exc)
        try:
            df = pd.read_parquet(path.as_posix(), engine=engine)
        except Exception as inner_exc:
            log.warning("Failed to read existing parquet at %s after retry: %s", path.as_posix(), inner_exc)
            return pd.DataFrame()
    except Exception as exc:
        log.warning("Failed to read existing parquet at %s: %s", path.as_posix(), exc)
        return pd.DataFrame()

    if columns:
        for col in missing:
            if col not in df.columns:
                df[col] = pd.NA
        ordered = [c for c in columns if c in df.columns]
        extra = [c for c in df.columns if c not in ordered]
        if ordered:
            df = df[ordered + extra]
    return df

def _write_manifest(df: pd.DataFrame, parquet_path: str) -> Path:
    rows = len(df)
    date_min, date_max = _format_date_bounds(df)
    now = datetime.now(timezone.utc)
    payload = {
        "version": str(int(now.timestamp() * 1000)),
        "built_at": now.isoformat(),
        "rows": rows,
        "date_min": date_min,
        "date_max": date_max,
        "parquet_path": str(Path(parquet_path).resolve()),
    }
    manifest_path = _manifest_path(parquet_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = manifest_path.parent / f".{manifest_path.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    os.replace(tmp, manifest_path)
    log.info("Updated manifest at %s", manifest_path.as_posix())
    _touch_refresh_marker(parquet_path, payload["built_at"])
    _broadcast_data_refresh(payload)
    return manifest_path

def _touch_refresh_marker(parquet_path: str, built_at: str) -> None:
    try:
        p = Path(parquet_path).resolve()
        marker = p.parent / ".last_refresh"
        marker.write_text(built_at, encoding="utf-8")
    except Exception:
        log.debug("Unable to update .last_refresh marker for %s", parquet_path, exc_info=True)

def _broadcast_data_refresh(payload: Dict[str, Any]) -> None:
    try:
        from app.services import event_bus  # type: ignore
    except Exception:
        return
    try:
        event_bus.publish(
            {
                "type": "data_refresh",
                "version": payload.get("version"),
                "built_at": payload.get("built_at"),
                "rows": payload.get("rows"),
            }
        )
    except Exception:
        log.debug("Failed to publish data_refresh event", exc_info=True)

def read_manifest(parquet_path: Optional[str] = None) -> Dict[str, Any]:
    manifest = _manifest_path(parquet_path)
    if not manifest.exists():
        return {}
    try:
        with manifest.open("r", encoding="utf-8") as fh:
            return cast(Dict[str, Any], json.load(fh))
    except Exception as exc:
        log.warning("Failed to read manifest at %s: %s", manifest.as_posix(), exc)
        return {}

def current_version(parquet_path: Optional[str] = None) -> str:
    manifest = read_manifest(parquet_path)
    return str(manifest.get("version", "0"))

def max_loaded_date(parquet_path: Optional[str] = None) -> Optional[pd.Timestamp]:
    manifest = read_manifest(parquet_path)
    date_max = manifest.get("date_max")
    if date_max:
        ts = pd.to_datetime(date_max, errors="coerce")
        if pd.notna(ts):
            return ts
    target = Path(parquet_path if parquet_path else get_config().parquet_path)
    if not target.exists():
        return None
    try:
        df = pd.read_parquet(target.as_posix(), columns=["Date"], engine=_detect_parquet_engine())
        if "Date" in df.columns:
            series = pd.to_datetime(df["Date"], errors="coerce").dropna()
            return series.max() if not series.empty else None
    except Exception as exc:
        log.warning("Failed to read parquet for max date at %s: %s", target.as_posix(), exc)
    return None

# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def get_dataframe(
    start: Optional[DateLike] = None,
    end: Optional[DateLike] = None,
    statuses: Optional[List[str]] = None,
    *,
    best_effort: bool = False,
    window_days: Optional[int] = None,
    updated_after: Optional[DateLike] = None,
) -> pd.DataFrame:
    """Unscoped live dataset (direct SQL)."""
    cfg = get_config()
    eng = create_mssql_engine(cfg)
    if statuses is None:
        statuses = cfg.order_statuses
    return build_fact(
        eng,
        start=start,
        end=end,
        statuses=statuses,
        best_effort=best_effort,
        window_days=window_days,
        updated_after=updated_after,
    )


def get_dataframe_for_user(
    *,
    user_sales_rep_id: Optional[str] = None,
    user_role: Optional[str] = None,
    region_ids: Optional[List[str]] = None,
    is_super_user: bool = False,
    start: Optional[DateLike] = None,
    end: Optional[DateLike] = None,
    statuses: Optional[List[str]] = None,
    sales_rep_override: Optional[str] = None,
    best_effort: bool = False,
    updated_after: Optional[DateLike] = None,
    **extra_filters: Any,
) -> pd.DataFrame:
    """
    Live dataset scoped at the SQL level (preferred for performance).
    Pass the current user's attributes from Flask (no rbac import needed).
    """
    user_obj = extra_filters.pop('user', None)
    if user_role is None and user_obj is not None:
        user_role = getattr(user_obj, 'role', None)

    if user_sales_rep_id is None and user_obj is not None:
        user_sales_rep_id = getattr(user_obj, 'sales_rep_id', None)

    region_scope = _normalize_scope_tokens(region_ids)
    if not region_scope and user_obj is not None:
        region_scope = _normalize_scope_tokens(getattr(user_obj, 'region_id', None))

    override_tokens = _normalize_scope_tokens(sales_rep_override)
    rep_tokens = override_tokens or _normalize_scope_tokens(user_sales_rep_id)
    if not rep_tokens and user_obj is not None:
        rep_tokens = _normalize_scope_tokens(getattr(user_obj, 'sales_rep_id', None))

    if user_sales_rep_id is None and rep_tokens:
        user_sales_rep_id = rep_tokens[0]

    loader_sales_override = sales_rep_override
    if not loader_sales_override and rep_tokens:
        loader_sales_override = rep_tokens[0] if len(rep_tokens) == 1 else ','.join(rep_tokens)

    role_norm = (user_role or '').strip().lower()
    if not is_super_user:
        allow_region_only = role_norm == 'sales_manager' and bool(region_scope)
        if not rep_tokens and not allow_region_only:
            return pd.DataFrame()

    cfg = get_config()
    eng = create_mssql_engine(cfg)
    if statuses is None:
        statuses = cfg.order_statuses
    df = build_fact(
        eng,
        start=start,
        end=end,
        statuses=statuses,
        is_super_user=is_super_user,
        user_role=user_role,
        user_sales_rep_id=user_sales_rep_id,
        region_ids=region_scope or None,
        sales_rep_override=loader_sales_override,
        best_effort=best_effort,
        updated_after=updated_after,
    )

    if not isinstance(df, pd.DataFrame) or df.empty:
        return df

    work = df.copy()

    def _norm_series(series: pd.Series) -> pd.Series:
        return series.astype('string').str.strip()

    scoped_tokens = rep_tokens
    if scoped_tokens:
        token_set = {tok.lower() for tok in scoped_tokens if tok}
        if token_set:
            mask = pd.Series(False, index=work.index)
            rep_cols = [
                col
                for col in work.columns
                if col.lower() in {
                    'salesrepid',
                    'sales_rep_id',
                    'primarysalesrepid',
                    'primary_sales_rep_id',
                    'userid',
                    'user_id',
                }
            ]
            for col in rep_cols:
                series = _norm_series(work[col]).str.lower()
                mask |= series.isin(token_set)
            work = work.loc[mask].copy()

    if region_scope and "RegionName" in work.columns:
        region_token_set = {tok.lower() for tok in region_scope if tok}
        if region_token_set:
            region_series = _norm_series(work["RegionName"]).str.lower()
            work = work.loc[region_series.isin(region_token_set)].copy()

    if extra_filters:
        log.debug('get_dataframe_for_user.unhandled_filters', extra={'keys': list(extra_filters.keys())})

    return work

def load_snapshot(columns: Optional[List[str]] = None, parquet_path: Optional[str] = None) -> pd.DataFrame:
    """Snapshot path is kept for compatibility; not used when DIRECT_SQL_ONLY=True."""
    path = Path(parquet_path if parquet_path else get_config().parquet_path)
    df = _read_existing_parquet(path, columns=columns)
    if df.empty:
        return df
    if columns:
        missing_cols = [c for c in columns if c not in df.columns]
        for c in missing_cols:
            df[c] = pd.NA
        return df.loc[:, [c for c in columns if c in df.columns]].copy()
    return df

def get_fact_df(from_cache: bool = True, parquet_path: Optional[str] = None) -> pd.DataFrame:
    """Kept for backward compatibility. In direct-sql mode, always queries live."""
    cfg = get_config()
    if cfg.direct_sql_only:
        return get_dataframe()
    if from_cache:
        df = load_snapshot(parquet_path=parquet_path)
        if not df.empty:
            return df
    return get_dataframe()

def _resolve_partition_date_col(df) -> str:
    """
    Pick the column the fact dataset is partitioned on.

    Production frames carry DateExpected, so it was hardcoded at every call
    site. Any frame without it - an older extract, or a caller that supplies
    only `Date` - blew up with "Missing date column for partitioning" instead
    of falling back. watermark_store already knows the candidate order; use it,
    and keep DateExpected as the preference.
    """
    from app.services import watermark_store

    try:
        if df is not None and "DateExpected" in getattr(df, "columns", []):
            return "DateExpected"
        chosen = watermark_store.choose_date_column(df) if df is not None else None
        return chosen or "DateExpected"
    except Exception:
        return "DateExpected"


def refresh_parquet(
    parquet_path: Optional[str] = None,
    force_full: bool = False,
    *,
    best_effort: bool = False,
    start_date: Optional[str] = None,
) -> str:
    """No-op in direct-sql mode; still supported if you flip DIRECT_SQL_ONLY=false."""
    from app.services import watermark_store
    from etl.partition_writer import upsert_dataset

    if get_config().direct_sql_only:
        log.info("DIRECT_SQL_ONLY=True → refresh_parquet skipped")
        return ""
    
    # Resolve the partitioned dataset path
    ds_path = watermark_store.resolve_dataset_path()
    p = Path(parquet_path if parquet_path else get_config().parquet_path)
    
    # Whether a full refresh is needed is a question about the *partitioned
    # dataset*, not about the single-file parquet this loader used to write.
    # The check used to read that legacy file, which the partitioned writer
    # never creates - so it was always empty, the full-refresh branch was taken
    # every time, and the incremental path below was unreachable. On a
    # production-sized fact that is the difference between a windowed refresh
    # and reloading history on every run.
    existing_manifest = watermark_store.read_manifest(ds_path) or {}
    try:
        existing_rows = int(existing_manifest.get("row_count") or 0)
    except (TypeError, ValueError):
        existing_rows = 0
    dataset_present = existing_rows > 0 and any(Path(ds_path).rglob("*.parquet"))
    if not dataset_present:
        # Fall back to the legacy single-file layout so a dataset that has not
        # been migrated yet still counts as existing data.
        dataset_present = not _read_existing_parquet(p).empty

    full_refresh_env = _get_bool("FULL_REFRESH", False)
    default_window = int(os.getenv("INCREMENTAL_WINDOW_DAYS", "7"))
    loader_kwargs = {"best_effort": best_effort} if best_effort else {}
    start_arg = start_date or _initial_start_date()

    if full_refresh_env or force_full or not dataset_present:
        df = get_dataframe(start=start_arg, end=None, **loader_kwargs)
        # Migrate to partitioned dataset
        manifest = upsert_dataset(
            df, 
            dataset_path=ds_path, 
            pk_col="OrderLineId",
            date_col=_resolve_partition_date_col(df),
        )
        return str(ds_path)

    manifest = existing_manifest or watermark_store.read_manifest(ds_path)
    # upsert_dataset writes max_date / max_dateexpected; nothing has ever
    # written "date_max". Reading only that key meant the high-water mark was
    # always missing and the branch below fell straight back to a full reload,
    # which is the other half of why the incremental path never ran.
    date_max = (
        manifest.get("max_date")
        or manifest.get("max_dateexpected")
        or manifest.get("date_max")
    )
    if not date_max:
        # Fallback to checking the monolithic file signature if partitioned manifest is missing
        old_sig_path = p.parent / "persisted_signature.json"
        if old_sig_path.exists():
            try:
                old_sig = json.loads(old_sig_path.read_text())
                date_max = old_sig.get("date_max")
            except Exception:
                pass
    
    if not date_max:
        df = get_dataframe(start=start_arg, end=None, **loader_kwargs)
        upsert_dataset(df, dataset_path=ds_path, pk_col="OrderLineId", date_col=_resolve_partition_date_col(df))
        return str(ds_path)

    last_ts = pd.to_datetime(date_max, errors="coerce")
    if pd.isna(last_ts):
        df = get_dataframe(start=start_arg, end=None, **loader_kwargs)
        upsert_dataset(df, dataset_path=ds_path, pk_col="OrderLineId", date_col=_resolve_partition_date_col(df))
        return str(ds_path)

    start_dt = (last_ts - pd.Timedelta(days=default_window)).date().isoformat()
    incr = get_dataframe(start=start_dt, end=None, statuses=None, window_days=default_window, **loader_kwargs)

    if incr.empty:
        log.info(f"Heartbeat: found {len(incr)} new/updated records in incremental window.")
        return str(ds_path)
    
    log.info(f"Heartbeat: found {len(incr)} new/updated records in incremental window.")
    upsert_dataset(
        incr,
        dataset_path=ds_path,
        pk_col="OrderLineId",
        date_col=_resolve_partition_date_col(incr),
        existing_manifest=manifest,
    )
    return str(ds_path)

# ─────────────────────────────────────────────────────────────────────────────
# Background refresher (kept for compatibility; off in direct mode)
# ─────────────────────────────────────────────────────────────────────────────

class _BackgroundRefresher:
    def __init__(self, parquet_path: Optional[str] = None):
        self.parquet_path = parquet_path
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _lockfile(self) -> Path:
        p = Path(self.parquet_path if self.parquet_path else get_config().parquet_path)
        return p.parent / ".refresh.lock"

    def _try_lock(self) -> Optional[int]:
        lock = self._lockfile()
        try:
            fd = os.open(lock.as_posix(), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            os.close(fd)
            return os.getpid()
        except FileExistsError:
            return None
        except Exception:
            return None

    def _unlock(self):
        lock = self._lockfile()
        try:
            if lock.exists():
                lock.unlink()
        except Exception:
            pass

    def _loop(self):
        base_minutes = _get_int("AUTO_REFRESH_MINUTES", 60)
        max_backoff = _get_int("AUTO_REFRESH_MAX_BACKOFF_MINUTES", 240)
        initial_delay = _get_int("AUTO_REFRESH_INITIAL_DELAY_MINUTES", 3)

        wait = max(0, initial_delay + int(random.uniform(0, base_minutes * 0.2)))
        self._stop.wait(wait)

        backoff = base_minutes
        while not self._stop.is_set():
            if get_config().direct_sql_only:
                log.info("[bg] direct-sql mode; refresher idle")
                self._stop.wait(base_minutes * 60)
                continue

            if self._try_lock() is not None:
                try:
                    log.info(f"Heartbeat: Checking for new data at {datetime.now()}...")
                    refresh_parquet(parquet_path=self.parquet_path, force_full=False)
                    log.info("[bg] refresh_parquet completed")
                    backoff = base_minutes
                except Exception as exc:
                    log.exception("[bg] refresh_parquet failed: %s", exc)
                    backoff = min(int(backoff * 2), max_backoff)
                finally:
                    self._unlock()
            else:
                log.info("[bg] another refresher holds the lock; skipping this cycle")

            jitter = int(random.uniform(0, base_minutes * 0.2))
            sleep_minutes = max(1, backoff + jitter)
            self._stop.wait(sleep_minutes * 60)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="DataLoaderRefresher", daemon=True)
        self._thread.start()
        log.info("Background refresher started (daemon thread)")

    def stop(self, timeout: Optional[float] = 5.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            log.info("Background refresher stopped")

_refresher: Optional[_BackgroundRefresher] = None

def start_background_refresher(parquet_path: Optional[str] = None):
    enabled = _get_bool("AUTO_REFRESH_ENABLED", False) or _get_bool("AUTO_REFRESH", False)
    if enabled and not get_config().direct_sql_only:
        global _refresher
        _refresher = _BackgroundRefresher(parquet_path=parquet_path)
        _refresher.start()

def stop_background_refresher():
    global _refresher
    if _refresher:
        _refresher.stop()
        _refresher = None


# ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?
# Audit helpers
# ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?ƒ"?

def audit_extract_layer(
    start: Optional[DateLike],
    end: Optional[DateLike],
    statuses: Optional[List[str]] = None,
    *,
    engine=None,
) -> Dict[str, Any]:
    cfg = get_config()
    statuses_list = _normalize_status_list(statuses, default=cfg.order_statuses)
    eng = engine or create_mssql_engine(cfg)
    truth = sql_truth(start, end, statuses_list, engine=eng)
    tables = extract_all(eng, start, end, statuses_list)
    extract_metrics = _audit_fact_metrics(tables["OrderLines"], packs=tables["Packs"], products=tables["Products"])
    extract_metrics.update(
        {
            "orders": int(len(tables["Orders"])),
            "order_lines": int(len(tables["OrderLines"])),
            "pack_rows": int(len(tables["Packs"])),
        }
    )
    return {
        "start": start,
        "end": end,
        "statuses": statuses_list,
        "sql_truth": truth,
        "extract": extract_metrics,
    }


def audit_enrich_layer(
    start: Optional[DateLike],
    end: Optional[DateLike],
    statuses: Optional[List[str]] = None,
    *,
    engine=None,
) -> Dict[str, Any]:
    cfg = get_config()
    statuses_list = _normalize_status_list(statuses, default=cfg.order_statuses)
    eng = engine or create_mssql_engine(cfg)
    truth = sql_truth(start, end, statuses_list, engine=eng)
    tables = extract_all(eng, start, end, statuses_list)
    merged = merge_dimensions(tables, engine=eng, return_stats=True)
    if isinstance(merged, tuple):
        fact_df, pack_stats = merged
    else:
        fact_df = merged
        pack_stats = {}
    enriched_df = enrich_data(fact_df, tables)
    metrics = _audit_fact_metrics(enriched_df, products=tables["Products"])
    metrics.update(
        {
            "orders": int(len(tables["Orders"])),
            "order_lines": int(len(enriched_df)),
            "pack_rows": pack_stats.get("pack_rows", metrics.get("pack_rows", 0)),
        }
    )
    if pack_stats:
        metrics.setdefault("pack_match_rate", pack_stats.get("pack_match_rate", metrics.get("pack_match_rate", 0.0)))
        metrics.update({k: v for k, v in pack_stats.items() if k not in metrics})
    return {
        "start": start,
        "end": end,
        "statuses": statuses_list,
        "sql_truth": truth,
        "enriched": metrics,
    }


def audit_cost_window(
    start: Optional[DateLike],
    end: Optional[DateLike],
    statuses: Optional[List[str]] = None,
    *,
    engine=None,
    best_effort: bool = False,
) -> Dict[str, Any]:
    cfg = get_config()
    statuses_list = _normalize_status_list(statuses, default=cfg.order_statuses)
    eng = engine or create_mssql_engine(cfg)
    truth = sql_truth(start, end, statuses_list, engine=eng)
    tables = extract_all(eng, start, end, statuses_list)
    merged = merge_dimensions(tables, engine=eng, return_stats=True)
    if isinstance(merged, tuple):
        fact_df, pack_stats = merged
    else:
        fact_df = merged
        pack_stats = {}
    enriched_df = enrich_data(fact_df, tables)
    final_df = finalize_dataframe(
        enriched_df,
        products_df=tables.get("Products"),
        packs_df=tables.get("Packs"),
        orderlines_df=tables.get("OrderLines"),
        best_effort=best_effort,
    )
    extract_metrics = _audit_fact_metrics(tables["OrderLines"], packs=tables["Packs"], products=tables["Products"])
    enriched_metrics = _audit_fact_metrics(enriched_df, products=tables["Products"], packs=tables["Packs"])
    final_metrics = _audit_fact_metrics(final_df, products=tables["Products"], packs=tables["Packs"])
    if pack_stats:
        enriched_metrics.setdefault("pack_match_rate", pack_stats.get("pack_match_rate", enriched_metrics.get("pack_match_rate", 0.0)))
        enriched_metrics.update({k: v for k, v in pack_stats.items() if k not in enriched_metrics})
        final_metrics.setdefault("pack_match_rate", pack_stats.get("pack_match_rate", final_metrics.get("pack_match_rate", 0.0)))
        final_metrics.update({k: v for k, v in pack_stats.items() if k not in final_metrics})
    coverage = _cost_coverage_details(final_df, limit=25)
    loader_cost_sum = float(pd.to_numeric(final_df.get("Cost"), errors="coerce").fillna(0.0).sum()) if not final_df.empty else 0.0
    loader_revenue_sum = float(pd.to_numeric(final_df.get("Revenue"), errors="coerce").fillna(0.0).sum()) if not final_df.empty else 0.0
    sql_cost = float(truth.get("cost", 0.0) or 0.0)
    cost_delta = loader_cost_sum - sql_cost
    cost_delta_pct = float((cost_delta / sql_cost) * 100.0) if sql_cost else None
    return {
        "start": start,
        "end": end,
        "statuses": statuses_list,
        "sql_truth": truth,
        "extract": extract_metrics,
        "enriched": enriched_metrics,
        "final": final_metrics,
        "cost_checks": {
            "loader_cost_sum": loader_cost_sum,
            "loader_revenue_sum": loader_revenue_sum,
            "sql_cost_sum": sql_cost,
            "cost_delta": cost_delta,
            "cost_delta_pct": cost_delta_pct,
            "coverage": coverage,
        },
    }


def validate_window(
    start: Optional[DateLike],
    end: Optional[DateLike],
    statuses: Optional[List[str]] = None,
    *,
    engine=None,
    best_effort: bool = False,
) -> Dict[str, Any]:
    """Validate a window for cost/revenue coverage and sample missing-cost rows."""
    cfg = get_config()
    statuses_list = _normalize_status_list(statuses, default=cfg.order_statuses)
    eng = engine or create_mssql_engine(cfg)
    df = build_fact(eng, start=start, end=end, statuses=statuses_list, best_effort=best_effort)
    metrics = _audit_fact_metrics(df)
    coverage = _cost_coverage_details(df, limit=25)
    cost_series = pd.to_numeric(df.get("Cost"), errors="coerce") if "Cost" in df.columns else pd.Series(dtype="float64")
    revenue_series = pd.to_numeric(df.get("Revenue"), errors="coerce") if "Revenue" in df.columns else pd.Series(dtype="float64")
    samples_df = pd.DataFrame()
    if not df.empty and not revenue_series.empty:
        mask = revenue_series.notna() & revenue_series.ne(0) & (cost_series.isna() | (cost_series == 0))
        samples_df = df.loc[mask].head(50).copy()
    sample_cols = [c for c in ["OrderLineId", "OrderId", "Date", "ProductId", "ProductName", "Revenue", "Cost", "Price", "CostPrice", "pack_cost_total"] if c in samples_df.columns]
    samples = samples_df[sample_cols].to_dict(orient="records") if not samples_df.empty else []
    cost_missing_rate = float(coverage.get("missing_pct", float(cost_series.isna().mean() if len(cost_series) else 1.0)))
    cost_zero_rate = float(coverage.get("zero_pct", 0.0))
    revenue_sum = float(revenue_series.dropna().sum()) if len(revenue_series) else 0.0
    cost_sum = float(cost_series.dropna().sum()) if len(cost_series) else 0.0
    payload = {
        "start": start,
        "end": end,
        "statuses": statuses_list,
        "rows": int(len(df)),
        "revenue_sum": revenue_sum,
        "cost_sum": cost_sum,
        "cost_missing_rate": cost_missing_rate,
        "cost_zero_rate": cost_zero_rate,
        "pack_match_rate": metrics.get("pack_match_rate", 0.0),
        "pack_row_rate": metrics.get("pack_row_rate", 0.0),
        "revenue_from_packs": metrics.get("revenue_from_packs", 0.0),
        "revenue_from_fallback_qty": metrics.get("revenue_from_fallback_qty", 0.0),
        "revenue_missing_qty": metrics.get("revenue_missing_qty", 0.0),
        "missing_qty_pct": metrics.get("missing_qty_pct", 0.0),
        "samples": samples,
        "top_missing_by_revenue": coverage.get("top_missing_by_revenue", []),
        "best_effort": best_effort,
    }
    _log_stage_audit("validate_window", df)
    return payload


def audit_persisted_layer(
    start: Optional[DateLike],
    end: Optional[DateLike],
    statuses: Optional[List[str]] = None,
) -> Dict[str, Any]:
    cfg = get_config()
    statuses_list = _normalize_status_list(statuses, default=cfg.order_statuses)
    truth = sql_truth(start, end, statuses_list)
    df = _load_persisted_fact()
    df = _apply_window_filters(df, start, end, statuses_list)
    metrics = _audit_fact_metrics(df)
    metrics.update(
        {
            "orders": int(df["OrderId"].dropna().nunique()) if "OrderId" in df.columns else 0,
            "order_lines": int(len(df)),
            "pack_rows": metrics.get("pack_rows", 0),
        }
    )
    return {
        "start": start,
        "end": end,
        "statuses": statuses_list,
        "sql_truth": truth,
        "persisted": metrics,
    }


def audit_api_layer(
    base_url: str,
    start: Optional[DateLike],
    end: Optional[DateLike],
    statuses: Optional[List[str]] = None,
    *,
    as_admin: bool = False,
    username: Optional[str] = None,
    password: Optional[str] = None,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    if not base_url:
        raise ValueError("base_url is required for audit-api")
    statuses_list = _normalize_status_list(statuses, default=get_config().order_statuses)
    params = {
        "start": start,
        "end": end,
        "statuses": ",".join(statuses_list),
    }
    url = base_url.rstrip("/") + "/api/_admin/audit/window"
    headers: Dict[str, str] = {}
    auth = None
    if as_admin and username and password:
        auth = (username, password)
    if token:
        headers["X-Admin-Token"] = token

    resp = requests.get(url, params=params, headers=headers, auth=auth, timeout=_get_int("API_AUDIT_TIMEOUT", 30))
    try:
        resp.raise_for_status()
    except Exception as exc:
        log.error("audit_api.request_failed", extra={"url": url, "status_code": resp.status_code, "error": str(exc)})
        raise
    payload = resp.json()
    payload.setdefault("start", start)
    payload.setdefault("end", end)
    payload.setdefault("statuses", statuses_list)
    return payload


def audit_parity(
    start: Optional[DateLike],
    end: Optional[DateLike],
    *,
    statuses: Optional[List[str]] = None,
    base_url: Optional[str] = None,
    tolerance_pct: float = 0.5,
    as_admin: bool = False,
    username: Optional[str] = None,
    password: Optional[str] = None,
    token: Optional[str] = None,
    best_effort: bool = False,
) -> Dict[str, Any]:
    """Compare SQL truth vs loader vs persisted vs API within tolerance."""
    cfg = get_config()
    statuses_list = _normalize_status_list(statuses, default=cfg.order_statuses)
    eng = create_mssql_engine(cfg)

    truth = sql_truth(start, end, statuses_list, engine=eng)

    loader_df = build_fact(
        eng,
        start=start,
        end=end,
        statuses=statuses_list,
        best_effort=best_effort,
    )
    loader_metrics = _stage_metrics(loader_df)

    persisted_df = _load_persisted_fact()
    persisted_df = _apply_window_filters(persisted_df, start, end, statuses_list)
    persisted_metrics = _stage_metrics(persisted_df)

    api_payload: Optional[Dict[str, Any]] = None
    api_metrics: Optional[Dict[str, Any]] = None
    if base_url:
        try:
            api_payload = audit_api_layer(
                base_url,
                start,
                end,
                statuses=statuses_list,
                as_admin=as_admin,
                username=username,
                password=password,
                token=token,
            )
            api_metrics = _stage_metrics(pd.DataFrame(api_payload.get("rows", []))) if isinstance(api_payload, dict) else None
        except Exception as exc:  # pragma: no cover - diagnostic only
            log.warning("audit_parity.api_failed", extra={"error": str(exc), "base_url": base_url})

    def _summary(m: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "rows": int(m.get("rows", 0) or 0),
            "orders": int(m.get("order_ids", 0) or 0),
            "order_lines": int(m.get("orderline_ids", m.get("rows", 0)) or 0),
            "products": int(m.get("product_ids", 0) or 0),
            "revenue": float(m.get("rev_sum", 0.0) or 0.0),
            "cost": float(m.get("cost_sum", 0.0) or 0.0),
            "pack_match_rate": float(m.get("pack_match_rate", 0.0) or 0.0),
            "pack_row_rate": float(m.get("pack_row_rate", 0.0) or 0.0),
            "revenue_from_packs_pct": float(m.get("revenue_from_packs_pct", 0.0) or 0.0),
            "revenue_from_fallback_pct": float(m.get("revenue_from_fallback_pct", 0.0) or 0.0),
            "missing_qty_pct": float(m.get("missing_qty_pct", 0.0) or 0.0),
        }

    def _pct_delta(observed: float, expected: float) -> float:
        try:
            if expected == 0:
                return 0.0 if observed == 0 else 100.0
            return float(abs(observed - expected) / abs(expected) * 100.0)
        except Exception:
            return 100.0

    loader_summary = _summary(loader_metrics)
    persisted_summary = _summary(persisted_metrics)
    api_summary = _summary(api_metrics or {}) if api_metrics else None
    truth_summary = {
        "rows": int(truth.get("order_lines", 0) or 0),
        "orders": int(truth.get("orders", 0) or 0),
        "order_lines": int(truth.get("order_lines", 0) or 0),
        "products": int(truth.get("products", 0) or 0),
        "revenue": float(truth.get("revenue_with_fallback", truth.get("revenue", 0.0)) or 0.0),
        "revenue_with_fallback": float(truth.get("revenue_with_fallback", truth.get("revenue", 0.0)) or 0.0),
        "revenue_pack_only": float(truth.get("revenue_pack_only", 0.0) or 0.0),
        "cost": float(truth.get("cost", 0.0) or 0.0),
        "pack_match_rate": float(truth.get("pack_match_rate", 0.0) or 0.0),
        "pack_row_rate": float(truth.get("pack_row_rate", 0.0) or 0.0),
    }

    revenue_delta_pct = _pct_delta(loader_summary["revenue"], truth_summary["revenue_with_fallback"])
    revenue_delta_pack_only_pct = _pct_delta(loader_summary["revenue"], truth_summary["revenue_pack_only"])

    comparisons = {
        "loader_rows_pct_diff": _pct_delta(loader_summary["rows"], truth_summary["order_lines"]),
        "loader_products_pct_diff": _pct_delta(loader_summary["products"], truth_summary["products"]),
        "loader_revenue_vs_fallback_pct_diff": revenue_delta_pct,
        "loader_revenue_vs_pack_only_pct_diff": revenue_delta_pack_only_pct,
        "loader_cost_pct_diff": _pct_delta(loader_summary["cost"], truth_summary["cost"]),
        "persisted_rows_pct_diff": _pct_delta(persisted_summary["rows"], truth_summary["order_lines"]),
        "persisted_products_pct_diff": _pct_delta(persisted_summary["products"], truth_summary["products"]),
        "persisted_revenue_pct_diff": _pct_delta(persisted_summary["revenue"], truth_summary["revenue"]),
        "persisted_cost_pct_diff": _pct_delta(persisted_summary["cost"], truth_summary["cost"]),
    }
    if api_summary:
        comparisons["api_rows_pct_diff"] = _pct_delta(api_summary["rows"], truth_summary["order_lines"])
        comparisons["api_revenue_pct_diff"] = _pct_delta(api_summary["revenue"], truth_summary["revenue"])

    tolerance = float(tolerance_pct)
    ok = revenue_delta_pct <= tolerance

    payload: Dict[str, Any] = {
        "start": start,
        "end": end,
        "statuses": statuses_list,
        "tolerance_pct": tolerance,
        "ok": ok,
        "truth": truth_summary,
        "loader": loader_summary,
        "persisted": persisted_summary,
        "comparisons_pct": {k: round(v, 4) for k, v in comparisons.items()},
        "revenue_delta_vs_fallback_pct": round(revenue_delta_pct, 4),
        "revenue_delta_vs_pack_only_pct": round(revenue_delta_pack_only_pct, 4),
    }
    payload["coverage"] = {
        "truth": {
            "pack_match_rate": truth_summary.get("pack_match_rate"),
            "pack_row_rate": truth_summary.get("pack_row_rate"),
        },
        "loader": {
            "pack_match_rate": loader_summary.get("pack_match_rate"),
            "pack_row_rate": loader_summary.get("pack_row_rate"),
            "revenue_from_packs_pct": loader_summary.get("revenue_from_packs_pct"),
            "revenue_from_fallback_pct": loader_summary.get("revenue_from_fallback_pct"),
            "missing_qty_pct": loader_summary.get("missing_qty_pct"),
        },
    }
    if api_payload is not None:
        payload["api_payload"] = api_payload
    if api_summary is not None:
        payload["api"] = api_summary
    return payload

# ─────────────────────────────────────────────────────────────────────────────
# CLI (unchanged semantics; now live by default)
# ─────────────────────────────────────────────────────────────────────────────

def _add_common_date_args(p: argparse.ArgumentParser):
    p.add_argument("--start", help="YYYY-MM-DD (inclusive)")
    p.add_argument("--end", help="YYYY-MM-DD (inclusive)")
    p.add_argument("--statuses", help="Comma list of order statuses to include (overrides ORDER_STATUSES)")
    p.add_argument("--parquet", help="Parquet snapshot path (overrides PARQUET_PATH)")

def _resolve_statuses(arg: Optional[str]) -> Optional[List[str]]:
    if arg is None:
        return None
    return [x.strip() for x in arg.split(",") if x.strip()]

def _cmd_audit_sql(args: argparse.Namespace):
    cfg = get_config()
    statuses = _resolve_statuses(args.statuses) or cfg.order_statuses
    payload = sql_truth(args.start, args.end, status=statuses)
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))

def _cmd_audit_cost(args: argparse.Namespace):
    cfg = get_config()
    statuses = _resolve_statuses(args.statuses) or cfg.order_statuses
    eng = create_mssql_engine(cfg)
    payload = audit_cost_window(
        args.start,
        args.end,
        statuses=statuses,
        engine=eng,
        best_effort=bool(getattr(args, "best_effort", False)),
    )
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))

def _cmd_validate_window(args: argparse.Namespace):
    cfg = get_config()
    statuses = _resolve_statuses(args.statuses) or cfg.order_statuses
    eng = create_mssql_engine(cfg)
    payload = validate_window(
        args.start,
        args.end,
        statuses=statuses,
        engine=eng,
        best_effort=bool(getattr(args, "best_effort", False)),
    )
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))

def _cmd_audit_extract(args: argparse.Namespace):
    cfg = get_config()
    statuses = _resolve_statuses(args.statuses) or cfg.order_statuses
    eng = create_mssql_engine(cfg)
    payload = audit_extract_layer(args.start, args.end, statuses=statuses, engine=eng)
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))

def _cmd_audit_enrich(args: argparse.Namespace):
    cfg = get_config()
    statuses = _resolve_statuses(args.statuses) or cfg.order_statuses
    eng = create_mssql_engine(cfg)
    payload = audit_enrich_layer(args.start, args.end, statuses=statuses, engine=eng)
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))

def _cmd_audit_persisted(args: argparse.Namespace):
    cfg = get_config()
    statuses = _resolve_statuses(args.statuses) or cfg.order_statuses
    payload = audit_persisted_layer(args.start, args.end, statuses=statuses)
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))

def _cmd_audit_api(args: argparse.Namespace):
    base_url = args.base_url or ""
    if not base_url:
        raise SystemExit("audit-api requires --base-url")
    payload = audit_api_layer(
        base_url,
        args.start,
        args.end,
        statuses=_resolve_statuses(args.statuses),
        as_admin=bool(args.as_admin),
        username=args.username or os.getenv("ADMIN_USERNAME"),
        password=args.password or os.getenv("ADMIN_PASSWORD"),
        token=args.admin_token or os.getenv("ADMIN_API_TOKEN"),
    )
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))

def _cmd_audit_parity(args: argparse.Namespace):
    payload = audit_parity(
        args.start,
        args.end,
        statuses=_resolve_statuses(args.statuses),
        base_url=args.base_url,
        tolerance_pct=float(args.tolerance_pct),
        as_admin=bool(args.as_admin),
        username=args.username,
        password=args.password,
        token=args.admin_token,
        best_effort=bool(getattr(args, "best_effort", False)),
    )
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0 if payload.get("ok", False) else 1

def _cmd_refresh(args: argparse.Namespace):
    if args.parquet:
        os.environ["PARQUET_PATH"] = args.parquet
    path = refresh_parquet(parquet_path=args.parquet, force_full=bool(args.full), best_effort=bool(getattr(args, "best_effort", False)))
    print(path)

def _cmd_initial(args: argparse.Namespace):
    if args.parquet:
        os.environ["PARQUET_PATH"] = args.parquet
    _cfg = get_config()
    start_arg = args.start or _initial_start_date()
    path = refresh_parquet(
        parquet_path=args.parquet,
        force_full=True,
        best_effort=bool(getattr(args, "best_effort", False)),
        start_date=start_arg,
    )
    print(path)
    return path

def _cmd_show_manifest(args: argparse.Namespace):
    if args.parquet:
        os.environ["PARQUET_PATH"] = args.parquet
    m = read_manifest(args.parquet)
    print(json.dumps(m, indent=2, sort_keys=True))

def _cmd_build_once(args: argparse.Namespace):
    if args.parquet:
        os.environ["PARQUET_PATH"] = args.parquet
    cfg = get_config()
    if args.statuses:
        cfg.order_statuses = _resolve_statuses(args.statuses)
    eng = create_mssql_engine(cfg)
    start_arg = args.start or _initial_start_date()
    df = build_fact(
        eng,
        start=start_arg,
        end=args.end,
        statuses=cfg.order_statuses,
        best_effort=bool(getattr(args, "best_effort", False)),
    )
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        if args.out.lower().endswith(".parquet"):
            df.to_parquet(args.out, index=False)
        else:
            df.to_csv(args.out, index=False)
        print(args.out)
    else:
        print(df.head(3).to_string(index=False))

def _cmd_run_refresher(args: argparse.Namespace):
    if args.parquet:
        os.environ["PARQUET_PATH"] = args.parquet

    stop_event = threading.Event()

    def _handle_sig(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)
    start_background_refresher(parquet_path=args.parquet)
    print("Refresher running. Press Ctrl+C to stop.")
    while not stop_event.is_set():
        time.sleep(0.5)
    stop_background_refresher()

def _make_cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="data_loader", description="Wholesale Analytics analytics loader (direct SQL with optional snapshot)"
    )
    sub = p.add_subparsers(dest="command")

    ps = sub.add_parser(
        "refresh",
        help="Incremental refresh (or full with --full); no-op if DIRECT_SQL_ONLY=True",
    )
    ps.add_argument("--full", action="store_true", help="Force full rebuild")
    ps.add_argument("--parquet", help="Snapshot path (default PARQUET_PATH)")
    ps.add_argument("--statuses", help="Comma list of order statuses (default=env or packed)")
    ps.add_argument("--best-effort", action="store_true", help="Allow filling missing numeric columns with 0.0 (non-strict)")
    ps.set_defaults(func=_cmd_refresh)

    pi = sub.add_parser(
        "initial-load", 
        help="Full backfill to snapshot; no-op if DIRECT_SQL_ONLY=True",
    )
    _add_common_date_args(pi)
    pi.add_argument("--best-effort", action="store_true", help="Allow filling missing numeric columns with 0.0 (non-strict)")
    pi.set_defaults(func=_cmd_initial)

    pm = sub.add_parser("show-manifest", help="Print manifest JSON (if snapshots used)")
    pm.add_argument("--parquet", help="Snapshot path (default PARQUET_PATH)")
    pm.set_defaults(func=_cmd_show_manifest)

    pb = sub.add_parser("build-once", help="One-off build (prints head or writes --out)")
    _add_common_date_args(pb)
    pb.add_argument("--out", help="Write to .csv or .parquet instead of printing")
    pb.add_argument("--best-effort", action="store_true", help="Allow filling missing numeric columns with 0.0 (non-strict)")
    pb.set_defaults(func=_cmd_build_once)

    psql = sub.add_parser("audit-sql", help="Audit SQL truth for a date window")
    _add_common_date_args(psql)
    psql.set_defaults(func=_cmd_audit_sql)

    ppar = sub.add_parser("audit-parity", help="Compare SQL vs loader vs persisted (and API if provided)")
    _add_common_date_args(ppar)
    ppar.add_argument("--base-url", help="Base URL for API parity (optional)")
    ppar.add_argument("--tolerance-pct", help="Allowed pct delta (default 0.5)", default=0.5)
    ppar.add_argument("--as-admin", action="store_true", help="Send API parity call as admin")
    ppar.add_argument("--admin-token", help="X-Admin-Token for API parity")
    ppar.add_argument("--username", help="Basic auth username for API parity")
    ppar.add_argument("--password", help="Basic auth password for API parity")
    ppar.add_argument("--best-effort", action="store_true", help="Allow filling missing numeric columns with 0.0 (non-strict)")
    ppar.set_defaults(func=_cmd_audit_parity)

    pcost = sub.add_parser("audit-cost", help="Audit cost/revenue derivation for a date window")
    _add_common_date_args(pcost)
    pcost.add_argument("--best-effort", action="store_true", help="Allow filling missing numeric columns with 0.0 (non-strict)")
    pcost.set_defaults(func=_cmd_audit_cost)

    pval = sub.add_parser("validate-window", help="Validate fact window with cost coverage and samples")
    _add_common_date_args(pval)
    pval.add_argument("--best-effort", action="store_true", help="Allow filling missing numeric columns with 0.0 (non-strict)")
    pval.set_defaults(func=_cmd_validate_window)

    pext = sub.add_parser("audit-extract", help="Audit extract layer vs SQL truth")
    _add_common_date_args(pext)
    pext.set_defaults(func=_cmd_audit_extract)

    penr = sub.add_parser("audit-enrich", help="Audit enriched frame vs SQL truth")
    _add_common_date_args(penr)
    penr.set_defaults(func=_cmd_audit_enrich)

    ppers = sub.add_parser("audit-persisted", help="Audit persisted parquet vs SQL truth")
    _add_common_date_args(ppers)
    ppers.set_defaults(func=_cmd_audit_persisted)

    papi = sub.add_parser("audit-api", help="Audit overview API response (admin mode)")
    _add_common_date_args(papi)
    papi.add_argument("--base-url", required=True, help="Base URL for the API (e.g., https://host)")
    papi.add_argument("--as-admin", action="store_true", help="Send admin credentials for bypassing scope")
    papi.add_argument("--username", help="Admin username (default ADMIN_USERNAME)")
    papi.add_argument("--password", help="Admin password (default ADMIN_PASSWORD)")
    papi.add_argument("--admin-token", help="Admin API token header (default ADMIN_API_TOKEN)")
    papi.set_defaults(func=_cmd_audit_api)

    pr = sub.add_parser("run-refresher", help="Run background refresher (disabled if DIRECT_SQL_ONLY=True)")
    pr.add_argument("--parquet", help="Snapshot path (default PARQUET_PATH)")
    pr.set_defaults(func=_cmd_run_refresher)

    return p

def main(argv: Optional[List[str]] = None):
    cli = _make_cli()
    args = cli.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        log.info("No command supplied; defaulting to 'initial-load'.")
        args = cli.parse_args(["initial-load"])
        func = getattr(args, "func", None)
        if func is None:
            cli.print_help()
            return 2
    if getattr(args, "statuses", None):
        os.environ["ORDER_STATUSES"] = args.statuses
    try:
        return func(args)  # type: ignore[attr-defined]
    except Exception as e:
        log.exception("Command failed: %s", e)
        return 1

if __name__ == "__main__":
    sys.exit(main())

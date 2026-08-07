from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import math
from typing import Any, Dict, Iterable, List, Tuple

import numpy as np
import pandas as pd

from flask import current_app
from app.core import access_policy
from app.services import fact_store, products_bundle
from app.services import filters_service
from app.services import margin_rules
from app.services.bundle_cache import cached_bundle
from app.services import presentation


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        parsed = float(value)
        if math.isnan(parsed):
            return None
        return parsed
    except Exception:
        return None


def _clean_num(value: Any, default: float = 0.0) -> float:
    parsed = _safe_float(value)
    return parsed if parsed is not None else float(default)


def _safe_div(numerator: Any, denominator: Any) -> float | None:
    num = _safe_float(numerator)
    den = _safe_float(denominator)
    if num is None or den is None or abs(den) <= 1e-12:
        return None
    return float(num / den)


def _pct_delta(current: Any, prior: Any) -> float | None:
    cur = _safe_float(current)
    prev = _safe_float(prior)
    if cur is None or prev is None or abs(prev) <= 1e-12:
        return None
    return float((cur - prev) / prev * 100.0)


def _clamp(value: Any, lower: float = 0.0, upper: float = 100.0) -> float:
    val = _clean_num(value)
    return float(min(max(val, lower), upper))


def _top_share_metrics(values: pd.Series | Iterable[Any]) -> Dict[str, float]:
    series = pd.to_numeric(pd.Series(list(values)), errors="coerce").fillna(0.0)
    series = series.clip(lower=0.0).sort_values(ascending=False).reset_index(drop=True)
    total = float(series.sum() or 0.0)
    if total <= 0.0:
        return {"top1_share_pct": 0.0, "top5_share_pct": 0.0, "top10_share_pct": 0.0, "hhi": 0.0}
    shares = series / total
    return {
        "top1_share_pct": float(shares.head(1).sum() * 100.0),
        "top5_share_pct": float(shares.head(5).sum() * 100.0),
        "top10_share_pct": float(shares.head(10).sum() * 100.0),
        "hhi": float(np.sum((shares.to_numpy(dtype=float) * 100.0) ** 2)),
    }


def _quantile_triplet(series: pd.Series) -> Tuple[float | None, float | None, float | None]:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return None, None, None
    values = clean.to_numpy(dtype=float)
    return (
        float(np.quantile(values, 0.10)),
        float(np.quantile(values, 0.50)),
        float(np.quantile(values, 0.90)),
    )


def _label_tone(score: Any, *, reverse: bool = False) -> str:
    value = _clean_num(score)
    if reverse:
        if value >= 70:
            return "risk"
        if value >= 45:
            return "warn"
        return "good"
    if value >= 75:
        return "good"
    if value >= 50:
        return "accent"
    if value >= 30:
        return "warn"
    return "risk"


def _as_date_string(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return pd.to_datetime(value).date().isoformat()
    except Exception:
        return str(value)


def _coalesce_text(df: pd.DataFrame, *columns: str, fallback: str = "") -> pd.Series:
    if df.empty:
        return pd.Series(dtype="string")
    out = pd.Series(pd.NA, index=df.index, dtype="string")
    for col in columns:
        if col not in df.columns:
            continue
        values = df[col].astype("string", copy=False).str.strip()
        values = values.where(values.str.len() > 0)
        mask = out.isna()
        if mask.any():
            out.loc[mask] = values.loc[mask]
    if fallback:
        out = out.fillna(fallback)
    return out


def _scope_hash(scope: Dict[str, Any]) -> str:
    return str(scope.get("scope_hash") or "")


def _user_identifier(user: Any) -> str:
    try:
        return str(getattr(user, "id", None) or getattr(user, "email", None) or getattr(user, "username", None) or "anon")
    except Exception:
        return "anon"


def _flag_enabled(name: str, default: bool = False) -> bool:
    raw = current_app.config.get(name)
    if raw is None:
        raw = default
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _forecast_v1_enabled() -> bool:
    # Standardized forecast flag for product drilldown forecast UI/API.
    if _flag_enabled("PRODUCT_FORECAST_V1", False):
        return True
    # Backward compatibility with older environments.
    return _flag_enabled("FEATURE_FORECAST_ENABLED", False)


def _safe_date(value: Any) -> datetime | None:
    try:
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            return None
        return parsed.to_pydatetime()
    except Exception:
        return None


def _window_days(start_iso: str | None, end_iso: str | None, rows_df: pd.DataFrame) -> float:
    start_dt = _safe_date(start_iso)
    end_dt = _safe_date(end_iso)
    if start_dt is not None and end_dt is not None and end_dt >= start_dt:
        return max(1.0, float((end_dt.date() - start_dt.date()).days + 1))
    if rows_df.empty:
        return 1.0
    dates = pd.to_datetime(rows_df.get("order_date"), errors="coerce")
    dates = dates[dates.notna()]
    if dates.empty:
        return 1.0
    span_days = float((dates.max().date() - dates.min().date()).days + 1)
    return max(1.0, span_days)


def _normalized_filters(filters: Any) -> Any:
    """
    Normalize date window for drilldown analytics:
    - Lookback bounded by selected global filter end date
    - If no start/end provided, leave default handling to build_where_clause
    """
    if filters is None:
        return filters
    try:
        # FilterParams dataclass path
        start = getattr(filters, "start", None)
        end = getattr(filters, "end", None)
        if start is not None or end is not None:
            return replace(filters, start=start, end=end)
        return filters
    except Exception:
        if isinstance(filters, dict):
            return {
                **filters,
                "start": filters.get("start") or filters.get("start_date"),
                "end": filters.get("end") or filters.get("end_date"),
            }
        return filters


def _product_row_query(product_id: str, filters: Any, scope: Dict[str, Any]) -> Tuple[pd.DataFrame, str | None, str | None]:
    cols = fact_store.list_columns()
    date_col = products_bundle._safe_col(cols, products_bundle.fs.CANON.date, "Date")
    revenue_col = products_bundle._safe_col(cols, products_bundle.fs.CANON.revenue, "Revenue")
    cost_expr = products_bundle._coalesce_expr(cols, (products_bundle.fs.CANON.cost, "Cost", "CostPrice"), "NULL")
    qty_expr = products_bundle._coalesce_expr(
        cols,
        (
            products_bundle.fs.CANON.qty_units,
            "ShippedItems",
            "QuantityOrdered",
            "Qty",
            "Quantity",
            "Units",
            "ItemCount",
        ),
        "0",
    )
    weight_expr = products_bundle._coalesce_expr(
        cols,
        (products_bundle.fs.CANON.weight_lb, "Weight", "WeightLb", "ShippedLb", "pack_weight_lb_sum"),
        "0",
    )
    customer_id_col = products_bundle._safe_col(cols, products_bundle.fs.CANON.customer_id, "CustomerID")
    customer_name_col = products_bundle._safe_col(cols, products_bundle.fs.CANON.customer_name, "CustomerName", "Name")
    order_id_col = products_bundle._safe_col(cols, products_bundle.fs.CANON.order_id, "OrderID")
    region_col = products_bundle._safe_col(cols, products_bundle.fs.CANON.region, "Region", "RegionName")
    supplier_col = products_bundle._safe_col(
        cols,
        products_bundle.fs.CANON.supplier_id,
        products_bundle.fs.CANON.supplier_name,
        "Supplier",
        "SupplierName",
    )
    ship_method_col = products_bundle._safe_col(cols, products_bundle.fs.CANON.ship_method, "ShippingMethodName", "ShipperName")

    if not all([date_col, revenue_col, customer_id_col, order_id_col]):
        return pd.DataFrame(), None, None

    exprs = products_bundle._product_exprs(cols)
    family_exprs = products_bundle._family_exprs(cols)
    product_key_expr = exprs["product_key_expr"]
    prod_id_expr = exprs["prod_id_expr"]
    sku_expr = exprs["sku_expr"]
    product_name_expr = exprs["product_name_expr"]
    display_name_expr = exprs["display_name_expr"]
    protein_expr = family_exprs["protein_expr"]
    category_expr = family_exprs["category_expr"]
    customer_name_expr = (
        f"COALESCE({customer_name_col}, {customer_id_col})" if customer_name_col else f"{customer_id_col}"
    )
    region_expr = region_col or "NULL"
    supplier_expr = supplier_col or "NULL"
    ship_expr = ship_method_col or "NULL"

    normalized = _normalized_filters(filters)
    where_sql, where_params, start_iso, end_iso = fact_store.build_where_clause(
        normalized,
        cols,
        scope,
        apply_default_window=True,
    )

    sql = f"""
        WITH scoped AS (
            SELECT
                {date_col}::DATE AS order_date,
                {product_key_expr} AS product_id,
                {prod_id_expr} AS product_id_raw,
                {sku_expr} AS sku,
                {product_name_expr} AS product_name,
                {display_name_expr} AS display_name,
                {protein_expr} AS protein_family,
                {category_expr} AS product_category,
                {customer_id_col}::VARCHAR AS customer_id,
                {customer_name_expr}::VARCHAR AS customer_name,
                {order_id_col}::VARCHAR AS order_id,
                {region_expr}::VARCHAR AS region,
                {supplier_expr}::VARCHAR AS supplier,
                {ship_expr}::VARCHAR AS ship_method,
                CAST({revenue_col} AS DOUBLE) AS revenue,
                CAST({cost_expr} AS DOUBLE) AS cost,
                CAST({qty_expr} AS DOUBLE) AS units,
                CAST({weight_expr} AS DOUBLE) AS weight_lb,
                CASE
                    WHEN CAST({qty_expr} AS DOUBLE) > 0 THEN CAST({revenue_col} AS DOUBLE) / NULLIF(CAST({qty_expr} AS DOUBLE), 0)
                    ELSE NULL
                END AS asp_unit,
                CASE
                    WHEN CAST({weight_expr} AS DOUBLE) > 0 THEN CAST({revenue_col} AS DOUBLE) / NULLIF(CAST({weight_expr} AS DOUBLE), 0)
                    ELSE NULL
                END AS asp_lb
            FROM fact
            WHERE {where_sql}
        )
        SELECT
            order_date,
            product_id,
            product_id_raw,
            sku,
            product_name,
            display_name,
            protein_family,
            product_category,
            customer_id,
            customer_name,
            order_id,
            region,
            supplier,
            ship_method,
            revenue,
            cost,
            units,
            weight_lb,
            asp_unit,
            asp_lb,
            revenue - cost AS profit
        FROM scoped
        WHERE product_id = ? OR product_id_raw = ?
        ORDER BY order_date
    """
    params = list(where_params) + [product_id, product_id]
    df = fact_store.execute_sql_df(sql, params, tag="products.drilldown.v2.rows")
    if df.empty:
        return df, start_iso, end_iso

    df = df.copy()
    df["order_date"] = pd.to_datetime(df["order_date"], errors="coerce")
    revenue_vals = pd.to_numeric(df["revenue"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    profit_vals = pd.to_numeric(df["profit"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    margin_vals = np.full(revenue_vals.shape, np.nan, dtype=float)
    np.divide(profit_vals, revenue_vals, out=margin_vals, where=revenue_vals != 0.0)
    df["margin_pct"] = margin_vals * 100.0
    df = margin_rules.annotate_margin_frame(
        df,
        protein_col="protein_family",
        category_col="product_category",
        revenue_col="revenue",
        cost_col="cost",
        profit_col="profit",
        margin_col="margin_pct",
        unit_cost_col="unit_cost",
        unit_price_col="asp_lb",
    )
    return df, start_iso, end_iso


def _scope_window_totals(filters: Any, scope: Dict[str, Any]) -> Dict[str, float]:
    cols = fact_store.list_columns()
    revenue_col = products_bundle._safe_col(cols, products_bundle.fs.CANON.revenue, "Revenue")
    customer_id_col = products_bundle._safe_col(cols, products_bundle.fs.CANON.customer_id, "CustomerID")
    if not revenue_col:
        return {"total_revenue": 0.0, "active_customers": 0.0}

    normalized = _normalized_filters(filters)
    where_sql, where_params, _start_iso, _end_iso = fact_store.build_where_clause(
        normalized,
        cols,
        scope,
        apply_default_window=True,
    )
    customer_expr = customer_id_col or "NULL"
    sql = f"""
        SELECT
            SUM(CAST({revenue_col} AS DOUBLE)) AS total_revenue,
            COUNT(DISTINCT {customer_expr}) AS active_customers
        FROM fact
        WHERE {where_sql}
    """
    raw = fact_store.execute_sql_df(sql, where_params, tag="products.drilldown.v2.scope_totals")
    if raw.empty:
        return {"total_revenue": 0.0, "active_customers": 0.0}
    row = raw.iloc[0]
    return {
        "total_revenue": _clean_num(row.get("total_revenue")),
        "active_customers": _clean_num(row.get("active_customers")),
    }


def _family_peer_context(product_id: str, filters: Any, scope: Dict[str, Any], rows_df: pd.DataFrame) -> Dict[str, Any]:
    if rows_df.empty:
        return {}

    family = _coalesce_text(rows_df, "protein_family", "product_category", fallback="").iloc[0]
    category = _coalesce_text(rows_df, "product_category", "protein_family", fallback="").iloc[0]
    family_value = str(family or "").strip()
    category_value = str(category or "").strip()
    if not family_value and not category_value:
        return {}

    cols = fact_store.list_columns()
    date_col = products_bundle._safe_col(cols, products_bundle.fs.CANON.date, "Date")
    revenue_col = products_bundle._safe_col(cols, products_bundle.fs.CANON.revenue, "Revenue")
    cost_expr = products_bundle._coalesce_expr(cols, (products_bundle.fs.CANON.cost, "Cost", "CostPrice"), "NULL")
    qty_expr = products_bundle._coalesce_expr(
        cols,
        (
            products_bundle.fs.CANON.qty_units,
            "ShippedItems",
            "QuantityOrdered",
            "Qty",
            "Quantity",
            "Units",
            "ItemCount",
        ),
        "0",
    )
    weight_expr = products_bundle._coalesce_expr(
        cols,
        (products_bundle.fs.CANON.weight_lb, "Weight", "WeightLb", "ShippedLb", "pack_weight_lb_sum"),
        "0",
    )
    if not date_col or not revenue_col:
        return {
            "protein_family": family_value or None,
            "product_category": category_value or None,
        }

    exprs = products_bundle._product_exprs(cols)
    family_exprs = products_bundle._family_exprs(cols)
    normalized = _normalized_filters(filters)
    where_sql, where_params, _start_iso, _end_iso = fact_store.build_where_clause(
        normalized,
        cols,
        scope,
        apply_default_window=True,
    )

    sql = f"""
        WITH scoped AS (
            SELECT
                {exprs["product_key_expr"]} AS product_id,
                {exprs["display_name_expr"]} AS display_name,
                {family_exprs["protein_expr"]} AS protein_family,
                {family_exprs["category_expr"]} AS product_category,
                CAST({revenue_col} AS DOUBLE) AS revenue,
                CAST({cost_expr} AS DOUBLE) AS cost,
                CAST({qty_expr} AS DOUBLE) AS units,
                CAST({weight_expr} AS DOUBLE) AS weight_lb
            FROM fact
            WHERE {where_sql}
        ),
        peers AS (
            SELECT
                product_id,
                ANY_VALUE(display_name) AS display_name,
                SUM(revenue) AS revenue,
                SUM(cost) AS cost,
                SUM(units) AS units,
                SUM(weight_lb) AS weight_lb
            FROM scoped
            WHERE product_id <> ?
              AND (
                protein_family = ?
                OR product_category = ?
              )
            GROUP BY 1
        )
        SELECT
            COUNT(*) AS peer_count,
            MEDIAN(
                CASE
                    WHEN weight_lb > 0 THEN revenue / NULLIF(weight_lb, 0)
                    WHEN units > 0 THEN revenue / NULLIF(units, 0)
                    ELSE NULL
                END
            ) AS peer_asp_lb,
            MEDIAN({margin_rules.sql_effective_margin_expr("revenue", "cost", "weight_lb", "units", fallback="NULL")}) AS peer_margin_pct,
            (SELECT display_name FROM peers ORDER BY revenue DESC NULLS LAST LIMIT 1) AS top_peer_display,
            (SELECT revenue FROM peers ORDER BY revenue DESC NULLS LAST LIMIT 1) AS top_peer_revenue
        FROM peers
    """
    params = list(where_params) + [product_id, family_value or category_value, category_value or family_value]
    peer_df = fact_store.execute_sql_df(sql, params, tag="products.drilldown.v2.family_peers")
    if peer_df.empty:
        return {
            "protein_family": family_value or None,
            "product_category": category_value or None,
        }

    peer_row = peer_df.iloc[0]
    return {
        "protein_family": family_value or None,
        "product_category": category_value or None,
        "peer_count": int(peer_row.get("peer_count") or 0),
        "peer_asp_lb": _safe_float(peer_row.get("peer_asp_lb")),
        "peer_margin_pct": _safe_float(peer_row.get("peer_margin_pct")),
        "top_peer_display": peer_row.get("top_peer_display"),
        "top_peer_revenue": _clean_num(peer_row.get("top_peer_revenue")),
    }


def apply_scope_and_filters(base_query_or_df: Any, filters: Any, current_user_obj: Any) -> pd.DataFrame:
    """
    Canonical scoped resolver for product drilldown datasets.
    `base_query_or_df` may be a pre-filtered DataFrame or a product_id string.
    """
    if isinstance(base_query_or_df, pd.DataFrame):
        return base_query_or_df.copy()

    product_id = str(base_query_or_df or "").strip()
    if not product_id:
        return pd.DataFrame()

    scope = filters_service.scope_from_user(current_user_obj)
    rows_df, _start_iso, _end_iso = _product_row_query(product_id, filters, scope)
    return rows_df


def build_time_series(rows_df: pd.DataFrame) -> Dict[str, Any]:
    if rows_df.empty:
        return {
            "monthly": [],
            "weekly": [],
            "seasonality": {
                "years": [],
                "months": [],
                "matrix": [],
                "weight_matrix": [],
                "profile": [],
                "strength_score": 0.0,
                "best_months": [],
                "worst_months": [],
                "seasonal_volatility_pct": None,
                "confidence_note": "No seasonal history available.",
            },
        }

    df = rows_df.copy()
    df["order_date"] = pd.to_datetime(df["order_date"], errors="coerce")
    df = df[df["order_date"].notna()]
    if df.empty:
        return {
            "monthly": [],
            "weekly": [],
            "seasonality": {
                "years": [],
                "months": [],
                "matrix": [],
                "weight_matrix": [],
                "profile": [],
                "strength_score": 0.0,
                "best_months": [],
                "worst_months": [],
                "seasonal_volatility_pct": None,
                "confidence_note": "No seasonal history available.",
            },
        }

    df["month_start"] = df["order_date"].dt.to_period("M").dt.to_timestamp()
    monthly = (
        df.groupby("month_start", dropna=False)
        .agg(
            revenue=("revenue", "sum"),
            cost=("cost", "sum"),
            profit=("profit", "sum"),
            units=("units", "sum"),
            weight_lb=("weight_lb", "sum"),
            orders=("order_id", "nunique"),
            customers=("customer_id", "nunique"),
        )
        .reset_index()
        .sort_values("month_start")
    )
    monthly_revenue = monthly["revenue"].to_numpy(dtype=float)
    monthly_profit = monthly["profit"].to_numpy(dtype=float)
    monthly_margin = np.full(monthly_revenue.shape, np.nan, dtype=float)
    np.divide(monthly_profit, monthly_revenue, out=monthly_margin, where=monthly_revenue != 0.0)
    monthly["margin_pct"] = monthly_margin * 100.0
    unit_vals = monthly["units"].to_numpy(dtype=float)
    weight_vals = monthly["weight_lb"].to_numpy(dtype=float)
    asp_vals = np.full(monthly_revenue.shape, np.nan, dtype=float)
    asp_lb_vals = np.full(monthly_revenue.shape, np.nan, dtype=float)
    np.divide(monthly_revenue, unit_vals, out=asp_vals, where=unit_vals > 0.0)
    np.divide(monthly_revenue, weight_vals, out=asp_lb_vals, where=weight_vals > 0.0)
    monthly["asp"] = asp_vals
    monthly["asp_lb"] = asp_lb_vals
    monthly_profit_lb = np.full(monthly_profit.shape, np.nan, dtype=float)
    np.divide(monthly_profit, weight_vals, out=monthly_profit_lb, where=weight_vals > 0.0)
    monthly["profit_per_lb"] = monthly_profit_lb
    monthly_weight_per_unit = np.full(weight_vals.shape, np.nan, dtype=float)
    np.divide(weight_vals, unit_vals, out=monthly_weight_per_unit, where=unit_vals > 0.0)
    monthly["weight_per_unit"] = monthly_weight_per_unit
    order_vals = monthly["orders"].to_numpy(dtype=float)
    customer_vals = monthly["customers"].to_numpy(dtype=float)
    units_per_order = np.full(unit_vals.shape, np.nan, dtype=float)
    weight_per_order = np.full(weight_vals.shape, np.nan, dtype=float)
    customers_per_order = np.full(customer_vals.shape, np.nan, dtype=float)
    np.divide(unit_vals, order_vals, out=units_per_order, where=order_vals > 0.0)
    np.divide(weight_vals, order_vals, out=weight_per_order, where=order_vals > 0.0)
    np.divide(customer_vals, order_vals, out=customers_per_order, where=order_vals > 0.0)
    monthly["units_per_order"] = units_per_order
    monthly["weight_per_order"] = weight_per_order
    monthly["customers_per_order"] = customers_per_order
    monthly["revenue_ma3"] = monthly["revenue"].rolling(window=3, min_periods=1).mean()
    monthly["weight_lb_ma3"] = monthly["weight_lb"].rolling(window=3, min_periods=1).mean()
    monthly["asp_lb_ma3"] = monthly["asp_lb"].rolling(window=3, min_periods=1).mean()
    monthly["month"] = monthly["month_start"].dt.strftime("%Y-%m")
    monthly["month_label"] = monthly["month_start"].dt.strftime("%b %Y")

    # Use Monday-start weeks; W-MON would start on Tuesday because the period ends on Monday.
    df["week_start"] = df["order_date"].dt.to_period("W-SUN").dt.start_time
    weekly = (
        df.groupby("week_start", dropna=False)
        .agg(
            revenue=("revenue", "sum"),
            profit=("profit", "sum"),
            units=("units", "sum"),
            weight_lb=("weight_lb", "sum"),
            orders=("order_id", "nunique"),
            customers=("customer_id", "nunique"),
        )
        .reset_index()
        .sort_values("week_start")
    )
    weekly_revenue = weekly["revenue"].to_numpy(dtype=float)
    weekly_profit = weekly["profit"].to_numpy(dtype=float)
    weekly_units = weekly["units"].to_numpy(dtype=float)
    weekly_weight = weekly["weight_lb"].to_numpy(dtype=float)
    weekly_margin = np.full(weekly_revenue.shape, np.nan, dtype=float)
    weekly_asp_lb = np.full(weekly_revenue.shape, np.nan, dtype=float)
    np.divide(weekly_profit, weekly_revenue, out=weekly_margin, where=weekly_revenue != 0.0)
    np.divide(weekly_revenue, weekly_weight, out=weekly_asp_lb, where=weekly_weight > 0.0)
    weekly["margin_pct"] = weekly_margin * 100.0
    weekly["asp_lb"] = weekly_asp_lb
    weekly["avg_weight_per_unit"] = np.where(weekly_units > 0.0, weekly_weight / weekly_units, np.nan)
    weekly["week"] = weekly["week_start"].dt.strftime("%Y-%m-%d")

    # Seasonality matrix (YoY x month)
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    monthly["year"] = monthly["month_start"].dt.year.astype(int)
    monthly["month_idx"] = monthly["month_start"].dt.month.astype(int)
    years = sorted(monthly["year"].dropna().unique().tolist())
    matrix: List[List[float | None]] = []
    weight_matrix: List[List[float | None]] = []
    for year in years:
        row_vals: List[float | None] = []
        weight_vals_for_year: List[float | None] = []
        subset = monthly[monthly["year"] == year]
        for month_idx in range(1, 13):
            hit = subset[subset["month_idx"] == month_idx]
            if hit.empty:
                row_vals.append(None)
                weight_vals_for_year.append(None)
            else:
                row_vals.append(_clean_num(hit.iloc[0]["revenue"]))
                weight_vals_for_year.append(_clean_num(hit.iloc[0]["weight_lb"]))
        matrix.append(row_vals)
        weight_matrix.append(weight_vals_for_year)

    season_profile = (
        monthly.groupby("month_idx", dropna=False)
        .agg(
            avg_revenue=("revenue", "mean"),
            avg_weight_lb=("weight_lb", "mean"),
            avg_units=("units", "mean"),
            avg_margin_pct=("margin_pct", "mean"),
            observations=("revenue", "size"),
        )
        .reset_index()
        .sort_values("month_idx")
    )
    revenue_mean = _safe_float(season_profile["avg_revenue"].mean())
    weight_mean = _safe_float(season_profile["avg_weight_lb"].mean())
    season_profile["month_name"] = season_profile["month_idx"].map(lambda idx: month_names[int(idx) - 1] if 1 <= int(idx) <= 12 else str(idx))
    season_profile["revenue_index"] = season_profile["avg_revenue"].apply(
        lambda value: (_clean_num(value) / revenue_mean * 100.0) if revenue_mean and revenue_mean > 0 else None
    )
    season_profile["weight_index"] = season_profile["avg_weight_lb"].apply(
        lambda value: (_clean_num(value) / weight_mean * 100.0) if weight_mean and weight_mean > 0 else None
    )
    valid_revenue_index = pd.to_numeric(season_profile["revenue_index"], errors="coerce").dropna()
    strength_score = float(min(100.0, valid_revenue_index.std(ddof=0) * 2.5)) if not valid_revenue_index.empty else 0.0
    history_months = len(monthly.index)
    if history_months < 6:
        strength_score = 0.0
    elif history_months < 12:
        strength_score *= history_months / 12.0
    seasonal_volatility_pct = float(valid_revenue_index.std(ddof=0)) if not valid_revenue_index.empty else None
    best_months = (
        season_profile.sort_values("revenue_index", ascending=False)["month_name"].head(3).astype(str).tolist()
        if not season_profile.empty
        else []
    )
    worst_months = (
        season_profile.sort_values("revenue_index", ascending=True)["month_name"].head(3).astype(str).tolist()
        if not season_profile.empty
        else []
    )
    confidence_note = "Seasonality is directional only; fewer than 12 months are available."
    if history_months < 6:
        confidence_note = "Insufficient monthly history for a reliable seasonality score."
    elif history_months >= 24:
        confidence_note = "Seasonality score uses at least two years of monthly history."
    elif history_months >= 12:
        confidence_note = "Seasonality score uses one year of monthly history."

    return {
        "monthly": monthly[
            [
                "month",
                "month_label",
                "revenue",
                "cost",
                "profit",
                "margin_pct",
                "orders",
                "customers",
                "units",
                "weight_lb",
                "asp",
                "asp_lb",
                "profit_per_lb",
                "weight_per_unit",
                "units_per_order",
                "weight_per_order",
                "customers_per_order",
                "revenue_ma3",
                "weight_lb_ma3",
                "asp_lb_ma3",
            ]
        ].replace([np.inf, -np.inf], np.nan).to_dict(orient="records"),
        "weekly": weekly[
            ["week", "revenue", "profit", "units", "weight_lb", "orders", "customers", "margin_pct", "asp_lb", "avg_weight_per_unit"]
        ].replace([np.inf, -np.inf], np.nan).to_dict(orient="records"),
        "seasonality": {
            "years": years,
            "months": month_names,
            "matrix": matrix,
            "weight_matrix": weight_matrix,
            "profile": season_profile.replace([np.inf, -np.inf], np.nan).to_dict(orient="records"),
            "strength_score": strength_score,
            "best_months": best_months,
            "worst_months": worst_months,
            "seasonal_volatility_pct": seasonal_volatility_pct,
            "confidence_note": confidence_note,
        },
    }


def build_distributions(rows_df: pd.DataFrame) -> Dict[str, Any]:
    if rows_df.empty:
        return {
            "price": {"samples": [], "p10": None, "p50": None, "p90": None, "label": "ASP/lb"},
            "price_unit": {"samples": [], "p10": None, "p50": None, "p90": None, "label": "ASP/unit"},
            "margin": {"samples": [], "p10": None, "p50": None, "p90": None},
            "profit_per_lb": {"samples": [], "p10": None, "p50": None, "p90": None},
            "weight_per_order": {"samples": [], "p10": None, "p50": None, "p90": None},
            "price_outlier": {
                "lower_bound": None,
                "upper_bound": None,
                "order_share_pct": 0.0,
                "revenue_share_pct": 0.0,
            },
        }

    price_samples = pd.to_numeric(rows_df.get("asp_lb"), errors="coerce")
    price_unit_samples = pd.to_numeric(rows_df.get("asp_unit"), errors="coerce")
    margin_samples = pd.to_numeric(rows_df.get("margin_pct"), errors="coerce")
    profit_series = pd.to_numeric(rows_df.get("profit"), errors="coerce")
    weight_series = pd.to_numeric(rows_df.get("weight_lb"), errors="coerce")
    profit_lb_samples = pd.Series(
        np.where(
            weight_series.fillna(0.0).to_numpy(dtype=float) > 0.0,
            profit_series.fillna(np.nan).to_numpy(dtype=float) / np.where(weight_series.fillna(0.0).to_numpy(dtype=float) == 0.0, np.nan, weight_series.fillna(0.0).to_numpy(dtype=float)),
            np.nan,
        )
    )
    order_level = (
        rows_df.groupby("order_id", dropna=False)
        .agg(revenue=("revenue", "sum"), weight_lb=("weight_lb", "sum"), units=("units", "sum"))
        .reset_index()
    )
    weight_order_samples = pd.to_numeric(order_level.get("weight_lb"), errors="coerce")
    price_lb_by_order = pd.Series(
        np.where(
            pd.to_numeric(order_level.get("weight_lb"), errors="coerce").fillna(0.0).to_numpy(dtype=float) > 0.0,
            pd.to_numeric(order_level.get("revenue"), errors="coerce").fillna(0.0).to_numpy(dtype=float)
            / np.where(
                pd.to_numeric(order_level.get("weight_lb"), errors="coerce").fillna(0.0).to_numpy(dtype=float) == 0.0,
                np.nan,
                pd.to_numeric(order_level.get("weight_lb"), errors="coerce").fillna(0.0).to_numpy(dtype=float),
            ),
            np.nan,
        )
    )
    p10, p50, p90 = _quantile_triplet(price_samples)
    unit_p10, unit_p50, unit_p90 = _quantile_triplet(price_unit_samples)
    m10, m50, m90 = _quantile_triplet(margin_samples)
    profit_lb_p10, profit_lb_p50, profit_lb_p90 = _quantile_triplet(profit_lb_samples)
    weight_p10, weight_p50, weight_p90 = _quantile_triplet(weight_order_samples)

    outlier_lower = None
    outlier_upper = None
    outlier_order_share = 0.0
    outlier_revenue_share = 0.0
    clean_order_prices = price_lb_by_order.dropna()
    if not clean_order_prices.empty:
        q1 = float(np.quantile(clean_order_prices.to_numpy(dtype=float), 0.25))
        q3 = float(np.quantile(clean_order_prices.to_numpy(dtype=float), 0.75))
        iqr = q3 - q1
        outlier_lower = q1 - (1.5 * iqr)
        outlier_upper = q3 + (1.5 * iqr)
        outlier_mask = (price_lb_by_order < outlier_lower) | (price_lb_by_order > outlier_upper)
        outlier_mask = outlier_mask.fillna(False)
        if len(order_level.index):
            outlier_order_share = float(outlier_mask.mean() * 100.0)
        total_order_revenue = float(pd.to_numeric(order_level.get("revenue"), errors="coerce").fillna(0.0).sum() or 0.0)
        if total_order_revenue > 0.0:
            outlier_revenue_share = float(
                pd.to_numeric(order_level.loc[outlier_mask, "revenue"], errors="coerce").fillna(0.0).sum() / total_order_revenue * 100.0
            )

    return {
        "price": {
            "samples": price_samples.dropna().astype(float).head(20_000).tolist(),
            "p10": p10,
            "p50": p50,
            "p90": p90,
            "label": "ASP/lb",
        },
        "price_unit": {
            "samples": price_unit_samples.dropna().astype(float).head(20_000).tolist(),
            "p10": unit_p10,
            "p50": unit_p50,
            "p90": unit_p90,
            "label": "ASP/unit",
        },
        "margin": {
            "samples": margin_samples.dropna().astype(float).head(20_000).tolist(),
            "p10": m10,
            "p50": m50,
            "p90": m90,
        },
        "profit_per_lb": {
            "samples": profit_lb_samples.dropna().astype(float).head(20_000).tolist(),
            "p10": profit_lb_p10,
            "p50": profit_lb_p50,
            "p90": profit_lb_p90,
        },
        "weight_per_order": {
            "samples": weight_order_samples.dropna().astype(float).head(20_000).tolist(),
            "p10": weight_p10,
            "p50": weight_p50,
            "p90": weight_p90,
        },
        "price_outlier": {
            "lower_bound": outlier_lower,
            "upper_bound": outlier_upper,
            "order_share_pct": outlier_order_share,
            "revenue_share_pct": outlier_revenue_share,
        },
    }


def _median_cadence_days(rows_df: pd.DataFrame) -> Dict[str, float]:
    if rows_df.empty:
        return {}
    df = rows_df.copy()
    df["order_date"] = pd.to_datetime(df["order_date"], errors="coerce")
    df = df[df["order_date"].notna()]
    if df.empty:
        return {}
    result: Dict[str, float] = {}
    for customer_id, grp in df.groupby("customer_id"):
        order_days = sorted(set(grp["order_date"].dt.date.tolist()))
        if len(order_days) < 2:
            continue
        diffs = np.diff(np.array(order_days, dtype="datetime64[D]")).astype("timedelta64[D]").astype(float)
        if diffs.size:
            result[str(customer_id)] = float(np.median(diffs))
    return result


def build_customer_breakdowns(rows_df: pd.DataFrame) -> Dict[str, Any]:
    if rows_df.empty:
        return {
            "rows": [],
            "top_rows": [],
            "top_weight_rows": [],
            "top_profit_rows": [],
            "at_risk_rows": [],
            "concentration": {
                "top1_share_pct": 0.0,
                "top5_share_pct": 0.0,
                "top10_share_pct": 0.0,
                "hhi": 0.0,
                "revenue": {"top1_share_pct": 0.0, "top5_share_pct": 0.0, "top10_share_pct": 0.0, "hhi": 0.0},
                "weight": {"top1_share_pct": 0.0, "top5_share_pct": 0.0, "top10_share_pct": 0.0, "hhi": 0.0},
                "profit": {"top1_share_pct": 0.0, "top5_share_pct": 0.0, "top10_share_pct": 0.0, "hhi": 0.0},
            },
            "repeat_customer_share_pct": 0.0,
            "new_customer_share_pct": 0.0,
            "current_28d_new_revenue_share_pct": 0.0,
            "current_28d_repeat_revenue_share_pct": 0.0,
            "cadence_median_days": None,
            "monthly_active_customers": [],
            "drop_signals": [],
        }

    df = rows_df.copy()
    df["order_date"] = pd.to_datetime(df["order_date"], errors="coerce")
    grouped = (
        df.groupby("customer_id", dropna=False)
        .agg(
            customer_name=("customer_name", "first"),
            revenue=("revenue", "sum"),
            cost=("cost", "sum"),
            profit=("profit", "sum"),
            orders=("order_id", "nunique"),
            units=("units", "sum"),
            weight_lb=("weight_lb", "sum"),
            first_order_date=("order_date", "min"),
            last_order_date=("order_date", "max"),
        )
        .reset_index()
    )
    grouped_revenue = grouped["revenue"].to_numpy(dtype=float)
    grouped_profit = grouped["profit"].to_numpy(dtype=float)
    grouped_margin = np.full(grouped_revenue.shape, np.nan, dtype=float)
    np.divide(grouped_profit, grouped_revenue, out=grouped_margin, where=grouped_revenue != 0.0)
    grouped["margin_pct"] = grouped_margin * 100.0
    grouped_units = grouped["units"].to_numpy(dtype=float)
    grouped_weight = grouped["weight_lb"].to_numpy(dtype=float)
    asp_vals = np.full(grouped_revenue.shape, np.nan, dtype=float)
    asp_lb_vals = np.full(grouped_revenue.shape, np.nan, dtype=float)
    np.divide(grouped_revenue, grouped_units, out=asp_vals, where=grouped_units > 0.0)
    np.divide(grouped_revenue, grouped_weight, out=asp_lb_vals, where=grouped_weight > 0.0)
    grouped["asp"] = asp_vals
    grouped["asp_lb"] = asp_lb_vals

    cadence = _median_cadence_days(df)
    grouped["cadence_days"] = grouped["customer_id"].astype(str).map(cadence)
    end_dt = pd.Timestamp(df["order_date"].max()).normalize()
    grouped["days_since_last"] = (end_dt - grouped["last_order_date"]).dt.days.astype(float)

    total_revenue = float(grouped["revenue"].sum() or 0.0)
    total_weight = float(grouped["weight_lb"].sum() or 0.0)
    positive_profit = grouped["profit"].clip(lower=0.0)
    total_positive_profit = float(positive_profit.sum() or 0.0)
    revenue_share = np.zeros(len(grouped.index), dtype=float)
    weight_share = np.zeros(len(grouped.index), dtype=float)
    profit_share = np.zeros(len(grouped.index), dtype=float)
    if total_revenue > 0.0:
        np.divide(
            grouped["revenue"].to_numpy(dtype=float),
            total_revenue,
            out=revenue_share,
        )
        revenue_share *= 100.0
    if total_weight > 0.0:
        np.divide(
            grouped["weight_lb"].to_numpy(dtype=float),
            total_weight,
            out=weight_share,
        )
        weight_share *= 100.0
    if total_positive_profit > 0.0:
        np.divide(
            positive_profit.to_numpy(dtype=float),
            total_positive_profit,
            out=profit_share,
        )
        profit_share *= 100.0
    grouped["revenue_share_pct"] = revenue_share
    grouped["weight_share_pct"] = weight_share
    grouped["profit_share_pct"] = profit_share

    first_order_dt = grouped["first_order_date"]
    current_start = end_dt - pd.Timedelta(days=27)
    grouped["is_new_customer"] = first_order_dt >= current_start
    grouped["is_repeat_customer"] = grouped["orders"].fillna(0).astype(float) >= 2.0

    current_28 = df[df["order_date"] >= current_start]
    current_28_by_customer = (
        current_28.groupby("customer_id", dropna=False).agg(current_28_revenue=("revenue", "sum")).reset_index()
        if not current_28.empty
        else pd.DataFrame(columns=["customer_id", "current_28_revenue"])
    )
    grouped = grouped.merge(current_28_by_customer, on="customer_id", how="left")
    grouped["current_28_revenue"] = grouped["current_28_revenue"].fillna(0.0)
    current_28_revenue_total = float(grouped["current_28_revenue"].sum() or 0.0)
    new_current_revenue = float(grouped.loc[grouped["is_new_customer"], "current_28_revenue"].sum() or 0.0)
    repeat_current_revenue = float(grouped.loc[~grouped["is_new_customer"], "current_28_revenue"].sum() or 0.0)

    grouped["risk_flag"] = np.where(
        grouped["days_since_last"] > np.maximum(45.0, grouped["cadence_days"].fillna(30.0) * 1.5),
        "At risk",
        "Active",
    )
    grouped["first_order_date"] = grouped["first_order_date"].dt.strftime("%Y-%m-%d")
    grouped["last_order_date"] = grouped["last_order_date"].dt.strftime("%Y-%m-%d")

    grouped = grouped.sort_values("revenue", ascending=False).reset_index(drop=True)
    revenue_concentration = _top_share_metrics(grouped["revenue"])
    weight_concentration = _top_share_metrics(grouped["weight_lb"])
    profit_concentration = _top_share_metrics(positive_profit)

    # Largest month-over-month declines by customer for this SKU.
    drop_signals: List[Dict[str, Any]] = []
    month_df = df.copy()
    month_df["month"] = month_df["order_date"].dt.to_period("M").dt.to_timestamp()
    customer_month = (
        month_df.groupby(["customer_id", "month"], dropna=False)
        .agg(customer_name=("customer_name", "first"), revenue=("revenue", "sum"))
        .reset_index()
    )
    if not customer_month.empty:
        latest_month = customer_month["month"].max()
        prev_month = (latest_month - pd.offsets.MonthBegin(1)).to_pydatetime().date() if pd.notna(latest_month) else None
        if prev_month is not None:
            prev_month_ts = pd.Timestamp(prev_month)
            current = customer_month[customer_month["month"] == latest_month][["customer_id", "customer_name", "revenue"]].rename(
                columns={"revenue": "revenue_current"}
            )
            prior = customer_month[customer_month["month"] == prev_month_ts][["customer_id", "revenue"]].rename(
                columns={"revenue": "revenue_prior"}
            )
            merged = current.merge(prior, on="customer_id", how="outer")
            merged["customer_name"] = merged["customer_name"].fillna(merged["customer_id"].astype(str))
            merged["revenue_current"] = merged["revenue_current"].fillna(0.0)
            merged["revenue_prior"] = merged["revenue_prior"].fillna(0.0)
            merged["delta_revenue"] = merged["revenue_current"] - merged["revenue_prior"]
            prior_vals = merged["revenue_prior"].to_numpy(dtype=float)
            delta_vals = merged["delta_revenue"].to_numpy(dtype=float)
            delta_pct = np.full(prior_vals.shape, np.nan, dtype=float)
            np.divide(delta_vals, prior_vals, out=delta_pct, where=prior_vals > 0.0)
            merged["delta_pct"] = delta_pct * 100.0
            drops = merged.sort_values("delta_revenue", ascending=True).head(10)
            drop_signals = drops.to_dict(orient="records")

    monthly_active = (
        month_df.groupby("month", dropna=False)
        .agg(customers=("customer_id", "nunique"), revenue=("revenue", "sum"), weight_lb=("weight_lb", "sum"))
        .reset_index()
        .sort_values("month")
    )
    monthly_active["month"] = monthly_active["month"].dt.strftime("%Y-%m")
    rows = grouped.replace([np.inf, -np.inf], np.nan).to_dict(orient="records")
    return {
        "rows": rows,
        "top_rows": rows[:15],
        "top_weight_rows": grouped.sort_values("weight_lb", ascending=False).head(15).replace([np.inf, -np.inf], np.nan).to_dict(orient="records"),
        "top_profit_rows": grouped.sort_values("profit", ascending=False).head(15).replace([np.inf, -np.inf], np.nan).to_dict(orient="records"),
        "at_risk_rows": grouped[grouped["risk_flag"] == "At risk"].sort_values(["revenue", "days_since_last"], ascending=[False, False]).head(10).replace([np.inf, -np.inf], np.nan).to_dict(orient="records"),
        "concentration": {
            "top1_share_pct": revenue_concentration["top1_share_pct"],
            "top5_share_pct": revenue_concentration["top5_share_pct"],
            "top10_share_pct": revenue_concentration["top10_share_pct"],
            "hhi": revenue_concentration["hhi"],
            "revenue": revenue_concentration,
            "weight": weight_concentration,
            "profit": profit_concentration,
        },
        "repeat_customer_share_pct": float(grouped["is_repeat_customer"].mean() * 100.0) if len(grouped.index) else 0.0,
        "new_customer_share_pct": float(grouped["is_new_customer"].mean() * 100.0) if len(grouped.index) else 0.0,
        "current_28d_new_revenue_share_pct": (new_current_revenue / current_28_revenue_total * 100.0) if current_28_revenue_total > 0.0 else 0.0,
        "current_28d_repeat_revenue_share_pct": (repeat_current_revenue / current_28_revenue_total * 100.0) if current_28_revenue_total > 0.0 else 0.0,
        "cadence_median_days": float(grouped["cadence_days"].dropna().median()) if grouped["cadence_days"].dropna().size else None,
        "monthly_active_customers": monthly_active.replace([np.inf, -np.inf], np.nan).to_dict(orient="records"),
        "drop_signals": drop_signals,
    }


def _group_breakdown(rows_df: pd.DataFrame, column: str, label: str) -> Dict[str, Any]:
    if rows_df.empty or column not in rows_df.columns:
        return {"rows": [], "top_rows": [], "label": label}
    df = rows_df.copy()
    key_series = _coalesce_text(df, column, fallback="Unknown")
    df["_group_key"] = key_series
    grouped = (
        df.groupby("_group_key", dropna=False)
        .agg(
            revenue=("revenue", "sum"),
            cost=("cost", "sum"),
            profit=("profit", "sum"),
            orders=("order_id", "nunique"),
            customers=("customer_id", "nunique"),
            units=("units", "sum"),
            weight_lb=("weight_lb", "sum"),
        )
        .reset_index()
        .rename(columns={"_group_key": "name"})
        .sort_values("revenue", ascending=False)
    )
    grouped_revenue = grouped["revenue"].to_numpy(dtype=float)
    grouped_profit = grouped["profit"].to_numpy(dtype=float)
    grouped_margin = np.full(grouped_revenue.shape, np.nan, dtype=float)
    np.divide(grouped_profit, grouped_revenue, out=grouped_margin, where=grouped_revenue != 0.0)
    grouped["margin_pct"] = grouped_margin * 100.0
    rows = grouped.replace([np.inf, -np.inf], np.nan).to_dict(orient="records")
    return {"rows": rows, "top_rows": rows[:12], "label": label}


def build_quality_flags(rows_df: pd.DataFrame) -> Dict[str, Any]:
    if rows_df.empty:
        return {
            "cost_coverage_pct": 0.0,
            "missing_cost_rows": 0,
            "missing_cost_revenue": 0.0,
            "missing_pack_rows": 0,
            "needs_protein_mapping_rows": 0,
            "needs_protein_mapping_revenue": 0.0,
        }

    cost_series = pd.to_numeric(rows_df.get("cost"), errors="coerce")
    revenue_series = pd.to_numeric(rows_df.get("revenue"), errors="coerce").fillna(0.0)
    # Zero can be a valid mapped cost; missingness, not value magnitude, should drive coverage.
    cost_present = cost_series.notna()
    coverage_pct = float(cost_present.mean() * 100.0) if len(rows_df.index) else 0.0
    missing_mask = ~cost_present
    missing_revenue = float(revenue_series[missing_mask].sum())
    missing_pack_rows = 0
    if "missing_packs" in rows_df.columns:
        try:
            missing_pack_rows = int(pd.Series(rows_df["missing_packs"]).fillna(False).astype(bool).sum())
        except Exception:
            missing_pack_rows = 0
    protein_series = _coalesce_text(rows_df, "protein_family", "product_category", fallback="")
    category_series = _coalesce_text(rows_df, "product_category", "protein_family", fallback="")
    mapping_needed_mask = pd.Series(
        [
            bool(margin_rules.resolve_margin_rule(protein=protein, category=category).get("needs_protein_mapping") or not margin_rules.resolve_margin_rule(protein=protein, category=category).get("mapped"))
            for protein, category in zip(protein_series.tolist(), category_series.tolist())
        ],
        index=rows_df.index,
        dtype="bool",
    )
    mapping_needed_revenue = float(revenue_series[mapping_needed_mask].sum()) if len(mapping_needed_mask.index) else 0.0

    return {
        "cost_coverage_pct": coverage_pct,
        "missing_cost_rows": int(missing_mask.sum()),
        "missing_cost_revenue": missing_revenue,
        "missing_pack_rows": missing_pack_rows,
        "needs_protein_mapping_rows": int(mapping_needed_mask.sum()),
        "needs_protein_mapping_revenue": mapping_needed_revenue,
    }


def _score_label(score: Any) -> str:
    value = _clean_num(score)
    if value >= 80:
        return "High"
    if value >= 60:
        return "Medium"
    if value >= 40:
        return "Watch"
    return "Low"


def _build_weight_analytics(rows_df: pd.DataFrame, monthly_rows: List[Dict[str, Any]], customers: Dict[str, Any]) -> Dict[str, Any]:
    if rows_df.empty:
        return {
            "summary": {},
            "monthly": [],
            "top_customers": [],
            "order_weight_distribution": {"p10": None, "p50": None, "p90": None},
            "correlations": {"weight_revenue": None, "weight_units": None, "price_lb_weight": None},
            "heavy_order_share_pct": None,
            "top_customer_weight_share_pct": 0.0,
            "intensity_change_pct": None,
            "demand_quality_note": "No weight history available.",
        }

    df = rows_df.copy()
    order_level = (
        df.groupby(["order_id", "order_date", "customer_id", "customer_name"], dropna=False)
        .agg(revenue=("revenue", "sum"), profit=("profit", "sum"), units=("units", "sum"), weight_lb=("weight_lb", "sum"))
        .reset_index()
    )
    order_level["price_lb"] = np.where(
        pd.to_numeric(order_level["weight_lb"], errors="coerce").fillna(0.0).to_numpy(dtype=float) > 0.0,
        pd.to_numeric(order_level["revenue"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
        / np.where(
            pd.to_numeric(order_level["weight_lb"], errors="coerce").fillna(0.0).to_numpy(dtype=float) == 0.0,
            np.nan,
            pd.to_numeric(order_level["weight_lb"], errors="coerce").fillna(0.0).to_numpy(dtype=float),
        ),
        np.nan,
    )
    order_level["profit_lb"] = np.where(
        pd.to_numeric(order_level["weight_lb"], errors="coerce").fillna(0.0).to_numpy(dtype=float) > 0.0,
        pd.to_numeric(order_level["profit"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
        / np.where(
            pd.to_numeric(order_level["weight_lb"], errors="coerce").fillna(0.0).to_numpy(dtype=float) == 0.0,
            np.nan,
            pd.to_numeric(order_level["weight_lb"], errors="coerce").fillna(0.0).to_numpy(dtype=float),
        ),
        np.nan,
    )
    order_level["weight_per_unit"] = np.where(
        pd.to_numeric(order_level["units"], errors="coerce").fillna(0.0).to_numpy(dtype=float) > 0.0,
        pd.to_numeric(order_level["weight_lb"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
        / np.where(
            pd.to_numeric(order_level["units"], errors="coerce").fillna(0.0).to_numpy(dtype=float) == 0.0,
            np.nan,
            pd.to_numeric(order_level["units"], errors="coerce").fillna(0.0).to_numpy(dtype=float),
        ),
        np.nan,
    )

    total_weight = _clean_num(df["weight_lb"].sum())
    total_units = _clean_num(df["units"].sum())
    total_revenue = _clean_num(df["revenue"].sum())
    total_profit = _clean_num(df["profit"].sum())
    total_orders = max(int(df["order_id"].nunique()), 0)
    total_customers = max(int(df["customer_id"].nunique()), 0)
    distribution_p10, distribution_p50, distribution_p90 = _quantile_triplet(order_level["weight_lb"])

    heavy_order_share = None
    if not order_level.empty and total_weight > 0.0:
        heavy_cap = max(1, int(math.ceil(len(order_level.index) * 0.10)))
        heavy_order_share = float(order_level.sort_values("weight_lb", ascending=False).head(heavy_cap)["weight_lb"].sum() / total_weight * 100.0)

    def _corr(series_a: pd.Series, series_b: pd.Series) -> float | None:
        clean = pd.DataFrame({"a": pd.to_numeric(series_a, errors="coerce"), "b": pd.to_numeric(series_b, errors="coerce")}).dropna()
        if len(clean.index) < 4:
            return None
        if clean["a"].std(ddof=0) <= 1e-9 or clean["b"].std(ddof=0) <= 1e-9:
            return None
        return float(np.corrcoef(clean["a"].to_numpy(dtype=float), clean["b"].to_numpy(dtype=float))[0, 1])

    monthly_df = pd.DataFrame.from_records(monthly_rows or [])
    intensity_change_pct = None
    if not monthly_df.empty:
        recent = monthly_df.tail(3)
        prior = monthly_df.iloc[-6:-3]
        recent_intensity = _safe_div(recent["weight_lb"].sum(), recent["units"].sum()) if not recent.empty else None
        prior_intensity = _safe_div(prior["weight_lb"].sum(), prior["units"].sum()) if not prior.empty else None
        intensity_change_pct = _pct_delta(recent_intensity, prior_intensity)

    top_customer_weight_share_pct = _clean_num((((customers.get("concentration") or {}).get("weight") or {}).get("top5_share_pct")))
    demand_quality_note = "Weight and unit movement are moving together."
    weight_units_corr = _corr(order_level["weight_lb"], order_level["units"])
    if len(order_level.index) < 8:
        demand_quality_note = "Weight diagnostics are directional only; fewer than 8 orders are available."
    elif weight_units_corr is not None and weight_units_corr < 0.5:
        demand_quality_note = "Weight and units are diverging; validate pack mix and order composition."

    monthly_payload = []
    if not monthly_df.empty:
        for _, row in monthly_df.iterrows():
            monthly_payload.append(
                {
                    "month": row.get("month"),
                    "month_label": row.get("month_label"),
                    "weight_lb": _safe_float(row.get("weight_lb")),
                    "weight_lb_ma3": _safe_float(row.get("weight_lb_ma3")),
                    "revenue_per_lb": _safe_float(row.get("asp_lb")),
                    "profit_per_lb": _safe_float(row.get("profit_per_lb")),
                    "weight_per_unit": _safe_float(row.get("weight_per_unit")),
                    "weight_per_order": _safe_float(row.get("weight_per_order")),
                }
            )

    return {
        "summary": {
            "total_weight_lb": total_weight,
            "revenue_per_lb": _safe_div(total_revenue, total_weight),
            "profit_per_lb": _safe_div(total_profit, total_weight),
            "avg_weight_per_unit": _safe_div(total_weight, total_units),
            "avg_weight_per_order": _safe_div(total_weight, total_orders),
            "avg_units_per_order": _safe_div(total_units, total_orders),
            "avg_weight_per_customer": _safe_div(total_weight, total_customers),
        },
        "monthly": monthly_payload,
        "top_customers": (customers.get("top_weight_rows") or [])[:10],
        "order_weight_distribution": {"p10": distribution_p10, "p50": distribution_p50, "p90": distribution_p90},
        "correlations": {
            "weight_revenue": _corr(order_level["weight_lb"], order_level["revenue"]),
            "weight_units": weight_units_corr,
            "price_lb_weight": _corr(order_level["price_lb"], order_level["weight_lb"]),
        },
        "heavy_order_share_pct": heavy_order_share,
        "top_customer_weight_share_pct": top_customer_weight_share_pct,
        "intensity_change_pct": intensity_change_pct,
        "demand_quality_note": demand_quality_note,
    }


def _build_lifecycle_insights(
    rows_df: pd.DataFrame,
    monthly_rows: List[Dict[str, Any]],
    base_lifecycle: Dict[str, Any],
    customers: Dict[str, Any],
) -> Dict[str, Any]:
    if rows_df.empty:
        return {
            "stage": "Insufficient history",
            "stage_score": 0.0,
            "trend_direction": "Unknown",
            "months_active": 0,
            "summary": "Not enough history to determine lifecycle stage.",
            "base_stage": (base_lifecycle or {}).get("stage"),
        }

    df = rows_df.copy()
    df["order_date"] = pd.to_datetime(df["order_date"], errors="coerce")
    df = df[df["order_date"].notna()]
    monthly_df = pd.DataFrame.from_records(monthly_rows or []).copy()
    if monthly_df.empty:
        return {
            "stage": "Insufficient history",
            "stage_score": 0.0,
            "trend_direction": "Unknown",
            "months_active": 0,
            "summary": "Not enough monthly history to determine lifecycle stage.",
            "base_stage": (base_lifecycle or {}).get("stage"),
        }

    end_dt = pd.Timestamp(df["order_date"].max()).normalize()
    last_sold_days = int((end_dt - pd.Timestamp(df["order_date"].max()).normalize()).days)
    monthly_df["revenue"] = pd.to_numeric(monthly_df.get("revenue"), errors="coerce").fillna(0.0)
    monthly_df["customers"] = pd.to_numeric(monthly_df.get("customers"), errors="coerce").fillna(0.0)
    months_active = int((monthly_df["revenue"] > 0).sum())
    recent3_revenue = float(monthly_df["revenue"].tail(3).sum() or 0.0)
    prior3_revenue = float(monthly_df["revenue"].iloc[-6:-3].sum() or 0.0)
    recent3_customers = float(monthly_df["customers"].tail(3).sum() or 0.0)
    prior3_customers = float(monthly_df["customers"].iloc[-6:-3].sum() or 0.0)
    recent_growth_pct = _pct_delta(recent3_revenue, prior3_revenue)
    breadth_change_pct = _pct_delta(recent3_customers, prior3_customers)
    trailing = monthly_df["revenue"].tail(min(12, len(monthly_df.index)))
    volatility = float(trailing.std(ddof=0) / trailing.mean()) if trailing.mean() not in {0, np.nan} else None
    revenue_deltas = trailing.diff().dropna()
    persistence = float((revenue_deltas.gt(0).mean() * 100.0)) if not revenue_deltas.empty else None
    dormant_gap_days = int((end_dt - pd.Timestamp(df["order_date"].max())).days)
    prior6 = float(monthly_df["revenue"].iloc[-9:-3].sum() or 0.0)

    stage = "Mature"
    if months_active <= 3:
        stage = "New"
    elif recent3_revenue > 0.0 and prior6 == 0.0 and months_active > 3:
        stage = "Reactivated"
    elif dormant_gap_days >= 90:
        stage = "Declining"
    elif recent_growth_pct is not None and recent_growth_pct >= 18.0 and (breadth_change_pct is None or breadth_change_pct >= 0.0):
        stage = "Growth"
    elif recent_growth_pct is not None and recent_growth_pct <= -15.0:
        stage = "Declining"
    elif volatility is not None and volatility >= 0.60:
        stage = "Unstable"

    trend_direction = "Stable"
    if recent_growth_pct is not None:
        if recent_growth_pct >= 10.0:
            trend_direction = "Growing"
        elif recent_growth_pct <= -10.0:
            trend_direction = "Declining"
    if stage == "Unstable":
        trend_direction = "Volatile"

    stage_score_map = {
        "Growth": 84.0,
        "Mature": 68.0,
        "New": 72.0,
        "Reactivated": 70.0,
        "Unstable": 42.0,
        "Declining": 28.0,
        "Insufficient history": 0.0,
    }
    stability_score = 100.0 - _clamp((volatility or 0.0) * 100.0)
    stage_score = _clamp((stage_score_map.get(stage, 50.0) * 0.65) + (stability_score * 0.35))
    adoption_note = "Customer adoption is stable."
    if breadth_change_pct is not None and breadth_change_pct >= 10.0:
        adoption_note = "Customer adoption is broadening."
    elif breadth_change_pct is not None and breadth_change_pct <= -10.0:
        adoption_note = "Customer adoption is narrowing."

    summary = (
        f"{stage} stage with {months_active} active months. "
        f"Recent 3-month revenue is {('up' if (recent_growth_pct or 0) >= 0 else 'down')} "
        f"{abs(_clean_num(recent_growth_pct)):.1f}% vs prior 3 months; {adoption_note.lower()}"
    )

    return {
        "stage": stage,
        "base_stage": (base_lifecycle or {}).get("stage"),
        "stage_score": stage_score,
        "trend_direction": trend_direction,
        "months_active": months_active,
        "last_sold_days": dormant_gap_days,
        "recent_growth_pct": recent_growth_pct,
        "breadth_change_pct": breadth_change_pct,
        "stability_score": stability_score,
        "trend_persistence_pct": persistence,
        "repeat_customer_share_pct": _safe_float(customers.get("repeat_customer_share_pct")),
        "summary": summary,
    }


def _build_risk_opportunity(
    *,
    total_revenue: float,
    quality: Dict[str, Any],
    customers: Dict[str, Any],
    lifecycle_insights: Dict[str, Any],
    seasonality: Dict[str, Any],
    distributions: Dict[str, Any],
    basket: Dict[str, Any],
    forecast: Dict[str, Any],
    margin_risk_summary: Dict[str, Any],
    weight_analytics: Dict[str, Any],
    delta_28: Dict[str, Any],
) -> Dict[str, Any]:
    risks: List[Dict[str, Any]] = []
    opportunities: List[Dict[str, Any]] = []

    concentration = customers.get("concentration") or {}
    top5_revenue_share = _clean_num((concentration.get("revenue") or {}).get("top5_share_pct"))
    top5_weight_share = _clean_num((concentration.get("weight") or {}).get("top5_share_pct"))
    if top5_revenue_share >= 70.0:
        risks.append(
            {
                "score": top5_revenue_share,
                "tone": "risk" if top5_revenue_share >= 85.0 else "warn",
                "title": "Customer concentration risk",
                "detail": f"Top 5 customers represent {top5_revenue_share:.1f}% of revenue and {top5_weight_share:.1f}% of weight.",
            }
        )

    margin_risk_exposure_pct = _clean_num(margin_risk_summary.get("revenue_exposure_pct"))
    if margin_risk_exposure_pct >= 15.0:
        risks.append(
            {
                "score": margin_risk_exposure_pct,
                "tone": "risk" if margin_risk_exposure_pct >= 30.0 else "warn",
                "title": "Margin recovery needed",
                "detail": f"{margin_risk_exposure_pct:.1f}% of revenue sits below the target margin threshold.",
            }
        )

    price_outlier_revenue_share = _clean_num((distributions.get("price_outlier") or {}).get("revenue_share_pct"))
    if price_outlier_revenue_share >= 8.0:
        risks.append(
            {
                "score": price_outlier_revenue_share,
                "tone": "warn",
                "title": "Price outlier exposure",
                "detail": f"{price_outlier_revenue_share:.1f}% of revenue lands outside normal ASP/lb bounds.",
            }
        )

    seasonality_strength = _clean_num(seasonality.get("strength_score"))
    if seasonality_strength >= 35.0:
        risks.append(
            {
                "score": seasonality_strength,
                "tone": "warn",
                "title": "Seasonality risk",
                "detail": f"Seasonality strength score is {seasonality_strength:.0f}; demand planning should follow seasonal peaks and troughs.",
            }
        )

    if str(lifecycle_insights.get("stage") or "").lower() in {"declining", "unstable"}:
        risks.append(
            {
                "score": 72.0 if str(lifecycle_insights.get("stage")).lower() == "declining" else 58.0,
                "tone": "risk" if str(lifecycle_insights.get("stage")).lower() == "declining" else "warn",
                "title": f"Lifecycle {lifecycle_insights.get('stage', 'risk')}",
                "detail": lifecycle_insights.get("summary") or "Lifecycle trend needs attention.",
            }
        )

    forecast_meta = forecast.get("meta") or {}
    if bool(forecast_meta.get("insufficient_history")) or _clean_num(forecast_meta.get("forecastability_score")) < 45.0:
        risks.append(
            {
                "score": max(20.0, 100.0 - _clean_num(forecast_meta.get("forecastability_score"))),
                "tone": "warn",
                "title": "Forecast uncertainty",
                "detail": forecast_meta.get("summary") or "Forecast is using fallback logic or has limited history.",
            }
        )

    coverage = _clean_num(quality.get("cost_coverage_pct"))
    history_points = int(forecast_meta.get("history_points") or 0)
    data_confidence_score = _clamp((coverage * 0.45) + min(100.0, history_points / 24.0 * 100.0) * 0.25 + (100.0 if total_revenue > 0 else 0.0) * 0.10 + (100.0 - min(100.0, seasonality_strength)) * 0.20)

    basket_rows = basket.get("rows") or []
    if basket_rows:
        best_pair = max(basket_rows, key=lambda item: (_clean_num(item.get("lift")), _clean_num(item.get("paired_revenue"))))
        if _clean_num(best_pair.get("lift")) >= 1.5:
            opportunities.append(
                {
                    "score": _clean_num(best_pair.get("lift")) * 20.0,
                    "tone": "accent",
                    "title": "Basket expansion",
                    "detail": f"{best_pair.get('display_name') or best_pair.get('sku')} shows lift {_clean_num(best_pair.get('lift')):.2f} with paired revenue {_clean_num(best_pair.get('paired_revenue')):,.0f}.",
                }
            )

    if margin_risk_summary.get("top_customer"):
        top_customer = margin_risk_summary["top_customer"]
        opportunities.append(
            {
                "score": _clean_num(top_customer.get("uplift_to_target")),
                "tone": "accent",
                "title": "Pricing recovery",
                "detail": f"{top_customer.get('customer_name') or top_customer.get('customer_id')} offers up to ${_clean_num(top_customer.get('uplift_to_target')):,.0f} of margin uplift to target.",
            }
        )

    if _clean_num(delta_28.get("revenue_delta_pct")) < 0 and _clean_num(customers.get("current_28d_repeat_revenue_share_pct")) >= 70.0:
        opportunities.append(
            {
                "score": _clean_num(customers.get("current_28d_repeat_revenue_share_pct")),
                "tone": "accent",
                "title": "Defend core repeat demand",
                "detail": f"Repeat customers still account for {_clean_num(customers.get('current_28d_repeat_revenue_share_pct')):.1f}% of recent 28-day revenue.",
            }
        )

    if _clean_num(weight_analytics.get("heavy_order_share_pct")) >= 35.0:
        opportunities.append(
            {
                "score": _clean_num(weight_analytics.get("heavy_order_share_pct")),
                "tone": "accent",
                "title": "Weight efficiency play",
                "detail": f"Top heavy orders drive {_clean_num(weight_analytics.get('heavy_order_share_pct')):.1f}% of shipped lb; review freight and pack economics for those accounts.",
            }
        )

    risks.sort(key=lambda item: item.get("score", 0.0), reverse=True)
    opportunities.sort(key=lambda item: item.get("score", 0.0), reverse=True)
    posture = "Stable"
    revenue_delta_pct = _safe_float(delta_28.get("revenue_delta_pct"))
    if str(lifecycle_insights.get("trend_direction") or "").lower() == "volatile":
        posture = "Volatile"
    elif revenue_delta_pct is not None and revenue_delta_pct >= 10.0:
        posture = "Growing"
    elif revenue_delta_pct is not None and revenue_delta_pct <= -10.0:
        posture = "Declining"

    return {
        "posture": posture,
        "data_confidence_score": data_confidence_score,
        "data_confidence_label": _score_label(data_confidence_score),
        "primary_risk": risks[0] if risks else None,
        "primary_opportunity": opportunities[0] if opportunities else None,
        "risks": risks[:6],
        "opportunities": opportunities[:6],
    }


def _build_decision_panel(
    *,
    risk_opportunity: Dict[str, Any],
    performance_story: Dict[str, Any],
    lifecycle_insights: Dict[str, Any],
    customers: Dict[str, Any],
) -> Dict[str, Any]:
    actions: List[Dict[str, Any]] = []
    primary_risk = risk_opportunity.get("primary_risk") or {}
    primary_opportunity = risk_opportunity.get("primary_opportunity") or {}
    if primary_risk:
        actions.append({"title": "Address the primary risk", "detail": primary_risk.get("detail"), "tone": primary_risk.get("tone") or "warn"})
    if primary_opportunity:
        actions.append({"title": "Pursue the highest-value opportunity", "detail": primary_opportunity.get("detail"), "tone": primary_opportunity.get("tone") or "accent"})

    if str(lifecycle_insights.get("stage") or "").lower() in {"declining", "unstable"}:
        actions.append(
            {
                "title": "Reset the lifecycle plan",
                "detail": lifecycle_insights.get("summary") or "Demand is no longer stable; review assortment, price, and customer reactivation plays.",
                "tone": "warn",
            }
        )
    elif performance_story.get("narrative"):
        actions.append(
            {
                "title": "Use the latest 28-day readout",
                "detail": performance_story.get("narrative"),
                "tone": "accent",
            }
        )

    at_risk_rows = customers.get("at_risk_rows") or []
    if at_risk_rows:
        lead = at_risk_rows[0]
        actions.append(
            {
                "title": "Re-engage at-risk customers",
                "detail": f"{lead.get('customer_name') or lead.get('customer_id')} has not bought in {int(_clean_num(lead.get('days_since_last')))} days.",
                "tone": "risk",
            }
        )

    return {"actions": actions[:4]}


def build_basket_affinity(product_id: str, filters: Any, current_user_obj: Any) -> Dict[str, Any]:
    scope = filters_service.scope_from_user(current_user_obj)
    cols = fact_store.list_columns()
    date_col = products_bundle._safe_col(cols, products_bundle.fs.CANON.date, "Date")
    order_id_col = products_bundle._safe_col(cols, products_bundle.fs.CANON.order_id, "OrderID")
    revenue_col = products_bundle._safe_col(cols, products_bundle.fs.CANON.revenue, "Revenue")
    if not all([date_col, order_id_col, revenue_col]):
        return {
            "rows": [],
            "base_orders": 0,
            "total_orders": 0,
            "insufficient_sample": True,
            "message": "Required columns missing for basket analysis.",
        }

    exprs = products_bundle._product_exprs(cols)
    product_key_expr = exprs["product_key_expr"]
    prod_id_expr = exprs["prod_id_expr"]
    sku_expr = exprs["sku_expr"]
    product_name_expr = exprs["product_name_expr"]
    display_name_expr = exprs["display_name_expr"]

    normalized = _normalized_filters(filters)
    where_sql, where_params, _start_iso, _end_iso = fact_store.build_where_clause(
        normalized,
        cols,
        scope,
        apply_default_window=True,
    )

    sql = f"""
        WITH base AS (
            SELECT
                {date_col}::DATE AS order_date,
                {order_id_col}::VARCHAR AS order_id,
                {product_key_expr} AS product_id,
                {prod_id_expr} AS product_id_raw,
                {sku_expr} AS sku,
                {product_name_expr} AS product_name,
                {display_name_expr} AS display_name,
                CAST({revenue_col} AS DOUBLE) AS revenue
            FROM fact
            WHERE {where_sql}
        ),
        orders_for_target AS (
            SELECT DISTINCT order_id
            FROM base
            WHERE product_id = ? OR product_id_raw = ?
        ),
        base_orders AS (
            SELECT COUNT(*) AS base_orders FROM orders_for_target
        ),
        all_orders AS (
            SELECT COUNT(DISTINCT order_id) AS total_orders FROM base
        ),
        other_orders AS (
            SELECT product_id, COUNT(DISTINCT order_id) AS orders_with_other
            FROM base
            GROUP BY product_id
        ),
        co_orders AS (
            SELECT
                b.product_id,
                any_value(b.sku) AS sku,
                any_value(b.product_name) AS product_name,
                any_value(b.display_name) AS display_name,
                COUNT(DISTINCT b.order_id) AS co_orders,
                SUM(b.revenue) AS paired_revenue
            FROM base b
            JOIN orders_for_target t ON b.order_id = t.order_id
            WHERE b.product_id <> ? AND (b.product_id_raw IS NULL OR b.product_id_raw <> ?)
            GROUP BY b.product_id
        )
        SELECT
            c.product_id,
            c.sku,
            c.product_name,
            c.display_name,
            c.co_orders,
            c.paired_revenue,
            o.orders_with_other,
            (SELECT base_orders FROM base_orders) AS base_orders,
            (SELECT total_orders FROM all_orders) AS total_orders
        FROM co_orders c
        LEFT JOIN other_orders o ON c.product_id = o.product_id
        ORDER BY c.co_orders DESC, c.paired_revenue DESC
    """
    params = list(where_params) + [product_id, product_id, product_id, product_id]
    raw = fact_store.execute_sql_df(sql, params, tag="products.drilldown.v2.basket")
    if raw.empty:
        return {
            "rows": [],
            "base_orders": 0,
            "total_orders": 0,
            "insufficient_sample": True,
            "message": "No co-purchase rows under current filters.",
        }

    base_orders = int(_clean_num(raw.iloc[0].get("base_orders")))
    total_orders = int(_clean_num(raw.iloc[0].get("total_orders")))
    if base_orders < 20:
        return {
            "rows": [],
            "base_orders": base_orders,
            "total_orders": total_orders,
            "insufficient_sample": True,
            "message": "Insufficient sample: need at least 20 base orders for lift.",
        }

    df = raw.copy()
    df["co_orders"] = pd.to_numeric(df["co_orders"], errors="coerce").fillna(0.0)
    df["paired_revenue"] = pd.to_numeric(df["paired_revenue"], errors="coerce").fillna(0.0)
    df["orders_with_other"] = pd.to_numeric(df["orders_with_other"], errors="coerce").fillna(0.0)
    overall_support = np.full(len(df.index), np.nan, dtype=float)
    support = np.full(len(df.index), np.nan, dtype=float)
    confidence = np.full(len(df.index), np.nan, dtype=float)
    lift = np.full(len(df.index), np.nan, dtype=float)
    if total_orders > 0:
        np.divide(
            df["orders_with_other"].to_numpy(dtype=float),
            float(total_orders),
            out=overall_support,
        )
        np.divide(
            df["co_orders"].to_numpy(dtype=float),
            float(total_orders),
            out=support,
        )
    if base_orders > 0:
        np.divide(
            df["co_orders"].to_numpy(dtype=float),
            float(base_orders),
            out=confidence,
        )
    np.divide(confidence, overall_support, out=lift, where=overall_support > 0.0)
    df["support"] = support
    df["overall_support"] = overall_support
    df["confidence"] = confidence
    df["lift"] = lift
    df["display_name"] = df.apply(
        lambda row: presentation.format_product_label(row.get("sku"), row.get("product_name") or row.get("display_name"), fallback=str(row.get("product_id") or "Associated Product")),
        axis=1,
    )
    df["display_name_axis"] = df.apply(
        lambda row: presentation.compact_product_label(row.get("sku"), row.get("product_name") or row.get("display_name"), max_length=34, fallback=str(row.get("product_id") or "Associated Product")),
        axis=1,
    )
    df = df.replace([np.inf, -np.inf], np.nan)
    rows = df.to_dict(orient="records")

    return {
        "rows": rows,
        "base_orders": base_orders,
        "total_orders": total_orders,
        "insufficient_sample": False,
        "message": None,
    }


def _reference_end_date(rows_df: pd.DataFrame, end_iso: str | None) -> pd.Timestamp | None:
    end_dt = pd.to_datetime(end_iso, errors="coerce")
    if pd.notna(end_dt):
        return pd.Timestamp(end_dt).normalize()
    dates = pd.to_datetime(rows_df.get("order_date"), errors="coerce")
    dates = dates[dates.notna()]
    if dates.empty:
        return None
    return pd.Timestamp(dates.max()).normalize()


def _performance_story(rows_df: pd.DataFrame, end_iso: str | None) -> Dict[str, Any]:
    if rows_df.empty:
        return {
            "current_window": {},
            "prior_window": {},
            "decomposition": {},
            "top_gainers": [],
            "top_decliners": [],
            "narrative": "Insufficient data to build performance story.",
        }

    end_dt = _reference_end_date(rows_df, end_iso)
    if end_dt is None:
        return {
            "current_window": {},
            "prior_window": {},
            "decomposition": {},
            "top_gainers": [],
            "top_decliners": [],
            "narrative": "Insufficient data to build performance story.",
        }

    df = rows_df.copy()
    df["order_date"] = pd.to_datetime(df["order_date"], errors="coerce")
    df = df[df["order_date"].notna()]
    if df.empty:
        return {
            "current_window": {},
            "prior_window": {},
            "decomposition": {},
            "top_gainers": [],
            "top_decliners": [],
            "narrative": "Insufficient data to build performance story.",
        }

    current_start = end_dt - pd.Timedelta(days=27)
    prior_end = current_start - pd.Timedelta(days=1)
    prior_start = prior_end - pd.Timedelta(days=27)

    cur = df[(df["order_date"] >= current_start) & (df["order_date"] <= end_dt)]
    prev = df[(df["order_date"] >= prior_start) & (df["order_date"] <= prior_end)]

    cur_revenue = _clean_num(cur["revenue"].sum())
    prev_revenue = _clean_num(prev["revenue"].sum())
    cur_units = _clean_num(cur["units"].sum())
    prev_units = _clean_num(prev["units"].sum())
    cur_weight = _clean_num(cur["weight_lb"].sum())
    prev_weight = _clean_num(prev["weight_lb"].sum())
    cur_profit = _clean_num(cur["profit"].sum())
    prev_profit = _clean_num(prev["profit"].sum())

    basis_key = "weight_lb" if (cur_weight > 0.0 or prev_weight > 0.0) else "units"
    basis_label = "lb" if basis_key == "weight_lb" else "units"
    cur_basis = cur_weight if basis_key == "weight_lb" else cur_units
    prev_basis = prev_weight if basis_key == "weight_lb" else prev_units

    cur_price = _safe_div(cur_revenue, cur_basis)
    prev_price = _safe_div(prev_revenue, prev_basis)
    cur_margin = _safe_div(cur_profit, cur_revenue)
    prev_margin = _safe_div(prev_profit, prev_revenue)
    cur_profit_per_lb = _safe_div(cur_profit, cur_weight)
    prev_profit_per_lb = _safe_div(prev_profit, prev_weight)

    price_effect = None
    volume_effect = None
    mix_effect = None
    if cur_price is not None and prev_price is not None:
        delta_price = cur_price - prev_price
        delta_basis = cur_basis - prev_basis
        price_effect = delta_price * prev_basis
        volume_effect = delta_basis * prev_price
        mix_effect = (cur_revenue - prev_revenue) - price_effect - volume_effect

    dominant_driver = None
    components = {
        "price_effect": price_effect,
        "volume_effect": volume_effect,
        "mix_effect": mix_effect,
    }
    non_null_components = {k: v for k, v in components.items() if v is not None}
    if non_null_components:
        dominant_driver = max(non_null_components.items(), key=lambda item: abs(item[1]))[0]

    cur_by_customer = (
        cur.groupby("customer_id", dropna=False)
        .agg(customer_name=("customer_name", "first"), revenue_current=("revenue", "sum"))
        .reset_index()
    )
    prev_by_customer = (
        prev.groupby("customer_id", dropna=False)
        .agg(revenue_prior=("revenue", "sum"))
        .reset_index()
    )
    movers = cur_by_customer.merge(prev_by_customer, on="customer_id", how="outer")
    movers["customer_name"] = movers["customer_name"].fillna(movers["customer_id"].astype(str))
    movers["revenue_current"] = movers["revenue_current"].fillna(0.0)
    movers["revenue_prior"] = movers["revenue_prior"].fillna(0.0)
    movers["delta_revenue"] = movers["revenue_current"] - movers["revenue_prior"]
    prior_vals = movers["revenue_prior"].to_numpy(dtype=float)
    delta_vals = movers["delta_revenue"].to_numpy(dtype=float)
    delta_pct = np.full(prior_vals.shape, np.nan, dtype=float)
    np.divide(delta_vals, prior_vals, out=delta_pct, where=prior_vals > 0.0)
    movers["delta_pct"] = delta_pct * 100.0
    movers = movers.sort_values("delta_revenue", ascending=False).reset_index(drop=True)

    top_gainers = movers.head(5).replace([np.inf, -np.inf], np.nan).to_dict(orient="records")
    top_decliners = movers.tail(5).sort_values("delta_revenue", ascending=True).replace([np.inf, -np.inf], np.nan).to_dict(orient="records")

    delta_rev = cur_revenue - prev_revenue
    abs_delta = abs(delta_rev)
    driver_txt_map = {
        "price_effect": f"ASP/{basis_label} realization",
        "volume_effect": f"{basis_label} volume movement",
        "mix_effect": "customer mix/interaction",
    }
    dominant_txt = driver_txt_map.get(dominant_driver, "mixed factors")
    profit_delta = cur_profit - prev_profit
    health = "Healthy"
    if delta_rev >= 0 and (cur_margin or 0.0) >= (prev_margin or 0.0):
        health = "Healthy"
    elif delta_rev >= 0 and (cur_margin or 0.0) < (prev_margin or 0.0):
        health = "Mixed"
    else:
        health = "At risk"
    recommended_action = "Hold course and monitor core customers."
    if health == "At risk":
        recommended_action = "Review price execution, cadence, and at-risk customers before volume erosion deepens."
    elif dominant_driver == "price_effect" and delta_rev >= 0:
        recommended_action = "Protect realized ASP/lb while checking demand elasticity."
    elif dominant_driver == "volume_effect" and delta_rev < 0:
        recommended_action = "Prioritize customer recovery and order cadence before changing price."

    narrative = (
        f"Last 28 days revenue moved by ${abs_delta:,.0f} vs the prior 28 days. "
        f"The dominant driver was {dominant_txt}; gross profit changed by ${abs(profit_delta):,.0f} "
        f"and margin {'improved' if (cur_margin or 0.0) >= (prev_margin or 0.0) else 'compressed'}."
    )

    return {
        "current_window": {
            "start": current_start.date().isoformat(),
            "end": end_dt.date().isoformat(),
            "revenue": cur_revenue,
            "units": cur_units,
            "weight_lb": cur_weight,
            "profit": cur_profit,
            "avg_price": cur_price,
            "margin_pct": (cur_margin * 100.0) if cur_margin is not None else None,
            "profit_per_lb": cur_profit_per_lb,
        },
        "prior_window": {
            "start": prior_start.date().isoformat(),
            "end": prior_end.date().isoformat(),
            "revenue": prev_revenue,
            "units": prev_units,
            "weight_lb": prev_weight,
            "profit": prev_profit,
            "avg_price": prev_price,
            "margin_pct": (prev_margin * 100.0) if prev_margin is not None else None,
            "profit_per_lb": prev_profit_per_lb,
        },
        "decomposition": {
            "price_effect": price_effect,
            "volume_effect": volume_effect,
            "mix_effect": mix_effect,
            "dominant_driver": dominant_driver,
            "delta_revenue": delta_rev,
            "delta_profit": profit_delta,
            "basis_label": basis_label,
        },
        "health": health,
        "recommended_action": recommended_action,
        "top_gainers": top_gainers,
        "top_decliners": top_decliners,
        "narrative": narrative,
    }


def _price_volume_payload(rows_df: pd.DataFrame) -> Dict[str, Any]:
    if rows_df.empty:
        return {
            "points": [],
            "elasticity": {"status": "insufficient_variation"},
            "price_label": "ASP/lb",
            "demand_label": "Shipped lb",
            "demand_basis": "lb",
        }

    df = rows_df.copy()
    df["order_date"] = pd.to_datetime(df["order_date"], errors="coerce")
    df = df[df["order_date"].notna()]
    if df.empty:
        return {
            "points": [],
            "elasticity": {"status": "insufficient_variation"},
            "price_label": "ASP/lb",
            "demand_label": "Shipped lb",
            "demand_basis": "lb",
        }

    order_level = (
        df.groupby(["order_id", "order_date"], dropna=False)
        .agg(
            revenue=("revenue", "sum"),
            profit=("profit", "sum"),
            units=("units", "sum"),
            weight_lb=("weight_lb", "sum"),
        )
        .reset_index()
        .sort_values("order_date")
    )
    units_vals = order_level["units"].to_numpy(dtype=float)
    weight_vals = order_level["weight_lb"].to_numpy(dtype=float)
    revenue_vals = order_level["revenue"].to_numpy(dtype=float)
    profit_vals = pd.to_numeric(order_level.get("profit"), errors="coerce").fillna(0.0).to_numpy(dtype=float)

    price_lb_vals = np.full(revenue_vals.shape, np.nan, dtype=float)
    price_unit_vals = np.full(revenue_vals.shape, np.nan, dtype=float)
    margin_vals = np.full(revenue_vals.shape, np.nan, dtype=float)
    np.divide(revenue_vals, weight_vals, out=price_lb_vals, where=weight_vals > 0.0)
    np.divide(revenue_vals, units_vals, out=price_unit_vals, where=units_vals > 0.0)
    np.divide(profit_vals, revenue_vals, out=margin_vals, where=revenue_vals != 0.0)

    demand_basis = "lb" if np.nan_to_num(weight_vals).sum() > 0.0 else "units"
    price_vals = price_lb_vals if demand_basis == "lb" else price_unit_vals
    demand_vals = weight_vals if demand_basis == "lb" else units_vals
    order_level["price_lb"] = price_lb_vals
    order_level["price_unit"] = price_unit_vals
    order_level["plot_price"] = price_vals
    order_level["demand"] = demand_vals
    order_level["margin_pct"] = margin_vals * 100.0
    order_level = order_level.replace([np.inf, -np.inf], np.nan)
    order_level = order_level[order_level["plot_price"].notna() & order_level["demand"].notna()]

    if order_level.empty:
        return {
            "points": [],
            "elasticity": {"status": "insufficient_variation"},
            "price_label": "ASP/lb" if demand_basis == "lb" else "ASP/unit",
            "demand_label": "Shipped lb" if demand_basis == "lb" else "Units",
            "demand_basis": demand_basis,
        }

    if len(order_level.index) > 500:
        order_level = order_level.tail(500)

    points = [
        {
            "period": pd.to_datetime(row["order_date"]).date().isoformat(),
            "price_lb": _safe_float(row["price_lb"]),
            "price_unit": _safe_float(row["price_unit"]),
            "plot_price": _safe_float(row["plot_price"]),
            "demand": _safe_float(row["demand"]),
            "weight_lb": _safe_float(row["weight_lb"]),
            "units": _safe_float(row["units"]),
            "revenue": _safe_float(row["revenue"]),
            "margin_pct": _safe_float(row["margin_pct"]),
        }
        for _, row in order_level.iterrows()
    ]

    x = order_level["plot_price"].to_numpy(dtype=float)
    y = order_level["demand"].to_numpy(dtype=float)
    status = "ok"
    slope = None
    corr = None
    if len(x) < 8 or np.nanstd(x) <= 1e-9 or np.nanstd(y) <= 1e-9:
        status = "insufficient_variation"
    else:
        try:
            slope = float(np.polyfit(x, y, 1)[0])
            corr = float(np.corrcoef(x, y)[0, 1])
        except Exception:
            status = "insufficient_variation"
            slope = None
            corr = None

    return {
        "points": points,
        "price_label": "ASP/lb" if demand_basis == "lb" else "ASP/unit",
        "demand_label": "Shipped lb" if demand_basis == "lb" else "Units",
        "demand_basis": demand_basis,
        "elasticity": {
            "status": status,
            "slope": slope,
            "correlation": corr,
            "note": "Indicative only; simple linear proxy under current scoped filters.",
        },
    }


def _margin_risk_rows(customers_rows: List[Dict[str, Any]], target_margin_pct: float | None = None) -> List[Dict[str, Any]]:
    enriched: List[Dict[str, Any]] = []
    for row in customers_rows:
        if not isinstance(row, dict):
            continue
        row_target_margin_pct = _safe_float(row.get("target_margin_pct"))
        if row_target_margin_pct is None:
            row_target_margin_pct = _safe_float(target_margin_pct)
        revenue = _clean_num(row.get("revenue"))
        margin_pct = _safe_float(row.get("margin_pct"))
        gap_pp = (row_target_margin_pct - margin_pct) if margin_pct is not None and row_target_margin_pct is not None else None
        if gap_pp is not None and gap_pp <= 0:
            continue
        uplift = (revenue * max(0.0, gap_pp or 0.0) / 100.0) if margin_pct is not None else None
        out = dict(row)
        out["target_margin_pct"] = row_target_margin_pct
        out["target_gap_pp"] = gap_pp
        out["uplift_to_target"] = uplift
        status = margin_rules.classify_margin_status(margin_pct, row.get("minimum_margin_pct"), row_target_margin_pct)
        out["risk_tone"] = "risk" if status.get("status_key") in {"red", "orange"} else "warn"
        out["target_status"] = status.get("target_status")
        out["status_key"] = status.get("status_key")
        enriched.append(out)
    enriched.sort(key=lambda item: (_clean_num(item.get("uplift_to_target")), _clean_num(item.get("revenue"))), reverse=True)
    return enriched


def _margin_risk_summary(rows: List[Dict[str, Any]], total_revenue: float) -> Dict[str, Any]:
    if not rows:
        return {
            "row_count": 0,
            "revenue_exposure": 0.0,
            "revenue_exposure_pct": 0.0,
            "uplift_to_target": 0.0,
            "top_customer": None,
        }
    revenue_exposure = float(sum(_clean_num(row.get("revenue")) for row in rows))
    uplift_total = float(sum(_clean_num(row.get("uplift_to_target")) for row in rows))
    return {
        "row_count": len(rows),
        "revenue_exposure": revenue_exposure,
        "revenue_exposure_pct": (revenue_exposure / total_revenue * 100.0) if total_revenue > 0.0 else 0.0,
        "uplift_to_target": uplift_total,
        "top_customer": rows[0],
    }


def _smape(actual: np.ndarray, predicted: np.ndarray) -> float | None:
    if actual.size == 0 or predicted.size == 0:
        return None
    denom = np.abs(actual) + np.abs(predicted)
    if not np.any(denom):
        return 0.0
    value = np.mean(2 * np.abs(actual - predicted) / np.where(denom == 0, 1.0, denom))
    return float(value * 100.0)


def _forecast_damped_ma(values: np.ndarray, horizon: int, seasonality_period: int | None = None) -> Tuple[np.ndarray, float]:
    clean = np.asarray(values, dtype=float)
    n = clean.size
    if n == 0:
        return np.zeros(horizon, dtype=float), 0.0
    window = max(2, min(6, n))
    recent = clean[-window:]
    base = float(np.mean(recent))
    slope = 0.0
    if n >= 3:
        x = np.arange(window, dtype=float)
        y = recent
        try:
            slope = float(np.polyfit(x, y, 1)[0])
        except Exception:
            slope = float((recent[-1] - recent[0]) / max(1, window - 1))

    seasonal_component = np.zeros(horizon, dtype=float)
    if seasonality_period and n >= seasonality_period:
        season = clean[-seasonality_period:]
        season_center = float(np.mean(season))
        for step in range(horizon):
            seasonal_component[step] = float(season[step % seasonality_period] - season_center)

    damp = 0.85
    preds = np.zeros(horizon, dtype=float)
    for step in range(1, horizon + 1):
        trend_term = slope * step * (damp ** (step - 1))
        preds[step - 1] = base + trend_term + (0.35 * seasonal_component[step - 1])
    preds = np.maximum(preds, 0.0)

    fitted = np.empty(n, dtype=float)
    fitted[:] = np.nan
    if n >= window:
        for idx in range(window, n):
            hist = clean[:idx]
            local_window = hist[-window:]
            local_base = float(np.mean(local_window))
            local_slope = 0.0
            if local_window.size >= 3:
                lx = np.arange(local_window.size, dtype=float)
                try:
                    local_slope = float(np.polyfit(lx, local_window, 1)[0])
                except Exception:
                    local_slope = float((local_window[-1] - local_window[0]) / max(1, local_window.size - 1))
            fitted[idx] = max(0.0, local_base + local_slope * (damp ** 0))
    residuals = clean - np.nan_to_num(fitted, nan=np.nanmean(clean))
    resid_std = float(np.nanstd(residuals)) if residuals.size else 0.0
    return preds, resid_std


def _forecast_moving_average(values: np.ndarray, horizon: int) -> Tuple[np.ndarray, float]:
    clean = np.asarray(values, dtype=float)
    n = clean.size
    if n == 0:
        return np.zeros(horizon, dtype=float), 0.0
    window = max(1, min(3, n))
    base = float(np.mean(clean[-window:]))
    slope = float((clean[-1] - clean[0]) / max(1, n - 1)) if n >= 2 else 0.0
    preds = np.array([base + (0.25 * slope * step) for step in range(1, horizon + 1)], dtype=float)
    preds = np.maximum(preds, 0.0)
    resid_std = float(np.nanstd(np.diff(clean))) if n >= 3 else float(np.nanstd(clean))
    return preds, resid_std


def _forecast_ets(values: np.ndarray, horizon: int, freq: str) -> Tuple[np.ndarray, float]:
    clean = np.asarray(values, dtype=float)
    n = clean.size
    if n == 0:
        return np.zeros(horizon, dtype=float), 0.0
    try:
        from statsmodels.tsa.holtwinters import ExponentialSmoothing  # type: ignore
    except Exception:
        return _forecast_damped_ma(clean, horizon, seasonality_period=12 if freq == "month" else 13)

    seasonal_periods = 12 if freq == "month" else 13
    use_seasonality = n >= (2 * seasonal_periods)
    try:
        model = ExponentialSmoothing(
            clean,
            trend="add",
            damped_trend=True,
            seasonal="add" if use_seasonality else None,
            seasonal_periods=seasonal_periods if use_seasonality else None,
            initialization_method="estimated",
        )
        fit = model.fit(optimized=True)
        preds = np.asarray(fit.forecast(horizon), dtype=float)
        preds = np.maximum(preds, 0.0)
        fitted_vals = np.asarray(fit.fittedvalues, dtype=float)
        resid_std = float(np.nanstd(clean - fitted_vals)) if fitted_vals.size else 0.0
        return preds, resid_std
    except Exception:
        return _forecast_damped_ma(clean, horizon, seasonality_period=seasonal_periods if n >= seasonal_periods else None)


def _period_increment(period: pd.Timestamp, freq: str, step: int) -> pd.Timestamp:
    if freq == "week":
        return period + pd.Timedelta(days=7 * step)
    return period + pd.DateOffset(months=step)


def _period_label(period: pd.Timestamp, freq: str) -> str:
    if freq == "week":
        return period.strftime("%Y-%m-%d")
    return period.strftime("%Y-%m")


def _series_by_freq(rows_df: pd.DataFrame, freq: str) -> pd.DataFrame:
    if rows_df.empty:
        return pd.DataFrame(columns=["period_start", "period", "revenue", "units", "weight_lb", "profit"])
    df = rows_df.copy()
    df["order_date"] = pd.to_datetime(df["order_date"], errors="coerce")
    df = df[df["order_date"].notna()]
    if df.empty:
        return pd.DataFrame(columns=["period_start", "period", "revenue", "units", "weight_lb", "profit"])
    if "profit" not in df.columns:
        if "cost" in df.columns:
            df["profit"] = pd.to_numeric(df.get("revenue"), errors="coerce").fillna(0.0) - pd.to_numeric(df.get("cost"), errors="coerce").fillna(0.0)
        else:
            df["profit"] = 0.0

    if freq == "week":
        df["period_start"] = df["order_date"].dt.to_period("W-SUN").dt.start_time
    else:
        df["period_start"] = df["order_date"].dt.to_period("M").dt.to_timestamp()

    grouped = (
        df.groupby("period_start", dropna=False)
        .agg(revenue=("revenue", "sum"), units=("units", "sum"), weight_lb=("weight_lb", "sum"), profit=("profit", "sum"))
        .reset_index()
        .sort_values("period_start")
    )
    grouped["period"] = grouped["period_start"].apply(lambda v: _period_label(pd.Timestamp(v), freq))
    return grouped


def _forecast_metric(values: np.ndarray, freq: str, horizon: int) -> Dict[str, Any]:
    n = int(values.size)
    warnings: List[str] = []
    insufficient_history = False

    method = "moving_average_fallback"
    if n >= 18:
        method = "ets"
    elif n >= 8:
        method = "damped_ma"
    else:
        method = "moving_average_fallback"
        insufficient_history = True
        warnings.append("Limited history; using moving average fallback.")

    if method == "ets":
        preds, resid_std = _forecast_ets(values, horizon, freq)
    elif method == "damped_ma":
        preds, resid_std = _forecast_damped_ma(values, horizon, seasonality_period=12 if freq == "month" else 13)
    else:
        preds, resid_std = _forecast_moving_average(values, horizon)

    # Backtest on holdout when feasible.
    smape_val = None
    if n >= 6:
        test_size = min(horizon, max(2, n // 4))
        if n - test_size >= 3:
            train = values[:-test_size]
            actual = values[-test_size:]
            if method == "ets":
                test_preds, _ = _forecast_ets(train, test_size, freq)
            elif method == "damped_ma":
                test_preds, _ = _forecast_damped_ma(train, test_size, seasonality_period=12 if freq == "month" else 13)
            else:
                test_preds, _ = _forecast_moving_average(train, test_size)
            smape_val = _smape(np.asarray(actual, dtype=float), np.asarray(test_preds, dtype=float))

    band_base = max(float(resid_std or 0.0), float(np.nanstd(values) * 0.10 if n else 0.0))
    if not np.isfinite(band_base) or band_base < 0:
        band_base = 0.0
    lower = np.maximum(preds - np.maximum(band_base, preds * 0.12), 0.0)
    upper = np.maximum(preds + np.maximum(band_base, preds * 0.12), 0.0)

    return {
        "method": method,
        "insufficient_history": bool(insufficient_history),
        "warnings": warnings,
        "smape": smape_val,
        "yhat": np.maximum(preds, 0.0),
        "yhat_lower": lower,
        "yhat_upper": upper,
        "history_points": n,
    }


def _forecast_from_rows(rows_df: pd.DataFrame, freq: str = "month", horizon: int = 6) -> Dict[str, Any]:
    freq_norm = "week" if str(freq).strip().lower().startswith("week") else "month"
    horizon_n = int(horizon or (12 if freq_norm == "week" else 6))
    horizon_n = max(1, min(horizon_n, 26 if freq_norm == "week" else 12))
    series_df = _series_by_freq(rows_df, freq_norm)
    if series_df.empty:
        return {
            "actual_series": [],
            "forecast_series": [],
            "meta": {
                "method": "moving_average_fallback",
                "history_points": 0,
                "insufficient_history": True,
                "smape": None,
                "warnings": ["No history available under current filters."],
            },
        }

    revenue_vals = pd.to_numeric(series_df["revenue"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    units_vals = pd.to_numeric(series_df["units"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    weight_vals = pd.to_numeric(series_df["weight_lb"], errors="coerce").fillna(0.0).to_numpy(dtype=float)

    revenue_fc = _forecast_metric(revenue_vals, freq_norm, horizon_n)
    units_fc = _forecast_metric(units_vals, freq_norm, horizon_n)
    weight_fc = _forecast_metric(weight_vals, freq_norm, horizon_n)

    last_period = pd.Timestamp(series_df["period_start"].iloc[-1])
    periods = [_period_increment(last_period, freq_norm, i) for i in range(1, horizon_n + 1)]

    actual_series = [
        {
            "period": str(row["period"]),
            "value": _clean_num(row["revenue"]),
            "revenue": _clean_num(row["revenue"]),
            "units": _clean_num(row["units"]),
            "weight_lb": _clean_num(row["weight_lb"]),
            "profit": _clean_num(row["profit"]),
        }
        for _, row in series_df.iterrows()
    ]

    forecast_series: List[Dict[str, Any]] = []
    for idx, period in enumerate(periods):
        forecast_series.append(
            {
                "period": _period_label(period, freq_norm),
                "yhat": _clean_num(revenue_fc["yhat"][idx]),
                "yhat_lower": _clean_num(revenue_fc["yhat_lower"][idx]),
                "yhat_upper": _clean_num(revenue_fc["yhat_upper"][idx]),
                "revenue_yhat": _clean_num(revenue_fc["yhat"][idx]),
                "revenue_yhat_lower": _clean_num(revenue_fc["yhat_lower"][idx]),
                "revenue_yhat_upper": _clean_num(revenue_fc["yhat_upper"][idx]),
                "units_yhat": _clean_num(units_fc["yhat"][idx]),
                "units_yhat_lower": _clean_num(units_fc["yhat_lower"][idx]),
                "units_yhat_upper": _clean_num(units_fc["yhat_upper"][idx]),
                "weight_yhat": _clean_num(weight_fc["yhat"][idx]),
                "weight_yhat_lower": _clean_num(weight_fc["yhat_lower"][idx]),
                "weight_yhat_upper": _clean_num(weight_fc["yhat_upper"][idx]),
            }
        )

    warnings: List[str] = []
    warnings.extend(revenue_fc.get("warnings") or [])
    warnings.extend(units_fc.get("warnings") or [])
    warnings.extend(weight_fc.get("warnings") or [])

    coeff_var = float(np.nanstd(revenue_vals) / np.nanmean(revenue_vals)) if revenue_vals.size and np.nanmean(revenue_vals) > 0 else None
    forecastability_score = 72.0
    if revenue_fc.get("insufficient_history"):
        forecastability_score -= 22.0
    if revenue_fc.get("smape") is not None:
        forecastability_score -= min(35.0, _clean_num(revenue_fc.get("smape")) * 0.8)
    if coeff_var is not None:
        forecastability_score -= min(20.0, coeff_var * 20.0)
    if len(series_df.index) >= 18:
        forecastability_score += 8.0
    forecastability_score = _clamp(forecastability_score)
    confidence_label = _score_label(forecastability_score)
    summary = (
        f"{confidence_label} forecast confidence using {revenue_fc.get('method')} "
        f"with {int(revenue_fc.get('history_points') or 0)} history points."
    )

    return {
        "actual_series": actual_series,
        "forecast_series": forecast_series,
        "meta": {
            "method": revenue_fc.get("method"),
            "history_points": int(revenue_fc.get("history_points") or 0),
            "insufficient_history": bool(revenue_fc.get("insufficient_history")),
            "smape": revenue_fc.get("smape"),
            "smape_units": units_fc.get("smape"),
            "smape_weight": weight_fc.get("smape"),
            "warnings": warnings,
            "freq": freq_norm,
            "horizon": horizon_n,
            "train_start": actual_series[0]["period"] if actual_series else None,
            "train_end": actual_series[-1]["period"] if actual_series else None,
            "forecastability_score": forecastability_score,
            "confidence_label": confidence_label,
            "summary": summary,
        },
    }


def build_product_forecast_payload(
    product_id: str,
    filters: Any,
    current_user_obj: Any,
    *,
    freq: str = "month",
    horizon: int | None = None,
) -> Dict[str, Any]:
    if not _forecast_v1_enabled():
        return {
            "error": {"message": "Forecast feature disabled."},
            "actual_series": [],
            "forecast_series": [],
            "meta": {
                "sku": str(product_id),
                "method": "moving_average_fallback",
                "history_points": 0,
                "insufficient_history": True,
                "warnings": ["Forecast feature flag is disabled."],
            },
        }

    freq_norm = "week" if str(freq).strip().lower().startswith("week") else "month"
    horizon_n = int(horizon or (12 if freq_norm == "week" else 6))
    horizon_n = max(1, min(horizon_n, 26 if freq_norm == "week" else 12))

    scope = filters_service.scope_from_user(current_user_obj)
    dataset_version = str(fact_store.cache_buster())
    ttl_seconds = 600
    extras = {
        "sku": str(product_id),
        "user_id": _user_identifier(current_user_obj),
        "version": "forecast_v1",
        "freq": freq_norm,
        "horizon": horizon_n,
    }

    def _builder() -> Dict[str, Any]:
        access_policy.enforce_entity_access("products", product_id, access_policy.get_current_scope(use_cache=True))
        rows_df, start_iso, end_iso = _product_row_query(str(product_id), filters, scope)
        if rows_df.empty:
            return {
                "error": {"message": "Product not found for scoped filters."},
                "actual_series": [],
                "forecast_series": [],
                "meta": {"sku": str(product_id), "freq": freq_norm, "horizon": horizon_n},
            }
        payload = _forecast_from_rows(rows_df, freq=freq_norm, horizon=horizon_n)
        meta = payload.setdefault("meta", {})
        meta["sku"] = str(product_id)
        meta["window_start"] = start_iso
        meta["window_end"] = end_iso
        meta["dataset_version"] = dataset_version
        meta["scope_hash"] = _scope_hash(scope)
        meta["user_id"] = _user_identifier(current_user_obj)
        meta["generated_at"] = datetime.now(timezone.utc).isoformat()
        return payload

    return cached_bundle(
        endpoint="products.drilldown.v2.forecast",
        filters=_normalized_filters(filters),
        scope=scope,
        dataset_version=dataset_version,
        extras=extras,
        ttl_seconds=ttl_seconds,
        builder=_builder,
    )


def _build_kpis(
    rows_df: pd.DataFrame,
    monthly_rows: List[Dict[str, Any]],
    quality: Dict[str, Any],
    *,
    window_days: float,
    scope_totals: Dict[str, float],
    distributions: Dict[str, Any],
    end_iso: str | None,
) -> Dict[str, Any]:
    if rows_df.empty:
        return {}

    revenue = float(pd.to_numeric(rows_df["revenue"], errors="coerce").fillna(0.0).sum())
    base_cost_series = pd.to_numeric(rows_df.get("base_cost"), errors="coerce")
    cost = float(pd.to_numeric(rows_df["cost"], errors="coerce").sum())
    # Profit should follow the row-level cost coverage rules instead of assuming missing costs are zero.
    profit = float(pd.to_numeric(rows_df["profit"], errors="coerce").sum())
    units = float(pd.to_numeric(rows_df["units"], errors="coerce").fillna(0.0).sum())
    weight_lb = float(pd.to_numeric(rows_df["weight_lb"], errors="coerce").fillna(0.0).sum())
    orders = int(rows_df["order_id"].nunique())
    customers = int(rows_df["customer_id"].nunique())
    margin_pct = (profit / revenue * 100.0) if revenue else None
    asp = (revenue / units) if units else None
    asp_lb = (revenue / weight_lb) if weight_lb else None
    base_cost = float(base_cost_series.sum()) if base_cost_series.notna().any() else None
    cost_per_lb = (cost / weight_lb) if weight_lb and cost is not None else None
    base_cost_per_lb = (base_cost / weight_lb) if weight_lb and base_cost is not None else None
    profit_per_lb = (profit / weight_lb) if weight_lb else None
    rev_per_customer = (revenue / customers) if customers else None

    first_sold = _as_date_string(rows_df["order_date"].min())
    last_sold = _as_date_string(rows_df["order_date"].max())
    end_dt = _reference_end_date(rows_df, end_iso)
    last_sold_days_ago = None
    if end_dt is not None and last_sold is not None:
        last_dt = pd.to_datetime(last_sold, errors="coerce")
        if pd.notna(last_dt):
            last_sold_days_ago = int((end_dt.normalize() - pd.Timestamp(last_dt).normalize()).days)

    mom = {"revenue_delta": None, "revenue_delta_pct": None, "units_delta": None, "margin_delta_pp": None}
    if len(monthly_rows) >= 2:
        cur = monthly_rows[-1]
        prev = monthly_rows[-2]
        cur_revenue = _clean_num(cur.get("revenue"))
        prev_revenue = _clean_num(prev.get("revenue"))
        cur_units = _clean_num(cur.get("units"))
        prev_units = _clean_num(prev.get("units"))
        cur_margin = _safe_float(cur.get("margin_pct"))
        prev_margin = _safe_float(prev.get("margin_pct"))
        mom["revenue_delta"] = cur_revenue - prev_revenue
        if prev_revenue > 0:
            mom["revenue_delta_pct"] = ((cur_revenue - prev_revenue) / prev_revenue) * 100.0
        mom["units_delta"] = cur_units - prev_units
        if cur_margin is not None and prev_margin is not None:
            mom["margin_delta_pp"] = cur_margin - prev_margin

    window_weeks = max(window_days / 7.0, 1.0)
    velocity_units_week = units / window_weeks
    velocity_weight_week = weight_lb / window_weeks

    scope_revenue = _clean_num(scope_totals.get("total_revenue"))
    scope_customers = _clean_num(scope_totals.get("active_customers"))
    contribution_share_pct = (revenue / scope_revenue * 100.0) if scope_revenue > 0 else None
    customer_penetration_pct = (customers / scope_customers * 100.0) if scope_customers > 0 else None

    price_payload = distributions.get("price") if isinstance(distributions, dict) else {}
    price_p10 = _safe_float((price_payload or {}).get("p10"))
    price_p50 = _safe_float((price_payload or {}).get("p50"))
    price_p90 = _safe_float((price_payload or {}).get("p90"))

    delta_28 = {
        "revenue_delta": None,
        "revenue_delta_pct": None,
        "units_delta": None,
        "units_delta_pct": None,
        "weight_delta": None,
        "weight_delta_pct": None,
        "margin_delta_pp": None,
        "current_revenue": None,
        "prior_revenue": None,
        "current_units": None,
        "prior_units": None,
        "current_weight_lb": None,
        "prior_weight_lb": None,
        "current_margin_pct": None,
        "prior_margin_pct": None,
    }
    if end_dt is not None:
        df = rows_df.copy()
        df["order_date"] = pd.to_datetime(df["order_date"], errors="coerce")
        df = df[df["order_date"].notna()]
        if not df.empty:
            cur_start = end_dt - pd.Timedelta(days=27)
            prev_end = cur_start - pd.Timedelta(days=1)
            prev_start = prev_end - pd.Timedelta(days=27)
            cur_rows = df[(df["order_date"] >= cur_start) & (df["order_date"] <= end_dt)]
            prev_rows = df[(df["order_date"] >= prev_start) & (df["order_date"] <= prev_end)]

            cur_rev = _clean_num(cur_rows["revenue"].sum())
            prev_rev = _clean_num(prev_rows["revenue"].sum())
            cur_units = _clean_num(cur_rows["units"].sum())
            prev_units = _clean_num(prev_rows["units"].sum())
            cur_weight = _clean_num(cur_rows["weight_lb"].sum())
            prev_weight = _clean_num(prev_rows["weight_lb"].sum())

            cur_profit_series = pd.to_numeric(cur_rows["profit"], errors="coerce")
            prev_profit_series = pd.to_numeric(prev_rows["profit"], errors="coerce")
            cur_profit = float(cur_profit_series.sum()) if cur_profit_series.notna().any() else None
            prev_profit = float(prev_profit_series.sum()) if prev_profit_series.notna().any() else None
            cur_margin = (cur_profit / cur_rev * 100.0) if (cur_profit is not None and cur_rev > 0) else None
            prev_margin = (prev_profit / prev_rev * 100.0) if (prev_profit is not None and prev_rev > 0) else None

            delta_28["current_revenue"] = cur_rev
            delta_28["prior_revenue"] = prev_rev
            delta_28["current_units"] = cur_units
            delta_28["prior_units"] = prev_units
            delta_28["current_weight_lb"] = cur_weight
            delta_28["prior_weight_lb"] = prev_weight
            delta_28["revenue_delta"] = cur_rev - prev_rev
            delta_28["units_delta"] = cur_units - prev_units
            delta_28["weight_delta"] = cur_weight - prev_weight
            if prev_rev > 0:
                delta_28["revenue_delta_pct"] = ((cur_rev - prev_rev) / prev_rev) * 100.0
            if prev_units > 0:
                delta_28["units_delta_pct"] = ((cur_units - prev_units) / prev_units) * 100.0
            if prev_weight > 0:
                delta_28["weight_delta_pct"] = ((cur_weight - prev_weight) / prev_weight) * 100.0
            delta_28["current_margin_pct"] = cur_margin
            delta_28["prior_margin_pct"] = prev_margin
            if cur_margin is not None and prev_margin is not None:
                delta_28["margin_delta_pp"] = cur_margin - prev_margin

    price_outlier = distributions.get("price_outlier") if isinstance(distributions, dict) else {}
    price_dispersion_ratio = _safe_div(price_p90, price_p10)

    return {
        "revenue": revenue,
        "base_cost": base_cost,
        "cost": cost,
        "effective_cost": cost,
        "profit": profit,
        "gross_margin_value": profit,
        "margin_pct": margin_pct,
        "orders": orders,
        "customers": customers,
        "units": units,
        "weight_lb": weight_lb,
        "asp": asp,
        "asp_lb": asp_lb,
        "base_cost_per_lb": base_cost_per_lb,
        "cost_per_lb": cost_per_lb,
        "effective_cost_per_lb": cost_per_lb,
        "revenue_per_lb": asp_lb,
        "profit_per_lb": profit_per_lb,
        "revenue_per_customer": rev_per_customer,
        "avg_units_per_order": _safe_div(units, orders),
        "avg_weight_per_order": _safe_div(weight_lb, orders),
        "avg_weight_per_customer": _safe_div(weight_lb, customers),
        "avg_weight_per_unit": _safe_div(weight_lb, units),
        "contribution_share_pct": contribution_share_pct,
        "velocity_units_week": velocity_units_week,
        "velocity_weight_week": velocity_weight_week,
        "customer_penetration_pct": customer_penetration_pct,
        "price_p10": price_p10,
        "price_p50": price_p50,
        "price_p90": price_p90,
        "price_dispersion_ratio": price_dispersion_ratio,
        "price_outlier_revenue_share_pct": _safe_float((price_outlier or {}).get("revenue_share_pct")),
        "delta_28d": delta_28,
        "first_sold": first_sold,
        "last_sold": last_sold,
        "last_sold_days_ago": last_sold_days_ago,
        "mom": mom,
        "cost_coverage_pct": quality.get("cost_coverage_pct"),
    }


def _trend_plot_payload(monthly_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    labels = [str(row.get("month_label") or row.get("month")) for row in monthly_rows]
    months = [str(row.get("month")) for row in monthly_rows]
    return {
        "labels": labels,
        "months": months,
        "revenue": [_clean_num(row.get("revenue")) for row in monthly_rows],
        "units": [_clean_num(row.get("units")) for row in monthly_rows],
        "profit": [_clean_num(row.get("profit")) for row in monthly_rows],
        "cost": [_clean_num(row.get("cost")) for row in monthly_rows],
        "margin_pct": [_safe_float(row.get("margin_pct")) for row in monthly_rows],
        "asp": [_safe_float(row.get("asp")) for row in monthly_rows],
        "asp_lb": [_safe_float(row.get("asp_lb")) for row in monthly_rows],
        "weight_lb": [_clean_num(row.get("weight_lb")) for row in monthly_rows],
        "profit_per_lb": [_safe_float(row.get("profit_per_lb")) for row in monthly_rows],
        "weight_per_unit": [_safe_float(row.get("weight_per_unit")) for row in monthly_rows],
        "revenue_ma3": [_safe_float(row.get("revenue_ma3")) for row in monthly_rows],
        "weight_lb_ma3": [_safe_float(row.get("weight_lb_ma3")) for row in monthly_rows],
        "asp_lb_ma3": [_safe_float(row.get("asp_lb_ma3")) for row in monthly_rows],
    }


def _base_drilldown_payload(product_id: str, filters: Any, scope: Dict[str, Any], args: Any | None = None) -> Dict[str, Any]:
    safe_args = args or {}
    try:
        return products_bundle.build_products_drilldown(product_id, filters, scope, safe_args)
    except Exception:
        return {}


def _should_show_forecast(base_payload: Dict[str, Any], monthly_rows: List[Dict[str, Any]]) -> tuple[bool, str | None]:
    if not _forecast_v1_enabled():
        return False, None
    history_months = len(monthly_rows)
    forecast_points = (base_payload.get("forecast") or {}).get("forecast") or []
    if history_months < 8:
        return False, "Forecast uses fallback due limited history."
    if not forecast_points:
        return False, "Forecast model returned no projection rows."
    return True, None


def _context_builder(product_id: str, filters: Any, current_user_obj: Any) -> Dict[str, Any]:
    # Entity access + RBAC scope check with the same helper used across bundles.
    access_policy.enforce_entity_access("products", product_id, access_policy.get_current_scope(use_cache=True))
    scope = filters_service.scope_from_user(current_user_obj)
    rows_df, start_iso, end_iso = _product_row_query(product_id, filters, scope)
    if rows_df.empty:
        return {
            "error": {"message": "Product not found for scoped filters."},
            "meta": {"sku": product_id, "page_id": "product_drilldown_v2"},
        }

    time_series = build_time_series(rows_df)
    monthly_rows = time_series.get("monthly") or []
    distributions = build_distributions(rows_df)
    quality = build_quality_flags(rows_df)
    customers = build_customer_breakdowns(rows_df)
    regions = _group_breakdown(rows_df, "region", "Region")
    suppliers = _group_breakdown(rows_df, "supplier", "Supplier")
    ship_methods = _group_breakdown(rows_df, "ship_method", "Ship Method")
    basket = build_basket_affinity(product_id, filters, current_user_obj)
    scope_totals = _scope_window_totals(filters, scope)
    family_context = _family_peer_context(product_id, filters, scope, rows_df)
    kpis = _build_kpis(
        rows_df,
        monthly_rows,
        quality,
        window_days=_window_days(start_iso, end_iso, rows_df),
        scope_totals=scope_totals,
        distributions=distributions,
        end_iso=end_iso,
    )
    peer_asp_lb = _safe_float(family_context.get("peer_asp_lb"))
    peer_margin_pct = _safe_float(family_context.get("peer_margin_pct"))
    if peer_asp_lb is not None and kpis.get("asp_lb") is not None:
        family_context["asp_vs_peer_pct"] = ((float(kpis.get("asp_lb")) - peer_asp_lb) / peer_asp_lb * 100.0) if peer_asp_lb else None
    else:
        family_context["asp_vs_peer_pct"] = None
    if peer_margin_pct is not None and kpis.get("margin_pct") is not None:
        family_context["margin_vs_peer_pp"] = float(kpis.get("margin_pct")) - peer_margin_pct
    else:
        family_context["margin_vs_peer_pp"] = None
    margin_profile = margin_rules.evaluate_margin_record(
        protein=family_context.get("protein_family"),
        category=family_context.get("product_category"),
        revenue=kpis.get("revenue"),
        cost=kpis.get("cost"),
        profit=kpis.get("profit"),
        margin_pct=kpis.get("margin_pct"),
        unit_cost=kpis.get("cost_per_lb"),
        unit_price=kpis.get("asp_lb"),
        basis_qty=kpis.get("weight_lb") or kpis.get("units"),
        weight_lb=kpis.get("weight_lb"),
        qty=kpis.get("units"),
        base_cost=kpis.get("base_cost"),
        base_unit_cost=kpis.get("base_cost_per_lb"),
    )
    family_context.update(
        {
            "rule_family": margin_profile.get("display_family"),
            "minimum_margin_pct": margin_profile.get("minimum_margin_pct"),
            "target_margin_pct": margin_profile.get("target_margin_pct"),
            "min_product_margin_pct": margin_profile.get("min_product_margin_pct"),
            "target_product_margin_pct": margin_profile.get("target_product_margin_pct"),
            "base_cost_per_lb": kpis.get("base_cost_per_lb"),
            "effective_cost_per_lb": kpis.get("cost_per_lb"),
            "cost_per_lb": kpis.get("cost_per_lb"),
            "current_price_lb": margin_profile.get("current_price"),
            "minimum_price_lb": margin_profile.get("minimum_price"),
            "target_price_lb": margin_profile.get("target_price"),
            "asp_lb_gap_to_min": margin_profile.get("min_price_gap"),
            "asp_lb_gap_to_target": margin_profile.get("target_price_gap"),
            "target_achievement_pct": margin_profile.get("target_achievement_pct"),
            "target_gap_pct_points": margin_profile.get("target_gap_pct_points"),
            "minimum_gap_pct_points": margin_profile.get("minimum_gap_pct_points"),
            "target_status": margin_profile.get("target_status"),
            "status_key": margin_profile.get("status_key"),
            "status_tone": margin_profile.get("status_tone"),
        }
    )
    performance_story = _performance_story(rows_df, end_iso)
    price_volume = _price_volume_payload(rows_df)
    customer_rows = [dict(row) for row in (customers.get("rows") or []) if isinstance(row, dict)]
    for customer_row in customer_rows:
        customer_row["target_margin_pct"] = margin_profile.get("target_margin_pct")
        customer_row["minimum_margin_pct"] = margin_profile.get("minimum_margin_pct")
    customers["rows"] = customer_rows
    margin_risk_rows = _margin_risk_rows(customer_rows, target_margin_pct=_safe_float(margin_profile.get("target_margin_pct")))
    margin_risk_summary = _margin_risk_summary(margin_risk_rows, _clean_num(kpis.get("revenue")))
    margin_risk = {
        "target_margin_pct": margin_profile.get("target_margin_pct"),
        "minimum_margin_pct": margin_profile.get("minimum_margin_pct"),
        "target_status": margin_profile.get("target_status"),
        "status_key": margin_profile.get("status_key"),
        "rows": margin_risk_rows,
        "summary": margin_risk_summary,
    }

    base_payload = _base_drilldown_payload(product_id, filters, scope, args={})
    classification = base_payload.get("classification") or {}
    lifecycle = base_payload.get("lifecycle") or {}
    sku_value = _coalesce_text(rows_df, "sku", fallback=product_id).iloc[0]
    raw_product_name = _coalesce_text(rows_df, "product_name", "display_name", "sku", fallback=product_id).iloc[0]
    product_name = presentation.format_product_label(sku_value, raw_product_name, fallback=str(product_id))

    forecast_feature_enabled = _forecast_v1_enabled()
    forecast_payload = _forecast_from_rows(rows_df, freq="month", horizon=6) if forecast_feature_enabled else {}
    forecast_note = None
    if forecast_feature_enabled and isinstance(forecast_payload, dict):
        f_meta = forecast_payload.get("meta") or {}
        if f_meta.get("insufficient_history"):
            warnings = f_meta.get("warnings") or []
            forecast_note = warnings[0] if warnings else "Forecast uses fallback due limited history."

    trend = _trend_plot_payload(monthly_rows)
    weight_analytics = _build_weight_analytics(rows_df, monthly_rows, customers)
    lifecycle_insights = _build_lifecycle_insights(rows_df, monthly_rows, lifecycle, customers)
    seasonality = time_series.get("seasonality") or {}
    risk_opportunity = _build_risk_opportunity(
        total_revenue=_clean_num(kpis.get("revenue")),
        quality=quality,
        customers=customers,
        lifecycle_insights=lifecycle_insights,
        seasonality=seasonality,
        distributions=distributions,
        basket=basket,
        forecast=forecast_payload,
        margin_risk_summary=margin_risk_summary,
        weight_analytics=weight_analytics,
        delta_28=kpis.get("delta_28d") or {},
    )
    decision_panel = _build_decision_panel(
        risk_opportunity=risk_opportunity,
        performance_story=performance_story,
        lifecycle_insights=lifecycle_insights,
        customers=customers,
    )

    weekly_rows = time_series.get("weekly") or []
    weekly_df = pd.DataFrame.from_records(weekly_rows)
    stability_index = None
    if len(weekly_df.index) >= 6 and pd.to_numeric(weekly_df.get("revenue"), errors="coerce").fillna(0.0).mean() > 0:
        weekly_revenue = pd.to_numeric(weekly_df.get("revenue"), errors="coerce").fillna(0.0)
        stability_index = _clamp(100.0 - ((weekly_revenue.std(ddof=0) / weekly_revenue.mean()) * 100.0))

    kpis.update(
        {
            "repeat_customer_share_pct": customers.get("repeat_customer_share_pct"),
            "new_customer_share_pct": customers.get("new_customer_share_pct"),
            "current_28d_repeat_revenue_share_pct": customers.get("current_28d_repeat_revenue_share_pct"),
            "cadence_days": customers.get("cadence_median_days"),
            "demand_stability_index": stability_index,
            "seasonality_strength_score": seasonality.get("strength_score"),
            "seasonality_best_months": seasonality.get("best_months"),
            "seasonality_worst_months": seasonality.get("worst_months"),
            "forecastability_score": (forecast_payload.get("meta") or {}).get("forecastability_score"),
            "forecast_confidence_label": (forecast_payload.get("meta") or {}).get("confidence_label"),
            "lifecycle_stage": lifecycle_insights.get("stage"),
            "lifecycle_score": lifecycle_insights.get("stage_score"),
            "trend_direction": lifecycle_insights.get("trend_direction"),
            "dependency_score": _clamp(((customers.get("concentration") or {}).get("revenue") or {}).get("top5_share_pct")),
            "margin_risk_exposure_pct": margin_risk_summary.get("revenue_exposure_pct"),
            "margin_uplift_to_target": margin_risk_summary.get("uplift_to_target"),
            "target_margin_pct": margin_profile.get("target_margin_pct"),
            "minimum_margin_pct": margin_profile.get("minimum_margin_pct"),
            "cost_per_lb": kpis.get("cost_per_lb"),
            "minimum_price_lb": margin_profile.get("minimum_price"),
            "target_price_lb": margin_profile.get("target_price"),
            "asp_lb_gap_to_min": margin_profile.get("min_price_gap"),
            "asp_lb_gap_to_target": margin_profile.get("target_price_gap"),
            "target_achievement_pct": margin_profile.get("target_achievement_pct"),
            "target_gap_pct_points": margin_profile.get("target_gap_pct_points"),
            "minimum_gap_pct_points": margin_profile.get("minimum_gap_pct_points"),
            "target_status": margin_profile.get("target_status"),
            "data_confidence_score": risk_opportunity.get("data_confidence_score"),
            "data_confidence_label": risk_opportunity.get("data_confidence_label"),
        }
    )

    header_summary = {
        "posture": risk_opportunity.get("posture"),
        "primary_risk": (risk_opportunity.get("primary_risk") or {}).get("title"),
        "primary_risk_detail": (risk_opportunity.get("primary_risk") or {}).get("detail"),
        "primary_opportunity": (risk_opportunity.get("primary_opportunity") or {}).get("title"),
        "primary_opportunity_detail": (risk_opportunity.get("primary_opportunity") or {}).get("detail"),
        "data_confidence_label": risk_opportunity.get("data_confidence_label"),
        "data_confidence_score": risk_opportunity.get("data_confidence_score"),
        "target_status": margin_profile.get("target_status"),
        "status_key": margin_profile.get("status_key"),
    }
    seasonality_export_rows: List[Dict[str, Any]] = []
    seasonality_years = (time_series.get("seasonality") or {}).get("years") or []
    seasonality_matrix = (time_series.get("seasonality") or {}).get("matrix") or []
    seasonality_weight_matrix = (time_series.get("seasonality") or {}).get("weight_matrix") or []
    seasonality_months = (time_series.get("seasonality") or {}).get("months") or []
    for idx, year in enumerate(seasonality_years):
        revenue_row = seasonality_matrix[idx] if idx < len(seasonality_matrix) else []
        weight_row = seasonality_weight_matrix[idx] if idx < len(seasonality_weight_matrix) else []
        for month, revenue_value, weight_value in zip(seasonality_months, revenue_row, weight_row):
            seasonality_export_rows.append(
                {
                    "year": year,
                    "month": month,
                    "revenue": revenue_value,
                    "weight_lb": weight_value,
                }
            )
    seasonality_export_rows.extend(
        [
            {
                "year": "profile",
                "month": row.get("month_name"),
                "revenue": row.get("avg_revenue"),
                "weight_lb": row.get("avg_weight_lb"),
                "revenue_index": row.get("revenue_index"),
                "weight_index": row.get("weight_index"),
                "avg_margin_pct": row.get("avg_margin_pct"),
            }
            for row in ((time_series.get("seasonality") or {}).get("profile") or [])
        ]
    )
    meta = {
        "page_id": "product_drilldown_v2",
        "sku": str(sku_value or product_id),
        "product_name": str(raw_product_name),
        "product_display_label": str(product_name),
        "product_axis_label": presentation.compact_product_label(sku_value, raw_product_name, max_length=42, fallback=str(product_id)),
        "primary_price_metric_label": "ASP/lb",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset_version": str(fact_store.cache_buster()),
        "window_start": start_iso,
        "window_end": end_iso,
        "scope_hash": _scope_hash(scope),
        "user_id": _user_identifier(current_user_obj),
    }

    return {
        "meta": meta,
        "kpis": kpis,
        "quality": quality,
        "trend": trend,
        "time_series": time_series,
        "distributions": distributions,
        "customers": customers,
        "regions": regions,
        "suppliers": suppliers,
        "ship_methods": ship_methods,
        "family_context": family_context,
        "basket": basket,
        "classification": classification,
        "lifecycle": lifecycle,
        "lifecycle_insights": lifecycle_insights,
        "performance_story": performance_story,
        "price_volume": price_volume,
        "margin_profile": margin_profile,
        "margin_risk": margin_risk,
        "weight_analytics": weight_analytics,
        "decision_panel": decision_panel,
        "risk_opportunity": risk_opportunity,
        "header_summary": header_summary,
        "forecast_feature_enabled": bool(forecast_feature_enabled),
        "forecast_enabled": bool(forecast_feature_enabled),
        "forecast_note": forecast_note,
        "forecast": forecast_payload,
        "datasets": {
            "kpis": [kpis],
            "monthly_series": monthly_rows,
            "customers": customers.get("rows") or [],
            "regions": regions.get("rows") or [],
            "suppliers": suppliers.get("rows") or [],
            "ship_methods": ship_methods.get("rows") or [],
            "basket": basket.get("rows") or [],
            "forecast": (
                [
                    {
                        "row_type": "actual",
                        "period": row.get("period"),
                        "value": row.get("value"),
                        "revenue": row.get("revenue"),
                        "units": row.get("units"),
                        "weight_lb": row.get("weight_lb"),
                        "profit": row.get("profit"),
                    }
                    for row in (forecast_payload.get("actual_series") or [])
                ]
                + [
                    {
                        "row_type": "forecast",
                        "period": row.get("period"),
                        "yhat": row.get("yhat"),
                        "yhat_lower": row.get("yhat_lower"),
                        "yhat_upper": row.get("yhat_upper"),
                        "revenue_yhat": row.get("revenue_yhat"),
                        "units_yhat": row.get("units_yhat"),
                        "weight_yhat": row.get("weight_yhat"),
                    }
                    for row in (forecast_payload.get("forecast_series") or [])
                ]
            ),
            "seasonality": seasonality_export_rows,
        },
    }


def build_product_drilldown_context(product_id: str, filters: Any, current_user_obj: Any) -> Dict[str, Any]:
    scope = filters_service.scope_from_user(current_user_obj)
    dataset_version = str(fact_store.cache_buster())
    ttl_seconds = 600
    extras = {
        "sku": str(product_id),
        "user_id": _user_identifier(current_user_obj),
        "version": "v2",
        "forecast_v1": bool(_forecast_v1_enabled()),
    }
    return cached_bundle(
        endpoint="products.drilldown.v2.context",
        filters=_normalized_filters(filters),
        scope=scope,
        dataset_version=dataset_version,
        extras=extras,
        ttl_seconds=ttl_seconds,
        builder=lambda: _context_builder(str(product_id), _normalized_filters(filters), current_user_obj),
    )


def build_export_dataset(context: Dict[str, Any], kind: str) -> Tuple[pd.DataFrame, pd.DataFrame, str]:
    datasets = context.get("datasets") or {}
    kind_map = {
        "kpis": ("kpis", "product_drilldown_kpis"),
        "monthly_series": ("monthly_series", "product_drilldown_monthly_series"),
        "customers": ("customers", "product_drilldown_customers"),
        "regions": ("regions", "product_drilldown_regions"),
        "suppliers": ("suppliers", "product_drilldown_suppliers"),
        "ship_methods": ("ship_methods", "product_drilldown_ship_methods"),
        "basket": ("basket", "product_drilldown_basket"),
        "seasonality": ("seasonality", "product_drilldown_seasonality"),
        "forecast": ("forecast", "product_drilldown_forecast"),
    }
    dataset_key, stem = kind_map.get(kind, ("monthly_series", "product_drilldown_monthly_series"))
    rows = datasets.get(dataset_key) or []
    data_df = pd.DataFrame.from_records(rows)
    meta_payload = context.get("meta") or {}
    metadata = [
        {"field": "sku", "value": meta_payload.get("sku")},
        {"field": "window_start", "value": meta_payload.get("window_start")},
        {"field": "window_end", "value": meta_payload.get("window_end")},
        {"field": "dataset_version", "value": meta_payload.get("dataset_version")},
        {"field": "generated_at", "value": datetime.now(timezone.utc).isoformat()},
        {"field": "user_id", "value": meta_payload.get("user_id")},
        {"field": "scope_hash", "value": meta_payload.get("scope_hash")},
    ]
    metadata_df = pd.DataFrame(metadata)
    return data_df, metadata_df, stem

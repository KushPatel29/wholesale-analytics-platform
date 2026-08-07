from __future__ import annotations

import os
from typing import Any, Dict
from urllib.parse import urlencode
import numpy as np
import pandas as pd
from flask import (
    abort,
    Blueprint,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required
from werkzeug.datastructures import MultiDict

from app.blueprints.overview import (
    _clamp_params_to_data_window,
    _query_filter_params,
)
from app.services.filters import (
    apply_filters as apply_filter_params,
    resolve_filters,
)

from ..core.audit import log_audit
from ..core.data_service import get_fact_df
from ..core.exports import (
    dataframe_to_csv_response,
    dataframes_to_xlsx_response,
    sanitize_filename,
)
from ..core.json_sanitizer import sanitize_for_json
from ..core.filters import build_global_filter_form
from ..core.rbac import can_view_costs, has_permission, permission_required, requires_roles
from ..services import analytics_utils as au
from app.core.exceptions import DatasetNotBuiltError
from app.core.features import legacy_pandas_enabled
from app.services import bundle_service, filters_service
from app.services import fact_store
from app.services import customers_cohorts_v2
from app.services.bundle_cache import cached_bundle

bp = Blueprint("customers", __name__, url_prefix="/customers")


def _legacy_disabled_response():
    resp = jsonify({"error": {"message": "Legacy customers endpoints are disabled; use /api/customers/bundle."}})
    resp.status_code = 410
    return resp


class _EmptyForm:
    def hidden_tag(self):
        return ""


def _bundle_args_for_sections(*sections: str) -> MultiDict:
    bundle_args = MultiDict(request.args)
    bundle_args.poplist("_sections")
    normalized = [str(section).strip().lower() for section in sections if str(section).strip()]
    if normalized:
        bundle_args["_sections"] = ",".join(normalized)
    return bundle_args


def _empty_customers_bundle_payload(*sections: str) -> dict[str, Any]:
    normalized = [str(section).strip().lower() for section in sections if str(section).strip()]
    requested_sections = normalized or ["overview", "rfm", "clv", "cohorts"]
    warning = "Customer analytics data is temporarily unavailable. Filters remain available."
    return {
        "kpis": {},
        "table": {"rows": [], "page": 1, "page_size": 25, "total_rows": 0, "total_pages": 1},
        "charts": {},
        "drivers": {},
        "definitions": {},
        "health_strip": {"chips": [], "narrative": ""},
        "executive_scorecard": {},
        "executive_narrative": "",
        "churn_risk_summary": {},
        "clv": {},
        "rfm": {},
        "cohorts": {},
        "warnings": [warning],
        "meta": {
            "page": 1,
            "page_size": 25,
            "total_rows": 0,
            "total_pages": 1,
            "sections": requested_sections,
            "degraded": True,
            "bundle_unavailable": True,
        },
    }


def _customers_bundle_payload(*sections: str) -> dict[str, Any]:
    args = _bundle_args_for_sections(*sections)
    try:
        payload = bundle_service.bundle("customers", args)
    except DatasetNotBuiltError as exc:
        current_app.logger.warning(
            "customers.bundle.dataset_unavailable",
            extra={"sections": list(sections), "path": request.path, "error": str(exc)},
        )
        return _empty_customers_bundle_payload(*sections)
    except Exception as exc:
        current_app.logger.exception(
            "customers.bundle.page_failed",
            extra={"sections": list(sections), "path": request.path},
        )
        fallback = _empty_customers_bundle_payload(*sections)
        fallback.setdefault("meta", {})["error"] = str(exc)
        return fallback

    if not isinstance(payload, dict):
        current_app.logger.warning(
            "customers.bundle.invalid_payload",
            extra={"sections": list(sections), "path": request.path, "payload_type": type(payload).__name__},
        )
        return _empty_customers_bundle_payload(*sections)

    if payload.get("error"):
        message = str((payload.get("error") or {}).get("message") or "Customer analytics data is unavailable.")
        current_app.logger.warning(
            "customers.bundle.page_degraded",
            extra={"sections": list(sections), "path": request.path, "error": message},
        )
        fallback = _empty_customers_bundle_payload(*sections)
        fallback.setdefault("meta", {})["error"] = message
        return fallback

    return payload


@bp.before_request
def _block_legacy_customers():
    if legacy_pandas_enabled():
        return None
    # Allow bundle-backed endpoints; block any remaining legacy pandas-heavy routes
    allowed = {
        "customers.index",
        "customers.kpis",
        "customers.rfm",
        "customers.cohorts",
        "customers.cohorts_apply_controls",
        "customers.cohorts_drilldown",
        "customers.churned_list",
        "customers.cohorts_export",
        "customers.export",
        "customers.rfm_export_alias",
        "customers.clv_export_alias",
        "customers.clv",
        "customers.drilldown",
    }
    if request.endpoint in allowed:
        return None
    return _legacy_disabled_response()


def _customers_kpis_v2_enabled() -> bool:
    raw = current_app.config.get("CUSTOMERS_KPIS_V2")
    if raw is None:
        raw = os.getenv("CUSTOMERS_KPIS_V2", "0")
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _customers_kpis_v3_enabled() -> bool:
    raw = current_app.config.get("CUSTOMERS_KPIS_V3")
    if raw is None:
        raw = os.getenv("CUSTOMERS_KPIS_V3", "0")
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _customers_rfm_v2_enabled() -> bool:
    raw = current_app.config.get("CUSTOMERS_RFM_V2")
    if raw is None:
        raw = os.getenv("CUSTOMERS_RFM_V2", "0")
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _customers_clv_v2_enabled() -> bool:
    raw = current_app.config.get("CUSTOMERS_CLV_V2")
    if raw is None:
        raw = os.getenv("CUSTOMERS_CLV_V2", "0")
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _customer_drilldown_v2_enabled() -> bool:
    raw = current_app.config.get("CUSTOMER_DRILLDOWN_V2")
    if raw is None:
        raw = os.getenv("CUSTOMER_DRILLDOWN_V2", "0")
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}

# ─────────────────────────────────────────────────────────────────────────────
# Utilities (prod hardened)
# ─────────────────────────────────────────────────────────────────────────────

def _pick_date_series(df: pd.DataFrame) -> pd.Series:
    """Get date series using centralized utilities, ensuring valid data exists."""
    # Prioritize columns that are likely to be populated for all orders (Ordered > Shipped)
    candidates = [
        "Date",                 # Canonical date
        "DateOrdered_line",     # Line-level order date
        "DateOrdered_order",    # Header-level order date
        "OrderDate",            # Common alias
        "DateOrdered",          # Common alias
        "InvoiceDate",          # Invoiced
        "ShipDate",             # Shipped (might be null for open orders)
        "DateShipped_line",
        "DateShipped_order",
        "DateShipped"
    ]
    
    # Use utility that checks for non-null values
    s = au.pick_first_valid_date_column(df, candidates)
    if s is not None:
        return au.normalize_datetime(s)

    # Fallback: just try to resolve any column even if empty (to return the right shape)
    col = au.resolve_column(df, candidates)
    if col:
        return au.normalize_datetime(df[col])
        
    return pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")

def _rev_col(df: pd.DataFrame) -> str:
    """Get revenue column using centralized utilities."""
    return au.revenue_column(df) or "revenue_ordered"

def _cost_col(df: pd.DataFrame) -> str | None:
    """Get cost column using centralized utilities."""
    return au.cost_column(df)

def _mode_or_first(s: pd.Series) -> object:
    try:
        m = s.mode(dropna=True)
        if not m.empty:
            return m.iloc[0]
        return s.dropna().iloc[0]
    except Exception:
        return None

def _effective_qty(df: pd.DataFrame) -> pd.Series:
    """Get effective quantity using centralized utilities and fallbacks."""
    unit_id = au.to_numeric_safe(df.get("UnitOfBillingId"))
    by_weight = (unit_id == 3)

    weight_col = au.resolve_column(df, ("pack_weight_lb_sum", "WeightLb", "ShippedLb", "Lb"))
    weight = au.to_numeric_safe(df.get(weight_col)) if weight_col else pd.Series(0.0, index=df.index)

    qty_col = au.resolve_column(df, ("Qty", "Quantity", "Units", "QuantityShipped", "pack_item_count_sum"))
    items = au.to_numeric_safe(df.get(qty_col)) if qty_col else pd.Series(0.0, index=df.index)

    qty = np.where(by_weight, weight, items)
    qty = pd.to_numeric(qty, errors="coerce").replace(0, np.nan)
    return pd.Series(qty, index=df.index, dtype="float64")

def _round2_df(df: pd.DataFrame, cols: list[str] | None = None) -> pd.DataFrame:
    if df.empty:
        return df
    if cols is None:
        cols = df.select_dtypes(include=[np.number]).columns.tolist()
    if cols:
        df[cols] = df[cols].round(2)
    return df

def _round2_list(values: list[float]) -> list[float]:
    return [None if v is None else (round(float(v), 2) if pd.notna(v) else None) for v in values]

def _build_product_label(frame: pd.DataFrame) -> pd.Series:
    """
    Robust label used everywhere:
    SkuName → ProductName → SkuName → ProductId → 'Unknown'
    """
    def clean(s: pd.Series | None) -> pd.Series | None:
        if isinstance(s, pd.Series):
            s = s.astype("string").str.strip()
            return s.where(s.notna() & (s.str.len() > 0))
        return None

    label = None
    for col in ("SkuName", "SKUName", "ProductName", "product_name", "Name", "SkuName"):
        if col in frame.columns:
            s = clean(frame[col])
            if label is None:
                label = s
            else:
                label = label.fillna(s)

    if label is None:
        label = pd.Series(pd.NA, index=frame.index, dtype="string")

    if "ProductId" in frame.columns:
        label = label.fillna(frame["ProductId"].astype("string"))

    return label.fillna("Unknown")

# ─────────────────────────────────────────────────────────────────────────────
# Customer aggregation used by KPIs / CLV / Cohorts
# ─────────────────────────────────────────────────────────────────────────────

def _customer_agg(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or "CustomerId" not in df.columns:
        return pd.DataFrame(
            columns=[
                "CustomerId","CustomerName","TotalRevenue","TotalCost","Profit",
                "TotalOrders","FirstOrder","LastOrder","AvgOrderWt",
                "DaysSinceLastOrder","MonthsActive","RepeatRate",
            ]
        )

    # Use centralized utilities for column resolution and calculations
    rev_col = _rev_col(df)
    cost_col = _cost_col(df)
    name_col = au.customer_name_column(df)

    revenue_s = au.to_numeric_safe(df[rev_col])

    orders_per_cust = (
        df.groupby("CustomerId", observed=True)["OrderId"]
        .nunique(dropna=True)
        .rename("TotalOrders")
    )

    rev_per_cust = revenue_s.groupby(df["CustomerId"], observed=True).sum().rename("TotalRevenue")
    if cost_col and cost_col in df.columns:
        cost_s = au.to_numeric_safe(df[cost_col])
        cost_per_cust = cost_s.groupby(df["CustomerId"], observed=True).sum().rename("TotalCost")
    else:
        cost_per_cust = pd.Series(0.0, index=rev_per_cust.index, name="TotalCost")

    dates = _pick_date_series(df)
    first_order = dates.groupby(df["CustomerId"], observed=True).min().rename("FirstOrder")
    last_order = dates.groupby(df["CustomerId"], observed=True).max().rename("LastOrder")

    avg_order_wt = pd.Series(dtype="float64")
    weight_col = au.resolve_column(df, ("pack_weight_lb_sum", "WeightLb"))
    if weight_col and weight_col in df.columns:
        tmp = df[["OrderId", "CustomerId", weight_col]].copy()
        tmp[weight_col] = au.to_numeric_safe(tmp[weight_col])
        order_weights = (
            tmp.groupby(["OrderId", "CustomerId"], observed=True)[weight_col]
            .sum()
            .reset_index()
        )
        avg_order_wt = (
            order_weights.groupby("CustomerId", observed=True)[weight_col]
            .mean()
            .rename("AvgOrderWt")
        )

    ref_date = last_order.max()
    days_since_last = (ref_date - last_order).dt.days.rename("DaysSinceLastOrder")
    months_active = (
        (last_order.dt.year - first_order.dt.year) * 12
        + (last_order.dt.month - first_order.dt.month)
        + 1
    ).rename("MonthsActive")
    repeat_rate = (
        ((orders_per_cust - 1).clip(lower=0))
        / orders_per_cust.replace(0, pd.NA)
    ).fillna(0).rename("RepeatRate")

    parts = [
        orders_per_cust, rev_per_cust, cost_per_cust,
        first_order, last_order, days_since_last, months_active, repeat_rate,
    ]
    if not avg_order_wt.empty:
        parts.append(avg_order_wt)

    cust = pd.concat(parts, axis=1)
    cust["Profit"] = cust["TotalRevenue"] - cust["TotalCost"]

    if pd.notna(ref_date):
        last_30_cutoff = ref_date - pd.Timedelta(days=30)
        last_90_cutoff = ref_date - pd.Timedelta(days=90)
        prev_90_cutoff = ref_date - pd.Timedelta(days=180)

        mask_last_30 = dates >= last_30_cutoff
        mask_last_90 = dates >= last_90_cutoff
        mask_prev_90 = (dates < last_90_cutoff) & (dates >= prev_90_cutoff)

        rev_last_30 = (
            revenue_s.where(mask_last_30)
            .groupby(df["CustomerId"], observed=True)
            .sum()
            .rename("RevenueLast30")
        )
        rev_last_90 = (
            revenue_s.where(mask_last_90)
            .groupby(df["CustomerId"], observed=True)
            .sum()
            .rename("RevenueLast90")
        )
        rev_prev_90 = (
            revenue_s.where(mask_prev_90)
            .groupby(df["CustomerId"], observed=True)
            .sum()
            .rename("RevenuePrev90")
        )
        ord_last_30 = (
            df.loc[mask_last_30]
            .groupby("CustomerId", observed=True)["OrderId"]
            .nunique(dropna=True)
            .rename("OrdersLast30")
        )
        ord_last_90 = (
            df.loc[mask_last_90]
            .groupby("CustomerId", observed=True)["OrderId"]
            .nunique(dropna=True)
            .rename("OrdersLast90")
        )

        for series in (rev_last_30, rev_last_90, rev_prev_90, ord_last_30, ord_last_90):
            if series is not None:
                cust[series.name] = series

        with np.errstate(divide="ignore", invalid="ignore"):
            cust["RevenueTrend90"] = (
                (cust.get("RevenueLast90", 0.0) - cust.get("RevenuePrev90", 0.0))
                / cust.get("RevenuePrev90", 0.0)
            ) * 100.0
        cust["RevenueTrend90"] = cust["RevenueTrend90"].replace([np.inf, -np.inf], np.nan)
    else:
        cust["RevenueLast30"] = 0.0
        cust["RevenueLast90"] = 0.0
        cust["OrdersLast30"] = 0.0
        cust["OrdersLast90"] = 0.0
        cust["RevenueTrend90"] = np.nan

    if "RevenuePrev90" in cust.columns:
        cust = cust.drop(columns=["RevenuePrev90"])

    for col in ("RevenueLast30", "RevenueLast90"):
        cust[col] = pd.to_numeric(cust.get(col), errors="coerce").fillna(0.0)
    for col in ("OrdersLast30", "OrdersLast90"):
        cust[col] = pd.to_numeric(cust.get(col), errors="coerce").fillna(0.0)
    if "RevenueTrend90" in cust.columns:
        cust["RevenueTrend90"] = pd.to_numeric(cust["RevenueTrend90"], errors="coerce")
    else:
        cust["RevenueTrend90"] = np.nan

    if name_col:
        names = (
            df.dropna(subset=["CustomerId"])
            .drop_duplicates(subset=["CustomerId"])[["CustomerId", name_col]]
            .set_index("CustomerId")
            .rename(columns={name_col: "CustomerName"})
        )
        cust = cust.join(names, how="left")
    else:
        cust["CustomerName"] = None

    cust = cust.reset_index()
    ordered_cols = [
        "CustomerId","CustomerName","TotalRevenue","TotalCost","Profit","TotalOrders",
        "FirstOrder","LastOrder","AvgOrderWt","DaysSinceLastOrder","MonthsActive","RepeatRate",
        "RevenueLast90","RevenueLast30","OrdersLast90","OrdersLast30","RevenueTrend90",
    ]
    for c in ordered_cols:
        if c not in cust.columns:
            cust[c] = pd.NA

    cust = _round2_df(cust, cols=cust.select_dtypes(include=[np.number]).columns.tolist())
    return cust[ordered_cols]

def _data_quality(df: pd.DataFrame) -> dict[str, int]:
    out = {"missing_unit_cost": 0}
    if df is None or df.empty:
        return out
    if "unit_cost_effective" in df.columns and "QuantityShipped" in df.columns:
        unit_cost = pd.to_numeric(df["unit_cost_effective"], errors="coerce")
        qty_shipped = pd.to_numeric(df["QuantityShipped"], errors="coerce").fillna(0)
        mask = ((unit_cost.isna()) | (unit_cost <= 0)) & (qty_shipped > 0)
        out["missing_unit_cost"] = int(mask.sum())
    return out

def _attach_churn_features(
    cust_agg: pd.DataFrame,
    fact_df: pd.DataFrame,
) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    """
    Merge churn probability/risk scores into customer aggregate and provide ranked rows.
    """
    if cust_agg is None:
        cust_agg = pd.DataFrame()
    cust = cust_agg.copy()
    churn_rows: list[dict[str, object]] = []

    if cust.empty or fact_df is None or fact_df.empty:
        if "ChurnProbability" not in cust.columns:
            cust["ChurnProbability"] = np.nan
        if "ChurnRisk" not in cust.columns:
            cust["ChurnRisk"] = "Unknown"
        return cust, churn_rows

    from ..core import ml as churn_ml

    try:
        bundle = churn_ml.get_cached_churn_model()
    except Exception:
        bundle = None

    if not bundle:
        cust["ChurnProbability"] = np.nan
        cust["ChurnRisk"] = "Unknown"
        return cust, churn_rows

    try:
        scores = churn_ml.score_churn(fact_df, bundle)
    except Exception:
        scores = pd.DataFrame()

    cust["ChurnProbability"] = np.nan
    cust["ChurnRisk"] = "Unknown"

    if scores is None or scores.empty:
        return cust, churn_rows

    scores = scores.rename(columns={"churn_prob": "ChurnProbability"})
    scores["ChurnProbability"] = pd.to_numeric(scores["ChurnProbability"], errors="coerce")
    scores["ChurnProbability"] = scores["ChurnProbability"].clip(lower=0.0, upper=1.0)

    scores["CustomerIdKey"] = scores["CustomerId"].astype("string")
    cust["CustomerIdKey"] = cust["CustomerId"].astype("string")

    bins = [0.0, 0.4, 0.7, 1.0000001]
    labels = ["Low", "Medium", "High"]
    risk_labels = pd.cut(
        scores["ChurnProbability"],
        bins=bins,
        labels=labels,
        include_lowest=True,
        right=False,
    ).astype("string")
    scores["ChurnRisk"] = risk_labels
    scores["ChurnRisk"] = scores["ChurnRisk"].fillna("Unknown")

    cust = cust.merge(
        scores[["CustomerIdKey", "ChurnProbability", "ChurnRisk"]],
        on="CustomerIdKey",
        how="left",
    )
    cust = cust.drop(columns=["CustomerIdKey"])
    churn_prob = pd.to_numeric(cust["ChurnProbability"], errors="coerce")
    cust["ChurnProbability"] = churn_prob.round(3)
    cust["ChurnRisk"] = cust["ChurnRisk"].astype("string")
    cust.loc[churn_prob.isna(), "ChurnRisk"] = "Unknown"

    churn_rows_df = (
        cust.dropna(subset=["ChurnProbability"])
        .sort_values("ChurnProbability", ascending=False)
        .loc[:, [
            "CustomerId",
            "CustomerName",
            "TotalRevenue",
            "DaysSinceLastOrder",
            "ChurnProbability",
            "ChurnRisk",
        ]]
        .head(20)
    )
    churn_rows = churn_rows_df.to_dict(orient="records")
    return cust, churn_rows

@bp.route("/", methods=["GET", "POST"])
@login_required
@requires_roles("sales", "sales_manager", "production", "gm", "owner", "admin")
def index():
    payload = _customers_bundle_payload("overview")
    v3_enabled = _customers_kpis_v3_enabled()
    v2_enabled = _customers_kpis_v2_enabled()
    try:
        filters_norm = _query_filter_params()
    except Exception:
        filters_norm = {}

    kpis = payload.get("kpis", {}) if isinstance(payload, dict) else {}
    table = payload.get("table", {}) if isinstance(payload, dict) else {}
    meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
    charts = payload.get("charts", {}) if isinstance(payload, dict) else {}
    drivers = payload.get("drivers", {}) if isinstance(payload, dict) else {}
    definitions = payload.get("definitions", {}) if isinstance(payload, dict) else {}
    health_strip = payload.get("health_strip", {}) if isinstance(payload, dict) else {}
    executive_scorecard = (
        payload.get("executive_scorecard")
        or kpis.get("scorecard")
        or {}
    ) if isinstance(payload, dict) else {}
    executive_narrative = (
        payload.get("executive_narrative")
        or kpis.get("executive_narrative")
        or ""
    ) if isinstance(payload, dict) else ""
    churn_risk_summary = (
        payload.get("churn_risk_summary")
        or kpis.get("churn_risk_summary")
        or {}
    ) if isinstance(payload, dict) else {}

    rows = []
    for row in table.get("rows", []) or []:
        revenue = row.get("revenue") or 0.0
        cost = row.get("cost") if row.get("cost") is not None else 0.0
        profit = row.get("profit") if row.get("profit") is not None else (revenue - cost)
        margin_pct = row.get("margin_pct")
        orders_val = row.get("orders") or 0
        rows.append(
            {
                "CustomerId": row.get("customer_id") or row.get("key"),
                "CustomerName": row.get("customer_name") or row.get("label") or row.get("key") or "Unknown",
                "TotalRevenue": revenue,
                "TotalCost": cost,
                "Profit": profit,
                "ProfitMarginPct": margin_pct,
                "TotalOrders": orders_val,
                "FirstOrder": row.get("first_order"),
                "LastOrder": row.get("last_order"),
                "RevenuePriorWindow": row.get("revenue_prior_window"),
                "DeltaRevenue": row.get("delta_revenue"),
                "DeltaRevenuePct": row.get("delta_revenue_pct"),
                "DeltaRevenueStatus": row.get("delta_revenue_status"),
                "DeltaRevenueLabel": row.get("delta_revenue_label"),
                "ProfitPriorWindow": row.get("profit_prior_window"),
                "DeltaProfit": row.get("delta_profit"),
                "DeltaProfitPct": row.get("delta_profit_pct"),
                "MarginPriorPct": row.get("margin_prior_pct"),
                "DeltaMarginPct": row.get("delta_margin_pct"),
                "OrdersLast90": row.get("orders_last_90"),
                "OrdersLast30": row.get("orders_last_30"),
                "OrdersPriorWindow": row.get("orders_prior_window"),
                "DeltaOrders": row.get("delta_orders"),
                "DeltaOrdersPct": row.get("delta_orders_pct"),
                "AvgOrderValue": row.get("avg_order_value"),
                "ASP": row.get("asp"),
                "RevenueLast90": row.get("revenue_last_90"),
                "DaysSinceLastOrder": row.get("days_since_last_order"),
                "ChurnRisk": row.get("churn_risk") or row.get("churn_risk_band"),
                "SegmentLabel": row.get("segment_label"),
            }
        )

    summary = {
        "total_revenue": kpis.get("revenue"),
        "total_orders": kpis.get("orders"),
        "total_customers": kpis.get("customers") or meta.get("total_rows") or len(rows),
        "customers_in_table": kpis.get("customers_in_table"),
        "avg_order_value": kpis.get("avg_order_value"),
        "margin_pct": kpis.get("margin_pct"),
        "repeat_rate_avg": kpis.get("repeat_rate_avg"),
        "loyal_customers": kpis.get("loyal_customers"),
        "loyal_share_pct": kpis.get("loyal_share_pct"),
        "at_risk_90": kpis.get("at_risk_90"),
        "churned_180": kpis.get("churned_180"),
        "active_last_30": kpis.get("active_last_30"),
        "new_last_90": kpis.get("new_last_90"),
        "revenue_last_90_total": kpis.get("revenue_last_90_total"),
        "revenue_last_30_total": kpis.get("revenue_last_30_total"),
        "total_profit": kpis.get("profit"),
        "high_risk_customers": kpis.get("high_risk_customers"),
        "medium_risk_customers": kpis.get("medium_risk_customers"),
        "low_risk_customers": kpis.get("low_risk_customers"),
        "revenue_at_risk": kpis.get("revenue_at_risk"),
        "avg_churn_probability": kpis.get("avg_churn_probability"),
        "nrr": kpis.get("nrr"),
        "grr": kpis.get("grr"),
        "growth_composition": kpis.get("growth_composition"),
        "top1_share_pct": kpis.get("top1_share_pct"),
        "top5_share_pct": kpis.get("top5_share_pct"),
        "hhi": kpis.get("hhi"),
        "margin_p10_pct": kpis.get("margin_p10_pct"),
        "margin_p50_pct": kpis.get("margin_p50_pct"),
        "margin_p90_pct": kpis.get("margin_p90_pct"),
        "negative_margin_customers": kpis.get("negative_margin_customers"),
        "negative_margin_revenue_share_pct": kpis.get("negative_margin_revenue_share_pct"),
        "avg_tenure_days": kpis.get("avg_tenure_days"),
        "churn_risk_revenue_at_stake": kpis.get("churn_risk_revenue_at_stake"),
        "median_days_between_orders": kpis.get("median_days_between_orders"),
        "median_aov": kpis.get("median_aov"),
        "median_units_per_order": kpis.get("median_units_per_order"),
        "median_weight_per_order": kpis.get("median_weight_per_order"),
        "cost_coverage_pct": kpis.get("cost_coverage_pct"),
        "cost_missing_rows": kpis.get("cost_missing_rows"),
        "top_customer": kpis.get("top_customer"),
        "scorecard": executive_scorecard,
        "executive_narrative": executive_narrative,
        "reactivation_rate_pct": kpis.get("reactivation_rate_pct"),
        "new_customer_revenue_share_pct": kpis.get("new_customer_revenue_share_pct"),
        "returning_customer_revenue_share_pct": kpis.get("returning_customer_revenue_share_pct"),
        "churn_risk_summary": churn_risk_summary,
        "window": kpis.get("window") or {},
    }
    page_totals = table.get("page_totals") or {
        "revenue": sum(r.get("TotalRevenue", 0) for r in rows),
        "orders": sum((r.get("TotalOrders") or 0) for r in rows),
        "profit": sum((r.get("Profit") or 0) for r in rows),
        "cost": sum((r.get("TotalCost") or 0) for r in rows),
    }

    total_rows = table.get("total_rows") or meta.get("total_rows") or len(rows)
    total_pages = table.get("total_pages") or meta.get("total_pages") or 1
    page = table.get("page") or meta.get("page") or 1
    per_page = table.get("page_size") or meta.get("page_size") or len(rows) or 25
    search_val = (request.args.get("search") or request.args.get("q") or "").strip()
    quick_filter = str(table.get("quick_filter") or request.args.get("quick_filter") or request.args.get("quick_filters") or "all")
    sort_by = str(table.get("sort_by") or request.args.get("sort") or request.args.get("sort_by") or "revenue")
    sort_dir = str(table.get("sort_dir") or request.args.get("sort_dir") or request.args.get("dir") or "desc")

    if v3_enabled:
        template_name = "customers/kpis_v3.html"
    elif v2_enabled:
        template_name = "customers/kpis_v2.html"
    else:
        template_name = "customers/kpis_unified.html"

    return render_template(
        template_name,
        rows=rows,
        table_rows=table.get("rows", []) or [],
        page_totals=page_totals,
        total_rows=total_rows,
        total_pages=total_pages,
        page=page,
        per_page=per_page,
        summary=summary,
        data_quality={"missing_unit_cost": int(kpis.get("cost_missing_rows") or 0)},
        payload=payload,
        kpis=kpis,
        table=table,
        charts=charts,
        drivers=drivers,
        definitions=definitions,
        health_strip=health_strip,
        quick_filter=quick_filter,
        sort_by=sort_by,
        sort_dir=sort_dir,
        show_costs=can_view_costs(current_user),
        customers_kpis_v3=v3_enabled,
        customers_kpis_v2=v2_enabled,
        executive_scorecard=executive_scorecard,
        executive_narrative=executive_narrative,
        churn_risk_summary=churn_risk_summary,
        filters=filters_norm,
        search=search_val,
    )

# ─────────────────────────────────────────────────────────────────────────────
# CLV
# ─────────────────────────────────────────────────────────────────────────────

@bp.route("/clv")
@login_required
@requires_roles("sales", "sales_manager", "production", "gm", "owner", "admin")
def clv():
    payload = _customers_bundle_payload("clv")
    clv_payload = (payload.get("clv") or {}) if isinstance(payload, dict) else {}
    definitions = payload.get("definitions", {}) if isinstance(payload, dict) else {}
    show_costs = can_view_costs(current_user)
    try:
        filters_norm = _query_filter_params()
    except Exception:
        filters_norm = {}

    if _customers_clv_v2_enabled():
        return render_template(
            "customers/clv_v2.html",
            payload=payload,
            clv=clv_payload,
            definitions=definitions,
            show_costs=show_costs,
            filters=filters_norm,
        )

    top_rows_raw = clv_payload.get("top") or []
    top_rows = []
    for row in top_rows_raw:
        top_rows.append(
            {
                "CustomerId": row.get("customer_id"),
                "CustomerName": row.get("customer_name"),
                "AvgOrderValue": row.get("avg_order_value"),
                "OrdersPerYear": row.get("orders_per_year"),
                "CLV": row.get("clv"),
                "GrossMarginPct": row.get("margin_pct"),
            }
        )
    bar_labels = [r.get("CustomerName") or r.get("CustomerId") for r in top_rows]
    bar_values = [r.get("GrossMarginPct") if show_costs else r.get("CLV") for r in top_rows]
    scatter_x = [r.get("CLV") or 0 for r in top_rows]
    scatter_y = [r.get("OrdersPerYear") or 0 for r in top_rows]
    scatter_size = [r.get("CLV") or 0 for r in top_rows]
    scatter_color = [r.get("GrossMarginPct") or 0 for r in top_rows]
    scatter_text = bar_labels
    return render_template(
        "customers/clv.html",
        show_costs=show_costs,
        top_rows=top_rows,
        bar_labels=bar_labels,
        bar_values=bar_values,
        scatter_x=scatter_x,
        scatter_y=scatter_y,
        scatter_size=scatter_size,
        scatter_color=scatter_color,
        scatter_text=scatter_text,
    )

# ─────────────────────────────────────────────────────────────────────────────
# KPIs
# ─────────────────────────────────────────────────────────────────────────────

@bp.route("/kpis", methods=["GET"])
@login_required
@requires_roles("sales", "sales_manager", "production", "gm", "owner", "admin")
def kpis():
    # Bundle-backed path; reuse index rendering to avoid legacy pandas code
    return index()
    cust_agg = _customer_agg(df_f)
    cust_agg, churn_rows = _attach_churn_features(cust_agg, df_f)
    data_quality = _data_quality(df_f)

    show_costs = can_view_costs(current_user)
    summary: dict[str, object] = {
        "total_customers": 0,
        "total_revenue": 0.0,
        "total_orders": 0,
        "avg_order_value": 0.0,
        "repeat_rate_avg": 0.0,
        "active_last_30": 0,
        "at_risk_90": 0,
        "new_last_90": 0,
        "loyal_customers": 0,
        "loyal_share_pct": 0.0,
        "total_profit": 0.0 if show_costs else None,
        "revenue_last_90_total": 0.0,
        "revenue_last_30_total": 0.0,
        "high_risk_customers": 0,
        "medium_risk_customers": 0,
        "low_risk_customers": 0,
        "revenue_at_risk": 0.0,
        "avg_churn_probability": None,
        "top_customer": None,
    }
    top_customers: list[dict[str, object]] = []
    if not cust_agg.empty:
        cust_agg = cust_agg.sort_values("TotalRevenue", ascending=False, kind="stable")
        revenue_series = pd.to_numeric(cust_agg["TotalRevenue"], errors="coerce").fillna(0.0)
        cost_series = pd.to_numeric(cust_agg["TotalCost"], errors="coerce").fillna(0.0)
        orders_series = pd.to_numeric(cust_agg["TotalOrders"], errors="coerce").fillna(0)
        months_series = pd.to_numeric(cust_agg["MonthsActive"], errors="coerce").replace(0, pd.NA)
        repeat_series = pd.to_numeric(cust_agg["RepeatRate"], errors="coerce").fillna(0.0)
        repeat_mean = repeat_series.mean()
        repeat_mean_pct = float(repeat_mean * 100.0) if pd.notna(repeat_mean) else 0.0
        days_since = pd.to_numeric(cust_agg["DaysSinceLastOrder"], errors="coerce")
        first_dates = pd.to_datetime(cust_agg["FirstOrder"], errors="coerce")
        try:
            first_dates = first_dates.dt.tz_convert(None)
        except (TypeError, AttributeError, ValueError):
            try:
                first_dates = first_dates.dt.tz_localize(None)
            except (TypeError, AttributeError, ValueError):
                pass
        last_dates = pd.to_datetime(cust_agg["LastOrder"], errors="coerce")
        try:
            last_dates = last_dates.dt.tz_convert(None)
        except (TypeError, AttributeError, ValueError):
            try:
                last_dates = last_dates.dt.tz_localize(None)
            except (TypeError, AttributeError, ValueError):
                pass

        profit_series = revenue_series - cost_series
        now_utc = pd.Timestamp.utcnow()
        if now_utc.tzinfo is not None and now_utc.tzinfo.utcoffset(now_utc) is not None:
            now_naive = now_utc.tz_convert(None).normalize()
        else:
            now_naive = now_utc.normalize()
        total_profit_value = float(profit_series.sum())
        cust_agg["AvgOrderValue"] = np.where(orders_series > 0, revenue_series / orders_series, 0.0)
        cust_agg["RevenuePerMonth"] = np.where(months_series.notna(), revenue_series / months_series, 0.0)
        cust_agg["OrdersPerMonth"] = np.where(months_series.notna(), orders_series / months_series, 0.0)
        if show_costs:
            cust_agg["ProfitMarginPct"] = np.where(revenue_series > 0, (profit_series / revenue_series) * 100.0, 0.0)
        total_revenue = float(revenue_series.sum())
        cust_agg["RevenueSharePct"] = np.where(total_revenue > 0, (revenue_series / total_revenue) * 100.0, 0.0)

        for col in ("AvgOrderValue", "RevenuePerMonth", "OrdersPerMonth"):
            if col in cust_agg.columns:
                cust_agg[col] = pd.to_numeric(cust_agg[col], errors="coerce").round(2)
        if "RevenueSharePct" in cust_agg.columns:
            cust_agg["RevenueSharePct"] = pd.to_numeric(cust_agg["RevenueSharePct"], errors="coerce").round(1)
        if show_costs and "ProfitMarginPct" in cust_agg.columns:
            cust_agg["ProfitMarginPct"] = pd.to_numeric(cust_agg["ProfitMarginPct"], errors="coerce").round(1)

        cust_agg["FirstOrder"] = first_dates.dt.strftime("%Y-%m-%d")
        cust_agg["LastOrder"] = last_dates.dt.strftime("%Y-%m-%d")
        for col in ("RevenueLast30", "RevenueLast90"):
            if col in cust_agg.columns:
                cust_agg[col] = pd.to_numeric(cust_agg[col], errors="coerce").round(2)
        for col in ("OrdersLast30", "OrdersLast90"):
            if col in cust_agg.columns:
                cust_agg[col] = pd.to_numeric(cust_agg[col], errors="coerce").round(0)
        if "RevenueTrend90" in cust_agg.columns:
            cust_agg["RevenueTrend90"] = pd.to_numeric(cust_agg["RevenueTrend90"], errors="coerce").round(1)
        if "ChurnProbability" in cust_agg.columns:
            cust_agg["ChurnProbability"] = pd.to_numeric(cust_agg["ChurnProbability"], errors="coerce").round(3)

        total_orders = int(orders_series.sum())
        total_customers = int(len(cust_agg))
        summary.update({
            "total_customers": total_customers,
            "total_revenue": total_revenue,
            "total_orders": total_orders,
            "avg_order_value": float(total_revenue / total_orders) if total_orders else 0.0,
            "repeat_rate_avg": repeat_mean_pct if total_customers else 0.0,
            "active_last_30": int((days_since <= 30).sum()),
            "at_risk_90": int((days_since >= 90).sum()),
            "new_last_90": int(((now_naive - first_dates) <= pd.Timedelta(days=90)).sum()),
            "loyal_customers": int((repeat_series >= 0.5).sum()),
        })
        if show_costs:
            summary["total_profit"] = float(round(total_profit_value, 2))
        loyal_total = summary["loyal_customers"] or 0
        summary["loyal_share_pct"] = float((loyal_total / total_customers) * 100.0) if total_customers else 0.0
        summary["avg_order_value"] = float(round(summary["avg_order_value"], 2)) if summary["avg_order_value"] else 0.0
        summary["repeat_rate_avg"] = float(round(summary["repeat_rate_avg"], 1)) if pd.notna(summary["repeat_rate_avg"]) else 0.0
        summary["loyal_share_pct"] = float(round(summary["loyal_share_pct"], 1)) if pd.notna(summary["loyal_share_pct"]) else 0.0
        revenue_last_90 = pd.to_numeric(cust_agg.get("RevenueLast90"), errors="coerce").fillna(0.0)
        revenue_last_30 = pd.to_numeric(cust_agg.get("RevenueLast30"), errors="coerce").fillna(0.0)
        summary["revenue_last_90_total"] = float(revenue_last_90.sum())
        summary["revenue_last_30_total"] = float(revenue_last_30.sum())
        churn_prob_series = pd.to_numeric(cust_agg.get("ChurnProbability"), errors="coerce")
        if churn_prob_series.notna().any():
            summary["avg_churn_probability"] = float(round(churn_prob_series.mean() * 100.0, 1))
        risk_series = cust_agg.get("ChurnRisk")
        if risk_series is not None:
            risk_series = risk_series.astype("string")
            high_mask = (risk_series == "High").fillna(False)
            med_mask = (risk_series == "Medium").fillna(False)
            low_mask = (risk_series == "Low").fillna(False)
            summary["high_risk_customers"] = int(high_mask.sum())
            summary["medium_risk_customers"] = int(med_mask.sum())
            summary["low_risk_customers"] = int(low_mask.sum())
            summary["revenue_at_risk"] = float(revenue_series.where(high_mask, 0.0).sum())

        top_row = cust_agg.iloc[0]
        top_orders = pd.to_numeric(top_row.get("TotalOrders"), errors="coerce")
        top_profit = pd.to_numeric(top_row.get("Profit"), errors="coerce") if show_costs else None
        summary["top_customer"] = {
            "name": top_row.get("CustomerName") or top_row.get("CustomerId"),
            "revenue": float(top_row.get("TotalRevenue") or 0.0),
            "orders": int(top_orders) if pd.notna(top_orders) else 0,
            "share": float(top_row.get("RevenueSharePct") or 0.0),
            "profit": float(top_profit) if (show_costs and top_profit is not None and pd.notna(top_profit)) else None,
            "revenue_last_90": float(top_row.get("RevenueLast90") or 0.0),
            "churn_risk": (str(top_row.get("ChurnRisk")) if top_row.get("ChurnRisk") else "Unknown"),
            "churn_probability": (
                float(top_row.get("ChurnProbability") * 100.0)
                if top_row.get("ChurnProbability") is not None and pd.notna(top_row.get("ChurnProbability"))
                else None
            ),
        }

        top_customers = cust_agg.head(15).to_dict(orient="records")

    try:
        page = max(1, int(request.args.get("page", 1)))
    except Exception:
        page = 1
    try:
        per_page = max(1, min(100, int(request.args.get("per_page", 50))))
    except Exception:
        per_page = 50

    total_rows = int(len(cust_agg))
    total_pages = int((total_rows + per_page - 1) // per_page) if per_page else 1
    start, end = (page - 1) * per_page, (page - 1) * per_page + per_page
    page_df = cust_agg.iloc[start:end] if total_rows else cust_agg
    page_rows = page_df.to_dict(orient="records") if not page_df.empty else []

    page_totals = {
        "revenue": float(pd.to_numeric(page_df.get("TotalRevenue"), errors="coerce").sum()) if not page_df.empty else 0.0,
        "orders": int(pd.to_numeric(page_df.get("TotalOrders"), errors="coerce").sum()) if not page_df.empty else 0,
    }
    if show_costs:
        page_totals["cost"] = float(pd.to_numeric(page_df.get("TotalCost"), errors="coerce").sum()) if not page_df.empty else 0.0
        page_totals["profit"] = float(pd.to_numeric(page_df.get("Profit"), errors="coerce").sum()) if not page_df.empty else 0.0
    else:
        page_totals["cost"] = None
        page_totals["profit"] = None

    return render_template(
        "customers/kpis.html",
        table=page_df,
        rows=page_rows,
        show_costs=show_costs,
        churn_rows=churn_rows,
        page=page, per_page=per_page, total_pages=total_pages, total_rows=total_rows,
        data_quality=data_quality,
        summary=summary,
        top_customers=top_customers,
        page_totals=page_totals,
    )


def _peer_price_benchmark(
    df_all: pd.DataFrame,
    df_cust: pd.DataFrame,
    label_col: str,
    region_col: str | None,
) -> pd.DataFrame:
    rev = _rev_col(df_all)
    qty_all = _effective_qty(df_all)
    with np.errstate(divide="ignore", invalid="ignore"):
        realized_all = (pd.to_numeric(df_all[rev], errors="coerce") / qty_all).replace([np.inf, -np.inf], np.nan)

    peers = df_all[[label_col]].copy()
    peers["realized"] = realized_all

    if region_col and region_col in df_all.columns:
        peers["Region"] = df_all[region_col]
        grp_cols = ["Region", label_col]
    else:
        grp_cols = [label_col]

    peer_median = peers.groupby(grp_cols, dropna=True, observed=True)["realized"].median().reset_index()

    if region_col and region_col in df_cust.columns:
        cust_region = _mode_or_first(df_cust[region_col])
        if cust_region is not None and "Region" in peer_median.columns:
            peer_median = peer_median[peer_median["Region"] == cust_region]

    qty_c = _effective_qty(df_cust)
    with np.errstate(divide="ignore", invalid="ignore"):
        cust_realized = (pd.to_numeric(df_cust[rev], errors="coerce") / qty_c).replace([np.inf, -np.inf], np.nan)

    cust_price = (
        pd.DataFrame({label_col: df_cust[label_col], "cust_unit_price": cust_realized})
        .dropna(subset=["cust_unit_price"])
        .groupby(label_col, observed=True)["cust_unit_price"].median()
        .reset_index()
    )

    if "Region" in peer_median.columns:
        peer_median = peer_median.drop(columns=["Region"])
    peer_median = peer_median.rename(columns={"realized": "peer_median"})

    tbl = cust_price.merge(peer_median, on=label_col, how="left")
    with np.errstate(divide="ignore", invalid="ignore"):
        tbl["delta_pct"] = (tbl["cust_unit_price"] - tbl["peer_median"]) / tbl["peer_median"] * 100.0
    tbl["suggest_price"] = tbl["peer_median"]

    for c in ("cust_unit_price", "peer_median", "delta_pct", "suggest_price"):
        if c in tbl.columns:
            tbl[c] = pd.to_numeric(tbl[c], errors="coerce").round(2)
    return tbl.set_index(label_col)

def _cross_sell_suggestions(
    df_all: pd.DataFrame,
    df_cust: pd.DataFrame,
    label_col: str,
    region_col: str | None,
    top_k: int = 5,
) -> list[dict[str, object]]:
    region_val = _mode_or_first(df_cust[region_col]) if region_col and region_col in df_cust.columns else None
    rev = _rev_col(df_all)
    pop = df_all.copy()
    if region_val is not None and region_col in pop.columns:
        pop = pop[pop[region_col] == region_val]
    bought = set(df_cust[label_col].dropna().astype(str).unique())
    if label_col not in pop.columns:
        return []
    cand = (
        pop[~pop[label_col].astype(str).isin(bought)]
        .groupby(label_col, observed=True)[rev]
        .sum()
        .sort_values(ascending=False)
        .head(top_k)
        .reset_index()
    )
    return [{"product": str(r[label_col]), "region": region_val, "revenue": float(round(r[rev], 2))} for _, r in cand.iterrows()]

# ─────────────────────────────────────────────────────────────────────────────
# Customer drilldown (fixed & robust)
# ─────────────────────────────────────────────────────────────────────────────

@bp.route("/drilldown/<customer_id>")
@login_required
@requires_roles("sales", "sales_manager", "production", "gm", "owner", "admin")
def drilldown(customer_id):
    args = MultiDict(request.args)
    args["customer_id"] = customer_id
    drilldown_v2 = _customer_drilldown_v2_enabled()
    if drilldown_v2:
        args["drilldown_v2"] = "1"
    raw_payload = bundle_service.drilldown("customers", args)
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    kpis_data = payload.get("kpis", {}) if isinstance(payload, dict) else {}
    trend = payload.get("trend", {}) if isinstance(payload, dict) else {}
    table_rows = payload.get("table", {}).get("rows", []) if isinstance(payload, dict) else []
    top_spend = payload.get("top_spend", []) if isinstance(payload, dict) else []
    top_weight = payload.get("top_weight", []) if isinstance(payload, dict) else []
    weekday_payload = payload.get("weekday", {}) if isinstance(payload, dict) else {}
    cadence_payload = payload.get("cadence", {}) if isinstance(payload, dict) else {}
    seasonality_payload = payload.get("seasonality", {}) if isinstance(payload, dict) else {}
    profit_mix = payload.get("profit_mix", {}) if isinstance(payload, dict) else {}
    basket_stats = payload.get("basket", {}) if isinstance(payload, dict) else {}
    price_table = payload.get("price_table", []) if isinstance(payload, dict) else []
    profile_payload = payload.get("profile", {}) if isinstance(payload, dict) else {}
    next_best_actions = payload.get("next_best_actions", []) if isinstance(payload, dict) else []
    hero_payload = payload.get("hero", {}) if isinstance(payload, dict) else {}
    scorecard_payload = payload.get("executive_scorecard", []) if isinstance(payload, dict) else []
    priority_engine = payload.get("priority_engine", {}) if isinstance(payload, dict) else {}
    trend_summary = payload.get("trend_summary", {}) if isinstance(payload, dict) else {}
    lifecycle_payload = payload.get("lifecycle", {}) if isinstance(payload, dict) else {}
    weight_analytics = payload.get("weight_analytics", {}) if isinstance(payload, dict) else {}
    product_intelligence = payload.get("product_intelligence", {}) if isinstance(payload, dict) else {}
    pricing_intelligence = payload.get("pricing_intelligence", {}) if isinstance(payload, dict) else {}
    ordering_rhythm = payload.get("ordering_rhythm", {}) if isinstance(payload, dict) else {}
    concentration_payload = payload.get("concentration", {}) if isinstance(payload, dict) else {}
    crm_workspace = payload.get("crm_workspace", {}) if isinstance(payload, dict) else {}
    trust_coverage = payload.get("trust_coverage", {}) if isinstance(payload, dict) else {}
    chart_states = payload.get("chart_states", {}) if isinstance(payload, dict) else {}
    category_rows = payload.get("categories", []) if isinstance(payload, dict) else []
    meta_payload = payload.get("meta", {}) if isinstance(payload, dict) else {}
    cross_sell_payload = []
    if isinstance(payload, dict):
        cross_sell_payload = payload.get("cross_sell") or payload.get("cross_sell_ideas") or []
    customer_name = kpis_data.get("label") or payload.get("meta", {}).get("entity_label") or customer_id
    months = trend.get("labels") or []
    monthly_revenue = trend.get("revenue") or []
    monthly_orders = trend.get("orders") or trend.get("qty") or []
    monthly_weight = trend.get("weight_lb") or []
    monthly_cost = trend.get("cost") or [0 for _ in monthly_revenue]
    monthly_profit = trend.get("profit") or [r - c for r, c in zip(monthly_revenue, monthly_cost)]
    monthly_margin = trend.get("margin_pct") or [((p / r) * 100.0 if r else None) for p, r in zip(monthly_profit, monthly_revenue)]
    monthly_revenue_lb = trend.get("revenue_per_lb") or []
    monthly_profit_lb = trend.get("profit_per_lb") or []
    monthly_revenue_rolling = trend.get("rolling_revenue") or []
    monthly_weight_rolling = trend.get("rolling_weight") or []
    monthly_previous_year = trend.get("previous_year_revenue") or [None for _ in months]
    show_costs = can_view_costs(current_user)
    if not show_costs:
        safe_rows = []
        for row in table_rows:
            if not isinstance(row, dict):
                continue
            clean_row = dict(row)
            for key in (
                "cost",
                "profit",
                "margin_pct",
                "margin_prior_pct",
                "margin_delta_pp",
                "cost_lb",
                "contribution_lb",
                "cost_null_rows",
                "cost_missing_pct",
            ):
                clean_row[key] = None
            safe_rows.append(clean_row)
        table_rows = safe_rows
        profit_mix = {}
        pricing_intelligence = dict(pricing_intelligence or {})
        pricing_summary = dict((pricing_intelligence.get("summary") or {}))
        for key in (
            "margin_quality_score",
            "under_target_margin_exposure_pct",
            "negative_margin_share_pct",
            "recoverable_margin_uplift",
        ):
            pricing_summary[key] = None
        pricing_intelligence["summary"] = pricing_summary
        pricing_intelligence["low_margin_watchlist"] = []
        pricing_intelligence["negative_margin_watchlist"] = []
        product_intelligence = dict(product_intelligence or {})
        product_intelligence["top_profit_products"] = []
        product_intelligence["margin_leakage_products"] = []
        hero_payload = dict(hero_payload or {})
        hero_payload["profitability_posture"] = "Restricted"
        hero_payload["badges"] = [
            badge
            for badge in (hero_payload.get("badges") or [])
            if (badge.get("label") or "").lower() not in {"margin leakage", "healthy margin"}
        ]
        trust_coverage = dict(trust_coverage or {})
        trust_coverage["cost_coverage_pct"] = None
        clean_scorecards = []
        for group in scorecard_payload or []:
            if not isinstance(group, dict):
                continue
            metrics = []
            for metric in group.get("metrics") or []:
                label = str((metric or {}).get("label") or "").lower()
                if "profit" in label or "margin" in label:
                    continue
                metrics.append(metric)
            clean_group = dict(group)
            clean_group["metrics"] = metrics
            if metrics:
                clean_scorecards.append(clean_group)
        scorecard_payload = clean_scorecards
        priority_engine = dict(priority_engine or {})
        priority_engine["scores"] = [
            score
            for score in (priority_engine.get("scores") or [])
            if str((score or {}).get("key") or "") not in {"margin_quality_score"}
        ]
    product_spend_top = top_spend or sorted(table_rows, key=lambda r: r.get("revenue", 0), reverse=True)[:5]
    product_weight_top = top_weight or sorted(
        [r for r in table_rows if r.get("weight_lb") is not None],
        key=lambda r: r.get("weight_lb", 0),
        reverse=True,
    )[:5]
    template_name = "customers/drilldown_v2.html" if drilldown_v2 else "customers/drilldown.html"
    scope_info = meta_payload.get("scope") if isinstance(meta_payload, dict) else {}
    if not isinstance(scope_info, dict):
        scope_info = {}
    base_export_pairs: list[tuple[str, str]] = []
    if hasattr(request.args, "lists"):
        for key, values in request.args.lists():
            if key in {"page", "dataset", "format", "export_type", "type", "customer_id", "drilldown_v2"}:
                continue
            for value in values:
                base_export_pairs.append((str(key), str(value)))

    def _drilldown_export_url(dataset: str, fmt: str) -> str:
        pairs = list(base_export_pairs)
        pairs.extend(
            [
                ("page", "drilldown"),
                ("customer_id", str(customer_id)),
                ("dataset", str(dataset)),
                ("format", str(fmt)),
            ]
        )
        query = urlencode(pairs, doseq=True)
        base = url_for("customers.export")
        return f"{base}?{query}" if query else base

    export_links = {
        "snapshot_xlsx": _drilldown_export_url("snapshot", "xlsx"),
        "orders_csv": _drilldown_export_url("orders", "csv"),
        "monthly_xlsx": _drilldown_export_url("monthly", "xlsx"),
        "products_xlsx": _drilldown_export_url("product_profitability", "xlsx"),
        "products_csv": _drilldown_export_url("product_profitability", "csv"),
        "price_xlsx": _drilldown_export_url("price", "xlsx"),
        "price_csv": _drilldown_export_url("price", "csv"),
        "cross_sell_xlsx": _drilldown_export_url("cross_sell", "xlsx"),
        "cross_sell_csv": _drilldown_export_url("cross_sell", "csv"),
        "cadence_xlsx": _drilldown_export_url("cadence", "xlsx"),
        "seasonality_xlsx": _drilldown_export_url("seasonality", "xlsx"),
        "categories_xlsx": _drilldown_export_url("categories", "xlsx"),
        "weight_xlsx": _drilldown_export_url("weight_operational", "xlsx"),
        "actions_xlsx": _drilldown_export_url("crm_actions", "xlsx"),
    }
    top_mix_rows = []
    for row in table_rows:
        if not isinstance(row, dict):
            continue
        top_mix_rows.append(
            {
                "sku": row.get("sku"),
                "product": row.get("product"),
                "protein_family": row.get("protein_family"),
                "category": row.get("category"),
                "revenue": row.get("revenue"),
                "weight_lb": row.get("weight_lb"),
                "profit": row.get("profit") if show_costs else None,
            }
        )
    workspace_chart_payload = sanitize_for_json(
        {
            "showCosts": show_costs,
            "trend": {
                "labels": months,
                "revenue": monthly_revenue,
                "orders": monthly_orders,
                "weight_lb": monthly_weight,
                "profit": monthly_profit if show_costs else [],
                "margin_pct": monthly_margin if show_costs else [],
                "revenue_per_lb": monthly_revenue_lb,
                "profit_per_lb": monthly_profit_lb if show_costs else [],
                "rolling_revenue": monthly_revenue_rolling,
                "rolling_weight": monthly_weight_rolling,
                "previous_year_revenue": monthly_previous_year,
            },
            "weekday": {
                "labels": weekday_payload.get("labels", []),
                "revenue": weekday_payload.get("revenue", []),
                "weight_lb": weekday_payload.get("weight_lb", []),
                "orders": weekday_payload.get("orders", []),
            },
            "seasonality": {
                "months": seasonality_payload.get("months", []),
                "years": seasonality_payload.get("years", []),
                "matrix": seasonality_payload.get("matrix", []),
            },
            "topMixRows": top_mix_rows,
            "chartStates": chart_states,
        }
    )
    return render_template(
        template_name,
        customer_id=customer_id,
        customer_name=customer_name,
        kpis={
            "total_revenue": kpis_data.get("total_revenue"),
            "total_profit": kpis_data.get("total_profit") if show_costs else None,
            "total_orders": kpis_data.get("total_orders"),
            "aov": kpis_data.get("aov"),
            "revenue_per_month": kpis_data.get("revenue_per_month"),
            "orders_per_month": kpis_data.get("orders_per_month"),
            "profit": kpis_data.get("total_profit") if show_costs else None,
            "profit_share_pct": kpis_data.get("profit_share_pct") if show_costs else None,
            "margin_pct": kpis_data.get("margin_pct") if show_costs else None,
            "revenue_last_90": kpis_data.get("revenue_last_90"),
            "revenue_last_30": kpis_data.get("revenue_last_30"),
            "revenue_window": kpis_data.get("revenue_window"),
            "revenue_prior_window": kpis_data.get("revenue_prior_window"),
            "revenue_delta_window": kpis_data.get("revenue_delta_window"),
            "revenue_delta_pct_window": kpis_data.get("revenue_delta_pct_window"),
            "orders_last_30": kpis_data.get("orders_last_30"),
            "orders_last_90": kpis_data.get("orders_last_90"),
            "orders_window": kpis_data.get("orders_window"),
            "orders_prior_window": kpis_data.get("orders_prior_window"),
            "orders_delta_window": kpis_data.get("orders_delta_window"),
            "avg_ticket_last_90": kpis_data.get("avg_ticket_last_90"),
            "profit_last_90": kpis_data.get("profit_last_90") if show_costs else None,
            "margin_last_90": kpis_data.get("margin_last_90") if show_costs else None,
            "churn_risk": kpis_data.get("churn_risk"),
            "days_since_last_order": kpis_data.get("days_since_last_order"),
            "cadence_avg_days": kpis_data.get("cadence_avg_days"),
            "cadence_median_days": kpis_data.get("cadence_median_days"),
            "cadence_min_days": kpis_data.get("cadence_min_days"),
            "cadence_max_days": kpis_data.get("cadence_max_days"),
            "best_weekday": kpis_data.get("best_weekday"),
            "best_weekday_revenue": kpis_data.get("best_weekday_revenue"),
            "active_status": kpis_data.get("active_status"),
            "recency_band": kpis_data.get("recency_band"),
            "segment_source": kpis_data.get("segment_source"),
            "rfm_segment": kpis_data.get("rfm_segment"),
            "clv_segment": kpis_data.get("clv_segment"),
            "segment": kpis_data.get("segment") or "Unknown",
            "segment_reason": kpis_data.get("segment_reason"),
            "first_order": kpis_data.get("first_order"),
            "last_order": kpis_data.get("last_order"),
            "months_active": kpis_data.get("months_active"),
            "days_span": kpis_data.get("days_span"),
            "primary_region": kpis_data.get("primary_region"),
            "primary_shipping": kpis_data.get("primary_shipping"),
            "unique_products": kpis_data.get("unique_products"),
            "top_product_label": kpis_data.get("top_product_label"),
            "top_product_revenue": kpis_data.get("top_product_revenue"),
            "top_weight_product": kpis_data.get("top_weight_product"),
            "top_weight_lb": kpis_data.get("top_weight_lb"),
            "top_weight_unit": kpis_data.get("top_weight_unit"),
            "top_profit_product": kpis_data.get("top_profit_product") if show_costs else None,
            "top_profit_value": kpis_data.get("top_profit_value") if show_costs else None,
            "top_profit_margin": kpis_data.get("top_profit_margin") if show_costs else None,
            "price_delta_min": kpis_data.get("price_delta_min"),
            "price_delta_max": kpis_data.get("price_delta_max"),
            "pricing_dispersion_above_peer_pct": kpis_data.get("pricing_dispersion_above_peer_pct"),
            "cross_sell_count": kpis_data.get("cross_sell_count"),
            "cost_coverage_pct": kpis_data.get("cost_coverage_pct"),
            "revenue_prior_90": kpis_data.get("revenue_prior_90"),
            "revenue_delta_90": kpis_data.get("revenue_delta_90"),
            "revenue_delta_pct_90": kpis_data.get("revenue_delta_pct_90"),
            "orders_prior_90": kpis_data.get("orders_prior_90"),
            "orders_delta_90": kpis_data.get("orders_delta_90"),
            "aov_median_order": kpis_data.get("aov_median_order"),
            "weeks_active": kpis_data.get("weeks_active"),
            "unique_categories": kpis_data.get("unique_categories"),
            "total_weight_lb": kpis_data.get("total_weight_lb"),
            "lifetime_weight_lb": kpis_data.get("lifetime_weight_lb"),
            "profit_lb": kpis_data.get("profit_lb") if show_costs else None,
            "avg_lb_per_order": kpis_data.get("avg_lb_per_order"),
            "avg_lb_per_order_lifetime": kpis_data.get("avg_lb_per_order_lifetime"),
            "avg_lb_per_week": kpis_data.get("avg_lb_per_week"),
            "avg_lb_per_month": kpis_data.get("avg_lb_per_month"),
            "owner_sales_rep": kpis_data.get("owner_sales_rep"),
            "historical_owner_sales_rep": kpis_data.get("historical_owner_sales_rep"),
            "last_sales_rep": kpis_data.get("last_sales_rep"),
            "last_sales_rep_date": kpis_data.get("last_sales_rep_date"),
            "primary_city": kpis_data.get("primary_city"),
            "primary_state": kpis_data.get("primary_state"),
            "best_weekday_share_pct": kpis_data.get("best_weekday_share_pct"),
            "best_weight_weekday": kpis_data.get("best_weight_weekday"),
            "best_weight_weekday_share_pct": kpis_data.get("best_weight_weekday_share_pct"),
            "contribution_share_pct": kpis_data.get("contribution_share_pct"),
            "window_contribution_share_pct": kpis_data.get("window_contribution_share_pct"),
            "weight_share_pct": kpis_data.get("weight_share_pct"),
            "window_weight_share_pct": kpis_data.get("window_weight_share_pct"),
            "avg_abs_price_delta_pct": kpis_data.get("avg_abs_price_delta_pct"),
            "pricing_dispersion_below_peer_pct": kpis_data.get("pricing_dispersion_below_peer_pct"),
            "top_product_share_pct": kpis_data.get("top_product_share_pct"),
            "top5_product_share_pct": kpis_data.get("top5_product_share_pct"),
            "top_weight_share_pct": kpis_data.get("top_weight_share_pct"),
            "top5_weight_share_pct": kpis_data.get("top5_weight_share_pct"),
            "top_category_share_pct": kpis_data.get("top_category_share_pct"),
            "top5_category_share_pct": kpis_data.get("top5_category_share_pct"),
            "repeat_share_pct": kpis_data.get("repeat_share_pct"),
            "repeat_revenue_share_pct": kpis_data.get("repeat_revenue_share_pct"),
            "recent_repeat_revenue_share_pct": kpis_data.get("recent_repeat_revenue_share_pct"),
            "relationship_health_score": kpis_data.get("relationship_health_score"),
            "churn_risk_score": kpis_data.get("churn_risk_score"),
            "growth_opportunity_score": kpis_data.get("growth_opportunity_score"),
            "pricing_quality_score": kpis_data.get("pricing_quality_score"),
            "margin_quality_score": kpis_data.get("margin_quality_score") if show_costs else None,
            "weight_importance_score": kpis_data.get("weight_importance_score"),
            "dependency_balance_score": kpis_data.get("dependency_balance_score"),
            "service_rhythm_score": kpis_data.get("service_rhythm_score"),
            "forecastability_score": kpis_data.get("forecastability_score"),
            "commercial_tier": kpis_data.get("commercial_tier"),
            "lifecycle_stage": kpis_data.get("lifecycle_stage"),
            "risk_posture": kpis_data.get("risk_posture"),
            "profitability_posture": kpis_data.get("profitability_posture") if show_costs else None,
            "dependency_posture": kpis_data.get("dependency_posture"),
            "coverage_posture": kpis_data.get("coverage_posture"),
            "trust_posture": kpis_data.get("trust_posture"),
        },
        months=months,
        monthly_revenue=monthly_revenue,
        monthly_orders=monthly_orders,
        monthly_weight=monthly_weight,
        monthly_cost=monthly_cost,
        monthly_profit=monthly_profit,
        monthly_margin=monthly_margin,
        monthly_revenue_lb=monthly_revenue_lb,
        monthly_profit_lb=monthly_profit_lb,
        monthly_revenue_rolling=monthly_revenue_rolling,
        monthly_weight_rolling=monthly_weight_rolling,
        monthly_previous_year=monthly_previous_year,
        product_spend_top=product_spend_top,
        product_spend_all=(payload.get("top_spend_full") or top_spend or []),
        product_weight_top=product_weight_top,
        product_weight_all=(payload.get("top_weight_full") or top_weight or []),
        product_profit_rows=table_rows,
        product_profit_chart=profit_mix or table_rows,
        weekday_labels=weekday_payload.get("labels", []),
        weekday_revenue=weekday_payload.get("revenue", []),
        weekday_weight=weekday_payload.get("weight_lb", []),
        weekday_orders=weekday_payload.get("orders", []),
        yoy_months=seasonality_payload.get("months", []),
        yoy_matrix=seasonality_payload.get("matrix", []),
        seasonality_years=seasonality_payload.get("years", []),
        cadence_days=cadence_payload.get("days", []),
        basket_stats=basket_stats,
        price_table=price_table,
        cross_sell=cross_sell_payload,
        categories=category_rows,
        profile=profile_payload,
        next_best_actions=next_best_actions,
        hero=hero_payload,
        executive_scorecard=scorecard_payload,
        priority_engine=priority_engine,
        trend_summary=trend_summary,
        lifecycle=lifecycle_payload,
        weight_analytics=weight_analytics,
        product_intelligence=product_intelligence,
        pricing_intelligence=pricing_intelligence,
        ordering_rhythm=ordering_rhythm,
        concentration=concentration_payload,
        crm_workspace=crm_workspace,
        trust_coverage=trust_coverage,
        chart_states=chart_states,
        payload=payload,
        workspace_chart_payload=workspace_chart_payload,
        scope_info=scope_info,
        export_links=export_links,
        window_start=meta_payload.get("window_start"),
        window_end=meta_payload.get("window_end"),
        generated_at=meta_payload.get("generated_at"),
        dataset_version=meta_payload.get("dataset_version"),
        window_reference_date=meta_payload.get("window_reference_date"),
        show_costs=show_costs,
    )
    """
    Render customer drilldown dashboard.
    
    Fix applied: Enforced strict filtering of base dataframe to customer_id before any aggregation
    to prevent global totals from leaking into charts. Implemented robust column resolution
    (dates, quantity, weight) to handle diverse schema variations.
    """
    base_df = get_fact_df()
    # 1. Scoping and Global Filters
    df = base_df
    show_costs = can_view_costs(current_user)

    params = _query_filter_params()
    effective_params = _clamp_params_to_data_window(df, params)
    df_f = apply_filter_params(df, effective_params)

    # 2. Filter to Customer (Strict)
    # Ensure we match string to string to avoid type mismatches
    if "CustomerId" not in df_f.columns:
        # Fallback if column missing
        df_c = pd.DataFrame(columns=df_f.columns)
    else:
        df_c = df_f[df_f["CustomerId"].astype(str) == str(customer_id)].copy()

    # If empty, render empty view immediately
    if df_c.empty:
        return render_template(
            "customers/drilldown.html",
            customer_id=customer_id,
            customer_name=str(customer_id),
            kpis={}, months=[], monthly_revenue=[], monthly_orders=[],
            monthly_cost=[], monthly_profit=[], monthly_margin=[],
            monthly_previous_year=[],
            product_spend_top=[], product_weight_top=[],
            product_profit_rows=[], product_profit_chart={"labels": [], "profit": [], "margin": []},
            weekday_labels=[], weekday_revenue=[],
            yoy_months=[], yoy_matrix=[],
            cadence_days=[], basket_stats={},
            price_table=[], cross_sell=[],
            show_costs=show_costs,
        )

    # 3. Column Resolution (Robust)
    
    # Ensure ProductName exists (fallback to cache/products.parquet if missing)
    if "ProductName" not in df_c.columns or df_c["ProductName"].isnull().all():
        try:
            prod_cache_path = "cache/products.parquet"
            if os.path.exists(prod_cache_path):
                # Load dimension columns only
                prod_dim = pd.read_parquet(prod_cache_path, columns=["product_id", "product_name"])
                # Deduplicate just in case
                prod_dim = prod_dim.drop_duplicates(subset=["product_id"])
                prod_dim = prod_dim.rename(columns={"product_id": "ProductId", "product_name": "ProductName_dim"})
                
                # Merge
                if "ProductId" in df_c.columns:
                    # Align types for merge
                    df_c["ProductId"] = df_c["ProductId"].astype(str)
                    prod_dim["ProductId"] = prod_dim["ProductId"].astype(str)
                    
                    df_c = df_c.merge(prod_dim, on="ProductId", how="left")
                    df_c["ProductName"] = df_c.get("ProductName", pd.Series(pd.NA)).fillna(df_c["ProductName_dim"])
        except Exception as e:
            pass # Keep going if fallback fails

    # Create SkuName column
    if "SkuName" not in df_c.columns:
        p_name = df_c["ProductName"].astype("string").fillna("Unknown") if "ProductName" in df_c.columns else pd.Series("Unknown", index=df_c.index)
        p_sku = df_c["SKU"].astype("string").fillna("-") if "SKU" in df_c.columns else pd.Series("-", index=df_c.index)
        df_c["SkuName"] = p_name + " (" + p_sku + ")"

    # Use SkuName for display
    df_c["sku_display"] = df_c["SkuName"]
    prod_label_col = "sku_display"

    # Identify key columns using user-specified lists + fallback
    rev_col = au.resolve_column(df_c, ("Revenue", "ExtPrice", "Sales", "revenue_ordered")) or "Revenue"
    cost_col = au.resolve_column(df_c, ("Cost", "ExtCost", "cost_ordered")) 
    
    qty_col = au.resolve_column(df_c, ("Qty", "Quantity", "Units", "QuantityShipped"))
    weight_col = au.resolve_column(df_c, ("WeightLb", "ShippedLb", "Lb", "pack_weight_lb_sum"))
    order_col = au.resolve_column(df_c, ("OrderId", "InvoiceNo", "OrderNo"))
    
    # Date resolution
    date_s = _pick_date_series(df_c)
    
    # Pre-calculate series for the Customer Scope
    revenue_series = pd.to_numeric(df_c.get(rev_col, 0), errors="coerce").fillna(0.0)
    cost_series = pd.to_numeric(df_c.get(cost_col, 0), errors="coerce").fillna(0.0) if cost_col else None
    
    # 4. KPIs Calculation
    total_revenue = float(round(revenue_series.sum(), 2))
    
    if order_col:
        total_orders = int(df_c[order_col].nunique())
    else:
        total_orders = 0
        
    aov = float(round(total_revenue / total_orders, 2)) if total_orders > 0 else 0.0

    # Date-based KPIs
    first_order_date = date_s.min()
    last_order_date = date_s.max()
    
    # Normalize dates for calc
    first_naive = au.normalize_datetime(first_order_date)
    last_naive = au.normalize_datetime(last_order_date)
    
    days_span = None
    if pd.notna(first_naive) and pd.notna(last_naive):
        days_span = (last_naive - first_naive).days

    months_active = None
    if pd.notna(first_naive) and pd.notna(last_naive):
        months_active = (last_naive.year - first_naive.year) * 12 + (last_naive.month - first_naive.month) + 1

    now_naive = pd.Timestamp.now().normalize()
    days_since_last_order = None
    if pd.notna(last_naive):
        days_since_last_order = (now_naive - last_naive).days

    # Recent Trends (Last 90d, 30d)
    ref_date = last_naive if pd.notna(last_naive) else now_naive
    
    revenue_last_30 = revenue_last_90 = revenue_prev_90 = 0.0
    orders_last_30 = orders_last_90 = orders_prev_90 = 0
    profit_last_90 = margin_last_90 = avg_ticket_last_90 = None

    if pd.notna(ref_date):
        # Ensure date_s is comparable (naive)
        date_compare = au.normalize_datetime(date_s)
        
        cutoff_30 = ref_date - pd.Timedelta(days=30)
        cutoff_90 = ref_date - pd.Timedelta(days=90)
        cutoff_180 = ref_date - pd.Timedelta(days=180)

        mask_30 = date_compare >= cutoff_30
        mask_90 = date_compare >= cutoff_90
        mask_prev_90 = (date_compare < cutoff_90) & (date_compare >= cutoff_180)

        revenue_last_30 = float(round(revenue_series[mask_30].sum(), 2))
        revenue_last_90 = float(round(revenue_series[mask_90].sum(), 2))
        revenue_prev_90 = float(round(revenue_series[mask_prev_90].sum(), 2))

        if order_col:
            orders_last_30 = int(df_c.loc[mask_30, order_col].nunique())
            orders_last_90 = int(df_c.loc[mask_90, order_col].nunique())
            orders_prev_90 = int(df_c.loc[mask_prev_90, order_col].nunique())

        if orders_last_90 > 0:
            avg_ticket_last_90 = float(round(revenue_last_90 / orders_last_90, 2))
            
        if show_costs and cost_series is not None:
            profit_s = revenue_series - cost_series
            profit_last_90 = float(round(profit_s[mask_90].sum(), 2))
            if revenue_last_90 != 0:
                margin_last_90 = float(round((profit_last_90 / revenue_last_90) * 100.0, 1))

    revenue_trend_90 = None
    if revenue_prev_90 > 0:
        revenue_trend_90 = float(round(((revenue_last_90 - revenue_prev_90) / revenue_prev_90) * 100.0, 1))
        
    orders_trend_90 = orders_last_90 - orders_prev_90

    # Per Month Averages
    revenue_per_month = float(round(total_revenue / months_active, 2)) if months_active and months_active > 0 else 0.0
    orders_per_month = float(round(total_orders / months_active, 2)) if months_active and months_active > 0 else 0.0

    # Metadata
    unique_products = int(df_c[prod_label_col].nunique())
    
    region_col = au.resolve_column(df_c, ("RegionName", "Region", "Region_Name"))
    primary_region = _mode_or_first(df_c[region_col]) if region_col else None
    
    ship_method_col = au.resolve_column(df_c, ("ShippingMethod", "ShipVia", "ShippingMethodName"))
    primary_shipping = _mode_or_first(df_c[ship_method_col]) if ship_method_col else None

    # Profit Totals
    profit_val = margin_pct = None
    if show_costs and cost_series is not None:
        profit_total_s = revenue_series - cost_series
        profit_val = float(round(profit_total_s.sum(), 2))
        if total_revenue != 0:
            margin_pct = float(round((profit_val / total_revenue) * 100.0, 2))

    # Segment Logic
    if total_revenue >= 250000:
        segment = "Enterprise"
    elif total_revenue >= 100000:
        segment = "Key Account"
    elif total_revenue >= 30000:
        segment = "Growth"
    else:
        segment = "Emerging"

    kpis_data = {
        "total_revenue": total_revenue,
        "total_orders": total_orders,
        "aov": aov,
        "first_order": first_naive.strftime("%Y-%m-%d") if pd.notna(first_naive) else None,
        "last_order": last_naive.strftime("%Y-%m-%d") if pd.notna(last_naive) else None,
        "days_span": days_span,
        "profit": profit_val,
        "margin_pct": margin_pct,
        "revenue_last_30": revenue_last_30,
        "revenue_last_90": revenue_last_90,
        "revenue_prev_90": revenue_prev_90,
        "revenue_trend_90": revenue_trend_90,
        "orders_last_30": orders_last_30,
        "orders_last_90": orders_last_90,
        "orders_prev_90": orders_prev_90,
        "orders_trend_90": orders_trend_90,
        "avg_ticket_last_90": avg_ticket_last_90,
        "profit_last_90": profit_last_90,
        "margin_last_90": margin_last_90,
        "months_active": months_active,
        "days_since_last_order": days_since_last_order,
        "revenue_per_month": revenue_per_month,
        "orders_per_month": orders_per_month,
        "unique_products": unique_products,
        "primary_region": primary_region,
        "primary_shipping": primary_shipping,
        "segment": segment,
        "segment_reason": f"Revenue ${total_revenue:,.0f} across {total_orders} orders"
    }

    # 5. Charts Data Construction
    
    # Monthly Revenue & Orders
    months, monthly_revenue, monthly_orders = [], [], []
    monthly_cost, monthly_profit, monthly_margin = [], [], []
    monthly_previous_year = []
    
    if pd.notna(date_s).any() and order_col:
        # Group by Month
        m_frame = pd.DataFrame({
            "Month": date_s.dt.to_period("M").dt.to_timestamp(),
            "Revenue": revenue_series,
            "Order": df_c[order_col]
        })
        if show_costs and cost_series is not None:
            m_frame["Cost"] = cost_series
        
        grp = m_frame.groupby("Month", observed=True)
        agg = grp.agg({
            "Revenue": "sum",
            "Order": "nunique",
            **({"Cost": "sum"} if "Cost" in m_frame.columns else {})
        }).sort_index()
        
        months = [d.strftime("%Y-%m") for d in agg.index]
        monthly_revenue = [float(round(v, 2)) for v in agg["Revenue"]]
        monthly_orders = [int(v) for v in agg["Order"]]
        
        # YoY Trend Line Calculation
        # Create a lookup for all revenue by month (including months not in the view if needed)
        rev_lookup = agg["Revenue"].to_dict()
        for d in agg.index:
            prev_year_date = d - pd.DateOffset(years=1)
            # Find the revenue for the same month in the previous year
            # We need to handle potential slight day mismatches if to_timestamp defaulted differently, 
            # but here it's consistent.
            val = rev_lookup.get(prev_year_date, 0.0)
            monthly_previous_year.append(float(round(val, 2)))

        if "Cost" in agg.columns:
            monthly_cost = [float(round(v, 2)) for v in agg["Cost"]]
            monthly_profit = [float(round(r - c, 2)) for r, c in zip(agg["Revenue"], agg["Cost"], strict=False)]
            monthly_margin = []
            for r, p in zip(agg["Revenue"], monthly_profit, strict=False):
                if r > 0:
                    monthly_margin.append(float(round((p/r)*100.0, 1)))
                else:
                    monthly_margin.append(None)

    # Top Products by Spend
    product_spend_top = []
    spend_mix = (
        pd.DataFrame({"label": df_c[prod_label_col], "rev": revenue_series})
        .groupby("label", observed=True)["rev"].sum().reset_index()
    )
    spend_mix = spend_mix[spend_mix["rev"] > 0].sort_values("rev", ascending=False).head(15)
    for _, r in spend_mix.iterrows():
        product_spend_top.append({"label": str(r["label"]), "revenue": float(round(r["rev"], 2))})
        
    if product_spend_top:
        kpis_data["top_product_label"] = product_spend_top[0]["label"]
        kpis_data["top_product_revenue"] = product_spend_top[0]["revenue"]

    # Top Products by Weight
    product_weight_top = []
    if weight_col:
        w_series = pd.to_numeric(df_c[weight_col], errors="coerce").fillna(0.0)
        weight_mix = (
            pd.DataFrame({"label": df_c[prod_label_col], "w": w_series})
            .groupby("label", observed=True)["w"].sum().reset_index()
        )
        weight_mix = weight_mix[weight_mix["w"] > 0].sort_values("w", ascending=False).head(15)
        for _, r in weight_mix.iterrows():
            product_weight_top.append({"label": str(r["label"]), "weight_lb": float(round(r["w"], 2))})
            
        if product_weight_top:
            kpis_data["top_weight_product"] = product_weight_top[0]["label"]
            kpis_data["top_weight_lb"] = product_weight_top[0]["weight_lb"]

    # Product Profitability
    product_profit_rows = []
    product_profit_chart = {"labels": [], "profit": [], "margin": []}
    
    if show_costs and cost_series is not None:
        p_frame = pd.DataFrame({
            "label": df_c[prod_label_col],
            "rev": revenue_series,
            "cost": cost_series
        })
        p_grp = p_frame.groupby("label", observed=True).sum().reset_index()
        p_grp["profit"] = p_grp["rev"] - p_grp["cost"]
        with np.errstate(divide="ignore", invalid="ignore"):
             p_grp["margin"] = np.where(p_grp["rev"] > 0, (p_grp["profit"] / p_grp["rev"]) * 100.0, np.nan)
        
        p_grp = p_grp.sort_values("profit", ascending=False)
        
        # Table rows (top 25)
        for _, r in p_grp.head(25).iterrows():
            product_profit_rows.append({
                "product": str(r["label"]),
                "revenue": float(round(r["rev"], 2)),
                "cost": float(round(r["cost"], 2)),
                "profit": float(round(r["profit"], 2)),
                "margin_pct": float(round(r["margin"], 1)) if pd.notna(r["margin"]) else None
            })
            
        # Chart (top 10)
        top10 = p_grp.head(10)
        product_profit_chart["labels"] = top10["label"].astype(str).tolist()
        product_profit_chart["profit"] = [float(round(v, 2)) for v in top10["profit"]]
        product_profit_chart["margin"] = [float(round(v, 1)) if pd.notna(v) else None for v in top10["margin"]]

        if not top10.empty:
            kpis_data["top_profit_product"] = str(top10.iloc[0]["label"])
            kpis_data["top_profit_value"] = float(round(top10.iloc[0]["profit"], 2))
            kpis_data["top_profit_margin"] = float(round(top10.iloc[0]["margin"], 1)) if pd.notna(top10.iloc[0]["margin"]) else None

    # Weekday Seasonality
    weekday_labels, weekday_revenue = [], []
    if pd.notna(date_s).any():
        wd_frame = pd.DataFrame({"day": date_s.dt.day_name(), "rev": revenue_series})
        wd_grp = wd_frame.groupby("day", observed=True)["rev"].sum()
        days_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        
        for d in days_order:
            if d in wd_grp.index:
                weekday_labels.append(d)
                weekday_revenue.append(float(round(wd_grp[d], 2)))
                
        if weekday_revenue:
            max_idx = np.argmax(weekday_revenue)
            kpis_data["best_weekday"] = weekday_labels[max_idx]
            kpis_data["best_weekday_revenue"] = weekday_revenue[max_idx]

    # YoY Matrix
    yoy_months, yoy_matrix = [], []
    if pd.notna(date_s).any():
        yoy_frame = pd.DataFrame({
            "Year": date_s.dt.year,
            "Month": date_s.dt.month,
            "Rev": revenue_series
        })
        piv = yoy_frame.pivot_table(index="Year", columns="Month", values="Rev", aggfunc="sum").fillna(0.0).sort_index()
        yoy_months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        for _, row in piv.iterrows():
            row_vals = []
            for m in range(1, 13):
                row_vals.append(float(round(row.get(m, 0.0), 2)))
            yoy_matrix.append(row_vals)

    # Buying Cadence
    cadence_days = []
    if pd.notna(date_s).any() and order_col:
        cad_frame = pd.DataFrame({"d": date_s, "ord": df_c[order_col]}).dropna().drop_duplicates(subset=["ord"]).sort_values("d")
        if len(cad_frame) > 1:
            diffs = cad_frame["d"].diff().dt.days.dropna()
            cadence_days = [float(x) for x in diffs if x is not None]

    if cadence_days:
        kpis_data["cadence_avg_days"] = float(round(np.mean(cadence_days), 1))
        kpis_data["cadence_median_days"] = float(round(np.median(cadence_days), 1))
        kpis_data["cadence_min_days"] = float(round(min(cadence_days), 1))
        kpis_data["cadence_max_days"] = float(round(max(cadence_days), 1))
    else:
        kpis_data.update(dict.fromkeys(["cadence_avg_days", "cadence_median_days", "cadence_min_days", "cadence_max_days"]))

    # Basket Stats
    basket_stats = {}
    if order_col:
        grp = df_c.groupby(order_col, observed=True)
        # Avg Weight per Order
        avg_w = 0.0
        if weight_col:
            w_s = pd.to_numeric(df_c[weight_col], errors="coerce").fillna(0.0)
            avg_w = w_s.groupby(df_c[order_col]).sum().mean()

        # Avg Items/Qty per Order
        avg_q = 0.0
        if qty_col:
             q_s = pd.to_numeric(df_c[qty_col], errors="coerce").fillna(0.0)
             avg_q = q_s.groupby(df_c[order_col]).sum().mean()
             
        basket_stats = {
            "orders": int(grp.ngroups),
            "avg_lines_per_order": float(round(grp.size().mean(), 2)),
            "avg_weight_lb": float(round(avg_w, 2)),
            "avg_items": float(round(avg_q, 1))
        }

    # Price Benchmarking (Uses global df_f for context, but filters to region if applicable)
    price_table = []
    try:
        peers = _peer_price_benchmark(df_f, df_c, prod_label_col, region_col)
        if not peers.empty:
            peers = peers.sort_values("delta_pct", ascending=True)
            for idx, row in peers.head(25).iterrows():
                price_table.append({
                    "product": str(idx),
                    "cust_unit_price": float(row["cust_unit_price"]) if pd.notna(row["cust_unit_price"]) else None,
                    "peer_median": float(row["peer_median"]) if pd.notna(row["peer_median"]) else None,
                    "delta_pct": float(round(row["delta_pct"], 1)) if pd.notna(row["delta_pct"]) else None,
                    "suggest_price": float(row["suggest_price"]) if pd.notna(row["suggest_price"]) else None
                })
    except Exception:
        pass

    if price_table:
        deltas = [r["delta_pct"] for r in price_table if r["delta_pct"] is not None]
        if deltas:
            kpis_data["price_delta_max"] = float(round(max(deltas), 1))
            kpis_data["price_delta_min"] = float(round(min(deltas), 1))
    else:
        kpis_data["price_delta_max"] = kpis_data["price_delta_min"] = None
        kpis_data["price_rows"] = 0
    kpis_data["price_rows"] = len(price_table)

    # Cross Sell (Uses global df_f)
    cross_sell = _cross_sell_suggestions(df_f, df_c, prod_label_col, region_col, top_k=5)
    kpis_data["cross_sell_count"] = len(cross_sell)

    # Churn Risk
    churn_probability = None
    churn_risk = "Unknown"
    try:
        from ..core import ml as churn_ml
        bundle = churn_ml.get_cached_churn_model()
        if bundle and not df_f.empty:
            scores = churn_ml.score_churn(df_f, bundle)
            if scores is not None and not scores.empty:
                # Filter to this customer
                match = scores[scores["CustomerId"].astype(str) == str(customer_id)]
                if not match.empty:
                    churn_probability = float(match.iloc[0]["churn_prob"])
    except Exception:
        pass

    if churn_probability is not None:
        churn_probability = max(0.0, min(1.0, churn_probability))
        if churn_probability >= 0.7:
            churn_risk = "High"
        elif churn_probability >= 0.4:
            churn_risk = "Medium"
        else:
            churn_risk = "Low"
        kpis_data["churn_probability"] = float(round(churn_probability, 3))
        kpis_data["churn_probability_pct"] = float(round(churn_probability * 100.0, 1))
    else:
        kpis_data["churn_probability"] = None
        kpis_data["churn_probability_pct"] = None
    kpis_data["churn_risk"] = churn_risk
    
    # Friendly Name
    name_col = au.resolve_column(df_c, ("CustomerName", "customer_name", "Name", "Customer"))
    customer_name = str(df_c[name_col].iloc[0]) if name_col and not df_c[name_col].empty else str(customer_id)

    return render_template(
        "customers/drilldown.html",
        customer_id=customer_id,
        customer_name=customer_name,
        kpis=kpis_data,
        months=months, monthly_revenue=monthly_revenue, monthly_orders=monthly_orders,
        monthly_cost=monthly_cost, monthly_profit=monthly_profit, monthly_margin=monthly_margin,
        monthly_previous_year=monthly_previous_year,
        product_spend_top=product_spend_top, product_weight_top=product_weight_top,
        product_profit_rows=product_profit_rows, product_profit_chart=product_profit_chart,
        weekday_labels=weekday_labels, weekday_revenue=weekday_revenue,
        yoy_months=yoy_months, yoy_matrix=yoy_matrix,
        cadence_days=cadence_days, basket_stats=basket_stats,
        price_table=price_table, cross_sell=cross_sell,
        show_costs=show_costs,
    )

# ─────────────────────────────────────────────────────────────────────────────
# Cohorts + export
# ─────────────────────────────────────────────────────────────────────────────

_COHORTS_STATE_SESSION_KEY = "customers.cohorts.v2.state"
_COHORTS_STATE_KEYS = {
    "threshold",
    "lookback_months",
    "cohort_granularity",
    "cohort_horizon",
    "reactivation_window_days",
    "at_risk_window_days",
    "status",
    "segmentation",
    "table_search",
    "table_page",
    "page_size",
}
_COHORTS_NON_FILTER_KEYS = _COHORTS_STATE_KEYS | {
    "csrf_token",
    "dataset",
    "format",
    "cohort",
    "month_index",
    "search",
    "page",
}

def _cohorts_v2_enabled() -> bool:
    try:
        return bool(current_app.config.get("COHORTS_V2", False))
    except Exception:
        return False


def _cohorts_state_v2_enabled() -> bool:
    try:
        raw = current_app.config.get("COHORTS_STATE_V2")
    except Exception:
        raw = None
    if raw is None:
        raw = os.getenv("COHORTS_STATE_V2", "1")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _cohorts_hardened_v3_enabled() -> bool:
    try:
        raw = current_app.config.get("COHORTS_HARDENED_V3")
    except Exception:
        raw = None
    if raw is None:
        raw = os.getenv("COHORTS_HARDENED_V3", "1")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _cohorts_debug_ui_enabled() -> bool:
    try:
        raw = current_app.config.get("COHORTS_DEBUG_UI")
    except Exception:
        raw = None
    if raw is None:
        raw = os.getenv("COHORTS_DEBUG_UI", "")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _cohorts_state_query_pairs(state: customers_cohorts_v2.CohortResolvedState | None) -> list[tuple[str, str]]:
    if state is None:
        return []
    pairs: list[tuple[str, str]] = []
    for key, value in state.as_query_params().items():
        if value is None:
            continue
        pairs.append((str(key), str(value)))
    return pairs


def _cohorts_filter_source(source: Any) -> Any:
    if not hasattr(source, "lists"):
        return source
    filtered = MultiDict()
    for key, values in source.lists():
        if str(key) in _COHORTS_NON_FILTER_KEYS:
            continue
        for value in values:
            filtered.add(key, value)
    return filtered


def _cohorts_passthrough_pairs(source: Any) -> list[tuple[str, str]]:
    if not hasattr(source, "lists"):
        return []
    pairs: list[tuple[str, str]] = []
    for key, values in source.lists():
        if key in _COHORTS_NON_FILTER_KEYS:
            continue
        for value in values:
            pairs.append((str(key), str(value)))
    return pairs


def _cohorts_redirect_url(state: customers_cohorts_v2.CohortResolvedState, source: Any) -> str:
    pairs = _cohorts_passthrough_pairs(source)
    pairs.extend(_cohorts_state_query_pairs(state))
    query = urlencode(pairs, doseq=True)
    base = url_for("customers.cohorts")
    return f"{base}?{query}" if query else base


def _cohorts_context(args_source: Any) -> tuple[Any, Dict[str, Any], Dict[str, Any]]:
    sticky_enabled = bool(current_app.config.get("STICKY_FILTERS", True))
    filter_source = _cohorts_filter_source(args_source)
    filters, filters_meta = resolve_filters(
        request,
        current_user,
        session_obj=session,
        source=filter_source,
        sticky_enabled=sticky_enabled,
    )
    scope = filters_service.scope_from_user(current_user)
    return filters, scope, filters_meta


def _resolve_cohorts_state(scope: Dict[str, Any]) -> customers_cohorts_v2.CohortResolvedState:
    if request.method == "POST":
        source = request.form
    else:
        source = request.args
    session_state = session.get(_COHORTS_STATE_SESSION_KEY, {}) if _cohorts_state_v2_enabled() else {}
    state = customers_cohorts_v2.resolve_cohorts_controls(
        source,
        session_state,
        allow_sales_rep=bool(scope.get("is_admin")),
    )
    if customers_cohorts_v2._cohorts_debug_enabled():
        current_app.logger.info(
            "cohorts_v2.state",
            extra={
                "request_id": getattr(getattr(current_app, "request_id", None), "request_id", None),
                "current_user_id": getattr(current_user, "id", None),
                "status": state.status,
                "segmentation": state.segmentation,
                "controls_source": state.source,
                "controls_hash": state.controls_hash,
                "scope_hash": scope.get("scope_hash"),
            },
        )
    return state


def _apply_cohorts_state_response(scope: Dict[str, Any]):
    state = customers_cohorts_v2.resolve_cohorts_controls(
        request.form,
        session.get(_COHORTS_STATE_SESSION_KEY, {}) if _cohorts_state_v2_enabled() else {},
        allow_sales_rep=bool(scope.get("is_admin")),
    )
    if _cohorts_state_v2_enabled():
        session[_COHORTS_STATE_SESSION_KEY] = state.as_session_state()
    for warning in state.warnings or ():
        flash(warning, "warning")
    return redirect(_cohorts_redirect_url(state, request.form))


def _cohorts_v2_bundle(
    args_source: Any,
    filters: Any,
    scope: Dict[str, Any],
    state: customers_cohorts_v2.CohortResolvedState,
    filters_meta: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    dataset_version = fact_store.cache_buster()
    filters_hash = (filters_meta or {}).get("filters_hash") or filters_service.filters_hash(filters)
    state_hash = customers_cohorts_v2.state_hash_for(
        filters_hash,
        state.controls_hash,
        scope.get("scope_hash"),
        dataset_version,
    )
    extras = state.cache_extras()
    extras["state_hash"] = state_hash
    payload = cached_bundle(
        endpoint="customers.cohorts.v2",
        filters=filters,
        scope=scope,
        dataset_version=dataset_version,
        extras=extras,
        builder=lambda: customers_cohorts_v2.build_cohorts_payload(
            filters,
            scope,
            args_source,
            filters_meta=filters_meta,
            state=state,
        ),
    )
    meta = payload.setdefault("meta", {})
    meta.setdefault("dataset_version", dataset_version)
    meta.setdefault("cohorts_v2", True)
    meta.setdefault("controls_hash", state.controls_hash)
    meta.setdefault("controls_source", state.source)
    meta.setdefault("status", state.status)
    meta.setdefault("segmentation", state.segmentation)
    meta.setdefault("filters_hash", filters_hash)
    meta.setdefault("state_hash", state_hash)
    if customers_cohorts_v2._cohorts_debug_enabled():
        current_app.logger.info(
            "cohorts_v2.cache",
            extra={
                "current_user_id": getattr(current_user, "id", None),
                "controls_hash": state.controls_hash,
                "filters_hash": filters_hash,
                "state_hash": state_hash,
                "scope_hash": scope.get("scope_hash"),
                "dataset_version": dataset_version,
                "cache_key": meta.get("cache_key"),
                "cache_hit": meta.get("cached"),
            },
        )
    return payload


def _cohorts_export_response(frame: pd.DataFrame, filename_stem: str, *, dataset_name: str | None = None) -> Response:
    fmt = (request.args.get("format") or "xlsx").strip().lower()
    stem = sanitize_filename(filename_stem or dataset_name or "cohorts_export", default="cohorts_export")
    if fmt == "csv":
        return dataframe_to_csv_response(frame if frame is not None else pd.DataFrame(), filename=f"{stem}.csv")
    defs = customers_cohorts_v2.dataset_definitions_frame(dataset_name or filename_stem)
    sheets = {"Data": frame if frame is not None else pd.DataFrame(), "Definitions": defs}
    return dataframes_to_xlsx_response(sheets, filename=f"{stem}.xlsx")


def _cohort_metrics(cust_df: pd.DataFrame, threshold_days: int) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Return (cohort_retention, churn_by_region, churn_trend_by_last_month)."""
    if cust_df.empty:
        return (
            pd.DataFrame(columns=["CohortMonth", "Customers", "Retained", "RetentionRate"]),
            pd.DataFrame(columns=["Region", "Churned", "Customers", "ChurnRate"]),
            pd.DataFrame(columns=["Month", "Churned"]),
        )

    df = cust_df.copy()
    df["CohortMonth"] = pd.to_datetime(df["FirstOrder"]).dt.to_period("M").dt.to_timestamp()
    df["Retained"] = df["DaysSinceLastOrder"].fillna(999999).astype(float) <= float(threshold_days)
    cohort = df.groupby("CohortMonth", observed=True).agg(
        Customers=("CustomerId", "nunique"),
        Retained=("Retained", "sum"),
    ).reset_index()
    cohort["RetentionRate"] = (cohort["Retained"] / cohort["Customers"]).fillna(0) * 100.0

    df["Churned"] = ~df["Retained"]
    region_col = au.resolve_column(df, ("RegionName", "Region_Name", "Region", "region_name"))
    if region_col is None:
        churn_by_region = pd.DataFrame(columns=["Region", "Churned", "Customers", "ChurnRate"])
    else:
        by_region = df.groupby(region_col, observed=True).agg(
            Customers=("CustomerId", "nunique"),
            Churned=("Churned", "sum"),
        ).reset_index().rename(columns={region_col: "Region"})
        by_region["ChurnRate"] = (by_region["Churned"] / by_region["Customers"]).fillna(0) * 100.0
        churn_by_region = by_region

    df["LastOrderMonth"] = pd.to_datetime(df["LastOrder"]).dt.to_period("M").dt.to_timestamp()
    churn_trend = (
        df[df["Churned"]]
        .groupby("LastOrderMonth", observed=True)["CustomerId"]
        .nunique()
        .reset_index()
        .rename(columns={"LastOrderMonth": "Month", "CustomerId": "Churned"})
        .sort_values("Month")
    )

    cohort["RetentionRate"] = cohort["RetentionRate"].round(2)
    churn_by_region["ChurnRate"] = churn_by_region["ChurnRate"].round(2)

    return cohort, churn_by_region, churn_trend

@bp.route("/cohorts", methods=["GET", "POST"])
@login_required
@permission_required("page.customers.view")
def cohorts():
    if not _cohorts_v2_enabled():
        payload = _customers_bundle_payload("cohorts")
        cohorts_payload = (payload.get("cohorts") or {}) if isinstance(payload, dict) else {}
        heatmap = cohorts_payload.get("heatmap") or {}
        cohort_labels = heatmap.get("cohorts") or []
        activity_labels = heatmap.get("activity") or []
        matrix_rows_raw = heatmap.get("rows") or []
        matrix_rows = [[float(v or 0.0) for v in row] for row in matrix_rows_raw]
        cohort_retention = []
        for row in matrix_rows:
            vals = [v for v in row if v is not None]
            cohort_retention.append(sum(vals) / len(vals) if vals else 0.0)
        return render_template(
            "customers/cohorts.html",
            form=_EmptyForm(),
            threshold=int(request.values.get("threshold", 90) or 90),
            cohort_labels=cohort_labels,
            cohort_retention=cohort_retention,
            region_labels=[],
            region_churn=[],
            trend_months=activity_labels,
            trend_values=[max(row) if row else 0 for row in matrix_rows],
        )

    args_source = request.form if request.method == "POST" else request.args
    filters, scope, filters_meta = _cohorts_context(args_source)
    if request.method == "POST" and _cohorts_state_v2_enabled():
        return _apply_cohorts_state_response(scope)

    state = _resolve_cohorts_state(scope)
    payload = _cohorts_v2_bundle(request.args, filters, scope, state, filters_meta)
    table_payload = customers_cohorts_v2.fetch_churn_status_list(
        filters,
        scope,
        request.args,
        filters_meta=filters_meta,
        state=state,
    )
    payload["table"] = table_payload

    return render_template(
        "customers/cohorts_v2.html",
        form=_EmptyForm(),
        payload=payload,
        table=table_payload,
        cohort_state=state.display_state(),
        cohort_state_query=state.as_query_params(),
        cohort_passthrough_pairs=_cohorts_passthrough_pairs(request.args),
        cohort_state_action_url=url_for("customers.cohorts_apply_controls") if _cohorts_state_v2_enabled() else url_for("customers.cohorts"),
        is_admin_scope=bool(scope.get("is_admin")),
        cohorts_v2_enabled=True,
        cohorts_state_v2_enabled=_cohorts_state_v2_enabled(),
        cohorts_hardened_v3_enabled=_cohorts_hardened_v3_enabled(),
        cohorts_debug_ui_enabled=bool(scope.get("is_admin")) and _cohorts_debug_ui_enabled(),
    )


@bp.post("/cohorts/controls/apply")
@login_required
@permission_required("page.customers.view")
def cohorts_apply_controls():
    if not _cohorts_v2_enabled():
        return redirect(url_for("customers.cohorts"))
    filters, scope, _filters_meta = _cohorts_context(request.form)
    _ = filters  # preserve filter resolution side effects and validation
    if not _cohorts_state_v2_enabled():
        return redirect(_cohorts_redirect_url(_resolve_cohorts_state(scope), request.form))
    return _apply_cohorts_state_response(scope)


@bp.get("/cohorts/drilldown")
@login_required
@permission_required("page.customers.view")
def cohorts_drilldown():
    if not _cohorts_v2_enabled():
        return jsonify({"error": {"message": "Cohorts v2 is disabled"}}), 404
    filters, scope, filters_meta = _cohorts_context(request.args)
    state = _resolve_cohorts_state(scope)
    payload = customers_cohorts_v2.fetch_cohort_drilldown(
        filters,
        scope,
        request.args,
        filters_meta=filters_meta,
        state=state,
    )
    return jsonify(payload)


@bp.get("/churned/list")
@login_required
@permission_required("page.customers.view")
def churned_list():
    if not _cohorts_v2_enabled():
        return jsonify({"error": {"message": "Cohorts v2 is disabled"}}), 404
    filters, scope, filters_meta = _cohorts_context(request.args)
    state = _resolve_cohorts_state(scope)
    payload = customers_cohorts_v2.fetch_churn_status_list(
        filters,
        scope,
        request.args,
        filters_meta=filters_meta,
        state=state,
    )
    return jsonify(payload)


@bp.get("/cohorts/export")
@login_required
@permission_required("page.customers.view")
def cohorts_export():
    if not _cohorts_v2_enabled():
        return jsonify({"error": {"message": "Cohorts v2 is disabled"}}), 404

    dataset = (request.args.get("dataset") or "status_list").strip().lower()
    filters, scope, filters_meta = _cohorts_context(request.args)
    state = _resolve_cohorts_state(scope)
    try:
        frame, stem = customers_cohorts_v2.build_export_dataset(
            filters,
            scope,
            request.args,
            dataset,
            filters_meta=filters_meta,
            state=state,
        )
    except ValueError as exc:
        return jsonify({"error": {"message": str(exc)}}), 400
    except Exception:
        return jsonify({"error": {"message": "Failed to build cohort export"}}), 500
    return _cohorts_export_response(frame, stem, dataset_name=dataset)


@bp.route("/cohorts/download")
@login_required
@permission_required("page.customers.view")
def cohorts_download():
    threshold = request.values.get("threshold", type=int) or 90
    base_df = get_fact_df()
    df = base_df
    params = _query_filter_params()
    effective_params = _clamp_params_to_data_window(base_df, params)
    df_f = apply_filter_params(df, effective_params)
    cust = _customer_agg(df_f)

    region_col = au.resolve_column(df_f, ("RegionName", "Region_Name", "region_name", "Region"))
    if region_col:
        region_mode = (
            df_f[["CustomerId", region_col]].dropna()
            .groupby("CustomerId", observed=True)[region_col]
            .agg(lambda s: s.mode().iloc[0] if not s.mode().empty else s.iloc[0])
        )
        cust = cust.merge(region_mode.rename("Region_Name"), left_on="CustomerId", right_index=True, how="left")

    addr_cols = ["Address1", "Address2", "City", "Province", "PostalCode"]
    have = [c for c in addr_cols if c in df_f.columns]
    addr = df_f.drop_duplicates(subset=["CustomerId"]).set_index("CustomerId")[have]
    cust = cust.set_index("CustomerId").join(addr, how="left").reset_index()

    cust["Churned"] = cust["DaysSinceLastOrder"].fillna(999999).astype(float) > float(threshold)
    churned = cust[cust["Churned"]].copy()
    churned["Profit"] = churned["TotalRevenue"] - churned["TotalCost"]
    churned = _round2_df(churned)

    try:
        log_audit(current_user, "export", {"resource": "customers_cohorts", "threshold": threshold})
        from flask import g as _g
        _g._export_logged = True
    except Exception:
        pass

    fmt = (request.values.get("format") or "xlsx").lower()
    fname = f"churned_customers_{threshold}d"
    if fmt == "csv":
        return dataframe_to_csv_response(churned, filename=f"{fname}.csv")
    return dataframes_to_xlsx_response({"ChurnedCustomers": churned}, filename=f"{fname}.xlsx")

# ─────────────────────────────────────────────────────────────────────────────
# RFM
# ─────────────────────────────────────────────────────────────────────────────

def _rfm_scores(recency: pd.Series, frequency: pd.Series, monetary: pd.Series) -> pd.DataFrame:
    def qscore(s: pd.Series, reverse: bool = False) -> pd.Series:
        s = pd.to_numeric(s, errors="coerce")
        valid = s.dropna()

        # Handle edge cases: not enough data or all same values
        if len(valid) == 0:
            return pd.Series(1, index=s.index, dtype=int)
        if valid.nunique() <= 1:
            return pd.Series(1, index=s.index, dtype=int)

        # Determine optimal number of quantiles based on unique values
        n_unique = valid.nunique()
        n_quantiles = min(4, n_unique)

        try:
            # Try qcut with dynamic labels
            if n_quantiles < 4:
                # Use rank-based approach for low cardinality
                ranks = s.rank(method="average", na_option="keep")
                labels = list(range(1, n_quantiles + 1))
                bins = pd.qcut(ranks, q=n_quantiles, labels=labels, duplicates="drop")
                # Map to 1-4 scale
                bins = bins.astype("float")
                if n_quantiles > 1:
                    bins = ((bins - 1) / (n_quantiles - 1) * 3 + 1).round().fillna(1)
                else:
                    bins = bins.fillna(1)
            else:
                # Normal case with 4 quantiles
                bins = pd.qcut(s, q=4, labels=[1, 2, 3, 4], duplicates="drop")
                bins = bins.astype("float").fillna(1)

            bins = bins.astype(int)
            return (5 - bins) if reverse else bins

        except (ValueError, TypeError):
            # Fallback: use simple rank-based scoring
            ranks = s.rank(method="average", na_option="keep", pct=True)
            scores = (ranks * 3 + 1).round().fillna(1).astype(int)
            scores = scores.clip(1, 4)
            return (5 - scores) if reverse else scores

    r = qscore(recency, reverse=True)
    f = qscore(frequency, reverse=False)
    m = qscore(monetary, reverse=False)
    out = pd.DataFrame({"R_Score": r, "F_Score": f, "M_Score": m})
    out["RFM_Score"] = out[["R_Score", "F_Score", "M_Score"]].sum(axis=1)
    return out

@bp.route("/rfm")
@login_required
@requires_roles("sales", "sales_manager", "production", "gm", "owner", "admin")
def rfm():
    payload = _customers_bundle_payload("rfm")
    rfm_payload = (payload.get("rfm") or {}) if isinstance(payload, dict) else {}
    definitions = payload.get("definitions", {}) if isinstance(payload, dict) else {}
    show_costs = can_view_costs(current_user)
    try:
        filters_norm = _query_filter_params()
    except Exception:
        filters_norm = {}
    if _customers_rfm_v2_enabled():
        return render_template(
            "customers/rfm_v2.html",
            payload=payload,
            rfm=rfm_payload,
            definitions=definitions,
            show_costs=show_costs,
            filters=filters_norm,
        )

    segments = rfm_payload.get("segments") or []
    top = rfm_payload.get("top") or []
    scatter = rfm_payload.get("scatter") or {}
    seg_labels = [s.get("segment") for s in segments]
    seg_values = [s.get("count", 0) for s in segments]
    top_rows = [
        {
            "CustomerId": row.get("customer_id"),
            "CustomerName": row.get("customer_name"),
            "R_Score": row.get("r"),
            "F_Score": row.get("f"),
            "M_Score": row.get("m"),
            "RFM_Score": row.get("score"),
            "Monetary": row.get("monetary"),
            "Frequency": row.get("frequency"),
        }
        for row in top
    ]
    scatter_x = scatter.get("frequency") or []
    scatter_y = scatter.get("monetary") or []
    scatter_text = scatter.get("labels") or []
    rfm_scores = scatter.get("scores") or []
    return render_template(
        "customers/rfm.html",
        seg_labels=seg_labels,
        seg_values=seg_values,
        top_rows=top_rows,
        scatter_x=scatter_x,
        scatter_y=scatter_y,
        scatter_text=scatter_text,
        rfm_scores=rfm_scores,
    )
    cust = _customer_agg(df_f)
    if cust.empty:
        return render_template(
            "customers/rfm.html",
            seg_labels=[], seg_values=[], top_rows=[], scatter_x=[], scatter_y=[], scatter_text=[], rfm_scores=[],
        )

    cust["Recency"] = cust["DaysSinceLastOrder"]
    cust["Frequency"] = cust["TotalOrders"]
    cust["Monetary"] = cust["TotalRevenue"]

    scores = _rfm_scores(cust["Recency"], cust["Frequency"], cust["Monetary"])
    cust = pd.concat([cust, scores], axis=1)

    def seg(score: int) -> str:
        if score >= 10:
            return "Champion"
        if score >= 8:
            return "Loyal"
        if score >= 6:
            return "At Risk"
        return "Dormant"

    cust["Segment"] = cust["RFM_Score"].astype(int).map(seg)

    seg_counts = cust["Segment"].value_counts()
    seg_labels = seg_counts.index.tolist()
    seg_values = seg_counts.values.tolist()

    top = cust.sort_values(["RFM_Score", "Monetary"], ascending=[False, False]).head(10)
    top = _round2_df(top)
    top_rows = top[["CustomerId", "CustomerName", "R_Score", "F_Score", "M_Score", "RFM_Score", "Monetary", "Frequency"]].to_dict(orient="records")

    scatter_x = _round2_list(cust["Frequency"].astype(float).tolist())
    scatter_y = _round2_list(cust["Monetary"].astype(float).tolist())
    scatter_text = [f"{row['CustomerName'] or row['CustomerId']} (RFM={row['RFM_Score']})" for _, row in cust.iterrows()]
    rfm_scores = cust["RFM_Score"].tolist()

    return render_template(
        "customers/rfm.html",
        seg_labels=seg_labels, seg_values=seg_values,
        top_rows=top_rows,
        scatter_x=scatter_x, scatter_y=scatter_y, scatter_text=scatter_text,
        rfm_scores=rfm_scores,
    )

# ─────────────────────────────────────────────────────────────────────────────
# Unified chart → Excel export (server-side, production-ready)
# ─────────────────────────────────────────────────────────────────────────────

def _xlsx(frames: dict[str, pd.DataFrame], filename: str) -> Response:
    """Small wrapper to return a multi-sheet Excel file with rounding."""
    clean = {}
    for name, df in frames.items():
        if df is None:
            continue
        df = df.copy()
        df = _round2_df(df)
        clean[name[:31] or "Sheet1"] = df
    return dataframes_to_xlsx_response(clean, filename=filename)

def _month_key_series(dt_series: pd.Series) -> pd.Series:
    return pd.to_datetime(dt_series, errors="coerce").dt.to_period("M").dt.to_timestamp()

def _weekday_order():
    return ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

def _drilldown_frames(df_f: pd.DataFrame, customer_id: str) -> dict[str, pd.DataFrame]:
    """Builds all drilldown chart DataFrames for a given customer."""
    frames: dict[str, pd.DataFrame] = {}
    if "CustomerId" not in df_f.columns:
        return frames

    df_c = df_f[df_f["CustomerId"].astype(str) == str(customer_id)].copy()
    if df_c.empty:
        return frames

    rev = _rev_col(df_c)
    date_s = _pick_date_series(df_c)
    df_c["__ProductLabel"] = _build_product_label(df_c)

    # Monthly Revenue & Orders
    if not date_s.dropna().empty and "OrderId" in df_c.columns:
        tmp = pd.DataFrame({
            "Month": _month_key_series(date_s),
            "Revenue": pd.to_numeric(df_c[rev], errors="coerce").fillna(0.0),
            "OrderId": df_c["OrderId"],
        })
        m = tmp.groupby("Month", observed=True).agg(
            Revenue=("Revenue", "sum"),
            Orders=("OrderId", "nunique")
        ).reset_index().sort_values("Month")
        frames["Monthly"] = m

    # Spend mix (Top products by revenue)
    spend = (
        pd.DataFrame({
            "Product": df_c["__ProductLabel"],
            "Revenue": pd.to_numeric(df_c[rev], errors="coerce").fillna(0.0),
        })
        .groupby("Product", observed=True)["Revenue"].sum().reset_index()
        .sort_values("Revenue", ascending=False).head(50)
    )
    frames["SpendMix"] = spend

    # Weight mix (Top products by weight)
    wcol = None
    for c in ("pack_weight_lb_sum", "WeightLb"):
        if c in df_c.columns:
            wcol = c; break
    if wcol:
        weight = (
            pd.DataFrame({
                "Product": df_c["__ProductLabel"],
                "WeightLb": pd.to_numeric(df_c[wcol], errors="coerce").fillna(0.0),
            })
            .groupby("Product", observed=True)["WeightLb"].sum().reset_index()
            .sort_values("WeightLb", ascending=False).head(50)
        )
        frames["WeightMix"] = weight

    # Weekday revenue
    if not date_s.dropna().empty:
        wk = pd.DataFrame({
            "Weekday": date_s.dt.day_name(),
            "Revenue": pd.to_numeric(df_c[rev], errors="coerce").fillna(0.0)
        })
        wk = wk.groupby("Weekday", observed=True)["Revenue"].sum().reindex(_weekday_order()).dropna().reset_index()
        frames["WeekdayRevenue"] = wk

    # YoY monthly heatmap (as a simple long table for Excel)
    if not date_s.dropna().empty:
        dfm = pd.DataFrame({"Year": date_s.dt.year, "MonthNum": date_s.dt.month, "Revenue": pd.to_numeric(df_c[rev], errors="coerce").fillna(0.0)})
        heat = dfm.groupby(["Year","MonthNum"], observed=True)["Revenue"].sum().reset_index().sort_values(["Year","MonthNum"])
        frames["YoYMonthly"] = heat

    # Price benchmarking
    region_col = au.resolve_column(df_c, ("RegionName", "Region_Name", "region_name", "Region"))
    try:
        bench = _peer_price_benchmark(df_f, df_c, "__ProductLabel", region_col)
        if not bench.empty:
            frames["PriceBenchmark"] = bench.reset_index()
    except Exception:
        pass

    return frames

def _clv_frames(cust: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Data used on CLV charts."""
    frames: dict[str, pd.DataFrame] = {}
    if cust.empty:
        return frames
    # Top by CLV (10) – same columns shown on card
    top = cust.sort_values(["CLV", "TotalRevenue"], ascending=[False, False]).head(10)
    frames["TopCLV"] = top[["CustomerId","CustomerName","AvgOrderValue","OrdersPerYear","CLV","GrossMarginPct"]].copy()

    # Scatter data (all customers)
    scat = cust[["CustomerId","CustomerName","TotalRevenue","TotalOrders","CLV","GrossMarginPct"]].copy()
    frames["Scatter"] = scat
    return frames

def _kpis_frames(cust_agg: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Table on KPIs page; expose current page and full table."""
    return {"Customers": cust_agg.copy()}

def _cohorts_frames(cust: pd.DataFrame, threshold_days: int) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    cohort_ret, churn_by_region, churn_trend = _cohort_metrics(cust, threshold_days)
    frames["CohortRetention"] = cohort_ret
    frames["ChurnByRegion"] = churn_by_region
    frames["ChurnTrend"] = churn_trend
    return frames

def _rfm_frames(cust: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Provide both the full scored table and top 10."""
    if cust.empty:
        return {}
    cust = cust.copy()
    cust["Recency"] = cust["DaysSinceLastOrder"]
    cust["Frequency"] = cust["TotalOrders"]
    cust["Monetary"] = cust["TotalRevenue"]
    scores = _rfm_scores(cust["Recency"], cust["Frequency"], cust["Monetary"])
    out = pd.concat([cust, scores], axis=1)
    top = out.sort_values(["RFM_Score", "Monetary"], ascending=[False, False]).head(10)
    return {"RFM_All": out, "RFM_Top10": top}


def _enforce_customers_export_access(page: str) -> None:
    if current_app.config.get("TESTING") and current_app.config.get("LOGIN_DISABLED"):
        return
    token = str(page or "").strip().lower()
    required = ["page.customers.view", "export.customers"]
    if token == "rfm":
        required.append("feature.customers.rfm.view")
    elif token == "cohorts":
        required.append("feature.customers.cohorts.view")
    elif token == "clv":
        required.append("feature.customers.clv.view")
    elif token == "drilldown":
        required.append("page.customers.drilldown.view")
    else:
        required.append("feature.customers.dashboard.view")
    if not has_permission(*required):
        abort(403, description="Missing permission for this export.")

@bp.route("/export", methods=["GET"])
@login_required
@requires_roles("sales", "sales_manager", "production", "gm", "owner", "admin")
def export():
    """
    Export chart/table data to Excel.
    Query params:
      - page: one of clv | kpis | drilldown | cohorts | rfm
      - chart: optional sub-key (informative; not strictly required)
      - customer_id: required for drilldown
      - threshold: int for cohorts (default 90)
    """
    page = (request.args.get("page") or "").lower().strip()
    chart = (request.args.get("chart") or "").lower().strip()
    customer_id = request.args.get("customer_id")
    threshold = request.args.get("threshold", type=int) or 90
    _enforce_customers_export_access(page)

    df_f = pd.DataFrame()
    uses_bundle_export = (
        (page == "clv" and _customers_clv_v2_enabled())
        or (page == "kpis" and (_customers_kpis_v3_enabled() or _customers_kpis_v2_enabled()))
        or (page == "rfm" and _customers_rfm_v2_enabled())
        or (page == "drilldown" and _customer_drilldown_v2_enabled())
    )
    if not uses_bundle_export:
        base_df = get_fact_df()
        params = _query_filter_params()
        effective_params = _clamp_params_to_data_window(base_df, params)
        df_f = apply_filter_params(base_df, effective_params)

    # CLV
    if page == "clv":
        if _customers_clv_v2_enabled():
            fmt = str(request.args.get("format") or "xlsx").strip().lower()
            dataset = str(request.args.get("dataset") or request.args.get("export_type") or "customers").strip().lower()
            export_args = request.args.copy()
            export_args["export_all"] = "1"
            export_args["clv_export_all"] = "1"
            export_args["clv_page"] = "1"

            payload = bundle_service.bundle("customers", export_args)
            meta_payload = (payload.get("meta") or {}) if isinstance(payload, dict) else {}
            clv_payload = (payload.get("clv") or {}) if isinstance(payload, dict) else {}
            settings = (clv_payload.get("settings") or {}) if isinstance(clv_payload, dict) else {}
            filters_payload = (clv_payload.get("filters") or {}) if isinstance(clv_payload, dict) else {}

            if dataset in {"segments", "segment_summary"}:
                records = clv_payload.get("segment_summary") or clv_payload.get("segment_leaderboard") or []
                data_df = pd.DataFrame.from_records(records)
                sheet_name = "SegmentSummary"
                stem = "customers_clv_segments"
            elif dataset in {"at_risk", "at_risk_high_value"}:
                records = clv_payload.get("at_risk_high_value") or []
                data_df = pd.DataFrame.from_records(records)
                sheet_name = "AtRiskHighValue"
                stem = "customers_clv_at_risk_high_value"
            elif dataset in {"leaderboard", "top"}:
                records = clv_payload.get("leaderboard") or clv_payload.get("top") or []
                data_df = pd.DataFrame.from_records(records)
                sheet_name = "Leaderboard"
                stem = "customers_clv_leaderboard"
            else:
                records = ((clv_payload.get("customers_table") or {}).get("rows") or [])
                data_df = pd.DataFrame.from_records(records)
                sheet_name = "Customers"
                stem = "customers_clv_customers"

            if data_df.empty:
                data_df = pd.DataFrame()

            metadata = {
                "generated_at_utc": pd.Timestamp.utcnow().isoformat(),
                "dataset": dataset,
                "dataset_version": meta_payload.get("dataset_version"),
                "filters_hash": meta_payload.get("filter_hash"),
                "filters_query": str(request.query_string.decode("utf-8") if request.query_string else ""),
                "clv_params_hash": settings.get("params_hash"),
                "lookback_months": settings.get("lookback_months"),
                "horizon_months": settings.get("horizon_months"),
                "discount_rate_pct": settings.get("discount_rate_pct"),
                "requested_monetary_basis": settings.get("requested_monetary_basis"),
                "effective_monetary_basis": settings.get("monetary_basis"),
                "retention_model": settings.get("retention_model"),
                "frequency_basis": settings.get("frequency_basis"),
                "lookback_start": settings.get("lookback_start"),
                "lookback_end": settings.get("lookback_end"),
                "prior_start": settings.get("prior_start"),
                "prior_end": settings.get("prior_end"),
                "monetary_caveat": settings.get("monetary_caveat"),
                "cost_coverage_pct": settings.get("cost_coverage_pct"),
                "search": filters_payload.get("search"),
                "segments": ",".join(filters_payload.get("segments") or []),
                "min_clv": filters_payload.get("min_clv"),
                "high_risk_only": filters_payload.get("high_risk_only"),
                "low_margin_only": filters_payload.get("low_margin_only"),
                "table_total_rows": (clv_payload.get("customers_table") or {}).get("total_rows"),
            }
            metadata_df = pd.DataFrame(
                [{"field": str(key), "value": "" if val is None else str(val)} for key, val in metadata.items()]
            )

            if fmt == "csv":
                filename = sanitize_filename(f"{stem}.csv")
                return dataframe_to_csv_response(data_df, filename=filename)

            filename = sanitize_filename(f"{stem}.xlsx")
            return dataframes_to_xlsx_response(
                {
                    sheet_name: data_df,
                    "Metadata": metadata_df,
                },
                filename=filename,
            )

        cust = _customer_agg(df_f)
        if not cust.empty:
            cust["AvgOrderValue"] = (pd.to_numeric(cust["TotalRevenue"], errors="coerce") / cust["TotalOrders"].replace(0, pd.NA)).fillna(0.0)
            months_active = pd.to_numeric(cust["MonthsActive"], errors="coerce").replace(0, pd.NA)
            cust["OrdersPerYear"] = (pd.to_numeric(cust["TotalOrders"], errors="coerce") / months_active * 12.0).fillna(0.0)
            cust["CLV"] = cust["AvgOrderValue"] * cust["OrdersPerYear"] * 3.0
            rev = pd.to_numeric(cust["TotalRevenue"], errors="coerce").astype(float)
            cost = pd.to_numeric(cust.get("TotalCost", 0.0), errors="coerce").astype(float)
            with np.errstate(divide="ignore", invalid="ignore"):
                gm_pct = np.where(rev.to_numpy() != 0.0, ((rev - cost) / rev) * 100.0, 0.0)
            cust["GrossMarginPct"] = gm_pct
            cust = _round2_df(cust, ["AvgOrderValue", "OrdersPerYear", "CLV", "GrossMarginPct"])
        frames = _clv_frames(cust)
        return _xlsx(frames, filename="clv_charts.xlsx")

    # KPIs
    if page == "kpis":
        if _customers_kpis_v3_enabled() or _customers_kpis_v2_enabled():
            fmt = str(request.args.get("format") or "xlsx").strip().lower()
            dataset = str(request.args.get("dataset") or request.args.get("export_type") or "table").strip().lower()
            export_args = request.args.copy()
            export_args["export_all"] = "1"
            export_args["page"] = "1"
            payload = bundle_service.bundle("customers", export_args)
            table_payload = (payload.get("table") or {}) if isinstance(payload, dict) else {}
            drivers_payload = (payload.get("drivers") or {}) if isinstance(payload, dict) else {}
            charts_payload = (payload.get("charts") or {}) if isinstance(payload, dict) else {}
            meta_payload = (payload.get("meta") or {}) if isinstance(payload, dict) else {}
            kpi_payload = (payload.get("kpis") or {}) if isinstance(payload, dict) else {}
            risk_payload = (payload.get("churn_risk_summary") or {}) if isinstance(payload, dict) else {}

            if dataset == "movers":
                records = drivers_payload.get("movers") or []
                export_df = pd.DataFrame.from_records(records)
                stem = "customers_kpis_movers"
                sheet_name = "Movers"
            elif dataset in {"lifecycle", "lifecycle_funnel"}:
                records = charts_payload.get("lifecycle_funnel") or []
                export_df = pd.DataFrame.from_records(records)
                stem = "customers_kpis_lifecycle_funnel"
                sheet_name = "LifecycleFunnel"
            elif dataset in {"composition", "revenue_composition"}:
                records = charts_payload.get("revenue_composition") or []
                export_df = pd.DataFrame.from_records(records)
                stem = "customers_kpis_revenue_composition"
                sheet_name = "RevenueComposition"
            elif dataset in {"risk_distribution", "risk_bands", "churn_risk"}:
                records = risk_payload.get("distribution") or charts_payload.get("churn_risk_distribution") or []
                export_df = pd.DataFrame.from_records(records)
                stem = "customers_kpis_risk_distribution"
                sheet_name = "RiskDistribution"
            elif dataset in {"top_at_risk", "at_risk_customers"}:
                records = risk_payload.get("top_at_risk_customers") or []
                export_df = pd.DataFrame.from_records(records)
                stem = "customers_kpis_top_at_risk"
                sheet_name = "TopAtRisk"
            else:
                records = table_payload.get("rows") or []
                export_df = pd.DataFrame.from_records(records)
                stem = "customers_kpis_table"
                sheet_name = "Customers"

            if export_df.empty:
                export_df = pd.DataFrame()

            metadata = {
                "generated_at_utc": pd.Timestamp.utcnow().isoformat(),
                "dataset_version": meta_payload.get("dataset_version"),
                "filters_hash": meta_payload.get("filter_hash"),
                "filters_query": str(request.query_string.decode("utf-8") if request.query_string else ""),
                "window_start": (kpi_payload.get("window") or {}).get("start"),
                "window_end": (kpi_payload.get("window") or {}).get("end"),
                "prior_window_start": (kpi_payload.get("window") or {}).get("prior_start"),
                "prior_window_end": (kpi_payload.get("window") or {}).get("prior_end"),
                "table_quick_filter": table_payload.get("quick_filter"),
                "table_sort_by": table_payload.get("sort_by"),
                "table_sort_dir": table_payload.get("sort_dir"),
                "table_total_rows": table_payload.get("total_rows"),
                "dataset": dataset,
                "kpis_version": "v3" if _customers_kpis_v3_enabled() else "v2",
            }
            metadata_df = pd.DataFrame(
                [{"field": str(key), "value": "" if val is None else str(val)} for key, val in metadata.items()]
            )

            if fmt == "csv":
                filename = sanitize_filename(f"{stem}.csv")
                return dataframe_to_csv_response(export_df, filename=filename)

            filename = sanitize_filename(f"{stem}.xlsx")
            return dataframes_to_xlsx_response(
                {
                    sheet_name: export_df,
                    "Metadata": metadata_df,
                },
                filename=filename,
            )

        cust_agg = _customer_agg(df_f)
        frames = _kpis_frames(cust_agg)
        return _xlsx(frames, filename="customers_kpis.xlsx")

    # Drilldown
    if page == "drilldown":
        if not customer_id:
            return Response("customer_id required", status=400)
        if _customer_drilldown_v2_enabled():
            fmt_values = request.args.getlist("format") if hasattr(request.args, "getlist") else []
            fmt = str((fmt_values[-1] if fmt_values else request.args.get("format")) or "xlsx").strip().lower()
            dataset = str(
                request.args.get("dataset")
                or request.args.get("scope")
                or request.args.get("type")
                or "snapshot"
            ).strip().lower()
            export_args = request.args.copy()
            export_args["customer_id"] = customer_id
            export_args["export_all"] = "1"
            export_args["drilldown_export_all"] = "1"
            export_args["drilldown_v2"] = "1"
            payload = bundle_service.drilldown("customers", export_args)
            if not isinstance(payload, dict):
                return Response("export unavailable", status=503)

            kpis_payload = (payload.get("kpis") or {}) if isinstance(payload, dict) else {}
            trend_payload = (payload.get("trend") or {}) if isinstance(payload, dict) else {}
            table_payload = (payload.get("table") or {}) if isinstance(payload, dict) else {}
            seasonality_payload = (payload.get("seasonality") or {}) if isinstance(payload, dict) else {}
            meta_payload = (payload.get("meta") or {}) if isinstance(payload, dict) else {}

            monthly_df = pd.DataFrame(
                {
                    "month": trend_payload.get("labels") or [],
                    "revenue": trend_payload.get("revenue") or [],
                    "orders": trend_payload.get("orders") or trend_payload.get("qty") or [],
                    "cost": trend_payload.get("cost") or [],
                    "profit": trend_payload.get("profit") or [],
                    "margin_pct": trend_payload.get("margin_pct") or [],
                    "revenue_previous_year": trend_payload.get("previous_year_revenue") or [],
                }
            )
            product_df = pd.DataFrame.from_records(table_payload.get("rows") or [])
            price_df = pd.DataFrame.from_records(payload.get("price_table") or [])
            cross_sell_df = pd.DataFrame.from_records(payload.get("cross_sell") or payload.get("cross_sell_ideas") or [])
            orders_df = pd.DataFrame.from_records(payload.get("orders") or [])
            top_spend_df = pd.DataFrame.from_records(payload.get("top_spend_full") or payload.get("top_spend") or [])
            top_weight_df = pd.DataFrame.from_records(payload.get("top_weight_full") or payload.get("top_weight") or [])
            cadence_df = pd.DataFrame({"days_between_orders": (payload.get("cadence") or {}).get("days") or []})
            categories_df = pd.DataFrame.from_records(payload.get("categories") or [])
            hero_df = pd.DataFrame(
                [
                    {"field": str(key), "value": "" if val is None else val}
                    for key, val in ((payload.get("hero") or {}) if isinstance(payload, dict) else {}).items()
                    if key != "badges"
                ]
            )
            hero_badges_df = pd.DataFrame.from_records(((payload.get("hero") or {}).get("badges") or []) if isinstance(payload, dict) else [])
            scorecard_rows = []
            for group in (payload.get("executive_scorecard") or []):
                if not isinstance(group, dict):
                    continue
                for metric in group.get("metrics") or []:
                    if not isinstance(metric, dict):
                        continue
                    scorecard_rows.append(
                        {
                            "group": group.get("title"),
                            "metric": metric.get("label"),
                            "value": metric.get("value"),
                            "format": metric.get("format"),
                            "detail": metric.get("detail"),
                        }
                    )
            scorecard_df = pd.DataFrame.from_records(scorecard_rows)
            priority_df = pd.DataFrame.from_records(((payload.get("priority_engine") or {}).get("scores") or []) if isinstance(payload, dict) else [])
            lifecycle_df = pd.DataFrame(
                [
                    {"field": str(key), "value": "" if val is None else val}
                    for key, val in ((payload.get("lifecycle") or {}) if isinstance(payload, dict) else {}).items()
                ]
            )
            weight_summary_df = pd.DataFrame(
                [
                    {"field": str(key), "value": "" if val is None else val}
                    for key, val in (((payload.get("weight_analytics") or {}).get("summary") or {}) if isinstance(payload, dict) else {}).items()
                ]
            )
            weight_top_products_df = pd.DataFrame.from_records((((payload.get("weight_analytics") or {}).get("top_products") or []) if isinstance(payload, dict) else []))
            actions_rows = []
            for lane, rows in ((payload.get("crm_workspace") or {}) if isinstance(payload, dict) else {}).items():
                for row in rows or []:
                    if not isinstance(row, dict):
                        continue
                    clean = dict(row)
                    clean["lane"] = lane
                    actions_rows.append(clean)
            actions_df = pd.DataFrame.from_records(actions_rows)
            trust_df = pd.DataFrame(
                [
                    {"field": str(key), "value": "" if val is None else val}
                    for key, val in ((payload.get("trust_coverage") or {}) if isinstance(payload, dict) else {}).items()
                ]
            )
            protein_focus = str(request.args.get("protein_focus") or "").strip()
            protein_focus_token = protein_focus.lower()
            action_lane = str(request.args.get("action_lane") or "").strip().lower()
            negative_only = str(request.args.get("negative_only") or "").strip().lower() in {"1", "true", "yes", "on"}
            below_target_only = str(request.args.get("below_target_only") or "").strip().lower() in {"1", "true", "yes", "on"}
            def _normalize_token(value: Any) -> str:
                return str(value or "").strip().lower()

            def _token_matches(value: Any, token: str) -> bool:
                if not token:
                    return True
                if isinstance(value, (list, tuple, set)):
                    return any(_normalize_token(item) == token for item in value)
                return _normalize_token(value) == token

            def _filter_by_token(frame: pd.DataFrame, *candidate_cols: str) -> pd.DataFrame:
                if frame.empty or not protein_focus_token:
                    return frame
                usable = [col for col in candidate_cols if col in frame.columns]
                if not usable:
                    return frame
                mask = pd.Series(False, index=frame.index)
                for col in usable:
                    mask = mask | frame[col].apply(lambda value: _token_matches(value, protein_focus_token))
                return frame.loc[mask].copy()

            def _ensure_columns(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
                if not frame.empty or list(frame.columns):
                    return frame
                return pd.DataFrame(columns=columns)

            product_df = _filter_by_token(product_df, "protein_family")
            price_df = _filter_by_token(price_df, "protein_family")
            cross_sell_df = _filter_by_token(cross_sell_df, "protein_family", "family")
            top_spend_df = _filter_by_token(top_spend_df, "protein_family")
            top_weight_df = _filter_by_token(top_weight_df, "protein_family")
            actions_df = _filter_by_token(actions_df, "related_families")
            if not product_df.empty and negative_only and "margin_pct" in product_df.columns:
                product_df = product_df.loc[pd.to_numeric(product_df["margin_pct"], errors="coerce") < 0].copy()
            if not price_df.empty and negative_only and "margin_pct" in price_df.columns:
                price_df = price_df.loc[pd.to_numeric(price_df["margin_pct"], errors="coerce") < 0].copy()
            if not product_df.empty and below_target_only and "margin_pct" in product_df.columns:
                product_targets = (
                    pd.to_numeric(product_df["target_margin_pct"], errors="coerce")
                    if "target_margin_pct" in product_df.columns
                    else pd.Series(np.nan, index=product_df.index, dtype="float64")
                )
                product_df = product_df.loc[pd.to_numeric(product_df["margin_pct"], errors="coerce") < product_targets].copy()
            if not price_df.empty and below_target_only and "margin_pct" in price_df.columns:
                price_targets = (
                    pd.to_numeric(price_df["target_margin_pct"], errors="coerce")
                    if "target_margin_pct" in price_df.columns
                    else pd.Series(np.nan, index=price_df.index, dtype="float64")
                )
                price_df = price_df.loc[pd.to_numeric(price_df["margin_pct"], errors="coerce") < price_targets].copy()
            if not actions_df.empty and action_lane and "lane" in actions_df.columns:
                actions_df = actions_df.loc[actions_df["lane"].astype("string").str.strip().str.lower() == action_lane].copy()
            product_df = _ensure_columns(
                product_df,
                [
                    "sku",
                    "product",
                    "protein_family",
                    "category",
                    "revenue",
                    "cost",
                    "profit",
                    "margin_pct",
                    "weight_lb",
                    "qty",
                    "orders",
                    "revenue_prior",
                    "profit_prior",
                ],
            )
            price_df = _ensure_columns(
                price_df,
                [
                    "sku",
                    "product",
                    "category",
                    "protein_family",
                    "cust_unit_price",
                    "peer_median",
                    "delta_pct",
                    "suggest_price",
                    "revenue",
                    "weight_lb",
                    "margin_pct",
                    "profit_lb",
                    "price_quality_flag",
                ],
            )
            cross_sell_df = _ensure_columns(
                cross_sell_df,
                [
                    "sku",
                    "product",
                    "protein_family",
                    "category",
                    "co_orders",
                    "revenue",
                    "candidate_orders",
                    "total_orders",
                    "confidence_pct",
                    "support_pct",
                    "lift",
                    "reason",
                    "explanation",
                ],
            )
            orders_df = _ensure_columns(
                orders_df,
                ["order_id", "order_date", "revenue", "cost", "profit", "weight_lb", "items", "lines"],
            )
            top_spend_df = _ensure_columns(top_spend_df, ["sku", "product", "protein_family", "category", "revenue", "revenue_share_pct"])
            top_weight_df = _ensure_columns(top_weight_df, ["sku", "product", "protein_family", "category", "weight_lb", "weight_share_pct"])
            categories_df = _ensure_columns(categories_df, ["category", "revenue", "profit", "weight_lb", "orders", "revenue_prior", "weight_prior"])
            actions_df = _ensure_columns(
                actions_df,
                [
                    "lane",
                    "type",
                    "title",
                    "why",
                    "detail",
                    "urgency",
                    "confidence",
                    "owner",
                    "related_products",
                    "related_categories",
                    "related_families",
                    "revenue_upside",
                    "profit_upside",
                    "margin_upside_pp",
                    "scope_label",
                    "pathway_label",
                    "tone",
                    "priority_score",
                ],
            )

            seasonality_rows = []
            matrix = seasonality_payload.get("matrix") or []
            months_axis = seasonality_payload.get("months") or []
            years_axis = seasonality_payload.get("years") or []
            for ridx, row_vals in enumerate(matrix):
                year_val = years_axis[ridx] if ridx < len(years_axis) else None
                for cidx, value in enumerate(row_vals or []):
                    month_val = months_axis[cidx] if cidx < len(months_axis) else None
                    seasonality_rows.append({"year": year_val, "month": month_val, "revenue": value})
            seasonality_df = pd.DataFrame.from_records(seasonality_rows)

            summary_rows = []
            for key, val in (kpis_payload or {}).items():
                summary_rows.append({"metric": str(key), "value": "" if val is None else val})
            summary_df = pd.DataFrame.from_records(summary_rows)

            metadata = {
                "generated_at_utc": pd.Timestamp.utcnow().isoformat(),
                "dataset": dataset,
                "dataset_version": meta_payload.get("dataset_version"),
                "filters_hash": meta_payload.get("filter_hash"),
                "filters_query": str(request.query_string.decode("utf-8") if request.query_string else ""),
                "customer_id": customer_id,
                "window_start": meta_payload.get("window_start"),
                "window_end": meta_payload.get("window_end"),
                "prior_window_start": meta_payload.get("prior_window_start"),
                "prior_window_end": meta_payload.get("prior_window_end"),
                "scope_mode": (meta_payload.get("scope") or {}).get("scope_mode"),
                "scope_hash": (meta_payload.get("scope") or {}).get("scope_hash"),
                "table_rows": len(product_df.index),
                "orders_rows": len(orders_df.index),
                "protein_focus": protein_focus or None,
                "action_lane": action_lane or None,
                "negative_only": negative_only,
                "below_target_only": below_target_only,
            }
            metadata_df = pd.DataFrame(
                [{"field": str(key), "value": "" if val is None else str(val)} for key, val in metadata.items()]
            )

            if dataset in {"product_profitability", "products", "profitability"}:
                export_df = product_df
                sheet_name = "ProductProfitability"
                stem = f"customer_{customer_id}_product_profitability"
            elif dataset in {"monthly", "trend", "monthly_trends"}:
                export_df = monthly_df
                sheet_name = "MonthlyTrends"
                stem = f"customer_{customer_id}_monthly_trends"
            elif dataset in {"price", "price_intelligence"}:
                export_df = price_df
                sheet_name = "PriceIntelligence"
                stem = f"customer_{customer_id}_price_intelligence"
            elif dataset in {"cross_sell", "cross_sell_ideas"}:
                export_df = cross_sell_df
                sheet_name = "CrossSell"
                stem = f"customer_{customer_id}_cross_sell"
            elif dataset in {"orders", "orders_lines"}:
                export_df = orders_df
                sheet_name = "Orders"
                stem = f"customer_{customer_id}_orders"
            elif dataset in {"top_spend", "spend"}:
                export_df = top_spend_df
                sheet_name = "TopSpend"
                stem = f"customer_{customer_id}_top_spend"
            elif dataset in {"top_weight", "weight"}:
                export_df = top_weight_df
                sheet_name = "TopWeight"
                stem = f"customer_{customer_id}_top_weight"
            elif dataset in {"weight_analysis", "weight_operational", "weight_summary"}:
                export_df = weight_top_products_df if not weight_top_products_df.empty else weight_summary_df
                sheet_name = "WeightOperational"
                stem = f"customer_{customer_id}_weight_operational"
            elif dataset in {"categories", "category_mix"}:
                export_df = categories_df
                sheet_name = "CategoryMix"
                stem = f"customer_{customer_id}_category_mix"
            elif dataset in {"actions", "crm_actions", "action_workspace"}:
                export_df = actions_df
                sheet_name = "CRMActionWorkspace"
                stem = f"customer_{customer_id}_crm_action_workspace"
            elif dataset in {"seasonality", "heatmap"}:
                export_df = seasonality_df
                sheet_name = "Seasonality"
                stem = f"customer_{customer_id}_seasonality"
            elif dataset in {"cadence"}:
                export_df = cadence_df
                sheet_name = "Cadence"
                stem = f"customer_{customer_id}_cadence"
            else:
                export_df = None
                sheet_name = "Snapshot"
                stem = f"customer_{customer_id}_snapshot"

            if fmt == "csv":
                if export_df is None:
                    export_df = summary_df
                return dataframe_to_csv_response(export_df if export_df is not None else pd.DataFrame(), filename=f"{stem}.csv")

            if export_df is not None:
                return dataframes_to_xlsx_response(
                    {
                        sheet_name: export_df if export_df is not None else pd.DataFrame(),
                        "Metadata": metadata_df,
                    },
                    filename=f"{stem}.xlsx",
                )

            return dataframes_to_xlsx_response(
                {
                    "SummaryKPIs": summary_df,
                    "Hero": hero_df,
                    "HeroBadges": hero_badges_df,
                    "ExecutiveScorecard": scorecard_df,
                    "PriorityEngine": priority_df,
                    "LifecycleRetention": lifecycle_df,
                    "MonthlyTrends": monthly_df,
                    "WeightOperational": weight_summary_df,
                    "WeightTopProducts": weight_top_products_df,
                    "ProductProfitability": product_df,
                    "CategoryMix": categories_df,
                    "TopProductsSpend": top_spend_df,
                    "TopProductsWeight": top_weight_df,
                    "Orders": orders_df,
                    "CrossSell": cross_sell_df,
                    "PriceIntelligence": price_df,
                    "CRMActionWorkspace": actions_df,
                    "TrustCoverage": trust_df,
                    "Seasonality": seasonality_df,
                    "Cadence": cadence_df,
                    "Metadata": metadata_df,
                },
                filename=f"{stem}.xlsx",
            )

        frames = _drilldown_frames(df_f, customer_id=customer_id)
        return _xlsx(frames, filename=f"customer_{customer_id}_drilldown.xlsx")

    # Cohorts
    if page == "cohorts":
        cust = _customer_agg(df_f)
        frames = _cohorts_frames(cust, threshold_days=threshold)
        return _xlsx(frames, filename=f"cohorts_{threshold}d.xlsx")

    # RFM
    if page == "rfm":
        if _customers_rfm_v2_enabled():
            fmt = str(request.args.get("format") or "xlsx").strip().lower()
            dataset = str(request.args.get("dataset") or request.args.get("export_type") or "customers_full").strip().lower()
            export_args = request.args.copy()
            export_args["export_all"] = "1"
            export_args["rfm_export_all"] = "1"
            export_args["rfm_page"] = "1"

            payload = bundle_service.bundle("customers", export_args)
            meta_payload = (payload.get("meta") or {}) if isinstance(payload, dict) else {}
            rfm_payload = (payload.get("rfm") or {}) if isinstance(payload, dict) else {}
            settings = (rfm_payload.get("settings") or {}) if isinstance(rfm_payload, dict) else {}
            filters_payload = (rfm_payload.get("filters") or {}) if isinstance(rfm_payload, dict) else {}

            if dataset == "top_customers":
                records = ((rfm_payload.get("segment_table") or {}).get("rows") or [])
                data_df = pd.DataFrame.from_records(records)
                sheet_name = "TopCustomers"
                stem = "customers_rfm_top_customers"
            elif dataset in {"segments", "segment_leaderboard"}:
                records = rfm_payload.get("segment_leaderboard") or rfm_payload.get("segment_summary") or []
                data_df = pd.DataFrame.from_records(records)
                sheet_name = "SegmentLeaderboard"
                stem = "customers_rfm_segment_leaderboard"
            elif dataset == "matrix_cells":
                matrix_rows = ((rfm_payload.get("matrix") or {}).get("rows") or [])
                flat_cells = []
                for row in matrix_rows:
                    for cell in row.get("cells") or []:
                        flat_cells.append(
                            {
                                "r_score": cell.get("r_score"),
                                "f_score": cell.get("f_score"),
                                "customers": cell.get("customers"),
                                "revenue": cell.get("revenue"),
                                "revenue_share_pct": cell.get("revenue_share_pct"),
                            }
                        )
                data_df = pd.DataFrame.from_records(flat_cells)
                sheet_name = "RFM_Matrix"
                stem = "customers_rfm_matrix"
            elif dataset == "heatmap_customers":
                records = ((rfm_payload.get("customers_table") or {}).get("rows") or [])
                data_df = pd.DataFrame.from_records(records)
                sheet_name = "HeatmapCustomers"
                stem = "customers_rfm_heatmap_customers"
            else:
                records = ((rfm_payload.get("customers_table") or {}).get("rows") or [])
                data_df = pd.DataFrame.from_records(records)
                sheet_name = "Customers"
                stem = "customers_rfm_customers"

            if data_df.empty:
                data_df = pd.DataFrame()

            metadata = {
                "generated_at_utc": pd.Timestamp.utcnow().isoformat(),
                "dataset": dataset,
                "dataset_version": meta_payload.get("dataset_version"),
                "filters_hash": meta_payload.get("filter_hash"),
                "filters_query": str(request.query_string.decode("utf-8") if request.query_string else ""),
                "rfm_params_hash": settings.get("params_hash"),
                "lookback_start": settings.get("lookback_start"),
                "lookback_end": settings.get("lookback_end"),
                "prior_start": settings.get("prior_start"),
                "prior_end": settings.get("prior_end"),
                "scoring_method": settings.get("scoring_method"),
                "requested_monetary_metric": settings.get("requested_monetary_metric"),
                "effective_monetary_metric": settings.get("monetary_metric"),
                "monetary_caveat": settings.get("monetary_caveat"),
                "search": filters_payload.get("search"),
                "segments": ",".join(filters_payload.get("segments") or []),
                "at_risk_only": filters_payload.get("at_risk_only"),
                "heat_r": filters_payload.get("heat_r"),
                "heat_f": filters_payload.get("heat_f"),
                "table_total_rows": (rfm_payload.get("customers_table") or {}).get("total_rows"),
                "selected_heat_cell": (
                    f"R{filters_payload.get('heat_r')}F{filters_payload.get('heat_f')}"
                    if (filters_payload.get("heat_r") is not None and filters_payload.get("heat_f") is not None)
                    else "none"
                ),
            }
            metadata_df = pd.DataFrame(
                [{"field": str(key), "value": "" if val is None else str(val)} for key, val in metadata.items()]
            )

            if fmt == "csv":
                filename = sanitize_filename(f"{stem}.csv")
                return dataframe_to_csv_response(data_df, filename=filename)
            filename = sanitize_filename(f"{stem}.xlsx")
            return dataframes_to_xlsx_response(
                {
                    sheet_name: data_df,
                    "Metadata": metadata_df,
                },
                filename=filename,
            )

        cust = _customer_agg(df_f)
        frames = _rfm_frames(cust)
        return _xlsx(frames, filename="rfm.xlsx")

    return Response("Unknown export page", status=400)


@bp.get("/rfm/export")
@login_required
@requires_roles("sales", "sales_manager", "production", "gm", "owner", "admin")
def rfm_export_alias():
    """Alias export endpoint for RFM v2 datasets."""
    export_type = str(request.args.get("type") or "customers").strip().lower()
    dataset_map = {
        "customers": "customers_full",
        "segments": "segment_leaderboard",
        "heatmap": "heatmap_customers",
        "top": "top_customers",
        "matrix": "matrix_cells",
    }
    dataset = dataset_map.get(export_type, "customers_full")

    params = request.args.to_dict(flat=True)
    params.pop("type", None)
    params["page"] = "rfm"
    params["dataset"] = dataset
    return redirect(url_for("customers.export", **params))


@bp.get("/clv/export")
@login_required
@requires_roles("sales", "sales_manager", "production", "gm", "owner", "admin")
def clv_export_alias():
    """Alias export endpoint for CLV v2 datasets."""
    export_type = str(request.args.get("type") or "customers").strip().lower()
    dataset_map = {
        "customers": "customers",
        "segments": "segments",
        "at_risk": "at_risk_high_value",
        "at_risk_high_value": "at_risk_high_value",
        "leaderboard": "leaderboard",
        "top": "leaderboard",
    }
    dataset = dataset_map.get(export_type, "customers")

    params = request.args.to_dict(flat=True)
    params.pop("type", None)
    params["page"] = "clv"
    params["dataset"] = dataset
    return redirect(url_for("customers.export", **params))

from __future__ import annotations

import json
import hashlib
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from flask import Blueprint, jsonify, render_template, request, session, abort, Response, current_app, url_for
from flask_login import login_required, current_user

from app.cache import cache
from app.core.exports import (
    dataframe_to_csv_response,
    dataframes_to_xlsx_response,
    sanitize_filename,
    xlsx_export_available,
)
from app.core.rbac import requires_roles, can_view_costs
from app.core import access_policy
from app.services.frame import load_canonical_df
from app.services.filters import apply_filters as apply_filter_params, filters_cache_key, resolve_filters
from app.services.bundle_builder import to_json_safe
from app.services.cache import cache_key as versioned_cache_key
from app.services import analytics_utils as au, bundle_service, filters_service, fact_store, salesreps_bundle
from app.core.features import legacy_pandas_enabled

bp = Blueprint("salesreps", __name__, url_prefix="/salesreps")

_TTL = 60180
_CACHE_REV = "salesreps-v3"
_MAX_ROWS = 5000
_BASE_FRAME_LOCK = threading.Lock()
_CUSTOMER_LOOKUP_LOCK = threading.Lock()
# Cache per-rep scoped frames to avoid repeated filtering across endpoints
_REP_FRAME_CACHE: Dict[str, pd.DataFrame] = {}
# Keep column projection narrow for perf but resilient to upstream schema
_BASE_COLUMNS = [
    # identity
    "SalesRepId",
    "SalesRepID",
    "PrimarySalesRepId",
    "PrimarySalesRepID",
    "UserId",
    "UserID",
    "SalesRepName",
    "PrimarySalesRepName",
    "UserName",
    # value fields
    "Revenue",
    "Cost",
    "Profit",
    # qty fields
    "Units",
    "Qty",
    "Quantity",
    "QuantityShipped",
    "QtyShipped",
    "QuantityOrdered",
    "QtyOrdered",
    "ItemCount",
    "pack_item_count_sum",
    "WeightLb",
    "pack_weight_lb_sum",
    # grouping/support
    "OrderId",
    "CustomerId",
    "CustomerName",
    "ProductId",
    "ProductName",
    "Date",
    "ShipDate",
]


def _legacy_disabled_response():
    resp = jsonify({"error": {"message": "Legacy salesreps endpoints are disabled; use /api/salesreps/bundle."}})
    resp.status_code = 410
    return resp


@bp.before_request
def _block_legacy_salesreps():
    if legacy_pandas_enabled():
        return None
    allowed = {
        "salesreps.index",
        "salesreps.rep_detail",
        "salesreps.export_xlsx",
        "salesreps.export_csv",
        "salesreps.rep_export",
        "salesreps.rep_export_data",
        "salesreps.drilldown_bundle_api",
    }
    if request.endpoint in allowed:
        return None
    return _legacy_disabled_response()


def _etag_json(payload: Any) -> Response:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    et = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    inm = request.headers.get("If-None-Match")
    if inm and inm == et:
        resp = Response(status=304)
        resp.headers["ETag"] = et
        resp.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
        return resp
    resp = jsonify(payload)
    resp.headers["ETag"] = et
    resp.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
    return resp


def _filters_from_request() -> Any:
    sticky_enabled = bool(current_app.config.get("STICKY_FILTERS", True))
    filters, _meta = resolve_filters(
        request,
        current_user,
        session_obj=session,
        source=request.args or {},
        sticky_enabled=sticky_enabled,
    )
    return filters


def _cache_key(name: str, params: Any, extra: Optional[Dict[str, Any]] = None) -> str:
    extras = {"endpoint": name, "rev": _CACHE_REV}
    try:
        extras["user"] = current_user.get_id() if hasattr(current_user, "get_id") else None
        extras["role"] = getattr(current_user, "role", None)
    except Exception:
        pass
    if extra:
        extras.update(extra)
    try:
        return versioned_cache_key(params, extras)
    except Exception:
        # fallback stable key
        return filters_cache_key(current_user, params, extras)


def _drilldown_v2_enabled() -> bool:
    try:
        return bool(current_app.config.get("SALESREP_DRILLDOWN_V2", False))
    except Exception:
        return False


def _salesreps_v2_enabled() -> bool:
    try:
        raw = current_app.config.get("SALESREPS_V2")
        if raw is None:
            raw = os.getenv("SALESREPS_V2", "0")
        return _to_bool(raw, default=False)
    except Exception:
        return False


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    try:
        return str(value).strip().lower() in {"1", "true", "yes", "on", "all"}
    except Exception:
        return default


def _fmt_ts(ts: Any) -> str | None:
    if ts is None:
        return None
    try:
        if hasattr(ts, "date"):
            return ts.date().isoformat()
    except Exception:
        pass
    try:
        return str(ts)[:10]
    except Exception:
        return None


def _export_window_tokens(filters: Any) -> tuple[str, str]:
    start = _fmt_ts(getattr(filters, "start", None))
    end = _fmt_ts(getattr(filters, "end", None))
    if start or end:
        return start or "start", end or "end"
    preset = str(getattr(filters, "preset", "") or "").strip().lower()
    if preset:
        return preset, preset
    return "window", "window"


def _export_filename(rep_id: str, dataset: str, filters: Any, extension: str) -> str:
    start_token, end_token = _export_window_tokens(filters)
    stem = f"SalesRep_{rep_id}_{dataset}_{start_token}_{end_token}"
    safe = sanitize_filename(stem, default="salesrep_export")
    ext = extension.lstrip(".").lower()
    return f"{safe}.{ext}"


def _salesreps_export_filename(filters: Any, extension: str) -> str:
    start_token, end_token = _export_window_tokens(filters)
    stem = f"SalesReps_{start_token}_{end_token}"
    safe = sanitize_filename(stem, default="salesreps_export")
    ext = extension.lstrip(".").lower()
    return f"{safe}.{ext}"


def _normalize_export_dataset(raw: Any) -> str:
    token = str(raw or "all").strip().lower()
    aliases = {
        "customer": "customers",
        "cust": "customers",
        "product": "products",
        "prod": "products",
        "orders": "history",
        "all_history": "history",
        "product_mix": "mix",
        "movers_customer": "movers_customers",
        "movers_product": "movers_products",
        "risk_margin": "margin_risk",
        "margin_leakage": "margin_risk",
        "atrisk": "at_risk",
    }
    return aliases.get(token, token)


def _enforce_rep_access(rep_id: str) -> None:
    access_policy.enforce_entity_access("salesreps", rep_id, access_policy.get_current_scope(use_cache=True))


def _sheet_label(dataset: str) -> str:
    mapping = {
        "summary": "Summary",
        "trend": "Trend",
        "customers": "Customers",
        "products": "Products",
        "mix": "Mix",
        "history": "History",
        "movers_customers": "Movers_Customers",
        "movers_products": "Movers_Products",
        "margin_risk": "Margin_Risk",
        "at_risk": "At_Risk_Customers",
    }
    return mapping.get(dataset, "Data")


def _export_include_history() -> bool:
    return any(
        _to_bool(request.args.get(key), default=False)
        for key in ("include_history", "all_time", "full_history", "export_all")
    )


def _build_rep_export_response(rep_id: str, *, force_dataset: str | None = None, force_format: str | None = None):
    try:
        filters = _filters_from_request()
    except Exception:
        filters = filters_service.default_filters(current_user)
    scope = filters_service.scope_from_user(current_user)
    access_policy.enforce_entity_access("salesreps", rep_id, access_policy.get_current_scope(use_cache=True))

    dataset = _normalize_export_dataset(
        force_dataset
        or request.args.get("export_type")
        or request.args.get("dataset")
        or "all"
    )
    fmt = str(force_format or request.args.get("format") or "xlsx").strip().lower()
    if fmt not in {"xlsx", "csv"}:
        fmt = "xlsx"
    if fmt == "xlsx" and not xlsx_export_available():
        fmt = "csv"
    include_history = _export_include_history()

    if dataset == "all":
        if fmt == "csv":
            # CSV can carry one dataset only, so do not build multi-sheet payloads.
            # This avoids unnecessary compute and keeps fallback behavior stable when XLSX engines are unavailable.
            csv_dataset = "history" if include_history else "customers"
            frame = salesreps_bundle.build_salesrep_export_dataset(
                rep_id,
                filters,
                scope,
                request.args,
                dataset=csv_dataset,
            )
            if frame is None:
                frame = pd.DataFrame()
            filename = _export_filename(rep_id, csv_dataset, filters, "csv")
            return dataframe_to_csv_response(frame, filename=filename)

        sheets = salesreps_bundle.build_salesrep_export_sheets(
            rep_id,
            filters,
            scope,
            request.args,
            include_history=include_history,
        )
        history_df = sheets.get("History") if include_history else None
        history_rows = int(len(history_df.index)) if isinstance(history_df, pd.DataFrame) else 0
        if include_history and history_rows > 200_000:
            # Keep large exports safe by streaming CSV for very large row counts.
            filename = _export_filename(rep_id, "history", filters, "csv")
            resp = dataframe_to_csv_response(history_df if isinstance(history_df, pd.DataFrame) else pd.DataFrame(), filename=filename)
            resp.headers["X-Export-Fallback"] = "csv"
            return resp
        filename = _export_filename(rep_id, "all_history" if include_history else "all", filters, "xlsx")
        return dataframes_to_xlsx_response(sheets, filename=filename, threshold_rows=200_000)

    frame = salesreps_bundle.build_salesrep_export_dataset(rep_id, filters, scope, request.args, dataset=dataset)
    rows = int(len(frame.index)) if isinstance(frame, pd.DataFrame) else 0
    if fmt == "xlsx" and dataset in {"history"} and rows > 200_000:
        filename = _export_filename(rep_id, dataset, filters, "csv")
        resp = dataframe_to_csv_response(frame, filename=filename)
        resp.headers["X-Export-Fallback"] = "csv"
        return resp
    if fmt == "csv":
        filename = _export_filename(rep_id, dataset, filters, "csv")
        return dataframe_to_csv_response(frame, filename=filename)
    filename = _export_filename(rep_id, dataset, filters, "xlsx")
    # Standardize XLSX exports with a metadata tab for auditability/parity.
    metadata_df = salesreps_bundle.build_salesrep_export_metadata_frame(
        rep_id=rep_id,
        filters=filters,
        scope=scope,
        export_type=dataset,
    )
    return dataframes_to_xlsx_response(
        {
            "Metadata": metadata_df,
            _sheet_label(dataset): frame,
        },
        filename=filename,
        threshold_rows=200_000,
    )


def _load_filtered_frame(params) -> pd.DataFrame:
    key = _cache_key("salesreps.base", params)
    cached = cache.get(key)
    if cached is not None:
        return cached
    with _BASE_FRAME_LOCK:
        cached = cache.get(key)
        if cached is not None:
            return cached
        try:
            df = load_canonical_df(columns=_BASE_COLUMNS)
        except Exception:
            current_app.logger.warning("salesreps.load_columns_failed", exc_info=True)
            df = load_canonical_df()
        df = apply_filter_params(df, params)
        cache.set(key, df, timeout=_TTL)
        try:
            current_app.logger.info(
                "salesreps.base_frame",
                extra={
                    "rows": len(df),
                    "cols": list(df.columns),
                    "filters": params,
                },
            )
        except Exception:
            pass
        return df


def _customer_name_lookup() -> Dict[str, str]:
    """
    Cached lookup of CustomerId -> CustomerName scoped to the current user.
    Used to backfill names when the fact frame is missing CustomerName.
    """
    key = _cache_key("salesreps.customer_lookup", params={}, extra={"scope": "customers"})
    cached = cache.get(key)
    if cached is not None:
        return cached
    with _CUSTOMER_LOOKUP_LOCK:
        cached = cache.get(key)
        if cached is not None:
            return cached
        try:
            df = load_canonical_df(columns=["CustomerId", "CustomerName"])
        except Exception:
            df = load_canonical_df()
        if df is None or df.empty or "CustomerId" not in df.columns:
            lookup: Dict[str, str] = {}
        else:
            tmp = df.copy()
            tmp["CustomerId"] = tmp["CustomerId"].astype("string").str.strip()
            if "CustomerName" in tmp.columns:
                tmp["CustomerName"] = tmp["CustomerName"].astype("string").str.strip()
            tmp = tmp[tmp["CustomerId"].notna() & (tmp["CustomerId"] != "")]
            if "CustomerName" in tmp.columns:
                tmp = tmp[tmp["CustomerName"].notna() & (tmp["CustomerName"] != "")]
            lookup = tmp.drop_duplicates("CustomerId").set_index("CustomerId")["CustomerName"].to_dict()
        cache.set(key, lookup, timeout=_TTL)
        return lookup


def _enrich_customer_names(df: pd.DataFrame) -> pd.DataFrame:
    """
    Ensure CustomerName is populated by mapping from cached dimension lookup when missing.
    """
    if df is None or df.empty or "CustomerId" not in df.columns:
        return df
    has_names = "CustomerName" in df.columns
    needs_fill = pd.Series(True, index=df.index)
    if has_names:
        try:
            existing = df["CustomerName"].astype("string").str.strip()
            needs_fill = existing.isna() | (existing == "")
            if not needs_fill.any():
                return df
        except Exception:
            pass
    lookup = _customer_name_lookup()
    if not lookup:
        return df
    out = df.copy()
    out["CustomerId"] = out["CustomerId"].astype("string").str.strip()
    if not has_names:
        out["CustomerName"] = pd.Series(pd.NA, index=out.index, dtype="string")
    out.loc[needs_fill, "CustomerName"] = out.loc[needs_fill, "CustomerId"].map(lookup)
    return out


def _rep_scoped_frame(rep_id: str, params) -> Optional[pd.DataFrame]:
    """
    Return cached scoped frame for a rep, ensuring consistent filtering across endpoints.
    """
    rep_id = (rep_id or "").strip()
    key = _cache_key("salesreps.rep_scope", params, {"rep_id": rep_id})
    cached = _REP_FRAME_CACHE.get(key)
    if cached is not None:
        return cached

    base = _load_filtered_frame(params)
    if base is None or base.empty:
        return pd.DataFrame()
    rep_id_col, _ = _ensure_rep_columns(base)
    tmp = base.copy()
    tmp["_rep_id"] = tmp[rep_id_col].astype("string").str.strip()
    # Audit how many distinct reps are available
    try:
        distinct_reps = tmp["_rep_id"].nunique()
        base_rows = len(tmp)
    except Exception:
        distinct_reps = None
        base_rows = len(tmp)
    scoped = tmp[tmp["_rep_id"] == rep_id]
    # Log coverage for diagnostics
    try:
        current_app.logger.info(
            "salesreps.rep_scope",
            extra={
                "rep_id": rep_id,
                "base_rows": base_rows,
                "distinct_reps": distinct_reps,
                "scoped_rows": len(scoped),
            },
        )
    except Exception:
        pass
    _REP_FRAME_CACHE[key] = scoped
    return scoped


def _monthly_trend(df: pd.DataFrame, include_profit: bool) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    date_ser = au.pick_first_valid_date_column(df, au.DATE_PRIORITY_ORDER)
    rev_col = au.revenue_column(df, required=False)
    profit_col = au.profit_column(df) if include_profit else None
    if date_ser is None or not rev_col:
        return []
    tmp = df.copy()
    tmp["_month"] = date_ser.dt.to_period("M").dt.to_timestamp()
    tmp[rev_col] = pd.to_numeric(tmp[rev_col], errors="coerce").fillna(0.0)
    agg_map = {"Revenue": (rev_col, "sum")}
    if profit_col and profit_col in tmp.columns:
        tmp[profit_col] = pd.to_numeric(tmp[profit_col], errors="coerce").fillna(0.0)
        agg_map["Profit"] = (profit_col, "sum")
    grouped = tmp.groupby("_month").agg(**agg_map)
    rows: List[Dict[str, Any]] = []
    for idx, row in grouped.iterrows():
        profit_val = float(row.get("Profit", 0.0) or 0.0) if include_profit and "Profit" in row else None
        rows.append(
            {
                "date": idx.date().isoformat(),
                "Revenue": float(row["Revenue"] or 0.0),
                "Profit": profit_val,
            }
        )
    return rows


def _ensure_rep_columns(df: pd.DataFrame) -> Tuple[str, str]:
    rep_id_col = au.sales_rep_id_column(df)
    rep_name_col = au.sales_rep_name_column(df)
    if not rep_id_col:
        raise ValueError(f"Sales Rep analytics unavailable: sales rep column not found. Available columns: {list(df.columns)}")
    if not rep_name_col:
        rep_name_col = rep_id_col
    return rep_id_col, rep_name_col


def _sum_numeric(values: Any) -> float:
    """Safe numeric sum that tolerates missing/invalid values."""
    if values is None:
        return 0.0
    try:
        series = pd.to_numeric(values, errors="coerce")
        return float(series.fillna(0.0).sum())
    except Exception:
        return 0.0


def _safe_divide_series(numer: Any, denom: Any) -> pd.Series:
    """Vectorized divide with zero/NaN protection."""
    num_s = pd.to_numeric(numer, errors="coerce")
    den_s = pd.to_numeric(denom, errors="coerce")
    den_s = den_s.where(den_s > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = num_s / den_s.replace(0, np.nan)
    return out.replace([np.inf, -np.inf], np.nan).fillna(0.0)


def _quantity_basis(table: pd.DataFrame) -> Tuple[pd.Series, str]:
    """
    Decide which quantity to use (units vs weight) so ASP/volume aren't zeroed when units are missing.
    Returns the chosen series and a human-readable label.
    """
    if table is None or table.empty:
        return pd.Series(dtype="float64"), "Units"
    idx = table.index
    units = pd.to_numeric(table["Units"], errors="coerce") if "Units" in table else pd.Series(0.0, index=idx, dtype="float64")
    weight = pd.to_numeric(table["WeightLb"], errors="coerce") if "WeightLb" in table else pd.Series(0.0, index=idx, dtype="float64")
    units_total = units.fillna(0.0).sum()
    weight_total = weight.fillna(0.0).sum()
    if units_total > 0:
        return units.fillna(0.0), "Units"
    if weight_total > 0:
        return weight.fillna(0.0), "Weight (lb)"
    return units.fillna(0.0), "Units"


def _finalize_rep_metrics(table: pd.DataFrame, include_costs: bool) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Apply canonical KPI calculations (margin, ASP, rev per customer) off the aggregated table.
    Ensures weighted margin (profit / revenue) is used everywhere.
    """
    if table is None or table.empty:
        return table, {
            "qty_label": "Units",
            "asp_label": "ASP",
            "asp_lb_label": "ASP / lb",
            "asp_unit_label": "ASP / unit",
            "has_margin": False,
        }

    has_profit = include_costs and "Profit" in table.columns
    table = table.copy()

    units_each = pd.to_numeric(table.get("ItemCount"), errors="coerce").fillna(0.0)
    units_col = pd.to_numeric(table.get("Units"), errors="coerce").fillna(0.0)
    units_each = units_each.where(units_each > 0, units_col)
    weight_lb = pd.to_numeric(table.get("WeightLb"), errors="coerce").fillna(0.0)

    table["UnitsEach"] = units_each
    table["Units"] = units_each  # backward compatible for UI
    table["WeightLb"] = weight_lb
    table["ASPUnit"] = _safe_divide_series(table["Revenue"], units_each)
    table["ASPLb"] = _safe_divide_series(table["Revenue"], weight_lb)
    table["ASP"] = table["ASPUnit"]

    if "Customers" in table:
        table["RevPerCustomer"] = _safe_divide_series(table["Revenue"], table["Customers"])
    if has_profit:
        margin = _safe_divide_series(table["Profit"], table["Revenue"]) * 100.0
        table["MarginPct"] = margin
        table["ROI"] = margin
    else:
        table.drop(columns=["Profit", "MarginPct", "ROI"], inplace=True, errors="ignore")

    meta = {
        "qty_label": "Units (ea)",
        "asp_label": "ASP / unit",
        "asp_lb_label": "ASP / lb",
        "asp_unit_label": "ASP / unit",
        "has_margin": has_profit,
    }
    return table, meta


def _build_kpis(table_df: pd.DataFrame, include_costs: bool, qty_label: str) -> Dict[str, Any]:
    revenue_total = _sum_numeric(table_df["Revenue"]) if table_df is not None else 0.0
    orders_total = int(_sum_numeric(table_df["Orders"])) if table_df is not None and "Orders" in table_df else 0
    customers_total = int(_sum_numeric(table_df["Customers"])) if table_df is not None and "Customers" in table_df else 0
    units_total = _sum_numeric(table_df["Units"]) if table_df is not None and "Units" in table_df else 0.0
    weight_total = _sum_numeric(table_df["WeightLb"]) if table_df is not None and "WeightLb" in table_df else 0.0

    profit_total = None
    margin_pct = None
    if include_costs and table_df is not None and "Profit" in table_df:
        profit_total = _sum_numeric(table_df["Profit"])
        margin_pct = (profit_total / revenue_total * 100.0) if revenue_total > 0 else 0.0

    asp_total = (revenue_total / units_total) if units_total else None
    asp_lb_total = (revenue_total / weight_total) if weight_total else None
    return {
        "revenue": float(revenue_total),
        "orders": orders_total,
        "customers": customers_total,
        "units": float(units_total),
        "units_label": qty_label,
        "weight_lb": float(weight_total),
        "profit": float(profit_total) if profit_total is not None else None,
        "margin_pct": float(margin_pct) if margin_pct is not None else None,
        "asp": float(asp_total) if asp_total is not None else None,
        "asp_lb": float(asp_lb_total) if asp_lb_total is not None else None,
    }


def _rep_rollup(df: pd.DataFrame, include_costs: bool) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    rep_id_col, rep_name_col = _ensure_rep_columns(df)
    rev_col = au.revenue_column(df, required=False)
    if not rev_col:
        raise ValueError(f"Revenue column not found. Available columns: {list(df.columns)}")

    cost_col = au.cost_column(df)
    profit_col = au.profit_column(df)
    units_col = au.units_column(df)
    weight_col = au.weight_lb_column(df)
    item_col = au.best_column(df, ("ItemCount", "pack_item_count_sum")) if hasattr(au, "best_column") else None
    order_col = au.order_id_column(df)
    cust_col = au.customer_id_column(df)

    tmp = df.copy()
    tmp["_rep_id"] = tmp[rep_id_col].astype("string").str.strip()
    tmp["_rep_name"] = tmp[rep_name_col].astype("string").str.strip()
    tmp[rev_col] = pd.to_numeric(tmp[rev_col], errors="coerce").fillna(0.0)
    if cost_col:
        tmp[cost_col] = pd.to_numeric(tmp[cost_col], errors="coerce").fillna(0.0)
    if profit_col:
        tmp[profit_col] = pd.to_numeric(tmp[profit_col], errors="coerce").fillna(0.0)
    if units_col:
        tmp[units_col] = pd.to_numeric(tmp[units_col], errors="coerce").fillna(0.0)
    if weight_col:
        tmp[weight_col] = pd.to_numeric(tmp[weight_col], errors="coerce").fillna(0.0)
    if item_col and item_col in tmp.columns:
        tmp[item_col] = pd.to_numeric(tmp[item_col], errors="coerce").fillna(0.0)

    def _as_float(value: Any) -> float:
        try:
            if pd.isna(value):
                return 0.0
        except Exception:
            pass
        try:
            return float(value)
        except Exception:
            return 0.0

    def _as_key(value: Any) -> str:
        try:
            if pd.isna(value):
                return ""
        except Exception:
            pass
        text = str(value).strip() if value is not None else ""
        if not text or text.lower() in {"nan", "none", "<na>"}:
            return ""
        return text

    prod_col = None
    for col in ("ProductId", "ProductID", "ProductName", "Product"):
        if col in tmp.columns:
            prod_col = col
            break

    date_ser = au.pick_first_valid_date_column(tmp, au.DATE_PRIORITY_ORDER)
    date_values = list(date_ser) if date_ser is not None else None
    records = tmp.to_dict("records")

    rep_stats: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for idx, row in enumerate(records):
        rep_id = _as_key(row.get("_rep_id"))
        if not rep_id:
            continue
        rep_name = _as_key(row.get("_rep_name")) or rep_id
        key = (rep_id, rep_name)
        stat = rep_stats.setdefault(
            key,
            {
                "RepId": rep_id,
                "RepName": rep_name,
                "Revenue": 0.0,
                "Orders": set(),
                "Customers": set(),
                "Units": 0.0,
                "ItemCount": 0.0,
                "WeightLb": 0.0,
                "Profit": 0.0,
                "Cost": 0.0,
                "ProductRevenue": {},
                "CustomerRevenue": {},
                "MonthRevenue": {},
            },
        )

        revenue_value = _as_float(row.get(rev_col))
        stat["Revenue"] += revenue_value

        if order_col and order_col in row:
            order_id = _as_key(row.get(order_col))
            if order_id:
                stat["Orders"].add(order_id)
        if cust_col and cust_col in row:
            customer_id = _as_key(row.get(cust_col))
            if customer_id:
                stat["Customers"].add(customer_id)
                customer_rev = stat["CustomerRevenue"]
                customer_rev[customer_id] = _as_float(customer_rev.get(customer_id)) + revenue_value

        if units_col and units_col in row:
            stat["Units"] += _as_float(row.get(units_col))
        if item_col and item_col in row:
            stat["ItemCount"] += _as_float(row.get(item_col))
        if weight_col and weight_col in row:
            stat["WeightLb"] += _as_float(row.get(weight_col))

        if profit_col and profit_col in row:
            stat["Profit"] += _as_float(row.get(profit_col))
        elif cost_col and cost_col in row:
            stat["Cost"] += _as_float(row.get(cost_col))

        if prod_col and prod_col in row:
            product_id = _as_key(row.get(prod_col))
            if product_id:
                product_rev = stat["ProductRevenue"]
                product_rev[product_id] = _as_float(product_rev.get(product_id)) + revenue_value

        if date_values is not None and idx < len(date_values):
            dt_value = date_values[idx]
            try:
                if pd.notna(dt_value):
                    month_key = pd.Timestamp(dt_value).to_period("M").to_timestamp()
                    month_rev = stat["MonthRevenue"]
                    month_rev[month_key] = _as_float(month_rev.get(month_key)) + revenue_value
            except Exception:
                pass

    rows: List[Dict[str, Any]] = []
    for stat in rep_stats.values():
        revenue_value = _as_float(stat["Revenue"])
        product_rev = stat["ProductRevenue"]
        customer_rev = stat["CustomerRevenue"]
        month_rev = stat["MonthRevenue"]

        top_product_share = 0.0
        if revenue_value > 0 and product_rev:
            top_product_share = sum(sorted(product_rev.values(), reverse=True)[:5]) / revenue_value

        top_customer_share = 0.0
        if revenue_value > 0 and customer_rev:
            top_customer_share = max(customer_rev.values()) / revenue_value

        momentum_pct = 0.0
        if month_rev:
            latest_month = max(month_rev.keys())
            last_start = (latest_month - pd.DateOffset(months=2)).normalize()
            prev_end = last_start - pd.DateOffset(days=1)
            prev_start = (prev_end - pd.DateOffset(months=2)).normalize()
            cur_total = sum(v for m, v in month_rev.items() if last_start <= m <= latest_month)
            prev_total = sum(v for m, v in month_rev.items() if prev_start <= m <= prev_end)
            if prev_total:
                momentum_pct = ((cur_total - prev_total) / prev_total) * 100.0

        if profit_col and profit_col in tmp.columns:
            profit_value = _as_float(stat["Profit"])
        elif cost_col and cost_col in tmp.columns:
            profit_value = revenue_value - _as_float(stat["Cost"])
        else:
            profit_value = 0.0

        units_value = _as_float(stat["Units"])
        item_count_value = _as_float(stat["ItemCount"]) if item_col and item_col in tmp.columns else units_value

        row = {
            "RepId": stat["RepId"],
            "RepName": stat["RepName"],
            "Revenue": revenue_value,
            "Orders": len(stat["Orders"]),
            "Customers": len(stat["Customers"]),
            "Units": units_value,
            "ItemCount": item_count_value,
            "WeightLb": _as_float(stat["WeightLb"]),
            "TopProductShare": top_product_share,
            "TopCustomerShare": top_customer_share,
            "MomentumPct": momentum_pct,
        }
        if include_costs:
            row["Profit"] = profit_value
        rows.append(row)

    data = pd.DataFrame(rows)

    return data


def _mix(df: pd.DataFrame, dim: str, top_n: int = 25) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    rev_col = au.revenue_column(df, required=False)
    if not rev_col:
        return []
    col_map = {
        "products": ("ProductName", "ProductId", "Product"),
        "customers": ("CustomerName", "CustomerId", "Customer"),
        "regions": ("RegionName", "Region"),
        "categories": ("Category", "CategoryName", "Segment", "ProdCategory"),
    }
    candidates = col_map.get(dim, col_map["products"])
    target = next((c for c in candidates if c in df.columns), None)
    if not target:
        return []
    tmp = df.copy()
    tmp[target] = tmp[target].astype("string").str.strip()
    tmp[rev_col] = pd.to_numeric(tmp[rev_col], errors="coerce").fillna(0.0)
    agg = tmp.groupby(target)[rev_col].sum().sort_values(ascending=False).head(top_n).reset_index()
    total = agg[rev_col].sum() or 1.0
    agg["share"] = agg[rev_col] / total
    return [{"label": row[target], "revenue": float(row[rev_col]), "share": float(row["share"])} for _, row in agg.iterrows()]


def _alerts(table_df: pd.DataFrame) -> List[str]:
    alerts: List[str] = []
    if table_df.empty:
        return alerts
    if "TopCustomerShare" in table_df.columns:
        risky = table_df[table_df["TopCustomerShare"] > 0.4]
        if not risky.empty:
            alerts.append(f"{len(risky)} rep(s) have high concentration risk (>40% top customer).")
    if "MarginPct" in table_df.columns:
        low_margin = table_df[table_df["MarginPct"] < 5]
        if not low_margin.empty:
            alerts.append(f"{len(low_margin)} rep(s) below 5% margin.")
    if "MomentumPct" in table_df.columns:
        fast = table_df[table_df["MomentumPct"] > 20]
        if not fast.empty:
            alerts.append(f"{len(fast)} rep(s) growing >20% vs prior period.")
        declining = table_df[table_df["MomentumPct"] < 0]
        if not declining.empty:
            alerts.append(f"{len(declining)} rep(s) are declining vs prior period.")
    return alerts


def _trend_by_rep(df: pd.DataFrame, top_n: int = 10) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    rev_col = au.revenue_column(df, required=False)
    if not rev_col:
        return []
    rep_id_col, rep_name_col = _ensure_rep_columns(df)
    date_ser = au.pick_first_valid_date_column(df, au.DATE_PRIORITY_ORDER)
    if date_ser is None:
        return []
    tmp = df.copy()
    tmp["_rep_id"] = tmp[rep_id_col].astype("string").str.strip()
    tmp["_rep_name"] = tmp[rep_name_col].astype("string").str.strip()
    tmp["_month"] = date_ser.dt.to_period("M").dt.to_timestamp()
    top_reps = (
        tmp.groupby("_rep_id")[rev_col].sum().sort_values(ascending=False).head(top_n).index.tolist()
    )
    tmp = tmp[tmp["_rep_id"].isin(top_reps)]
    grouped = tmp.groupby(["_month", "_rep_id", "_rep_name"])[rev_col].sum().reset_index()
    return [
        {
            "month": row["_month"].date().isoformat(),
            "rep_id": row["_rep_id"],
            "rep_name": row["_rep_name"],
            "revenue": float(row[rev_col]),
        }
        for _, row in grouped.iterrows()
    ]


def _table_response(params, include_costs: bool) -> Tuple[pd.DataFrame, List[Dict[str, Any]], Dict[str, Any]]:
    cache_key_rollup = _cache_key("salesreps.rollup", params, {"include_costs": include_costs})
    cached = cache.get(cache_key_rollup)
    if cached is not None:
        table, meta = cached
    else:
        frame = _load_filtered_frame(params)
        table = _rep_rollup(frame, include_costs=include_costs)
        table, meta = _finalize_rep_metrics(table, include_costs=include_costs)
        cache.set(cache_key_rollup, (table, meta), timeout=_TTL)
        try:
            current_app.logger.info(
                "salesreps.rollup",
                extra={
                    "rows_base": len(frame),
                    "reps": len(table),
                    "revenue_sum": float(table["Revenue"].sum()) if "Revenue" in table else 0.0,
                    "profit_sum": float(table["Profit"].sum()) if "Profit" in table else 0.0,
                    "margin_pct": float(table["Profit"].sum() / table["Revenue"].sum() * 100.0) if "Profit" in table and table["Revenue"].sum() > 0 else 0.0,
                },
            )
        except Exception:
            pass
    table = table.sort_values("Revenue", ascending=False)
    rows = table.to_dict(orient="records")
    return table, rows, meta


@bp.route("/")
@login_required
@requires_roles("admin", "owner", "gm", "manager", "sales")
def index():
    try:
        filters = _filters_from_request()
    except Exception:
        filters = {}
    salesreps_v2_enabled = _salesreps_v2_enabled()
    template_name = "salesreps/index.html" if salesreps_v2_enabled else "salesreps/index_legacy.html"
    return render_template(
        template_name,
        error_message=None,
        table=[],
        alerts=[],
        kpis={},
        trend=[],
        top_reps=[],
        efficiency=[],
        mix=[],
        filters=filters,
        units_label="Units",
        asp_label="ASP",
        asp_lb_label="ASP / lb",
        has_margin=False,
        salesreps_v2_enabled=salesreps_v2_enabled,
    )


@bp.get("/api/overview")
@login_required
@requires_roles("admin", "owner", "gm", "manager", "sales")
def api_overview():
    params = _filters_from_request()
    include_costs = can_view_costs(current_user)
    table_df, rows, meta = _table_response(params, include_costs)
    payload = {
        "kpis": _build_kpis(table_df, include_costs, meta.get("qty_label", "Units")),
        "alerts": _alerts(table_df),
        "rows": rows,
        "meta": meta,
    }
    return _etag_json(payload)


@bp.get("/api/trend")
@login_required
@requires_roles("admin", "owner", "gm", "manager", "sales")
def api_trend():
    params = _filters_from_request()
    payload = _trend_by_rep(_load_filtered_frame(params))
    return _etag_json({"series": payload})


@bp.get("/api/mix")
@login_required
@requires_roles("admin", "owner", "gm", "manager", "sales")
def api_mix():
    params = _filters_from_request()
    dim = (request.args.get("dim") or "products").strip().lower()
    payload = _mix(_load_filtered_frame(params), dim=dim, top_n=25)
    return _etag_json({"dimension": dim, "rows": payload})


@bp.get("/api/table")
@login_required
@requires_roles("admin", "owner", "gm", "manager", "sales")
def api_table():
    params = _filters_from_request()
    include_costs = can_view_costs(current_user)
    table_df, rows, meta = _table_response(params, include_costs)
    search = (request.args.get("search") or request.args.get("q") or "").lower().strip()
    if search:
        rows = [r for r in rows if search in str(r.get("RepName") or "").lower()]
    sort = (request.args.get("sort") or request.args.get("sort_by") or "revenue").lower()
    dir_raw = (request.args.get("dir") or request.args.get("sort_dir") or "desc").lower()
    reverse = dir_raw not in {"asc", "ascending", "up", "1"}
    sort_map = {
        "rep": "RepName",
        "repname": "RepName",
        "rep_name": "RepName",
        "revenue": "Revenue",
        "profit": "Profit",
        "margin_pct": "MarginPct",
        "orders": "Orders",
        "customers": "Customers",
        "weight": "WeightLb",
        "weight_lb": "WeightLb",
        "units": "Units",
        "asp_lb": "ASPLb",
        "asp": "ASP",
        "top_customer_share": "TopCustomerShare",
    }
    sort_key = sort_map.get(sort, "Revenue")
    if sort_key == "RepName":
        rows = sorted(rows, key=lambda r: (r.get("RepName") or "").lower(), reverse=reverse)
    else:
        rows = sorted(rows, key=lambda r: float(r.get(sort_key, 0) or 0), reverse=reverse)
    page = max(1, int(request.args.get("page", 1)))
    try:
        page_size = int(request.args.get("page_size", 25))
    except Exception:
        page_size = 25
    if page_size not in {25, 50, 100}:
        page_size = 25
    start = (page - 1) * page_size
    end = start + page_size
    return _etag_json({
        "total": len(rows),
        "page": page,
        "page_size": page_size,
        "rows": rows[start:end],
        "meta": meta,
    })


@bp.get("/export.xlsx")
@login_required
@requires_roles("admin", "owner", "gm", "manager", "sales")
def export_xlsx():
    try:
        filters = _filters_from_request()
    except Exception:
        filters = filters_service.default_filters(current_user)
    scope = filters_service.scope_from_user(current_user)
    try:
        sheets = salesreps_bundle.build_salesreps_export_workbook_sheets(filters, scope, request.args)
    except Exception as exc:  # pragma: no cover - defensive
        current_app.logger.exception("salesreps.export.failed", extra={"format": "xlsx"})
        return jsonify({"error": {"message": str(exc)}}), 503
    filename = _salesreps_export_filename(filters, "xlsx")
    return dataframes_to_xlsx_response(sheets, filename=filename)


@bp.get("/export.csv")
@login_required
@requires_roles("admin", "owner", "gm", "manager", "sales")
def export_csv():
    try:
        filters = _filters_from_request()
    except Exception:
        filters = filters_service.default_filters(current_user)
    scope = filters_service.scope_from_user(current_user)
    try:
        frame = salesreps_bundle.build_salesreps_export_frame(filters, scope, request.args)
    except Exception as exc:  # pragma: no cover - defensive
        current_app.logger.exception("salesreps.export.failed", extra={"format": "csv"})
        return jsonify({"error": {"message": str(exc)}}), 503
    filename = _salesreps_export_filename(filters, "csv")
    return dataframe_to_csv_response(frame, filename=filename)


def _drilldown_payload(rep_id: str, params) -> Dict[str, Any]:
    scoped = _rep_scoped_frame(rep_id, params)
    if scoped is None:
        return {}
    if scoped.empty:
        return {
            "empty": True,
            "summary": {},
            "trend": [],
            "customers": [],
            "products": [],
            "mix": [],
            "alerts": [],
            "meta": {
                "qty_label": "Units",
                "asp_label": "ASP",
                "asp_lb_label": "ASP / lb",
                "has_margin": False,
            },
            "top_customer_share": None,
        }
    scoped = _enrich_customer_names(scoped)
    include_costs = can_view_costs(current_user)
    table = _rep_rollup(scoped, include_costs=include_costs)
    table, meta = _finalize_rep_metrics(table, include_costs=include_costs)
    if table is None or table.empty:
        return {
            "empty": True,
            "summary": {},
            "trend": [],
            "customers": [],
            "products": [],
            "mix": [],
            "alerts": [],
            "meta": meta,
            "top_customer_share": None,
        }
    summary = table.iloc[0].to_dict()
    monthly_trend = _monthly_trend(scoped, include_profit=include_costs)
    customers = au.leaderboard(scoped, "CustomerId", "CustomerName", limit=50)
    products = au.leaderboard(scoped, "ProductId", "ProductName", limit=50)
    mix = _mix(scoped, dim="products", top_n=15)
    alerts = _alerts(table)
    top_customer_share = None
    try:
        total_revenue = float(summary.get("Revenue", 0.0) or 0.0)
        top_rev = float(customers[0]["revenue"]) if customers else 0.0
        top_customer_share = (top_rev / total_revenue) if total_revenue else None
    except Exception:
        top_customer_share = None
    # Logging for diagnostics
    try:
        null_names = sum(1 for r in customers if not r.get("name"))
        current_app.logger.info(
            "salesreps.drilldown_payload",
            extra={
                "rep_id": rep_id,
                "rows": len(scoped),
                "trend_rows": len(monthly_trend),
                "mix_rows": len(mix),
                "customers_rows": len(customers),
                "customer_names_missing": null_names,
            },
        )
    except Exception:
        pass
    return {
        "summary": summary,
        "trend": monthly_trend,
        "customers": customers,
        "products": products,
        "mix": mix,
        "alerts": alerts,
        "meta": meta,
        "top_customer_share": top_customer_share,
    }


@bp.route("/<rep_id>")
@login_required
@requires_roles("admin", "owner", "gm", "manager", "sales")
def rep_detail(rep_id: str):
    _enforce_rep_access(rep_id)
    try:
        params = _filters_from_request()
    except Exception:
        params = {}
    payload = {
        "empty": True,
        "summary": {},
        "trend": [],
        "customers": [],
        "products": [],
        "mix": [],
        "alerts": [],
        "meta": {},
    }
    return render_template(
        "salesreps/drilldown.html",
        rep_id=rep_id,
        payload=payload,
        filters=params,
        salesrep_drilldown_v2_enabled=_drilldown_v2_enabled(),
    )


@bp.get("/api/drilldown/bundle")
@login_required
@requires_roles("admin", "owner", "gm", "manager", "sales")
def drilldown_bundle_api():
    rep_id = (
        request.args.get("rep_id")
        or request.args.get("salesrep_id")
        or request.args.get("sales_rep_id")
        or request.args.get("id")
    )
    if not rep_id:
        return jsonify({"error": {"message": "rep_id is required"}}), 400
    payload = bundle_service.drilldown("salesreps", request.args)
    status_code = 200
    try:
        if isinstance(payload, dict) and payload.get("error"):
            message = str(payload.get("error", {}).get("message", "")).lower()
            status_code = 404 if "not found" in message else 400
    except Exception:
        status_code = 200
    return jsonify(to_json_safe(payload)), status_code


@bp.get("/<rep_id>/api/summary")
@login_required
@requires_roles("admin", "owner", "gm", "manager", "sales")
def rep_api_summary(rep_id: str):
    _enforce_rep_access(rep_id)
    params = _filters_from_request()
    payload = _drilldown_payload(rep_id, params)
    if not payload:
        abort(404)
    return _etag_json(payload)


@bp.get("/<rep_id>/api/trend")
@login_required
@requires_roles("admin", "owner", "gm", "manager", "sales")
def rep_api_trend(rep_id: str):
    _enforce_rep_access(rep_id)
    params = _filters_from_request()
    scoped = _rep_scoped_frame(rep_id, params)
    if scoped is None:
        abort(404)
    include_costs = can_view_costs(current_user)
    return _etag_json({"trend": _monthly_trend(scoped, include_profit=include_costs)})


@bp.get("/<rep_id>/api/customers")
@login_required
@requires_roles("admin", "owner", "gm", "manager", "sales")
def rep_api_customers(rep_id: str):
    _enforce_rep_access(rep_id)
    params = _filters_from_request()
    scoped = _rep_scoped_frame(rep_id, params)
    if scoped is None:
        abort(404)
    scoped = _enrich_customer_names(scoped)
    return _etag_json({"customers": au.leaderboard(scoped, "CustomerId", "CustomerName", limit=100)})


@bp.get("/<rep_id>/api/products")
@login_required
@requires_roles("admin", "owner", "gm", "manager", "sales")
def rep_api_products(rep_id: str):
    _enforce_rep_access(rep_id)
    params = _filters_from_request()
    scoped = _rep_scoped_frame(rep_id, params)
    if scoped is None:
        abort(404)
    return _etag_json({"products": au.leaderboard(scoped, "ProductId", "ProductName", limit=100)})


@bp.get("/<rep_id>/api/mix")
@login_required
@requires_roles("admin", "owner", "gm", "manager", "sales")
def rep_api_mix(rep_id: str):
    _enforce_rep_access(rep_id)
    params = _filters_from_request()
    dim = (request.args.get("dim") or "products").strip().lower()
    scoped = _rep_scoped_frame(rep_id, params)
    if scoped is None:
        abort(404)
    return _etag_json({"dimension": dim, "rows": _mix(scoped, dim=dim, top_n=50)})


@bp.get("/<rep_id>/api/alerts")
@login_required
@requires_roles("admin", "owner", "gm", "manager", "sales")
def rep_api_alerts(rep_id: str):
    _enforce_rep_access(rep_id)
    params = _filters_from_request()
    scoped = _rep_scoped_frame(rep_id, params)
    if scoped is None:
        abort(404)
    include_costs = can_view_costs(current_user)
    table = _rep_rollup(scoped, include_costs=include_costs)
    table, _ = _finalize_rep_metrics(table, include_costs=include_costs)
    return _etag_json({"alerts": _alerts(table)})


@bp.get("/<rep_id>/export.xlsx")
@login_required
@requires_roles("admin", "owner", "gm", "manager", "sales")
def rep_export(rep_id: str):
    return _build_rep_export_response(rep_id, force_dataset="all", force_format="xlsx")


@bp.get("/<rep_id>/export")
@login_required
@requires_roles("admin", "owner", "gm", "manager", "sales")
def rep_export_data(rep_id: str):
    return _build_rep_export_response(rep_id)

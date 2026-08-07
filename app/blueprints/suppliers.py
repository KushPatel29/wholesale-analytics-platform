# app/blueprints/suppliers.py
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple
import time

import numpy as np
import pandas as pd
from flask import (
    Blueprint,
    abort,
    current_app,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required
from werkzeug.datastructures import MultiDict
from werkzeug.exceptions import BadRequest, Forbidden, InternalServerError

from ..core.audit import log_audit
from ..core.data_service import get_fact_df, apply_global_filters
from ..core.exports import dataframes_to_xlsx_response, dataframe_to_csv_response
from ..core.filters import build_global_filter_form
from ..core.rbac import can_view_costs, requires_roles
from app.services import fact_store
from ..services import analytics_utils as au
from app.services import bundle_service, filters_service
from app.services.filters import filters_to_store, resolve_filters
from app.core.features import legacy_pandas_enabled

bp = Blueprint("suppliers", __name__, url_prefix="/suppliers")
logger = logging.getLogger(__name__)


def _suppliers_v2_enabled() -> bool:
    raw = current_app.config.get("SUPPLIERS_V2")
    if raw is None:
        raw = os.getenv("SUPPLIERS_V2", "0")
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _supplier_drilldown_v2_enabled() -> bool:
    raw = current_app.config.get("SUPPLIER_DRILLDOWN_V2")
    if raw is None:
        raw = os.getenv("SUPPLIER_DRILLDOWN_V2", "0")
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _supplier_drilldown_v2_active() -> bool:
    # Safety gate: V2 drilldown behavior is enabled only when both suppliers V2 flags are on.
    return _suppliers_v2_enabled() and _supplier_drilldown_v2_enabled()


def _legacy_disabled_response():
    resp = jsonify({"error": {"message": "Legacy suppliers endpoints are disabled; use /api/suppliers/bundle."}})
    resp.status_code = 410
    return resp


@bp.before_request
def _block_legacy_suppliers():
    if legacy_pandas_enabled():
        return None
    allowed = {
        "suppliers.index",
        "suppliers.drilldown",
        "suppliers.api_drilldown_bundle",
    }
    if request.endpoint in allowed:
        return None
    return _legacy_disabled_response()


class SuppliersDataError(RuntimeError):
    """Raised when supplier data is missing required columns."""

    def __init__(self, message: str, missing: Optional[List[str]] = None):
        super().__init__(message)
        self.missing_columns = missing or []

# ───────────────────────────────────────────────────────────
# Config
# ───────────────────────────────────────────────────────────
VIEW_ROLES = ("production", "gm", "owner", "admin")
TOP_N_DEFAULT = 15
TOP_N_MAX = 200
TABLE_PAGE_SIZE_DEFAULT = 50
TABLE_PAGE_SIZE_MAX = 500
SUPPLIERS_CACHE_VERSION = "cost_rev_resolver_v2"

# ───────────────────────────────────────────────────────────
# Utilities (column detection, safe ops)
# ───────────────────────────────────────────────────────────
def _first_present(df: pd.DataFrame, *cands: str) -> Optional[str]:
    if df is None or df.empty:
        return None
    lower = {c.lower(): c for c in df.columns}
    for c in cands:
        if c and c.lower() in lower:
            return lower[c.lower()]
    return None


_SKU_NAME_CANDIDATES: Tuple[str, ...] = (
    # sku-ish fields last; descriptive ones first when we choose later
    "SkuName",
    "SKUName",
    "ProductName",
    "ProductDescription",
    "ProductLabel",
    "Name_product",
    "Name",
    "SKU",
)

_SKU_TIMESTAMP_CANDIDATES: Tuple[str, ...] = (
    "ProductUpdatedAt",
    "UpdatedAt_product",
    "EffectiveDate",
    "UpdatedAt",
    "ModifiedAt",
    "LastImport",
)


def _format_sku_fallback(product_id: Any, raw: Any = None) -> str:
    candidate = product_id
    if pd.isna(candidate) and raw is not None and not pd.isna(raw):
        candidate = raw
    if pd.isna(candidate):
        return "SKU"
    try:
        return f"SKU {int(candidate)}"
    except Exception:
        text = str(candidate).strip()
        return f"SKU {text}" if text else "SKU"


@lru_cache(maxsize=1)
def _get_sku_map() -> pd.Series:
    """
    Build a ProductId -> HumanName map from the product snapshot,
    preferring the latest non-numeric non-id-like value.
    """
    base_cols = ["ProductId", *(_SKU_NAME_CANDIDATES + _SKU_TIMESTAMP_CANDIDATES)]
    requested_cols = list(dict.fromkeys(base_cols))
    try:
        products = fact_store.get_sales_fact(columns=sorted(requested_cols))
    except Exception:
        try:
            products = fact_store.get_sales_fact()
        except Exception:
            return pd.Series(dtype="object")

    if products is None or products.empty or "ProductId" not in products.columns:
        return pd.Series(dtype="object")

    keep_cols = ["ProductId"]
    keep_cols.extend([c for c in _SKU_NAME_CANDIDATES if c in products.columns])
    keep_cols.extend([c for c in _SKU_TIMESTAMP_CANDIDATES if c in products.columns])
    df = products[keep_cols].copy()
    df = df.dropna(subset=["ProductId"])
    df["ProductId"] = pd.to_numeric(df["ProductId"], errors="coerce")
    df = df[df["ProductId"].notna()]
    if df.empty:
        return pd.Series(dtype="object")
    df["ProductId"] = df["ProductId"].astype("Int64")

    name_cols = [c for c in _SKU_NAME_CANDIDATES if c in df.columns]
    if not name_cols:
        return pd.Series(dtype="object")

    for col in name_cols:
        series = df[col].astype("string").str.strip()
        series = series.replace({"": pd.NA, "nan": pd.NA, "none": pd.NA, "None": pd.NA, "NULL": pd.NA})
        df[col] = series

    names = df[name_cols].bfill(axis=1).iloc[:, 0].astype("string")

    # drop numeric/id lookalikes (e.g., "15662" or exact ProductId as string)
    pid_float = df["ProductId"].astype("float64")
    pid_str = df["ProductId"].astype("string").str.strip()
    names_numeric = pd.to_numeric(names, errors="coerce")
    numeric_match = names_numeric.notna() & pd.notna(pid_float) & (names_numeric == pid_float)
    names = names.where(~numeric_match, pd.NA)
    names = names.where(names != pid_str, pd.NA)

    df["_SkuNameCandidate"] = names

    sort_cols, ascending = ["ProductId"], [True]
    for col in _SKU_TIMESTAMP_CANDIDATES:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
            sort_cols.append(col)
            ascending.append(True)

    df = df.sort_values(sort_cols, ascending=ascending, na_position="last")
    latest = df.drop_duplicates(subset=["ProductId"], keep="last")

    sku_series = latest.set_index("ProductId")["_SkuNameCandidate"].astype("string")
    if sku_series.empty:
        return pd.Series(dtype="object")

    fallback = sku_series.index.to_series().apply(_format_sku_fallback)
    sku_series = sku_series.where(sku_series.notna(), fallback)
    sku_series = sku_series.astype(object)
    return sku_series


def _rev_col(df: pd.DataFrame) -> str:
    """Get revenue column using centralized utilities."""
    return au.revenue_column(df) or "revenue_ordered"


def _cost_col(df: pd.DataFrame) -> Optional[str]:
    """Get cost column using centralized utilities."""
    return au.cost_column(df)


def _qty_item_col(df: pd.DataFrame) -> Optional[str]:
    """Get quantity column using centralized utilities."""
    return au.units_column(df)


def _qty_weight_col(df: pd.DataFrame) -> Optional[str]:
    """Get weight column using centralized utilities."""
    return au.weight_lb_column(df)


def _order_id_col(df: pd.DataFrame) -> Optional[str]:
    """Get order id column using centralized utilities."""
    return au.order_id_column(df)


def _customer_id_col(df: pd.DataFrame) -> Optional[str]:
    """Get customer id column using centralized utilities."""
    return au.customer_id_column(df)


def _supplier_name_col(df: pd.DataFrame) -> Optional[str]:
    """Get supplier name column using centralized utilities."""
    try:
        return au.supplier_name_column(df)
    except ValueError as exc:
        logger.warning("Supplier name column detection failed; using fallbacks (%s)", exc)
        return None


def _customer_name_col(df: pd.DataFrame) -> Optional[str]:
    """Get customer name column using centralized utilities."""
    return au.customer_name_column(df)


def _product_label_cols(df: pd.DataFrame) -> List[str]:
    cands = [
        "ProductDescription",
        "product_description",
        "ProductName",
        "product_name",
        "ProductLabel",
        "product_label",
        "Name_product",
        "name_product",
        "SkuName",
        "SKUName",
        "Name",
        "SKU",
    ]
    return [c for c in cands if c in df.columns]


def _safe_int(v, default=0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _safe_div(num: Any, den: Any) -> Any:
    """Division that returns None for zero/None denominators."""
    try:
        return au.safe_div(num, den)
    except Exception:
        try:
            if den in (None, 0) or (hasattr(pd, "isna") and pd.isna(den)):
                return None
        except Exception:
            pass
        try:
            return num / den
        except Exception:
            return None


def _round2_df(df: pd.DataFrame, cols: Optional[List[str]] = None) -> pd.DataFrame:
    if df.empty:
        return df
    if cols is None:
        cols = df.select_dtypes(include=[np.number]).columns.tolist()
    if cols:
        df[cols] = df[cols].round(2)
    return df


def _search_filter(df: pd.DataFrame, q: Optional[str], cols: List[str]) -> pd.DataFrame:
    """Case-insensitive substring search across `cols`."""
    if not q:
        return df
    q = str(q).strip().lower()
    if not q:
        return df
    mask = pd.Series(False, index=df.index)
    for c in cols:
        if c in df.columns:
            mask = mask | df[c].astype(str).str.lower().str.contains(q, na=False)
    return df[mask]


def _sort_and_page(
    df: pd.DataFrame,
    sort_by: Optional[str],
    sort_dir: str,
    page: int,
    per_page: int,
    default: str = "Revenue",
) -> Tuple[pd.DataFrame, int]:
    """Stable sort (mergesort to keep groups) and paginate."""
    col = sort_by if (sort_by in df.columns) else (default if default in df.columns else df.columns[0])
    ascending = str(sort_dir).lower() == "asc"
    df = df.sort_values(col, ascending=ascending, kind="mergesort")
    total = int(len(df))
    start, end = (page - 1) * per_page, (page - 1) * per_page + per_page
    return df.iloc[start:end].copy(), total


def _hashable_filters(filters: dict) -> Tuple:
    if not isinstance(filters, dict):
        return tuple()
    return tuple(sorted((k, tuple(v) if isinstance(v, (list, tuple, set)) else v) for k, v in filters.items()))


def _resolved_filters_dict() -> Dict[str, Any]:
    try:
        params, _meta = resolve_filters(
            request,
            current_user,
            session_obj=session,
            source=request.args or {},
            sticky_enabled=bool(current_app.config.get("STICKY_FILTERS", True)),
            update_session=False,
        )
        return filters_to_store(params)
    except Exception:
        return session.get("filters", {}) or {}

def _log_context(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    filters = _resolved_filters_dict()
    filters_summary = {k: len(v) if isinstance(v, (list, tuple, set)) else bool(v) for k, v in filters.items()}
    ctx = {
        "request_id": getattr(g, "request_id", None),
        "user_id": getattr(current_user, "id", None) or getattr(current_user, "email", None),
        "filters": filters_summary,
        "cache_key": {"filters_hash": _hashable_filters(filters), "user_key": _user_cache_key()},
    }
    if extra:
        ctx.update(extra)
    return ctx


def _user_cache_key() -> Tuple:
    try:
        uid = getattr(current_user, "id", None) or getattr(current_user, "email", None) or "anon"
        roles = tuple(sorted(getattr(current_user, "roles", []) or []))
    except Exception:
        uid, roles = "anon", tuple()
    return (uid, roles, bool(can_view_costs(current_user)))


def _jsonify_ok(payload: dict):
    def conv(o: Any):
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return float(o)
        if isinstance(o, (np.ndarray,)):
            return o.tolist()
        if isinstance(o, (pd.Timestamp,)):
            return o.to_pydatetime().isoformat()
        return o

    out = {}
    for k, v in payload.items():
        if isinstance(v, (list, tuple)):
            out[k] = [conv(x) for x in v]
        elif isinstance(v, dict):
            out[k] = {kk: conv(vv) for kk, vv in v.items()}
        else:
            out[k] = conv(v)
    return jsonify(out)

# ───────────────────────────────────────────────────────────
# Data prep + caching
# ───────────────────────────────────────────────────────────
def _prepare_df() -> pd.DataFrame:
    base_df = get_fact_df()
    filters = _resolved_filters_dict()
    df = apply_global_filters(base_df, filters)
    return df


def _ensure_supplier_name(df: pd.DataFrame) -> pd.DataFrame:
    """
    Ensure df has a clean string 'SupplierName' (fallbacks to SupplierId)
    so UI can always show names while URLs keep the id.
    """
    if df.empty:
        return df

    out = df.copy()
    supplier_id_col = au.supplier_id_column(out)
    if supplier_id_col and "SupplierId" not in out.columns:
        out["SupplierId"] = out[supplier_id_col]

    cand = _supplier_name_col(out)
    if cand:
        name = out[cand].astype("string").str.strip()
        name = name.replace({"<NA>": None, "": None})
    else:
        name = pd.Series([None] * len(out), index=out.index, dtype="object")

    if "SupplierId" in out.columns:
        sid = out["SupplierId"].astype("string").str.strip()
        out["SupplierName"] = name.fillna(sid)
        out["SupplierName"] = out["SupplierName"].fillna("Unknown Supplier")
    else:
        out["SupplierName"] = name.fillna("Unknown Supplier")

    return out


@lru_cache(maxsize=128)
def _cached_frame(filters_hash: Tuple, user_key: Tuple, version_marker: str) -> Dict[str, Any]:
    df = _prepare_df()
    if df.empty:
        return {
            "df": df,
            "rev_col": None,
            "cost_col": None,
            "units_col": None,
            "weight_col": None,
            "date_col": None,
            "order_id_col": None,
            "customer_id_col": None,
            "name_col": None,
        }

    # 1. Robust Column Detection
    rev = au.revenue_column(df, required=False)
    if not rev:
        # If we can't find revenue, we can't do much, but let's try to proceed carefully
        # or just raise the error as before.
        raise SuppliersDataError(
            "Required revenue column is missing for suppliers view.",
            missing=["Revenue"],
        )

    # Enhanced Cost Detection
    cost_candidates = ["Cost", "cost", "Total Cost", "TotalCost", "ExtCost", "ExtendedCost", "cost_ordered", "cost_shipped"]
    cost = _first_present(df, *cost_candidates)
    if not cost:
        cost = au.cost_column(df)

    units_col = au.units_column(df)
    weight_col = au.weight_lb_column(df)
    date_col = au.date_column(df)
    order_id_col = au.order_id_column(df)
    customer_id_col = au.customer_id_column(df)
    name_col = au.supplier_name_column(df)

    # Log detection for debugging
    if pd.notna(rev):
        logger.debug(f"Suppliers: Revenue column resolved to '{rev}'")
    else:
        logger.warning("Suppliers: Revenue column NOT found")

    logger.info(
        "Suppliers frame columns detected",
        extra=_log_context({
            "detected": {
            "rev": rev,
            "cost": cost,
                "units": units_col,
                "weight_lb": weight_col,
                "date": date_col,
                "order_id": order_id_col,
                "customer_id": customer_id_col,
                "name_col": name_col,
                "top_columns": list(df.columns[:20])
            }
        })
    )

    # 2. Type Coercion (Clean Data)
    # We work on a copy to avoid side effects on the cached global df if it's shared (though _prepare_df returns a scoped copy usually)
    # _prepare_df returns a copy or slice when filters are applied.
    # To be safe, we modify the df in place here as this function is the provider of the "suppliers frame".
    
    # Revenue (resolve per-row when raw revenue is missing/zero)
    resolved_revenue = au.resolve_revenue(
        df,
        revenue_col=rev,
        units_col=units_col,
        weight_col=weight_col,
    )
    if resolved_revenue is None or resolved_revenue.empty:
        df[rev] = pd.to_numeric(df[rev], errors="coerce").fillna(0.0)
    else:
        df["_ResolvedRevenue"] = resolved_revenue.fillna(0.0)
        rev = "_ResolvedRevenue"

    # Units (coerce first so we can use for cost calc)
    if units_col and units_col in df.columns:
        df[units_col] = pd.to_numeric(df[units_col], errors="coerce").fillna(0.0)

    # Cost (resolved per row with weight/unit fallback)
    if cost and cost in df.columns:
        df[cost] = pd.to_numeric(df[cost], errors="coerce")
    resolved_cost = au.resolve_cost(
        df,
        cost_col=cost,
        units_col=units_col,
        weight_col=weight_col,
    )
    df["_ResolvedCost"] = resolved_cost
    cost = "_ResolvedCost"

    # Weight
    if weight_col and weight_col in df.columns:
        df[weight_col] = pd.to_numeric(df[weight_col], errors="coerce").fillna(0.0)

    # Date
    if date_col and date_col in df.columns:
        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")

    # Name column check
    has_name = (name_col and name_col in df.columns) or ("SupplierId" in df.columns)
    if not has_name:
        raise SuppliersDataError(
            "Missing supplier name or id column for suppliers view.",
            missing=["SupplierName", "SupplierId"],
        )

    return {
        "df": df,
        "rev_col": rev,
        "cost_col": cost,
        "units_col": units_col,
        "weight_col": weight_col,
        "date_col": date_col,
        "order_id_col": order_id_col,
        "customer_id_col": customer_id_col,
        "name_col": name_col,
    }


def _get_frame() -> Dict[str, Any]:
    fh = _hashable_filters(_resolved_filters_dict())
    base_version = fact_store.cache_buster() if fact_store is not None else str(time.time())
    version = f"{base_version}:{SUPPLIERS_CACHE_VERSION}"
    return _cached_frame(fh, _user_cache_key(), version)

# ───────────────────────────────────────────────────────────
# Analytics builders
# ───────────────────────────────────────────────────────────
def _share_and_hhi(series: pd.Series) -> Dict[str, float]:
    """
    Return concentration metrics for a revenue series split across groups.
    Now uses centralized analytics utilities for consistent calculation.
    """
    s = au.to_numeric_safe(series)
    tot = float(s.sum()) or 1.0
    shares = (s / tot).clip(lower=0.0).values

    # Use centralized HHI calculation (note: input is already in shares)
    hhi = float(np.sum((shares * 100) ** 2))  # HHI in 0..10,000

    # Calculate top-N shares
    top5_share = float(np.sort(shares)[-5:].sum() * 100)
    top1_share = float(np.sort(shares)[-1] * 100) if len(shares) else 0.0

    return {"hhi": round(hhi, 2), "top5_share": round(top5_share, 2), "top1_share": round(top1_share, 2)}


def build_overview_payload() -> Dict[str, Any]:
    """Supplier overview across current filters (used by page + API)."""
    o = _get_frame()
    df, rev_col, cost_col = o["df"], o["rev_col"], o["cost_col"]
    units_col, weight_col = o.get("units_col"), o.get("weight_col")
    order_id_col, customer_id_col = o.get("order_id_col"), o.get("customer_id_col")
    date_col = o.get("date_col")

    if df.empty or not rev_col:
        return {"kpis": {}, "top": {"labels": [], "values": []}, "trend": {"labels": [], "values": []}}

    data = _ensure_supplier_name(df)

    # Group per supplier
    gk = ["SupplierId", "SupplierName"] if "SupplierId" in data.columns else ["SupplierName"]
    agg = (
        data.groupby(gk, observed=True)
        .agg(
            Revenue=(rev_col, "sum"),
            Orders=(order_id_col, "nunique") if order_id_col else (rev_col, "count"),
            Products=("ProductId", "nunique") if "ProductId" in data.columns else (rev_col, "count"),
            Customers=(customer_id_col, "nunique") if customer_id_col else (rev_col, "count"),
        )
        .reset_index()
    )
    if not customer_id_col:
        agg["Customers"] = np.nan

    # Spend / Margin (role-guarded)
    # Ensure show_costs is based on permission primarily
    permission_to_view = can_view_costs(current_user)
    has_cost_col = bool(cost_col and cost_col in data.columns)

    if permission_to_view and has_cost_col:
        spend = data.groupby(gk, observed=True).agg(Spend=(cost_col, au.sum_cost)).reset_index()
        agg = agg.merge(spend, on=gk, how="left")
        agg["Profit"] = au.safe_profit(agg["Revenue"], agg["Spend"])
        agg["Margin%"] = au.safe_margin_pct(agg["Revenue"], agg["Spend"])
    else:
        agg["Spend"] = np.nan
        agg["Profit"] = np.nan
        agg["Margin%"] = np.nan

    # KPI aggregates
    total_revenue = float(pd.to_numeric(agg["Revenue"], errors="coerce").sum())
    total_suppliers = int(agg["SupplierId"].nunique()) if "SupplierId" in agg.columns else int(len(agg))
    order_denom = data[order_id_col].nunique() if order_id_col else max(len(data), 1)
    avg_order_value = float(
        (pd.to_numeric(data[rev_col], errors="coerce").sum() / (order_denom if order_denom else 1))
    )

    # 12M monthly trend
    trend_labels, trend_values = [], []
    if date_col and date_col in data.columns:
        mt = data.groupby(data[date_col].dt.to_period("M").dt.to_timestamp(), observed=True)[rev_col].sum().sort_index()
        if not mt.empty:
            mt_l12 = mt.tail(12)
            trend_labels = [d.strftime("%Y-%m") for d in mt_l12.index.to_pydatetime()]
            trend_values = [float(round(v, 2)) for v in mt_l12.values]

    # Top suppliers by revenue
    top = agg.sort_values("Revenue", ascending=False).head(TOP_N_DEFAULT)
    labels = top["SupplierName"].astype(str).fillna(top.get("SupplierId", "").astype(str) if "SupplierId" in top.columns else "").tolist()
    values = [float(round(x, 2)) for x in top["Revenue"].values]

    # Concentration
    rev_series = agg.set_index("SupplierId")["Revenue"] if "SupplierId" in agg.columns else agg.set_index("SupplierName")["Revenue"]
    conc = _share_and_hhi(rev_series)

    avg_margin = None
    if permission_to_view and has_cost_col:
        avg_margin_val = pd.to_numeric(agg["Margin%"], errors="coerce").mean()
        avg_margin = float(round(avg_margin_val, 2)) if pd.notna(avg_margin_val) else None

    kpis = {
        "total_revenue": round(total_revenue, 2),
        "total_suppliers": total_suppliers,
        "avg_order_value": round(avg_order_value, 2),
        "avg_margin": avg_margin,
        "concentration_hhi": conc["hhi"],
        "concentration_top5_share": conc["top5_share"],
        "concentration_top1_share": conc["top1_share"],
    }

    return {"kpis": kpis, "top": {"labels": labels, "values": values}, "trend": {"labels": trend_labels, "values": trend_values}}


def build_table_payload(
    page: int,
    per_page: int,
    sort_by: Optional[str] = None,
    sort_dir: str = "desc",
    q: Optional[str] = None,
) -> Dict[str, Any]:
    o = _get_frame()
    df, rev_col, cost_col = o["df"], o["rev_col"], o.get("cost_col")
    units_col, weight_col = o.get("units_col"), o.get("weight_col")
    order_id_col, customer_id_col = o.get("order_id_col"), o.get("customer_id_col")
    date_col = o.get("date_col")

    if df.empty or not rev_col:
        return {"rows": [], "page": page, "per_page": per_page, "total": 0}

    data = _ensure_supplier_name(df)
    
    # Coercions are already done in _cached_frame for key columns.
    # Only need to ensure presence check for aggregation
    permission_to_view = can_view_costs(current_user)
    has_cost_col = bool(cost_col and (cost_col in data.columns))
    
    group_cols = ["SupplierId", "SupplierName"] if "SupplierId" in data.columns else ["SupplierName"]

    agg_spec: Dict[str, Tuple[str, str]] = {"Revenue": (rev_col, "sum")}
    if permission_to_view and has_cost_col:
        agg_spec["Cost"] = (cost_col, au.sum_cost)
    if units_col and units_col in data.columns:
        agg_spec["Units"] = (units_col, "sum")
    if weight_col and weight_col in data.columns:
        agg_spec["WeightLb"] = (weight_col, "sum")
    if order_id_col and order_id_col in data.columns:
        agg_spec["Orders"] = (order_id_col, "nunique")
    if customer_id_col and customer_id_col in data.columns:
        agg_spec["Customers"] = (customer_id_col, "nunique")
    if "ProductId" in data.columns:
        agg_spec["Products"] = ("ProductId", "nunique")

    if date_col and date_col in data.columns:
        agg_spec["LastSold"] = (date_col, "max")

    grp = data.groupby(group_cols, dropna=False, observed=True).agg(**agg_spec).reset_index()

    if "Orders" not in grp.columns:
        grp["Orders"] = data.groupby(group_cols, dropna=False, observed=True).size().values
    if "Customers" not in grp.columns:
        grp["Customers"] = np.nan
    if "Units" not in grp.columns:
        grp["Units"] = np.nan
    if "WeightLb" not in grp.columns:
        grp["WeightLb"] = np.nan
    if "Products" not in grp.columns:
        grp["Products"] = np.nan
    
    # Always ensure Cost exists if user has permission, even if data is missing
    if permission_to_view and "Cost" not in grp.columns:
        grp["Cost"] = np.nan

    grp["Revenue"] = pd.to_numeric(grp["Revenue"], errors="coerce")
    grp["Units"] = pd.to_numeric(grp["Units"], errors="coerce")
    grp["WeightLb"] = pd.to_numeric(grp["WeightLb"], errors="coerce")
    if "Cost" in grp.columns:
        grp["Cost"] = pd.to_numeric(grp["Cost"], errors="coerce")

    grp["AvgSalePrice"] = _safe_div(grp["Revenue"], grp["Units"])
    grp["AvgSalePricePerLb"] = _safe_div(grp["Revenue"], grp["WeightLb"])

    if permission_to_view and "Cost" in grp.columns:
        grp["Profit"] = au.safe_profit(grp["Revenue"], grp["Cost"])
        margin_pct = au.safe_margin_pct(grp["Revenue"], grp["Cost"])
        roi_pct = au.safe_roi_pct(grp["Profit"], grp["Cost"])
        grp["MarginPct"] = margin_pct / 100.0 if isinstance(margin_pct, pd.Series) else margin_pct
        grp["ROIPct"] = roi_pct / 100.0 if isinstance(roi_pct, pd.Series) else roi_pct
        grp["AvgCostPerUnit"] = _safe_div(grp["Cost"], grp["Units"])
        grp["AvgCostPerLb"] = _safe_div(grp["Cost"], grp["WeightLb"])
        grp["ProfitPerUnit"] = _safe_div(grp["Profit"], grp["Units"])
        grp["ProfitPerLb"] = _safe_div(grp["Profit"], grp["WeightLb"])
    else:
        for col in ["Profit", "MarginPct", "ROIPct", "AvgCostPerUnit", "AvgCostPerLb", "ProfitPerUnit", "ProfitPerLb"]:
            grp[col] = np.nan

    float_cols = [
        "Revenue",
        "Cost",
        "Profit",
        "AvgSalePrice",
        "AvgSalePricePerLb",
        "AvgCostPerUnit",
        "AvgCostPerLb",
        "ProfitPerUnit",
        "ProfitPerLb",
        "MarginPct",
        "ROIPct",
    ]

    if "LastSold" in grp.columns:
        # Date is already datetime from _cached_frame
        grp["LastSold"] = grp["LastSold"].dt.strftime("%Y-%m-%d").fillna("")

    grp = grp.replace([np.inf, -np.inf], np.nan)
    grp = _round2_df(grp, cols=[c for c in (float_cols + ["Units", "WeightLb"]) if c in grp.columns])

    # optional search + sort + paging
    grp = _search_filter(grp, q, ["SupplierName", "SupplierId"])
    page_df, total = _sort_and_page(grp, sort_by, sort_dir, page, per_page, default="Revenue")

    rows = page_df.to_dict(orient="records")
    return {"rows": rows, "page": page, "per_page": per_page, "total": total}


def build_drilldown_payload(supplier_id: str) -> Dict[str, Any]:
    o = _get_frame()
    df, rev_col, cost_col = o["df"], o["rev_col"], o["cost_col"]
    units_col, weight_col = o.get("units_col"), o.get("weight_col")
    order_id_col, customer_id_col = o.get("order_id_col"), o.get("customer_id_col")
    date_col = o.get("date_col")

    if df.empty or not rev_col or "SupplierId" not in df.columns:
        return {}

    df = _ensure_supplier_name(df)
    df_s = df[df["SupplierId"].astype(str) == str(supplier_id)].copy()
    if df_s.empty:
        return {}

    supplier_name = df_s["SupplierName"].dropna().astype(str).iloc[0] if "SupplierName" in df_s.columns else str(supplier_id)

    # Trend (12M)
    months, monthly_revenue = [], []
    if date_col and date_col in df_s.columns:
        # date_col is already datetime
        mt = df_s.groupby(df_s[date_col].dt.to_period("M").dt.to_timestamp(), observed=True)[rev_col].sum().sort_index()
        if not mt.empty:
            months = [x.strftime("%Y-%m") for x in mt.index.to_pydatetime()]
            monthly_revenue = [float(round(v, 2)) for v in mt.values]

    # Product details (clean names) first — used by charts + table cards
    product_details_df = _build_supplier_product_details(supplier_id)

    # Top products by revenue (use cleaned names)
    top_prod_labels, top_prod_values = [], []
    if not product_details_df.empty and "ProductName" in product_details_df.columns:
        tp_df = product_details_df.sort_values("Revenue", ascending=False).head(TOP_N_DEFAULT)
        top_prod_labels = tp_df["ProductName"].astype(str).tolist()
        top_prod_values = [float(round(v, 2)) for v in tp_df["Revenue"].values]
    else:
        prod_label_col = _first_present(
            df_s, "ProductDescription", "product_description",
            "ProductName", "product_name",
            "ProductLabel", "product_label",
            "Name_product", "name_product",
            "SkuName", "SKUName", "SKU"
        ) or "ProductId"
        tp = df_s.groupby(prod_label_col, observed=True)[rev_col].sum().sort_values(ascending=False).head(TOP_N_DEFAULT)
        top_prod_labels = [str(x) for x in tp.index]
        top_prod_values = [float(round(v, 2)) for v in tp.values]

    # Top customers
    cust_label_col = _customer_name_col(df_s) or (customer_id_col if customer_id_col and customer_id_col in df_s.columns else None)
    if cust_label_col and cust_label_col in df_s.columns:
        tc = df_s.groupby(cust_label_col, observed=True)[rev_col].sum().sort_values(ascending=False).head(TOP_N_DEFAULT)
        top_cust_labels = [str(x) for x in tc.index]
        top_cust_values = [float(round(v, 2)) for v in tc.values]
    else:
        tc = pd.Series(dtype=float)
        top_cust_labels, top_cust_values = [], []

    # Concentration within supplier
    prod_conc = _share_and_hhi(pd.Series(top_prod_values)) if len(top_prod_values) else {"hhi": 0, "top5_share": 0, "top1_share": 0}
    cust_conc = _share_and_hhi(tc)

    # Unit Price distribution & stats for this supplier (Revenue / Qty)
    unit_prices = []
    p10 = p50 = p90 = None
    try:
        qty_cand = [c for c in [units_col, _qty_item_col(df_s)] if c and c in df_s.columns]
        qcol = qty_cand[0] if qty_cand else None
        if qcol:
            q = pd.to_numeric(df_s[qcol], errors="coerce")
            r = pd.to_numeric(df_s[rev_col], errors="coerce")
            with np.errstate(divide="ignore", invalid="ignore"):
                unit_price = (r / q).replace([np.inf, -np.inf], np.nan)
            unit_price = unit_price.dropna()
            if not unit_price.empty:
                unit_prices = [float(round(x, 4)) for x in unit_price.sample(min(len(unit_price), 10000), random_state=42)]
                p10 = float(round(unit_price.quantile(0.10), 2))
                p50 = float(round(unit_price.quantile(0.50), 2))
                p90 = float(round(unit_price.quantile(0.90), 2))
    except Exception:
        pass

    # Margin stats when allowed
    margin_p10 = margin_p50 = margin_p90 = None
    if can_view_costs(current_user) and cost_col and cost_col in df_s.columns:
        m = au.safe_margin_pct(df_s[rev_col], df_s[cost_col])
        m = pd.to_numeric(m, errors="coerce").dropna()
        if not m.empty:
            margin_p10 = float(round(m.quantile(0.10), 2))
            margin_p50 = float(round(m.quantile(0.50), 2))
            margin_p90 = float(round(m.quantile(0.90), 2))

    product_metrics: List[Dict[str, Any]] = []
    if not product_details_df.empty:
        product_metrics = product_details_df.head(TOP_N_DEFAULT).to_dict(orient="records")

    return {
        "supplier_id": supplier_id,
        "supplier_name": supplier_name,
        "trend": {"labels": months, "values": monthly_revenue},
        "top_products": {"labels": top_prod_labels, "values": top_prod_values, "concentration": prod_conc},
        "top_customers": {"labels": top_cust_labels, "values": top_cust_values, "concentration": cust_conc},
        "unit_price": {"values": unit_prices, "p10": p10, "p50": p50, "p90": p90},
        "margin_stats": {"p10": margin_p10, "p50": margin_p50, "p90": margin_p90},
        "product_metrics": product_metrics,
    }


def _build_supplier_product_details(supplier_id: str) -> pd.DataFrame:
    """Return detailed product-level metrics for a supplier, with human-readable names."""
    o = _get_frame()
    df, rev_col, cost_col = o["df"], o["rev_col"], o["cost_col"]
    if df.empty or not rev_col or "SupplierId" not in df.columns:
        return pd.DataFrame()

    df_s = df[df["SupplierId"].astype(str) == str(supplier_id)].copy()
    if df_s.empty:
        return pd.DataFrame()

    show_costs = can_view_costs(current_user)
    has_cost_col = bool(cost_col and cost_col in df_s.columns)
    units_col = o.get("units_col") if o.get("units_col") in df_s.columns else _qty_item_col(df_s)
    units_col = units_col if units_col in df_s.columns else None
    weight_col = o.get("weight_col") if o.get("weight_col") in df_s.columns else _qty_weight_col(df_s)
    weight_col = weight_col if weight_col in df_s.columns else None
    order_id_col = o.get("order_id_col") if o.get("order_id_col") in df_s.columns else _order_id_col(df_s)
    order_id_col = order_id_col if order_id_col in df_s.columns else None
    customer_id_col = o.get("customer_id_col") if o.get("customer_id_col") in df_s.columns else _customer_id_col(df_s)
    customer_id_col = customer_id_col if customer_id_col in df_s.columns else None

    # Normalize product identifiers used for grouping in the export
    product_label_cols = _product_label_cols(df_s)
    prod_id_col = "ProductId" if "ProductId" in df_s.columns else None
    if prod_id_col:
        df_s["_ExportProductId"] = df_s[prod_id_col]
    else:
        fallback_id_col = product_label_cols[0] if product_label_cols else None
        df_s["_ExportProductId"] = df_s[fallback_id_col] if fallback_id_col else df_s.index

    # Prefer descriptive text for display
    label_col = None
    for cand in product_label_cols:
        if cand != prod_id_col:
            label_col = cand
            break

    fallback_id_str = df_s["_ExportProductId"].astype(str)
    if label_col:
        name_series = df_s[label_col]
        df_s["_ExportProductName"] = name_series.where(name_series.notna(), fallback_id_str)
    else:
        df_s["_ExportProductName"] = fallback_id_str
    df_s["_ExportProductName"] = df_s["_ExportProductName"].fillna(fallback_id_str)

    # Pick the best human-facing name source (descriptive first; SKU-ish last)
    sku_col = _first_present(
        df_s,
        "ProductDescription", "product_description",
        "ProductName", "product_name",
        "ProductLabel", "product_label",
        "Name_product", "name_product",
        "SkuName", "SKUName", "Name", "SKU",
    )
    if sku_col:
        sku_series = df_s[sku_col]
        df_s["_ExportSkuName"] = sku_series.where(sku_series.notna(), df_s["_ExportProductName"])
    else:
        df_s["_ExportSkuName"] = df_s["_ExportProductName"]
    df_s["_ExportSkuName"] = df_s["_ExportSkuName"].fillna(df_s["_ExportProductName"])

    def _first_nonempty(series: pd.Series) -> Optional[str]:
        for val in series:
            if pd.notna(val):
                sval = str(val).strip()
                if sval and sval.lower() not in {"nan", "none"}:
                    return sval
        return None

    agg_spec = {"Product": ("_ExportProductName", _first_nonempty), "Revenue": (rev_col, "sum")}
    if order_id_col:
        agg_spec["Orders"] = (order_id_col, "nunique")
    if customer_id_col:
        agg_spec["Customers"] = (customer_id_col, "nunique")
    if units_col:
        agg_spec["Units"] = (units_col, "sum")
    if weight_col:
        agg_spec["WeightLb"] = (weight_col, "sum")
    if show_costs and cost_col and cost_col in df_s.columns:
        agg_spec["Cost"] = (cost_col, au.sum_cost)

    meta_sources = {
        "SkuName": ["_ExportSkuName"],
        "ProductName": ["ProductName", "product_name"],
        "ProductLabel": ["ProductLabel", "product_label"],
        "Name_product": ["Name_product", "name_product"],
        "ProductDescription": ["ProductDescription", "product_description"],
        "SKU": ["SKU"],
    }
    for out_col, candidates in meta_sources.items():
        for source_col in candidates:
            if source_col in df_s.columns:
                agg_spec[out_col] = (source_col, _first_nonempty)
                break

    details = df_s.groupby("_ExportProductId", observed=True).agg(**agg_spec).reset_index()
    details = details.rename(columns={"_ExportProductId": "ProductId"})

    raw_product_ids = details["ProductId"].copy()
    details["ProductId"] = pd.to_numeric(details["ProductId"], errors="coerce").astype("Int64")
    product_id_str = details["ProductId"].astype("string").str.strip()

    top_df = details.copy()

    def _first_valid_from_row(row: pd.Series) -> str:
        for val in row:
            sval = str(val).strip()
            if sval and sval.lower() not in {"nan", "none"}:
                return sval
        return ""

    name_cols = [c for c in [
        "ProductDescription", "product_description",
        "ProductName", "product_name",
        "ProductLabel", "product_label",
        "Name_product", "name_product",
        "SkuName", "SKUName",
    ] if c in top_df.columns]
    if name_cols:
        top_df["SkuName"] = top_df[name_cols].apply(_first_valid_from_row, axis=1).astype("string")
    else:
        top_df["SkuName"] = ""

    if "ProductId" in top_df.columns:
        same_as_id = top_df["SkuName"].astype("string").str.strip() == product_id_str
        top_df.loc[same_as_id, "SkuName"] = ""

    global_sku_map = _get_sku_map()
    if "ProductId" in top_df.columns and not global_sku_map.empty:
        product_id_numeric = pd.to_numeric(top_df["ProductId"], errors="coerce")
        mapped_names = product_id_numeric.map(global_sku_map)
        mapped_names = mapped_names.astype("string").str.strip()
        missing = top_df["SkuName"].astype("string").str.strip().eq("")
        top_df.loc[missing, "SkuName"] = mapped_names.where(mapped_names.notna(), top_df.loc[missing, "SkuName"])

    top_df["SkuName"] = top_df["SkuName"].astype("string").replace({"<NA>": ""}).fillna("").str.strip()
    missing = top_df["SkuName"].eq("")
    if missing.any():
        fallback = top_df.loc[missing].apply(
            lambda row: _format_sku_fallback(row["ProductId"], raw_product_ids.loc[row.name]), axis=1
        )
        top_df.loc[missing, "SkuName"] = fallback.astype("string").str.strip()

    # Compose final 'ProductName' (prefer SKU code if available, with readable name)
    if "SKU" in top_df.columns:
        top_df["SKU"] = top_df["SKU"].astype("string").replace({"<NA>": ""}).fillna("").str.strip()
    else:
        top_df["SKU"] = ""

    sku_clean = top_df["SKU"].astype("string").str.strip()
    if "ProductId" in top_df.columns:
        bad = sku_clean == product_id_str
        top_df.loc[bad, "SKU"] = ""
        sku_clean = top_df["SKU"].astype("string").str.strip()

    name_clean = top_df["SkuName"].astype("string").str.strip()
    has_sku = sku_clean.ne("")
    has_name = name_clean.ne("")
    top_df["Product"] = np.where(has_sku, np.where(has_name, sku_clean + " - " + name_clean, sku_clean), name_clean)

    product_clean = top_df["Product"].astype("string").str.strip()
    if "ProductId" in top_df.columns:
        fallback_mask = product_clean.eq("") & product_id_str.notna() & product_id_str.ne("")
        if fallback_mask.any():
            top_df.loc[fallback_mask, "Product"] = "SKU " + product_id_str[fallback_mask]

    top_df["ProductName"] = top_df["Product"].astype("string").replace({"<NA>": ""}).fillna("").str.strip()

    if "ProductId" in top_df.columns:
        compare_mask = product_id_str.notna() & product_id_str.ne("")
        if compare_mask.any():
            unequal_share = (~(top_df.loc[compare_mask, "ProductName"].astype("string") == product_id_str[compare_mask])).mean()
            assert unequal_share > 0.8, "Product column still equals ProductId; mapping failed."

    top_df["SkuName"] = top_df["SkuName"].astype(object)
    top_df["ProductName"] = top_df["ProductName"].astype(object)

    drop_cols = [c for c in ("Product", "SKU") if c in top_df.columns]
    if drop_cols:
        top_df = top_df.drop(columns=drop_cols)

    details = top_df

    # Ensure expected numeric columns exist even when data or permissions are missing
    if "Units" not in details.columns:
        details["Units"] = np.nan
    if "WeightLb" not in details.columns:
        details["WeightLb"] = np.nan
    if "Cost" not in details.columns:
        details["Cost"] = np.nan
    if "Orders" not in details.columns:
        details["Orders"] = df_s.groupby("_ExportProductId", observed=True).size().values
    if "Customers" not in details.columns:
        details["Customers"] = np.nan

    details["Revenue"] = pd.to_numeric(details["Revenue"], errors="coerce")
    details["Units"] = pd.to_numeric(details["Units"], errors="coerce")
    details["WeightLb"] = pd.to_numeric(details["WeightLb"], errors="coerce")
    if "Cost" in details.columns:
        details["Cost"] = pd.to_numeric(details["Cost"], errors="coerce")

    details["AvgSalePrice"] = _safe_div(details["Revenue"], details["Units"])
    details["AvgSalePricePerLb"] = _safe_div(details["Revenue"], details["WeightLb"])

    if show_costs and cost_col and cost_col in df_s.columns:
        revenue = details["Revenue"]
        cost = details["Cost"]
        profit = au.safe_profit(revenue, cost)
        details["Profit"] = profit
        details["Margin%"] = au.safe_margin_pct(revenue, cost)
        details["ROI%"] = au.safe_roi_pct(profit, cost)
        details["AvgCostPerUnit"] = _safe_div(cost, details["Units"])
        details["ProfitPerUnit"] = _safe_div(profit, details["Units"])
        details["AvgCostPerLb"] = _safe_div(cost, details["WeightLb"])
        details["ProfitPerLb"] = _safe_div(profit, details["WeightLb"])
    else:
        details["Cost"] = np.nan
        details["Profit"] = np.nan
        details["Margin%"] = np.nan
        details["ROI%"] = np.nan
        details["AvgCostPerUnit"] = np.nan
        details["ProfitPerUnit"] = np.nan
        details["AvgCostPerLb"] = np.nan
        details["ProfitPerLb"] = np.nan

    desired_cols = [
        "ProductName", "Revenue", "Orders", "Customers", "Units", "WeightLb", "Cost",
        "AvgSalePrice", "Profit", "Margin%", "ROI%", "AvgCostPerLb",
        "AvgSalePricePerLb", "AvgCostPerUnit", "ProfitPerUnit", "ProfitPerLb",
    ]

    details = details.replace([np.inf, -np.inf], np.nan)
    details = details.sort_values("Revenue", ascending=False).reset_index(drop=True)
    details = details.loc[:, [c for c in desired_cols if c in details.columns]]
    return _round2_df(details)


def _build_top_products_summary_frame(details: pd.DataFrame) -> pd.DataFrame:
    if details is None or details.empty:
        return pd.DataFrame(columns=["ProductName", "Revenue"])

    summary = details.copy()
    if "Revenue" in summary.columns:
        summary = summary.sort_values("Revenue", ascending=False)

    product_col = None
    for cand in ("ProductName", "Product", "SkuName"):
        if cand in summary.columns:
            product_col = cand
            break

    summary_cols: List[str] = []
    if product_col:
        summary_cols.append(product_col)
    if "Revenue" in summary.columns:
        summary_cols.append("Revenue")
    else:
        numeric_cols = summary.select_dtypes(include=[np.number]).columns.tolist()
        if numeric_cols:
            summary_cols.append(numeric_cols[0])

    summary = summary.loc[:, [c for c in summary_cols if c in summary.columns]].copy()
    if product_col and product_col in summary.columns and product_col != "ProductName":
        summary = summary.rename(columns={product_col: "ProductName"})
    if "ProductName" in summary.columns:
        summary["ProductName"] = summary["ProductName"].astype(object)
    return _round2_df(summary)

# ───────────────────────────────────────────────────────────
# Pages (server render)
# ───────────────────────────────────────────────────────────
@bp.route("/", methods=["GET", "POST"])
@login_required
@requires_roles(*VIEW_ROLES)
def index():
    if not legacy_pandas_enabled():
        try:
            payload = bundle_service.bundle("suppliers", request.args)
        except Exception:  # pragma: no cover - defensive
            logger.exception("suppliers.bundle_render_failed")
            payload = {"kpis": {}, "charts": {}, "table": {"rows": []}, "meta": {}}
        filter_options_bootstrap = None
        try:
            filters_norm, _meta = resolve_filters(
                request,
                current_user,
                session_obj=session,
                source=request.args or {},
                sticky_enabled=bool(current_app.config.get("STICKY_FILTERS", True)),
            )
            filters_norm_dict = filters_to_store(filters_norm)
            try:
                filter_options_bootstrap = filters_service.load_filter_options(
                    filters_norm,
                    filters_service.scope_from_user(current_user),
                    requested_keys=("statuses", "regions", "methods", "customers", "suppliers", "products", "sales_reps"),
                    use_cache=True,
                )
                if isinstance(filter_options_bootstrap, dict):
                    filter_options_bootstrap.setdefault("meta", {})
                    filter_options_bootstrap["meta"]["source"] = "server-inline"
            except Exception:
                logger.exception("suppliers.inline_filter_options_failed")
        except Exception:
            filters_norm_dict = {}
        kpis_payload = payload.get("kpis", {}) if isinstance(payload, dict) else {}
        charts_payload = payload.get("charts", {}) if isinstance(payload, dict) else {}
        table_payload = payload.get("table", {}) if isinstance(payload, dict) else {}
        trend_payload = charts_payload.get("trend_12m", {}) if isinstance(charts_payload, dict) else {}
        top_payload = charts_payload.get("top_suppliers", {}) if isinstance(charts_payload, dict) else {}
        rows = []
        for row in table_payload.get("rows", []) or []:
            supplier_id = row.get("supplier_id") or row.get("SupplierId") or row.get("key")
            supplier_name = row.get("supplier_name") or row.get("SupplierName") or row.get("label") or supplier_id
            revenue = row.get("revenue")
            if revenue is None:
                revenue = row.get("Revenue")
            revenue = float(revenue or 0.0)
            cost = row.get("cost")
            if cost is None:
                cost = row.get("Cost")
            profit = row.get("profit")
            if profit is None:
                profit = row.get("Profit")
            if profit is None and cost is not None:
                try:
                    profit = revenue - float(cost)
                except Exception:
                    profit = None
            margin_pct = row.get("margin_pct")
            if margin_pct is None:
                margin_pct = row.get("MarginPct")
            roi_pct = row.get("roi_pct")
            if roi_pct is None:
                roi_pct = row.get("ROIPct")
            units = row.get("units")
            if units is None:
                units = row.get("Units")
            weight_lb = row.get("weight_lb")
            if weight_lb is None:
                weight_lb = row.get("WeightLb")
            rows.append(
                {
                    "SupplierId": supplier_id,
                    "SupplierName": supplier_name or "Unknown",
                    "Revenue": revenue,
                    "Cost": cost,
                    "Profit": profit,
                    "MarginPct": margin_pct,
                    "ROIPct": roi_pct,
                    "Units": units,
                    "WeightLb": weight_lb,
                    "AvgSalePricePerLb": row.get("avg_sale_price_per_lb") or row.get("AvgSalePricePerLb"),
                    "AvgCostPerUnit": row.get("avg_cost_per_unit") or row.get("AvgCostPerUnit"),
                    "AvgCostPerLb": row.get("avg_cost_per_lb") or row.get("AvgCostPerLb"),
                    "ProfitPerUnit": row.get("profit_per_unit") or row.get("ProfitPerUnit"),
                    "ProfitPerLb": row.get("profit_per_lb") or row.get("ProfitPerLb"),
                    "Products": row.get("products") or row.get("Products"),
                    "Orders": row.get("orders") or row.get("Orders"),
                    "LastSold": row.get("last_sold") or row.get("LastSold"),
                }
            )
        kpis_render = dict(kpis_payload or {})
        kpis_render["avg_margin"] = kpis_payload.get("avg_margin_pct")
        # Ensure template-safe defaults for strict undefined environments.
        for key, default in {
            "total_revenue": 0,
            "total_suppliers": 0,
            "avg_order_value": 0,
            "concentration_hhi": 0,
            "concentration_top1_share": 0,
            "concentration_top5_share": 0,
            "avg_margin": None,
        }.items():
            kpis_render.setdefault(key, default)
        suppliers_v2 = _suppliers_v2_enabled()
        template_name = "suppliers/index_v2.html" if suppliers_v2 else "suppliers/index.html"
        return render_template(
            template_name,
            form=None,
            filters=filters_norm_dict,
            rows=rows,
            kpis=kpis_render,
            table_rows=rows,
            suppliers_warning=None,
            trend_labels=trend_payload.get("labels", []) or [],
            trend_values=trend_payload.get("revenue", []) or [],
            sup_labels=top_payload.get("labels", []) or [r["SupplierName"] for r in rows],
            sup_values=top_payload.get("values", []) or [r["Revenue"] for r in rows],
            show_costs=can_view_costs(current_user),
            suppliers_v2=suppliers_v2,
            payload=payload,
            filter_options_bootstrap=filter_options_bootstrap,
        )
    try:
        base_df = get_fact_df()
        filters = _resolved_filters_dict()
        form = build_global_filter_form(base_df, data=filters)

        if request.method == "POST" and form.validate_on_submit():
            filters.update({
                "start_date": form.start_date.data.isoformat() if getattr(form.start_date, "data", None) else None,
                "end_date": form.end_date.data.isoformat() if getattr(form.end_date, "data", None) else None,
                "regions": list(form.regions.data or []),
                "shipping_methods": list(form.shipping_methods.data or []),
                "customers": list(form.customers.data or []),
                "suppliers": list(getattr(form, "suppliers", []).data or []) if hasattr(form, "suppliers") else filters.get("suppliers", []),
            })
            session["filters"] = filters
            _cached_frame.cache_clear()
            return redirect(url_for("suppliers.index"))

        # Initial payload for server-render fallbacks
        ov = build_overview_payload()
        table = build_table_payload(page=1, per_page=TABLE_PAGE_SIZE_DEFAULT)

        return render_template(
            "suppliers/index.html",
            form=form,
            filters=_resolved_filters_dict(),
            sup_labels=ov.get("top", {}).get("labels", []),
            sup_values=ov.get("top", {}).get("values", []),
            trend_labels=ov.get("trend", {}).get("labels", []),
            trend_values=ov.get("trend", {}).get("values", []),
            kpis=ov.get("kpis", {}),
            table_rows=table.get("rows", []),
            show_costs=can_view_costs(current_user),
        )
    except SuppliersDataError as exc:
        logger.error("Suppliers overview unavailable: %s", exc, extra=_log_context({"missing_columns": exc.missing_columns}))
        error_message = "Suppliers page could not load due to missing columns: " + ", ".join(exc.missing_columns or ["unknown"])
        return render_template(
            "suppliers/index.html",
            form=build_global_filter_form(get_fact_df(), data=_resolved_filters_dict()),
            filters=_resolved_filters_dict(),
            sup_labels=[],
            sup_values=[],
            trend_labels=[],
            trend_values=[],
            kpis={},
            table_rows=[],
            show_costs=can_view_costs(current_user),
            error_message=error_message,
        )
    except Exception:
        logger.exception("Error loading suppliers index", extra=_log_context())
        raise InternalServerError("An error occurred while loading suppliers.")


@bp.route("/<supplier_id>")
@login_required
@requires_roles(*VIEW_ROLES)
def drilldown(supplier_id):
    if not legacy_pandas_enabled():
        if _supplier_drilldown_v2_active():
            args = MultiDict(request.args)
            args.setlist("supplier_id", [str(supplier_id)])
            args.setlist("supplier_drilldown_v2", ["1"])
            try:
                payload = bundle_service.drilldown("suppliers", args)
            except Forbidden:
                scope_info = {}
                try:
                    from app.core import access_policy

                    candidate = access_policy.get_current_scope(use_cache=True)
                    if isinstance(candidate, dict):
                        scope_info = candidate
                    elif hasattr(candidate, "as_dict"):
                        scope_info = candidate.as_dict()
                except Exception:
                    scope_info = {}
                logger.warning(
                    "suppliers.drilldown.access_denied",
                    extra={
                        "user_id": getattr(current_user, "id", None),
                        "required_permission": "page.suppliers.view",
                        "supplier_id": str(supplier_id),
                        "scope_hash": scope_info.get("scope_hash"),
                    },
                )
                raise
            except Exception:
                logger.exception("suppliers.drilldown_v2.bundle_failed", extra={"supplier_id": str(supplier_id)})
                payload = {"error": {"message": "Unable to load supplier drilldown data."}, "meta": {}}

            if not isinstance(payload, dict):
                payload = {"error": {"message": "Unable to load supplier drilldown data."}, "meta": {}}

            kpis_payload = payload.get("kpis") or {}
            charts_payload = payload.get("charts") or {}
            supplier_v2 = payload.get("supplier_v2") or payload.get("v2") or {}
            if not isinstance(supplier_v2, dict):
                supplier_v2 = {}
            page_notice = None
            if payload.get("error"):
                try:
                    page_notice = str((payload.get("error") or {}).get("message") or "Unable to load supplier drilldown data.")
                except Exception:
                    page_notice = "Unable to load supplier drilldown data."
            trend_payload = payload.get("trend") or charts_payload.get("trend") or {}
            top_prod_payload = charts_payload.get("top_products") or {}
            top_cust_payload = charts_payload.get("top_customers") or {}
            unit_payload = charts_payload.get("unit_price") or {}

            return render_template(
                "suppliers/drilldown_v2.html",
                supplier_id=str(kpis_payload.get("supplier_id") or supplier_id),
                supplier_name=str(kpis_payload.get("supplier_name") or supplier_id),
                supplier={"SupplierId": str(kpis_payload.get("supplier_id") or supplier_id), "SupplierName": str(kpis_payload.get("supplier_name") or supplier_id)},
                months=trend_payload.get("labels") or [],
                monthly_revenue=trend_payload.get("revenue") or [],
                top_prod_labels=top_prod_payload.get("labels") or [],
                top_prod_values=top_prod_payload.get("values") or [],
                top_cust_labels=top_cust_payload.get("labels") or [],
                top_cust_values=top_cust_payload.get("values") or [],
                unit_price_stats=(unit_payload.get("stats") or {}),
                unit_prices=(unit_payload.get("values") or []),
                margin_stats=(charts_payload.get("margin_stats") or {}),
                product_metrics=((payload.get("table") or {}).get("rows") or []),
                prod_conc=(top_prod_payload.get("concentration") or {}),
                cust_conc=(top_cust_payload.get("concentration") or {}),
                show_costs=can_view_costs(current_user),
                supplier_v2=supplier_v2,
                payload=payload,
                page_notice=page_notice,
            )
        return render_template(
            "suppliers/drilldown.html",
            supplier_id=supplier_id,
            supplier_name=str(supplier_id),
            supplier={"SupplierId": supplier_id, "SupplierName": str(supplier_id)},
            months=[],
            monthly_revenue=[],
            top_prod_labels=[],
            top_prod_values=[],
            top_cust_labels=[],
            top_cust_values=[],
            unit_price_stats={},
            unit_prices=[],
            margin_stats={},
            product_metrics=[],
            prod_conc={},
            cust_conc={},
            show_costs=can_view_costs(current_user),
        )
    try:
        payload = build_drilldown_payload(supplier_id)
        if not payload:
            return render_template(
                "suppliers/drilldown.html",
                supplier_id=supplier_id,
                supplier_name=str(supplier_id),
                supplier={"SupplierId": supplier_id, "SupplierName": str(supplier_id)},
                months=[], monthly_revenue=[],
                top_prod_labels=[], top_prod_values=[],
                top_cust_labels=[], top_cust_values=[],
                unit_price_stats={}, unit_prices=[],
                margin_stats={}, product_metrics=[],
                show_costs=can_view_costs(current_user),
            )
        return render_template(
            "suppliers/drilldown.html",
            supplier_id=payload["supplier_id"],
            supplier_name=payload["supplier_name"],
            supplier={"SupplierId": payload["supplier_id"], "SupplierName": payload["supplier_name"]},
            months=payload["trend"]["labels"],
            monthly_revenue=payload["trend"]["values"],
            top_prod_labels=payload["top_products"]["labels"],
            top_prod_values=payload["top_products"]["values"],
            prod_conc=payload["top_products"]["concentration"],
            top_cust_labels=payload["top_customers"]["labels"],
            top_cust_values=payload["top_customers"]["values"],
            cust_conc=payload["top_customers"]["concentration"],
            unit_price_stats={"p10": payload["unit_price"]["p10"], "p50": payload["unit_price"]["p50"], "p90": payload["unit_price"]["p90"]},
            unit_prices=payload["unit_price"]["values"],
            margin_stats=payload["margin_stats"],
            product_metrics=payload.get("product_metrics", []),
            show_costs=can_view_costs(current_user),
        )
    except SuppliersDataError as exc:
        logger.error("Supplier drilldown unavailable: %s", exc, extra=_log_context({"missing_columns": exc.missing_columns, "supplier_id": supplier_id}))
        error_message = "Supplier details could not load due to missing columns: " + ", ".join(exc.missing_columns or ["unknown"])
        return render_template(
            "suppliers/drilldown.html",
            supplier_id=supplier_id,
            supplier_name=str(supplier_id),
            supplier={"SupplierId": supplier_id, "SupplierName": str(supplier_id)},
            months=[], monthly_revenue=[],
            top_prod_labels=[], top_prod_values=[],
            top_cust_labels=[], top_cust_values=[],
            unit_price_stats={}, unit_prices=[],
            margin_stats={}, product_metrics=[],
            show_costs=can_view_costs(current_user),
            error_message=error_message,
        )
    except Exception:
        logger.exception("Error in supplier drilldown", extra=_log_context({"supplier_id": supplier_id}))
        raise InternalServerError("An error occurred while loading supplier details.")

# ───────────────────────────────────────────────────────────
# JSON APIs
# ───────────────────────────────────────────────────────────
@bp.get("/api/drilldown/bundle")
@login_required
@requires_roles(*VIEW_ROLES)
def api_drilldown_bundle():
    supplier_id = request.args.get("supplier_id")
    if not supplier_id:
        abort(400, "supplier_id is required")
    payload = bundle_service.drilldown("suppliers", request.args)
    return jsonify(payload)


@bp.get("/api/overview")
@login_required
@requires_roles(*VIEW_ROLES)
def api_overview():
    try:
        return _jsonify_ok(build_overview_payload())
    except SuppliersDataError as exc:
        logger.warning("Suppliers overview unavailable (api): %s", exc, extra=_log_context({"missing_columns": exc.missing_columns}))
        return jsonify(
            {
                "error": str(exc),
                "missing_columns": exc.missing_columns,
                "request_id": getattr(g, "request_id", None),
            }
        ), 400


@bp.get("/api/table")
@login_required
@requires_roles(*VIEW_ROLES)
def api_table():
    page = max(_safe_int(request.args.get("page", 1), 1), 1)
    per_page_req = _safe_int(request.args.get("per_page", TABLE_PAGE_SIZE_DEFAULT), TABLE_PAGE_SIZE_DEFAULT)
    per_page = max(min(per_page_req, TABLE_PAGE_SIZE_MAX), 1)
    sort_by = request.args.get("sort_by")
    sort_dir = request.args.get("sort_dir", "desc")
    q = request.args.get("q")
    try:
        return _jsonify_ok(build_table_payload(page, per_page, sort_by=sort_by, sort_dir=sort_dir, q=q))
    except SuppliersDataError as exc:
        logger.warning("Suppliers table unavailable (api): %s", exc, extra=_log_context({"missing_columns": exc.missing_columns}))
        return jsonify(
            {
                "error": str(exc),
                "missing_columns": exc.missing_columns,
                "request_id": getattr(g, "request_id", None),
            }
        ), 400


@bp.get("/api/drilldown")
@login_required
@requires_roles(*VIEW_ROLES)
def api_drilldown():
    supplier_id = request.args.get("supplier_id")
    if not supplier_id:
        abort(400, "supplier_id is required")
    try:
        return _jsonify_ok(build_drilldown_payload(supplier_id))
    except SuppliersDataError as exc:
        logger.warning("Suppliers drilldown unavailable (api): %s", exc, extra=_log_context({"missing_columns": exc.missing_columns}))
        return jsonify(
            {
                "error": str(exc),
                "missing_columns": exc.missing_columns,
                "request_id": getattr(g, "request_id", None),
            }
        ), 400

# ───────────────────────────────────────────────────────────
# Exports
# ───────────────────────────────────────────────────────────
@bp.get("/export")
@login_required
@requires_roles(*VIEW_ROLES)
def export_overview():
    """Export supplier overview (KPIs, Trend, Top, Table)."""
    fmt = (request.args.get("format") or "xlsx").lower()

    try:
        ov = build_overview_payload()
        table = build_table_payload(page=1, per_page=TABLE_PAGE_SIZE_MAX)
    except SuppliersDataError as exc:
        logger.error("Suppliers export unavailable: %s", exc, extra=_log_context({"missing_columns": exc.missing_columns}))
        raise BadRequest(str(exc))

    kpi_df = pd.DataFrame([ov.get("kpis", {})])
    trend_df = pd.DataFrame({"Month": ov.get("trend", {}).get("labels", []), "Revenue": ov.get("trend", {}).get("values", [])})
    top_df = pd.DataFrame({"Supplier": ov.get("top", {}).get("labels", []), "Revenue": ov.get("top", {}).get("values", [])})
    tbl_df = pd.DataFrame(table.get("rows", []))

    frames = {
        "KPIs": _round2_df(kpi_df),
        "Trend": _round2_df(trend_df),
        "TopSuppliers": _round2_df(top_df),
        "Table": _round2_df(tbl_df),
    }

    if fmt == "csv":
        return dataframe_to_csv_response(frames["Table"], filename="suppliers_table.csv")
    return dataframes_to_xlsx_response(frames, filename="suppliers_overview.xlsx")


@bp.get("/export/<supplier_id>")
@login_required
@requires_roles(*VIEW_ROLES)
def export_supplier(supplier_id):
    """Export a single supplier drilldown (Trend, TopProducts, TopCustomers, Price/Margins)."""
    fmt = (request.args.get("format") or "xlsx").lower()
    try:
        d = build_drilldown_payload(supplier_id) or {}
        product_details = _build_supplier_product_details(supplier_id)
    except SuppliersDataError as exc:
        logger.error("Supplier export unavailable: %s", exc, extra=_log_context({"missing_columns": exc.missing_columns, "supplier_id": supplier_id}))
        raise BadRequest(str(exc))
    top_products_summary = _build_top_products_summary_frame(product_details)
    if top_products_summary.empty:
        top_products_summary = _round2_df(pd.DataFrame({
            "Product": d.get("top_products", {}).get("labels", []),
            "Revenue": d.get("top_products", {}).get("values", []),
        }))

    frames = {
        "Trend": _round2_df(pd.DataFrame({"Month": d.get("trend", {}).get("labels", []), "Revenue": d.get("trend", {}).get("values", [])})),
    }

    if not product_details.empty:
        frames["TopProducts"] = product_details
        frames["TopProductsSummary"] = top_products_summary
    else:
        frames["TopProducts"] = top_products_summary

    frames["TopCustomers"] = _round2_df(pd.DataFrame({
        "Customer": d.get("top_customers", {}).get("labels", []),
        "Revenue": d.get("top_customers", {}).get("values", []),
    }))
    frames["UnitPriceSamples"] = _round2_df(pd.DataFrame({"UnitPrice": d.get("unit_price", {}).get("values", [])}))
    frames["Stats"] = pd.DataFrame([{
        "ProdHHI": d.get("top_products", {}).get("concentration", {}).get("hhi"),
        "ProdTop5Share%": d.get("top_products", {}).get("concentration", {}).get("top5_share"),
        "CustHHI": d.get("top_customers", {}).get("concentration", {}).get("hhi"),
        "CustTop5Share%": d.get("top_customers", {}).get("concentration", {}).get("top5_share"),
        "UP_p10": d.get("unit_price", {}).get("p10"),
        "UP_p50": d.get("unit_price", {}).get("p50"),
        "UP_p90": d.get("unit_price", {}).get("p90"),
        "Margin_p10": d.get("margin_stats", {}).get("p10"),
        "Margin_p50": d.get("margin_stats", {}).get("p50"),
        "Margin_p90": d.get("margin_stats", {}).get("p90"),
    }])

    try:
        log_audit(current_user, "export", {"resource": "supplier", "supplier_id": supplier_id})
        from flask import g as _g
        _g._export_logged = True
    except Exception:
        pass

    if fmt == "csv":
        return dataframe_to_csv_response(frames["Trend"], filename=f"supplier_{supplier_id}_trend.csv")
    return dataframes_to_xlsx_response(frames, filename=f"supplier_{supplier_id}_overview.xlsx")

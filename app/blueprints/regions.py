# app/blueprints/regions.py
from __future__ import annotations

import logging
import os
import time
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from flask import Blueprint, jsonify, render_template, request, session, redirect, url_for, abort, current_app
from flask_login import current_user, login_required
from werkzeug.datastructures import MultiDict

from ..core.data_service import get_fact_df, apply_global_filters
from ..core.filters import build_global_filter_form
from ..core.rbac import requires_roles
from ..core.audit import log_audit
from ..core.exports import (
    dataframes_to_xlsx_response,
    dataframe_to_csv_response,
    sanitize_filename,
)
from ..services import analytics_utils as au
from app.services import fact_store, bundle_service, filters_service, regions_bundle
from app.services.filters import filters_to_store, resolve_filters
from app.core.features import legacy_pandas_enabled

bp = Blueprint("regions", __name__, url_prefix="/regions")
logger = logging.getLogger(__name__)


def _flag_enabled(name: str, default: bool = False) -> bool:
    raw = current_app.config.get(name)
    if raw is None:
        raw = os.getenv(name, "1" if default else "0")
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _regions_v2_enabled() -> bool:
    return _flag_enabled("REGIONS_V2", False)


def _region_overview_v2_enabled() -> bool:
    return _flag_enabled("REGION_OVERVIEW_V2", False)


def _region_drilldown_v2_enabled() -> bool:
    return _flag_enabled("REGION_DRILLDOWN_V2", False)


def _regions_overview_v2_active() -> bool:
    return _regions_v2_enabled() and _region_overview_v2_enabled()


def _regions_drilldown_v2_active() -> bool:
    return _regions_v2_enabled() and _region_drilldown_v2_enabled()


def _legacy_disabled_response():
    resp = jsonify({"error": {"message": "Legacy regions endpoints are disabled; use /api/regions/bundle."}})
    resp.status_code = 410
    return resp


@bp.before_request
def _block_legacy_regions():
    if legacy_pandas_enabled():
        return None
    if request.endpoint and request.endpoint.startswith("regions."):
        return None
    return _legacy_disabled_response()

# ─────────────────────────────────────────────────────────────────────────────
# Utilities / config
# ─────────────────────────────────────────────────────────────────────────────

TOP_N_DEFAULT = 15
ROLLING_WINDOW = 3
CHURN_THRESHOLD_DAYS = 90.0

def _safe_int(v, default=0) -> int:
    try:
        return int(v)
    except Exception:
        return default

def _round2_df(df: pd.DataFrame, cols: Optional[List[str]] = None) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    if cols is None:
        cols = df.select_dtypes(include=[np.number]).columns.tolist()
    if cols:
        df[cols] = df[cols].round(2)
    return df

def _select_revenue_column(df: pd.DataFrame) -> str:
    """Get revenue column using centralized utilities."""
    return au.revenue_column(df) or "revenue_ordered"

def _select_qty_column(df: pd.DataFrame) -> Optional[str]:
    """Get quantity column using centralized utilities."""
    return au.quantity_column(df)

def _region_col(df: pd.DataFrame) -> Optional[str]:
    """Get region column using centralized utilities."""
    return au.region_column(df)

def _customer_name_col(df: pd.DataFrame) -> Optional[str]:
    """Get customer name column using centralized utilities."""
    return au.customer_name_column(df)

def _product_name_col(df: pd.DataFrame) -> Optional[str]:
    """Get product name column using centralized utilities."""
    return au.product_name_column(df)

def _user_regions(user) -> List[str]:
    rid = getattr(user, "region_id", None)
    if not rid:
        return []
    if isinstance(rid, (list, tuple, set)):
        return [str(x).strip() for x in rid if str(x).strip()]
    s = str(rid).replace(";", ",").replace("|", ",")
    return [r.strip() for r in s.split(",") if r.strip()]

def _rolling_avg(s: pd.Series, w: int = ROLLING_WINDOW) -> pd.Series:
    """Calculate rolling average using centralized utilities."""
    return au.calculate_rolling_average(s, window=w, min_periods=1)

def _yoy_growth(curr: pd.Series) -> Optional[float]:
    """
    Calculate YoY growth: last complete month vs. same month prior year.
    Uses centralized calculation utilities.
    """
    if curr is None or curr.empty or not isinstance(curr.index, pd.DatetimeIndex):
        return None
    last = curr.dropna().iloc[-1] if not curr.dropna().empty else None
    if last is None:
        return None
    last_month = curr.dropna().index[-1]
    comp_month = last_month - pd.DateOffset(years=1)
    if comp_month in curr.index and pd.notna(curr.loc[comp_month]):
        # Use centralized YoY growth calculation
        return au.calculate_yoy_growth(float(last), float(curr.loc[comp_month]))
    return None

def _as_float_list(s: pd.Series) -> List[float]:
    s = pd.to_numeric(s, errors="coerce")
    return [float(round(x, 2)) if pd.notna(x) else 0.0 for x in s.tolist()]

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

# NOTE: kept for backward compatibility with older tests and patches.
def scope_dataframe(df: pd.DataFrame, user: object) -> pd.DataFrame:
    """Apply any region-level scoping to the dataframe (no-op by default)."""
    return df

# ─────────────────────────────────────────────────────────────────────────────
# Cached prep
# ─────────────────────────────────────────────────────────────────────────────

def _prepare_df() -> pd.DataFrame:
    base = get_fact_df()
    filters = _resolved_filters_dict()
    df = apply_global_filters(base, filters)
    try:
        df = scope_dataframe(df, current_user)
    except Exception:
        pass
    return df

def _user_cache_key() -> Tuple:
    try:
        uid = getattr(current_user, "id", None) or getattr(current_user, "email", None) or "anon"
        roles = tuple(sorted(getattr(current_user, "roles", []) or []))
        # include region_id to avoid cross-tenant leakage for region-scoped managers
        rids = tuple(_user_regions(current_user))
    except Exception:
        uid, roles, rids = "anon", tuple(), tuple()
    return (uid, roles, rids)

@lru_cache(maxsize=128)
def _cached_region_overview(filters_key: Tuple, user_key: Tuple, version_marker: str) -> Dict[str, object]:
    df = _prepare_df()
    if df.empty:
        return {"df": df, "region_col": None, "rev_col": None, "qty_col": None}
    region = _region_col(df)
    return {
        "df": df,
        "region_col": region,
        "rev_col": _select_revenue_column(df),
        "qty_col": _select_qty_column(df),
    }

def _get_overview_ctx() -> Dict[str, object]:
    version = fact_store.cache_buster() if fact_store is not None else str(time.time())
    return _cached_region_overview(_hashable_filters(_resolved_filters_dict()), _user_cache_key(), version)

# ─────────────────────────────────────────────────────────────────────────────
# Index builders
# ─────────────────────────────────────────────────────────────────────────────

def _build_index_payload() -> Dict[str, object]:
    ctx = _get_overview_ctx()
    df: pd.DataFrame = ctx["df"]
    region_col: Optional[str] = ctx["region_col"]
    rev_col: Optional[str] = ctx["rev_col"]
    qty_col: Optional[str] = ctx["qty_col"]

    if not region_col or df.empty or not rev_col:
        return {
            "region_labels": [],
            "region_values": [],
            "table_rows": [],
            "kpis": {
                "total_revenue": 0.0,
                "regions_count": 0,
                "avg_aov": None,
                "yoy_growth": None,
            },
        }

    # Revenue by region
    revenue_by_region = df.groupby(region_col, observed=True)[rev_col].sum().sort_values(ascending=False)

    # KPI table per region
    orders = df.groupby(region_col, observed=True)["OrderId"].nunique().rename("Orders")
    customers = df.groupby(region_col, observed=True)["CustomerId"].nunique().rename("Customers")
    revenue = revenue_by_region.rename("Revenue")
    aov = (revenue / orders.replace(0, np.nan)).rename("AOV").fillna(0)

    # Repeat rate per region (share of customers with >1 order)
    cust_order_counts = (
        df.groupby([region_col, "CustomerId"], observed=True)["OrderId"]
        .nunique()
        .rename("CustOrders")
        .reset_index()
    )
    repeat = (
        cust_order_counts.assign(Repeat=cust_order_counts["CustOrders"] > 1)
        .groupby(region_col, observed=True)["Repeat"]
        .mean()
        .mul(100.0)
        .rename("RepeatRatePct")
    )

    # Churn % per region using DaysSinceLastOrder if available from a customer aggregation
    # We compute simple per-region churn using the last order date windowed on df itself
    churn_pct = pd.Series(dtype=float, name="ChurnPct")
    if "Date" in df.columns:
        df_dates = df[[region_col, "CustomerId", "Date"]].copy()
        df_dates["Date"] = pd.to_datetime(df_dates["Date"], errors="coerce")
        last_by_cust = df_dates.dropna().groupby([region_col, "CustomerId"], observed=True)["Date"].max().reset_index()
        if not last_by_cust.empty:
            max_date = last_by_cust["Date"].max()
            last_by_cust["DaysSince"] = (max_date - last_by_cust["Date"]).dt.days.astype(float)
            churn_pct = (
                (last_by_cust.assign(Churned=(last_by_cust["DaysSince"] > CHURN_THRESHOLD_DAYS))
                 .groupby(region_col, observed=True)["Churned"].mean()
                 .mul(100.0))
                .rename("ChurnPct")
            )

    # Concentration: share of revenue captured by top customer/product (within region)
    # (computed over all lines; it’s an approximation but fast)
    top_customer_share = pd.Series(dtype=float, name="TopCustomerSharePct")
    top_product_share = pd.Series(dtype=float, name="TopProductSharePct")
    try:
        # Top customer share
        cust_rev = df.groupby([region_col, "CustomerId"], observed=True)[rev_col].sum().rename("CustRev")
        reg_total = df.groupby(region_col, observed=True)[rev_col].sum().rename("RegionRev")
        tc = cust_rev.reset_index().sort_values([region_col, "CustRev"], ascending=[True, False])
        top_cust = tc.groupby(region_col, observed=True).first().reset_index()[[region_col, "CustRev"]]
        top_customer_share = (top_cust.merge(reg_total.reset_index(), on=region_col, how="left")
                              .assign(SharePct=lambda x: np.where(x["RegionRev"] > 0, (x["CustRev"] / x["RegionRev"]) * 100.0, np.nan))
                              .set_index(region_col)["SharePct"])
        top_customer_share.name = "TopCustomerSharePct"
    except Exception:
        pass
    try:
        # Top product share
        prod_key = _product_name_col(df) or "ProductId"
        prod_rev = df.groupby([region_col, prod_key], observed=True)[rev_col].sum().rename("ProdRev")
        reg_total = df.groupby(region_col, observed=True)[rev_col].sum().rename("RegionRev")
        tp = prod_rev.reset_index().sort_values([region_col, "ProdRev"], ascending=[True, False])
        top_prod = tp.groupby(region_col, observed=True).first().reset_index()[[region_col, "ProdRev"]]
        top_product_share = (top_prod.merge(reg_total.reset_index(), on=region_col, how="left")
                             .assign(SharePct=lambda x: np.where(x["RegionRev"] > 0, (x["ProdRev"] / x["RegionRev"]) * 100.0, np.nan))
                             .set_index(region_col)["SharePct"])
        top_product_share.name = "TopProductSharePct"
    except Exception:
        pass

    table = pd.concat([customers, orders, revenue, aov, repeat, churn_pct, top_customer_share, top_product_share], axis=1)
    table = table.reset_index().rename(columns={region_col: "Region"})
    table = table.fillna(0)
    table_rows = _round2_df(table).sort_values("Revenue", ascending=False).to_dict(orient="records")

    # Overall KPIs
    total_revenue = float(round(revenue.sum(), 2))
    regions_count = int(table.shape[0])
    avg_aov = float(round(aov.replace([np.inf, -np.inf], np.nan).dropna().mean(), 2)) if not aov.empty else None

    # YoY growth for the whole dataset (sum by month across all regions)
    yoy_val = None
    if "Date" in df.columns:
        m = pd.to_datetime(df["Date"], errors="coerce").dt.to_period("M").dt.to_timestamp()
        by_month = df.groupby(m, observed=True)[rev_col].sum().sort_index()
        yoy_val = _yoy_growth(by_month)

    return {
        "region_labels": revenue_by_region.index.astype(str).tolist(),
        "region_values": _as_float_list(revenue_by_region),
        "table_rows": table_rows,
        "kpis": {
            "total_revenue": total_revenue,
            "regions_count": regions_count,
            "avg_aov": avg_aov,
            "yoy_growth": float(round(yoy_val, 2)) if yoy_val is not None else None,
        },
    }

# ─────────────────────────────────────────────────────────────────────────────
# Drilldown builders
# ─────────────────────────────────────────────────────────────────────────────

def _build_drilldown_payload(region_name: str) -> Dict[str, object]:
    ctx = _get_overview_ctx()
    df: pd.DataFrame = ctx["df"]
    region_col: Optional[str] = ctx["region_col"]
    rev_col: Optional[str] = ctx["rev_col"]

    if not region_col or df.empty or not rev_col:
        return {
            "months": [], "monthly_revenue": [],
            "top_cust_labels": [], "top_cust_values": [],
            "top_prod_labels": [], "top_prod_values": [],
            "weekday_labels": [], "weekday_values": [],
            "kpi": {},
            "churn_rows": [],
        }

    # Enforce region access for sales_manager
    role = (getattr(current_user, "role", None) or "").lower()
    if role == "sales_manager":
        allowed = _user_regions(current_user)
        if allowed and str(region_name) not in set(allowed):
            abort(403)

    df_r = df[df[region_col].astype(str) == str(region_name)].copy()
    if df_r.empty:
        return {
            "months": [], "monthly_revenue": [],
            "top_cust_labels": [], "top_cust_values": [],
            "top_prod_labels": [], "top_prod_values": [],
            "weekday_labels": [], "weekday_values": [],
            "kpi": {},
            "churn_rows": [],
        }

    # Trend
    months, monthly_revenue = [], []
    yoy = None
    mom = None
    wow = None
    ma3 = []

    if "Date" in df_r.columns:
        dfm = df_r[["Date", rev_col]].copy()
        dfm["Date"] = pd.to_datetime(dfm["Date"], errors="coerce")
        dfm = dfm.dropna(subset=["Date"])
        
        if not dfm.empty:
            # Monthly aggregation
            # We use to_period('M').to_timestamp() to align to month start/end consistently
            m_rev = dfm.set_index("Date").resample("M")[rev_col].sum().fillna(0).sort_index()
            
            # Format months for chart
            months = [d.strftime("%Y-%m") for d in m_rev.index]
            monthly_revenue = _as_float_list(m_rev)
            
            # Metrics
            yoy = _yoy_growth(m_rev)
            ma3 = _as_float_list(_rolling_avg(m_rev))

            # MoM Calculation
            if len(m_rev) >= 2:
                last_val = m_rev.iloc[-1]
                prev_val = m_rev.iloc[-2]
                if prev_val != 0:
                    mom = ((last_val - prev_val) / abs(prev_val)) * 100.0

            # WoW Calculation
            w_rev = dfm.set_index("Date").resample("W")[rev_col].sum().fillna(0).sort_index()
            if len(w_rev) >= 2:
                last_wk = w_rev.iloc[-1]
                prev_wk = w_rev.iloc[-2]
                if prev_wk != 0:
                    wow = ((last_wk - prev_wk) / abs(prev_wk)) * 100.0

    # Top customers
    cust_name = _customer_name_col(df_r) or "CustomerId"
    tc = df_r.groupby(cust_name, observed=True)[rev_col].sum().sort_values(ascending=False).head(TOP_N_DEFAULT)
    top_cust_labels = [str(x) for x in tc.index.to_list()]
    top_cust_values = _as_float_list(tc)

    # Top products
    prod_name = _product_name_col(df_r) or "ProductId"
    tp = df_r.groupby(prod_name, observed=True)[rev_col].sum().sort_values(ascending=False).head(TOP_N_DEFAULT)
    top_prod_labels = [str(x) for x in tp.index.to_list()]
    top_prod_values = _as_float_list(tp)

    # Weekday mix (0=Mon..6=Sun) if dates present
    weekday_labels, weekday_values = [], []
    if "Date" in df_r.columns:
        w = pd.to_datetime(df_r["Date"], errors="coerce").dt.weekday
        wk = df_r.groupby(w, observed=True)[rev_col].sum()
        if not wk.empty:
            weekday_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            vals = [wk.get(i, 0.0) for i in range(7)]
            weekday_values = [float(round(x, 2)) for x in vals]

    # Churn list (customers in this region only)
    churn_rows = []
    if "Date" in df_r.columns:
        # Avoid duplicate columns if cust_name is CustomerId
        cols_sel = ["CustomerId", "Date"]
        if cust_name and cust_name != "CustomerId" and cust_name in df_r.columns:
            cols_sel.append(cust_name)
            
        df_dates = df_r[cols_sel].copy()
        df_dates["Date"] = pd.to_datetime(df_dates["Date"], errors="coerce")
        
        # Group keys
        g_keys = ["CustomerId"]
        if cust_name and cust_name != "CustomerId" and cust_name in df_dates.columns:
            g_keys.append(cust_name)

        last_by_cust = df_dates.dropna().groupby(g_keys, observed=True)["Date"].max().reset_index()
        if not last_by_cust.empty:
            max_date = last_by_cust["Date"].max()
            last_by_cust["DaysSinceLastOrder"] = (max_date - last_by_cust["Date"]).dt.days.astype(float)
            churned = last_by_cust[last_by_cust["DaysSinceLastOrder"] > CHURN_THRESHOLD_DAYS].copy()
            # attach revenue totals to sort by value
            cust_rev = df_r.groupby("CustomerId", observed=True)[rev_col].sum().rename("TotalRevenue")
            churned = churned.merge(cust_rev.reset_index(), on="CustomerId", how="left")
            churned = churned.sort_values(["DaysSinceLastOrder", "TotalRevenue"], ascending=[False, False])
            
            # Ensure CustomerName exists for display
            if cust_name and cust_name != "CustomerId" and cust_name in churned.columns:
                churned = churned.rename(columns={cust_name: "CustomerName"})
            else:
                churned["CustomerName"] = churned["CustomerId"]

            churn_rows = churned.loc[:, ["CustomerId", "CustomerName", "TotalRevenue", "Date", "DaysSinceLastOrder"]]
            churn_rows = churn_rows.rename(columns={"Date": "LastOrder"})
            churn_rows = churn_rows.to_dict(orient="records")

    # KPIs for this region
    kpi = {
        "total_revenue": float(round(df_r[rev_col].sum(), 2)),
        "orders": int(df_r["OrderId"].nunique()) if "OrderId" in df_r.columns else None,
        "customers": int(df_r["CustomerId"].nunique()) if "CustomerId" in df_r.columns else None,
        "avg_order_value": None,
        "yoy_growth": float(round(yoy, 2)) if yoy is not None else None,
        "mom_growth": float(round(mom, 2)) if mom is not None else None,
        "wow_growth": float(round(wow, 2)) if wow is not None else None,
        "ma3_series": ma3,
    }
    if kpi["orders"]:
        kpi["avg_order_value"] = float(round(kpi["total_revenue"] / max(kpi["orders"], 1), 2))

    return {
        "months": months,
        "monthly_revenue": monthly_revenue,
        "top_cust_labels": top_cust_labels,
        "top_cust_values": top_cust_values,
        "top_prod_labels": top_prod_labels,
        "top_prod_values": top_prod_values,
        "weekday_labels": weekday_labels,
        "weekday_values": weekday_values,
        "kpi": kpi,
        "churn_rows": churn_rows,
    }

# ─────────────────────────────────────────────────────────────────────────────
# Routes: index
# ─────────────────────────────────────────────────────────────────────────────

@bp.route("/", methods=["GET", "POST"])
@login_required
@requires_roles("sales_manager", "gm", "owner", "admin")
def index():
    overview_v2_active = _regions_overview_v2_active()
    template_name = "regions/index_v2.html" if overview_v2_active else "regions/index.html"
    if not legacy_pandas_enabled():
        try:
            filters_norm, _meta = resolve_filters(
                request,
                current_user,
                session_obj=session,
                source=request.args or {},
                sticky_enabled=bool(current_app.config.get("STICKY_FILTERS", True)),
            )
            filters_norm_dict = filters_to_store(filters_norm)
        except Exception:
            filters_norm_dict = {}
        return render_template(
            template_name,
            form=None,
            filters=filters_norm_dict,
            region_labels=[],
            region_values=[],
            table_rows=[],
            kpis={},
            regions_v2_enabled=overview_v2_active,
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
            # Bust cached overview for this user when filters change
            _cached_region_overview.cache_clear()
            try:
                log_audit(current_user, "filters_change", {k: v for k, v in filters.items() if v})
            except Exception:
                pass
            return redirect(url_for("regions.index"))

        payload = _build_index_payload()
        return render_template(
            template_name,
            form=form,
            filters=_resolved_filters_dict(),
            region_labels=payload["region_labels"],
            region_values=payload["region_values"],
            table_rows=payload["table_rows"],
            kpis=payload["kpis"],
            regions_v2_enabled=overview_v2_active,
        )
    except Exception:
        logger.exception("Error in regions index")
        # graceful fallback to empty page
        return render_template(
            template_name,
            form=None,
            filters=_resolved_filters_dict(),
            region_labels=[],
            region_values=[],
            table_rows=[],
            kpis={"total_revenue": 0.0, "regions_count": 0, "avg_aov": None, "yoy_growth": None},
            regions_v2_enabled=overview_v2_active,
        )

# ─────────────────────────────────────────────────────────────────────────────
# Routes: drilldown
# ─────────────────────────────────────────────────────────────────────────────

@bp.route("/<region_name>")
@login_required
@requires_roles("sales_manager", "gm", "owner", "admin")
def drilldown(region_name):
    if not legacy_pandas_enabled():
        if _regions_drilldown_v2_active():
            args = MultiDict(request.args)
            args.setlist("region_id", [str(region_name)])
            args.setlist("region_drilldown_v2", ["1"])
            args.setlist("drilldown_v2", ["1"])
            try:
                payload = bundle_service.drilldown("regions", args)
            except Exception:
                logger.exception("regions.drilldown_v2.bundle_failed", extra={"region_id": str(region_name)})
                payload = {"error": {"message": "Unable to load region drilldown data."}, "meta": {}}
            return render_template(
                "regions/drilldown_v2.html",
                region_name=region_name,
                payload=payload,
                regions_drilldown_v2_enabled=True,
            )
        return render_template(
            "regions/drilldown.html",
            region_name=region_name,
            months=[],
            monthly_revenue=[],
            top_cust_labels=[],
            top_cust_values=[],
            top_prod_labels=[],
            top_prod_values=[],
            weekday_labels=[],
            weekday_values=[],
            churn_rows=[],
            kpi={},
            regions_drilldown_v2_enabled=False,
        )
    try:
        data = _build_drilldown_payload(region_name)
        return render_template(
            "regions/drilldown.html",
            region_name=region_name,
            months=data["months"],
            monthly_revenue=data["monthly_revenue"],
            top_cust_labels=data["top_cust_labels"],
            top_cust_values=data["top_cust_values"],
            top_prod_labels=data["top_prod_labels"],
            top_prod_values=data["top_prod_values"],
            weekday_labels=data["weekday_labels"],
            weekday_values=data["weekday_values"],
            churn_rows=data["churn_rows"],
            kpi=data["kpi"],
            regions_drilldown_v2_enabled=False,
        )
    except Exception:
        logger.exception("Error in region drilldown")
        return render_template(
            "regions/drilldown.html",
            region_name=region_name,
            months=[],
            monthly_revenue=[],
            top_cust_labels=[],
            top_cust_values=[],
            top_prod_labels=[],
            top_prod_values=[],
            weekday_labels=[],
            weekday_values=[],
            churn_rows=[],
            kpi={},
            regions_drilldown_v2_enabled=False,
        )


def _regions_effective_filters_and_scope():
    sticky_enabled = bool(current_app.config.get("STICKY_FILTERS", True))
    filters, _meta = resolve_filters(
        request,
        current_user,
        session_obj=session,
        source=request.args or {},
        sticky_enabled=sticky_enabled,
        update_session=False,
    )
    scope = filters_service.scope_from_user(current_user)
    return filters, scope


# ─────────────────────────────────────────────────────────────────────────────
# Exports
# ─────────────────────────────────────────────────────────────────────────────

@bp.route("/export")
@login_required
@requires_roles("sales_manager", "gm", "owner", "admin")
def export_overview():
    """
    Export regions overview (table + bar data). ?format=xlsx|csv
    """
    try:
        fmt = (request.args.get("format") or "xlsx").lower()
        dataset = str(request.args.get("dataset") or "summary").strip().lower()
        stamp = pd.Timestamp.utcnow().strftime("%Y%m%d")
        if not legacy_pandas_enabled():
            args = MultiDict(request.args)
            args["page"] = "1"
            args["page_size"] = "100000"
            if not args.get("sort"):
                args["sort"] = "revenue"
            if not args.get("sort_dir"):
                args["sort_dir"] = "desc"
            payload = bundle_service.bundle("regions", args)
            table_rows = (payload.get("table") or {}).get("rows") or []
            chart = (payload.get("charts") or {}).get("revenue_by_region") or {}
            chart_labels = chart.get("labels") or [row.get("region") for row in table_rows]
            chart_values = chart.get("values") or [row.get("revenue") for row in table_rows]
            df_table = pd.DataFrame(
                [
                    {
                        "Region": row.get("region"),
                        "Customers": row.get("customers"),
                        "Orders": row.get("orders"),
                        "Revenue": row.get("revenue"),
                        "RevenuePrior": row.get("revenue_prior"),
                        "DeltaRevenue": row.get("delta_revenue"),
                        "DeltaRevenuePct": row.get("delta_revenue_pct"),
                        "AOV": row.get("aov"),
                        "Profit": row.get("profit"),
                        "MarginPct": row.get("margin_pct"),
                        "RepeatPct": row.get("repeat_pct"),
                        "ChurnPct": row.get("churn_pct"),
                        "NewCustomerPct": row.get("new_customer_pct"),
                        "TopCustomerSharePct": row.get("top_customer_share_pct"),
                        "TopProductSharePct": row.get("top_product_share_pct"),
                        "TopSupplierSharePct": row.get("top_supplier_share_pct"),
                        "RiskBand": row.get("risk_band"),
                        "RiskSummary": row.get("risk_summary"),
                        "DataQualityFlag": row.get("data_quality_flag"),
                        "CostCoveragePct": row.get("cost_coverage_pct"),
                        "PacksCoveragePct": row.get("packs_coverage_pct"),
                        "RevenuePerCustomer": row.get("revenue_per_customer"),
                        "ProfitPerOrder": row.get("profit_per_order"),
                        "RevenuePerUnit": row.get("revenue_per_unit"),
                        "RevenuePerLb": row.get("revenue_per_lb"),
                    }
                    for row in table_rows
                ]
            )
            df_bar = pd.DataFrame({"Region": chart_labels, "Revenue": chart_values})
            df_momentum = pd.DataFrame(
                [
                    {
                        "Region": row.get("region"),
                        "RevenueCurrent": row.get("revenue_current"),
                        "RevenuePrior": row.get("revenue_prior"),
                        "DeltaRevenue": row.get("delta_revenue"),
                        "DeltaRevenuePct": row.get("delta_revenue_pct"),
                        "DeltaRevenueLabel": row.get("delta_revenue_label"),
                        "DeltaRevenueStatus": row.get("delta_revenue_status"),
                        "DeltaOrders": row.get("delta_orders"),
                        "DeltaCustomers": row.get("delta_customers"),
                        "ProfitDelta": row.get("profit_delta"),
                        "MarginDeltaPP": row.get("margin_delta_pp"),
                    }
                    for row in ((payload.get("momentum") or {}).get("rows") or [])
                ]
            )
            df_risk = pd.DataFrame(
                [
                    {
                        "Region": row.get("region"),
                        "RiskBand": row.get("risk_band"),
                        "RiskSummary": row.get("risk_summary"),
                        "Revenue": row.get("revenue"),
                        "MarginPct": row.get("margin_pct"),
                        "ChurnPct": row.get("churn_pct"),
                        "TopCustomerSharePct": row.get("top_customer_share_pct"),
                        "TopProductSharePct": row.get("top_product_share_pct"),
                        "CostCoveragePct": row.get("cost_coverage_pct"),
                        "PacksCoveragePct": row.get("packs_coverage_pct"),
                        "DeltaRevenue": row.get("delta_revenue"),
                        "DataQualityFlag": row.get("data_quality_flag"),
                    }
                    for row in ((payload.get("risk") or {}).get("rows") or [])
                ]
            )
            kpis = payload.get("kpis") or {}
            meta = payload.get("meta") or {}
            df_summary = pd.DataFrame(
                [
                    {"Metric": "Total Revenue", "Value": kpis.get("total_revenue")},
                    {"Metric": "Total Profit", "Value": kpis.get("profit")},
                    {"Metric": "Margin %", "Value": kpis.get("margin_pct")},
                    {"Metric": "Regions in Scope", "Value": kpis.get("regions_count")},
                    {"Metric": "Customers in Scope", "Value": kpis.get("customers")},
                    {"Metric": "Orders in Scope", "Value": kpis.get("orders")},
                    {"Metric": "Avg Order Value", "Value": kpis.get("avg_order_value")},
                    {"Metric": "YoY Growth %", "Value": kpis.get("yoy_growth")},
                    {"Metric": "Revenue Delta vs Prior", "Value": kpis.get("revenue_delta_prior")},
                    {"Metric": "Revenue Delta vs Prior %", "Value": kpis.get("revenue_delta_prior_pct")},
                    {"Metric": "Revenue HHI", "Value": kpis.get("revenue_hhi")},
                    {"Metric": "Top 1 Share %", "Value": kpis.get("concentration_top1_pct")},
                    {"Metric": "Top 5 Share %", "Value": kpis.get("concentration_top5_pct")},
                    {"Metric": "Repeat Rate %", "Value": kpis.get("repeat_rate_pct")},
                    {"Metric": "New Customer Share %", "Value": kpis.get("new_customer_share_pct")},
                    {"Metric": "Churn Risk Regions", "Value": kpis.get("churn_risk_regions_count")},
                    {"Metric": "Cost Coverage %", "Value": kpis.get("cost_coverage_pct")},
                    {"Metric": "Packs Coverage %", "Value": (meta.get("packs_coverage") or {}).get("packs_coverage_pct")},
                    {"Metric": "Freshness", "Value": (meta.get("freshness") or {}).get("label")},
                    {"Metric": "Window Start", "Value": kpis.get("start")},
                    {"Metric": "Window End", "Value": kpis.get("end")},
                ]
            )
        else:
            payload = _build_index_payload()
            df_table = pd.DataFrame(payload["table_rows"])
            df_bar = pd.DataFrame({"Region": payload["region_labels"], "Revenue": payload["region_values"]})
            df_summary = pd.DataFrame([{"Metric": "TotalRevenue", "Value": (payload.get("kpis") or {}).get("total_revenue")}])
            df_momentum = pd.DataFrame()
            df_risk = pd.DataFrame()

        safe = {
            "table": f"regions_kpi_table_{stamp}",
            "risk": f"regions_risk_concentration_{stamp}",
        }.get(dataset, f"regions_summary_{stamp}")
        if dataset == "table":
            sheets = {"RegionsTable": _round2_df(df_table)}
            csv_df = sheets["RegionsTable"]
        elif dataset == "risk":
            sheets = {"Risk": _round2_df(df_risk if not df_risk.empty else pd.DataFrame())}
            csv_df = sheets["Risk"]
        else:
            sheets = {
                "Summary": _round2_df(df_summary),
                "Regions": _round2_df(df_table),
                "RevenueByRegion": _round2_df(df_bar),
                "Momentum": _round2_df(df_momentum if not df_momentum.empty else pd.DataFrame()),
                "Risk": _round2_df(df_risk if not df_risk.empty else pd.DataFrame()),
            }
            csv_df = sheets["Regions"]
        if fmt == "csv":
            return dataframe_to_csv_response(csv_df, filename=f"{safe}.csv")
        return dataframes_to_xlsx_response(sheets, filename=f"{safe}.xlsx")
    except Exception:
        logger.exception("Failed to export regions overview")
        return dataframes_to_xlsx_response({"Regions": pd.DataFrame()}, filename="regions_overview_error.xlsx")


@bp.route("/export_momentum")
@login_required
@requires_roles("sales_manager", "gm", "owner", "admin")
def export_momentum():
    """
    Export regional momentum comparison (current vs prior comparable window).
    """
    try:
        fmt = (request.args.get("format") or "csv").lower()
        stamp = pd.Timestamp.utcnow().strftime("%Y%m%d")
        if not legacy_pandas_enabled():
            args = MultiDict(request.args)
            args["page"] = "1"
            args["page_size"] = "100000"
            payload = bundle_service.bundle("regions", args)
            momentum = payload.get("momentum") or {}
            rows = momentum.get("rows") or []
            window = momentum.get("window") or {}
            df = pd.DataFrame(
                [
                    {
                        "Region": row.get("region"),
                        "RevenueCurrent": row.get("revenue_current"),
                        "RevenuePrior": row.get("revenue_prior"),
                        "DeltaRevenue": row.get("delta_revenue"),
                        "DeltaRevenuePct": row.get("delta_revenue_pct"),
                        "DeltaRevenueLabel": row.get("delta_revenue_label"),
                        "DeltaRevenueStatus": row.get("delta_revenue_status"),
                        "DeltaOrders": row.get("delta_orders"),
                        "DeltaCustomers": row.get("delta_customers"),
                        "ProfitDelta": row.get("profit_delta"),
                        "MarginDeltaPP": row.get("margin_delta_pp"),
                    }
                    for row in rows
                ],
                columns=[
                    "Region",
                    "RevenueCurrent",
                    "RevenuePrior",
                    "DeltaRevenue",
                    "DeltaRevenuePct",
                    "DeltaRevenueLabel",
                    "DeltaRevenueStatus",
                    "DeltaOrders",
                    "DeltaCustomers",
                    "ProfitDelta",
                    "MarginDeltaPP",
                ],
            )
            meta_df = pd.DataFrame(
                [
                    {
                        "CurrentStart": window.get("current_start"),
                        "CurrentEnd": window.get("current_end"),
                        "PriorStart": window.get("prior_start"),
                        "PriorEnd": window.get("prior_end"),
                        "Note": "Prior period = same duration immediately preceding current window.",
                    }
                ]
            )
        else:
            df = pd.DataFrame(
                columns=[
                    "Region",
                    "RevenueCurrent",
                    "RevenuePrior",
                    "DeltaRevenue",
                    "DeltaRevenuePct",
                    "DeltaOrders",
                    "DeltaCustomers",
                ]
            )
            meta_df = pd.DataFrame([{"Note": "Momentum export is available on the bundle-backed regions page."}])

        if fmt == "xlsx":
            return dataframes_to_xlsx_response(
                {"Momentum": _round2_df(df), "WindowMeta": meta_df},
                filename=f"regions_momentum_{stamp}.xlsx",
            )
        return dataframe_to_csv_response(_round2_df(df), filename=f"regions_momentum_{stamp}.csv")
    except Exception:
        logger.exception("Failed to export regional momentum")
        if (request.args.get("format") or "csv").lower() == "xlsx":
            return dataframes_to_xlsx_response({"Momentum": pd.DataFrame()}, filename="regions_momentum_error.xlsx")
        return dataframe_to_csv_response(pd.DataFrame(), filename="regions_momentum_error.csv")

@bp.route("/<region_name>/export")
@login_required
@requires_roles("sales_manager", "gm", "owner", "admin")
def export_region(region_name):
    """
    Export a region drilldown dataset (trend, top customers, top products, weekday mix).
    """
    try:
        fmt = (request.args.get("format") or "xlsx").lower()
        dataset = str(
            request.args.get("dataset")
            or request.args.get("scope")
            or request.args.get("type")
            or "full"
        ).strip().lower()
        if not legacy_pandas_enabled():
            from app.core import access_policy

            access_policy.enforce_entity_access("regions", region_name, access_policy.get_current_scope(use_cache=True))
            filters, scope = _regions_effective_filters_and_scope()
            export_args = MultiDict(request.args)
            export_args["region_id"] = region_name
            export_args["drilldown_v2"] = "1"
            export_args["region_drilldown_v2"] = "1"
            frames, meta = regions_bundle.build_region_drilldown_export_frames(region_name, filters, scope, export_args)
            stamp = pd.Timestamp.utcnow().strftime("%Y%m%d")
            safe = sanitize_filename(str(region_name) if region_name is not None else "region")
            metadata_df = pd.DataFrame(
                [
                    {"field": "region_id", "value": str(region_name)},
                    {"field": "window_start", "value": meta.get("start")},
                    {"field": "window_end", "value": meta.get("end")},
                    {"field": "prior_start", "value": meta.get("prior_start")},
                    {"field": "prior_end", "value": meta.get("prior_end")},
                    {"field": "filters_query", "value": request.query_string.decode("utf-8", errors="ignore")},
                    {"field": "dataset", "value": dataset},
                ]
            )

            aliases = {
                "summary": ("summary", "Summary", f"region_{safe}_summary_{stamp}"),
                "customers": ("customers", "Customers", f"region_{safe}_customers_{stamp}"),
                "products": ("products", "Products", f"region_{safe}_products_{stamp}"),
                "churn": ("churn", "ChurnRisk", f"region_{safe}_churn_{stamp}"),
                "retention": ("churn", "ChurnRisk", f"region_{safe}_churn_{stamp}"),
                "trend": ("trend", "Trend", f"region_{safe}_trend_{stamp}"),
                "shipping": ("shipping", "ShippingMix", f"region_{safe}_shipping_{stamp}"),
                "suppliers": ("suppliers", "SupplierMix", f"region_{safe}_suppliers_{stamp}"),
                "weekday": ("weekday", "Weekday", f"region_{safe}_weekday_{stamp}"),
                "insights": ("insights", "Insights", f"region_{safe}_insights_{stamp}"),
            }
            if dataset in aliases:
                frame_key, sheet_name, stem = aliases[dataset]
                frame = _round2_df(frames.get(frame_key, pd.DataFrame()).copy())
                if fmt == "csv":
                    return dataframe_to_csv_response(frame, filename=f"{stem}.csv")
                return dataframes_to_xlsx_response(
                    {sheet_name: frame, "Metadata": metadata_df},
                    filename=f"{stem}.xlsx",
                )

            sheets = {
                "Summary": _round2_df(frames.get("summary", pd.DataFrame()).copy()),
                "Trend": _round2_df(frames.get("trend", pd.DataFrame()).copy()),
                "Customers": _round2_df(frames.get("customers", pd.DataFrame()).copy()),
                "Products": _round2_df(frames.get("products", pd.DataFrame()).copy()),
                "ChurnRisk": _round2_df(frames.get("churn", pd.DataFrame()).copy()),
                "ShippingMix": _round2_df(frames.get("shipping", pd.DataFrame()).copy()),
                "SupplierMix": _round2_df(frames.get("suppliers", pd.DataFrame()).copy()),
                "Weekday": _round2_df(frames.get("weekday", pd.DataFrame()).copy()),
                "Insights": frames.get("insights", pd.DataFrame()).copy(),
                "Metadata": metadata_df,
            }
            try:
                log_audit(current_user, "export", {"resource": "region_drilldown", "region": region_name, "dataset": dataset or "full"})
                from flask import g as _g

                _g._export_logged = True
            except Exception:
                pass
            if fmt == "csv":
                return dataframe_to_csv_response(sheets["Trend"], filename=f"region_{safe}_trend.csv")
            return dataframes_to_xlsx_response(sheets, filename=f"region_{safe}_drilldown.xlsx")
        else:
            data = _build_drilldown_payload(region_name)
            months = data["months"]
            monthly_revenue = data["monthly_revenue"]
            top_customers = [
                {"customer_name": name, "revenue": val}
                for name, val in zip(data["top_cust_labels"], data["top_cust_values"])
            ]
            top_products = [
                {"product_name": name, "revenue": val}
                for name, val in zip(data["top_prod_labels"], data["top_prod_values"])
            ]
            weekday_payload = [
                {"weekday": name, "revenue": val}
                for name, val in zip(data["weekday_labels"], data["weekday_values"])
            ]
            shipping_payload = []
            churn_rows = data["churn_rows"]
        safe = sanitize_filename(str(region_name) if region_name is not None else "region")

        trend = pd.DataFrame({"Month": months, "Revenue": monthly_revenue})
        top_cust = pd.DataFrame(
            [
                {
                    "CustomerId": row.get("customer_id"),
                    "Customer": row.get("customer_name") or row.get("customer_id"),
                    "Revenue": row.get("revenue"),
                    "Profit": row.get("profit"),
                }
                for row in top_customers
            ]
        )
        top_prod = pd.DataFrame(
            [
                {
                    "ProductId": row.get("product_id"),
                    "Product": row.get("product_name") or row.get("product_id"),
                    "Revenue": row.get("revenue"),
                    "Profit": row.get("profit"),
                }
                for row in top_products
            ]
        )
        weekday = pd.DataFrame(
            [
                {
                    "Weekday": row.get("weekday"),
                    "Revenue": row.get("revenue"),
                }
                for row in weekday_payload
            ]
        )
        shipping = pd.DataFrame(
            [
                {
                    "Method": row.get("method"),
                    "Revenue": row.get("revenue"),
                    "Pct": row.get("pct"),
                }
                for row in shipping_payload
            ]
        )
        churn = pd.DataFrame(churn_rows)

        sheets = {
            "Trend": _round2_df(trend),
            "TopCustomers": _round2_df(top_cust),
            "TopProducts": _round2_df(top_prod),
            "WeekdayMix": _round2_df(weekday),
            "ShippingMix": _round2_df(shipping),
            "Churned": _round2_df(churn),
        }

        try:
            log_audit(current_user, "export", {"resource": "region_drilldown", "region": region_name})
            from flask import g as _g
            _g._export_logged = True
        except Exception:
            pass

        if fmt == "csv":
            # If CSV requested, export Trend sheet
            return dataframe_to_csv_response(sheets["Trend"], filename=f"region_{safe}_trend.csv")
        return dataframes_to_xlsx_response(sheets, filename=f"region_{safe}_drilldown.xlsx")
    except Exception:
        logger.exception("Failed to export region drilldown")
        return dataframes_to_xlsx_response({"Error": pd.DataFrame()}, filename="region_export_error.xlsx")

@bp.route("/<region_name>/churn_download")
@login_required
@requires_roles("sales_manager", "gm", "owner", "admin")
def churn_download(region_name):
    """
    Kept for backward compatibility; now powered by the drilldown builder.
    """
    try:
        fmt = (request.values.get("format") or "xlsx").lower()
        if not legacy_pandas_enabled():
            from app.core import access_policy

            access_policy.enforce_entity_access("regions", region_name, access_policy.get_current_scope(use_cache=True))
            filters, scope = _regions_effective_filters_and_scope()
            export_args = MultiDict(request.args)
            export_args["region_id"] = region_name
            export_args["drilldown_v2"] = "1"
            export_args["region_drilldown_v2"] = "1"
            churn_frame, _meta = regions_bundle.build_region_drilldown_export_dataset(
                region_name,
                filters,
                scope,
                export_args,
                "churn",
            )
        else:
            data = _build_drilldown_payload(region_name)
            churn_frame = pd.DataFrame(data["churn_rows"])
        safe = sanitize_filename(str(region_name) if region_name is not None else "region")
        out = _round2_df(churn_frame.copy())
        try:
            log_audit(current_user, "export", {"resource": "region_churn", "region": region_name})
            from flask import g as _g
            _g._export_logged = True
        except Exception:
            pass
        if fmt == "csv":
            return dataframe_to_csv_response(out, filename=f"region_{safe}_churned.csv")
        return dataframes_to_xlsx_response({"ChurnedCustomers": out}, filename=f"region_{safe}_churned.xlsx")
    except Exception:
        logger.exception("Failed to export region churn")
        return dataframes_to_xlsx_response({"ChurnedCustomers": pd.DataFrame()}, filename="region_churn_error.xlsx")

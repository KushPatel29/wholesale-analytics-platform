from __future__ import annotations

import json
import hashlib
from typing import Any, Dict

import pandas as pd
from flask import Blueprint, request, Response, session, g, current_app
from flask_login import current_user, login_required

from ..core.data_service import get_fact_df
from ..core.rbac import can_view_costs
from ..cache import cache
from ..services.filters import FilterParams, apply_filters as apply_filter_params, resolve_filters
from ..services import analytics_utils as au
from ..services.cache import cache_key as versioned_cache_key
import time


bp = Blueprint("api_slice", __name__, url_prefix="/api/slice")


def _select_revenue_column(df: pd.DataFrame) -> str:
    return au.revenue_column(df) or "Revenue"


def _select_quantity_column(df: pd.DataFrame) -> str:
    for c in ["QuantityShipped", "QuantityOrdered", "pack_item_count_sum", "pack_weight_lb_sum"]:
        if c in df.columns:
            return c
    return "QuantityOrdered"


def _product_name_col(df: pd.DataFrame) -> str:
    return next((c for c in df.columns if c.lower() in {"product_name", "name"}), "ProductName")


def _customer_name_col(df: pd.DataFrame) -> str:
    return next((c for c in df.columns if c.lower() in {"customername", "customer_name", "name", "customer"}), "CustomerName")


def _region_col(df: pd.DataFrame) -> str | None:
    return next((c for c in df.columns if c.lower() in {"region_name", "region", "regionname", "regionid"}), None)


def _supplier_name_col(df: pd.DataFrame) -> str:
    for c in ["Supplier_Name", "Name", "Supplier_ShortName"]:
        if c in df.columns:
            return c
    return "Supplier_Name"


def _filter_params_for_request() -> FilterParams:
    if hasattr(g, "_slice_params"):
        return getattr(g, "_slice_params")
    params, _meta = resolve_filters(
        request,
        current_user,
        session_obj=session,
        source=request.args or {},
        sticky_enabled=bool(current_app.config.get("STICKY_FILTERS", True)),
        update_session=False,
    )
    setattr(g, "_slice_params", params)
    return params


def _scoped_filtered_df() -> pd.DataFrame:
    if hasattr(g, "_slice_df"):
        return getattr(g, "_slice_df")
    base = get_fact_df()
    df = base
    params = _filter_params_for_request()
    df = apply_filter_params(df, params)
    setattr(g, "_slice_df", df)
    return df


def _slice_cache_key(name: str) -> str:
    params = _filter_params_for_request()
    query_args = {
        k: request.args.getlist(k)
        for k in sorted(request.args.keys())
        if not k.startswith("_")
    }
    extras = {
        "endpoint": f"slice.{name}",
        "user": getattr(current_user, "get_id", lambda: None)(),
        "path": request.path,
        "args": query_args,
    }
    return versioned_cache_key(params, extras)


def _etag_response(payload: Dict[str, Any]) -> Response:
    body = json.dumps(payload, separators=(",", ":"), default=str)
    etag = '"' + hashlib.sha256(body.encode("utf-8")).hexdigest() + '"'
    inm = request.headers.get("If-None-Match")
    if inm and inm == etag:
        resp = Response(status=304)
        resp.headers["ETag"] = etag
        # Allow revalidation caching for this API
        resp.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
        return resp
    resp = Response(response=body, status=200, mimetype="application/json")
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
    return resp


@bp.get("/kpis")
@login_required
@cache.cached(timeout=300, key_prefix=lambda: _slice_cache_key("kpis"))
def kpis():
    df = _scoped_filtered_df()
    if df is None or df.empty:
        return _etag_response({
            "total_customers": 0,
            "total_revenue": 0.0,
            "total_orders": 0,
            "aov": 0.0,
            "churn_rate": 0.0,
        })
    rev_col = _select_revenue_column(df)
    revenue_series = pd.to_numeric(df.get(rev_col, 0), errors="coerce").fillna(0)
    total_revenue = float(revenue_series.sum())
    total_orders = int(pd.Series(df.get("OrderId", pd.Series(dtype='Int64'))).nunique())
    total_customers = int(pd.Series(df.get("CustomerId", pd.Series(dtype='Int64'))).nunique())
    aov = float(revenue_series.groupby(df.get("OrderId")).sum().mean()) if total_orders > 0 else 0.0
    churn_rate = 0.0
    if total_customers > 0 and "CustomerId" in df.columns and "Date" in df.columns:
        cust_last = df.groupby("CustomerId")["Date"].max()
        ref_date = pd.to_datetime(df["Date"].max())
        days_since = (ref_date - cust_last).dt.days
        churned = (days_since > 90).sum()
        churn_rate = float(churned) / float(len(cust_last)) * 100.0
    payload = {
        "total_customers": total_customers,
        "total_revenue": round(total_revenue, 2),
        "total_orders": total_orders,
        "aov": round(aov, 2),
        "churn_rate": round(churn_rate, 2),
    }
    return _etag_response(payload)


@bp.get("/monthly")
@login_required
@cache.cached(timeout=300, key_prefix=lambda: _slice_cache_key("monthly"))
def monthly():
    df = _scoped_filtered_df()
    if df is None or df.empty or "Date" not in df.columns:
        return _etag_response({"months": [], "revenue": [], "orders": []})
    rev_col = _select_revenue_column(df)
    dfm = df[["Date", rev_col, "OrderId"]].copy()
    dfm["Month"] = pd.to_datetime(dfm["Date"]).dt.to_period("M").dt.to_timestamp()
    m_rev = dfm.groupby("Month")[rev_col].sum().sort_index()
    m_ord = dfm.groupby("Month")["OrderId"].nunique().sort_index()
    months = [d.strftime("%Y-%m") for d in m_rev.index.to_pydatetime()]
    revenue = [round(float(x), 2) for x in m_rev.values]
    orders = [int(x) for x in m_ord.values]
    return _etag_response({"months": months, "revenue": revenue, "orders": orders})


@cache.memoize(timeout=300)
def _memoized_customer_agg(cache_token: str, n: int) -> Dict[str, Any]:
    t_fetch = time.perf_counter()
    df = _scoped_filtered_df()
    if df is None or df.empty or "CustomerId" not in df.columns:
        current_app.logger.info(
            "slice.customer_agg",
            extra={"duration_ms": int((time.perf_counter() - t_fetch) * 1000), "rows": 0},
        )
        return {"rows": []}
    rev_col = _select_revenue_column(df)
    name_col = _customer_name_col(df)
    df = df.copy()
    df[rev_col] = pd.to_numeric(df[rev_col], errors="coerce").fillna(0)
    if df["CustomerId"].dtype == object:
        df["CustomerId"] = df["CustomerId"].astype("category")
    agg = df.groupby(["CustomerId"]).agg(
        TotalRevenue=(rev_col, "sum"),
        TotalOrders=("OrderId", "nunique"),
    ).reset_index()
    if name_col in df.columns:
        names = (
            df.dropna(subset=["CustomerId"])
            .drop_duplicates(subset=["CustomerId"])
            [["CustomerId", name_col]]
            .rename(columns={name_col: "CustomerName"})
        )
        agg = agg.merge(names, on="CustomerId", how="left")
    else:
        agg["CustomerName"] = None
    agg["AvgOrderValue"] = (agg["TotalRevenue"] / agg["TotalOrders"].replace(0, pd.NA)).fillna(0)
    top = agg.sort_values(["TotalRevenue", "TotalOrders"], ascending=[False, False]).head(n)
    rows = top[["CustomerId", "CustomerName", "TotalRevenue", "TotalOrders", "AvgOrderValue"]]
    current_app.logger.info(
        "slice.customer_agg",
        extra={
            "duration_ms": int((time.perf_counter() - t_fetch) * 1000),
            "rows": int(len(rows)),
            "source_rows": int(len(df)),
        },
    )
    return {"rows": rows.to_dict(orient="records")}


@bp.get("/customer_agg")
@login_required
def customer_agg():
    token = _slice_cache_key("customer-agg")
    n = request.args.get("n", type=int) or 100
    t0 = time.perf_counter()
    payload = _memoized_customer_agg(token, n)
    duration = time.perf_counter() - t0
    current_app.logger.info(
        "slice.customer_agg.response",
        extra={"duration_ms": int(duration * 1000)},
    )
    return _etag_response(payload)


@cache.memoize(timeout=300)
def _memoized_product_agg(cache_token: str, n: int) -> Dict[str, Any]:
    t_fetch = time.perf_counter()
    df = _scoped_filtered_df()
    if df is None or df.empty or "ProductId" not in df.columns:
        current_app.logger.info(
            "slice.product_agg",
            extra={"duration_ms": int((time.perf_counter() - t_fetch) * 1000), "rows": 0},
        )
        return {"rows": []}
    rev_col = _select_revenue_column(df)
    qty_col = _select_quantity_column(df)
    name_col = _product_name_col(df)
    df = df.copy()
    df[rev_col] = pd.to_numeric(df[rev_col], errors="coerce").fillna(0)
    df[qty_col] = pd.to_numeric(df.get(qty_col), errors="coerce").fillna(0)
    if df["ProductId"].dtype == object:
        df["ProductId"] = df["ProductId"].astype("category")
    grp = df.groupby(["ProductId"]).agg(
        Revenue=(rev_col, "sum"),
        Orders=("OrderId", "nunique"),
        TotalQty=(qty_col, "sum"),
    ).reset_index()
    if name_col in df.columns:
        names = (
            df.dropna(subset=["ProductId"])
            .drop_duplicates(subset=["ProductId"])
            [["ProductId", name_col]]
            .rename(columns={name_col: "ProductName"})
        )
        grp = grp.merge(names, on="ProductId", how="left")
    else:
        grp["ProductName"] = None
    grp["AvgPrice"] = (grp["Revenue"] / grp["TotalQty"].replace(0, pd.NA)).fillna(0)
    top = grp.sort_values(["Revenue", "Orders"], ascending=[False, False]).head(n)
    rows = top[["ProductId", "ProductName", "Revenue", "Orders", "TotalQty", "AvgPrice"]]
    current_app.logger.info(
        "slice.product_agg",
        extra={
            "duration_ms": int((time.perf_counter() - t_fetch) * 1000),
            "rows": int(len(rows)),
            "source_rows": int(len(df)),
        },
    )
    return {"rows": rows.to_dict(orient="records")}


@bp.get("/product_agg")
@login_required
def product_agg():
    token = _slice_cache_key("product-agg")
    n = request.args.get("n", type=int) or 100
    t0 = time.perf_counter()
    payload = _memoized_product_agg(token, n)
    duration = time.perf_counter() - t0
    current_app.logger.info(
        "slice.product_agg.response",
        extra={"duration_ms": int(duration * 1000)},
    )
    return _etag_response(payload)


@bp.get("/region_agg")
@login_required
@cache.cached(timeout=300, key_prefix=lambda: _slice_cache_key("region-agg"))
def region_agg():
    df = _scoped_filtered_df()
    region_col = _region_col(df) if df is not None and not df.empty else None
    if df is None or df.empty or not region_col:
        return _etag_response({"rows": []})
    rev_col = _select_revenue_column(df)
    if df[region_col].dtype == object:
        df[region_col] = df[region_col].astype("category")
    grp = df.groupby(region_col)
    orders = grp["OrderId"].nunique().rename("Orders")
    customers = grp["CustomerId"].nunique().rename("Customers")
    revenue = grp[rev_col].sum().rename("Revenue")
    aov = (revenue / orders.replace(0, pd.NA)).fillna(0).rename("AOV")
    table = pd.concat([customers, orders, revenue, aov], axis=1).reset_index()
    table = table.rename(columns={region_col: "Region"}).fillna(0)
    n = request.args.get("n", type=int) or 100
    top = table.sort_values(["Revenue", "Orders"], ascending=[False, False]).head(n)
    out = top[["Region", "Customers", "Orders", "Revenue", "AOV"]].to_dict(orient="records")
    return _etag_response({"rows": out})


@bp.get("/supplier_agg")
@login_required
@cache.cached(timeout=300, key_prefix=lambda: _slice_cache_key("supplier-agg"))
def supplier_agg():
    df = _scoped_filtered_df()
    if df is None or df.empty or "SupplierId" not in df.columns:
        return _etag_response({"rows": []})
    rev_col = _select_revenue_column(df)
    cost_col = au.cost_column(df) or next((c for c in ["cost_shipped", "cost_ordered"] if c in df.columns), None)
    name_col = _supplier_name_col(df)
    grp_cols = ["SupplierId"]
    if df["SupplierId"].dtype == object:
        df["SupplierId"] = df["SupplierId"].astype("category")
    g = df.groupby(grp_cols).agg(
        Revenue=(rev_col, "sum"),
        Orders=("OrderId", "nunique"),
        Products=("ProductId", "nunique"),
    ).reset_index()
    if name_col in df.columns:
        names = df.dropna(subset=["SupplierId"]).drop_duplicates(subset=["SupplierId"])[["SupplierId", name_col]].rename(columns={name_col: "SupplierName"})
        g = g.merge(names, on="SupplierId", how="left")
    else:
        g["SupplierName"] = None
    show_costs = can_view_costs(current_user)
    if show_costs and cost_col:
        spend = df.groupby(["SupplierId"])[cost_col].sum().rename("Spend").reset_index()
        g = g.merge(spend, on="SupplierId", how="left")
    n = request.args.get("n", type=int) or 100
    top = g.sort_values(["Revenue", "Orders"], ascending=[False, False]).head(n)
    cols = ["SupplierId", "SupplierName", "Revenue", "Orders", "Products"]
    if show_costs and "Spend" in top.columns:
        cols.append("Spend")
    out = top[cols].to_dict(orient="records")
    return _etag_response({"rows": out})


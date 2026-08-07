
from __future__ import annotations

from datetime import date, timedelta
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple
import math
import time

import pandas as pd
from flask_login import current_user

from app.cache import cache
from app.services import overview_v2 as ov2
from app.services.filters import FilterParams, filters_cache_key
from data.store import get_conn as get_duck_conn, init_views as init_duck_views, list_columns as duck_columns, manifest_max_date, manifest_version

try:
    from app.services.products_bundle import TARGET_MARGIN as DEFAULT_TARGET_MARGIN  # type: ignore
except Exception:
    DEFAULT_TARGET_MARGIN = 0.20


INSIGHTS_TTL_SECONDS = 600
INSIGHTS_VERSION = "2026-02-03"
DEFAULT_MIN_REVENUE = 5000.0

_insights_lock: Lock = Lock()
_insights_locks: Dict[str, Lock] = {}


def _dataset_marker() -> str:
    return manifest_max_date() or manifest_version() or ""


def _lock_for(key: str) -> Lock:
    with _insights_lock:
        lock = _insights_locks.get(key)
        if lock is None:
            lock = Lock()
            _insights_locks[key] = lock
    return lock


def _is_month_aligned(start: date, end: date) -> bool:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    if start_ts.day != 1:
        return False
    return (end_ts + pd.Timedelta(days=1)).day == 1


def _periods_from_filters(filters: FilterParams, include_current_month: bool) -> Dict[str, Any]:
    start_dt, end_dt, _ = ov2._normalize_dates(filters, include_current_month)
    if not start_dt or not end_dt:
        today = pd.Timestamp.utcnow().tz_localize(None).date()
        end_dt = end_dt or today
        start_dt = start_dt or (end_dt - timedelta(days=30))
    if start_dt > end_dt:
        start_dt, end_dt = end_dt, start_dt

    aligned = _is_month_aligned(start_dt, end_dt)
    end_ts = pd.Timestamp(end_dt)
    start_ts = pd.Timestamp(start_dt)

    length = max(1, int((end_ts - start_ts).days) + 1)
    curr_start = start_ts
    curr_end = end_ts
    prev_end = curr_start - pd.Timedelta(days=1)
    prev_start = prev_end - pd.Timedelta(days=length - 1)
    yoy_start = curr_start - pd.DateOffset(years=1)
    yoy_end = curr_end - pd.DateOffset(years=1)

    return {
        "aligned": aligned,
        "window_start": start_dt.isoformat(),
        "window_end": end_dt.isoformat(),
        "curr_start": curr_start.date().isoformat(),
        "curr_end": curr_end.date().isoformat(),
        "prev_start": prev_start.date().isoformat(),
        "prev_end": prev_end.date().isoformat(),
        "yoy_start": yoy_start.date().isoformat(),
        "yoy_end": yoy_end.date().isoformat(),
    }

def _where_clause_no_date(filters: FilterParams, cols: set[str]) -> Tuple[str, List[Any]]:
    where_parts: List[str] = ["1=1"]
    params: List[Any] = []

    def _add_clause(values: List[str], column: Optional[str]) -> None:
        if not values or not column:
            return
        placeholders = ", ".join("?" for _ in values)
        where_parts.append(f"{column} IN ({placeholders})")
        params.extend(values)

    region_col = "RegionName" if "RegionName" in cols else None
    method_col = "ShippingMethodName" if "ShippingMethodName" in cols else ("ShippingMethodLabel" if "ShippingMethodLabel" in cols else None)
    customer_col = "CustomerId" if "CustomerId" in cols else ("CustomerName" if "CustomerName" in cols else None)
    supplier_col = "SupplierId" if "SupplierId" in cols else ("SupplierName" if "SupplierName" in cols else None)
    product_col = "ProductId" if "ProductId" in cols else ("ProductName" if "ProductName" in cols else None)
    sales_rep_col = "SalesRepName" if "SalesRepName" in cols else ("PrimarySalesRepName" if "PrimarySalesRepName" in cols else None)

    _add_clause(ov2._values_list(getattr(filters, "regions", ())), region_col)
    _add_clause(ov2._values_list(getattr(filters, "methods", ())), method_col)
    _add_clause(ov2._values_list(getattr(filters, "customers", ())), customer_col)
    _add_clause(ov2._values_list(getattr(filters, "suppliers", ())), supplier_col)
    _add_clause(ov2._values_list(getattr(filters, "products", ())), product_col)
    _add_clause(ov2._values_list(getattr(filters, "sales_reps", ())), sales_rep_col)

    return " AND ".join(where_parts), params


def _struct(val: Any) -> Dict[str, Any]:
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    try:
        return dict(val)
    except Exception:
        return {}


def _fmt_currency(val: Optional[float]) -> str:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return "n/a"
    return f"${val:,.0f}"


def _clean_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        num = float(val)
    except Exception:
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num

def build_insights_payload(
    filters: FilterParams,
    *,
    include_current_month: bool = False,
    defaulted_window: bool = False,
    target_margin: Optional[float] = None,
    min_revenue: Optional[float] = None,
) -> Dict[str, Any]:
    dataset_marker = _dataset_marker()
    cache_key = filters_cache_key(
        current_user,
        filters,
        extras={
            "scope": "overview_insights",
            "dataset": dataset_marker,
            "version": INSIGHTS_VERSION,
            "include_current_month": bool(include_current_month),
            "defaulted_window": bool(defaulted_window),
        },
    )
    cached = cache.get(cache_key)
    if cached:
        if isinstance(cached, dict):
            cached.setdefault("meta", {})["cache_hit"] = True
        return cached

    lock = _lock_for(cache_key)
    with lock:
        cached = cache.get(cache_key)
        if cached:
            if isinstance(cached, dict):
                cached.setdefault("meta", {})["cache_hit"] = True
            return cached

        started = time.perf_counter()
        conn = get_duck_conn()
        init_duck_views(conn)
        cols = duck_columns(conn)

        date_col = ov2._safe_col(cols, "Date", "ShipDate", "OrderDate")
        revenue_col = ov2._safe_col(cols, "Revenue", "TotalRevenue", "Sales")
        cost_col = ov2._safe_col(cols, "Cost", "CostPrice", "TotalCost")
        qty_col = ov2._safe_col(cols, "QuantityShipped", "QuantityOrdered", "Qty", "Units", "ItemCount", "pack_item_count_sum")
        weight_col = ov2._safe_col(cols, "WeightLb", "Weight", "pack_weight_lb_sum")
        customer_id_col = ov2._safe_col(cols, "CustomerId", "CustomerID")
        customer_name_col = ov2._safe_col(cols, "CustomerName", "Customer")
        product_id_col = ov2._safe_col(cols, "ProductId", "ProductID", "SKU")
        product_name_col = ov2._safe_col(cols, "ProductName", "Product", "Description")

        if not date_col or not revenue_col:
            return {
                "insights": {"callouts": [], "message": "Required columns missing for insights."},
                "drivers": {"message": "Required columns missing for drivers."},
                "concentration": {"message": "Required columns missing for concentration."},
                "profitability": {"message": "Required columns missing for profitability."},
                "meta": {"has_data": False, "cache_hit": False},
            }

        target_margin = float(target_margin if target_margin is not None else DEFAULT_TARGET_MARGIN) * 100
        min_revenue = float(min_revenue if min_revenue is not None else DEFAULT_MIN_REVENUE)
        periods = _periods_from_filters(filters, include_current_month)

        date_expr = ov2._col_expr(date_col, "DATE", "NULL")
        revenue_expr = ov2._col_expr(revenue_col, "DOUBLE", "0")
        cost_raw_expr = ov2._col_expr(cost_col, "DOUBLE", "NULL")
        qty_expr = ov2._col_expr(qty_col, "DOUBLE", "0")
        weight_expr = ov2._col_expr(weight_col, "DOUBLE", "0")
        customer_id_expr = ov2._col_expr(customer_id_col, "VARCHAR", "NULL")
        customer_name_expr = ov2._col_expr(customer_name_col, "VARCHAR", "NULL")
        product_id_expr = ov2._col_expr(product_id_col, "VARCHAR", "NULL")
        product_name_expr = ov2._col_expr(product_name_col, "VARCHAR", "NULL")

        where_sql, where_params = _where_clause_no_date(filters, cols)
        use_weight = "TRUE" if weight_col else "FALSE"

        sql = (
            f"""
            WITH scoped_base AS (
                SELECT
                    {date_expr} AS order_date,
                    {revenue_expr} AS revenue,
                    {cost_raw_expr} AS cost_raw,
                    COALESCE({cost_raw_expr}, 0.0) AS cost,
                    {qty_expr} AS qty,
                    {weight_expr} AS weight,
                    {customer_id_expr} AS customer_id,
                    {customer_name_expr} AS customer_name,
                    {product_id_expr} AS product_id,
                    {product_name_expr} AS product_name
                FROM fact
                WHERE {where_sql}
            ),
            scoped AS (
                SELECT
                    *,
                    CASE WHEN {use_weight} THEN COALESCE(weight, 0.0) ELSE COALESCE(qty, 0.0) END AS qty_basis,
                    COALESCE(NULLIF(customer_id, ''), NULLIF(customer_name, ''), 'Unknown') AS customer_key,
                    COALESCE(NULLIF(customer_name, ''), customer_id, 'Unknown') AS customer_label,
                    COALESCE(NULLIF(product_id, ''), NULLIF(product_name, ''), 'Unknown') AS product_key,
                    COALESCE(NULLIF(product_name, ''), product_id, 'Unknown') AS product_label
                FROM scoped_base
                WHERE order_date IS NOT NULL
            ),
            current_window AS (
                SELECT * FROM scoped WHERE order_date BETWEEN ? AND ?
            ),
            prev_window AS (
                SELECT * FROM scoped WHERE order_date BETWEEN ? AND ?
            ),
            yoy_window AS (
                SELECT * FROM scoped WHERE order_date BETWEEN ? AND ?
            ),
            current_totals AS (
                SELECT SUM(revenue) AS revenue, SUM(cost) AS cost, SUM(qty_basis) AS qty, COUNT(*) AS rows
                FROM current_window
            ),
            prev_totals AS (
                SELECT SUM(revenue) AS revenue, SUM(cost) AS cost, SUM(qty_basis) AS qty, COUNT(*) AS rows
                FROM prev_window
            ),
            yoy_totals AS (
                SELECT SUM(revenue) AS revenue, SUM(cost) AS cost, SUM(qty_basis) AS qty, COUNT(*) AS rows
                FROM yoy_window
            ),
            customer_curr AS (
                SELECT customer_key, MAX(customer_id) AS customer_id, MAX(customer_label) AS label, SUM(revenue) AS revenue
                FROM current_window
                GROUP BY 1
            ),
            customer_prev AS (
                SELECT customer_key, MAX(customer_id) AS customer_id, MAX(customer_label) AS label, SUM(revenue) AS revenue
                FROM prev_window
                GROUP BY 1
            ),
            customer_delta AS (
                SELECT
                    COALESCE(c.customer_key, p.customer_key) AS customer_key,
                    COALESCE(c.customer_id, p.customer_id) AS customer_id,
                    COALESCE(c.label, p.label) AS label,
                    COALESCE(c.revenue, 0) AS revenue_curr,
                    COALESCE(p.revenue, 0) AS revenue_prev,
                    COALESCE(c.revenue, 0) - COALESCE(p.revenue, 0) AS delta
                FROM customer_curr c
                FULL OUTER JOIN customer_prev p
                    ON c.customer_key = p.customer_key
            ),
            top_customer_gainer AS (
                SELECT * FROM customer_delta WHERE delta > 0 ORDER BY delta DESC LIMIT 1
            ),
            customer_first AS (
                SELECT customer_key, MAX(customer_id) AS customer_id, MAX(customer_label) AS label, MIN(order_date) AS first_date
                FROM scoped
                GROUP BY 1
            ),
            new_customers AS (
                SELECT customer_key FROM customer_first WHERE first_date BETWEEN ? AND ?
            ),
            current_customers AS (
                SELECT DISTINCT customer_key FROM current_window
            ),
            new_customer_stats AS (
                SELECT
                    (SELECT COUNT(*) FROM current_customers) AS active_customers,
                    (SELECT COUNT(*) FROM new_customers) AS new_customers,
                    (SELECT COALESCE(SUM(revenue), 0) FROM current_window cw JOIN new_customers n ON cw.customer_key = n.customer_key) AS new_revenue
            ),
            cost_coverage AS (
                SELECT
                    COUNT(*) AS rows,
                    SUM(CASE WHEN cost_raw IS NOT NULL THEN 1 ELSE 0 END) AS cost_rows
                FROM current_window
            ),
            product_curr_cost AS (
                SELECT product_key, MAX(product_id) AS product_id, MAX(product_label) AS label, SUM(revenue) AS revenue, SUM(cost) AS cost
                FROM current_window
                WHERE cost_raw IS NOT NULL
                GROUP BY 1
            ),
            product_prev_cost AS (
                SELECT product_key, SUM(revenue) AS revenue, SUM(cost) AS cost
                FROM prev_window
                WHERE cost_raw IS NOT NULL
                GROUP BY 1
            ),
            product_curr_margin AS (
                SELECT *,
                    CASE WHEN revenue > 0 THEN (revenue - cost) / revenue * 100 ELSE NULL END AS margin_pct
                FROM product_curr_cost
            ),
            product_prev_margin AS (
                SELECT product_key,
                    CASE WHEN revenue > 0 THEN (revenue - cost) / revenue * 100 ELSE NULL END AS margin_pct
                FROM product_prev_cost
            ),
            margin_risk AS (
                SELECT *
                FROM product_curr_margin
                WHERE revenue >= {min_revenue} AND margin_pct < {target_margin}
            ),
            margin_risk_count AS (
                SELECT COUNT(*) AS risk_count FROM margin_risk
            ),
            margin_risk_top AS (
                SELECT * FROM margin_risk ORDER BY revenue DESC LIMIT 5
            ),
            margin_ranked AS (
                SELECT
                    margin_pct,
                    revenue,
                    SUM(revenue) OVER () AS total_rev,
                    SUM(revenue) OVER (ORDER BY margin_pct ROWS UNBOUNDED PRECEDING) AS cum_rev
                FROM product_curr_margin
                WHERE revenue > 0 AND margin_pct IS NOT NULL
            ),
            margin_stats AS (
                SELECT
                    MIN(CASE WHEN total_rev > 0 AND cum_rev / total_rev >= 0.10 THEN margin_pct END) AS p10,
                    MIN(CASE WHEN total_rev > 0 AND cum_rev / total_rev >= 0.50 THEN margin_pct END) AS p50,
                    MIN(CASE WHEN total_rev > 0 AND cum_rev / total_rev >= 0.90 THEN margin_pct END) AS p90,
                    SUM(CASE WHEN margin_pct < 0 THEN 1 ELSE 0 END) AS below_zero,
                    SUM(CASE WHEN margin_pct > 50 THEN 1 ELSE 0 END) AS above_fifty
                FROM margin_ranked
            ),
            negative_margin AS (
                SELECT product_id, label, revenue, margin_pct
                FROM product_curr_margin
                WHERE margin_pct < 0
                ORDER BY revenue DESC
                LIMIT 5
            ),
            margin_drop AS (
                SELECT
                    c.product_id,
                    c.label,
                    c.revenue AS revenue_curr,
                    c.margin_pct AS margin_curr,
                    p.margin_pct AS margin_prev,
                    c.margin_pct - p.margin_pct AS margin_delta
                FROM product_curr_margin c
                JOIN product_prev_margin p
                    ON c.product_key = p.product_key
                WHERE c.revenue >= {min_revenue} AND p.margin_pct IS NOT NULL
                ORDER BY margin_delta ASC
                LIMIT 5
            ),
            customer_rev AS (
                SELECT customer_key, SUM(revenue) AS revenue
                FROM current_window
                GROUP BY 1
            ),
            customer_rank AS (
                SELECT revenue, ROW_NUMBER() OVER (ORDER BY revenue DESC) AS rn, SUM(revenue) OVER () AS total_rev
                FROM customer_rev
            ),
            customer_conc AS (
                SELECT
                    CASE WHEN total_rev > 0 THEN MAX(revenue / total_rev) OVER () * 100 ELSE NULL END AS top1_share,
                    CASE WHEN total_rev > 0 THEN SUM(CASE WHEN rn <= 5 THEN revenue / total_rev ELSE 0 END) OVER () * 100 ELSE NULL END AS top5_share,
                    CASE WHEN total_rev > 0 THEN SUM((revenue / total_rev) * (revenue / total_rev)) OVER () * 10000 ELSE NULL END AS hhi
                FROM customer_rank
                LIMIT 1
            ),
            product_rev AS (
                SELECT product_key, SUM(revenue) AS revenue
                FROM current_window
                GROUP BY 1
            ),
            product_rank AS (
                SELECT revenue, ROW_NUMBER() OVER (ORDER BY revenue DESC) AS rn, SUM(revenue) OVER () AS total_rev
                FROM product_rev
            ),
            product_conc AS (
                SELECT
                    CASE WHEN total_rev > 0 THEN MAX(revenue / total_rev) OVER () * 100 ELSE NULL END AS top1_share,
                    CASE WHEN total_rev > 0 THEN SUM(CASE WHEN rn <= 5 THEN revenue / total_rev ELSE 0 END) OVER () * 100 ELSE NULL END AS top5_share,
                    CASE WHEN total_rev > 0 THEN SUM((revenue / total_rev) * (revenue / total_rev)) OVER () * 10000 ELSE NULL END AS hhi
                FROM product_rank
                LIMIT 1
            ),
            sku_curr AS (
                SELECT product_key, SUM(revenue) AS revenue, SUM(cost) AS cost, SUM(qty_basis) AS qty
                FROM current_window
                GROUP BY 1
            ),
            sku_prev AS (
                SELECT product_key, SUM(revenue) AS revenue, SUM(cost) AS cost, SUM(qty_basis) AS qty
                FROM prev_window
                GROUP BY 1
            ),
            sku_yoy AS (
                SELECT product_key, SUM(revenue) AS revenue, SUM(cost) AS cost, SUM(qty_basis) AS qty
                FROM yoy_window
                GROUP BY 1
            ),
            rev_decomp_mom AS (
                SELECT
                    SUM((p1 - p0) * q0) AS price_effect,
                    SUM(p0 * (q1 - q0)) AS volume_effect,
                    SUM((p1 - p0) * (q1 - q0)) AS mix_effect,
                    SUM(rev1 - rev0) AS total_effect
                FROM (
                    SELECT
                        COALESCE(c.revenue, 0) AS rev1,
                        COALESCE(p.revenue, 0) AS rev0,
                        COALESCE(c.qty, 0) AS q1,
                        COALESCE(p.qty, 0) AS q0,
                        CASE WHEN COALESCE(c.qty, 0) > 0 THEN COALESCE(c.revenue, 0) / COALESCE(c.qty, 0) ELSE 0 END AS p1,
                        CASE WHEN COALESCE(p.qty, 0) > 0 THEN COALESCE(p.revenue, 0) / COALESCE(p.qty, 0) ELSE 0 END AS p0
                    FROM sku_curr c
                    FULL OUTER JOIN sku_prev p
                        ON c.product_key = p.product_key
                )
            ),
            rev_decomp_yoy AS (
                SELECT
                    SUM((p1 - p0) * q0) AS price_effect,
                    SUM(p0 * (q1 - q0)) AS volume_effect,
                    SUM((p1 - p0) * (q1 - q0)) AS mix_effect,
                    SUM(rev1 - rev0) AS total_effect
                FROM (
                    SELECT
                        COALESCE(c.revenue, 0) AS rev1,
                        COALESCE(p.revenue, 0) AS rev0,
                        COALESCE(c.qty, 0) AS q1,
                        COALESCE(p.qty, 0) AS q0,
                        CASE WHEN COALESCE(c.qty, 0) > 0 THEN COALESCE(c.revenue, 0) / COALESCE(c.qty, 0) ELSE 0 END AS p1,
                        CASE WHEN COALESCE(p.qty, 0) > 0 THEN COALESCE(p.revenue, 0) / COALESCE(p.qty, 0) ELSE 0 END AS p0
                    FROM sku_curr c
                    FULL OUTER JOIN sku_yoy p
                        ON c.product_key = p.product_key
                )
            ),
            sku_curr_cost AS (
                SELECT product_key, SUM(revenue) AS revenue, SUM(cost) AS cost, SUM(qty_basis) AS qty
                FROM current_window
                WHERE cost_raw IS NOT NULL
                GROUP BY 1
            ),
            sku_prev_cost AS (
                SELECT product_key, SUM(revenue) AS revenue, SUM(cost) AS cost, SUM(qty_basis) AS qty
                FROM prev_window
                WHERE cost_raw IS NOT NULL
                GROUP BY 1
            ),
            sku_yoy_cost AS (
                SELECT product_key, SUM(revenue) AS revenue, SUM(cost) AS cost, SUM(qty_basis) AS qty
                FROM yoy_window
                WHERE cost_raw IS NOT NULL
                GROUP BY 1
            ),
            profit_decomp_mom AS (
                SELECT
                    SUM((u1 - u0) * q0) AS price_effect,
                    SUM(u0 * (q1 - q0)) AS volume_effect,
                    SUM((u1 - u0) * (q1 - q0)) AS mix_effect,
                    SUM((rev1 - cost1) - (rev0 - cost0)) AS total_effect
                FROM (
                    SELECT
                        COALESCE(c.revenue, 0) AS rev1,
                        COALESCE(c.cost, 0) AS cost1,
                        COALESCE(c.qty, 0) AS q1,
                        COALESCE(p.revenue, 0) AS rev0,
                        COALESCE(p.cost, 0) AS cost0,
                        COALESCE(p.qty, 0) AS q0,
                        CASE WHEN COALESCE(c.qty, 0) > 0 THEN (COALESCE(c.revenue, 0) - COALESCE(c.cost, 0)) / COALESCE(c.qty, 0) ELSE 0 END AS u1,
                        CASE WHEN COALESCE(p.qty, 0) > 0 THEN (COALESCE(p.revenue, 0) - COALESCE(p.cost, 0)) / COALESCE(p.qty, 0) ELSE 0 END AS u0
                    FROM sku_curr_cost c
                    FULL OUTER JOIN sku_prev_cost p
                        ON c.product_key = p.product_key
                )
            ),
            profit_decomp_yoy AS (
                SELECT
                    SUM((u1 - u0) * q0) AS price_effect,
                    SUM(u0 * (q1 - q0)) AS volume_effect,
                    SUM((u1 - u0) * (q1 - q0)) AS mix_effect,
                    SUM((rev1 - cost1) - (rev0 - cost0)) AS total_effect
                FROM (
                    SELECT
                        COALESCE(c.revenue, 0) AS rev1,
                        COALESCE(c.cost, 0) AS cost1,
                        COALESCE(c.qty, 0) AS q1,
                        COALESCE(p.revenue, 0) AS rev0,
                        COALESCE(p.cost, 0) AS cost0,
                        COALESCE(p.qty, 0) AS q0,
                        CASE WHEN COALESCE(c.qty, 0) > 0 THEN (COALESCE(c.revenue, 0) - COALESCE(c.cost, 0)) / COALESCE(c.qty, 0) ELSE 0 END AS u1,
                        CASE WHEN COALESCE(p.qty, 0) > 0 THEN (COALESCE(p.revenue, 0) - COALESCE(p.cost, 0)) / COALESCE(p.qty, 0) ELSE 0 END AS u0
                    FROM sku_curr_cost c
                    FULL OUTER JOIN sku_yoy_cost p
                        ON c.product_key = p.product_key
                )
            )
            SELECT
                (SELECT revenue FROM current_totals) AS revenue_curr,
                (SELECT revenue FROM prev_totals) AS revenue_prev,
                (SELECT revenue FROM yoy_totals) AS revenue_yoy,
                (SELECT rows FROM current_totals) AS rows_curr,
                (SELECT rows FROM prev_totals) AS rows_prev,
                (SELECT rows FROM yoy_totals) AS rows_yoy,
                (SELECT struct_pack(customer_id:=customer_id, label:=label, delta:=delta, current:=revenue_curr, previous:=revenue_prev) FROM top_customer_gainer) AS top_customer_gainer,
                (SELECT struct_pack(active_customers:=active_customers, new_customers:=new_customers, new_revenue:=new_revenue) FROM new_customer_stats) AS new_customer_stats,
                (SELECT struct_pack(rows:=rows, cost_rows:=cost_rows) FROM cost_coverage) AS cost_coverage,
                (SELECT struct_pack(hhi:=hhi, top1_share:=top1_share, top5_share:=top5_share) FROM customer_conc) AS conc_customer,
                (SELECT struct_pack(hhi:=hhi, top1_share:=top1_share, top5_share:=top5_share) FROM product_conc) AS conc_product,
                (SELECT struct_pack(price_effect:=price_effect, volume_effect:=volume_effect, mix_effect:=mix_effect, total:=total_effect) FROM rev_decomp_mom) AS rev_decomp_mom,
                (SELECT struct_pack(price_effect:=price_effect, volume_effect:=volume_effect, mix_effect:=mix_effect, total:=total_effect) FROM rev_decomp_yoy) AS rev_decomp_yoy,
                (SELECT struct_pack(price_effect:=price_effect, volume_effect:=volume_effect, mix_effect:=mix_effect, total:=total_effect) FROM profit_decomp_mom) AS profit_decomp_mom,
                (SELECT struct_pack(price_effect:=price_effect, volume_effect:=volume_effect, mix_effect:=mix_effect, total:=total_effect) FROM profit_decomp_yoy) AS profit_decomp_yoy,
                (SELECT risk_count FROM margin_risk_count) AS margin_risk_count,
                (SELECT list(struct_pack(product_id:=product_id, label:=label, revenue:=revenue, margin_pct:=margin_pct, gap_to_target:=({target_margin} - margin_pct))) FROM margin_risk_top) AS margin_risk_list,
                (SELECT struct_pack(p10:=p10, p50:=p50, p90:=p90, below_zero:=below_zero, above_fifty:=above_fifty) FROM margin_stats) AS margin_stats,
                (SELECT list(struct_pack(product_id:=product_id, label:=label, revenue:=revenue, margin_pct:=margin_pct)) FROM negative_margin) AS margin_negative,
                (SELECT list(struct_pack(product_id:=product_id, label:=label, revenue:=revenue_curr, margin_pct:=margin_curr, margin_delta:=margin_delta)) FROM margin_drop) AS margin_drop
            """
        )
        params = list(where_params)
        params.extend(
            [
                periods["curr_start"],
                periods["curr_end"],
                periods["prev_start"],
                periods["prev_end"],
                periods["yoy_start"],
                periods["yoy_end"],
                periods["curr_start"],
                periods["curr_end"],
            ]
        )

        df = ov2._exec_df(sql, params, "overview_insights", conn)
        row = df.iloc[0].to_dict() if not df.empty else {}

        rev_curr = _clean_float(row.get("revenue_curr"))
        rev_prev = _clean_float(row.get("revenue_prev"))
        rev_yoy = _clean_float(row.get("revenue_yoy"))
        rows_curr = int(row.get("rows_curr") or 0)
        rows_prev = int(row.get("rows_prev") or 0)
        rows_yoy = int(row.get("rows_yoy") or 0)

        cost_cov = _struct(row.get("cost_coverage"))
        cost_rows = int(cost_cov.get("cost_rows") or 0)
        total_rows = int(cost_cov.get("rows") or 0)
        cost_coverage_pct = round(cost_rows / total_rows * 100, 2) if total_rows else None
        cost_available = bool(cost_col)

        top_customer = _struct(row.get("top_customer_gainer"))
        top_customer_delta = _clean_float(top_customer.get("delta"))

        margin_risk_count = int(row.get("margin_risk_count") or 0)
        margin_risk_list = ov2._struct_list(row.get("margin_risk_list"))

        conc_customer = _struct(row.get("conc_customer"))
        conc_product = _struct(row.get("conc_product"))

        new_customer_stats = _struct(row.get("new_customer_stats"))
        active_customers = int(new_customer_stats.get("active_customers") or 0)
        new_customers = int(new_customer_stats.get("new_customers") or 0)
        new_revenue = _clean_float(new_customer_stats.get("new_revenue")) or 0.0

        rev_mom_pct = None
        rev_mom_detail = None
        if rev_prev is not None and rev_prev != 0:
            rev_mom_pct = (rev_curr or 0.0 - rev_prev) / rev_prev * 100
            rev_mom_detail = f"{_fmt_currency(rev_curr)} vs {_fmt_currency(rev_prev)} (Δ {_fmt_currency((rev_curr or 0.0) - rev_prev)})"
        else:
            rev_mom_detail = "Not enough history to compare."

        new_share = (new_customers / active_customers * 100) if active_customers else None
        rev_share = (new_revenue / rev_curr * 100) if rev_curr else None
        new_detail_parts = []
        if active_customers:
            new_detail_parts.append(f"{new_customers} new of {active_customers}")
        if rev_share is not None:
            new_detail_parts.append(f"Revenue share {rev_share:.1f}%")
        new_detail = " - ".join(new_detail_parts) if new_detail_parts else "No active customers."

        insights_callouts: List[Dict[str, Any]] = []

        insights_callouts.append(
            {
                "title": "Revenue MoM",
                "value": rev_mom_pct,
                "value_fmt": "percent",
                "detail": rev_mom_detail,
                "severity": "positive" if rev_mom_pct and rev_mom_pct > 0 else "negative" if rev_mom_pct and rev_mom_pct < 0 else "neutral",
                "tooltip": "MoM compares the current period to the immediately preceding period of equal length.",
            }
        )

        if top_customer and top_customer_delta is not None:
            insights_callouts.append(
                {
                    "title": "Top Customer Gainer",
                    "value": top_customer_delta,
                    "value_fmt": "currency",
                    "detail": top_customer.get("label"),
                    "severity": "positive" if top_customer_delta > 0 else "neutral",
                    "link": {"kind": "customer", "id": top_customer.get("customer_id") or top_customer.get("label")},
                    "tooltip": "Largest positive customer revenue delta vs the previous period.",
                }
            )
        else:
            insights_callouts.append(
                {
                    "title": "Top Customer Gainer",
                    "value": None,
                    "value_fmt": "currency",
                    "detail": "Not enough history.",
                    "severity": "neutral",
                    "tooltip": "Largest positive customer revenue delta vs the previous period.",
                }
            )

        risk_detail = f"Target {target_margin:.0f}% on ${min_revenue:,.0f}+ SKUs"
        if cost_coverage_pct is not None and cost_coverage_pct < 90:
            risk_detail = f"{risk_detail} - Cost coverage {cost_coverage_pct:.0f}%"
        insights_callouts.append(
            {
                "title": "Margin Risk Items",
                "value": margin_risk_count,
                "value_fmt": "number",
                "detail": risk_detail,
                "severity": "warning" if margin_risk_count > 0 else "info",
                "tooltip": "Products with revenue above the minimum and margin below the target threshold.",
            }
        )

        if conc_customer:
            top_share = _clean_float(conc_customer.get("top1_share"))
            hhi_val = _clean_float(conc_customer.get("hhi"))
            insights_callouts.append(
                {
                    "title": "Top Customer Share",
                    "value": top_share,
                    "value_fmt": "percent",
                    "detail": f"HHI {int(hhi_val or 0)}",
                    "severity": "warning" if top_share and top_share >= 20 else "info",
                    "tooltip": "Share of revenue from the single largest customer; HHI measures concentration (0-10,000).",
                }
            )

        insights_callouts.append(
            {
                "title": "New Customer Share",
                "value": new_share,
                "value_fmt": "percent",
                "detail": new_detail,
                "severity": "info",
                "tooltip": "Customers whose first-ever order falls inside the current period.",
            }
        )

        def _driver_struct(raw: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "price_effect": _clean_float(raw.get("price_effect")),
                "volume_effect": _clean_float(raw.get("volume_effect")),
                "mix_effect": _clean_float(raw.get("mix_effect")),
                "delta": _clean_float(raw.get("total")),
            }

        drivers_mom = {"revenue": _driver_struct(_struct(row.get("rev_decomp_mom"))), "profit": _driver_struct(_struct(row.get("profit_decomp_mom")))}
        drivers_yoy = {"revenue": _driver_struct(_struct(row.get("rev_decomp_yoy"))), "profit": _driver_struct(_struct(row.get("profit_decomp_yoy")))}

        if rows_curr == 0 or rows_prev == 0:
            drivers_mom["message"] = "Not enough history to compute MoM drivers."
        if rows_curr == 0 or rows_yoy == 0:
            drivers_yoy["message"] = "Not enough history to compute YoY drivers."

        drivers_payload = {
            "mom": drivers_mom,
            "yoy": drivers_yoy,
            "coverage": {"cost_pct": cost_coverage_pct, "cost_available": cost_available},
        }

        profitability_payload = {
            "margin_pct": _struct(row.get("margin_stats")),
            "margin_risk": [],
            "coverage": {"cost_pct": cost_coverage_pct, "cost_available": cost_available},
        }

        margin_negative = ov2._struct_list(row.get("margin_negative"))
        margin_drop = ov2._struct_list(row.get("margin_drop"))
        margin_risk_list = ov2._struct_list(row.get("margin_risk_list"))
        margin_items: List[Dict[str, Any]] = []
        for item in margin_negative:
            margin_items.append(
                {
                    "entity_id": item.get("product_id") or item.get("label"),
                    "label": item.get("label"),
                    "risk": "negative_margin",
                    "margin_pct": _clean_float(item.get("margin_pct")),
                    "revenue": _clean_float(item.get("revenue")),
                }
            )
        for item in margin_drop:
            margin_items.append(
                {
                    "entity_id": item.get("product_id") or item.get("label"),
                    "label": item.get("label"),
                    "risk": "margin_drop",
                    "margin_pct": _clean_float(item.get("margin_pct")),
                    "margin_delta": _clean_float(item.get("margin_delta")),
                    "revenue": _clean_float(item.get("revenue")),
                }
            )
        for item in margin_risk_list:
            margin_items.append(
                {
                    "entity_id": item.get("product_id") or item.get("label"),
                    "label": item.get("label"),
                    "risk": "below_target",
                    "margin_pct": _clean_float(item.get("margin_pct")),
                    "gap_to_target": _clean_float(item.get("gap_to_target")),
                    "revenue": _clean_float(item.get("revenue")),
                }
            )
        profitability_payload["margin_risk"] = margin_items

        if not cost_available or (cost_coverage_pct is not None and cost_coverage_pct < 25):
            profitability_payload["message"] = "Insufficient cost coverage to compute profitability metrics."

        payload = {
            "insights": {"callouts": insights_callouts},
            "drivers": drivers_payload,
            "concentration": {"customer": conc_customer, "product": conc_product},
            "profitability": profitability_payload,
            "meta": {
                "window": periods,
                "revenue_current": rev_curr,
                "revenue_prev": rev_prev,
                "revenue_yoy": rev_yoy,
                "dataset_version": dataset_marker,
                "cache_hit": False,
                "generated_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        }

        cache.set(cache_key, payload, timeout=INSIGHTS_TTL_SECONDS)
        return payload

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import replace
from typing import Any, Dict, List, Mapping, Optional, Tuple

from flask import g, current_app, request, session
from flask_login import current_user

from app.services import fact_store
from app.services import filters_service
from app.services.filters import bind_filter_cache_key, resolve_filters
from app.services.bundle_cache import cached_bundle
from app.services import fact_schema as fs
from app.services.bundle_builder import payload_size, to_json_safe, validate_bundle
from app.services import customers_bundle
from app.services import products_bundle
from app.services import regions_bundle
from app.services import suppliers_bundle
from app.services import salesreps_bundle
from app.services import stakeholder_report_bundle
from app.core import access_policy


def _pagination(args: Any, default_size: int = 25, max_size: int = 200) -> Tuple[int, int]:
    try:
        page = max(1, int(args.get("page", 1)))
    except Exception:
        page = 1
    try:
        size = int(args.get("page_size") or args.get("per_page") or default_size)
    except Exception:
        size = default_size
    size = max(1, min(size, max_size))
    return page, size


def _grouping(page: str) -> Tuple[str, str]:
    mapping = {
        "products": ("ProductId", "ProductName"),
        "customers": ("CustomerId", "CustomerName"),
        "regions": ("RegionName", "RegionName"),
        "suppliers": ("SupplierId", "SupplierName"),
        "salesreps": ("SalesRepName", "SalesRepName"),
    }
    return mapping.get(page, ("ProductId", "ProductName"))


def _sorting(args: Any) -> Tuple[str, str]:
    sort_by_raw = (args.get("sort") or args.get("sort_by") or "revenue").strip().lower() if hasattr(args, "get") else "revenue"
    sort_dir_raw = (args.get("sort_dir") or args.get("direction") or "desc").strip().lower() if hasattr(args, "get") else "desc"

    allowed = {
        "revenue": "revenue",
        "qty": "qty",
        "quantity": "qty",
        "profit": "profit",
        "margin": "margin_pct",
        "margin_pct": "margin_pct",
        "label": "label",
        "name": "label",
    }
    sort_col = allowed.get(sort_by_raw, "revenue")
    sort_dir = "ASC" if sort_dir_raw in {"asc", "ascending", "up", "1"} else "DESC"
    return sort_col, sort_dir


def _safe_col(cols: set[str], *candidates: str) -> str | None:
    for cand in candidates:
        if cand in cols:
            return cand
    return None


def _kpi_and_trend_sql(where_sql: str, params: List[Any], date_col: str, revenue_col: str, qty_col: str, cost_col: str) -> Tuple[str, List[Any], str, List[Any]]:
    kpi_sql = f"""
        SELECT
            COALESCE(SUM({revenue_col}), 0) AS revenue,
            COALESCE(SUM({qty_col}), 0) AS qty,
            COALESCE(SUM({cost_col}), 0) AS cost,
            COUNT(*) AS rows
        FROM fact
        WHERE {where_sql}
    """
    trend_sql = f"""
        SELECT
            strftime('%Y-%m', {date_col}) AS month,
            COALESCE(SUM({revenue_col}), 0) AS revenue,
            COALESCE(SUM({qty_col}), 0) AS qty
        FROM fact
        WHERE {where_sql}
        GROUP BY 1
        ORDER BY 1
    """
    return kpi_sql, list(params), trend_sql, list(params)


def _table_sql(
    where_sql: str,
    params: List[Any],
    key_col: str,
    label_col: str,
    revenue_col: str,
    qty_col: str,
    cost_col: str,
    page: int,
    page_size: int,
    sort_col: str,
    sort_dir: str,
) -> Tuple[str, List[Any]]:
    offset = (page - 1) * page_size
    sort_expr = sort_col if sort_col in {"revenue", "qty", "profit", "margin_pct", "label"} else "revenue"
    direction = "ASC" if sort_dir == "ASC" else "DESC"
    sql = f"""
        SELECT *
        FROM (
            SELECT
                {key_col} AS key,
                {label_col} AS label,
                COALESCE(SUM({revenue_col}), 0) AS revenue,
                COALESCE(SUM({qty_col}), 0) AS qty,
                COALESCE(SUM({cost_col}), 0) AS cost,
                COALESCE(SUM({revenue_col}) - SUM({cost_col}), 0) AS profit,
                CASE
                    WHEN SUM({revenue_col}) = 0 THEN NULL
                    ELSE (SUM({revenue_col}) - SUM({cost_col})) / SUM({revenue_col}) * 100.0
                END AS margin_pct,
                COUNT(*) OVER() AS total_groups
            FROM fact
            WHERE {where_sql}
            GROUP BY 1,2
        ) t
        ORDER BY {sort_expr} {direction}
        LIMIT ?
        OFFSET ?
    """
    return sql, list(params) + [page_size, offset]


def _log_debug_counts(page: str, filters: Any, scope: Dict[str, Any]) -> None:
    try:
        debug_enabled = bool(current_app and (current_app.config.get("DEBUG") or request.args.get("_debug") == "1"))
    except Exception:
        debug_enabled = False
    if not debug_enabled:
        return
    try:
        cols = fact_store.list_columns()
        base_filters = replace(
            filters,
            regions=tuple(),
            methods=tuple(),
            customers=tuple(),
            suppliers=tuple(),
            products=tuple(),
            sales_reps=tuple(),
        )
        base_where, base_params, _, _ = fact_store.build_where_clause(base_filters, cols, scope, apply_default_window=True)
        full_where, full_params, _, _ = fact_store.build_where_clause(filters, cols, scope, apply_default_window=True)
        conn = fact_store.get_conn()
        base_count = conn.execute(f"SELECT COUNT(*) AS c FROM fact WHERE {base_where}", base_params).fetchone()[0]
        full_count = conn.execute(f"SELECT COUNT(*) AS c FROM fact WHERE {full_where}", full_params).fetchone()[0]
        current_app.logger.info(
            f"{page}.bundle.debug_counts",
            extra={
                "base_count": int(base_count or 0),
                "full_count": int(full_count or 0),
                "scope": scope,
                "base_where": base_where,
                "full_where": full_where,
            },
        )
    except Exception:
        try:
            current_app.logger.debug("bundle.debug_counts.failed", exc_info=True)
        except Exception:
            pass


_DRILLDOWN_CONFIG: Dict[str, Dict[str, Any]] = {
    "products": {
        "key_col": fs.CANON.product_id,
        "label_col": fs.CANON.product_name,
        "id_params": ("product_id", "sku", "id"),
        "related": (fs.CANON.customer_id, fs.CANON.customer_name),
        "page_id": "product_drilldown",
    },
    "customers": {
        "key_col": fs.CANON.customer_id,
        "label_col": fs.CANON.customer_name,
        "id_params": ("customer_id", "id"),
        "related": (fs.CANON.product_id, fs.CANON.product_name),
        "page_id": "customer_drilldown",
    },
    "suppliers": {
        "key_col": fs.CANON.supplier_id,
        "label_col": fs.CANON.supplier_name,
        "id_params": ("supplier_id", "id"),
        "related": (fs.CANON.product_id, fs.CANON.product_name),
        "page_id": "supplier_drilldown",
    },
    "regions": {
        "key_col": fs.CANON.region,
        "label_col": fs.CANON.region,
        "id_params": ("region_id", "region", "id"),
        "related": (fs.CANON.customer_id, fs.CANON.customer_name),
        "page_id": "region_drilldown",
    },
    "salesreps": {
        "key_col": "SalesRepId",
        "label_col": fs.CANON.sales_rep,
        "id_params": ("salesrep_id", "sales_rep_id", "rep_id", "sales_rep", "id"),
        "related": (fs.CANON.customer_id, fs.CANON.customer_name),
        "page_id": "salesrep_drilldown",
    },
}


def _entity_param(entity: str, args: Any) -> Optional[str]:
    cfg = _DRILLDOWN_CONFIG.get(entity) or {}
    params = cfg.get("id_params") or ()
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    for key in params:
        val = getter(key)
        if val:
            return str(val)
    return None


def _canonical_param_key(name: Any) -> str:
    raw = str(name or "").strip().lower()
    if raw.endswith("[]"):
        raw = raw[:-2]
    return raw.replace("-", "_")


def _requested_sections(args: Any) -> Optional[Tuple[str, ...]]:
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    getlist = args.getlist if hasattr(args, "getlist") else None
    raw_values: List[Any] = []
    for key in ("_sections", "sections"):
        if getlist is not None:
            try:
                raw_values.extend(getlist(key))
            except Exception:
                pass
        value = getter(key)
        if value not in (None, ""):
            raw_values.append(value)

    if not raw_values:
        return None

    aliases = {
        "all": "all",
        "full": "all",
        "overview": "overview",
        "summary": "overview",
        "kpis": "overview",
        "strategy": "strategy",
        "demand": "demand",
        "pricing": "pricing",
        "execution": "execution",
        "assortment": "assortment",
        "table": "table",
        "clv": "clv",
        "rfm": "rfm",
        "cohort": "cohorts",
        "cohorts": "cohorts",
    }
    normalized: set[str] = set()
    for raw in raw_values:
        if raw is None:
            continue
        parts = raw if isinstance(raw, (list, tuple, set)) else str(raw).split(",")
        for part in parts:
            token = str(part or "").strip().lower().replace("-", "_")
            if not token:
                continue
            resolved = aliases.get(token)
            if resolved == "all":
                return None
            if resolved:
                normalized.add(resolved)

    return tuple(sorted(normalized)) or None


def _bundle_query_budget(page: str, meta: Mapping[str, Any]) -> Optional[int]:
    if page == "customers":
        sections = {
            str(section).strip().lower()
            for section in (meta.get("sections") or [])
            if str(section).strip()
        }
        if not sections:
            return 9
        if sections == {"overview"}:
            return 6
        if sections in ({"clv"}, {"rfm"}, {"cohorts"}):
            return 7
        return 9
    if page == "products":
        sections = {
            str(section).strip().lower()
            for section in (meta.get("sections") or [])
            if str(section).strip()
        }
        if not sections:
            return 3
        if sections == {"table"}:
            return 1
        if sections.issubset({"overview", "strategy", "demand"}):
            return 1
        if "table" in sections:
            return 2
        return 2
    if page == "salesreps":
        return 4
    if page in {"regions", "suppliers"}:
        return 3
    return None


def _drilldown_query_budget(entity: str) -> Optional[int]:
    if entity == "customers":
        return 12
    if entity == "regions":
        return 5
    if entity in {"products", "suppliers", "salesreps"}:
        return 3
    return None


def _drilldown_filter_source(entity: str, args: Any) -> Any:
    """
    Remove route/entity identifier params from the filter source before resolving
    global filters. This keeps sticky/global filters authoritative on drilldowns.
    """
    source = getattr(args, "args", None) if hasattr(args, "args") else args
    if source is None:
        return {}

    cfg = _DRILLDOWN_CONFIG.get(entity) or {}
    id_params = {_canonical_param_key(key) for key in (cfg.get("id_params") or ())}
    if not id_params:
        return source

    if hasattr(source, "copy") and hasattr(source, "keys"):
        try:
            cleaned = source.copy()
            for key in list(cleaned.keys()):
                if _canonical_param_key(key) in id_params:
                    try:
                        cleaned.poplist(key)
                    except Exception:
                        cleaned.pop(key, None)
            return cleaned
        except Exception:
            pass

    if isinstance(source, Mapping):
        try:
            return {
                key: value
                for key, value in source.items()
                if _canonical_param_key(key) not in id_params
            }
        except Exception:
            return {}

    return source


def _drilldown_grouping(entity: str, cols: set[str]) -> tuple[str, str]:
    cfg = _DRILLDOWN_CONFIG.get(entity) or {}
    related = cfg.get("related") or ()
    if len(related) == 2:
        key_col = _safe_col(cols, related[0], related[1], "key", "id")
        label_col = _safe_col(cols, related[1], related[0], "label", "name")
    else:
        key_col = _safe_col(cols, fs.CANON.customer_id, fs.CANON.customer_name, "key", "id")
        label_col = _safe_col(cols, fs.CANON.customer_name, fs.CANON.customer_id, "label", "name")
    key_col = key_col or (related[0] if related else None) or "key"
    label_col = label_col or (related[1] if related else related[0] if related else None) or "label"
    return key_col, label_col


def _build_drilldown_bundle(
    entity: str,
    entity_id: str,
    filters: Any,
    scope: Dict[str, Any],
    args: Any,
) -> Dict[str, Any]:
    cols = fact_store.list_columns()
    if not cols:
        return {"error": {"message": "Fact view unavailable"}, "meta": {"cached": False}}

    date_col = _safe_col(cols, fs.CANON.date, "Date")
    revenue_col = _safe_col(cols, fs.CANON.revenue, "Revenue")
    qty_col = _safe_col(cols, fs.CANON.qty_units, "QuantityShipped", "QuantityOrdered")
    cost_col = _safe_col(cols, fs.CANON.cost, "CostPrice", "Cost")
    order_col = _safe_col(cols, fs.CANON.order_id, "OrderId", "OrderID") or key_col

    cfg = _DRILLDOWN_CONFIG.get(entity) or {}
    key_col = _safe_col(cols, cfg.get("key_col"), "Id", "ID")
    label_col = _safe_col(cols, cfg.get("label_col"), "Name", "Label")
    if not key_col:
        key_col = _safe_col(cols, cfg.get("label_col"), fs.CANON.sales_rep, "SalesRepName")
    page_id = cfg.get("page_id") or f"{entity}_drilldown"

    if not (date_col and revenue_col and qty_col and cost_col and key_col):
        return {
            "error": {"message": "Required columns missing for drilldown"},
            "meta": {"cached": False, "page_id": page_id, "entity_id": entity_id},
        }

    base_where, base_params, start_iso, end_iso = fact_store.build_where_clause(
        filters, cols, scope, apply_default_window=True
    )
    where_sql = f"({base_where}) AND {key_col} = ?"
    params_with_entity = list(base_params) + [entity_id]

    page_num, page_size = _pagination(args)
    rel_key_col, rel_label_col = _drilldown_grouping(entity, cols)
    if (rel_key_col not in cols) or (rel_label_col not in cols):
        rel_key_col = key_col
        rel_label_col = label_col or key_col

    kpi_sql = f"""
        SELECT
            MIN({label_col}) AS label,
            COALESCE(SUM({revenue_col}), 0) AS revenue,
            COALESCE(SUM({qty_col}), 0) AS qty,
            COALESCE(SUM({cost_col}), 0) AS cost,
            COUNT(*) AS rows,
            COUNT(DISTINCT {order_col}) AS orders,
            COUNT(DISTINCT {rel_key_col}) AS related_groups
        FROM fact
        WHERE {where_sql}
    """
    trend_sql = f"""
        SELECT
            strftime('%Y-%m', {date_col}) AS month,
            COALESCE(SUM({revenue_col}), 0) AS revenue,
            COALESCE(SUM({qty_col}), 0) AS qty,
            COALESCE(SUM({revenue_col}) - SUM({cost_col}), 0) AS profit
        FROM fact
        WHERE {where_sql}
        GROUP BY 1
        ORDER BY 1
    """
    offset = (page_num - 1) * page_size
    table_sql = f"""
        SELECT *
        FROM (
            SELECT
                {rel_key_col} AS key,
                {rel_label_col} AS label,
                COALESCE(SUM({revenue_col}), 0) AS revenue,
                COALESCE(SUM({qty_col}), 0) AS qty,
                COALESCE(SUM({cost_col}), 0) AS cost,
                COALESCE(SUM({revenue_col}) - SUM({cost_col}), 0) AS profit,
                COUNT(DISTINCT {order_col}) AS orders,
                COUNT(*) OVER() AS total_groups
            FROM fact
            WHERE {where_sql}
            GROUP BY 1,2
        ) t
        ORDER BY revenue DESC
        LIMIT ?
        OFFSET ?
    """

    kpi_df = fact_store.execute_sql_df(kpi_sql, params_with_entity, tag=f"{entity}.drilldown.kpi")
    if kpi_df.empty or (kpi_df.iloc[0].get("rows", 0) in (0, None)):
        empty_payload = {
            "kpis": {
                "revenue": 0.0,
                "qty": 0.0,
                "cost": 0.0,
                "profit": 0.0,
                "margin_pct": None,
                "rows": 0,
                "orders": 0,
                "related": 0,
                "start": start_iso,
                "end": end_iso,
            },
            "trend": {"labels": [], "revenue": [], "qty": [], "profit": []},
            "charts": {"trend": {"labels": [], "revenue": [], "qty": [], "profit": []}},
            "table": {
                "rows": [],
                "page": page_num,
                "page_size": page_size,
                "total": 0,
                "sort_by": "revenue",
                "sort_dir": "desc",
            },
            "warnings": ["Not found"],
            "meta": {"cached": False, "page_id": page_id, "entity_id": entity_id, "entity_label": entity_id},
        }
        return empty_payload
    trend_df = fact_store.execute_sql_df(trend_sql, params_with_entity, tag=f"{entity}.drilldown.trend")
    table_df = fact_store.execute_sql_df(
        table_sql, params_with_entity + [page_size, offset], tag=f"{entity}.drilldown.table"
    )

    revenue = float(kpi_df.at[0, "revenue"]) if not kpi_df.empty else 0.0
    qty = float(kpi_df.at[0, "qty"]) if not kpi_df.empty else 0.0
    cost = float(kpi_df.at[0, "cost"]) if not kpi_df.empty else 0.0
    profit = revenue - cost
    margin_pct = (profit / revenue * 100.0) if revenue else None
    orders = int(kpi_df.at[0, "orders"]) if not kpi_df.empty else 0
    related_groups = int(kpi_df.at[0, "related_groups"]) if not kpi_df.empty else 0
    entity_label = kpi_df.at[0, "label"] if not kpi_df.empty else entity_id

    trend_labels = trend_df["month"].tolist() if "month" in trend_df else []
    trend_revenue = [float(x) if x is not None else 0.0 for x in trend_df.get("revenue", [])]
    trend_qty = [float(x) if x is not None else 0.0 for x in trend_df.get("qty", [])]
    trend_profit = [float(x) if x is not None else 0.0 for x in trend_df.get("profit", [])]

    rows = []
    total_groups = 0
    if not table_df.empty:
        total_groups = int(table_df.iloc[0].get("total_groups", len(table_df)))
        for _, row in table_df.iterrows():
            rev = float(row.get("revenue") or 0.0)
            ct = float(row.get("qty") or 0.0)
            cst = float(row.get("cost") or 0.0)
            prof = float(row.get("profit") or (rev - cst))
            mp_val = row.get("margin_pct", None)
            if mp_val is None:
                mp = (prof / rev * 100.0) if rev else None
            else:
                try:
                    mp_candidate = float(mp_val)
                    mp = None if math.isnan(mp_candidate) else mp_candidate
                except Exception:
                    mp = (prof / rev * 100.0) if rev else None
            rows.append(
                {
                    "key": row.get("key"),
                    "label": row.get("label"),
                    "revenue": rev,
                    "qty": ct,
                    "cost": cst,
                    "profit": prof,
                    "margin_pct": mp,
                    "orders": int(row.get("orders") or 0),
                }
            )

    payload: Dict[str, Any] = {
        "kpis": {
            "revenue": revenue,
            "qty": qty,
            "cost": cost,
            "profit": profit,
            "margin_pct": margin_pct,
            "rows": int(kpi_df.at[0, "rows"]) if not kpi_df.empty else 0,
            "orders": orders,
            "related": related_groups,
            "start": start_iso,
            "end": end_iso,
        },
        "trend": {"labels": trend_labels, "revenue": trend_revenue, "qty": trend_qty, "profit": trend_profit},
        "charts": {"trend": {"labels": trend_labels, "revenue": trend_revenue, "qty": trend_qty, "profit": trend_profit}},
        "table": {
            "rows": rows,
            "page": page_num,
            "page_size": page_size,
            "total": total_groups,
            "sort_by": "revenue",
            "sort_dir": "desc",
        },
        "meta": {"page_id": page_id, "entity_id": entity_id, "entity_label": entity_label},
    }
    return payload


def _build_bundle(page: str, filters: Any, scope: Dict[str, Any], args: Any) -> Dict[str, Any]:
    if page == "customers":
        # Specialized DuckDB-first builder for customers with pagination + tabs.
        return customers_bundle.build_customers_bundle(
            filters,
            scope,
            args,
            requested_sections=_requested_sections(args),
        )
    if page == "products":
        # Rich bundle for product intelligence
        return products_bundle.build_products_bundle(
            filters,
            scope,
            args,
            requested_sections=_requested_sections(args),
        )
    if page == "regions":
        return regions_bundle.build_regions_bundle(filters, scope, args)
    if page == "suppliers":
        return suppliers_bundle.build_suppliers_bundle(filters, scope, args)
    if page == "salesreps":
        return salesreps_bundle.build_salesreps_bundle(filters, scope, args)
    if page == "stakeholder_report":
        return stakeholder_report_bundle.build_bundle(filters, scope, args)
    started = time.perf_counter()
    cols = fact_store.list_columns()
    date_col = _safe_col(cols, fs.CANON.date, "Date")
    revenue_col = _safe_col(cols, fs.CANON.revenue, "Revenue")
    qty_col = _safe_col(cols, fs.CANON.qty_units, "QuantityShipped", "QuantityOrdered")
    cost_col = _safe_col(cols, fs.CANON.cost, "CostPrice", "Cost")

    if not (date_col and revenue_col and qty_col and cost_col):
        return {"error": {"message": "Required columns missing"}, "meta": {"cached": False}}

    page_num, page_size = _pagination(args)
    sort_col, sort_dir = _sorting(args)
    group_key, group_label = _grouping(page)

    where_sql, where_params, start_iso, end_iso = fact_store.build_where_clause(filters, cols, scope, apply_default_window=True)

    kpi_sql, kpi_params, trend_sql, trend_params = _kpi_and_trend_sql(where_sql, where_params, date_col, revenue_col, qty_col, cost_col)
    table_sql, table_params = _table_sql(
        where_sql,
        where_params,
        group_key,
        group_label,
        revenue_col,
        qty_col,
        cost_col,
        page_num,
        page_size,
        sort_col,
        sort_dir,
    )

    kpi_df = fact_store.execute_sql_df(kpi_sql, kpi_params, tag=f"{page}.kpis")
    trend_df = fact_store.execute_sql_df(trend_sql, trend_params, tag=f"{page}.trend")
    table_df = fact_store.execute_sql_df(table_sql, table_params, tag=f"{page}.table")

    revenue = float(kpi_df.at[0, "revenue"]) if not kpi_df.empty else 0.0
    qty = float(kpi_df.at[0, "qty"]) if not kpi_df.empty else 0.0
    cost = float(kpi_df.at[0, "cost"]) if not kpi_df.empty else 0.0
    profit = revenue - cost
    margin_pct = (profit / revenue * 100.0) if revenue else None

    trend_labels = trend_df["month"].tolist() if "month" in trend_df else []
    trend_revenue = [float(x) if x is not None else 0.0 for x in trend_df.get("revenue", [])]
    trend_qty = [float(x) if x is not None else 0.0 for x in trend_df.get("qty", [])]

    rows = []
    total_groups = 0
    if not table_df.empty:
        total_groups = int(table_df.iloc[0].get("total_groups", len(table_df)))
        for _, row in table_df.iterrows():
            rev = float(row.get("revenue") or 0.0)
            ct = float(row.get("qty") or 0.0)
            cst = float(row.get("cost") or 0.0)
            prof = float(row.get("profit") or (rev - cst))
            mp_val = row.get("margin_pct", None)
            if mp_val is None:
                mp = (prof / rev * 100.0) if rev else None
            else:
                try:
                    mp_candidate = float(mp_val)
                    mp = None if math.isnan(mp_candidate) else mp_candidate
                except Exception:
                    mp = (prof / rev * 100.0) if rev else None
            rows.append(
                {
                    "key": row.get("key"),
                    "label": row.get("label"),
                    "revenue": rev,
                    "qty": ct,
                    "cost": cst,
                    "profit": prof,
                    "margin_pct": mp,
                }
            )

    duration_ms = int((time.perf_counter() - started) * 1000)
    payload = {
        "kpis": {
            "revenue": revenue,
            "qty": qty,
            "cost": cost,
            "profit": profit,
            "margin_pct": margin_pct,
            "rows": int(kpi_df.at[0, "rows"]) if not kpi_df.empty else 0,
            "start": start_iso,
            "end": end_iso,
        },
        "trend": {"labels": trend_labels, "revenue": trend_revenue, "qty": trend_qty},
        "charts": {"trend": {"labels": trend_labels, "revenue": trend_revenue, "qty": trend_qty}},
        "table": {
            "rows": rows,
            "page": page_num,
            "page_size": page_size,
            "total": total_groups,
            "sort_by": sort_col,
            "sort_dir": sort_dir,
        },
        "meta": {"duration_ms": duration_ms},
    }
    payload.setdefault("warnings", [])
    return payload


def bundle(page: str, args: Any) -> Dict[str, Any]:
    source = getattr(args, "args", {}) or args or {}
    sticky_enabled = bool(current_app.config.get("STICKY_FILTERS", True))
    filters, _meta = resolve_filters(
        request,
        current_user,
        session_obj=session,
        source=source,
        sticky_enabled=sticky_enabled,
    )
    scope = filters_service.scope_from_user(current_user)
    dataset_version = fact_store.cache_buster()
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    extras = {
        "page": getter("page", 1),
        "page_size": getter("page_size", None),
        "sort": getter("sort") or getter("sort_by", None),
        "sort_dir": getter("dir") or getter("sort_dir", None),
        "search": getter("search") or getter("q", None),
        "quick_filters": getter("quick_filters") or getter("quick_filter", None),
        "topN": getter("topN") or getter("top_n") or getter("top", None),
        "segments": getter("segments") or getter("segment", None),
        "at_risk": getter("at_risk", None),
        "export_all": getter("export_all", None),
        "dataset": getter("dataset") or getter("export_type", None),
        "tab": getter("tab", None),
        "attribution_mode": getter("attribution_mode") or getter("mode", None),
        "roster_mode": getter("roster_mode") or getter("rep_roster") or getter("rep_status", None),
        "transfer_only": getter("transfer_only") or getter("transfers_only", None),
    }
    requested_sections = _requested_sections(args)
    if requested_sections:
        extras["sections"] = list(requested_sections)
    if page == "customers":
        rfm_params_payload = {
            "lookback_months": getter("rfm_lookback_months") or getter("lookback_months"),
            "scoring_method": getter("rfm_scoring_method") or getter("scoring_method"),
            "monetary_metric": getter("rfm_monetary_metric") or getter("monetary_metric"),
            "recency_thresholds": getter("rfm_recency_thresholds"),
            "frequency_thresholds": getter("rfm_frequency_thresholds"),
            "monetary_thresholds": getter("rfm_monetary_thresholds"),
            "rfm_search": getter("rfm_search"),
            "rfm_segments": getter("rfm_segments") or getter("segment"),
            "r_min": getter("r_min"),
            "r_max": getter("r_max"),
            "f_min": getter("f_min"),
            "f_max": getter("f_max"),
            "m_min": getter("m_min"),
            "m_max": getter("m_max"),
            "heat_r": getter("heat_r"),
            "heat_f": getter("heat_f"),
            "rfm_at_risk_only": getter("rfm_at_risk_only") or getter("at_risk_only"),
            "rfm_top_mode": getter("rfm_top_mode") or getter("top_mode"),
            "rfm_scatter_mode": getter("rfm_scatter_mode") or getter("scatter_mode"),
            "rfm_sort_by": getter("rfm_sort_by"),
            "rfm_sort_dir": getter("rfm_sort_dir"),
            "rfm_page": getter("rfm_page"),
            "rfm_page_size": getter("rfm_page_size"),
            "rfm_top_n": getter("rfm_top_n"),
        }
        rfm_params_hash = hashlib.sha256(
            json.dumps(rfm_params_payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()
        extras.update(
            {
                "rfm_params_hash": rfm_params_hash,
                "rfm_lookback_months": rfm_params_payload.get("lookback_months"),
                "rfm_scoring_method": rfm_params_payload.get("scoring_method"),
                "rfm_monetary_metric": rfm_params_payload.get("monetary_metric"),
                "heat_r": rfm_params_payload.get("heat_r"),
                "heat_f": rfm_params_payload.get("heat_f"),
            }
        )
        clv_params_payload = {
            "lookback_months": getter("clv_lookback_months") or getter("clv_lookback") or getter("lookback_months"),
            "horizon_months": getter("clv_horizon_months") or getter("clv_horizon") or getter("horizon_months"),
            "discount_rate": getter("clv_discount_rate") or getter("discount_rate"),
            "monetary_basis": getter("clv_monetary_basis") or getter("clv_monetary_metric") or getter("monetary_basis"),
            "retention_model": getter("clv_retention_model") or getter("retention_model"),
            "frequency_basis": getter("clv_frequency_basis") or getter("frequency_basis"),
            "clv_search": getter("clv_search"),
            "clv_segments": getter("clv_segments"),
            "clv_min_clv": getter("clv_min_clv"),
            "clv_high_risk_only": getter("clv_high_risk_only"),
            "clv_low_margin_only": getter("clv_low_margin_only"),
            "clv_top_mode": getter("clv_top_mode"),
            "clv_top_n": getter("clv_top_n"),
            "clv_sort_by": getter("clv_sort_by"),
            "clv_sort_dir": getter("clv_sort_dir"),
            "clv_scatter_mode": getter("clv_scatter_mode"),
            "clv_page": getter("clv_page"),
            "clv_page_size": getter("clv_page_size"),
            "clv_export_all": getter("clv_export_all"),
        }
        clv_params_hash = hashlib.sha256(
            json.dumps(clv_params_payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()
        extras.update(
            {
                "clv_params_hash": clv_params_hash,
                "clv_lookback_months": clv_params_payload.get("lookback_months"),
                "clv_horizon_months": clv_params_payload.get("horizon_months"),
                "clv_monetary_basis": clv_params_payload.get("monetary_basis"),
                "clv_top_mode": clv_params_payload.get("clv_top_mode"),
            }
        )
    if page == "products":
        _log_debug_counts(page, filters, scope)
    filters_json = filters_service.canonical_json(filters)
    filter_hash = hashlib.sha256(filters_json.encode("utf-8")).hexdigest()
    ttl_seconds = 3060 if page == "products" else None
    payload = cached_bundle(
        endpoint=f"{page}.bundle",
        filters=filters,
        scope=scope,
        dataset_version=dataset_version,
        extras=extras,
        ttl_seconds=ttl_seconds,
        builder=lambda: _build_bundle(page, filters, scope, args),
    )
    payload = validate_bundle(page, payload, drilldown=False)
    meta = payload.setdefault("meta", {})
    try:
        stats = getattr(g, "_duckdb_stats", None)
    except Exception:
        stats = None
    meta.setdefault("duckdb_query_count", int(stats.get("count", 0)) if stats else 0)
    meta.setdefault("duckdb_ms", int(stats.get("total_ms", 0)) if stats else 0)
    meta.setdefault("filter_hash", filter_hash)
    meta.setdefault("page_id", page)
    meta.setdefault(
        "scope",
        {
            "scope_mode": scope.get("scope_mode"),
            "allowed_count": scope.get("allowed_count"),
            "is_admin": scope.get("is_admin"),
        },
    )
    meta.setdefault("dataset_version", dataset_version)
    if ttl_seconds:
        meta.setdefault("cache_ttl", ttl_seconds)
    meta.setdefault("sort_by", extras.get("sort") or "revenue")
    meta.setdefault("sort_dir", (extras.get("sort_dir") or "desc").lower())
    try:
        table_meta = payload.get("table", {})
        if isinstance(table_meta, dict):
            meta.setdefault("page", int(table_meta.get("page", 1)))
            meta.setdefault("page_size", int(table_meta.get("page_size", extras.get("page_size") or 0)))
    except Exception:
        meta.setdefault("page", 1)
        meta.setdefault("page_size", extras.get("page_size"))
    meta["cache_hit"] = bool(meta.get("cached"))
    bind_filter_cache_key(meta.get("cache_key"))
    if scope.get("scope_mode") == "none" and not scope.get("is_admin"):
        meta.setdefault("no_access_configured", True)
        payload.setdefault("warnings", [])
        if isinstance(payload.get("warnings"), list):
            payload["warnings"].append("Access not configured")
    try:
        meta.setdefault("packs_coverage", fact_store.packs_coverage(filters, scope=scope, apply_default_window=True))
    except Exception:
        meta.setdefault("packs_coverage", {})
    try:
        safe_payload = to_json_safe(payload)
        meta.setdefault("payload_bytes", payload_size(safe_payload))
    except Exception:
        meta.setdefault("payload_bytes", None)
    try:
        budget = _bundle_query_budget(page, meta)
        if budget is not None:
            qcount = int(meta.get("duckdb_query_count") or 0)
            meta.setdefault("query_budget", budget)
            if qcount > budget:
                current_app.logger.warning(
                    f"{page}.bundle.query_budget_exceeded",
                    extra={
                        "query_count": qcount,
                        "query_budget": budget,
                        "page_id": page,
                        "filter_hash": meta.get("filter_hash"),
                        "sections": list(meta.get("sections") or []),
                    },
                )
                meta.setdefault("query_budget_exceeded", True)
    except Exception:
        pass
    return payload


def drilldown(entity: str, args: Any) -> Dict[str, Any]:
    source = _drilldown_filter_source(entity, args)
    sticky_enabled = bool(current_app.config.get("STICKY_FILTERS", True))
    filters, _meta = resolve_filters(
        request,
        current_user,
        session_obj=session,
        source=source,
        sticky_enabled=sticky_enabled,
    )
    scope = filters_service.scope_from_user(current_user)
    dataset_version = fact_store.cache_buster()
    entity_id = _entity_param(entity, args)
    if not entity_id:
        message = "sku required" if entity == "products" else "entity_id required"
        return {"error": {"message": message}, "meta": {"cached": False, "page_id": f"{entity}_drilldown"}}

    # Enforce entity access for non-admins
    access_policy.enforce_entity_access(entity, entity_id, access_policy.get_current_scope(use_cache=True))

    filters_json = filters_service.canonical_json(filters)
    filter_hash = hashlib.sha256((filters_json + str(entity_id)).encode("utf-8")).hexdigest()
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    extras = {
        "page": getter("page", 1),
        "page_size": getter("page_size", None),
        "entity_id": entity_id,
        "topN": getter("topN") or getter("top_n") or getter("top", None),
        "extras": getter("extras") or getter("include_extras"),
        "export_all": str(getter("export_all") or getter("drilldown_export_all") or "").strip().lower() in {"1", "true", "yes", "on"},
        "drilldown_v2": str(getter("drilldown_v2") or "").strip().lower() in {"1", "true", "yes", "on"},
        "attribution_mode": getter("attribution_mode") or getter("mode", None),
        "roster_mode": getter("roster_mode") or getter("rep_roster") or getter("rep_status", None),
        "transfer_only": getter("transfer_only") or getter("transfers_only", None),
    }

    def _builder() -> Dict[str, Any]:
        if entity == "customers":
            return customers_bundle.build_customers_drilldown(filters, scope, args)
        if entity == "products":
            # Reuse products bundle drilldown when available
            try:
                from app.services import products_bundle as pb  # local import to avoid cycle

                if hasattr(pb, "build_products_drilldown"):
                    return pb.build_products_drilldown(entity_id, filters, scope, args)
            except Exception:
                pass
        if entity == "regions":
            return regions_bundle.build_regions_drilldown(entity_id, filters, scope, args)
        if entity == "suppliers":
            return suppliers_bundle.build_suppliers_drilldown(entity_id, filters, scope, args)
        if entity == "salesreps":
            return salesreps_bundle.build_salesreps_drilldown(entity_id, filters, scope, args)
        return _build_drilldown_bundle(entity, entity_id, filters, scope, args)

    ttl_seconds = 60180 if entity == "products" else None
    payload = cached_bundle(
        endpoint=f"{entity}.drilldown.bundle",
        filters=filters,
        scope=scope,
        dataset_version=dataset_version,
        extras=extras,
        ttl_seconds=ttl_seconds,
        builder=_builder,
    )
    if scope.get("scope_mode") == "none" and not scope.get("is_admin"):
        # Avoid contract errors for users without configured access; return empty payload with warning.
        payload = payload if isinstance(payload, dict) else {}
        payload.pop("error", None)
        payload.setdefault("kpis", {})
        payload.setdefault("trend", {})
        payload.setdefault("table", {"rows": []})
        meta = payload.setdefault("meta", {})
        meta.setdefault("no_access_configured", True)
        payload.setdefault("warnings", [])
        if isinstance(payload.get("warnings"), list):
            payload["warnings"].append("Access not configured")
    payload = validate_bundle(entity, payload, drilldown=True)
    meta = payload.setdefault("meta", {})
    try:
        stats = getattr(g, "_duckdb_stats", None)
    except Exception:
        stats = None
    meta.setdefault("duckdb_query_count", int(stats.get("count", 0)) if stats else 0)
    meta.setdefault("duckdb_ms", int(stats.get("total_ms", 0)) if stats else 0)
    meta.setdefault("filter_hash", filter_hash)
    meta.setdefault("page_id", meta.get("page_id") or f"{entity}_drilldown")
    meta.setdefault(
        "scope",
        {
            "scope_mode": scope.get("scope_mode"),
            "allowed_count": scope.get("allowed_count"),
            "is_admin": scope.get("is_admin"),
        },
    )
    try:
        page_meta = max(1, int(extras.get("page") or 1))
    except Exception:
        page_meta = 1
    try:
        page_size_meta = max(1, int(extras.get("page_size") or 25))
    except Exception:
        page_size_meta = 25
    meta.setdefault("page", page_meta)
    meta.setdefault("page_size", page_size_meta)
    meta.setdefault("entity_id", entity_id)
    meta["cache_hit"] = bool(meta.get("cached"))
    bind_filter_cache_key(meta.get("cache_key"))
    try:
        meta.setdefault("packs_coverage", fact_store.packs_coverage(filters, scope=scope, apply_default_window=True))
    except Exception:
        meta.setdefault("packs_coverage", {})
    try:
        safe_payload = to_json_safe(payload)
        meta.setdefault("payload_bytes", payload_size(safe_payload))
    except Exception:
        meta.setdefault("payload_bytes", None)
    try:
        budget = _drilldown_query_budget(entity)
        if budget is not None:
            qcount = int(meta.get("duckdb_query_count") or 0)
            meta.setdefault("query_budget", budget)
            if qcount > budget:
                current_app.logger.warning(
                    f"{entity}.drilldown.query_budget_exceeded",
                    extra={
                        "query_count": qcount,
                        "query_budget": budget,
                        "entity_id": entity_id,
                        "filter_hash": meta.get("filter_hash"),
                    },
                )
                meta.setdefault("query_budget_exceeded", True)
    except Exception:
        pass
    return payload

from __future__ import annotations

import math
from dataclasses import replace
from statistics import pstdev
from typing import Any, Dict, List, Tuple

import pandas as pd

from app.services import fact_schema as fs
from app.services import fact_store
from app.services.filters import normalize_filters
from app.services import margin_rules

CHURN_THRESHOLD_DAYS = 90
TOP_N_DEFAULT = 25
AT_RISK_THRESHOLD_DAYS = 30
_DETAIL_MIN_DENOM = 500.0


def _safe_col(cols: set[str], *candidates: str) -> str | None:
    for cand in candidates:
        if cand and cand in cols:
            return cand
    return None


def _to_list(val: Any) -> list:
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, tuple):
        return list(val)
    try:
        import numpy as np  # type: ignore

        if isinstance(val, np.ndarray):
            return val.tolist()
    except Exception:
        pass
    return [val]


def _struct_list(val: Any) -> list[dict]:
    out: list[dict] = []
    for item in _to_list(val):
        if item is None:
            continue
        if isinstance(item, dict):
            out.append(item)
            continue
        try:
            out.append(dict(item))
            continue
        except Exception:
            pass
        try:
            out.append(item._asdict())  # type: ignore[attr-defined]
            continue
        except Exception:
            pass
        out.append({})
    return out


def _clean_float(val: Any, default: float = 0.0) -> float:
    try:
        fval = float(val)
        if math.isnan(fval):
            return default
        return fval
    except Exception:
        return default


def _clean_optional_float(val: Any) -> float | None:
    try:
        if val is None:
            return None
        fval = float(val)
        if math.isnan(fval):
            return None
        return fval
    except Exception:
        return None


def _clean_int(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except Exception:
        return default


def _parse_top_n(args: Any, default: int = TOP_N_DEFAULT) -> int:
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    try:
        raw = getter("topN") or getter("top_n") or getter("top") or default
        n = int(raw)
        if n < 0:
            return default
        return n
    except Exception:
        return default


def _quote(col: str) -> str:
    return fact_store.quote_identifier(col)


def _coalesce_numeric_expr(candidates: list[str], default: str) -> str:
    expressions: list[str] = []
    seen: set[str] = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        expressions.append(f"CAST({_quote(cand)} AS DOUBLE)")
    if not expressions:
        return default
    return f"COALESCE({', '.join(expressions + [default])})"


def _month_from_label(label: str) -> Tuple[int, int] | None:
    try:
        parts = label.split("-")
        if len(parts) != 2:
            return None
        return int(parts[0]), int(parts[1])
    except Exception:
        return None


def _calc_growth(labels: list[str], values: list[float]) -> Tuple[float | None, float | None]:
    if not labels or len(values) < 2:
        return None, None
    last = values[-1]
    prev = values[-2]
    if prev == 0:
        mom = None
    else:
        mom = (last - prev) / abs(prev) * 100.0
    lookup = {label: values[idx] for idx, label in enumerate(labels)}
    last_label = labels[-1]
    ym = _month_from_label(last_label)
    if not ym:
        return mom, None
    prev_label = f"{ym[0] - 1:04d}-{ym[1]:02d}"
    prev_year = lookup.get(prev_label)
    if prev_year in (None, 0):
        yoy = None
    else:
        yoy = (last - prev_year) / abs(prev_year) * 100.0
    return mom, yoy


def _calc_wow(weekly_values: list[float]) -> float | None:
    if len(weekly_values) < 2:
        return None
    last = weekly_values[-1]
    prev = weekly_values[-2]
    if prev == 0:
        return None
    return (last - prev) / abs(prev) * 100.0


def _comparison_windows(start_iso: str | None, end_iso: str | None) -> Dict[str, str | None]:
    if not start_iso or not end_iso:
        return {
            "current_start": start_iso,
            "current_end": end_iso,
            "prior_start": None,
            "prior_end": None,
            "yoy_start": None,
            "yoy_end": None,
        }
    try:
        current_start = pd.Timestamp(start_iso).normalize()
        current_end = pd.Timestamp(end_iso).normalize()
        if current_end <= current_start:
            current_end = current_start + pd.Timedelta(days=1)
        span = current_end - current_start
        prior_end = current_start
        prior_start = prior_end - span
        yoy_start = current_start - pd.DateOffset(years=1)
        yoy_end = current_end - pd.DateOffset(years=1)
        return {
            "current_start": current_start.date().isoformat(),
            "current_end": current_end.date().isoformat(),
            "prior_start": prior_start.date().isoformat(),
            "prior_end": prior_end.date().isoformat(),
            "yoy_start": yoy_start.date().isoformat(),
            "yoy_end": yoy_end.date().isoformat(),
        }
    except Exception:
        return {
            "current_start": start_iso,
            "current_end": end_iso,
            "prior_start": None,
            "prior_end": None,
            "yoy_start": None,
            "yoy_end": None,
        }


def _filters_with_window(filters: Any, start_iso: str, end_iso: str) -> Any:
    normalized = normalize_filters(filters or {})
    return replace(
        normalized,
        start=pd.to_datetime(start_iso, errors="coerce"),
        end=pd.to_datetime(end_iso, errors="coerce"),
        preset=None,
    )


def _pagination(args: Any, default_size: int = 25, max_size: int = 100000) -> Tuple[int, int]:
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    try:
        page = max(1, int(getter("page", 1)))
    except Exception:
        page = 1
    try:
        size = int(getter("page_size") or getter("per_page") or default_size)
    except Exception:
        size = default_size
    size = max(1, min(size, max_size))
    return page, size


def _sort_params(args: Any) -> Tuple[str, str]:
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    sort_by_raw = str(getter("sort") or getter("sort_by") or "revenue").strip().lower()
    sort_dir_raw = str(getter("sort_dir") or getter("direction") or "desc").strip().lower()
    sort_dir = "ASC" if sort_dir_raw in {"asc", "ascending", "up", "1"} else "DESC"
    mapping = {
        "region": "region",
        "name": "region",
        "customers": "customers",
        "customers_prior": "customers_prior",
        "orders": "orders",
        "orders_prior": "orders_prior",
        "revenue": "revenue",
        "revenue_prior": "revenue_prior",
        "revenue_delta": "delta_revenue",
        "revenue_delta_pct": "delta_revenue_pct",
        "aov": "aov",
        "repeat": "repeat_pct",
        "repeat_pct": "repeat_pct",
        "churn": "churn_pct",
        "churn_pct": "churn_pct",
        "new_customer_pct": "new_customer_pct",
        "new_customer_share_pct": "new_customer_pct",
        "returning_customer_pct": "returning_customer_pct",
        "top_customer_share_pct": "top_customer_share_pct",
        "top_product_share_pct": "top_product_share_pct",
        "profit": "profit",
        "profit_delta": "profit_delta",
        "margin": "margin_pct",
        "margin_pct": "margin_pct",
        "margin_delta_pp": "margin_delta_pp",
        "profit_per_order": "profit_per_order",
        "revenue_per_customer": "revenue_per_customer",
        "revenue_per_unit": "revenue_per_unit",
        "revenue_per_lb": "revenue_per_lb",
        "top_supplier_share_pct": "top_supplier_share_pct",
        "cost_coverage_pct": "cost_coverage_pct",
        "packs_coverage_pct": "packs_coverage_pct",
        "at_risk_customers": "at_risk_customers",
        "active_customers_30d": "active_customers_30d",
        "active_customers_90d": "active_customers_90d",
    }
    sort_by = mapping.get(sort_by_raw, "revenue")
    return sort_by, sort_dir


def _extract_search(args: Any) -> str:
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    return str(getter("search") or getter("q") or "").strip()


def _extract_quick_filter(args: Any) -> str:
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    return str(getter("quick_filter") or getter("quick") or "").strip().lower()


def _required_columns(cols: set[str]) -> Dict[str, Any]:
    date_col = _safe_col(cols, fs.CANON.date, "Date")
    region_col = _safe_col(cols, fs.CANON.region, "RegionName", "Region")
    revenue_col = _safe_col(cols, fs.CANON.revenue, "Revenue")
    cost_candidates = [cand for cand in (fs.CANON.cost, "Cost", "CostPrice") if cand in cols]
    qty_candidates = [
        cand
        for cand in (fs.CANON.qty_units, "ShippedItems", "QuantityOrdered", "Qty", "Quantity", "Units", "ItemCount", "pack_item_count_sum")
        if cand in cols
    ]
    order_col = _safe_col(cols, fs.CANON.order_id, "OrderId", "OrderID")
    customer_col = _safe_col(cols, fs.CANON.customer_id, "CustomerId", "CustomerID")
    customer_name_col = _safe_col(cols, fs.CANON.customer_name, "CustomerName", "Customer")
    product_col = _safe_col(cols, fs.CANON.product_id, "ProductId", "ProductID", "SKU")
    product_name_col = _safe_col(cols, fs.CANON.product_name, "ProductName", "Product", "Description")
    ship_method_col = _safe_col(cols, fs.CANON.ship_method, "ShippingMethodName", "ShippingMethodLabel", "ShipMethod_Name")
    supplier_col = _safe_col(cols, fs.CANON.supplier_id, "SupplierId", "SupplierID", "SupplierName")
    supplier_name_col = _safe_col(cols, fs.CANON.supplier_name, "SupplierName", "Supplier")
    weight_candidates = [cand for cand in (fs.CANON.weight_lb, "Weight", "WeightLb", "ShippedLb", "pack_weight_lb_sum") if cand in cols]
    protein_col = _safe_col(cols, "Protein", "ProteinType", "ProteinName", "Category", "ProductCategory")
    category_col = _safe_col(cols, "Category", "ProductCategory", "Protein", "ProteinType", "ProteinName")
    missing_packs_col = _safe_col(cols, "missing_packs")
    return {
        "date": date_col,
        "region": region_col,
        "revenue": revenue_col,
        "cost": cost_candidates[0] if cost_candidates else None,
        "cost_candidates": cost_candidates,
        "qty": qty_candidates[0] if qty_candidates else None,
        "qty_candidates": qty_candidates,
        "order": order_col,
        "customer": customer_col,
        "customer_name": customer_name_col,
        "product": product_col,
        "product_name": product_name_col,
        "ship_method": ship_method_col,
        "supplier": supplier_col,
        "supplier_name": supplier_name_col,
        "weight": weight_candidates[0] if weight_candidates else None,
        "weight_candidates": weight_candidates,
        "protein": protein_col,
        "category": category_col,
        "missing_packs": missing_packs_col,
    }


def _region_scoped_sql(cols_map: Dict[str, Any], where_sql: str) -> str:
    date_col = _quote(cols_map["date"])
    region_col = _quote(cols_map["region"])
    revenue_col = _quote(cols_map["revenue"])
    order_col = _quote(cols_map["order"])
    customer_col = _quote(cols_map["customer"])
    customer_name_col = _quote(cols_map["customer_name"]) if cols_map.get("customer_name") else None
    product_col = _quote(cols_map["product"]) if cols_map.get("product") else None
    product_name_col = _quote(cols_map["product_name"]) if cols_map.get("product_name") else None
    ship_col = _quote(cols_map["ship_method"]) if cols_map.get("ship_method") else None
    supplier_col = _quote(cols_map["supplier"]) if cols_map.get("supplier") else None
    supplier_name_col = _quote(cols_map["supplier_name"]) if cols_map.get("supplier_name") else None
    cost_candidates = list(cols_map.get("cost_candidates") or ([cols_map["cost"]] if cols_map.get("cost") else []))
    qty_candidates = list(cols_map.get("qty_candidates") or ([cols_map["qty"]] if cols_map.get("qty") else []))
    weight_candidates = list(cols_map.get("weight_candidates") or ([cols_map["weight"]] if cols_map.get("weight") else []))
    protein_col = _quote(cols_map["protein"]) if cols_map.get("protein") else None
    category_col = _quote(cols_map["category"]) if cols_map.get("category") else None
    missing_packs_col = _quote(cols_map["missing_packs"]) if cols_map.get("missing_packs") else None

    base_cost_expr = _coalesce_numeric_expr(cost_candidates, "NULL::DOUBLE")
    customer_name_expr = f"CAST({customer_name_col} AS VARCHAR)" if customer_name_col else "NULL::VARCHAR"
    product_expr = f"CAST({product_col} AS VARCHAR)" if product_col else "NULL::VARCHAR"
    product_name_expr = f"CAST({product_name_col} AS VARCHAR)" if product_name_col else "NULL::VARCHAR"
    ship_expr = f"CAST({ship_col} AS VARCHAR)" if ship_col else "NULL::VARCHAR"
    supplier_expr = f"CAST({supplier_col} AS VARCHAR)" if supplier_col else "NULL::VARCHAR"
    supplier_name_expr = f"CAST({supplier_name_col} AS VARCHAR)" if supplier_name_col else "NULL::VARCHAR"
    qty_expr = _coalesce_numeric_expr(qty_candidates, "0::DOUBLE")
    weight_expr = _coalesce_numeric_expr(weight_candidates, "0::DOUBLE")
    protein_expr = f"CAST({protein_col} AS VARCHAR)" if protein_col else "NULL::VARCHAR"
    category_expr = f"CAST({category_col} AS VARCHAR)" if category_col else "NULL::VARCHAR"
    missing_packs_expr = f"CAST({missing_packs_col} AS BOOLEAN)" if missing_packs_col else "NULL::BOOLEAN"
    protein_family_expr = f"COALESCE(NULLIF({protein_expr}, ''), NULLIF({category_expr}, ''), 'Unassigned')"
    product_category_expr = f"COALESCE(NULLIF({category_expr}, ''), NULLIF({protein_expr}, ''), 'Unassigned')"
    effective_cost_expr = margin_rules.sql_effective_cost_expr(base_cost_expr, weight_expr, qty_expr, fallback="NULL::DOUBLE")
    minimum_margin_expr = margin_rules.sql_margin_rule_expr(protein_family_expr, product_category_expr, "min_gross_margin_pct")
    target_margin_expr = margin_rules.sql_margin_rule_expr(protein_family_expr, product_category_expr, "target_gross_margin_pct")

    return f"""
        SELECT
            CAST({date_col} AS DATE) AS order_date,
            CAST({region_col} AS VARCHAR) AS region,
            CAST({order_col} AS VARCHAR) AS order_id,
            CAST({customer_col} AS VARCHAR) AS customer_id,
            {customer_name_expr} AS customer_name,
            {product_expr} AS product_id,
            {product_name_expr} AS product_name,
            {ship_expr} AS ship_method,
            {supplier_expr} AS supplier_id,
            {supplier_name_expr} AS supplier_name,
            {protein_family_expr} AS protein_family,
            {product_category_expr} AS product_category,
            CAST({revenue_col} AS DOUBLE) AS revenue,
            {base_cost_expr} AS base_cost,
            ({effective_cost_expr}) AS cost,
            {qty_expr} AS qty,
            {weight_expr} AS weight_lb,
            ({minimum_margin_expr}) AS minimum_margin_pct,
            ({target_margin_expr}) AS target_margin_pct,
            {missing_packs_expr} AS missing_packs
        FROM fact
        WHERE {where_sql}
    """


def _safe_pct(numerator: Any, denominator: Any) -> float | None:
    num = _clean_float(numerator, 0.0)
    den = _clean_float(denominator, 0.0)
    if abs(den) <= 1e-9:
        return None
    return (num / abs(den)) * 100.0


def _mover_delta_meta(current: float, previous: float) -> tuple[float | None, str, bool]:
    curr = float(current or 0.0)
    prev = float(previous or 0.0)
    if abs(prev) <= 1e-9:
        if curr > 0:
            return None, "New", False
        return 0.0, "0.0%", False
    pct = ((curr - prev) / abs(prev)) * 100.0
    if prev > 0 and abs(curr) <= 1e-9:
        return -100.0, "Lost", False
    if abs(prev) < _DETAIL_MIN_DENOM:
        return pct, "Low base", True
    return pct, f"{pct:.1f}%", False


def _delta_status(delta_pct: float | None, label: str) -> tuple[str, str]:
    if label == "New":
        return "new", "New"
    if label == "Lost":
        return "lost", "Lost"
    if label == "Low base":
        return "low_base", "Low base"
    if delta_pct is None or abs(delta_pct) < 2.5:
        return "stable", "Stable"
    return ("gainer", label) if delta_pct >= 0 else ("decliner", label)


def _freshness_meta() -> Dict[str, Any]:
    meta = fact_store.get_meta() or {}
    raw = meta.get("date_max") or meta.get("max_date") or meta.get("last_refresh_utc")
    out = {
        "date_max": None,
        "freshness_days": None,
        "status": "unknown",
        "label": "Freshness unavailable",
    }
    if raw is None:
        return out
    try:
        ts = pd.to_datetime(raw, utc=True, errors="coerce")
        if pd.isna(ts):
            return out
        today = pd.Timestamp.utcnow()
        if today.tzinfo is None:
            today = today.tz_localize("UTC")
        freshness_days = int((today.normalize() - ts.normalize()).days)
        out["date_max"] = ts.date().isoformat()
        out["freshness_days"] = freshness_days
        if freshness_days <= 1:
            out["status"] = "fresh"
            out["label"] = f"Fresh through {out['date_max']}"
        elif freshness_days <= 3:
            out["status"] = "watch"
            out["label"] = f"{freshness_days}d lag"
        else:
            out["status"] = "stale"
            out["label"] = f"{freshness_days}d lag"
    except Exception:
        return out
    return out


def _risk_profile(row: Dict[str, Any]) -> tuple[str, int, list[str]]:
    reasons: list[str] = []
    score = 0
    margin_pct = _clean_optional_float(row.get("margin_pct"))
    status_key = str(row.get("status_key") or "").strip().lower()
    if not status_key:
        status_key = str(
            margin_rules.classify_margin_status(
                margin_pct,
                row.get("minimum_margin_pct"),
                row.get("target_margin_pct"),
            ).get("status_key")
            or ""
        ).strip().lower()
    churn_pct = _clean_float(row.get("churn_pct"), 0.0)
    at_risk_pct = _clean_float(row.get("at_risk_pct"), 0.0)
    top_customer_share = _clean_float(row.get("top_customer_share_pct"), 0.0)
    top_product_share = _clean_float(row.get("top_product_share_pct"), 0.0)
    delta_revenue = _clean_float(row.get("delta_revenue"), 0.0)
    cost_coverage = _clean_optional_float(row.get("cost_coverage_pct"))
    packs_coverage = _clean_optional_float(row.get("packs_coverage_pct"))

    if top_customer_share >= 40.0 or top_product_share >= 30.0:
        score += 1
        reasons.append("Concentration")
    if status_key in {"red", "orange"}:
        score += 1
        reasons.append("Low margin")
    if churn_pct >= 15.0 or at_risk_pct >= 20.0:
        score += 1
        reasons.append("Retention risk")
    if delta_revenue < 0:
        score += 1
        reasons.append("Negative trend")
    if cost_coverage is not None and cost_coverage < 90.0:
        score += 1
        reasons.append("Cost coverage")
    if packs_coverage is not None and packs_coverage < 95.0:
        score += 1
        reasons.append("Packs coverage")

    if score >= 3:
        return "High", score, reasons
    if score >= 1:
        return "Medium", score, reasons
    return "Low", score, reasons


def _quality_flag(row: Dict[str, Any]) -> str:
    cost_coverage = _clean_optional_float(row.get("cost_coverage_pct"))
    packs_coverage = _clean_optional_float(row.get("packs_coverage_pct"))
    missing_cost_revenue = _clean_float(row.get("missing_cost_revenue"), 0.0)
    if (
        (cost_coverage is not None and cost_coverage < 80.0)
        or (packs_coverage is not None and packs_coverage < 80.0)
    ):
        return "Critical"
    if (
        (cost_coverage is not None and cost_coverage < 95.0)
        or (packs_coverage is not None and packs_coverage < 95.0)
        or missing_cost_revenue > 0
    ):
        return "Watch"
    return "OK"


def _sort_frame(frame: pd.DataFrame, sort_by: str, sort_dir: str) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    column = sort_by if sort_by in frame.columns else "revenue"
    ascending = str(sort_dir).upper() == "ASC"
    work = frame.copy()
    if column == "region":
        work["_sort_region"] = work["region"].fillna("").astype(str).str.lower()
        work = work.sort_values(["_sort_region", "revenue"], ascending=[ascending, False], na_position="last")
        return work.drop(columns=["_sort_region"])
    return work.sort_values([column, "region"], ascending=[ascending, True], na_position="last")


def _apply_quick_filter(frame: pd.DataFrame, quick_filter: str) -> pd.DataFrame:
    token = str(quick_filter or "").strip().lower()
    if not token or token in {"all", "none"}:
        return frame
    if frame.empty:
        return frame

    revenue_series = pd.to_numeric(frame.get("revenue"), errors="coerce").fillna(0.0)
    margin_series = pd.to_numeric(frame.get("margin_pct"), errors="coerce")
    median_revenue = float(revenue_series[revenue_series > 0].median()) if (revenue_series > 0).any() else 0.0
    margin_floor = float(margin_series.dropna().quantile(0.25)) if margin_series.notna().any() else 15.0
    margin_floor = min(margin_floor, 15.0)

    if token in {"high_revenue", "revenue"}:
        threshold = max(median_revenue * 1.25, float(revenue_series.quantile(0.75) or 0.0))
        return frame.loc[revenue_series >= threshold].copy()
    if token in {"high_risk", "risk"}:
        return frame.loc[frame["risk_band"].astype(str).str.lower() == "high"].copy()
    if token in {"high_concentration", "concentration"}:
        mask = (
            pd.to_numeric(frame.get("top_customer_share_pct"), errors="coerce").fillna(0.0) >= 35.0
        ) | (
            pd.to_numeric(frame.get("top_product_share_pct"), errors="coerce").fillna(0.0) >= 25.0
        )
        return frame.loc[mask].copy()
    if token in {"margin_weak", "low_margin"}:
        mask = pd.to_numeric(frame.get("margin_pct"), errors="coerce") < margin_floor
        return frame.loc[mask.fillna(False)].copy()
    if token in {"fast_growth", "growth"}:
        delta_pct = pd.to_numeric(frame.get("delta_revenue_pct"), errors="coerce")
        status = frame.get("delta_revenue_status", pd.Series(dtype="object")).astype(str).str.lower()
        mask = (pd.to_numeric(frame.get("delta_revenue"), errors="coerce").fillna(0.0) > 0) & (
            delta_pct.fillna(0.0) >= 10.0
        )
        mask = mask | status.isin({"new", "gainer"})
        return frame.loc[mask].copy()
    return frame


def _serialize_rows(frame: pd.DataFrame, columns: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if frame.empty:
        return rows
    for rec in frame.to_dict(orient="records"):
        row: dict[str, Any] = {}
        for col in columns:
            val = rec.get(col)
            if isinstance(val, float) and math.isnan(val):
                row[col] = None
            else:
                row[col] = val
        rows.append(row)
    return rows


def _build_empty_regions_payload(start_iso: str | None = None, end_iso: str | None = None) -> Dict[str, Any]:
    windows = _comparison_windows(start_iso, end_iso)
    return {
        "kpis": {
            "total_revenue": 0.0,
            "regions_count": 0,
            "avg_order_value": 0.0,
            "orders": 0,
            "customers": 0,
            "rows": 0,
            "rows_with_cost": 0,
            "cost_coverage_pct": None,
            "profit": None,
            "margin_pct": None,
            "yoy_growth": None,
            "mom_growth": None,
            "wow_growth": None,
            "revenue_delta_prior": 0.0,
            "revenue_delta_prior_pct": None,
            "prior_period_revenue": 0.0,
            "revenue_volatility": None,
            "revenue_volatility_pct": None,
            "concentration_top1_pct": None,
            "concentration_top5_pct": None,
            "revenue_hhi": None,
            "repeat_rate_pct": None,
            "new_customer_share_pct": None,
            "churn_risk_regions_count": 0,
            "stability_score": None,
            "start": start_iso,
            "end": end_iso,
        },
        "trend": {"labels": [], "revenue": [], "orders": [], "profit": [], "margin_pct": []},
        "charts": {
            "revenue_by_region": {"labels": [], "values": [], "top_n": TOP_N_DEFAULT},
            "profitability_by_region": {"rows": [], "top_n": TOP_N_DEFAULT},
        },
        "momentum": {
            "window": {
                "current_start": windows.get("current_start"),
                "current_end": windows.get("current_end"),
                "prior_start": windows.get("prior_start"),
                "prior_end": windows.get("prior_end"),
                "has_prior_period": False,
            },
            "rows": [],
            "gainers": [],
            "decliners": [],
        },
        "table": {
            "rows": [],
            "page": 1,
            "page_size": 25,
            "total": 0,
            "total_rows": 0,
            "sort_by": "revenue",
            "sort_dir": "desc",
            "search": "",
            "quick_filter": "",
        },
        "concentration": {"over_reliant_regions": [], "summary": {}},
        "retention": {"rows": [], "summary": {}},
        "operations": {"rows": []},
        "unit_economics": {"rows": []},
        "risk": {"rows": [], "summary": {}},
        "opportunity_matrix": {"points": [], "revenue_median": None, "margin_median": None},
        "meta": {"page_id": "regions", "freshness": _freshness_meta()},
    }


def _regions_overview_context(filters: Any, scope: Dict[str, Any]) -> Dict[str, Any]:
    cols = fact_store.list_columns()
    cols_map = _required_columns(cols)
    missing = [k for k in ("date", "region", "revenue", "order", "customer") if not cols_map.get(k)]
    if missing:
        return {"error": {"message": f"Required columns missing for regions bundle: {', '.join(missing)}"}, "meta": {"cached": False}}

    where_sql, params, start_iso, end_iso = fact_store.build_where_clause(filters, cols, scope, apply_default_window=True)
    scoped_sql = _region_scoped_sql(cols_map, where_sql)
    windows = _comparison_windows(start_iso, end_iso)

    prior_where_sql = None
    prior_params: List[Any] = []
    prior_scoped_sql = None
    if windows.get("prior_start") and windows.get("prior_end"):
        prior_filters = _filters_with_window(filters, str(windows["prior_start"]), str(windows["prior_end"]))
        prior_where_sql, prior_params, _, _ = fact_store.build_where_clause(prior_filters, cols, scope, apply_default_window=True)
        prior_scoped_sql = _region_scoped_sql(cols_map, prior_where_sql)

    metrics_sql = f"""
        WITH scoped AS (
            {scoped_sql}
        ),
        monthly AS (
            SELECT
                strftime('%Y-%m', order_date) AS month,
                SUM(revenue) AS revenue,
                COUNT(DISTINCT order_id) AS orders,
                CASE WHEN SUM(cost) IS NULL THEN NULL ELSE SUM(revenue) - SUM(cost) END AS profit,
                CASE WHEN SUM(revenue) > 0 AND SUM(cost) IS NOT NULL THEN (SUM(revenue) - SUM(cost)) / SUM(revenue) * 100 ELSE NULL END AS margin_pct
            FROM scoped
            GROUP BY 1
            ORDER BY 1
        ),
        weekly AS (
            SELECT
                strftime('%Y-%W', order_date) AS week,
                SUM(revenue) AS revenue
            FROM scoped
            GROUP BY 1
            ORDER BY 1
        ),
        kpis AS (
            SELECT
                SUM(revenue) AS revenue,
                COUNT(DISTINCT region) AS regions,
                COUNT(DISTINCT order_id) AS orders,
                COUNT(DISTINCT customer_id) AS customers,
                COUNT(*) AS rows,
                SUM(CASE WHEN cost IS NOT NULL THEN 1 ELSE 0 END) AS rows_with_cost,
                CASE WHEN SUM(cost) IS NULL THEN NULL ELSE SUM(revenue) - SUM(cost) END AS profit,
                CASE WHEN SUM(revenue) > 0 AND SUM(cost) IS NOT NULL THEN (SUM(revenue) - SUM(cost)) / SUM(revenue) * 100 ELSE NULL END AS margin_pct,
                CASE
                    WHEN SUM(CASE WHEN revenue > 0 AND minimum_margin_pct IS NOT NULL THEN revenue ELSE 0 END) > 0
                    THEN SUM(CASE WHEN revenue > 0 AND minimum_margin_pct IS NOT NULL THEN revenue * minimum_margin_pct ELSE 0 END)
                        / NULLIF(SUM(CASE WHEN revenue > 0 AND minimum_margin_pct IS NOT NULL THEN revenue ELSE 0 END), 0)
                    ELSE NULL
                END AS minimum_margin_pct,
                CASE
                    WHEN SUM(CASE WHEN revenue > 0 AND target_margin_pct IS NOT NULL THEN revenue ELSE 0 END) > 0
                    THEN SUM(CASE WHEN revenue > 0 AND target_margin_pct IS NOT NULL THEN revenue * target_margin_pct ELSE 0 END)
                        / NULLIF(SUM(CASE WHEN revenue > 0 AND target_margin_pct IS NOT NULL THEN revenue ELSE 0 END), 0)
                    ELSE NULL
                END AS target_margin_pct,
                CASE WHEN COUNT(DISTINCT order_id) > 0 THEN SUM(revenue) / COUNT(DISTINCT order_id) ELSE 0 END AS avg_order_value,
                CASE WHEN COUNT(*) > 0 THEN SUM(CASE WHEN cost IS NOT NULL THEN 1 ELSE 0 END)::DOUBLE / COUNT(*) * 100 ELSE NULL END AS cost_coverage_pct
            FROM scoped
        ),
        cust_orders AS (
            SELECT customer_id, COUNT(DISTINCT order_id) AS orders
            FROM scoped
            WHERE customer_id IS NOT NULL AND customer_id <> ''
            GROUP BY 1
        ),
        repeat_totals AS (
            SELECT
                SUM(CASE WHEN orders >= 2 THEN 1 ELSE 0 END) AS repeat_customers,
                COUNT(*) AS total_customers
            FROM cust_orders
        )
        SELECT
            kpis.*,
            (SELECT repeat_customers FROM repeat_totals) AS repeat_customers,
            (SELECT total_customers FROM repeat_totals) AS repeat_total,
            (SELECT list(struct_pack(month:=month, revenue:=revenue, orders:=orders, profit:=profit, margin_pct:=margin_pct)) FROM monthly) AS monthly,
            (SELECT list(struct_pack(week:=week, revenue:=revenue)) FROM weekly) AS weekly
        FROM kpis
    """

    region_sql = f"""
        WITH current_scoped AS (
            {scoped_sql}
        ),
        prior_scoped AS (
            {prior_scoped_sql if prior_scoped_sql else "SELECT * FROM current_scoped WHERE 1 = 0"}
        ),
        base_regions AS (
            SELECT DISTINCT region
            FROM current_scoped
            WHERE region IS NOT NULL AND TRIM(region) <> ''
            UNION
            SELECT DISTINCT region
            FROM prior_scoped
            WHERE region IS NOT NULL AND TRIM(region) <> ''
        ),
        current_rollup AS (
            SELECT
                region,
                SUM(revenue) AS revenue,
                SUM(cost) AS cost,
                CASE WHEN SUM(cost) IS NULL THEN NULL ELSE SUM(revenue) - SUM(cost) END AS profit,
                CASE WHEN SUM(revenue) > 0 AND SUM(cost) IS NOT NULL THEN (SUM(revenue) - SUM(cost)) / SUM(revenue) * 100 ELSE NULL END AS margin_pct,
                CASE
                    WHEN SUM(CASE WHEN revenue > 0 AND minimum_margin_pct IS NOT NULL THEN revenue ELSE 0 END) > 0
                    THEN SUM(CASE WHEN revenue > 0 AND minimum_margin_pct IS NOT NULL THEN revenue * minimum_margin_pct ELSE 0 END)
                        / NULLIF(SUM(CASE WHEN revenue > 0 AND minimum_margin_pct IS NOT NULL THEN revenue ELSE 0 END), 0)
                    ELSE NULL
                END AS minimum_margin_pct,
                CASE
                    WHEN SUM(CASE WHEN revenue > 0 AND target_margin_pct IS NOT NULL THEN revenue ELSE 0 END) > 0
                    THEN SUM(CASE WHEN revenue > 0 AND target_margin_pct IS NOT NULL THEN revenue * target_margin_pct ELSE 0 END)
                        / NULLIF(SUM(CASE WHEN revenue > 0 AND target_margin_pct IS NOT NULL THEN revenue ELSE 0 END), 0)
                    ELSE NULL
                END AS target_margin_pct,
                COUNT(DISTINCT order_id) AS orders,
                COUNT(DISTINCT customer_id) AS customers,
                SUM(COALESCE(qty, 0)) AS qty,
                SUM(COALESCE(weight_lb, 0)) AS weight_lb,
                COUNT(*) AS rows_total,
                SUM(CASE WHEN cost IS NOT NULL THEN 1 ELSE 0 END) AS rows_with_cost,
                SUM(CASE WHEN cost IS NULL THEN revenue ELSE 0 END) AS missing_cost_revenue,
                SUM(CASE WHEN missing_packs THEN 1 ELSE 0 END) AS missing_packs_rows
            FROM current_scoped
            WHERE region IS NOT NULL AND TRIM(region) <> ''
            GROUP BY 1
        ),
        prior_rollup AS (
            SELECT
                region,
                SUM(revenue) AS revenue_prior,
                SUM(cost) AS cost_prior,
                CASE WHEN SUM(cost) IS NULL THEN NULL ELSE SUM(revenue) - SUM(cost) END AS profit_prior,
                CASE WHEN SUM(revenue) > 0 AND SUM(cost) IS NOT NULL THEN (SUM(revenue) - SUM(cost)) / SUM(revenue) * 100 ELSE NULL END AS margin_pct_prior,
                COUNT(DISTINCT order_id) AS orders_prior,
                COUNT(DISTINCT customer_id) AS customers_prior
            FROM prior_scoped
            WHERE region IS NOT NULL AND TRIM(region) <> ''
            GROUP BY 1
        ),
        current_cust AS (
            SELECT
                region,
                customer_id,
                COALESCE(MAX(customer_name), customer_id) AS customer_name,
                SUM(revenue) AS revenue_current,
                COUNT(DISTINCT order_id) AS orders_current,
                MAX(order_date) AS last_order_current
            FROM current_scoped
            WHERE region IS NOT NULL AND TRIM(region) <> '' AND customer_id IS NOT NULL AND customer_id <> ''
            GROUP BY 1,2
        ),
        prior_cust AS (
            SELECT
                region,
                customer_id,
                COALESCE(MAX(customer_name), customer_id) AS customer_name,
                SUM(revenue) AS revenue_prior,
                COUNT(DISTINCT order_id) AS orders_prior
            FROM prior_scoped
            WHERE region IS NOT NULL AND TRIM(region) <> '' AND customer_id IS NOT NULL AND customer_id <> ''
            GROUP BY 1,2
        ),
        customer_compare AS (
            SELECT
                COALESCE(c.region, p.region) AS region,
                COALESCE(c.customer_id, p.customer_id) AS customer_id,
                COALESCE(c.customer_name, p.customer_name) AS customer_name,
                COALESCE(c.revenue_current, 0) AS revenue_current,
                COALESCE(p.revenue_prior, 0) AS revenue_prior,
                COALESCE(c.orders_current, 0) AS orders_current,
                COALESCE(p.orders_prior, 0) AS orders_prior,
                c.last_order_current AS last_order_current
            FROM current_cust c
            FULL OUTER JOIN prior_cust p
                ON p.region = c.region AND p.customer_id = c.customer_id
        ),
        customer_summary AS (
            SELECT
                region,
                SUM(CASE WHEN orders_current >= 2 THEN 1 ELSE 0 END) AS repeat_customers,
                SUM(CASE WHEN revenue_current > 0 AND revenue_prior <= 0 THEN 1 ELSE 0 END) AS new_customers,
                SUM(CASE WHEN revenue_current > 0 AND revenue_prior > 0 THEN 1 ELSE 0 END) AS returning_customers,
                SUM(CASE WHEN revenue_current <= 0 AND revenue_prior > 0 THEN 1 ELSE 0 END) AS lost_customers,
                SUM(CASE WHEN revenue_current > 0 AND revenue_prior <= 0 THEN revenue_current ELSE 0 END) AS new_customer_revenue,
                SUM(CASE WHEN revenue_current > 0 AND revenue_prior > 0 THEN revenue_current ELSE 0 END) AS returning_customer_revenue
            FROM customer_compare
            GROUP BY 1
        ),
        current_ref AS (
            SELECT region, MAX(order_date) AS ref_date
            FROM current_scoped
            WHERE region IS NOT NULL AND TRIM(region) <> ''
            GROUP BY 1
        ),
        customer_health AS (
            SELECT
                cc.region,
                SUM(CASE WHEN DATE_DIFF('day', cc.last_order_current, cr.ref_date) <= 30 THEN 1 ELSE 0 END) AS active_customers_30d,
                SUM(CASE WHEN DATE_DIFF('day', cc.last_order_current, cr.ref_date) <= 90 THEN 1 ELSE 0 END) AS active_customers_90d,
                SUM(CASE WHEN DATE_DIFF('day', cc.last_order_current, cr.ref_date) > 30 AND DATE_DIFF('day', cc.last_order_current, cr.ref_date) <= 90 THEN 1 ELSE 0 END) AS at_risk_customers,
                SUM(CASE WHEN DATE_DIFF('day', cc.last_order_current, cr.ref_date) > 90 THEN 1 ELSE 0 END) AS churned_customers
            FROM customer_compare cc
            JOIN current_ref cr ON cr.region = cc.region
            WHERE cc.revenue_current > 0 AND cc.last_order_current IS NOT NULL
            GROUP BY 1
        ),
        customer_concentration AS (
            SELECT region, MAX(customer_revenue) AS top_customer_revenue
            FROM (
                SELECT region, customer_id, SUM(revenue) AS customer_revenue
                FROM current_scoped
                WHERE region IS NOT NULL AND TRIM(region) <> '' AND customer_id IS NOT NULL AND customer_id <> ''
                GROUP BY 1,2
            ) ranked
            GROUP BY 1
        ),
        product_metrics AS (
            SELECT
                region,
                COUNT(DISTINCT product_id) AS product_count,
                MAX(product_revenue) AS top_product_revenue
            FROM (
                SELECT region, product_id, SUM(revenue) AS product_revenue
                FROM current_scoped
                WHERE region IS NOT NULL AND TRIM(region) <> '' AND product_id IS NOT NULL AND product_id <> ''
                GROUP BY 1,2
            ) ranked
            GROUP BY 1
        ),
        supplier_metrics AS (
            SELECT
                region,
                COUNT(DISTINCT supplier_key) AS supplier_count,
                MAX(supplier_revenue) AS top_supplier_revenue
            FROM (
                SELECT
                    region,
                    COALESCE(NULLIF(supplier_id, ''), NULLIF(supplier_name, '')) AS supplier_key,
                    SUM(revenue) AS supplier_revenue
                FROM current_scoped
                WHERE region IS NOT NULL AND TRIM(region) <> ''
                  AND COALESCE(NULLIF(supplier_id, ''), NULLIF(supplier_name, '')) IS NOT NULL
                GROUP BY 1,2
            ) ranked
            GROUP BY 1
        ),
        ship_ranked AS (
            SELECT
                region,
                ship_method AS dominant_ship_method,
                SUM(revenue) AS dominant_ship_revenue,
                ROW_NUMBER() OVER (PARTITION BY region ORDER BY SUM(revenue) DESC, ship_method) AS rn
            FROM current_scoped
            WHERE region IS NOT NULL AND TRIM(region) <> '' AND ship_method IS NOT NULL AND ship_method <> ''
            GROUP BY 1,2
        ),
        ship_top AS (
            SELECT region, dominant_ship_method, dominant_ship_revenue
            FROM ship_ranked
            WHERE rn = 1
        )
        SELECT
            base.region AS region,
            c.revenue,
            c.cost,
            c.profit,
            c.margin_pct,
            c.minimum_margin_pct,
            c.target_margin_pct,
            c.orders,
            c.customers,
            c.qty,
            c.weight_lb,
            c.rows_total,
            c.rows_with_cost,
            c.missing_cost_revenue,
            c.missing_packs_rows,
            p.revenue_prior,
            p.cost_prior,
            p.profit_prior,
            p.margin_pct_prior,
            p.orders_prior,
            p.customers_prior,
            cs.repeat_customers,
            cs.new_customers,
            cs.returning_customers,
            cs.lost_customers,
            cs.new_customer_revenue,
            cs.returning_customer_revenue,
            ch.active_customers_30d,
            ch.active_customers_90d,
            ch.at_risk_customers,
            ch.churned_customers,
            cc.top_customer_revenue,
            pm.product_count,
            pm.top_product_revenue,
            sm.supplier_count,
            sm.top_supplier_revenue,
            st.dominant_ship_method,
            st.dominant_ship_revenue
        FROM base_regions base
        LEFT JOIN current_rollup c ON c.region = base.region
        LEFT JOIN prior_rollup p ON p.region = base.region
        LEFT JOIN customer_summary cs ON cs.region = base.region
        LEFT JOIN customer_health ch ON ch.region = base.region
        LEFT JOIN customer_concentration cc ON cc.region = base.region
        LEFT JOIN product_metrics pm ON pm.region = base.region
        LEFT JOIN supplier_metrics sm ON sm.region = base.region
        LEFT JOIN ship_top st ON st.region = base.region
        ORDER BY COALESCE(c.revenue, 0) DESC, base.region
    """

    metrics_df = fact_store.execute_sql_df(metrics_sql, params, tag="regions.kpis")
    region_df = fact_store.execute_sql_df(region_sql, params + prior_params, tag="regions.table")

    yoy_start = windows.get("yoy_start")
    yoy_end = windows.get("yoy_end")

    yoy_growth = None
    if yoy_start and yoy_end:
        yoy_filters = _filters_with_window(filters, str(yoy_start), str(yoy_end))
        yoy_where_sql, yoy_params, _, _ = fact_store.build_where_clause(yoy_filters, cols, scope, apply_default_window=True)
        yoy_scoped_sql = _region_scoped_sql(cols_map, yoy_where_sql)
        yoy_sql = f"""
            WITH scoped AS (
                {yoy_scoped_sql}
            )
            SELECT SUM(revenue) AS revenue
            FROM scoped
        """
        yoy_df = fact_store.execute_sql_df(yoy_sql, yoy_params, tag="regions.yoy")
        yoy_revenue_raw = yoy_df.iloc[0].get("revenue") if not yoy_df.empty else None
        try:
            yoy_revenue = float(yoy_revenue_raw) if yoy_revenue_raw is not None else None
        except Exception:
            yoy_revenue = None
        if yoy_revenue is not None and not math.isnan(yoy_revenue) and yoy_revenue != 0:
            metrics_revenue_raw = metrics_df.iloc[0].get("revenue") if not metrics_df.empty else 0.0
            try:
                current_revenue = float(metrics_revenue_raw or 0.0)
            except Exception:
                current_revenue = 0.0
            yoy_growth = (current_revenue - yoy_revenue) / abs(yoy_revenue) * 100.0
    return {
        "metrics_df": metrics_df,
        "region_df": region_df,
        "start_iso": start_iso,
        "end_iso": end_iso,
        "windows": windows,
        "yoy_growth": yoy_growth,
        "has_missing_packs": bool(cols_map.get("missing_packs")),
        "freshness": _freshness_meta(),
    }


def _materialize_region_frame(context: Dict[str, Any]) -> pd.DataFrame:
    region_df = context.get("region_df")
    if not isinstance(region_df, pd.DataFrame) or region_df.empty:
        return pd.DataFrame()

    records: list[dict[str, Any]] = []
    has_missing_packs = bool(context.get("has_missing_packs"))
    for row in region_df.to_dict(orient="records"):
        region = str(row.get("region") or "").strip()
        if not region:
            continue
        revenue = _clean_float(row.get("revenue"), 0.0)
        revenue_prior = _clean_float(row.get("revenue_prior"), 0.0)
        cost = _clean_optional_float(row.get("cost"))
        cost_prior = _clean_optional_float(row.get("cost_prior"))
        profit = _clean_optional_float(row.get("profit"))
        profit_prior = _clean_optional_float(row.get("profit_prior"))
        margin_pct = _clean_optional_float(row.get("margin_pct"))
        margin_pct_prior = _clean_optional_float(row.get("margin_pct_prior"))
        minimum_margin_pct = _clean_optional_float(row.get("minimum_margin_pct"))
        target_margin_pct = _clean_optional_float(row.get("target_margin_pct"))
        orders = _clean_int(row.get("orders"))
        orders_prior = _clean_int(row.get("orders_prior"))
        customers = _clean_int(row.get("customers"))
        customers_prior = _clean_int(row.get("customers_prior"))
        qty = _clean_float(row.get("qty"), 0.0)
        weight_lb = _clean_float(row.get("weight_lb"), 0.0)
        rows_total = _clean_int(row.get("rows_total"))
        rows_with_cost = _clean_int(row.get("rows_with_cost"))
        repeat_customers = _clean_int(row.get("repeat_customers"))
        new_customers = _clean_int(row.get("new_customers"))
        returning_customers = _clean_int(row.get("returning_customers"))
        lost_customers = _clean_int(row.get("lost_customers"))
        new_customer_revenue = _clean_float(row.get("new_customer_revenue"), 0.0)
        returning_customer_revenue = _clean_float(row.get("returning_customer_revenue"), 0.0)
        active_customers_30d = _clean_int(row.get("active_customers_30d"))
        active_customers_90d = _clean_int(row.get("active_customers_90d"))
        at_risk_customers = _clean_int(row.get("at_risk_customers"))
        churned_customers = _clean_int(row.get("churned_customers"))
        top_customer_revenue = _clean_float(row.get("top_customer_revenue"), 0.0)
        top_product_revenue = _clean_float(row.get("top_product_revenue"), 0.0)
        top_supplier_revenue = _clean_float(row.get("top_supplier_revenue"), 0.0)
        supplier_count = _clean_int(row.get("supplier_count"))
        product_count = _clean_int(row.get("product_count"))
        dominant_ship_method = row.get("dominant_ship_method")
        dominant_ship_revenue = _clean_float(row.get("dominant_ship_revenue"), 0.0)
        missing_cost_revenue = _clean_float(row.get("missing_cost_revenue"), 0.0)
        missing_packs_rows = _clean_int(row.get("missing_packs_rows"))

        delta_revenue = revenue - revenue_prior
        delta_orders = orders - orders_prior
        delta_customers = customers - customers_prior
        delta_revenue_pct, delta_label, low_base = _mover_delta_meta(revenue, revenue_prior)
        delta_status, delta_label = _delta_status(delta_revenue_pct, delta_label)

        cost_coverage_pct = _safe_pct(rows_with_cost, rows_total)
        packs_coverage_pct = None
        if has_missing_packs and rows_total > 0:
            packs_coverage_pct = ((rows_total - missing_packs_rows) / rows_total) * 100.0
        aov = (revenue / orders) if orders > 0 else 0.0
        repeat_pct = _safe_pct(repeat_customers, customers)
        churn_pct = _safe_pct(churned_customers, customers)
        at_risk_pct = _safe_pct(at_risk_customers, customers)
        new_customer_pct = _safe_pct(new_customer_revenue, revenue)
        returning_customer_pct = _safe_pct(returning_customer_revenue, revenue)
        top_customer_share_pct = _safe_pct(top_customer_revenue, revenue)
        top_product_share_pct = _safe_pct(top_product_revenue, revenue)
        top_supplier_share_pct = _safe_pct(top_supplier_revenue, revenue)
        dominant_ship_share_pct = _safe_pct(dominant_ship_revenue, revenue)
        revenue_per_customer = (revenue / customers) if customers > 0 else None
        profit_per_order = (profit / orders) if (profit is not None and orders > 0) else None
        revenue_per_unit = (revenue / qty) if qty > 0 else None
        revenue_per_lb = (revenue / weight_lb) if weight_lb > 0 else None
        margin_delta_pp = None
        if margin_pct is not None and margin_pct_prior is not None:
            margin_delta_pp = margin_pct - margin_pct_prior
        profit_delta = None
        if profit is not None and profit_prior is not None:
            profit_delta = profit - profit_prior
        status = margin_rules.classify_margin_status(margin_pct, minimum_margin_pct, target_margin_pct)

        record = {
            "region_id": region,
            "region": region,
            "key": region,
            "label": region,
            "revenue": round(revenue, 2),
            "revenue_prior": round(revenue_prior, 2),
            "cost": None if cost is None else round(cost, 2),
            "cost_prior": None if cost_prior is None else round(cost_prior, 2),
            "profit": None if profit is None else round(profit, 2),
            "profit_prior": None if profit_prior is None else round(profit_prior, 2),
            "profit_delta": None if profit_delta is None else round(profit_delta, 2),
            "margin_pct": None if margin_pct is None else round(margin_pct, 2),
            "margin_pct_prior": None if margin_pct_prior is None else round(margin_pct_prior, 2),
            "margin_delta_pp": None if margin_delta_pp is None else round(margin_delta_pp, 2),
            "minimum_margin_pct": None if minimum_margin_pct is None else round(minimum_margin_pct, 2),
            "target_margin_pct": None if target_margin_pct is None else round(target_margin_pct, 2),
            "target_gap_pct_points": None if margin_pct is None or target_margin_pct is None else round(margin_pct - target_margin_pct, 2),
            "minimum_gap_pct_points": None if margin_pct is None or minimum_margin_pct is None else round(margin_pct - minimum_margin_pct, 2),
            "orders": orders,
            "orders_prior": orders_prior,
            "delta_orders": delta_orders,
            "customers": customers,
            "customers_prior": customers_prior,
            "delta_customers": delta_customers,
            "aov": round(aov, 2),
            "repeat_pct": None if repeat_pct is None else round(repeat_pct, 2),
            "churn_pct": None if churn_pct is None else round(churn_pct, 2),
            "at_risk_pct": None if at_risk_pct is None else round(at_risk_pct, 2),
            "at_risk_customers": at_risk_customers,
            "active_customers_30d": active_customers_30d,
            "active_customers_90d": active_customers_90d,
            "new_customers": new_customers,
            "returning_customers": returning_customers,
            "lost_customers": lost_customers,
            "new_customer_revenue": round(new_customer_revenue, 2),
            "returning_customer_revenue": round(returning_customer_revenue, 2),
            "new_customer_pct": None if new_customer_pct is None else round(new_customer_pct, 2),
            "returning_customer_pct": None if returning_customer_pct is None else round(returning_customer_pct, 2),
            "top_customer_share_pct": None if top_customer_share_pct is None else round(top_customer_share_pct, 2),
            "top_product_share_pct": None if top_product_share_pct is None else round(top_product_share_pct, 2),
            "top_supplier_share_pct": None if top_supplier_share_pct is None else round(top_supplier_share_pct, 2),
            "dominant_ship_method": dominant_ship_method,
            "dominant_ship_share_pct": None if dominant_ship_share_pct is None else round(dominant_ship_share_pct, 2),
            "supplier_count": supplier_count,
            "product_count": product_count,
            "qty": round(qty, 2),
            "weight_lb": round(weight_lb, 2),
            "revenue_per_customer": None if revenue_per_customer is None else round(revenue_per_customer, 2),
            "profit_per_order": None if profit_per_order is None else round(profit_per_order, 2),
            "revenue_per_unit": None if revenue_per_unit is None else round(revenue_per_unit, 2),
            "revenue_per_lb": None if revenue_per_lb is None else round(revenue_per_lb, 2),
            "rows_total": rows_total,
            "rows_with_cost": rows_with_cost,
            "cost_coverage_pct": None if cost_coverage_pct is None else round(cost_coverage_pct, 2),
            "packs_coverage_pct": None if packs_coverage_pct is None else round(packs_coverage_pct, 2),
            "missing_cost_revenue": round(missing_cost_revenue, 2),
            "missing_packs_rows": missing_packs_rows,
            "delta_revenue": round(delta_revenue, 2),
            "delta_revenue_pct": None if delta_revenue_pct is None else round(delta_revenue_pct, 2),
            "delta_revenue_label": delta_label,
            "delta_revenue_status": delta_status,
            "low_base": bool(low_base),
            "has_current_data": bool(revenue > 0),
            "has_prior_data": bool(revenue_prior > 0),
            **status,
        }
        risk_band, risk_score, risk_reasons = _risk_profile(record)
        record["risk_band"] = risk_band
        record["risk_score"] = risk_score
        record["risk_reasons"] = risk_reasons
        record["risk_summary"] = ", ".join(risk_reasons) if risk_reasons else "Healthy"
        record["data_quality_flag"] = _quality_flag(record)
        records.append(record)

    if not records:
        return pd.DataFrame()
    return pd.DataFrame.from_records(records)


def build_regions_bundle(filters: Any, scope: Dict[str, Any], args: Any) -> Dict[str, Any]:
    context = _regions_overview_context(filters, scope)
    if context.get("error"):
        return context

    metrics_df: pd.DataFrame = context["metrics_df"]
    region_frame = _materialize_region_frame(context)
    start_iso = context.get("start_iso")
    end_iso = context.get("end_iso")
    if metrics_df.empty:
        return _build_empty_regions_payload(start_iso, end_iso)

    def _m(col: str, default: Any = None) -> Any:
        if metrics_df.empty or col not in metrics_df.columns:
            return default
        return metrics_df.at[0, col]

    monthly = _struct_list(_m("monthly"))
    weekly = _struct_list(_m("weekly"))
    trend_labels = [str(item.get("month")) for item in monthly if item.get("month") is not None]
    trend_revenue = [_clean_float(item.get("revenue")) for item in monthly]
    trend_orders = [_clean_int(item.get("orders")) for item in monthly]
    trend_profit = [item.get("profit") if item.get("profit") is None else _clean_float(item.get("profit")) for item in monthly]
    trend_margin = [item.get("margin_pct") if item.get("margin_pct") is None else _clean_float(item.get("margin_pct")) for item in monthly]
    weekly_values = [_clean_float(item.get("revenue")) for item in weekly]

    mom_growth, _legacy_yoy = _calc_growth(trend_labels, trend_revenue)
    wow_growth = _calc_wow(weekly_values)
    trend_tail = trend_revenue[-12:] if len(trend_revenue) > 12 else trend_revenue
    revenue_volatility = None
    revenue_volatility_pct = None
    stability_score = None
    if len(trend_tail) >= 2:
        revenue_volatility = float(round(pstdev(trend_tail), 2))
        avg_tail = sum(trend_tail) / len(trend_tail)
        if avg_tail > 0:
            revenue_volatility_pct = float(round((revenue_volatility / avg_tail) * 100.0, 2))
            stability_score = float(round(max(0.0, 100.0 - min(revenue_volatility_pct, 100.0)), 2))

    revenue_total = _clean_float(_m("revenue"))
    prior_total_revenue = float(pd.to_numeric(region_frame.get("revenue_prior"), errors="coerce").fillna(0.0).sum()) if not region_frame.empty else 0.0
    window_delta_revenue = revenue_total - prior_total_revenue
    window_delta_pct = (window_delta_revenue / abs(prior_total_revenue) * 100.0) if prior_total_revenue else None

    current_rows = region_frame.loc[pd.to_numeric(region_frame.get("revenue"), errors="coerce").fillna(0.0) > 0].copy() if not region_frame.empty else pd.DataFrame()
    current_rows = current_rows.sort_values(["revenue", "region"], ascending=[False, True]) if not current_rows.empty else current_rows
    chart_labels = current_rows["region"].astype(str).tolist() if not current_rows.empty else []
    chart_values = [float(v) for v in current_rows["revenue"].tolist()] if not current_rows.empty else []

    top1_share_pct = None
    top5_share_pct = None
    revenue_hhi = None
    if revenue_total > 0 and chart_values:
        shares = [v / revenue_total for v in chart_values if v > 0]
        if shares:
            top1_share_pct = shares[0] * 100.0
            top5_share_pct = sum(shares[:5]) * 100.0
            revenue_hhi = sum((share * 100.0) ** 2 for share in shares)

    repeat_rate_pct = _safe_pct(_m("repeat_customers"), _m("repeat_total"))
    new_customer_share_pct = _safe_pct(region_frame.get("new_customer_revenue", pd.Series(dtype="float64")).sum(), revenue_total) if not region_frame.empty else None
    churn_risk_regions_count = int((region_frame.get("risk_band", pd.Series(dtype="object")).astype(str).str.lower() == "high").sum()) if not region_frame.empty else 0
    kpi_margin_status = margin_rules.classify_margin_status(_m("margin_pct"), _m("minimum_margin_pct"), _m("target_margin_pct"))

    kpis = {
        "total_revenue": revenue_total,
        "regions_count": _clean_int(_m("regions")),
        "avg_order_value": _clean_float(_m("avg_order_value")),
        "orders": _clean_int(_m("orders")),
        "customers": _clean_int(_m("customers")),
        "rows": _clean_int(_m("rows")),
        "rows_with_cost": _clean_int(_m("rows_with_cost")),
        "cost_coverage_pct": _m("cost_coverage_pct"),
        "profit": _m("profit"),
        "margin_pct": _m("margin_pct"),
        "minimum_margin_pct": _m("minimum_margin_pct"),
        "target_margin_pct": _m("target_margin_pct"),
        "yoy_growth": None if context.get("yoy_growth") is None else round(float(context["yoy_growth"]), 2),
        "mom_growth": None if mom_growth is None else round(mom_growth, 2),
        "wow_growth": None if wow_growth is None else round(wow_growth, 2),
        "revenue_delta_prior": round(window_delta_revenue, 2),
        "revenue_delta_prior_pct": None if window_delta_pct is None else round(window_delta_pct, 2),
        "prior_period_revenue": round(prior_total_revenue, 2),
        "revenue_volatility": revenue_volatility,
        "revenue_volatility_pct": revenue_volatility_pct,
        "stability_score": stability_score,
        "concentration_top1_pct": None if top1_share_pct is None else round(top1_share_pct, 2),
        "concentration_top5_pct": None if top5_share_pct is None else round(top5_share_pct, 2),
        "revenue_hhi": None if revenue_hhi is None else round(revenue_hhi, 2),
        "repeat_rate_pct": None if repeat_rate_pct is None else round(repeat_rate_pct, 2),
        "new_customer_share_pct": None if new_customer_share_pct is None else round(new_customer_share_pct, 2),
        "churn_risk_regions_count": churn_risk_regions_count,
        "start": start_iso,
        "end": end_iso,
        **kpi_margin_status,
    }

    profitability_rows = _serialize_rows(
        current_rows,
        [
            "region",
            "revenue",
            "profit",
            "margin_pct",
            "minimum_margin_pct",
            "target_margin_pct",
            "target_gap_pct_points",
            "status_key",
            "target_status",
            "status_color",
            "aov",
            "profit_per_order",
            "revenue_per_customer",
            "revenue_per_unit",
            "revenue_per_lb",
        ],
    )

    momentum_source = region_frame.copy()
    momentum_source = momentum_source.sort_values(["delta_revenue", "region"], ascending=[False, True]) if not momentum_source.empty else momentum_source
    momentum_rows = _serialize_rows(
        momentum_source,
        [
            "region",
            "revenue",
            "revenue_prior",
            "delta_revenue",
            "delta_revenue_pct",
            "delta_revenue_label",
            "delta_revenue_status",
            "delta_orders",
            "delta_customers",
            "profit_delta",
            "margin_delta_pp",
            "risk_band",
        ],
    )
    for row in momentum_rows:
        row["revenue_current"] = row.pop("revenue", 0.0)

    gainers = [row for row in momentum_rows if _clean_float(row.get("delta_revenue"), 0.0) > 0][:5]
    decliners = [row for row in reversed(momentum_rows) if _clean_float(row.get("delta_revenue"), 0.0) < 0][:5]

    concentration_rows = current_rows.loc[
        (
            pd.to_numeric(current_rows.get("top_customer_share_pct"), errors="coerce").fillna(0.0) >= 35.0
        ) | (
            pd.to_numeric(current_rows.get("top_product_share_pct"), errors="coerce").fillna(0.0) >= 25.0
        )
    ].copy() if not current_rows.empty else pd.DataFrame()
    concentration_rows = concentration_rows.sort_values(["top_customer_share_pct", "revenue"], ascending=[False, False]) if not concentration_rows.empty else concentration_rows

    retention_rows = current_rows.sort_values(["churn_pct", "at_risk_customers"], ascending=[False, False]) if not current_rows.empty else current_rows
    operations_rows = current_rows.sort_values(["revenue", "region"], ascending=[False, True]) if not current_rows.empty else current_rows
    unit_rows = current_rows.sort_values(["profit_per_order", "revenue"], ascending=[False, False], na_position="last") if not current_rows.empty else current_rows

    risk_rows = region_frame.loc[
        (region_frame.get("risk_band", pd.Series(dtype="object")).astype(str).str.lower() != "low")
        | (region_frame.get("data_quality_flag", pd.Series(dtype="object")).astype(str).str.lower() != "ok")
        | (pd.to_numeric(region_frame.get("delta_revenue"), errors="coerce").fillna(0.0) < 0)
    ].copy() if not region_frame.empty else pd.DataFrame()
    risk_rows = risk_rows.sort_values(["risk_score", "revenue"], ascending=[False, False]) if not risk_rows.empty else risk_rows

    opportunity_points: list[dict[str, Any]] = []
    revenue_median = None
    margin_median = None
    matrix_base = current_rows.loc[current_rows["margin_pct"].notna()].copy() if not current_rows.empty else pd.DataFrame()
    if not matrix_base.empty:
        revenue_median = float(pd.to_numeric(matrix_base["revenue"], errors="coerce").median())
        margin_median = float(pd.to_numeric(matrix_base["margin_pct"], errors="coerce").median())
        for rec in matrix_base.to_dict(orient="records"):
            revenue_val = _clean_float(rec.get("revenue"), 0.0)
            margin_val = _clean_float(rec.get("margin_pct"), 0.0)
            if revenue_val >= revenue_median and margin_val >= margin_median:
                quadrant = "Scale"
            elif revenue_val >= revenue_median and margin_val < margin_median:
                quadrant = "Protect"
            elif revenue_val < revenue_median and margin_val < margin_median:
                quadrant = "Fix"
            else:
                quadrant = "Watch"
            opportunity_points.append(
                {
                    "region": rec.get("region"),
                    "revenue": revenue_val,
                    "margin_pct": margin_val,
                    "growth_pct": rec.get("delta_revenue_pct"),
                    "quadrant": quadrant,
                }
            )

    page_num, page_size = _pagination(args)
    sort_by, sort_dir = _sort_params(args)
    search = _extract_search(args)
    quick_filter = _extract_quick_filter(args)
    table_frame = region_frame.copy()
    if search:
        table_frame = table_frame.loc[table_frame["region"].astype(str).str.lower().str.contains(search.lower(), na=False)].copy()
    table_frame = _apply_quick_filter(table_frame, quick_filter)
    table_frame = _sort_frame(table_frame, sort_by, sort_dir)
    total_groups = int(len(table_frame.index))
    page_frame = table_frame.iloc[(page_num - 1) * page_size : page_num * page_size].copy()

    table_rows = _serialize_rows(
        page_frame,
        [
            "region_id",
            "region",
            "key",
            "label",
            "customers",
            "orders",
            "revenue",
            "profit",
            "margin_pct",
            "minimum_margin_pct",
            "target_margin_pct",
            "target_gap_pct_points",
            "status_key",
            "target_status",
            "status_color",
            "aov",
            "repeat_pct",
            "churn_pct",
            "new_customer_pct",
            "top_customer_share_pct",
            "top_product_share_pct",
            "delta_revenue",
            "delta_revenue_pct",
            "delta_revenue_label",
            "delta_revenue_status",
            "cost_coverage_pct",
            "packs_coverage_pct",
            "missing_cost_revenue",
            "data_quality_flag",
            "risk_band",
            "risk_summary",
            "at_risk_customers",
            "active_customers_30d",
            "active_customers_90d",
            "revenue_per_customer",
            "profit_per_order",
            "revenue_per_unit",
            "revenue_per_lb",
            "dominant_ship_method",
            "dominant_ship_share_pct",
            "supplier_count",
            "product_count",
            "top_supplier_share_pct",
        ],
    )

    payload = {
        "kpis": kpis,
        "trend": {
            "labels": trend_labels,
            "revenue": trend_revenue,
            "orders": trend_orders,
            "profit": trend_profit,
            "margin_pct": trend_margin,
        },
        "charts": {
            "revenue_by_region": {
                "labels": chart_labels,
                "values": chart_values,
                "top_n": _parse_top_n(args),
            },
            "profitability_by_region": {
                "rows": profitability_rows,
                "top_n": _parse_top_n(args),
            },
        },
        "momentum": {
            "window": {
                "current_start": context["windows"].get("current_start"),
                "current_end": context["windows"].get("current_end"),
                "prior_start": context["windows"].get("prior_start"),
                "prior_end": context["windows"].get("prior_end"),
                "has_prior_period": bool(prior_total_revenue > 0),
            },
            "rows": momentum_rows,
            "gainers": gainers,
            "decliners": decliners,
        },
        "table": {
            "rows": table_rows,
            "page": page_num,
            "page_size": page_size,
            "total": total_groups,
            "total_rows": total_groups,
            "sort_by": sort_by,
            "sort_dir": sort_dir.lower(),
            "search": search,
            "quick_filter": quick_filter,
        },
        "concentration": {
            "summary": {
                "top1_share_pct": kpis["concentration_top1_pct"],
                "top5_share_pct": kpis["concentration_top5_pct"],
                "hhi": kpis["revenue_hhi"],
                "over_reliant_regions_count": int(len(concentration_rows.index)),
            },
            "over_reliant_regions": _serialize_rows(
                concentration_rows.head(12),
                [
                    "region",
                    "revenue",
                    "top_customer_share_pct",
                    "top_product_share_pct",
                    "top_supplier_share_pct",
                    "risk_band",
                ],
            ),
        },
        "retention": {
            "summary": {
                "repeat_rate_pct": kpis["repeat_rate_pct"],
                "new_customer_share_pct": kpis["new_customer_share_pct"],
                "high_risk_regions": churn_risk_regions_count,
            },
            "rows": _serialize_rows(
                retention_rows.head(15),
                [
                    "region",
                    "repeat_pct",
                    "churn_pct",
                    "at_risk_customers",
                    "active_customers_30d",
                    "active_customers_90d",
                    "new_customer_pct",
                    "returning_customer_pct",
                    "risk_band",
                ],
            ),
        },
        "operations": {
            "rows": _serialize_rows(
                operations_rows.head(15),
                [
                    "region",
                    "dominant_ship_method",
                    "dominant_ship_share_pct",
                    "supplier_count",
                    "product_count",
                    "top_supplier_share_pct",
                    "top_product_share_pct",
                ],
            )
        },
        "unit_economics": {
            "rows": _serialize_rows(
                unit_rows.head(15),
                [
                    "region",
                    "revenue_per_customer",
                    "profit_per_order",
                    "revenue_per_unit",
                    "revenue_per_lb",
                    "margin_pct",
                ],
            )
        },
        "risk": {
            "summary": {
                "high_risk_regions": churn_risk_regions_count,
                "quality_watch_regions": int((region_frame.get("data_quality_flag", pd.Series(dtype="object")).astype(str).str.lower() != "ok").sum()) if not region_frame.empty else 0,
                "negative_trend_regions": int((pd.to_numeric(region_frame.get("delta_revenue"), errors="coerce").fillna(0.0) < 0).sum()) if not region_frame.empty else 0,
            },
            "rows": _serialize_rows(
                risk_rows.head(15),
                [
                    "region",
                    "risk_band",
                    "risk_summary",
                    "revenue",
                    "margin_pct",
                    "churn_pct",
                    "top_customer_share_pct",
                    "top_product_share_pct",
                    "cost_coverage_pct",
                    "packs_coverage_pct",
                    "delta_revenue",
                    "data_quality_flag",
                ],
            ),
        },
        "opportunity_matrix": {
            "points": opportunity_points,
            "revenue_median": revenue_median,
            "margin_median": margin_median,
        },
        "meta": {
            "page_id": "regions",
            "freshness": context.get("freshness") or _freshness_meta(),
            "last_refresh": (context.get("freshness") or {}).get("date_max"),
        },
    }
    return payload


_WEEKDAY_LABELS = {
    "0": "Sun",
    "1": "Mon",
    "2": "Tue",
    "3": "Wed",
    "4": "Thu",
    "5": "Fri",
    "6": "Sat",
}


def _iso_date_value(val: Any) -> str | None:
    if val is None:
        return None
    try:
        ts = pd.to_datetime(val, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.date().isoformat()
    except Exception:
        text = str(val).strip()
        return text or None


def _hhi_pct(values: list[float], total: float) -> float | None:
    if total <= 0:
        return None
    shares = [float(v) / total for v in values if float(v) > 0]
    if not shares:
        return None
    return round(sum((share * 100.0) ** 2 for share in shares), 2)


def _top_share_pct(values: list[float], total: float, limit: int) -> float | None:
    if total <= 0:
        return None
    if not values:
        return None
    return round(sum(values[:limit]) / total * 100.0, 2)


def _risk_order(label: str) -> int:
    token = str(label or "").strip().lower()
    return {
        "lost": 5,
        "churned": 4,
        "at risk": 3,
        "warming": 2,
        "new": 1,
        "active": 0,
    }.get(token, 0)


def _region_drilldown_context(region_id: str, filters: Any, scope: Dict[str, Any]) -> Dict[str, Any]:
    cols = fact_store.list_columns()
    cols_map = _required_columns(cols)
    missing = [k for k in ("date", "region", "revenue", "order", "customer") if not cols_map.get(k)]
    if missing:
        return {
            "error": {"message": f"Required columns missing for regions drilldown: {', '.join(missing)}"},
            "meta": {"cached": False},
        }

    where_sql, params, start_iso, end_iso = fact_store.build_where_clause(filters, cols, scope, apply_default_window=True)
    region_filter_sql = f"({where_sql}) AND CAST({_quote(cols_map['region'])} AS VARCHAR) = ?"
    current_scoped_sql = _region_scoped_sql(cols_map, region_filter_sql)
    current_params = list(params) + [region_id]

    windows = _comparison_windows(start_iso, end_iso)

    prior_scoped_sql = "SELECT * FROM current_scoped WHERE 1 = 0"
    prior_params: list[Any] = []
    if windows.get("prior_start") and windows.get("prior_end"):
        prior_filters = _filters_with_window(filters, str(windows["prior_start"]), str(windows["prior_end"]))
        prior_where_sql, prior_base_params, _, _ = fact_store.build_where_clause(
            prior_filters,
            cols,
            scope,
            apply_default_window=True,
        )
        prior_filter_sql = f"({prior_where_sql}) AND CAST({_quote(cols_map['region'])} AS VARCHAR) = ?"
        prior_scoped_sql = _region_scoped_sql(cols_map, prior_filter_sql)
        prior_params = list(prior_base_params) + [region_id]

    yoy_scoped_sql = "SELECT * FROM current_scoped WHERE 1 = 0"
    yoy_params: list[Any] = []
    if windows.get("yoy_start") and windows.get("yoy_end"):
        yoy_filters = _filters_with_window(filters, str(windows["yoy_start"]), str(windows["yoy_end"]))
        yoy_where_sql, yoy_base_params, _, _ = fact_store.build_where_clause(
            yoy_filters,
            cols,
            scope,
            apply_default_window=True,
        )
        yoy_filter_sql = f"({yoy_where_sql}) AND CAST({_quote(cols_map['region'])} AS VARCHAR) = ?"
        yoy_scoped_sql = _region_scoped_sql(cols_map, yoy_filter_sql)
        yoy_params = list(yoy_base_params) + [region_id]

    return {
        "region_id": str(region_id),
        "cols_map": cols_map,
        "start_iso": start_iso,
        "end_iso": end_iso,
        "windows": windows,
        "current_scoped_sql": current_scoped_sql,
        "prior_scoped_sql": prior_scoped_sql,
        "yoy_scoped_sql": yoy_scoped_sql,
        "current_params": current_params,
        "prior_params": prior_params,
        "yoy_params": yoy_params,
        "has_missing_packs": bool(cols_map.get("missing_packs")),
        "freshness": _freshness_meta(),
    }


def _region_drilldown_summary_frame(context: Dict[str, Any]) -> pd.DataFrame:
    summary_sql = f"""
        WITH current_scoped AS (
            {context["current_scoped_sql"]}
        ),
        prior_scoped AS (
            {context["prior_scoped_sql"]}
        ),
        yoy_scoped AS (
            {context["yoy_scoped_sql"]}
        ),
        ref AS (
            SELECT
                COALESCE(
                    (SELECT MAX(order_date) FROM current_scoped),
                    (SELECT MAX(order_date) FROM prior_scoped),
                    CURRENT_DATE
                ) AS ref_date
        ),
        current_base AS (
            SELECT
                SUM(revenue) AS revenue_current,
                SUM(cost) AS cost_current,
                CASE WHEN SUM(cost) IS NULL THEN NULL ELSE SUM(revenue) - SUM(cost) END AS profit_current,
                CASE WHEN SUM(revenue) > 0 AND SUM(cost) IS NOT NULL THEN (SUM(revenue) - SUM(cost)) / SUM(revenue) * 100 ELSE NULL END AS margin_pct_current,
                CASE
                    WHEN SUM(CASE WHEN revenue > 0 AND minimum_margin_pct IS NOT NULL THEN revenue ELSE 0 END) > 0
                    THEN SUM(CASE WHEN revenue > 0 AND minimum_margin_pct IS NOT NULL THEN revenue * minimum_margin_pct ELSE 0 END)
                        / NULLIF(SUM(CASE WHEN revenue > 0 AND minimum_margin_pct IS NOT NULL THEN revenue ELSE 0 END), 0)
                    ELSE NULL
                END AS minimum_margin_pct_current,
                CASE
                    WHEN SUM(CASE WHEN revenue > 0 AND target_margin_pct IS NOT NULL THEN revenue ELSE 0 END) > 0
                    THEN SUM(CASE WHEN revenue > 0 AND target_margin_pct IS NOT NULL THEN revenue * target_margin_pct ELSE 0 END)
                        / NULLIF(SUM(CASE WHEN revenue > 0 AND target_margin_pct IS NOT NULL THEN revenue ELSE 0 END), 0)
                    ELSE NULL
                END AS target_margin_pct_current,
                COUNT(DISTINCT order_id) AS orders_current,
                COUNT(DISTINCT customer_id) AS customers_current,
                SUM(COALESCE(qty, 0)) AS qty_current,
                SUM(COALESCE(weight_lb, 0)) AS weight_lb_current,
                COUNT(*) AS rows_current,
                SUM(CASE WHEN cost IS NOT NULL THEN 1 ELSE 0 END) AS rows_with_cost_current,
                SUM(CASE WHEN cost IS NULL THEN revenue ELSE 0 END) AS missing_cost_revenue_current,
                SUM(CASE WHEN missing_packs THEN 1 ELSE 0 END) AS missing_packs_rows_current
            FROM current_scoped
        ),
        prior_base AS (
            SELECT
                SUM(revenue) AS revenue_prior,
                SUM(cost) AS cost_prior,
                CASE WHEN SUM(cost) IS NULL THEN NULL ELSE SUM(revenue) - SUM(cost) END AS profit_prior,
                CASE WHEN SUM(revenue) > 0 AND SUM(cost) IS NOT NULL THEN (SUM(revenue) - SUM(cost)) / SUM(revenue) * 100 ELSE NULL END AS margin_pct_prior,
                COUNT(DISTINCT order_id) AS orders_prior,
                COUNT(DISTINCT customer_id) AS customers_prior
            FROM prior_scoped
        ),
        yoy_base AS (
            SELECT SUM(revenue) AS revenue_yoy FROM yoy_scoped
        ),
        current_cust AS (
            SELECT
                customer_id,
                COALESCE(MAX(customer_name), customer_id) AS customer_name,
                SUM(revenue) AS revenue_current,
                COUNT(DISTINCT order_id) AS orders_current,
                CASE WHEN SUM(cost) IS NULL THEN NULL ELSE SUM(revenue) - SUM(cost) END AS profit_current,
                MAX(order_date) AS last_order_current
            FROM current_scoped
            WHERE customer_id IS NOT NULL AND customer_id <> ''
            GROUP BY 1
        ),
        prior_cust AS (
            SELECT
                customer_id,
                COALESCE(MAX(customer_name), customer_id) AS customer_name,
                SUM(revenue) AS revenue_prior,
                COUNT(DISTINCT order_id) AS orders_prior,
                CASE WHEN SUM(cost) IS NULL THEN NULL ELSE SUM(revenue) - SUM(cost) END AS profit_prior,
                MAX(order_date) AS last_order_prior
            FROM prior_scoped
            WHERE customer_id IS NOT NULL AND customer_id <> ''
            GROUP BY 1
        ),
        customer_compare AS (
            SELECT
                COALESCE(c.customer_id, p.customer_id) AS customer_id,
                COALESCE(c.customer_name, p.customer_name) AS customer_name,
                COALESCE(c.revenue_current, 0) AS revenue_current,
                COALESCE(p.revenue_prior, 0) AS revenue_prior,
                COALESCE(c.orders_current, 0) AS orders_current,
                COALESCE(p.orders_prior, 0) AS orders_prior,
                c.profit_current AS profit_current,
                p.profit_prior AS profit_prior,
                c.last_order_current AS last_order_current,
                p.last_order_prior AS last_order_prior
            FROM current_cust c
            FULL OUTER JOIN prior_cust p ON p.customer_id = c.customer_id
        ),
        customer_summary AS (
            SELECT
                SUM(CASE WHEN orders_current >= 2 THEN 1 ELSE 0 END) AS repeat_customers,
                SUM(CASE WHEN revenue_current > 0 AND revenue_prior <= 0 THEN 1 ELSE 0 END) AS new_customers,
                SUM(CASE WHEN revenue_current > 0 AND revenue_prior > 0 THEN 1 ELSE 0 END) AS returning_customers,
                SUM(CASE WHEN revenue_current <= 0 AND revenue_prior > 0 THEN 1 ELSE 0 END) AS lost_customers,
                SUM(CASE WHEN revenue_current > 0 AND revenue_prior <= 0 THEN revenue_current ELSE 0 END) AS new_customer_revenue,
                SUM(CASE WHEN revenue_current > 0 AND revenue_prior > 0 THEN revenue_current ELSE 0 END) AS returning_customer_revenue,
                SUM(CASE WHEN revenue_current <= 0 AND revenue_prior > 0 THEN revenue_prior ELSE 0 END) AS lost_customer_revenue
            FROM customer_compare
        ),
        customer_health AS (
            SELECT
                SUM(CASE WHEN revenue_current > 0 AND last_order_current IS NOT NULL AND DATE_DIFF('day', last_order_current, ref.ref_date) <= 30 THEN 1 ELSE 0 END) AS active_customers_30d,
                SUM(CASE WHEN revenue_current > 0 AND last_order_current IS NOT NULL AND DATE_DIFF('day', last_order_current, ref.ref_date) > 30 AND DATE_DIFF('day', last_order_current, ref.ref_date) <= 60 THEN 1 ELSE 0 END) AS warming_customers,
                SUM(CASE WHEN revenue_current > 0 AND last_order_current IS NOT NULL AND DATE_DIFF('day', last_order_current, ref.ref_date) > 60 AND DATE_DIFF('day', last_order_current, ref.ref_date) <= {CHURN_THRESHOLD_DAYS} THEN 1 ELSE 0 END) AS at_risk_customers,
                SUM(CASE WHEN revenue_current > 0 AND last_order_current IS NOT NULL AND DATE_DIFF('day', last_order_current, ref.ref_date) > {CHURN_THRESHOLD_DAYS} THEN 1 ELSE 0 END) AS churned_customers,
                SUM(CASE WHEN revenue_current > 0 AND last_order_current IS NOT NULL AND DATE_DIFF('day', last_order_current, ref.ref_date) > 60 THEN revenue_current ELSE 0 END) AS at_risk_revenue,
                SUM(CASE WHEN revenue_current > 0 AND last_order_current IS NOT NULL AND DATE_DIFF('day', last_order_current, ref.ref_date) > {CHURN_THRESHOLD_DAYS} THEN revenue_current ELSE 0 END) AS churned_revenue
            FROM customer_compare, ref
        ),
        customer_concentration AS (
            SELECT
                MAX(CASE WHEN rn = 1 THEN revenue_current END) AS top_customer_revenue,
                SUM(CASE WHEN rn <= 5 THEN revenue_current ELSE 0 END) AS top5_customer_revenue,
                SUM(CASE WHEN rn <= 10 THEN revenue_current ELSE 0 END) AS top10_customer_revenue,
                SUM(CASE WHEN totals.total_revenue > 0 THEN POWER(revenue_current / totals.total_revenue * 100.0, 2) ELSE 0 END) AS customer_hhi
            FROM (
                SELECT
                    customer_id,
                    revenue_current,
                    ROW_NUMBER() OVER (ORDER BY revenue_current DESC, customer_id) AS rn
                FROM current_cust
                WHERE revenue_current > 0
            ) ranked
            CROSS JOIN (
                SELECT COALESCE(SUM(revenue_current), 0) AS total_revenue
                FROM current_cust
            ) totals
        ),
        current_prod AS (
            SELECT
                product_id,
                COALESCE(MAX(product_name), product_id) AS product_name,
                COALESCE(MAX(protein_family), MAX(product_category), 'Unassigned') AS protein_family,
                COALESCE(MAX(product_category), MAX(protein_family), 'Unassigned') AS product_category,
                SUM(revenue) AS revenue_current,
                SUM(base_cost) AS base_cost_current,
                SUM(cost) AS cost_current,
                CASE WHEN SUM(cost) IS NULL THEN NULL ELSE SUM(revenue) - SUM(cost) END AS profit_current,
                CASE WHEN SUM(revenue) > 0 AND SUM(cost) IS NOT NULL THEN (SUM(revenue) - SUM(cost)) / SUM(revenue) * 100 ELSE NULL END AS margin_pct_current,
                COUNT(DISTINCT order_id) AS orders_current,
                SUM(COALESCE(qty, 0)) AS qty_current,
                SUM(COALESCE(weight_lb, 0)) AS weight_lb_current
            FROM current_scoped
            WHERE product_id IS NOT NULL AND product_id <> ''
            GROUP BY 1
        ),
        product_summary AS (
            SELECT COUNT(*) AS product_count FROM current_prod
        ),
        product_concentration AS (
            SELECT
                MAX(CASE WHEN rn = 1 THEN revenue_current END) AS top_product_revenue,
                SUM(CASE WHEN rn <= 5 THEN revenue_current ELSE 0 END) AS top5_product_revenue,
                SUM(CASE WHEN rn <= 10 THEN revenue_current ELSE 0 END) AS top10_product_revenue,
                SUM(CASE WHEN totals.total_revenue > 0 THEN POWER(revenue_current / totals.total_revenue * 100.0, 2) ELSE 0 END) AS product_hhi
            FROM (
                SELECT
                    product_id,
                    revenue_current,
                    ROW_NUMBER() OVER (ORDER BY revenue_current DESC, product_id) AS rn
                FROM current_prod
                WHERE revenue_current > 0
            ) ranked
            CROSS JOIN (
                SELECT COALESCE(SUM(revenue_current), 0) AS total_revenue
                FROM current_prod
            ) totals
        ),
        supplier_rollup AS (
            SELECT
                COALESCE(NULLIF(supplier_id, ''), NULLIF(supplier_name, '')) AS supplier_key,
                COALESCE(NULLIF(supplier_name, ''), NULLIF(supplier_id, '')) AS supplier_name,
                SUM(revenue) AS supplier_revenue
            FROM current_scoped
            WHERE COALESCE(NULLIF(supplier_id, ''), NULLIF(supplier_name, '')) IS NOT NULL
            GROUP BY 1, 2
        ),
        supplier_summary AS (
            SELECT
                COUNT(*) AS supplier_count,
                MAX(CASE WHEN rn = 1 THEN supplier_revenue END) AS top_supplier_revenue
            FROM (
                SELECT
                    supplier_key,
                    supplier_revenue,
                    ROW_NUMBER() OVER (ORDER BY supplier_revenue DESC, supplier_key) AS rn
                FROM supplier_rollup
            ) ranked
        ),
        ship_rollup AS (
            SELECT
                ship_method AS method,
                SUM(revenue) AS ship_revenue
            FROM current_scoped
            WHERE ship_method IS NOT NULL AND ship_method <> ''
            GROUP BY 1
        ),
        shipping_summary AS (
            SELECT
                MAX(CASE WHEN rn = 1 THEN method END) AS dominant_ship_method,
                MAX(CASE WHEN rn = 1 THEN ship_revenue END) AS dominant_ship_revenue
            FROM (
                SELECT
                    method,
                    ship_revenue,
                    ROW_NUMBER() OVER (ORDER BY ship_revenue DESC, method) AS rn
                FROM ship_rollup
            ) ranked
        )
        SELECT
            current_base.*,
            prior_base.*,
            yoy_base.revenue_yoy,
            customer_summary.*,
            customer_health.*,
            customer_concentration.*,
            product_summary.product_count,
            product_concentration.*,
            supplier_summary.supplier_count,
            supplier_summary.top_supplier_revenue,
            shipping_summary.dominant_ship_method,
            shipping_summary.dominant_ship_revenue,
            ref.ref_date
        FROM current_base
        CROSS JOIN prior_base
        CROSS JOIN yoy_base
        CROSS JOIN customer_summary
        CROSS JOIN customer_health
        CROSS JOIN customer_concentration
        CROSS JOIN product_summary
        CROSS JOIN product_concentration
        CROSS JOIN supplier_summary
        CROSS JOIN shipping_summary
        CROSS JOIN ref
    """
    params = context["current_params"] + context["prior_params"] + context["yoy_params"]
    return fact_store.execute_sql_df(summary_sql, params, tag="regions.drilldown.summary")


def _region_drilldown_trend_frame(context: Dict[str, Any]) -> pd.DataFrame:
    trend_sql = f"""
        WITH current_scoped AS (
            {context["current_scoped_sql"]}
        ),
        prior_scoped AS (
            {context["prior_scoped_sql"]}
        ),
        monthly_current AS (
            SELECT
                strftime('%Y-%m', order_date) AS month,
                SUM(revenue) AS revenue,
                CASE WHEN SUM(cost) IS NULL THEN NULL ELSE SUM(revenue) - SUM(cost) END AS profit,
                CASE WHEN SUM(revenue) > 0 AND SUM(cost) IS NOT NULL THEN (SUM(revenue) - SUM(cost)) / SUM(revenue) * 100 ELSE NULL END AS margin_pct,
                COUNT(DISTINCT order_id) AS orders,
                COUNT(DISTINCT customer_id) AS customers
            FROM current_scoped
            GROUP BY 1
            ORDER BY 1
        ),
        monthly_prior AS (
            SELECT
                strftime('%Y-%m', order_date) AS month,
                SUM(revenue) AS revenue,
                CASE WHEN SUM(cost) IS NULL THEN NULL ELSE SUM(revenue) - SUM(cost) END AS profit,
                CASE WHEN SUM(revenue) > 0 AND SUM(cost) IS NOT NULL THEN (SUM(revenue) - SUM(cost)) / SUM(revenue) * 100 ELSE NULL END AS margin_pct,
                COUNT(DISTINCT order_id) AS orders,
                COUNT(DISTINCT customer_id) AS customers
            FROM prior_scoped
            GROUP BY 1
            ORDER BY 1
        ),
        weekly_current AS (
            SELECT
                strftime('%Y-%W', order_date) AS week,
                SUM(revenue) AS revenue
            FROM current_scoped
            GROUP BY 1
            ORDER BY 1
        ),
        weekday_current AS (
            SELECT
                strftime('%w', order_date) AS weekday,
                SUM(revenue) AS revenue,
                COUNT(DISTINCT order_id) AS orders,
                COUNT(DISTINCT customer_id) AS customers,
                CASE WHEN COUNT(DISTINCT order_id) > 0 THEN SUM(revenue) / COUNT(DISTINCT order_id) ELSE 0 END AS aov
            FROM current_scoped
            GROUP BY 1
            ORDER BY 1
        )
        SELECT
            (SELECT list(struct_pack(month:=month, revenue:=revenue, profit:=profit, margin_pct:=margin_pct, orders:=orders, customers:=customers)) FROM monthly_current) AS monthly_current,
            (SELECT list(struct_pack(month:=month, revenue:=revenue, profit:=profit, margin_pct:=margin_pct, orders:=orders, customers:=customers)) FROM monthly_prior) AS monthly_prior,
            (SELECT list(struct_pack(week:=week, revenue:=revenue)) FROM weekly_current) AS weekly_current,
            (SELECT list(struct_pack(weekday:=weekday, revenue:=revenue, orders:=orders, customers:=customers, aov:=aov)) FROM weekday_current) AS weekday_current
    """
    params = context["current_params"] + context["prior_params"]
    return fact_store.execute_sql_df(trend_sql, params, tag="regions.drilldown.trend")


def _region_drilldown_customer_frame(context: Dict[str, Any], tag: str = "regions.drilldown.customers") -> pd.DataFrame:
    customer_sql = f"""
        WITH current_scoped AS (
            {context["current_scoped_sql"]}
        ),
        prior_scoped AS (
            {context["prior_scoped_sql"]}
        ),
        ref AS (
            SELECT
                COALESCE(
                    (SELECT MAX(order_date) FROM current_scoped),
                    (SELECT MAX(order_date) FROM prior_scoped),
                    CURRENT_DATE
                ) AS ref_date
        ),
        current_cust AS (
            SELECT
                customer_id,
                COALESCE(MAX(customer_name), customer_id) AS customer_name,
                SUM(revenue) AS revenue_current,
                SUM(cost) AS cost_current,
                CASE WHEN SUM(cost) IS NULL THEN NULL ELSE SUM(revenue) - SUM(cost) END AS profit_current,
                COUNT(DISTINCT order_id) AS orders_current,
                MAX(order_date) AS last_order_current
            FROM current_scoped
            WHERE customer_id IS NOT NULL AND customer_id <> ''
            GROUP BY 1
        ),
        prior_cust AS (
            SELECT
                customer_id,
                COALESCE(MAX(customer_name), customer_id) AS customer_name,
                SUM(revenue) AS revenue_prior,
                SUM(cost) AS cost_prior,
                CASE WHEN SUM(cost) IS NULL THEN NULL ELSE SUM(revenue) - SUM(cost) END AS profit_prior,
                COUNT(DISTINCT order_id) AS orders_prior,
                MAX(order_date) AS last_order_prior
            FROM prior_scoped
            WHERE customer_id IS NOT NULL AND customer_id <> ''
            GROUP BY 1
        )
        SELECT
            COALESCE(c.customer_id, p.customer_id) AS customer_id,
            COALESCE(c.customer_name, p.customer_name) AS customer_name,
            COALESCE(c.revenue_current, 0) AS revenue_current,
            COALESCE(p.revenue_prior, 0) AS revenue_prior,
            c.cost_current AS cost_current,
            p.cost_prior AS cost_prior,
            c.profit_current AS profit_current,
            p.profit_prior AS profit_prior,
            COALESCE(c.orders_current, 0) AS orders_current,
            COALESCE(p.orders_prior, 0) AS orders_prior,
            c.last_order_current AS last_order_current,
            p.last_order_prior AS last_order_prior,
            COALESCE(c.last_order_current, p.last_order_prior) AS last_order_any,
            DATE_DIFF('day', COALESCE(c.last_order_current, p.last_order_prior), ref.ref_date) AS days_since_last
        FROM current_cust c
        FULL OUTER JOIN prior_cust p ON p.customer_id = c.customer_id
        CROSS JOIN ref
        WHERE COALESCE(c.customer_id, p.customer_id) IS NOT NULL
          AND COALESCE(c.customer_id, p.customer_id) <> ''
        ORDER BY COALESCE(c.revenue_current, 0) DESC, COALESCE(c.customer_name, p.customer_name)
    """
    params = context["current_params"] + context["prior_params"]
    return fact_store.execute_sql_df(customer_sql, params, tag=tag)


def _region_drilldown_product_frame(context: Dict[str, Any], tag: str = "regions.drilldown.products") -> pd.DataFrame:
    product_sql = f"""
        WITH current_scoped AS (
            {context["current_scoped_sql"]}
        ),
        prior_scoped AS (
            {context["prior_scoped_sql"]}
        ),
        current_prod AS (
            SELECT
                product_id,
                COALESCE(MAX(product_name), product_id) AS product_name,
                COALESCE(MAX(protein_family), MAX(product_category), 'Unassigned') AS protein_family,
                COALESCE(MAX(product_category), MAX(protein_family), 'Unassigned') AS product_category,
                SUM(revenue) AS revenue_current,
                SUM(base_cost) AS base_cost_current,
                SUM(cost) AS cost_current,
                CASE WHEN SUM(cost) IS NULL THEN NULL ELSE SUM(revenue) - SUM(cost) END AS profit_current,
                CASE WHEN SUM(revenue) > 0 AND SUM(cost) IS NOT NULL THEN (SUM(revenue) - SUM(cost)) / SUM(revenue) * 100 ELSE NULL END AS margin_pct_current,
                COUNT(DISTINCT order_id) AS orders_current,
                SUM(COALESCE(qty, 0)) AS qty_current,
                SUM(COALESCE(weight_lb, 0)) AS weight_lb_current
            FROM current_scoped
            WHERE product_id IS NOT NULL AND product_id <> ''
            GROUP BY 1
        ),
        prior_prod AS (
            SELECT
                product_id,
                COALESCE(MAX(product_name), product_id) AS product_name,
                COALESCE(MAX(protein_family), MAX(product_category), 'Unassigned') AS protein_family,
                COALESCE(MAX(product_category), MAX(protein_family), 'Unassigned') AS product_category,
                SUM(revenue) AS revenue_prior,
                SUM(base_cost) AS base_cost_prior,
                SUM(cost) AS cost_prior,
                CASE WHEN SUM(cost) IS NULL THEN NULL ELSE SUM(revenue) - SUM(cost) END AS profit_prior,
                CASE WHEN SUM(revenue) > 0 AND SUM(cost) IS NOT NULL THEN (SUM(revenue) - SUM(cost)) / SUM(revenue) * 100 ELSE NULL END AS margin_pct_prior,
                COUNT(DISTINCT order_id) AS orders_prior,
                SUM(COALESCE(qty, 0)) AS qty_prior,
                SUM(COALESCE(weight_lb, 0)) AS weight_lb_prior
            FROM prior_scoped
            WHERE product_id IS NOT NULL AND product_id <> ''
            GROUP BY 1
        )
        SELECT
            COALESCE(c.product_id, p.product_id) AS product_id,
            COALESCE(c.product_name, p.product_name) AS product_name,
            COALESCE(c.protein_family, p.protein_family, 'Unassigned') AS protein_family,
            COALESCE(c.product_category, p.product_category, 'Unassigned') AS product_category,
            COALESCE(c.revenue_current, 0) AS revenue_current,
            COALESCE(p.revenue_prior, 0) AS revenue_prior,
            c.base_cost_current AS base_cost_current,
            p.base_cost_prior AS base_cost_prior,
            c.cost_current AS cost_current,
            p.cost_prior AS cost_prior,
            c.profit_current AS profit_current,
            p.profit_prior AS profit_prior,
            c.margin_pct_current AS margin_pct_current,
            p.margin_pct_prior AS margin_pct_prior,
            COALESCE(c.orders_current, 0) AS orders_current,
            COALESCE(p.orders_prior, 0) AS orders_prior,
            COALESCE(c.qty_current, 0) AS qty_current,
            COALESCE(p.qty_prior, 0) AS qty_prior,
            COALESCE(c.weight_lb_current, 0) AS weight_lb_current,
            COALESCE(p.weight_lb_prior, 0) AS weight_lb_prior
        FROM current_prod c
        FULL OUTER JOIN prior_prod p ON p.product_id = c.product_id
        WHERE COALESCE(c.product_id, p.product_id) IS NOT NULL
          AND COALESCE(c.product_id, p.product_id) <> ''
        ORDER BY COALESCE(c.revenue_current, 0) DESC, COALESCE(c.product_name, p.product_name)
    """
    params = context["current_params"] + context["prior_params"]
    return fact_store.execute_sql_df(product_sql, params, tag=tag)


def _region_drilldown_ops_frame(context: Dict[str, Any]) -> pd.DataFrame:
    ops_sql = f"""
        WITH current_scoped AS (
            {context["current_scoped_sql"]}
        ),
        shipping_mix AS (
            SELECT
                ship_method AS method,
                SUM(revenue) AS revenue,
                COUNT(DISTINCT order_id) AS orders,
                CASE WHEN COUNT(DISTINCT order_id) > 0 THEN SUM(revenue) / COUNT(DISTINCT order_id) ELSE 0 END AS aov
            FROM current_scoped
            WHERE ship_method IS NOT NULL AND ship_method <> ''
            GROUP BY 1
            ORDER BY revenue DESC
        ),
        supplier_mix AS (
            SELECT
                COALESCE(NULLIF(supplier_id, ''), NULLIF(supplier_name, '')) AS supplier_id,
                COALESCE(NULLIF(supplier_name, ''), NULLIF(supplier_id, '')) AS supplier_name,
                SUM(revenue) AS revenue,
                CASE WHEN SUM(cost) IS NULL THEN NULL ELSE SUM(revenue) - SUM(cost) END AS profit,
                COUNT(DISTINCT order_id) AS orders
            FROM current_scoped
            WHERE COALESCE(NULLIF(supplier_id, ''), NULLIF(supplier_name, '')) IS NOT NULL
            GROUP BY 1, 2
            ORDER BY revenue DESC
        )
        SELECT
            (SELECT list(struct_pack(method:=method, revenue:=revenue, orders:=orders, aov:=aov)) FROM shipping_mix) AS shipping_mix,
            (SELECT list(struct_pack(supplier_id:=supplier_id, supplier_name:=supplier_name, revenue:=revenue, profit:=profit, orders:=orders)) FROM supplier_mix) AS supplier_mix
    """
    return fact_store.execute_sql_df(ops_sql, context["current_params"], tag="regions.drilldown.operations")


def _prepare_region_customer_rows(
    customer_df: pd.DataFrame,
    revenue_total: float,
    prior_revenue_total: float,
) -> list[dict[str, Any]]:
    if customer_df.empty:
        return []

    rows: list[dict[str, Any]] = []
    denominator_prior = prior_revenue_total if prior_revenue_total > 0 else revenue_total
    for rec in customer_df.to_dict(orient="records"):
        customer_id = str(rec.get("customer_id") or "").strip()
        if not customer_id:
            continue
        customer_name = str(rec.get("customer_name") or customer_id)
        revenue_current = round(_clean_float(rec.get("revenue_current"), 0.0), 2)
        revenue_prior = round(_clean_float(rec.get("revenue_prior"), 0.0), 2)
        profit_current = _clean_optional_float(rec.get("profit_current"))
        orders_current = _clean_int(rec.get("orders_current"))
        orders_prior = _clean_int(rec.get("orders_prior"))
        days_since_last = _clean_optional_float(rec.get("days_since_last"))
        last_order = _iso_date_value(rec.get("last_order_current") or rec.get("last_order_prior") or rec.get("last_order_any"))
        delta_revenue = round(revenue_current - revenue_prior, 2)
        delta_pct, delta_label, low_base = _mover_delta_meta(revenue_current, revenue_prior)
        delta_status, delta_label = _delta_status(delta_pct, delta_label)
        share_pct = _safe_pct(revenue_current, revenue_total)
        share_lost_pct = _safe_pct(revenue_prior, denominator_prior)
        repeat_pct = 100.0 if orders_current >= 2 else 0.0
        if revenue_current <= 0 and revenue_prior > 0:
            risk_level = "Lost"
        elif days_since_last is None:
            risk_level = "New" if revenue_current > 0 and revenue_prior <= 0 else "Active"
        elif days_since_last > CHURN_THRESHOLD_DAYS:
            risk_level = "Churned"
        elif days_since_last > 60:
            risk_level = "At risk"
        elif days_since_last > 30:
            risk_level = "Warming"
        elif revenue_current > 0 and revenue_prior <= 0:
            risk_level = "New"
        else:
            risk_level = "Active"
        rows.append(
            {
                "customer_id": customer_id,
                "customer_name": customer_name,
                "revenue": revenue_current,
                "revenue_current": revenue_current,
                "revenue_prior": revenue_prior,
                "profit": None if profit_current is None else round(profit_current, 2),
                "profit_current": None if profit_current is None else round(profit_current, 2),
                "orders": orders_current,
                "orders_current": orders_current,
                "orders_prior": orders_prior,
                "repeat_pct": round(repeat_pct, 2),
                "repeat_customer": bool(orders_current >= 2),
                "revenue_share_pct": None if share_pct is None else round(share_pct, 2),
                "delta_revenue": delta_revenue,
                "delta_revenue_pct": None if delta_pct is None else round(delta_pct, 2),
                "delta_revenue_label": delta_label,
                "delta_revenue_status": delta_status,
                "low_base": bool(low_base),
                "last_order": last_order,
                "days_since_last": None if days_since_last is None else int(days_since_last),
                "risk_level": risk_level,
                "prior_revenue_window": revenue_prior,
                "region_revenue_share_lost_pct": None if share_lost_pct is None else round(share_lost_pct, 2),
            }
        )

    rows.sort(key=lambda item: (float(item.get("revenue_current") or 0.0), item.get("customer_name") or ""), reverse=True)
    return rows


def _prepare_region_product_rows(product_df: pd.DataFrame, revenue_total: float) -> list[dict[str, Any]]:
    if product_df.empty:
        return []

    rows: list[dict[str, Any]] = []
    for rec in product_df.to_dict(orient="records"):
        product_id = str(rec.get("product_id") or "").strip()
        if not product_id:
            continue
        product_name = str(rec.get("product_name") or product_id)
        protein_family = str(rec.get("protein_family") or "Unassigned")
        product_category = str(rec.get("product_category") or protein_family or "Unassigned")
        revenue_current = round(_clean_float(rec.get("revenue_current"), 0.0), 2)
        revenue_prior = round(_clean_float(rec.get("revenue_prior"), 0.0), 2)
        base_cost_current = _clean_optional_float(rec.get("base_cost_current"))
        cost_current = _clean_optional_float(rec.get("cost_current"))
        profit_current = _clean_optional_float(rec.get("profit_current"))
        margin_current = _clean_optional_float(rec.get("margin_pct_current"))
        margin_prior = _clean_optional_float(rec.get("margin_pct_prior"))
        qty_current = _clean_float(rec.get("qty_current"), 0.0)
        weight_current = _clean_float(rec.get("weight_lb_current"), 0.0)
        orders_current = _clean_int(rec.get("orders_current"))
        delta_revenue = round(revenue_current - revenue_prior, 2)
        delta_pct, delta_label, low_base = _mover_delta_meta(revenue_current, revenue_prior)
        delta_status, delta_label = _delta_status(delta_pct, delta_label)
        share_pct = _safe_pct(revenue_current, revenue_total)
        revenue_per_unit = (revenue_current / qty_current) if qty_current > 0 else None
        if margin_current is None and revenue_current > 0:
            risk_tag = "Cost gap"
        elif margin_current is not None and margin_current < 15.0:
            risk_tag = "Margin risk"
        elif delta_status == "decliner":
            risk_tag = "Declining"
        elif delta_status == "new":
            risk_tag = "New"
        elif delta_status == "gainer" and (share_pct or 0.0) < 5.0:
            risk_tag = "Opportunity"
        else:
            risk_tag = "Core"
        margin_delta_pp = None
        if margin_current is not None and margin_prior is not None:
            margin_delta_pp = round(margin_current - margin_prior, 2)
        rows.append(
            {
                "product_id": product_id,
                "product_name": product_name,
                "protein_family": protein_family,
                "product_category": product_category,
                "revenue": revenue_current,
                "revenue_current": revenue_current,
                "revenue_prior": revenue_prior,
                "base_cost": None if base_cost_current is None else round(base_cost_current, 2),
                "cost": None if cost_current is None else round(cost_current, 2),
                "effective_cost_basis": None if cost_current is None else round(cost_current, 2),
                "profit": None if profit_current is None else round(profit_current, 2),
                "profit_current": None if profit_current is None else round(profit_current, 2),
                "margin_pct": None if margin_current is None else round(margin_current, 2),
                "margin_pct_current": None if margin_current is None else round(margin_current, 2),
                "margin_pct_prior": None if margin_prior is None else round(margin_prior, 2),
                "margin_delta_pp": margin_delta_pp,
                "orders": orders_current,
                "orders_current": orders_current,
                "qty": round(qty_current, 2),
                "qty_current": round(qty_current, 2),
                "weight_lb": round(weight_current, 2),
                "weight_lb_current": round(weight_current, 2),
                "cost_lb": None if cost_current is None or weight_current <= 0 else round(cost_current / weight_current, 4),
                "effective_cost_lb": None if cost_current is None or weight_current <= 0 else round(cost_current / weight_current, 4),
                "revenue_per_unit": None if revenue_per_unit is None else round(revenue_per_unit, 2),
                "revenue_share_pct": None if share_pct is None else round(share_pct, 2),
                "delta_revenue": delta_revenue,
                "delta_revenue_pct": None if delta_pct is None else round(delta_pct, 2),
                "delta_revenue_label": delta_label,
                "delta_revenue_status": delta_status,
                "low_base": bool(low_base),
                "risk_tag": risk_tag,
            }
        )

    rows = margin_rules.annotate_margin_rows(
        rows,
        protein_keys=("protein_family",),
        category_keys=("product_category",),
        revenue_key="revenue",
        cost_key="cost",
        profit_key="profit",
        margin_key="margin_pct",
        unit_cost_key="cost_lb",
    )
    for row in rows:
        margin_current = _clean_optional_float(row.get("margin_pct"))
        status_key = str(row.get("status_key") or "").strip().lower()
        if margin_current is None and _clean_float(row.get("revenue"), 0.0) > 0:
            row["risk_tag"] = "Cost gap"
        elif status_key in {"red", "orange"}:
            row["risk_tag"] = "Margin risk"
        elif row.get("delta_revenue_status") == "decliner":
            row["risk_tag"] = "Declining"
        elif row.get("delta_revenue_status") == "new":
            row["risk_tag"] = "New"
        elif row.get("delta_revenue_status") == "gainer" and (_clean_float(row.get("revenue_share_pct"), 0.0) < 5.0):
            row["risk_tag"] = "Opportunity"
        else:
            row["risk_tag"] = "Core"

    rows.sort(key=lambda item: (float(item.get("revenue_current") or 0.0), item.get("product_name") or ""), reverse=True)
    return rows


def _build_region_drilldown_insights(
    scorecard: Dict[str, Any],
    customers_payload: Dict[str, Any],
    products_payload: Dict[str, Any],
    operations_payload: Dict[str, Any],
) -> list[dict[str, Any]]:
    revenue_delta = _clean_float(scorecard.get("revenue_delta_window"), 0.0)
    new_customer_share = _clean_float(scorecard.get("new_customer_share_pct"), 0.0)
    top_customer_share = _clean_float(scorecard.get("top_customer_share_pct"), 0.0)
    top_product_share = _clean_float(scorecard.get("top_product_share_pct"), 0.0)
    churn_pct = _clean_float(scorecard.get("churn_pct"), 0.0)
    at_risk_customers = _clean_int(scorecard.get("at_risk_customers"))
    cost_coverage = _clean_optional_float(scorecard.get("cost_coverage_pct"))
    dominant_ship_method = str(scorecard.get("dominant_ship_method") or "").strip()
    dominant_ship_share = _clean_float(scorecard.get("dominant_ship_share_pct"), 0.0)
    margin_risk_count = _clean_int((products_payload.get("summary") or {}).get("margin_risk_count"))

    if abs(revenue_delta) < 1e-9:
        changed = "Revenue is stable versus the immediately preceding window."
        changed_tone = "neutral"
    elif revenue_delta > 0 and new_customer_share >= 20.0:
        changed = f"Revenue is up {revenue_delta:,.0f}, with new customers driving {new_customer_share:.1f}% of current revenue."
        changed_tone = "positive"
    elif revenue_delta > 0:
        changed = f"Revenue is up {revenue_delta:,.0f} versus the prior window."
        changed_tone = "positive"
    else:
        changed = f"Revenue is down {abs(revenue_delta):,.0f} versus the prior window."
        changed_tone = "warning"

    if top_customer_share >= 35.0 or top_product_share >= 25.0:
        risk = (
            f"Concentration is elevated: top customer share is {top_customer_share:.1f}% and "
            f"top product share is {top_product_share:.1f}%."
        )
        risk_tone = "warning"
    elif churn_pct >= 15.0 or at_risk_customers >= 3:
        risk = f"Customer health needs attention: churn is {churn_pct:.1f}% with {at_risk_customers} customers already at risk."
        risk_tone = "warning"
    elif cost_coverage is not None and cost_coverage < 90.0:
        risk = f"Data quality is limiting confidence: cost coverage is {cost_coverage:.1f}%."
        risk_tone = "warning"
    else:
        risk = "Risk profile is controlled across customer health, concentration, and coverage."
        risk_tone = "neutral"

    if margin_risk_count > 0:
        next_step = f"Review the {margin_risk_count} margin-risk products and protect profitable mix before pushing growth."
    elif dominant_ship_method:
        next_step = f"Validate operating dependency on {dominant_ship_method}, which carries {dominant_ship_share:.1f}% of region revenue."
    else:
        next_step = "Use customer and product movers to confirm which accounts and SKUs are driving the next action."

    return [
        {"title": "What changed", "text": changed, "tone": changed_tone},
        {"title": "What is risky", "text": risk, "tone": risk_tone},
        {"title": "What to do next", "text": next_step, "tone": "action"},
    ]


def build_regions_drilldown(region_id: str, filters: Any, scope: Dict[str, Any], args: Any) -> Dict[str, Any]:
    context = _region_drilldown_context(region_id, filters, scope)
    if context.get("error"):
        return context

    summary_df = _region_drilldown_summary_frame(context)
    trend_df = _region_drilldown_trend_frame(context)
    customer_df = _region_drilldown_customer_frame(context)
    product_df = _region_drilldown_product_frame(context)
    ops_df = _region_drilldown_ops_frame(context)

    top_n = max(10, min(_parse_top_n(args, default=25), 250))
    summary_row = summary_df.iloc[0].to_dict() if not summary_df.empty else {}
    trend_row = trend_df.iloc[0].to_dict() if not trend_df.empty else {}
    ops_row = ops_df.iloc[0].to_dict() if not ops_df.empty else {}

    current_revenue = round(_clean_float(summary_row.get("revenue_current"), 0.0), 2)
    prior_revenue = round(_clean_float(summary_row.get("revenue_prior"), 0.0), 2)
    yoy_revenue_raw = _clean_optional_float(summary_row.get("revenue_yoy"))
    profit_current = _clean_optional_float(summary_row.get("profit_current"))
    margin_current = _clean_optional_float(summary_row.get("margin_pct_current"))
    minimum_margin_current = _clean_optional_float(summary_row.get("minimum_margin_pct_current"))
    target_margin_current = _clean_optional_float(summary_row.get("target_margin_pct_current"))
    orders_current = _clean_int(summary_row.get("orders_current"))
    customers_current = _clean_int(summary_row.get("customers_current"))
    qty_current = _clean_float(summary_row.get("qty_current"), 0.0)
    weight_lb_current = _clean_float(summary_row.get("weight_lb_current"), 0.0)
    rows_current = _clean_int(summary_row.get("rows_current"))
    rows_with_cost_current = _clean_int(summary_row.get("rows_with_cost_current"))
    missing_cost_revenue = round(_clean_float(summary_row.get("missing_cost_revenue_current"), 0.0), 2)
    missing_packs_rows = _clean_int(summary_row.get("missing_packs_rows_current"))
    aov = (current_revenue / orders_current) if orders_current > 0 else 0.0
    revenue_per_customer = (current_revenue / customers_current) if customers_current > 0 else None
    profit_per_order = (profit_current / orders_current) if (profit_current is not None and orders_current > 0) else None
    revenue_per_unit = (current_revenue / qty_current) if qty_current > 0 else None
    revenue_per_lb = (current_revenue / weight_lb_current) if weight_lb_current > 0 else None
    asp = revenue_per_unit

    monthly_current = _struct_list(trend_row.get("monthly_current"))
    monthly_prior = _struct_list(trend_row.get("monthly_prior"))
    weekly_current = _struct_list(trend_row.get("weekly_current"))
    weekday_current = _struct_list(trend_row.get("weekday_current"))

    trend_labels = [str(item.get("month")) for item in monthly_current if item.get("month") is not None]
    trend_revenue = [_clean_float(item.get("revenue"), 0.0) for item in monthly_current]
    trend_profit = [item.get("profit") if item.get("profit") is None else _clean_float(item.get("profit"), 0.0) for item in monthly_current]
    trend_margin = [item.get("margin_pct") if item.get("margin_pct") is None else _clean_float(item.get("margin_pct"), 0.0) for item in monthly_current]
    trend_orders = [_clean_int(item.get("orders")) for item in monthly_current]
    trend_customers = [_clean_int(item.get("customers")) for item in monthly_current]

    prior_labels = [str(item.get("month")) for item in monthly_prior if item.get("month") is not None]
    prior_revenue_series = [_clean_float(item.get("revenue"), 0.0) for item in monthly_prior]
    prior_profit_series = [item.get("profit") if item.get("profit") is None else _clean_float(item.get("profit"), 0.0) for item in monthly_prior]
    prior_margin_series = [item.get("margin_pct") if item.get("margin_pct") is None else _clean_float(item.get("margin_pct"), 0.0) for item in monthly_prior]
    prior_orders_series = [_clean_int(item.get("orders")) for item in monthly_prior]
    prior_customers_series = [_clean_int(item.get("customers")) for item in monthly_prior]
    weekly_values = [_clean_float(item.get("revenue"), 0.0) for item in weekly_current]

    mom_growth, _unused_legacy_yoy = _calc_growth(trend_labels, trend_revenue)
    wow_growth = _calc_wow(weekly_values)
    yoy_growth = None
    if yoy_revenue_raw is not None and not math.isnan(yoy_revenue_raw) and abs(yoy_revenue_raw) > 1e-9:
        yoy_growth = round((current_revenue - yoy_revenue_raw) / abs(yoy_revenue_raw) * 100.0, 2)

    repeat_customers = _clean_int(summary_row.get("repeat_customers"))
    repeat_pct = _safe_pct(repeat_customers, customers_current)
    new_customers = _clean_int(summary_row.get("new_customers"))
    returning_customers = _clean_int(summary_row.get("returning_customers"))
    lost_customers = _clean_int(summary_row.get("lost_customers"))
    new_customer_revenue = round(_clean_float(summary_row.get("new_customer_revenue"), 0.0), 2)
    returning_customer_revenue = round(_clean_float(summary_row.get("returning_customer_revenue"), 0.0), 2)
    lost_customer_revenue = round(_clean_float(summary_row.get("lost_customer_revenue"), 0.0), 2)
    new_customer_share_pct = _safe_pct(new_customer_revenue, current_revenue)
    returning_customer_share_pct = _safe_pct(returning_customer_revenue, current_revenue)
    active_customers_30d = _clean_int(summary_row.get("active_customers_30d"))
    warming_customers = _clean_int(summary_row.get("warming_customers"))
    at_risk_customers = _clean_int(summary_row.get("at_risk_customers"))
    churned_customers = _clean_int(summary_row.get("churned_customers"))
    at_risk_revenue = round(_clean_float(summary_row.get("at_risk_revenue"), 0.0), 2)
    churned_revenue = round(_clean_float(summary_row.get("churned_revenue"), 0.0), 2)
    active_customers_90d = active_customers_30d + warming_customers + at_risk_customers
    churn_pct = _safe_pct(churned_customers, customers_current)

    top_customer_share_pct = _safe_pct(summary_row.get("top_customer_revenue"), current_revenue)
    top_customer_top5_share_pct = _safe_pct(summary_row.get("top5_customer_revenue"), current_revenue)
    top_customer_top10_share_pct = _safe_pct(summary_row.get("top10_customer_revenue"), current_revenue)
    customer_hhi = _clean_optional_float(summary_row.get("customer_hhi"))
    top_product_share_pct = _safe_pct(summary_row.get("top_product_revenue"), current_revenue)
    top_product_top5_share_pct = _safe_pct(summary_row.get("top5_product_revenue"), current_revenue)
    top_product_top10_share_pct = _safe_pct(summary_row.get("top10_product_revenue"), current_revenue)
    product_hhi = _clean_optional_float(summary_row.get("product_hhi"))
    product_count = _clean_int(summary_row.get("product_count"))
    supplier_count = _clean_int(summary_row.get("supplier_count"))
    top_supplier_share_pct = _safe_pct(summary_row.get("top_supplier_revenue"), current_revenue)
    dominant_ship_method = summary_row.get("dominant_ship_method")
    dominant_ship_share_pct = _safe_pct(summary_row.get("dominant_ship_revenue"), current_revenue)
    margin_status = margin_rules.classify_margin_status(margin_current, minimum_margin_current, target_margin_current)

    cost_coverage_pct = _safe_pct(rows_with_cost_current, rows_current)
    packs_coverage_pct = None
    if context.get("has_missing_packs") and rows_current > 0:
        packs_coverage_pct = round((rows_current - missing_packs_rows) / rows_current * 100.0, 2)

    window_delta_revenue = round(current_revenue - prior_revenue, 2)
    window_delta_pct = None if prior_revenue == 0 else round(window_delta_revenue / abs(prior_revenue) * 100.0, 2)
    customer_health_warning = bool((churn_pct or 0.0) >= 15.0 or at_risk_customers >= max(3, int(customers_current * 0.2)))
    quality_flag = _quality_flag(
        {
            "cost_coverage_pct": cost_coverage_pct,
            "packs_coverage_pct": packs_coverage_pct,
            "missing_cost_revenue": missing_cost_revenue,
        }
    )

    customer_rows_all = _prepare_region_customer_rows(customer_df, current_revenue, prior_revenue)
    product_rows_all = _prepare_region_product_rows(product_df, current_revenue)

    active_customer_rows = [row for row in customer_rows_all if _clean_float(row.get("revenue_current"), 0.0) > 0]
    top_customer_rows = active_customer_rows[:top_n]
    customer_profit_rows = sorted(
        active_customer_rows,
        key=lambda item: float(item.get("profit_current") or float("-inf")),
        reverse=True,
    )[:top_n]
    customer_movers = sorted(
        customer_rows_all,
        key=lambda item: abs(float(item.get("delta_revenue") or 0.0)),
        reverse=True,
    )[:top_n]

    active_product_rows = [row for row in product_rows_all if _clean_float(row.get("revenue_current"), 0.0) > 0]
    top_product_rows = active_product_rows[:top_n]
    top_product_profit_rows = sorted(
        active_product_rows,
        key=lambda item: float(item.get("profit_current") or float("-inf")),
        reverse=True,
    )[:top_n]
    margin_risk_rows = [
        row for row in active_product_rows if str(row.get("risk_tag") or "").lower() in {"margin risk", "cost gap"}
    ][:top_n]
    product_movers = sorted(
        product_rows_all,
        key=lambda item: abs(float(item.get("delta_revenue") or 0.0)),
        reverse=True,
    )[:top_n]

    shipping_mix = _struct_list(ops_row.get("shipping_mix"))
    shipping_total_revenue = sum(_clean_float(item.get("revenue"), 0.0) for item in shipping_mix)
    shipping_total_orders = sum(_clean_int(item.get("orders")) for item in shipping_mix)
    shipping_mix_rows = [
        {
            "method": item.get("method"),
            "revenue": round(_clean_float(item.get("revenue"), 0.0), 2),
            "orders": _clean_int(item.get("orders")),
            "aov": round(_clean_float(item.get("aov"), 0.0), 2),
            "pct": _safe_pct(item.get("revenue"), shipping_total_revenue),
            "orders_pct": _safe_pct(item.get("orders"), shipping_total_orders),
        }
        for item in shipping_mix
        if str(item.get("method") or "").strip()
    ]

    supplier_mix = _struct_list(ops_row.get("supplier_mix"))
    supplier_total_revenue = sum(_clean_float(item.get("revenue"), 0.0) for item in supplier_mix)
    supplier_mix_rows = [
        {
            "supplier_id": item.get("supplier_id"),
            "supplier_name": item.get("supplier_name") or item.get("supplier_id"),
            "revenue": round(_clean_float(item.get("revenue"), 0.0), 2),
            "profit": None if item.get("profit") is None else round(_clean_float(item.get("profit"), 0.0), 2),
            "orders": _clean_int(item.get("orders")),
            "pct": _safe_pct(item.get("revenue"), supplier_total_revenue),
        }
        for item in supplier_mix
        if str(item.get("supplier_id") or item.get("supplier_name") or "").strip()
    ]

    weekday_rows = [
        {
            "weekday": str(item.get("weekday")),
            "label": _WEEKDAY_LABELS.get(str(item.get("weekday")), str(item.get("weekday"))),
            "revenue": round(_clean_float(item.get("revenue"), 0.0), 2),
            "orders": _clean_int(item.get("orders")),
            "customers": _clean_int(item.get("customers")),
            "aov": round(_clean_float(item.get("aov"), 0.0), 2),
        }
        for item in weekday_current
    ]
    weekday_rows.sort(key=lambda item: item.get("weekday") or "")
    best_weekday = max(weekday_rows, key=lambda item: float(item.get("revenue") or 0.0), default=None)
    weakest_weekday = min(weekday_rows, key=lambda item: float(item.get("revenue") or 0.0), default=None) if weekday_rows else None

    retention_rows = [
        row
        for row in customer_rows_all
        if str(row.get("risk_level") or "").strip().lower() in {"lost", "churned", "at risk", "warming"}
    ]
    retention_rows.sort(
        key=lambda item: (
            _risk_order(str(item.get("risk_level") or "")),
            float(item.get("revenue_prior") or 0.0),
            float(item.get("revenue_current") or 0.0),
        ),
        reverse=True,
    )

    customer_revenue_values = [float(row.get("revenue_current") or 0.0) for row in active_customer_rows]
    product_revenue_values = [float(row.get("revenue_current") or 0.0) for row in active_product_rows]
    customer_summary = {
        "top1_share_pct": None if top_customer_share_pct is None else round(top_customer_share_pct, 2),
        "top5_share_pct": None if top_customer_top5_share_pct is None else round(top_customer_top5_share_pct, 2),
        "top10_share_pct": None if top_customer_top10_share_pct is None else round(top_customer_top10_share_pct, 2),
        "hhi": None if customer_hhi is None else round(customer_hhi, 2),
        "count": len(active_customer_rows),
    }
    product_summary = {
        "top1_share_pct": None if top_product_share_pct is None else round(top_product_share_pct, 2),
        "top5_share_pct": None if top_product_top5_share_pct is None else round(top_product_top5_share_pct, 2),
        "top10_share_pct": None if top_product_top10_share_pct is None else round(top_product_top10_share_pct, 2),
        "hhi": None if product_hhi is None else round(product_hhi, 2),
        "count": product_count,
        "margin_risk_count": len([row for row in active_product_rows if str(row.get("risk_tag") or "").lower() in {"margin risk", "cost gap"}]),
    }

    scorecard = {
        "region_name": str(region_id),
        "total_revenue": current_revenue,
        "prior_revenue": prior_revenue,
        "revenue_delta_window": window_delta_revenue,
        "revenue_delta_window_pct": window_delta_pct,
        "total_profit": None if profit_current is None else round(profit_current, 2),
        "margin_pct": None if margin_current is None else round(margin_current, 2),
        "minimum_margin_pct": None if minimum_margin_current is None else round(minimum_margin_current, 2),
        "target_margin_pct": None if target_margin_current is None else round(target_margin_current, 2),
        "target_gap_pct_points": None if margin_current is None or target_margin_current is None else round(margin_current - target_margin_current, 2),
        "orders": orders_current,
        "customers": customers_current,
        "avg_order_value": round(aov, 2),
        "revenue_per_customer": None if revenue_per_customer is None else round(revenue_per_customer, 2),
        "profit_per_order": None if profit_per_order is None else round(profit_per_order, 2),
        "revenue_per_unit": None if revenue_per_unit is None else round(revenue_per_unit, 2),
        "revenue_per_lb": None if revenue_per_lb is None else round(revenue_per_lb, 2),
        "asp": None if asp is None else round(asp, 2),
        "repeat_pct": None if repeat_pct is None else round(repeat_pct, 2),
        "churn_pct": None if churn_pct is None else round(churn_pct, 2),
        "new_customer_share_pct": None if new_customer_share_pct is None else round(new_customer_share_pct, 2),
        "returning_customer_share_pct": None if returning_customer_share_pct is None else round(returning_customer_share_pct, 2),
        "new_customers": new_customers,
        "returning_customers": returning_customers,
        "lost_customers": lost_customers,
        "lost_customer_revenue": lost_customer_revenue,
        "at_risk_customers": at_risk_customers,
        "at_risk_revenue": at_risk_revenue,
        "churned_customers": churned_customers,
        "churned_revenue": churned_revenue,
        "active_customers_30d": active_customers_30d,
        "active_customers_90d": active_customers_90d,
        "warming_customers": warming_customers,
        "mom_growth": None if mom_growth is None else round(mom_growth, 2),
        "wow_growth": None if wow_growth is None else round(wow_growth, 2),
        "yoy_growth": yoy_growth,
        "top_customer_share_pct": None if top_customer_share_pct is None else round(top_customer_share_pct, 2),
        "top_customer_top5_share_pct": None if top_customer_top5_share_pct is None else round(top_customer_top5_share_pct, 2),
        "customer_hhi": None if customer_hhi is None else round(customer_hhi, 2),
        "top_product_share_pct": None if top_product_share_pct is None else round(top_product_share_pct, 2),
        "top_product_top5_share_pct": None if top_product_top5_share_pct is None else round(top_product_top5_share_pct, 2),
        "top_product_top10_share_pct": None if top_product_top10_share_pct is None else round(top_product_top10_share_pct, 2),
        "product_hhi": None if product_hhi is None else round(product_hhi, 2),
        "supplier_count": supplier_count,
        "top_supplier_share_pct": None if top_supplier_share_pct is None else round(top_supplier_share_pct, 2),
        "dominant_ship_method": dominant_ship_method,
        "dominant_ship_share_pct": None if dominant_ship_share_pct is None else round(dominant_ship_share_pct, 2),
        "product_count": product_count,
        "cost_coverage_pct": None if cost_coverage_pct is None else round(cost_coverage_pct, 2),
        "packs_coverage_pct": packs_coverage_pct,
        "missing_cost_revenue": missing_cost_revenue,
        "data_quality_flag": quality_flag,
        "customer_health_warning": customer_health_warning,
        "rows_current": rows_current,
        "ref_date": _iso_date_value(summary_row.get("ref_date")),
        **margin_status,
    }

    kpis = {
        "revenue": current_revenue,
        "profit": scorecard.get("total_profit"),
        "margin_pct": scorecard.get("margin_pct"),
        "minimum_margin_pct": scorecard.get("minimum_margin_pct"),
        "target_margin_pct": scorecard.get("target_margin_pct"),
        "target_gap_pct_points": scorecard.get("target_gap_pct_points"),
        "status_key": scorecard.get("status_key"),
        "target_status": scorecard.get("target_status"),
        "status_color": scorecard.get("status_color"),
        "orders": orders_current,
        "customers": customers_current,
        "avg_order_value": scorecard.get("avg_order_value"),
        "repeat_pct": scorecard.get("repeat_pct"),
        "churn_pct": scorecard.get("churn_pct"),
        "days_since_last_order": None if scorecard.get("ref_date") is None else None,
        "mom_growth": scorecard.get("mom_growth"),
        "yoy_growth": scorecard.get("yoy_growth"),
        "wow_growth": scorecard.get("wow_growth"),
        "start": context.get("start_iso"),
        "end": context.get("end_iso"),
        "revenue_per_customer": scorecard.get("revenue_per_customer"),
        "profit_per_order": scorecard.get("profit_per_order"),
        "revenue_per_unit": scorecard.get("revenue_per_unit"),
        "revenue_per_lb": scorecard.get("revenue_per_lb"),
        "new_customer_share_pct": scorecard.get("new_customer_share_pct"),
        "revenue_delta_prior": scorecard.get("revenue_delta_window"),
        "revenue_delta_prior_pct": scorecard.get("revenue_delta_window_pct"),
        "top_customer_share_pct": scorecard.get("top_customer_share_pct"),
        "top_product_share_pct": scorecard.get("top_product_share_pct"),
        "cost_coverage_pct": scorecard.get("cost_coverage_pct"),
        "packs_coverage_pct": scorecard.get("packs_coverage_pct"),
        "data_quality_flag": quality_flag,
    }

    trend_payload = {
        "labels": trend_labels,
        "revenue": trend_revenue,
        "profit": trend_profit,
        "margin_pct": trend_margin,
        "orders": trend_orders,
        "customers": trend_customers,
        "prior_labels": prior_labels,
        "prior_revenue": prior_revenue_series,
        "prior_profit": prior_profit_series,
        "prior_margin_pct": prior_margin_series,
        "prior_orders": prior_orders_series,
        "prior_customers": prior_customers_series,
    }

    top_customers_legacy = [
        {
            "customer_id": row.get("customer_id"),
            "customer_name": row.get("customer_name"),
            "revenue": row.get("revenue_current"),
            "profit": row.get("profit_current"),
            "orders": row.get("orders_current"),
            "repeat_pct": row.get("repeat_pct"),
            "revenue_share_pct": row.get("revenue_share_pct"),
            "last_order": row.get("last_order"),
            "delta_revenue": row.get("delta_revenue"),
            "delta_revenue_pct": row.get("delta_revenue_pct"),
            "delta_revenue_label": row.get("delta_revenue_label"),
            "delta_revenue_status": row.get("delta_revenue_status"),
        }
        for row in top_customer_rows
    ]

    top_products_legacy = [
        {
            "product_id": row.get("product_id"),
            "product_name": row.get("product_name"),
            "revenue": row.get("revenue_current"),
            "profit": row.get("profit_current"),
            "margin_pct": row.get("margin_pct_current"),
            "orders": row.get("orders_current"),
            "qty": row.get("qty_current"),
            "revenue_share_pct": row.get("revenue_share_pct"),
            "delta_revenue": row.get("delta_revenue"),
            "delta_revenue_pct": row.get("delta_revenue_pct"),
            "delta_revenue_label": row.get("delta_revenue_label"),
            "delta_revenue_status": row.get("delta_revenue_status"),
            "risk_tag": row.get("risk_tag"),
        }
        for row in top_product_rows
    ]

    table_rows = [
        {
            "key": row.get("customer_id"),
            "label": row.get("customer_name") or row.get("customer_id"),
            "revenue": row.get("revenue_current"),
            "profit": row.get("profit_current"),
        }
        for row in top_customer_rows
    ]

    retention_buckets = [
        {"label": "Active", "count": active_customers_30d, "revenue": current_revenue - at_risk_revenue - churned_revenue},
        {"label": "Warming", "count": warming_customers, "revenue": None},
        {"label": "At risk", "count": at_risk_customers, "revenue": at_risk_revenue},
        {"label": "Churned", "count": churned_customers, "revenue": churned_revenue},
        {"label": "Lost", "count": lost_customers, "revenue": lost_customer_revenue},
    ]

    region_v2 = {
        "window": {
            "start": context.get("start_iso"),
            "end": context.get("end_iso"),
            "prior_start": context.get("windows", {}).get("prior_start"),
            "prior_end": context.get("windows", {}).get("prior_end"),
            "yoy_start": context.get("windows", {}).get("yoy_start"),
            "yoy_end": context.get("windows", {}).get("yoy_end"),
            "has_prior_period": bool(prior_revenue > 0),
            "has_yoy_period": bool(yoy_revenue_raw not in (None, 0)),
        },
        "scorecard": scorecard,
        "trend": {
            **trend_payload,
            "comparison_note": "Current window vs immediately preceding equivalent window.",
        },
        "customers": {
            "top_rows": top_customer_rows,
            "profit_rows": customer_profit_rows,
            "movers": customer_movers,
            "concentration": {
                **customer_summary,
                "hhi": _hhi_pct(customer_revenue_values, current_revenue) if current_revenue > 0 else customer_summary.get("hhi"),
            },
            "summary": {
                "new_customers": new_customers,
                "returning_customers": returning_customers,
                "lost_customers": lost_customers,
                "new_customer_share_pct": None if new_customer_share_pct is None else round(new_customer_share_pct, 2),
                "returning_customer_share_pct": None if returning_customer_share_pct is None else round(returning_customer_share_pct, 2),
            },
            "top_n": top_n,
        },
        "products": {
            "top_rows": top_product_rows,
            "profit_rows": top_product_profit_rows,
            "movers": product_movers,
            "margin_risk_rows": margin_risk_rows,
            "concentration": {
                **product_summary,
                "hhi": _hhi_pct(product_revenue_values, current_revenue) if current_revenue > 0 else product_summary.get("hhi"),
            },
            "summary": product_summary,
            "top_n": top_n,
        },
        "retention": {
            "summary": {
                "repeat_pct": None if repeat_pct is None else round(repeat_pct, 2),
                "churn_pct": None if churn_pct is None else round(churn_pct, 2),
                "at_risk_customers": at_risk_customers,
                "at_risk_revenue": at_risk_revenue,
                "lost_customers": lost_customers,
                "lost_customer_revenue": lost_customer_revenue,
                "active_customers_30d": active_customers_30d,
                "active_customers_90d": active_customers_90d,
                "new_customer_share_pct": None if new_customer_share_pct is None else round(new_customer_share_pct, 2),
                "returning_customer_share_pct": None if returning_customer_share_pct is None else round(returning_customer_share_pct, 2),
            },
            "buckets": retention_buckets,
            "rows": retention_rows[:250],
        },
        "operations": {
            "shipping_mix": shipping_mix_rows,
            "supplier_mix": supplier_mix_rows[:25],
            "weekday": weekday_rows,
            "best_weekday": best_weekday,
            "weakest_weekday": weakest_weekday,
        },
        "insights": _build_region_drilldown_insights(
            scorecard,
            {"summary": customer_summary},
            {"summary": product_summary},
            {"shipping_mix": shipping_mix_rows},
        ),
    }

    payload = {
        "kpis": kpis,
        "trend": trend_payload,
        "table": {
            "rows": table_rows,
            "page": 1,
            "page_size": len(table_rows),
            "total": len(table_rows),
            "sort_by": "revenue",
            "sort_dir": "desc",
        },
        "charts": {
            "top_customers": top_customers_legacy,
            "top_products": top_products_legacy,
            "shipping_mix": shipping_mix_rows,
            "weekly_revenue": weekly_current,
            "weekday_revenue": weekday_rows,
            "churned_customers": retention_rows[:250],
            "trend": trend_payload,
        },
        "region_v2": region_v2,
        "meta": {
            "page_id": "region_drilldown",
            "entity_id": region_id,
            "entity_label": region_id,
            "freshness": context.get("freshness") or _freshness_meta(),
            "last_refresh": (context.get("freshness") or {}).get("date_max"),
        },
    }
    return payload


def build_region_drilldown_export_frames(
    region_id: str,
    filters: Any,
    scope: Dict[str, Any],
    args: Any,
) -> tuple[dict[str, pd.DataFrame], Dict[str, Any]]:
    args_payload: dict[str, Any] = {}
    if hasattr(args, "to_dict"):
        try:
            args_payload = args.to_dict(flat=True)  # type: ignore[assignment]
        except Exception:
            args_payload = {}
    elif isinstance(args, dict):
        args_payload = dict(args)
    args_payload["top_n"] = str(max(50, _parse_top_n(args, default=250)))
    args_payload["drilldown_v2"] = "1"
    args_payload["region_drilldown_v2"] = "1"

    payload = build_regions_drilldown(str(region_id), filters, scope, args_payload)
    if not isinstance(payload, dict) or payload.get("error"):
        raise ValueError((payload.get("error") or {}).get("message") if isinstance(payload, dict) else "Export unavailable")
    region_v2 = payload.get("region_v2") or {}
    if not isinstance(region_v2, dict):
        region_v2 = {}

    scorecard = region_v2.get("scorecard") or {}
    trend = region_v2.get("trend") or {}
    operations = region_v2.get("operations") or {}
    retention = region_v2.get("retention") or {}
    window = region_v2.get("window") or {}

    context = _region_drilldown_context(region_id, filters, scope)
    if context.get("error"):
        raise ValueError((context.get("error") or {}).get("message") or "Export unavailable")
    customer_df = _region_drilldown_customer_frame(context, tag="regions.drilldown.export.customers")
    product_df = _region_drilldown_product_frame(context, tag="regions.drilldown.export.products")
    current_revenue = _clean_float(scorecard.get("total_revenue"), 0.0)
    prior_revenue = _clean_float(scorecard.get("prior_revenue"), 0.0)
    customer_rows_all = _prepare_region_customer_rows(customer_df, current_revenue, prior_revenue)
    product_rows_all = _prepare_region_product_rows(product_df, current_revenue)
    churn_rows = [
        row
        for row in customer_rows_all
        if str(row.get("risk_level") or "").strip().lower() in {"lost", "churned", "at risk", "warming"}
    ]

    summary_frame = pd.DataFrame(
        [
            {"metric": "Region", "value": str(region_id)},
            {"metric": "WindowStart", "value": window.get("start")},
            {"metric": "WindowEnd", "value": window.get("end")},
            {"metric": "PriorStart", "value": window.get("prior_start")},
            {"metric": "PriorEnd", "value": window.get("prior_end")},
            {"metric": "Revenue", "value": scorecard.get("total_revenue")},
            {"metric": "Profit", "value": scorecard.get("total_profit")},
            {"metric": "MarginPct", "value": scorecard.get("margin_pct")},
            {"metric": "Orders", "value": scorecard.get("orders")},
            {"metric": "Customers", "value": scorecard.get("customers")},
            {"metric": "AvgOrderValue", "value": scorecard.get("avg_order_value")},
            {"metric": "RevenuePerCustomer", "value": scorecard.get("revenue_per_customer")},
            {"metric": "ProfitPerOrder", "value": scorecard.get("profit_per_order")},
            {"metric": "RepeatPct", "value": scorecard.get("repeat_pct")},
            {"metric": "ChurnPct", "value": scorecard.get("churn_pct")},
            {"metric": "NewCustomerSharePct", "value": scorecard.get("new_customer_share_pct")},
            {"metric": "MoMGrowthPct", "value": scorecard.get("mom_growth")},
            {"metric": "YoYGrowthPct", "value": scorecard.get("yoy_growth")},
            {"metric": "RevenueDeltaVsPrior", "value": scorecard.get("revenue_delta_window")},
            {"metric": "RevenueDeltaVsPriorPct", "value": scorecard.get("revenue_delta_window_pct")},
            {"metric": "TopCustomerSharePct", "value": scorecard.get("top_customer_share_pct")},
            {"metric": "TopProductSharePct", "value": scorecard.get("top_product_share_pct")},
            {"metric": "CustomerHHI", "value": scorecard.get("customer_hhi")},
            {"metric": "ProductHHI", "value": scorecard.get("product_hhi")},
            {"metric": "AtRiskCustomers", "value": scorecard.get("at_risk_customers")},
            {"metric": "AtRiskRevenue", "value": scorecard.get("at_risk_revenue")},
            {"metric": "CostCoveragePct", "value": scorecard.get("cost_coverage_pct")},
            {"metric": "PacksCoveragePct", "value": scorecard.get("packs_coverage_pct")},
            {"metric": "DataQualityFlag", "value": scorecard.get("data_quality_flag")},
        ]
    )

    current_labels = trend.get("labels") or []
    prior_labels = trend.get("prior_labels") or []
    max_len = max(len(current_labels), len(prior_labels))
    trend_rows = []
    for idx in range(max_len):
        trend_rows.append(
            {
                "current_month": current_labels[idx] if idx < len(current_labels) else None,
                "current_revenue": (trend.get("revenue") or [])[idx] if idx < len(trend.get("revenue") or []) else None,
                "current_profit": (trend.get("profit") or [])[idx] if idx < len(trend.get("profit") or []) else None,
                "current_margin_pct": (trend.get("margin_pct") or [])[idx] if idx < len(trend.get("margin_pct") or []) else None,
                "current_orders": (trend.get("orders") or [])[idx] if idx < len(trend.get("orders") or []) else None,
                "current_customers": (trend.get("customers") or [])[idx] if idx < len(trend.get("customers") or []) else None,
                "prior_month": prior_labels[idx] if idx < len(prior_labels) else None,
                "prior_revenue": (trend.get("prior_revenue") or [])[idx] if idx < len(trend.get("prior_revenue") or []) else None,
                "prior_profit": (trend.get("prior_profit") or [])[idx] if idx < len(trend.get("prior_profit") or []) else None,
                "prior_margin_pct": (trend.get("prior_margin_pct") or [])[idx] if idx < len(trend.get("prior_margin_pct") or []) else None,
                "prior_orders": (trend.get("prior_orders") or [])[idx] if idx < len(trend.get("prior_orders") or []) else None,
                "prior_customers": (trend.get("prior_customers") or [])[idx] if idx < len(trend.get("prior_customers") or []) else None,
            }
        )
    trend_frame = pd.DataFrame.from_records(trend_rows)

    frames = {
        "summary": summary_frame,
        "trend": trend_frame,
        "customers": pd.DataFrame.from_records(customer_rows_all),
        "products": pd.DataFrame.from_records(product_rows_all),
        "churn": pd.DataFrame.from_records(churn_rows),
        "shipping": pd.DataFrame.from_records(operations.get("shipping_mix") or []),
        "suppliers": pd.DataFrame.from_records(operations.get("supplier_mix") or []),
        "weekday": pd.DataFrame.from_records(operations.get("weekday") or []),
        "insights": pd.DataFrame.from_records(region_v2.get("insights") or []),
        "retention_buckets": pd.DataFrame.from_records(retention.get("buckets") or []),
    }
    meta = {
        "region_id": str(region_id),
        "dataset_version": ((payload.get("meta") or {}).get("dataset_version") if isinstance(payload.get("meta"), dict) else None),
        "start": window.get("start"),
        "end": window.get("end"),
        "prior_start": window.get("prior_start"),
        "prior_end": window.get("prior_end"),
    }
    return frames, meta


def build_region_drilldown_export_dataset(
    region_id: str,
    filters: Any,
    scope: Dict[str, Any],
    args: Any,
    dataset: str,
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    token = str(dataset or "summary").strip().lower()
    aliases = {
        "table": "customers",
        "customer": "customers",
        "product": "products",
        "operations": "shipping",
        "operation": "shipping",
        "ship": "shipping",
        "supplier": "suppliers",
        "trend": "trend",
        "monthly": "trend",
        "risk": "churn",
        "retention": "churn",
        "churned": "churn",
        "kpis": "summary",
    }
    token = aliases.get(token, token)
    frames, meta = build_region_drilldown_export_frames(region_id, filters, scope, args)
    if token not in frames:
        raise ValueError(f"Unsupported region drilldown export dataset: {dataset}")
    return frames[token], meta

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from app.services import analytics_utils as au

from app.services.filters import FilterParams
from app.services.overview_query import compute_overview


def _timestamp(value: pd.Timestamp | None) -> str | None:
    if value is None or pd.isna(value):
        return None
    try:
        if getattr(value, "tzinfo", None) is not None:
            value = value.tz_convert(None)  # type: ignore[attr-defined]
    except Exception:
        try:
            value = value.tz_localize(None)  # type: ignore[attr-defined]
        except Exception:
            pass
    try:
        return value.isoformat()
    except Exception:
        return str(value)


def _filters_payload(filters: FilterParams | None) -> Dict[str, Any]:
    if filters is None:
        return {"has_active_filters": False}

    if is_dataclass(filters):
        data = asdict(filters)
    else:
        try:
            data = filters._asdict()  # type: ignore[attr-defined]
        except Exception:
            data = dict(getattr(filters, "__dict__", {}))

    payload = {
        "start": _timestamp(data.get("start")),
        "end": _timestamp(data.get("end")),
        "regions": list(data.get("regions") or []),
        "methods": list(data.get("methods") or []),
        "customers": list(data.get("customers") or []),
        "suppliers": list(data.get("suppliers") or []),
        "sales_reps": list(data.get("sales_reps") or []),
    }
    payload["has_active_filters"] = any(
        payload[key] for key in ("regions", "methods", "customers", "suppliers", "sales_reps")
    ) or payload["start"] is not None or payload["end"] is not None
    return payload


def _data_window(df: pd.DataFrame) -> Dict[str, Any]:
    if df is None or df.empty or "Date" not in df.columns:
        return {"start": None, "end": None, "days": 0, "rows": int(len(df) if df is not None else 0)}

    dates = pd.to_datetime(df["Date"], errors="coerce").dropna()
    if dates.empty:
        return {"start": None, "end": None, "days": 0, "rows": int(len(df))}

    start = dates.min()
    end = dates.max()
    days = max(1, int((end - start).days) + 1)
    return {
        "start": _timestamp(start),
        "end": _timestamp(end),
        "days": days,
        "rows": int(len(df)),
    }


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _normalize_top(items: Iterable[Dict[str, Any]], limit: int = 5) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for raw in list(items or [])[:limit]:
        normalized.append(
            {
                "id": raw.get("id"),
                "label": raw.get("label") or raw.get("name") or "Unknown",
                "revenue": round(_safe_float(raw.get("revenue")), 2),
                "share": round(_safe_float(raw.get("share")), 2),
                "orders": _safe_int(raw.get("orders") or raw.get("count")),
            }
        )
    return normalized


def _weight_from_overview(overview: Dict[str, Any], kpis: Dict[str, Any]) -> float:
    meat = overview.get("meat_metrics", {}) if overview else {}
    yield_metrics = meat.get("yield_metrics", {})
    if yield_metrics:
        return _safe_float(yield_metrics.get("total_weight_lbs"))
    return _safe_float(kpis.get("total_weight_lbs"))


def _weight_trend(df: pd.DataFrame) -> Dict[str, List[Any]]:
    if df is None or df.empty or "Date" not in df.columns:
        return {"months": [], "values": []}
    work = df[["Date"]].copy()
    weight = None
    for col in ("WeightLb", "pack_weight_lb_sum", "QtyShippedLb"):
        if col in df.columns:
            weight = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
            break
    if weight is None:
        return {"months": [], "values": []}
    work["Weight"] = weight
    work["Date"] = pd.to_datetime(work["Date"], errors="coerce")
    work = work.dropna(subset=["Date"])
    if work.empty:
        return {"months": [], "values": []}
    monthly = (
        work.groupby(work["Date"].dt.to_period("M"))["Weight"]
        .sum()
        .sort_index()
    )
    months = [period.to_timestamp().strftime("%Y-%m") for period in monthly.index]
    values = [round(float(val), 2) for val in monthly.values]
    return {"months": months, "values": values}


def _spotlight_text(health: Dict[str, Any], kpis: Dict[str, Any], customer_top: List[Dict[str, Any]]) -> Dict[str, str]:
    status = (health.get("status") or "stable").title()
    growth = (health.get("growth_trend") or "flat").title()
    rev_delta = _safe_float(kpis.get("rev_delta_pct"))
    headline = f"{status} Health · {growth} Growth"

    detail_parts: List[str] = []
    detail_parts.append(f"Revenue {rev_delta:+.1f}% vs prior period")
    if customer_top:
        leader = customer_top[0]
        detail_parts.append(
            f"{leader['label']} leads with {leader['share']:.1f}% share"
        )
    concerns = health.get("concerns") or []
    if concerns:
        detail_parts.append(f"Watch: {concerns[0]}")
    return {"headline": headline, "detail": " · ".join(detail_parts)}


def build_summary_payload(
    df: pd.DataFrame,
    overview: Optional[Dict[str, Any]] = None,
    *,
    filters: FilterParams | None = None,
    version: str | None = None,
) -> Dict[str, Any]:
    """
    Compose a single payload that fuels the overview dashboard UI.
    """
    generated_at = pd.Timestamp.utcnow().isoformat()
    has_data = df is not None and not df.empty
    overview_data = overview or (compute_overview(df) if has_data else {"kpis": {}, "monthly": {}})
    kpis = overview_data.get("kpis", {}) or {}
    monthly = overview_data.get("monthly", {}) or {}
    insights = overview_data.get("insights", {}) or {}
    operations = overview_data.get("operations", {}) or {}
    dashboard = overview_data.get("dashboard_summary", {}) or {}
    margin_info = operations.get("margin", {}) or {}

    months = monthly.get("months") or []
    weight_series = monthly.get("weight")
    if not weight_series:
        computed_weight = _weight_trend(df)
        if not months:
            months = computed_weight["months"]
        weight_series = computed_weight["values"]

    customer_insights = insights.get("customers", {}) or {}
    product_insights = insights.get("products", {}) or {}
    supplier_insights = insights.get("suppliers", {}) or {}
    sales_rep_insights = insights.get("salesReps", {}) or {}

    top_customers = _normalize_top(customer_insights.get("top", []))
    top_products = _normalize_top(product_insights.get("top", []))
    top_suppliers = _normalize_top(supplier_insights.get("top", []))
    top_reps = _normalize_top(sales_rep_insights.get("top", []))

    total_weight = _weight_from_overview(overview_data, kpis)
    avg_weight_per_order = _safe_float(kpis.get("avg_weight_per_order"))
    if not avg_weight_per_order and total_weight:
        orders = _safe_float(kpis.get("total_orders"))
        avg_weight_per_order = round(total_weight / orders, 2) if orders else 0.0

    trends = {
        "months": months,
        "revenue": monthly.get("revenue", []),
        "orders": monthly.get("orders", []),
        "weight": weight_series or [],
    }

    detail_payload = {
        "revenue": {
            "value": round(_safe_float(kpis.get("total_revenue")), 2),
            "delta_pct": round(_safe_float(kpis.get("rev_delta_pct")), 1),
            "delta": round(_safe_float(kpis.get("rev_delta")), 2),
            "prior": round(_safe_float(kpis.get("rev_prior")), 2),
        },
        "orders": {
            "value": _safe_int(kpis.get("total_orders")),
            "delta_pct": round(_safe_float(kpis.get("orders_delta_pct")), 1),
            "delta": _safe_int(kpis.get("orders_delta")),
            "prior": _safe_int(kpis.get("orders_prior")),
        },
        "customers": {
            "value": _safe_int(kpis.get("total_customers")),
            "delta_pct": round(_safe_float(kpis.get("customers_delta_pct")), 1),
            "delta": _safe_int(kpis.get("customers_delta")),
            "prior": _safe_int(kpis.get("customers_prior")),
        },
        "aov": {
            "value": round(_safe_float(kpis.get("aov")), 2),
            "delta": round(_safe_float(kpis.get("aov_delta")), 2),
            "delta_pct": round(_safe_float(kpis.get("aov_delta_pct")), 1),
        },
        "weight": {
            "value": round(total_weight, 2),
            "avg_per_order": round(avg_weight_per_order, 2),
        },
        "margin_pct": round(_safe_float(margin_info.get("gross_margin_pct")), 1),
    }

    total_revenue = _safe_float(margin_info.get("revenue"))
    if not total_revenue:
        total_revenue = _safe_float(kpis.get("total_revenue"))
    total_cost = _safe_float(margin_info.get("cost"))
    total_revenue = round(total_revenue, 2)
    total_cost = round(total_cost, 2)
    total_profit = total_revenue - total_cost
    margin_pct = margin_info.get("gross_margin_pct")
    if margin_pct is None:
        margin_pct = (total_profit / total_revenue * 100.0) if total_revenue else 0.0

    kpi_payload = {
        "revenue": total_revenue,
        "cost": total_cost,
        "profit": total_profit,
        "margin_pct": round(_safe_float(margin_pct), 1),
        "orders": _safe_int(kpis.get("total_orders")),
        "customers": _safe_int(kpis.get("total_customers")),
        "aov": round(_safe_float(kpis.get("aov")), 2),
        "weight": {
            "value": round(total_weight, 2),
            "avg_per_order": round(avg_weight_per_order, 2),
        },
    }

    health = {
        "score": _safe_int(dashboard.get("health_score")),
        "status": dashboard.get("health_status") or "unknown",
        "growth_trend": dashboard.get("growth_trend") or "stable",
        "risk_level": dashboard.get("risk_level") or "low",
        "strengths": dashboard.get("key_strengths") or [],
        "concerns": dashboard.get("key_concerns") or [],
        "data_range": dashboard.get("data_range") or {},
    }

    data_window = _data_window(df)
    cost_col = au.cost_column(df) if df is not None else None
    cost_series = pd.to_numeric(df.get(cost_col, pd.Series(dtype=float)), errors="coerce") if df is not None and cost_col else pd.Series(dtype=float)
    cost_missing_rate = float(cost_series.isna().mean()) if len(cost_series) else 1.0
    summary_payload = {
        "meta": {
            "generated_at": generated_at,
            "version": version or "unknown",
            "filters": _filters_payload(filters),
            "data_window": data_window,
            "window": data_window,
            "cost_missing_rate": round(cost_missing_rate, 4),
            "has_data": has_data,
            "source": "overview_summary",
        },
        "kpis": kpi_payload,
        "kpis_detail": detail_payload,
        "trends": trends,
        "insights": {
            "customers": {
                "repeat_rate": round(_safe_float(customer_insights.get("repeat_rate")), 1),
                "active_30d": _safe_int(customer_insights.get("active_30d")),
                "at_risk": _safe_int(customer_insights.get("at_risk")),
                "total": _safe_int(customer_insights.get("total")),
                "top": top_customers,
            },
            "products": {
                "avg_units_per_order": round(_safe_float(product_insights.get("avg_units_per_order")), 2),
                "top": top_products,
            },
            "suppliers": {
                "top": top_suppliers,
            },
            "sales_reps": {
                "active": _safe_int(sales_rep_insights.get("active")),
                "avg_orders_per_rep": round(_safe_float(sales_rep_insights.get("avg_orders_per_rep")), 1),
                "top3_share": round(_safe_float(sales_rep_insights.get("top3_share")), 1),
                "top": top_reps,
            },
        },
        "health": health,
        "spotlight": _spotlight_text(health, kpis, top_customers),
    }
    return summary_payload

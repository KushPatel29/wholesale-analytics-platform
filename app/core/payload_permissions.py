"""Prune JSON payload sections that the current user should not receive."""

from __future__ import annotations

from typing import Any


def _copy_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _empty_table(table: Any) -> dict[str, Any]:
    out = _copy_dict(table)
    out["rows"] = []
    out["total_rows"] = 0
    out["total_pages"] = 0
    out["page_totals"] = {}
    return out


def _prune_products_bundle(payload: dict[str, Any], user: Any) -> dict[str, Any]:
    from app.core import rbac

    out = dict(payload)
    charts = _copy_dict(out.get("charts"))
    table = _copy_dict(out.get("table"))

    if not rbac.can_view_feature("feature.products.trajectory.view", user):
        charts["trajectory"] = {"grain": "monthly", "labels": [], "revenue": [], "qty": [], "profit": [], "margin_pct": []}
        out["monthly_series"] = []
        out["trend"] = {}
        out["forecast_overlay"] = []
        out["projected_next_month"] = {}

    if not rbac.can_view_feature("feature.products.pricing.view", user):
        charts["unit_price_dist"] = []
        out["price_vs_velocity"] = []
        perf = _copy_dict(out.get("performance_bubble"))
        perf["points"] = []
        out["performance_bubble"] = perf
        guardrails = _copy_dict(out.get("pricing_guardrails"))
        guardrails["rows"] = []
        for key in ("high_outlier_count", "low_outlier_count", "outside_count"):
            guardrails[key] = 0
        guardrails["outside_pct"] = None
        out["pricing_guardrails"] = guardrails
        execution_lists = _copy_dict(out.get("execution_lists"))
        for key in list(execution_lists.keys()):
            execution_lists[key] = []
        out["execution_lists"] = execution_lists

    if not rbac.can_view_feature("feature.products.segments.view", user):
        charts["segments"] = {"summary": [], "movers": [], "mix_shift": []}

    if not rbac.can_view_feature("feature.products.table.view", user):
        out["table"] = _empty_table(table)
    else:
        out["table"] = table

    if not rbac.can_view_feature("feature.products.recommendations.view", user) or not rbac.can_view_sensitive(
        "data.price_recommendation.view",
        user,
    ):
        out["recommendations"] = []
        out["ai_signals"] = []
        for row in table.get("rows") or []:
            if isinstance(row, dict):
                row["quick_rec"] = None
                row["recommendation"] = None

    if not rbac.can_view_feature("feature.products.forecast.view", user):
        out["forecast_overlay"] = []

    out["charts"] = charts
    return out


def _prune_customers_bundle(payload: dict[str, Any], user: Any) -> dict[str, Any]:
    from app.core import rbac

    out = dict(payload)
    if not rbac.can_view_feature("feature.customers.rfm.view", user):
        out["rfm"] = {
            "segments": [],
            "top": [],
            "scatter": {},
            "matrix": {"rows": []},
            "segment_table": {"rows": [], "total_rows": 0},
            "customers_table": {"rows": [], "total_rows": 0},
        }
    if not rbac.can_view_feature("feature.customers.cohorts.view", user):
        out["cohorts"] = {
            "matrix": [],
            "sizes": [],
            "heatmap": [],
            "retention": [],
            "summary": {},
        }
    if not rbac.can_view_feature("feature.customers.clv.view", user):
        out["clv"] = {
            "top": [],
            "leaderboard": [],
            "segment_summary": [],
            "customers_table": {"rows": [], "total_rows": 0},
            "charts": {},
            "settings": {},
        }
    return out


def apply_payload_permissions(payload: Any, user: Any = None, *, path: str | None = None) -> Any:
    if not isinstance(payload, dict):
        return payload

    current_path = str(path or "").strip().lower()
    if not current_path:
        return payload

    if current_path.startswith("/api/products/bundle"):
        return _prune_products_bundle(payload, user)
    if current_path.startswith("/api/customers/bundle"):
        return _prune_customers_bundle(payload, user)
    return payload

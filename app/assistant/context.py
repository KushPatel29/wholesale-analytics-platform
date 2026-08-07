from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Sequence

from flask import current_app, request, session
from flask_login import current_user

from app.core import rbac
from app.core.access_policy import get_current_scope
from app.core.sensitive_data import sensitive_access_flags
from app.services.filters import FilterParams, parse_filters, resolve_filters


_MODULE_PERMS: tuple[str, ...] = (
    "page.overview.view",
    "page.customers.view",
    "page.products.view",
    "page.regions.view",
    "page.suppliers.view",
    "page.salesreps.view",
    "page.returns.view",
    "page.admin.view",
    "page.notifications.view",
)

_PAGE_VISIBLE_SECTIONS: Dict[str, Sequence[str]] = {
    "overview": (
        "scorecard",
        "trend",
        "movers",
        "price_volume_mix",
        "concentration",
        "margin_risk",
        "data_health",
        "forecast",
    ),
    "customers": (
        "kpis",
        "executive_narrative",
        "churn_risk",
        "drivers",
        "rfm",
        "clv",
        "cohorts",
        "table",
    ),
    "products": (
        "kpis",
        "trajectory",
        "price_velocity",
        "movers",
        "pricing_guardrails",
        "recommendations",
        "table",
    ),
    "regions": (
        "kpis",
        "trend",
        "momentum",
        "concentration",
        "retention",
        "risk",
        "table",
    ),
    "suppliers": (
        "kpis",
        "trend",
        "movers",
        "risk_opportunities",
        "segments",
        "executive_summary",
        "table",
    ),
    "salesreps": (
        "kpis",
        "trend",
        "top_reps",
        "concentration",
        "risk_flags",
        "table",
    ),
    "returns": (
        "summary",
        "approvals",
        "status",
        "reasons",
        "workflow",
    ),
}


def canonical_page(raw: str | None) -> str:
    token = str(raw or "").strip().lower()
    if not token:
        return "overview"
    mapping = {
        "overview": "overview",
        "overview_page": "overview",
        "pages": "overview",
        "/": "overview",
        "/overview": "overview",
        "customers": "customers",
        "/customers": "customers",
        "products": "products",
        "/products": "products",
        "regions": "regions",
        "/regions": "regions",
        "suppliers": "suppliers",
        "/suppliers": "suppliers",
        "salesreps": "salesreps",
        "/salesreps": "salesreps",
        "returns": "returns",
        "returns_portal": "returns",
        "returns_ops": "returns",
        "returns_warehouse": "returns",
        "returns_admin": "returns",
        "/returns": "returns",
        "admin": "admin",
        "/admin": "admin",
        "notifications": "notifications",
        "/notifications": "notifications",
        "assistant": "assistant",
        "/assistant": "assistant",
    }
    if token in mapping:
        return mapping[token]
    if token.startswith("/"):
        for key, value in mapping.items():
            if key and token.startswith(key):
                return value
    if "." in token:
        base = token.split(".", 1)[0]
        if base in mapping:
            return mapping[base]
    return token.strip("/").split("/", 1)[0] or "overview"


def context_blob(payload: Mapping[str, Any] | None) -> Dict[str, Any]:
    data = payload.get("context") if isinstance(payload, Mapping) else None
    if isinstance(data, Mapping):
        return dict(data)
    return {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return [item for item in value if item not in (None, "")]
    if value in (None, ""):
        return []
    return [value]


def _allowed_metrics(page: str, sensitive: Mapping[str, bool]) -> list[str]:
    base = {
        "overview": ["revenue", "orders", "qty", "trend", "movers", "concentration", "forecast"],
        "customers": ["revenue", "orders", "repeat_rate", "churn_risk", "retention", "at_risk_customers"],
        "products": ["revenue", "qty", "unit_price", "velocity", "mix", "dependency"],
        "regions": ["revenue", "orders", "customers", "concentration", "retention", "risk"],
        "suppliers": ["revenue", "active_suppliers", "dependency", "revenue_at_risk"],
        "salesreps": ["revenue", "orders", "customers", "concentration", "risk_flags"],
        "returns": ["total_returns", "total_credit_amount", "status", "reasons", "pending_approvals"],
    }.get(page, ["revenue", "orders"])
    metrics = list(base)
    if sensitive.get("cost"):
        metrics.extend(["cost", "cost_coverage_pct"])
    if sensitive.get("profit"):
        metrics.extend(["profit", "profit_per_order"])
    if sensitive.get("margin"):
        metrics.extend(["margin_pct"])
    if sensitive.get("margin_risk"):
        metrics.extend(["margin_risk"])
    seen: set[str] = set()
    deduped: list[str] = []
    for metric in metrics:
        token = str(metric).strip()
        if token and token not in seen:
            seen.add(token)
            deduped.append(token)
    return deduped


def _entity_from_route(page: str, ref_path: str | None) -> Dict[str, Any] | None:
    raw = str(ref_path or "").strip()
    if not raw:
        return None
    path = raw.split("?", 1)[0].strip("/")
    if not path:
        return None
    parts = [segment for segment in path.split("/") if segment]
    if not parts:
        return None

    if page in {"customers", "products", "regions", "suppliers", "salesreps"}:
        for idx, part in enumerate(parts):
            if part in {"customers", "products", "regions", "suppliers", "salesreps"} and idx + 1 < len(parts):
                candidate = parts[idx + 1]
                if candidate and candidate not in {"drilldown", "detail"}:
                    return {
                        "type": part.rstrip("s"),
                        "id": candidate,
                        "label": candidate,
                        "source": "route",
                    }
        trailing = parts[-1]
        if trailing and trailing not in {"customers", "products", "regions", "suppliers", "salesreps", "drilldown", "detail"}:
            return {
                "type": page.rstrip("s"),
                "id": trailing,
                "label": trailing,
                "source": "route",
            }
    if page == "returns":
        match = re.search(r"(?:rma[-_/]?|returns?/)(\d+)", raw.lower())
        if match:
            token = match.group(1)
            return {"type": "return", "id": token, "label": f"RMA-{token}", "source": "route"}
    return None


def _normalized_entity(blob: Mapping[str, Any], page: str, ref_path: str | None) -> Dict[str, Any] | None:
    explicit = blob.get("entity")
    if isinstance(explicit, Mapping):
        etype = str(explicit.get("type") or explicit.get("entity_type") or page.rstrip("s")).strip()
        eid = str(explicit.get("id") or explicit.get("entity_id") or "").strip()
        label = str(explicit.get("label") or explicit.get("name") or eid).strip()
        if etype or eid:
            return {
                "type": etype or page.rstrip("s"),
                "id": eid or None,
                "label": label or None,
                "source": "context",
            }
    selected = blob.get("selected")
    if isinstance(selected, Mapping):
        eid = str(selected.get("id") or selected.get("value") or "").strip()
        label = str(selected.get("label") or selected.get("name") or eid).strip()
        if eid:
            return {
                "type": page.rstrip("s"),
                "id": eid,
                "label": label or eid,
                "source": "context",
            }
    return _entity_from_route(page, ref_path)


def _visible_sections(page: str, blob: Mapping[str, Any]) -> list[str]:
    sections = blob.get("visible_sections") if isinstance(blob, Mapping) else None
    if isinstance(sections, (list, tuple)):
        cleaned = [str(section).strip() for section in sections if str(section).strip()]
        if cleaned:
            return cleaned[:20]
    return list(_PAGE_VISIBLE_SECTIONS.get(page, ()))


def resolve_filters_from_payload(payload: Mapping[str, Any] | None) -> FilterParams:
    ctx = context_blob(payload)
    filters_blob = ctx.get("filters") if isinstance(ctx, Mapping) else None
    if isinstance(filters_blob, Mapping):
        return parse_filters(dict(filters_blob))
    sticky_enabled = bool(current_app.config.get("STICKY_FILTERS", True))
    return resolve_filters(
        request,
        current_user,
        session_obj=session,
        source=request.args or {},
        sticky_enabled=sticky_enabled,
    )[0]


def has_any_access(user: Any) -> bool:
    return bool(rbac.user_has_any_permission(user, *_MODULE_PERMS))


def module_access_map(user: Any) -> Dict[str, bool]:
    return {
        "overview": bool(rbac.can_view_page("overview", user)),
        "customers": bool(rbac.can_view_page("customers", user)),
        "products": bool(rbac.can_view_page("products", user)),
        "regions": bool(rbac.can_view_page("regions", user)),
        "suppliers": bool(rbac.can_view_page("suppliers", user)),
        "salesreps": bool(rbac.can_view_page("salesreps", user)),
        "returns": bool(
            rbac.user_has_any_permission(
                user,
                "page.returns.view",
                "returns.create",
                "page.returns.customer_portal",
                "admin.returns.manage",
            )
        ),
        "admin": bool(rbac.can_view_page("admin", user)),
        "notifications": bool(rbac.can_view_page("notifications", user)),
    }


def build_page_context(
    payload: Mapping[str, Any] | None = None,
    *,
    page_hint: str | None = None,
    ref_path: str | None = None,
    enable_page_context: bool = True,
) -> Dict[str, Any]:
    blob = context_blob(payload) if enable_page_context else {}
    effective_ref_path = str(blob.get("ref_path") or ref_path or request.path or "").strip()
    page = canonical_page(blob.get("page") or page_hint or effective_ref_path)
    filters = resolve_filters_from_payload(payload if enable_page_context else None)
    scope = get_current_scope(use_cache=True).as_dict(include_allowed=True)
    sensitive = sensitive_access_flags(current_user)
    module_access = module_access_map(current_user)
    selected_entity = _normalized_entity(blob, page, effective_ref_path)
    start_iso = getattr(filters, "start", None).isoformat() if getattr(filters, "start", None) is not None else None
    end_iso = getattr(filters, "end", None).isoformat() if getattr(filters, "end", None) is not None else None
    page_state = {
        "module": page,
        "view": str(blob.get("view") or blob.get("tab") or "").strip() or page,
        "drilldown": bool(selected_entity and selected_entity.get("id")),
        "selected_entity": selected_entity,
        "visible_sections": _visible_sections(page, blob),
        "allowed_metrics": _allowed_metrics(page, sensitive),
        "active_window": {
            "start": start_iso,
            "end": end_iso,
            "label": f"{start_iso or 'auto'} -> {end_iso or 'auto'}",
        },
        "trust_context": {
            "cost_visible": bool(sensitive.get("cost")),
            "profit_visible": bool(sensitive.get("profit")),
            "margin_visible": bool(sensitive.get("margin")),
            "margin_risk_visible": bool(sensitive.get("margin_risk")),
        },
        "local_drill_state": {
            "selected_rows": _as_list(blob.get("selected_rows")),
            "expanded_section": str(blob.get("expanded_section") or "").strip() or None,
            "comparison_mode": str(blob.get("comparison_mode") or "").strip() or None,
        },
        "freshness_context": blob.get("freshness") if isinstance(blob.get("freshness"), Mapping) else None,
    }
    return {
        "page": page,
        "filters": {
            "start": start_iso,
            "end": end_iso,
            "regions": list(getattr(filters, "regions", ()) or ()),
            "methods": list(getattr(filters, "methods", ()) or ()),
            "customers": list(getattr(filters, "customers", ()) or ()),
            "suppliers": list(getattr(filters, "suppliers", ()) or ()),
            "products": list(getattr(filters, "products", ()) or ()),
            "sales_reps": list(getattr(filters, "sales_reps", ()) or ()),
        },
        "scope": scope,
        "module_access": module_access,
        "sensitive_data_access": sensitive,
        "user_role": str(getattr(current_user, "role", "") or ""),
        "entity": selected_entity,
        "page_state": page_state,
        "ref_path": effective_ref_path or None,
        "raw_context": blob,
    }

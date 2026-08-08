from __future__ import annotations

import base64
import math
import json
from dataclasses import replace
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode

import pandas as pd
from flask import current_app, request, url_for
from itsdangerous import BadSignature, BadTimeSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.access_policy import permissions_version
from app.core.exports import dataframe_to_csv_response
from app.core.rbac import can_view_costs, user_has_permission
from app.services import analytics_utils as au
from app.services import fact_store, filters as filters_svc, filters_service, overview_v2
from app.services import salesreps_bundle


DRILLDOWN_TOKEN_SALT = "wholesale.universal-drilldown.v1"
DRILLDOWN_MAX_AGE_SECONDS = 60 * 60 * 8
MAX_ROWS = 250

SOURCE_PAGE_ALIASES = {
    "regions_v2": "regions",
    "region_drilldown_v2": "region_drilldown",
    "supplier_drilldown_v2": "supplier_drilldown",
    "customers_kpis": "customers",
}

ALLOWED_SOURCE_PAGES = {
    "overview",
    "customers",
    "customer_drilldown",
    "products",
    "product_drilldown",
    "regions",
    "region_drilldown",
    "suppliers",
    "supplier_drilldown",
    "salesreps",
    "salesrep_drilldown",
}

ALLOWED_TARGETS = {
    "workspace",
    "customer",
    "product",
    "region",
    "supplier",
    "salesrep",
}

ENTITY_PERMISSION_MAP: dict[str, tuple[str, ...]] = {
    "customer": ("page.customers.view", "page.customers.drilldown.view"),
    "product": ("page.products.view", "page.products.drilldown.view"),
    "region": ("page.regions.view", "page.regions.drilldown.view"),
    "supplier": ("page.suppliers.view", "page.suppliers.drilldown.view"),
    "salesrep": ("page.salesreps.view", "page.salesreps.drilldown.view"),
}

SOURCE_PERMISSION_MAP: dict[str, tuple[str, ...]] = {
    "overview": ("page.overview.view",),
    "customers": ("page.customers.view",),
    "customer_drilldown": ("page.customers.view", "page.customers.drilldown.view"),
    "products": ("page.products.view",),
    "product_drilldown": ("page.products.view", "page.products.drilldown.view"),
    "regions": ("page.regions.view",),
    "region_drilldown": ("page.regions.view", "page.regions.drilldown.view"),
    "suppliers": ("page.suppliers.view",),
    "supplier_drilldown": ("page.suppliers.view", "page.suppliers.drilldown.view"),
    "salesreps": ("page.salesreps.view",),
    "salesrep_drilldown": ("page.salesreps.view", "page.salesreps.drilldown.view"),
}

SOURCE_PAGE_LABELS: dict[str, str] = {
    "overview": "Overview",
    "customers": "Customers",
    "customer_drilldown": "Customer Drilldown",
    "products": "Products",
    "product_drilldown": "Product Drilldown",
    "regions": "Regions",
    "region_drilldown": "Region Drilldown",
    "suppliers": "Suppliers",
    "supplier_drilldown": "Supplier Drilldown",
    "salesreps": "Sales Reps",
    "salesrep_drilldown": "Sales Rep Drilldown",
}

DETAIL_COLUMNS = [
    "DateExpected",
    "Date",
    "OrderId",
    "CustomerId",
    "CustomerName",
    "ProductId",
    "SKU",
    "ProductName",
    "ProteinType",
    "ProteinName",
    "Category",
    "ProductCategory",
    "RegionName",
    "Region",
    "SupplierName",
    "SalesRepId",
    "SalesRepName",
    "OrderStatus",
    "Revenue",
    "Cost",
    "QuantityOrdered",
    "WeightLb",
    "pack_item_count_sum",
    "pack_weight_lb_sum",
]

ALLOWED_TARGET_QUERY_METRICS = {
    "revenue",
    "profit",
    "margin_pct",
    "margin_dollar",
    "orders",
    "customers",
    "weight_lb",
    "metric",
}
ALLOWED_TARGET_QUERY_GRAINS = {"monthly", "quarterly", "yearly", "ttm"}
ALLOWED_TARGET_QUERY_VIEWS = {"absolute", "yoy_delta", "index"}


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        secret_key=str(current_app.config.get("SECRET_KEY") or ""),
        salt=DRILLDOWN_TOKEN_SALT,
    )


def _token_max_age() -> int:
    raw = current_app.config.get("DRILLDOWN_TOKEN_MAX_AGE_SECONDS", DRILLDOWN_MAX_AGE_SECONDS)
    try:
        return max(60, int(raw))
    except Exception:
        return DRILLDOWN_MAX_AGE_SECONDS


def _clean_text(value: Any, *, max_len: int = 240) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_len]


def _clean_slug(value: Any, allowed: Iterable[str]) -> str | None:
    text = _clean_text(value, max_len=80)
    if not text:
        return None
    lowered = text.lower()
    return lowered if lowered in set(allowed) else None


def _normalize_source_page(value: Any) -> str | None:
    text = _clean_text(value, max_len=80)
    if not text:
        return None
    lowered = text.lower()
    canonical = SOURCE_PAGE_ALIASES.get(lowered, lowered)
    return canonical if canonical in ALLOWED_SOURCE_PAGES else None


def _source_page_label(value: Any) -> str:
    token = _clean_text(value, max_len=80)
    if not token:
        return "Drilldown"
    canonical = SOURCE_PAGE_ALIASES.get(token.lower(), token.lower())
    return SOURCE_PAGE_LABELS.get(canonical, canonical.replace("_", " ").title())


def _clean_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        num = float(value)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return float(num)


def _safe_user_id(user_obj: Any) -> str:
    if user_obj is None:
        return ""
    try:
        if hasattr(user_obj, "get_id"):
            value = user_obj.get_id()
            if value is not None:
                return str(value)
    except Exception:
        pass
    value = getattr(user_obj, "id", None)
    return "" if value is None else str(value)


def _scope_payload(user_obj: Any) -> dict[str, Any]:
    payload = filters_service.scope_from_user(user_obj)
    return payload if isinstance(payload, dict) else {}


def _sanitize_nested(value: Any, *, depth: int = 0) -> Any:
    if depth > 3:
        return None
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return _clean_number(value)
    if isinstance(value, str):
        return _clean_text(value, max_len=240)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for raw_key, raw_val in list(value.items())[:20]:
            key = _clean_text(raw_key, max_len=60)
            if not key or key.startswith("_"):
                continue
            clean = _sanitize_nested(raw_val, depth=depth + 1)
            if clean is None:
                continue
            out[key] = clean
        return out or None
    if isinstance(value, (list, tuple, set)):
        items = []
        for item in list(value)[:15]:
            clean = _sanitize_nested(item, depth=depth + 1)
            if clean is None:
                continue
            items.append(clean)
        return items or None
    return None


def _sanitize_filter_store(raw_filters: Any, *, apply_defaults: bool = True) -> dict[str, Any]:
    try:
        parsed = filters_svc.parse_filters(raw_filters or {})
        if not apply_defaults and isinstance(raw_filters, Mapping):
            has_explicit_dates = any(
                key in raw_filters
                for key in ("start", "start_date", "end", "end_date", "date_preset", "preset", "range_preset")
            )
            if not has_explicit_dates:
                parsed = replace(parsed, start=None, end=None, preset=None)
        return filters_svc.filters_to_store(parsed)
    except Exception:
        return filters_svc.filters_to_store(filters_svc.parse_filters({}))


def _sanitize_target_query(raw_query: Any) -> dict[str, Any]:
    query = raw_query if isinstance(raw_query, Mapping) else {}
    sanitized: dict[str, Any] = {}

    attribution_mode = _clean_text(query.get("attribution_mode"), max_len=40)
    if attribution_mode in {"current_owner", "historical_rep"}:
        sanitized["attribution_mode"] = attribution_mode

    roster_mode = _clean_text(query.get("roster_mode"), max_len=40)
    if roster_mode in {"current_only", "include_former"}:
        sanitized["roster_mode"] = roster_mode

    transfer_only = query.get("transfer_only")
    if transfer_only is not None:
        sanitized["transfer_only"] = str(transfer_only).strip().lower() in {"1", "true", "yes", "on"}

    metric = _clean_text(query.get("metric"), max_len=40)
    if metric in ALLOWED_TARGET_QUERY_METRICS:
        sanitized["metric"] = metric

    leaderboard_metric = _clean_text(query.get("leaderboard_metric"), max_len=40)
    if leaderboard_metric in ALLOWED_TARGET_QUERY_METRICS:
        sanitized["leaderboard_metric"] = leaderboard_metric

    trend_metric = _clean_text(query.get("trend_metric"), max_len=40)
    if trend_metric in ALLOWED_TARGET_QUERY_METRICS:
        sanitized["trend_metric"] = trend_metric

    trend_grain = _clean_text(query.get("trend_grain"), max_len=20)
    if trend_grain in ALLOWED_TARGET_QUERY_GRAINS:
        sanitized["trend_grain"] = trend_grain

    trend_view = _clean_text(query.get("trend_view"), max_len=20)
    if trend_view in ALLOWED_TARGET_QUERY_VIEWS:
        sanitized["trend_view"] = trend_view

    top_n_raw = query.get("top_n")
    if top_n_raw is None:
        top_n_raw = query.get("topN")
    top_n_num = _clean_number(top_n_raw)
    if top_n_num is not None:
        sanitized["top_n"] = max(5, min(25, int(top_n_num)))

    return sanitized


def _target_query_pairs(store: Mapping[str, Any]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    if not isinstance(store, Mapping):
        return pairs
    if store.get("attribution_mode"):
        pairs.append(("attribution_mode", str(store["attribution_mode"])))
    if store.get("roster_mode"):
        pairs.append(("roster_mode", str(store["roster_mode"])))
    if "transfer_only" in store:
        pairs.append(("transfer_only", "1" if bool(store.get("transfer_only")) else "0"))
    if store.get("metric"):
        pairs.append(("metric", str(store["metric"])))
    if store.get("leaderboard_metric"):
        pairs.append(("leaderboard_metric", str(store["leaderboard_metric"])))
    if store.get("trend_metric"):
        pairs.append(("trend_metric", str(store["trend_metric"])))
    if store.get("trend_grain"):
        pairs.append(("trend_grain", str(store["trend_grain"])))
    if store.get("trend_view"):
        pairs.append(("trend_view", str(store["trend_view"])))
    if store.get("top_n") is not None:
        pairs.append(("top_n", str(int(store["top_n"]))))
    return pairs


def _merge_filters(base_store: Mapping[str, Any], patch_store: Mapping[str, Any]) -> dict[str, Any]:
    base_params = filters_svc.parse_filters(base_store or {})
    patch_params = filters_svc.parse_filters(patch_store or {})
    if isinstance(patch_store, Mapping):
        has_explicit_dates = any(
            patch_store.get(key) not in (None, "", [], ())
            for key in ("start", "start_date", "end", "end_date", "date_preset", "preset", "range_preset")
        )
        if not has_explicit_dates:
            patch_params = replace(patch_params, start=None, end=None, preset=None)
    merged = replace(
        base_params,
        start=patch_params.start if patch_params.start is not None else base_params.start,
        end=patch_params.end if patch_params.end is not None else base_params.end,
        statuses=patch_params.statuses or base_params.statuses,
        regions=patch_params.regions or base_params.regions,
        methods=patch_params.methods or base_params.methods,
        customers=patch_params.customers or base_params.customers,
        suppliers=patch_params.suppliers or base_params.suppliers,
        products=patch_params.products or base_params.products,
        sales_reps=patch_params.sales_reps or base_params.sales_reps,
        preset=patch_params.preset or base_params.preset,
        date_type=patch_params.date_type or base_params.date_type,
        protein_min=patch_params.protein_min if patch_params.protein_min is not None else base_params.protein_min,
        protein_max=patch_params.protein_max if patch_params.protein_max is not None else base_params.protein_max,
        protein_name_like=patch_params.protein_name_like or base_params.protein_name_like,
        complete_months_only=patch_params.complete_months_only
        if patch_store and "complete_months_only" in patch_store
        else base_params.complete_months_only,
    )
    return filters_svc.filters_to_store(merged)


def _filters_query_pairs(store: Mapping[str, Any]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    if not isinstance(store, Mapping):
        return pairs
    mapping = {
        "start_date": "start",
        "end_date": "end",
        "date_preset": "date_preset",
        "date_type": "date_type",
        "statuses": "statuses",
        "regions": "regions",
        "shipping_methods": "methods",
        "customers": "customers",
        "suppliers": "suppliers",
        "products": "products",
        "sales_reps": "sales_reps",
        "protein_min": "protein_min",
        "protein_max": "protein_max",
        "protein_name_like": "protein_name_like",
        "complete_months_only": "complete_months_only",
    }
    for source_key, query_key in mapping.items():
        value = store.get(source_key)
        if value in (None, "", [], (), {}):
            continue
        if isinstance(value, (list, tuple, set)):
            for item in value:
                text = _clean_text(item, max_len=120)
                if text:
                    pairs.append((query_key, text))
            continue
        if isinstance(value, bool):
            pairs.append((query_key, "1" if value else "0"))
            continue
        pairs.append((query_key, str(value)))
    if pairs:
        pairs.append(("_gf", "1"))
    return pairs


def _source_back_href(
    context: Mapping[str, Any],
    merged_filters: Mapping[str, Any],
    target_query: Mapping[str, Any] | None = None,
) -> str | None:
    source_page = str(context.get("source_page") or "")
    source_entity_id = _clean_text(context.get("source_entity_id"), max_len=120)
    if source_page == "overview":
        base = _safe_url_for("overview_page.overview_landing")
    elif source_page == "customers":
        base = _safe_url_for("customers.index")
    elif source_page == "customer_drilldown" and source_entity_id:
        base = _safe_url_for("customers.drilldown", customer_id=source_entity_id)
    elif source_page == "products":
        base = _safe_url_for("products.index")
    elif source_page == "product_drilldown" and source_entity_id:
        base = _safe_url_for("products.drilldown", product_id=source_entity_id)
    elif source_page == "regions":
        base = _safe_url_for("regions.index")
    elif source_page == "region_drilldown" and source_entity_id:
        base = _safe_url_for("regions.drilldown", region_name=source_entity_id)
    elif source_page == "suppliers":
        base = _safe_url_for("suppliers.index")
    elif source_page == "supplier_drilldown" and source_entity_id:
        base = _safe_url_for("suppliers.drilldown", supplier_id=source_entity_id)
    elif source_page == "salesreps":
        base = _safe_url_for("salesreps.index")
    elif source_page == "salesrep_drilldown" and source_entity_id:
        base = _safe_url_for("salesreps.rep_detail", rep_id=source_entity_id)
    else:
        return None
    if not base:
        return None
    query_pairs = _filters_query_pairs(merged_filters)
    query_pairs.extend(_target_query_pairs(target_query or {}))
    query = urlencode(query_pairs, doseq=True)
    return f"{base}?{query}" if query else base


def _safe_url_for(endpoint: str, **values: Any) -> str | None:
    try:
        return url_for(endpoint, **values)
    except RuntimeError:
        try:
            with current_app.test_request_context():
                return url_for(endpoint, **values)
        except Exception:
            return None
    except Exception:
        return None


def sanitize_click_payload(raw_payload: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = raw_payload if isinstance(raw_payload, Mapping) else {}
    extra = _sanitize_nested(payload.get("extra")) or {}
    target_filters = {}
    if isinstance(extra, dict) and extra.get("target_filters"):
        target_filters = _sanitize_filter_store(extra.get("target_filters"), apply_defaults=False)
        extra = dict(extra)
        extra["target_filters"] = target_filters
    sanitized = {
        "version": "v1",
        "source_page": _normalize_source_page(payload.get("source_page")),
        "source_module": _clean_text(payload.get("source_module"), max_len=80),
        "source_section": _clean_text(payload.get("source_section"), max_len=120),
        "source_widget": _clean_text(payload.get("source_widget"), max_len=120),
        "source_entity_type": _clean_slug(payload.get("source_entity_type"), ENTITY_PERMISSION_MAP.keys()),
        "source_entity_id": _clean_text(payload.get("source_entity_id"), max_len=120),
        "source_entity_label": _clean_text(payload.get("source_entity_label"), max_len=160),
        "clicked_entity_type": _clean_slug(payload.get("clicked_entity_type"), ENTITY_PERMISSION_MAP.keys()),
        "clicked_entity_id": _clean_text(payload.get("clicked_entity_id"), max_len=120),
        "clicked_entity_label": _clean_text(payload.get("clicked_entity_label"), max_len=160),
        "clicked_metric": _clean_text(payload.get("clicked_metric"), max_len=120),
        "clicked_metric_value": _clean_number(payload.get("clicked_metric_value")),
        "comparison_metric": _clean_text(payload.get("comparison_metric"), max_len=120),
        "clicked_time_grain": _clean_text(payload.get("clicked_time_grain"), max_len=40),
        "clicked_time_value": _clean_text(payload.get("clicked_time_value"), max_len=80),
        "clicked_bucket": _clean_text(payload.get("clicked_bucket"), max_len=120),
        "requested_target": _clean_slug(payload.get("requested_target"), ALLOWED_TARGETS) or "workspace",
        "display_mode": _clean_text(payload.get("display_mode"), max_len=40),
        "active_filter_state": _sanitize_filter_store(payload.get("active_filter_state") or {}),
        "target_query": _sanitize_target_query(payload.get("target_query")),
        "extra": extra,
    }
    if not sanitized["source_page"]:
        raise ValueError("source_page is required")
    return sanitized


def issue_context_token(raw_payload: Mapping[str, Any], *, user_obj: Any) -> tuple[str, dict[str, Any]]:
    context = sanitize_click_payload(raw_payload)
    scope = _scope_payload(user_obj)
    merged_filters = _merge_filters(
        context.get("active_filter_state") or {},
        ((context.get("extra") or {}).get("target_filters") or {}),
    )
    context["active_filter_state"] = merged_filters
    context["issued_for_user_id"] = _safe_user_id(user_obj)
    context["scope_hash"] = str(scope.get("scope_hash") or "")
    context["permissions_version"] = str(scope.get("permissions_version") or permissions_version())
    context["back_href"] = _source_back_href(context, merged_filters, context.get("target_query") or {})
    token = _serializer().dumps(context)
    return token, context


def load_context_token(token: str, *, user_obj: Any) -> dict[str, Any]:
    if not token:
        raise ValueError("missing token")
    try:
        payload = _serializer().loads(token, max_age=_token_max_age())
    except SignatureExpired as exc:
        raise ValueError("expired") from exc
    except (BadSignature, BadTimeSignature) as exc:
        raise ValueError("invalid") from exc

    if not isinstance(payload, Mapping):
        raise ValueError("invalid")
    context = sanitize_click_payload(payload)
    issued_user_id = _clean_text(payload.get("issued_for_user_id"), max_len=80) or ""
    current_user_id = _safe_user_id(user_obj)
    if issued_user_id and current_user_id and issued_user_id != current_user_id:
        raise PermissionError("unauthorized user")

    scope = _scope_payload(user_obj)
    token_scope_hash = _clean_text(payload.get("scope_hash"), max_len=120) or ""
    current_scope_hash = _clean_text(scope.get("scope_hash"), max_len=120) or ""
    if token_scope_hash and current_scope_hash and token_scope_hash != current_scope_hash:
        raise PermissionError("scope changed")

    token_perm_version = _clean_text(payload.get("permissions_version"), max_len=40) or ""
    current_perm_version = str(scope.get("permissions_version") or permissions_version())
    if token_perm_version and current_perm_version and token_perm_version != current_perm_version:
        raise PermissionError("permissions changed")

    context["back_href"] = _clean_text(payload.get("back_href"), max_len=512)
    return context


def _assert_context_permissions(context: Mapping[str, Any], *, user_obj: Any) -> None:
    if bool(current_app.config.get("LOGIN_DISABLED")) or bool(current_app.config.get("AUTHZ_DISABLED")):
        return
    requested_target = str(context.get("requested_target") or "workspace")
    if requested_target in ENTITY_PERMISSION_MAP:
        required = ENTITY_PERMISSION_MAP[requested_target]
    else:
        required = SOURCE_PERMISSION_MAP.get(str(context.get("source_page") or ""), ())
    if required and not user_has_permission(user_obj, *required):
        raise PermissionError("missing permission")


def _target_endpoint(context: Mapping[str, Any]) -> tuple[str, dict[str, str]] | None:
    requested = str(context.get("requested_target") or "workspace")
    entity_id = _clean_text(context.get("clicked_entity_id"), max_len=120)
    if requested == "customer" and entity_id:
        return "customers.drilldown", {"customer_id": entity_id}
    if requested == "product" and entity_id:
        return "products.drilldown", {"product_id": entity_id}
    if requested == "region" and entity_id:
        return "regions.drilldown", {"region_name": entity_id}
    if requested == "supplier" and entity_id:
        return "suppliers.drilldown", {"supplier_id": entity_id}
    if requested == "salesrep" and entity_id:
        return "salesreps.rep_detail", {"rep_id": entity_id}
    return None


def resolve_target_url(context: Mapping[str, Any], token: str, *, user_obj: Any) -> str:
    _assert_context_permissions(context, user_obj=user_obj)
    endpoint = _target_endpoint(context)
    if endpoint is None:
        return url_for("drilldowns.workspace", token=token)
    endpoint_name, path_kwargs = endpoint
    base = url_for(endpoint_name, **path_kwargs)
    query_pairs = _filters_query_pairs(context.get("active_filter_state") or {})
    if endpoint_name == "salesreps.rep_detail":
        query_pairs.extend(_target_query_pairs(context.get("target_query") or {}))
    query_pairs.append(("drill_context", token))
    query = urlencode(query_pairs, doseq=True)
    return f"{base}?{query}" if query else base


def decode_context_param(raw_value: str | None) -> dict[str, Any]:
    if not raw_value:
        raise ValueError("missing context")
    padded = str(raw_value).strip()
    padded += "=" * (-len(padded) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8")
    except Exception as exc:
        raise ValueError("invalid context") from exc
    try:
        payload = json.loads(decoded)
    except Exception as exc:
        raise ValueError("invalid context") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("invalid context")
    return sanitize_click_payload(payload)


def describe_filters(filter_store: Mapping[str, Any]) -> list[str]:
    items: list[str] = []
    start = _clean_text(filter_store.get("start_date"), max_len=20) if isinstance(filter_store, Mapping) else None
    end = _clean_text(filter_store.get("end_date"), max_len=20) if isinstance(filter_store, Mapping) else None
    if start or end:
        items.append(f"Window {start or 'start'} to {end or 'latest'}")
    for key, label in (
        ("regions", "Regions"),
        ("customers", "Customers"),
        ("products", "Products"),
        ("suppliers", "Suppliers"),
        ("sales_reps", "Sales Reps"),
        ("shipping_methods", "Methods"),
    ):
        values = list((filter_store.get(key) or []) if isinstance(filter_store, Mapping) else [])
        if not values:
            continue
        if len(values) == 1:
            items.append(f"{label}: {values[0]}")
        else:
            items.append(f"{label}: {len(values)} selected")
    return items


def build_context_banner(token: str | None, *, user_obj: Any) -> dict[str, Any] | None:
    if not token:
        return None
    context = load_context_token(token, user_obj=user_obj)
    clicked_label = _clean_text(context.get("clicked_entity_label") or context.get("clicked_metric") or context.get("clicked_bucket"), max_len=160)
    source_page = _source_page_label(context.get("source_page"))
    section = _clean_text(context.get("source_section"), max_len=120)
    widget = _clean_text(context.get("source_widget"), max_len=120)
    headline = "Drilled context active"
    parts = [part for part in (source_page, section, widget, clicked_label) if part]
    return {
        "headline": headline,
        "path": " > ".join(parts),
        "filter_chips": describe_filters(context.get("active_filter_state") or {}),
        "back_href": context.get("back_href"),
        "clear_href": _clear_drill_context_href(),
    }


def _clear_drill_context_href() -> str:
    params = request.args.copy()
    params.pop("drill_context", None)
    query = params.to_dict(flat=False)
    pairs: list[tuple[str, str]] = []
    for key, values in query.items():
        for value in values:
            pairs.append((key, str(value)))
    encoded = urlencode(pairs, doseq=True)
    return f"{request.path}?{encoded}" if encoded else request.path


def _metric_summary_cards(df: pd.DataFrame, *, show_costs: bool) -> list[dict[str, str]]:
    revenue_col = au.revenue_column(df) or au.resolve_column(df, ("Revenue", "revenue"))
    cost_col = au.cost_column(df)
    date_col = au.resolve_column(df, ("Date", "DateExpected", "OrderDate"))
    order_col = au.resolve_column(df, ("OrderId", "OrderID"))
    customer_col = au.resolve_column(df, ("CustomerId", "CustomerID"))
    product_col = au.resolve_column(df, ("ProductId", "SKU", "ProductName"))
    weight_col = au.resolve_column(df, ("WeightLb", "pack_weight_lb_sum"))

    revenue = float(au.safe_sum(df.get(revenue_col))) if revenue_col else 0.0
    cost = float(au.safe_sum(df.get(cost_col))) if cost_col and show_costs else 0.0
    profit = revenue - cost if show_costs and cost_col else None
    margin_pct = ((profit / revenue) * 100.0) if (profit is not None and revenue) else None
    orders = int(df[order_col].dropna().nunique()) if order_col and order_col in df.columns else 0
    customers = int(df[customer_col].dropna().nunique()) if customer_col and customer_col in df.columns else 0
    products = int(df[product_col].dropna().nunique()) if product_col and product_col in df.columns else 0
    weight = float(au.safe_sum(df.get(weight_col))) if weight_col else 0.0

    cards = [
        {"label": "Revenue", "value": f"${revenue:,.0f}", "detail": "Scoped slice revenue"},
        {"label": "Orders", "value": f"{orders:,}", "detail": "Distinct orders"},
        {"label": "Customers", "value": f"{customers:,}", "detail": "Visible customers"},
        {"label": "Products", "value": f"{products:,}", "detail": "Visible products"},
    ]
    if weight:
        cards.append({"label": "Weight", "value": f"{weight:,.0f} lb", "detail": "Scoped shipped weight"})
    if show_costs:
        cards.append({"label": "Profit", "value": "—" if profit is None else f"${profit:,.0f}", "detail": "Revenue minus cost"})
        cards.append({"label": "Margin", "value": "—" if margin_pct is None else f"{margin_pct:,.1f}%", "detail": "Profit as % of revenue"})
    if date_col and date_col in df.columns:
        dates = pd.to_datetime(df[date_col], errors="coerce").dropna()
        if not dates.empty:
            cards.append({"label": "Slice Window", "value": f"{dates.min().date()} to {dates.max().date()}", "detail": "Visible detail rows"})
    return cards[:6]


def _format_value_for_column(column: str, value: Any) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "—"
    label = str(column or "").strip().lower()
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    if "date" in label or label == "month":
        try:
            ts = pd.to_datetime(value, errors="coerce")
            if pd.notna(ts):
                return ts.date().isoformat()
        except Exception:
            pass
    if isinstance(value, (int, float)):
        num = float(value)
        if any(token in label for token in ("revenue", "profit", "cost", "price", "asp", "aov", "uplift", "delta")):
            return f"${num:,.2f}" if abs(num) < 100 else f"${num:,.0f}"
        if label.endswith("_pct") or "margin" in label or "share" in label or "confidence" in label or "support" in label or "lift" == label:
            if "lift" == label:
                return f"{num:,.2f}x"
            return f"{num:,.1f}%"
        if "weight" in label or label.endswith("_lb"):
            return f"{num:,.1f} lb" if abs(num) < 100 else f"{num:,.0f} lb"
        return f"{num:,.1f}" if abs(num) < 100 else f"{num:,.0f}"
    return str(value)


def _frame_to_table(df: pd.DataFrame, *, title: str, limit: int = 100) -> dict[str, Any]:
    if df is None or df.empty:
        return {"title": title, "columns": [], "rows": []}
    safe = df.copy().head(limit)
    columns = [{"key": str(col), "label": str(col).replace("_", " ").title()} for col in safe.columns]
    rows = []
    for record in safe.to_dict(orient="records"):
        rendered = {str(col): _format_value_for_column(str(col), record.get(col)) for col in safe.columns}
        rows.append(rendered)
    return {"title": title, "columns": columns, "rows": rows}


def _build_order_rollup(df: pd.DataFrame, *, show_costs: bool) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    date_col = au.resolve_column(df, ("Date", "DateExpected", "OrderDate"))
    order_col = au.resolve_column(df, ("OrderId", "OrderID"))
    customer_col = au.resolve_column(df, ("CustomerName", "CustomerId"))
    revenue_col = au.revenue_column(df) or au.resolve_column(df, ("Revenue",))
    cost_col = au.cost_column(df)
    weight_col = au.resolve_column(df, ("WeightLb", "pack_weight_lb_sum"))
    if not order_col or not revenue_col:
        return pd.DataFrame()
    work = pd.DataFrame(
        {
            "order_date": pd.to_datetime(df.get(date_col), errors="coerce") if date_col else pd.NaT,
            "order_id": df.get(order_col),
            "customer": df.get(customer_col) if customer_col else None,
            "revenue": pd.to_numeric(df.get(revenue_col), errors="coerce"),
            "cost": pd.to_numeric(df.get(cost_col), errors="coerce") if cost_col else 0.0,
            "weight_lb": pd.to_numeric(df.get(weight_col), errors="coerce") if weight_col else 0.0,
            "lines": 1,
        }
    ).dropna(subset=["order_id"])
    grouped = work.groupby(["order_date", "order_id", "customer"], dropna=False, as_index=False).agg(
        revenue=("revenue", "sum"),
        cost=("cost", "sum"),
        weight_lb=("weight_lb", "sum"),
        lines=("lines", "sum"),
    )
    grouped["profit"] = grouped["revenue"] - grouped["cost"] if show_costs else None
    grouped["margin_pct"] = grouped.apply(
        lambda row: ((float(row["profit"]) / float(row["revenue"])) * 100.0) if show_costs and row["revenue"] else None,
        axis=1,
    )
    grouped = grouped.sort_values(["order_date", "revenue"], ascending=[False, False])
    rename_map = {"order_date": "Date", "order_id": "Order", "customer": "Customer", "weight_lb": "Weight_lb", "lines": "Lines"}
    return grouped.rename(columns=rename_map)


def _build_product_rollup(df: pd.DataFrame, *, show_costs: bool) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    product_col = au.resolve_column(df, ("ProductName", "SKU", "ProductId"))
    category_col = au.resolve_column(df, ("Protein", "ProteinType", "ProteinName", "Category", "ProductCategory"))
    revenue_col = au.revenue_column(df) or au.resolve_column(df, ("Revenue",))
    cost_col = au.cost_column(df)
    weight_col = au.resolve_column(df, ("WeightLb", "pack_weight_lb_sum"))
    order_col = au.resolve_column(df, ("OrderId", "OrderID"))
    if not product_col or not revenue_col:
        return pd.DataFrame()
    work = pd.DataFrame(
        {
            "Product": df.get(product_col),
            "Category": df.get(category_col) if category_col else "Unassigned",
            "Revenue": pd.to_numeric(df.get(revenue_col), errors="coerce"),
            "Cost": pd.to_numeric(df.get(cost_col), errors="coerce") if cost_col else 0.0,
            "Weight_lb": pd.to_numeric(df.get(weight_col), errors="coerce") if weight_col else 0.0,
            "Order": df.get(order_col) if order_col else None,
        }
    )
    grouped = work.groupby(["Product", "Category"], dropna=False, as_index=False).agg(
        Revenue=("Revenue", "sum"),
        Cost=("Cost", "sum"),
        Weight_lb=("Weight_lb", "sum"),
        Orders=("Order", "nunique"),
    )
    grouped["Profit"] = grouped["Revenue"] - grouped["Cost"] if show_costs else None
    grouped["Margin_pct"] = grouped.apply(
        lambda row: ((float(row["Profit"]) / float(row["Revenue"])) * 100.0) if show_costs and row["Revenue"] else None,
        axis=1,
    )
    grouped = grouped.sort_values("Revenue", ascending=False)
    return grouped


def _apply_time_slice(df: pd.DataFrame, context: Mapping[str, Any]) -> pd.DataFrame:
    if df.empty:
        return df
    time_grain = str(context.get("clicked_time_grain") or "").strip().lower()
    time_value = _clean_text(context.get("clicked_time_value"), max_len=80)
    if not time_grain or not time_value:
        return df
    date_col = au.resolve_column(df, ("Date", "DateExpected", "OrderDate"))
    if not date_col or date_col not in df.columns:
        return df
    dates = pd.to_datetime(df[date_col], errors="coerce")
    if time_grain in {"month", "period_month"}:
        mask = dates.dt.to_period("M").astype("string") == str(time_value)
        return df.loc[mask.fillna(False)].copy()
    if time_grain in {"day", "date"}:
        mask = dates.dt.date.astype("string") == str(time_value)
        return df.loc[mask.fillna(False)].copy()
    if time_grain == "weekday":
        mask = dates.dt.day_name().str.lower() == str(time_value).lower()
        return df.loc[mask.fillna(False)].copy()
    return df


def _apply_extra_filters(df: pd.DataFrame, context: Mapping[str, Any]) -> pd.DataFrame:
    if df.empty:
        return df
    extra = (context.get("extra") or {}) if isinstance(context.get("extra"), Mapping) else {}
    category_value = _clean_text(extra.get("category"), max_len=160)
    if category_value:
        category_col = au.resolve_column(df, ("Protein", "ProteinType", "ProteinName", "Category", "ProductCategory"))
        if category_col and category_col in df.columns:
            mask = df[category_col].astype("string").fillna("").str.lower() == category_value.lower()
            df = df.loc[mask].copy()
    product_value = _clean_text(extra.get("product"), max_len=160)
    if product_value:
        product_col = au.resolve_column(df, ("ProductName", "SKU", "ProductId"))
        if product_col and product_col in df.columns:
            mask = df[product_col].astype("string").fillna("").str.lower() == product_value.lower()
            df = df.loc[mask].copy()
    return df


def _fact_workspace(context: Mapping[str, Any], *, user_obj: Any) -> dict[str, Any]:
    scope = _scope_payload(user_obj)
    show_costs = can_view_costs(user_obj)
    filters_store = context.get("active_filter_state") or {}
    extra = (context.get("extra") or {}) if isinstance(context.get("extra"), Mapping) else {}
    filter_mode = str(extra.get("filter_mode") or "current_window").strip().lower()
    params = filters_svc.parse_filters(filters_store)
    if filter_mode == "lifetime_visible":
        params = replace(params, start=None, end=None, preset="all")
    df = fact_store.query_fact(
        params,
        columns=DETAIL_COLUMNS,
        scope=scope,
        apply_default_window=False,
        use_cache=True,
    )
    df = _apply_time_slice(df, context)
    df = _apply_extra_filters(df, context)
    order_table = _build_order_rollup(df, show_costs=show_costs)
    product_table = _build_product_rollup(df, show_costs=show_costs)
    return {
        "summary_cards": _metric_summary_cards(df, show_costs=show_costs),
        "primary_table": _frame_to_table(order_table, title="Order detail", limit=MAX_ROWS),
        "secondary_table": _frame_to_table(product_table.head(40), title="Top contributors", limit=40),
        "rows_available": int(len(order_table.index)),
        "empty_message": "No detail rows matched the drilled context. Broaden the inherited filters or clear the time slice.",
    }


def _overview_workspace(context: Mapping[str, Any], *, user_obj: Any) -> dict[str, Any]:
    _assert_context_permissions({"requested_target": "workspace", "source_page": "overview"}, user_obj=user_obj)
    extra = (context.get("extra") or {}) if isinstance(context.get("extra"), Mapping) else {}
    drill_token = _clean_text(extra.get("drilldown"), max_len=40) or "movers"
    dimension = _clean_text(extra.get("dimension"), max_len=40) or None
    filters = filters_svc.parse_filters(context.get("active_filter_state") or {})
    frame = overview_v2.build_drilldown_frame(
        filters,
        drilldown=drill_token,
        dimension=dimension,
        include_current_month=False,
        defaulted_window=False,
    )
    if frame is None:
        frame = pd.DataFrame()
    frame = frame.replace({pd.NA: None}).copy()
    summary_cards = []
    if not frame.empty:
        summary_cards.append({"label": "Rows", "value": f"{len(frame.index):,}", "detail": "Resolved detail rows"})
        if "revenue" in frame.columns:
            revenue = pd.to_numeric(frame["revenue"], errors="coerce").sum()
            summary_cards.append({"label": "Revenue", "value": f"${float(revenue):,.0f}", "detail": "Visible impacted revenue"})
        top_label_col = next((col for col in ("label", "customer_name", "product_name", "region") if col in frame.columns), None)
        if top_label_col:
            top_value = _clean_text(frame.iloc[0].get(top_label_col), max_len=120)
            if top_value:
                summary_cards.append({"label": "Top row", "value": top_value, "detail": "Highest-ranked visible item"})
    return {
        "summary_cards": summary_cards,
        "primary_table": _frame_to_table(frame, title="Resolved overview detail", limit=MAX_ROWS),
        "secondary_table": None,
        "rows_available": int(len(frame.index)),
        "empty_message": "This overview drilldown did not resolve to any visible rows under the current filters and RBAC scope.",
    }


def _narrative_workspace(context: Mapping[str, Any]) -> dict[str, Any]:
    extra = (context.get("extra") or {}) if isinstance(context.get("extra"), Mapping) else {}
    summary_cards = []
    if context.get("clicked_metric_value") is not None and context.get("clicked_metric"):
        summary_cards.append(
            {
                "label": _clean_text(context.get("clicked_metric"), max_len=80) or "Metric",
                "value": _format_value_for_column(str(context.get("clicked_metric") or ""), context.get("clicked_metric_value")),
                "detail": "Clicked value",
            }
        )
    for key, label in (("confidence", "Confidence"), ("revenue_upside", "Revenue upside"), ("profit_upside", "Profit upside")):
        value = extra.get(key)
        if value is None:
            continue
        summary_cards.append({"label": label, "value": _format_value_for_column(key, value), "detail": "Context carried from source"})
    narrative = _clean_text(extra.get("narrative") or extra.get("detail") or extra.get("explanation"), max_len=1000)
    tag_rows = []
    for key, label in (("related_products", "Product"), ("related_categories", "Category")):
        for item in list(extra.get(key) or [])[:10]:
            text = _clean_text(item, max_len=160)
            if text:
                tag_rows.append({"Type": label, "Value": text})
    secondary = _frame_to_table(pd.DataFrame(tag_rows), title="Related context", limit=20) if tag_rows else None
    return {
        "summary_cards": summary_cards,
        "primary_table": None,
        "secondary_table": secondary,
        "rows_available": int(len(tag_rows)),
        "narrative": narrative,
        "empty_message": "This drilldown carries narrative context rather than a tabular slice.",
    }


def _salesreps_workspace(context: Mapping[str, Any], *, user_obj: Any) -> dict[str, Any]:
    scope = _scope_payload(user_obj)
    show_costs = can_view_costs(user_obj)
    filters = filters_svc.parse_filters(context.get("active_filter_state") or {})
    target_query = context.get("target_query") or {}
    extra = (context.get("extra") or {}) if isinstance(context.get("extra"), Mapping) else {}

    attribution_context = salesreps_bundle._salesrep_attribution_context(filters, scope, dict(target_query))
    if attribution_context.get("error"):
        message = _clean_text(((attribution_context.get("error") or {}).get("message")), max_len=240) or (
            "Sales rep attributed workspace could not be resolved."
        )
        return {
            "summary_cards": [],
            "primary_table": None,
            "secondary_table": None,
            "rows_available": 0,
            "narrative": _clean_text(extra.get("detail"), max_len=1000),
            "empty_message": message,
        }

    filter_clauses: list[str] = []
    filter_params: list[Any] = []

    filter_mode = (_clean_text(extra.get("filter_mode"), max_len=40) or "current_window").lower()
    include_yoy_window = bool(extra.get("include_yoy_window"))
    if filter_mode == "current_window":
        filter_clauses.append("is_current_window = 1")
    elif filter_mode == "comparison_window" or include_yoy_window:
        filter_clauses.append("(is_current_window = 1 OR is_yoy_window = 1)")

    time_grain = str(context.get("clicked_time_grain") or "").strip().lower()
    time_value = _clean_text(context.get("clicked_time_value"), max_len=80)
    if time_grain in {"month", "period_month"} and time_value:
        if include_yoy_window:
            filter_clauses.append(
                "strftime('%Y-%m', CASE WHEN is_yoy_window = 1 THEN order_date + INTERVAL 1 YEAR ELSE order_date END) = ?"
            )
        else:
            filter_clauses.append("strftime('%Y-%m', order_date) = ?")
        filter_params.append(time_value)
    elif time_grain == "quarter" and time_value:
        year_text, quarter_text = (time_value.split("-Q", 1) + [""])[:2]
        if year_text.isdigit() and quarter_text.isdigit():
            quarter_num = int(quarter_text)
            if 1 <= quarter_num <= 4:
                month_start = (quarter_num - 1) * 3 + 1
                month_end = month_start + 2
                if include_yoy_window:
                    filter_clauses.append(
                        "(strftime('%Y', CASE WHEN is_yoy_window = 1 THEN order_date + INTERVAL 1 YEAR ELSE order_date END) = ? "
                        "AND CAST(strftime('%m', CASE WHEN is_yoy_window = 1 THEN order_date + INTERVAL 1 YEAR ELSE order_date END) AS INTEGER) BETWEEN ? AND ?)"
                    )
                else:
                    filter_clauses.append(
                        "(strftime('%Y', order_date) = ? AND CAST(strftime('%m', order_date) AS INTEGER) BETWEEN ? AND ?)"
                    )
                filter_params.extend([year_text, month_start, month_end])
    elif time_grain == "year" and time_value and time_value.isdigit():
        if include_yoy_window:
            filter_clauses.append(
                "strftime('%Y', CASE WHEN is_yoy_window = 1 THEN order_date + INTERVAL 1 YEAR ELSE order_date END) = ?"
            )
        else:
            filter_clauses.append("strftime('%Y', order_date) = ?")
        filter_params.append(time_value)

    rep_token = _clean_text(extra.get("rep_id"), max_len=120)
    if not rep_token and str(context.get("clicked_entity_type") or "") == "salesrep":
        rep_token = _clean_text(context.get("clicked_entity_id"), max_len=120)
    if rep_token:
        filter_clauses.append("(LOWER(COALESCE(rep_key, '')) = LOWER(?) OR LOWER(COALESCE(rep_name, '')) = LOWER(?))")
        filter_params.extend([rep_token, rep_token])

    current_owner_token = _clean_text(extra.get("current_owner_id") or extra.get("current_owner_name"), max_len=120)
    if current_owner_token:
        filter_clauses.append(
            "(LOWER(COALESCE(current_owner_id, '')) = LOWER(?) OR LOWER(COALESCE(current_owner_name, '')) = LOWER(?))"
        )
        filter_params.extend([current_owner_token, current_owner_token])

    prior_rep_token = _clean_text(extra.get("prior_rep_id") or extra.get("prior_rep_name"), max_len=120)
    if prior_rep_token:
        filter_clauses.append("(LOWER(COALESCE(prior_rep_id, '')) = LOWER(?) OR LOWER(COALESCE(prior_rep_name, '')) = LOWER(?))")
        filter_params.extend([prior_rep_token, prior_rep_token])

    territory_name = _clean_text(extra.get("territory_name") or (context.get("clicked_bucket") if extra.get("bucket_type") == "territory" else None), max_len=160)
    if territory_name:
        filter_clauses.append(
            "LOWER(COALESCE(territory_name, territory_id, 'Unassigned')) = LOWER(?)"
        )
        filter_params.append(territory_name)

    protein_family = _clean_text(extra.get("protein_family") or (context.get("clicked_bucket") if extra.get("bucket_type") == "protein" else None), max_len=160)
    if protein_family:
        filter_clauses.append(
            "LOWER(COALESCE(protein_family, category_name, 'Unassigned')) = LOWER(?)"
        )
        filter_params.append(protein_family)

    if bool(extra.get("inherited_only")):
        filter_clauses.append("inherited_flag = 1")
    if bool(extra.get("direct_only")):
        filter_clauses.append("COALESCE(inherited_flag, 0) = 0")
    if bool(extra.get("transfer_activity_only")):
        filter_clauses.append("(ownership_changed = 1 OR owner_missing = 1)")

    dq_bucket = (_clean_text(extra.get("dq_bucket"), max_len=80) or "").lower()
    if dq_bucket in {"unassigned", "needs_review"}:
        filter_clauses.append("owner_missing = 1 OR LOWER(COALESCE(dq_status, '')) = 'needs_review'")
    elif dq_bucket in {"fact_fallback", "fact_owner_only"}:
        filter_clauses.append("LOWER(COALESCE(owner_source, '')) = 'fact_current_owner'")
    elif dq_bucket == "inactive_current_owner":
        filter_clauses.append("current_owner_active = FALSE")

    where_sql = " AND ".join(f"({clause})" for clause in filter_clauses) if filter_clauses else "1=1"
    cte_sql = attribution_context["cte_sql"]
    params = list(attribution_context.get("params") or []) + filter_params

    summary_sql = f"""
        WITH
        {cte_sql},
        scoped AS (
            SELECT *
            FROM attributed_base
            WHERE {where_sql}
        )
        SELECT
            COUNT(*) AS detail_rows,
            COUNT(DISTINCT order_id) AS orders,
            COUNT(DISTINCT customer_id) AS customers,
            COUNT(DISTINCT current_owner_id) AS owners,
            COUNT(DISTINCT territory_name) AS territories,
            COALESCE(SUM(revenue), 0) AS revenue,
            SUM(profit) AS profit,
            COALESCE(SUM(CASE WHEN inherited_flag = 1 THEN revenue ELSE 0 END), 0) AS inherited_revenue,
            COALESCE(SUM(CASE WHEN inherited_flag = 0 THEN revenue ELSE 0 END), 0) AS direct_revenue,
            COUNT(DISTINCT CASE WHEN inherited_flag = 1 THEN customer_id END) AS inherited_customers,
            COUNT(DISTINCT CASE WHEN inherited_flag = 0 THEN customer_id END) AS direct_customers
        FROM scoped
    """
    detail_sql = f"""
        WITH
        {cte_sql},
        scoped AS (
            SELECT *
            FROM attributed_base
            WHERE {where_sql}
        )
        SELECT
            order_date AS OrderDate,
            order_id AS OrderId,
            customer_name AS Customer,
            current_owner_name AS CurrentOwner,
            prior_rep_name AS InheritedFrom,
            territory_name AS Territory,
            product_name AS Product,
            protein_family AS Protein,
            revenue AS Revenue,
            {"profit AS Profit," if show_costs else ""}
            weight_lb AS WeightLb,
            CASE WHEN inherited_flag = 1 THEN 'Inherited' ELSE 'Direct' END AS Attribution,
            owner_source AS OwnerSource
        FROM scoped
        ORDER BY order_date DESC NULLS LAST, order_id DESC NULLS LAST
        LIMIT ?
    """
    secondary_sql = f"""
        WITH
        {cte_sql},
        scoped AS (
            SELECT *
            FROM attributed_base
            WHERE {where_sql}
        )
        SELECT
            COALESCE(customer_name, customer_id, 'Unassigned') AS Customer,
            COALESCE(SUM(revenue), 0) AS Revenue,
            {"SUM(profit) AS Profit," if show_costs else ""}
            COUNT(DISTINCT order_id) AS Orders
        FROM scoped
        GROUP BY 1
        ORDER BY Revenue DESC NULLS LAST, Customer
        LIMIT 20
    """

    summary_df = fact_store.execute_sql_df(summary_sql, params, tag="salesreps.workspace.summary")
    detail_df = fact_store.execute_sql_df(detail_sql, params + [MAX_ROWS], tag="salesreps.workspace.detail")
    secondary_df = fact_store.execute_sql_df(secondary_sql, params, tag="salesreps.workspace.customers")

    summary = summary_df.iloc[0].to_dict() if summary_df is not None and not summary_df.empty else {}
    detail_rows = int(summary.get("detail_rows") or 0)
    revenue = float(summary.get("revenue") or 0.0)
    inherited_revenue = float(summary.get("inherited_revenue") or 0.0)
    inherited_share = (inherited_revenue / revenue * 100.0) if revenue else None

    summary_cards = [
        {"label": "Revenue", "value": _format_value_for_column("revenue", summary.get("revenue")), "detail": "Attributed slice revenue"},
        {"label": "Orders", "value": _format_value_for_column("orders", summary.get("orders")), "detail": "Visible order detail rows"},
        {"label": "Customers", "value": _format_value_for_column("customers", summary.get("customers")), "detail": "Distinct impacted customers"},
        {
            "label": "Direct vs Inherited",
            "value": f"{_format_value_for_column('direct_revenue', summary.get('direct_revenue'))} / {_format_value_for_column('inherited_revenue', summary.get('inherited_revenue'))}",
            "detail": "Current-owner direct book vs inherited book",
        },
    ]
    if inherited_share is not None:
        summary_cards.append(
            {
                "label": "Inherited Exposure",
                "value": _format_value_for_column("share_pct", inherited_share),
                "detail": "Revenue share from inherited accounts",
            }
        )
    if show_costs:
        summary_cards.append(
            {
                "label": "Profit",
                "value": _format_value_for_column("profit", summary.get("profit")),
                "detail": "Visible profit where cost is available",
            }
        )

    narrative = _clean_text(extra.get("detail"), max_len=1000)
    if not narrative and territory_name:
        narrative = f"Attributed detail for territory {territory_name} under the current sales rep ownership model."
    elif not narrative and protein_family:
        narrative = f"Attributed detail for department {protein_family} under the current sales rep ownership model."

    return {
        "summary_cards": summary_cards[:6],
        "primary_table": _frame_to_table(detail_df, title="Attributed order detail", limit=MAX_ROWS),
        "secondary_table": _frame_to_table(secondary_df, title="Top customers in slice", limit=20),
        "rows_available": detail_rows,
        "narrative": narrative,
        "empty_message": "No attributed sales rep detail matched the drilled context under the current ownership model and RBAC scope.",
    }


def build_workspace_model(context: Mapping[str, Any], *, user_obj: Any) -> dict[str, Any]:
    _assert_context_permissions(context, user_obj=user_obj)
    extra = (context.get("extra") or {}) if isinstance(context.get("extra"), Mapping) else {}
    workspace_kind = _clean_text(extra.get("workspace_kind"), max_len=60) or "fact_orders"
    if workspace_kind == "overview_prebuilt":
        detail = _overview_workspace(context, user_obj=user_obj)
    elif workspace_kind == "narrative":
        detail = _narrative_workspace(context)
    elif workspace_kind == "salesreps_attributed":
        detail = _salesreps_workspace(context, user_obj=user_obj)
    else:
        detail = _fact_workspace(context, user_obj=user_obj)
    source_page = _source_page_label(context.get("source_page"))
    title = _clean_text(context.get("clicked_entity_label") or context.get("clicked_metric") or context.get("clicked_bucket"), max_len=160) or "Drilldown detail"
    subtitle_parts = [source_page]
    if context.get("source_section"):
        subtitle_parts.append(str(context.get("source_section")))
    if context.get("source_widget"):
        subtitle_parts.append(str(context.get("source_widget")))
    subtitle = "Drilled from " + " > ".join([part for part in subtitle_parts if part])
    model = {
        "title": title,
        "subtitle": subtitle,
        "filter_chips": describe_filters(context.get("active_filter_state") or {}),
        "clicked_metric": _clean_text(context.get("clicked_metric"), max_len=120),
        "clicked_metric_value": _format_value_for_column(
            str(context.get("clicked_metric") or ""),
            context.get("clicked_metric_value"),
        )
        if context.get("clicked_metric_value") is not None
        else None,
        "time_context": _clean_text(context.get("clicked_time_value"), max_len=80),
        "back_href": context.get("back_href"),
        "context": context,
        **detail,
    }
    return model


def workspace_export_response(model: Mapping[str, Any], *, token: str):
    primary = model.get("primary_table") if isinstance(model, Mapping) else None
    if not isinstance(primary, Mapping) or not primary.get("rows"):
        return None
    rows = primary.get("rows") or []
    df = pd.DataFrame.from_records(rows)
    if df.empty:
        return None
    stem = (_clean_text(model.get("title"), max_len=80) or "drilldown").replace(" ", "_").lower()
    return dataframe_to_csv_response(df, filename=f"{stem}_detail.csv")

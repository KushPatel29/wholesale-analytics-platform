from __future__ import annotations

import copy
import hashlib
import inspect
import json
import logging
import os
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from flask import g, has_request_context

from app.core import access_policy
from app.core.cache_manager import TTLValueCache
from app.core.exceptions import DatasetNotBuiltError
from app.services import fact_store
from app.services.filters import (
    FilterParams,
    build_filter_summary,
    canonical_filters_hash as _canonical_filters_hash,
    canonical_filters_json as _canonical_filters_json,
    filters_to_store,
    normalize_filters,
    parse_filters,
    sanitize_filters,
    resolve_effective_filters,
)

logger = logging.getLogger(__name__)

OPTIONS_TTL_MINUTES = int(os.getenv("FILTER_OPTIONS_TTL_MINUTES", os.getenv("FILTER_OPTIONS_TTL", "1060")))
OPTIONS_TTL_SECONDS = max(60, OPTIONS_TTL_MINUTES * 60)
OPTIONS_STALE_TTL_MINUTES = max(OPTIONS_TTL_MINUTES, int(os.getenv("FILTER_OPTIONS_STALE_TTL_MINUTES", "2880")))
OPTIONS_STALE_TTL_SECONDS = max(OPTIONS_TTL_SECONDS, OPTIONS_STALE_TTL_MINUTES * 60)
OPTION_QUERY_BUDGET_MS = max(500, int(os.getenv("FILTER_OPTIONS_QUERY_BUDGET_MS", "2500")))
_OPTIONS_CACHE = TTLValueCache(maxsize=64)
_OPTIONS_STALE_CACHE = TTLValueCache(maxsize=64)
_OPTION_GROUP_CACHE = TTLValueCache(maxsize=256)
_OPTION_GROUP_STALE_CACHE = TTLValueCache(maxsize=256)
OPTION_KEYS = ("statuses", "regions", "methods", "ship_methods", "customers", "suppliers", "products", "sales_reps", "protein_groups")
OPTION_KEY_SET = set(OPTION_KEYS)
_OPTION_ALIAS_MAP = {
    "shipping_methods": "methods",
    "ship_method": "methods",
    "shipping_method": "methods",
    "method": "methods",
    "customer": "customers",
    "supplier": "suppliers",
    "product": "products",
    "sales_rep": "sales_reps",
    "salesrep": "sales_reps",
    "salesreps": "sales_reps",
    "protein_group": "protein_groups",
    "meat_type": "protein_groups",
    "species": "protein_groups",
}
_PRIMARY_OPTION_KEYS = ("statuses", "regions", "methods", "customers", "suppliers", "products", "sales_reps", "protein_groups")
_OPTION_GROUPS = {
    "statuses": ("statuses",),
    "regions": ("regions",),
    "methods": ("methods", "ship_methods"),
    "ship_methods": ("methods", "ship_methods"),
    "customers": ("customers",),
    "suppliers": ("suppliers",),
    "products": ("products",),
    "sales_reps": ("sales_reps",),
    "protein_groups": ("protein_groups",),
}


def _clean_tokens(raw: Iterable[Any] | None) -> list[str]:
    vals: list[str] = []
    if raw is None:
        return vals
    try:
        for item in raw:
            if item is None:
                continue
            sval = str(item).strip()
            if sval:
                vals.append(sval)
    except Exception:
        pass
    return vals


def scope_from_user(user: Any) -> Dict[str, Any]:
    scope_obj = access_policy.scope_for_user(user, use_cache=True)
    scope = scope_obj.as_dict(include_allowed=True)
    try:
        scope["role"] = getattr(user, "role", None)
    except Exception:
        scope["role"] = None
    # Backward-compatible aliases for existing callers
    scope["sales_rep_ids"] = _clean_tokens(scope.get("allowed_erp_user_ids"))
    scope["is_super_user"] = bool(scope_obj.is_admin or scope_obj.scope_mode == "all")
    return scope


def canonical_json(filters: Any) -> str:
    """Stable JSON string for cache keys."""
    return _canonical_filters_json(filters)


def filters_hash(filters: Any) -> str:
    """Stable hash for filter payloads (used in cache keys/logging)."""
    return _canonical_filters_hash(filters)


def _request_options_cache_bucket() -> dict[str, Any] | None:
    try:
        if not has_request_context():
            return None
        bucket = getattr(g, "_filter_options_request_cache", None)
        if isinstance(bucket, dict):
            return bucket
        bucket = {}
        g._filter_options_request_cache = bucket
        return bucket
    except Exception:
        return None


def _clone_payload(value: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


def normalize_requested_option_keys(raw: Any = None) -> tuple[str, ...]:
    if raw is None:
        return tuple(OPTION_KEYS)

    values: list[str] = []
    if isinstance(raw, str):
        values.extend(part.strip() for part in raw.split(","))
    else:
        try:
            for item in raw:
                if item is None:
                    continue
                if isinstance(item, str):
                    values.extend(part.strip() for part in item.split(","))
                else:
                    values.append(str(item).strip())
        except Exception:
            values.append(str(raw).strip())

    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        token = str(item or "").strip().lower().replace("-", "_")
        if not token:
            continue
        token = _OPTION_ALIAS_MAP.get(token, token)
        if token not in OPTION_KEY_SET or token in seen:
            continue
        seen.add(token)
        normalized.append(token)

    if not normalized:
        return tuple(OPTION_KEYS)
    if "methods" in normalized and "ship_methods" not in seen:
        normalized.append("ship_methods")
    if "ship_methods" in normalized and "methods" not in seen:
        normalized.append("methods")
    return tuple(normalized)


def default_filters(user: Any = None) -> FilterParams:
    """Return default filters (current fiscal year) honoring any RBAC presets."""
    params = parse_filters({})
    scope = scope_from_user(user)
    if scope.get("scope_mode") != "all":
        allowed = _clean_tokens(scope.get("allowed_erp_user_ids"))
        if allowed:
            params = params.__class__(**{**params.__dict__, "sales_reps": tuple(allowed)})
    return normalize_filters(params)


def schema(filters: Any = None) -> Dict[str, Any]:
    params = normalize_filters(filters) if filters is not None else default_filters()
    defaults = filters_to_store(params)
    fields = [
        {"name": "start_date", "type": "date", "label": "Start Date", "default": defaults.get("start_date"), "aliases": ["start", "date_start"]},
        {"name": "end_date", "type": "date", "label": "End Date", "default": defaults.get("end_date"), "aliases": ["end", "date_end"]},
        {"name": "date_preset", "type": "select", "label": "Fiscal Range", "default": defaults.get("date_preset")},
        {"name": "date_type", "type": "hidden", "label": "Date Type", "default": defaults.get("date_type")},
        {"name": "statuses", "type": "multi", "label": "Statuses"},
        {"name": "regions", "type": "multi", "label": "Regions", "aliases": ["region_ids"]},
        {"name": "customers", "type": "multi", "label": "Customers", "aliases": ["customer_ids"]},
        {"name": "suppliers", "type": "multi", "label": "Suppliers", "aliases": ["supplier_ids"]},
        {"name": "products", "type": "multi", "label": "Products", "aliases": ["product_ids"]},
        {"name": "sales_reps", "type": "multi", "label": "Sales Reps", "aliases": ["sales_rep_ids"]},
        {"name": "shipping_methods", "type": "multi", "label": "Shipping Methods", "aliases": ["ship_method_ids", "methods"]},
        {"name": "protein_groups", "type": "multi", "label": "Protein Groups", "aliases": ["protein_group", "meat_type", "species"]},
        {"name": "yield_min", "type": "number", "label": "Yield Min %", "default": defaults.get("yield_min")},
        {"name": "yield_max", "type": "number", "label": "Yield Max %", "default": defaults.get("yield_max")},
    ]
    return {
        "fields": fields,
        "defaults": defaults,
        "dataset_version": fact_store.cache_buster(),
    }


def _list_expr(alias: str, col: Optional[str]) -> Optional[str]:
    if not col:
        return None
    safe = fact_store.quote_identifier(col)
    return f"list_sort(list(distinct CAST({safe} AS VARCHAR))) AS {alias}"


def _list_struct_expr(alias: str, id_col: Optional[str], label_col: Optional[str]) -> Optional[str]:
    if not id_col or not label_col:
        return None
    id_safe = fact_store.quote_identifier(id_col)
    label_safe = fact_store.quote_identifier(label_col)
    return (
        "list(distinct CASE WHEN {id_col} IS NULL THEN NULL "
        "ELSE struct_pack(id:=CAST({id_col} AS VARCHAR), "
        "label:=CAST(COALESCE({label_col}, {id_col}) AS VARCHAR)) END) AS {alias}"
    ).format(id_col=id_safe, label_col=label_safe, alias=alias)


def _option_selects(cols: set[str], requested_keys: tuple[str, ...]) -> List[str]:
    selects: List[str] = []
    requested = set(requested_keys or OPTION_KEYS)
    # Statuses use a single displayable string.
    if "statuses" in requested:
        status_col = fact_store.choose_column(("OrderStatus", "Status", "order_status"), cols)
        expr = _list_expr("statuses", status_col)
        if expr:
            selects.append(expr)

    def add_bucket(alias: str, id_candidates: Sequence[str], label_candidates: Sequence[str]) -> None:
        if alias not in requested:
            return
        id_col = fact_store.choose_column(id_candidates, cols)
        label_col = fact_store.choose_column(label_candidates, cols)
        if id_col and label_col and id_col != label_col:
            expr = _list_struct_expr(alias, id_col, label_col)
        else:
            expr = _list_expr(alias, id_col or label_col)
        if expr:
            selects.append(expr)

    # Align IDs with filter where-clause priority to avoid mismatches.
    add_bucket("regions", ("RegionId", "RegionName", "Region"), ("RegionName", "Region", "RegionId"))
    add_bucket(
        "methods",
        ("ShippingMethodName", "ShippingMethodLabel", "ShippingMethodRequested", "ShipMethod_Name"),
        ("ShippingMethodLabel", "ShippingMethodName", "ShippingMethodRequested", "ShipMethod_Name"),
    )
    add_bucket("customers", ("CustomerId", "CustomerName", "Customer"), ("CustomerName", "Customer", "CustomerId"))
    add_bucket("suppliers", ("SupplierId", "SupplierName", "Supplier"), ("SupplierName", "Supplier", "SupplierId"))
    add_bucket("products", ("ProductId", "SKU", "ProductName", "Product"), ("ProductName", "Product", "SKU", "ProductId"))
    add_bucket(
        "sales_reps",
        ("SalesRepId", "PrimarySalesRepId", "SalesRepName", "PrimarySalesRepName"),
        ("SalesRepName", "PrimarySalesRepName", "SalesRepId", "PrimarySalesRepId"),
    )
    from app.services import fact_schema
    add_bucket(
        "protein_groups",
        fact_schema.PROTEIN_CANDIDATES,
        fact_schema.PROTEIN_CANDIDATES,
    )
    return selects


def _coerce_option_value(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, dict):
        raw = val.get("id") or val.get("value") or val.get("label") or val.get("name")
        if raw is None:
            return None
        sval = str(raw).strip()
        return sval or None
    sval = str(val).strip()
    return sval or None


def _coerce_option_label(val: Any, fallback: str) -> str:
    if isinstance(val, dict):
        raw = val.get("label") or val.get("name") or val.get("title") or val.get("value") or val.get("id")
        if raw is not None:
            text = str(raw).strip()
            if text:
                return text
    return fallback


def _to_options(raw: Iterable[Any] | None, bucket: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if raw is None:
        return out
    seen: set[str] = set()
    for val in raw:
        sval = _coerce_option_value(val)
        if not sval:
            continue
        if sval in seen:
            continue
        seen.add(sval)
        label = _coerce_option_label(val, sval)
        out.append({"id": sval, "label": label, "bucket": bucket, "value": sval})
    if out:
        out.sort(key=lambda item: (item.get("label") or item.get("id") or "").lower())
    return out


def _normalize_options_map(raw_options: Any, *, requested_keys: Any = None) -> Dict[str, list[dict[str, str]]]:
    raw = raw_options if isinstance(raw_options, dict) else {}
    requested = normalize_requested_option_keys(requested_keys)
    options: Dict[str, list[dict[str, str]]] = {}
    for key in requested:
        raw_list = raw.get(key)
        if raw_list is None and key == "ship_methods":
            raw_list = raw.get("methods")
        options[key] = _to_options(raw_list or [], key)
    if "methods" in requested and "ship_methods" not in options:
        options["ship_methods"] = _to_options(raw.get("ship_methods") or raw.get("methods") or [], "ship_methods")
    if "ship_methods" in requested and "methods" not in options:
        options["methods"] = _to_options(raw.get("methods") or raw.get("ship_methods") or [], "methods")
    return options


def _option_counts(options: Dict[str, list[dict[str, str]]]) -> Dict[str, int]:
    return {key: len(options.get(key) or []) for key in options.keys()}


def selected_option_keys(filters: Any) -> tuple[str, ...]:
    params = normalize_filters(filters)
    requested: list[str] = []
    if getattr(params, "statuses", ()):
        requested.append("statuses")
    if getattr(params, "regions", ()):
        requested.append("regions")
    if getattr(params, "methods", ()):
        requested.append("methods")
    if getattr(params, "customers", ()):
        requested.append("customers")
    if getattr(params, "suppliers", ()):
        requested.append("suppliers")
    if getattr(params, "products", ()):
        requested.append("products")
    if getattr(params, "sales_reps", ()):
        requested.append("sales_reps")
    if getattr(params, "protein_groups", ()):
        requested.append("protein_groups")
    return normalize_requested_option_keys(requested)


def _option_query_groups(requested_keys: tuple[str, ...]) -> tuple[str, ...]:
    groups: list[str] = []
    seen: set[str] = set()
    for key in requested_keys:
        group = "methods" if key in {"methods", "ship_methods"} else key
        if group in seen:
            continue
        seen.add(group)
        groups.append(group)
    return tuple(groups)


def _scope_cache_payload(scope: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    scope_payload = scope or {}
    return {
        "scope_mode": scope_payload.get("scope_mode"),
        "allowed_count": scope_payload.get("allowed_count"),
        "scope_hash": scope_payload.get("scope_hash"),
        "permissions_version": scope_payload.get("permissions_version"),
    }


def _option_group_cache_key(
    filters: FilterParams,
    scope: Optional[Dict[str, Any]],
    group: str,
    *,
    requested_keys: Sequence[str],
) -> str:
    key_payload = {
        "endpoint": "filters.options.group",
        "group": group,
        "requested_dimensions": list(requested_keys),
        "filters": filters_to_store(filters),
        "filters_hash": filters_hash(filters),
        "scope": _scope_cache_payload(scope),
        "version": fact_store.cache_buster(),
    }
    return hashlib.sha256(json.dumps(key_payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _query_option_group(
    conn: Any,
    cols: set[str],
    where_sql: str,
    params: Sequence[Any],
    *,
    group_keys: Sequence[str],
) -> Dict[str, Any]:
    selects = _option_selects(cols, tuple(group_keys))
    if not selects:
        return {
            "options": _normalize_options_map({}, requested_keys=group_keys),
            "duration_ms": 0,
            "status": "unavailable",
        }
    group_started = time.perf_counter()
    sql = f"SELECT {', '.join(selects)} FROM fact WHERE {where_sql}"
    cursor = conn.execute(sql, list(params))
    row = cursor.fetchone()
    names = [meta[0] for meta in (cursor.description or [])]
    raw_group: Dict[str, Any] = {}
    if row is not None:
        for idx, name in enumerate(names):
            raw_group[name] = row[idx] if idx < len(row) else None
    return {
        "options": _normalize_options_map(raw_group, requested_keys=group_keys),
        "duration_ms": int((time.perf_counter() - group_started) * 1000),
        "status": "ok",
    }


def sanitize_filters_against_options(filters: Any, payload: Mapping[str, Any] | None) -> tuple[FilterParams, Dict[str, Any]]:
    params = normalize_filters(filters)
    raw_options = payload.get("options") if isinstance(payload, Mapping) else {}
    options = _normalize_options_map(raw_options)
    allowlists: Dict[str, Dict[str, str]] = {}
    for key in OPTION_KEYS:
        items = options.get(key) or []
        allowlists[key] = {}
        for item in items:
            raw = item.get("id") or item.get("value") or item.get("label")
            if raw in (None, ""):
                continue
            token = str(raw).strip()
            if not token:
                continue
            allowlists[key][token.lower()] = token

    dropped: Dict[str, list[str]] = {}

    def _sanitize(bucket: str, values: Iterable[str] | None, *, lower: bool = False) -> tuple[str, ...]:
        current = tuple(values or ())
        if not current:
            return tuple()
        allowlist = allowlists.get(bucket)
        if not allowlist:
            return current
        kept: list[str] = []
        seen: set[str] = set()
        for raw in current:
            token = str(raw).strip()
            if not token:
                continue
            canonical = allowlist.get(token.lower())
            if canonical is None:
                dropped.setdefault(bucket, [])
                if token not in dropped[bucket]:
                    dropped[bucket].append(token)
                continue
            normalized = canonical.lower() if lower else canonical
            compare = normalized.lower() if lower else normalized
            if compare in seen:
                continue
            seen.add(compare)
            kept.append(normalized)
        return tuple(kept)

    sanitized = FilterParams(
        start=params.start,
        end=params.end,
        statuses=_sanitize("statuses", getattr(params, "statuses", ()), lower=True),
        regions=_sanitize("regions", getattr(params, "regions", ())),
        methods=_sanitize("methods", getattr(params, "methods", ())),
        customers=_sanitize("customers", getattr(params, "customers", ())),
        suppliers=_sanitize("suppliers", getattr(params, "suppliers", ())),
        products=_sanitize("products", getattr(params, "products", ())),
        sales_reps=_sanitize("sales_reps", getattr(params, "sales_reps", ())),
        protein_groups=_sanitize("protein_groups", getattr(params, "protein_groups", ())),
        yield_min=params.yield_min,
        yield_max=params.yield_max,
        preset=params.preset,
        protein_min=params.protein_min,
        protein_max=params.protein_max,
        protein_name_like=params.protein_name_like,
        complete_months_only=params.complete_months_only,
    )
    meta = {
        "sanitized": bool(dropped),
        "dropped_filters": {key: list(values) for key, values in dropped.items()},
        "filters_notice": (
            "Some filters were removed because they are no longer available in the current filter options."
            if dropped else None
        ),
    }
    return sanitized, meta


def empty_options_payload(
    filters: Any,
    scope: Optional[Dict[str, Any]] = None,
    *,
    requested_keys: Any = None,
    error: Any = None,
) -> Dict[str, Any]:
    params = normalize_filters(filters)
    requested = normalize_requested_option_keys(requested_keys)
    meta = fact_store.get_meta()
    options = _normalize_options_map({}, requested_keys=requested)
    payload = {
        "options": options,
        "dataset_version": fact_store.cache_buster(),
        "filters": filters_to_store(params),
        "summary": build_filter_summary(params),
        "scope": scope or {},
        "date_min": meta.get("date_min") or meta.get("min_date"),
        "date_max": meta.get("date_max") or meta.get("max_date"),
        "cached": False,
        "duration_ms": 0,
        "meta": {
            "degraded": True,
            "requested_dimensions": list(requested),
            "option_counts": _option_counts(options),
            "dimension_meta": {
                key: {
                    "status": "error",
                    "duration_ms": 0,
                    "error": str(error) if error is not None else "Filter options are unavailable.",
                    "option_count": 0,
                }
                for key in options.keys()
            },
            "partial_failures": list(options.keys()),
            "stale": False,
            "stale_dimensions": [],
        },
    }
    if error is not None:
        payload["meta"]["error"] = str(error)
    return payload


def _apply_group_payload(
    options: Dict[str, list[dict[str, str]]],
    dimension_meta: Dict[str, Dict[str, Any]],
    stale_dimensions: list[str],
    group_keys: Sequence[str],
    group_payload: Mapping[str, Any],
    *,
    cache_hit: bool,
    cache_key: str | None = None,
    error: str | None = None,
) -> None:
    normalized_group = _normalize_options_map(group_payload.get("options"), requested_keys=group_keys)
    duration_ms = int(group_payload.get("duration_ms") or 0)
    group_is_stale = bool(group_payload.get("stale"))
    group_status = "stale" if group_is_stale else (group_payload.get("status") or "ok")
    if cache_key and not group_is_stale:
        _OPTION_GROUP_STALE_CACHE.set(
            cache_key,
            _clone_payload({**dict(group_payload), "options": normalized_group}),
            OPTIONS_STALE_TTL_SECONDS,
        )
    for key in group_keys:
        items = normalized_group.get(key) or []
        options[key] = items
        dimension_meta[key] = {
            "status": group_status,
            "duration_ms": duration_ms,
            "error": error,
            "option_count": len(items),
            "cached": bool(cache_hit),
            "stale": group_is_stale,
        }
        if group_is_stale and key not in stale_dimensions:
            stale_dimensions.append(key)


def _apply_group_failure(
    options: Dict[str, list[dict[str, str]]],
    dimension_meta: Dict[str, Dict[str, Any]],
    partial_failures: list[str],
    group_keys: Sequence[str],
    *,
    duration_ms: int,
    error: str,
) -> None:
    for key in group_keys:
        options[key] = []
        dimension_meta[key] = {
            "status": "error",
            "duration_ms": duration_ms,
            "error": error,
            "option_count": 0,
            "cached": False,
            "stale": False,
        }
        partial_failures.append(key)


def _options_payload(filters: FilterParams, scope: Dict[str, Any], *, requested_keys: tuple[str, ...]) -> Dict[str, Any]:
    cols = fact_store.list_columns()
    if not cols:
        raise DatasetNotBuiltError("Fact view not initialized.")
    requested = normalize_requested_option_keys(requested_keys)
    empty_options = _normalize_options_map({}, requested_keys=requested)
    query_groups = _option_query_groups(requested)
    if not query_groups:
        return {
            "options": empty_options,
            "dataset_version": fact_store.cache_buster(),
            "filters": filters_to_store(filters),
            "summary": build_filter_summary(filters),
            "meta": {
                "requested_dimensions": list(requested),
                "option_counts": _option_counts(empty_options),
                "dimension_meta": {},
                "partial_failures": [],
                "degraded": False,
            },
        }

    where_sql, params, start_iso, end_iso = fact_store.build_where_clause(
        filters, cols, scope, apply_default_window=True
    )
    conn = fact_store.get_conn()
    started = time.perf_counter()
    deadline = started + (OPTION_QUERY_BUDGET_MS / 1000.0)
    options: Dict[str, list[dict[str, str]]] = {}
    dimension_meta: Dict[str, Dict[str, Any]] = {}
    partial_failures: list[str] = []
    stale_dimensions: list[str] = []
    pending_groups: list[tuple[str, tuple[str, ...], str, Mapping[str, Any] | None]] = []

    for group in query_groups:
        group_keys = tuple(key for key in _OPTION_GROUPS.get(group, (group,)) if key in requested)
        if not group_keys:
            continue
        group_started = time.perf_counter()
        cache_key = _option_group_cache_key(filters, scope, group, requested_keys=group_keys)
        cached_group_payload = _OPTION_GROUP_CACHE.get(cache_key)
        stale_group_payload = _OPTION_GROUP_STALE_CACHE.get(cache_key)
        if cached_group_payload is None and group_started > deadline:
            if stale_group_payload is not None:
                _apply_group_payload(
                    options,
                    dimension_meta,
                    stale_dimensions,
                    group_keys,
                    {**dict(stale_group_payload), "stale": True},
                    cache_hit=True,
                    error=f"Served stale options after exceeding the {OPTION_QUERY_BUDGET_MS}ms options budget.",
                )
                continue
            _apply_group_failure(
                options,
                dimension_meta,
                partial_failures,
                group_keys,
                duration_ms=0,
                error=f"Skipped after exceeding the {OPTION_QUERY_BUDGET_MS}ms options budget.",
            )
            continue

        selects = _option_selects(cols, group_keys)
        if not selects:
            for key in group_keys:
                options[key] = []
                dimension_meta[key] = {
                    "status": "unavailable",
                    "duration_ms": 0,
                    "error": "Filter dimension is unavailable in the current dataset.",
                    "option_count": 0,
                    "cached": False,
                }
            continue

        if cached_group_payload is not None:
            _apply_group_payload(
                options,
                dimension_meta,
                stale_dimensions,
                group_keys,
                cached_group_payload,
                cache_hit=True,
                cache_key=cache_key,
            )
            continue

        pending_groups.append((group, group_keys, cache_key, stale_group_payload))

    if pending_groups:
        if time.perf_counter() > deadline:
            for _group, group_keys, _cache_key, stale_group_payload in pending_groups:
                if stale_group_payload is not None:
                    _apply_group_payload(
                        options,
                        dimension_meta,
                        stale_dimensions,
                        group_keys,
                        {**dict(stale_group_payload), "stale": True},
                        cache_hit=True,
                        error=f"Served stale options after exceeding the {OPTION_QUERY_BUDGET_MS}ms options budget.",
                    )
                    continue
                _apply_group_failure(
                    options,
                    dimension_meta,
                    partial_failures,
                    group_keys,
                    duration_ms=0,
                    error=f"Skipped after exceeding the {OPTION_QUERY_BUDGET_MS}ms options budget.",
                )
        else:
            batch_started = time.perf_counter()
            batched_group_keys = normalize_requested_option_keys(
                [key for _group, group_keys, _cache_key, _stale in pending_groups for key in group_keys]
            )
            try:
                batch_payload = _query_option_group(conn, cols, where_sql, params, group_keys=batched_group_keys)
                batch_duration_ms = int(batch_payload.get("duration_ms") or ((time.perf_counter() - batch_started) * 1000))
                batch_options = _normalize_options_map(batch_payload.get("options"), requested_keys=batched_group_keys)
                for _group, group_keys, cache_key, _stale_group_payload in pending_groups:
                    group_payload = {
                        "options": {key: batch_options.get(key) or [] for key in group_keys},
                        "duration_ms": batch_duration_ms,
                        "status": batch_payload.get("status") or "ok",
                        "stale": bool(batch_payload.get("stale")),
                    }
                    _OPTION_GROUP_CACHE.set(cache_key, _clone_payload(group_payload), OPTIONS_TTL_SECONDS)
                    _apply_group_payload(
                        options,
                        dimension_meta,
                        stale_dimensions,
                        group_keys,
                        group_payload,
                        cache_hit=False,
                        cache_key=cache_key,
                    )
            except Exception as exc:
                duration_ms = int((time.perf_counter() - batch_started) * 1000)
                for group, group_keys, _cache_key, stale_group_payload in pending_groups:
                    logger.exception(
                        "filters.options.dimension_failed",
                        extra={
                            "dimension_group": group,
                            "dimensions": list(group_keys),
                            "duration_ms": duration_ms,
                            "scope_mode": scope.get("scope_mode"),
                            "filter_hash": filters_hash(filters),
                        },
                    )
                    if stale_group_payload is not None:
                        _apply_group_payload(
                            options,
                            dimension_meta,
                            stale_dimensions,
                            group_keys,
                            {**dict(stale_group_payload), "stale": True},
                            cache_hit=True,
                            error=str(exc),
                        )
                        continue
                    _apply_group_failure(
                        options,
                        dimension_meta,
                        partial_failures,
                        group_keys,
                        duration_ms=duration_ms,
                        error=str(exc),
                    )

    meta = fact_store.get_meta()
    date_min = meta.get("date_min") or meta.get("min_date")
    date_max = meta.get("date_max") or meta.get("max_date")
    normalized_options = _normalize_options_map(options, requested_keys=requested)
    duration_ms = int((time.perf_counter() - started) * 1000)

    return {
        "options": normalized_options,
        "dataset_version": fact_store.cache_buster(),
        "duration_ms": duration_ms,
        "filters": filters_to_store(filters),
        "summary": build_filter_summary(filters),
        "scope": scope,
        "start": start_iso,
        "end": end_iso,
        "date_min": date_min,
        "date_max": date_max,
        "meta": {
            "requested_dimensions": list(requested),
            "option_counts": _option_counts(normalized_options),
            "dimension_meta": dimension_meta,
            "partial_failures": partial_failures,
            "stale": bool(stale_dimensions),
            "stale_dimensions": stale_dimensions,
            "degraded": bool(partial_failures or stale_dimensions),
        },
    }


def get_filter_options(
    filters: Any,
    scope: Optional[Dict[str, Any]] = None,
    *,
    use_cache: bool = True,
    requested_keys: Any = None,
) -> Dict[str, Any]:
    params = normalize_filters(filters)
    scope_payload = scope or {}
    version = fact_store.cache_buster()
    requested = normalize_requested_option_keys(requested_keys)
    cache_key_parts = {
        "endpoint": "filters.options",
        "filters": filters_to_store(params),
        "filters_hash": filters_hash(params),
        "scope": {
            "scope_mode": scope_payload.get("scope_mode"),
            "allowed_count": scope_payload.get("allowed_count"),
            "scope_hash": scope_payload.get("scope_hash"),
            "permissions_version": scope_payload.get("permissions_version"),
        },
        "requested_dimensions": list(requested),
        "version": version,
    }
    key = hashlib.sha256(json.dumps(cache_key_parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    def _build() -> Dict[str, Any]:
        payload = _options_payload(params, scope_payload, requested_keys=requested)
        payload["cached"] = False
        return payload

    if not use_cache:
        payload = _build()
        payload.setdefault("meta", {})
        payload["meta"]["cache_key"] = key
        payload["meta"]["cache_ttl"] = OPTIONS_TTL_SECONDS
        payload["meta"]["cached"] = False
        payload["meta"]["requested_dimensions"] = list(requested)
        return payload

    request_cache = _request_options_cache_bucket()
    stale_result = _OPTIONS_STALE_CACHE.get(key) if use_cache else None
    stale_error: Exception | None = None
    stale_fallback = False
    if request_cache is not None and key in request_cache:
        result = request_cache[key]
        hit = True
    else:
        try:
            result, hit = _OPTIONS_CACHE.get_or_compute(key, OPTIONS_TTL_SECONDS, _build)
        except Exception as exc:
            if stale_result is None:
                raise
            result = _clone_payload(stale_result)
            hit = True
            stale_fallback = True
            stale_error = exc
        if request_cache is not None:
            request_cache[key] = result
    if isinstance(result, dict):
        result["dataset_version"] = result.get("dataset_version", version)
        result["cached"] = bool(hit)
        result.setdefault("meta", {})
        result["meta"]["cache_key"] = key
        result["meta"]["cache_ttl"] = OPTIONS_TTL_SECONDS
        result["meta"]["cached"] = bool(hit)
        result["meta"]["requested_dimensions"] = list(requested)
        result.setdefault("date_min", None)
        result.setdefault("date_max", None)
        if isinstance(result.get("options"), dict):
            result["options"] = _normalize_options_map(result.get("options"), requested_keys=requested)
            result["meta"].setdefault("option_counts", _option_counts(result["options"]))
        result.setdefault("summary", build_filter_summary(params))
        result["meta"].setdefault("partial_failures", [])
        result["meta"].setdefault("stale_dimensions", [])
        result["meta"]["stale"] = bool(stale_fallback or result["meta"].get("stale_dimensions"))
        if stale_fallback:
            result["meta"]["degraded"] = True
            result["meta"]["stale_error"] = str(stale_error) if stale_error is not None else None
        else:
            result["meta"].setdefault("degraded", False)
        if not result["meta"].get("degraded"):
            _OPTIONS_STALE_CACHE.set(key, _clone_payload(result), OPTIONS_STALE_TTL_SECONDS)
    return result


def load_filter_options(
    filters: Any,
    scope: Optional[Dict[str, Any]] = None,
    *,
    requested_keys: Any = None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """
    Call the active options loader while tolerating older/narrower call signatures.

    Tests and emergency overrides sometimes replace `get_filter_options` with a
    minimal callable that only accepts `(filters, scope)`. Keep those paths working
    without degrading the entire filters flow.
    """
    loader = get_filter_options
    scope_payload = scope or {}

    accepts_scope = True
    accepts_varargs = False
    accepts_kwargs = False
    accepted_names: set[str] = set()
    try:
        signature = inspect.signature(loader)
    except (TypeError, ValueError):
        signature = None

    if signature is not None:
        positional_capacity = 0
        for param in signature.parameters.values():
            accepted_names.add(param.name)
            if param.kind == inspect.Parameter.VAR_POSITIONAL:
                accepts_varargs = True
            elif param.kind == inspect.Parameter.VAR_KEYWORD:
                accepts_kwargs = True
            elif param.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
                positional_capacity += 1
        accepts_scope = accepts_varargs or positional_capacity >= 2
    else:
        accepts_varargs = True
        accepts_kwargs = True

    kwargs: Dict[str, Any] = {}
    if accepts_kwargs or "requested_keys" in accepted_names:
        kwargs["requested_keys"] = requested_keys
    if accepts_kwargs or "use_cache" in accepted_names:
        kwargs["use_cache"] = use_cache

    if accepts_scope:
        return loader(filters, scope_payload, **kwargs)
    return loader(filters, **kwargs)


def validate_filters(
    filters: Any,
    scope: Optional[Dict[str, Any]] = None,
    *,
    requested_keys: Any = None,
    use_cache: bool = True,
) -> tuple[FilterParams, Dict[str, Any]]:
    scope_payload = scope or {}
    params, scope_meta = sanitize_filters(filters, scope_payload, include_meta=True, use_cache=use_cache)
    requested = normalize_requested_option_keys(requested_keys or selected_option_keys(params))
    option_meta: Dict[str, Any] = {"sanitized": False, "dropped_filters": {}, "filters_notice": None}
    validation_degraded = False
    if requested:
        try:
            options_payload = load_filter_options(
                params,
                scope_payload,
                requested_keys=requested,
                use_cache=use_cache,
            )
            params, option_meta = sanitize_filters_against_options(params, options_payload)
        except Exception as exc:
            validation_degraded = True
            option_meta = {
                "sanitized": False,
                "dropped_filters": {},
                "filters_notice": f"Filter validation was partially degraded: {exc}",
            }
            logger.exception(
                "filters.validation_failed",
                extra={
                    "dimensions": list(requested),
                    "scope_mode": scope_payload.get("scope_mode"),
                    "filter_hash": filters_hash(params),
                },
            )

    dropped: Dict[str, list[str]] = {}
    for source in (scope_meta.get("dropped") or {}, option_meta.get("dropped_filters") or {}):
        if not isinstance(source, Mapping):
            continue
        for key, values in source.items():
            bucket = dropped.setdefault(str(key), [])
            if not isinstance(values, Iterable) or isinstance(values, (str, bytes)):
                values = [values]
            for value in values:
                token = str(value).strip()
                if token and token not in bucket:
                    bucket.append(token)

    notice = option_meta.get("filters_notice") or scope_meta.get("notice")
    meta = {
        "sanitized": bool(scope_meta.get("sanitized") or option_meta.get("sanitized")),
        "dropped_filters": dropped,
        "filters_notice": notice,
        "validation_degraded": validation_degraded,
        "requested_dimensions": list(requested),
    }
    return params, meta


def options_etag(payload: Dict[str, Any]) -> str:
    basis = {
        "dataset_version": payload.get("dataset_version"),
        "filters": payload.get("filters"),
        "scope": payload.get("scope"),
        "date_min": payload.get("date_min"),
        "date_max": payload.get("date_max"),
        "options": payload.get("options"),
    }
    try:
        encoded = json.dumps(basis, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
    except Exception:
        return ""

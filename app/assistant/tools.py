from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field, replace
from html import escape as html_escape
from io import BytesIO
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, List, Mapping, Sequence

import pandas as pd
from cachetools import TTLCache

from app.core import rbac
from app.core.access_policy import get_current_scope
from app.core.exports import dataframes_to_xlsx_bytes, sanitize_filename
from app.core.sensitive_data import mask_json_payload, sensitive_access_flags, sensitive_field_category
from app.returns import service as returns_service
from app.services import (
    bundle_service,
    customers_bundle,
    fact_schema as fs,
    fact_store,
    overview_v2,
    products_bundle,
    regions_bundle,
    salesreps_bundle,
    suppliers_bundle,
)
from app.services.filters import FilterParams, filters_cache_key

from .digest_schedule_store import (
    create_schedule as create_digest_schedule_record,
    delete_schedule as delete_digest_schedule_record,
    get_schedule as get_digest_schedule_record,
    list_schedules as list_digest_schedule_records,
    mark_schedule_run as mark_digest_schedule_run,
)
from .export_job_store import enqueue_export_job
from .export_store import create_export
from .glossary import explain_metric, get_page_help, knowledge_snapshot, search_glossary


_OVERVIEW_CACHE = TTLCache(maxsize=256, ttl=90)
_MODULE_CACHE = TTLCache(maxsize=512, ttl=120)
_KNOWLEDGE_CACHE = TTLCache(maxsize=512, ttl=900)
_ENTITY_RESOLUTION_CACHE = TTLCache(maxsize=512, ttl=180)
_ENTITY_RESOLUTION_LOCK = RLock()
_OVERVIEW_LOCK = RLock()
_MODULE_LOCK = RLock()
_KNOWLEDGE_LOCK = RLock()


@dataclass
class ToolContext:
    user: Any
    page: str
    filters: FilterParams
    scope: Dict[str, Any]
    raw_context: Dict[str, Any] = field(default_factory=dict)
    page_state: Dict[str, Any] = field(default_factory=dict)
    sensitive_flags: Dict[str, bool] = field(default_factory=dict)
    enable_glossary: bool = True
    conversation: Dict[str, Any] = field(default_factory=dict)


def _jsonable(value: Any) -> Any:
    if isinstance(value, pd.DataFrame):
        return value.to_dict(orient="records")
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (int, float, bool, str)) or value is None:
        return value
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    return str(value)


def _mask(data: Any, user: Any) -> Any:
    try:
        return mask_json_payload(_jsonable(data), user)
    except Exception:
        return _jsonable(data)


def _scope_used(ctx: ToolContext) -> Dict[str, Any]:
    try:
        current = get_current_scope(use_cache=True)
        return current.as_dict(include_allowed=True)
    except Exception:
        return dict(ctx.scope or {})


def _window_used(ctx: ToolContext, meta: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "start": getattr(ctx.filters, "start", None).isoformat() if getattr(ctx.filters, "start", None) is not None else None,
        "end": getattr(ctx.filters, "end", None).isoformat() if getattr(ctx.filters, "end", None) is not None else None,
    }
    if isinstance(meta, Mapping):
        for key in ("window", "window_used"):
            raw_window = meta.get(key)
            if isinstance(raw_window, Mapping):
                out.update(
                    {
                        "start": raw_window.get("start") or out["start"],
                        "end": raw_window.get("end") or out["end"],
                        "rows": raw_window.get("rows") if raw_window.get("rows") is not None else out.get("rows"),
                    }
                )
        if meta.get("window_start") or meta.get("window_end"):
            out["start"] = meta.get("window_start") or out["start"]
            out["end"] = meta.get("window_end") or out["end"]
    return out


def _tool_response(
    *,
    status: str,
    title: str,
    data: Any = None,
    ctx: ToolContext,
    meta: Mapping[str, Any] | None = None,
    notes: List[str] | None = None,
    citations: List[str] | None = None,
    next_actions: List[str] | None = None,
    module: str | None = None,
) -> Dict[str, Any]:
    return {
        "status": status,
        "title": title,
        "module": module or ctx.page,
        "data": _mask(data, ctx.user),
        "scope_used": _scope_used(ctx),
        "window_used": _window_used(ctx, meta),
        "trust_flags": dict(ctx.sensitive_flags or {}),
        "notes": list(notes or []),
        "citations": list(citations or []),
        "next_actions": list(next_actions or []),
    }


def _forbidden(ctx: ToolContext, title: str, note: str, *, module: str | None = None) -> Dict[str, Any]:
    return _tool_response(
        status="forbidden",
        title=title,
        data={},
        ctx=ctx,
        notes=[note],
        module=module,
    )


def _module_forbidden(ctx: ToolContext, module: str, title: str) -> Dict[str, Any]:
    return _forbidden(ctx, title, f"{module.title()} module access is not granted for this user.", module=module)


def _module_access(ctx: ToolContext) -> Dict[str, bool]:
    return {
        "overview": bool(rbac.can_view_page("overview", ctx.user)),
        "customers": bool(rbac.can_view_page("customers", ctx.user)),
        "products": bool(rbac.can_view_page("products", ctx.user)),
        "regions": bool(rbac.can_view_page("regions", ctx.user)),
        "suppliers": bool(rbac.can_view_page("suppliers", ctx.user)),
        "salesreps": bool(rbac.can_view_page("salesreps", ctx.user)),
        "returns": bool(
            rbac.user_has_any_permission(
                ctx.user,
                "page.returns.view",
                "returns.create",
                "page.returns.customer_portal",
                "admin.returns.manage",
            )
        ),
        "admin": bool(rbac.can_view_page("admin", ctx.user)),
        "notifications": bool(rbac.can_view_page("notifications", ctx.user)),
    }


def _filters_source(ctx: ToolContext) -> Dict[str, Any]:
    return {
        "start": getattr(ctx.filters, "start", None).date().isoformat() if getattr(ctx.filters, "start", None) is not None else None,
        "end": getattr(ctx.filters, "end", None).date().isoformat() if getattr(ctx.filters, "end", None) is not None else None,
        "regions": list(getattr(ctx.filters, "regions", ()) or ()),
        "methods": list(getattr(ctx.filters, "methods", ()) or ()),
        "customers": list(getattr(ctx.filters, "customers", ()) or ()),
        "suppliers": list(getattr(ctx.filters, "suppliers", ()) or ()),
        "products": list(getattr(ctx.filters, "products", ()) or ()),
        "sales_reps": list(getattr(ctx.filters, "sales_reps", ()) or ()),
    }


def _entity_hint(ctx: ToolContext, args: Mapping[str, Any] | None, default_type: str) -> str | None:
    payload = dict(args or {})
    for key in ("id", "entity_id", f"{default_type}_id", default_type):
        token = payload.get(key)
        if token:
            return str(token)
    entity = ctx.raw_context.get("entity") if isinstance(ctx.raw_context.get("entity"), Mapping) else None
    if isinstance(entity, Mapping):
        etype = str(entity.get("type") or "").strip().lower()
        if not etype or etype == default_type:
            token = entity.get("id")
            if token:
                return str(token)
    page_state_entity = ctx.page_state.get("selected_entity") if isinstance(ctx.page_state.get("selected_entity"), Mapping) else None
    if isinstance(page_state_entity, Mapping):
        etype = str(page_state_entity.get("type") or "").strip().lower()
        if not etype or etype == default_type:
            token = page_state_entity.get("id")
            if token:
                return str(token)
    return None


def _selected_entity_blob(ctx: ToolContext) -> Dict[str, Any]:
    page_state_entity = ctx.page_state.get("selected_entity") if isinstance(ctx.page_state.get("selected_entity"), Mapping) else None
    if isinstance(page_state_entity, Mapping):
        return dict(page_state_entity)
    raw_entity = ctx.raw_context.get("entity") if isinstance(ctx.raw_context.get("entity"), Mapping) else None
    if isinstance(raw_entity, Mapping):
        return dict(raw_entity)
    return {}


def _entity_filter_attr(entity_type: str) -> str:
    token = _module_token(entity_type)
    mapping = {
        "customers": "customers",
        "products": "products",
        "regions": "regions",
        "suppliers": "suppliers",
        "salesreps": "sales_reps",
    }
    return mapping.get(token, "")


def _with_entity_filter(ctx: ToolContext, entity_type: str, filter_token: str) -> ToolContext:
    attr = _entity_filter_attr(entity_type)
    if not attr or not filter_token:
        return ctx
    current_values = tuple(getattr(ctx.filters, attr, ()) or ())
    if filter_token in current_values:
        return ctx
    updated_filters = replace(ctx.filters, **{attr: tuple([*current_values, filter_token])})
    return replace(ctx, filters=updated_filters)


def _lookup_norm(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def _entity_filter_token_from_row(entity_type: str, row: Mapping[str, Any]) -> str:
    token = _module_token(entity_type)
    key_candidates: Dict[str, Sequence[str]] = {
        "customers": ("customer_id", "id", "key", "CustomerId"),
        "products": ("product_id", "id", "key", "sku", "ProductId"),
        "regions": ("region", "region_name", "label", "RegionName"),
        "suppliers": ("supplier_id", "id", "key", "SupplierId"),
        "salesreps": ("rep_id", "salesrep_id", "id", "key", "SalesRepId"),
    }
    label_candidates: Dict[str, Sequence[str]] = {
        "customers": ("customer_name", "label", "name"),
        "products": ("product_name", "display_name", "label", "name"),
        "regions": ("region_name", "label", "name", "region"),
        "suppliers": ("supplier_name", "label", "name"),
        "salesreps": ("rep_name", "sales_rep", "label", "name"),
    }
    for key in key_candidates.get(token, ()):
        if row.get(key) not in (None, ""):
            return str(row.get(key))
    for key in label_candidates.get(token, ()):
        if row.get(key) not in (None, ""):
            return str(row.get(key))
    return _dimension_label_from_row(row, token)


def _read_salesrep_aliases() -> List[Dict[str, str]]:
    csv_path = Path(__file__).resolve().parents[1] / "core" / "userid.csv"
    if not csv_path.exists():
        return []
    rows: List[Dict[str, str]] = []
    try:
        for line in csv_path.read_text(encoding="utf-8").splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 3 or parts[0].lower().startswith("id"):
                continue
            full_name = " ".join(part for part in parts[1:3] if part).strip()
            if not full_name:
                continue
            rows.append({"id": parts[0], "label": full_name})
    except Exception:
        return []
    return rows


def _entity_resolution_candidates(ctx: ToolContext, entity_type: str, entity_name: str, *, limit: int = 12) -> List[Dict[str, Any]]:
    token = _module_token(entity_type)
    name = str(entity_name or "").strip()
    if token not in {"customers", "products", "regions", "suppliers", "salesreps"} or not name:
        return []
    cache_key = json.dumps(
        {
            "entity_type": token,
            "entity_name": name,
            "scope": _scope_used(ctx),
            "window": _window_used(ctx),
        },
        sort_keys=True,
        default=str,
    )
    with _ENTITY_RESOLUTION_LOCK:
        cached = _ENTITY_RESOLUTION_CACHE.get(cache_key)
    if isinstance(cached, list):
        return [dict(item) for item in cached]

    cols = fact_store.list_columns()
    id_candidates = {
        "customers": ("CustomerId", "CustomerID"),
        "products": ("ProductId", "SKU"),
        "regions": ("RegionId", "RegionName"),
        "suppliers": ("SupplierId", "SupplierID"),
        "salesreps": ("SalesRepId", "PrimarySalesRepId", "SalesRepUserId"),
    }
    label_candidates = {
        "customers": (fs.CANON.customer_name, "Customer", "CustomerId"),
        "products": (fs.CANON.product_name, "Product", "ProductId"),
        "regions": (fs.CANON.region, "Region"),
        "suppliers": (fs.CANON.supplier_name, "Supplier", "SupplierId"),
        "salesreps": (fs.CANON.sales_rep, "PrimarySalesRepName", "SalesRepId"),
    }
    id_col = fact_store.choose_column(id_candidates.get(token, ()), cols)
    label_col = fact_store.choose_column(label_candidates.get(token, ()), cols) or id_col
    if label_col is None:
        fallback = _read_salesrep_aliases() if token == "salesreps" else []
        ranked = _rank_entity_matches(fallback, name, limit=limit)
        with _ENTITY_RESOLUTION_LOCK:
            _ENTITY_RESOLUTION_CACHE[cache_key] = [dict(item) for item in ranked]
        return ranked

    where_sql, params, _, _ = fact_store.build_where_clause(ctx.filters, cols, _scope_used(ctx), apply_default_window=True)
    label_q = fact_store.quote_identifier(label_col)
    id_q = fact_store.quote_identifier(id_col) if id_col else "NULL"
    search_terms = [term for term in _lookup_norm(name).split() if term]
    if search_terms:
        like_clauses = [f"LOWER(CAST(COALESCE({label_q}, {id_q}) AS VARCHAR)) LIKE ?" for _ in search_terms]
        where_sql = f"({where_sql}) AND (" + " AND ".join(like_clauses) + ")"
        params = list(params) + [f"%{term}%" for term in search_terms]
    sql = f"""
        SELECT
            CAST({id_q} AS VARCHAR) AS entity_id,
            CAST(COALESCE({label_q}, {id_q}) AS VARCHAR) AS entity_label
        FROM fact
        WHERE {where_sql}
        GROUP BY 1, 2
        ORDER BY 2
        LIMIT ?
    """
    frame = fact_store.execute_sql_df(sql, list(params) + [max(25, limit * 4)], tag=f"assistant.resolve.{token}")
    rows = [
        {"id": str(row.get("entity_id") or "").strip(), "label": str(row.get("entity_label") or "").strip()}
        for row in frame.to_dict(orient="records")
        if str(row.get("entity_id") or row.get("entity_label") or "").strip()
    ]
    if token == "salesreps":
        rows.extend(_read_salesrep_aliases())
    ranked = _rank_entity_matches(rows, name, limit=limit)
    with _ENTITY_RESOLUTION_LOCK:
        _ENTITY_RESOLUTION_CACHE[cache_key] = [dict(item) for item in ranked]
    return ranked


def _rank_entity_matches(rows: Sequence[Mapping[str, Any]], query: str, *, limit: int = 12) -> List[Dict[str, Any]]:
    query_norm = _lookup_norm(query)
    terms = [term for term in query_norm.split() if term]
    ranked: List[Dict[str, Any]] = []
    for raw in rows:
        label = str(raw.get("label") or raw.get("name") or raw.get("id") or "").strip()
        entity_id = str(raw.get("id") or label).strip()
        if not label and not entity_id:
            continue
        label_norm = _lookup_norm(label)
        id_norm = _lookup_norm(entity_id)
        score = 0
        if query_norm and query_norm in {label_norm, id_norm}:
            score = 100
        elif query_norm and (label_norm.startswith(query_norm) or id_norm.startswith(query_norm)):
            score = 95
        elif query_norm and query_norm in label_norm:
            score = 90
        elif terms and all(term in label_norm or term in id_norm for term in terms):
            score = 84
        elif terms and any(term in label_norm or term in id_norm for term in terms):
            score = 72
        if score <= 0:
            continue
        ranked.append({"id": entity_id or label, "label": label or entity_id, "score": score})
    ranked.sort(key=lambda item: (-int(item.get("score") or 0), len(str(item.get("label") or "")), str(item.get("label") or "")))
    deduped: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in ranked:
        key = (str(item.get("id") or ""), str(item.get("label") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= max(1, limit):
            break
    return deduped


def _resolve_entity_reference_match(ctx: ToolContext, entity_type: str, entity_name: str) -> Dict[str, Any]:
    token = _module_token(entity_type)
    name = str(entity_name or "").strip()
    if not token or not name:
        return {}
    candidates = _entity_resolution_candidates(ctx, token, name, limit=8)
    top = candidates[0] if candidates else {}
    if not top:
        return {"entity_type": token, "query": name, "matched": False, "candidates": []}
    second_score = int((candidates[1] or {}).get("score") or 0) if len(candidates) > 1 else 0
    top_score = int(top.get("score") or 0)
    ambiguous = len(candidates) > 1 and top_score < 96 and abs(top_score - second_score) <= 4
    return {
        "entity_type": token,
        "query": name,
        "matched": bool(top_score >= 72 and not ambiguous),
        "ambiguous": ambiguous,
        "id": str(top.get("id") or "").strip(),
        "label": str(top.get("label") or "").strip(),
        "score": top_score,
        "candidates": candidates[:5],
        "filter_token": str(top.get("id") or top.get("label") or "").strip(),
    }


def _effective_filter_reference(ctx: ToolContext, args: Mapping[str, Any] | None, default_type: str) -> Dict[str, Any]:
    payload = dict(args or {})
    filter_type = _module_token(str(payload.get("filter_entity_type") or default_type or "")).strip().lower()
    filter_name = str(payload.get("filter_entity_name") or payload.get("selected_entity_name") or "").strip()
    filter_id = str(payload.get("filter_entity_id") or "").strip()
    selected = _selected_entity_blob(ctx)
    selected_type = _module_token(str(selected.get("type") or "").strip().lower())
    if not filter_type and selected_type:
        filter_type = selected_type
    if not filter_id and filter_type:
        filter_id = _entity_hint(ctx, payload, filter_type.rstrip("s")) or ""
    if not filter_name and filter_type and selected_type == filter_type:
        filter_name = str(selected.get("label") or selected.get("id") or "").strip()
    if filter_id:
        return {
            "entity_type": filter_type,
            "matched": True,
            "id": filter_id,
            "label": filter_name or filter_id,
            "filter_token": filter_id,
            "score": 100,
        }
    if filter_type and filter_name:
        return _resolve_entity_reference_match(ctx, filter_type, filter_name)
    return {}


def _hash_permissions(user: Any) -> str:
    return hashlib.sha1(",".join(sorted(rbac.effective_permissions(user))).encode("utf-8")).hexdigest()


def _payload_status(payload: Any, *, primary_keys: Sequence[str] = ()) -> str:
    if not isinstance(payload, Mapping):
        return "error"
    if payload.get("error"):
        return "error"
    if not primary_keys:
        return "ok"
    for key in primary_keys:
        value = payload.get(key)
        if isinstance(value, Mapping) and value:
            return "ok"
        if isinstance(value, list) and value:
            return "ok"
    return "empty"


def _top_rows(rows: Any, *, limit: int = 8, sort_key: str = "revenue") -> List[Dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    normalized = [dict(row) for row in rows if isinstance(row, Mapping)]
    if not normalized:
        return []

    def _num(value: Any) -> float:
        try:
            return float(value)
        except Exception:
            return 0.0

    if sort_key:
        normalized.sort(key=lambda row: _num(row.get(sort_key)), reverse=True)
    return normalized[: max(1, int(limit))]


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return float(default)
    if isinstance(value, bool):
        return float(default)
    try:
        return float(value)
    except Exception:
        return float(default)


def _num_or_none(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _pct(value: Any) -> str | None:
    num = _num_or_none(value)
    if num is None:
        return None
    return f"{num:.1f}%"


def _safe_get_number(data: Mapping[str, Any] | None, keys: Sequence[str]) -> float | None:
    if not isinstance(data, Mapping):
        return None
    for key in keys:
        if key in data:
            value = _num_or_none(data.get(key))
            if value is not None:
                return value
    return None


def _severity_from_score(score: float) -> str:
    if score >= 88:
        return "high"
    if score >= 68:
        return "medium"
    return "low"


def _module_token(module: str) -> str:
    token = str(module or "").strip().lower()
    mapping = {
        "sales_rep": "salesreps",
        "sales_reps": "salesreps",
        "salesrep": "salesreps",
        "customer": "customers",
        "product": "products",
        "region": "regions",
        "supplier": "suppliers",
    }
    return mapping.get(token, token or "overview")


def _can_export_module(ctx: ToolContext, module: str) -> bool:
    token = _module_token(module)
    if token == "assistant":
        token = "overview"
    if token not in {"overview", "customers", "products", "regions", "suppliers", "salesreps", "returns"}:
        return False
    return bool(rbac.can_export(token, ctx.user))


def _to_frame(data: Any, *, fallback_columns: Sequence[str] | None = None) -> pd.DataFrame:
    if isinstance(data, pd.DataFrame):
        return data.copy()
    if isinstance(data, Mapping):
        if not data:
            return pd.DataFrame(columns=list(fallback_columns or ()))
        return pd.DataFrame([dict(data)])
    if isinstance(data, list):
        rows = [dict(row) for row in data if isinstance(row, Mapping)]
        if rows:
            return pd.DataFrame(rows)
        return pd.DataFrame(columns=list(fallback_columns or ()))
    return pd.DataFrame(columns=list(fallback_columns or ()))


def _trend_to_frame(trend: Any, *, label_col: str = "period") -> pd.DataFrame:
    if isinstance(trend, list):
        rows = [dict(row) for row in trend if isinstance(row, Mapping)]
        if rows:
            return pd.DataFrame(rows)
        return pd.DataFrame(columns=[label_col, "value"])
    if not isinstance(trend, Mapping):
        return pd.DataFrame(columns=[label_col, "value"])
    labels = list(trend.get("labels") or [])
    value_keys = [key for key, value in trend.items() if key != "labels" and isinstance(value, list)]
    if not labels or not value_keys:
        # Sometimes trend comes as mapping of scalar keys.
        if all(not isinstance(v, (list, tuple, Mapping)) for v in trend.values()):
            return _to_frame(trend)
        return pd.DataFrame(columns=[label_col, "value"])
    limit = min(len(labels), *(len(trend.get(key) or []) for key in value_keys))
    rows: List[Dict[str, Any]] = []
    for idx in range(limit):
        row: Dict[str, Any] = {label_col: labels[idx]}
        for key in value_keys:
            values = list(trend.get(key) or [])
            row[key] = values[idx] if idx < len(values) else None
        rows.append(row)
    return pd.DataFrame(rows)


def _series_values_from_trend(trend: Any) -> List[float]:
    frame = _trend_to_frame(trend)
    if frame.empty:
        return []
    for column in frame.columns:
        if str(column).lower() in {"period", "label", "labels", "month", "date"}:
            continue
        numeric = pd.to_numeric(frame[column], errors="coerce").dropna().tolist()
        if numeric:
            return [float(value) for value in numeric]
    return []


def _metadata_frame(
    ctx: ToolContext,
    *,
    module: str,
    export_type: str,
    notes: Sequence[str] | None = None,
) -> pd.DataFrame:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    scope = _scope_used(ctx)
    window = _window_used(ctx)
    rows: List[Dict[str, Any]] = [
        {"field": "generated_at", "value": now},
        {"field": "generated_by", "value": str(getattr(ctx.user, "email", None) or getattr(ctx.user, "username", None) or getattr(ctx.user, "id", "unknown"))},
        {"field": "module", "value": module},
        {"field": "export_type", "value": export_type},
        {"field": "scope_mode", "value": scope.get("scope_mode")},
        {"field": "scope_allowed_count", "value": scope.get("allowed_count")},
        {"field": "window_start", "value": window.get("start")},
        {"field": "window_end", "value": window.get("end")},
        {"field": "cost_visible", "value": bool((ctx.sensitive_flags or {}).get("cost"))},
        {"field": "profit_visible", "value": bool((ctx.sensitive_flags or {}).get("profit"))},
        {"field": "margin_visible", "value": bool((ctx.sensitive_flags or {}).get("margin"))},
    ]
    for note in list(notes or []):
        rows.append({"field": "note", "value": str(note)})
    return pd.DataFrame(rows)


def _is_sensitive_export_allowed(ctx: ToolContext, column_name: str) -> bool:
    category = sensitive_field_category(str(column_name or ""))
    if category is None:
        return True
    flags = dict(ctx.sensitive_flags or sensitive_access_flags(ctx.user))
    if not bool(flags.get("export_sensitive")):
        return False
    if category == "cost":
        return bool(flags.get("cost"))
    if category == "margin":
        return bool(flags.get("margin"))
    if category == "profit":
        return bool(flags.get("profit"))
    if category == "recommendations":
        return bool(flags.get("recommendations"))
    if category == "margin_risk":
        return bool(flags.get("margin_risk"))
    return True


def _resolve_requested_columns(frame: pd.DataFrame, requested_columns: Sequence[str] | None = None) -> List[str]:
    if frame is None or not isinstance(frame, pd.DataFrame):
        return []
    if not requested_columns:
        return []
    actual = [str(col) for col in list(frame.columns)]
    lowered_map = {str(col).strip().lower(): str(col) for col in actual}
    selected: List[str] = []
    for token in list(requested_columns or []):
        key = str(token or "").strip()
        if not key:
            continue
        candidate = lowered_map.get(key.lower())
        if candidate and candidate not in selected:
            selected.append(candidate)
    return selected


def _apply_export_column_policy(
    ctx: ToolContext,
    frame: pd.DataFrame,
    *,
    requested_columns: Sequence[str] | None = None,
    include_all_allowed_columns: bool = False,
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    if frame is None or not isinstance(frame, pd.DataFrame):
        empty = pd.DataFrame()
        return empty, {
            "available_columns": [],
            "allowed_columns": [],
            "selected_columns": [],
            "excluded_columns": [],
            "requested_columns": list(requested_columns or []),
            "include_all_allowed_columns": bool(include_all_allowed_columns),
        }
    available = [str(col) for col in list(frame.columns)]
    allowed = [col for col in available if _is_sensitive_export_allowed(ctx, col)]
    excluded = [col for col in available if col not in allowed]
    requested = _resolve_requested_columns(frame, requested_columns=requested_columns)
    if requested:
        selected = [col for col in requested if col in allowed]
    elif include_all_allowed_columns:
        selected = list(allowed)
    else:
        selected = list(allowed)
    selected = [col for col in selected if col in frame.columns]
    output = frame[selected].copy() if selected else pd.DataFrame()
    policy = {
        "available_columns": available,
        "allowed_columns": allowed,
        "selected_columns": selected,
        "excluded_columns": excluded,
        "requested_columns": list(requested_columns or []),
        "include_all_allowed_columns": bool(include_all_allowed_columns),
    }
    return output, policy


def _normalize_chart_specs(
    chart_specs: Sequence[Mapping[str, Any]] | None,
    *,
    available_sheets: Sequence[str],
) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    available = {str(name): str(name) for name in list(available_sheets or [])}
    for raw in list(chart_specs or []):
        if not isinstance(raw, Mapping):
            continue
        source_sheet = str(raw.get("source_sheet") or "").strip()
        if source_sheet not in available:
            continue
        chart_type = str(raw.get("chart_type") or "line").strip().lower()
        if chart_type not in {"line", "bar", "column"}:
            chart_type = "line"
        normalized.append(
            {
                "source_sheet": source_sheet,
                "chart_sheet": str(raw.get("chart_sheet") or "Charts").strip()[:31] or "Charts",
                "chart_type": chart_type,
                "category_col": str(raw.get("category_col") or "").strip(),
                "value_cols": [str(item).strip() for item in list(raw.get("value_cols") or []) if str(item).strip()],
                "title": str(raw.get("title") or "").strip(),
                "x_axis": str(raw.get("x_axis") or "").strip(),
                "y_axis": str(raw.get("y_axis") or "").strip(),
                "insert_cell": str(raw.get("insert_cell") or "B2").strip() or "B2",
            }
        )
    return normalized


def _sheets_with_column_policy(
    ctx: ToolContext,
    sheets: Mapping[str, Any],
    *,
    requested_columns: Sequence[str] | None = None,
    include_all_allowed_columns: bool = False,
) -> tuple[Dict[str, pd.DataFrame], Dict[str, Dict[str, Any]]]:
    out: Dict[str, pd.DataFrame] = {}
    policy: Dict[str, Dict[str, Any]] = {}
    for name, frame in dict(sheets or {}).items():
        safe_name = str(name or "Sheet")[:31].replace("/", "_").replace(":", "_")
        frame_obj = frame if isinstance(frame, pd.DataFrame) else _to_frame(frame)
        filtered, column_policy = _apply_export_column_policy(
            ctx,
            frame_obj,
            requested_columns=requested_columns,
            include_all_allowed_columns=include_all_allowed_columns,
        )
        out[safe_name] = filtered
        policy[safe_name] = dict(column_policy)
    return out, policy


def _csv_bytes(frame: pd.DataFrame) -> bytes:
    if frame is None or not isinstance(frame, pd.DataFrame):
        return b""
    return frame.to_csv(index=False).encode("utf-8")


def _int_env(name: str, default: int) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except Exception:
        return int(default)


def _build_export_request_key(
    *,
    ctx: ToolContext,
    module: str,
    export_type: str,
    fmt: str,
    requested_columns: Sequence[str],
    include_all_allowed_columns: bool,
    include_chart: bool,
    chart_count: int,
    row_count: int,
) -> str:
    base = {
        "user": str(getattr(ctx.user, "id", "anon")),
        "module": str(module or ""),
        "export_type": str(export_type or ""),
        "format": str(fmt or ""),
        "requested_columns": [str(item) for item in list(requested_columns or [])],
        "include_all_allowed_columns": bool(include_all_allowed_columns),
        "include_chart": bool(include_chart),
        "chart_count": int(chart_count),
        "row_count": int(row_count),
        "scope": _scope_used(ctx),
        "window": _window_used(ctx),
    }
    digest = hashlib.sha256(json.dumps(base, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return f"export:{digest}"


def _build_export_artifact(
    *,
    user_id: Any,
    token_module: str,
    export_type: str,
    safe_sheets: Mapping[str, pd.DataFrame],
    filename_stem: str,
    fmt: str,
    chart_defs: Sequence[Mapping[str, Any]],
    include_all_allowed_columns: bool,
    column_policy: Mapping[str, Mapping[str, Any]],
    scope_used: Mapping[str, Any],
    window_used: Mapping[str, Any],
    export_plan: Mapping[str, Any],
) -> Dict[str, Any]:
    filename = sanitize_filename(filename_stem or f"{token_module}_assistant_export", default="assistant_export")
    file_data = b""
    content_type = "application/octet-stream"
    effective_format = fmt
    chart_embedded = False
    fallback_note = ""
    if fmt == "csv":
        primary_name = next((name for name in safe_sheets.keys() if name != "Metadata"), next(iter(safe_sheets.keys()), "Data"))
        primary_frame = safe_sheets.get(primary_name) if isinstance(primary_name, str) else None
        file_data = _csv_bytes(primary_frame if isinstance(primary_frame, pd.DataFrame) else pd.DataFrame())
        content_type = "text/csv"
        filename = f"{filename}.csv" if not filename.lower().endswith(".csv") else filename
    else:
        try:
            file_data = dataframes_to_xlsx_bytes(dict(safe_sheets), chart_specs=chart_defs)
            content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            filename = f"{filename}.xlsx" if not filename.lower().endswith(".xlsx") else filename
            chart_embedded = bool(chart_defs)
        except Exception:
            primary_name = next((name for name in safe_sheets.keys() if name != "Metadata"), next(iter(safe_sheets.keys()), "Data"))
            primary_frame = safe_sheets.get(primary_name) if isinstance(primary_name, str) else None
            file_data = _csv_bytes(primary_frame if isinstance(primary_frame, pd.DataFrame) else pd.DataFrame())
            content_type = "application/octet-stream"
            effective_format = "csv"
            filename = f"{filename}.csv" if not filename.lower().endswith(".csv") else filename
            fallback_note = "XLSX engine unavailable; generated CSV fallback."

    artifact = create_export(
        user_id,
        filename=filename,
        data=file_data,
        content_type=content_type,
        meta={
            "module": token_module,
            "export_type": export_type,
            "format": effective_format,
            "sheets": list(safe_sheets.keys()),
            "scope_mode": scope_used.get("scope_mode"),
            "window": dict(window_used or {}),
            "chart_requested": bool(chart_defs),
            "chart_embedded": bool(chart_embedded),
            "include_all_allowed_columns": bool(include_all_allowed_columns),
            "column_policy": {
                name: {
                    "allowed_count": len(list((meta or {}).get("allowed_columns") or [])),
                    "excluded_count": len(list((meta or {}).get("excluded_columns") or [])),
                }
                for name, meta in column_policy.items()
            },
            "export_plan": dict(export_plan or {}),
        },
    )
    return {
        "artifact": artifact,
        "format": effective_format,
        "chart_embedded": bool(chart_embedded),
        "fallback_note": fallback_note,
    }


def _chart_specs_for_sheets(
    sheets: Mapping[str, pd.DataFrame],
    *,
    module: str,
    include_chart: bool,
) -> List[Dict[str, Any]]:
    if not include_chart:
        return []
    specs: List[Dict[str, Any]] = []
    trend = sheets.get("Trend")
    if isinstance(trend, pd.DataFrame) and not trend.empty and len(trend.columns) >= 2:
        category = str(trend.columns[0])
        value_cols = [str(col) for col in list(trend.columns[1:4])]
        specs.append(
            {
                "source_sheet": "Trend",
                "chart_sheet": "Charts",
                "chart_type": "line",
                "category_col": category,
                "value_cols": value_cols,
                "title": f"{module.title()} Trend",
                "x_axis": category,
                "y_axis": "Value",
                "insert_cell": "B2",
            }
        )
    ranked = sheets.get("Ranked List")
    if isinstance(ranked, pd.DataFrame) and not ranked.empty:
        category_col = "label" if "label" in ranked.columns else str(ranked.columns[0])
        value_col = None
        for candidate in ("metric_value", "value", "revenue", "profit", "margin_pct"):
            if candidate in ranked.columns:
                value_col = candidate
                break
        if value_col:
            specs.append(
                {
                    "source_sheet": "Ranked List",
                    "chart_sheet": "Charts",
                    "chart_type": "bar",
                    "category_col": category_col,
                    "value_cols": [value_col],
                    "title": "Ranked Entities",
                    "x_axis": category_col,
                    "y_axis": value_col,
                    "insert_cell": "B22",
                }
            )
    grouped = sheets.get("Grouped Metric")
    if isinstance(grouped, pd.DataFrame) and not grouped.empty:
        category_col = "label" if "label" in grouped.columns else str(grouped.columns[0])
        value_col = "value" if "value" in grouped.columns else None
        if value_col:
            specs.append(
                {
                    "source_sheet": "Grouped Metric",
                    "chart_sheet": "Charts",
                    "chart_type": "bar",
                    "category_col": category_col,
                    "value_cols": [value_col],
                    "title": "Grouped Metric",
                    "x_axis": category_col,
                    "y_axis": value_col,
                    "insert_cell": "B42",
                }
            )
    return specs


def _register_workbook_export(
    ctx: ToolContext,
    *,
    module: str,
    export_type: str,
    sheets: Mapping[str, pd.DataFrame],
    filename_stem: str,
    notes: Sequence[str] | None = None,
    chart_specs: Sequence[Mapping[str, Any]] | None = None,
    output_format: str = "xlsx",
    requested_columns: Sequence[str] | None = None,
    include_all_allowed_columns: bool = False,
    export_plan: Mapping[str, Any] | None = None,
    async_export: bool = False,
) -> Dict[str, Any]:
    token_module = _module_token(module if module != "assistant" else "overview")
    if not _can_export_module(ctx, token_module):
        return _module_forbidden(ctx, token_module, "Assistant Excel Export")
    safe_sheets, column_policy = _sheets_with_column_policy(
        ctx,
        sheets,
        requested_columns=requested_columns,
        include_all_allowed_columns=include_all_allowed_columns,
    )
    fmt = str(output_format or "xlsx").strip().lower()
    if fmt not in {"xlsx", "csv"}:
        fmt = "xlsx"
    if "Metadata" not in safe_sheets:
        safe_sheets["Metadata"] = _metadata_frame(
            ctx,
            module=token_module,
            export_type=export_type,
            notes=[
                *(list(notes or [])),
                f"output_format={fmt}",
                f"include_all_allowed_columns={bool(include_all_allowed_columns)}",
            ],
        )
        column_policy["Metadata"] = {
            "available_columns": list(safe_sheets["Metadata"].columns),
            "allowed_columns": list(safe_sheets["Metadata"].columns),
            "selected_columns": list(safe_sheets["Metadata"].columns),
            "excluded_columns": [],
            "requested_columns": [],
            "include_all_allowed_columns": True,
        }

    chart_defs = _normalize_chart_specs(chart_specs, available_sheets=list(safe_sheets.keys()))
    row_count = int(sum(len(frame.index) for frame in safe_sheets.values() if isinstance(frame, pd.DataFrame)))
    scope_used = _scope_used(ctx)
    window_used = _window_used(ctx)
    export_plan_payload = dict(export_plan or {})
    user_id = getattr(ctx.user, "id", "anon")

    force_async = bool(async_export)
    async_row_threshold = max(2000, _int_env("ASSISTANT_EXPORT_ASYNC_ROW_THRESHOLD", 8000))
    should_async = bool(force_async or row_count >= async_row_threshold)
    request_key = _build_export_request_key(
        ctx=ctx,
        module=token_module,
        export_type=export_type,
        fmt=fmt,
        requested_columns=list(requested_columns or []),
        include_all_allowed_columns=bool(include_all_allowed_columns),
        include_chart=bool(chart_defs),
        chart_count=len(chart_defs),
        row_count=row_count,
    )

    def _build_artifact_sync() -> Dict[str, Any]:
        artifact_result = _build_export_artifact(
            user_id=user_id,
            token_module=token_module,
            export_type=export_type,
            safe_sheets=safe_sheets,
            filename_stem=filename_stem,
            fmt=fmt,
            chart_defs=chart_defs,
            include_all_allowed_columns=bool(include_all_allowed_columns),
            column_policy=column_policy,
            scope_used=scope_used,
            window_used=window_used,
            export_plan=export_plan_payload,
        )
        artifact = artifact_result.get("artifact")
        if artifact is None:
            raise RuntimeError("export_artifact_missing")
        return {
            "export_id": str(getattr(artifact, "export_id", "") or ""),
            "filename": str(getattr(artifact, "filename", "") or ""),
            "content_type": str(getattr(artifact, "content_type", "") or ""),
            "expires_at": float(getattr(artifact, "expires_at", 0) or 0),
            "meta": dict(getattr(artifact, "meta", {}) or {}),
            "format": str(artifact_result.get("format") or fmt),
            "chart_embedded": bool(artifact_result.get("chart_embedded")),
            "fallback_note": str(artifact_result.get("fallback_note") or ""),
        }

    if should_async:
        queued = enqueue_export_job(
            user_id,
            request_key=request_key,
            task=_build_artifact_sync,
            meta={
                "module": token_module,
                "export_type": export_type,
                "format": fmt,
                "row_count": row_count,
                "chart_count": len(chart_defs),
            },
        )
        queue_status = str(queued.get("status") or "").strip().lower()
        if queue_status in {"rate_limited", "busy"}:
            retry_after = int(queued.get("retry_after_seconds") or 10)
            return _tool_response(
                status="error",
                title="Assistant File Export",
                data={
                    "status": queue_status,
                    "retry_after_seconds": retry_after,
                    "message": "Export queue is busy. Please retry shortly.",
                    "row_count": row_count,
                    "sheets": list(safe_sheets.keys()),
                    "chart_count": len(chart_defs),
                },
                ctx=ctx,
                notes=[
                    "Export generation is bounded and rate-limited per user.",
                    f"Retry after about {retry_after} seconds.",
                ],
                module=token_module,
            )
        job = queued.get("job")
        if job is None:
            return _tool_response(
                status="error",
                title="Assistant File Export",
                data={"message": "Unable to enqueue export job."},
                ctx=ctx,
                notes=["Export queue unavailable."],
                module=token_module,
            )
        job_id = str(getattr(job, "job_id", "") or "")
        status_token = str(getattr(job, "status", "pending") or "pending").strip().lower()
        payload: Dict[str, Any] = {
            "job_id": job_id,
            "status": status_token,
            "status_url": f"/ai/exports/jobs/{job_id}",
            "api_status_url": f"/api/assistant/exports/jobs/{job_id}",
            "format": fmt,
            "row_count": row_count,
            "sheets": list(safe_sheets.keys()),
            "chart_count": len(chart_defs),
            "column_policy": column_policy,
            "include_all_allowed_columns": bool(include_all_allowed_columns),
            "export_plan": export_plan_payload,
        }
        if status_token == "completed":
            export_id = str(getattr(job, "export_id", "") or "")
            if export_id:
                payload.update(
                    {
                        "export_id": export_id,
                        "filename": str(getattr(job, "filename", "") or f"{filename_stem}.xlsx"),
                        "download_url": f"/ai/exports/{export_id}/download",
                        "api_download_url": f"/api/assistant/exports/{export_id}/download",
                    }
                )
        return _tool_response(
            status="ok",
            title="Assistant File Export",
            data=payload,
            ctx=ctx,
            notes=list(notes or [])
            + [
                "Export is generated asynchronously to keep the page responsive.",
                "Use status URL to track completion and retrieve download link.",
            ],
            citations=["assistant.export_job_store", "assistant.export_store"],
            next_actions=[
                "Wait for job status to become completed.",
                "Download workbook when ready.",
            ],
            module=token_module,
        )

    result = _build_artifact_sync()
    effective_format = str(result.get("format") or fmt)
    fallback_note = str(result.get("fallback_note") or "")
    export_id = str(result.get("export_id") or "")
    filename = str(result.get("filename") or "assistant_export.xlsx")
    download_url = f"/ai/exports/{export_id}/download"
    title = "Assistant Excel Export" if effective_format == "xlsx" else "Assistant File Export"
    tool_notes = list(notes or [])
    if fallback_note:
        tool_notes.append(fallback_note)
    return _tool_response(
        status="ok",
        title=title,
        data={
            "export_id": export_id,
            "filename": filename,
            "format": effective_format,
            "download_url": download_url,
            "api_download_url": f"/api/assistant/exports/{export_id}/download",
            "expires_at": float(result.get("expires_at") or 0),
            "sheets": list(safe_sheets.keys()),
            "chart_count": len(chart_defs),
            "chart_embedded": bool(result.get("chart_embedded")),
            "column_policy": column_policy,
            "include_all_allowed_columns": bool(include_all_allowed_columns),
            "export_plan": export_plan_payload,
            "row_count": row_count,
        },
        ctx=ctx,
        notes=tool_notes,
        citations=["assistant.export_store", "core.exports.dataframes_to_xlsx_bytes"],
        next_actions=[
            "Download workbook from the provided link.",
            "Ask to refine export scope/sheets if needed.",
        ],
        module=token_module,
    )


def _overview_context(ctx: ToolContext) -> Dict[str, Any]:
    cache_key = filters_cache_key(
        ctx.user,
        ctx.filters,
        extras={
            "scope": "assistant_overview_context",
            "page": ctx.page,
            "permissions": _hash_permissions(ctx.user),
        },
    )
    with _OVERVIEW_LOCK:
        cached = _OVERVIEW_CACHE.get(cache_key)
    if isinstance(cached, dict):
        return cached
    try:
        payload = overview_v2.build_overview_context(
            ctx.filters,
            user_scope=dict(_scope_used(ctx)),
            include_current_month=True,
            defaulted_window=False,
        )
    except Exception:
        payload = {
            "scorecard_kpis": [],
            "trend_series": {"monthly": {"labels": [], "revenue": []}},
            "movers": {"customer": {"gainers": []}, "product": {"gainers": []}},
            "risk": {"profitability": {"margin_risk": []}},
            "data_health": [],
            "warnings": ["overview_context_unavailable"],
        }
    with _OVERVIEW_LOCK:
        _OVERVIEW_CACHE[cache_key] = payload
    return payload


def _module_bundle(ctx: ToolContext, module: str, args: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    token = str(module or "").strip().lower()
    if token == "sales_reps":
        token = "salesreps"
    if token == "sales_rep":
        token = "salesreps"
    if token not in {"customers", "products", "regions", "suppliers", "salesreps"}:
        return {"error": {"message": f"Unsupported module '{module}'"}}
    extras_blob = {"module": token, "args": dict(args or {}), "permissions": _hash_permissions(ctx.user)}
    cache_key = filters_cache_key(
        ctx.user,
        ctx.filters,
        extras={"assistant_module": hashlib.sha1(json.dumps(extras_blob, sort_keys=True, default=str).encode("utf-8")).hexdigest()},
    )
    with _MODULE_LOCK:
        cached = _MODULE_CACHE.get(cache_key)
    if isinstance(cached, dict):
        return cached

    scope = _scope_used(ctx)
    source_args = dict(args or {})
    try:
        if token == "customers":
            payload = customers_bundle.build_customers_bundle(ctx.filters, scope, source_args)
        elif token == "products":
            payload = products_bundle.build_products_bundle(ctx.filters, scope, source_args)
        elif token == "regions":
            payload = regions_bundle.build_regions_bundle(ctx.filters, scope, source_args)
        elif token == "suppliers":
            payload = suppliers_bundle.build_suppliers_bundle(ctx.filters, scope, source_args)
        else:
            payload = salesreps_bundle.build_salesreps_bundle(ctx.filters, scope, source_args)
    except Exception as exc:
        payload = {"error": {"message": str(exc)}}

    if isinstance(payload, dict):
        with _MODULE_LOCK:
            _MODULE_CACHE[cache_key] = payload
    return payload


def _drilldown(entity: str, ctx: ToolContext, args: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    source = _filters_source(ctx)
    source.update(dict(args or {}))
    return bundle_service.drilldown(entity, source)


def _page_help_cached(page: str, section: str | None = None) -> Dict[str, Any]:
    key = f"{str(page).strip().lower()}::{str(section or '').strip().lower()}"
    with _KNOWLEDGE_LOCK:
        cached = _KNOWLEDGE_CACHE.get(key)
    if isinstance(cached, dict):
        return cached
    out = get_page_help(page, section=section)
    with _KNOWLEDGE_LOCK:
        _KNOWLEDGE_CACHE[key] = out
    return out


def get_user_scope(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    del args
    return _tool_response(
        status="ok",
        title="User Scope And Permissions",
        data={
            "scope": _scope_used(ctx),
            "module_access": _module_access(ctx),
            "sensitive_data_access": dict(ctx.sensitive_flags or sensitive_access_flags(ctx.user)),
        },
        ctx=ctx,
        notes=["Scope and permissions are enforced server-side for all assistant tools."],
        citations=["rbac.effective_permissions", "access_policy.scope_for_user"],
    )


def get_current_page_context(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    del args
    return _tool_response(
        status="ok",
        title="Current Page Context",
        data={
            "page": ctx.page,
            "filters": _jsonable(
                {
                    "start": getattr(ctx.filters, "start", None),
                    "end": getattr(ctx.filters, "end", None),
                    "regions": list(getattr(ctx.filters, "regions", ()) or ()),
                    "methods": list(getattr(ctx.filters, "methods", ()) or ()),
                    "customers": list(getattr(ctx.filters, "customers", ()) or ()),
                    "suppliers": list(getattr(ctx.filters, "suppliers", ()) or ()),
                    "products": list(getattr(ctx.filters, "products", ()) or ()),
                    "sales_reps": list(getattr(ctx.filters, "sales_reps", ()) or ()),
                }
            ),
            "page_state": dict(ctx.page_state or {}),
            "ui_context": dict(ctx.raw_context or {}),
        },
        ctx=ctx,
        notes=["When explicit page context is unavailable, sticky/global filters are used."],
        citations=["filters.resolve_filters", "assistant.request.context"],
    )


def get_overview_summary(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    del args
    if not rbac.can_view_page("overview", ctx.user):
        return _module_forbidden(ctx, "overview", "Overview Summary")
    bundle_ctx = _overview_context(ctx)
    bundle = bundle_ctx.get("bundle") or {}
    briefing = bundle.get("executive_briefing") or {}
    meta = bundle.get("meta") or {}
    data = {
        "biggest_win": briefing.get("biggest_win"),
        "biggest_decline": briefing.get("biggest_decline"),
        "key_risk": briefing.get("key_risk"),
        "top_action": briefing.get("top_action"),
        "watchouts": briefing.get("watchouts") or [],
        "recommended_actions": briefing.get("recommended_actions") or [],
        "narrative": (bundle_ctx.get("narrative_insights") or {}).get("narrative") or [],
    }
    return _tool_response(
        status="ok",
        title="Overview Executive Briefing",
        data=data,
        ctx=ctx,
        meta=meta,
        notes=["Summary is computed from live overview bundle data under current scope and filters."],
        citations=["overview_v2.build_overview_context"],
        next_actions=[
            "Ask for top movers to isolate who drove the change.",
            "Ask for concentration risk to assess dependency exposure.",
            "Ask for data health to verify confidence before actioning.",
        ],
        module="overview",
    )


def get_overview_kpis(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    del args
    if not rbac.can_view_page("overview", ctx.user):
        return _module_forbidden(ctx, "overview", "Overview KPI Command Center")
    bundle_ctx = _overview_context(ctx)
    scorecard = dict((bundle_ctx.get("scorecard_kpis") or {}))
    if not rbac.can_view_costs(ctx.user):
        for key in ("profit", "margin_pct", "profit_per_order", "profit_per_lb", "profit_mom", "profit_yoy"):
            scorecard[key] = None
    elif not rbac.can_view_profit(ctx.user):
        for key in ("profit", "profit_per_order", "profit_per_lb", "profit_mom", "profit_yoy"):
            scorecard[key] = None
    if not rbac.can_view_margin(ctx.user):
        for key in ("margin_pct", "margin_mom", "margin_yoy"):
            scorecard[key] = None
    meta = ((bundle_ctx.get("bundle") or {}).get("meta") or {})
    return _tool_response(
        status="ok",
        title="Overview KPI Command Center",
        data=scorecard,
        ctx=ctx,
        meta=meta,
        citations=["overview_v2.build_overview_context"],
        notes=["Sensitive KPI fields are permission-filtered before synthesis."],
        module="overview",
    )


def get_trend_series(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    if not rbac.can_view_page("overview", ctx.user):
        return _module_forbidden(ctx, "overview", "Trend Diagnostics")
    metric = str(args.get("metric") or "revenue").strip().lower()
    grain = str(args.get("grain") or "monthly").strip().lower()
    sensitive_metric = {
        "profit": "profit",
        "margin": "margin",
        "margin_pct": "margin",
        "cost": "cost",
    }.get(metric)
    if sensitive_metric == "cost" and not rbac.can_view_costs(ctx.user):
        return _forbidden(ctx, "Trend Diagnostics", "Cost series is hidden for this user.", module="overview")
    if sensitive_metric == "profit" and not rbac.can_view_profit(ctx.user):
        return _forbidden(ctx, "Trend Diagnostics", "Profit series is hidden for this user.", module="overview")
    if sensitive_metric == "margin" and not rbac.can_view_margin(ctx.user):
        return _forbidden(ctx, "Trend Diagnostics", "Margin series is hidden for this user.", module="overview")

    bundle_ctx = _overview_context(ctx)
    trend = bundle_ctx.get("trend_series") or {}
    bucket = trend.get(grain) if isinstance(trend, Mapping) else {}
    if not isinstance(bucket, Mapping):
        bucket = {}
    if not bucket:
        bucket = trend if isinstance(trend, Mapping) else {}
    labels = list(bucket.get("labels") or [])
    values = list(bucket.get(metric) or [])
    payload = {"grain": grain, "metric": metric, "labels": labels, "values": values}
    return _tool_response(
        status="ok" if labels else "empty",
        title="Trend Diagnostics",
        data=payload,
        ctx=ctx,
        meta=((bundle_ctx.get("bundle") or {}).get("meta") or {}),
        citations=["overview_v2.build_overview_context"],
        notes=["Trend output follows active filter window and scope."],
        module="overview",
    )


def get_price_volume_mix(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    del args
    if not rbac.can_view_page("overview", ctx.user):
        return _module_forbidden(ctx, "overview", "Price / Volume / Mix Drivers")
    bundle_ctx = _overview_context(ctx)
    drivers = (bundle_ctx.get("drivers") or {})
    return _tool_response(
        status="ok" if drivers else "empty",
        title="Price / Volume / Mix Drivers",
        data=drivers,
        ctx=ctx,
        citations=["overview_v2.build_overview_context"],
        notes=["Driver decomposition uses the same backend logic as the overview diagnostics workspace."],
        module="overview",
    )


def get_top_movers(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    if not rbac.can_view_page("overview", ctx.user):
        return _module_forbidden(ctx, "overview", "Top Movers")
    dimension = str(args.get("dimension") or "customer").strip().lower()
    normalized = {
        "customers": "customer",
        "customer": "customer",
        "products": "product",
        "product": "product",
        "regions": "region",
        "region": "region",
        "suppliers": "supplier",
        "supplier": "supplier",
        "salesreps": "salesrep",
        "sales_rep": "salesrep",
        "salesrep": "salesrep",
    }.get(dimension, "customer")
    bundle_ctx = _overview_context(ctx)
    movers = (bundle_ctx.get("movers") or {})
    selected = movers.get(normalized) if isinstance(movers, Mapping) else None
    if not isinstance(selected, Mapping):
        selected = {}
    return _tool_response(
        status="ok" if selected else "empty",
        title=f"Top Movers ({normalized.title()})",
        data={
            "dimension": normalized,
            "gainers": list(selected.get("gainers") or []),
            "decliners": list(selected.get("decliners") or []),
        },
        ctx=ctx,
        citations=["overview_v2.build_overview_context"],
        notes=["Movers use low-base guardrails from overview bundle logic."],
        module="overview",
    )


def get_concentration_risk(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    del args
    if not rbac.can_view_page("overview", ctx.user):
        return _module_forbidden(ctx, "overview", "Concentration And Dependency Risk")
    bundle_ctx = _overview_context(ctx)
    risk_block = (bundle_ctx.get("risk") or {}).get("concentration") or {}
    return _tool_response(
        status="ok" if risk_block else "empty",
        title="Concentration And Dependency Risk",
        data=risk_block,
        ctx=ctx,
        citations=["overview_v2.build_overview_context"],
        module="overview",
    )


def get_margin_watchlist(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    del args
    if not rbac.can_view_margin_risk(ctx.user):
        return _forbidden(ctx, "Margin Risk Watchlist", "Margin risk data is hidden for this user.", module="overview")
    bundle_ctx = _overview_context(ctx)
    profitability = (bundle_ctx.get("risk") or {}).get("profitability") or {}
    rows = list(profitability.get("margin_risk") or [])
    return _tool_response(
        status="ok" if rows else "empty",
        title="Margin Risk Watchlist",
        data={"rows": rows[:50], "count": len(rows)},
        ctx=ctx,
        citations=["overview_v2.build_overview_context"],
        notes=["Rows are filtered by scope and sensitive-data permissions."],
        module="overview",
    )


def get_forecast_summary(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    del args
    if not rbac.can_view_page("overview", ctx.user):
        return _module_forbidden(ctx, "overview", "Forecast Outlook")
    bundle_ctx = _overview_context(ctx)
    forecast = bundle_ctx.get("forecast") or {}
    status = "ok" if bool(forecast.get("enabled")) else "empty"
    return _tool_response(
        status=status,
        title="Forecast Outlook",
        data=forecast,
        ctx=ctx,
        citations=["overview_v2.build_overview_context"],
        notes=["Forecast availability is gated by history depth and quality checks."],
        module="overview",
    )


def get_data_health(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    del args
    if not rbac.can_view_page("overview", ctx.user):
        return _module_forbidden(ctx, "overview", "Data Trust And Health")
    bundle_ctx = _overview_context(ctx)
    health = bundle_ctx.get("data_health") or {}
    return _tool_response(
        status="ok" if health else "empty",
        title="Data Trust And Health",
        data=health,
        ctx=ctx,
        citations=["overview_v2.build_overview_context"],
        next_actions=[
            "Download data issues from the Data Health section for remediation.",
            "Re-run analysis after cost/pack coverage gaps are fixed.",
        ],
        module="overview",
    )


def _detail_tool(ctx: ToolContext, *, entity: str, page_perm: str, drill_perm: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if not rbac.user_has_permission(ctx.user, page_perm):
        return _forbidden(ctx, f"{entity.title()} Detail", f"{entity.title()} module access is not granted.", module=entity)
    if not rbac.user_has_permission(ctx.user, drill_perm):
        return _forbidden(ctx, f"{entity.title()} Detail", f"{entity.title()} drilldown access is not granted.", module=entity)
    payload = _drilldown(entity, ctx, args)
    if not isinstance(payload, Mapping):
        return _tool_response(status="error", title=f"{entity.title()} Detail", data={}, ctx=ctx, notes=["Unexpected drilldown payload."], module=entity)
    if payload.get("error"):
        return _tool_response(
            status="empty",
            title=f"{entity.title()} Detail",
            data={"error": payload.get("error")},
            ctx=ctx,
            meta=payload.get("meta") if isinstance(payload.get("meta"), Mapping) else None,
            module=entity,
        )
    compact = {
        "kpis": payload.get("kpis"),
        "trend": payload.get("trend"),
        "table": payload.get("table"),
        "meta": payload.get("meta"),
        "warnings": payload.get("warnings") or [],
    }
    return _tool_response(
        status="ok",
        title=f"{entity.title()} Detail",
        data=compact,
        ctx=ctx,
        meta=payload.get("meta") if isinstance(payload.get("meta"), Mapping) else None,
        citations=[f"bundle_service.drilldown:{entity}"],
        module=entity,
    )


def get_customer_detail(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    customer_id = args.get("customer_id") or args.get("id") or _entity_hint(ctx, args, "customer")
    args.setdefault("customer_id", customer_id)
    args.setdefault("id", customer_id)
    return _detail_tool(
        ctx,
        entity="customers",
        page_perm="page.customers.view",
        drill_perm="page.customers.drilldown.view",
        args=args,
    )


def get_product_detail(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    product_id = args.get("product_id") or args.get("id") or _entity_hint(ctx, args, "product")
    args.setdefault("product_id", product_id)
    args.setdefault("id", product_id)
    return _detail_tool(
        ctx,
        entity="products",
        page_perm="page.products.view",
        drill_perm="page.products.drilldown.view",
        args=args,
    )


def get_region_detail(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    region_id = args.get("region") or args.get("region_id") or args.get("id") or _entity_hint(ctx, args, "region")
    args.setdefault("region", region_id)
    args.setdefault("id", region_id)
    return _detail_tool(
        ctx,
        entity="regions",
        page_perm="page.regions.view",
        drill_perm="page.regions.drilldown.view",
        args=args,
    )


def get_supplier_detail(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    supplier_id = args.get("supplier_id") or args.get("id") or _entity_hint(ctx, args, "supplier")
    args.setdefault("supplier_id", supplier_id)
    args.setdefault("id", supplier_id)
    return _detail_tool(
        ctx,
        entity="suppliers",
        page_perm="page.suppliers.view",
        drill_perm="page.suppliers.drilldown.view",
        args=args,
    )


def get_sales_rep_detail(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    rep_id = args.get("salesrep_id") or args.get("sales_rep_id") or args.get("id") or _entity_hint(ctx, args, "salesrep")
    args.setdefault("salesrep_id", rep_id)
    args.setdefault("id", rep_id)
    return _detail_tool(
        ctx,
        entity="salesreps",
        page_perm="page.salesreps.view",
        drill_perm="page.salesreps.drilldown.view",
        args=args,
    )


def _summary_from_module_payload(ctx: ToolContext, module: str, payload: Dict[str, Any], title: str) -> Dict[str, Any]:
    if payload.get("error"):
        return _tool_response(
            status="error",
            title=title,
            data={"error": payload.get("error")},
            ctx=ctx,
            module=module,
        )
    data = {
        "kpis": payload.get("kpis") or {},
        "trend": payload.get("trend") or {},
        "table": payload.get("table") or {},
        "meta": payload.get("meta") or {},
    }
    if module == "customers":
        data["executive_narrative"] = payload.get("executive_narrative")
        data["scorecard"] = payload.get("executive_scorecard") or {}
        data["churn_risk_summary"] = payload.get("churn_risk_summary") or {}
        data["recommended_actions"] = ((payload.get("drivers") or {}).get("recommended_actions") or [])[:6]
    elif module == "products":
        data["insights"] = payload.get("insights") or []
        data["recommendations"] = (payload.get("recommendations") or [])[:8]
        data["ai_signals"] = payload.get("ai_signals") or []
        data["pricing_guardrails"] = payload.get("pricing_guardrails") or {}
    elif module == "regions":
        data["momentum"] = payload.get("momentum") or {}
        data["risk"] = payload.get("risk") or {}
        data["concentration"] = payload.get("concentration") or {}
    elif module == "suppliers":
        data["executive_summary"] = payload.get("executive_summary") or {}
        data["movers"] = payload.get("movers") or {}
        data["risk_opportunities"] = payload.get("risk_opportunities") or {}
    elif module == "salesreps":
        data["risk_flags"] = payload.get("risk_flags") or []
        data["charts"] = payload.get("charts") or {}
    return _tool_response(
        status=_payload_status(payload, primary_keys=("kpis", "table", "trend")),
        title=title,
        data=data,
        ctx=ctx,
        meta=payload.get("meta") if isinstance(payload.get("meta"), Mapping) else None,
        citations=[f"{module}_bundle"],
        module=module,
    )


def get_customer_summary(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not rbac.can_view_page("customers", ctx.user):
        return _module_forbidden(ctx, "customers", "Customer Portfolio Summary")
    payload = _module_bundle(ctx, "customers", args)
    return _summary_from_module_payload(ctx, "customers", payload, "Customer Portfolio Summary")


def get_customer_kpis(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not rbac.can_view_page("customers", ctx.user):
        return _module_forbidden(ctx, "customers", "Customer KPI Summary")
    payload = _module_bundle(ctx, "customers", args)
    return _tool_response(
        status=_payload_status(payload, primary_keys=("kpis",)),
        title="Customer KPI Summary",
        data={"kpis": payload.get("kpis") or {}, "scorecard": payload.get("executive_scorecard") or {}},
        ctx=ctx,
        meta=payload.get("meta") if isinstance(payload.get("meta"), Mapping) else None,
        citations=["customers_bundle.build_customers_bundle"],
        module="customers",
    )


def get_customer_trend(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not rbac.can_view_page("customers", ctx.user):
        return _module_forbidden(ctx, "customers", "Customer Trend")
    payload = _module_bundle(ctx, "customers", args)
    return _tool_response(
        status=_payload_status(payload, primary_keys=("trend",)),
        title="Customer Trend",
        data={"trend": payload.get("trend") or {}, "charts": (payload.get("charts") or {}).get("trend") or {}},
        ctx=ctx,
        meta=payload.get("meta") if isinstance(payload.get("meta"), Mapping) else None,
        citations=["customers_bundle.build_customers_bundle"],
        module="customers",
    )


def get_customer_watchouts(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not rbac.can_view_page("customers", ctx.user):
        return _module_forbidden(ctx, "customers", "Customer Watchouts")
    payload = _module_bundle(ctx, "customers", args)
    kpis = payload.get("kpis") if isinstance(payload.get("kpis"), Mapping) else {}
    churn = payload.get("churn_risk_summary") if isinstance(payload.get("churn_risk_summary"), Mapping) else {}
    top_risk = list(churn.get("top_at_risk_customers") or [])
    actions = list(((payload.get("drivers") or {}).get("recommended_actions") or []))[:8]
    watchouts: List[Dict[str, Any]] = []
    at_risk = kpis.get("at_risk_90")
    if at_risk:
        watchouts.append({"label": "Customers At Risk (90d)", "value": at_risk})
    if kpis.get("cost_coverage_pct") is not None:
        watchouts.append({"label": "Cost Coverage %", "value": kpis.get("cost_coverage_pct")})
    if kpis.get("top_customer"):
        watchouts.append({"label": "Top Customer Exposure", "value": (kpis.get("top_customer") or {}).get("share")})
    data = {
        "watchouts": watchouts,
        "top_at_risk_customers": top_risk[:12],
        "health_strip": payload.get("health_strip") or {},
        "recommended_actions": actions,
    }
    return _tool_response(
        status="ok" if data["watchouts"] or data["top_at_risk_customers"] else "empty",
        title="Customer Watchouts",
        data=data,
        ctx=ctx,
        meta=payload.get("meta") if isinstance(payload.get("meta"), Mapping) else None,
        citations=["customers_bundle.build_customers_bundle"],
        next_actions=actions[:6],
        module="customers",
    )


def get_customer_relationships(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not rbac.can_view_page("customers", ctx.user):
        return _module_forbidden(ctx, "customers", "Customer Relationships")
    args = dict(args or {})
    customer_id = _entity_hint(ctx, args, "customer")
    if not customer_id:
        bundle = _module_bundle(ctx, "customers", args)
        top_customer = ((bundle.get("kpis") or {}).get("top_customer") or {})
        customer_id = top_customer.get("id")
    if not customer_id:
        return _tool_response(
            status="empty",
            title="Customer Relationships",
            data={"message": "No customer is selected in context. Provide a customer id for relationship analysis."},
            ctx=ctx,
            module="customers",
        )
    drill = _drilldown("customers", ctx, {"customer_id": customer_id, "id": customer_id})
    if drill.get("error"):
        return _tool_response(status="empty", title="Customer Relationships", data={"error": drill.get("error")}, ctx=ctx, module="customers")
    rows = list(((drill.get("table") or {}).get("rows") or []))
    top_products = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        top_products.append(
            {
                "product_id": row.get("product_id") or row.get("sku") or row.get("key"),
                "product_name": row.get("product_name") or row.get("label") or row.get("name"),
                "revenue": row.get("revenue"),
                "profit": row.get("profit"),
                "margin_pct": row.get("margin_pct"),
            }
        )
    return _tool_response(
        status="ok" if top_products else "empty",
        title="Customer Relationships",
        data={
            "customer_id": customer_id,
            "top_products": top_products[:12],
            "trend": drill.get("trend") or {},
            "kpis": drill.get("kpis") or {},
        },
        ctx=ctx,
        meta=drill.get("meta") if isinstance(drill.get("meta"), Mapping) else None,
        citations=["bundle_service.drilldown:customers"],
        next_actions=[
            "Ask for product dependency risk for this customer.",
            "Compare this customer against prior period for retention and margin.",
            "Request a leadership-ready summary for this account.",
        ],
        module="customers",
    )


def get_customer_drilldown_explanation(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    detail = get_customer_detail(ctx, args)
    if detail.get("status") != "ok":
        return detail
    data = detail.get("data") if isinstance(detail.get("data"), Mapping) else {}
    kpis = data.get("kpis") if isinstance(data.get("kpis"), Mapping) else {}
    table_rows = ((data.get("table") or {}).get("rows") or []) if isinstance(data.get("table"), Mapping) else []
    explanation = {
        "summary": "Customer drilldown combines account KPI profile, trend shape, and contributing rows.",
        "focus_areas": [
            "Check revenue/profit trajectory versus prior period.",
            "Review top contributing products and margin concentration.",
            "Validate coverage/trust notes before escalating conclusions.",
        ],
        "kpi_snapshot": kpis,
        "top_rows": _top_rows(table_rows, limit=10),
    }
    return _tool_response(
        status="ok",
        title="Customer Drilldown Explanation",
        data=explanation,
        ctx=ctx,
        notes=["Drilldown explanation is generated from existing customer bundle logic."],
        citations=["bundle_service.drilldown:customers"],
        module="customers",
    )


def get_product_summary(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not rbac.can_view_page("products", ctx.user):
        return _module_forbidden(ctx, "products", "Product Portfolio Summary")
    payload = _module_bundle(ctx, "products", args)
    return _summary_from_module_payload(ctx, "products", payload, "Product Portfolio Summary")


def get_product_kpis(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not rbac.can_view_page("products", ctx.user):
        return _module_forbidden(ctx, "products", "Product KPI Summary")
    payload = _module_bundle(ctx, "products", args)
    return _tool_response(
        status=_payload_status(payload, primary_keys=("kpis",)),
        title="Product KPI Summary",
        data={"kpis": payload.get("kpis") or {}, "velocity": payload.get("velocity") or {}},
        ctx=ctx,
        meta=payload.get("meta") if isinstance(payload.get("meta"), Mapping) else None,
        citations=["products_bundle.build_products_bundle"],
        module="products",
    )


def get_product_trend(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not rbac.can_view_page("products", ctx.user):
        return _module_forbidden(ctx, "products", "Product Trend")
    payload = _module_bundle(ctx, "products", args)
    trend = payload.get("trend") or ((payload.get("charts") or {}).get("trajectory") or {})
    return _tool_response(
        status="ok" if isinstance(trend, Mapping) and (trend.get("labels") or trend.get("revenue")) else "empty",
        title="Product Trend",
        data={"trend": trend, "projected_next_month": payload.get("projected_next_month")},
        ctx=ctx,
        meta=payload.get("meta") if isinstance(payload.get("meta"), Mapping) else None,
        citations=["products_bundle.build_products_bundle"],
        module="products",
    )


def get_product_watchouts(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not rbac.can_view_page("products", ctx.user):
        return _module_forbidden(ctx, "products", "Product Watchouts")
    payload = _module_bundle(ctx, "products", args)
    guardrails = payload.get("pricing_guardrails") if isinstance(payload.get("pricing_guardrails"), Mapping) else {}
    ai_signals = list(payload.get("ai_signals") or [])
    recs = list(payload.get("recommendations") or [])
    watchouts = [
        {"label": "Pricing Outliers", "value": guardrails.get("outside_count")},
        {"label": "High Outlier Count", "value": guardrails.get("high_outlier_count")},
        {"label": "Low Outlier Count", "value": guardrails.get("low_outlier_count")},
    ]
    return _tool_response(
        status="ok" if ai_signals or recs or any(item.get("value") for item in watchouts) else "empty",
        title="Product Watchouts",
        data={
            "watchouts": watchouts,
            "pricing_guardrails": guardrails,
            "ai_signals": ai_signals[:10],
            "recommendations": recs[:10],
        },
        ctx=ctx,
        meta=payload.get("meta") if isinstance(payload.get("meta"), Mapping) else None,
        citations=["products_bundle.build_products_bundle"],
        next_actions=[
            "Review products with both high revenue and low margin.",
            "Check customer and supplier dependency for exposed SKUs.",
            "Validate pricing guardrail outliers before actioning price changes.",
        ],
        module="products",
    )


def get_product_dependencies(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not rbac.can_view_page("products", ctx.user):
        return _module_forbidden(ctx, "products", "Product Dependencies")
    args = dict(args or {})
    product_id = _entity_hint(ctx, args, "product")
    if product_id:
        drill = _drilldown("products", ctx, {"product_id": product_id, "id": product_id})
        if drill.get("error"):
            return _tool_response(status="empty", title="Product Dependencies", data={"error": drill.get("error")}, ctx=ctx, module="products")
        rows = list(((drill.get("table") or {}).get("rows") or []))
        dependencies = _top_rows(rows, limit=12)
        return _tool_response(
            status="ok" if dependencies else "empty",
            title="Product Dependencies",
            data={"product_id": product_id, "related_entities": dependencies, "kpis": drill.get("kpis") or {}},
            ctx=ctx,
            meta=drill.get("meta") if isinstance(drill.get("meta"), Mapping) else None,
            citations=["bundle_service.drilldown:products"],
            module="products",
        )

    payload = _module_bundle(ctx, "products", args)
    rows = list(((payload.get("table") or {}).get("rows") or []))
    dependency_rows = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        dependency_rows.append(
            {
                "product_id": row.get("product_id") or row.get("sku") or row.get("key"),
                "product_name": row.get("display_name") or row.get("product_name") or row.get("label"),
                "top_customer_share": row.get("top_customer_share"),
                "customer_hhi": row.get("customer_hhi"),
                "supplier_count": row.get("supplier_count"),
                "region_breadth": row.get("region_breadth"),
                "revenue": row.get("revenue"),
                "margin_pct": row.get("margin_pct"),
            }
        )
    return _tool_response(
        status="ok" if dependency_rows else "empty",
        title="Product Dependencies",
        data={"rows": _top_rows(dependency_rows, limit=15), "note": "Set a specific product in context for deeper relationships."},
        ctx=ctx,
        meta=payload.get("meta") if isinstance(payload.get("meta"), Mapping) else None,
        citations=["products_bundle.build_products_bundle"],
        module="products",
    )


def get_region_summary(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not rbac.can_view_page("regions", ctx.user):
        return _module_forbidden(ctx, "regions", "Regional Summary")
    payload = _module_bundle(ctx, "regions", args)
    return _summary_from_module_payload(ctx, "regions", payload, "Regional Summary")


def get_region_trend(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not rbac.can_view_page("regions", ctx.user):
        return _module_forbidden(ctx, "regions", "Regional Trend")
    payload = _module_bundle(ctx, "regions", args)
    trend = payload.get("trend") or ((payload.get("charts") or {}).get("trend") or {})
    return _tool_response(
        status="ok" if isinstance(trend, Mapping) and (trend.get("labels") or trend.get("revenue")) else "empty",
        title="Regional Trend",
        data={"trend": trend, "momentum": payload.get("momentum") or {}},
        ctx=ctx,
        meta=payload.get("meta") if isinstance(payload.get("meta"), Mapping) else None,
        citations=["regions_bundle.build_regions_bundle"],
        module="regions",
    )


def get_region_movers(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not rbac.can_view_page("regions", ctx.user):
        return _module_forbidden(ctx, "regions", "Regional Movers")
    payload = _module_bundle(ctx, "regions", args)
    momentum = payload.get("momentum") if isinstance(payload.get("momentum"), Mapping) else {}
    data = {
        "window": momentum.get("window") or {},
        "gainers": list(momentum.get("gainers") or []),
        "decliners": list(momentum.get("decliners") or []),
        "rows": list(momentum.get("rows") or [])[:20],
    }
    return _tool_response(
        status="ok" if data["rows"] or data["gainers"] or data["decliners"] else "empty",
        title="Regional Movers",
        data=data,
        ctx=ctx,
        meta=payload.get("meta") if isinstance(payload.get("meta"), Mapping) else None,
        citations=["regions_bundle.build_regions_bundle"],
        module="regions",
    )


def get_region_watchouts(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not rbac.can_view_page("regions", ctx.user):
        return _module_forbidden(ctx, "regions", "Regional Watchouts")
    payload = _module_bundle(ctx, "regions", args)
    risk = payload.get("risk") if isinstance(payload.get("risk"), Mapping) else {}
    concentration = payload.get("concentration") if isinstance(payload.get("concentration"), Mapping) else {}
    data = {
        "risk_summary": risk.get("summary") or {},
        "risk_rows": list(risk.get("rows") or [])[:15],
        "concentration_summary": concentration.get("summary") or {},
        "over_reliant_regions": list((concentration.get("over_reliant_regions") or []))[:15],
    }
    return _tool_response(
        status="ok" if data["risk_rows"] or data["over_reliant_regions"] else "empty",
        title="Regional Watchouts",
        data=data,
        ctx=ctx,
        meta=payload.get("meta") if isinstance(payload.get("meta"), Mapping) else None,
        citations=["regions_bundle.build_regions_bundle"],
        next_actions=[
            "Prioritize regions with both decline and high dependency.",
            "Compare high-risk regions to supplier or customer drivers.",
        ],
        module="regions",
    )


def get_supplier_summary(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not rbac.can_view_page("suppliers", ctx.user):
        return _module_forbidden(ctx, "suppliers", "Supplier Summary")
    payload = _module_bundle(ctx, "suppliers", args)
    return _summary_from_module_payload(ctx, "suppliers", payload, "Supplier Summary")


def get_supplier_trend(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not rbac.can_view_page("suppliers", ctx.user):
        return _module_forbidden(ctx, "suppliers", "Supplier Trend")
    payload = _module_bundle(ctx, "suppliers", args)
    trend = payload.get("trend") or ((payload.get("charts") or {}).get("revenue_profit_trend") or {})
    return _tool_response(
        status="ok" if isinstance(trend, Mapping) and trend else "empty",
        title="Supplier Trend",
        data={"trend": trend, "movers": payload.get("movers") or {}},
        ctx=ctx,
        meta=payload.get("meta") if isinstance(payload.get("meta"), Mapping) else None,
        citations=["suppliers_bundle.build_suppliers_bundle"],
        module="suppliers",
    )


def get_supplier_watchouts(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not rbac.can_view_page("suppliers", ctx.user):
        return _module_forbidden(ctx, "suppliers", "Supplier Watchouts")
    payload = _module_bundle(ctx, "suppliers", args)
    risk = payload.get("risk_opportunities") if isinstance(payload.get("risk_opportunities"), Mapping) else {}
    kpis = payload.get("kpis") if isinstance(payload.get("kpis"), Mapping) else {}
    summary = payload.get("executive_summary") if isinstance(payload.get("executive_summary"), Mapping) else {}
    data = {
        "risk_opportunities": risk,
        "kpi_watchouts": {
            "at_risk_suppliers": kpis.get("at_risk_suppliers"),
            "revenue_at_risk": kpis.get("revenue_at_risk"),
            "concentration_hhi": kpis.get("concentration_hhi"),
            "cost_coverage_pct": kpis.get("cost_coverage_pct"),
        },
        "executive_summary": summary,
    }
    return _tool_response(
        status="ok" if any(value for value in data["kpi_watchouts"].values()) or bool(risk) else "empty",
        title="Supplier Watchouts",
        data=data,
        ctx=ctx,
        meta=payload.get("meta") if isinstance(payload.get("meta"), Mapping) else None,
        citations=["suppliers_bundle.build_suppliers_bundle"],
        next_actions=[
            "Investigate suppliers with high revenue stake and worsening margin.",
            "Review concentration before taking sourcing action.",
        ],
        module="suppliers",
    )


def get_sales_rep_summary(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not rbac.can_view_page("salesreps", ctx.user):
        return _module_forbidden(ctx, "salesreps", "Sales Rep Portfolio Summary")
    payload = _module_bundle(ctx, "salesreps", args)
    return _summary_from_module_payload(ctx, "salesreps", payload, "Sales Rep Portfolio Summary")


def get_sales_rep_trend(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not rbac.can_view_page("salesreps", ctx.user):
        return _module_forbidden(ctx, "salesreps", "Sales Rep Trend")
    payload = _module_bundle(ctx, "salesreps", args)
    trend = payload.get("trend") or ((payload.get("charts") or {}).get("trend") or {})
    return _tool_response(
        status="ok" if isinstance(trend, Mapping) and trend else "empty",
        title="Sales Rep Trend",
        data={"trend": trend, "kpis": payload.get("kpis") or {}},
        ctx=ctx,
        meta=payload.get("meta") if isinstance(payload.get("meta"), Mapping) else None,
        citations=["salesreps_bundle.build_salesreps_bundle"],
        module="salesreps",
    )


def get_sales_rep_watchouts(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not rbac.can_view_page("salesreps", ctx.user):
        return _module_forbidden(ctx, "salesreps", "Sales Rep Watchouts")
    payload = _module_bundle(ctx, "salesreps", args)
    risk_flags = list(payload.get("risk_flags") or [])
    table_rows = list(((payload.get("table") or {}).get("rows") or []))
    concentration_rows = [
        {
            "rep_id": row.get("rep_id"),
            "rep_name": row.get("rep_name"),
            "top_customer_share": row.get("top_customer_share"),
            "risk_summary": row.get("risk_summary"),
            "revenue": row.get("revenue"),
            "margin_pct": row.get("margin_pct"),
        }
        for row in table_rows
        if isinstance(row, Mapping)
    ]
    return _tool_response(
        status="ok" if risk_flags or concentration_rows else "empty",
        title="Sales Rep Watchouts",
        data={
            "risk_flags": risk_flags,
            "concentration_rows": _top_rows(concentration_rows, limit=12),
            "what_changed": (payload.get("kpis") or {}).get("what_changed"),
        },
        ctx=ctx,
        meta=payload.get("meta") if isinstance(payload.get("meta"), Mapping) else None,
        citations=["salesreps_bundle.build_salesreps_bundle"],
        next_actions=[
            "Review rep portfolios with high concentration and weakening trend.",
            "Compare at-risk rep portfolios against regional exposure.",
        ],
        module="salesreps",
    )


def get_returns_summary(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    del args
    if not rbac.user_has_any_permission(
        ctx.user,
        "page.returns.view",
        "returns.create",
        "page.returns.customer_portal",
        "admin.returns.manage",
    ):
        return _module_forbidden(ctx, "returns", "Returns Analytics Summary")
    start = getattr(ctx.filters, "start", None)
    end = getattr(ctx.filters, "end", None)
    payload = returns_service.returns_analytics_snapshot(
        actor_user=ctx.user,
        from_date=start.date().isoformat() if start is not None else None,
        to_date=end.date().isoformat() if end is not None else None,
    )
    summary = dict(payload.get("summary") or {})
    frames = payload.get("frames") or {}
    top_customers = []
    top_skus = []
    try:
        top_customers = _jsonable((frames.get("top_customers") or pd.DataFrame()).head(10))
    except Exception:
        top_customers = []
    try:
        top_skus = _jsonable((frames.get("top_skus") or pd.DataFrame()).head(10))
    except Exception:
        top_skus = []
    return _tool_response(
        status="ok",
        title="Returns Analytics Summary",
        data={"summary": summary, "top_customers": top_customers, "top_skus": top_skus},
        ctx=ctx,
        citations=["returns_service.returns_analytics_snapshot"],
        module="returns",
    )


def get_pending_returns(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    del args
    if not _module_access(ctx).get("returns", False):
        return _module_forbidden(ctx, "returns", "Pending Returns And Approvals")
    start = getattr(ctx.filters, "start", None)
    end = getattr(ctx.filters, "end", None)
    rows = returns_service.list_rmas(
        statuses=[returns_service.STATUS_PENDING, returns_service.STATUS_NEEDS_REVIEW, returns_service.STATUS_WH_APPROVED],
        actor_user=ctx.user,
        from_date=start.date().isoformat() if start is not None else None,
        to_date=end.date().isoformat() if end is not None else None,
    )
    sorted_rows = sorted(rows, key=lambda row: float(row.get("total_credit_amount") or 0), reverse=True)
    data = {
        "pending_count": len(rows),
        "rows": sorted_rows[:30],
        "high_credit_pending": _top_rows(sorted_rows, limit=8, sort_key="total_credit_amount"),
    }
    return _tool_response(
        status="ok" if rows else "empty",
        title="Pending Returns And Approvals",
        data=data,
        ctx=ctx,
        citations=["returns_service.list_rmas"],
        next_actions=[
            "Prioritize pending approvals with highest credit impact.",
            "Review repeated reason codes for process/cost controls.",
        ],
        module="returns",
    )


def get_returns_status_overview(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    del args
    if not _module_access(ctx).get("returns", False):
        return _module_forbidden(ctx, "returns", "Returns Status Overview")
    start = getattr(ctx.filters, "start", None)
    end = getattr(ctx.filters, "end", None)
    rows = returns_service.list_rmas(
        actor_user=ctx.user,
        from_date=start.date().isoformat() if start is not None else None,
        to_date=end.date().isoformat() if end is not None else None,
    )
    counts: Dict[str, int] = {}
    for row in rows:
        label = str(row.get("status_label") or row.get("status") or "unknown").strip()
        counts[label] = counts.get(label, 0) + 1
    ranked = [{"status": key, "count": value} for key, value in sorted(counts.items(), key=lambda item: item[1], reverse=True)]
    return _tool_response(
        status="ok" if ranked else "empty",
        title="Returns Status Overview",
        data={"status_counts": ranked, "total": len(rows)},
        ctx=ctx,
        citations=["returns_service.list_rmas"],
        module="returns",
    )


def get_returns_reason_patterns(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    del args
    if not _module_access(ctx).get("returns", False):
        return _module_forbidden(ctx, "returns", "Returns Reason Patterns")
    start = getattr(ctx.filters, "start", None)
    end = getattr(ctx.filters, "end", None)
    payload = returns_service.returns_analytics_snapshot(
        actor_user=ctx.user,
        from_date=start.date().isoformat() if start is not None else None,
        to_date=end.date().isoformat() if end is not None else None,
    )
    frames = payload.get("frames") or {}
    reason_rows = _jsonable((frames.get("reason_breakdown") or pd.DataFrame()).head(15))
    category_rows = _jsonable((frames.get("category_breakdown") or pd.DataFrame()).head(15))
    follow_up_rows = _jsonable((frames.get("follow_up_breakdown") or pd.DataFrame()).head(15))
    return _tool_response(
        status="ok" if reason_rows or category_rows else "empty",
        title="Returns Reason Patterns",
        data={
            "reason_breakdown": reason_rows,
            "category_breakdown": category_rows,
            "follow_up_breakdown": follow_up_rows,
        },
        ctx=ctx,
        citations=["returns_service.returns_analytics_snapshot"],
        module="returns",
    )


def get_returns_workflow_help(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    section = str(args.get("section") or "workflow").strip().lower()
    payload = _page_help_cached("returns", section=section)
    transitions = []
    for status, targets in dict(getattr(returns_service, "TRANSITIONS", {}) or {}).items():
        transitions.append({"from": status, "to": sorted(list(targets or []))})
    return _tool_response(
        status=payload.get("status", "empty"),
        title="Returns Workflow Help",
        data={
            "page_help": payload.get("matches") or [],
            "status_transitions": transitions,
        },
        ctx=ctx,
        citations=["assistant.glossary.page_help", "returns_service.TRANSITIONS"],
        module="returns",
    )


def search_business_glossary(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not ctx.enable_glossary:
        return _tool_response(
            status="forbidden",
            title="Business Glossary Search",
            data=[],
            ctx=ctx,
            notes=["Glossary support is disabled by configuration."],
        )
    query = str((args or {}).get("query") or "").strip()
    result = search_glossary(query)
    return _tool_response(
        status=result.get("status", "empty"),
        title="Business Glossary Search",
        data=result.get("matches") or [],
        ctx=ctx,
        notes=["Glossary answers explain KPI and workflow definitions; they do not bypass data permissions."],
        citations=["assistant.glossary"],
        module="knowledge",
    )


def explain_metric_definition(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not ctx.enable_glossary:
        return _tool_response(
            status="forbidden",
            title="Metric Definition",
            data=[],
            ctx=ctx,
            notes=["Metric glossary support is disabled by configuration."],
            module="knowledge",
        )
    metric = str((args or {}).get("metric") or "").strip()
    result = explain_metric(metric)
    return _tool_response(
        status=result.get("status", "empty"),
        title="Metric Definition",
        data=result.get("matches") or [],
        ctx=ctx,
        citations=["assistant.glossary"],
        module="knowledge",
    )


def get_metric_definition(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return explain_metric_definition(ctx, args)


def get_page_help_tool(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    token = str((args or {}).get("page") or ctx.page or "overview").strip().lower()
    section = str((args or {}).get("section") or "").strip().lower() or None
    payload = _page_help_cached(token, section=section)
    return _tool_response(
        status=payload.get("status", "empty"),
        title="Page Help",
        data={"matches": payload.get("matches") or [], "knowledge": knowledge_snapshot()},
        ctx=ctx,
        citations=["assistant.glossary.page_help"],
        module="knowledge",
    )


def get_entity_relationship_context(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = str(args.get("module") or ctx.page or "").strip().lower()
    if module in {"customer", "customers"}:
        return get_customer_relationships(ctx, args)
    if module in {"product", "products"}:
        return get_product_dependencies(ctx, args)
    if module in {"region", "regions"}:
        return get_region_movers(ctx, args)
    if module in {"supplier", "suppliers"}:
        return get_supplier_watchouts(ctx, args)
    if module in {"salesrep", "sales_reps", "salesreps"}:
        return get_sales_rep_watchouts(ctx, args)
    return _tool_response(
        status="empty",
        title="Entity Relationship Context",
        data={"message": "No relationship context is available for this module."},
        ctx=ctx,
    )


def get_page_bundle(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = _module_token(str(args.get("module") or ctx.page or "overview"))
    if module in {"overview", "assistant"}:
        if not rbac.can_view_page("overview", ctx.user):
            return _module_forbidden(ctx, "overview", "Page Bundle")
        payload = _overview_context(ctx)
        return _tool_response(
            status="ok",
            title="Page Bundle",
            data={
                "module": "overview",
                "page_state": dict(ctx.page_state or {}),
                "kpis": payload.get("scorecard_kpis") or {},
                "trend": payload.get("trend_series") or {},
                "movers": payload.get("movers") or {},
                "risk": payload.get("risk") or {},
                "data_health": payload.get("data_health") or {},
                "forecast": payload.get("forecast") or {},
                "bundle_meta": (payload.get("bundle") or {}).get("meta") or {},
            },
            ctx=ctx,
            citations=["overview_v2.build_overview_context"],
            module="overview",
        )
    if module in {"customers", "products", "regions", "suppliers", "salesreps"}:
        if not rbac.can_view_page(module, ctx.user):
            return _module_forbidden(ctx, module, "Page Bundle")
        payload = _module_bundle(ctx, module, args)
        return _tool_response(
            status=_payload_status(payload, primary_keys=("kpis", "table", "trend")),
            title="Page Bundle",
            data={
                "module": module,
                "page_state": dict(ctx.page_state or {}),
                "kpis": payload.get("kpis") or {},
                "trend": payload.get("trend") or {},
                "table": payload.get("table") or {},
                "charts": payload.get("charts") or {},
                "bundle_meta": payload.get("meta") or {},
            },
            ctx=ctx,
            meta=payload.get("meta") if isinstance(payload.get("meta"), Mapping) else None,
            citations=[f"{module}_bundle"],
            module=module,
        )
    if module == "returns":
        return get_returns_summary(ctx, args)
    return _tool_response(
        status="empty",
        title="Page Bundle",
        data={"message": f"No page bundle is configured for module '{module}'."},
        ctx=ctx,
    )


def get_entity_page_bundle(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = _module_token(str(args.get("module") or ctx.page or "overview"))
    if module == "customers":
        return get_customer_detail(ctx, args)
    if module == "products":
        return get_product_detail(ctx, args)
    if module == "regions":
        return get_region_detail(ctx, args)
    if module == "suppliers":
        return get_supplier_detail(ctx, args)
    if module == "salesreps":
        return get_sales_rep_detail(ctx, args)
    return get_page_bundle(ctx, args)


def get_current_page_summary(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    args.setdefault("module", _module_token(ctx.page))
    return summarize_module_state(ctx, args)


def get_current_page_visible_state(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    del args
    state = dict(ctx.page_state or {})
    return _tool_response(
        status="ok",
        title="Current Page Visible State",
        data={
            "module": ctx.page,
            "visible_sections": list(state.get("visible_sections") or []),
            "allowed_metrics": list(state.get("allowed_metrics") or []),
            "selected_entity": state.get("selected_entity") or {},
            "active_window": state.get("active_window") or {},
            "comparison_mode": ((state.get("local_drill_state") or {}).get("comparison_mode") if isinstance(state.get("local_drill_state"), Mapping) else None),
        },
        ctx=ctx,
        module=ctx.page,
    )


def get_overview_history(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not rbac.can_view_page("overview", ctx.user):
        return _module_forbidden(ctx, "overview", "Overview History")
    trend = get_trend_series(ctx, {"metric": str((args or {}).get("metric") or "revenue"), "grain": str((args or {}).get("grain") or "monthly")})
    summary = get_overview_summary(ctx, {})
    trend_data = trend.get("data") if isinstance(trend.get("data"), Mapping) else {}
    trend_frame = _trend_to_frame({"labels": trend_data.get("labels") or [], str(trend_data.get("metric") or "value"): trend_data.get("values") or []})
    return _tool_response(
        status="ok" if not trend_frame.empty else "empty",
        title="Overview History",
        data={
            "trend": trend_data,
            "summary": summary.get("data") if isinstance(summary.get("data"), Mapping) else {},
            "history_rows": _jsonable(trend_frame.tail(36)),
        },
        ctx=ctx,
        module="overview",
        citations=["overview_v2.build_overview_context"],
    )


def _history_for_module(ctx: ToolContext, module: str, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    token = _module_token(module)
    params = dict(args or {})

    def _fallback_history(
        *,
        title: str,
        entity_type: str,
        module_key: str,
        source: Mapping[str, Any],
        default_citation: str,
    ) -> Dict[str, Any]:
        payload = source.get("data") if isinstance(source.get("data"), Mapping) else {}
        trend_payload = payload.get("trend") if isinstance(payload, Mapping) else {}
        if not isinstance(trend_payload, (Mapping, list)):
            trend_payload = payload if isinstance(payload, Mapping) else {}
        kpis_payload = payload.get("kpis") if isinstance(payload, Mapping) and isinstance(payload.get("kpis"), Mapping) else {}
        table_payload = payload.get("table") if isinstance(payload, Mapping) else {}
        notes = [str(item).strip() for item in (source.get("notes") or []) if str(item).strip()]
        notes.append("Detailed drilldown data was unavailable; history falls back to scoped trend context.")
        return _tool_response(
            status=str(source.get("status") or "empty"),
            title=title,
            data={
                "entity": _entity_hint(ctx, params, entity_type),
                "kpis": kpis_payload,
                "trend": trend_payload,
                "table": table_payload if isinstance(table_payload, (Mapping, list)) else {},
            },
            ctx=ctx,
            module=module_key,
            notes=notes,
            citations=list(source.get("citations") or [default_citation]),
            next_actions=list(source.get("next_actions") or []),
        )

    def _history_error(
        *,
        title: str,
        entity_type: str,
        module_key: str,
        exc: Exception,
    ) -> Dict[str, Any]:
        message = str(exc).strip() or "History lookup failed."
        lowered = message.lower()
        forbidden = ("403" in message) or ("forbidden" in lowered) or ("not in scope" in lowered)
        return _tool_response(
            status="forbidden" if forbidden else "error",
            title=title,
            data={
                "entity": _entity_hint(ctx, params, entity_type),
                "message": "Selected entity is outside your visible scope." if forbidden else "History lookup failed.",
            },
            ctx=ctx,
            module=module_key,
            notes=[message],
            citations=["assistant.scope"],
        )

    if token == "customers":
        try:
            detail = get_customer_detail(ctx, params)
        except Exception as exc:
            return _history_error(title="Customer History", entity_type="customer", module_key="customers", exc=exc)
        if detail.get("status") == "ok":
            data = detail.get("data") if isinstance(detail.get("data"), Mapping) else {}
            return _tool_response(
                status="ok",
                title="Customer History",
                data={
                    "entity": _entity_hint(ctx, params, "customer"),
                    "kpis": data.get("kpis") or {},
                    "trend": data.get("trend") or {},
                    "table": data.get("table") or {},
                },
                ctx=ctx,
                module="customers",
                citations=["bundle_service.drilldown:customers"],
            )
        try:
            trend_source = get_customer_trend(ctx, params)
        except Exception as exc:
            return _history_error(title="Customer History", entity_type="customer", module_key="customers", exc=exc)
        return _fallback_history(
            title="Customer History",
            entity_type="customer",
            module_key="customers",
            source=trend_source,
            default_citation="bundle_service.customers_bundle",
        )
    if token == "products":
        try:
            detail = get_product_detail(ctx, params)
        except Exception as exc:
            return _history_error(title="Product History", entity_type="product", module_key="products", exc=exc)
        if detail.get("status") == "ok":
            data = detail.get("data") if isinstance(detail.get("data"), Mapping) else {}
            return _tool_response(
                status="ok",
                title="Product History",
                data={
                    "entity": _entity_hint(ctx, params, "product"),
                    "kpis": data.get("kpis") or {},
                    "trend": data.get("trend") or {},
                    "table": data.get("table") or {},
                },
                ctx=ctx,
                module="products",
                citations=["bundle_service.drilldown:products"],
            )
        try:
            trend_source = get_product_trend(ctx, params)
        except Exception as exc:
            return _history_error(title="Product History", entity_type="product", module_key="products", exc=exc)
        return _fallback_history(
            title="Product History",
            entity_type="product",
            module_key="products",
            source=trend_source,
            default_citation="bundle_service.products_bundle",
        )
    if token == "regions":
        try:
            detail = get_region_detail(ctx, params)
        except Exception as exc:
            return _history_error(title="Region History", entity_type="region", module_key="regions", exc=exc)
        if detail.get("status") == "ok":
            data = detail.get("data") if isinstance(detail.get("data"), Mapping) else {}
            return _tool_response(
                status="ok",
                title="Region History",
                data={
                    "entity": _entity_hint(ctx, params, "region"),
                    "kpis": data.get("kpis") or {},
                    "trend": data.get("trend") or {},
                    "table": data.get("table") or {},
                },
                ctx=ctx,
                module="regions",
                citations=["bundle_service.drilldown:regions"],
            )
        try:
            trend_source = get_region_trend(ctx, params)
        except Exception as exc:
            return _history_error(title="Region History", entity_type="region", module_key="regions", exc=exc)
        return _fallback_history(
            title="Region History",
            entity_type="region",
            module_key="regions",
            source=trend_source,
            default_citation="bundle_service.regions_bundle",
        )
    if token == "suppliers":
        try:
            detail = get_supplier_detail(ctx, params)
        except Exception as exc:
            return _history_error(title="Supplier History", entity_type="supplier", module_key="suppliers", exc=exc)
        if detail.get("status") == "ok":
            data = detail.get("data") if isinstance(detail.get("data"), Mapping) else {}
            return _tool_response(
                status="ok",
                title="Supplier History",
                data={
                    "entity": _entity_hint(ctx, params, "supplier"),
                    "kpis": data.get("kpis") or {},
                    "trend": data.get("trend") or {},
                    "table": data.get("table") or {},
                },
                ctx=ctx,
                module="suppliers",
                citations=["bundle_service.drilldown:suppliers"],
            )
        try:
            trend_source = get_supplier_trend(ctx, params)
        except Exception as exc:
            return _history_error(title="Supplier History", entity_type="supplier", module_key="suppliers", exc=exc)
        return _fallback_history(
            title="Supplier History",
            entity_type="supplier",
            module_key="suppliers",
            source=trend_source,
            default_citation="bundle_service.suppliers_bundle",
        )
    if token == "salesreps":
        try:
            detail = get_sales_rep_detail(ctx, params)
        except Exception as exc:
            return _history_error(title="Sales Rep History", entity_type="salesrep", module_key="salesreps", exc=exc)
        if detail.get("status") == "ok":
            data = detail.get("data") if isinstance(detail.get("data"), Mapping) else {}
            return _tool_response(
                status="ok",
                title="Sales Rep History",
                data={
                    "entity": _entity_hint(ctx, params, "salesrep"),
                    "kpis": data.get("kpis") or {},
                    "trend": data.get("trend") or {},
                    "table": data.get("table") or {},
                },
                ctx=ctx,
                module="salesreps",
                citations=["bundle_service.drilldown:salesreps"],
            )
        try:
            trend_source = get_sales_rep_trend(ctx, params)
        except Exception as exc:
            return _history_error(title="Sales Rep History", entity_type="salesrep", module_key="salesreps", exc=exc)
        return _fallback_history(
            title="Sales Rep History",
            entity_type="salesrep",
            module_key="salesreps",
            source=trend_source,
            default_citation="bundle_service.salesreps_bundle",
        )
    if token == "returns":
        try:
            summary = get_returns_summary(ctx, params)
            patterns = get_returns_reason_patterns(ctx, params)
        except Exception as exc:
            return _history_error(title="Returns History", entity_type="returns", module_key="returns", exc=exc)
        return _tool_response(
            status="ok" if summary.get("status") == "ok" or patterns.get("status") == "ok" else "empty",
            title="Returns History",
            data={
                "summary": summary.get("data") if isinstance(summary.get("data"), Mapping) else {},
                "patterns": patterns.get("data") if isinstance(patterns.get("data"), Mapping) else {},
            },
            ctx=ctx,
            module="returns",
            citations=["returns_service.returns_analytics_snapshot"],
        )
    return get_overview_history(ctx, params)


def get_customer_history(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return _history_for_module(ctx, "customers", args)


def get_product_history(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return _history_for_module(ctx, "products", args)


def get_region_history(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return _history_for_module(ctx, "regions", args)


def get_supplier_history(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return _history_for_module(ctx, "suppliers", args)


def get_sales_rep_history(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return _history_for_module(ctx, "salesreps", args)


def get_returns_history(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return _history_for_module(ctx, "returns", args)


def compare_periods_for_entity(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = _module_token(str(args.get("module") or ctx.page or "overview"))
    comparison = compare_periods(ctx, {"module": module})
    history = _history_for_module(ctx, module, args)
    return _tool_response(
        status="ok" if comparison.get("status") == "ok" or history.get("status") == "ok" else "empty",
        title="Entity Period Comparison",
        data={
            "module": module,
            "comparison": comparison.get("data") if isinstance(comparison.get("data"), Mapping) else {},
            "history": history.get("data") if isinstance(history.get("data"), Mapping) else {},
        },
        ctx=ctx,
        module=module,
        citations=["assistant.compare_periods_for_entity"],
    )


def explain_history_for_entity(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = _module_token(str(args.get("module") or ctx.page or "overview"))
    history = _history_for_module(ctx, module, args)
    comparison = compare_periods(ctx, {"module": module})
    history_data = history.get("data") if isinstance(history.get("data"), Mapping) else {}
    comparison_data = comparison.get("data") if isinstance(comparison.get("data"), Mapping) else {}
    narrative = (
        "Historical movement is summarized from trend shape, period deltas, and scoped drilldown context. "
        "Review trust/coverage context before taking margin-sensitive actions."
    )
    return _tool_response(
        status="ok" if history.get("status") == "ok" or comparison.get("status") == "ok" else "empty",
        title="Entity History Explanation",
        data={
            "module": module,
            "narrative": narrative,
            "history": history_data,
            "comparison": comparison_data,
        },
        ctx=ctx,
        module=module,
        citations=["assistant.explain_history_for_entity"],
    )


def _entity_comparison_tool(ctx: ToolContext, dimension: str, args: Dict[str, Any] | None = None, *, title: str) -> Dict[str, Any]:
    args = dict(args or {})
    compared = compare_entities(ctx, {"dimension": dimension, "metric": args.get("metric") or "revenue", "limit": args.get("limit") or 12})
    ids = [str(item).strip() for item in list(args.get("entity_ids") or args.get("ids") or []) if str(item).strip()]
    data = compared.get("data") if isinstance(compared.get("data"), Mapping) else {}
    top = list(data.get("top") or []) if isinstance(data.get("top"), list) else []
    bottom = list(data.get("bottom") or []) if isinstance(data.get("bottom"), list) else []
    if ids:
        id_keys = ("customer_id", "product_id", "region", "supplier_id", "rep_id", "salesrep_id", "id", "key")
        wanted = {token.lower() for token in ids}

        def _keep(row: Any) -> bool:
            if not isinstance(row, Mapping):
                return False
            for key in id_keys:
                value = row.get(key)
                if value and str(value).strip().lower() in wanted:
                    return True
            return False

        top = [row for row in top if _keep(row)]
        bottom = [row for row in bottom if _keep(row)]
    return _tool_response(
        status=compared.get("status", "empty"),
        title=title,
        data={"dimension": dimension, "top": top, "bottom": bottom, "metric": data.get("metric")},
        ctx=ctx,
        module=_module_token(dimension),
        citations=compared.get("citations") or ["assistant.compare_entities"],
    )


def compare_customers(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return _entity_comparison_tool(ctx, "customers", args, title="Customer Comparison")


def compare_products(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return _entity_comparison_tool(ctx, "products", args, title="Product Comparison")


def compare_regions(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return _entity_comparison_tool(ctx, "regions", args, title="Region Comparison")


def compare_suppliers(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return _entity_comparison_tool(ctx, "suppliers", args, title="Supplier Comparison")


def compare_sales_reps(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return _entity_comparison_tool(ctx, "salesreps", args, title="Sales Rep Comparison")


def compare_periods(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = str(args.get("module") or ctx.page or "overview").strip().lower()
    if module in {"overview", "assistant"}:
        summary = get_overview_summary(ctx, args)
        kpis = get_overview_kpis(ctx, args)
        return _tool_response(
            status="ok" if summary.get("status") == "ok" or kpis.get("status") == "ok" else "empty",
            title="Period Comparison",
            data={
                "module": "overview",
                "briefing": summary.get("data") or {},
                "kpis": kpis.get("data") or {},
                "comparison_note": "Overview comparison uses current window with prior-period deltas from scorecard metrics.",
            },
            ctx=ctx,
            citations=["overview_v2.build_overview_context"],
            module="overview",
        )
    if module == "customers":
        payload = _module_bundle(ctx, "customers", args)
        scorecard = payload.get("executive_scorecard") or {}
        return _tool_response(
            status="ok" if scorecard else "empty",
            title="Period Comparison",
            data={"module": "customers", "scorecard": scorecard, "window": (payload.get("meta") or {})},
            ctx=ctx,
            meta=payload.get("meta") if isinstance(payload.get("meta"), Mapping) else None,
            citations=["customers_bundle.build_customers_bundle"],
            module="customers",
        )
    if module == "products":
        payload = _module_bundle(ctx, "products", args)
        kpis = payload.get("kpis") or {}
        fields = {
            "revenue": kpis.get("revenue"),
            "profit": kpis.get("profit"),
            "margin_pct": kpis.get("margin_pct"),
            "delta_revenue": kpis.get("delta_revenue"),
            "delta_revenue_pct": kpis.get("delta_revenue_pct"),
        }
        return _tool_response(
            status="ok" if any(value is not None for value in fields.values()) else "empty",
            title="Period Comparison",
            data={"module": "products", "comparison": fields},
            ctx=ctx,
            meta=payload.get("meta") if isinstance(payload.get("meta"), Mapping) else None,
            citations=["products_bundle.build_products_bundle"],
            module="products",
        )
    if module == "regions":
        payload = _module_bundle(ctx, "regions", args)
        kpis = payload.get("kpis") or {}
        fields = {
            "revenue_delta_prior": kpis.get("revenue_delta_prior"),
            "revenue_delta_prior_pct": kpis.get("revenue_delta_prior_pct"),
            "mom_growth": kpis.get("mom_growth"),
            "yoy_growth": kpis.get("yoy_growth"),
        }
        return _tool_response(
            status="ok" if any(value is not None for value in fields.values()) else "empty",
            title="Period Comparison",
            data={"module": "regions", "comparison": fields},
            ctx=ctx,
            meta=payload.get("meta") if isinstance(payload.get("meta"), Mapping) else None,
            citations=["regions_bundle.build_regions_bundle"],
            module="regions",
        )
    if module == "suppliers":
        payload = _module_bundle(ctx, "suppliers", args)
        kpis = payload.get("kpis") or {}
        fields = {
            "revenue_delta": kpis.get("revenue_delta"),
            "revenue_delta_pct": kpis.get("revenue_delta_pct"),
            "profit_delta": kpis.get("profit_delta"),
            "margin_delta_pp": kpis.get("margin_delta_pp"),
        }
        return _tool_response(
            status="ok" if any(value is not None for value in fields.values()) else "empty",
            title="Period Comparison",
            data={"module": "suppliers", "comparison": fields},
            ctx=ctx,
            meta=payload.get("meta") if isinstance(payload.get("meta"), Mapping) else None,
            citations=["suppliers_bundle.build_suppliers_bundle"],
            module="suppliers",
        )
    if module in {"salesreps", "sales_rep", "sales_reps"}:
        payload = _module_bundle(ctx, "salesreps", args)
        kpis = payload.get("kpis") or {}
        fields = {
            "revenue_mom_pct": kpis.get("revenue_mom_pct"),
            "profit_mom_pct": kpis.get("profit_mom_pct"),
            "margin_mom_pct": kpis.get("margin_mom_pct"),
        }
        return _tool_response(
            status="ok" if any(value is not None for value in fields.values()) else "empty",
            title="Period Comparison",
            data={"module": "salesreps", "comparison": fields},
            ctx=ctx,
            meta=payload.get("meta") if isinstance(payload.get("meta"), Mapping) else None,
            citations=["salesreps_bundle.build_salesreps_bundle"],
            module="salesreps",
        )
    return _tool_response(
        status="empty",
        title="Period Comparison",
        data={"message": f"Unsupported module '{module}' for period comparison."},
        ctx=ctx,
    )


_RANK_METRIC_ALIASES: Dict[str, tuple[str, ...]] = {
    "revenue": ("revenue", "sales", "total_revenue", "net_revenue"),
    "profit": ("profit", "gross_profit", "gp"),
    "margin_pct": ("margin_pct", "margin", "margin_percent"),
    "orders": ("orders", "order_count", "count"),
    "quantity": ("quantity", "qty", "units", "volume"),
    "asp": ("asp", "avg_price", "average_price"),
    "aov": ("aov", "average_order_value"),
    "shipped_weight": ("shipped_weight", "weight", "weight_lb", "lbs"),
    "product_count": ("product_count", "sku_count", "count_products"),
}


def _normalize_metric_token(metric: Any) -> str:
    token = str(metric or "revenue").strip().lower()
    if token in _RANK_METRIC_ALIASES:
        return token
    if token in {"sales", "net sales"}:
        return "revenue"
    if token in {"margin", "margin %", "margin pct"}:
        return "margin_pct"
    if token in {"units", "qty"}:
        return "quantity"
    return "revenue"


def _sensitive_metric_forbidden(
    ctx: ToolContext,
    *,
    metric: str,
    title: str,
    module: str,
) -> Dict[str, Any] | None:
    normalized = _normalize_metric_token(metric)
    if normalized == "profit" and not rbac.can_view_profit(ctx.user):
        return _forbidden(ctx, title, "Profit-based analysis is hidden for this user.", module=module)
    if normalized == "margin_pct" and not rbac.can_view_margin(ctx.user):
        return _forbidden(ctx, title, "Margin-based analysis is hidden for this user.", module=module)
    return None


def _dimension_module(dimension: Any) -> str:
    token = _module_token(str(dimension or "customers"))
    if token in {"overview", "assistant", "returns"}:
        return "customers"
    if token not in {"customers", "products", "regions", "suppliers", "salesreps"}:
        return "customers"
    return token


def _dimension_label_from_row(row: Mapping[str, Any], module: str) -> str:
    key_map: Dict[str, Sequence[str]] = {
        "customers": ("customer_name", "customer", "name", "label", "account", "id"),
        "products": ("product_name", "product", "display_name", "name", "label", "sku", "id"),
        "regions": ("region", "region_name", "name", "label", "id"),
        "suppliers": ("supplier_name", "supplier", "vendor", "name", "label", "id"),
        "salesreps": ("rep_name", "sales_rep", "rep", "name", "label", "id"),
    }
    for key in key_map.get(module, ("label", "name", "id")):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return "entity"


def _metric_value_from_row(row: Mapping[str, Any], metric: str) -> float | None:
    aliases = _RANK_METRIC_ALIASES.get(metric, _RANK_METRIC_ALIASES["revenue"])
    for key in aliases:
        value = _num_or_none(row.get(key))
        if value is not None:
            return value
    for fallback in ("revenue", "profit", "margin_pct", "value"):
        value = _num_or_none(row.get(fallback))
        if value is not None:
            return value
    return None


def _rows_for_dimension(ctx: ToolContext, module: str, args: Mapping[str, Any] | None = None) -> tuple[List[Dict[str, Any]], Dict[str, Any], List[str]]:
    payload = _module_bundle(ctx, module, args)
    rows = list(((payload.get("table") or {}).get("rows") or []))
    norm_rows = [dict(row) for row in rows if isinstance(row, Mapping)]
    meta = payload.get("meta") if isinstance(payload.get("meta"), Mapping) else {}
    return norm_rows, dict(meta or {}), [f"{module}_bundle"]


def resolve_entity_reference(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    entity_type = _module_token(str(args.get("entity_type") or args.get("secondary_entity_type") or "").strip())
    entity_name = str(args.get("entity_name") or args.get("filter_entity_name") or args.get("selected_entity_name") or "").strip()
    if not entity_type or not entity_name:
        return _tool_response(
            status="empty",
            title="Entity Reference Resolution",
            data={"message": "Entity type and entity name are required for resolution."},
            ctx=ctx,
            module=entity_type or ctx.page,
        )
    resolved = _resolve_entity_reference_match(ctx, entity_type, entity_name)
    notes = ["Resolution is scoped and fuzzy-matched against permission-safe entity labels."]
    if resolved.get("ambiguous"):
        notes.append("Multiple close matches were found; clarify the entity name for a stronger match.")
    elif not resolved.get("matched"):
        notes.append("No safe match was found in the current scope/filter context.")
    return _tool_response(
        status="ok" if resolved.get("matched") else "empty",
        title="Entity Reference Resolution",
        data=resolved,
        ctx=ctx,
        module=entity_type,
        notes=notes,
        citations=["assistant.entity_resolution", "fact_store.execute_sql_df"],
    )


def rank_entities(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    metric = _normalize_metric_token(args.get("metric"))
    direction = str(args.get("direction") or "top").strip().lower()
    if direction not in {"top", "bottom"}:
        direction = "top"
    limit = max(1, min(50, int(args.get("limit") or 10)))
    module = _dimension_module(args.get("entity_type") or args.get("dimension") or ctx.page or "customers")
    secondary = _module_token(args.get("secondary_entity_type") or "")
    selected_entity_name = str(args.get("selected_entity_name") or "").strip()
    exclude_low_base = bool(args.get("exclude_low_base", False))
    filter_entity_type = _module_token(str(args.get("filter_entity_type") or secondary or "").strip())
    filter_reference = _effective_filter_reference(ctx, args, filter_entity_type)

    if not rbac.can_view_page(module, ctx.user):
        return _module_forbidden(ctx, module, "Ranked Entities")
    sensitive_guard = _sensitive_metric_forbidden(ctx, metric=metric, title="Ranked Entities", module=module)
    if sensitive_guard is not None:
        return sensitive_guard

    if filter_entity_type and filter_reference and not filter_reference.get("matched"):
        return _tool_response(
            status="empty",
            title="Ranked Entities",
            data={
                "dimension": module,
                "metric": metric,
                "direction": direction,
                "limit": limit,
                "filter_entity_type": filter_entity_type,
                "filter_entity_name": str(filter_reference.get("query") or selected_entity_name or "").strip(),
                "candidates": list(filter_reference.get("candidates") or []),
            },
            ctx=ctx,
            citations=["assistant.entity_resolution"],
            notes=["The requested entity could not be resolved safely in the current scope."],
            module=module,
        )

    scoped_ctx = ctx
    filter_token = str(filter_reference.get("filter_token") or "").strip()
    if filter_entity_type and filter_token:
        scoped_ctx = _with_entity_filter(ctx, filter_entity_type, filter_token)

    rows: List[Dict[str, Any]] = []
    citations: List[str] = []
    meta: Dict[str, Any] = {}
    relationship_hint = ""
    if filter_entity_type:
        relationship_hint = f"{module}_for_{filter_entity_type}"
    rows, meta, citations = _rows_for_dimension(scoped_ctx, module, args)

    ranked_rows: List[Dict[str, Any]] = []
    for row in rows:
        metric_value = _metric_value_from_row(row, metric)
        if metric_value is None:
            continue
        if exclude_low_base:
            revenue_base = _num_or_none(row.get("revenue"))
            if revenue_base is not None and revenue_base < 1000:
                continue
        ranked_rows.append(
            {
                "label": _dimension_label_from_row(row, module),
                "metric": metric,
                "metric_value": metric_value,
                "row": row,
            }
        )

    ranked_rows.sort(key=lambda item: float(item.get("metric_value") or 0.0), reverse=(direction != "bottom"))
    ranked_rows = ranked_rows[:limit]
    for idx, row in enumerate(ranked_rows, start=1):
        row["rank"] = idx

    status = "ok" if ranked_rows else "empty"
    return _tool_response(
        status=status,
        title="Ranked Entities",
        data={
            "dimension": module,
            "metric": metric,
            "direction": direction,
            "limit": limit,
            "relationship_hint": relationship_hint,
            "selected_entity_name": selected_entity_name,
            "filter_entity_type": filter_entity_type,
            "filter_entity_label": str(filter_reference.get("label") or selected_entity_name or "").strip(),
            "filter_entity_name": str(filter_reference.get("query") or selected_entity_name or "").strip(),
            "rows": ranked_rows,
        },
        ctx=scoped_ctx,
        meta=meta,
        citations=citations or [f"{module}_bundle"],
        notes=["Ranking is deterministic and permission-scoped.", "Low-base suppression is optional and non-destructive."],
        module=module,
    )


def aggregate_by_dimension(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    metric = _normalize_metric_token(args.get("metric"))
    dimension = _dimension_module(args.get("dimension") or args.get("entity_type") or ctx.page or "customers")
    limit = max(1, min(50, int(args.get("limit") or 15)))
    if not rbac.can_view_page(dimension, ctx.user):
        return _module_forbidden(ctx, dimension, "Grouped Metric Aggregation")
    sensitive_guard = _sensitive_metric_forbidden(
        ctx,
        metric=metric,
        title="Grouped Metric Aggregation",
        module=dimension,
    )
    if sensitive_guard is not None:
        return sensitive_guard

    rows, meta, citations = _rows_for_dimension(ctx, dimension, args)
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        label = _dimension_label_from_row(row, dimension)
        value = _metric_value_from_row(row, metric)
        if value is None:
            continue
        bucket = grouped.setdefault(label, {"label": label, "value": 0.0, "count": 0})
        bucket["value"] = float(bucket.get("value") or 0.0) + float(value)
        bucket["count"] = int(bucket.get("count") or 0) + 1

    output = list(grouped.values())
    output.sort(key=lambda item: float(item.get("value") or 0.0), reverse=True)
    output = output[:limit]
    for idx, row in enumerate(output, start=1):
        row["rank"] = idx

    return _tool_response(
        status="ok" if output else "empty",
        title="Grouped Metric Aggregation",
        data={"dimension": dimension, "metric": metric, "groups": output, "limit": limit},
        ctx=ctx,
        meta=meta,
        citations=citations or [f"{dimension}_bundle"],
        notes=["Grouping uses scoped rows and deterministic summation."],
        module=dimension,
    )


def get_top_regions(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    ranked = rank_entities(ctx, {**dict(args or {}), "entity_type": "regions"})
    ranked["title"] = "Top Regions"
    return ranked


def get_top_customers(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    ranked = rank_entities(ctx, {**dict(args or {}), "entity_type": "customers"})
    ranked["title"] = "Top Customers"
    return ranked


def get_top_products(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    ranked = rank_entities(ctx, {**dict(args or {}), "entity_type": "products"})
    ranked["title"] = "Top Products"
    return ranked


def get_top_suppliers(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    ranked = rank_entities(ctx, {**dict(args or {}), "entity_type": "suppliers"})
    ranked["title"] = "Top Suppliers"
    return ranked


def get_top_sales_reps(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    ranked = rank_entities(ctx, {**dict(args or {}), "entity_type": "salesreps"})
    ranked["title"] = "Top Sales Reps"
    return ranked


def get_top_products_for_customer(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    ranked = rank_entities(ctx, {**dict(args or {}), "entity_type": "products", "secondary_entity_type": "customers"})
    ranked["title"] = "Top Products For Customer"
    return ranked


def get_top_customers_for_supplier(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    ranked = rank_entities(ctx, {**dict(args or {}), "entity_type": "customers", "secondary_entity_type": "suppliers"})
    ranked["title"] = "Top Customers For Supplier"
    return ranked


def get_top_customers_for_product(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    ranked = rank_entities(ctx, {**dict(args or {}), "entity_type": "customers", "secondary_entity_type": "products"})
    ranked["title"] = "Top Customers For Product"
    return ranked


def get_top_products_for_supplier(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    ranked = rank_entities(ctx, {**dict(args or {}), "entity_type": "products", "secondary_entity_type": "suppliers"})
    ranked["title"] = "Top Products For Supplier"
    return ranked


def get_top_products_for_sales_rep(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    ranked = rank_entities(ctx, {**dict(args or {}), "entity_type": "products", "secondary_entity_type": "salesreps"})
    ranked["title"] = "Top Products For Sales Rep"
    return ranked


def _row_label_tool(row: Mapping[str, Any]) -> str:
    for key in (
        "label",
        "name",
        "customer_name",
        "product_name",
        "region_name",
        "supplier_name",
        "rep_name",
        "sales_rep",
        "id",
        "key",
    ):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return "entity"


def _first_metric_value(row: Mapping[str, Any]) -> Any:
    return next((row.get(key) for key in ("metric_value", "value", "revenue", "profit", "margin_pct") if row.get(key) is not None), None)


def _fmt_tool_value(value: Any) -> str:
    num = _num_or_none(value)
    if num is None:
        return str(value)
    if abs(num) >= 1000:
        return f"{num:,.2f}"
    return f"{num:.2f}"


def _nested_ranking_title(parent_type: str, child_type: str) -> str:
    labels = {
        ("customers", "products"): "Top Customers With Top Products",
        ("suppliers", "products"): "Top Suppliers With Top Products",
        ("salesreps", "customers"): "Top Sales Reps With Top Customers",
        ("regions", "products"): "Top Regions With Top Products",
        ("products", "customers"): "Top Products With Top Customers",
    }
    return labels.get((parent_type, child_type), "Nested Rankings")


def _flatten_nested_groups(groups: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        parent_label = str(group.get("parent_label") or "").strip()
        parent_value = group.get("metric_value")
        for child in list(group.get("children") or []):
            if not isinstance(child, Mapping):
                continue
            rows.append(
                {
                    "parent_label": parent_label,
                    "parent_metric_value": parent_value,
                    "child_label": _row_label_tool(child),
                    "child_metric_value": _first_metric_value(child),
                    "child_rank": child.get("rank"),
                }
            )
    return rows


def get_nested_rankings(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    parent_type = _dimension_module(args.get("parent_entity_type") or args.get("entity_type") or ctx.page or "customers")
    child_type = _dimension_module(args.get("child_entity_type") or "products")
    metric = _normalize_metric_token(args.get("metric"))
    direction = str(args.get("direction") or "top").strip().lower()
    if direction not in {"top", "bottom"}:
        direction = "top"
    title = _nested_ranking_title(parent_type, child_type)
    if not rbac.can_view_page(parent_type, ctx.user):
        return _module_forbidden(ctx, parent_type, title)
    if not rbac.can_view_page(child_type, ctx.user):
        return _forbidden(
            ctx,
            title,
            f"Nested {child_type} detail is not available for this user.",
            module=parent_type,
        )
    sensitive_guard = _sensitive_metric_forbidden(ctx, metric=metric, title=title, module=parent_type)
    if sensitive_guard is not None:
        return sensitive_guard
    parent_limit = max(1, min(10, int(args.get("parent_limit") or args.get("limit") or 5)))
    child_limit = max(1, min(10, int(args.get("child_limit") or 5)))
    filter_entity_type = _module_token(str(args.get("filter_entity_type") or "").strip())
    filter_reference = _effective_filter_reference(ctx, args, filter_entity_type)
    if filter_entity_type and filter_reference and not filter_reference.get("matched"):
        return _tool_response(
            status="empty",
            title=title,
            data={
                "parent_type": parent_type,
                "child_type": child_type,
                "metric": metric,
                "groups": [],
                "render_strategy": "compact_summary",
                "filter_entity_type": filter_entity_type,
                "filter_entity_name": str(filter_reference.get("query") or "").strip(),
                "candidates": list(filter_reference.get("candidates") or []),
            },
            ctx=ctx,
            notes=["The requested relationship filter could not be resolved safely in the current scope."],
            citations=["assistant.entity_resolution"],
            module=parent_type,
        )

    scoped_ctx = ctx
    filter_token = str(filter_reference.get("filter_token") or "").strip()
    if filter_entity_type and filter_token:
        scoped_ctx = _with_entity_filter(ctx, filter_entity_type, filter_token)

    parent_ranking = rank_entities(
        scoped_ctx,
        {
            **args,
            "entity_type": parent_type,
            "dimension": parent_type,
            "metric": metric,
            "direction": direction,
            "limit": parent_limit,
            "filter_entity_type": "",
            "secondary_entity_type": "",
        },
    )
    if str(parent_ranking.get("status") or "").strip().lower() == "forbidden":
        return _tool_response(
            status="forbidden",
            title=title,
            data={"parent_type": parent_type, "child_type": child_type, "metric": metric, "groups": []},
            ctx=scoped_ctx,
            notes=list(parent_ranking.get("notes") or []),
            citations=list(parent_ranking.get("citations") or []),
            module=parent_type,
        )
    parent_data = parent_ranking.get("data") if isinstance(parent_ranking.get("data"), Mapping) else {}
    parent_rows = [dict(item) for item in list(parent_data.get("rows") or []) if isinstance(item, Mapping)]
    groups: List[Dict[str, Any]] = []
    for parent in parent_rows[:parent_limit]:
        parent_filter = _entity_filter_token_from_row(parent_type, parent.get("row") if isinstance(parent.get("row"), Mapping) else parent)
        if not parent_filter:
            continue
        child_ctx = _with_entity_filter(scoped_ctx, parent_type, parent_filter)
        child_ranking = rank_entities(
            child_ctx,
            {
                **args,
                "entity_type": child_type,
                "dimension": child_type,
                "metric": metric,
                "direction": direction,
                "limit": child_limit,
                "filter_entity_type": "",
                "secondary_entity_type": "",
            },
        )
        if str(child_ranking.get("status") or "").strip().lower() == "forbidden":
            return _tool_response(
                status="forbidden",
                title=title,
                data={"parent_type": parent_type, "child_type": child_type, "metric": metric, "groups": []},
                ctx=child_ctx,
                notes=list(child_ranking.get("notes") or []),
                citations=list(child_ranking.get("citations") or []),
                module=parent_type,
            )
        child_data = child_ranking.get("data") if isinstance(child_ranking.get("data"), Mapping) else {}
        child_rows = [dict(item) for item in list(child_data.get("rows") or []) if isinstance(item, Mapping)]
        groups.append(
            {
                "parent_label": _row_label_tool(parent),
                "parent_filter_token": parent_filter,
                "metric_value": _first_metric_value(parent),
                "rank": parent.get("rank"),
                "children": child_rows[:child_limit],
            }
        )

    total_child_rows = sum(len(list(group.get("children") or [])) for group in groups)
    render_strategy = "inline"
    if total_child_rows > 24 or (parent_limit * child_limit) > 30:
        render_strategy = "compact_summary"
    if total_child_rows > 40:
        render_strategy = "export_recommended"
    citations = list(parent_ranking.get("citations") or [])
    if filter_entity_type:
        citations.append("assistant.entity_resolution")
    return _tool_response(
        status="ok" if groups else "empty",
        title=title,
        data={
            "parent_type": parent_type,
            "child_type": child_type,
            "metric": metric,
            "direction": direction,
            "parent_limit": parent_limit,
            "child_limit": child_limit,
            "render_strategy": render_strategy,
            "groups": groups,
            "flat_rows": _flatten_nested_groups(groups),
            "filter_entity_type": filter_entity_type,
            "filter_entity_label": str(filter_reference.get("label") or "").strip(),
        },
        ctx=scoped_ctx,
        citations=citations or [f"{parent_type}_bundle", f"{child_type}_bundle"],
        notes=[
            "Nested rankings are bounded to safe parent and child limits.",
            "Parent ranking is executed first, then child rankings are scoped inside each parent entity.",
        ],
        next_actions=[
            "Export this hierarchy to Excel for full detail.",
            "Compare these parent entities with the prior period.",
            "Show history for the top parent entity.",
        ],
        module=parent_type,
    )


def get_top_customers_with_top_products(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return get_nested_rankings(ctx, {**dict(args or {}), "parent_entity_type": "customers", "child_entity_type": "products"})


def get_top_suppliers_with_top_products(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return get_nested_rankings(ctx, {**dict(args or {}), "parent_entity_type": "suppliers", "child_entity_type": "products"})


def get_top_sales_reps_with_top_customers(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return get_nested_rankings(ctx, {**dict(args or {}), "parent_entity_type": "salesreps", "child_entity_type": "customers"})


def get_top_regions_with_top_products(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return get_nested_rankings(ctx, {**dict(args or {}), "parent_entity_type": "regions", "child_entity_type": "products"})


def get_top_products_with_top_customers(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return get_nested_rankings(ctx, {**dict(args or {}), "parent_entity_type": "products", "child_entity_type": "customers"})


def get_detailed_metric_breakdown(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    if str(args.get("query_shape") or "") == "nested_ranking":
        nested = get_nested_rankings(ctx, args)
        data = nested.get("data") if isinstance(nested.get("data"), Mapping) else {}
        groups = [item for item in list(data.get("groups") or []) if isinstance(item, Mapping)]
        parent_total = sum(float(_num_or_none(group.get("metric_value")) or 0.0) for group in groups)
        concentration_note = ""
        if groups and parent_total > 0:
            concentration_note = f"Top parent share={_fmt_tool_value((float(_num_or_none(groups[0].get('metric_value')) or 0.0) / parent_total) * 100.0)}% of displayed parent total."
        return _tool_response(
            status=nested.get("status", "empty"),
            title="Detailed Metric Breakdown",
            data={
                "metric": data.get("metric"),
                "totals": {"displayed_parent_total": parent_total, "displayed_parent_count": len(groups)},
                "concentration_note": concentration_note,
            },
            ctx=ctx,
            module=str(data.get("parent_type") or ctx.page),
            citations=nested.get("citations") or [],
        )
    ranking = rank_entities(ctx, args)
    data = ranking.get("data") if isinstance(ranking.get("data"), Mapping) else {}
    rows = [item for item in list(data.get("rows") or []) if isinstance(item, Mapping)]
    total = sum(float(_num_or_none(_first_metric_value(row)) or 0.0) for row in rows)
    concentration_note = ""
    if rows and total > 0:
        top_share = (float(_num_or_none(_first_metric_value(rows[0])) or 0.0) / total) * 100.0
        concentration_note = f"Top displayed entity contributes {_fmt_tool_value(top_share)}% of shown {data.get('metric') or 'metric'}."
    return _tool_response(
        status=ranking.get("status", "empty"),
        title="Detailed Metric Breakdown",
        data={
            "metric": data.get("metric"),
            "totals": {"displayed_total": total, "displayed_count": len(rows)},
            "concentration_note": concentration_note,
        },
        ctx=ctx,
        module=str(data.get("dimension") or ctx.page),
        citations=ranking.get("citations") or [],
    )


def get_entity_driver_breakdown(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    ranking = rank_entities(ctx, args)
    data = ranking.get("data") if isinstance(ranking.get("data"), Mapping) else {}
    rows = [dict(item.get("row") or item) for item in list(data.get("rows") or []) if isinstance(item, Mapping)]
    driver_rows: List[Dict[str, Any]] = []
    for row in rows[:5]:
        driver_rows.append(
            {
                "label": _dimension_label_from_row(row, str(data.get("dimension") or ctx.page)),
                "revenue": row.get("revenue"),
                "profit": row.get("profit"),
                "margin_pct": row.get("margin_pct"),
                "orders": row.get("orders"),
            }
        )
    summary = "Driver breakdown surfaces revenue, profit, margin, and order mix for the displayed leaders."
    return _tool_response(
        status=ranking.get("status", "empty"),
        title="Entity Driver Breakdown",
        data={"summary": summary, "rows": driver_rows},
        ctx=ctx,
        module=str(data.get("dimension") or ctx.page),
        citations=ranking.get("citations") or [],
    )


def get_entity_margin_breakdown(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    ranking = rank_entities(ctx, args)
    data = ranking.get("data") if isinstance(ranking.get("data"), Mapping) else {}
    rows = [dict(item.get("row") or item) for item in list(data.get("rows") or []) if isinstance(item, Mapping)]
    margin_rows = []
    for row in rows[:5]:
        margin_rows.append(
            {
                "label": _dimension_label_from_row(row, str(data.get("dimension") or ctx.page)),
                "margin_pct": row.get("margin_pct"),
                "profit": row.get("profit"),
                "revenue": row.get("revenue"),
            }
        )
    summary = (
        "Margin fields are permission-scoped; hidden values stay masked."
        if not bool((ctx.sensitive_flags or {}).get("margin"))
        else "Margin lens highlights whether top-ranked entities are also margin accretive."
    )
    return _tool_response(
        status=ranking.get("status", "empty"),
        title="Entity Margin Breakdown",
        data={"summary": summary, "rows": margin_rows},
        ctx=ctx,
        module=str(data.get("dimension") or ctx.page),
        citations=ranking.get("citations") or [],
    )


def get_top_margin_risk_products(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    limit = max(1, min(50, int(args.get("limit") or 10)))
    metric = _normalize_metric_token(args.get("metric") or "revenue")
    rows: List[Dict[str, Any]] = []
    citations: List[str] = []
    if rbac.can_view_margin_risk(ctx.user):
        watch = get_margin_watchlist(ctx, {})
        data = watch.get("data") if isinstance(watch.get("data"), Mapping) else {}
        rows = [dict(item) for item in list(data.get("rows") or []) if isinstance(item, Mapping)]
        citations = list(watch.get("citations") or [])
    if not rows:
        payload = _module_bundle(ctx, "products", args)
        table_rows = list(((payload.get("table") or {}).get("rows") or []))
        for row in table_rows:
            if not isinstance(row, Mapping):
                continue
            margin = _num_or_none(row.get("margin_pct"))
            if margin is not None and margin <= 0:
                rows.append(dict(row))
        citations = ["products_bundle.build_products_bundle"]
    ranked = []
    for row in rows:
        metric_value = _metric_value_from_row(row, metric)
        if metric_value is None:
            continue
        ranked.append({"label": _dimension_label_from_row(row, "products"), "metric": metric, "metric_value": metric_value, "row": row})
    ranked.sort(key=lambda item: float(item.get("metric_value") or 0.0), reverse=True)
    ranked = ranked[:limit]
    for idx, row in enumerate(ranked, start=1):
        row["rank"] = idx
    return _tool_response(
        status="ok" if ranked else "empty",
        title="Top Margin-Risk Products",
        data={"metric": metric, "rows": ranked, "limit": limit},
        ctx=ctx,
        citations=citations or ["assistant.margin_risk_products"],
        module="products",
    )


def get_top_decliners(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    dimension = str(args.get("entity_type") or "customers").strip().lower()
    if _module_token(dimension) in {"regions"}:
        movers = get_region_movers(ctx, args)
        data = movers.get("data") if isinstance(movers.get("data"), Mapping) else {}
        rows = [dict(item) for item in list(data.get("decliners") or data.get("rows") or []) if isinstance(item, Mapping)]
        return _tool_response(
            status="ok" if rows else "empty",
            title="Top Decliners",
            data={"dimension": "regions", "rows": _top_rows(rows, limit=max(1, min(30, int(args.get('limit') or 10))), sort_key="delta_revenue")},
            ctx=ctx,
            citations=movers.get("citations") or ["regions_bundle.build_regions_bundle"],
            module="regions",
        )
    ranked = rank_entities(ctx, {**args, "direction": "bottom"})
    ranked["title"] = "Top Decliners"
    return ranked


def get_top_gainers(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    dimension = str(args.get("entity_type") or "customers").strip().lower()
    if _module_token(dimension) in {"regions"}:
        movers = get_region_movers(ctx, args)
        data = movers.get("data") if isinstance(movers.get("data"), Mapping) else {}
        rows = [dict(item) for item in list(data.get("gainers") or data.get("rows") or []) if isinstance(item, Mapping)]
        return _tool_response(
            status="ok" if rows else "empty",
            title="Top Gainers",
            data={"dimension": "regions", "rows": _top_rows(rows, limit=max(1, min(30, int(args.get('limit') or 10))), sort_key="delta_revenue")},
            ctx=ctx,
            citations=movers.get("citations") or ["regions_bundle.build_regions_bundle"],
            module="regions",
        )
    ranked = rank_entities(ctx, {**args, "direction": "top"})
    ranked["title"] = "Top Gainers"
    return ranked


def compare_current_vs_prior_period_rankings(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    current = rank_entities(ctx, args)
    current_rows = list((((current.get("data") or {}).get("rows") or []) if isinstance(current.get("data"), Mapping) else []))
    period = compare_periods(ctx, {"module": _dimension_module(args.get("entity_type") or args.get("dimension") or ctx.page or "overview")})
    period_data = period.get("data") if isinstance(period.get("data"), Mapping) else {}
    return _tool_response(
        status="ok" if current_rows else "empty",
        title="Current Vs Prior Ranking Comparison",
        data={"current_rankings": current_rows, "period_comparison": period_data},
        ctx=ctx,
        citations=list(current.get("citations") or []) + list(period.get("citations") or []),
        module=_dimension_module(args.get("entity_type") or args.get("dimension") or ctx.page),
    )


def compare_entities(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    dimension = str(args.get("dimension") or args.get("module") or "customers").strip().lower()
    metric = _normalize_metric_token(args.get("metric"))
    limit = max(3, min(25, int(args.get("limit") or 10)))
    module_map = {
        "customers": "customers",
        "customer": "customers",
        "products": "products",
        "product": "products",
        "regions": "regions",
        "region": "regions",
        "suppliers": "suppliers",
        "supplier": "suppliers",
        "salesreps": "salesreps",
        "sales_rep": "salesreps",
        "salesrep": "salesreps",
    }
    module = module_map.get(dimension, "customers")
    if module != "customers" and not rbac.can_view_page(module, ctx.user):
        return _module_forbidden(ctx, module, "Entity Comparison")
    if module == "customers" and not rbac.can_view_page("customers", ctx.user):
        return _module_forbidden(ctx, module, "Entity Comparison")
    sensitive_guard = _sensitive_metric_forbidden(ctx, metric=metric, title="Entity Comparison", module=module)
    if sensitive_guard is not None:
        return sensitive_guard

    payload = _module_bundle(ctx, module, args)
    rows = list(((payload.get("table") or {}).get("rows") or []))
    sorted_rows = _top_rows(rows, limit=max(limit * 2, 12), sort_key=metric)
    top = sorted_rows[:limit]
    bottom = sorted_rows[-limit:] if len(sorted_rows) > limit else []
    return _tool_response(
        status="ok" if top else "empty",
        title="Entity Comparison",
        data={
            "dimension": module,
            "metric": metric,
            "top": top,
            "bottom": bottom,
        },
        ctx=ctx,
        meta=payload.get("meta") if isinstance(payload.get("meta"), Mapping) else None,
        citations=[f"{module}_bundle"],
        module=module,
    )


def summarize_module_state(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = str(args.get("module") or ctx.page or "overview").strip().lower()
    if module in {"overview", "assistant"}:
        return get_overview_summary(ctx, args)
    if module in {"customer", "customers"}:
        return get_customer_summary(ctx, args)
    if module in {"product", "products"}:
        return get_product_summary(ctx, args)
    if module in {"region", "regions"}:
        return get_region_summary(ctx, args)
    if module in {"supplier", "suppliers"}:
        return get_supplier_summary(ctx, args)
    if module in {"salesrep", "salesreps", "sales_rep", "sales_reps"}:
        return get_sales_rep_summary(ctx, args)
    if module == "returns":
        return get_returns_summary(ctx, args)
    return _tool_response(
        status="empty",
        title="Module State Summary",
        data={"message": f"Unsupported module '{module}' for module summary."},
        ctx=ctx,
    )


def get_entity_watchouts(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = str(args.get("module") or ctx.page or "").strip().lower()
    if module in {"overview", "assistant"}:
        return get_margin_watchlist(ctx, args)
    if module in {"customer", "customers"}:
        return get_customer_watchouts(ctx, args)
    if module in {"product", "products"}:
        return get_product_watchouts(ctx, args)
    if module in {"region", "regions"}:
        return get_region_watchouts(ctx, args)
    if module in {"supplier", "suppliers"}:
        return get_supplier_watchouts(ctx, args)
    if module in {"salesrep", "salesreps", "sales_rep", "sales_reps"}:
        return get_sales_rep_watchouts(ctx, args)
    if module == "returns":
        return get_pending_returns(ctx, args)
    return _tool_response(
        status="empty",
        title="Entity Watchouts",
        data={"message": "No watchout tool is configured for this module."},
        ctx=ctx,
    )


def get_priority_investigations(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    investigations: List[Dict[str, Any]] = []
    access = _module_access(ctx)
    requested_module = _module_token(str(args.get("module") or "all"))
    if requested_module == "assistant":
        requested_module = "overview"
    include_all = requested_module in {"all", "cross_module", "cross-module", "business", "portfolio", "*"}

    def include(module: str) -> bool:
        return include_all or requested_module == module

    if include("overview") and access.get("overview"):
        try:
            overview = _overview_context(ctx)
            concentration = ((overview.get("risk") or {}).get("concentration") or {})
            profitability = ((overview.get("risk") or {}).get("profitability") or {})
            hhi = concentration.get("hhi") or concentration.get("customer_hhi")
            margin_risk_rows = list((profitability.get("margin_risk") or []))
            if hhi:
                investigations.append(
                    {
                        "module": "overview",
                        "priority": 95,
                        "severity": "high" if float(hhi) > 2200 else "medium",
                        "title": "Concentration Risk Elevated",
                        "detail": f"HHI={hhi} indicates dependency exposure under current scope.",
                    }
                )
            if margin_risk_rows:
                investigations.append(
                    {
                        "module": "overview",
                        "priority": 90,
                        "severity": "high",
                        "title": "Margin Risk Rows Detected",
                        "detail": f"{len(margin_risk_rows)} entities are in the margin-risk watchlist.",
                    }
                )
        except Exception:
            pass

    if include("customers") and access.get("customers"):
        payload = _module_bundle(ctx, "customers", {})
        kpis = payload.get("kpis") if isinstance(payload.get("kpis"), Mapping) else {}
        at_risk = int(kpis.get("at_risk_90") or 0)
        if at_risk > 0:
            investigations.append(
                {
                    "module": "customers",
                    "priority": 85,
                    "severity": "high" if at_risk >= 10 else "medium",
                    "title": "Customer Churn Risk",
                    "detail": f"{at_risk} customers are flagged as at-risk in the active window.",
                }
            )

    if include("products") and access.get("products"):
        payload = _module_bundle(ctx, "products", {})
        guardrails = payload.get("pricing_guardrails") if isinstance(payload.get("pricing_guardrails"), Mapping) else {}
        outside = int(guardrails.get("outside_count") or 0)
        if outside > 0:
            investigations.append(
                {
                    "module": "products",
                    "priority": 82,
                    "severity": "medium",
                    "title": "Pricing Guardrail Exceptions",
                    "detail": f"{outside} SKUs are outside pricing guardrails and need review.",
                }
            )

    if include("regions") and access.get("regions"):
        payload = _module_bundle(ctx, "regions", {})
        risk_summary = ((payload.get("risk") or {}).get("summary") or {}) if isinstance(payload.get("risk"), Mapping) else {}
        high_risk_regions = int(risk_summary.get("high_risk_regions") or 0)
        if high_risk_regions > 0:
            investigations.append(
                {
                    "module": "regions",
                    "priority": 80,
                    "severity": "medium",
                    "title": "High-Risk Regions",
                    "detail": f"{high_risk_regions} regions are flagged as high risk.",
                }
            )

    if include("suppliers") and access.get("suppliers"):
        payload = _module_bundle(ctx, "suppliers", {})
        kpis = payload.get("kpis") if isinstance(payload.get("kpis"), Mapping) else {}
        at_risk_suppliers = int(kpis.get("at_risk_suppliers") or 0)
        if at_risk_suppliers > 0:
            investigations.append(
                {
                    "module": "suppliers",
                    "priority": 78,
                    "severity": "medium",
                    "title": "Supplier Dependency / Risk",
                    "detail": f"{at_risk_suppliers} suppliers are marked at-risk in current scope.",
                }
            )

    if include("salesreps") and access.get("salesreps"):
        payload = _module_bundle(ctx, "salesreps", {})
        risk_flags = list(payload.get("risk_flags") or [])
        high_count = sum(1 for row in risk_flags if str(row.get("severity") or "").lower() == "high")
        if high_count > 0:
            investigations.append(
                {
                    "module": "salesreps",
                    "priority": 76,
                    "severity": "medium",
                    "title": "Sales Rep Portfolio Risk",
                    "detail": f"{high_count} high-severity risk flags are active in rep portfolios.",
                }
            )

    if include("returns") and access.get("returns"):
        pending = returns_service.list_rmas(
            statuses=[returns_service.STATUS_PENDING, returns_service.STATUS_NEEDS_REVIEW],
            actor_user=ctx.user,
        )
        if pending:
            investigations.append(
                {
                    "module": "returns",
                    "priority": 74,
                    "severity": "medium",
                    "title": "Pending Returns Approvals",
                    "detail": f"{len(pending)} returns require workflow attention.",
                }
            )

    investigations.sort(key=lambda row: (float(row.get("priority") or 0), str(row.get("severity") or "")), reverse=True)
    result_module = "cross_module" if include_all else requested_module
    return _tool_response(
        status="ok" if investigations else "empty",
        title="Priority Investigations",
        data={"investigations": investigations[:12], "requested_module": result_module},
        ctx=ctx,
        next_actions=[item.get("title") for item in investigations[:5] if item.get("title")],
        citations=[
            "overview_v2.build_overview_context",
            "customers_bundle.build_customers_bundle",
            "products_bundle.build_products_bundle",
            "regions_bundle.build_regions_bundle",
            "suppliers_bundle.build_suppliers_bundle",
            "salesreps_bundle.build_salesreps_bundle",
            "returns_service.list_rmas",
        ],
        module=result_module,
    )


def get_priority_actions(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    inv = get_priority_investigations(ctx, args)
    rows = list(((inv.get("data") or {}).get("investigations") or []) if isinstance(inv.get("data"), Mapping) else [])
    actions: List[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        module = str(row.get("module") or "").strip().lower()
        title = str(row.get("title") or "priority item").strip()
        if module == "overview":
            actions.append(f"Validate concentration and margin watchlist for '{title}' before cross-module escalation.")
        elif module == "customers":
            actions.append(f"Open customer drilldowns for '{title}' and assign account recovery plans.")
        elif module == "products":
            actions.append(f"Review SKU pricing guardrails linked to '{title}' and plan corrective actions.")
        elif module == "regions":
            actions.append(f"Compare impacted regions behind '{title}' and isolate driver entities.")
        elif module == "suppliers":
            actions.append(f"Assess supplier dependency and negotiate mitigation for '{title}'.")
        elif module == "salesreps":
            actions.append(f"Review portfolio mix and concentration coaching needs for '{title}'.")
        elif module == "returns":
            actions.append(f"Triage pending returns related to '{title}' by credit impact and SLA.")
        else:
            actions.append(f"Investigate '{title}' in detail and assign an owner.")
    deduped: List[str] = []
    for action in actions:
        if action not in deduped:
            deduped.append(action)
    return _tool_response(
        status="ok" if deduped else "empty",
        title="Priority Actions",
        data={"actions": deduped[:12]},
        ctx=ctx,
        citations=["assistant.priority_actions"],
        module="cross_module",
    )


def get_related_investigations(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    del args
    page = str(ctx.page or "overview").strip().lower()
    mapping: Dict[str, List[str]] = {
        "overview": [
            "Which customers and products drove this change?",
            "Where is concentration risk highest?",
            "What should leadership focus on this week?",
        ],
        "customers": [
            "Which products matter most for this customer?",
            "Is margin pressure concentrated in a few SKUs?",
            "Summarize this customer for leadership.",
        ],
        "products": [
            "Which customers drive this product?",
            "Is this SKU dependent on one region or supplier?",
            "What sales actions should we take next?",
        ],
        "regions": [
            "Which customers and products are driving this region?",
            "Are there supplier dependencies affecting this region?",
            "Which regions are highest risk this quarter?",
        ],
        "suppliers": [
            "Which products are most exposed to this supplier?",
            "Are supplier issues linked to margin risk?",
            "What is the highest-priority supplier action?",
        ],
        "salesreps": [
            "Which customers drive this rep portfolio?",
            "Where is concentration risk most severe?",
            "Compare this rep with peers in the same scope.",
        ],
        "returns": [
            "What approvals are pending by credit impact?",
            "What return reasons are trending up?",
            "How does returns risk connect to customer profitability?",
        ],
    }
    suggestions = mapping.get(page, mapping["overview"])
    return _tool_response(
        status="ok",
        title="Related Investigations",
        data={"suggestions": suggestions},
        ctx=ctx,
        citations=["assistant.related_investigations"],
        module=page,
    )


def get_executive_summary(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = str(args.get("module") or ctx.page or "overview").strip().lower()

    if module in {"overview", "assistant"}:
        summary = get_overview_summary(ctx, args)
        data = summary.get("data") if isinstance(summary.get("data"), Mapping) else {}
        return _tool_response(
            status=summary.get("status", "empty"),
            title="Executive Summary",
            data={
                "module": "overview",
                "biggest_win": data.get("biggest_win"),
                "biggest_decline": data.get("biggest_decline"),
                "key_risks": [data.get("key_risk")] if data.get("key_risk") else [],
                "key_drivers": data.get("narrative") or [],
                "recommended_actions": data.get("recommended_actions") or [],
                "next_drill_paths": [
                    "Compare top movers by customer and product.",
                    "Review concentration and margin watchlists.",
                    "Validate data health before final decisions.",
                ],
            },
            ctx=ctx,
            citations=summary.get("citations") or ["overview_v2.build_overview_context"],
            next_actions=summary.get("next_actions") or [],
            module="overview",
        )

    summary_tool_map: Dict[str, Callable[[ToolContext, Dict[str, Any] | None], Dict[str, Any]]] = {
        "customers": get_customer_summary,
        "products": get_product_summary,
        "regions": get_region_summary,
        "suppliers": get_supplier_summary,
        "salesreps": get_sales_rep_summary,
        "returns": get_returns_summary,
    }
    tool = summary_tool_map.get(module)
    if tool is None:
        return _tool_response(
            status="empty",
            title="Executive Summary",
            data={"message": f"Executive summary is not configured for module '{module}'."},
            ctx=ctx,
        )
    source = tool(ctx, args)
    src_data = source.get("data") if isinstance(source.get("data"), Mapping) else {}
    kpis = src_data.get("kpis") if isinstance(src_data.get("kpis"), Mapping) else {}

    risks = []
    if module == "customers":
        risks = [src_data.get("churn_risk_summary"), src_data.get("health_strip")]
    elif module == "products":
        risks = [src_data.get("pricing_guardrails"), src_data.get("ai_signals")]
    elif module == "regions":
        risks = [src_data.get("risk"), src_data.get("concentration")]
    elif module == "suppliers":
        risks = [src_data.get("risk_opportunities")]
    elif module == "salesreps":
        risks = [src_data.get("risk_flags")]
    elif module == "returns":
        risks = [src_data.get("summary")]

    executive_payload = {
        "module": module,
        "biggest_win": {"title": "Revenue", "value": kpis.get("revenue") or kpis.get("total_revenue")},
        "biggest_decline": {"title": "Primary Watchout", "value": kpis.get("delta_revenue") or kpis.get("revenue_delta")},
        "key_risks": [item for item in risks if item],
        "key_drivers": src_data.get("insights") or src_data.get("momentum") or src_data.get("movers") or [],
        "recommended_actions": source.get("next_actions") or [],
        "next_drill_paths": [
            f"Ask for {module} watchouts with severity ranking.",
            f"Compare {module} performance versus prior period.",
            "Request scope-limited action list for managers.",
        ],
    }
    return _tool_response(
        status=source.get("status", "empty"),
        title="Executive Summary",
        data=executive_payload,
        ctx=ctx,
        citations=source.get("citations") or [f"{module}_bundle"],
        next_actions=source.get("next_actions") or [],
        module=module,
    )


def get_export_options_for_page(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    del args
    module = _module_token(ctx.page)
    options: List[Dict[str, Any]] = []
    if module in {"overview", "assistant"}:
        module = "overview"
        options = [
            {"key": "export_current_page_excel", "label": "Overview Snapshot Workbook"},
            {"key": "export_analysis_bundle_excel", "label": "Overview Analysis Bundle"},
            {"key": "export_leadership_pack_excel", "label": "Leadership Pack"},
            {"key": "export_watchlist_excel", "label": "Risk Watchlist Workbook"},
            {"key": "export_chart_series_file", "label": "Trend Chart Series File"},
            {"key": "export_chart_image_file", "label": "Trend Chart Image"},
            {"key": "export_custom_analysis_file", "label": "Custom Analysis File"},
        ]
    elif module in {"customers", "products", "regions", "suppliers", "salesreps", "returns"}:
        options = [
            {"key": "export_current_page_excel", "label": f"{module.title()} Page Workbook"},
            {"key": "export_current_entity_history_excel", "label": "Entity History Workbook"},
            {"key": "export_analysis_bundle_excel", "label": "Analysis Bundle Workbook"},
            {"key": "export_watchlist_excel", "label": "Watchlist Workbook"},
            {"key": "export_chart_series_file", "label": "Chart Series File"},
            {"key": "export_chart_image_file", "label": "Chart Image File"},
            {"key": "export_custom_analysis_file", "label": "Custom Analysis File"},
        ]
        if module in {"customers", "products", "regions", "suppliers", "salesreps"}:
            options.append({"key": "export_custom_scoped_excel", "label": "Custom Scoped Workbook"})
    return _tool_response(
        status="ok" if options else "empty",
        title="Export Options",
        data={"module": module, "options": options, "export_allowed": _can_export_module(ctx, module)},
        ctx=ctx,
        module=module,
        citations=["assistant.export_options"],
    )


def _export_plan_from_args(ctx: ToolContext, module: str, args: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    request = dict(args or {})
    output_format = str(request.get("output_format") or "xlsx").strip().lower()
    chart_image_only = bool(request.get("chart_image_only", False))
    image_format = str(request.get("image_format") or "").strip().lower()
    if chart_image_only and image_format in {"svg", "png"}:
        output_format = image_format
    if output_format not in {"xlsx", "csv"}:
        output_format = image_format if image_format in {"svg", "png"} and chart_image_only else "xlsx"
    export_mode = str(request.get("export_mode") or request.get("audience") or "standard").strip().lower()
    if export_mode in {"leadership", "executive"}:
        export_mode = "executive"
    elif export_mode in {"analyst", "detailed"}:
        export_mode = "analyst"
    else:
        export_mode = "standard"
    include_chart = bool(request.get("include_chart", False))
    include_summary = bool(request.get("include_summary_sheet", True))
    include_metadata = bool(request.get("include_metadata_sheet", True))
    include_all_allowed = bool(request.get("include_all_allowed_columns", False))
    include_history = bool(request.get("include_history", True))
    export_intent = str(request.get("export_intent_type") or "export_table").strip().lower() or "export_table"
    sheets: List[Dict[str, Any]] = []
    if include_summary:
        sheets.append({"name": "Summary", "type": "summary"})
    if export_intent in {"export_ranked_list", "export_table"}:
        sheets.append({"name": "Ranked List", "type": "ranked_table"})
    if export_intent == "export_grouped_metric":
        sheets.append({"name": "Grouped Metric", "type": "grouped_metric"})
    if export_intent in {"export_entity_history", "export_custom_analysis_pack"} and include_history:
        sheets.append({"name": "History", "type": "history"})
    if export_intent == "export_comparison":
        sheets.append({"name": "Comparison", "type": "comparison"})
    if include_chart:
        sheets.append({"name": "Charts", "type": "chart_pack"})
    if include_metadata:
        sheets.append({"name": "Metadata", "type": "metadata"})
    return {
        "format": output_format,
        "mode": export_mode,
        "module": module,
        "export_intent_type": export_intent,
        "page_context_used": bool(request.get("use_current_page_context", True)),
        "use_current_filters": bool(request.get("use_current_filters", True)),
        "use_full_history": bool(request.get("use_full_history", False)),
        "include_history": include_history,
        "include_chart": include_chart,
        "chart_image_only": chart_image_only,
        "image_format": image_format,
        "include_summary_sheet": include_summary,
        "include_metadata_sheet": include_metadata,
        "include_all_allowed_columns": include_all_allowed,
        "async_export": bool(request.get("async_export", False)),
        "requested_columns": list(request.get("requested_columns") or []),
        "metrics": [str(item) for item in list(request.get("metrics") or [request.get("metric") or "revenue"]) if str(item).strip()],
        "group_by_dimension": str(request.get("dimension") or request.get("group_by_dimension") or ""),
        "ranking_direction": str(request.get("direction") or request.get("ranking_direction") or "top"),
        "limit": int(max(1, min(500, int(request.get("limit") or 25)))),
        "window": _window_used(ctx),
        "scope": _scope_used(ctx),
        "sheets": sheets,
    }


def get_exportable_columns_for_context(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = _module_token(str(args.get("module") or ctx.page or "overview"))
    include_all_allowed_columns = bool(args.get("include_all_allowed_columns", False))
    requested_columns = [str(item) for item in list(args.get("requested_columns") or []) if str(item).strip()]
    sheets = _workbook_for_module_page(ctx, module, args)
    _, policy = _sheets_with_column_policy(
        ctx,
        sheets,
        requested_columns=requested_columns,
        include_all_allowed_columns=include_all_allowed_columns,
    )
    all_allowed = sorted({str(col) for meta in policy.values() for col in list((meta or {}).get("allowed_columns") or [])})
    all_excluded = sorted({str(col) for meta in policy.values() for col in list((meta or {}).get("excluded_columns") or [])})
    return _tool_response(
        status="ok",
        title="Exportable Columns",
        data={
            "module": module,
            "requested_columns": requested_columns,
            "include_all_allowed_columns": include_all_allowed_columns,
            "sheets": policy,
            "all_allowed_columns": all_allowed,
            "all_excluded_columns": all_excluded,
            "export_sensitive": bool((ctx.sensitive_flags or {}).get("export_sensitive")),
        },
        ctx=ctx,
        module=module,
        notes=[
            "Column selection is enforced server-side using permission-aware export policy.",
            "Sensitive columns are excluded unless export-sensitive permission is granted.",
        ],
        citations=["assistant.exportable_columns_policy", "core.sensitive_data"],
    )


def get_ranked_dataset(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    ranked = rank_entities(ctx, args)
    data = ranked.get("data") if isinstance(ranked.get("data"), Mapping) else {}
    return _tool_response(
        status=ranked.get("status", "empty"),
        title="Ranked Dataset",
        data={
            "dimension": data.get("dimension"),
            "metric": data.get("metric"),
            "direction": data.get("direction"),
            "rows": list(data.get("rows") or []),
            "limit": data.get("limit"),
        },
        ctx=ctx,
        module=data.get("dimension"),
        citations=ranked.get("citations") or [],
    )


def get_grouped_dataset(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    grouped = aggregate_by_dimension(ctx, args)
    data = grouped.get("data") if isinstance(grouped.get("data"), Mapping) else {}
    return _tool_response(
        status=grouped.get("status", "empty"),
        title="Grouped Dataset",
        data={
            "dimension": data.get("dimension"),
            "metric": data.get("metric"),
            "groups": list(data.get("groups") or []),
            "limit": data.get("limit"),
        },
        ctx=ctx,
        module=data.get("dimension"),
        citations=grouped.get("citations") or [],
    )


def get_entity_history_dataset(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = _module_token(str(args.get("module") or ctx.page or "overview"))
    history = _history_for_module(ctx, module, args)
    data = history.get("data") if isinstance(history.get("data"), Mapping) else {}
    trend = data.get("trend") if isinstance(data.get("trend"), Mapping) else {}
    table_rows = (data.get("table") or {}).get("rows") if isinstance(data.get("table"), Mapping) else data.get("table")
    return _tool_response(
        status=history.get("status", "empty"),
        title="Entity History Dataset",
        data={
            "module": module,
            "summary": data.get("summary") or data.get("kpis") or {},
            "trend": trend,
            "rows": list(table_rows or []) if isinstance(table_rows, list) else [],
        },
        ctx=ctx,
        module=module,
        citations=history.get("citations") or [],
    )


def get_comparison_dataset(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    comparison = compare_entities(ctx, args)
    period = compare_periods(ctx, {"module": _dimension_module(args.get("dimension") or args.get("entity_type") or ctx.page)})
    data = comparison.get("data") if isinstance(comparison.get("data"), Mapping) else {}
    period_data = period.get("data") if isinstance(period.get("data"), Mapping) else {}
    return _tool_response(
        status=comparison.get("status", "empty"),
        title="Comparison Dataset",
        data={
            "dimension": data.get("dimension"),
            "metric": data.get("metric"),
            "top": list(data.get("top") or []),
            "bottom": list(data.get("bottom") or []),
            "period": period_data,
        },
        ctx=ctx,
        module=data.get("dimension"),
        citations=list(comparison.get("citations") or []) + list(period.get("citations") or []),
    )


def get_summary_dataset(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    summary = summarize_module_state(ctx, args)
    data = summary.get("data") if isinstance(summary.get("data"), Mapping) else {}
    return _tool_response(
        status=summary.get("status", "empty"),
        title="Summary Dataset",
        data={"module": data.get("module"), "snapshot": data.get("snapshot") or {}, "highlights": list(data.get("highlights") or [])},
        ctx=ctx,
        module=data.get("module"),
        citations=summary.get("citations") or [],
    )


def get_chart_series(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = _module_token(str(args.get("module") or ctx.page or "overview"))
    metric = _normalize_metric_token(args.get("metric"))
    chart_type = str(args.get("chart_type") or "line").strip().lower()
    if chart_type not in {"line", "bar", "column"}:
        chart_type = "line"
    if module == "overview":
        payload = _overview_context(ctx)
        trend = (((payload.get("trend_series") or {}).get("monthly") or {}) if isinstance(payload.get("trend_series"), Mapping) else {})
    elif module in {"customers", "products", "regions", "suppliers", "salesreps"}:
        payload = _module_bundle(ctx, module, args)
        trend = payload.get("trend") or ((payload.get("charts") or {}).get("trend") if isinstance(payload.get("charts"), Mapping) else {})
    else:
        history = _history_for_module(ctx, module, args)
        data = history.get("data") if isinstance(history.get("data"), Mapping) else {}
        trend = data.get("trend") or {}
    frame = _trend_to_frame(trend, label_col="period")
    if frame.empty:
        history = _history_for_module(ctx, module, args)
        hdata = history.get("data") if isinstance(history.get("data"), Mapping) else {}
        htrend = hdata.get("trend")
        if isinstance(htrend, (Mapping, list)):
            frame = _trend_to_frame(htrend, label_col="period")
        if frame.empty:
            history_rows = list(hdata.get("history_rows") or [])
            if history_rows:
                frame = _to_frame(history_rows, fallback_columns=("period", "value"))
        if frame.empty:
            table = hdata.get("table")
            if isinstance(table, Mapping):
                frame = _to_frame(table.get("rows") or [], fallback_columns=("period", "value"))
            elif isinstance(table, list):
                frame = _to_frame(table, fallback_columns=("period", "value"))
        if not frame.empty and "period" not in frame.columns and "label" in frame.columns:
            frame = frame.rename(columns={"label": "period"})
    if frame.empty:
        return _tool_response(
            status="empty",
            title="Chart Series",
            data={"module": module, "metric": metric, "series": [], "chart_type": chart_type},
            ctx=ctx,
            module=module,
        )
    labels = frame.iloc[:, 0].tolist()
    value_col = None
    for candidate in (metric, "revenue", "profit", "margin_pct", "value"):
        if candidate in frame.columns:
            value_col = candidate
            break
    if value_col is None:
        value_col = str(frame.columns[1]) if len(frame.columns) > 1 else str(frame.columns[0])
    values = pd.to_numeric(frame[value_col], errors="coerce").fillna(0).tolist()
    series = [{"label": str(labels[idx]), "value": float(values[idx])} for idx in range(min(len(labels), len(values)))]
    return _tool_response(
        status="ok" if series else "empty",
        title="Chart Series",
        data={"module": module, "metric": metric, "chart_type": chart_type, "value_column": value_col, "series": series},
        ctx=ctx,
        module=module,
        citations=[f"{module}_bundle", "assistant.history"],
    )


def get_export_metadata(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = _module_token(str(args.get("module") or ctx.page or "overview"))
    export_type = str(args.get("export_type") or "custom").strip().lower() or "custom"
    rows = _metadata_frame(ctx, module=module, export_type=export_type, notes=list(args.get("notes") or []))
    return _tool_response(
        status="ok",
        title="Export Metadata",
        data={"module": module, "export_type": export_type, "rows": _jsonable(rows)},
        ctx=ctx,
        module=module,
        citations=["assistant.export_metadata"],
    )


def get_leadership_summary_dataset(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    summary = get_leadership_summary(ctx, args)
    data = summary.get("data") if isinstance(summary.get("data"), Mapping) else {}
    return _tool_response(
        status=summary.get("status", "empty"),
        title="Leadership Summary Dataset",
        data=dict(data),
        ctx=ctx,
        module=data.get("module") or _module_token(str((args or {}).get("module") or ctx.page)),
        citations=summary.get("citations") or [],
    )


def _workbook_for_module_page(ctx: ToolContext, module: str, args: Dict[str, Any] | None = None) -> Dict[str, pd.DataFrame]:
    token = _module_token(module)
    sheets: Dict[str, pd.DataFrame] = {}
    if token == "overview":
        payload = _overview_context(ctx)
        sheets["KPIs"] = _to_frame(payload.get("scorecard_kpis"), fallback_columns=["revenue", "orders", "profit", "margin_pct"])
        monthly = (((payload.get("trend_series") or {}).get("monthly") or {}) if isinstance(payload.get("trend_series"), Mapping) else {})
        sheets["Trend"] = _trend_to_frame(monthly, label_col="period")
        movers = payload.get("movers") if isinstance(payload.get("movers"), Mapping) else {}
        sheets["Movers Customers"] = _to_frame((movers.get("customer") or {}).get("gainers") if isinstance(movers.get("customer"), Mapping) else [])
        sheets["Movers Products"] = _to_frame((movers.get("product") or {}).get("gainers") if isinstance(movers.get("product"), Mapping) else [])
        sheets["Risk"] = _to_frame(((payload.get("risk") or {}).get("profitability") or {}).get("margin_risk", []))
        sheets["Data Health"] = _to_frame(payload.get("data_health"), fallback_columns=["cost_coverage_pct", "pack_coverage_pct"])
        return sheets
    if token in {"customers", "products", "regions", "suppliers", "salesreps"}:
        payload = _module_bundle(ctx, token, args)
        sheets["KPIs"] = _to_frame(payload.get("kpis"), fallback_columns=["revenue", "orders"])
        sheets["Trend"] = _trend_to_frame(payload.get("trend") or ((payload.get("charts") or {}).get("trend") if isinstance(payload.get("charts"), Mapping) else {}))
        table = (payload.get("table") or {}).get("rows") if isinstance(payload.get("table"), Mapping) else []
        sheets["Table"] = _to_frame(table)
        return sheets
    if token == "returns":
        snap = returns_service.returns_analytics_snapshot(
            actor_user=ctx.user,
            from_date=getattr(ctx.filters, "start", None).date().isoformat() if getattr(ctx.filters, "start", None) is not None else None,
            to_date=getattr(ctx.filters, "end", None).date().isoformat() if getattr(ctx.filters, "end", None) is not None else None,
        )
        summary = snap.get("summary") if isinstance(snap.get("summary"), Mapping) else {}
        frames = snap.get("frames") or {}
        sheets["Summary"] = _to_frame(summary)
        sheets["Top Customers"] = _to_frame(_jsonable((frames.get("top_customers") or pd.DataFrame()).head(200)))
        sheets["Top SKUs"] = _to_frame(_jsonable((frames.get("top_skus") or pd.DataFrame()).head(200)))
        sheets["Reasons"] = _to_frame(_jsonable((frames.get("reason_breakdown") or pd.DataFrame()).head(200)))
        return sheets
    return {"Data": pd.DataFrame()}


def _export_common_kwargs(
    ctx: ToolContext,
    module: str,
    args: Mapping[str, Any] | None = None,
    *,
    default_export_type: str,
) -> Dict[str, Any]:
    request = dict(args or {})
    requested_columns = [str(item) for item in list(request.get("requested_columns") or []) if str(item).strip()]
    include_all_allowed_columns = bool(request.get("include_all_allowed_columns", False))
    include_chart = bool(request.get("include_chart", False))
    async_export = bool(request.get("async_export", False))
    output_format = str(request.get("output_format") or "xlsx").strip().lower()
    if output_format not in {"xlsx", "csv"}:
        output_format = "xlsx"
    export_plan = _export_plan_from_args(
        ctx,
        module,
        {
            **request,
            "module": module,
            "export_type": default_export_type,
            "requested_columns": requested_columns,
            "include_all_allowed_columns": include_all_allowed_columns,
            "include_chart": include_chart,
            "output_format": output_format,
        },
    )
    return {
        "requested_columns": requested_columns,
        "include_all_allowed_columns": include_all_allowed_columns,
        "include_chart": include_chart,
        "async_export": async_export,
        "output_format": output_format,
        "export_plan": export_plan,
    }


def export_current_page_excel(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = _module_token(str(args.get("module") or ctx.page or "overview"))
    options = _export_common_kwargs(ctx, module, args, default_export_type="current_page")
    sheets = _workbook_for_module_page(ctx, module, args)
    chart_specs = _chart_specs_for_sheets(sheets, module=module, include_chart=bool(options.get("include_chart")))
    return _register_workbook_export(
        ctx,
        module=module,
        export_type="current_page",
        sheets=sheets,
        filename_stem=f"{module}_page_export",
        notes=["Workbook uses current page filters and permission-scoped fields."],
        chart_specs=chart_specs,
        output_format=str(options.get("output_format") or "xlsx"),
        requested_columns=list(options.get("requested_columns") or []),
        include_all_allowed_columns=bool(options.get("include_all_allowed_columns")),
        export_plan=options.get("export_plan") if isinstance(options.get("export_plan"), Mapping) else {},
        async_export=bool(options.get("async_export")),
    )


def export_current_entity_history_excel(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = _module_token(str(args.get("module") or ctx.page or "overview"))
    options = _export_common_kwargs(ctx, module, args, default_export_type="entity_history")
    history = _history_for_module(ctx, module, args)
    data = history.get("data") if isinstance(history.get("data"), Mapping) else {}
    sheets: Dict[str, pd.DataFrame] = {
        "History Summary": _to_frame(data.get("kpis") or data.get("summary") or {}),
        "History Trend": _trend_to_frame(data.get("trend") or data.get("history_rows") or {}),
        "History Table": _to_frame((data.get("table") or {}).get("rows") if isinstance(data.get("table"), Mapping) else data.get("table")),
    }
    if bool(options.get("include_chart")) and "History Trend" in sheets:
        sheets["Trend"] = sheets["History Trend"]
    chart_specs = _chart_specs_for_sheets(sheets, module=module, include_chart=bool(options.get("include_chart")))
    entity_id = _entity_hint(ctx, args, module.rstrip("s"))
    stem = f"{module}_{entity_id or 'history'}_export"
    return _register_workbook_export(
        ctx,
        module=module,
        export_type="entity_history",
        sheets=sheets,
        filename_stem=stem,
        notes=["Entity history export uses current scope and may be sparse when selected entity context is missing."],
        chart_specs=chart_specs,
        output_format=str(options.get("output_format") or "xlsx"),
        requested_columns=list(options.get("requested_columns") or []),
        include_all_allowed_columns=bool(options.get("include_all_allowed_columns")),
        export_plan=options.get("export_plan") if isinstance(options.get("export_plan"), Mapping) else {},
        async_export=bool(options.get("async_export")),
    )


def export_analysis_bundle_excel(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = _module_token(str(args.get("module") or ctx.page or "overview"))
    options = _export_common_kwargs(ctx, module, args, default_export_type="analysis_bundle")
    summary = summarize_module_state(ctx, {"module": module})
    watchouts = get_entity_watchouts(ctx, {"module": module})
    actions = get_priority_actions(ctx, {})
    history = _history_for_module(ctx, module, args)
    sheets = {
        "Summary": _to_frame(summary.get("data") if isinstance(summary.get("data"), Mapping) else {}),
        "Watchouts": _to_frame((watchouts.get("data") or {}).get("watchouts") if isinstance(watchouts.get("data"), Mapping) else watchouts.get("data")),
        "Actions": _to_frame({"actions": list(((actions.get("data") or {}).get("actions") or []) if isinstance(actions.get("data"), Mapping) else [])}),
        "History": _to_frame(history.get("data") if isinstance(history.get("data"), Mapping) else {}),
    }
    if bool(options.get("include_chart")) and "History" in sheets:
        history_frame = sheets.get("History")
        if isinstance(history_frame, pd.DataFrame) and not history_frame.empty:
            sheets["Trend"] = history_frame
    chart_specs = _chart_specs_for_sheets(sheets, module=module, include_chart=bool(options.get("include_chart")))
    return _register_workbook_export(
        ctx,
        module=module,
        export_type="analysis_bundle",
        sheets=sheets,
        filename_stem=f"{module}_analysis_bundle",
        notes=["Bundle includes summary, watchouts, actions, and history context."],
        chart_specs=chart_specs,
        output_format=str(options.get("output_format") or "xlsx"),
        requested_columns=list(options.get("requested_columns") or []),
        include_all_allowed_columns=bool(options.get("include_all_allowed_columns")),
        export_plan=options.get("export_plan") if isinstance(options.get("export_plan"), Mapping) else {},
        async_export=bool(options.get("async_export")),
    )


def export_leadership_pack_excel(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = _module_token(str(args.get("module") or ctx.page or "overview"))
    options = _export_common_kwargs(ctx, module, args, default_export_type="leadership_pack")
    digest = get_leadership_summary(ctx, {"module": module, "length": "short"})
    risks = get_priority_risks(ctx, {"module": module})
    actions = get_priority_actions(ctx, {})
    trust = get_confidence_or_trust_summary(ctx, {})
    sheets = {
        "Leadership Summary": _to_frame(digest.get("data") if isinstance(digest.get("data"), Mapping) else {}),
        "Priority Risks": _to_frame((risks.get("data") or {}).get("risks") if isinstance(risks.get("data"), Mapping) else []),
        "Actions": _to_frame({"actions": list(((actions.get("data") or {}).get("actions") or []) if isinstance(actions.get("data"), Mapping) else [])}),
        "Trust": _to_frame(trust.get("data") if isinstance(trust.get("data"), Mapping) else {}),
    }
    chart_specs = _chart_specs_for_sheets(sheets, module=module, include_chart=bool(options.get("include_chart")))
    return _register_workbook_export(
        ctx,
        module=module,
        export_type="leadership_pack",
        sheets=sheets,
        filename_stem=f"{module}_leadership_pack",
        notes=["Leadership pack is concise and scoped; verify caveats in Trust sheet before distribution."],
        chart_specs=chart_specs,
        output_format=str(options.get("output_format") or "xlsx"),
        requested_columns=list(options.get("requested_columns") or []),
        include_all_allowed_columns=bool(options.get("include_all_allowed_columns")),
        export_plan=options.get("export_plan") if isinstance(options.get("export_plan"), Mapping) else {},
        async_export=bool(options.get("async_export")),
    )


def export_watchlist_excel(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = _module_token(str(args.get("module") or ctx.page or "overview"))
    options = _export_common_kwargs(ctx, module, args, default_export_type="watchlist")
    watchouts = get_entity_watchouts(ctx, {"module": module})
    data = watchouts.get("data") if isinstance(watchouts.get("data"), Mapping) else {}
    sheets = {
        "Watchouts": _to_frame(data.get("watchouts") if isinstance(data, Mapping) else data),
        "Rows": _to_frame(data.get("risk_rows") if isinstance(data, Mapping) else []),
        "Metadata": _metadata_frame(ctx, module=module, export_type="watchlist", notes=["Watchlist rows are permission-filtered."]),
    }
    chart_specs = _chart_specs_for_sheets(sheets, module=module, include_chart=bool(options.get("include_chart")))
    return _register_workbook_export(
        ctx,
        module=module,
        export_type="watchlist",
        sheets=sheets,
        filename_stem=f"{module}_watchlist",
        notes=["Watchlist export includes current scoped risk rows and caveats."],
        chart_specs=chart_specs,
        output_format=str(options.get("output_format") or "xlsx"),
        requested_columns=list(options.get("requested_columns") or []),
        include_all_allowed_columns=bool(options.get("include_all_allowed_columns")),
        export_plan=options.get("export_plan") if isinstance(options.get("export_plan"), Mapping) else {},
        async_export=bool(options.get("async_export")),
    )


def export_custom_scoped_excel(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = _module_token(str(args.get("module") or ctx.page or "overview"))
    options = _export_common_kwargs(ctx, module, args, default_export_type="custom_scoped")
    include_history = bool(args.get("include_history", True))
    include_watchouts = bool(args.get("include_watchouts", True))
    include_actions = bool(args.get("include_actions", True))
    sheets = _workbook_for_module_page(ctx, module, args)
    if include_history:
        history = _history_for_module(ctx, module, args)
        hdata = history.get("data") if isinstance(history.get("data"), Mapping) else {}
        sheets["History"] = _to_frame(hdata.get("trend") or hdata)
    if include_watchouts:
        watchouts = get_entity_watchouts(ctx, {"module": module})
        sheets["Watchouts"] = _to_frame((watchouts.get("data") or {}).get("watchouts") if isinstance(watchouts.get("data"), Mapping) else [])
    if include_actions:
        actions = get_priority_actions(ctx, {})
        sheets["Actions"] = _to_frame({"actions": list(((actions.get("data") or {}).get("actions") or []) if isinstance(actions.get("data"), Mapping) else [])})
    chart_specs = _chart_specs_for_sheets(sheets, module=module, include_chart=bool(options.get("include_chart")))
    return _register_workbook_export(
        ctx,
        module=module,
        export_type="custom_scoped",
        sheets=sheets,
        filename_stem=f"{module}_custom_scoped_export",
        notes=["Custom scoped export honors include flags and current permission scope."],
        chart_specs=chart_specs,
        output_format=str(options.get("output_format") or "xlsx"),
        requested_columns=list(options.get("requested_columns") or []),
        include_all_allowed_columns=bool(options.get("include_all_allowed_columns")),
        export_plan=options.get("export_plan") if isinstance(options.get("export_plan"), Mapping) else {},
        async_export=bool(options.get("async_export")),
    )


def export_nested_ranking_excel(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = _dimension_module(args.get("parent_entity_type") or args.get("entity_type") or ctx.page)
    options = _export_common_kwargs(ctx, module, args, default_export_type="hierarchical_analysis")
    nested = get_nested_rankings(ctx, args)
    data = nested.get("data") if isinstance(nested.get("data"), Mapping) else {}
    groups = [item for item in list(data.get("groups") or []) if isinstance(item, Mapping)]
    parent_rows = [
        {
            "parent_label": str(group.get("parent_label") or ""),
            "parent_metric_value": group.get("metric_value"),
            "parent_rank": group.get("rank"),
            "child_count": len(list(group.get("children") or [])),
        }
        for group in groups
    ]
    sheets = {
        "Parent Summary": _to_frame(parent_rows),
        "Child Detail": _to_frame(_flatten_nested_groups(groups)),
        "Metadata": _metadata_frame(
            ctx,
            module=module,
            export_type="hierarchical_analysis",
            notes=[
                f"parent_type={data.get('parent_type')}",
                f"child_type={data.get('child_type')}",
                f"metric={data.get('metric')}",
            ],
        ),
    }
    chart_specs = _chart_specs_for_sheets(sheets, module=module, include_chart=bool(options.get("include_chart")))
    return _register_workbook_export(
        ctx,
        module=module,
        export_type="hierarchical_analysis",
        sheets=sheets,
        filename_stem=f"{module}_hierarchical_analysis",
        notes=["Hierarchical export includes parent summary and flattened child detail rows."],
        chart_specs=chart_specs,
        output_format=str(options.get("output_format") or "xlsx"),
        requested_columns=list(options.get("requested_columns") or []),
        include_all_allowed_columns=bool(options.get("include_all_allowed_columns")),
        export_plan=options.get("export_plan") if isinstance(options.get("export_plan"), Mapping) else {},
        async_export=bool(options.get("async_export")),
    )


def export_hierarchical_analysis_excel(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return export_nested_ranking_excel(ctx, args)


def export_ranked_list_excel(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    options = _export_common_kwargs(ctx, _dimension_module(args.get("entity_type") or args.get("dimension") or ctx.page), args, default_export_type="ranked_list")
    ranking = rank_entities(ctx, args)
    data = ranking.get("data") if isinstance(ranking.get("data"), Mapping) else {}
    module = _dimension_module(data.get("dimension") or args.get("entity_type") or ctx.page)
    rows = list(data.get("rows") or [])
    sheets = {
        "Ranked List": _to_frame(rows),
        "Metadata": _metadata_frame(
            ctx,
            module=module,
            export_type="ranked_list",
            notes=[f"metric={data.get('metric')}", f"direction={data.get('direction')}", f"limit={data.get('limit')}"],
        ),
    }
    chart_specs = _chart_specs_for_sheets(sheets, module=module, include_chart=bool(options.get("include_chart")))
    return _register_workbook_export(
        ctx,
        module=module,
        export_type="ranked_list",
        sheets=sheets,
        filename_stem=f"{module}_ranked_list",
        notes=["Ranked list export is generated from permission-scoped deterministic rankings."],
        chart_specs=chart_specs,
        output_format=str(options.get("output_format") or "xlsx"),
        requested_columns=list(options.get("requested_columns") or []),
        include_all_allowed_columns=bool(options.get("include_all_allowed_columns")),
        export_plan=options.get("export_plan") if isinstance(options.get("export_plan"), Mapping) else {},
        async_export=bool(options.get("async_export")),
    )


def export_grouped_metric_excel(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    options = _export_common_kwargs(ctx, _dimension_module(args.get("dimension") or args.get("entity_type") or ctx.page), args, default_export_type="grouped_metric")
    grouped = aggregate_by_dimension(ctx, args)
    data = grouped.get("data") if isinstance(grouped.get("data"), Mapping) else {}
    module = _dimension_module(data.get("dimension") or args.get("dimension") or ctx.page)
    sheets = {
        "Grouped Metric": _to_frame(data.get("groups") or []),
        "Metadata": _metadata_frame(
            ctx,
            module=module,
            export_type="grouped_metric",
            notes=[f"metric={data.get('metric')}", f"dimension={data.get('dimension')}", f"limit={data.get('limit')}"],
        ),
    }
    chart_specs = _chart_specs_for_sheets(sheets, module=module, include_chart=bool(options.get("include_chart")))
    return _register_workbook_export(
        ctx,
        module=module,
        export_type="grouped_metric",
        sheets=sheets,
        filename_stem=f"{module}_grouped_metric",
        notes=["Grouped metric export preserves scoped aggregation rows and metadata."],
        chart_specs=chart_specs,
        output_format=str(options.get("output_format") or "xlsx"),
        requested_columns=list(options.get("requested_columns") or []),
        include_all_allowed_columns=bool(options.get("include_all_allowed_columns")),
        export_plan=options.get("export_plan") if isinstance(options.get("export_plan"), Mapping) else {},
        async_export=bool(options.get("async_export")),
    )


def export_comparison_excel(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    options = _export_common_kwargs(ctx, _dimension_module(args.get("dimension") or args.get("entity_type") or ctx.page), args, default_export_type="comparison")
    comparison = compare_entities(ctx, args)
    period = compare_periods(ctx, {"module": _dimension_module(args.get("dimension") or args.get("entity_type") or ctx.page)})
    data = comparison.get("data") if isinstance(comparison.get("data"), Mapping) else {}
    module = _dimension_module(data.get("dimension") or args.get("dimension") or ctx.page)
    sheets = {
        "Top Comparison": _to_frame(data.get("top") or []),
        "Bottom Comparison": _to_frame(data.get("bottom") or []),
        "Period Comparison": _to_frame(period.get("data") if isinstance(period.get("data"), Mapping) else {}),
        "Metadata": _metadata_frame(ctx, module=module, export_type="comparison", notes=["Comparison export is scope and permission aware."]),
    }
    chart_specs = _chart_specs_for_sheets(sheets, module=module, include_chart=bool(options.get("include_chart")))
    return _register_workbook_export(
        ctx,
        module=module,
        export_type="comparison",
        sheets=sheets,
        filename_stem=f"{module}_comparison_export",
        notes=["Comparison export combines entity ranking deltas and period context."],
        chart_specs=chart_specs,
        output_format=str(options.get("output_format") or "xlsx"),
        requested_columns=list(options.get("requested_columns") or []),
        include_all_allowed_columns=bool(options.get("include_all_allowed_columns")),
        export_plan=options.get("export_plan") if isinstance(options.get("export_plan"), Mapping) else {},
        async_export=bool(options.get("async_export")),
    )


def _chart_series_svg(
    rows: Sequence[Mapping[str, Any]],
    *,
    title: str,
    chart_type: str = "line",
) -> bytes:
    width = 980
    height = 520
    left = 78
    right = 940
    top = 68
    bottom = 452
    values: List[float] = []
    labels: List[str] = []
    for row in list(rows or []):
        if not isinstance(row, Mapping):
            continue
        labels.append(str(row.get("label") or ""))
        try:
            values.append(float(row.get("value") or 0.0))
        except Exception:
            values.append(0.0)
    if not values:
        values = [0.0]
        labels = ["n/a"]
    vmin = min(values)
    vmax = max(values)
    if vmax == vmin:
        vmax = vmin + 1.0
    count = max(1, len(values))
    step_x = (right - left) / float(max(1, count - 1))

    def _x(i: int) -> float:
        return float(left + (i * step_x))

    def _y(v: float) -> float:
        ratio = (float(v) - vmin) / (vmax - vmin)
        return float(bottom - (ratio * (bottom - top)))

    parts: List[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect x="0" y="0" width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{left}" y="34" font-family="Arial, sans-serif" font-size="22" fill="#1f2937">{html_escape(title)}</text>',
        f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke="#94a3b8" stroke-width="1.2"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" stroke="#94a3b8" stroke-width="1.2"/>',
    ]
    ticks = 5
    for idx in range(ticks + 1):
        ratio = idx / float(ticks)
        y = bottom - ((bottom - top) * ratio)
        val = vmin + ((vmax - vmin) * ratio)
        parts.append(f'<line x1="{left - 4}" y1="{y:.2f}" x2="{right}" y2="{y:.2f}" stroke="#eef2f7" stroke-width="1"/>')
        parts.append(
            f'<text x="{left - 8}" y="{y + 4:.2f}" text-anchor="end" font-family="Arial, sans-serif" font-size="11" fill="#64748b">{val:,.2f}</text>'
        )

    if str(chart_type or "").strip().lower() == "bar":
        bar_width = max(6.0, min(42.0, (right - left) / float(max(1, count * 1.8))))
        for i, value in enumerate(values):
            x = _x(i) - (bar_width / 2.0)
            y = _y(value)
            h = max(1.0, bottom - y)
            parts.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_width:.2f}" height="{h:.2f}" fill="#1d4ed8" opacity="0.9"/>')
    else:
        points = " ".join(f"{_x(i):.2f},{_y(v):.2f}" for i, v in enumerate(values))
        parts.append(f'<polyline points="{points}" fill="none" stroke="#1d4ed8" stroke-width="2.4"/>')
        for i, value in enumerate(values):
            cx = _x(i)
            cy = _y(value)
            parts.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="3.2" fill="#1d4ed8"/>')

    label_step = max(1, int(len(labels) / 8) if len(labels) > 8 else 1)
    for i, label in enumerate(labels):
        if i % label_step != 0 and i != len(labels) - 1:
            continue
        x = _x(i)
        text = html_escape(label[:16])
        parts.append(
            f'<text x="{x:.2f}" y="{bottom + 18}" text-anchor="middle" font-family="Arial, sans-serif" font-size="11" fill="#64748b">{text}</text>'
        )

    parts.append("</svg>")
    return "\n".join(parts).encode("utf-8")


def _chart_series_png_bytes(
    rows: Sequence[Mapping[str, Any]],
    *,
    title: str,
    chart_type: str = "line",
) -> bytes | None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None
    labels: List[str] = []
    values: List[float] = []
    for row in list(rows or []):
        if not isinstance(row, Mapping):
            continue
        labels.append(str(row.get("label") or ""))
        try:
            values.append(float(row.get("value") or 0.0))
        except Exception:
            values.append(0.0)
    if not labels:
        labels = ["n/a"]
        values = [0.0]
    fig, ax = plt.subplots(figsize=(10, 4.8), dpi=140)
    if str(chart_type or "").strip().lower() == "bar":
        ax.bar(labels, values, color="#1d4ed8")
    else:
        ax.plot(labels, values, color="#1d4ed8", marker="o", linewidth=2)
    ax.set_title(title)
    ax.grid(alpha=0.2)
    fig.tight_layout()
    try:
        buf = BytesIO()
        fig.savefig(buf, format="png")
        buf.seek(0)
        return buf.read()
    finally:
        plt.close(fig)


def export_chart_series_file(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = _module_token(str(args.get("module") or ctx.page or "overview"))
    series_tool = get_chart_series(ctx, args)
    series_data = series_tool.get("data") if isinstance(series_tool.get("data"), Mapping) else {}
    rows = list(series_data.get("series") or [])
    if not rows:
        fallback_module = module
        page_module = _module_token(str(ctx.page or "overview"))
        if not _can_export_module(ctx, fallback_module) and _can_export_module(ctx, page_module):
            fallback_module = page_module
        fallback_args = {**args, "module": fallback_module, "include_chart": False}
        fallback: Dict[str, Any]
        ranked_candidate: Dict[str, Any] | None = None
        if str(args.get("dimension") or args.get("entity_type") or "").strip():
            ranked_candidate = export_ranked_list_excel(
                ctx,
                {
                    **fallback_args,
                    "entity_type": str(args.get("entity_type") or args.get("dimension")),
                },
            )
            if str(ranked_candidate.get("status") or "").strip().lower() == "ok":
                fallback = ranked_candidate
            else:
                # Region/supplier/etc. ranking export may be permission-limited for some roles.
                # Fall back to current-page scoped export if ranked export is not allowed/available.
                fallback = export_current_page_excel(ctx, fallback_args)
        else:
            fallback = export_current_page_excel(ctx, fallback_args)
        if str(fallback.get("status") or "").lower() == "ok":
            notes = list(fallback.get("notes") or [])
            notes.append("Requested chart series was empty; generated scoped workbook without chart sheet.")
            fallback["notes"] = notes
        return fallback
    chart_frame = _to_frame(rows)
    sheets = {
        "Chart Series": chart_frame,
        "Metadata": _metadata_frame(
            ctx,
            module=module,
            export_type="chart_series",
            notes=["Chart-series export includes scoped labels/values for downstream graphing."],
        ),
    }
    chart_specs = [
        {
            "source_sheet": "Chart Series",
            "chart_sheet": "Charts",
            "chart_type": str(series_data.get("chart_type") or "line"),
            "category_col": "label",
            "value_cols": ["value"],
            "title": f"{module.title()} {str(series_data.get('metric') or 'metric').title()}",
            "x_axis": "label",
            "y_axis": "value",
            "insert_cell": "B2",
        }
    ]
    options = _export_common_kwargs(ctx, module, {**args, "include_chart": True}, default_export_type="chart_series")
    return _register_workbook_export(
        ctx,
        module=module,
        export_type="chart_series",
        sheets=sheets,
        filename_stem=f"{module}_chart_series",
        notes=["Chart-series export is permission-scoped and suitable for chart-only requests."],
        chart_specs=chart_specs,
        output_format=str(options.get("output_format") or "xlsx"),
        requested_columns=list(options.get("requested_columns") or []),
        include_all_allowed_columns=bool(options.get("include_all_allowed_columns")),
        export_plan=options.get("export_plan") if isinstance(options.get("export_plan"), Mapping) else {},
        async_export=bool(options.get("async_export")),
    )


def export_chart_image_file(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = _module_token(str(args.get("module") or ctx.page or "overview"))
    series_tool = get_chart_series(ctx, args)
    series_data = series_tool.get("data") if isinstance(series_tool.get("data"), Mapping) else {}
    metric = str(series_data.get("metric") or args.get("metric") or "metric").strip()
    chart_type = str(series_data.get("chart_type") or args.get("chart_type") or "line").strip().lower()
    if chart_type not in {"line", "bar"}:
        chart_type = "line"
    rows = list(series_data.get("series") or [])
    fallback_note = ""
    if not rows:
        ranked = get_ranked_dataset(
            ctx,
            {
                **args,
                "module": module,
                "metric": metric,
                "limit": int(args.get("limit") or 10),
                "direction": str(args.get("direction") or "top"),
            },
        )
        ranked_data = ranked.get("data") if isinstance(ranked.get("data"), Mapping) else {}
        ranked_rows = [dict(item) for item in list(ranked_data.get("rows") or []) if isinstance(item, Mapping)]
        if ranked_rows:
            ranked_module = _dimension_module(ranked_data.get("dimension") or module)
            ranked_metric = _normalize_metric_token(ranked_data.get("metric") or metric)
            rows = [
                {
                    "label": _dimension_label_from_row(row, ranked_module),
                    "value": float(_metric_value_from_row(row, ranked_metric) or 0.0),
                }
                for row in ranked_rows[:20]
            ]
            chart_type = "bar"
            fallback_note = "Trend rows were sparse; generated chart from scoped ranked rows."
    if not rows:
        summary = get_current_page_summary(ctx, {"module": module})
        sdata = summary.get("data") if isinstance(summary.get("data"), Mapping) else {}
        kpis = sdata.get("kpis") if isinstance(sdata.get("kpis"), Mapping) else {}
        val_num = None
        for candidate in (
            kpis.get(metric),
            kpis.get("revenue"),
            kpis.get("profit"),
            kpis.get("margin_pct"),
            sdata.get("revenue"),
            sdata.get("profit"),
            sdata.get("margin_pct"),
        ):
            coerced = _num_or_none(candidate)
            if coerced is not None:
                val_num = coerced
                break
        if val_num is not None:
            rows = [{"label": "Current Window", "value": float(val_num)}]
            fallback_note = "Trend rows were unavailable; generated chart from current scoped KPI value."
    if not rows:
        rows = [{"label": "No scoped data", "value": 0.0}]
        if not fallback_note:
            fallback_note = "No chart rows were available; generated a placeholder chart with scoped no-data label."
    requested_format = str(args.get("image_format") or args.get("format") or "").strip().lower()
    if requested_format not in {"png", "svg"}:
        requested_format = "png" if "png" in str(args.get("message") or "").lower() else "svg"
    title = f"{module.title()} {metric.title()} Trend"
    content_type = "image/svg+xml"
    chart_data = _chart_series_svg(rows, title=title, chart_type=chart_type)
    ext = "svg"
    notes = ["Chart image export is scope-filtered and permission-safe."]
    if fallback_note:
        notes.append(fallback_note)
    if requested_format == "png":
        png_bytes = _chart_series_png_bytes(rows, title=title, chart_type=chart_type)
        if png_bytes:
            chart_data = png_bytes
            content_type = "image/png"
            ext = "png"
        else:
            notes.append("PNG renderer unavailable in this environment; generated SVG image instead.")

    stem = sanitize_filename(f"{module}_{metric}_chart", default="assistant_chart_export")
    filename = f"{stem}.{ext}"
    artifact = create_export(
        getattr(ctx.user, "id", "anon"),
        filename=filename,
        data=chart_data,
        content_type=content_type,
        meta={
            "module": module,
            "export_type": "chart_image",
            "format": ext,
            "metric": metric,
            "chart_type": chart_type,
            "rows": len(rows),
            "scope_mode": (_scope_used(ctx) or {}).get("scope_mode"),
            "window": _window_used(ctx),
        },
    )
    download_url = f"/ai/exports/{artifact.export_id}/download"
    return _tool_response(
        status="ok",
        title="Assistant Chart Export",
        data={
            "export_id": artifact.export_id,
            "filename": artifact.filename,
            "format": ext,
            "download_url": download_url,
            "api_download_url": f"/api/assistant/exports/{artifact.export_id}/download",
            "expires_at": artifact.expires_at,
            "metric": metric,
            "chart_type": chart_type,
            "row_count": len(rows),
        },
        ctx=ctx,
        notes=notes,
        citations=["assistant.export_store"],
        next_actions=[
            "Download chart image from the link.",
            "Ask for workbook version if tabular detail is needed.",
        ],
        module=module,
    )


def export_custom_analysis_file(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = _module_token(str(args.get("module") or ctx.page or "overview"))
    plan = _export_plan_from_args(ctx, module, args)
    intent = str(plan.get("export_intent_type") or "").strip().lower()
    merged_args = {
        **args,
        "module": module,
        "output_format": plan.get("format"),
        "include_chart": bool(plan.get("include_chart")),
        "async_export": bool(args.get("async_export", False)),
    }
    if intent == "export_ranked_list":
        return export_ranked_list_excel(ctx, merged_args)
    if intent == "export_hierarchical_analysis":
        return export_hierarchical_analysis_excel(ctx, merged_args)
    if intent == "export_grouped_metric":
        return export_grouped_metric_excel(ctx, merged_args)
    if intent == "export_entity_history":
        return export_current_entity_history_excel(ctx, merged_args)
    if intent == "export_comparison":
        return export_comparison_excel(ctx, merged_args)
    if intent == "export_leadership_pack":
        return export_leadership_pack_excel(ctx, merged_args)
    if intent in {"export_chart_only", "export_chart_plus_data"}:
        if bool(args.get("chart_image_only")):
            return export_chart_image_file(ctx, merged_args)
        return export_chart_series_file(ctx, merged_args)
    if intent in {"export_custom_analysis_pack"}:
        return export_analysis_bundle_excel(ctx, merged_args)
    return export_current_page_excel(ctx, merged_args)


def build_export_configuration(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = _module_token(str(args.get("module") or ctx.page or "overview"))
    requested_columns = [str(item) for item in list(args.get("requested_columns") or []) if str(item).strip()]
    config = {
        "module": module,
        "window_mode": str(args.get("window_mode") or "current_page"),
        "include_history": bool(args.get("include_history", True)),
        "include_watchouts": bool(args.get("include_watchouts", True)),
        "include_actions": bool(args.get("include_actions", True)),
        "include_chart": bool(args.get("include_chart", False)),
        "chart_image_only": bool(args.get("chart_image_only", False)),
        "image_format": str(args.get("image_format") or ""),
        "include_summary_sheet": bool(args.get("include_summary_sheet", True)),
        "include_metadata_sheet": bool(args.get("include_metadata_sheet", True)),
        "include_all_allowed_columns": bool(args.get("include_all_allowed_columns", False)),
        "async_export": bool(args.get("async_export", False)),
        "requested_columns": requested_columns,
        "output_format": str(args.get("output_format") or "xlsx"),
        "export_intent_type": str(args.get("export_intent_type") or "export_table"),
        "audience": str(args.get("audience") or "manager"),
        "detail_level": str(args.get("detail_level") or "standard"),
        "metric": str(args.get("metric") or "revenue"),
        "group_by_dimension": str(args.get("dimension") or args.get("group_by_dimension") or ""),
        "ranking_direction": str(args.get("direction") or "top"),
        "limit": max(1, min(500, int(args.get("limit") or 25))),
        "exclude_low_base": bool(args.get("exclude_low_base", False)),
    }
    plan = _export_plan_from_args(ctx, module, config)
    return _tool_response(
        status="ok",
        title="Export Configuration Draft",
        data={"configuration": config, "export_plan": plan, "review_required": True, "non_destructive": True},
        ctx=ctx,
        module=module,
        notes=["Configuration is a draft and does not mutate saved views or production data."],
    )


def refine_export_request(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    base = dict(args.get("base") or {})
    instruction = str(args.get("instruction") or args.get("refinement") or "").strip().lower()
    if "leadership" in instruction:
        base["audience"] = "leadership"
        base["detail_level"] = "short"
    if "detailed" in instruction or "analyst" in instruction:
        base["detail_level"] = "detailed"
    if "full history" in instruction or "24 month" in instruction:
        base["window_mode"] = "full_history"
        base["include_history"] = True
    if "exclude low-base" in instruction or "exclude low base" in instruction:
        base["exclude_low_base"] = True
    if "all available columns" in instruction or "all visible columns" in instruction or "all columns" in instruction:
        base["include_all_allowed_columns"] = True
    if "chart" in instruction or "graph" in instruction:
        base["include_chart"] = True
    if "chart image" in instruction or "graph image" in instruction or "as png" in instruction or "as svg" in instruction:
        base["chart_image_only"] = True
    if "png" in instruction:
        base["image_format"] = "png"
    elif "svg" in instruction:
        base["image_format"] = "svg"
    if "async" in instruction or "background" in instruction:
        base["async_export"] = True
    if "csv" in instruction:
        base["output_format"] = "csv"
    if "xlsx" in instruction or "excel" in instruction:
        base["output_format"] = "xlsx"
    if "customer-only" in instruction or "customer only" in instruction:
        base["module"] = "customers"
    module = _module_token(str(base.get("module") or ctx.page))
    base["module"] = module
    plan = _export_plan_from_args(ctx, module, base)
    return _tool_response(
        status="ok",
        title="Refined Export Request",
        data={"configuration": base, "export_plan": plan, "review_required": True, "non_destructive": True},
        ctx=ctx,
        module=module,
    )


def build_saved_view_suggestion(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = _module_token(str(args.get("module") or ctx.page or "overview"))
    suggestion = {
        "name": str(args.get("name") or f"{module.title()} Investigation View"),
        "module": module,
        "filters": _filters_source(ctx),
        "visible_sections": list((ctx.page_state or {}).get("visible_sections") or []),
        "purpose": str(args.get("purpose") or "Focused investigation view"),
        "review_required": True,
        "non_destructive": True,
    }
    return _tool_response(
        status="ok",
        title="Saved View Suggestion",
        data=suggestion,
        ctx=ctx,
        module=module,
        notes=["Suggestion is not automatically persisted; route through existing saved-view workflow."],
    )


def build_analysis_bundle_request(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = _module_token(str(args.get("module") or ctx.page or "overview"))
    request_payload = {
        "module": module,
        "bundle_type": str(args.get("bundle_type") or "analysis"),
        "include_sections": list(args.get("include_sections") or ["summary", "history", "watchouts", "actions"]),
        "filters": _filters_source(ctx),
        "review_required": True,
        "non_destructive": True,
    }
    return _tool_response(
        status="ok",
        title="Analysis Bundle Request Draft",
        data=request_payload,
        ctx=ctx,
        module=module,
    )


def set_answer_mode(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    mode = str((args or {}).get("mode") or "standard").strip().lower()
    if mode not in {"standard", "executive", "analyst", "simple"}:
        mode = "standard"
    return _tool_response(
        status="ok",
        title="Answer Mode Set",
        data={"mode": mode, "review_required": False, "non_destructive": True},
        ctx=ctx,
        module=ctx.page,
    )


def set_export_mode(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    mode = str((args or {}).get("mode") or "standard").strip().lower()
    if mode not in {"standard", "leadership", "analyst"}:
        mode = "standard"
    return _tool_response(
        status="ok",
        title="Export Mode Set",
        data={"mode": mode, "review_required": False, "non_destructive": True},
        ctx=ctx,
        module=ctx.page,
    )


def _module_open_path(module: str) -> str:
    token = str(module or "").strip().lower()
    mapping = {
        "overview": "/overview",
        "customers": "/customers",
        "products": "/products",
        "regions": "/regions",
        "suppliers": "/suppliers",
        "salesreps": "/salesreps",
        "returns": "/returns",
        "assistant": "/assistant",
    }
    return mapping.get(token, "/assistant")


def _overview_signal_cards(ctx: ToolContext) -> List[Dict[str, Any]]:
    cards: List[Dict[str, Any]] = []
    if not rbac.can_view_page("overview", ctx.user):
        return cards
    payload = _overview_context(ctx)
    kpis = payload.get("scorecard_kpis") if isinstance(payload.get("scorecard_kpis"), Mapping) else {}
    risk = payload.get("risk") if isinstance(payload.get("risk"), Mapping) else {}
    concentration = risk.get("concentration") if isinstance(risk.get("concentration"), Mapping) else {}
    profitability = risk.get("profitability") if isinstance(risk.get("profitability"), Mapping) else {}
    health = payload.get("data_health") if isinstance(payload.get("data_health"), Mapping) else {}
    forecast = payload.get("forecast") if isinstance(payload.get("forecast"), Mapping) else {}

    revenue_mom = _safe_get_number(
        kpis,
        (
            "revenue_mom_pct",
            "mom_revenue_pct",
            "revenue_delta_pct",
            "delta_revenue_pct",
            "mom_growth",
        ),
    )
    profit_mom = _safe_get_number(
        kpis,
        (
            "profit_mom_pct",
            "mom_profit_pct",
            "profit_delta_pct",
        ),
    )
    if revenue_mom is not None and abs(revenue_mom) >= 4:
        cards.append(
            {
                "id": "overview_revenue_shift",
                "module": "overview",
                "title": "Material Revenue Shift",
                "narrative": f"Revenue moved {_pct(revenue_mom)} versus prior period in the active scope.",
                "severity": "high" if abs(revenue_mom) >= 8 else "medium",
                "priority": 90 if abs(revenue_mom) >= 8 else 76,
                "confidence": 0.82,
                "evidence": {"revenue_mom_pct": revenue_mom},
                "open_path": _module_open_path("overview"),
            }
        )
    if profit_mom is not None and revenue_mom is not None and (profit_mom + 2.0) < revenue_mom:
        cards.append(
            {
                "id": "overview_profit_vs_revenue",
                "module": "overview",
                "title": "Profit Underperforming Revenue",
                "narrative": (
                    f"Profit movement ({_pct(profit_mom)}) is weaker than revenue ({_pct(revenue_mom)}), "
                    "which may indicate cost pressure or unfavorable mix."
                ),
                "severity": "high" if (revenue_mom - profit_mom) >= 6 else "medium",
                "priority": 84,
                "confidence": 0.76,
                "evidence": {"profit_mom_pct": profit_mom, "revenue_mom_pct": revenue_mom},
                "open_path": _module_open_path("overview"),
            }
        )

    hhi = _safe_get_number(concentration, ("hhi", "customer_hhi", "product_hhi"))
    if hhi is not None and hhi >= 1800:
        cards.append(
            {
                "id": "overview_concentration_risk",
                "module": "overview",
                "title": "Concentration Risk Elevated",
                "narrative": f"Concentration index is {hhi:.0f}; dependency exposure is increasing in this window.",
                "severity": "high" if hhi >= 2500 else "medium",
                "priority": 88 if hhi >= 2500 else 74,
                "confidence": 0.87,
                "evidence": {"hhi": hhi},
                "open_path": _module_open_path("overview"),
            }
        )

    margin_rows = list(profitability.get("margin_risk") or [])
    if margin_rows:
        cards.append(
            {
                "id": "overview_margin_risk_rows",
                "module": "overview",
                "title": "Margin-Risk Entities Identified",
                "narrative": f"{len(margin_rows)} entities are currently in the margin-risk watchlist.",
                "severity": "high" if len(margin_rows) >= 10 else "medium",
                "priority": 82,
                "confidence": 0.8,
                "evidence": {"margin_risk_count": len(margin_rows)},
                "open_path": _module_open_path("overview"),
            }
        )

    cost_coverage = _safe_get_number(health, ("cost_coverage_pct", "cost_coverage"))
    pack_coverage = _safe_get_number(health, ("pack_coverage_pct", "pack_coverage"))
    if cost_coverage is not None and cost_coverage < 92:
        cards.append(
            {
                "id": "overview_cost_coverage_caveat",
                "module": "overview",
                "title": "Confidence Reduced By Coverage",
                "narrative": f"Cost coverage is {_pct(cost_coverage)}; profit and margin conclusions should be treated as directional.",
                "severity": "medium",
                "priority": 70,
                "confidence": 0.9,
                "evidence": {"cost_coverage_pct": cost_coverage, "pack_coverage_pct": pack_coverage},
                "open_path": _module_open_path("overview"),
            }
        )

    confidence = _safe_get_number(forecast, ("confidence", "confidence_score"))
    if forecast.get("enabled") and confidence is not None and confidence < 0.6:
        cards.append(
            {
                "id": "overview_forecast_confidence",
                "module": "overview",
                "title": "Forecast Stability Is Weak",
                "narrative": "Forecast confidence is low in this period; use directional planning and monitor weekly.",
                "severity": "medium",
                "priority": 66,
                "confidence": 0.72,
                "evidence": {"forecast_confidence": confidence},
                "open_path": _module_open_path("overview"),
            }
        )
    return cards


def _module_signal_cards(ctx: ToolContext, module: str) -> List[Dict[str, Any]]:
    token = str(module or "").strip().lower()
    access = _module_access(ctx)
    cards: List[Dict[str, Any]] = []

    if token in {"overview", "assistant"}:
        if not access.get("overview"):
            return cards
        return _overview_signal_cards(ctx)

    if token == "customers" and access.get("customers"):
        payload = _module_bundle(ctx, "customers", {})
        kpis = payload.get("kpis") if isinstance(payload.get("kpis"), Mapping) else {}
        at_risk = int(_to_float(kpis.get("at_risk_90"), 0))
        top_share = _num_or_none(((kpis.get("top_customer") or {}).get("share") if isinstance(kpis.get("top_customer"), Mapping) else None))
        if at_risk > 0:
            cards.append(
                {
                    "id": "customers_at_risk",
                    "module": "customers",
                    "title": "Customer Churn Risk Rising",
                    "narrative": f"{at_risk} customers are currently flagged as at-risk.",
                    "severity": "high" if at_risk >= 10 else "medium",
                    "priority": 83 if at_risk >= 10 else 71,
                    "confidence": 0.81,
                    "evidence": {"at_risk_90": at_risk},
                    "open_path": _module_open_path("customers"),
                }
            )
        if top_share is not None and top_share >= 0.25:
            cards.append(
                {
                    "id": "customers_concentration",
                    "module": "customers",
                    "title": "Top-Account Dependency",
                    "narrative": f"Top customer share is {top_share:.1%}; concentration risk should be reviewed.",
                    "severity": "high" if top_share >= 0.35 else "medium",
                    "priority": 78,
                    "confidence": 0.79,
                    "evidence": {"top_customer_share": top_share},
                    "open_path": _module_open_path("customers"),
                }
            )
        return cards

    if token == "products" and access.get("products"):
        payload = _module_bundle(ctx, "products", {})
        guardrails = payload.get("pricing_guardrails") if isinstance(payload.get("pricing_guardrails"), Mapping) else {}
        outside = int(_to_float(guardrails.get("outside_count"), 0))
        low_outlier = int(_to_float(guardrails.get("low_outlier_count"), 0))
        if outside > 0:
            cards.append(
                {
                    "id": "products_guardrail_outliers",
                    "module": "products",
                    "title": "SKU Guardrail Exceptions",
                    "narrative": f"{outside} products are outside pricing guardrails under current filters.",
                    "severity": "high" if outside >= 10 else "medium",
                    "priority": 80,
                    "confidence": 0.78,
                    "evidence": {"outside_count": outside, "low_outlier_count": low_outlier},
                    "open_path": _module_open_path("products"),
                }
            )
        ai_signals = list(payload.get("ai_signals") or [])
        if ai_signals:
            cards.append(
                {
                    "id": "products_ai_signals",
                    "module": "products",
                    "title": "Product Risk Signals Active",
                    "narrative": f"{len(ai_signals)} product risk signals were flagged for review.",
                    "severity": "medium",
                    "priority": 70,
                    "confidence": 0.73,
                    "evidence": {"signal_count": len(ai_signals)},
                    "open_path": _module_open_path("products"),
                }
            )
        return cards

    if token == "regions" and access.get("regions"):
        payload = _module_bundle(ctx, "regions", {})
        risk_summary = ((payload.get("risk") or {}).get("summary") or {}) if isinstance(payload.get("risk"), Mapping) else {}
        high_risk_regions = int(_to_float(risk_summary.get("high_risk_regions"), 0))
        if high_risk_regions > 0:
            cards.append(
                {
                    "id": "regions_high_risk",
                    "module": "regions",
                    "title": "Regional Risk Cluster",
                    "narrative": f"{high_risk_regions} regions are currently high-risk and need focused review.",
                    "severity": "high" if high_risk_regions >= 3 else "medium",
                    "priority": 77,
                    "confidence": 0.76,
                    "evidence": {"high_risk_regions": high_risk_regions},
                    "open_path": _module_open_path("regions"),
                }
            )
        return cards

    if token == "suppliers" and access.get("suppliers"):
        payload = _module_bundle(ctx, "suppliers", {})
        kpis = payload.get("kpis") if isinstance(payload.get("kpis"), Mapping) else {}
        at_risk = int(_to_float(kpis.get("at_risk_suppliers"), 0))
        revenue_at_risk = _num_or_none(kpis.get("revenue_at_risk"))
        if at_risk > 0:
            cards.append(
                {
                    "id": "suppliers_at_risk",
                    "module": "suppliers",
                    "title": "Supplier Risk Exposure",
                    "narrative": f"{at_risk} suppliers are flagged at-risk in your visible scope.",
                    "severity": "high" if at_risk >= 5 else "medium",
                    "priority": 75,
                    "confidence": 0.8,
                    "evidence": {"at_risk_suppliers": at_risk, "revenue_at_risk": revenue_at_risk},
                    "open_path": _module_open_path("suppliers"),
                }
            )
        return cards

    if token in {"salesreps", "sales_rep", "sales_reps"} and access.get("salesreps"):
        payload = _module_bundle(ctx, "salesreps", {})
        risk_flags = list(payload.get("risk_flags") or [])
        high_count = sum(1 for row in risk_flags if str((row or {}).get("severity") or "").strip().lower() == "high")
        if high_count > 0:
            cards.append(
                {
                    "id": "salesreps_high_flags",
                    "module": "salesreps",
                    "title": "Rep Portfolio Risk Flags",
                    "narrative": f"{high_count} high-severity sales-rep portfolio risk flags are active.",
                    "severity": "high" if high_count >= 4 else "medium",
                    "priority": 73,
                    "confidence": 0.75,
                    "evidence": {"high_severity_flags": high_count},
                    "open_path": _module_open_path("salesreps"),
                }
            )
        return cards

    if token == "returns" and access.get("returns"):
        pending = returns_service.list_rmas(
            statuses=[returns_service.STATUS_PENDING, returns_service.STATUS_NEEDS_REVIEW],
            actor_user=ctx.user,
        )
        if pending:
            cards.append(
                {
                    "id": "returns_pending_queue",
                    "module": "returns",
                    "title": "Returns Queue Requires Attention",
                    "narrative": f"{len(pending)} returns are pending review/approval.",
                    "severity": "high" if len(pending) >= 15 else "medium",
                    "priority": 72,
                    "confidence": 0.86,
                    "evidence": {"pending_count": len(pending)},
                    "open_path": _module_open_path("returns"),
                }
            )
        return cards

    return cards


def get_proactive_insights(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = str(args.get("module") or ctx.page or "overview").strip().lower()
    include_cross = bool(args.get("cross_module") or module in {"all", "cross_module", "cross-module"})
    modules = [module]
    access = _module_access(ctx)
    if include_cross and module == "overview" and not access.get("overview"):
        return _module_forbidden(ctx, "overview", "Proactive Insights")
    if not include_cross and module in access and not access.get(module):
        return _module_forbidden(ctx, module, "Proactive Insights")
    if include_cross:
        modules = [m for m in ("overview", "customers", "products", "regions", "suppliers", "salesreps", "returns") if access.get(m)]
        if not modules:
            return _forbidden(ctx, "Proactive Insights", "No accessible modules are available for proactive insights.", module=module)

    cards: List[Dict[str, Any]] = []
    suppressed = 0
    for token in modules:
        for card in _module_signal_cards(ctx, token):
            if _to_float(card.get("confidence"), 0.0) < 0.55:
                suppressed += 1
                continue
            cards.append(card)
    cards.sort(key=lambda row: (_to_float(row.get("priority"), 0.0), _to_float(row.get("confidence"), 0.0)), reverse=True)
    selected = cards[:10]
    next_actions = [f"Investigate: {row.get('title')}" for row in selected[:4] if row.get("title")]
    return _tool_response(
        status="ok" if selected else "empty",
        title="Proactive Insights",
        data={
            "module": module,
            "cross_module": include_cross,
            "cards": selected,
            "suppressed_low_confidence": suppressed,
            "summary": selected[0].get("narrative") if selected else "No high-confidence proactive insights in this scope/window.",
        },
        ctx=ctx,
        notes=[
            "Insights are generated from deterministic service signals and are suppressed when confidence is weak.",
            "All insight cards are scoped by permissions, filters, and sensitive-data visibility.",
        ],
        citations=[
            "overview_v2.build_overview_context",
            "customers_bundle.build_customers_bundle",
            "products_bundle.build_products_bundle",
            "regions_bundle.build_regions_bundle",
            "suppliers_bundle.build_suppliers_bundle",
            "salesreps_bundle.build_salesreps_bundle",
            "returns_service.list_rmas",
        ],
        next_actions=next_actions,
        module="cross_module" if include_cross else module,
    )


def get_anomaly_narratives(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = str(args.get("module") or ctx.page or "overview").strip().lower()
    signals = _module_signal_cards(ctx, module if module not in {"all", "cross_module", "cross-module"} else "overview")
    comparisons = compare_periods(ctx, {"module": module if module not in {"all", "cross_module", "cross-module"} else "overview"})
    comparison_data = comparisons.get("data") if isinstance(comparisons.get("data"), Mapping) else {}
    comparison_summary = comparison_data.get("comparison") if isinstance(comparison_data.get("comparison"), Mapping) else {}

    narratives: List[Dict[str, Any]] = []
    for card in signals[:8]:
        severity = str(card.get("severity") or "medium").strip().lower()
        narratives.append(
            {
                "title": card.get("title"),
                "severity": severity,
                "priority": card.get("priority"),
                "narrative": card.get("narrative"),
                "why_unusual": "Signal exceeds rule threshold for materiality or risk concentration.",
                "evidence": card.get("evidence") or {},
                "confidence": card.get("confidence"),
            }
        )

    if isinstance(comparison_summary, Mapping) and comparison_summary and len(narratives) < 10:
        narratives.append(
            {
                "title": "Period Shift Context",
                "severity": "medium",
                "priority": 64,
                "narrative": "Current movement is interpreted alongside prior-period comparison metrics.",
                "why_unusual": "Comparison deltas indicate a material directional shift.",
                "evidence": dict(comparison_summary),
                "confidence": 0.68,
            }
        )

    narratives.sort(key=lambda row: (_to_float(row.get("priority"), 0.0), _to_float(row.get("confidence"), 0.0)), reverse=True)
    return _tool_response(
        status="ok" if narratives else "empty",
        title="Anomaly And Risk Narratives",
        data={
            "module": module,
            "narratives": narratives[:10],
            "explainability": "Rule-backed thresholds with explicit evidence fields are used.",
        },
        ctx=ctx,
        notes=["Narratives are deterministic and evidence-backed; no black-box anomaly score is used."],
        citations=["assistant.phase4.anomaly_rules"],
        next_actions=[f"Investigate: {row.get('title')}" for row in narratives[:4] if row.get("title")],
        module=module,
    )


def get_priority_risks(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = str(args.get("module") or "all").strip().lower()
    modules: List[str]
    if module in {"all", "cross_module", "cross-module"}:
        access = _module_access(ctx)
        modules = [m for m in ("overview", "customers", "products", "regions", "suppliers", "salesreps", "returns") if access.get(m)]
    else:
        modules = [module]

    risks: List[Dict[str, Any]] = []
    for token in modules:
        for card in _module_signal_cards(ctx, token):
            score = int(max(1, min(99, round(_to_float(card.get("priority"), 0.0) * (_to_float(card.get("confidence"), 0.0) or 0.65) / 100.0 * 120.0))))
            risks.append(
                {
                    "module": card.get("module"),
                    "title": card.get("title"),
                    "detail": card.get("narrative"),
                    "severity": card.get("severity"),
                    "priority": int(_to_float(card.get("priority"), 0.0)),
                    "risk_score": score,
                    "open_path": card.get("open_path"),
                    "evidence": card.get("evidence") or {},
                }
            )

    risks.sort(
        key=lambda row: (
            _to_float(row.get("risk_score"), 0.0),
            _to_float(row.get("priority"), 0.0),
        ),
        reverse=True,
    )
    top = risks[:12]
    return _tool_response(
        status="ok" if top else "empty",
        title="Priority Risks",
        data={"module": module, "risks": top},
        ctx=ctx,
        notes=["Risk ranking prioritizes severity, business priority, and confidence while staying scope-safe."],
        citations=["assistant.phase4.priority_risks"],
        next_actions=[f"Open {row.get('module')} for {row.get('title')}" for row in top[:5] if row.get("module") and row.get("title")],
        module="cross_module" if module in {"all", "cross_module", "cross-module"} else module,
    )


def get_guided_investigation_paths(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = str(args.get("module") or ctx.page or "overview").strip().lower()
    access = _module_access(ctx)
    carry_filters = _filters_source(ctx)
    paths: List[Dict[str, Any]] = []

    if module in {"overview", "assistant"}:
        if access.get("customers"):
            paths.append(
                {
                    "id": "ov_to_customers",
                    "title": "Inspect Customer Movers",
                    "why": "Customer changes often explain aggregate performance movement.",
                    "question": "Which customers drove the main movement?",
                    "module": "customers",
                    "open_path": _module_open_path("customers"),
                    "carry_filters": dict(carry_filters),
                }
            )
        if access.get("products"):
            paths.append(
                {
                    "id": "ov_to_products",
                    "title": "Review Margin-Risk SKUs",
                    "why": "Product-level mix and pricing often drive margin deterioration.",
                    "question": "Which products combine high revenue with margin risk?",
                    "module": "products",
                    "open_path": _module_open_path("products"),
                    "carry_filters": dict(carry_filters),
                }
            )
        if access.get("regions"):
            paths.append(
                {
                    "id": "ov_to_regions",
                    "title": "Check Regional Deterioration",
                    "why": "Regional concentration can hide localized decline.",
                    "question": "Which regions are weakening and why?",
                    "module": "regions",
                    "open_path": _module_open_path("regions"),
                    "carry_filters": dict(carry_filters),
                }
            )

    if module == "products" and access.get("customers"):
        paths.append(
            {
                "id": "products_to_customers",
                "title": "Trace Product To Customer Exposure",
                "why": "Customer dependency can explain product volatility.",
                "question": "Which customers are driving this product risk?",
                "module": "customers",
                "open_path": _module_open_path("customers"),
                "carry_filters": dict(carry_filters),
            }
        )
    if module == "customers" and access.get("products"):
        paths.append(
            {
                "id": "customers_to_products",
                "title": "Trace Customer To SKU Mix",
                "why": "SKU mix changes can drive customer-level margin pressure.",
                "question": "Which SKUs are driving this customer movement?",
                "module": "products",
                "open_path": _module_open_path("products"),
                "carry_filters": dict(carry_filters),
            }
        )
    if module == "returns" and access.get("returns"):
        paths.append(
            {
                "id": "returns_pending",
                "title": "Open Pending Approvals",
                "why": "Pending approvals can create operational and credit backlog.",
                "question": "Which approvals should be processed first?",
                "module": "returns",
                "open_path": "/returns/approvals",
                "carry_filters": dict(carry_filters),
            }
        )

    if access.get("overview"):
        paths.append(
            {
                "id": "cross_summary",
                "title": "Generate Cross-Module Summary",
                "why": "Leadership review needs wins, declines, risks, and actions in one view.",
                "question": "Summarize top wins, declines, risks, and actions.",
                "module": "overview",
                "open_path": _module_open_path("overview"),
                "carry_filters": dict(carry_filters),
            }
        )

    # Keep only permission-valid and non-broken paths.
    valid: List[Dict[str, Any]] = []
    for row in paths:
        target_module = str(row.get("module") or "").strip().lower()
        if target_module and target_module in access and not access.get(target_module):
            continue
        open_path = str(row.get("open_path") or "").strip()
        if not open_path.startswith("/"):
            continue
        valid.append(row)

    return _tool_response(
        status="ok" if valid else "empty",
        title="Guided Investigation Paths",
        data={"module": module, "paths": valid[:10]},
        ctx=ctx,
        notes=["Investigation paths are permission-checked and include carry-filter payloads when available."],
        citations=["assistant.phase4.guided_paths"],
        next_actions=[row.get("title") for row in valid[:5] if row.get("title")],
        module=module,
    )


def get_confidence_or_trust_summary(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    del args
    trust = dict(ctx.sensitive_flags or {})
    caveats: List[str] = []
    score = 100.0

    if not trust.get("cost"):
        caveats.append("Cost visibility is restricted.")
        score -= 12
    if not trust.get("profit"):
        caveats.append("Profit visibility is restricted.")
        score -= 10
    if not trust.get("margin"):
        caveats.append("Margin visibility is restricted.")
        score -= 10

    cost_coverage = None
    pack_coverage = None
    if rbac.can_view_page("overview", ctx.user):
        health_tool = get_data_health(ctx, {})
        health = health_tool.get("data") if isinstance(health_tool.get("data"), Mapping) else {}
        cost_coverage = _safe_get_number(health, ("cost_coverage_pct", "cost_coverage"))
        pack_coverage = _safe_get_number(health, ("pack_coverage_pct", "pack_coverage"))
        if cost_coverage is not None and cost_coverage < 95:
            caveats.append(f"Cost coverage at {_pct(cost_coverage)} reduces confidence in profit conclusions.")
            score -= 14 if cost_coverage < 90 else 7
        if pack_coverage is not None and pack_coverage < 95:
            caveats.append(f"Pack coverage at {_pct(pack_coverage)} may affect weight-based metrics.")
            score -= 8 if pack_coverage < 90 else 4

    score = max(20.0, min(100.0, score))
    level = "high" if score >= 85 else ("medium" if score >= 65 else "low")
    return _tool_response(
        status="ok",
        title="Confidence And Trust Summary",
        data={
            "confidence_score": round(score, 1),
            "confidence_level": level,
            "trust_flags": trust,
            "cost_coverage_pct": cost_coverage,
            "pack_coverage_pct": pack_coverage,
            "caveats": caveats,
        },
        ctx=ctx,
        notes=["Confidence score is deterministic and combines sensitivity restrictions with data coverage caveats."],
        citations=["assistant.phase4.confidence_summary"],
        module="trust",
    )


def get_cross_module_risk_summary(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    del args
    risks_tool = get_priority_risks(ctx, {"module": "all"})
    rows = list(((risks_tool.get("data") or {}).get("risks") or []) if isinstance(risks_tool.get("data"), Mapping) else [])
    by_module: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        module = str(row.get("module") or "unknown").strip().lower()
        existing = by_module.get(module)
        if existing is None or _to_float(row.get("risk_score"), 0.0) > _to_float(existing.get("risk_score"), 0.0):
            by_module[module] = dict(row)
    ranked = sorted(by_module.values(), key=lambda row: _to_float(row.get("risk_score"), 0.0), reverse=True)
    summary_line = ", ".join(f"{row.get('module')}: {row.get('title')}" for row in ranked[:4] if row.get("module") and row.get("title"))
    return _tool_response(
        status="ok" if ranked else "empty",
        title="Cross-Module Risk Summary",
        data={
            "top_module_risks": ranked[:8],
            "summary": summary_line or "No cross-module risks exceeded configured thresholds.",
        },
        ctx=ctx,
        notes=["Cross-module view keeps only the highest-priority risk per module for scanability."],
        citations=["assistant.phase4.cross_module_risks"],
        module="cross_module",
    )


def get_risk_trend_baseline(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = _module_token(str(args.get("module") or ctx.page or "overview"))
    metric = str(args.get("metric") or "margin_pct").strip().lower()

    series: List[float] = []
    if module in {"overview", "assistant"}:
        trend_tool = get_trend_series(ctx, {"metric": metric, "grain": "monthly"})
        trend_data = trend_tool.get("data") if isinstance(trend_tool.get("data"), Mapping) else {}
        trend_payload = {
            "labels": list(trend_data.get("labels") or []),
            str(trend_data.get("metric") or metric): list(trend_data.get("values") or []),
        }
        series = _series_values_from_trend(trend_payload)
    elif module == "customers":
        trend_tool = get_customer_trend(ctx, args)
        trend_data = trend_tool.get("data") if isinstance(trend_tool.get("data"), Mapping) else {}
        series = _series_values_from_trend(trend_data.get("trend") if isinstance(trend_data.get("trend"), Mapping) else trend_data)
    elif module == "products":
        trend_tool = get_product_trend(ctx, args)
        trend_data = trend_tool.get("data") if isinstance(trend_tool.get("data"), Mapping) else {}
        series = _series_values_from_trend(trend_data.get("trend") if isinstance(trend_data.get("trend"), Mapping) else trend_data)
    elif module == "regions":
        trend_tool = get_region_trend(ctx, args)
        trend_data = trend_tool.get("data") if isinstance(trend_tool.get("data"), Mapping) else {}
        series = _series_values_from_trend(trend_data.get("trend") if isinstance(trend_data.get("trend"), Mapping) else trend_data)
    elif module == "suppliers":
        trend_tool = get_supplier_trend(ctx, args)
        trend_data = trend_tool.get("data") if isinstance(trend_tool.get("data"), Mapping) else {}
        series = _series_values_from_trend(trend_data.get("trend") if isinstance(trend_data.get("trend"), Mapping) else trend_data)
    elif module == "salesreps":
        trend_tool = get_sales_rep_trend(ctx, args)
        trend_data = trend_tool.get("data") if isinstance(trend_tool.get("data"), Mapping) else {}
        series = _series_values_from_trend(trend_data.get("trend") if isinstance(trend_data.get("trend"), Mapping) else trend_data)

    if len(series) < 3:
        return _tool_response(
            status="empty",
            title="Risk Trend Baseline",
            data={
                "module": module,
                "metric": metric,
                "observations": len(series),
                "message": "Not enough historical points for statistical baseline.",
            },
            ctx=ctx,
            notes=["Need at least 3 points to compute trend baseline z-score."],
            citations=["assistant.phase5.risk_baseline"],
            module=module,
        )

    history = series[:-1]
    current = float(series[-1])
    mean = float(sum(history) / len(history))
    variance = float(sum((value - mean) ** 2 for value in history) / max(1, len(history) - 1))
    std_dev = float(variance ** 0.5)
    z_score = float((current - mean) / std_dev) if std_dev > 1e-9 else 0.0
    severity = "high" if abs(z_score) >= 2.25 else "medium" if abs(z_score) >= 1.5 else "low"
    direction = "deteriorating" if current < mean else "improving"

    risk_tool = get_priority_risks(ctx, {"module": module})
    risk_rows = list(((risk_tool.get("data") or {}).get("risks") or []) if isinstance(risk_tool.get("data"), Mapping) else [])
    top_risk = risk_rows[0] if risk_rows else {}
    narrative = (
        f"{module.title()} {metric} is {direction} vs baseline "
        f"(current={current:.2f}, baseline={mean:.2f}, z={z_score:.2f})."
    )
    if isinstance(top_risk, Mapping) and top_risk.get("title"):
        narrative = f"{narrative} Top linked risk: {top_risk.get('title')}."

    return _tool_response(
        status="ok",
        title="Risk Trend Baseline",
        data={
            "module": module,
            "metric": metric,
            "current_value": current,
            "baseline_mean": mean,
            "baseline_std": std_dev,
            "z_score": z_score,
            "severity": severity,
            "direction": direction,
            "observations": len(series),
            "narrative": narrative,
            "top_risk": dict(top_risk) if isinstance(top_risk, Mapping) else {},
        },
        ctx=ctx,
        notes=[
            "Baseline uses historical points excluding the latest value.",
            "Use severity with trust/coverage context before escalation.",
        ],
        citations=["assistant.phase5.risk_baseline"],
        module=module,
    )


def get_causal_attribution_graph(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = _module_token(str(args.get("module") or ctx.page or "overview"))
    nodes: List[Dict[str, Any]] = [
        {"id": "outcome", "label": f"{module.title()} performance", "type": "outcome", "weight": 1.0}
    ]
    edges: List[Dict[str, Any]] = []

    change_tool = get_entity_change_explanation(ctx, {"module": module})
    change_data = change_tool.get("data") if isinstance(change_tool.get("data"), Mapping) else {}
    driver_rows = list(change_data.get("drivers") or []) if isinstance(change_data, Mapping) else []
    for idx, row in enumerate(driver_rows[:5], start=1):
        if not isinstance(row, Mapping):
            continue
        driver_label = str(row.get("driver") or row.get("label") or row.get("name") or f"Driver {idx}")
        driver_id = f"drv_{idx}"
        weight = abs(_to_float(row.get("impact_pct"), _to_float(row.get("impact"), 0.0)))
        nodes.append({"id": driver_id, "label": driver_label, "type": "driver", "weight": weight or 1.0})
        edges.append(
            {
                "source": driver_id,
                "target": "outcome",
                "relation": "influences",
                "weight": weight or 1.0,
                "direction": "negative" if _to_float(row.get("impact_pct"), 0.0) < 0 else "positive",
            }
        )

    dimension = "customer"
    if module == "products":
        dimension = "product"
    elif module == "regions":
        dimension = "region"
    elif module == "suppliers":
        dimension = "supplier"
    elif module == "salesreps":
        dimension = "salesrep"
    elif module == "returns":
        dimension = "customer"

    movers_tool = get_top_movers(ctx, {"dimension": dimension, "limit": 5})
    movers_data = movers_tool.get("data") if isinstance(movers_tool.get("data"), Mapping) else {}
    movers = list(movers_data.get("movers") or [])
    for idx, row in enumerate(movers[:5], start=1):
        if not isinstance(row, Mapping):
            continue
        label = str(row.get("label") or row.get("name") or row.get("entity") or f"Mover {idx}")
        node_id = f"mvr_{idx}"
        impact = _to_float(row.get("impact"), _to_float(row.get("delta_value"), 0.0))
        nodes.append({"id": node_id, "label": label, "type": "entity", "weight": abs(impact) or 1.0})
        parent = "outcome"
        if driver_rows:
            parent = f"drv_{(idx - 1) % max(1, min(len(driver_rows), 5)) + 1}"
        edges.append(
            {
                "source": node_id,
                "target": parent,
                "relation": "contributes_to",
                "weight": abs(impact) or 1.0,
                "direction": "negative" if impact < 0 else "positive",
            }
        )

    risk_tool = get_priority_risks(ctx, {"module": module})
    risks = list(((risk_tool.get("data") or {}).get("risks") or []) if isinstance(risk_tool.get("data"), Mapping) else [])
    for idx, row in enumerate(risks[:3], start=1):
        if not isinstance(row, Mapping):
            continue
        node_id = f"rsk_{idx}"
        score = _to_float(row.get("risk_score"), 0.0)
        nodes.append({"id": node_id, "label": str(row.get("title") or f"Risk {idx}"), "type": "risk", "weight": score or 1.0})
        edges.append(
            {
                "source": node_id,
                "target": "outcome",
                "relation": "risk_signal",
                "weight": score or 1.0,
                "direction": "negative",
            }
        )

    if not edges:
        return _tool_response(
            status="empty",
            title="Causal Attribution Graph",
            data={"module": module, "nodes": nodes, "edges": []},
            ctx=ctx,
            notes=["No high-confidence causal signals were available for graph assembly in this scope."],
            citations=["assistant.phase5.causal_graph"],
            module=module,
        )

    return _tool_response(
        status="ok",
        title="Causal Attribution Graph",
        data={
            "module": module,
            "nodes": nodes,
            "edges": edges,
            "summary": f"Graph assembled with {len(nodes)} nodes and {len(edges)} relationships.",
        },
        ctx=ctx,
        notes=["Graph is heuristic and tool-grounded; treat as directional attribution, not deterministic causality."],
        citations=["assistant.phase5.causal_graph", "assistant.entity_change_explanation", "assistant.top_movers"],
        module=module,
    )


def get_next_best_questions(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = str(args.get("module") or ctx.page or "overview").strip().lower()
    base = get_recommended_followups(ctx, args)
    suggestions = list(((base.get("data") or {}).get("suggestions") or []) if isinstance(base.get("data"), Mapping) else [])
    proactive = get_proactive_insights(ctx, {"module": module})
    cards = list(((proactive.get("data") or {}).get("cards") or []) if isinstance(proactive.get("data"), Mapping) else [])
    for card in cards[:4]:
        if not isinstance(card, Mapping):
            continue
        title = str(card.get("title") or "").strip().lower()
        if "margin" in title:
            suggestions.append("Which entities have high revenue and margin risk together?")
        elif "concentration" in title or "dependency" in title:
            suggestions.append("Where is concentration risk increasing and what should we do first?")
        elif "returns" in title:
            suggestions.append("Which returns approvals should be prioritized this week?")
        else:
            suggestions.append("What is the most likely driver behind this signal?")
    deduped: List[str] = []
    for item in suggestions:
        token = str(item).strip()
        if token and token not in deduped:
            deduped.append(token)
    return _tool_response(
        status="ok",
        title="Next Best Questions",
        data={"module": module, "questions": deduped[:12]},
        ctx=ctx,
        citations=["assistant.phase4.next_best_questions"],
        module=module,
    )


def get_executive_digest(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = str(args.get("module") or ctx.page or "overview").strip().lower()
    if module == "assistant":
        module = "overview"
    length = str(args.get("length") or "medium").strip().lower()
    if length not in {"short", "medium", "long"}:
        length = "medium"
    audience = str(args.get("audience") or "leadership").strip().lower()

    if module == "overview" and not rbac.can_view_page("overview", ctx.user):
        return _module_forbidden(ctx, "overview", "Executive Digest")
    if module == "customers" and not rbac.can_view_page("customers", ctx.user):
        return _module_forbidden(ctx, "customers", "Executive Digest")
    if module == "products" and not rbac.can_view_page("products", ctx.user):
        return _module_forbidden(ctx, "products", "Executive Digest")
    if module == "regions" and not rbac.can_view_page("regions", ctx.user):
        return _module_forbidden(ctx, "regions", "Executive Digest")
    if module == "suppliers" and not rbac.can_view_page("suppliers", ctx.user):
        return _module_forbidden(ctx, "suppliers", "Executive Digest")
    if module in {"salesreps", "sales_rep", "sales_reps"} and not rbac.can_view_page("salesreps", ctx.user):
        return _module_forbidden(ctx, "salesreps", "Executive Digest")
    if module == "returns" and not _module_access(ctx).get("returns", False):
        return _module_forbidden(ctx, "returns", "Executive Digest")

    executive = get_executive_summary(ctx, {"module": module})
    executive_data = executive.get("data") if isinstance(executive.get("data"), Mapping) else {}
    risk_tool = get_priority_risks(ctx, {"module": module})
    risk_rows = list(((risk_tool.get("data") or {}).get("risks") or []) if isinstance(risk_tool.get("data"), Mapping) else [])
    actions_tool = get_priority_actions(ctx, {"module": module})
    actions = list(((actions_tool.get("data") or {}).get("actions") or []) if isinstance(actions_tool.get("data"), Mapping) else [])
    trust_tool = get_confidence_or_trust_summary(ctx, {})
    trust_data = trust_tool.get("data") if isinstance(trust_tool.get("data"), Mapping) else {}
    proactive = get_proactive_insights(ctx, {"module": module})
    cards = list(((proactive.get("data") or {}).get("cards") or []) if isinstance(proactive.get("data"), Mapping) else [])
    guided = get_guided_investigation_paths(ctx, {"module": module})
    paths = list(((guided.get("data") or {}).get("paths") or []) if isinstance(guided.get("data"), Mapping) else [])

    lead_signal = cards[0] if cards else None
    summary_text = "No material high-confidence change was detected in the current scoped window."
    if isinstance(lead_signal, Mapping):
        summary_text = str(lead_signal.get("narrative") or summary_text)

    wins = [executive_data.get("biggest_win")] if executive_data.get("biggest_win") else []
    declines = [executive_data.get("biggest_decline")] if executive_data.get("biggest_decline") else []
    key_risks = risk_rows[: (2 if length == "short" else 4 if length == "medium" else 6)]
    rec_actions = actions[: (3 if length == "short" else 5 if length == "medium" else 8)]
    next_paths = [
        {
            "title": row.get("title"),
            "module": row.get("module"),
            "open_path": row.get("open_path"),
            "question": row.get("question"),
        }
        for row in paths[: (2 if length == "short" else 4 if length == "medium" else 6)]
        if isinstance(row, Mapping)
    ]
    caveats = list(trust_data.get("caveats") or [])
    if not caveats:
        caveats = ["No major trust caveats were detected for visible fields."]

    digest = {
        "audience": audience,
        "length": length,
        "module": module,
        "executive_summary": summary_text,
        "key_wins": wins,
        "key_declines": declines,
        "major_risks": key_risks,
        "data_caveats": caveats,
        "recommended_actions": rec_actions,
        "suggested_next_investigations": next_paths,
        "spoken_summary": summary_text,
    }
    return _tool_response(
        status="ok",
        title="Executive Digest",
        data=digest,
        ctx=ctx,
        notes=["Digest is generated from scoped tool outputs and does not include unrestricted data."],
        citations=[
            "assistant.executive_summary",
            "assistant.phase4.priority_risks",
            "assistant.priority_actions",
            "assistant.phase4.confidence_summary",
        ],
        next_actions=rec_actions[:5],
        module=module,
    )


def get_manager_digest(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    merged = dict(args or {})
    merged.setdefault("audience", "manager")
    merged.setdefault("length", "medium")
    source = get_executive_digest(ctx, merged)
    payload = source.get("data") if isinstance(source.get("data"), Mapping) else {}
    manager_view = dict(payload)
    manager_view["audience"] = "manager"
    manager_view["manager_focus"] = "Prioritize issues that combine impact, urgency, and execution ownership."
    return _tool_response(
        status=source.get("status", "empty"),
        title="Manager Digest",
        data=manager_view,
        ctx=ctx,
        notes=source.get("notes") or [],
        citations=source.get("citations") or ["assistant.phase4.executive_digest"],
        next_actions=source.get("next_actions") or [],
        module=source.get("module"),
    )


def get_leadership_summary(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    merged = dict(args or {})
    merged.setdefault("audience", "leadership")
    merged.setdefault("length", "short")
    source = get_executive_digest(ctx, merged)
    payload = source.get("data") if isinstance(source.get("data"), Mapping) else {}
    leadership = dict(payload)
    leadership["audience"] = "leadership"
    return _tool_response(
        status=source.get("status", "empty"),
        title="Leadership Summary",
        data=leadership,
        ctx=ctx,
        notes=source.get("notes") or [],
        citations=source.get("citations") or ["assistant.phase4.executive_digest"],
        next_actions=source.get("next_actions") or [],
        module=source.get("module"),
    )


def get_investigation_checklist(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = str(args.get("module") or ctx.page or "overview").strip().lower()
    guided = get_guided_investigation_paths(ctx, {"module": module})
    paths = list(((guided.get("data") or {}).get("paths") or []) if isinstance(guided.get("data"), Mapping) else [])
    risk_tool = get_priority_risks(ctx, {"module": module if module != "assistant" else "overview"})
    risks = list(((risk_tool.get("data") or {}).get("risks") or []) if isinstance(risk_tool.get("data"), Mapping) else [])
    checklist: List[Dict[str, Any]] = []
    step_num = 1
    for row in paths[:6]:
        if not isinstance(row, Mapping):
            continue
        checklist.append(
            {
                "step": step_num,
                "task": row.get("title"),
                "question": row.get("question"),
                "module": row.get("module"),
                "open_path": row.get("open_path"),
                "owner_hint": "Manager",
                "status": "todo",
            }
        )
        step_num += 1
    for risk in risks[:4]:
        if not isinstance(risk, Mapping):
            continue
        checklist.append(
            {
                "step": step_num,
                "task": f"Validate risk: {risk.get('title')}",
                "question": risk.get("detail"),
                "module": risk.get("module"),
                "open_path": risk.get("open_path"),
                "owner_hint": "Analyst",
                "status": "todo",
            }
        )
        step_num += 1
    return _tool_response(
        status="ok" if checklist else "empty",
        title="Investigation Checklist",
        data={
            "module": module,
            "checklist": checklist[:12],
            "review_required": True,
            "non_destructive": True,
        },
        ctx=ctx,
        notes=["Checklist output is advisory and requires human review before operational use."],
        citations=["assistant.phase4.investigation_checklist"],
        next_actions=[item.get("task") for item in checklist[:5] if item.get("task")],
        module=module,
    )


def get_entity_change_explanation(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = str(args.get("module") or ctx.page or "overview").strip().lower()
    tool_map: Dict[str, Callable[[ToolContext, Dict[str, Any] | None], Dict[str, Any]]] = {
        "customers": get_customer_detail,
        "products": get_product_detail,
        "regions": get_region_detail,
        "suppliers": get_supplier_detail,
        "salesreps": get_sales_rep_detail,
    }
    source = compare_periods(ctx, {"module": module})
    detail = None
    if module in tool_map:
        detail = tool_map[module](ctx, args)
    comparison = source.get("data") if isinstance(source.get("data"), Mapping) else {}
    detail_data = detail.get("data") if isinstance(detail, Mapping) and isinstance(detail.get("data"), Mapping) else {}
    explanation = {
        "module": module,
        "summary": "Entity movement is explained using period comparison and available drilldown context.",
        "comparison": comparison,
        "entity_detail": {
            "kpis": detail_data.get("kpis") if isinstance(detail_data.get("kpis"), Mapping) else {},
            "top_rows": _top_rows(((detail_data.get("table") or {}).get("rows") if isinstance(detail_data.get("table"), Mapping) else []), limit=8),
        },
        "confidence": "directional" if not ctx.sensitive_flags.get("cost") else "standard",
    }
    return _tool_response(
        status="ok" if comparison or detail_data else "empty",
        title="Entity Change Explanation",
        data=explanation,
        ctx=ctx,
        notes=["Explanation combines deterministic period deltas with scoped drilldown rows where available."],
        citations=["assistant.compare_periods", "bundle_service.drilldown"],
        module=module,
    )


def get_workflow_assist_note(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = str(args.get("module") or ctx.page or "overview").strip().lower()
    note_type = str(args.get("note_type") or "summary").strip().lower()
    if note_type not in {"summary", "checklist", "risk_explanation", "next_steps"}:
        note_type = "summary"

    digest = get_manager_digest(ctx, {"module": module, "length": "short"})
    digest_data = digest.get("data") if isinstance(digest.get("data"), Mapping) else {}
    checklist_tool = get_investigation_checklist(ctx, {"module": module})
    checklist = list(((checklist_tool.get("data") or {}).get("checklist") or []) if isinstance(checklist_tool.get("data"), Mapping) else [])

    lines: List[str] = []
    lines.append(f"Module: {module}")
    lines.append(f"Executive Summary: {str(digest_data.get('executive_summary') or 'No summary available.')}")
    if note_type in {"checklist", "next_steps"}:
        for item in checklist[:6]:
            if not isinstance(item, Mapping):
                continue
            lines.append(f"- {item.get('task')}")
    if note_type == "risk_explanation":
        risks = list(digest_data.get("major_risks") or [])
        for risk in risks[:4]:
            if isinstance(risk, Mapping):
                lines.append(f"- Risk: {risk.get('title')} ({risk.get('severity')})")

    return _tool_response(
        status="ok",
        title="Workflow Assist Draft",
        data={
            "module": module,
            "note_type": note_type,
            "review_required": True,
            "non_destructive": True,
            "body_lines": lines,
            "source_digest": digest_data,
        },
        ctx=ctx,
        notes=[
            "This output is a reviewable draft and does not perform autonomous workflow mutation.",
            "All generated content remains permission-scoped and auditable.",
        ],
        citations=["assistant.phase4.workflow_assist"],
        next_actions=["Review and edit draft note before sharing or attaching to workflow."],
        module=module,
    )


def create_digest_schedule(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    module = _module_token(str(args.get("module") or ctx.page or "overview"))
    cadence = str(args.get("cadence") or "weekly").strip().lower()
    if cadence not in {"daily", "weekly", "monthly"}:
        cadence = "weekly"
    audience = str(args.get("audience") or "leadership").strip().lower()
    if audience not in {"leadership", "manager"}:
        audience = "leadership"
    length = str(args.get("length") or "short").strip().lower()
    if length not in {"short", "medium", "long"}:
        length = "short"
    timezone = str(args.get("timezone") or "UTC").strip() or "UTC"
    hour_local = int(args.get("hour_local") or 8)
    hour_local = max(0, min(23, hour_local))

    if module not in {"overview", "customers", "products", "regions", "suppliers", "salesreps", "returns"}:
        return _tool_response(
            status="error",
            title="Digest Schedule Created",
            data={"message": f"Unsupported module '{module}' for digest schedule."},
            ctx=ctx,
            module=module,
        )

    if module != "returns" and not rbac.can_view_page(module if module != "salesreps" else "salesreps", ctx.user):
        return _module_forbidden(ctx, module, "Digest Schedule Created")
    if module == "returns" and not _module_access(ctx).get("returns", False):
        return _module_forbidden(ctx, "returns", "Digest Schedule Created")

    schedule = create_digest_schedule_record(
        getattr(ctx.user, "id", "anon"),
        {
            "module": module,
            "cadence": cadence,
            "audience": audience,
            "length": length,
            "timezone": timezone,
            "hour_local": hour_local,
            "scope": _scope_used(ctx),
            "filters": {
                "start": _window_used(ctx).get("start"),
                "end": _window_used(ctx).get("end"),
            },
        },
    )
    return _tool_response(
        status="ok",
        title="Digest Schedule Created",
        data={"schedule": schedule},
        ctx=ctx,
        notes=[
            "Schedules are permission-scoped and audit-ready.",
            "Delivery is governed and reviewable; no destructive workflow actions are executed.",
        ],
        citations=["assistant.phase5.scheduled_digest"],
        next_actions=["Run scheduled digest now", "List scheduled digests"],
        module=module,
    )


def list_digest_schedules(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    del args
    rows = list_digest_schedule_records(getattr(ctx.user, "id", "anon"))
    visible: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        module = _module_token(str(row.get("module") or "overview"))
        if module == "returns":
            if not _module_access(ctx).get("returns", False):
                continue
        elif not rbac.can_view_page(module if module != "salesreps" else "salesreps", ctx.user):
            continue
        visible.append(dict(row))
    return _tool_response(
        status="ok",
        title="Digest Schedules",
        data={"schedules": visible},
        ctx=ctx,
        citations=["assistant.phase5.scheduled_digest"],
        module="cross_module",
    )


def run_digest_schedule(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    schedule_id = str(args.get("schedule_id") or "").strip()
    rows = list_digest_schedule_records(getattr(ctx.user, "id", "anon"))
    schedule: Dict[str, Any] | None = None
    if schedule_id:
        schedule = get_digest_schedule_record(getattr(ctx.user, "id", "anon"), schedule_id)
    elif rows:
        schedule = dict(rows[0])
        schedule_id = str(schedule.get("schedule_id") or "")

    if not isinstance(schedule, Mapping):
        return _tool_response(
            status="empty",
            title="Scheduled Digest Run",
            data={"message": "No digest schedule found for this user."},
            ctx=ctx,
            module="cross_module",
        )

    module = _module_token(str(schedule.get("module") or "overview"))
    audience = str(schedule.get("audience") or "leadership")
    length = str(schedule.get("length") or "short")
    digest = get_executive_digest(ctx, {"module": module, "audience": audience, "length": length})
    status = str(digest.get("status") or "empty")
    if schedule_id:
        mark_digest_schedule_run(getattr(ctx.user, "id", "anon"), schedule_id, status=status)
        refreshed = get_digest_schedule_record(getattr(ctx.user, "id", "anon"), schedule_id)
        if isinstance(refreshed, Mapping):
            schedule = refreshed

    return _tool_response(
        status="ok" if status == "ok" else status,
        title="Scheduled Digest Run",
        data={
            "schedule": dict(schedule),
            "digest": digest.get("data") if isinstance(digest.get("data"), Mapping) else {},
            "digest_status": status,
        },
        ctx=ctx,
        notes=["Run is explicit and auditable. Digest content remains permission-scoped."],
        citations=["assistant.phase5.scheduled_digest", "assistant.phase4.executive_digest"],
        module=module,
    )


def delete_digest_schedule(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = dict(args or {})
    schedule_id = str(args.get("schedule_id") or "").strip()
    if not schedule_id:
        return _tool_response(
            status="error",
            title="Digest Schedule Deleted",
            data={"message": "schedule_id is required."},
            ctx=ctx,
            module="cross_module",
        )
    removed = delete_digest_schedule_record(getattr(ctx.user, "id", "anon"), schedule_id)
    return _tool_response(
        status="ok" if removed else "empty",
        title="Digest Schedule Deleted",
        data={"schedule_id": schedule_id, "deleted": bool(removed)},
        ctx=ctx,
        citations=["assistant.phase5.scheduled_digest"],
        module="cross_module",
    )


def get_recommended_followups(ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    del args
    module_access = _module_access(ctx)
    page = str(ctx.page or "overview").strip().lower()
    module_prompts: Dict[str, List[str]] = {
        "overview": [
            "Summarize this page for leadership.",
            "Why is revenue changing?",
            "Show historical business trend for this page.",
            "Export overview leadership workbook.",
            "Schedule a weekly leadership digest.",
            "What are the biggest risks?",
            "Which entities drove change?",
            "What should I investigate next?",
        ],
        "customers": [
            "Summarize this customer.",
            "Show full history for this customer.",
            "Export this customer history workbook.",
            "What are the biggest watchouts here?",
            "Which products matter most?",
            "Is this customer at risk?",
            "What changed recently?",
        ],
        "products": [
            "Summarize this product.",
            "Show full history for this product.",
            "Export this product workbook.",
            "Why is this product's margin changing?",
            "Which customers drive this SKU?",
            "Is this product risky?",
            "What should sales review?",
        ],
        "regions": [
            "Summarize this region.",
            "Show region history and compare with last year.",
            "Export region analysis workbook.",
            "What is changing here?",
            "Which customers or products matter most?",
            "Are there profitability issues?",
        ],
        "suppliers": [
            "Summarize this supplier.",
            "Show supplier history over time.",
            "Export supplier analysis workbook.",
            "Are there dependency risks?",
            "What changed in supplier performance?",
            "Which products matter most?",
        ],
        "salesreps": [
            "Summarize this portfolio.",
            "Show rep portfolio history.",
            "Export rep portfolio workbook.",
            "Which customers are driving this rep's performance?",
            "Where are the risks?",
            "What changed recently?",
        ],
        "returns": [
            "Summarize returns in my scope.",
            "Show returns history.",
            "Export returns workbook.",
            "What approvals are pending?",
            "What are the main return reasons?",
            "Explain the workflow here.",
        ],
    }
    selected = module_prompts.get(page, module_prompts["overview"])
    suggestions: List[str] = list(selected)
    if module_access.get("overview") and page != "overview":
        suggestions.append("Summarize current business priorities across modules.")
    if module_access.get("returns") and page != "returns":
        suggestions.append("Are returns affecting margin or customer risk?")
    suggestions.extend(
        [
            "Explain that in simpler terms.",
            "Show more detail.",
            "Summarize for leadership.",
            "What should I do next?",
        ]
    )
    deduped: List[str] = []
    for prompt in suggestions:
        if prompt not in deduped:
            deduped.append(prompt)
    return _tool_response(
        status="ok",
        title="Recommended Follow-up Questions",
        data={"suggestions": deduped[:12]},
        ctx=ctx,
        citations=["assistant.followups"],
    )


ToolFn = Callable[[ToolContext, Dict[str, Any] | None], Dict[str, Any]]


TOOL_REGISTRY: Dict[str, ToolFn] = {
    "get_user_scope": get_user_scope,
    "get_current_page_context": get_current_page_context,
    "get_overview_summary": get_overview_summary,
    "get_overview_kpis": get_overview_kpis,
    "get_trend_series": get_trend_series,
    "get_price_volume_mix": get_price_volume_mix,
    "get_top_movers": get_top_movers,
    "get_concentration_risk": get_concentration_risk,
    "get_margin_watchlist": get_margin_watchlist,
    "get_forecast_summary": get_forecast_summary,
    "get_data_health": get_data_health,
    "get_customer_detail": get_customer_detail,
    "get_product_detail": get_product_detail,
    "get_region_detail": get_region_detail,
    "get_supplier_detail": get_supplier_detail,
    "get_sales_rep_detail": get_sales_rep_detail,
    "get_customer_summary": get_customer_summary,
    "get_customer_kpis": get_customer_kpis,
    "get_customer_trend": get_customer_trend,
    "get_customer_watchouts": get_customer_watchouts,
    "get_customer_relationships": get_customer_relationships,
    "get_customer_drilldown_explanation": get_customer_drilldown_explanation,
    "get_product_summary": get_product_summary,
    "get_product_kpis": get_product_kpis,
    "get_product_trend": get_product_trend,
    "get_product_watchouts": get_product_watchouts,
    "get_product_dependencies": get_product_dependencies,
    "get_region_summary": get_region_summary,
    "get_region_trend": get_region_trend,
    "get_region_movers": get_region_movers,
    "get_region_watchouts": get_region_watchouts,
    "get_supplier_summary": get_supplier_summary,
    "get_supplier_trend": get_supplier_trend,
    "get_supplier_watchouts": get_supplier_watchouts,
    "get_sales_rep_summary": get_sales_rep_summary,
    "get_sales_rep_trend": get_sales_rep_trend,
    "get_sales_rep_watchouts": get_sales_rep_watchouts,
    "get_returns_summary": get_returns_summary,
    "get_pending_returns": get_pending_returns,
    "get_returns_status_overview": get_returns_status_overview,
    "get_returns_reason_patterns": get_returns_reason_patterns,
    "get_returns_workflow_help": get_returns_workflow_help,
    "search_business_glossary": search_business_glossary,
    "explain_metric_definition": explain_metric_definition,
    "get_metric_definition": get_metric_definition,
    "get_page_help": get_page_help_tool,
    "get_page_bundle": get_page_bundle,
    "get_entity_page_bundle": get_entity_page_bundle,
    "get_current_page_summary": get_current_page_summary,
    "get_current_page_visible_state": get_current_page_visible_state,
    "get_entity_relationship_context": get_entity_relationship_context,
    "get_overview_history": get_overview_history,
    "get_customer_history": get_customer_history,
    "get_product_history": get_product_history,
    "get_region_history": get_region_history,
    "get_supplier_history": get_supplier_history,
    "get_sales_rep_history": get_sales_rep_history,
    "get_returns_history": get_returns_history,
    "rank_entities": rank_entities,
    "aggregate_by_dimension": aggregate_by_dimension,
    "get_top_regions": get_top_regions,
    "get_top_customers": get_top_customers,
    "get_top_products": get_top_products,
    "get_top_suppliers": get_top_suppliers,
    "get_top_sales_reps": get_top_sales_reps,
    "get_top_products_for_customer": get_top_products_for_customer,
    "get_top_customers_for_supplier": get_top_customers_for_supplier,
    "get_top_customers_for_product": get_top_customers_for_product,
    "get_top_products_for_supplier": get_top_products_for_supplier,
    "get_top_products_for_sales_rep": get_top_products_for_sales_rep,
    "get_nested_rankings": get_nested_rankings,
    "get_top_customers_with_top_products": get_top_customers_with_top_products,
    "get_top_suppliers_with_top_products": get_top_suppliers_with_top_products,
    "get_top_sales_reps_with_top_customers": get_top_sales_reps_with_top_customers,
    "get_top_regions_with_top_products": get_top_regions_with_top_products,
    "get_top_products_with_top_customers": get_top_products_with_top_customers,
    "resolve_entity_reference": resolve_entity_reference,
    "get_detailed_metric_breakdown": get_detailed_metric_breakdown,
    "get_entity_driver_breakdown": get_entity_driver_breakdown,
    "get_entity_margin_breakdown": get_entity_margin_breakdown,
    "get_top_margin_risk_products": get_top_margin_risk_products,
    "get_top_decliners": get_top_decliners,
    "get_top_gainers": get_top_gainers,
    "compare_current_vs_prior_period_rankings": compare_current_vs_prior_period_rankings,
    "compare_periods_for_entity": compare_periods_for_entity,
    "explain_history_for_entity": explain_history_for_entity,
    "compare_periods": compare_periods,
    "compare_entities": compare_entities,
    "compare_customers": compare_customers,
    "compare_products": compare_products,
    "compare_regions": compare_regions,
    "compare_suppliers": compare_suppliers,
    "compare_sales_reps": compare_sales_reps,
    "summarize_module_state": summarize_module_state,
    "get_entity_watchouts": get_entity_watchouts,
    "get_priority_investigations": get_priority_investigations,
    "get_priority_actions": get_priority_actions,
    "get_related_investigations": get_related_investigations,
    "get_executive_summary": get_executive_summary,
    "get_export_options_for_page": get_export_options_for_page,
    "get_exportable_columns_for_context": get_exportable_columns_for_context,
    "get_ranked_dataset": get_ranked_dataset,
    "get_grouped_dataset": get_grouped_dataset,
    "get_entity_history_dataset": get_entity_history_dataset,
    "get_comparison_dataset": get_comparison_dataset,
    "get_summary_dataset": get_summary_dataset,
    "get_chart_series": get_chart_series,
    "get_export_metadata": get_export_metadata,
    "get_leadership_summary_dataset": get_leadership_summary_dataset,
    "export_current_page_excel": export_current_page_excel,
    "export_current_entity_history_excel": export_current_entity_history_excel,
    "export_analysis_bundle_excel": export_analysis_bundle_excel,
    "export_leadership_pack_excel": export_leadership_pack_excel,
    "export_watchlist_excel": export_watchlist_excel,
    "export_custom_scoped_excel": export_custom_scoped_excel,
    "export_ranked_list_excel": export_ranked_list_excel,
    "export_grouped_metric_excel": export_grouped_metric_excel,
    "export_comparison_excel": export_comparison_excel,
    "export_nested_ranking_excel": export_nested_ranking_excel,
    "export_hierarchical_analysis_excel": export_hierarchical_analysis_excel,
    "export_chart_series_file": export_chart_series_file,
    "export_chart_image_file": export_chart_image_file,
    "export_custom_analysis_file": export_custom_analysis_file,
    "build_export_configuration": build_export_configuration,
    "refine_export_request": refine_export_request,
    "build_saved_view_suggestion": build_saved_view_suggestion,
    "build_analysis_bundle_request": build_analysis_bundle_request,
    "set_answer_mode": set_answer_mode,
    "set_export_mode": set_export_mode,
    "get_proactive_insights": get_proactive_insights,
    "get_anomaly_narratives": get_anomaly_narratives,
    "get_priority_risks": get_priority_risks,
    "get_guided_investigation_paths": get_guided_investigation_paths,
    "get_confidence_or_trust_summary": get_confidence_or_trust_summary,
    "get_cross_module_risk_summary": get_cross_module_risk_summary,
    "get_risk_trend_baseline": get_risk_trend_baseline,
    "get_causal_attribution_graph": get_causal_attribution_graph,
    "get_next_best_questions": get_next_best_questions,
    "get_executive_digest": get_executive_digest,
    "get_manager_digest": get_manager_digest,
    "get_leadership_summary": get_leadership_summary,
    "get_investigation_checklist": get_investigation_checklist,
    "get_entity_change_explanation": get_entity_change_explanation,
    "get_workflow_assist_note": get_workflow_assist_note,
    "create_digest_schedule": create_digest_schedule,
    "list_digest_schedules": list_digest_schedules,
    "run_digest_schedule": run_digest_schedule,
    "delete_digest_schedule": delete_digest_schedule,
    "get_recommended_followups": get_recommended_followups,
}


def execute_tool(name: str, ctx: ToolContext, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
    fn = TOOL_REGISTRY.get(str(name or "").strip())
    if fn is None:
        return _tool_response(
            status="error",
            title="Unknown Tool",
            data={},
            ctx=ctx,
            notes=[f"Tool '{name}' is not registered."],
        )
    try:
        return fn(ctx, args or {})
    except Exception as exc:
        return _tool_response(
            status="error",
            title=f"{name} failed",
            data={},
            ctx=ctx,
            notes=[str(exc)],
        )


def tool_names() -> List[str]:
    return sorted(TOOL_REGISTRY.keys())

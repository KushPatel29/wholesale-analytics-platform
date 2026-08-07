from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
import uuid
from typing import Any, Mapping, Optional, Sequence

import pandas as pd
from cachetools import TTLCache
from concurrent.futures import Future
from flask import current_app, g, request, has_request_context
from flask_login import current_user
from werkzeug.exceptions import InternalServerError

from fact_checkpoints import log_fact_checkpoint
from app.services import fact_store, filters_service
from app.core.exceptions import DatasetNotBuiltError
from app.services.filters import (
    FilterParams,
    apply_filters,
    bind_filter_cache_key,
    normalize_filters,
    parse_filters,
)
from app.services.frame import canonicalize
from app.services import analytics_utils as au
from app.core import access_policy


def _bool_env(name: str, default: bool = False) -> bool:
    try:
        val = os.getenv(name)
        if val is None:
            return default
        return val.strip().lower() in {"1", "true", "yes", "on"}
    except Exception:
        return default


def _scope_tokens(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.replace(";", ",").replace("|", ",")
        return [p.strip() for p in raw.split(",") if p.strip()]
    vals: list[str] = []
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


_FRAME_CACHE = TTLCache(maxsize=int(os.getenv("FACT_CACHE_MAX", "128")), ttl=int(os.getenv("FACT_CACHE_TTL", "120")))
_FRAME_CACHE_LOCK = threading.RLock()
_INFLIGHT: dict[str, Future] = {}


def _singleflight(key: str, loader) -> Any:
    """Deduplicate concurrent identical requests."""
    with _FRAME_CACHE_LOCK:
        fut = _INFLIGHT.get(key)
        if fut:
            return fut.result()
        fut = Future()
        _INFLIGHT[key] = fut
    try:
        result = loader()
        fut.set_result(result)
        return result
    finally:
        with _FRAME_CACHE_LOCK:
            _INFLIGHT.pop(key, None)


def _user_scope(user: Any, *, sales_rep_override: Optional[str] = None, region_ids_override: Optional[Sequence[str]] = None) -> dict[str, Any]:
    user_obj = user
    try:
        if user_obj is None and current_user:
            user_obj = current_user  # type: ignore[assignment]
    except Exception:
        pass

    scope_obj = access_policy.scope_for_user(user_obj, use_cache=True)
    scope = scope_obj.as_dict(include_allowed=True)
    scope["role"] = (getattr(user_obj, "role", None) or "").strip().lower() if user_obj is not None else None
    scope["sales_rep_ids"] = _scope_tokens(scope.get("allowed_erp_user_ids"))
    scope["is_super_user"] = bool(scope_obj.is_admin or scope_obj.scope_mode == "all")
    scope["user"] = user_obj
    scope["user_sales_rep_id"] = getattr(user_obj, "erp_user_id", None) if user_obj is not None else None

    if sales_rep_override:
        override_tokens = _scope_tokens(sales_rep_override)
        scope["allowed_erp_user_ids"] = override_tokens
        scope["sales_rep_ids"] = override_tokens
        scope["scope_mode"] = "list" if override_tokens else "none"
        try:
            basis = f"{scope.get('scope_mode')}:{'|'.join(sorted(override_tokens))}"
            scope["scope_hash"] = hashlib.sha256(basis.encode("utf-8")).hexdigest()
        except Exception:
            scope["scope_hash"] = None
    if region_ids_override:
        scope["region_ids"] = _scope_tokens(region_ids_override)

    return scope


def _canonical_filters(filters: FilterParams) -> dict[str, Any]:
    payload = {
        "start": getattr(filters, "start", None),
        "end": getattr(filters, "end", None),
        "statuses": sorted(getattr(filters, "statuses", ())),
        "regions": sorted(getattr(filters, "regions", ())),
        "methods": sorted(getattr(filters, "methods", ())),
        "customers": sorted(getattr(filters, "customers", ())),
        "suppliers": sorted(getattr(filters, "suppliers", ())),
        "products": sorted(getattr(filters, "products", ())),
        "sales_reps": sorted(getattr(filters, "sales_reps", ())),
        "preset": getattr(filters, "preset", None),
    }
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in payload.items()}


def _cache_key(endpoint: str, filters: FilterParams, scope: dict[str, Any], data_version: str, *, columns: Optional[list[str]] = None, best_effort: bool = False) -> str:
    payload = {
        "endpoint": endpoint,
        "filters": _canonical_filters(filters),
        "filters_hash": filters_service.filters_hash(filters),
        "scope": {
            "role": scope.get("role"),
            "user_id": scope.get("user_id"),
            "scope_mode": scope.get("scope_mode"),
            "scope_hash": scope.get("scope_hash"),
            "permissions_version": scope.get("permissions_version"),
        },
        "data_version": data_version,
        "columns": columns or [],
        "best_effort": best_effort,
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _safe_iso(ts: Any) -> Optional[str]:
    try:
        if ts is None or (isinstance(ts, float) and pd.isna(ts)):
            return None
        if isinstance(ts, pd.Timestamp):
            ts = ts.tz_localize(None) if getattr(ts, "tzinfo", None) else ts
            return ts.isoformat()
        return pd.to_datetime(ts).isoformat()
    except Exception:
        return None


def current_data_version() -> str:
    """
    Shared data version marker used in cache keys to avoid stale payloads.
    Prefers snapshot manifest/cache_buster; falls back to a time bucket.
    """
    try:
        token = os.getenv("DATA_VERSION_TOKEN")
        if token:
            return str(token)
    except Exception:
        pass

    try:
        return fact_store.cache_buster()
    except Exception:
        pass

    return str(int(pd.Timestamp.utcnow().timestamp()))


@dataclass
class FactContext:
    df: pd.DataFrame
    meta: dict[str, Any]
    cache_key: Optional[str]
    cache_hit: bool
    source: str


def _fill_dimensions(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure missing dimension values do not cause row drops downstream."""
    if df is None or df.empty:
        return df
    fills = {
        "SupplierName": "Unknown Supplier",
        "CustomerName": "Unknown Customer",
        "RegionName": "Unknown Region",
        "ProductName": "Unknown Product",
        "ProductId": "Unknown Product",
        "SupplierId": "Unknown Supplier",
        "CustomerId": "Unknown Customer",
        "OrderStatus": "Unknown",
    }
    work = df.copy()
    for col, default in fills.items():
        if col in work.columns:
            work[col] = work[col].fillna(default)
    return work


def _record_meta(meta: dict[str, Any]) -> None:
    try:
        g.fact_meta = meta
    except Exception:
        pass


def _log_request(meta: dict[str, Any], cache_key: Optional[str]) -> None:
    if not _bool_env("DATA_DEBUG", False) and not _bool_env("DEBUG_FILTERS", False):
        return
    try:
        ck_hash = hashlib.sha256((cache_key or "").encode("utf-8")).hexdigest()[:12]
        endpoint = request.path if has_request_context() else None
        payload = {
            "endpoint": endpoint,
            "user_id": meta.get("user_id"),
            "role": meta.get("role"),
            "is_super_user": meta.get("is_super_user"),
            "start": meta.get("start"),
            "end": meta.get("end"),
            "statuses": meta.get("statuses"),
            "revenue_sum": meta.get("revenue_sum"),
            "rows": meta.get("rows"),
            "cache_hit": meta.get("cache_hit"),
            "cache_key_hash": ck_hash,
        }
        current_app.logger.info("fact_context", extra=payload)
    except Exception:
        try:
            current_app.logger.debug("fact_context_log_failed", exc_info=True)
        except Exception:
            pass


def _log_api_audit(meta: dict[str, Any]) -> None:
    """Structured log for API responses to catch scope/filter mismatches early."""
    try:
        if not has_request_context():
            return
        scoped = bool(meta.get("allowed_erp_user_ids")) and not meta.get("is_super_user")
        payload = {
            "route": request.path,
            "user_id": meta.get("user_id"),
            "role": meta.get("role"),
            "start": meta.get("start"),
            "end": meta.get("end"),
            "statuses": meta.get("statuses"),
            "scoped": scoped,
            "cache_hit": meta.get("cache_hit"),
            "rows_returned": meta.get("rows"),
            "revenue_returned": meta.get("revenue_sum"),
        }
        current_app.logger.info("api.request.summary", extra=payload)
    except Exception:
        try:
            current_app.logger.debug("api_request_summary_failed", exc_info=True)
        except Exception:
            pass


def get_fact_context(
    user: Any = None,
    filters: Any = None,
    *,
    columns: Optional[list[str]] = None,
    sales_rep_override: Optional[str] = None,
    region_ids_override: Optional[Sequence[str]] = None,
    use_cache: bool = True,
    best_effort: bool = False,
) -> FactContext:
    """
    Centralized fact loader that applies consistent date/status filters and scope.
    Returns the dataframe plus a metadata envelope for diagnostics.
    """
    started = time.perf_counter()
    parsed_filters: FilterParams
    if filters is None:
        try:
            parsed_filters = parse_filters(getattr(request, "args", {}) or {})
        except Exception:
            parsed_filters = normalize_filters({})
    else:
        parsed_filters = normalize_filters(filters)

    scope = _user_scope(user, sales_rep_override=sales_rep_override, region_ids_override=region_ids_override)
    role = (scope.get("role") or "").strip().lower()
    is_admin = role == "admin"
    user_id_val = scope.get("user_id")
    data_version = current_data_version()

    start = getattr(parsed_filters, "start", None)
    end = getattr(parsed_filters, "end", None)
    preset = getattr(parsed_filters, "preset", None)
    if is_admin and start is None and end is None and preset is None:
        try:
            start = pd.Timestamp(year=2018, month=1, day=1)
        except Exception:
            start = None
        apply_default_override = False
    else:
        apply_default_override = True
    start_iso = start.date().isoformat() if start is not None else None
    end_iso = end.date().isoformat() if end is not None else None
    all_time = str(preset).lower() in {"all", "__all__", "*"} if preset else False
    apply_default_window = (start is None and end is None and not all_time and apply_default_override)

    statuses = tuple(getattr(parsed_filters, "statuses", ()) or ())
    status_list = list(statuses) if statuses else None

    if has_request_context():
        try:
            raw_best = request.args.get("best_effort")
            if raw_best is not None:
                best_effort = str(raw_best).strip().lower() in {"1", "true", "yes", "on"}
        except Exception:
            pass
    best_effort_flag = bool(best_effort)
    try:
        g.fact_best_effort = best_effort_flag
    except Exception:
        pass

    endpoint = request.path if has_request_context() else "offline"
    cache_key = _cache_key(endpoint, parsed_filters, scope, data_version, columns=columns, best_effort=best_effort_flag)
    bind_filter_cache_key(cache_key)

    df: Optional[pd.DataFrame] = None
    cache_hit = False
    cached_at: Optional[float] = None
    if use_cache:
        with _FRAME_CACHE_LOCK:
            cached = _FRAME_CACHE.get(cache_key)
        if cached is not None:
            df = cached.get("df")
            cached_at = cached.get("cached_at")
            cache_hit = True

    def _load_frame() -> pd.DataFrame:
        if current_app.config.get("TESTING"):
            if current_app.config.get("TEST_STATE") is not None:
                try:
                    import data_loader as loader  # type: ignore

                    return loader.load_snapshot(columns=columns)
                except Exception:
                    pass
            try:
                import data_loader as loader  # type: ignore

                if hasattr(loader, "get_fact_df"):
                    try:
                        return loader.get_fact_df(columns=columns)
                    except TypeError:
                        pass
            except Exception:
                pass
            try:
                try:
                    return fact_store.get_sales_fact(columns=columns, filters=parsed_filters)
                except TypeError:
                    return fact_store.get_sales_fact(columns=columns)
            except DatasetNotBuiltError:
                raise
            except Exception:
                pass
        return fact_store.query_fact(
            filters=parsed_filters,
            columns=columns,
            scope=scope,
            apply_default_window=apply_default_window,
            use_cache=True,
        )

    if df is None:
        df = _singleflight(cache_key, _load_frame)
        if use_cache:
            with _FRAME_CACHE_LOCK:
                _FRAME_CACHE[cache_key] = {"df": df.copy(), "cached_at": time.time()}

    source = "duckdb"

    df = canonicalize(df)
    df = _fill_dimensions(df)
    df = apply_filters(df, parsed_filters)

    # Scope is enforced at query time via DuckDB; no pandas post-filtering.

    dim_gaps = {}
    for col in ("ProductId", "CustomerId", "SupplierId"):
        if col in df.columns:
            dim_gaps[f"{col.lower()}_missing"] = int(df[col].isna().sum())
    if any(v > 0 for v in dim_gaps.values()):
        current_app.logger.warning("fact_context.dimension_gaps", extra=dim_gaps)

    rev_col = au.revenue_column(df) or "Revenue"
    cost_col = au.cost_column(df) or "Cost"
    revenue_series = au.to_numeric_safe(df.get(rev_col, pd.Series(dtype=float))) if not df.empty else pd.Series(dtype=float)
    cost_series = au.to_numeric_safe(df.get(cost_col, pd.Series(dtype=float))) if not df.empty else pd.Series(dtype=float)
    revenue_sum = float(revenue_series.sum()) if len(revenue_series) else 0.0
    cost_sum = float(cost_series.sum()) if len(cost_series) else 0.0
    cost_missing_rate = float(cost_series.isna().mean()) if len(cost_series) else 1.0
    packs_coverage = {}
    if "missing_packs" in df.columns:
        try:
            missing_mask = df["missing_packs"].fillna(False).astype(bool)
            total_lines = int(len(df))
            missing_lines = int(missing_mask.sum())
            has_lines = max(0, total_lines - missing_lines)
            packs_coverage = {
                "total_orderlines": total_lines,
                "has_packs_orderlines": has_lines,
                "missing_packs_orderlines": missing_lines,
                "packs_coverage_pct": round((has_lines / total_lines) * 100.0, 2) if total_lines else None,
            }
        except Exception:
            packs_coverage = {}
    correlation_id = getattr(g, "request_id", None) or str(uuid.uuid4())
    if len(df) and ((cost_col not in df.columns) or cost_series.isna().all()):
        if best_effort_flag:
            df["Cost"] = cost_series.fillna(0.0)
            if rev_col in df.columns:
                df["Profit"] = au.to_numeric_safe(df[rev_col]).fillna(0.0) - df["Cost"]
        else:
            current_app.logger.error(
                "fact.cost_missing",
                extra={
                    "request_id": correlation_id,
                    "cost_col": cost_col,
                    "columns": list(df.columns),
                    "rows": len(df),
                },
            )
            raise InternalServerError(
                description=f"Cost column missing or empty; request_id={correlation_id}. Pass best_effort=1 to allow zero-fill."
            )
    elif best_effort_flag:
        df["Cost"] = cost_series.fillna(0.0)
        if rev_col in df.columns:
            df["Profit"] = au.to_numeric_safe(df[rev_col]).fillna(0.0) - df["Cost"]

    date_min = date_max = None
    if "Date" in df.columns:
        dates = pd.to_datetime(df["Date"], errors="coerce").dropna()
        if not dates.empty:
            date_min = _safe_iso(dates.min())
            date_max = _safe_iso(dates.max())

    updated_max = None
    for col in [c for c in df.columns if "updated" in c.lower()]:
        try:
            ts = pd.to_datetime(df[col], errors="coerce")
            if ts.notna().any():
                candidate = ts.max()
                updated_max = _safe_iso(candidate) if updated_max is None else max(updated_max, _safe_iso(candidate))
        except Exception:
            continue

    meta = {
        "rows": int(len(df)),
        "unique_order_lines": int(df["OrderLineId"].nunique()) if "OrderLineId" in df.columns else int(len(df)),
        "date_min": date_min,
        "date_max": date_max,
        "updated_max": updated_max,
        "revenue_sum": round(revenue_sum, 2),
        "cost_sum": round(cost_sum, 2),
        "cost_missing_rate": cost_missing_rate,
        "packs_coverage": packs_coverage,
        "best_effort": best_effort_flag,
        "role": scope.get("role"),
        "is_super_user": bool(scope.get("is_super_user")),
        "scoped": bool(scope.get("allowed_erp_user_ids")) and not scope.get("is_super_user"),
        "allowed_erp_user_ids": scope.get("allowed_erp_user_ids"),
        "user_id": scope.get("user_id"),
        "data_version": data_version,
        "cache_hit": cache_hit,
        "cache_key_hash": hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:12] if cache_key else None,
        "cache_key": cache_key,
        "cache_age_seconds": (time.time() - cached_at) if cache_hit and cached_at else None,
        "request_id": getattr(g, "request_id", None),
        "cached": cache_hit,
        "start": _safe_iso(start),
        "end": _safe_iso(end),
        "statuses": list(statuses),
        "source": source,
    }

    _record_meta(meta)
    _log_request(meta, cache_key)
    _log_api_audit(meta)
    total_ms = int((time.perf_counter() - started) * 1000)
    warn_threshold = int(os.getenv("FACT_REQUEST_WARN_MS", "1500"))
    try:
        route = request.path if has_request_context() else None
    except Exception:
        route = None
    timing_payload = {
        "route": route,
        "duration_ms": total_ms,
        "rows": len(df),
        "source": source,
        "cache_hit": cache_hit,
        "start": _safe_iso(start),
        "end": _safe_iso(end),
        "statuses": list(statuses),
    }
    try:
        if total_ms > warn_threshold:
            current_app.logger.warning("fact.request.timing", extra=timing_payload)
        else:
            current_app.logger.info("fact.request.timing", extra=timing_payload)
    except Exception:
        current_app.logger.debug("fact.request.timing_log_failed", exc_info=True)
    log_fact_checkpoint(
        "backend.fact.filtered",
        df,
        {
            "start": start_iso,
            "end": end_iso,
            "statuses": list(statuses),
            "role": role,
            "is_admin": is_admin,
            "cache_hit": cache_hit,
            "cache_key": cache_key,
        },
    )

    return FactContext(df=df, meta=meta, cache_key=cache_key, cache_hit=cache_hit, source=source)


def get_fact_dataframe(*, user: Any = None, filters: Any = None, columns: Optional[list[str]] = None, **kwargs: Any) -> pd.DataFrame:
    """Convenience wrapper returning only the dataframe."""
    return get_fact_context(user=user, filters=filters, columns=columns, **kwargs).df

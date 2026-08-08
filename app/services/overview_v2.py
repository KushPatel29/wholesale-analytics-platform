from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Tuple
import json
import math
import os
import time
from concurrent.futures import Future
from datetime import date, datetime, timedelta
from threading import Lock, RLock

import pandas as pd
import numpy as np
from cachetools import TTLCache
from flask import current_app, g
from flask_login import current_user


from app.cache import cache
from app.services import analytics_utils as au
from app.services import margin_rules
from app.services import overview_metrics as om
from app.services.filters import (
    FilterParams,
    fiscal_month_label,
    filters_cache_key,
)
from data.store import (
    get_conn as get_duck_conn,
    init_views as init_duck_views,
    list_columns as duck_columns,
    manifest_max_date,
    manifest_version,
)

# Cache TTL requested by requirements (16.716 hours)
CACHE_TTL = 60_180

BUNDLE_CACHE_TTL = CACHE_TTL
BUNDLE_SCHEMA_VERSION = "overview_bundle_v1"
_BUNDLE_REV = "2026-03-28-v4"
_DRIVER_DECOMP_REV = "2026-02-23-v2"
_DRIVER_DECOMP_TOLERANCE = 0.01
_DRIVER_DECOMP_TOP_N = 5

# The margin-risk watchlist is annotated by `margin_rules.annotate_margin_frame`,
# which returns 80-odd columns of pricing intermediates. The overview only ever
# renders a dozen of them, so shipping the rest cost 459 KB per copy - and the
# same list is reachable from three places in the payload (`profitability`,
# `insights.margin_risk`, `overview_metrics.profitability`), so json.dumps wrote
# it three times: 1.38 MB of a 1.44 MB response. Project to what the front end
# reads and the whole bundle drops to about 45 KB.
#
# The drilldown and export paths build their own frame (`_build_margin_risk_table`)
# and keep every column, so nothing analytical is lost here - only the columns
# that were being sent to a page that never looked at them.
MARGIN_RISK_PAYLOAD_FIELDS: tuple[str, ...] = (
    "entity_id",
    "label",
    "product",
    "supplier",
    "protein",
    "family",
    "revenue",
    "revenue_share",
    "profit",
    "profit_impact",
    "profit_lost_vs_target",
    "margin_pct",
    "margin_delta",
    "minimum_margin_pct",
    "target_margin_pct",
    "gap_to_target",
    "status_key",
    "target_status",
    "risk",
)

# How many watchlist rows travel to the browser. Every renderer slices the top
# 5 or 10; the tail only ever fed two aggregates, and those are now computed
# server-side and sent alongside (`margin_risk_total_count`,
# `margin_risk_revenue_share_pct`) so the numbers on screen are unchanged.
MARGIN_RISK_PAYLOAD_LIMIT = max(int(os.getenv("OVERVIEW_MARGIN_RISK_LIMIT", "40") or 40), 1)

_bundle_cache = TTLCache(maxsize=int(os.getenv("OVERVIEW_BUNDLE_CACHE_MAXSIZE", "128") or 128), ttl=BUNDLE_CACHE_TTL)
_bundle_cache_lock: RLock = RLock()
_bundle_lock: Lock = Lock()
_bundle_locks: Dict[str, Lock] = {}
_inflight_bundles: Dict[str, Future] = {}


@dataclass
class FrameContext:
    df: pd.DataFrame
    colmap: Dict[str, Optional[str]]
    flags: Dict[str, bool]
    missing: List[str]
    window: Dict[str, Any]
    last_refresh: Optional[str]
    version: str
    cache_hit: bool
    cost_missing_rate: float = 1.0
    best_effort: bool = False


def _log_perf(stage: str, start_ts: float, **extra: Any) -> None:
    """Lightweight structured timing log."""
    try:
        elapsed_ms = (time.perf_counter() - start_ts) * 1000
        payload = {"stage": stage, "ms": round(elapsed_ms, 2)}
        if extra:
            payload.update(extra)
        current_app.logger.info("overview.perf", extra=payload)
    except Exception:
        # Logging must never break request handling
        pass


def _manifest_meta() -> Tuple[str, Optional[str]]:
    """Return (version, last_refresh_iso) from loader manifest."""
    manifest: Dict[str, Any] = {}
    try:
        from app.services import fact_store  # type: ignore

        manifest = fact_store.get_meta() or {}
        version = str(manifest.get("dataset_version") or manifest.get("version") or fact_store.cache_buster())
    except Exception:
        version = "0"
    last_refresh = (
        manifest.get("built_at_utc")
        or manifest.get("built_at")
        or manifest.get("last_refresh_utc")
        or manifest.get("refreshed_at")
    )
    return version, last_refresh


def _coerce_manifest_timestamp(value: Any) -> Optional[pd.Timestamp]:
    if value is None:
        return None
    try:
        ts = pd.Timestamp(value)
    except Exception:
        return None
    if getattr(ts, "tzinfo", None) is not None:
        try:
            ts = ts.tz_convert(None)
        except Exception:
            ts = ts.tz_localize(None)
    return ts


def _refresh_meta() -> Dict[str, Any]:
    version, last_refresh = _manifest_meta()
    refresh_ts = _coerce_manifest_timestamp(last_refresh)
    cutoff_ts = _coerce_manifest_timestamp(manifest_max_date())
    now_ts = pd.Timestamp.utcnow().tz_localize(None)
    refresh_age_hours: Optional[int] = None
    refresh_age_days: Optional[int] = None
    if refresh_ts is not None:
        age_seconds = max(0.0, float((now_ts - refresh_ts).total_seconds()))
        refresh_age_hours = int(age_seconds // 3600)
        refresh_age_days = int(age_seconds // 86400)
    return {
        "version": version,
        "last_refresh": refresh_ts.isoformat() if refresh_ts is not None else (str(last_refresh) if last_refresh else None),
        "data_cutoff": cutoff_ts.date().isoformat() if cutoff_ts is not None else None,
        "refresh_age_hours": refresh_age_hours,
        "refresh_age_days": refresh_age_days,
    }


def _window_method_label(window_contract: om.WindowContract) -> str:
    mapping = {
        "month_to_date_vs_prior_month_same_day": "Month-to-date",
        "completed_months_vs_prior_completed_months": "Completed months",
        "selected_window_vs_prior_matched_days": "Matched days",
        "fiscal_year_to_date_vs_prior_fiscal_year_to_date": "Fiscal year-to-date",
        "fiscal_year_vs_prior_fiscal_year": "Fiscal year",
        "fiscal_quarter_to_date_vs_prior_fiscal_quarter_same_day": "Fiscal quarter-to-date",
    }
    return mapping.get(str(window_contract.method or "").strip(), "Filtered window")


def _trend_labels_from_index(index: pd.Index, *, date_type: str | None = None) -> list[str]:
    labels: list[str] = []
    is_fiscal = str(date_type or "").strip().lower() == "fiscal"
    for raw in list(index):
        fallback = str(raw)
        if not is_fiscal:
            labels.append(fallback)
            continue
        try:
            if isinstance(raw, pd.Period):
                stamp = raw.to_timestamp()
            else:
                stamp = pd.Timestamp(raw)
        except Exception:
            labels.append(fallback)
            continue
        labels.append(fiscal_month_label(stamp) or fallback)
    return labels


def _date_bounds(df: pd.DataFrame, date_col: Optional[str]) -> Dict[str, Any]:
    if df is None or df.empty or not date_col or date_col not in df.columns:
        return {"start": None, "end": None, "days": 0, "rows": int(len(df) if df is not None else 0)}
    dates = pd.to_datetime(df[date_col], errors="coerce").dropna()
    if dates.empty:
        return {"start": None, "end": None, "days": 0, "rows": int(len(df))}
    start = dates.min().normalize()
    end = dates.max().normalize()
    days = max(1, int((end - start).days) + 1)
    return {"start": start.isoformat(), "end": end.isoformat(), "days": days, "rows": int(len(df))}


def _filters_payload(filters: FilterParams) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "start": getattr(filters, "start", None).isoformat() if getattr(filters, "start", None) is not None else None,
        "end": getattr(filters, "end", None).isoformat() if getattr(filters, "end", None) is not None else None,
        "regions": list(getattr(filters, "regions", ())),
        "methods": list(getattr(filters, "methods", ())),
        "customers": list(getattr(filters, "customers", ())),
        "suppliers": list(getattr(filters, "suppliers", ())),
        "products": list(getattr(filters, "products", ())),
        "sales_reps": list(getattr(filters, "sales_reps", ())),
    }
    payload["has_active_filters"] = any(payload[k] for k in ("regions", "methods", "customers", "suppliers", "products", "sales_reps")) or bool(payload["start"] or payload["end"])
    return payload


def _cache_key(name: str, filters: FilterParams, version: str) -> str:
    """Builds a namespaced cache key for overview payloads."""
    return filters_cache_key(current_user, filters, extras={"scope": "overview_v2", "payload": name, "version": version})


def _from_cache(name: str, filters: FilterParams, version: str) -> Optional[Dict[str, Any]]:
    key = _cache_key(name, filters, version)
    cached = cache.get(key)
    if cached is not None:
        if isinstance(cached, dict):
            meta = cached.get("meta")
            if isinstance(meta, dict):
                meta["cache_hit"] = True
            else:
                cached["cache_hit"] = True  # type: ignore[assignment]
        return cached
    return None


def _store_cache(name: str, filters: FilterParams, version: str, payload: Dict[str, Any]) -> None:
    key = _cache_key(name, filters, version)
    cache.set(key, payload, timeout=CACHE_TTL)


def _clone_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json.loads(json.dumps(payload))
    except Exception:
        return dict(payload)


def _request_id() -> Optional[str]:
    try:
        return getattr(g, "request_id", None)
    except Exception:
        return None


def _stable_filters(filters: FilterParams) -> Dict[str, Any]:
    payload = _filters_payload(filters)
    for key in ("regions", "methods", "customers", "suppliers", "products", "sales_reps"):
        vals = payload.get(key) or []
        payload[key] = sorted({str(v) for v in vals})
    return payload


def _dataset_marker() -> str:
    return manifest_max_date() or manifest_version() or ""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _driver_decomp_v2_enabled() -> bool:
    return _env_bool("DRIVER_DECOMP_V2", False)


def _bundle_cache_key(filters: FilterParams, include_current_month: bool, defaulted_window: bool) -> str:
    try:
        from app.services import fact_store  # type: ignore

        dataset_version = fact_store.cache_buster()
    except Exception:
        dataset_version = _dataset_marker()
    try:
        window_contract = om.resolve_window_contract(filters, include_current_month=include_current_month)
        window_payload = {
            "current_start": window_contract.current_start.isoformat(),
            "current_end": window_contract.current_end.isoformat(),
            "prior_month_start": window_contract.prior_month_start.isoformat(),
            "prior_month_end": window_contract.prior_month_end.isoformat(),
            "prior_year_start": window_contract.prior_year_start.isoformat(),
            "prior_year_end": window_contract.prior_year_end.isoformat(),
        }
    except Exception:
        window_payload = {}
    return filters_cache_key(
        current_user,
        filters,
        extras={
            "scope": "overview_v2_bundle",
            "block_id": "overview_bundle",
            "window": window_payload,
            "include_current_month": bool(include_current_month),
            "defaulted_window": bool(defaulted_window),
            "dataset_max_date": _dataset_marker(),
            "dataset_version": dataset_version,
            "driver_decomp_v2": _driver_decomp_v2_enabled(),
            "driver_decomp_rev": _DRIVER_DECOMP_REV,
            "bundle_schema": BUNDLE_SCHEMA_VERSION,
            "bundle_rev": _BUNDLE_REV,
        },
    )


def _bundle_cache_get(cache_key: str) -> Optional[Dict[str, Any]]:
    with _bundle_cache_lock:
        ctx = _bundle_cache.get(cache_key)
    if not ctx:
        return None
    payload = _clone_payload(ctx.get("payload", {}))
    monthly = ctx.get("monthly")
    return {"payload": payload, "monthly": monthly, "cache_hit": True}


def _bundle_cache_set(cache_key: str, payload: Dict[str, Any], monthly: pd.DataFrame) -> None:
    with _bundle_cache_lock:
        _bundle_cache[cache_key] = {"payload": payload, "monthly": monthly}


def _bundle_lock_for(cache_key: str) -> Lock:
    with _bundle_cache_lock:
        lock = _bundle_locks.get(cache_key)
        if lock is None:
            lock = Lock()
            _bundle_locks[cache_key] = lock
    return lock


def _values_list(raw: Any) -> List[str]:
    vals: List[str] = []
    if raw is None:
        return vals
    try:
        iterable = raw if isinstance(raw, (list, tuple, set)) else [raw]
        for item in iterable:
            if item is None:
                continue
            s = str(item).strip()
            if not s or s.lower() == "all":
                continue
            vals.append(s)
    except Exception:
        pass
    return vals


def _to_date(val: Any) -> Optional[date]:
    if val is None:
        return None
    if isinstance(val, pd.Timestamp):
        return val.date()
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    try:
        return datetime.fromisoformat(str(val)).date()
    except Exception:
        return None


def _normalize_dates(filters: FilterParams, include_current_month: bool) -> tuple[Optional[date], Optional[date], bool]:
    start_dt = _to_date(getattr(filters, "start", None))
    end_dt = _to_date(getattr(filters, "end", None))
    defaulted = False
    if start_dt is None and end_dt is None:
        marker = manifest_max_date()
        try:
            end_dt = datetime.fromisoformat(marker).date() if marker else date.today()
        except Exception:
            end_dt = date.today()
        start_dt = end_dt - timedelta(days=180)
        defaulted = True
    if end_dt and not include_current_month:
        month_start = date.today().replace(day=1)
        if end_dt >= month_start:
            end_dt = month_start - timedelta(days=1)
    return start_dt, end_dt, defaulted


def _current_scope_payload() -> Optional[Dict[str, Any]]:
    """
    Resolve the calling user's row-level scope, or None outside a request.

    Every other bundle passes a scope into `fact_store.build_where_clause`.
    This module builds its own SQL against a raw DuckDB connection, so it has
    to fetch the same scope itself.
    """
    try:
        from flask import has_request_context  # type: ignore
        from app.core import access_policy  # type: ignore

        if not has_request_context():
            return None
        return access_policy.get_current_scope(use_cache=True).as_dict(include_allowed=True)
    except Exception:
        return None


def _scope_predicate(cols: set[str]) -> tuple[Optional[str], List[Any]]:
    """Translate the current user's scope into a SQL predicate."""
    scope = _current_scope_payload()
    if scope is None:
        return None, []
    try:
        from app.services import fact_store  # type: ignore

        clause, params = fact_store.build_scope_clause(scope, cols)
    except Exception:
        # Failing closed would black out the page for everyone if the access
        # layer errors, so log and fall back to the unscoped clause only when
        # the scope could not be resolved at all.
        try:
            current_app.logger.exception("overview_v2.scope_clause_failed")
        except Exception:
            pass
        return None, []
    if not clause or clause == "1=1":
        return None, []
    return clause, list(params)


def _where_clause(filters: FilterParams, cols: set[str], include_current_month: bool) -> tuple[str, List[Any], Optional[str], Optional[str], bool]:
    start_dt, end_dt, defaulted_dates = _normalize_dates(filters, include_current_month)
    where_parts: List[str] = ["1=1"]
    params: List[Any] = []

    # Row-level security first: without this the v2 overview returns the whole
    # company to a rep who is scoped to their own book.
    scope_sql, scope_params = _scope_predicate(cols)
    if scope_sql:
        where_parts.append(scope_sql)
        params.extend(scope_params)
    if start_dt:
        where_parts.append("Date >= ?")
        params.append(start_dt.isoformat())
    if end_dt:
        end_excl = end_dt + timedelta(days=1)
        where_parts.append("Date < ?")
        params.append(end_excl.isoformat())

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

    _add_clause(_values_list(getattr(filters, "regions", ())), region_col)
    _add_clause(_values_list(getattr(filters, "methods", ())), method_col)
    _add_clause(_values_list(getattr(filters, "customers", ())), customer_col)
    _add_clause(_values_list(getattr(filters, "suppliers", ())), supplier_col)
    _add_clause(_values_list(getattr(filters, "products", ())), product_col)
    _add_clause(_values_list(getattr(filters, "sales_reps", ())), sales_rep_col)

    status_col = "OrderStatus" if "OrderStatus" in cols else None
    statuses = _values_list(getattr(filters, "statuses", ()))
    if not statuses:
        raw_default = os.getenv("ORDER_STATUSES")
        if raw_default:
            statuses = [s.strip() for s in raw_default.split(",") if s.strip()]
        else:
            statuses = ["packed", "invoiced", "shipped", "delivered"]
    else:
        allowed = set(
            [s.strip().lower() for s in (os.getenv("ORDER_STATUSES") or "").split(",") if s.strip()]
            or ["packed", "invoiced", "shipped", "delivered"]
        )
        statuses = [s for s in statuses if str(s).strip().lower() in allowed]
    if statuses and status_col:
        placeholders = ", ".join("?" for _ in statuses)
        where_parts.append(f"{status_col} IN ({placeholders})")
        params.extend(statuses)

    return " AND ".join(where_parts), params, start_dt.isoformat() if start_dt else None, end_dt.isoformat() if end_dt else None, defaulted_dates


def _exec_df(sql: str, params: List[Any], tag: str, conn) -> pd.DataFrame:
    started = time.perf_counter()
    df: pd.DataFrame
    try:
        df = conn.execute(sql, params).fetchdf()
    except Exception:
        try:
            current_app.logger.exception("duckdb.query_failed", extra={"tag": tag, "request_id": _request_id()})
        except Exception:
            pass
        raise
    finally:
        duration_ms = int((time.perf_counter() - started) * 1000)
        try:
            rows = len(df) if "df" in locals() else None
            current_app.logger.info(
                "duckdb.query",
                extra={"tag": tag, "duration_ms": duration_ms, "rows": rows, "request_id": _request_id()},
            )
        except Exception:
            pass
    return df


def _sum_expr(column: str, cols: set[str]) -> str:
    return f"SUM(COALESCE({column}, 0))" if column in cols else "0"


def _count_expr(column: str, cols: set[str]) -> str:
    return f"COUNT(DISTINCT {column})" if column in cols else "0"


def _safe_col(cols: set[str], *candidates: str) -> Optional[str]:
    for cand in candidates:
        if cand and cand in cols:
            return cand
    return None


def _quote_identifier(label: str) -> str:
    safe = str(label).replace('"', '""')
    return f'"{safe}"'


def _col_expr(col: Optional[str], cast: str, default: str) -> str:
    if not col:
        return f"{default}::{cast}"
    return f"CAST({_quote_identifier(col)} AS {cast})"


def _coalesce_numeric_expr(cols: set[str], candidates: list[str], default: str) -> str:
    expressions: list[str] = []
    seen: set[str] = set()
    for cand in candidates:
        if not cand or cand not in cols or cand in seen:
            continue
        seen.add(cand)
        expressions.append(f"CAST({_quote_identifier(cand)} AS DOUBLE)")
    if not expressions:
        return default
    return f"COALESCE({', '.join(expressions + [default])})"


def _to_list(val: Any) -> List[Any]:
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, tuple):
        return list(val)
    try:
        if isinstance(val, np.ndarray):  # type: ignore
            return val.tolist()
    except Exception:
        pass
    return [val]


def _struct_list(val: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
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
        if math.isnan(fval):  # type: ignore[name-defined]
            return default
        return fval
    except Exception:
        return default


def _clean_optional_float(val: Any) -> Optional[float]:
    try:
        fval = float(val)
        if math.isnan(fval):  # type: ignore[name-defined]
            return None
        return fval
    except Exception:
        return None


def _driver_direction(value: Optional[float], eps: float = 1e-9) -> str:
    if value is None:
        return "flat"
    if value > eps:
        return "up"
    if value < -eps:
        return "down"
    return "flat"


def _driver_share(effect: Optional[float], total: Optional[float], eps: float = 1e-9) -> Optional[float]:
    if effect is None or total is None:
        return None
    if abs(total) <= eps:
        return None
    return (effect / total) * 100.0


def _signed_money(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.0f}"


def _driver_insight(period_label: str, metric_label: str, metric_block: Dict[str, Any]) -> str:
    delta = _clean_optional_float(metric_block.get("delta"))
    if delta is None:
        return f"{period_label} {metric_label.lower()} drivers are unavailable."
    if abs(delta) <= 1e-9:
        return f"{period_label} {metric_label.lower()} was flat; price, volume, and mix netted to near zero."

    components = [
        ("Price", _clean_optional_float(metric_block.get("price_effect")) or 0.0),
        ("Volume", _clean_optional_float(metric_block.get("volume_effect")) or 0.0),
        ("Mix", _clean_optional_float(metric_block.get("mix_effect")) or 0.0),
    ]
    main_name, main_value = max(components, key=lambda item: abs(item[1]))
    share = _driver_share(main_value, delta)
    share_txt = "" if share is None else f", {share:+.0f}% share"
    direction = "up" if delta > 0 else "down"
    return (
        f"{period_label} {metric_label.lower()} {direction} ${abs(delta):,.0f}, "
        f"driven mainly by {main_name} ({_signed_money(main_value)}{share_txt})."
    )


def _build_driver_contributors(
    frame: pd.DataFrame,
    *,
    contribution_col: str,
    unit_cur_col: str,
    unit_prev_col: str,
    total_cur_col: str,
    total_prev_col: str,
    qty_cur_col: str,
    qty_prev_col: str,
    top_n: int,
) -> List[Dict[str, Any]]:
    if frame.empty or contribution_col not in frame.columns:
        return []
    ranked: List[Tuple[float, Any, float]] = []
    for rec in frame.itertuples(index=False):
        contrib = _clean_optional_float(getattr(rec, contribution_col, None))
        if contrib is None or abs(contrib) <= 0:
            continue
        ranked.append((abs(contrib), rec, contrib))
    ranked.sort(key=lambda item: item[0], reverse=True)
    ranked = ranked[: max(1, top_n)]
    rows: List[Dict[str, Any]] = []
    for _abs_val, rec, contrib in ranked:
        rows.append(
            {
                "sku_id": str(getattr(rec, "sku_key", "") or ""),
                "sku": str(getattr(rec, "sku_label", "") or "Unknown"),
                "contribution": contrib,
                "current_qty": _clean_optional_float(getattr(rec, qty_cur_col, None)),
                "prior_qty": _clean_optional_float(getattr(rec, qty_prev_col, None)),
                "current_unit": _clean_optional_float(getattr(rec, unit_cur_col, None)),
                "prior_unit": _clean_optional_float(getattr(rec, unit_prev_col, None)),
                "current_total": _clean_optional_float(getattr(rec, total_cur_col, None)),
                "prior_total": _clean_optional_float(getattr(rec, total_prev_col, None)),
            }
        )
    return rows


def _driver_metric_block(
    frame: pd.DataFrame,
    *,
    metric: str,
    period_label: str,
    tolerance: float,
    top_n: int,
    metric_available: bool,
) -> Dict[str, Any]:
    metric = str(metric or "revenue").strip().lower()
    if frame.empty:
        return {
            "current": 0.0,
            "previous": 0.0,
            "delta": 0.0,
            "delta_pct": None,
            "delta_pct_na_reason": "no prior-period value",
            "price_effect": 0.0,
            "volume_effect": 0.0,
            "mix_effect": 0.0,
            "drivers": [],
            "top_contributors": {"price_effect": [], "volume_effect": [], "mix_effect": []},
            "reconciliation": {
                "total_delta": 0.0,
                "sum_effects": 0.0,
                "residual": 0.0,
                "tolerance": tolerance,
                "within_tolerance": True,
            },
        }

    if metric == "profit" and not metric_available:
        return {
            "current": None,
            "previous": None,
            "delta": None,
            "delta_pct": None,
            "delta_pct_na_reason": "insufficient cost coverage",
            "price_effect": None,
            "volume_effect": None,
            "mix_effect": None,
            "drivers": [],
            "top_contributors": {"price_effect": [], "volume_effect": [], "mix_effect": []},
            "message": f"{period_label} profit drivers unavailable due to missing cost coverage.",
        }

    work = frame.copy()
    if metric == "profit":
        total_cur_col = "profit_cur"
        total_prev_col = "profit_prev"
        qty_cur_col = "qty_with_cost_cur"
        qty_prev_col = "qty_with_cost_prev"
        unit_cur_col = "margin_cur"
        unit_prev_col = "margin_prev"
        unit_label = "Unit margin"
    else:
        total_cur_col = "revenue_cur"
        total_prev_col = "revenue_prev"
        qty_cur_col = "qty_cur"
        qty_prev_col = "qty_prev"
        unit_cur_col = "price_cur"
        unit_prev_col = "price_prev"
        unit_label = "Unit price"

    for col in (
        total_cur_col,
        total_prev_col,
        qty_cur_col,
        qty_prev_col,
        unit_cur_col,
        unit_prev_col,
    ):
        if col not in work.columns:
            work[col] = 0.0
        work[col] = pd.to_numeric(work[col], errors="coerce").fillna(0.0)

    work["qty_avg"] = (work[qty_cur_col] + work[qty_prev_col]) / 2.0
    work["unit_avg"] = (work[unit_cur_col] + work[unit_prev_col]) / 2.0
    work["price_contrib"] = (work[unit_cur_col] - work[unit_prev_col]) * work["qty_avg"]
    work["volume_contrib"] = (work[qty_cur_col] - work[qty_prev_col]) * work["unit_avg"]
    work["total_delta_contrib"] = work[total_cur_col] - work[total_prev_col]
    work["mix_contrib"] = work["total_delta_contrib"] - work["price_contrib"] - work["volume_contrib"]

    def _series_nansum(series: pd.Series) -> float:
        arr = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float, copy=False)
        if arr.size == 0:
            return 0.0
        return float(np.nansum(arr))

    current_total = _series_nansum(work[total_cur_col])
    previous_total = _series_nansum(work[total_prev_col])
    total_delta = _series_nansum(work["total_delta_contrib"])
    price_effect = _series_nansum(work["price_contrib"])
    volume_effect = _series_nansum(work["volume_contrib"])
    mix_effect = float(total_delta - price_effect - volume_effect)
    sum_effects = float(price_effect + volume_effect + mix_effect)
    residual = float(sum_effects - total_delta)
    within_tolerance = abs(residual) <= tolerance

    if not within_tolerance:
        try:
            current_app.logger.warning(
                "overview.driver_decomp.reconcile",
                extra={
                    "metric": metric,
                    "period": period_label,
                    "residual": residual,
                    "tolerance": tolerance,
                    "request_id": _request_id(),
                },
            )
        except Exception:
            pass

    delta_pct: Optional[float] = None
    if abs(previous_total) > 1e-9:
        delta_pct = (total_delta / abs(previous_total)) * 100.0

    driver_rows = []
    for label, key, effect in (
        ("Price", "price_effect", price_effect),
        ("Volume", "volume_effect", volume_effect),
        ("Mix", "mix_effect", mix_effect),
    ):
        driver_rows.append(
            {
                "driver": label,
                "key": key,
                "delta": effect,
                "share_of_delta_pct": _driver_share(effect, total_delta),
                "direction": _driver_direction(effect),
            }
        )

    block: Dict[str, Any] = {
        "current": current_total,
        "previous": previous_total,
        "delta": total_delta,
        "delta_pct": delta_pct,
        "delta_pct_na_reason": "no prior-period value" if delta_pct is None else None,
        "price_effect": price_effect,
        "volume_effect": volume_effect,
        "mix_effect": mix_effect,
        "unit_label": unit_label,
        "drivers": driver_rows,
        "top_contributors": {
            "price_effect": _build_driver_contributors(
                work,
                contribution_col="price_contrib",
                unit_cur_col=unit_cur_col,
                unit_prev_col=unit_prev_col,
                total_cur_col=total_cur_col,
                total_prev_col=total_prev_col,
                qty_cur_col=qty_cur_col,
                qty_prev_col=qty_prev_col,
                top_n=top_n,
            ),
            "volume_effect": _build_driver_contributors(
                work,
                contribution_col="volume_contrib",
                unit_cur_col=unit_cur_col,
                unit_prev_col=unit_prev_col,
                total_cur_col=total_cur_col,
                total_prev_col=total_prev_col,
                qty_cur_col=qty_cur_col,
                qty_prev_col=qty_prev_col,
                top_n=top_n,
            ),
            "mix_effect": _build_driver_contributors(
                work,
                contribution_col="mix_contrib",
                unit_cur_col=unit_cur_col,
                unit_prev_col=unit_prev_col,
                total_cur_col=total_cur_col,
                total_prev_col=total_prev_col,
                qty_cur_col=qty_cur_col,
                qty_prev_col=qty_prev_col,
                top_n=top_n,
            ),
        },
        "reconciliation": {
            "total_delta": total_delta,
            "sum_effects": sum_effects,
            "residual": residual,
            "tolerance": tolerance,
            "within_tolerance": within_tolerance,
        },
    }
    block["insight"] = _driver_insight(period_label, metric.title(), block)
    return block


def _driver_pair_frame(
    grouped: pd.DataFrame,
    *,
    prior_bucket: str,
) -> pd.DataFrame:
    cols = [
        "sku_key",
        "sku_label",
        "revenue",
        "qty",
        "revenue_with_cost",
        "cost",
        "qty_with_cost",
    ]
    current = grouped[grouped["bucket"] == "current"][cols].copy()
    prior = grouped[grouped["bucket"] == prior_bucket][cols].copy()
    if current.empty:
        current = pd.DataFrame(columns=cols)
    if prior.empty:
        prior = pd.DataFrame(columns=cols)

    current = current.rename(
        columns={
            "sku_label": "sku_label_cur",
            "revenue": "revenue_cur",
            "qty": "qty_cur",
            "revenue_with_cost": "revenue_with_cost_cur",
            "cost": "cost_cur",
            "qty_with_cost": "qty_with_cost_cur",
        }
    )
    prior = prior.rename(
        columns={
            "sku_label": "sku_label_prev",
            "revenue": "revenue_prev",
            "qty": "qty_prev",
            "revenue_with_cost": "revenue_with_cost_prev",
            "cost": "cost_prev",
            "qty_with_cost": "qty_with_cost_prev",
        }
    )

    merged = current.merge(prior, on="sku_key", how="outer")
    if merged.empty:
        merged = pd.DataFrame(
            columns=[
                "sku_key",
                "sku_label",
                "revenue_cur",
                "revenue_prev",
                "qty_cur",
                "qty_prev",
                "revenue_with_cost_cur",
                "revenue_with_cost_prev",
                "cost_cur",
                "cost_prev",
                "qty_with_cost_cur",
                "qty_with_cost_prev",
                "price_cur",
                "price_prev",
                "margin_cur",
                "margin_prev",
                "profit_cur",
                "profit_prev",
            ]
        )
        return merged

    merged["sku_label"] = (
        merged.get("sku_label_cur", pd.Series(dtype=object))
        .fillna(merged.get("sku_label_prev", pd.Series(dtype=object)))
        .fillna("Unknown")
        .astype(str)
    )

    for col in (
        "revenue_cur",
        "revenue_prev",
        "qty_cur",
        "qty_prev",
        "revenue_with_cost_cur",
        "revenue_with_cost_prev",
        "cost_cur",
        "cost_prev",
        "qty_with_cost_cur",
        "qty_with_cost_prev",
    ):
        if col not in merged.columns:
            merged[col] = 0.0
        merged[col] = pd.to_numeric(merged[col], errors="coerce").fillna(0.0)

    merged["price_cur"] = np.where(merged["qty_cur"] > 0, merged["revenue_cur"] / merged["qty_cur"], 0.0)
    merged["price_prev"] = np.where(merged["qty_prev"] > 0, merged["revenue_prev"] / merged["qty_prev"], 0.0)
    merged["profit_cur"] = merged["revenue_with_cost_cur"] - merged["cost_cur"]
    merged["profit_prev"] = merged["revenue_with_cost_prev"] - merged["cost_prev"]
    merged["margin_cur"] = np.where(merged["qty_with_cost_cur"] > 0, merged["profit_cur"] / merged["qty_with_cost_cur"], 0.0)
    merged["margin_prev"] = np.where(merged["qty_with_cost_prev"] > 0, merged["profit_prev"] / merged["qty_with_cost_prev"], 0.0)
    return merged


def _compute_driver_decomposition_v2(
    conn,
    *,
    where_sql: str,
    where_params: List[Any],
    date_expr: str,
    revenue_expr: str,
    cost_raw_expr: str,
    qty_expr: str,
    product_id_expr: str,
    product_name_expr: str,
    window_contract: om.WindowContract,
    cost_available: bool,
    cost_coverage_pct: Optional[float],
) -> Dict[str, Any]:
    primary_delta_label = str(window_contract.delta_short_label or "Primary comparison")
    primary_compare_label = str(window_contract.prior_label or "Prior comparable window")
    primary_detail_label = primary_delta_label if primary_delta_label not in {"Prior window", "Current window"} else "Primary comparison"
    effective_cost_expr = margin_rules.sql_effective_cost_expr("cost_raw", None, "qty", fallback="NULL::DOUBLE")
    sql = f"""
        WITH scoped_base AS (
            SELECT
                {date_expr} AS order_date,
                {revenue_expr} AS revenue,
                {cost_raw_expr} AS cost_raw,
                {qty_expr} AS qty,
                {product_id_expr} AS product_id,
                {product_name_expr} AS product_name
            FROM fact
            WHERE {where_sql}
        ),
        scoped AS (
            SELECT
                order_date,
                revenue,
                ({effective_cost_expr}) AS cost,
                cost_raw,
                qty,
                COALESCE(NULLIF(product_id, ''), NULLIF(product_name, ''), 'Unknown') AS sku_key,
                COALESCE(NULLIF(product_name, ''), NULLIF(product_id, ''), 'Unknown') AS sku_label
            FROM scoped_base
        ),
        bucketed AS (
            SELECT
                CASE
                    WHEN order_date >= CAST(? AS DATE) AND order_date < CAST(? AS DATE) THEN 'current'
                    WHEN order_date >= CAST(? AS DATE) AND order_date < CAST(? AS DATE) THEN 'mom_prior'
                    WHEN order_date >= CAST(? AS DATE) AND order_date < CAST(? AS DATE) THEN 'yoy_prior'
                    ELSE NULL
                END AS bucket,
                sku_key,
                sku_label,
                revenue,
                cost,
                cost_raw,
                qty
            FROM scoped
            WHERE order_date IS NOT NULL
        )
        SELECT
            bucket,
            sku_key,
            MAX(sku_label) AS sku_label,
            SUM(revenue) AS revenue,
            SUM(qty) AS qty,
            SUM(CASE WHEN cost_raw IS NOT NULL THEN revenue ELSE 0 END) AS revenue_with_cost,
            SUM(CASE WHEN cost_raw IS NOT NULL THEN cost ELSE 0 END) AS cost,
            SUM(CASE WHEN cost_raw IS NOT NULL THEN qty ELSE 0 END) AS qty_with_cost
        FROM bucketed
        WHERE bucket IS NOT NULL
        GROUP BY 1, 2
    """

    params = list(where_params)
    params.extend(
        [
            window_contract.current_start.isoformat(),
            window_contract.current_end_exclusive.isoformat(),
            window_contract.prior_month_start.isoformat(),
            (window_contract.prior_month_end + timedelta(days=1)).isoformat(),
            window_contract.prior_year_start.isoformat(),
            (window_contract.prior_year_end + timedelta(days=1)).isoformat(),
        ]
    )
    grouped = _exec_df(sql, params, "driver_decomp_v2", conn)
    if grouped.empty:
        return {
            "enabled": True,
            "schema_version": _DRIVER_DECOMP_REV,
            "metric_default": "revenue",
            "methodology": {
                "name": "Symmetric Price-Volume-Mix",
                "grain": "SKU",
                "definitions": {
                    "price": "Impact from unit-value change at average quantity.",
                    "volume": "Impact from quantity change at average unit value.",
                    "mix": "Residual to reconcile total change exactly.",
                },
                "formulas": [],
            },
            "coverage": {"cost_pct": cost_coverage_pct, "cost_available": bool(cost_available)},
            "mom": {
                "key": "mom",
                "label": primary_detail_label,
                "comparison_label": primary_compare_label,
                "message": f"Not enough history to compute {primary_compare_label.lower()} drivers.",
                "revenue": {},
                "profit": {},
            },
            "yoy": {
                "key": "yoy",
                "label": "YoY",
                "comparison_label": "Same period last year",
                "message": "Not enough history to compute year-over-year drivers.",
                "revenue": {},
                "profit": {},
            },
        }

    for col in ("revenue", "qty", "revenue_with_cost", "cost", "qty_with_cost"):
        grouped[col] = pd.to_numeric(grouped[col], errors="coerce").fillna(0.0)

    profit_metric_available = bool(cost_available) and (cost_coverage_pct is None or float(cost_coverage_pct) >= 80.0)

    def _period_block(
        period_key: str,
        period_label: str,
        prior_bucket: str,
        prior_label: str,
        prior_start: date,
        prior_end: date,
    ) -> Dict[str, Any]:
        pair = _driver_pair_frame(grouped, prior_bucket=prior_bucket)
        revenue_block = _driver_metric_block(
            pair,
            metric="revenue",
            period_label=period_label,
            tolerance=_DRIVER_DECOMP_TOLERANCE,
            top_n=_DRIVER_DECOMP_TOP_N,
            metric_available=True,
        )
        profit_block = _driver_metric_block(
            pair,
            metric="profit",
            period_label=period_label,
            tolerance=_DRIVER_DECOMP_TOLERANCE,
            top_n=_DRIVER_DECOMP_TOP_N,
            metric_available=profit_metric_available,
        )
        block = {
            "key": period_key,
            "label": period_label,
            "comparison_label": prior_label,
            "window": {
                "current_start": window_contract.current_start.isoformat(),
                "current_end": window_contract.current_end.isoformat(),
                "prior_start": prior_start.isoformat(),
                "prior_end": prior_end.isoformat(),
            },
            "revenue": revenue_block,
            "profit": profit_block,
        }
        block["message"] = revenue_block.get("insight") or ""
        return block

    mom_block = _period_block(
        "mom",
        primary_detail_label,
        "mom_prior",
        primary_compare_label,
        window_contract.prior_month_start,
        window_contract.prior_month_end,
    )
    yoy_block = _period_block(
        "yoy",
        "YoY",
        "yoy_prior",
        "Same period last year",
        window_contract.prior_year_start,
        window_contract.prior_year_end,
    )

    return {
        "enabled": True,
        "schema_version": _DRIVER_DECOMP_REV,
        "metric_default": "revenue",
        "methodology": {
            "name": "Symmetric Price-Volume-Mix",
            "grain": "SKU",
            "definitions": {
                "price": "Impact from unit-value change at average quantity.",
                "volume": "Impact from quantity change at average unit value.",
                "mix": "Residual term so Price + Volume + Mix = Total.",
            },
            "formulas": [
                "Price effect = Σ((Pcur - Pprev) * avg(Qcur, Qprev))",
                "Volume effect = Σ((Qcur - Qprev) * avg(Pcur, Pprev))",
                "Mix effect = Total delta - Price effect - Volume effect",
            ],
        },
        "coverage": {"cost_pct": cost_coverage_pct, "cost_available": bool(cost_available)},
        "mom": mom_block,
        "yoy": yoy_block,
    }


def _compute_window_comparison_context(
    conn,
    *,
    where_sql: str,
    where_params: List[Any],
    date_expr: str,
    revenue_expr: str,
    cost_raw_expr: str,
    qty_expr: str,
    weight_expr: str,
    order_expr: str,
    customer_id_expr: str,
    customer_name_expr: str,
    product_id_expr: str,
    product_name_expr: str,
    window_contract: om.WindowContract,
) -> Dict[str, Any]:
    preprior_end = window_contract.prior_month_start - timedelta(days=1)
    preprior_start = preprior_end - timedelta(days=max(1, window_contract.current_days) - 1)
    effective_cost_expr = margin_rules.sql_effective_cost_expr("cost_raw", "weight", "qty", fallback="NULL::DOUBLE")
    sql = f"""
        WITH scoped_base AS (
            SELECT
                {date_expr} AS order_date,
                {revenue_expr} AS revenue,
                {cost_raw_expr} AS cost_raw,
                {qty_expr} AS qty,
                {weight_expr} AS weight,
                {order_expr} AS order_id,
                {customer_id_expr} AS customer_id,
                {customer_name_expr} AS customer_name,
                {product_id_expr} AS product_id,
                {product_name_expr} AS product_name
            FROM fact
            WHERE {where_sql}
        ),
        scoped AS (
            SELECT
                order_date,
                revenue,
                ({effective_cost_expr}) AS cost,
                cost_raw,
                qty,
                weight,
                order_id,
                COALESCE(NULLIF(customer_id, ''), NULLIF(customer_name, ''), 'Unknown') AS customer_key,
                COALESCE(NULLIF(product_id, ''), NULLIF(product_name, ''), 'Unknown') AS product_key
            FROM scoped_base
            WHERE order_date IS NOT NULL
        ),
        current_window AS (
            SELECT * FROM scoped WHERE order_date >= CAST(? AS DATE) AND order_date < CAST(? AS DATE)
        ),
        prior_window AS (
            SELECT * FROM scoped WHERE order_date >= CAST(? AS DATE) AND order_date < CAST(? AS DATE)
        ),
        yoy_window AS (
            SELECT * FROM scoped WHERE order_date >= CAST(? AS DATE) AND order_date < CAST(? AS DATE)
        ),
        preprior_window AS (
            SELECT * FROM scoped WHERE order_date >= CAST(? AS DATE) AND order_date < CAST(? AS DATE)
        ),
        current_totals AS (
            SELECT
                SUM(revenue) AS revenue,
                SUM(CASE WHEN cost_raw IS NOT NULL THEN cost ELSE 0 END) AS cost,
                SUM(CASE WHEN cost_raw IS NOT NULL THEN revenue ELSE 0 END) AS revenue_with_cost,
                SUM(CASE WHEN cost_raw IS NOT NULL THEN qty ELSE 0 END) AS qty_with_cost,
                SUM(qty) AS qty,
                SUM(weight) AS weight,
                COUNT(DISTINCT order_id) AS orders,
                COUNT(DISTINCT customer_key) AS customers
            FROM current_window
        ),
        prior_totals AS (
            SELECT
                SUM(revenue) AS revenue,
                SUM(CASE WHEN cost_raw IS NOT NULL THEN cost ELSE 0 END) AS cost,
                SUM(CASE WHEN cost_raw IS NOT NULL THEN revenue ELSE 0 END) AS revenue_with_cost,
                SUM(CASE WHEN cost_raw IS NOT NULL THEN qty ELSE 0 END) AS qty_with_cost,
                SUM(qty) AS qty,
                SUM(weight) AS weight,
                COUNT(DISTINCT order_id) AS orders,
                COUNT(DISTINCT customer_key) AS customers
            FROM prior_window
        ),
        yoy_totals AS (
            SELECT
                SUM(revenue) AS revenue,
                SUM(CASE WHEN cost_raw IS NOT NULL THEN cost ELSE 0 END) AS cost,
                SUM(CASE WHEN cost_raw IS NOT NULL THEN revenue ELSE 0 END) AS revenue_with_cost,
                SUM(CASE WHEN cost_raw IS NOT NULL THEN qty ELSE 0 END) AS qty_with_cost,
                SUM(qty) AS qty,
                SUM(weight) AS weight,
                COUNT(DISTINCT order_id) AS orders,
                COUNT(DISTINCT customer_key) AS customers
            FROM yoy_window
        ),
        customer_curr AS (SELECT DISTINCT customer_key FROM current_window),
        customer_prev AS (SELECT DISTINCT customer_key FROM prior_window),
        customer_preprev AS (SELECT DISTINCT customer_key FROM preprior_window),
        customer_stats AS (
            SELECT
                (SELECT COUNT(*) FROM customer_curr) AS customers_current,
                (SELECT COUNT(*) FROM customer_prev) AS customers_prev,
                (SELECT COUNT(*) FROM customer_curr WHERE customer_key NOT IN (SELECT customer_key FROM customer_prev)) AS new_customers,
                (SELECT COUNT(*) FROM customer_curr WHERE customer_key IN (SELECT customer_key FROM customer_prev)) AS returning_customers,
                (SELECT COUNT(*) FROM customer_prev WHERE customer_key NOT IN (SELECT customer_key FROM customer_preprev)) AS new_customers_prev,
                (SELECT COUNT(*) FROM customer_prev WHERE customer_key IN (SELECT customer_key FROM customer_preprev)) AS returning_customers_prev
        ),
        activity AS (
            SELECT
                (SELECT COUNT(DISTINCT product_key) FROM current_window) AS active_skus,
                (SELECT COUNT(DISTINCT product_key) FROM prior_window) AS active_skus_prev,
                (SELECT COUNT(DISTINCT customer_key) FROM current_window) AS active_customers,
                (SELECT COUNT(DISTINCT customer_key) FROM prior_window) AS active_customers_prev
        )
        SELECT
            current_totals.revenue AS revenue_current,
            current_totals.cost AS cost_current,
            current_totals.revenue_with_cost AS revenue_with_cost_current,
            current_totals.qty AS qty_current,
            current_totals.qty_with_cost AS qty_with_cost_current,
            current_totals.weight AS weight_current,
            current_totals.orders AS orders_current,
            current_totals.customers AS customers_current_total,
            prior_totals.revenue AS revenue_prior,
            prior_totals.cost AS cost_prior,
            prior_totals.revenue_with_cost AS revenue_with_cost_prior,
            prior_totals.qty AS qty_prior,
            prior_totals.qty_with_cost AS qty_with_cost_prior,
            prior_totals.weight AS weight_prior,
            prior_totals.orders AS orders_prior,
            prior_totals.customers AS customers_prior_total,
            yoy_totals.revenue AS revenue_yoy,
            yoy_totals.cost AS cost_yoy,
            yoy_totals.revenue_with_cost AS revenue_with_cost_yoy,
            yoy_totals.qty AS qty_yoy,
            yoy_totals.qty_with_cost AS qty_with_cost_yoy,
            yoy_totals.weight AS weight_yoy,
            yoy_totals.orders AS orders_yoy,
            yoy_totals.customers AS customers_yoy_total,
            customer_stats.*,
            activity.*
        FROM current_totals
        CROSS JOIN prior_totals
        CROSS JOIN yoy_totals
        CROSS JOIN customer_stats
        CROSS JOIN activity
    """
    params = list(where_params)
    params.extend(
        [
            window_contract.current_start.isoformat(),
            window_contract.current_end_exclusive.isoformat(),
            window_contract.prior_month_start.isoformat(),
            (window_contract.prior_month_end + timedelta(days=1)).isoformat(),
            window_contract.prior_year_start.isoformat(),
            (window_contract.prior_year_end + timedelta(days=1)).isoformat(),
            preprior_start.isoformat(),
            (preprior_end + timedelta(days=1)).isoformat(),
        ]
    )
    df = _exec_df(sql, params, "overview_window_comparison", conn)
    row = df.iloc[0].to_dict() if not df.empty else {}

    current_metrics = _window_metric_summary(
        revenue=_clean_optional_float(row.get("revenue_current")),
        cost=_clean_optional_float(row.get("cost_current")),
        revenue_with_cost=_clean_optional_float(row.get("revenue_with_cost_current")),
        qty=_clean_optional_float(row.get("qty_current")),
        weight=_clean_optional_float(row.get("weight_current")),
        orders=int(row.get("orders_current") or 0),
        customers=int(row.get("customers_current_total") or 0),
    )
    current_metrics["qty_with_cost"] = _clean_optional_float(row.get("qty_with_cost_current"))
    current_metrics["asp_cost_basis"] = (
        au.safe_div(current_metrics.get("revenue_with_cost"), current_metrics.get("qty_with_cost"))
        if current_metrics.get("qty_with_cost")
        else None
    )

    prior_metrics = _window_metric_summary(
        revenue=_clean_optional_float(row.get("revenue_prior")),
        cost=_clean_optional_float(row.get("cost_prior")),
        revenue_with_cost=_clean_optional_float(row.get("revenue_with_cost_prior")),
        qty=_clean_optional_float(row.get("qty_prior")),
        weight=_clean_optional_float(row.get("weight_prior")),
        orders=int(row.get("orders_prior") or 0),
        customers=int(row.get("customers_prior_total") or 0),
    )
    prior_metrics["qty_with_cost"] = _clean_optional_float(row.get("qty_with_cost_prior"))

    yoy_metrics = _window_metric_summary(
        revenue=_clean_optional_float(row.get("revenue_yoy")),
        cost=_clean_optional_float(row.get("cost_yoy")),
        revenue_with_cost=_clean_optional_float(row.get("revenue_with_cost_yoy")),
        qty=_clean_optional_float(row.get("qty_yoy")),
        weight=_clean_optional_float(row.get("weight_yoy")),
        orders=int(row.get("orders_yoy") or 0),
        customers=int(row.get("customers_yoy_total") or 0),
    )
    yoy_metrics["qty_with_cost"] = _clean_optional_float(row.get("qty_with_cost_yoy"))

    operations = {
        "customers": {
            "current": int(row.get("customers_current") or 0),
            "previous": int(row.get("customers_prev") or 0),
            "new": int(row.get("new_customers") or 0),
            "returning": int(row.get("returning_customers") or 0),
            "new_prev": int(row.get("new_customers_prev") or 0),
            "returning_prev": int(row.get("returning_customers_prev") or 0),
        },
        "activity": {
            "active_skus": int(row.get("active_skus") or 0),
            "active_skus_prev": int(row.get("active_skus_prev") or 0),
            "active_customers": int(row.get("active_customers") or 0),
            "active_customers_prev": int(row.get("active_customers_prev") or 0),
        },
    }
    return {
        "current_metrics": current_metrics,
        "prior_metrics": prior_metrics,
        "yoy_metrics": yoy_metrics,
        "operations": operations,
    }


def _compute_window_movers(
    conn,
    *,
    where_sql: str,
    where_params: List[Any],
    date_expr: str,
    revenue_expr: str,
    qty_expr: str,
    label_expr: str,
    entity_expr: str,
    dataset_name: str,
    window_contract: om.WindowContract,
) -> List[Dict[str, Any]]:
    sql = f"""
        WITH scoped AS (
            SELECT
                {date_expr} AS order_date,
                {revenue_expr} AS revenue,
                {qty_expr} AS qty,
                {label_expr} AS label,
                {entity_expr} AS entity_id
            FROM fact
            WHERE {where_sql}
        ),
        current_rollup AS (
            SELECT
                COALESCE(NULLIF(label, ''), NULLIF(entity_id, ''), 'Unknown') AS label,
                COALESCE(NULLIF(entity_id, ''), NULLIF(label, ''), 'Unknown') AS entity_id,
                SUM(revenue) AS current,
                SUM(qty) AS qty_current
            FROM scoped
            WHERE order_date >= CAST(? AS DATE) AND order_date < CAST(? AS DATE)
            GROUP BY 1, 2
        ),
        prior_rollup AS (
            SELECT
                COALESCE(NULLIF(label, ''), NULLIF(entity_id, ''), 'Unknown') AS label,
                COALESCE(NULLIF(entity_id, ''), NULLIF(label, ''), 'Unknown') AS entity_id,
                SUM(revenue) AS previous,
                SUM(qty) AS qty_previous
            FROM scoped
            WHERE order_date >= CAST(? AS DATE) AND order_date < CAST(? AS DATE)
            GROUP BY 1, 2
        )
        SELECT
            COALESCE(c.label, p.label, 'Unknown') AS label,
            COALESCE(c.entity_id, p.entity_id, COALESCE(c.label, p.label, 'Unknown')) AS entity_id,
            COALESCE(c.current, 0.0) AS current,
            COALESCE(p.previous, 0.0) AS previous,
            COALESCE(c.current, 0.0) - COALESCE(p.previous, 0.0) AS delta,
            COALESCE(c.qty_current, 0.0) AS qty_current,
            COALESCE(p.qty_previous, 0.0) AS qty_previous,
            COALESCE(c.qty_current, 0.0) - COALESCE(p.qty_previous, 0.0) AS qty_delta,
            CASE WHEN COALESCE(c.qty_current, 0.0) > 0 THEN COALESCE(c.current, 0.0) / COALESCE(c.qty_current, 0.0) ELSE NULL END AS asp_current,
            CASE WHEN COALESCE(p.qty_previous, 0.0) > 0 THEN COALESCE(p.previous, 0.0) / COALESCE(p.qty_previous, 0.0) ELSE NULL END AS asp_previous,
            CASE
                WHEN COALESCE(c.qty_current, 0.0) > 0 AND COALESCE(p.qty_previous, 0.0) > 0
                    THEN COALESCE(c.current, 0.0) / COALESCE(c.qty_current, 0.0) - COALESCE(p.previous, 0.0) / COALESCE(p.qty_previous, 0.0)
                ELSE NULL
            END AS asp_delta
        FROM current_rollup c
        FULL OUTER JOIN prior_rollup p
            ON c.entity_id = p.entity_id
        WHERE ABS(COALESCE(c.current, 0.0)) > 0 OR ABS(COALESCE(p.previous, 0.0)) > 0
        ORDER BY ABS(COALESCE(c.current, 0.0) - COALESCE(p.previous, 0.0)) DESC, label
    """
    params = list(where_params)
    params.extend(
        [
            window_contract.current_start.isoformat(),
            window_contract.current_end_exclusive.isoformat(),
            window_contract.prior_month_start.isoformat(),
            (window_contract.prior_month_end + timedelta(days=1)).isoformat(),
        ]
    )
    frame = _exec_df(sql, params, dataset_name, conn)
    if frame.empty:
        return []
    return _normalize_mover_rows(frame.to_dict(orient="records"))


def _window_top_movers(
    conn,
    *,
    where_sql: str,
    where_params: List[Any],
    date_expr: str,
    revenue_expr: str,
    qty_expr: str,
    customer_id_expr: str,
    customer_name_expr: str,
    product_id_expr: str,
    product_name_expr: str,
    region_expr: str,
    window_contract: om.WindowContract,
) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    customer_rows = _compute_window_movers(
        conn,
        where_sql=where_sql,
        where_params=where_params,
        date_expr=date_expr,
        revenue_expr=revenue_expr,
        qty_expr=qty_expr,
        label_expr=f"COALESCE(NULLIF({customer_name_expr}, ''), NULLIF({customer_id_expr}, ''), 'Unknown')",
        entity_expr=f"COALESCE(NULLIF({customer_id_expr}, ''), NULLIF({customer_name_expr}, ''), 'Unknown')",
        dataset_name="overview_customer_movers",
        window_contract=window_contract,
    )
    product_rows = _compute_window_movers(
        conn,
        where_sql=where_sql,
        where_params=where_params,
        date_expr=date_expr,
        revenue_expr=revenue_expr,
        qty_expr=qty_expr,
        label_expr=f"COALESCE(NULLIF({product_name_expr}, ''), NULLIF({product_id_expr}, ''), 'Unknown')",
        entity_expr=f"COALESCE(NULLIF({product_id_expr}, ''), NULLIF({product_name_expr}, ''), 'Unknown')",
        dataset_name="overview_product_movers",
        window_contract=window_contract,
    )
    region_rows = _compute_window_movers(
        conn,
        where_sql=where_sql,
        where_params=where_params,
        date_expr=date_expr,
        revenue_expr=revenue_expr,
        qty_expr=qty_expr,
        label_expr=f"COALESCE(NULLIF({region_expr}, ''), 'Unknown')",
        entity_expr=f"COALESCE(NULLIF({region_expr}, ''), 'Unknown')",
        dataset_name="overview_region_movers",
        window_contract=window_contract,
    )

    def _bucket(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        gainers = [row for row in rows if float(row.get("delta") or 0.0) >= 0]
        decliners = [row for row in rows if float(row.get("delta") or 0.0) < 0]
        gainers.sort(key=lambda rec: float(rec.get("delta") or 0.0), reverse=True)
        decliners.sort(key=lambda rec: float(rec.get("delta") or 0.0))
        return {"gainers": gainers[:10], "decliners": decliners[:10]}

    return {
        "customer": _bucket(customer_rows),
        "product": _bucket(product_rows),
        "region": _bucket(region_rows),
    }


def _compute_window_margin_risk(
    conn,
    *,
    where_sql: str,
    where_params: List[Any],
    date_expr: str,
    revenue_expr: str,
    cost_raw_expr: str,
    qty_expr: str,
    weight_expr: str,
    product_id_expr: str,
    product_name_expr: str,
    supplier_expr: str,
    protein_expr: str,
    window_contract: om.WindowContract,
) -> Dict[str, Any]:
    effective_cost_expr = margin_rules.sql_effective_cost_expr("cost_raw", "weight", "qty", fallback="NULL::DOUBLE")
    sql = f"""
        WITH scoped AS (
            SELECT
                {date_expr} AS order_date,
                {revenue_expr} AS revenue,
                {cost_raw_expr} AS cost_raw,
                {qty_expr} AS qty,
                {weight_expr} AS weight,
                ({effective_cost_expr}) AS cost,
                {product_id_expr} AS product_id,
                {product_name_expr} AS product_name,
                {supplier_expr} AS supplier,
                {protein_expr} AS protein
            FROM fact
            WHERE {where_sql}
        ),
        current_rollup AS (
            SELECT
                COALESCE(NULLIF(product_id, ''), NULLIF(product_name, ''), 'Unknown') AS entity_id,
                COALESCE(NULLIF(product_name, ''), NULLIF(product_id, ''), 'Unknown') AS label,
                COALESCE(NULLIF(supplier, ''), 'Unknown') AS supplier,
                COALESCE(NULLIF(protein, ''), 'Unknown') AS protein,
                SUM(CASE WHEN cost_raw IS NOT NULL THEN revenue ELSE 0 END) AS revenue_with_cost,
                SUM(CASE WHEN cost_raw IS NOT NULL THEN cost ELSE 0 END) AS cost,
                SUM(CASE WHEN cost_raw IS NOT NULL THEN qty ELSE 0 END) AS qty,
                SUM(CASE WHEN cost_raw IS NOT NULL THEN weight ELSE 0 END) AS weight
            FROM scoped
            WHERE order_date >= CAST(? AS DATE) AND order_date < CAST(? AS DATE)
            GROUP BY 1, 2, 3, 4
        ),
        prior_rollup AS (
            SELECT
                COALESCE(NULLIF(product_id, ''), NULLIF(product_name, ''), 'Unknown') AS entity_id,
                SUM(CASE WHEN cost_raw IS NOT NULL THEN revenue ELSE 0 END) AS revenue_with_cost_prev,
                SUM(CASE WHEN cost_raw IS NOT NULL THEN cost ELSE 0 END) AS cost_prev
            FROM scoped
            WHERE order_date >= CAST(? AS DATE) AND order_date < CAST(? AS DATE)
            GROUP BY 1
        ),
        combined AS (
            SELECT
                c.entity_id,
                c.label,
                c.supplier,
                c.protein,
                c.revenue_with_cost AS revenue,
                c.cost,
                c.qty,
                c.weight,
                p.revenue_with_cost_prev AS revenue_prev,
                p.cost_prev
            FROM current_rollup c
            LEFT JOIN prior_rollup p
                ON c.entity_id = p.entity_id
        ),
        scored AS (
            SELECT
                *,
                CASE WHEN revenue > 0 THEN (revenue - cost) / revenue * 100 ELSE NULL END AS margin_pct,
                CASE WHEN revenue_prev > 0 THEN (revenue_prev - cost_prev) / revenue_prev * 100 ELSE NULL END AS margin_prev,
                CASE WHEN SUM(revenue) OVER () > 0 THEN revenue / SUM(revenue) OVER () * 100 ELSE 0 END AS revenue_share
            FROM combined
            WHERE revenue > 0
        )
        SELECT
            entity_id,
            label,
            supplier,
            protein,
            revenue,
            revenue_share,
            cost,
            margin_pct,
            margin_prev,
            qty,
            weight AS weight_lb,
            CASE WHEN margin_prev IS NOT NULL THEN margin_pct - margin_prev ELSE NULL END AS margin_delta,
            (revenue - cost) AS profit
        FROM scored
        WHERE margin_pct IS NOT NULL
        ORDER BY revenue DESC, entity_id
    """
    params = list(where_params)
    params.extend(
        [
            window_contract.current_start.isoformat(),
            window_contract.current_end_exclusive.isoformat(),
            window_contract.prior_month_start.isoformat(),
            (window_contract.prior_month_end + timedelta(days=1)).isoformat(),
        ]
    )
    frame = _exec_df(sql, params, "overview_margin_risk_windowed", conn)
    if frame.empty:
        return {"rows": [], "minimum_margin_pct": None, "target_margin_pct": None}
    frame["effective_cost_basis"] = pd.to_numeric(frame.get("cost"), errors="coerce")
    frame = margin_rules.annotate_margin_frame(
        frame,
        protein_col="protein",
        category_col="protein",
        revenue_col="revenue",
        cost_col="cost",
        profit_col="profit",
        margin_col="margin_pct",
    )
    weighted_minimum_margin_pct = margin_rules.weighted_minimum_margin_pct(frame.to_dict(orient="records"))
    weighted_target_margin_pct = margin_rules.weighted_target_margin_pct(frame.to_dict(orient="records"))
    frame["profit_impact"] = pd.to_numeric(frame.get("profit"), errors="coerce")
    frame["profit_lost_vs_target"] = pd.to_numeric(frame.get("profit_uplift_to_target"), errors="coerce")
    frame["risk"] = np.select(
        [
            frame["status_key"].isin(["red", "orange"]),
            frame["status_key"].eq("yellow"),
            pd.to_numeric(frame.get("margin_delta"), errors="coerce") < -5.0,
        ],
        [
            "below_minimum",
            "below_target",
            "margin_drop",
        ],
        default="",
    )
    frame = frame[
        frame["risk"].astype("string").fillna("").str.len() > 0
    ].copy()
    if frame.empty:
        return {
            "rows": [],
            "minimum_margin_pct": weighted_minimum_margin_pct,
            "target_margin_pct": weighted_target_margin_pct,
        }
    out = []
    for rec in frame.sort_values(["profit_lost_vs_target", "revenue"], ascending=[False, False]).to_dict(orient="records"):
        row = {key: rec[key] for key in MARGIN_RISK_PAYLOAD_FIELDS if key in rec}
        row["profit_impact"] = _clean_optional_float(rec.get("profit"))
        out.append(row)
    out.sort(key=lambda rec: float(rec.get("profit_impact") or 0.0))
    return {
        "rows": out,
        "minimum_margin_pct": weighted_minimum_margin_pct,
        "target_margin_pct": weighted_target_margin_pct,
    }


def _build_health_issues(
    *,
    cost_missing: int,
    pack_missing: int,
    product_missing: int,
    window_contract: om.WindowContract,
) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    if cost_missing > 0:
        issues.append(
            {
                "label": "Rows missing cost",
                "count": int(cost_missing),
                "detail": "Profit, margin, and finance-sensitive outputs are partially restricted until cost is backfilled.",
                "severity": "warning",
            }
        )
    if pack_missing > 0:
        issues.append(
            {
                "label": "Rows missing pack mapping",
                "count": int(pack_missing),
                "detail": "Weighted diagnostics such as lb-based metrics and ASP can drift when pack attributes are missing.",
                "severity": "warning",
            }
        )
    if product_missing > 0:
        issues.append(
            {
                "label": "Rows missing product mapping",
                "count": int(product_missing),
                "detail": "Unmapped product rows weaken movers, watchlists, and commercial mix readouts.",
                "severity": "warning",
            }
        )
    if window_contract.terminal_period_incomplete:
        issues.append(
            {
                "label": "Current period is incomplete",
                "count": 1,
                "detail": window_contract.note,
                "severity": "info",
            }
        )
    return issues


def get_bundle_context(filters: FilterParams, include_current_month: bool = False, defaulted_window: bool = False) -> Dict[str, Any]:
    """Primary entrypoint: build or fetch the aggregated overview bundle context using DuckDB."""
    cache_key = _bundle_cache_key(filters, include_current_month, defaulted_window)
    cached = _bundle_cache_get(cache_key)
    if cached:
        payload = cached.get("payload", {})
        payload.setdefault("meta", {}).update({"cache_hit": True, "cache_key": cache_key})
        return {"payload": payload, "monthly": cached.get("monthly"), "cache_hit": True, "cache_key": cache_key}

    lock = _bundle_lock_for(cache_key)
    with lock:
        cached = _bundle_cache_get(cache_key)
        if cached:
            payload = cached.get("payload", {})
            payload.setdefault("meta", {}).update({"cache_hit": True, "cache_key": cache_key})
            return {"payload": payload, "monthly": cached.get("monthly"), "cache_hit": True, "cache_key": cache_key}

        ctx = _compute_bundle_context(
            filters,
            include_current_month=include_current_month,
            defaulted_window=defaulted_window,
            cache_key=cache_key,
        )
        _bundle_cache_set(cache_key, ctx["payload"], ctx.get("monthly", pd.DataFrame()))
        payload = _clone_payload(ctx["payload"])
        payload.setdefault("meta", {})["cache_key"] = cache_key
        return {"payload": payload, "monthly": ctx.get("monthly"), "cache_hit": False, "cache_key": cache_key}


def _compute_bundle_context(
    filters: FilterParams,
    *,
    include_current_month: bool,
    defaulted_window: bool,
    cache_key: Optional[str] = None,
) -> Dict[str, Any]:
    conn = get_duck_conn()
    init_duck_views(conn)
    cols = duck_columns(conn)
    window_contract = om.resolve_window_contract(filters, include_current_month=include_current_month)
    expanded_filters = replace(
        filters,
        start=pd.Timestamp(window_contract.history_start),
        end=pd.Timestamp(window_contract.current_end),
    )
    where_sql, params, _start_iso, _end_iso, defaulted_dates = _where_clause(expanded_filters, cols, True)
    where_params = list(params)
    defaulted_flag = defaulted_window or defaulted_dates or bool(window_contract.defaulted)
    start_iso = window_contract.current_start.isoformat()
    end_iso = window_contract.current_end.isoformat()
    current_start_iso = window_contract.current_start.isoformat()
    current_end_excl_iso = window_contract.current_end_exclusive.isoformat()
    # Anchor month-based diagnostics to the inclusive terminal date, not the
    # exclusive bound, so completed month windows do not roll into the next month.
    period_anchor_iso = window_contract.current_end.isoformat()
    _prior_start_iso = window_contract.prior_month_start.isoformat()
    _prior_end_excl_iso = (window_contract.prior_month_end + timedelta(days=1)).isoformat()
    _yoy_start_iso = window_contract.prior_year_start.isoformat()
    _yoy_end_excl_iso = (window_contract.prior_year_end + timedelta(days=1)).isoformat()
    preprior_end = window_contract.prior_month_start - timedelta(days=1)
    preprior_start = preprior_end - timedelta(days=max(1, window_contract.current_days) - 1)
    _preprior_start_iso = preprior_start.isoformat()
    _preprior_end_excl_iso = (preprior_end + timedelta(days=1)).isoformat()

    date_col = _safe_col(cols, "Date", "ShipDate", "OrderDate")
    revenue_col = _safe_col(cols, "Revenue", "TotalRevenue", "Sales")
    cost_candidates = ["Cost", "CostPrice", "TotalCost"]
    order_col = _safe_col(cols, "OrderId", "OrderID")
    customer_id_col = _safe_col(cols, "CustomerId", "CustomerID")
    customer_name_col = _safe_col(cols, "CustomerName", "Customer")
    product_id_col = _safe_col(cols, "ProductId", "ProductID", "SKU")
    product_name_col = _safe_col(cols, "ProductName", "Product", "Description")
    region_col = _safe_col(cols, "RegionName", "Region")
    ship_col = _safe_col(cols, "ShippingMethodName", "ShippingMethodLabel", "ShipMethod_Name")
    supplier_col = _safe_col(cols, "SupplierName", "SupplierId")
    protein_col = _safe_col(cols, "Protein", "ProteinType", "ProteinName", "Category", "ProductCategory")
    pack_item_col = _safe_col(cols, "pack_item_count_sum")
    pack_weight_col = _safe_col(cols, "pack_weight_lb_sum")
    cost_available = any(c in cols for c in cost_candidates)

    date_expr = _col_expr(date_col, "DATE", "NULL")
    revenue_expr = _col_expr(revenue_col, "DOUBLE", "0")
    cost_raw_expr = _coalesce_numeric_expr(cols, cost_candidates, "NULL::DOUBLE")
    qty_expr = _coalesce_numeric_expr(
        cols,
        ["QuantityShipped", "ShippedItems", "QuantityOrdered", "Qty", "Quantity", "Units", "ItemCount", "pack_item_count_sum"],
        "0::DOUBLE",
    )
    weight_expr = _coalesce_numeric_expr(cols, ["WeightLb", "Weight", "ShippedLb", "pack_weight_lb_sum"], "0::DOUBLE")
    order_expr = _col_expr(order_col, "VARCHAR", "NULL")
    customer_id_expr = _col_expr(customer_id_col, "VARCHAR", "NULL")
    customer_name_expr = _col_expr(customer_name_col, "VARCHAR", "NULL")
    product_id_expr = _col_expr(product_id_col, "VARCHAR", "NULL")
    product_name_expr = _col_expr(product_name_col, "VARCHAR", "NULL")
    region_expr = _col_expr(region_col, "VARCHAR", "NULL")
    ship_expr = _col_expr(ship_col, "VARCHAR", "NULL")
    supplier_expr = _col_expr(supplier_col, "VARCHAR", "NULL")
    protein_expr = _col_expr(protein_col, "VARCHAR", "NULL")
    pack_item_expr = _col_expr(pack_item_col, "DOUBLE", "NULL")
    pack_weight_expr = _col_expr(pack_weight_col, "DOUBLE", "NULL")
    effective_cost_expr = margin_rules.sql_effective_cost_expr("cost_raw", "weight", "qty", fallback="NULL::DOUBLE")

    sql = f"""
        WITH scoped_base AS (
            SELECT
                {date_expr} AS order_date,
                {revenue_expr} AS revenue,
                {cost_raw_expr} AS cost_raw,
                {qty_expr} AS qty,
                {weight_expr} AS weight,
                {order_expr} AS order_id,
                {customer_id_expr} AS customer_id,
                {customer_name_expr} AS customer_name,
                {product_id_expr} AS product_id,
                {product_name_expr} AS product_name,
                {region_expr} AS region,
                {ship_expr} AS ship_method,
                {supplier_expr} AS supplier,
                {protein_expr} AS protein,
                {pack_item_expr} AS pack_item_count,
                {pack_weight_expr} AS pack_weight_lb
            FROM fact
            WHERE {where_sql}
        ),
        scoped AS (
            SELECT
                order_date,
                revenue,
                ({effective_cost_expr}) AS cost,
                cost_raw,
                qty,
                weight,
                order_id,
                customer_id,
                customer_name,
                product_id,
                product_name,
                region,
                ship_method,
                supplier,
                protein,
                pack_item_count,
                pack_weight_lb
            FROM scoped_base
        ),
        current_window AS (
            SELECT *
            FROM scoped
            WHERE order_date >= CAST(? AS DATE)
              AND order_date < CAST(? AS DATE)
        ),
        totals AS (
            SELECT
                SUM(revenue) AS revenue,
                SUM(CASE WHEN cost_raw IS NOT NULL THEN cost ELSE 0 END) AS cost,
                SUM(CASE WHEN cost_raw IS NOT NULL THEN revenue ELSE 0 END) AS revenue_with_cost,
                SUM(CASE WHEN cost_raw IS NOT NULL THEN qty ELSE 0 END) AS qty_with_cost,
                SUM(qty) AS qty,
                SUM(weight) AS weight,
                COUNT(DISTINCT order_id) AS orders,
                COUNT(DISTINCT customer_id) AS customers
            FROM current_window
        ),
        monthly_all AS (
            SELECT
                CAST(date_trunc('month', order_date) AS DATE) AS month,
                SUM(revenue) AS revenue,
                SUM(cost) AS cost,
                SUM(qty) AS qty,
                SUM(weight) AS weight,
                COUNT(DISTINCT order_id) AS orders,
                COUNT(DISTINCT customer_id) AS customers
            FROM scoped
            WHERE order_date IS NOT NULL
            GROUP BY 1
        ),
        monthly AS (
            SELECT
                CAST(date_trunc('month', order_date) AS DATE) AS month,
                SUM(revenue) AS revenue,
                SUM(cost) AS cost,
                SUM(qty) AS qty,
                SUM(weight) AS weight,
                COUNT(DISTINCT order_id) AS orders,
                COUNT(DISTINCT customer_id) AS customers
            FROM current_window
            WHERE order_date IS NOT NULL
            GROUP BY 1
        ),
        periods AS (
            SELECT
                CAST(date_trunc('month', CAST(? AS DATE)) AS DATE) AS curr_month,
                CAST(date_trunc('month', CAST(? AS DATE) - INTERVAL '1 month') AS DATE) AS prev_month,
                CAST(date_trunc('month', CAST(? AS DATE) - INTERVAL '12 month') AS DATE) AS yoy_month,
                CAST(date_trunc('month', CAST(? AS DATE) - INTERVAL '2 month') AS DATE) AS prev2_month
        ),
        period_pivot AS (
            SELECT
                MAX(CASE WHEN m.month = p.curr_month THEN m.revenue END) AS rev_curr,
                MAX(CASE WHEN m.month = p.prev_month THEN m.revenue END) AS rev_prev,
                MAX(CASE WHEN m.month = p.yoy_month THEN m.revenue END) AS rev_yoy,
                MAX(CASE WHEN m.month = p.curr_month THEN m.cost END) AS cost_curr,
                MAX(CASE WHEN m.month = p.prev_month THEN m.cost END) AS cost_prev,
                MAX(CASE WHEN m.month = p.yoy_month THEN m.cost END) AS cost_yoy,
                MAX(CASE WHEN m.month = p.curr_month THEN m.qty END) AS qty_curr,
                MAX(CASE WHEN m.month = p.prev_month THEN m.qty END) AS qty_prev,
                MAX(CASE WHEN m.month = p.yoy_month THEN m.qty END) AS qty_yoy
            FROM monthly_all m
            CROSS JOIN periods p
        ),
        drivers AS (
            SELECT
                rev_curr, rev_prev, rev_yoy,
                cost_curr, cost_prev, cost_yoy,
                qty_curr, qty_prev, qty_yoy,
                CASE WHEN qty_curr > 0 THEN rev_curr / qty_curr ELSE NULL END AS price_curr,
                CASE WHEN qty_prev > 0 THEN rev_prev / qty_prev ELSE NULL END AS price_prev,
                CASE WHEN qty_yoy > 0 THEN rev_yoy / qty_yoy ELSE NULL END AS price_yoy,
                CASE WHEN qty_curr > 0 THEN (rev_curr - cost_curr) / qty_curr ELSE NULL END AS unit_profit_curr,
                CASE WHEN qty_prev > 0 THEN (rev_prev - cost_prev) / qty_prev ELSE NULL END AS unit_profit_prev,
                CASE WHEN qty_yoy > 0 THEN (rev_yoy - cost_yoy) / qty_yoy ELSE NULL END AS unit_profit_yoy
            FROM period_pivot
        ),
        driver_effects AS (
            SELECT
                *,
                CASE WHEN qty_curr IS NOT NULL AND qty_prev IS NOT NULL AND qty_curr > 0 AND qty_prev > 0
                    THEN (price_curr - price_prev) * qty_curr END AS mom_price_effect,
                CASE WHEN qty_curr IS NOT NULL AND qty_prev IS NOT NULL AND qty_curr > 0 AND qty_prev > 0
                    THEN (qty_curr - qty_prev) * price_prev END AS mom_volume_effect,
                CASE WHEN rev_curr IS NOT NULL AND rev_prev IS NOT NULL
                    THEN rev_curr - rev_prev
                        - COALESCE((CASE WHEN qty_curr IS NOT NULL AND qty_prev IS NOT NULL AND qty_curr > 0 AND qty_prev > 0
                            THEN (price_curr - price_prev) * qty_curr END), 0)
                        - COALESCE((CASE WHEN qty_curr IS NOT NULL AND qty_prev IS NOT NULL AND qty_curr > 0 AND qty_prev > 0
                            THEN (qty_curr - qty_prev) * price_prev END), 0)
                    END AS mom_mix_effect,
                CASE WHEN qty_curr IS NOT NULL AND qty_yoy IS NOT NULL AND qty_curr > 0 AND qty_yoy > 0
                    THEN (price_curr - price_yoy) * qty_curr END AS yoy_price_effect,
                CASE WHEN qty_curr IS NOT NULL AND qty_yoy IS NOT NULL AND qty_curr > 0 AND qty_yoy > 0
                    THEN (qty_curr - qty_yoy) * price_yoy END AS yoy_volume_effect,
                CASE WHEN rev_curr IS NOT NULL AND rev_yoy IS NOT NULL
                    THEN rev_curr - rev_yoy
                        - COALESCE((CASE WHEN qty_curr IS NOT NULL AND qty_yoy IS NOT NULL AND qty_curr > 0 AND qty_yoy > 0
                            THEN (price_curr - price_yoy) * qty_curr END), 0)
                        - COALESCE((CASE WHEN qty_curr IS NOT NULL AND qty_yoy IS NOT NULL AND qty_curr > 0 AND qty_yoy > 0
                            THEN (qty_curr - qty_yoy) * price_yoy END), 0)
                    END AS yoy_mix_effect,
                CASE WHEN qty_curr IS NOT NULL AND qty_prev IS NOT NULL AND qty_curr > 0 AND qty_prev > 0
                    THEN (unit_profit_curr - unit_profit_prev) * qty_curr END AS mom_profit_price_effect,
                CASE WHEN qty_curr IS NOT NULL AND qty_prev IS NOT NULL AND qty_curr > 0 AND qty_prev > 0
                    THEN (qty_curr - qty_prev) * unit_profit_prev END AS mom_profit_volume_effect,
                CASE WHEN rev_curr IS NOT NULL AND rev_prev IS NOT NULL
                    THEN (rev_curr - cost_curr) - (rev_prev - cost_prev)
                        - COALESCE((CASE WHEN qty_curr IS NOT NULL AND qty_prev IS NOT NULL AND qty_curr > 0 AND qty_prev > 0
                            THEN (unit_profit_curr - unit_profit_prev) * qty_curr END), 0)
                        - COALESCE((CASE WHEN qty_curr IS NOT NULL AND qty_prev IS NOT NULL AND qty_curr > 0 AND qty_prev > 0
                            THEN (qty_curr - qty_prev) * unit_profit_prev END), 0)
                    END AS mom_profit_mix_effect,
                CASE WHEN qty_curr IS NOT NULL AND qty_yoy IS NOT NULL AND qty_curr > 0 AND qty_yoy > 0
                    THEN (unit_profit_curr - unit_profit_yoy) * qty_curr END AS yoy_profit_price_effect,
                CASE WHEN qty_curr IS NOT NULL AND qty_yoy IS NOT NULL AND qty_curr > 0 AND qty_yoy > 0
                    THEN (qty_curr - qty_yoy) * unit_profit_yoy END AS yoy_profit_volume_effect,
                CASE WHEN rev_curr IS NOT NULL AND rev_yoy IS NOT NULL
                    THEN (rev_curr - cost_curr) - (rev_yoy - cost_yoy)
                        - COALESCE((CASE WHEN qty_curr IS NOT NULL AND qty_yoy IS NOT NULL AND qty_curr > 0 AND qty_yoy > 0
                            THEN (unit_profit_curr - unit_profit_yoy) * qty_curr END), 0)
                        - COALESCE((CASE WHEN qty_curr IS NOT NULL AND qty_yoy IS NOT NULL AND qty_curr > 0 AND qty_yoy > 0
                            THEN (qty_curr - qty_yoy) * unit_profit_yoy END), 0)
                    END AS yoy_profit_mix_effect
            FROM drivers
        ),
        customer_rollup AS (
            SELECT
                COALESCE(NULLIF(customer_name, ''), customer_id, 'Unknown') AS label,
                COALESCE(NULLIF(customer_id, ''), NULLIF(customer_name, ''), 'Unknown') AS entity_id,
                SUM(revenue) AS revenue
            FROM current_window
            GROUP BY 1, 2
        ),
        product_rollup AS (
            SELECT
                COALESCE(NULLIF(product_name, ''), product_id, 'Unknown') AS label,
                COALESCE(NULLIF(product_id, ''), NULLIF(product_name, ''), 'Unknown') AS entity_id,
                SUM(revenue) AS revenue
            FROM current_window
            GROUP BY 1, 2
        ),
        region_rollup AS (
            SELECT
                COALESCE(NULLIF(region, ''), 'Unknown') AS label,
                COALESCE(NULLIF(region, ''), 'Unknown') AS entity_id,
                SUM(revenue) AS revenue
            FROM current_window
            GROUP BY 1, 2
        ),
        customer_mix AS (
            SELECT label, entity_id, revenue
            FROM customer_rollup
            ORDER BY revenue DESC
            LIMIT 10
        ),
        product_mix AS (
            SELECT label, entity_id, revenue
            FROM product_rollup
            ORDER BY revenue DESC
            LIMIT 10
        ),
        region_mix AS (
            SELECT label, entity_id, revenue
            FROM region_rollup
            ORDER BY revenue DESC
            LIMIT 10
        ),
        customer_pareto AS (
            SELECT
                label,
                entity_id,
                revenue,
                CASE WHEN SUM(revenue) OVER () > 0 THEN SUM(revenue) OVER (ORDER BY revenue DESC ROWS UNBOUNDED PRECEDING) / SUM(revenue) OVER () * 100 ELSE 0 END AS cum_pct
            FROM customer_rollup
            ORDER BY revenue DESC
            LIMIT 30
        ),
        product_pareto AS (
            SELECT
                label,
                entity_id,
                revenue,
                CASE WHEN SUM(revenue) OVER () > 0 THEN SUM(revenue) OVER (ORDER BY revenue DESC ROWS UNBOUNDED PRECEDING) / SUM(revenue) OVER () * 100 ELSE 0 END AS cum_pct
            FROM product_rollup
            ORDER BY revenue DESC
            LIMIT 30
        ),
        region_pareto AS (
            SELECT
                label,
                entity_id,
                revenue,
                CASE WHEN SUM(revenue) OVER () > 0 THEN SUM(revenue) OVER (ORDER BY revenue DESC ROWS UNBOUNDED PRECEDING) / SUM(revenue) OVER () * 100 ELSE 0 END AS cum_pct
            FROM region_rollup
            ORDER BY revenue DESC
            LIMIT 30
        ),
        customer_monthly AS (
            SELECT
                COALESCE(NULLIF(customer_name, ''), customer_id, 'Unknown') AS label,
                customer_id AS entity_id,
                CAST(date_trunc('month', order_date) AS DATE) AS month,
                SUM(revenue) AS revenue,
                SUM(qty) AS qty
            FROM scoped
            WHERE order_date IS NOT NULL
            GROUP BY 1,2,3
        ),
        product_monthly AS (
            SELECT
                COALESCE(NULLIF(product_name, ''), product_id, 'Unknown') AS label,
                product_id AS entity_id,
                COALESCE(NULLIF(supplier, ''), 'Unknown') AS supplier,
                COALESCE(NULLIF(protein, ''), 'Unknown') AS protein,
                CAST(date_trunc('month', order_date) AS DATE) AS month,
                SUM(revenue) AS revenue,
                SUM(CASE WHEN cost_raw IS NOT NULL THEN revenue ELSE 0 END) AS revenue_with_cost,
                SUM(CASE WHEN cost_raw IS NOT NULL THEN cost ELSE 0 END) AS cost,
                SUM(qty) AS qty
            FROM scoped
            WHERE order_date IS NOT NULL
            GROUP BY 1,2,3,4,5
        ),
        region_monthly AS (
            SELECT
                COALESCE(NULLIF(region, ''), 'Unknown') AS label,
                CAST(date_trunc('month', order_date) AS DATE) AS month,
                SUM(revenue) AS revenue,
                SUM(qty) AS qty
            FROM scoped
            WHERE order_date IS NOT NULL
            GROUP BY 1,2
        ),
        customer_movers AS (
            SELECT
                label,
                entity_id,
                SUM(CASE WHEN month = p.curr_month THEN revenue ELSE 0 END) AS current,
                SUM(CASE WHEN month = p.prev_month THEN revenue ELSE 0 END) AS previous,
                SUM(CASE WHEN month = p.curr_month THEN qty ELSE 0 END) AS qty_current,
                SUM(CASE WHEN month = p.prev_month THEN qty ELSE 0 END) AS qty_previous
            FROM customer_monthly
            CROSS JOIN periods p
            GROUP BY 1,2
        ),
        product_movers AS (
            SELECT
                label,
                entity_id,
                SUM(CASE WHEN month = p.curr_month THEN revenue ELSE 0 END) AS current,
                SUM(CASE WHEN month = p.prev_month THEN revenue ELSE 0 END) AS previous,
                SUM(CASE WHEN month = p.curr_month THEN qty ELSE 0 END) AS qty_current,
                SUM(CASE WHEN month = p.prev_month THEN qty ELSE 0 END) AS qty_previous
            FROM product_monthly
            CROSS JOIN periods p
            GROUP BY 1,2
        ),
        region_movers AS (
            SELECT
                label,
                SUM(CASE WHEN month = p.curr_month THEN revenue ELSE 0 END) AS current,
                SUM(CASE WHEN month = p.prev_month THEN revenue ELSE 0 END) AS previous,
                SUM(CASE WHEN month = p.curr_month THEN qty ELSE 0 END) AS qty_current,
                SUM(CASE WHEN month = p.prev_month THEN qty ELSE 0 END) AS qty_previous
            FROM region_monthly
            CROSS JOIN periods p
            GROUP BY 1
        ),
        customer_movers_calc AS (
            SELECT
                label,
                entity_id,
                current,
                previous,
                current - previous AS delta,
                CASE WHEN ABS(previous) > 0 THEN (current - previous) / ABS(previous) * 100 ELSE NULL END AS delta_pct,
                qty_current,
                qty_previous,
                qty_current - qty_previous AS qty_delta,
                CASE WHEN qty_current > 0 THEN current / qty_current ELSE NULL END AS asp_current,
                CASE WHEN qty_previous > 0 THEN previous / qty_previous ELSE NULL END AS asp_previous,
                CASE WHEN qty_current > 0 AND qty_previous > 0 THEN current / qty_current - previous / qty_previous ELSE NULL END AS asp_delta
            FROM customer_movers
        ),
        product_movers_calc AS (
            SELECT
                label,
                entity_id,
                current,
                previous,
                current - previous AS delta,
                CASE WHEN ABS(previous) > 0 THEN (current - previous) / ABS(previous) * 100 ELSE NULL END AS delta_pct,
                qty_current,
                qty_previous,
                qty_current - qty_previous AS qty_delta,
                CASE WHEN qty_current > 0 THEN current / qty_current ELSE NULL END AS asp_current,
                CASE WHEN qty_previous > 0 THEN previous / qty_previous ELSE NULL END AS asp_previous,
                CASE WHEN qty_current > 0 AND qty_previous > 0 THEN current / qty_current - previous / qty_previous ELSE NULL END AS asp_delta
            FROM product_movers
        ),
        region_movers_calc AS (
            SELECT
                label,
                current,
                previous,
                current - previous AS delta,
                CASE WHEN ABS(previous) > 0 THEN (current - previous) / ABS(previous) * 100 ELSE NULL END AS delta_pct,
                qty_current,
                qty_previous,
                qty_current - qty_previous AS qty_delta,
                CASE WHEN qty_current > 0 THEN current / qty_current ELSE NULL END AS asp_current,
                CASE WHEN qty_previous > 0 THEN previous / qty_previous ELSE NULL END AS asp_previous,
                CASE WHEN qty_current > 0 AND qty_previous > 0 THEN current / qty_current - previous / qty_previous ELSE NULL END AS asp_delta
            FROM region_movers
        ),
        customer_gainers AS (
            SELECT * FROM customer_movers_calc ORDER BY delta DESC LIMIT 10
        ),
        customer_decliners AS (
            SELECT * FROM customer_movers_calc ORDER BY delta ASC LIMIT 10
        ),
        product_gainers AS (
            SELECT * FROM product_movers_calc ORDER BY delta DESC LIMIT 10
        ),
        product_decliners AS (
            SELECT * FROM product_movers_calc ORDER BY delta ASC LIMIT 10
        ),
        region_gainers AS (
            SELECT * FROM region_movers_calc ORDER BY delta DESC LIMIT 10
        ),
        region_decliners AS (
            SELECT * FROM region_movers_calc ORDER BY delta ASC LIMIT 10
        ),
        health AS (
            SELECT
                COUNT(*) AS rows,
                SUM(CASE WHEN cost_raw IS NULL THEN 1 ELSE 0 END) AS cost_missing,
                SUM(CASE WHEN pack_item_count IS NULL AND pack_weight_lb IS NULL THEN 1 ELSE 0 END) AS pack_missing,
                SUM(CASE WHEN product_id IS NULL OR product_name IS NULL THEN 1 ELSE 0 END) AS product_missing
            FROM current_window
        ),
        customer_conc_ranked AS (
            SELECT
                label,
                entity_id,
                revenue,
                ROW_NUMBER() OVER (ORDER BY revenue DESC) AS rn,
                SUM(revenue) OVER () AS total_rev
            FROM customer_rollup
        ),
        customer_conc AS (
            SELECT
                CASE WHEN total_rev > 0 THEN SUM((revenue / total_rev) * (revenue / total_rev)) OVER () * 10000 ELSE NULL END AS hhi,
                CASE WHEN total_rev > 0 THEN MAX(CASE WHEN rn = 1 THEN revenue / total_rev ELSE NULL END) OVER () * 100 ELSE NULL END AS top1_share,
                MAX(CASE WHEN rn = 1 THEN label ELSE NULL END) OVER () AS top1_label,
                MAX(CASE WHEN rn = 1 THEN entity_id ELSE NULL END) OVER () AS top1_entity_id,
                CASE WHEN total_rev > 0 THEN SUM(CASE WHEN rn <= 5 THEN revenue / total_rev ELSE 0 END) OVER () * 100 ELSE NULL END AS top5_share
            FROM customer_conc_ranked
            LIMIT 1
        ),
        product_conc_ranked AS (
            SELECT
                label,
                entity_id,
                revenue,
                ROW_NUMBER() OVER (ORDER BY revenue DESC) AS rn,
                SUM(revenue) OVER () AS total_rev
            FROM product_rollup
        ),
        product_conc AS (
            SELECT
                CASE WHEN total_rev > 0 THEN SUM((revenue / total_rev) * (revenue / total_rev)) OVER () * 10000 ELSE NULL END AS hhi,
                CASE WHEN total_rev > 0 THEN MAX(CASE WHEN rn = 1 THEN revenue / total_rev ELSE NULL END) OVER () * 100 ELSE NULL END AS top1_share,
                MAX(CASE WHEN rn = 1 THEN label ELSE NULL END) OVER () AS top1_label,
                MAX(CASE WHEN rn = 1 THEN entity_id ELSE NULL END) OVER () AS top1_entity_id,
                CASE WHEN total_rev > 0 THEN SUM(CASE WHEN rn <= 5 THEN revenue / total_rev ELSE 0 END) OVER () * 100 ELSE NULL END AS top5_share
            FROM product_conc_ranked
            LIMIT 1
        ),
        customer_curr AS (
            SELECT DISTINCT customer_id
            FROM scoped
            WHERE customer_id IS NOT NULL
              AND CAST(date_trunc('month', order_date) AS DATE) = (SELECT curr_month FROM periods)
        ),
        customer_prev AS (
            SELECT DISTINCT customer_id
            FROM scoped
            WHERE customer_id IS NOT NULL
              AND CAST(date_trunc('month', order_date) AS DATE) = (SELECT prev_month FROM periods)
        ),
        customer_prev2 AS (
            SELECT DISTINCT customer_id
            FROM scoped
            WHERE customer_id IS NOT NULL
              AND CAST(date_trunc('month', order_date) AS DATE) = (SELECT prev2_month FROM periods)
        ),
        customer_stats AS (
            SELECT
                (SELECT COUNT(*) FROM customer_curr) AS customers_current,
                (SELECT COUNT(*) FROM customer_prev) AS customers_prev,
                (SELECT COUNT(*) FROM customer_curr WHERE customer_id NOT IN (SELECT customer_id FROM customer_prev)) AS new_customers,
                (SELECT COUNT(*) FROM customer_curr WHERE customer_id IN (SELECT customer_id FROM customer_prev)) AS returning_customers,
                (SELECT COUNT(*) FROM customer_prev WHERE customer_id NOT IN (SELECT customer_id FROM customer_prev2)) AS new_customers_prev,
                (SELECT COUNT(*) FROM customer_prev WHERE customer_id IN (SELECT customer_id FROM customer_prev2)) AS returning_customers_prev
        ),
        activity AS (
            SELECT
                COUNT(DISTINCT CASE WHEN CAST(date_trunc('month', order_date) AS DATE) = (SELECT curr_month FROM periods) THEN product_id END) AS active_skus,
                COUNT(DISTINCT CASE WHEN CAST(date_trunc('month', order_date) AS DATE) = (SELECT prev_month FROM periods) THEN product_id END) AS active_skus_prev,
                COUNT(DISTINCT CASE WHEN CAST(date_trunc('month', order_date) AS DATE) = (SELECT curr_month FROM periods) THEN customer_id END) AS active_customers,
                COUNT(DISTINCT CASE WHEN CAST(date_trunc('month', order_date) AS DATE) = (SELECT prev_month FROM periods) THEN customer_id END) AS active_customers_prev
            FROM scoped
        ),
        region_ops_mix AS (
            SELECT
                label,
                revenue,
                CASE WHEN SUM(revenue) OVER () > 0 THEN revenue / SUM(revenue) OVER () * 100 ELSE 0 END AS share
            FROM region_rollup
            ORDER BY revenue DESC
            LIMIT 10
        ),
        method_ops_mix AS (
            SELECT
                COALESCE(NULLIF(ship_method, ''), 'Unknown') AS label,
                SUM(revenue) AS revenue
            FROM current_window
            GROUP BY 1
        ),
        method_ops_ranked AS (
            SELECT
                label,
                revenue,
                CASE WHEN SUM(revenue) OVER () > 0 THEN revenue / SUM(revenue) OVER () * 100 ELSE 0 END AS share
            FROM method_ops_mix
            ORDER BY revenue DESC
            LIMIT 10
        ),
        supplier_ops_mix AS (
            SELECT
                COALESCE(NULLIF(supplier, ''), 'Unknown') AS label,
                SUM(revenue) AS revenue
            FROM current_window
            GROUP BY 1
        ),
        supplier_ops_ranked AS (
            SELECT
                label,
                revenue,
                CASE WHEN SUM(revenue) OVER () > 0 THEN revenue / SUM(revenue) OVER () * 100 ELSE 0 END AS share
            FROM supplier_ops_mix
            ORDER BY revenue DESC
            LIMIT 10
        ),
        protein_ops_mix AS (
            SELECT
                COALESCE(NULLIF(protein, ''), 'Unknown') AS label,
                SUM(revenue) AS revenue
            FROM current_window
            GROUP BY 1
        ),
        protein_ops_ranked AS (
            SELECT
                label,
                revenue,
                CASE WHEN SUM(revenue) OVER () > 0 THEN revenue / SUM(revenue) OVER () * 100 ELSE 0 END AS share
            FROM protein_ops_mix
            ORDER BY revenue DESC
            LIMIT 10
        ),
        weekday_dist AS (
            SELECT
                CASE strftime('%w', order_date)
                    WHEN '0' THEN 'Sun'
                    WHEN '1' THEN 'Mon'
                    WHEN '2' THEN 'Tue'
                    WHEN '3' THEN 'Wed'
                    WHEN '4' THEN 'Thu'
                    WHEN '5' THEN 'Fri'
                    WHEN '6' THEN 'Sat'
                    ELSE 'Unknown'
                END AS weekday,
                CAST(strftime('%w', order_date) AS INTEGER) AS idx,
                SUM(revenue) AS revenue
            FROM current_window
            WHERE order_date IS NOT NULL
            GROUP BY 1,2
        ),
        weekday_ranked AS (
            SELECT
                weekday,
                idx,
                revenue,
                CASE WHEN SUM(revenue) OVER () > 0 THEN revenue / SUM(revenue) OVER () * 100 ELSE 0 END AS share
            FROM weekday_dist
            ORDER BY idx
        ),
        customer_margin AS (
            SELECT
                COALESCE(NULLIF(customer_name, ''), customer_id, 'Unknown') AS label,
                SUM(CASE WHEN cost_raw IS NOT NULL THEN revenue ELSE 0 END) AS revenue,
                SUM(CASE WHEN cost_raw IS NOT NULL THEN cost ELSE 0 END) AS cost
            FROM current_window
            GROUP BY 1
        ),
        customer_margin_pct AS (
            SELECT
                label,
                CASE WHEN revenue > 0 THEN (revenue - cost) / revenue * 100 ELSE NULL END AS margin_pct
            FROM customer_margin
            WHERE revenue > 0
        ),
        margin_stats AS (
            SELECT
                approx_quantile(margin_pct, 0.1) AS p10,
                approx_quantile(margin_pct, 0.5) AS p50,
                approx_quantile(margin_pct, 0.9) AS p90,
                SUM(CASE WHEN margin_pct < 0 THEN 1 ELSE 0 END) AS below_zero,
                SUM(CASE WHEN margin_pct > 50 THEN 1 ELSE 0 END) AS above_fifty,
                COUNT(*) AS count
            FROM customer_margin_pct
        )
        SELECT
            totals.*,
            (SELECT list(struct_pack(month:=month, revenue:=revenue, cost:=cost, qty:=qty, weight:=weight, orders:=orders, customers:=customers)) FROM (SELECT * FROM monthly ORDER BY month)) AS monthly,
            (SELECT list(struct_pack(label:=label, entity_id:=entity_id, value:=revenue)) FROM customer_mix) AS mix_customer,
            (SELECT list(struct_pack(label:=label, entity_id:=entity_id, value:=revenue)) FROM product_mix) AS mix_product,
            (SELECT list(struct_pack(label:=label, entity_id:=entity_id, value:=revenue)) FROM region_mix) AS mix_region,
            (SELECT list(struct_pack(label:=label, entity_id:=entity_id, value:=revenue, cum_pct:=cum_pct)) FROM customer_pareto) AS pareto_customer,
            (SELECT list(struct_pack(label:=label, entity_id:=entity_id, value:=revenue, cum_pct:=cum_pct)) FROM product_pareto) AS pareto_product,
            (SELECT list(struct_pack(label:=label, entity_id:=entity_id, value:=revenue, cum_pct:=cum_pct)) FROM region_pareto) AS pareto_region,
            (SELECT list(struct_pack(label:=label, entity_id:=entity_id, current:=current, previous:=previous, delta:=delta, delta_pct:=delta_pct, qty_current:=qty_current, qty_previous:=qty_previous, qty_delta:=qty_delta, asp_current:=asp_current, asp_previous:=asp_previous, asp_delta:=asp_delta)) FROM customer_gainers) AS movers_customer_gainers,
            (SELECT list(struct_pack(label:=label, entity_id:=entity_id, current:=current, previous:=previous, delta:=delta, delta_pct:=delta_pct, qty_current:=qty_current, qty_previous:=qty_previous, qty_delta:=qty_delta, asp_current:=asp_current, asp_previous:=asp_previous, asp_delta:=asp_delta)) FROM customer_decliners) AS movers_customer_decliners,
            (SELECT list(struct_pack(label:=label, entity_id:=entity_id, current:=current, previous:=previous, delta:=delta, delta_pct:=delta_pct, qty_current:=qty_current, qty_previous:=qty_previous, qty_delta:=qty_delta, asp_current:=asp_current, asp_previous:=asp_previous, asp_delta:=asp_delta)) FROM product_gainers) AS movers_product_gainers,
            (SELECT list(struct_pack(label:=label, entity_id:=entity_id, current:=current, previous:=previous, delta:=delta, delta_pct:=delta_pct, qty_current:=qty_current, qty_previous:=qty_previous, qty_delta:=qty_delta, asp_current:=asp_current, asp_previous:=asp_previous, asp_delta:=asp_delta)) FROM product_decliners) AS movers_product_decliners,
            (SELECT list(struct_pack(label:=label, current:=current, previous:=previous, delta:=delta, delta_pct:=delta_pct, qty_current:=qty_current, qty_previous:=qty_previous, qty_delta:=qty_delta, asp_current:=asp_current, asp_previous:=asp_previous, asp_delta:=asp_delta)) FROM region_gainers) AS movers_region_gainers,
            (SELECT list(struct_pack(label:=label, current:=current, previous:=previous, delta:=delta, delta_pct:=delta_pct, qty_current:=qty_current, qty_previous:=qty_previous, qty_delta:=qty_delta, asp_current:=asp_current, asp_previous:=asp_previous, asp_delta:=asp_delta)) FROM region_decliners) AS movers_region_decliners,
            health.rows AS health_rows,
            health.cost_missing AS health_cost_missing,
            health.pack_missing AS health_pack_missing,
            health.product_missing AS health_product_missing,
            (SELECT struct_pack(hhi:=hhi, top1_share:=top1_share, top1_label:=top1_label, top1_entity_id:=top1_entity_id, top5_share:=top5_share) FROM customer_conc) AS conc_customer,
            (SELECT struct_pack(hhi:=hhi, top1_share:=top1_share, top1_label:=top1_label, top1_entity_id:=top1_entity_id, top5_share:=top5_share) FROM product_conc) AS conc_product,
            (SELECT struct_pack(p10:=p10, p50:=p50, p90:=p90, below_zero:=below_zero, above_fifty:=above_fifty, count:=count) FROM margin_stats) AS margin_stats,
            (SELECT list(struct_pack(label:=label, revenue:=revenue, share:=share)) FROM region_ops_mix) AS ops_region_mix,
            (SELECT list(struct_pack(label:=label, revenue:=revenue, share:=share)) FROM method_ops_ranked) AS ops_method_mix,
            (SELECT list(struct_pack(label:=label, revenue:=revenue, share:=share)) FROM supplier_ops_ranked) AS ops_supplier_mix,
            (SELECT list(struct_pack(label:=label, revenue:=revenue, share:=share)) FROM protein_ops_ranked) AS ops_protein_mix,
            (SELECT list(struct_pack(weekday:=weekday, revenue:=revenue, share:=share)) FROM weekday_ranked) AS weekday_mix,
            customer_stats.customers_current AS customers_current,
            customer_stats.customers_prev AS customers_prev,
            customer_stats.new_customers AS new_customers,
            customer_stats.returning_customers AS returning_customers,
            customer_stats.new_customers_prev AS new_customers_prev,
            customer_stats.returning_customers_prev AS returning_customers_prev,
            activity.active_skus AS active_skus,
            activity.active_skus_prev AS active_skus_prev,
            activity.active_customers AS active_customers,
            activity.active_customers_prev AS active_customers_prev,
            (SELECT struct_pack(
                revenue:=struct_pack(current:=rev_curr, previous:=rev_prev, delta:=CASE WHEN rev_curr IS NOT NULL AND rev_prev IS NOT NULL THEN rev_curr - rev_prev ELSE NULL END, price_effect:=mom_price_effect, volume_effect:=mom_volume_effect, mix_effect:=mom_mix_effect),
                profit:=struct_pack(current:=CASE WHEN rev_curr IS NOT NULL AND cost_curr IS NOT NULL THEN rev_curr - cost_curr ELSE NULL END, previous:=CASE WHEN rev_prev IS NOT NULL AND cost_prev IS NOT NULL THEN rev_prev - cost_prev ELSE NULL END, delta:=CASE WHEN rev_curr IS NOT NULL AND rev_prev IS NOT NULL AND cost_curr IS NOT NULL AND cost_prev IS NOT NULL THEN (rev_curr - cost_curr) - (rev_prev - cost_prev) ELSE NULL END, price_effect:=mom_profit_price_effect, volume_effect:=mom_profit_volume_effect, mix_effect:=mom_profit_mix_effect)
            ) FROM driver_effects) AS drivers_mom,
            (SELECT struct_pack(
                revenue:=struct_pack(current:=rev_curr, previous:=rev_yoy, delta:=CASE WHEN rev_curr IS NOT NULL AND rev_yoy IS NOT NULL THEN rev_curr - rev_yoy ELSE NULL END, price_effect:=yoy_price_effect, volume_effect:=yoy_volume_effect, mix_effect:=yoy_mix_effect),
                profit:=struct_pack(current:=CASE WHEN rev_curr IS NOT NULL AND cost_curr IS NOT NULL THEN rev_curr - cost_curr ELSE NULL END, previous:=CASE WHEN rev_yoy IS NOT NULL AND cost_yoy IS NOT NULL THEN rev_yoy - cost_yoy ELSE NULL END, delta:=CASE WHEN rev_curr IS NOT NULL AND rev_yoy IS NOT NULL AND cost_curr IS NOT NULL AND cost_yoy IS NOT NULL THEN (rev_curr - cost_curr) - (rev_yoy - cost_yoy) ELSE NULL END, price_effect:=yoy_profit_price_effect, volume_effect:=yoy_profit_volume_effect, mix_effect:=yoy_profit_mix_effect)
            ) FROM driver_effects) AS drivers_yoy
        FROM totals
        CROSS JOIN health
        CROSS JOIN customer_stats
        CROSS JOIN activity
    """

    query_params = list(where_params)
    query_params.extend(
        [
            current_start_iso,
            current_end_excl_iso,
            period_anchor_iso,
            period_anchor_iso,
            period_anchor_iso,
            period_anchor_iso,
        ]
    )

    df = _exec_df(sql, query_params, "bundle_all", conn)
    row = df.iloc[0].to_dict() if not df.empty else {}

    comparison_ctx = _compute_window_comparison_context(
        conn,
        where_sql=where_sql,
        where_params=where_params,
        date_expr=date_expr,
        revenue_expr=revenue_expr,
        cost_raw_expr=cost_raw_expr,
        qty_expr=qty_expr,
        weight_expr=weight_expr,
        order_expr=order_expr,
        customer_id_expr=customer_id_expr,
        customer_name_expr=customer_name_expr,
        product_id_expr=product_id_expr,
        product_name_expr=product_name_expr,
        window_contract=window_contract,
    )
    current_window_metrics = comparison_ctx.get("current_metrics") or {}
    prior_window_metrics = comparison_ctx.get("prior_metrics") or {}
    yoy_window_metrics = comparison_ctx.get("yoy_metrics") or {}
    revenue = _clean_optional_float(current_window_metrics.get("revenue"))
    cost = _clean_optional_float(current_window_metrics.get("cost"))
    revenue_with_cost = _clean_optional_float(current_window_metrics.get("revenue_with_cost"))
    qty_with_cost = _clean_optional_float(current_window_metrics.get("qty_with_cost"))
    qty = _clean_optional_float(current_window_metrics.get("qty"))
    weight = _clean_optional_float(current_window_metrics.get("weight"))
    orders = int(current_window_metrics.get("orders") or 0)
    customers = int(current_window_metrics.get("customers") or 0)
    profit = _clean_optional_float(current_window_metrics.get("profit"))
    asp = _clean_optional_float(current_window_metrics.get("asp"))
    aov = _clean_optional_float(current_window_metrics.get("aov"))
    rev_per_customer = au.safe_div(revenue, customers) if customers and revenue is not None else None
    margin_pct = _clean_optional_float(current_window_metrics.get("margin_pct"))
    roi_pct = au.safe_div(profit, cost) * 100 if (profit is not None and cost) else None
    asp_cost_basis = _clean_optional_float(current_window_metrics.get("asp_cost_basis"))

    totals = {
        "revenue": revenue,
        "cost": cost,
        "profit": profit,
        "revenue_with_cost": revenue_with_cost,
        "qty": qty,
        "qty_with_cost": qty_with_cost,
        "weight": weight,
        "orders": orders,
        "customers": customers,
        "asp": float(asp or 0.0) if asp is not None else None,
        "asp_cost_basis": float(asp_cost_basis or 0.0) if asp_cost_basis is not None else None,
        "aov": float(aov or 0.0) if aov is not None else None,
        "rev_per_customer": float(rev_per_customer or 0.0) if rev_per_customer is not None else None,
        "margin_pct": float(margin_pct or 0.0) if margin_pct is not None else None,
        "roi_pct": float(roi_pct or 0.0) if roi_pct is not None else None,
    }

    monthly_list = _struct_list(row.get("monthly"))
    monthly_df = pd.DataFrame(monthly_list)
    if monthly_df.empty:
        monthly_df = pd.DataFrame(columns=["month", "revenue", "cost", "qty", "weight", "orders", "customers"]).set_index(
            pd.Index([], name="month")
        )
    else:
        monthly_df["month"] = pd.to_datetime(monthly_df["month"], errors="coerce")
        monthly_df = monthly_df.dropna(subset=["month"])
        monthly_df["month"] = monthly_df["month"].dt.to_period("M")
        monthly_df.set_index("month", inplace=True)
        monthly_df = monthly_df.sort_index()
        monthly_df["profit"] = monthly_df["revenue"] - monthly_df["cost"]
        monthly_df["aov"] = au.safe_div(monthly_df["revenue"], monthly_df["orders"].replace(0, pd.NA))
        monthly_df["asp"] = au.safe_div(monthly_df["revenue"], monthly_df["qty"].replace(0, pd.NA))
        monthly_df["margin_pct"] = au.safe_div(monthly_df["profit"], monthly_df["revenue"]) * 100
        monthly_df["margin_pct"] = monthly_df["margin_pct"].replace([np.inf, -np.inf], np.nan).clip(lower=-100.0, upper=1000.0)

    def _series_from_monthly(col: str) -> pd.Series:
        if col not in monthly_df.columns:
            return pd.Series(dtype=float)
        return monthly_df[col].astype(float)

    deltas = _comparison_deltas(current_window_metrics, prior_window_metrics, yoy_window_metrics)

    weekly_sql = f"""
        WITH scoped_base AS (
            SELECT
                {date_expr} AS order_date,
                {revenue_expr} AS revenue,
                {cost_raw_expr} AS cost_raw,
                {qty_expr} AS qty,
                {weight_expr} AS weight
            FROM fact
            WHERE {where_sql}
        ),
        scoped AS (
            SELECT
                order_date,
                revenue,
                ({effective_cost_expr}) AS cost,
                cost_raw,
                qty
            FROM scoped_base
        )
        SELECT
            DATE_TRUNC('week', order_date)::DATE AS week_start,
            SUM(revenue) AS revenue,
            SUM(CASE WHEN cost_raw IS NOT NULL THEN cost ELSE 0 END) AS cost,
            SUM(qty) AS qty
        FROM scoped
        WHERE order_date >= CAST(? AS DATE) AND order_date < CAST(? AS DATE)
        GROUP BY 1
        ORDER BY 1
    """
    weekly_params = list(where_params)
    weekly_params.extend(
        [
            window_contract.current_start.isoformat(),
            (window_contract.current_end + timedelta(days=1)).isoformat(),
        ]
    )
    weekly_df = _exec_df(weekly_sql, weekly_params, "overview_weekly_trend", conn)
    if weekly_df.empty:
        weekly_df = pd.DataFrame(columns=["week_start", "revenue", "cost", "qty"])
    else:
        weekly_df["week_start"] = pd.to_datetime(weekly_df["week_start"], errors="coerce")
        weekly_df = weekly_df.dropna(subset=["week_start"]).sort_values("week_start")
        weekly_df["profit"] = pd.to_numeric(weekly_df["revenue"], errors="coerce").fillna(0.0) - pd.to_numeric(weekly_df["cost"], errors="coerce").fillna(0.0)
        weekly_df["asp"] = au.safe_div(
            pd.to_numeric(weekly_df["revenue"], errors="coerce").fillna(0.0),
            pd.to_numeric(weekly_df["qty"], errors="coerce").replace(0, pd.NA),
        )
        weekly_df["margin_pct"] = au.safe_div(weekly_df["profit"], pd.to_numeric(weekly_df["revenue"], errors="coerce").replace(0, pd.NA)) * 100

    monthly_labels = _trend_labels_from_index(monthly_df.index, date_type=window_contract.date_type)
    trend = {
        "months": [str(idx) for idx in monthly_df.index],
        "labels": monthly_labels,
        "period_type": window_contract.date_type,
        "bucket_label": window_contract.trend_bucket_label,
        "revenue": [float(v) if pd.notna(v) else None for v in _series_from_monthly("revenue")],
        "units": [float(v) if pd.notna(v) else None for v in _series_from_monthly("qty")],
        "asp": [float(v) if pd.notna(v) else None for v in _series_from_monthly("asp")],
        "profit": [float(v) if pd.notna(v) else None for v in _series_from_monthly("profit")],
        "margin_pct": [float(v) if pd.notna(v) else None for v in _series_from_monthly("margin_pct")],
        "cost": [float(v) if pd.notna(v) else None for v in _series_from_monthly("cost")],
        "monthly": {
            "months": [str(idx) for idx in monthly_df.index],
            "labels": monthly_labels,
            "period_type": window_contract.date_type,
            "bucket_label": window_contract.trend_bucket_label,
            "revenue": [float(v) if pd.notna(v) else None for v in _series_from_monthly("revenue")],
            "units": [float(v) if pd.notna(v) else None for v in _series_from_monthly("qty")],
            "asp": [float(v) if pd.notna(v) else None for v in _series_from_monthly("asp")],
            "profit": [float(v) if pd.notna(v) else None for v in _series_from_monthly("profit")],
            "margin_pct": [float(v) if pd.notna(v) else None for v in _series_from_monthly("margin_pct")],
            "cost": [float(v) if pd.notna(v) else None for v in _series_from_monthly("cost")],
        },
        "weekly": {
            "months": [d.date().isoformat() for d in weekly_df.get("week_start", pd.Series(dtype="datetime64[ns]"))],
            "revenue": [float(v) if pd.notna(v) else None for v in weekly_df.get("revenue", pd.Series(dtype=float))],
            "units": [float(v) if pd.notna(v) else None for v in weekly_df.get("qty", pd.Series(dtype=float))],
            "asp": [float(v) if pd.notna(v) else None for v in weekly_df.get("asp", pd.Series(dtype=float))],
            "profit": [float(v) if pd.notna(v) else None for v in weekly_df.get("profit", pd.Series(dtype=float))],
            "margin_pct": [float(v) if pd.notna(v) else None for v in weekly_df.get("margin_pct", pd.Series(dtype=float))],
            "cost": [float(v) if pd.notna(v) else None for v in weekly_df.get("cost", pd.Series(dtype=float))],
        },
    }

    mix = {
        "customer": _struct_list(row.get("mix_customer")),
        "product": _struct_list(row.get("mix_product")),
        "region": _struct_list(row.get("mix_region")),
    }

    def _pareto_from_list(rows: List[Dict[str, Any]]) -> Dict[str, List[Any]]:
        labels = [str(r.get("label") or "Unknown") for r in rows]
        entity_ids = [str(r.get("entity_id") or r.get("label") or "Unknown") for r in rows]
        values = [_clean_float(r.get("value")) for r in rows]
        cum = [_clean_float(r.get("cum_pct")) for r in rows]
        return {"labels": labels, "entity_ids": entity_ids, "values": values, "cum_pct": cum}

    pareto = {
        "customer": _pareto_from_list(_struct_list(row.get("pareto_customer"))),
        "product": _pareto_from_list(_struct_list(row.get("pareto_product"))),
        "region": _pareto_from_list(_struct_list(row.get("pareto_region"))),
    }

    top_movers = _window_top_movers(
        conn,
        where_sql=where_sql,
        where_params=where_params,
        date_expr=date_expr,
        revenue_expr=revenue_expr,
        qty_expr=qty_expr,
        customer_id_expr=customer_id_expr,
        customer_name_expr=customer_name_expr,
        product_id_expr=product_id_expr,
        product_name_expr=product_name_expr,
        region_expr=region_expr,
        window_contract=window_contract,
    )

    rows_total = int(row.get("health_rows") or 0)
    cost_missing = int(row.get("health_cost_missing") or 0)
    pack_missing = int(row.get("health_pack_missing") or 0)
    product_missing = int(row.get("health_product_missing") or 0)
    cost_missing_pct = round((cost_missing / rows_total) * 100, 2) if rows_total else None
    cost_coverage_pct = round(100 - cost_missing_pct, 2) if cost_missing_pct is not None else None
    has_packs = max(0, rows_total - pack_missing)
    packs_coverage_pct = round((has_packs / rows_total) * 100, 2) if rows_total else None
    refresh_meta = _refresh_meta()
    health = {
        "rows": rows_total,
        "cost_missing_pct": cost_missing_pct,
        "cost_coverage_pct": cost_coverage_pct,
        "pack_missing_pct": round((pack_missing / rows_total) * 100, 2) if rows_total else None,
        "packs_coverage_pct": packs_coverage_pct,
        "total_orderlines": rows_total,
        "has_packs_orderlines": has_packs,
        "missing_packs_orderlines": pack_missing,
        "product_mapping_missing": product_missing,
        "freshness_sla_days": refresh_meta.get("refresh_age_days"),
        "freshness_sla_hours": refresh_meta.get("refresh_age_hours"),
        "freshness_basis": "governed_refresh",
        "governed_refresh_at": refresh_meta.get("last_refresh"),
        "data_cutoff": refresh_meta.get("data_cutoff"),
        "defaulted_window": defaulted_flag,
        "include_current_month": include_current_month,
        "cost_available": cost_available,
        "comparison_note": window_contract.note,
        "issues": [],
    }
    health["issues"] = _build_health_issues(
        cost_missing=cost_missing,
        pack_missing=pack_missing,
        product_missing=product_missing,
        window_contract=window_contract,
    )

    operations = {
        "customers": dict((comparison_ctx.get("operations") or {}).get("customers") or {}),
        "activity": dict((comparison_ctx.get("operations") or {}).get("activity") or {}),
        "mix": {
            "region": _struct_list(row.get("ops_region_mix")),
            "method": _struct_list(row.get("ops_method_mix")),
            "supplier": _struct_list(row.get("ops_supplier_mix")),
            "protein": _struct_list(row.get("ops_protein_mix")),
        },
        "weekday": _struct_list(row.get("weekday_mix")),
    }

    concentration = {
        "customer": row.get("conc_customer") or {},
        "product": row.get("conc_product") or {},
    }
    for key in ("customer", "product"):
        block = concentration.get(key) or {}
        block["risk_label"] = om.hhi_risk_label(block.get("hhi"))
        concentration[key] = block

    margin_risk_payload = _compute_window_margin_risk(
        conn,
        where_sql=where_sql,
        where_params=where_params,
        date_expr=date_expr,
        revenue_expr=revenue_expr,
        cost_raw_expr=cost_raw_expr,
        qty_expr=qty_expr,
        weight_expr=weight_expr,
        product_id_expr=product_id_expr,
        product_name_expr=product_name_expr,
        supplier_expr=supplier_expr,
        protein_expr=protein_expr,
        window_contract=window_contract,
    )
    profitability = {
        "margin_pct": row.get("margin_stats") or {},
        "margin_risk": margin_risk_payload.get("rows") or [],
        "minimum_margin_pct": _clean_optional_float(margin_risk_payload.get("minimum_margin_pct")),
        "target_margin_pct": _clean_optional_float(margin_risk_payload.get("target_margin_pct")),
    }
    for item in profitability["margin_risk"]:
        margin_val = _clean_optional_float(item.get("margin_pct"))
        revenue_val = _clean_optional_float(item.get("revenue"))
        if margin_val is not None and revenue_val is not None:
            item["profit"] = _clean_optional_float(item.get("profit")) or (revenue_val * margin_val / 100.0)
            item["profit_impact"] = item["profit"]
        item["supplier"] = item.get("supplier") or "Unknown"
        item["protein"] = item.get("protein") or "Unknown"
    profitability["margin_risk"] = sorted(
        profitability["margin_risk"],
        key=lambda rec: float(rec.get("profit_impact") or 0.0),
    )
    profitability["coverage"] = {"cost_pct": cost_coverage_pct, "cost_available": cost_available}
    cost_ok = cost_available and (cost_coverage_pct is None or cost_coverage_pct >= 80)
    if not cost_ok:
        profitability["margin_risk"] = []
        profitability["message"] = "Insufficient cost coverage for detailed margin risk."

    driver_decomp_enabled = _driver_decomp_v2_enabled()
    drivers = {
        "enabled": False,
        "schema_version": "legacy",
        "metric_default": "revenue",
        "coverage": {"cost_pct": cost_coverage_pct, "cost_available": cost_available},
        "mom": row.get("drivers_mom") or {},
        "yoy": row.get("drivers_yoy") or {},
    }
    if driver_decomp_enabled:
        try:
            drivers = _compute_driver_decomposition_v2(
                conn,
                where_sql=where_sql,
                where_params=where_params,
                date_expr=date_expr,
                revenue_expr=revenue_expr,
                cost_raw_expr=cost_raw_expr,
                qty_expr=qty_expr,
                product_id_expr=product_id_expr,
                product_name_expr=product_name_expr,
                window_contract=window_contract,
                cost_available=cost_available,
                cost_coverage_pct=cost_coverage_pct,
            )
        except Exception:
            try:
                current_app.logger.exception("overview.driver_decomp_v2.failed", extra={"request_id": _request_id()})
            except Exception:
                pass

    for key in ("mom", "yoy"):
        block = drivers.get(key) or {}
        rev = block.get("revenue") or {}
        total = _clean_optional_float(rev.get("delta"))
        volume = _clean_optional_float(rev.get("volume_effect"))
        mix_effect = _clean_optional_float(rev.get("mix_effect"))
        if not block.get("message") and total is not None and (volume is not None or mix_effect is not None):
            block_label = str(block.get("label") or key.upper())
            direction = "up" if total >= 0 else "down"
            vol_text = "n/a" if volume is None else f"{volume:+,.0f}"
            mix_text = "n/a" if mix_effect is None else f"{mix_effect:+,.0f}"
            block["message"] = f"{block_label} revenue {direction} ${abs(total):,.0f}: volume {vol_text}, mix {mix_text}."
        drivers[key] = block

    filters_payload = _filters_payload(filters)
    filters_labels = {
        "regions": filters_payload.get("regions") or [],
        "methods": filters_payload.get("methods") or [],
        "customers": filters_payload.get("customers") or [],
        "suppliers": filters_payload.get("suppliers") or [],
        "products": filters_payload.get("products") or [],
        "sales_reps": filters_payload.get("sales_reps") or [],
    }

    version = str(refresh_meta.get("version") or "0")
    last_refresh = refresh_meta.get("last_refresh")
    window_days = None
    if start_iso and end_iso:
        try:
            window_days = (datetime.fromisoformat(end_iso) - datetime.fromisoformat(start_iso)).days + 1
        except Exception:
            window_days = None
    window = window_contract.as_dict()
    window.update(
        {
            "start": start_iso,
            "end": end_iso,
            "days": window_days,
            "rows": rows_total,
            "method_label": _window_method_label(window_contract),
            "period_status": "incomplete" if bool(window_contract.terminal_period_incomplete) else "complete",
            "period_status_label": "Incomplete period" if bool(window_contract.terminal_period_incomplete) else "Completed period",
        }
    )
    primary_delta_label = str(window_contract.delta_short_label or "Prior window")
    primary_compare_label = str(window_contract.prior_label or "Prior comparable window")
    comparison_label = str(window_contract.comparison_label or "Current window vs prior comparable window")

    def _callouts() -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        if window_contract.terminal_period_incomplete:
            items.append(
                {
                    "title": "Period context",
                    "value": window_contract.current_short_label,
                    "value_fmt": "text",
                    "detail": window_contract.note,
                    "severity": "info",
                }
            )
        rev_mom = deltas.get("revenue", {}).get("mom_pct")
        if rev_mom is not None:
            items.append(
                {
                    "title": f"Revenue {primary_delta_label}" if primary_delta_label in {"MoM", "MTD"} else "Revenue vs prior window",
                    "value": rev_mom,
                    "value_fmt": "percent",
                    "detail": comparison_label,
                    "severity": "positive" if rev_mom > 0 else "negative" if rev_mom < 0 else "neutral",
                }
            )
        movers = top_movers.get("customer", {}).get("gainers") or []
        if movers:
            top = movers[0]
            items.append(
                {
                    "title": "Top Customer Gainer",
                    "value": top.get("delta"),
                    "value_fmt": "currency",
                    "detail": top.get("label"),
                    "severity": "positive",
                    "link": {"kind": "customer", "id": top.get("entity_id") or top.get("label")},
                }
            )
        risks = profitability.get("margin_risk") or []
        if risks:
            items.append(
                {
                    "title": "Margin Risk Items",
                    "value": len(risks),
                    "value_fmt": "number",
                    "detail": "High-revenue products below target margin",
                    "severity": "warning",
                }
            )
        conc_customer = concentration.get("customer") or {}
        top_share = conc_customer.get("top1_share")
        if top_share is not None:
            items.append(
                {
                    "title": "Top Customer Share",
                    "value": top_share,
                    "value_fmt": "percent",
                    "detail": f"HHI {int(conc_customer.get('hhi') or 0)}",
                    "severity": "warning" if top_share >= 20 else "info",
                }
            )
        new_curr = operations.get("customers", {}).get("new") or 0
        cust_curr = operations.get("customers", {}).get("current") or 0
        new_share = (new_curr / cust_curr * 100) if cust_curr else None
        if new_share is not None:
            items.append(
                {
                    "title": "New Customer Share",
                    "value": new_share,
                    "value_fmt": "percent",
                    "detail": f"{new_curr} new of {cust_curr}",
                    "severity": "info",
                }
            )
        if cost_coverage_pct is not None:
            items.append(
                {
                    "title": "Cost Coverage",
                    "value": cost_coverage_pct,
                    "value_fmt": "percent",
                    "detail": "Missing cost may impact profit metrics" if cost_coverage_pct < 90 else "Coverage healthy",
                    "severity": "warning" if cost_coverage_pct < 90 else "info",
                }
            )
        return items[:5]

    insights = {
        "callouts": _callouts(),
        "margin_risk": profitability.get("margin_risk") or [],
    }

    customer_movers_bucket = top_movers.get("customer") or {}
    top_customer_gainer = (customer_movers_bucket.get("gainers") or [None])[0]
    top_customer_decliner = (customer_movers_bucket.get("decliners") or [None])[0]
    profit_per_order = au.safe_div(profit, orders) if (profit is not None and orders) else None
    profit_per_lb = au.safe_div(profit, weight) if (profit is not None and weight) else None
    customer_ops = operations.get("customers") or {}
    customer_current = int(customer_ops.get("current") or 0)
    new_customers = int(customer_ops.get("new") or 0)
    returning_customers = int(customer_ops.get("returning") or 0)
    new_share_pct = (new_customers / customer_current * 100.0) if customer_current else None
    returning_share_pct = (returning_customers / customer_current * 100.0) if customer_current else None
    margin_risk_rows = profitability.get("margin_risk") or []
    margin_risk_revenue = float(
        sum(float(_clean_optional_float(rec.get("revenue")) or 0.0) for rec in margin_risk_rows)
    )
    margin_risk_revenue_share = (
        (margin_risk_revenue / float(revenue or 0.0) * 100.0)
        if revenue
        else None
    )
    revenue_mom_delta = _clean_optional_float(deltas.get("revenue", {}).get("mom"))
    revenue_mom_delta_pct = _clean_optional_float(deltas.get("revenue", {}).get("mom_pct"))
    volume_effect = _clean_optional_float(((drivers.get("mom") or {}).get("revenue") or {}).get("volume_effect"))
    mix_effect = _clean_optional_float(((drivers.get("mom") or {}).get("revenue") or {}).get("mix_effect"))

    narrative: List[str] = []
    if window_contract.terminal_period_incomplete:
        narrative.append(
            f"Current period is still open through {window_contract.current_window_label}; comparisons are aligned to {window_contract.prior_window_label} to avoid distorted partial-period reads."
        )
    if revenue_mom_delta is not None:
        direction = "up" if revenue_mom_delta >= 0 else "down"
        pct_text = "n/a" if revenue_mom_delta_pct is None else f"{revenue_mom_delta_pct:+.1f}%"
        vol_text = "n/a" if volume_effect is None else _signed_money(volume_effect)
        mix_text = "n/a" if mix_effect is None else _signed_money(mix_effect)
        narrative.append(
            f"Revenue {direction} {_signed_money(revenue_mom_delta)} versus {primary_compare_label.lower()} ({pct_text}), driven by Volume {vol_text} and Mix {mix_text}."
        )
    if top_customer_gainer or top_customer_decliner:
        gainer_text = ""
        decliner_text = ""
        if isinstance(top_customer_gainer, dict):
            gainer_text = f"Top gainer: {top_customer_gainer.get('label') or 'Unknown'} ({_signed_money(_clean_optional_float(top_customer_gainer.get('delta')))})."
        if isinstance(top_customer_decliner, dict):
            decliner_text = f"Top decliner: {top_customer_decliner.get('label') or 'Unknown'} ({_signed_money(_clean_optional_float(top_customer_decliner.get('delta')))})."
        narrative.append(" ".join([part for part in [gainer_text, decliner_text] if part]).strip())
    if margin_risk_rows:
        rev_share_text = "n/a" if margin_risk_revenue_share is None else f"{margin_risk_revenue_share:.1f}%"
        narrative.append(
            f"Margin risk: {len(margin_risk_rows)} SKUs below target margin on ${margin_risk_revenue:,.0f} revenue ({rev_share_text} of total)."
        )
    narrative = [line for line in narrative if line][:3]

    def _best_mover(direction: str) -> Optional[Dict[str, Any]]:
        bucket_name = "gainers" if str(direction).strip().lower() == "positive" else "decliners"
        best: Optional[Dict[str, Any]] = None
        for dim in ("customer", "product", "region"):
            rows = (top_movers.get(dim) or {}).get(bucket_name) or []
            for row in rows:
                delta = _clean_optional_float((row or {}).get("delta"))
                if delta is None:
                    continue
                score = abs(delta)
                if best is None or score > float(best.get("_score") or 0.0):
                    best = {
                        "_score": score,
                        "dimension": dim,
                        "label": (row or {}).get("label") or "Unknown",
                        "delta": delta,
                        "entity_id": (row or {}).get("entity_id") or (row or {}).get("label"),
                        "delta_pct_label": (row or {}).get("delta_pct_label"),
                    }
        if best:
            best.pop("_score", None)
        return best

    biggest_gain = _best_mover("positive")
    biggest_decliner = _best_mover("negative")

    improved: List[str] = []
    if revenue_mom_delta is not None and revenue_mom_delta > 0:
        pct_text = "n/a" if revenue_mom_delta_pct is None else f"{revenue_mom_delta_pct:+.1f}%"
        improved.append(f"Revenue improved {_signed_money(revenue_mom_delta)} versus {primary_compare_label.lower()} ({pct_text}).")
    if volume_effect is not None and volume_effect > 0:
        improved.append(f"Volume contributed {_signed_money(volume_effect)} to the primary comparison uplift.")
    if biggest_gain:
        improved.append(
            f"Strongest gainer: {biggest_gain.get('label') or 'Unknown'} ({_signed_money(_clean_optional_float(biggest_gain.get('delta')))})."
        )
    if new_share_pct is not None and new_share_pct > 0:
        improved.append(f"New-customer share is {new_share_pct:.1f}% of active customers.")

    declined: List[str] = []
    if revenue_mom_delta is not None and revenue_mom_delta < 0:
        pct_text = "n/a" if revenue_mom_delta_pct is None else f"{revenue_mom_delta_pct:+.1f}%"
        declined.append(f"Revenue declined {_signed_money(revenue_mom_delta)} versus {primary_compare_label.lower()} ({pct_text}).")
    if mix_effect is not None and mix_effect < 0:
        declined.append(f"Mix reduced revenue by {_signed_money(mix_effect)} versus {primary_compare_label.lower()}.")
    if biggest_decliner:
        declined.append(
            f"Biggest decline: {biggest_decliner.get('label') or 'Unknown'} ({_signed_money(_clean_optional_float(biggest_decliner.get('delta')))})."
        )
    profit_yoy_delta_pct = _clean_optional_float(deltas.get("profit", {}).get("yoy_pct"))
    if profit_yoy_delta_pct is not None and profit_yoy_delta_pct < 0:
        declined.append(f"Profit is down {abs(profit_yoy_delta_pct):.1f}% versus prior year.")

    watchouts: List[str] = []
    if margin_risk_rows:
        watchouts.append(f"{len(margin_risk_rows)} SKUs are below target margin.")
    top1_customer = _clean_optional_float((concentration.get("customer") or {}).get("top1_share"))
    customer_hhi = _clean_optional_float((concentration.get("customer") or {}).get("hhi"))
    if top1_customer is not None:
        if customer_hhi is not None:
            watchouts.append(f"Top customer concentration is {top1_customer:.1f}% with HHI {customer_hhi:.0f}.")
        else:
            watchouts.append(f"Top customer concentration is {top1_customer:.1f}%.")
    if cost_coverage_pct is not None and cost_coverage_pct < 90:
        watchouts.append(f"Cost coverage is {cost_coverage_pct:.1f}% and limits finance confidence.")
    if packs_coverage_pct is not None and packs_coverage_pct < 98:
        watchouts.append(f"Packs coverage is {packs_coverage_pct:.1f}% and may distort weighted metrics.")
    if product_missing:
        watchouts.append(f"{int(product_missing)} rows still have missing product mapping.")
    if window_contract.terminal_period_incomplete:
        watchouts.append(window_contract.note)

    recommended_actions: List[Dict[str, Any]] = []
    if cost_coverage_pct is not None and cost_coverage_pct < 90:
        recommended_actions.append(
            {
                "title": "Restore cost coverage",
                "detail": "Backfill missing cost so profit, margin, and recommendation views stay decision-grade.",
                "target": "data_health",
                "severity": "warning",
            }
        )
    if margin_risk_rows:
        recommended_actions.append(
            {
                "title": "Review margin-risk SKUs",
                "detail": f"Prioritize {len(margin_risk_rows)} SKUs currently below target margin.",
                "target": "margin_risk",
                "severity": "warning",
            }
        )
    if biggest_decliner:
        recommended_actions.append(
            {
                "title": "Inspect the largest decline",
                "detail": f"Review {biggest_decliner.get('label') or 'the top decliner'} before the next trading cycle.",
                "target": f"movers_{biggest_decliner.get('dimension') or 'customer'}",
                "severity": "negative",
            }
        )
    if top1_customer is not None and top1_customer >= 20:
        recommended_actions.append(
            {
                "title": "Protect concentration exposure",
                "detail": "Pressure-test account concentration and diversify near-term pipeline coverage.",
                "target": "concentration",
                "severity": "warning",
            }
        )

    biggest_win_card: Dict[str, Any]
    if biggest_gain:
        biggest_win_card = {
            "title": f"Top {str(biggest_gain.get('dimension') or 'customer').title()} gainer",
            "value": biggest_gain.get("delta"),
            "value_fmt": "currency",
            "detail": biggest_gain.get("label"),
            "severity": "positive",
            "link": {"kind": biggest_gain.get("dimension"), "id": biggest_gain.get("entity_id")},
        }
    else:
        biggest_win_card = {
            "title": "Revenue momentum",
            "value": revenue_mom_delta,
            "value_fmt": "currency",
            "detail": f"Revenue change versus {primary_compare_label.lower()}",
            "severity": "positive" if (revenue_mom_delta or 0) >= 0 else "neutral",
        }

    biggest_decline_card: Dict[str, Any]
    if biggest_decliner:
        biggest_decline_card = {
            "title": f"Top {str(biggest_decliner.get('dimension') or 'customer').title()} decline",
            "value": biggest_decliner.get("delta"),
            "value_fmt": "currency",
            "detail": biggest_decliner.get("label"),
            "severity": "negative",
            "link": {"kind": biggest_decliner.get("dimension"), "id": biggest_decliner.get("entity_id")},
        }
    else:
        biggest_decline_card = {
            "title": "Largest decline",
            "value": None,
            "value_fmt": "currency",
            "detail": "No material declines detected in the active window.",
            "severity": "neutral",
        }

    if cost_coverage_pct is not None and cost_coverage_pct < 90:
        key_risk_card = {
            "title": "Finance coverage risk",
            "value": cost_coverage_pct,
            "value_fmt": "percent",
            "detail": "Cost coverage is below the decision-safe threshold.",
            "severity": "warning",
        }
    elif margin_risk_rows:
        key_risk_card = {
            "title": "Margin watchlist",
            "value": len(margin_risk_rows),
            "value_fmt": "number",
            "detail": "High-revenue SKUs are under target margin.",
            "severity": "warning",
        }
    elif top1_customer is not None:
        key_risk_card = {
            "title": "Concentration exposure",
            "value": top1_customer,
            "value_fmt": "percent",
            "detail": "Largest customer share of revenue.",
            "severity": "warning" if top1_customer >= 20 else "info",
        }
    else:
        key_risk_card = {
            "title": "Key risk",
            "value": None,
            "value_fmt": "number",
            "detail": "No elevated commercial risk flagged for the active window.",
            "severity": "info",
        }

    primary_action = (recommended_actions or [None])[0] or {}
    executive_briefing = {
        "biggest_win": biggest_win_card,
        "biggest_decline": biggest_decline_card,
        "key_risk": key_risk_card,
        "top_action": {
            "title": primary_action.get("title") or "Maintain current operating rhythm",
            "value": None,
            "value_fmt": "text",
            "detail": primary_action.get("detail") or "No urgent remediation is required for the active filters.",
            "severity": primary_action.get("severity") or "info",
            "target": primary_action.get("target"),
        },
        "improved": improved[:4],
        "declined": declined[:4],
        "watchouts": watchouts[:4],
        "recommended_actions": recommended_actions[:4],
    }

    executive_scorecard = {
        "headline": {
            "revenue": totals.get("revenue"),
            "profit": totals.get("profit"),
            "margin_pct": totals.get("margin_pct"),
            "revenue_mom": deltas.get("revenue", {}).get("mom"),
            "revenue_mom_pct": deltas.get("revenue", {}).get("mom_pct"),
            "revenue_yoy": deltas.get("revenue", {}).get("yoy"),
            "revenue_yoy_pct": deltas.get("revenue", {}).get("yoy_pct"),
            "profit_mom": deltas.get("profit", {}).get("mom"),
            "profit_mom_pct": deltas.get("profit", {}).get("mom_pct"),
            "profit_yoy": deltas.get("profit", {}).get("yoy"),
            "profit_yoy_pct": deltas.get("profit", {}).get("yoy_pct"),
            "margin_mom": deltas.get("margin_pct", {}).get("mom"),
            "margin_mom_pct": deltas.get("margin_pct", {}).get("mom_pct"),
            "margin_yoy": deltas.get("margin_pct", {}).get("yoy"),
            "margin_yoy_pct": deltas.get("margin_pct", {}).get("yoy_pct"),
            "comparison_label": comparison_label,
            "primary_compare_label": primary_compare_label,
            "primary_delta_label": primary_delta_label,
            "current_window_label": window_contract.current_window_label,
            "prior_window_label": window_contract.prior_window_label,
            "yoy_label": window_contract.yoy_label,
            "yoy_window_label": window_contract.yoy_window_label,
            "comparison_note": window_contract.note,
            "is_partial_period": bool(window_contract.is_partial_period),
        },
        "unit_economics": {
            "asp": totals.get("asp"),
            "aov": totals.get("aov"),
            "profit_per_order": profit_per_order,
            "profit_per_lb": profit_per_lb,
        },
        "growth_retention": {
            "new_customers": new_customers,
            "new_customer_share_pct": new_share_pct,
            "returning_customer_share_pct": returning_share_pct,
            "active_customers_current": customer_current,
            "active_customers_previous": int(customer_ops.get("previous") or 0),
        },
        "risk_indicators": {
            "margin_risk_sku_count": len(margin_risk_rows),
            "margin_risk_revenue_share_pct": margin_risk_revenue_share,
            "top1_customer_share_pct": (concentration.get("customer") or {}).get("top1_share"),
            "customer_hhi": (concentration.get("customer") or {}).get("hhi"),
            "cost_coverage_pct": cost_coverage_pct,
            "packs_coverage_pct": packs_coverage_pct,
            "product_mapping_missing": product_missing,
        },
        "narrative": narrative,
    }

    # Every server-side aggregate over the watchlist has been computed by now,
    # so the rows themselves can be capped for transport. The two figures the
    # browser used to derive by walking the whole array travel with it, so the
    # rendered count and revenue share stay exactly what they were before the
    # cap - see tests/test_overview_bundle_payload.py.
    profitability["margin_risk_total_count"] = len(margin_risk_rows)
    profitability["margin_risk_revenue_share_pct"] = margin_risk_revenue_share
    if len(margin_risk_rows) > MARGIN_RISK_PAYLOAD_LIMIT:
        profitability["margin_risk"] = margin_risk_rows[:MARGIN_RISK_PAYLOAD_LIMIT]
        profitability["margin_risk_truncated"] = True
    else:
        profitability["margin_risk_truncated"] = False
    insights["margin_risk"] = profitability["margin_risk"]

    overview_metrics = {
        "window": window,
        "kpis": totals,
        "executive": {
            "revenue_current": totals.get("revenue"),
            "revenue_mom_delta": deltas.get("revenue", {}).get("mom"),
            "revenue_mom_delta_pct": deltas.get("revenue", {}).get("mom_pct"),
            "main_driver_sentence": (drivers.get("mom") or {}).get("message"),
            "cost_coverage_pct": cost_coverage_pct,
            "primary_delta_label": primary_delta_label,
            "primary_compare_label": primary_compare_label,
            "comparison_label": comparison_label,
            "comparison_note": window_contract.note,
            "current_window_label": window_contract.current_window_label,
            "prior_window_label": window_contract.prior_window_label,
            "yoy_label": window_contract.yoy_label,
            "yoy_window_label": window_contract.yoy_window_label,
            "trust_summary": health.get("comparison_note"),
            "narrative": narrative,
        },
        "movers": top_movers,
        "decomposition": drivers,
        "concentration": concentration,
        "profitability": profitability,
        "momentum": operations.get("customers") or {},
        "scorecard": executive_scorecard,
        "mix": {
            "revenue": mix,
            "operational": (operations or {}).get("mix") or {},
            "weekday": (operations or {}).get("weekday") or [],
        },
        "briefing": executive_briefing,
    }

    payload: Dict[str, Any] = {
        "schema": BUNDLE_SCHEMA_VERSION,
        "kpis": totals,
        "deltas": deltas,
        "trend": trend,
        "mix": mix,
        "pareto": pareto,
        "top_movers": top_movers,
        "health": health,
        "drivers": drivers,
        "operations": operations,
        "concentration": concentration,
        "profitability": profitability,
        "insights": insights,
        "executive_briefing": executive_briefing,
        "executive_scorecard": executive_scorecard,
        "overview_metrics": overview_metrics,
        "meta": {
            "filters": filters_payload,
            "filter_labels": filters_labels,
            "window": window,
            "last_refresh": last_refresh,
            "data_cutoff": refresh_meta.get("data_cutoff"),
            "refresh_age_days": refresh_meta.get("refresh_age_days"),
            "refresh_age_hours": refresh_meta.get("refresh_age_hours"),
            "version": version,
            "has_data": bool(rows_total),
            "cache_hit": False,
            "include_current_month": include_current_month,
            "defaulted_window": defaulted_flag,
            "feature_flags": {
                "driver_decomp_v2": bool(drivers.get("enabled")),
            },
        },
    }
    if cache_key:
        payload["meta"]["cache_key"] = cache_key

    return {"payload": payload, "monthly": monthly_df}

def get_filtered_frame(user: Any, filters: FilterParams) -> FrameContext:
    """Deprecated: kept for backward compatibility; use get_bundle_context instead."""
    raise RuntimeError("get_filtered_frame is deprecated. Use get_bundle_context instead.")


def _series(df: pd.DataFrame, colmap: Dict[str, Optional[str]], value_col: str) -> pd.Series:
    date_col = colmap.get("date")
    if df is None or df.empty or not date_col or date_col not in df.columns:
        return pd.Series(dtype=float)
    if not value_col or value_col not in df.columns:
        return pd.Series(dtype=float)
    s = pd.to_datetime(df[date_col], errors="coerce")
    numeric = au.to_numeric_safe(df[value_col])
    work = pd.DataFrame({"Date": s, "value": numeric})
    work = work.dropna(subset=["Date"])
    if work.empty:
        return pd.Series(dtype=float)
    grouped = work.groupby(work["Date"].dt.to_period("M"))["value"].sum().sort_index()
    return grouped


def _aggregate(df: pd.DataFrame, colmap: Dict[str, Optional[str]]) -> Dict[str, float]:
    revenue_col = colmap.get("revenue")
    cost_col = colmap.get("cost")
    qty_col = colmap.get("qty")
    weight_col = colmap.get("weight")
    order_col = colmap.get("order_id")
    customer_col = colmap.get("customer_id")

    revenue = au.safe_sum(df.get(revenue_col)) if revenue_col else 0.0
    cost = au.safe_sum(df.get(cost_col)) if cost_col else 0.0
    profit = revenue - cost if cost_col else None
    qty = au.safe_sum(df.get(qty_col)) if qty_col else 0.0
    weight = au.safe_sum(df.get(weight_col)) if weight_col else 0.0
    orders = int(df[order_col].dropna().nunique()) if order_col and order_col in df.columns else 0
    customers = int(df[customer_col].dropna().nunique()) if customer_col and customer_col in df.columns else 0

    asp = au.safe_div(revenue, qty) if qty else None
    aov = au.safe_div(revenue, orders) if orders else None
    rev_per_customer = au.safe_div(revenue, customers) if customers else None
    margin_div = au.safe_div(profit, revenue)
    margin_pct = (margin_div * 100) if (margin_div is not None and profit is not None and revenue) else None
    roi_pct = au.safe_div(profit, cost) * 100 if profit is not None and cost else None

    return {
        "revenue": float(revenue or 0.0),
        "cost": float(cost or 0.0),
        "profit": float(profit or 0.0) if profit is not None else None,
        "qty": float(qty or 0.0),
        "weight": float(weight or 0.0),
        "orders": orders,
        "customers": customers,
        "asp": float(asp or 0.0) if asp is not None else None,
        "aov": float(aov or 0.0) if aov is not None else None,
        "rev_per_customer": float(rev_per_customer or 0.0) if rev_per_customer is not None else None,
        "margin_pct": float(margin_pct or 0.0) if margin_pct is not None else None,
        "roi_pct": float(roi_pct or 0.0) if roi_pct is not None else None,
    }


def _delta_pct(current: float, prior: float) -> Optional[float]:
    pct = om.delta_percent(current, prior, abs_prior=True)
    if pct is None:
        return None
    return round(float(pct), 2)


def build_summary(filters: FilterParams) -> Dict[str, Any]:
    ctx: Dict[str, Any] | None = None
    if current_app.config.get("TESTING"):
        try:
            frame_ctx = get_filtered_frame(current_user, filters)
            if isinstance(frame_ctx, FrameContext):
                df = frame_ctx.df if isinstance(frame_ctx.df, pd.DataFrame) else pd.DataFrame()
                colmap = frame_ctx.colmap or au.column_map(df)
                monthly = _monthly_rollup(df, colmap)

                def _series_from_monthly(col: str) -> pd.Series:
                    if isinstance(monthly, pd.DataFrame) and col in monthly.columns:
                        return monthly[col]
                    return pd.Series(dtype=float)

                deltas = {
                    "revenue": _deltas_from_series(_series_from_monthly("revenue")),
                    "cost": _deltas_from_series(_series_from_monthly("cost")),
                    "profit": _deltas_from_series(_series_from_monthly("profit")),
                    "margin_pct": _deltas_from_series(_series_from_monthly("margin_pct")),
                    "orders": _deltas_from_series(_series_from_monthly("orders")),
                    "customers": _deltas_from_series(_series_from_monthly("customers")),
                }
                deltas["qty"] = _deltas_from_series(_series_from_monthly("qty"))
                deltas["aov"] = _deltas_from_series(_series_from_monthly("aov"))
                deltas["asp"] = _deltas_from_series(_series_from_monthly("asp"))

                meta = {
                    "has_data": bool(len(df)),
                    "missing_columns": list(frame_ctx.missing or []),
                    "window": frame_ctx.window or {},
                    "version": frame_ctx.version,
                    "last_refresh": frame_ctx.last_refresh,
                    "cache_hit": bool(frame_ctx.cache_hit),
                    "source": "overview_v2.testing_frame_ctx",
                }
                payload = {"kpis": _aggregate(df, colmap), "meta": meta, "deltas": deltas}
                ctx = {"payload": payload, "monthly": monthly, "cache_hit": bool(frame_ctx.cache_hit)}
        except RuntimeError:
            ctx = None
        except Exception:
            current_app.logger.exception("overview_v2.build_summary.testing_fallback_failed")
            ctx = None
    if ctx is None:
        ctx = get_bundle_context(filters)
    payload = ctx.get("payload", {})
    monthly = ctx.get("monthly")
    if not isinstance(monthly, pd.DataFrame):
        monthly = pd.DataFrame()
    revenue_series = monthly.get("revenue", pd.Series(dtype=float))

    mom_pct = payload.get("deltas", {}).get("revenue", {}).get("mom_pct")
    yoy_pct = payload.get("deltas", {}).get("revenue", {}).get("yoy_pct")
    rolling_pct = None
    if isinstance(revenue_series, pd.Series) and not revenue_series.empty:
        last_3m = float(revenue_series.tail(3).sum())
        prior_3m = float(revenue_series.tail(6).head(3).sum()) if len(revenue_series) >= 6 else 0.0
        rolling_pct = _delta_pct(last_3m, prior_3m) if prior_3m else None

    meta = payload.get("meta", {}).copy()
    meta["cache_hit"] = bool(ctx.get("cache_hit") or meta.get("cache_hit"))
    summary = {
        "kpis": payload.get("kpis", {}),
        "deltas": {
            "mom_pct": mom_pct,
            "rolling_3m_pct": rolling_pct,
            "yoy_pct": yoy_pct,
        },
        "meta": meta,
    }
    return summary


def build_trend(filters: FilterParams, months: int = 12, exclude_partial: bool = True) -> Dict[str, Any]:
    ctx = get_bundle_context(filters)
    monthly = ctx.get("monthly")
    if not isinstance(monthly, pd.DataFrame):
        monthly = pd.DataFrame()
    work = monthly.copy() if isinstance(monthly, pd.DataFrame) else pd.DataFrame()
    if not work.empty and exclude_partial:
        current_month = pd.Timestamp.utcnow().tz_localize(None).to_period("M")
        if work.index.max() == current_month and len(work) > 1:
            work = work.iloc[:-1]
    if months and months > 0 and not work.empty:
        work = work.tail(months)
    window_meta = (((ctx.get("payload") or {}).get("meta") or {}).get("window") or {}) if isinstance(ctx.get("payload"), dict) else {}
    date_type = window_meta.get("date_type") if isinstance(window_meta, dict) else None
    labels = _trend_labels_from_index(work.index, date_type=date_type) if not work.empty else []

    def _vals(col: str) -> List[Any]:
        if col not in work.columns:
            return []
        return [float(v) if pd.notna(v) else None for v in work[col]]

    payload = {
        "months": [str(idx) for idx in work.index] if not work.empty else [],
        "labels": labels,
        "revenue": _vals("revenue"),
        "qty": _vals("qty"),
        "asp": _vals("asp"),
        "meta": {
            "has_data": bool(len(work)),
            "cache_hit": bool(ctx.get("cache_hit")),
            "date_type": date_type,
            "bucket_label": window_meta.get("trend_bucket_label") if isinstance(window_meta, dict) else None,
        },
    }
    return payload


def _group_mix(df: pd.DataFrame, group_col: Optional[str], colmap: Dict[str, Optional[str]], limit: int = 8) -> List[Dict[str, Any]]:
    revenue_col = colmap.get("revenue")
    if not group_col or group_col not in df.columns or not revenue_col or revenue_col not in df.columns:
        return []
    work = df[[group_col, revenue_col]].copy()
    work[revenue_col] = au.to_numeric_safe(work[revenue_col])
    grouped = work.groupby(group_col)[revenue_col].sum().sort_values(ascending=False)
    total = float(grouped.sum() or 0.0)
    rows: List[Dict[str, Any]] = []
    for label, value in grouped.head(limit).items():
        share = au.safe_div(value, total) * 100 if total else 0.0
        rows.append({"label": str(label) if pd.notna(label) else "Unknown", "revenue": float(value or 0.0), "share": float(share or 0.0)})
    other_sum = float(grouped.iloc[limit:].sum() or 0.0) if len(grouped) > limit else 0.0
    if other_sum:
        rows.append({"label": "Other", "revenue": other_sum, "share": float(au.safe_div(other_sum, total) * 100 if total else 0.0)})
    return rows


def build_mix(filters: FilterParams, dim: str = "region") -> Dict[str, Any]:
    ctx = get_bundle_context(filters)
    key = (dim or "region").lower()
    mix_payload = ctx.get("payload", {}).get("mix", {})
    rows = mix_payload.get(key, [])
    meta = ctx.get("payload", {}).get("meta", {}).copy()
    meta["cache_hit"] = bool(ctx.get("cache_hit") or meta.get("cache_hit"))
    meta["has_data"] = bool(rows)
    return {"dimension": key, "rows": rows, "meta": meta}


def build_top(filters: FilterParams, metric: str = "product", limit: int = 10) -> Dict[str, Any]:
    ctx = get_bundle_context(filters)
    metric_key = (metric or "product").lower()
    mix_data = ctx.get("payload", {}).get("mix", {})
    rows_src = mix_data.get(metric_key, [])
    total = sum(float(r.get("value", 0.0) or 0.0) for r in rows_src)
    rows = []
    for rec in rows_src[:limit]:
        revenue = float(rec.get("value", 0.0) or 0.0)
        share = float(au.safe_div(revenue, total) * 100 if total else 0.0)
        rows.append({"label": rec.get("label"), "revenue": revenue, "share": share})
    meta = ctx.get("payload", {}).get("meta", {}).copy()
    meta["cache_hit"] = bool(ctx.get("cache_hit") or meta.get("cache_hit"))
    meta["has_data"] = bool(rows)
    return {"metric": metric_key, "rows": rows, "meta": meta}


def build_pareto(filters: FilterParams, dim: str = "product") -> Dict[str, Any]:
    ctx = get_bundle_context(filters)
    dim_key = (dim or "product").lower()
    chart = ctx.get("payload", {}).get("pareto", {}).get(dim_key, {"labels": [], "values": [], "cum_pct": []})
    meta = ctx.get("payload", {}).get("meta", {}).copy()
    meta["cache_hit"] = bool(ctx.get("cache_hit") or meta.get("cache_hit"))
    meta["has_data"] = bool(chart.get("labels"))
    payload = {"dimension": dim_key, **chart, "meta": meta}
    return payload



def build_alerts(filters: FilterParams) -> Dict[str, Any]:
    ctx = get_bundle_context(filters)
    payload = ctx.get("payload", {})
    meta = payload.get("meta", {}).copy()
    meta["cache_hit"] = bool(ctx.get("cache_hit") or meta.get("cache_hit"))
    alerts: List[Dict[str, Any]] = []

    summary = build_summary(filters)
    kpis = summary.get("kpis", {})
    margin_pct = kpis.get("margin_pct")
    profitability = payload.get("profitability", {}) if isinstance(payload.get("profitability"), dict) else {}
    minimum_margin_pct = profitability.get("minimum_margin_pct")
    target_margin_pct = profitability.get("target_margin_pct")
    momentum = summary.get("deltas", {}).get("rolling_3m_pct")

    top_customers = build_top(filters, metric="customer", limit=1).get("rows", [])
    if top_customers:
        top_share = top_customers[0].get("share") or 0.0
        if top_share > 35:
            alerts.append({"severity": "warning", "title": "Concentration risk", "detail": f"Top customer is {top_share:.1f}% of revenue"})

    margin_status = margin_rules.classify_margin_status(margin_pct, minimum_margin_pct, target_margin_pct)
    if margin_pct is not None:
        if margin_status.get("status_key") in {"red", "orange"}:
            detail = f"Margin at {margin_pct:.1f}% is below minimum guardrail"
            if minimum_margin_pct is not None:
                detail = f"Margin at {margin_pct:.1f}% is below the {minimum_margin_pct:.1f}% minimum guardrail"
            alerts.append({"severity": "danger", "title": "Margin pressure", "detail": detail})
        elif margin_status.get("status_key") == "yellow":
            detail = f"Margin at {margin_pct:.1f}% is between minimum and target"
            if target_margin_pct is not None:
                detail = f"Margin at {margin_pct:.1f}% is still below the {target_margin_pct:.1f}% target"
            alerts.append({"severity": "warning", "title": "Margin below target", "detail": detail})
    if momentum is not None and momentum < -5:
        alerts.append({"severity": "warning", "title": "Momentum decline", "detail": f"Revenue trending {momentum:.1f}% vs prior 3 months"})

    health = payload.get("health", {})
    if health.get("product_mapping_missing"):
        alerts.append({"severity": "info", "title": "Data gaps", "detail": "Missing product identifiers detected"})
    if not meta.get("has_data"):
        alerts.append({"severity": "danger", "title": "No data", "detail": "No rows for the selected filters"})

    return {"alerts": alerts, "meta": meta}


def build_health(filters: FilterParams) -> Dict[str, Any]:
    ctx = get_bundle_context(filters)
    payload = ctx.get("payload", {})
    health = payload.get("health", {})
    meta = payload.get("meta", {}).copy()
    meta["cache_hit"] = bool(ctx.get("cache_hit") or meta.get("cache_hit"))
    missing_pct = {
        "cost": health.get("cost_missing_pct"),
        "pack": health.get("pack_missing_pct"),
        "product": health.get("product_mapping_missing"),
    }
    return {
        "rows": int(health.get("rows", 0) or 0),
        "missing_columns": [],
        "missing_pct": missing_pct,
        "meta": meta,
    }


_DETAIL_SCHEMA_REV = "overview_detail_tables_v2"
_DETAIL_MIN_DENOM = 500.0
_DETAIL_COLUMNS = [
    "Date",
    "ShipDate",
    "OrderDate",
    "Revenue",
    "TotalRevenue",
    "Sales",
    "revenue_ordered",
    "revenue_shipped",
    "Cost",
    "CostPrice",
    "TotalCost",
    "ExtCost",
    "COGS",
    "QuantityShipped",
    "QuantityOrdered",
    "Qty",
    "Units",
    "ItemCount",
    "pack_item_count_sum",
    "CustomerId",
    "CustomerID",
    "CustomerName",
    "ProductId",
    "ProductID",
    "SKU",
    "ProductName",
    "RegionName",
    "Region",
    "SupplierName",
    "SupplierId",
    "ProteinType",
    "ProteinName",
    "Category",
    "ProductCategory",
    "Protein",
]


def _overview_detail_cache_key(
    filters: FilterParams,
    *,
    include_current_month: bool,
    defaulted_window: bool,
    block_id: str,
) -> str:
    try:
        from app.services import fact_store  # type: ignore

        dataset_version = fact_store.cache_buster()
    except Exception:
        dataset_version = _dataset_marker()
    return filters_cache_key(
        current_user,
        filters,
        extras={
            "scope": "overview_v2_detail",
            "block_id": block_id,
            "include_current_month": bool(include_current_month),
            "defaulted_window": bool(defaulted_window),
            "dataset_version": dataset_version,
            "dataset_max_date": _dataset_marker(),
            "detail_schema": _DETAIL_SCHEMA_REV,
            "bundle_rev": _BUNDLE_REV,
        },
    )


def _pick_col(df: pd.DataFrame, *candidates: str) -> Optional[str]:
    if df is None or df.empty:
        return None
    lookup = {str(col).lower(): str(col) for col in df.columns}
    for cand in candidates:
        if not cand:
            continue
        if cand in df.columns:
            return cand
        resolved = lookup.get(str(cand).lower())
        if resolved:
            return resolved
    return None


def _clean_text_series(series: pd.Series) -> pd.Series:
    out = series.astype("string")
    out = out.fillna("")
    out = out.str.strip()
    return out


def _series_or_empty(df: pd.DataFrame, col: Optional[str], default: Any = "") -> pd.Series:
    if col and col in df.columns:
        return df[col]
    return pd.Series([default] * len(df), index=df.index)


def _first_non_empty(values: pd.Series) -> str:
    try:
        for raw in values:
            sval = str(raw or "").strip()
            if sval:
                return sval
    except Exception:
        pass
    return "Unknown"


def _prepare_detail_frame(
    filters: FilterParams,
    *,
    include_current_month: bool,
) -> tuple[pd.DataFrame, om.WindowContract]:
    from app.core.data_service import get_fact_df  # Imported lazily to avoid cyclic imports.

    window_contract = om.resolve_window_contract(filters, include_current_month=include_current_month)
    expanded = replace(
        filters,
        start=pd.Timestamp(window_contract.history_start),
        end=pd.Timestamp(window_contract.current_end),
    )
    raw = get_fact_df(filters=expanded, columns=_DETAIL_COLUMNS, best_effort=True)
    if raw is None or raw.empty:
        return pd.DataFrame(), window_contract

    date_col = _pick_col(raw, "Date", "ShipDate", "OrderDate")
    revenue_col = _pick_col(raw, "Revenue", "TotalRevenue", "Sales", "revenue_ordered", "revenue_shipped")
    cost_col = _pick_col(raw, "Cost", "CostPrice", "TotalCost", "ExtCost", "COGS")
    qty_col = _pick_col(raw, "QuantityShipped", "QuantityOrdered", "Qty", "Units", "ItemCount", "pack_item_count_sum")
    customer_id_col = _pick_col(raw, "CustomerId", "CustomerID")
    customer_name_col = _pick_col(raw, "CustomerName")
    product_id_col = _pick_col(raw, "ProductId", "ProductID", "SKU")
    product_name_col = _pick_col(raw, "ProductName")
    region_col = _pick_col(raw, "RegionName", "Region")
    supplier_col = _pick_col(raw, "SupplierName", "SupplierId")
    protein_col = _pick_col(raw, "Protein", "ProteinType", "ProteinName", "Category", "ProductCategory")

    if not date_col or not revenue_col:
        return pd.DataFrame(), window_contract

    frame = pd.DataFrame(index=raw.index)
    frame["date"] = pd.to_datetime(raw[date_col], errors="coerce")
    frame["revenue"] = pd.to_numeric(raw[revenue_col], errors="coerce").fillna(0.0)
    if cost_col and cost_col in raw.columns:
        frame["cost"] = pd.to_numeric(raw[cost_col], errors="coerce")
    else:
        frame["cost"] = np.nan
    if qty_col and qty_col in raw.columns:
        frame["qty"] = pd.to_numeric(raw[qty_col], errors="coerce").fillna(0.0)
    else:
        frame["qty"] = 0.0

    frame["customer_id"] = _clean_text_series(_series_or_empty(raw, customer_id_col))
    frame["customer_name"] = _clean_text_series(_series_or_empty(raw, customer_name_col))
    frame["product_id"] = _clean_text_series(_series_or_empty(raw, product_id_col))
    frame["product_name"] = _clean_text_series(_series_or_empty(raw, product_name_col))
    frame["region"] = _clean_text_series(_series_or_empty(raw, region_col))
    frame["supplier"] = _clean_text_series(_series_or_empty(raw, supplier_col))
    frame["protein"] = _clean_text_series(_series_or_empty(raw, protein_col))

    frame = frame.dropna(subset=["date"])
    if frame.empty:
        return frame, window_contract
    frame["date"] = frame["date"].dt.tz_localize(None)
    return frame, window_contract


def _window_slice(frame: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=frame.columns if isinstance(frame, pd.DataFrame) else [])
    start_ts = pd.Timestamp(start)
    end_excl = pd.Timestamp(end) + pd.Timedelta(days=1)
    mask = (frame["date"] >= start_ts) & (frame["date"] < end_excl)
    return frame.loc[mask].copy()


def _mover_delta_meta(current: float, previous: float) -> tuple[Optional[float], str, bool]:
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


def _normalize_mover_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        row = dict(rec)
        current_val = _clean_optional_float(row.get("current")) or 0.0
        previous_val = _clean_optional_float(row.get("previous")) or 0.0
        delta_val = _clean_optional_float(row.get("delta"))
        if delta_val is None:
            delta_val = current_val - previous_val
        pct_val, pct_label, low_sample = _mover_delta_meta(current_val, previous_val)
        row["current"] = float(current_val)
        row["previous"] = float(previous_val)
        row["delta"] = float(delta_val)
        row["delta_pct"] = None if pct_val is None else float(pct_val)
        row["delta_pct_label"] = pct_label
        row["low_sample"] = bool(low_sample)
        row["direction"] = "gainer" if float(delta_val) >= 0 else "decliner"
        out.append(row)
    return out


def _build_movers_table(
    current: pd.DataFrame,
    prior: pd.DataFrame,
    *,
    label_col: str,
    id_col: Optional[str],
) -> pd.DataFrame:
    out_cols = [
        "label",
        "entity_id",
        "current",
        "previous",
        "delta",
        "delta_pct",
        "delta_pct_label",
        "low_sample",
        "qty_current",
        "qty_previous",
        "qty_delta",
        "asp_current",
        "asp_previous",
        "asp_delta",
        "direction",
    ]
    if current.empty and prior.empty:
        return pd.DataFrame(columns=out_cols)

    def _rollup_map(source: pd.DataFrame) -> Dict[str, Dict[str, float | str]]:
        if source.empty:
            return {}
        tmp = source.copy()
        entity_src = _clean_text_series(tmp[id_col]) if id_col and id_col in tmp.columns else _clean_text_series(tmp[label_col])
        label_src = _clean_text_series(tmp[label_col]) if label_col in tmp.columns else pd.Series([""] * len(tmp), index=tmp.index)
        entity_resolved = entity_src.where(entity_src.ne(""), label_src.where(label_src.ne(""), "Unknown"))
        label_resolved = label_src.where(label_src.ne(""), entity_resolved).where(lambda s: s.ne(""), "Unknown")
        revenue_series = pd.to_numeric(tmp.get("revenue"), errors="coerce").fillna(0.0)
        qty_series = pd.to_numeric(tmp.get("qty"), errors="coerce").fillna(0.0)
        bucket: Dict[str, Dict[str, float | str]] = {}
        for idx in tmp.index:
            entity_id = str(entity_resolved.loc[idx] or "Unknown").strip() or "Unknown"
            label = str(label_resolved.loc[idx] or entity_id).strip() or entity_id
            rec = bucket.get(entity_id)
            if rec is None:
                rec = {"label": label, "revenue": 0.0, "qty": 0.0}
                bucket[entity_id] = rec
            rec["revenue"] = float(rec["revenue"]) + float(revenue_series.loc[idx] or 0.0)
            rec["qty"] = float(rec["qty"]) + float(qty_series.loc[idx] or 0.0)
            if (not str(rec.get("label") or "").strip()) or str(rec.get("label")).strip() == "Unknown":
                rec["label"] = label
        return bucket

    cur_map = _rollup_map(current)
    prv_map = _rollup_map(prior)
    if not cur_map and not prv_map:
        return pd.DataFrame(columns=out_cols)

    rows: List[Dict[str, Any]] = []
    for entity_id in set(cur_map.keys()) | set(prv_map.keys()):
        cur_rec = cur_map.get(entity_id, {"label": "", "revenue": 0.0, "qty": 0.0})
        prv_rec = prv_map.get(entity_id, {"label": "", "revenue": 0.0, "qty": 0.0})
        current_value = float(cur_rec.get("revenue", 0.0) or 0.0)
        previous_value = float(prv_rec.get("revenue", 0.0) or 0.0)
        qty_current = float(cur_rec.get("qty", 0.0) or 0.0)
        qty_previous = float(prv_rec.get("qty", 0.0) or 0.0)
        if abs(current_value) <= 0 and abs(previous_value) <= 0:
            continue
        delta = current_value - previous_value
        qty_delta = qty_current - qty_previous
        asp_current = (current_value / qty_current) if qty_current > 0 else np.nan
        asp_previous = (previous_value / qty_previous) if qty_previous > 0 else np.nan
        delta_pct, delta_pct_label, low_sample = _mover_delta_meta(current_value, previous_value)
        label = str(cur_rec.get("label") or prv_rec.get("label") or entity_id or "Unknown").strip() or "Unknown"
        rows.append(
            {
                "label": label,
                "entity_id": str(entity_id),
                "current": current_value,
                "previous": previous_value,
                "delta": delta,
                "delta_pct": delta_pct,
                "delta_pct_label": delta_pct_label,
                "low_sample": bool(low_sample),
                "qty_current": qty_current,
                "qty_previous": qty_previous,
                "qty_delta": qty_delta,
                "asp_current": asp_current,
                "asp_previous": asp_previous,
                "asp_delta": asp_current - asp_previous if np.isfinite(asp_current) and np.isfinite(asp_previous) else np.nan,
                "direction": "gainer" if delta >= 0 else "decliner",
            }
        )

    if not rows:
        return pd.DataFrame(columns=out_cols)
    rows.sort(key=lambda r: float(r.get("delta", 0.0)), reverse=True)
    return pd.DataFrame(rows, columns=out_cols)


def _build_concentration_table(current: pd.DataFrame, *, label_col: str, id_col: Optional[str]) -> pd.DataFrame:
    if current.empty:
        return pd.DataFrame(columns=["rank", "label", "entity_id", "revenue", "share_pct", "cum_share_pct", "hhi_component", "hhi_total"])
    tmp = current.copy()
    entity = _clean_text_series(tmp[id_col]) if id_col else _clean_text_series(tmp[label_col])
    label = _clean_text_series(tmp[label_col])
    entity = entity.where(entity.ne(""), label.where(label.ne(""), "Unknown"))
    label = label.where(label.ne(""), entity)
    tmp["entity_id"] = entity
    tmp["label"] = label
    grouped = tmp.groupby(["entity_id", "label"], as_index=False)["revenue"].sum()
    grouped = grouped[grouped["revenue"].abs() > 0]
    grouped = grouped.sort_values("revenue", ascending=False).reset_index(drop=True)
    total = float(grouped["revenue"].sum() or 0.0)
    grouped["share_pct"] = np.where(total > 0, (grouped["revenue"] / total) * 100.0, 0.0)
    grouped["cum_share_pct"] = grouped["share_pct"].cumsum()
    grouped["rank"] = np.arange(1, len(grouped) + 1)
    grouped["hhi_component"] = grouped["share_pct"] ** 2
    grouped["hhi_total"] = float(grouped["hhi_component"].sum() or 0.0)
    return grouped[
        ["rank", "label", "entity_id", "revenue", "share_pct", "cum_share_pct", "hhi_component", "hhi_total"]
    ].copy()


def _build_margin_risk_table(current: pd.DataFrame, prior: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "label",
        "entity_id",
        "supplier",
        "protein",
        "revenue",
        "revenue_share",
        "revenue_with_cost",
        "cost",
        "profit",
        "margin_pct",
        "margin_prev",
        "margin_delta",
        "minimum_margin_pct",
        "target_margin_pct",
        "target_status",
        "status_key",
        "profit_lost_vs_target",
        "risk",
    ]
    if current.empty:
        return pd.DataFrame(columns=columns)

    cur = current.copy()
    cur["entity_id"] = _clean_text_series(cur["product_id"]).where(
        lambda s: s.ne(""),
        _clean_text_series(cur["product_name"]).where(lambda s: s.ne(""), "Unknown"),
    )
    cur["label"] = _clean_text_series(cur["product_name"]).where(
        lambda s: s.ne(""),
        cur["entity_id"],
    )
    cur["supplier"] = _clean_text_series(cur["supplier"]).where(lambda s: s.ne(""), "Unknown")
    cur["protein"] = _clean_text_series(cur["protein"]).where(lambda s: s.ne(""), "Unknown")
    cur["has_cost"] = cur["cost"].notna()
    cur["revenue_with_cost"] = np.where(cur["has_cost"], cur["revenue"], 0.0)
    cur["cost_filled"] = cur["cost"].fillna(0.0)
    cur_rollup = (
        cur.groupby("entity_id", as_index=False)
        .agg(
            label=("label", _first_non_empty),
            supplier=("supplier", _first_non_empty),
            protein=("protein", _first_non_empty),
            revenue=("revenue", "sum"),
            revenue_with_cost=("revenue_with_cost", "sum"),
            cost=("cost_filled", "sum"),
        )
    )
    if cur_rollup.empty:
        return pd.DataFrame(columns=columns)

    prev_margin = pd.DataFrame(columns=["entity_id", "margin_prev"])
    if not prior.empty:
        prv = prior.copy()
        prv["entity_id"] = _clean_text_series(prv["product_id"]).where(
            lambda s: s.ne(""),
            _clean_text_series(prv["product_name"]).where(lambda s: s.ne(""), "Unknown"),
        )
        prv["has_cost"] = prv["cost"].notna()
        prv["revenue_with_cost"] = np.where(prv["has_cost"], prv["revenue"], 0.0)
        prv["cost_filled"] = prv["cost"].fillna(0.0)
        prev_rollup = (
            prv.groupby("entity_id", as_index=False)
            .agg(
                revenue_prev=("revenue_with_cost", "sum"),
                cost_prev=("cost_filled", "sum"),
            )
        )
        if not prev_rollup.empty:
            prev_rollup["margin_prev"] = np.where(
                prev_rollup["revenue_prev"] > 0,
                ((prev_rollup["revenue_prev"] - prev_rollup["cost_prev"]) / prev_rollup["revenue_prev"]) * 100.0,
                np.nan,
            )
            prev_margin = prev_rollup[["entity_id", "margin_prev"]]

    out = cur_rollup.merge(prev_margin, on="entity_id", how="left")
    out["profit"] = out["revenue_with_cost"] - out["cost"]
    out["margin_pct"] = np.where(
        out["revenue_with_cost"] > 0,
        (out["profit"] / out["revenue_with_cost"]) * 100.0,
        np.nan,
    )
    out["margin_delta"] = out["margin_pct"] - out["margin_prev"]
    total_revenue = float(out["revenue_with_cost"].sum() or 0.0)
    out["revenue_share"] = np.where(total_revenue > 0, (out["revenue_with_cost"] / total_revenue) * 100.0, 0.0)
    out["effective_cost_basis"] = pd.to_numeric(out.get("cost"), errors="coerce")
    out = margin_rules.annotate_margin_frame(
        out,
        protein_col="protein",
        category_col="protein",
        revenue_col="revenue_with_cost",
        profit_col="profit",
        margin_col="margin_pct",
        cost_col="cost",
    )
    out["minimum_margin_pct"] = pd.to_numeric(out.get("minimum_margin_pct"), errors="coerce")
    out["target_margin_pct"] = pd.to_numeric(out.get("target_margin_pct"), errors="coerce")
    out["profit_lost_vs_target"] = pd.to_numeric(out.get("profit_uplift_to_target"), errors="coerce").fillna(0.0)
    out["risk"] = np.select(
        [
            out["status_key"].isin(["red", "orange"]),
            out["status_key"].eq("yellow"),
            out["margin_delta"] < -5,
        ],
        [
            "below_minimum",
            "below_target",
            "margin_drop",
        ],
        default="",
    )
    out = out[out["risk"].astype(str).str.len() > 0]
    out = out.sort_values(["profit_lost_vs_target", "revenue_with_cost"], ascending=[False, False]).reset_index(drop=True)
    return out[columns].copy()


def _drivers_frame(block: Dict[str, Any], period_key: str) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for metric in ("revenue", "profit"):
        metric_block = (block.get(period_key) or {}).get(metric) or {}
        driver_rows = metric_block.get("drivers") or []
        if not isinstance(driver_rows, list):
            driver_rows = []
        for row in driver_rows:
            rows.append(
                {
                    "period": period_key.upper(),
                    "metric": metric,
                    "driver": row.get("driver") or row.get("key"),
                    "delta": row.get("delta"),
                    "share_of_delta_pct": row.get("share_of_delta_pct"),
                    "direction": row.get("direction"),
                }
            )
    return pd.DataFrame(rows, columns=["period", "metric", "driver", "delta", "share_of_delta_pct", "direction"])


def build_detail_tables(
    filters: FilterParams,
    *,
    include_current_month: bool = True,
    defaulted_window: bool = False,
) -> Dict[str, pd.DataFrame]:
    cache_key = _overview_detail_cache_key(
        filters,
        include_current_month=include_current_month,
        defaulted_window=defaulted_window,
        block_id="detail_tables",
    )
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        out: Dict[str, pd.DataFrame] = {}
        for key, frame in cached.items():
            if isinstance(frame, pd.DataFrame):
                out[key] = frame.copy()
        if out:
            return out

    payload = build_overview_bundle(
        filters,
        include_current_month=include_current_month,
        defaulted_window=defaulted_window,
    )
    work, window_contract = _prepare_detail_frame(filters, include_current_month=include_current_month)
    current = _window_slice(work, window_contract.current_start, window_contract.current_end) if not work.empty else pd.DataFrame()
    prior = _window_slice(work, window_contract.prior_month_start, window_contract.prior_month_end) if not work.empty else pd.DataFrame()

    customer_movers = _build_movers_table(current, prior, label_col="customer_name", id_col="customer_id")
    product_movers = _build_movers_table(current, prior, label_col="product_name", id_col="product_id")
    region_movers = _build_movers_table(current, prior, label_col="region", id_col=None)
    customer_conc = _build_concentration_table(current, label_col="customer_name", id_col="customer_id")
    product_conc = _build_concentration_table(current, label_col="product_name", id_col="product_id")
    margin_risk = _build_margin_risk_table(current, prior)

    health = payload.get("health") or {}
    issues = health.get("issues") if isinstance(health, dict) else []
    issues_df = pd.DataFrame(issues if isinstance(issues, list) else [])
    if issues_df.empty:
        issues_df = pd.DataFrame(columns=["label", "count"])

    drivers = payload.get("drivers") or {}
    mom_df = _drivers_frame(drivers, "mom")
    yoy_df = _drivers_frame(drivers, "yoy")

    out = {
        "movers_customer": customer_movers,
        "movers_product": product_movers,
        "movers_region": region_movers,
        "concentration_customer": customer_conc,
        "concentration_product": product_conc,
        "margin_risk": margin_risk,
        "data_health_issues": issues_df,
        "drivers_mom": mom_df,
        "drivers_yoy": yoy_df,
    }
    cache.set(cache_key, {k: v.copy() for k, v in out.items()}, timeout=CACHE_TTL)
    return out


def build_snapshot_sheets(
    filters: FilterParams,
    *,
    include_current_month: bool = True,
    defaulted_window: bool = False,
) -> Dict[str, pd.DataFrame]:
    payload = build_overview_bundle(
        filters,
        include_current_month=include_current_month,
        defaulted_window=defaulted_window,
    )
    details = build_detail_tables(
        filters,
        include_current_month=include_current_month,
        defaulted_window=defaulted_window,
    )
    meta = payload.get("meta") or {}
    health = payload.get("health") or {}
    kpis = payload.get("kpis") or {}

    metadata_rows = [
        {"field": "generated_at_utc", "value": pd.Timestamp.utcnow().isoformat()},
        {"field": "dataset_version", "value": meta.get("version")},
        {"field": "last_refresh", "value": meta.get("last_refresh")},
        {"field": "data_cutoff", "value": meta.get("data_cutoff")},
        {"field": "refresh_age_hours", "value": meta.get("refresh_age_hours")},
        {"field": "window_start", "value": ((meta.get("window") or {}).get("start"))},
        {"field": "window_end", "value": ((meta.get("window") or {}).get("end"))},
        {"field": "include_current_month", "value": bool(meta.get("include_current_month", include_current_month))},
        {"field": "defaulted_window", "value": bool(meta.get("defaulted_window", defaulted_window))},
        {"field": "filters", "value": json.dumps(meta.get("filters") or {}, sort_keys=True, default=str)},
    ]
    metadata_df = pd.DataFrame(metadata_rows, columns=["field", "value"])

    kpi_df = pd.DataFrame(
        [{"metric": key, "value": val} for key, val in kpis.items()],
        columns=["metric", "value"],
    )
    if kpi_df.empty:
        kpi_df = pd.DataFrame(columns=["metric", "value"])

    health_df = pd.DataFrame(
        [
            {"metric": "rows", "value": health.get("rows")},
            {"metric": "cost_missing_pct", "value": health.get("cost_missing_pct")},
            {"metric": "cost_coverage_pct", "value": health.get("cost_coverage_pct")},
            {"metric": "packs_coverage_pct", "value": health.get("packs_coverage_pct")},
            {"metric": "packs_covered_orderlines", "value": health.get("has_packs_orderlines")},
            {"metric": "packs_total_orderlines", "value": health.get("total_orderlines")},
            {"metric": "product_mapping_missing", "value": health.get("product_mapping_missing")},
            {"metric": "freshness_sla_days", "value": health.get("freshness_sla_days")},
            {"metric": "freshness_sla_hours", "value": health.get("freshness_sla_hours")},
            {"metric": "governed_refresh_at", "value": health.get("governed_refresh_at")},
            {"metric": "data_cutoff", "value": health.get("data_cutoff")},
        ],
        columns=["metric", "value"],
    )

    sheets: Dict[str, pd.DataFrame] = {
        "Metadata": metadata_df,
        "KPIs": kpi_df,
        "Drivers_MoM": details.get("drivers_mom", pd.DataFrame()),
        "Drivers_YoY": details.get("drivers_yoy", pd.DataFrame()),
        "Movers_Customers": details.get("movers_customer", pd.DataFrame()),
        "Movers_Products": details.get("movers_product", pd.DataFrame()),
        "Movers_Regions": details.get("movers_region", pd.DataFrame()),
        "Concentration_Customers": details.get("concentration_customer", pd.DataFrame()),
        "Concentration_Products": details.get("concentration_product", pd.DataFrame()),
        "Margin_Risk": details.get("margin_risk", pd.DataFrame()),
        "Data_Health": health_df,
        "Data_Health_Issues": details.get("data_health_issues", pd.DataFrame()),
    }
    return {name: (df if isinstance(df, pd.DataFrame) else pd.DataFrame()) for name, df in sheets.items()}


def build_drilldown_frame(
    filters: FilterParams,
    *,
    drilldown: str,
    dimension: str | None = None,
    include_current_month: bool = True,
    defaulted_window: bool = False,
) -> pd.DataFrame:
    details = build_detail_tables(
        filters,
        include_current_month=include_current_month,
        defaulted_window=defaulted_window,
    )
    token = str(drilldown or "").strip().lower()
    dim = str(dimension or "").strip().lower()

    if token == "margin_risk":
        return details.get("margin_risk", pd.DataFrame()).copy()
    if token == "concentration":
        if dim == "product":
            return details.get("concentration_product", pd.DataFrame()).copy()
        return details.get("concentration_customer", pd.DataFrame()).copy()
    if token == "movers":
        if dim == "product":
            return details.get("movers_product", pd.DataFrame()).copy()
        if dim == "region":
            return details.get("movers_region", pd.DataFrame()).copy()
        return details.get("movers_customer", pd.DataFrame()).copy()
    if token == "data_health":
        return details.get("data_health_issues", pd.DataFrame()).copy()
    return pd.DataFrame()

def _singleflight_bundle(key: str, fn):
    with _bundle_lock:
        existing = _inflight_bundles.get(key)
        if existing:
            fut = existing
        else:
            fut = Future()
            _inflight_bundles[key] = fut
            existing = None
    if existing:
        return fut.result()
    try:
        result = fn()
        fut.set_result(result)
        return result
    except Exception as exc:  # pragma: no cover - propagate but resolve future
        try:
            fut.set_exception(exc)
        except Exception:
            pass
        raise
    finally:
        with _bundle_lock:
            _inflight_bundles.pop(key, None)


def _deltas_from_series(series: pd.Series) -> Dict[str, Optional[float]]:
    if series is None or series.empty:
        return {"mom": None, "mom_pct": None, "yoy": None, "yoy_pct": None, "current": None, "previous": None, "yoy_previous": None}
    current = float(series.iloc[-1]) if len(series) else 0.0
    prev = float(series.iloc[-2]) if len(series) > 1 else 0.0
    mom_delta = current - prev if len(series) > 1 else None
    mom = _delta_pct(current, prev) if len(series) > 1 else None
    yoy_base = float(series.iloc[-13]) if len(series) > 12 else 0.0
    yoy_delta = current - yoy_base if len(series) > 12 else None
    yoy = _delta_pct(current, yoy_base) if len(series) > 12 else None
    return {
        "mom": mom_delta,
        "mom_pct": mom,
        "yoy": yoy_delta,
        "yoy_pct": yoy,
        "current": current,
        "previous": prev if len(series) > 1 else None,
        "yoy_previous": yoy_base if len(series) > 12 else None,
    }


def _window_metric_summary(
    *,
    revenue: Optional[float],
    cost: Optional[float],
    revenue_with_cost: Optional[float],
    qty: Optional[float],
    weight: Optional[float],
    orders: int,
    customers: int,
) -> Dict[str, Optional[float] | int]:
    profit = (revenue_with_cost - cost) if (revenue_with_cost is not None and cost is not None) else None
    aov = au.safe_div(revenue, orders) if (revenue is not None and orders) else None
    asp = au.safe_div(revenue, qty) if (revenue is not None and qty) else None
    margin_pct = (au.safe_div(profit, revenue_with_cost) * 100.0) if (profit is not None and revenue_with_cost) else None
    profit_per_order = au.safe_div(profit, orders) if (profit is not None and orders) else None
    profit_per_lb = au.safe_div(profit, weight) if (profit is not None and weight) else None
    return {
        "revenue": revenue,
        "cost": cost,
        "profit": profit,
        "revenue_with_cost": revenue_with_cost,
        "qty": qty,
        "weight": weight,
        "orders": orders,
        "customers": customers,
        "aov": aov,
        "asp": asp,
        "margin_pct": margin_pct,
        "profit_per_order": profit_per_order,
        "profit_per_lb": profit_per_lb,
    }


def _comparison_deltas(
    current_metrics: Dict[str, Any],
    prior_metrics: Dict[str, Any],
    yoy_metrics: Dict[str, Any],
) -> Dict[str, Dict[str, Optional[float]]]:
    metric_keys = [
        "revenue",
        "cost",
        "profit",
        "margin_pct",
        "orders",
        "customers",
        "qty",
        "weight",
        "aov",
        "asp",
        "profit_per_order",
        "profit_per_lb",
    ]
    out: Dict[str, Dict[str, Optional[float]]] = {}
    for key in metric_keys:
        current_val = _clean_optional_float(current_metrics.get(key))
        prior_val = _clean_optional_float(prior_metrics.get(key))
        yoy_val = _clean_optional_float(yoy_metrics.get(key))
        out[key] = {
            "mom": om.delta_value(current_val, prior_val),
            "mom_pct": om.delta_percent(current_val, prior_val),
            "yoy": om.delta_value(current_val, yoy_val),
            "yoy_pct": om.delta_percent(current_val, yoy_val),
            "current": current_val,
            "previous": prior_val,
            "yoy_previous": yoy_val,
        }
    out["units"] = dict(out.get("qty") or {})
    return out


def _monthly_rollup(df: pd.DataFrame, colmap: Dict[str, Optional[str]]) -> pd.DataFrame:
    date_col = colmap.get("date")
    revenue_col = colmap.get("revenue")
    if df.empty or not date_col or not revenue_col or date_col not in df.columns or revenue_col not in df.columns:
        return pd.DataFrame(columns=["revenue", "cost", "qty", "weight", "orders", "customers", "profit", "aov", "asp", "margin_pct"])

    work = pd.DataFrame({"Date": pd.to_datetime(df[date_col], errors="coerce")})
    work["month"] = work["Date"].dt.to_period("M")
    work["revenue"] = au.to_numeric_safe(df[revenue_col])
    cost_col = colmap.get("cost")
    qty_col = colmap.get("qty")
    weight_col = colmap.get("weight")
    order_col = colmap.get("order_id")
    customer_col = colmap.get("customer_id")

    work["cost"] = au.to_numeric_safe(df[cost_col]) if cost_col and cost_col in df.columns else 0.0
    work["qty"] = au.to_numeric_safe(df[qty_col]) if qty_col and qty_col in df.columns else 0.0
    work["weight"] = au.to_numeric_safe(df[weight_col]) if weight_col and weight_col in df.columns else 0.0
    work["orders"] = df[order_col] if order_col and order_col in df.columns else pd.NA
    work["customers"] = df[customer_col] if customer_col and customer_col in df.columns else pd.NA
    work = work.dropna(subset=["Date"])
    if work.empty:
        return pd.DataFrame(columns=["revenue", "cost", "qty", "weight", "orders", "customers", "profit", "aov", "asp", "margin_pct"])

    grouped = (
        work.groupby("month")
        .agg(
            {
                "revenue": "sum",
                "cost": "sum",
                "qty": "sum",
                "weight": "sum",
                "orders": lambda s: pd.Series(s).dropna().nunique(),
                "customers": lambda s: pd.Series(s).dropna().nunique(),
            }
        )
        .sort_index()
    )
    grouped["profit"] = grouped["revenue"] - grouped["cost"]
    grouped["aov"] = au.safe_div(grouped["revenue"], grouped["orders"].replace(0, pd.NA))
    grouped["asp"] = au.safe_div(grouped["revenue"], grouped["qty"].replace(0, pd.NA))
    grouped["margin_pct"] = au.safe_div(grouped["profit"], grouped["revenue"]) * 100
    grouped["margin_pct"] = grouped["margin_pct"].replace([np.inf, -np.inf], np.nan)
    grouped["margin_pct"] = grouped["margin_pct"].clip(lower=-100.0, upper=1000.0)
    return grouped


def _mix_payload(df: pd.DataFrame, colmap: Dict[str, Optional[str]]) -> Dict[str, List[Dict[str, Any]]]:
    revenue_col = colmap.get("revenue")
    if df.empty or not revenue_col or revenue_col not in df.columns:
        return {"customer": [], "product": [], "region": []}

    dims = {
        "customer": colmap.get("customer_name"),
        "product": colmap.get("product_name"),
        "region": colmap.get("region"),
    }
    out: Dict[str, List[Dict[str, Any]]] = {}
    for key, col in dims.items():
        if col and col in df.columns:
            grouped = df.groupby(col)[revenue_col].sum().sort_values(ascending=False).head(10)
            out[key] = [{"label": str(idx), "value": float(val)} for idx, val in grouped.items()]
        else:
            out[key] = []
    return out


def _pareto_payload(df: pd.DataFrame, colmap: Dict[str, Optional[str]]) -> Dict[str, Dict[str, List[Any]]]:
    revenue_col = colmap.get("revenue")
    if df.empty or not revenue_col or revenue_col not in df.columns:
        return {"customer": {}, "product": {}, "region": {}}

    dims = {
        "customer": colmap.get("customer_name"),
        "product": colmap.get("product_name"),
        "region": colmap.get("region"),
    }
    out: Dict[str, Dict[str, List[Any]]] = {}
    for key, col in dims.items():
        if col and col in df.columns:
            grouped = df.groupby(col)[revenue_col].sum().sort_values(ascending=False)
            top = grouped.head(30)
            total = float(top.sum()) if float(top.sum()) > 0 else float(grouped.sum())
            running = 0.0
            labels: List[str] = []
            values: List[float] = []
            cum_pct: List[float] = []
            for label, val in top.items():
                v = float(val or 0.0)
                running += v
                labels.append(str(label))
                values.append(v)
                cum_pct.append(round((running / total) * 100.0, 2) if total else 0.0)
            out[key] = {"labels": labels, "values": values, "cum_pct": cum_pct}
        else:
            out[key] = {"labels": [], "values": [], "cum_pct": []}
    return out


def _top_movers(df: pd.DataFrame, colmap: Dict[str, Optional[str]]) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    date_col = colmap.get("date")
    revenue_col = colmap.get("revenue")
    if df.empty or not date_col or not revenue_col or date_col not in df.columns or revenue_col not in df.columns:
        return {"customer": {"gainers": [], "decliners": []}, "product": {"gainers": [], "decliners": []}, "region": {"gainers": [], "decliners": []}}

    dates = pd.to_datetime(df[date_col], errors="coerce")
    periods = dates.dt.to_period("M")
    if periods.isna().all():
        return {"customer": {"gainers": [], "decliners": []}, "product": {"gainers": [], "decliners": []}, "region": {"gainers": [], "decliners": []}}

    last_period = periods.max()
    prev_period = last_period - 1
    df_local = df.copy()
    df_local["_period"] = periods
    df_local = df_local[df_local["_period"].isin([last_period, prev_period])]

    dims = {
        "customer": colmap.get("customer_name"),
        "product": colmap.get("product_name"),
        "region": colmap.get("region"),
    }

    results: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for key, col in dims.items():
        if col and col in df_local.columns:
            grouped = df_local.groupby([col, "_period"])[revenue_col].sum().unstack(fill_value=0)
            current = grouped.get(last_period, pd.Series(dtype=float))
            previous = grouped.get(prev_period, pd.Series(dtype=float))
            if previous is None:
                previous = pd.Series(0, index=current.index)
            delta = current - previous
            delta_pct = au.safe_div(delta, previous.replace(0, pd.NA)) * 100
            summary = pd.DataFrame(
                {
                    "label": grouped.index.astype(str),
                    "current": current.astype(float),
                    "previous": previous.astype(float),
                    "delta": delta.astype(float),
                    "delta_pct": delta_pct.astype(float).replace({pd.NA: None}),
                }
            )
            gainers = summary.sort_values("delta", ascending=False).head(10)
            decliners = summary.sort_values("delta", ascending=True).head(10)

            def _records(frame: pd.DataFrame) -> List[Dict[str, Any]]:
                return [
                    {
                        "label": str(r.label),
                        "current": float(r.current),
                        "previous": float(r.previous),
                        "delta": float(r.delta),
                        "delta_pct": (None if pd.isna(r.delta_pct) else float(r.delta_pct)),
                    }
                    for r in frame.itertuples(index=False)
                ]

            results[key] = {"gainers": _records(gainers), "decliners": _records(decliners)}
        else:
            results[key] = {"gainers": [], "decliners": []}
    return results


def _health_bundle_payload(df: pd.DataFrame, colmap: Dict[str, Optional[str]], ctx: FrameContext, *, defaulted_window: bool, include_current_month: bool) -> Dict[str, Any]:
    cost_col = colmap.get("cost")
    cost_missing_pct = float(df[cost_col].isna().mean()) if cost_col and cost_col in df.columns and len(df) else 1.0
    pack_cols = [c for c in ("pack_item_count_sum", "pack_weight_lb_sum") if c in df.columns]
    pack_missing_pct = float(df[pack_cols].isna().all(axis=1).mean()) if pack_cols and len(df) else 1.0
    product_missing = 0
    if "ProductId" in df.columns or "ProductName" in df.columns:
        ids = df["ProductId"].isna() if "ProductId" in df.columns else pd.Series(False, index=df.index)
        names = df["ProductName"].isna() if "ProductName" in df.columns else pd.Series(False, index=df.index)
        product_missing = int((ids | names).sum())

    issues: List[Dict[str, Any]] = []
    if cost_col:
        issues.append({"label": "Missing cost", "count": int(df[cost_col].isna().sum())})
    if pack_cols:
        issues.append({"label": "Missing pack match", "count": int(df[pack_cols].isna().all(axis=1).sum())})
    if product_missing:
        issues.append({"label": "Missing product mapping", "count": product_missing})
    if ctx.missing:
        issues.append({"label": "Missing columns", "count": len(ctx.missing)})
    issues = sorted(issues, key=lambda rec: rec["count"], reverse=True)[:5]

    return {
        "rows": int(len(df)),
        "window": ctx.window,
        "last_refresh": ctx.last_refresh,
        "cost_missing_pct": round(cost_missing_pct * 100, 2) if cost_missing_pct is not None else None,
        "pack_missing_pct": round(pack_missing_pct * 100, 2) if pack_missing_pct is not None else None,
        "product_mapping_missing": product_missing,
        "issues": issues,
        "defaulted_window": defaulted_window,
        "include_current_month": include_current_month,
        "missing_columns": ctx.missing,
    }


def _region_label_map(df: pd.DataFrame) -> Dict[str, str]:
    if df is None or df.empty:
        return {}
    rid_col = next((c for c in df.columns if c.lower() in {"regionid", "region_id"}), None)
    name_col = next((c for c in df.columns if c.lower() in {"regionname", "region_name", "region"}), None)
    if not rid_col:
        return {}
    ids = df[rid_col].astype("string").str.strip()
    names = df[name_col].astype("string").str.strip() if name_col and name_col in df.columns else pd.Series(dtype="string")
    work = pd.DataFrame({"id": ids, "name": names})
    work = work.dropna(subset=["id"])
    work["id"] = work["id"].str.strip()
    work["name"] = work["name"].fillna("").str.strip()
    work["label"] = work["name"].where(work["name"].ne(""), work["id"])
    work = work[work["id"].ne("")]
    return {str(row.id): str(row.label) for row in work.drop_duplicates(subset=["id"]).itertuples(index=False)}



def build_overview_bundle(
    filters: FilterParams,
    *,
    include_current_month: bool = False,
    defaulted_window: bool = False,
) -> Dict[str, Any]:
    """
    Single aggregated payload for the Overview page (KPI strip, trends, mix, health, top movers).
    """
    ctx = get_bundle_context(filters, include_current_month=include_current_month, defaulted_window=defaulted_window)
    payload = ctx.get("payload", {})
    meta = payload.get("meta", {})
    meta["cache_hit"] = bool(ctx.get("cache_hit") or meta.get("cache_hit"))
    payload["meta"] = meta
    return payload


def _history_points_for_forecast(payload: Dict[str, Any], metric: str = "revenue") -> int:
    trend = payload.get("trend") or {}
    monthly = trend.get("monthly") or trend
    values = monthly.get(metric) or []
    if not isinstance(values, list):
        return 0
    points = 0
    for val in values:
        if val is None:
            continue
        try:
            num = float(val)
        except Exception:
            continue
        if np.isnan(num):
            continue
        points += 1
    return points


def _forecast_gate(payload: Dict[str, Any], min_history_points: int = 6) -> Dict[str, Any]:
    history_points = _history_points_for_forecast(payload, metric="revenue")
    has_data = bool((payload.get("meta") or {}).get("has_data"))
    if not has_data:
        return {
            "enabled": False,
            "history_points": history_points,
            "min_history_points": int(min_history_points),
            "reason": "No data in current window.",
        }
    if history_points < int(min_history_points):
        return {
            "enabled": False,
            "history_points": history_points,
            "min_history_points": int(min_history_points),
            "reason": f"Insufficient history ({history_points} points, need at least {int(min_history_points)}).",
        }
    return {
        "enabled": True,
        "history_points": history_points,
        "min_history_points": int(min_history_points),
        "reason": None,
    }


def build_overview_context(
    filters: FilterParams,
    user_scope: Optional[Dict[str, Any]] = None,
    *,
    include_current_month: bool = False,
    defaulted_window: bool = False,
) -> Dict[str, Any]:
    """
    Canonical context builder for the V2 overview UI.
    This intentionally wraps the existing bundle as the single source of truth.
    """
    _ = user_scope  # Scope is enforced upstream in query/bundle utilities.
    bundle = build_overview_bundle(
        filters,
        include_current_month=include_current_month,
        defaulted_window=defaulted_window,
    )
    scorecard = bundle.get("executive_scorecard") or {}
    insights = bundle.get("insights") or {}
    health = bundle.get("health") or {}
    concentration = bundle.get("concentration") or {}
    profitability = bundle.get("profitability") or {}
    movers = bundle.get("top_movers") or {}
    drivers = bundle.get("drivers") or {}
    trend = bundle.get("trend") or {}
    meta = bundle.get("meta") or {}
    base_kpis = bundle.get("kpis") or {}
    headline = scorecard.get("headline") or {}
    unit_economics = scorecard.get("unit_economics") or {}
    risk_indicators = scorecard.get("risk_indicators") or {}

    scorecard_kpis = {
        "revenue": headline.get("revenue", base_kpis.get("revenue")),
        "profit": headline.get("profit", base_kpis.get("profit")),
        "margin_pct": headline.get("margin_pct", base_kpis.get("margin_pct")),
        "orders": base_kpis.get("orders"),
        "customers": base_kpis.get("customers"),
        "qty": base_kpis.get("qty"),
        "weight": base_kpis.get("weight"),
        "aov": unit_economics.get("aov", base_kpis.get("aov")),
        "asp": unit_economics.get("asp", base_kpis.get("asp")),
        "profit_per_order": unit_economics.get("profit_per_order"),
        "profit_per_lb": unit_economics.get("profit_per_lb"),
        "revenue_mom": headline.get("revenue_mom"),
        "revenue_mom_pct": headline.get("revenue_mom_pct"),
        "revenue_yoy": headline.get("revenue_yoy"),
        "revenue_yoy_pct": headline.get("revenue_yoy_pct"),
        "profit_mom": headline.get("profit_mom"),
        "profit_mom_pct": headline.get("profit_mom_pct"),
        "profit_yoy": headline.get("profit_yoy"),
        "profit_yoy_pct": headline.get("profit_yoy_pct"),
        "margin_mom": headline.get("margin_mom"),
        "margin_mom_pct": headline.get("margin_mom_pct"),
        "margin_yoy": headline.get("margin_yoy"),
        "margin_yoy_pct": headline.get("margin_yoy_pct"),
        "new_customer_share_pct": (scorecard.get("growth_retention") or {}).get("new_customer_share_pct"),
        "returning_customer_share_pct": (scorecard.get("growth_retention") or {}).get("returning_customer_share_pct"),
        "margin_risk_sku_count": risk_indicators.get("margin_risk_sku_count"),
        "margin_risk_revenue_share_pct": risk_indicators.get("margin_risk_revenue_share_pct"),
        "top1_customer_share_pct": risk_indicators.get("top1_customer_share_pct"),
        "customer_hhi": risk_indicators.get("customer_hhi"),
    }

    watchouts: List[str] = []
    top1_customer = ((concentration.get("customer") or {}).get("top1_share"))
    hhi_customer = ((concentration.get("customer") or {}).get("hhi"))
    if top1_customer is not None:
        if hhi_customer is not None:
            watchouts.append(f"Customer concentration: Top 1 {float(top1_customer):.1f}% (HHI {float(hhi_customer):.0f}).")
        else:
            watchouts.append(f"Customer concentration: Top 1 {float(top1_customer):.1f}%.")

    cost_coverage = health.get("cost_coverage_pct")
    if cost_coverage is not None:
        if float(cost_coverage) < 90:
            watchouts.append(f"Cost coverage is {float(cost_coverage):.1f}% and may impact profit metrics.")
    packs_coverage = health.get("packs_coverage_pct")
    if packs_coverage is not None:
        if float(packs_coverage) < 98:
            watchouts.append(f"Packs coverage is {float(packs_coverage):.1f}% and may impact weighted metrics.")

    margin_risk_rows = profitability.get("margin_risk") or []
    # The bundle ships a capped watchlist, so count the population rather than
    # the rows that happened to travel.
    margin_risk_count = profitability.get("margin_risk_total_count")
    if margin_risk_count is None:
        margin_risk_count = len(margin_risk_rows)
    if margin_risk_rows:
        watchouts.append(f"Margin risk present: {int(margin_risk_count)} SKU(s) below target margin.")
    watchouts = watchouts[:4]

    context = {
        "scorecard_kpis": scorecard_kpis,
        "narrative_insights": {
            "narrative": scorecard.get("narrative") or [],
            "callouts": insights.get("callouts") or [],
            "watchouts": watchouts,
        },
        "trend_series": trend,
        "drivers": drivers,
        "movers": movers,
        "risk": {
            "concentration": concentration,
            "profitability": profitability,
            "operations": bundle.get("operations") or {},
        },
        "data_health": health,
        "forecast": _forecast_gate(bundle),
        "meta": meta,
        "bundle": bundle,
    }
    return context

# app/blueprints/overview.py
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple, Optional, Union

import time
import json
from decimal import Decimal
import re
from dataclasses import asdict, is_dataclass

import numpy as np
import pandas as pd
from flask import Blueprint, Response, jsonify, request, g, current_app, render_template, session
from flask_login import login_required, current_user

from app.cache import cache
from app.core.exports import (
    dataframe_to_csv_response,
    dataframes_to_xlsx_response,
    sanitize_filename,
    xlsx_export_available,
)
from app.services.cache import cache_key as versioned_cache_key
from app.core.data_service import get_fact_df
from app.core.exceptions import DatasetNotBuiltError
from app.services import fact_store
from app.services.filters import (
    FilterParams,
    get_fiscal_periods,
    parse_filters,
    apply_filters as apply_filter_params,
    filter_args_present,
    filter_capture_requested,
    read_sticky_filters_from_session,
    resolve_filters,
    filters_cache_key,
)
from app.services.overview_query import (
    build_filter_options,
    compute_overview,
    etag_for,
    fact_frame,
    cards_summary,
    series_summary,
    top_summary,
    mix_summary,
    table_summary,
    options_summary,
)
from app.services.overview_summary import build_summary_payload
from app.services import overview_v2 as ov2
from app.services import overview_forecast as oforecast


bp = Blueprint("overview_api", __name__, url_prefix="/api/overview")
page_bp = Blueprint("overview_page", __name__, url_prefix="/overview")

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
ALLOWED_METRICS = {"revenue", "profit", "margin"}
try:
    if hasattr(oforecast, "ALLOWED_METRICS"):
        oforecast.ALLOWED_METRICS.update(ALLOWED_METRICS)
except Exception:
    pass

# human-friendly freq aliases → pandas offsets
FREQ_ALIASES: Dict[str, str] = {
    "d": "D", "day": "D", "daily": "D",
    "m": "M", "ms": "M", "month": "M", "monthly": "M",
    "q": "Q", "quarter": "Q", "quarterly": "Q",
    "w": "W-SUN", "wk": "W-SUN", "week": "W-SUN", "weekly": "W-SUN",
}
# What we allow on /series
ALLOWED_FREQ = {"D", "M", "Q", "W-SUN"}

# Small revision salt so caches are busted when this file changes
_CACHE_REV = "2025-11-04-r2"
_OVERVIEW_API_CACHE_TTL_SECONDS = 900

# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────
def _json_error(msg: str, status: int = 400) -> Response:
    resp = jsonify({"error": msg})
    resp.status_code = status
    resp.headers.setdefault("Cache-Control", "no-store")
    resp.headers.setdefault("Vary", "Cookie, Authorization")
    return resp

def _to_jsonable(obj: Any) -> Any:
    """Make nested payloads strictly JSON-serializable (and deterministic)."""
    if isinstance(obj, float):
        return 0.0 if (np.isnan(obj) or np.isinf(obj)) else float(obj)
    if isinstance(obj, (np.floating,)):
        val = float(obj)
        return 0.0 if (np.isnan(val) or np.isinf(val)) else val
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (pd.Timestamp,)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        try:
            return float(obj)
        except Exception:
            return str(obj)
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(v) for v in obj]
    return obj

def _safe_jsonify(payload: Union[Dict[str, Any], List[Any]]) -> Response:
    resp = jsonify(_to_jsonable(payload))
    resp.headers.setdefault("Vary", "Cookie, Authorization")
    return resp

def _etag_json(payload: Union[Dict[str, Any], List[Any]], *, hash_payload: Union[Dict[str, Any], List[Any], None] = None) -> Response:
    """Return JSON with strong ETag + 304 short-circuit."""
    etag_source = hash_payload if hash_payload is not None else payload
    et = etag_for(_to_jsonable(etag_source))
    inm = request.headers.get("If-None-Match")
    if inm and inm == et:
        resp = Response(status=304)
        resp.headers["ETag"] = et
        resp.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
        resp.headers["Vary"] = "Cookie, Authorization"
        return resp
    resp = _safe_jsonify(payload)
    resp.headers["ETag"] = et
    resp.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
    return resp

def _frame_date_range(frame: pd.DataFrame | None) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    if frame is None or frame.empty or "Date" not in frame.columns:
        return None, None
    try:
        dates = pd.to_datetime(frame["Date"], errors="coerce").dropna()
    except Exception:
        return None, None
    if dates.empty:
        return None, None
    start = dates.min()
    end = dates.max()
    try:
        if getattr(start, "tzinfo", None) is not None:
            start = start.tz_localize(None)
    except Exception:
        pass
    try:
        if getattr(end, "tzinfo", None) is not None:
            end = end.tz_localize(None)
    except Exception:
        pass
    return start.normalize(), end.normalize()

def _timestamp_to_iso(ts: pd.Timestamp | None) -> str | None:
    if ts is None or pd.isna(ts):
        return None
    try:
        if getattr(ts, "tzinfo", None) is not None:
            ts = ts.tz_localize(None)
    except Exception:
        pass
    try:
        return ts.date().isoformat()
    except Exception:
        try:
            return ts.isoformat()
        except Exception:
            return None

def _filters_to_payload(params: FilterParams) -> Dict[str, Any]:
    def _iso(val: pd.Timestamp | None) -> str | None:
        return _timestamp_to_iso(val)

    payload: Dict[str, Any] = {
        "start": _iso(params.start),
        "end": _iso(params.end),
        "regions": list(params.regions),
        "methods": list(params.methods),
        "customers": list(params.customers),
        "suppliers": list(getattr(params, "suppliers", ()) ),
        "products": list(getattr(params, "products", ()) ),
        "sales_reps": list(getattr(params, "sales_reps", ()) ),
    }
    payload["has_active_filters"] = any(
        payload[key]
        for key in ("regions", "methods", "customers", "suppliers", "products", "sales_reps")
    ) or bool(payload["start"] or payload["end"])
    return payload


def _attach_request_meta(payload: Dict[str, Any], *, cached: bool, duration_ms: Optional[int]) -> Dict[str, Any]:
    meta = payload.setdefault("meta", {})
    meta["request_id"] = getattr(g, "request_id", None)
    if duration_ms is not None:
        meta["duration_ms"] = duration_ms
    meta["cached"] = bool(cached or meta.get("cache_hit"))
    return payload

def _query_filter_params() -> FilterParams:
    if not hasattr(g, "_overview_query_params"):
        sticky_enabled = bool(current_app.config.get("STICKY_FILTERS", True))
        params, _meta = resolve_filters(
            request,
            current_user,
            session_obj=session,
            source=request.args or {},
            sticky_enabled=sticky_enabled,
        )
        setattr(g, "_overview_query_params", params)
    return getattr(g, "_overview_query_params")


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    try:
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off"}:
            return False
    except Exception:
        pass
    return default


def _coerce_float(value: Any, default: float = 0.0, minimum: float | None = None) -> float:
    try:
        out = float(value)
    except Exception:
        out = float(default)
    if minimum is not None and out < minimum:
        out = float(minimum)
    return out


def _last_closed_month_end(now: pd.Timestamp | None = None) -> pd.Timestamp:
    now = now or pd.Timestamp.utcnow().tz_localize(None)
    month_start = now.normalize().replace(day=1)
    return (month_start - pd.Timedelta(days=1)).normalize()


def _closed_month_window(months: int = 3, include_current_month: bool = False) -> tuple[pd.Timestamp, pd.Timestamp]:
    now = pd.Timestamp.utcnow().tz_localize(None)
    if include_current_month:
        end = now.normalize()
    else:
        end = _last_closed_month_end(now)
    end_month_start = end.replace(day=1)
    start = (end_month_start - pd.DateOffset(months=max(0, int(months) - 1))).normalize()
    return start, end


def _overview_effective_filters() -> tuple[FilterParams, bool, bool]:
    """
    Returns (filters, include_current_month, defaulted_window).

    Overview-specific behavior:
    - Default to the current fiscal year including the current day.
    - Ignore deprecated include_current_month toggles/params from old clients.
    """
    include_current_month = True
    source = request.args or {}
    sticky_enabled = bool(current_app.config.get("STICKY_FILTERS", True))
    try:
        user_id = current_user.get_id() if hasattr(current_user, "get_id") else None
    except Exception:
        user_id = None

    explicit_request = filter_args_present(source) or filter_capture_requested(source)
    user_supplied_dates = any(source.get(k) for k in ("start", "start_date", "startDate", "end", "end_date", "endDate"))
    user_supplied_preset = bool(source.get("date_preset") or source.get("preset") or source.get("range_preset"))
    sticky_payload = read_sticky_filters_from_session(session, user_id=user_id) if sticky_enabled else None
    has_sticky = isinstance(sticky_payload, dict) and bool(sticky_payload)

    parsed, _meta = resolve_filters(
        request,
        current_user,
        session_obj=session,
        source=source,
        sticky_enabled=sticky_enabled,
    )
    defaulted_window = False

    if (explicit_request and not user_supplied_dates and not user_supplied_preset) or (
        not explicit_request and not has_sticky
    ):
        fiscal_window = get_fiscal_periods().get("current_fy") or {}
        start = fiscal_window.get("start")
        end = fiscal_window.get("end")
        defaulted_window = True
    else:
        start = getattr(parsed, "start", None)
        end = getattr(parsed, "end", None)
        if end is None:
            end = pd.Timestamp.utcnow().tz_localize(None).normalize()
        if start is None and end is not None:
            start = end.replace(day=1)

    if start is not None and end is not None and start > end:
        start, end = end, start

    try:
        from dataclasses import asdict

        data = asdict(parsed)
    except Exception:
        data = {
            "start": getattr(parsed, "start", None),
            "end": getattr(parsed, "end", None),
            "regions": getattr(parsed, "regions", ()),
            "methods": getattr(parsed, "methods", ()),
            "customers": getattr(parsed, "customers", ()),
            "suppliers": getattr(parsed, "suppliers", ()),
            "products": getattr(parsed, "products", ()),
            "sales_reps": getattr(parsed, "sales_reps", ()),
            "preset": getattr(parsed, "preset", None),
            "protein_min": getattr(parsed, "protein_min", None),
            "protein_max": getattr(parsed, "protein_max", None),
            "protein_name_like": getattr(parsed, "protein_name_like", None),
            "complete_months_only": getattr(parsed, "complete_months_only", True),
        }

    data["start"] = start
    data["end"] = end
    return FilterParams(**data), include_current_month, defaulted_window


def _export_window_tokens(filters: FilterParams) -> tuple[str, str]:
    start = _timestamp_to_iso(getattr(filters, "start", None))
    end = _timestamp_to_iso(getattr(filters, "end", None))
    if start or end:
        return start or "start", end or "end"
    return "window", "window"


def _export_filename(prefix: str, filters: FilterParams, extension: str) -> str:
    start_token, end_token = _export_window_tokens(filters)
    stem = sanitize_filename(f"{prefix}_{start_token}_{end_token}", default=prefix)
    ext = extension.lstrip(".").lower()
    return f"{stem}.{ext}"


def _apply_movers_guardrails(
    frame: pd.DataFrame,
    *,
    min_baseline: float,
    exclude_low_base: bool,
    min_new_current: float,
    min_lost_prior: float,
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=(frame.columns if isinstance(frame, pd.DataFrame) else [])), {
            "min_baseline": float(min_baseline),
            "exclude_low_base": bool(exclude_low_base),
            "min_new_current": float(min_new_current),
            "min_lost_prior": float(min_lost_prior),
            "rows_before": 0,
            "rows_after": 0,
            "rows_filtered": 0,
        }

    out = frame.copy()
    before = int(len(out.index))

    def _num_series(column: str) -> pd.Series:
        if column in out.columns:
            return pd.to_numeric(out[column], errors="coerce").fillna(0.0)
        return pd.Series(0.0, index=out.index, dtype="float64")

    prev = _num_series("previous")
    curr = _num_series("current")
    labels = out.get("delta_pct_label")
    if labels is None:
        labels = pd.Series([""] * len(out.index), index=out.index)
    labels = labels.astype("string").fillna("").str.lower()

    if "low_sample" not in out.columns:
        out["low_sample"] = (prev.abs() > 0) & (prev.abs() < float(min_baseline))
    else:
        out["low_sample"] = out["low_sample"].astype(bool)

    keep = pd.Series(True, index=out.index)
    if exclude_low_base:
        keep &= ~out["low_sample"]

    new_mask = labels.eq("new")
    if bool(new_mask.any()):
        keep &= (~new_mask) | (curr.abs() >= float(min_new_current))

    lost_mask = labels.eq("lost")
    if bool(lost_mask.any()):
        keep &= (~lost_mask) | (prev.abs() >= float(min_lost_prior))

    filtered = out.loc[keep].copy()
    after = int(len(filtered.index))
    meta = {
        "min_baseline": float(min_baseline),
        "exclude_low_base": bool(exclude_low_base),
        "min_new_current": float(min_new_current),
        "min_lost_prior": float(min_lost_prior),
        "rows_before": before,
        "rows_after": after,
        "rows_filtered": max(0, before - after),
    }
    return filtered, meta


def _pick_column(df: pd.DataFrame, candidates: Iterable[str]) -> str | None:
    if df is None or df.empty:
        return None
    if not candidates:
        return None
    cols = list(df.columns)
    direct = {c.lower(): c for c in cols}
    normalized = {re.sub(r"[^a-z0-9]+", "", c.lower()): c for c in cols}
    for cand in candidates:
        if not cand:
            continue
        low = str(cand).strip().lower()
        if low in direct:
            return direct[low]
        key = re.sub(r"[^a-z0-9]+", "", low)
        if key in normalized:
            return normalized[key]
    return None


def _num_sum(series: Any) -> float:
    if series is None:
        return 0.0
    try:
        s = pd.to_numeric(series, errors="coerce")
        return float(s.fillna(0.0).sum())
    except Exception:
        return 0.0


def _nunique(series: Any) -> int:
    try:
        return int(pd.Series(series).dropna().nunique())
    except Exception:
        return 0


def _manifest_meta() -> tuple[str, str | None]:
    try:
        from app.services import fact_store  # type: ignore

        meta = fact_store.get_meta() if hasattr(fact_store, "get_meta") else {}
        if meta:
            version = str(meta.get("watermark") or meta.get("schema_fingerprint") or "cache")
            last_refresh = meta.get("last_refresh_utc") or meta.get("watermark") or meta.get("watermark_dt")
            return version, str(last_refresh) if last_refresh else None
    except Exception:
        pass

    manifest: Dict[str, Any] = fact_store.get_meta() if hasattr(fact_store, "get_meta") else {}
    try:
        version = str(manifest.get("dataset_version") or manifest.get("version") or fact_store.cache_buster())
    except Exception:
        version = "unknown"
    last_refresh = manifest.get("built_at") or manifest.get("refreshed_at") or manifest.get("last_refresh_utc") or manifest.get("watermark")
    return version, str(last_refresh) if last_refresh else None

def _clamp_params_to_data_window(df: pd.DataFrame, params: FilterParams) -> FilterParams:
    if df is None or df.empty:
        return params

    data_start, data_end = _frame_date_range(df)

    if data_start is None or data_end is None:
        if getattr(params, "start", None) is None and getattr(params, "end", None) is None:
            return params
        try:
            if is_dataclass(params):
                data = asdict(params)
            else:
                try:
                    data = params._asdict()
                except Exception:
                    data = dict(getattr(params, "__dict__", {}))
            data["start"] = None
            data["end"] = None
            return FilterParams(**data)
        except Exception:
            return params

    start = getattr(params, "start", None)
    end = getattr(params, "end", None)

    if start is None and end is None:
        return params

    def _norm(ts):
        if ts is None or (isinstance(ts, float) and np.isnan(ts)):
            return None
        try:
            if getattr(ts, "tzinfo", None) is not None:
                try:
                    ts = ts.tz_localize(None)
                except Exception:
                    ts = ts.tz_convert(None)
        except Exception:
            pass
        try:
            return ts.normalize()
        except Exception:
            return ts

    start_n = _norm(start)
    end_n = _norm(end)

    if start_n is not None and end_n is not None and start_n > end_n:
        start_n, end_n = end_n, start_n

    if start_n is not None and end_n is None:
        if start_n > data_end:
            start_n, end_n = data_start, data_end
    elif end_n is not None and start_n is None:
        if end_n < data_start:
            start_n, end_n = data_start, data_end

    if start_n is not None and end_n is not None:
        if end_n < data_start or start_n > data_end:
            start_n, end_n = data_start, data_end

    if start_n is getattr(params, "start", None) and end_n is getattr(params, "end", None):
        return params

    try:
        if is_dataclass(params):
            data = asdict(params)
        else:
            try:
                data = params._asdict()
            except Exception:
                data = dict(getattr(params, "__dict__", {}))
        data["start"] = start_n
        data["end"] = end_n
        return FilterParams(**data)
    except Exception:
        return params

# ─────────────────────────────────────────────────────────────────────────────
# Data loading helpers
# ─────────────────────────────────────────────────────────────────────────────
def _get_scoped_fact_df(params: Optional[FilterParams] = None) -> pd.DataFrame:
    """Gets the base fact dataframe, scoped and filtered."""
    base_df = fact_frame(params, apply_filter=False) if params is not None else fact_frame(apply_filter=False)
    if base_df is None or base_df.empty:
        return pd.DataFrame()
        
    if params:
        effective_params = _clamp_params_to_data_window(base_df, params)
        df_f = apply_filter_params(base_df, effective_params)
    else:
        df_f = base_df
    
    return df_f

# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@bp.get("/ping")
@login_required
def ping():
    return "pong"

@bp.get("/summary")
@login_required
def summary():
    """
    GET /api/overview/summary
    """
    params = _query_filter_params()
    try:
        scoped_df = _get_scoped_fact_df(params)
    except DatasetNotBuiltError as exc:
        return _json_error(str(exc) or "Dataset not built.", 503)

    if scoped_df is None or scoped_df.empty:
        if current_app.config.get("TESTING"):
            try:
                all_params = parse_filters({"preset": "all"})
                scoped_df = _get_scoped_fact_df(all_params)
                params = all_params
            except Exception:
                pass
        if scoped_df is None or scoped_df.empty:
            return _json_error("No data available for the selected filters.", 404)

    overview = compute_overview(scoped_df)
    payload = build_summary_payload(
        scoped_df,
        overview,
        filters=params,
        version=fact_store.cache_buster(),
    )
    return _etag_json(payload)

@bp.get("/cards")
@login_required
def cards():
    params = _query_filter_params()
    comparison = request.args.get("comparison")
    
    # For period-over-period, we need the base frame without applied filters,
    # but still scoped to the current user.
    base_df = fact_frame(params, apply_filter=False)
    base_df = base_df if base_df is not None else pd.DataFrame()
    
    payload = cards_summary(params, frame=base_df, comparison=comparison)
    
    return _etag_json(payload)

@bp.get("/series")
@login_required
def series():
    params = _query_filter_params()
    freq_raw = (request.args.get("freq") or "d").lower()
    freq = FREQ_ALIASES.get(freq_raw, "D")
    if freq not in ALLOWED_FREQ:
        return _json_error(f"Invalid freq: '{freq_raw}'", 400)

    scoped_df = _get_scoped_fact_df(params)
    if scoped_df is None or scoped_df.empty:
        return _json_error("No data for series.", 404)

    payload = series_summary(params, freq=freq, frame=scoped_df)
    return _etag_json(payload)

@bp.route("/forecast", methods=["GET", "POST"])
@login_required
def forecast():
    body = request.get_json(silent=True) or {}
    metric = (body.get("metric") or request.args.get("metric") or "revenue").lower()
    granularity = str(body.get("granularity") or request.args.get("granularity") or "monthly").strip().lower()
    forecast_v2_enabled = bool(current_app.config.get("OVERVIEW_FORECAST_V2", False))
    requested_v2 = _coerce_bool(body.get("v2") or request.args.get("v2"), default=False)
    use_v2 = bool(requested_v2 or forecast_v2_enabled)
    include_current_month = _coerce_bool(
        body.get("include_current_month")
        or request.args.get("include_current_month")
        or request.args.get("include_current")
        or request.args.get("include_current_months"),
        default=False,
    )
    try:
        horizon = int(body.get("horizon_months") or body.get("horizon") or request.args.get("horizon_months") or request.args.get("horizon") or 6)
    except Exception:
        horizon = 6

    filters_payload = body.get("filters")
    if isinstance(filters_payload, dict):
        try:
            params = parse_filters(filters_payload)
        except Exception:
            params = _query_filter_params()
    else:
        params, include_current_month, _ = _overview_effective_filters()

    try:
        if use_v2:
            payload = oforecast.forecast_metric_v2(
                params,
                metric=metric,
                horizon=horizon,
                granularity=granularity,
                include_current=include_current_month,
            )
        else:
            payload = oforecast.forecast_metric(
                params,
                metric=metric,
                horizon_months=horizon,
                include_current_month=include_current_month,
            )
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except Exception as exc:  # pragma: no cover - defensive
        current_app.logger.exception("overview.forecast.failed")
        return _json_error(f"Unable to generate forecast: {exc}", 500)
    return _etag_json(payload)


@bp.get("/insights")
@login_required
def insights():
    filters, include_current_month, defaulted_window = _overview_effective_filters()
    try:
        bundle = fact_store.query_overview(
            filters,
            include_current_month=include_current_month,
            defaulted_window=defaulted_window,
        )
        payload = {
            "insights": bundle.get("insights", {}),
            "drivers": bundle.get("drivers", {}),
            "concentration": bundle.get("concentration", {}),
            "profitability": bundle.get("profitability", {}),
            "meta": bundle.get("meta", {}),
        }
    except Exception as exc:  # pragma: no cover - defensive
        current_app.logger.exception("overview.insights.failed")
        return _json_error(f"Unable to compute insights: {exc}", 500)
    return _etag_json(payload)

@bp.get("/top")
@login_required
def top():
    params = _query_filter_params()
    try:
        limit = int(request.args.get("limit", 5))
    except (TypeError, ValueError):
        limit = 5
    limit = max(3, min(25, limit))

    scoped_df = _get_scoped_fact_df(params)
    if scoped_df is None or scoped_df.empty:
        return _json_error("No data for top summary.", 404)

    payload = top_summary(params, frame=scoped_df, limit=limit)
    payload["meta"] = {
        "filters": _filters_to_payload(params),
        "version": fact_store.cache_buster(),
    }
    return _etag_json(payload)


@bp.get("/mix")
@login_required
def mix():
    params = _query_filter_params()
    scoped_df = _get_scoped_fact_df(params)
    if scoped_df is None or scoped_df.empty:
        return _json_error("No data for mix summary.", 404)

    payload = mix_summary(params, frame=scoped_df)
    payload["meta"] = {
        "filters": _filters_to_payload(params),
        "version": fact_store.cache_buster(),
    }
    return _etag_json(payload)


@bp.get("/table")
@login_required
def overview_table():
    params = _query_filter_params()
    dimension = (request.args.get("dimension") or "product").strip().lower()
    try:
        page = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(request.args.get("page_size", 25))
    except (TypeError, ValueError):
        page_size = 25
    sort = (request.args.get("sort") or "-revenue").strip()

    page = max(1, min(500, page))
    page_size = max(5, min(200, page_size))

    scoped_df = _get_scoped_fact_df(params)
    if scoped_df is None or scoped_df.empty:
        return _json_error("No data for table.", 404)

    payload = table_summary(
        params,
        frame=scoped_df,
        dimension=dimension,
        page=page,
        page_size=page_size,
        sort=sort,
    )
    payload["meta"] = {
        "filters": _filters_to_payload(params),
        "version": fact_store.cache_buster(),
    }
    return _etag_json(payload)


@bp.get("/options")
@login_required
def options():
    # Options should be based on the full dataset, not the scoped one
    base_df = get_fact_df(filters={"preset": "all"})
    if base_df is None or base_df.empty:
        return Response(status=204)
    payload = options_summary(base_df)
    resp = _etag_json(payload)
    resp.headers.setdefault("Cache-Control", "public, max-age=300")
    return resp


# ---------------------------------------------------------------------------
# Overview landing page (Business Performance) + v2 APIs
# ---------------------------------------------------------------------------


@page_bp.get("/")
@page_bp.get("")
@login_required
def overview_landing():
    render_start = time.perf_counter()
    version, last_refresh = _manifest_meta()
    overview_v3_enabled = bool(current_app.config.get("OVERVIEW_V3", False))
    overview_v2_enabled = bool(current_app.config.get("OVERVIEW_V2", False))
    overview_v2_classic_enabled = bool(current_app.config.get("OVERVIEW_V2_CLASSIC", False))
    overview_forecast_v2_enabled = bool(current_app.config.get("OVERVIEW_FORECAST_V2", False))
    overview_movers_fast_enabled = bool(current_app.config.get("OVERVIEW_MOVERS_FAST", False))
    if overview_v2_enabled and bool(current_app.config.get("OVERVIEW_V2_ADMIN_ONLY", False)):
        role = str(getattr(current_user, "role", "") or "").strip().lower()
        overview_v2_enabled = role in {"admin", "manager"}
        if not overview_v2_enabled:
            overview_forecast_v2_enabled = False
            overview_movers_fast_enabled = False
    if not overview_v2_enabled:
        overview_forecast_v2_enabled = False
        overview_movers_fast_enabled = False
    template_ctx = {
        "filters": {},
        "last_refresh": last_refresh,
        "version": version,
        "overview_v3_enabled": overview_v3_enabled,
        "overview_v2_enabled": overview_v2_enabled,
        "overview_v2_classic_enabled": overview_v2_classic_enabled,
        "overview_forecast_v2_enabled": overview_forecast_v2_enabled,
        "overview_movers_fast_enabled": overview_movers_fast_enabled,
    }
    if overview_v2_enabled and not overview_v2_classic_enabled:
        # The V3 shell is the production-grade Overview experience. Keep the
        # classic V2 template behind an explicit fallback flag only.
        template_name = "overview/index_v3.html"
    elif overview_v2_enabled:
        template_name = "overview/index.html"
    elif overview_v3_enabled:
        template_name = "overview/index_v3.html"
    else:
        template_name = "overview/index_legacy.html"
    html = render_template(template_name, **template_ctx)
    try:
        current_app.logger.info(
            "overview.perf",
            extra={
                "stage": "template_render",
                "ms": round((time.perf_counter() - render_start) * 1000, 2),
            },
        )
    except Exception:
        pass
    return html


@page_bp.get("/api")
@login_required
def overview_api() -> Response:
    start_ts = time.perf_counter()
    mix_dim = str(request.args.get("mix") or request.args.get("mix_dim") or "customer").strip().lower()
    if mix_dim not in {"customer", "product", "region"}:
        mix_dim = "customer"

    filters, include_current_month, defaulted_window = _overview_effective_filters()
    version, last_refresh = _manifest_meta()

    cache_key = filters_cache_key(
        current_user,
        filters,
        extras={
            "scope": "overview_api",
            "mix": mix_dim,
            "include_current_month": include_current_month,
            "version": version,
            "rev": _CACHE_REV,
        },
    )

    cached = cache.get(cache_key)
    if isinstance(cached, dict) and cached:
        try:
            current_app.logger.info(
                "overview.api",
                extra={
                    "duration_ms": round((time.perf_counter() - start_ts) * 1000, 2),
                    "cache": "hit",
                },
            )
        except Exception:
            pass
        return _etag_json(cached)

    columns = sorted(
        {
            "Date",
            "OrderId",
            "OrderID",
            "OrderLineId",
            "CustomerId",
            "CustomerID",
            "CustomerName",
            "ProductId",
            "ProductID",
            "ProductName",
            "SKU",
            "RegionName",
            "Region",
            "SupplierName",
            "ShippingMethodName",
            "ShippingMethodLabel",
            "Method",
            "WeightLb",
            "pack_weight_lb_sum",
            "QuantityShipped",
            "QuantityOrdered",
            "Qty",
            "Units",
            "UnitsShipped",
            "QtyShipped",
            "QtyOrdered",
            "pack_item_count_sum",
            "Revenue",
            "ExtRevenue",
            "ExtPrice",
            "ExtendedPrice",
            "Sales",
            "NetSales",
            "TotalRevenue",
            "Cost",
            "ExtCost",
            "ExtendedCost",
            "COGS",
        }
    )

    df = get_fact_df(start=filters.start, end=filters.end, columns=columns)
    if df is None:
        df = pd.DataFrame()

    try:
        df = apply_filter_params(df, filters)
    except Exception:
        current_app.logger.exception("overview.api.apply_filters_failed")

    date_col = _pick_column(df, ("Date", "OrderDate", "InvoiceDate", "ShipDate"))
    revenue_col = _pick_column(df, ("Revenue", "Sales", "NetSales", "ExtRevenue", "ExtPrice", "ExtendedPrice", "TotalRevenue"))
    qty_col = _pick_column(df, ("QuantityShipped", "QtyShipped", "UnitsShipped", "Units", "Qty", "Quantity", "QuantityOrdered", "QtyOrdered", "pack_item_count_sum"))
    cost_col = _pick_column(df, ("Cost", "COGS", "ExtCost", "ExtendedCost"))
    order_col = _pick_column(df, ("OrderId", "OrderID", "OrderLineId"))
    customer_col = _pick_column(df, ("CustomerName", "CustomerId", "CustomerID"))

    revenue = _num_sum(df.get(revenue_col)) if revenue_col else 0.0
    qty = _num_sum(df.get(qty_col)) if qty_col else 0.0
    orders = _nunique(df.get(order_col)) if order_col else 0
    customers = _nunique(df.get(customer_col)) if customer_col else 0

    asp_denom = qty if qty > 0 else (orders if orders > 0 else None)
    asp = (revenue / asp_denom) if asp_denom else None
    aov = (revenue / orders) if orders else None

    cost = _num_sum(df.get(cost_col)) if cost_col else None
    profit = (revenue - cost) if cost is not None else None
    margin_pct = ((profit / revenue) * 100.0) if (profit is not None and revenue) else None

    months: list[str] = []
    series_revenue: list[float] = []
    series_qty: list[float] = []
    series_asp: list[float | None] = []
    if date_col and revenue_col and date_col in df.columns and revenue_col in df.columns and not df.empty:
        work = pd.DataFrame(
            {
                "Date": pd.to_datetime(df[date_col], errors="coerce"),
                "revenue": pd.to_numeric(df[revenue_col], errors="coerce").fillna(0.0),
            }
        ).dropna(subset=["Date"])
        if qty_col and qty_col in df.columns:
            work["qty"] = pd.to_numeric(df[qty_col], errors="coerce").fillna(0.0)
        else:
            work["qty"] = 0.0
        grouped = work.groupby(work["Date"].dt.to_period("M"), sort=True).agg({"revenue": "sum", "qty": "sum"})
        for period, row in grouped.iterrows():
            months.append(str(period))
            r = float(row.get("revenue") or 0.0)
            q = float(row.get("qty") or 0.0)
            series_revenue.append(r)
            series_qty.append(q)
            series_asp.append((r / q) if q > 0 else None)

    dim_map = {
        "customer": ("CustomerName", "CustomerId", "CustomerID"),
        "product": ("ProductName", "SKU", "ProductId", "ProductID"),
        "region": ("RegionName", "Region"),
    }
    dim_col = _pick_column(df, dim_map[mix_dim])
    mix_labels: list[str] = []
    mix_values: list[float] = []
    pareto_labels: list[str] = []
    pareto_values: list[float] = []
    pareto_cum_pct: list[float] = []

    if dim_col and revenue_col and dim_col in df.columns and revenue_col in df.columns and not df.empty:
        grouped = (
            pd.DataFrame(
                {
                    "dim": df[dim_col].astype("string"),
                    "revenue": pd.to_numeric(df[revenue_col], errors="coerce").fillna(0.0),
                }
            )
            .assign(dim=lambda d: d["dim"].fillna("(unknown)").astype("string"))
            .groupby("dim", sort=False)["revenue"]
            .sum()
            .sort_values(ascending=False)
        )

        top15 = grouped.head(15)
        mix_labels = [str(x) for x in top15.index.tolist()]
        mix_values = [float(x) for x in top15.values.tolist()]

        top30 = grouped.head(30)
        total = float(top30.sum()) if float(top30.sum()) > 0 else float(grouped.sum())
        running = 0.0
        for label, val in top30.items():
            v = float(val or 0.0)
            running += v
            pareto_labels.append(str(label))
            pareto_values.append(v)
            pareto_cum_pct.append(round((running / total) * 100.0, 2) if total else 0.0)

    window_start = None
    window_end = None
    if date_col and date_col in df.columns and not df.empty:
        dates = pd.to_datetime(df[date_col], errors="coerce").dropna()
        if not dates.empty:
            window_start = _timestamp_to_iso(dates.min().normalize())
            window_end = _timestamp_to_iso(dates.max().normalize())

    missing_rates: Dict[str, float] = {}
    for key, col in (("date", date_col), ("revenue", revenue_col), ("qty", qty_col), ("cost", cost_col), ("dim", dim_col)):
        if col and col in df.columns and len(df) > 0:
            try:
                missing_rates[key] = float(pd.isna(df[col]).mean())
            except Exception:
                missing_rates[key] = 0.0

    payload: Dict[str, Any] = {
        "kpis": {
            "revenue": revenue,
            "qty": qty,
            "orders": orders,
            "customers": customers,
            "asp": asp,
            "aov": aov,
            "profit": profit,
            "margin_pct": margin_pct,
        },
        "series": {
            "months": months,
            "revenue": series_revenue,
            "qty": series_qty,
            "asp": series_asp,
        },
        "mix": {"labels": mix_labels, "values": mix_values},
        "pareto": {"labels": pareto_labels, "values": pareto_values, "cum_pct": pareto_cum_pct},
        "meta": {
            "last_refresh": last_refresh,
            "version": version,
            "rows": int(len(df)),
            "window": {"start": window_start, "end": window_end},
            "defaulted_window": defaulted_window,
            "include_current_month": include_current_month,
            "mix_dim": mix_dim,
            "missing_rate": missing_rates,
            "filters": _filters_to_payload(filters),
        },
    }

    cache.set(cache_key, payload, timeout=_OVERVIEW_API_CACHE_TTL_SECONDS)
    try:
        current_app.logger.info(
            "overview.api",
            extra={
                "duration_ms": round((time.perf_counter() - start_ts) * 1000, 2),
                "cache": "miss",
                "rows": int(len(df)),
            },
        )
    except Exception:
        pass
    return _etag_json(payload)


@page_bp.get("/api/summary")
@login_required
def overview_summary_v2():
    params = _query_filter_params()
    payload = ov2.build_summary(params)
    return _etag_json(payload)


@page_bp.get("/api/context")
@login_required
def overview_context_v2():
    start_ts = time.perf_counter()
    filters, include_current_month, defaulted_window = _overview_effective_filters()
    payload = ov2.build_overview_context(
        filters,
        include_current_month=include_current_month,
        defaulted_window=defaulted_window,
    )
    if bool(current_app.config.get("OVERVIEW_MOVERS_FAST", False)):
        payload["movers"] = {}
        bundle = payload.get("bundle")
        if isinstance(bundle, dict):
            bundle["top_movers"] = {}
    duration_ms = int((time.perf_counter() - start_ts) * 1000)
    cached = ((payload.get("meta") or {}).get("cache_hit")) if isinstance(payload, dict) else False
    payload = _attach_request_meta(payload, cached=bool(cached), duration_ms=duration_ms)
    etag_payload = json.loads(json.dumps(payload))
    etag_meta = etag_payload.get("meta", {})
    if isinstance(etag_meta, dict):
        etag_meta.pop("request_id", None)
        etag_meta.pop("duration_ms", None)
    return _etag_json(payload, hash_payload=etag_payload)


@page_bp.get("/api/movers")
@login_required
def overview_movers_fast():
    dimension = str(request.args.get("dimension") or "customer").strip().lower()
    if dimension not in {"customer", "product", "region"}:
        dimension = "customer"
    min_baseline = _coerce_float(request.args.get("min_baseline"), default=500.0, minimum=0.0)
    min_new_current = _coerce_float(request.args.get("min_new_current"), default=min_baseline, minimum=0.0)
    min_lost_prior = _coerce_float(request.args.get("min_lost_prior"), default=min_baseline, minimum=0.0)
    exclude_low_base = _coerce_bool(request.args.get("exclude_low_base"), default=False)

    filters, include_current_month, defaulted_window = _overview_effective_filters()
    version, _last_refresh = _manifest_meta()
    cache_key = filters_cache_key(
        current_user,
        filters,
        extras={
            "scope": "overview_movers_fast",
            "dimension": dimension,
            "include_current_month": include_current_month,
            "defaulted_window": defaulted_window,
            "min_baseline": min_baseline,
            "exclude_low_base": exclude_low_base,
            "min_new_current": min_new_current,
            "min_lost_prior": min_lost_prior,
            "dataset_version": version,
            "rev": "2026-03-07-v1",
        },
    )
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return _etag_json(cached)

    frame = ov2.build_drilldown_frame(
        filters,
        drilldown="movers",
        dimension=dimension,
        include_current_month=include_current_month,
        defaulted_window=defaulted_window,
    )
    if frame is None:
        frame = pd.DataFrame()
    frame = frame.replace({np.nan: None})
    frame, guardrails_meta = _apply_movers_guardrails(
        frame,
        min_baseline=min_baseline,
        exclude_low_base=exclude_low_base,
        min_new_current=min_new_current,
        min_lost_prior=min_lost_prior,
    )
    rows = frame.to_dict(orient="records")
    payload = {
        "dimension": dimension,
        "rows": rows,
        "meta": {
            "rows": int(len(rows)),
            "filters": _filters_to_payload(filters),
            "include_current_month": include_current_month,
            "defaulted_window": defaulted_window,
            "guardrails": guardrails_meta,
            "cache_hit": False,
        },
    }
    cache.set(cache_key, payload, timeout=300)
    return _etag_json(payload)


@page_bp.get("/api/bundle")
@login_required
def overview_bundle_v3():
    start_ts = time.perf_counter()
    filters, include_current_month, defaulted_window = _overview_effective_filters()
    payload = fact_store.query_overview(filters, include_current_month=include_current_month, defaulted_window=defaulted_window)
    duration_ms = int((time.perf_counter() - start_ts) * 1000)
    cached = payload.get("meta", {}).get("cache_hit", False)
    payload = _attach_request_meta(payload, cached=cached, duration_ms=duration_ms)
    etag_payload = json.loads(json.dumps(payload))
    etag_meta = etag_payload.get("meta", {})
    etag_meta.pop("request_id", None)
    etag_meta.pop("duration_ms", None)
    resp = _etag_json(payload, hash_payload=etag_payload)
    resp.headers["X-Request-ID"] = getattr(g, "request_id", "") or ""
    return resp


@page_bp.get("/api/trend")
@login_required
def overview_trend_v2():
    params = _query_filter_params()
    try:
        months = int(request.args.get("months", 12))
    except Exception:
        months = 12
    exclude_partial = str(request.args.get("include_current", "false")).lower() not in {"true", "1", "yes"}
    payload = ov2.build_trend(params, months=months, exclude_partial=exclude_partial)
    return _etag_json(payload)


@page_bp.get("/api/mix")
@login_required
def overview_mix_v2():
    params = _query_filter_params()
    dim = request.args.get("dim", "region")
    payload = ov2.build_mix(params, dim=dim)
    return _etag_json(payload)


@page_bp.get("/api/top")
@login_required
def overview_top_v2():
    params = _query_filter_params()
    metric = request.args.get("metric", "product")
    try:
        limit = int(request.args.get("limit", 10))
    except Exception:
        limit = 10
    payload = ov2.build_top(params, metric=metric, limit=limit)
    return _etag_json(payload)


@page_bp.get("/api/alerts")
@login_required
def overview_alerts_v2():
    params = _query_filter_params()
    payload = ov2.build_alerts(params)
    return _etag_json(payload)


@page_bp.get("/api/pareto")
@login_required
def overview_pareto_v2():
    params = _query_filter_params()
    dim = request.args.get("dim", "product")
    payload = ov2.build_pareto(params, dim=dim)
    return _etag_json(payload)


@page_bp.get("/api/health")
@login_required
def overview_health_v2():
    params = _query_filter_params()
    payload = ov2.build_health(params)
    return _etag_json(payload)


@page_bp.get("/api/export/snapshot")
@login_required
def overview_snapshot_export():
    fmt = str(request.args.get("format") or "xlsx").strip().lower()
    if fmt not in {"xlsx", "csv"}:
        fmt = "xlsx"
    if fmt == "xlsx" and not xlsx_export_available():
        fmt = "csv"

    filters, include_current_month, defaulted_window = _overview_effective_filters()
    sheets = ov2.build_snapshot_sheets(
        filters,
        include_current_month=include_current_month,
        defaulted_window=defaulted_window,
    )
    dataset = str(request.args.get("dataset") or request.args.get("export_type") or "all").strip().lower()
    sheet_aliases = {
        "all": None,
        "kpis": "KPIs",
        "drivers_mom": "Drivers_MoM",
        "drivers_yoy": "Drivers_YoY",
        "movers_customers": "Movers_Customers",
        "movers_products": "Movers_Products",
        "movers_regions": "Movers_Regions",
        "concentration_customers": "Concentration_Customers",
        "concentration_products": "Concentration_Products",
        "margin_risk": "Margin_Risk",
        "data_health": "Data_Health",
        "data_health_issues": "Data_Health_Issues",
    }
    multi_sheet_aliases = {
        "drivers": ["Drivers_MoM", "Drivers_YoY"],
        "concentration": ["Concentration_Customers", "Concentration_Products"],
    }
    selected_sheet = sheet_aliases.get(dataset)
    selected_sheets = multi_sheet_aliases.get(dataset)
    if selected_sheet:
        metadata_df = sheets.get("Metadata", pd.DataFrame())
        selected_df = sheets.get(selected_sheet, pd.DataFrame())
        sheets = {"Metadata": metadata_df, selected_sheet: selected_df}
    elif selected_sheets:
        metadata_df = sheets.get("Metadata", pd.DataFrame())
        narrowed = {"Metadata": metadata_df}
        for sheet_name in selected_sheets:
            narrowed[sheet_name] = sheets.get(sheet_name, pd.DataFrame())
        sheets = narrowed

    if fmt == "csv":
        if selected_sheet:
            df = sheets.get(selected_sheet, pd.DataFrame())
            filename = _export_filename(f"business_performance_{dataset}", filters, "csv")
        elif selected_sheets:
            df = sheets.get(selected_sheets[0], pd.DataFrame())
            filename = _export_filename(f"business_performance_{dataset}", filters, "csv")
        else:
            df = sheets.get("KPIs", pd.DataFrame())
            filename = _export_filename("business_performance_snapshot_kpis", filters, "csv")
        return dataframe_to_csv_response(df, filename=filename)

    stem = f"business_performance_{dataset}" if (selected_sheet or selected_sheets) else "business_performance_snapshot"
    filename = _export_filename(stem, filters, "xlsx")
    return dataframes_to_xlsx_response(sheets, filename=filename)


@page_bp.get("/api/export/trend")
@login_required
def overview_trend_export():
    fmt = str(request.args.get("format") or "csv").strip().lower()
    if fmt not in {"csv", "xlsx"}:
        fmt = "csv"
    if fmt == "xlsx" and not xlsx_export_available():
        fmt = "csv"

    freq = str(request.args.get("freq") or "monthly").strip().lower()
    if freq not in {"monthly", "weekly"}:
        freq = "monthly"

    filters, include_current_month, defaulted_window = _overview_effective_filters()
    bundle = ov2.build_overview_bundle(
        filters,
        include_current_month=include_current_month,
        defaulted_window=defaulted_window,
    )
    trend = bundle.get("trend") or {}
    block = trend.get(freq) if isinstance(trend.get(freq), dict) else trend
    labels = list((block.get("months") or []))

    def _series(name: str) -> List[Any]:
        vals = block.get(name) or []
        return list(vals) if isinstance(vals, list) else []

    revenue_vals = _series("revenue")
    profit_vals = _series("profit")
    margin_vals = _series("margin_pct")
    units_vals = _series("units")
    cost_vals = _series("cost")

    rows: List[Dict[str, Any]] = []
    for idx, label in enumerate(labels):
        rows.append(
            {
                "Period": label,
                "Revenue": revenue_vals[idx] if idx < len(revenue_vals) else None,
                "Profit": profit_vals[idx] if idx < len(profit_vals) else None,
                "MarginPct": margin_vals[idx] if idx < len(margin_vals) else None,
                "Units": units_vals[idx] if idx < len(units_vals) else None,
                "Cost": cost_vals[idx] if idx < len(cost_vals) else None,
            }
        )
    trend_df = pd.DataFrame(rows, columns=["Period", "Revenue", "Profit", "MarginPct", "Units", "Cost"])

    meta = bundle.get("meta") or {}
    meta_df = pd.DataFrame(
        [
            {"field": "generated_at_utc", "value": pd.Timestamp.utcnow().isoformat()},
            {"field": "dataset_version", "value": meta.get("version")},
            {"field": "window_start", "value": (meta.get("window") or {}).get("start")},
            {"field": "window_end", "value": (meta.get("window") or {}).get("end")},
            {"field": "frequency", "value": freq},
            {"field": "include_current_month", "value": bool(meta.get("include_current_month", include_current_month))},
            {"field": "filters", "value": json.dumps(meta.get("filters") or {}, sort_keys=True, default=str)},
        ]
    )

    if fmt == "csv":
        filename = _export_filename(f"business_performance_trend_{freq}", filters, "csv")
        return dataframe_to_csv_response(trend_df, filename=filename)

    filename = _export_filename(f"business_performance_trend_{freq}", filters, "xlsx")
    return dataframes_to_xlsx_response({"Trend": trend_df, "Metadata": meta_df}, filename=filename)


@page_bp.get("/api/drilldown/<drilldown>")
@login_required
def overview_drilldown_v2(drilldown: str):
    token = str(drilldown or "").strip().lower()
    if token not in {"movers", "margin_risk", "concentration", "data_health"}:
        return _json_error("Unsupported drilldown.", 400)

    fmt = str(request.args.get("format") or "json").strip().lower()
    if fmt not in {"json", "xlsx", "csv"}:
        fmt = "json"
    if fmt == "xlsx" and not xlsx_export_available():
        fmt = "csv"

    dimension = str(request.args.get("dimension") or "customer").strip().lower()
    if token == "movers" and dimension not in {"customer", "product", "region"}:
        dimension = "customer"
    if token == "concentration" and dimension not in {"customer", "product"}:
        dimension = "customer"

    filters, include_current_month, defaulted_window = _overview_effective_filters()
    frame = ov2.build_drilldown_frame(
        filters,
        drilldown=token,
        dimension=dimension,
        include_current_month=include_current_month,
        defaulted_window=defaulted_window,
    )
    if frame is None:
        frame = pd.DataFrame()
    frame = frame.replace({np.nan: None})
    guardrails_meta: Dict[str, Any] | None = None
    if token == "movers":
        min_baseline = _coerce_float(request.args.get("min_baseline"), default=500.0, minimum=0.0)
        min_new_current = _coerce_float(request.args.get("min_new_current"), default=min_baseline, minimum=0.0)
        min_lost_prior = _coerce_float(request.args.get("min_lost_prior"), default=min_baseline, minimum=0.0)
        exclude_low_base = _coerce_bool(request.args.get("exclude_low_base"), default=False)
        frame, guardrails_meta = _apply_movers_guardrails(
            frame,
            min_baseline=min_baseline,
            exclude_low_base=exclude_low_base,
            min_new_current=min_new_current,
            min_lost_prior=min_lost_prior,
        )

    if fmt == "json":
        rows = frame.to_dict(orient="records")
        payload = {
            "drilldown": token,
            "dimension": dimension,
            "rows": rows,
            "meta": {
                "rows": int(len(rows)),
                "filters": _filters_to_payload(filters),
                "include_current_month": include_current_month,
                "defaulted_window": defaulted_window,
            },
        }
        if guardrails_meta is not None:
            payload["meta"]["guardrails"] = guardrails_meta
        return _etag_json(payload)

    bundle_meta = fact_store.query_overview(
        filters,
        include_current_month=include_current_month,
        defaulted_window=defaulted_window,
    ).get("meta", {})
    metadata_df = pd.DataFrame(
        [
            {"field": "generated_at_utc", "value": pd.Timestamp.utcnow().isoformat()},
            {"field": "drilldown", "value": token},
            {"field": "dimension", "value": dimension},
            {"field": "rows", "value": int(len(frame.index))},
            {"field": "dataset_version", "value": bundle_meta.get("version")},
            {"field": "window_start", "value": (bundle_meta.get("window") or {}).get("start")},
            {"field": "window_end", "value": (bundle_meta.get("window") or {}).get("end")},
            {"field": "filters", "value": json.dumps(bundle_meta.get("filters") or {}, sort_keys=True, default=str)},
        ]
    )
    if guardrails_meta is not None:
        for key, value in guardrails_meta.items():
            metadata_df.loc[len(metadata_df.index)] = {"field": f"guardrail_{key}", "value": value}

    stem = f"business_performance_{token}"
    if token in {"movers", "concentration"}:
        stem = f"{stem}_{dimension}"

    if fmt == "csv":
        export_df = frame.copy()
        if guardrails_meta is not None:
            export_df["guardrail_min_baseline"] = guardrails_meta.get("min_baseline")
            export_df["guardrail_exclude_low_base"] = guardrails_meta.get("exclude_low_base")
            export_df["guardrail_min_new_current"] = guardrails_meta.get("min_new_current")
            export_df["guardrail_min_lost_prior"] = guardrails_meta.get("min_lost_prior")
        filename = _export_filename(stem, filters, "csv")
        return dataframe_to_csv_response(export_df, filename=filename)

    filename = _export_filename(stem, filters, "xlsx")
    return dataframes_to_xlsx_response(
        {
            "Metadata": metadata_df,
            "Data": frame,
        },
        filename=filename,
    )

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Dict, List, Tuple
import hashlib
import json
import math
import os
import re
from zoneinfo import ZoneInfo

import pandas as pd
from flask import current_app, g, has_request_context
from flask_login import current_user

from app.services import fact_schema as fs
from app.services import fact_store
from app.services.filters import normalize_filters


def _safe_col(cols: set[str], *candidates: str) -> str | None:
    for cand in candidates:
        if cand and cand in cols:
            return cand
    return None


def _coerce_int(raw: Any, default: int, low: int, high: int) -> int:
    try:
        value = int(str(raw).strip())
    except Exception:
        return default
    return max(low, min(high, value))


@dataclass(frozen=True)
class CohortControls:
    churn_threshold_days: int = 90
    lookback_months: int = 24
    cohort_granularity: str = "month"
    cohort_horizon: int = 12
    reactivation_window_days: int = 30
    at_risk_window_days: int = 30

    def as_dict(self) -> Dict[str, Any]:
        return {
            "churn_threshold_days": int(self.churn_threshold_days),
            "lookback_months": int(self.lookback_months),
            "cohort_granularity": str(self.cohort_granularity),
            "cohort_horizon": int(self.cohort_horizon),
            "reactivation_window_days": int(self.reactivation_window_days),
            "at_risk_window_days": int(self.at_risk_window_days),
        }


@dataclass(frozen=True)
class CohortResolvedState:
    controls: CohortControls
    status: str = "at_risk"
    segmentation: str = "region"
    table_search: str = ""
    table_page: int = 1
    page_size: int = 25
    source: str = "defaults"
    warnings: tuple[str, ...] = ()
    controls_hash: str = ""

    def cache_extras(self) -> Dict[str, Any]:
        extras = self.controls.as_dict()
        extras.update(
            {
                "status": self.status,
                "segmentation": self.segmentation,
                "table_search": self.table_search,
                "table_page": int(self.table_page),
                "page_size": int(self.page_size),
                "controls_hash": self.controls_hash,
            }
        )
        return extras

    def as_session_state(self) -> Dict[str, Any]:
        state = self.controls.as_dict()
        state.update(
            {
                "status": self.status,
                "segmentation": self.segmentation,
                "table_search": self.table_search,
                "table_page": int(self.table_page),
                "page_size": int(self.page_size),
            }
        )
        return state

    def as_query_params(self) -> Dict[str, str]:
        params = {
            "threshold": str(int(self.controls.churn_threshold_days)),
            "lookback_months": str(int(self.controls.lookback_months)),
            "cohort_granularity": str(self.controls.cohort_granularity),
            "cohort_horizon": str(int(self.controls.cohort_horizon)),
            "reactivation_window_days": str(int(self.controls.reactivation_window_days)),
            "at_risk_window_days": str(int(self.controls.at_risk_window_days)),
            "status": self.status,
            "segmentation": self.segmentation,
            "table_page": str(int(self.table_page)),
            "page_size": str(int(self.page_size)),
        }
        if self.table_search:
            params["table_search"] = self.table_search
        return params

    def display_state(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "segmentation": self.segmentation,
            "table_search": self.table_search,
            "table_page": int(self.table_page),
            "page_size": int(self.page_size),
            "source": self.source,
            "controls_hash": self.controls_hash,
            "warnings": list(self.warnings or ()),
            **self.controls.as_dict(),
        }


def parse_controls(args: Any) -> CohortControls:
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: _d)
    gran_raw = str(getter("cohort_granularity", "month") or "month").strip().lower()
    granularity = "quarter" if gran_raw.startswith("q") else "month"
    horizon_default = 8 if granularity == "quarter" else 12

    threshold = _coerce_int(getter("threshold", 90), 90, 30, 365)
    lookback = _coerce_int(getter("lookback_months", 24), 24, 6, 60)
    horizon = _coerce_int(getter("cohort_horizon", horizon_default), horizon_default, 1, 24)
    react_window = _coerce_int(getter("reactivation_window_days", 30), 30, 7, 180)
    at_risk_window = _coerce_int(getter("at_risk_window_days", 30), 30, 1, 120)
    at_risk_window = min(at_risk_window, threshold)

    return CohortControls(
        churn_threshold_days=threshold,
        lookback_months=lookback,
        cohort_granularity=granularity,
        cohort_horizon=horizon,
        reactivation_window_days=react_window,
        at_risk_window_days=at_risk_window,
    )


def _state_get(args: Any, key: str) -> Any:
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: _d)
    try:
        return getter(key, None)
    except TypeError:
        return getter(key)


def _has_explicit_state_value(args: Any, key: str) -> bool:
    raw = _state_get(args, key)
    if raw is None:
        return False
    if isinstance(raw, str):
        return raw.strip() != ""
    return True


def _pick_state_value(args: Any, session_state: Dict[str, Any], key: str, default: Any = None) -> tuple[Any, bool, bool]:
    if _has_explicit_state_value(args, key):
        return _state_get(args, key), True, False
    if key in (session_state or {}):
        return session_state.get(key), False, True
    return default, False, False


def _warn_on_int_override(
    raw: Any,
    *,
    label: str,
    default: int,
    low: int,
    high: int,
    warnings: list[str],
) -> int:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except Exception:
        warnings.append(f"Invalid {label}; using {default}.")
        return default
    bounded = max(low, min(high, value))
    if bounded != value:
        warnings.append(f"{label} out of range; using {bounded}.")
    return bounded


def _normalize_segmentation(raw: Any, *, allow_sales_rep: bool = True) -> tuple[str, str | None]:
    token = str(raw or "region").strip().lower()
    allowed = {"region", "segment"}
    if allow_sales_rep:
        allowed.add("sales_rep")
    if token in allowed:
        return token, None
    fallback = "region"
    return fallback, "Invalid segmentation; showing region."


def _state_hash_payload(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def state_hash_for(
    filters_hash: str | None,
    controls_hash: str | None,
    scope_hash: str | None,
    dataset_version: str | None,
) -> str:
    payload = {
        "filters_hash": str(filters_hash or ""),
        "controls_hash": str(controls_hash or ""),
        "scope_hash": str(scope_hash or ""),
        "dataset_version": str(dataset_version or ""),
    }
    return _state_hash_payload(payload)


def resolve_cohorts_controls(
    args: Any,
    session_state: Dict[str, Any] | None = None,
    *,
    allow_sales_rep: bool = True,
) -> CohortResolvedState:
    session_state = dict(session_state or {})
    warnings: list[str] = []

    threshold_raw, threshold_explicit, threshold_session = _pick_state_value(args, session_state, "threshold", 90)
    lookback_raw, lookback_explicit, lookback_session = _pick_state_value(args, session_state, "lookback_months", 24)
    granularity_raw, granularity_explicit, granularity_session = _pick_state_value(args, session_state, "cohort_granularity", "month")
    horizon_default = 8 if str(granularity_raw or "month").strip().lower().startswith("q") else 12
    horizon_raw, horizon_explicit, horizon_session = _pick_state_value(args, session_state, "cohort_horizon", horizon_default)
    react_raw, react_explicit, react_session = _pick_state_value(args, session_state, "reactivation_window_days", 30)
    at_risk_raw, at_risk_explicit, at_risk_session = _pick_state_value(args, session_state, "at_risk_window_days", 30)
    status_raw, status_explicit, status_session = _pick_state_value(args, session_state, "status", "at_risk")
    seg_raw, seg_explicit, seg_session = _pick_state_value(args, session_state, "segmentation", "region")
    search_raw, search_explicit, search_session = _pick_state_value(args, session_state, "table_search", "")
    page_raw, page_explicit, page_session = _pick_state_value(args, session_state, "table_page", 1)
    size_raw, size_explicit, size_session = _pick_state_value(args, session_state, "page_size", 25)

    explicit_used = any(
        [
            threshold_explicit,
            lookback_explicit,
            granularity_explicit,
            horizon_explicit,
            react_explicit,
            at_risk_explicit,
            status_explicit,
            seg_explicit,
            search_explicit,
            page_explicit,
            size_explicit,
        ]
    )
    session_used = any(
        [
            threshold_session,
            lookback_session,
            granularity_session,
            horizon_session,
            react_session,
            at_risk_session,
            status_session,
            seg_session,
            search_session,
            page_session,
            size_session,
        ]
    )

    merged = {
        "threshold": threshold_raw,
        "lookback_months": lookback_raw,
        "cohort_granularity": granularity_raw,
        "cohort_horizon": horizon_raw,
        "reactivation_window_days": react_raw,
        "at_risk_window_days": at_risk_raw,
    }
    controls = parse_controls(merged)

    if threshold_explicit:
        _warn_on_int_override(
            threshold_raw,
            label="churn threshold",
            default=90,
            low=30,
            high=365,
            warnings=warnings,
        )
    if lookback_explicit:
        _warn_on_int_override(
            lookback_raw,
            label="lookback months",
            default=24,
            low=6,
            high=60,
            warnings=warnings,
        )
    if horizon_explicit:
        _warn_on_int_override(
            horizon_raw,
            label="retention horizon",
            default=horizon_default,
            low=1,
            high=24,
            warnings=warnings,
        )
    if react_explicit:
        _warn_on_int_override(
            react_raw,
            label="reactivation window",
            default=30,
            low=7,
            high=180,
            warnings=warnings,
        )
    if at_risk_explicit:
        normalized_at_risk = _warn_on_int_override(
            at_risk_raw,
            label="at-risk window",
            default=30,
            low=1,
            high=120,
            warnings=warnings,
        )
        if normalized_at_risk > controls.churn_threshold_days:
            warnings.append(
                f"At-risk window cannot exceed churn threshold; using {controls.at_risk_window_days}."
            )

    status = _normalize_status(status_raw)
    if status_explicit and str(status_raw or "").strip().lower() not in {"active", "churned", "at_risk", "reactivated"}:
        warnings.append("Invalid status; showing at-risk customers.")

    segmentation, seg_warning = _normalize_segmentation(seg_raw, allow_sales_rep=allow_sales_rep)
    if seg_warning:
        warnings.append(seg_warning)

    table_search = str(search_raw or "").strip()
    if search_raw is None:
        table_search = ""
    table_page = _warn_on_int_override(
        page_raw,
        label="table page",
        default=1,
        low=1,
        high=999999,
        warnings=warnings if page_explicit else [],
    )
    page_size = _warn_on_int_override(
        size_raw,
        label="page size",
        default=25,
        low=1,
        high=500,
        warnings=warnings if size_explicit else [],
    )

    source = "request" if explicit_used else ("session" if session_used else "defaults")
    hash_payload = {
        **controls.as_dict(),
        "status": status,
        "segmentation": segmentation,
        "table_search": table_search,
        "table_page": int(table_page),
        "page_size": int(page_size),
    }
    controls_hash = _state_hash_payload(hash_payload)
    return CohortResolvedState(
        controls=controls,
        status=status,
        segmentation=segmentation,
        table_search=table_search,
        table_page=int(table_page),
        page_size=int(page_size),
        source=source,
        warnings=tuple(warnings),
        controls_hash=controls_hash,
    )


def _cohorts_debug_enabled() -> bool:
    raw = None
    try:
        raw = current_app.config.get("COHORTS_DEBUG")
    except Exception:
        raw = None
    if raw is None:
        raw = os.getenv("COHORTS_DEBUG", "")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _cohorts_hardened_v3_enabled() -> bool:
    raw = None
    try:
        raw = current_app.config.get("COHORTS_HARDENED_V3")
    except Exception:
        raw = None
    if raw is None:
        raw = os.getenv("COHORTS_HARDENED_V3", "1")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _cohorts_timezone_name() -> str:
    try:
        tz_name = str(current_app.config.get("FACT_REFRESH_TZ") or "").strip()
    except Exception:
        tz_name = ""
    if not tz_name:
        tz_name = str(os.getenv("FACT_REFRESH_TZ") or "America/Vancouver").strip()
    return tz_name or "America/Vancouver"


def _cohorts_today_iso() -> str:
    tz_name = _cohorts_timezone_name()
    try:
        return datetime.now(ZoneInfo(tz_name)).date().isoformat()
    except Exception:
        return datetime.utcnow().date().isoformat()


def _coerce_date_ts(value: Any) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    try:
        ts = pd.to_datetime(value, errors="coerce")
    except Exception:
        ts = pd.NaT
    if ts is None or pd.isna(ts):
        return None
    return pd.Timestamp(ts).normalize()


def _cohorts_dataset_end_ts() -> pd.Timestamp:
    today_ts = _coerce_date_ts(_cohorts_today_iso()) or pd.Timestamp.utcnow().normalize()
    live_ts = None
    try:
        cols = fact_store.list_columns()
        date_col = _safe_col(cols, fs.CANON.date, "DateExpected", "Date")
        if date_col:
            sql = f"SELECT MAX({fact_store.quote_identifier(date_col)}) AS max_date FROM fact"
            row = fact_store.get_conn().execute(sql).fetchone()
            if row:
                live_ts = _coerce_date_ts(row[0])
    except Exception:
        live_ts = None
    if live_ts is not None:
        return min(live_ts, today_ts)

    try:
        meta = fact_store.get_meta() or {}
    except Exception:
        meta = {}
    dataset_ts = _coerce_date_ts(meta.get("date_max") or meta.get("max_date"))
    if dataset_ts is None:
        return today_ts
    return min(dataset_ts, today_ts)


def _debug_log(event: str, **payload: Any) -> None:
    if not _cohorts_debug_enabled():
        return
    try:
        data = dict(payload or {})
        if has_request_context():
            data.setdefault("request_id", getattr(g, "request_id", None))
            try:
                data.setdefault("current_user_id", current_user.get_id() if getattr(current_user, "is_authenticated", False) else None)
            except Exception:
                data.setdefault("current_user_id", getattr(current_user, "id", None))
        current_app.logger.info(event, extra=data)
    except Exception:
        pass


def _scope_summary(scope: Dict[str, Any] | None) -> Dict[str, Any]:
    scope = scope or {}
    return {
        "scope_mode": scope.get("scope_mode"),
        "user_id": scope.get("user_id"),
        "scope_hash": scope.get("scope_hash"),
        "allowed_customer_count": len(scope.get("allowed_customer_ids") or scope.get("customer_ids") or []),
        "allowed_rep_count": len(scope.get("allowed_erp_user_ids") or scope.get("sales_rep_ids") or scope.get("rep_ids") or []),
        "allowed_region_count": len(scope.get("allowed_region_ids") or scope.get("region_ids") or []),
        "allowed_supplier_count": len(scope.get("allowed_supplier_ids") or scope.get("supplier_ids") or []),
    }


def _cohorts_base_filters(filters: Any, filters_meta: Dict[str, Any] | None = None) -> Any:
    params = normalize_filters(filters or {})
    source_label = str((filters_meta or {}).get("source") or "").strip().lower()
    # Strip hidden default windows so cohorts controls define the analysis window.
    if source_label in {"defaults", "default"}:
        try:
            return replace(params, start=None, end=None, preset=None)
        except Exception:
            return params
    return params


def _analysis_window_start_iso(ref_date: str | None, controls: CohortControls) -> str | None:
    if not ref_date:
        return None
    try:
        ref_ts = pd.Timestamp(ref_date)
        start_ts = ref_ts.to_period("M").to_timestamp() - pd.DateOffset(months=controls.lookback_months)
        return start_ts.date().isoformat()
    except Exception:
        return None


def _effective_window_bounds(filters: Any, controls: CohortControls) -> Dict[str, str | None]:
    if hasattr(filters, "start") and hasattr(filters, "end"):
        params = filters
    else:
        params = normalize_filters(filters or {})
    global_start = _coerce_date_ts(getattr(params, "start", None))
    global_end = _coerce_date_ts(getattr(params, "end", None))
    end_cap = _cohorts_dataset_end_ts()
    effective_end_ts = min(global_end, end_cap) if global_end is not None else end_cap
    lookback_start_ts = effective_end_ts.to_period("M").to_timestamp() - pd.DateOffset(months=controls.lookback_months)
    effective_start_ts = lookback_start_ts if global_start is None else max(global_start, lookback_start_ts)
    if effective_start_ts > effective_end_ts:
        effective_start_ts = effective_end_ts
    churn_cutoff_ts = effective_end_ts - pd.Timedelta(days=int(controls.churn_threshold_days))
    return {
        "global_window_start": global_start.date().isoformat() if global_start is not None else None,
        "global_window_end": global_end.date().isoformat() if global_end is not None else None,
        "effective_window_start": effective_start_ts.date().isoformat(),
        "effective_window_end": effective_end_ts.date().isoformat(),
        "window_end_exclusive": (effective_end_ts + pd.Timedelta(days=1)).date().isoformat(),
        "lookback_start": lookback_start_ts.date().isoformat(),
        "churn_cutoff_date": churn_cutoff_ts.date().isoformat(),
        "today_local": _cohorts_today_iso(),
    }


def _analysis_window_meta(
    kpis: Dict[str, Any],
    controls: CohortControls,
    resolver_meta: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    resolver_meta = dict(resolver_meta or {})
    ref_date = (
        resolver_meta.get("effective_window_end")
        or resolver_meta.get("resolved_window_end")
        or kpis.get("ref_date")
        or _cohorts_today_iso()
    )
    analysis_start = (
        resolver_meta.get("effective_window_start")
        or resolver_meta.get("resolved_window_start")
        or _analysis_window_start_iso(ref_date, controls)
    )
    return {
        "analysis_window_start": analysis_start,
        "analysis_window_end": ref_date,
        "ref_date": ref_date,
        "first_order_date": kpis.get("first_order_date"),
        "global_window_start": resolver_meta.get("global_window_start"),
        "global_window_end": resolver_meta.get("global_window_end"),
        "churn_cutoff_date": resolver_meta.get("churn_cutoff_date"),
    }


def _dataset_suffix(controls: CohortControls) -> str:
    gran = "quarter" if controls.cohort_granularity == "quarter" else "month"
    return f"t{int(controls.churn_threshold_days)}_lb{int(controls.lookback_months)}_{gran}_h{int(controls.cohort_horizon)}"


def _low_sample_threshold() -> int:
    return 10


def _log_block_metrics(block: str, frame: pd.DataFrame | None = None, **payload: Any) -> None:
    data = dict(payload or {})
    if isinstance(frame, pd.DataFrame):
        data.setdefault("row_count", int(len(frame.index)))
        if "customer_id" in frame.columns:
            try:
                data.setdefault("distinct_customers", int(frame["customer_id"].nunique(dropna=True)))
            except Exception:
                pass
    _debug_log("cohorts_v2.block", block=block, **data)


def _chart_rows(rows: list[dict[str, Any]], *, top_n: int = 8, other_label: str = "Other") -> list[dict[str, Any]]:
    if not rows:
        return []
    ordered = sorted(
        [dict(row or {}) for row in rows],
        key=lambda rec: (
            -_safe_int(rec.get("customers")),
            -_safe_int(rec.get("churned_customers")),
            str(rec.get("segment") or ""),
        ),
    )
    if len(ordered) <= top_n:
        return ordered

    head = ordered[:top_n]
    tail = ordered[top_n:]
    customers = sum(_safe_int(row.get("customers")) for row in tail)
    churned = sum(_safe_int(row.get("churned_customers")) for row in tail)
    reactivated = sum(_safe_int(row.get("reactivated_customers")) for row in tail)
    threshold = _low_sample_threshold()
    head.append(
        {
            "segment": other_label,
            "customers": customers,
            "churned_customers": churned,
            "reactivated_customers": reactivated,
            "churn_rate_pct": round((churned / customers * 100.0), 2) if customers > 0 else 0.0,
            "low_sample": 0 < customers < threshold,
            "low_sample_threshold": threshold,
        }
    )
    return head


def _granularity_unit(granularity: str) -> str:
    return "quarter" if str(granularity or "").strip().lower() == "quarter" else "month"


def _cohort_label(value: Any, granularity: str) -> str:
    if value is None or pd.isna(value):
        return ""
    ts = pd.Timestamp(value)
    if _granularity_unit(granularity) == "quarter":
        q = ((int(ts.month) - 1) // 3) + 1
        return f"{int(ts.year)}-Q{q}"
    return ts.strftime("%Y-%m")


def _iso_date(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).date().isoformat()


def _safe_float(value: Any) -> float:
    try:
        fval = float(value)
        if math.isnan(fval):
            return 0.0
        return fval
    except Exception:
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        if value is None or pd.isna(value):
            return 0
        return int(value)
    except Exception:
        return 0


def _normalize_status(raw: Any) -> str:
    token = str(raw or "at_risk").strip().lower()
    if token in {"active", "churned", "at_risk", "reactivated"}:
        return token
    return "at_risk"


def _parse_cohort_period(raw: Any, granularity: str) -> pd.Timestamp | None:
    token = str(raw or "").strip()
    if not token:
        return None
    unit = _granularity_unit(granularity)
    if unit == "quarter":
        match = re.match(r"^(\d{4})[-_/ ]?[Qq](\d)$", token)
        if match:
            year = int(match.group(1))
            quarter = int(match.group(2))
            if 1 <= quarter <= 4:
                month = (quarter - 1) * 3 + 1
                return pd.Timestamp(year=year, month=month, day=1)
    if re.match(r"^\d{4}-\d{2}$", token):
        token = f"{token}-01"
    try:
        ts = pd.to_datetime(token, errors="coerce")
    except Exception:
        ts = pd.NaT
    if ts is None or pd.isna(ts):
        return None
    ts = pd.Timestamp(ts).normalize()
    if unit == "quarter":
        month = ((int(ts.month) - 1) // 3) * 3 + 1
        return pd.Timestamp(year=int(ts.year), month=month, day=1)
    return pd.Timestamp(year=int(ts.year), month=int(ts.month), day=1)


def _build_base_cte_legacy(
    filters: Any,
    scope: Dict[str, Any],
    controls: CohortControls,
    filters_meta: Dict[str, Any] | None = None,
) -> tuple[str, List[Any], Dict[str, Any]]:
    cols = fact_store.list_columns()
    date_col = _safe_col(cols, fs.CANON.date, "DateExpected", "Date")
    cust_id_col = _safe_col(cols, fs.CANON.customer_id, "CustomerID")
    cust_name_col = _safe_col(cols, fs.CANON.customer_name, "Customer")
    order_col = _safe_col(cols, fs.CANON.order_id, "OrderID")
    revenue_col = _safe_col(cols, fs.CANON.revenue, "Revenue", "revenue_ordered")
    region_col = _safe_col(cols, fs.CANON.region, "Region")
    sales_rep_col = _safe_col(cols, fs.CANON.sales_rep, "SalesRepName", "PrimarySalesRepName")

    required = [date_col, cust_id_col, cust_name_col, order_col, revenue_col]
    if any(col is None for col in required):
        missing = [name for name, col in {
            "date": date_col,
            "customer_id": cust_id_col,
            "customer_name": cust_name_col,
            "order_id": order_col,
            "revenue": revenue_col,
        }.items() if col is None]
        raise ValueError(f"Missing required columns: {', '.join(missing)}")

    base_filters = _cohorts_base_filters(filters, filters_meta)
    where_sql, where_params, _, _ = fact_store.build_where_clause(
        base_filters,
        cols,
        scope,
        apply_default_window=False,
    )

    q = fact_store.quote_identifier
    date_q = q(date_col)  # type: ignore[arg-type]
    cust_id_q = q(cust_id_col)  # type: ignore[arg-type]
    cust_name_q = q(cust_name_col)  # type: ignore[arg-type]
    order_q = q(order_col)  # type: ignore[arg-type]
    revenue_q = q(revenue_col)  # type: ignore[arg-type]

    region_expr = (
        f"NULLIF(TRIM(CAST({q(region_col)} AS VARCHAR)), '')"  # type: ignore[arg-type]
        if region_col
        else "NULL"
    )
    sales_rep_expr = (
        f"NULLIF(TRIM(CAST({q(sales_rep_col)} AS VARCHAR)), '')"  # type: ignore[arg-type]
        if sales_rep_col
        else "NULL"
    )

    at_risk_floor = max(0, controls.churn_threshold_days - controls.at_risk_window_days)
    today_iso = _cohorts_today_iso()

    base_cte = f"""
        WITH scoped_orders AS (
            SELECT
                CAST({cust_id_q} AS VARCHAR) AS customer_id,
                COALESCE(NULLIF(TRIM(CAST({cust_name_q} AS VARCHAR)), ''), CAST({cust_id_q} AS VARCHAR)) AS customer_name,
                CAST({date_q} AS DATE) AS order_date,
                DATE_TRUNC('month', CAST({date_q} AS DATE)) AS order_month,
                CAST(COALESCE({revenue_q}, 0) AS DOUBLE) AS revenue,
                {region_expr} AS region_raw,
                {sales_rep_expr} AS sales_rep_raw,
                CAST({order_q} AS VARCHAR) AS order_id
            FROM fact
            WHERE {where_sql}
              AND {date_q} IS NOT NULL
              AND {cust_id_q} IS NOT NULL
        ),
        ref AS (
            SELECT
                COALESCE(LEAST(MAX(order_date), CAST(? AS DATE)), CAST(? AS DATE)) AS ref_date,
                MIN(order_date) AS min_order_date
            FROM scoped_orders
        ),
        analysis_window AS (
            SELECT
                ref_date,
                DATE_TRUNC('month', ref_date) - (? * INTERVAL 1 MONTH) AS window_start,
                DATE_TRUNC('month', ref_date) + INTERVAL 1 MONTH AS window_end_exclusive
            FROM ref
        ),
        analysis_orders AS (
            SELECT o.*
            FROM scoped_orders o
            CROSS JOIN analysis_window aw
            WHERE o.order_date >= aw.window_start
              AND o.order_date < aw.window_end_exclusive
        ),
        customer_month AS (
            SELECT
                customer_id,
                ANY_VALUE(customer_name) AS customer_name,
                order_month,
                COUNT(DISTINCT order_id) AS orders_count,
                SUM(revenue) AS revenue
            FROM analysis_orders
            GROUP BY 1,3
        ),
        customer_profile AS (
            SELECT
                customer_id,
                ANY_VALUE(customer_name) AS customer_name,
                MIN(order_date) AS first_order_date,
                DATE_TRUNC('month', MIN(order_date)) AS first_order_month,
                MAX(order_date) AS last_order_date,
                DATE_TRUNC('month', MAX(order_date)) AS last_order_month,
                COUNT(DISTINCT order_id) AS orders_count,
                SUM(revenue) AS lifetime_revenue
            FROM scoped_orders
            GROUP BY 1
        ),
        customer_region_stats AS (
            SELECT customer_id, region_raw AS region, COUNT(*) AS samples, MAX(order_date) AS last_seen
            FROM scoped_orders
            WHERE region_raw IS NOT NULL
            GROUP BY 1,2
        ),
        customer_region_rank AS (
            SELECT
                customer_id,
                region,
                ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY samples DESC, last_seen DESC, region ASC) AS rn
            FROM customer_region_stats
        ),
        customer_sales_rep_stats AS (
            SELECT customer_id, sales_rep_raw AS sales_rep, COUNT(*) AS samples, MAX(order_date) AS last_seen
            FROM scoped_orders
            WHERE sales_rep_raw IS NOT NULL
            GROUP BY 1,2
        ),
        customer_sales_rep_rank AS (
            SELECT
                customer_id,
                sales_rep,
                ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY samples DESC, last_seen DESC, sales_rep ASC) AS rn
            FROM customer_sales_rep_stats
        ),
        customer_dims AS (
            SELECT
                p.customer_id,
                COALESCE(rr.region, 'Unknown') AS region,
                sr.sales_rep
            FROM customer_profile p
            LEFT JOIN customer_region_rank rr
                ON rr.customer_id = p.customer_id
               AND rr.rn = 1
            LEFT JOIN customer_sales_rep_rank sr
                ON sr.customer_id = p.customer_id
               AND sr.rn = 1
        ),
        customer_finance AS (
            SELECT
                o.customer_id,
                SUM(CASE WHEN o.order_date > aw.ref_date - INTERVAL 90 DAY THEN o.revenue ELSE 0 END) AS revenue_last_90,
                SUM(
                    CASE
                        WHEN o.order_date <= aw.ref_date - INTERVAL 90 DAY
                         AND o.order_date > aw.ref_date - INTERVAL 180 DAY
                        THEN o.revenue
                        ELSE 0
                    END
                ) AS revenue_prev_90
            FROM scoped_orders o
            CROSS JOIN analysis_window aw
            GROUP BY 1
        ),
        order_days AS (
            SELECT DISTINCT customer_id, order_date
            FROM scoped_orders
        ),
        order_gaps AS (
            SELECT
                customer_id,
                order_date,
                LEAD(order_date) OVER (PARTITION BY customer_id ORDER BY order_date) AS next_order_date
            FROM order_days
        ),
        churn_events_all AS (
            SELECT
                customer_id,
                order_date + (? * INTERVAL 1 DAY) AS churn_date,
                DATE_TRUNC('month', order_date + (? * INTERVAL 1 DAY)) AS churn_month
            FROM order_gaps
            WHERE next_order_date IS NULL
               OR DATE_DIFF('day', order_date, next_order_date) > ?
        ),
        reactivation_events_all AS (
            SELECT
                customer_id,
                next_order_date AS order_date,
                DATE_TRUNC('month', next_order_date) AS react_month
            FROM order_gaps
            WHERE next_order_date IS NOT NULL
              AND DATE_DIFF('day', order_date, next_order_date) > ?
        ),
        reactivation_by_month AS (
            SELECT react_month, COUNT(DISTINCT customer_id) AS reactivations
            FROM reactivation_events_all
            CROSS JOIN analysis_window aw
            WHERE order_date >= aw.window_start
              AND order_date < aw.window_end_exclusive
            GROUP BY 1
        ),
        reactivation_in_window AS (
            SELECT DISTINCT e.customer_id
            FROM reactivation_events_all e
            CROSS JOIN analysis_window aw
            WHERE e.order_date >= aw.window_start
              AND e.order_date < aw.window_end_exclusive
        ),
        customer_status AS (
            SELECT
                p.customer_id,
                DATE_DIFF('day', p.last_order_date, aw.ref_date) AS days_since_last,
                CASE
                    WHEN DATE_DIFF('day', p.last_order_date, aw.ref_date) > ? THEN 'churned'
                    WHEN DATE_DIFF('day', p.last_order_date, aw.ref_date) BETWEEN ? AND ? THEN 'at_risk'
                    ELSE 'active'
                END AS status
            FROM customer_profile p
            CROSS JOIN analysis_window aw
        ),
        customer_state AS (
            SELECT
                p.customer_id,
                p.customer_name,
                p.first_order_date,
                p.first_order_month,
                p.last_order_date,
                p.last_order_month,
                p.orders_count,
                p.lifetime_revenue,
                d.region,
                d.sales_rep,
                COALESCE(fin.revenue_last_90, 0) AS revenue_last_90,
                COALESCE(fin.revenue_prev_90, 0) AS revenue_prev_90,
                GREATEST(COALESCE(fin.revenue_prev_90, 0) - COALESCE(fin.revenue_last_90, 0), 0) AS lost_revenue_estimate,
                st.days_since_last,
                st.status,
                CASE WHEN rr.customer_id IS NOT NULL THEN TRUE ELSE FALSE END AS reactivated_recent
            FROM customer_profile p
            LEFT JOIN customer_dims d ON d.customer_id = p.customer_id
            LEFT JOIN customer_finance fin ON fin.customer_id = p.customer_id
            LEFT JOIN customer_status st ON st.customer_id = p.customer_id
            LEFT JOIN reactivation_in_window rr ON rr.customer_id = p.customer_id
        ),
        customer_activity_base AS (
            SELECT
                cm.customer_id,
                cm.customer_name,
                p.first_order_date,
                p.first_order_month,
                DATE_TRUNC('{_granularity_unit(controls.cohort_granularity)}', p.first_order_date) AS cohort_period,
                DATE_TRUNC('{_granularity_unit(controls.cohort_granularity)}', cm.order_month) AS order_period,
                cm.order_month,
                cm.orders_count,
                cm.revenue,
                d.region,
                d.sales_rep
            FROM customer_month cm
            JOIN customer_profile p ON p.customer_id = cm.customer_id
            LEFT JOIN customer_dims d ON d.customer_id = cm.customer_id
        )
    """

    params = list(where_params) + [
        today_iso,
        today_iso,
        controls.lookback_months,
        controls.churn_threshold_days,
        controls.churn_threshold_days,
        controls.churn_threshold_days,
        controls.churn_threshold_days,
        controls.churn_threshold_days,
        at_risk_floor,
        controls.churn_threshold_days,
    ]
    meta = {
        "filters_source": (filters_meta or {}).get("filters_source"),
        "source": (filters_meta or {}).get("source"),
        "resolved_window_start": (filters_meta or {}).get("window_start"),
        "resolved_window_end": (filters_meta or {}).get("window_end"),
        "today_local": today_iso,
        "lookback_months": controls.lookback_months,
        "churn_threshold_days": controls.churn_threshold_days,
    }
    return base_cte, params, meta


def _build_base_cte_hardened(
    filters: Any,
    scope: Dict[str, Any],
    controls: CohortControls,
    filters_meta: Dict[str, Any] | None = None,
) -> tuple[str, List[Any], Dict[str, Any]]:
    cols = fact_store.list_columns()
    date_col = _safe_col(cols, fs.CANON.date, "DateExpected", "Date")
    cust_id_col = _safe_col(cols, fs.CANON.customer_id, "CustomerID")
    cust_name_col = _safe_col(cols, fs.CANON.customer_name, "Customer")
    order_col = _safe_col(cols, fs.CANON.order_id, "OrderID")
    revenue_col = _safe_col(cols, fs.CANON.revenue, "Revenue", "revenue_ordered")
    region_col = _safe_col(cols, fs.CANON.region, "Region")
    sales_rep_col = _safe_col(cols, fs.CANON.sales_rep, "SalesRepName", "PrimarySalesRepName")

    required = [date_col, cust_id_col, cust_name_col, order_col, revenue_col]
    if any(col is None for col in required):
        missing = [
            name
            for name, col in {
                "date": date_col,
                "customer_id": cust_id_col,
                "customer_name": cust_name_col,
                "order_id": order_col,
                "revenue": revenue_col,
            }.items()
            if col is None
        ]
        raise ValueError(f"Missing required columns: {', '.join(missing)}")

    base_filters = _cohorts_base_filters(filters, filters_meta)
    bounds = _effective_window_bounds(base_filters, controls)
    history_filters = replace(
        normalize_filters(base_filters),
        start=None,
        end=_coerce_date_ts(bounds.get("effective_window_end")),
        preset=None,
    )
    history_where_sql, history_params, _, _ = fact_store.build_where_clause(
        history_filters,
        cols,
        scope,
        apply_default_window=False,
    )

    q = fact_store.quote_identifier
    date_q = q(date_col)  # type: ignore[arg-type]
    cust_id_q = q(cust_id_col)  # type: ignore[arg-type]
    cust_name_q = q(cust_name_col)  # type: ignore[arg-type]
    order_q = q(order_col)  # type: ignore[arg-type]
    revenue_q = q(revenue_col)  # type: ignore[arg-type]

    region_expr = (
        f"NULLIF(TRIM(CAST({q(region_col)} AS VARCHAR)), '')"  # type: ignore[arg-type]
        if region_col
        else "NULL"
    )
    sales_rep_expr = (
        f"NULLIF(TRIM(CAST({q(sales_rep_col)} AS VARCHAR)), '')"  # type: ignore[arg-type]
        if sales_rep_col
        else "NULL"
    )

    at_risk_floor = max(0, controls.churn_threshold_days - controls.at_risk_window_days)

    base_cte = f"""
        WITH scoped_orders_history AS (
            SELECT
                CAST({cust_id_q} AS VARCHAR) AS customer_id,
                COALESCE(NULLIF(TRIM(CAST({cust_name_q} AS VARCHAR)), ''), CAST({cust_id_q} AS VARCHAR)) AS customer_name,
                CAST({date_q} AS DATE) AS order_date,
                DATE_TRUNC('month', CAST({date_q} AS DATE)) AS order_month,
                CAST(COALESCE({revenue_q}, 0) AS DOUBLE) AS revenue,
                {region_expr} AS region_raw,
                {sales_rep_expr} AS sales_rep_raw,
                CAST({order_q} AS VARCHAR) AS order_id
            FROM fact
            WHERE {history_where_sql}
              AND {date_q} IS NOT NULL
              AND {cust_id_q} IS NOT NULL
        ),
        ref AS (
            SELECT CAST(? AS DATE) AS ref_date
        ),
        analysis_window AS (
            SELECT
                ref_date,
                CAST(? AS DATE) AS window_start,
                CAST(? AS DATE) AS window_end_exclusive
            FROM ref
        ),
        analysis_orders AS (
            SELECT o.*
            FROM scoped_orders_history o
            CROSS JOIN analysis_window aw
            WHERE o.order_date >= aw.window_start
              AND o.order_date < aw.window_end_exclusive
        ),
        window_customers AS (
            SELECT DISTINCT customer_id
            FROM analysis_orders
        ),
        customer_month AS (
            SELECT
                customer_id,
                ANY_VALUE(customer_name) AS customer_name,
                order_month,
                COUNT(DISTINCT order_id) AS orders_count,
                SUM(revenue) AS revenue
            FROM analysis_orders
            GROUP BY 1,3
        ),
        customer_profile AS (
            SELECT
                o.customer_id,
                ANY_VALUE(o.customer_name) AS customer_name,
                MIN(o.order_date) AS first_order_date,
                DATE_TRUNC('month', MIN(o.order_date)) AS first_order_month,
                MAX(o.order_date) AS last_order_date,
                DATE_TRUNC('month', MAX(o.order_date)) AS last_order_month,
                COUNT(DISTINCT o.order_id) AS orders_count,
                SUM(o.revenue) AS lifetime_revenue
            FROM scoped_orders_history o
            JOIN window_customers wc ON wc.customer_id = o.customer_id
            GROUP BY 1
        ),
        customer_region_stats AS (
            SELECT o.customer_id, o.region_raw AS region, COUNT(*) AS samples, MAX(o.order_date) AS last_seen
            FROM scoped_orders_history o
            JOIN window_customers wc ON wc.customer_id = o.customer_id
            WHERE o.region_raw IS NOT NULL
            GROUP BY 1,2
        ),
        customer_region_rank AS (
            SELECT
                customer_id,
                region,
                ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY samples DESC, last_seen DESC, region ASC) AS rn
            FROM customer_region_stats
        ),
        customer_sales_rep_stats AS (
            SELECT o.customer_id, o.sales_rep_raw AS sales_rep, COUNT(*) AS samples, MAX(o.order_date) AS last_seen
            FROM scoped_orders_history o
            JOIN window_customers wc ON wc.customer_id = o.customer_id
            WHERE o.sales_rep_raw IS NOT NULL
            GROUP BY 1,2
        ),
        customer_sales_rep_rank AS (
            SELECT
                customer_id,
                sales_rep,
                ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY samples DESC, last_seen DESC, sales_rep ASC) AS rn
            FROM customer_sales_rep_stats
        ),
        customer_dims AS (
            SELECT
                p.customer_id,
                COALESCE(rr.region, 'Unknown') AS region,
                sr.sales_rep
            FROM customer_profile p
            LEFT JOIN customer_region_rank rr
                ON rr.customer_id = p.customer_id
               AND rr.rn = 1
            LEFT JOIN customer_sales_rep_rank sr
                ON sr.customer_id = p.customer_id
               AND sr.rn = 1
        ),
        customer_finance AS (
            SELECT
                o.customer_id,
                SUM(CASE WHEN o.order_date > aw.ref_date - INTERVAL 90 DAY THEN o.revenue ELSE 0 END) AS revenue_last_90,
                SUM(
                    CASE
                        WHEN o.order_date <= aw.ref_date - INTERVAL 90 DAY
                         AND o.order_date > aw.ref_date - INTERVAL 180 DAY
                        THEN o.revenue
                        ELSE 0
                    END
                ) AS revenue_prev_90
            FROM scoped_orders_history o
            JOIN window_customers wc ON wc.customer_id = o.customer_id
            CROSS JOIN analysis_window aw
            GROUP BY 1
        ),
        order_days AS (
            SELECT DISTINCT o.customer_id, o.order_date
            FROM scoped_orders_history o
            JOIN window_customers wc ON wc.customer_id = o.customer_id
        ),
        order_gaps AS (
            SELECT
                customer_id,
                order_date,
                LEAD(order_date) OVER (PARTITION BY customer_id ORDER BY order_date) AS next_order_date
            FROM order_days
        ),
        churn_events_all AS (
            SELECT
                customer_id,
                order_date + (? * INTERVAL 1 DAY) AS churn_date,
                DATE_TRUNC('month', order_date + (? * INTERVAL 1 DAY)) AS churn_month
            FROM order_gaps
            WHERE next_order_date IS NULL
               OR DATE_DIFF('day', order_date, next_order_date) > ?
        ),
        reactivation_events_all AS (
            SELECT
                customer_id,
                next_order_date AS order_date,
                DATE_TRUNC('month', next_order_date) AS react_month
            FROM order_gaps
            WHERE next_order_date IS NOT NULL
              AND DATE_DIFF('day', order_date, next_order_date) > ?
        ),
        reactivation_by_month AS (
            SELECT react_month, COUNT(DISTINCT customer_id) AS reactivations
            FROM reactivation_events_all
            CROSS JOIN analysis_window aw
            WHERE order_date >= aw.window_start
              AND order_date < aw.window_end_exclusive
            GROUP BY 1
        ),
        reactivation_in_window AS (
            SELECT DISTINCT e.customer_id
            FROM reactivation_events_all e
            CROSS JOIN analysis_window aw
            WHERE e.order_date >= aw.window_start
              AND e.order_date < aw.window_end_exclusive
        ),
        customer_status AS (
            SELECT
                p.customer_id,
                DATE_DIFF('day', p.last_order_date, aw.ref_date) AS days_since_last,
                CASE
                    WHEN DATE_DIFF('day', p.last_order_date, aw.ref_date) > ? THEN 'churned'
                    WHEN DATE_DIFF('day', p.last_order_date, aw.ref_date) BETWEEN ? AND ? THEN 'at_risk'
                    ELSE 'active'
                END AS status
            FROM customer_profile p
            CROSS JOIN analysis_window aw
        ),
        customer_state AS (
            SELECT
                p.customer_id,
                p.customer_name,
                p.first_order_date,
                p.first_order_month,
                p.last_order_date,
                p.last_order_month,
                p.orders_count,
                p.lifetime_revenue,
                d.region,
                d.sales_rep,
                COALESCE(fin.revenue_last_90, 0) AS revenue_last_90,
                COALESCE(fin.revenue_prev_90, 0) AS revenue_prev_90,
                GREATEST(COALESCE(fin.revenue_prev_90, 0) - COALESCE(fin.revenue_last_90, 0), 0) AS lost_revenue_estimate,
                st.days_since_last,
                st.status,
                CASE WHEN rr.customer_id IS NOT NULL THEN TRUE ELSE FALSE END AS reactivated_recent
            FROM customer_profile p
            LEFT JOIN customer_dims d ON d.customer_id = p.customer_id
            LEFT JOIN customer_finance fin ON fin.customer_id = p.customer_id
            LEFT JOIN customer_status st ON st.customer_id = p.customer_id
            LEFT JOIN reactivation_in_window rr ON rr.customer_id = p.customer_id
        ),
        customer_activity_base AS (
            SELECT
                cm.customer_id,
                cm.customer_name,
                p.first_order_date,
                p.first_order_month,
                DATE_TRUNC('{_granularity_unit(controls.cohort_granularity)}', p.first_order_date) AS cohort_period,
                DATE_TRUNC('{_granularity_unit(controls.cohort_granularity)}', cm.order_month) AS order_period,
                cm.order_month,
                cm.orders_count,
                cm.revenue,
                d.region,
                d.sales_rep
            FROM customer_month cm
            JOIN customer_profile p ON p.customer_id = cm.customer_id
            LEFT JOIN customer_dims d ON d.customer_id = cm.customer_id
        )
    """

    params = list(history_params) + [
        bounds.get("effective_window_end"),
        bounds.get("effective_window_start"),
        bounds.get("window_end_exclusive"),
        controls.churn_threshold_days,
        controls.churn_threshold_days,
        controls.churn_threshold_days,
        controls.churn_threshold_days,
        controls.churn_threshold_days,
        at_risk_floor,
        controls.churn_threshold_days,
    ]
    meta = {
        "filters_source": (filters_meta or {}).get("filters_source"),
        "source": (filters_meta or {}).get("source"),
        "resolved_window_start": (filters_meta or {}).get("window_start"),
        "resolved_window_end": (filters_meta or {}).get("window_end"),
        "today_local": bounds.get("today_local"),
        "lookback_months": controls.lookback_months,
        "churn_threshold_days": controls.churn_threshold_days,
        "global_window_start": bounds.get("global_window_start"),
        "global_window_end": bounds.get("global_window_end"),
        "effective_window_start": bounds.get("effective_window_start"),
        "effective_window_end": bounds.get("effective_window_end"),
        "churn_cutoff_date": bounds.get("churn_cutoff_date"),
    }
    return base_cte, params, meta


def _build_base_cte(
    filters: Any,
    scope: Dict[str, Any],
    controls: CohortControls,
    filters_meta: Dict[str, Any] | None = None,
) -> tuple[str, List[Any], Dict[str, Any]]:
    if not _cohorts_hardened_v3_enabled():
        return _build_base_cte_legacy(filters, scope, controls, filters_meta=filters_meta)
    return _build_base_cte_hardened(filters, scope, controls, filters_meta=filters_meta)


def _state_rows_to_dicts(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    rows: list[dict[str, Any]] = []
    for rec in df.to_dict(orient="records"):
        rows.append(
            {
                "customer_id": rec.get("customer_id"),
                "customer_name": rec.get("customer_name") or rec.get("customer_id"),
                "region": rec.get("region") or "Unknown",
                "sales_rep": rec.get("sales_rep"),
                "last_order_date": _iso_date(rec.get("last_order_date")),
                "days_since_last": _safe_int(rec.get("days_since_last")),
                "status": rec.get("status") or "active",
                "reactivated_recent": bool(rec.get("reactivated_recent")),
                "lifetime_revenue": round(_safe_float(rec.get("lifetime_revenue")), 2),
                "revenue_prev_90": round(_safe_float(rec.get("revenue_prev_90")), 2),
                "revenue_last_90": round(_safe_float(rec.get("revenue_last_90")), 2),
                "lost_revenue_estimate": round(_safe_float(rec.get("lost_revenue_estimate")), 2),
            }
        )
    return rows


def _strip_total_rows(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return frame
    records: list[dict[str, Any]] = []
    for rec in frame.to_dict(orient="records"):
        if not isinstance(rec, dict):
            continue
        rec.pop("total_rows", None)
        records.append(rec)
    return pd.DataFrame.from_records(records)


def _kpi_payload(base_cte: str, base_params: List[Any]) -> Dict[str, Any]:
    active_sql = (
        "SUM(CASE WHEN status IN ('active', 'at_risk') THEN 1 ELSE 0 END)"
        if _cohorts_hardened_v3_enabled()
        else "SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END)"
    )
    sql = f"""
        {base_cte}
        SELECT
            COUNT(*) AS customers_total,
            {active_sql} AS active_customers,
            SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS healthy_active_customers,
            SUM(CASE WHEN status = 'churned' THEN 1 ELSE 0 END) AS churned_customers,
            SUM(CASE WHEN status = 'at_risk' THEN 1 ELSE 0 END) AS at_risk_customers,
            SUM(CASE WHEN reactivated_recent THEN 1 ELSE 0 END) AS reactivated_customers,
            SUM(CASE WHEN status = 'churned' THEN lost_revenue_estimate ELSE 0 END) AS lost_revenue_estimate,
            SUM(revenue_prev_90) AS revenue_prev_90_total,
            SUM(revenue_last_90) AS revenue_last_90_total,
            MAX(aw.ref_date) AS ref_date,
            MIN(first_order_date) AS first_order_date
        FROM customer_state
        CROSS JOIN analysis_window aw
    """
    frame = fact_store.execute_sql_df(sql, base_params, tag="customers.cohorts_v2.kpis")
    if frame.empty:
        return {
            "customers_total": 0,
            "active_customers": 0,
            "churned_customers": 0,
            "at_risk_customers": 0,
            "reactivated_customers": 0,
            "churn_rate_pct": 0.0,
            "reactivation_rate_pct": 0.0,
            "lost_revenue_estimate": 0.0,
            "net_retention_pct": None,
            "ref_date": None,
            "first_order_date": None,
        }

    row = frame.iloc[0].to_dict()
    total = _safe_int(row.get("customers_total"))
    churned = _safe_int(row.get("churned_customers"))
    reactivated = _safe_int(row.get("reactivated_customers"))
    prev_90 = _safe_float(row.get("revenue_prev_90_total"))
    last_90 = _safe_float(row.get("revenue_last_90_total"))
    net_retention = (last_90 / prev_90 * 100.0) if prev_90 > 0 else None

    active = _safe_int(row.get("active_customers"))
    reconciled = (active + churned) == total if _cohorts_hardened_v3_enabled() else (active + churned + _safe_int(row.get("at_risk_customers"))) == total
    if _cohorts_hardened_v3_enabled() and not reconciled:
        _debug_log(
            "cohorts_v2.reconcile",
            customers_total=total,
            active_customers=active,
            churned_customers=churned,
            at_risk_customers=_safe_int(row.get("at_risk_customers")),
            reactivated_customers=reactivated,
        )

    return {
        "customers_total": total,
        "active_customers": active,
        "healthy_active_customers": _safe_int(row.get("healthy_active_customers")),
        "churned_customers": churned,
        "at_risk_customers": _safe_int(row.get("at_risk_customers")),
        "reactivated_customers": reactivated,
        "churn_rate_pct": round((churned / total * 100.0), 2) if total > 0 else 0.0,
        "reactivation_rate_pct": round((reactivated / churned * 100.0), 2) if churned > 0 else 0.0,
        "lost_revenue_estimate": round(_safe_float(row.get("lost_revenue_estimate")), 2),
        "net_retention_pct": (round(net_retention, 2) if net_retention is not None else None),
        "ref_date": _iso_date(row.get("ref_date")),
        "first_order_date": _iso_date(row.get("first_order_date")),
        "reconciled": reconciled,
    }


def _retention_payload(base_cte: str, base_params: List[Any], controls: CohortControls) -> tuple[Dict[str, Any], pd.DataFrame]:
    unit = _granularity_unit(controls.cohort_granularity)
    sql = f"""
        {base_cte}
        ,cohorts_in_window AS (
            SELECT
                customer_id,
                DATE_TRUNC('{unit}', first_order_date) AS cohort_period
            FROM customer_state
            CROSS JOIN analysis_window aw
            WHERE first_order_date >= aw.window_start
              AND first_order_date < aw.window_end_exclusive
        )
        ,retention AS (
            SELECT
                c.cohort_period,
                DATE_DIFF('{unit}', c.cohort_period, ca.order_period) AS month_index,
                COUNT(DISTINCT ca.customer_id) AS retained_count
            FROM cohorts_in_window c
            JOIN customer_activity_base ca
              ON ca.customer_id = c.customer_id
             AND ca.cohort_period = c.cohort_period
            WHERE ca.order_period >= c.cohort_period
            GROUP BY 1,2
        ),
        cohort_sizes AS (
            SELECT
                cohort_period,
                COUNT(DISTINCT customer_id) AS cohort_size
            FROM cohorts_in_window
            GROUP BY 1
        )
        SELECT
            r.cohort_period,
            r.month_index,
            s.cohort_size,
            r.retained_count,
            CASE WHEN s.cohort_size > 0 THEN r.retained_count::DOUBLE / s.cohort_size * 100 ELSE 0 END AS retention_pct
        FROM retention r
        JOIN cohort_sizes s ON s.cohort_period = r.cohort_period
        WHERE r.month_index BETWEEN 0 AND ?
        ORDER BY r.cohort_period, r.month_index
    """
    frame = fact_store.execute_sql_df(
        sql,
        base_params + [controls.cohort_horizon],
        tag="customers.cohorts_v2.retention",
    )

    if frame.empty:
        payload = {
            "cohorts": [],
            "cohort_periods": [],
            "month_indexes": list(range(0, controls.cohort_horizon + 1)),
            "values": [],
            "retained": [],
            "cohort_sizes": [],
            "rows": [],
        }
        return payload, pd.DataFrame()

    frame = frame.copy()
    frame["cohort_period"] = pd.to_datetime(frame["cohort_period"], errors="coerce")
    frame["month_index"] = pd.to_numeric(frame["month_index"], errors="coerce").fillna(0).astype(int)
    frame["cohort_size"] = pd.to_numeric(frame["cohort_size"], errors="coerce").fillna(0).astype(int)
    frame["retained_count"] = pd.to_numeric(frame["retained_count"], errors="coerce").fillna(0).astype(int)
    frame["retention_pct"] = pd.to_numeric(frame["retention_pct"], errors="coerce").fillna(0.0).astype(float)

    cohorts = sorted([c for c in frame["cohort_period"].dropna().unique()])
    month_indexes = list(range(0, controls.cohort_horizon + 1))
    size_map = (
        frame.groupby("cohort_period", observed=True)["cohort_size"]
        .max()
        .to_dict()
    )
    retained_map = {
        (row.cohort_period, int(row.month_index)): int(row.retained_count)
        for row in frame.itertuples()
    }
    pct_map = {
        (row.cohort_period, int(row.month_index)): float(row.retention_pct)
        for row in frame.itertuples()
    }

    values: list[list[float]] = []
    retained: list[list[int]] = []
    cohort_sizes: list[int] = []
    cohort_labels: list[str] = []
    cohort_periods: list[str] = []
    rows: list[dict[str, Any]] = []

    for cohort_period in cohorts:
        cohort_period_ts = pd.Timestamp(cohort_period)
        cohort_label = _cohort_label(cohort_period_ts, controls.cohort_granularity)
        cohort_labels.append(cohort_label)
        cohort_periods.append(cohort_period_ts.date().isoformat())
        size = int(size_map.get(cohort_period_ts, 0))
        cohort_sizes.append(size)
        row_vals: list[float] = []
        row_retained: list[int] = []
        for month_index in month_indexes:
            retained_count = int(retained_map.get((cohort_period_ts, month_index), 0))
            pct = float(pct_map.get((cohort_period_ts, month_index), 0.0))
            row_vals.append(round(pct, 2))
            row_retained.append(retained_count)
            rows.append(
                {
                    "cohort_period": cohort_period_ts.date().isoformat(),
                    "cohort_label": cohort_label,
                    "month_index": month_index,
                    "cohort_size": size,
                    "retained_count": retained_count,
                    "retention_pct": round(pct, 2),
                }
            )
        values.append(row_vals)
        retained.append(row_retained)

    payload = {
        "cohorts": cohort_labels,
        "cohort_periods": cohort_periods,
        "month_indexes": month_indexes,
        "values": values,
        "retained": retained,
        "cohort_sizes": cohort_sizes,
        "rows": rows,
    }
    export_frame = pd.DataFrame(rows)
    return payload, export_frame


def _trend_payload(base_cte: str, base_params: List[Any], controls: CohortControls) -> tuple[Dict[str, Any], pd.DataFrame]:
    sql = f"""
        {base_cte}
        ,months AS (
            SELECT month_start
            FROM analysis_window aw,
            generate_series(
                DATE_TRUNC('month', aw.window_start),
                DATE_TRUNC('month', aw.ref_date),
                INTERVAL 1 MONTH
            ) AS t(month_start)
        ),
        month_bounds AS (
            SELECT
                m.month_start,
                LEAST(
                    aw.ref_date,
                    CAST(m.month_start + INTERVAL 1 MONTH - INTERVAL 1 DAY AS DATE)
                ) AS month_end
            FROM months m
            CROSS JOIN analysis_window aw
        ),
        new_churn AS (
            SELECT
                churn_month AS month_start,
                COUNT(DISTINCT customer_id) AS new_churn_count
            FROM churn_events_all
            CROSS JOIN analysis_window aw
            WHERE churn_date >= aw.window_start
              AND churn_date < aw.window_end_exclusive
            GROUP BY 1
        ),
        customer_base AS (
            SELECT
                mb.month_start,
                COUNT(*) AS customer_base
            FROM month_bounds mb
            JOIN customer_state cs
              ON cs.first_order_date <= mb.month_end
            GROUP BY 1
        ),
        churned_snapshot AS (
            SELECT
                mb.month_start,
                COUNT(*) AS churned_count
            FROM month_bounds mb
            JOIN customer_state cs
              ON cs.first_order_date <= mb.month_end
             AND cs.last_order_date <= mb.month_end - (? * INTERVAL 1 DAY)
            GROUP BY 1
        )
        SELECT
            m.month_start,
            COALESCE(nc.new_churn_count, 0) AS new_churn_count,
            COALESCE(rm.reactivations, 0) AS reactivations,
            COALESCE(cb.customer_base, 0) AS customer_base,
            COALESCE(ch.churned_count, 0) AS churned_count,
            CASE
                WHEN COALESCE(cb.customer_base, 0) > 0
                THEN COALESCE(ch.churned_count, 0)::DOUBLE / cb.customer_base * 100
                ELSE 0
            END AS churn_rate_pct
        FROM months m
        LEFT JOIN new_churn nc ON nc.month_start = m.month_start
        LEFT JOIN reactivation_by_month rm ON rm.react_month = m.month_start
        LEFT JOIN customer_base cb ON cb.month_start = m.month_start
        LEFT JOIN churned_snapshot ch ON ch.month_start = m.month_start
        ORDER BY m.month_start
    """
    frame = fact_store.execute_sql_df(
        sql,
        base_params + [controls.churn_threshold_days],
        tag="customers.cohorts_v2.trend",
    )
    if frame.empty:
        payload = {
            "months": [],
            "new_churn": [],
            "reactivations": [],
            "churn_rate_pct": [],
            "churned_count": [],
            "rows": [],
        }
        return payload, pd.DataFrame()

    frame = frame.copy()
    frame["month_start"] = pd.to_datetime(frame["month_start"], errors="coerce")
    frame["new_churn_count"] = pd.to_numeric(frame["new_churn_count"], errors="coerce").fillna(0).astype(int)
    frame["reactivations"] = pd.to_numeric(frame["reactivations"], errors="coerce").fillna(0).astype(int)
    frame["customer_base"] = pd.to_numeric(frame["customer_base"], errors="coerce").fillna(0).astype(int)
    frame["churned_count"] = pd.to_numeric(frame["churned_count"], errors="coerce").fillna(0).astype(int)
    frame["churn_rate_pct"] = pd.to_numeric(frame["churn_rate_pct"], errors="coerce").fillna(0.0).astype(float)

    rows: list[dict[str, Any]] = []
    for row in frame.itertuples():
        rows.append(
            {
                "month": pd.Timestamp(row.month_start).strftime("%Y-%m"),
                "new_churn_count": int(row.new_churn_count),
                "reactivations": int(row.reactivations),
                "churned_count": int(row.churned_count),
                "customer_base": int(row.customer_base),
                "churn_rate_pct": round(float(row.churn_rate_pct), 2),
            }
        )

    payload = {
        "months": [r["month"] for r in rows],
        "new_churn": [r["new_churn_count"] for r in rows],
        "reactivations": [r["reactivations"] for r in rows],
        "churn_rate_pct": [r["churn_rate_pct"] for r in rows],
        "churned_count": [r["churned_count"] for r in rows],
        "rows": rows,
    }
    return payload, pd.DataFrame(rows)


def _segmentation_frame(base_cte: str, base_params: List[Any], dimension: str) -> pd.DataFrame:
    dim = str(dimension or "").strip().lower()
    if dim == "region":
        expr = "COALESCE(NULLIF(region, ''), 'Unknown')"
        sql = f"""
            {base_cte}
            SELECT
                {expr} AS segment,
                COUNT(*) AS customers,
                SUM(CASE WHEN status = 'churned' THEN 1 ELSE 0 END) AS churned_customers,
                SUM(CASE WHEN reactivated_recent THEN 1 ELSE 0 END) AS reactivated_customers,
                CASE WHEN COUNT(*) > 0 THEN SUM(CASE WHEN status = 'churned' THEN 1 ELSE 0 END)::DOUBLE / COUNT(*) * 100 ELSE 0 END AS churn_rate_pct
            FROM customer_state
            GROUP BY 1
            ORDER BY churned_customers DESC, customers DESC, segment
        """
        return fact_store.execute_sql_df(sql, base_params, tag="customers.cohorts_v2.seg.region")

    if dim == "sales_rep":
        expr = "COALESCE(NULLIF(sales_rep, ''), 'Unassigned')"
        sql = f"""
            {base_cte}
            SELECT
                {expr} AS segment,
                COUNT(*) AS customers,
                SUM(CASE WHEN status = 'churned' THEN 1 ELSE 0 END) AS churned_customers,
                SUM(CASE WHEN reactivated_recent THEN 1 ELSE 0 END) AS reactivated_customers,
                CASE WHEN COUNT(*) > 0 THEN SUM(CASE WHEN status = 'churned' THEN 1 ELSE 0 END)::DOUBLE / COUNT(*) * 100 ELSE 0 END AS churn_rate_pct
            FROM customer_state
            GROUP BY 1
            ORDER BY churned_customers DESC, customers DESC, segment
        """
        return fact_store.execute_sql_df(sql, base_params, tag="customers.cohorts_v2.seg.sales_rep")

    sql = f"""
        {base_cte}
        ,ranked AS (
            SELECT
                customer_id,
                status,
                reactivated_recent,
                NTILE(4) OVER (ORDER BY lifetime_revenue DESC NULLS LAST) AS tier
            FROM customer_state
        )
        SELECT
            CASE tier
                WHEN 1 THEN 'Tier 1 (Top 25%)'
                WHEN 2 THEN 'Tier 2'
                WHEN 3 THEN 'Tier 3'
                ELSE 'Tier 4'
            END AS segment,
            COUNT(*) AS customers,
            SUM(CASE WHEN status = 'churned' THEN 1 ELSE 0 END) AS churned_customers,
            SUM(CASE WHEN reactivated_recent THEN 1 ELSE 0 END) AS reactivated_customers,
            CASE WHEN COUNT(*) > 0 THEN SUM(CASE WHEN status = 'churned' THEN 1 ELSE 0 END)::DOUBLE / COUNT(*) * 100 ELSE 0 END AS churn_rate_pct
        FROM ranked
        GROUP BY 1
        ORDER BY segment
    """
    return fact_store.execute_sql_df(sql, base_params, tag="customers.cohorts_v2.seg.segment")


def _segmentation_rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    threshold = _low_sample_threshold()
    rows = []
    for rec in frame.to_dict(orient="records"):
        customers = _safe_int(rec.get("customers"))
        churned_customers = _safe_int(rec.get("churned_customers"))
        reactivated_customers = _safe_int(rec.get("reactivated_customers"))
        rows.append(
            {
                "segment": rec.get("segment") or "Unknown",
                "customers": customers,
                "churned_customers": churned_customers,
                "reactivated_customers": reactivated_customers,
                "churn_rate_pct": round(_safe_float(rec.get("churn_rate_pct")), 2),
                "low_sample": 0 < customers < threshold,
                "low_sample_threshold": threshold,
            }
        )
    return rows


def _drivers_preview(base_cte: str, base_params: List[Any], limit: int = 25) -> dict[str, list[dict[str, Any]]]:
    churned_sql = f"""
        {base_cte}
        SELECT
            customer_id,
            customer_name,
            region,
            sales_rep,
            last_order_date,
            days_since_last,
            status,
            reactivated_recent,
            lifetime_revenue,
            revenue_prev_90,
            revenue_last_90,
            lost_revenue_estimate
        FROM customer_state
        WHERE status = 'churned'
        ORDER BY lost_revenue_estimate DESC, revenue_prev_90 DESC, lifetime_revenue DESC, customer_name
        LIMIT ?
    """
    at_risk_sql = f"""
        {base_cte}
        SELECT
            customer_id,
            customer_name,
            region,
            sales_rep,
            last_order_date,
            days_since_last,
            status,
            reactivated_recent,
            lifetime_revenue,
            revenue_prev_90,
            revenue_last_90,
            lost_revenue_estimate
        FROM customer_state
        WHERE status = 'at_risk'
        ORDER BY lost_revenue_estimate DESC, revenue_prev_90 DESC, lifetime_revenue DESC, customer_name
        LIMIT ?
    """
    churned_df = fact_store.execute_sql_df(churned_sql, base_params + [limit], tag="customers.cohorts_v2.drivers.churned")
    at_risk_df = fact_store.execute_sql_df(at_risk_sql, base_params + [limit], tag="customers.cohorts_v2.drivers.at_risk")
    return {
        "top_churn_risk": _state_rows_to_dicts(churned_df),
        "at_risk_preview": _state_rows_to_dicts(at_risk_df),
    }


def build_cohorts_payload(
    filters: Any,
    scope: Dict[str, Any],
    args: Any,
    filters_meta: Dict[str, Any] | None = None,
    state: CohortResolvedState | None = None,
) -> Dict[str, Any]:
    state = state or resolve_cohorts_controls(args, allow_sales_rep=bool(scope.get("is_admin")))
    controls = state.controls
    base_cte, base_params, resolver_meta = _build_base_cte(filters, scope, controls, filters_meta=filters_meta)
    dataset_version = fact_store.cache_buster()

    _debug_log(
        "cohorts_v2.request",
        dataset_version=dataset_version,
        resolved_filters_source=resolver_meta.get("source"),
        resolved_filters_origin=resolver_meta.get("filters_source"),
        resolved_window_start=resolver_meta.get("resolved_window_start"),
        resolved_window_end=resolver_meta.get("resolved_window_end"),
        today_local=resolver_meta.get("today_local"),
        controls_source=state.source,
        controls_hash=state.controls_hash,
        status=state.status,
        segmentation=state.segmentation,
        churn_threshold_days=controls.churn_threshold_days,
        lookback_months=controls.lookback_months,
        cohort_granularity=controls.cohort_granularity,
        cohort_horizon=controls.cohort_horizon,
        **_scope_summary(scope),
    )

    kpis = _kpi_payload(base_cte, base_params)
    window_meta = _analysis_window_meta(kpis, controls, resolver_meta)
    dataset_version = str(dataset_version or "")
    state_hash = state_hash_for(
        (filters_meta or {}).get("filters_hash"),
        state.controls_hash,
        scope.get("scope_hash"),
        dataset_version,
    )
    _debug_log(
        "cohorts_v2.window",
        analysis_window_start=window_meta.get("analysis_window_start"),
        analysis_window_end=window_meta.get("analysis_window_end"),
        global_window_start=window_meta.get("global_window_start"),
        global_window_end=window_meta.get("global_window_end"),
        churn_cutoff_date=window_meta.get("churn_cutoff_date"),
        ref_date=window_meta.get("ref_date"),
        first_order_date=window_meta.get("first_order_date"),
        state_hash=state_hash,
    )
    retention, retention_df = _retention_payload(base_cte, base_params, controls)
    trend, trend_df = _trend_payload(base_cte, base_params, controls)

    region_df = _segmentation_frame(base_cte, base_params, "region")
    sales_rep_df = _segmentation_frame(base_cte, base_params, "sales_rep")
    segment_df = _segmentation_frame(base_cte, base_params, "segment")

    region_rows = _segmentation_rows(region_df)
    sales_rep_rows = _segmentation_rows(sales_rep_df)
    segment_rows = _segmentation_rows(segment_df)
    region_chart_rows = _chart_rows(region_rows, top_n=8, other_label="Other")
    unknown_region_customers = next(
        (int(r.get("customers") or 0) for r in region_rows if str(r.get("segment")) == "Unknown"),
        0,
    )
    total_customers = max(_safe_int(kpis.get("customers_total")), 0)
    unknown_region_pct = round((unknown_region_customers / total_customers * 100.0), 2) if total_customers > 0 else 0.0
    low_sample_regions = sum(1 for row in region_rows if row.get("low_sample"))

    drivers = _drivers_preview(base_cte, base_params, limit=25)

    _log_block_metrics(
        "kpis",
        None,
        customers_total=total_customers,
        active_customers=_safe_int(kpis.get("active_customers")),
        churned_customers=_safe_int(kpis.get("churned_customers")),
        at_risk_customers=_safe_int(kpis.get("at_risk_customers")),
        reactivated_customers=_safe_int(kpis.get("reactivated_customers")),
    )
    _log_block_metrics(
        "retention",
        retention_df,
        distinct_cohorts=len(retention.get("cohorts") or []),
        horizon=controls.cohort_horizon,
    )
    _log_block_metrics(
        "churn_trend",
        trend_df,
        distinct_months=len(trend.get("months") or []),
    )
    _log_block_metrics(
        "churn_by_region",
        region_df,
        distinct_segments=len(region_rows),
        unknown_region_pct=unknown_region_pct,
        low_sample_segments=low_sample_regions,
    )
    _log_block_metrics(
        "churn_by_sales_rep",
        sales_rep_df,
        distinct_segments=len(sales_rep_rows),
    )
    _log_block_metrics(
        "churn_segment",
        segment_df,
        distinct_segments=len(segment_rows),
    )

    warnings: list[str] = []
    warnings.extend(list(state.warnings or ()))
    if _cohorts_hardened_v3_enabled() and not bool(kpis.get("reconciled")):
        warnings.append("Cohort KPI reconciliation warning: active plus churned did not match the scoped customer base.")
    if unknown_region_customers > 0:
        warnings.append(
            f"{unknown_region_customers} customers ({unknown_region_pct:.2f}%) have missing region mapping and are shown as 'Unknown'."
        )
    if low_sample_regions > 0:
        warnings.append(
            f"{low_sample_regions} regions are marked low-sample (<{_low_sample_threshold()} customers), so 100% churn bars are not statistically reliable."
        )
    if not retention.get("cohorts"):
        warnings.append("Insufficient data for cohort retention. Try increasing lookback months or broadening filters.")

    payload = {
        "controls": controls.as_dict(),
        "state": state.display_state(),
        "definitions": {
            "cohort": "Cohort = customers whose first purchase date is in the selected cohort period.",
            "retention": "Retention in period k = percent of cohort customers with at least one order in period k after first purchase.",
            "churn": "Active Customers = customers whose last order date is after the churn cutoff. Churned = customer whose last order date is on or before the churn cutoff.",
            "reactivated": "Reactivated = customer who had a prior inactivity gap beyond the churn threshold and then placed an order during the current analysis window.",
            "at_risk": "At-risk = customer with days-since-last-order between threshold-N and threshold.",
            "region": "Churn by region shows churned count and churn rate, where churn rate = churned customers / total customers in the region.",
            "trend": "Churn trend uses month-end snapshots: churned customers at period end / customers with history by that month.",
            "net_retention": "90-day revenue retention proxy = revenue in the last 90 days / revenue in the prior 90 days for the current cohort scope.",
        },
        "kpis": kpis,
        "retention": retention,
        "trend": trend,
        "segmentation": {
            "region": region_rows,
            "region_chart": region_chart_rows,
            "sales_rep": sales_rep_rows,
            "segment": segment_rows,
            "unknown_region_customers": unknown_region_customers,
            "unknown_region_pct": unknown_region_pct,
            "low_sample_regions": low_sample_regions,
        },
        "drivers": drivers,
        "warnings": warnings,
        "data_completeness": {
            "status": ("incomplete" if unknown_region_customers > 0 else "complete"),
            "unknown_region_customers": unknown_region_customers,
            "unknown_region_pct": unknown_region_pct,
            "low_sample_regions": low_sample_regions,
            "message": (
                f"{unknown_region_customers} customers are missing region mapping."
                if unknown_region_customers > 0
                else "Region mappings are complete for the current scope."
            ),
        },
        "exports": {
            "cohort_heatmap_rows": len(retention_df.index),
            "churn_trend_rows": len(trend_df.index),
            "region_rows": len(region_df.index),
            "sales_rep_rows": len(sales_rep_df.index),
            "segment_rows": len(segment_df.index),
        },
        "meta": {
            "analysis_window_start": window_meta.get("analysis_window_start"),
            "analysis_window_end": window_meta.get("analysis_window_end"),
            "global_window_start": window_meta.get("global_window_start"),
            "global_window_end": window_meta.get("global_window_end"),
            "churn_cutoff_date": window_meta.get("churn_cutoff_date"),
            "ref_date": kpis.get("ref_date"),
            "first_order_date": kpis.get("first_order_date"),
            "dataset_version": dataset_version,
            "filters_source": resolver_meta.get("filters_source"),
            "resolved_filters_source": resolver_meta.get("source"),
            "resolved_window_start": resolver_meta.get("resolved_window_start"),
            "resolved_window_end": resolver_meta.get("resolved_window_end"),
            "effective_window_start": resolver_meta.get("effective_window_start"),
            "effective_window_end": resolver_meta.get("effective_window_end"),
            "today_local": resolver_meta.get("today_local"),
            "scope_hash": scope.get("scope_hash"),
            "controls_hash": state.controls_hash,
            "controls_source": state.source,
            "status": state.status,
            "segmentation": state.segmentation,
            "filters_hash": (filters_meta or {}).get("filters_hash"),
            "state_hash": state_hash,
        },
    }
    return payload


def fetch_churn_status_list(
    filters: Any,
    scope: Dict[str, Any],
    args: Any,
    filters_meta: Dict[str, Any] | None = None,
    state: CohortResolvedState | None = None,
    *,
    status: str | None = None,
    search: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
    export_all: bool = False,
) -> Dict[str, Any]:
    state = state or resolve_cohorts_controls(args, allow_sales_rep=bool(scope.get("is_admin")))
    controls = state.controls
    base_cte, base_params, resolver_meta = _build_base_cte(filters, scope, controls, filters_meta=filters_meta)
    dataset_version = fact_store.cache_buster()
    state_hash = state_hash_for(
        (filters_meta or {}).get("filters_hash"),
        state.controls_hash,
        scope.get("scope_hash"),
        dataset_version,
    )

    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: _d)
    status_norm = _normalize_status(status if status is not None else state.status)
    search_value = search if search is not None else state.table_search
    if search_value in (None, ""):
        search_value = getter("search", "")
    search_raw = str(search_value or "").strip().lower()
    page_num = int(page if page is not None else state.table_page)
    size_num = int(page_size if page_size is not None else state.page_size)
    offset = max(0, (int(page_num) - 1) * int(size_num))

    if status_norm == "active":
        status_clause = "status = 'active'"
    elif status_norm == "churned":
        status_clause = "status = 'churned'"
    elif status_norm == "reactivated":
        status_clause = "reactivated_recent = TRUE"
    else:
        status_clause = "status = 'at_risk'"

    search_like = f"%{search_raw}%"
    where_sql = f"""
        {status_clause}
        AND (
            ? = ''
            OR LOWER(CAST(customer_id AS VARCHAR)) LIKE ?
            OR LOWER(CAST(customer_name AS VARCHAR)) LIKE ?
            OR LOWER(CAST(COALESCE(region, '') AS VARCHAR)) LIKE ?
            OR LOWER(CAST(COALESCE(sales_rep, '') AS VARCHAR)) LIKE ?
        )
    """
    params_common = base_params + [search_raw, search_like, search_like, search_like, search_like]

    if export_all:
        sql = f"""
            {base_cte}
            SELECT
                customer_id,
                customer_name,
                region,
                sales_rep,
                last_order_date,
                days_since_last,
                status,
                reactivated_recent,
                lifetime_revenue,
                revenue_prev_90,
                revenue_last_90,
                lost_revenue_estimate
            FROM customer_state
            WHERE {where_sql}
            ORDER BY lost_revenue_estimate DESC, lifetime_revenue DESC, customer_name
        """
        frame = fact_store.execute_sql_df(sql, params_common, tag="customers.cohorts_v2.status_list.export")
        rows = _state_rows_to_dicts(frame)
        total = len(rows)
        _log_block_metrics(
            "status_list",
            frame,
            status=status_norm,
            export_all=True,
            total_rows=total,
        )
        return {
            "status": status_norm,
            "search": search_raw,
            "controls_hash": state.controls_hash,
            "state_hash": state_hash,
            "filters_hash": (filters_meta or {}).get("filters_hash"),
            "analysis_window_start": resolver_meta.get("effective_window_start") or resolver_meta.get("resolved_window_start"),
            "analysis_window_end": resolver_meta.get("effective_window_end") or resolver_meta.get("resolved_window_end"),
            "rows": rows,
            "page": 1,
            "page_size": total,
            "total_rows": total,
            "total_pages": 1,
            "warnings": list(state.warnings or ()),
        }

    sql = f"""
        {base_cte}
        SELECT
            *,
            COUNT(*) OVER() AS total_rows
        FROM (
            SELECT
                customer_id,
                customer_name,
                region,
                sales_rep,
                last_order_date,
                days_since_last,
                status,
                reactivated_recent,
                lifetime_revenue,
                revenue_prev_90,
                revenue_last_90,
                lost_revenue_estimate
            FROM customer_state
            WHERE {where_sql}
            ORDER BY lost_revenue_estimate DESC, lifetime_revenue DESC, customer_name
        ) s
        LIMIT ? OFFSET ?
    """
    frame = fact_store.execute_sql_df(
        sql,
        params_common + [size_num, offset],
        tag="customers.cohorts_v2.status_list.page",
    )
    total_rows = _safe_int(frame.iloc[0].get("total_rows")) if not frame.empty else 0
    rows_frame = _strip_total_rows(frame)
    rows = _state_rows_to_dicts(rows_frame)
    total_pages = math.ceil(total_rows / size_num) if size_num > 0 and total_rows > 0 else 0
    _log_block_metrics(
        "status_list",
        rows_frame,
        status=status_norm,
        export_all=False,
        total_rows=total_rows,
        page=page_num,
        page_size=size_num,
    )
    return {
        "status": status_norm,
        "search": search_raw,
        "controls_hash": state.controls_hash,
        "state_hash": state_hash,
        "filters_hash": (filters_meta or {}).get("filters_hash"),
        "analysis_window_start": resolver_meta.get("effective_window_start") or resolver_meta.get("resolved_window_start"),
        "analysis_window_end": resolver_meta.get("effective_window_end") or resolver_meta.get("resolved_window_end"),
        "rows": rows,
        "page": int(page_num),
        "page_size": int(size_num),
        "total_rows": int(total_rows),
        "total_pages": int(total_pages),
        "warnings": list(state.warnings or ()),
    }


def fetch_cohort_drilldown(
    filters: Any,
    scope: Dict[str, Any],
    args: Any,
    filters_meta: Dict[str, Any] | None = None,
    state: CohortResolvedState | None = None,
    *,
    cohort: str | None = None,
    month_index: int | None = None,
    search: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
    export_all: bool = False,
) -> Dict[str, Any]:
    state = state or resolve_cohorts_controls(args, allow_sales_rep=bool(scope.get("is_admin")))
    controls = state.controls
    base_cte, base_params, resolver_meta = _build_base_cte(filters, scope, controls, filters_meta=filters_meta)
    dataset_version = fact_store.cache_buster()
    state_hash = state_hash_for(
        (filters_meta or {}).get("filters_hash"),
        state.controls_hash,
        scope.get("scope_hash"),
        dataset_version,
    )
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: _d)

    cohort_token = cohort if cohort is not None else str(getter("cohort", "") or "")
    cohort_period = _parse_cohort_period(cohort_token, controls.cohort_granularity)
    if cohort_period is None:
        return {
            "cohort": cohort_token,
            "month_index": 0,
            "rows": [],
            "page": 1,
            "page_size": 0,
            "total_rows": 0,
            "total_pages": 0,
        }

    idx = month_index if month_index is not None else _coerce_int(getter("month_index", 0), 0, 0, 120)
    search_raw = (search if search is not None else getter("search", "") or "").strip().lower()
    page_num = page if page is not None else _coerce_int(getter("page", 1), 1, 1, 999999)
    size_num = page_size if page_size is not None else _coerce_int(getter("page_size", 50), 50, 1, 500)
    offset = max(0, (int(page_num) - 1) * int(size_num))
    search_like = f"%{search_raw}%"

    unit = _granularity_unit(controls.cohort_granularity)
    where_sql = f"""
        ca.cohort_period = CAST(? AS DATE)
        AND DATE_DIFF('{unit}', ca.cohort_period, ca.order_period) = ?
        AND (
            ? = ''
            OR LOWER(CAST(cs.customer_id AS VARCHAR)) LIKE ?
            OR LOWER(CAST(cs.customer_name AS VARCHAR)) LIKE ?
            OR LOWER(CAST(COALESCE(cs.region, '') AS VARCHAR)) LIKE ?
            OR LOWER(CAST(COALESCE(cs.sales_rep, '') AS VARCHAR)) LIKE ?
        )
    """
    params_common = base_params + [
        cohort_period.date().isoformat(),
        idx,
        search_raw,
        search_like,
        search_like,
        search_like,
        search_like,
    ]

    if export_all:
        sql = f"""
            {base_cte}
            ,retained_cells AS (
                SELECT DISTINCT customer_id, cohort_period, order_period
                FROM customer_activity_base
            )
            SELECT
                cs.customer_id,
                cs.customer_name,
                cs.region,
                cs.sales_rep,
                cs.last_order_date,
                cs.days_since_last,
                cs.status,
                cs.reactivated_recent,
                cs.lifetime_revenue,
                cs.revenue_prev_90,
                cs.revenue_last_90,
                cs.lost_revenue_estimate
            FROM customer_state cs
            JOIN retained_cells ca ON ca.customer_id = cs.customer_id
            WHERE {where_sql}
            ORDER BY cs.lost_revenue_estimate DESC, cs.lifetime_revenue DESC, cs.customer_name
        """
        frame = fact_store.execute_sql_df(sql, params_common, tag="customers.cohorts_v2.drilldown.export")
        rows = _state_rows_to_dicts(frame)
        total = len(rows)
        _log_block_metrics(
            "cohort_drilldown",
            frame,
            cohort=cohort_period.date().isoformat(),
            month_index=idx,
            export_all=True,
            total_rows=total,
        )
        return {
            "cohort": cohort_period.date().isoformat(),
            "cohort_label": _cohort_label(cohort_period, controls.cohort_granularity),
            "month_index": int(idx),
            "search": search_raw,
            "controls_hash": state.controls_hash,
            "state_hash": state_hash,
            "filters_hash": (filters_meta or {}).get("filters_hash"),
            "analysis_window_start": resolver_meta.get("effective_window_start") or resolver_meta.get("resolved_window_start"),
            "analysis_window_end": resolver_meta.get("effective_window_end") or resolver_meta.get("resolved_window_end"),
            "rows": rows,
            "page": 1,
            "page_size": total,
            "total_rows": total,
            "total_pages": 1,
            "warnings": list(state.warnings or ()),
        }

    sql = f"""
        {base_cte}
        ,retained_cells AS (
            SELECT DISTINCT customer_id, cohort_period, order_period
            FROM customer_activity_base
        )
        SELECT
            *,
            COUNT(*) OVER() AS total_rows
        FROM (
            SELECT
                cs.customer_id,
                cs.customer_name,
                cs.region,
                cs.sales_rep,
                cs.last_order_date,
                cs.days_since_last,
                cs.status,
                cs.reactivated_recent,
                cs.lifetime_revenue,
                cs.revenue_prev_90,
                cs.revenue_last_90,
                cs.lost_revenue_estimate
            FROM customer_state cs
            JOIN retained_cells ca ON ca.customer_id = cs.customer_id
            WHERE {where_sql}
            ORDER BY cs.lost_revenue_estimate DESC, cs.lifetime_revenue DESC, cs.customer_name
        ) d
        LIMIT ? OFFSET ?
    """
    frame = fact_store.execute_sql_df(
        sql,
        params_common + [size_num, offset],
        tag="customers.cohorts_v2.drilldown.page",
    )
    total_rows = _safe_int(frame.iloc[0].get("total_rows")) if not frame.empty else 0
    rows_frame = _strip_total_rows(frame)
    rows = _state_rows_to_dicts(rows_frame)
    total_pages = math.ceil(total_rows / size_num) if size_num > 0 and total_rows > 0 else 0
    _log_block_metrics(
        "cohort_drilldown",
        rows_frame,
        cohort=cohort_period.date().isoformat(),
        month_index=idx,
        export_all=False,
        total_rows=total_rows,
        page=page_num,
        page_size=size_num,
    )
    return {
        "cohort": cohort_period.date().isoformat(),
        "cohort_label": _cohort_label(cohort_period, controls.cohort_granularity),
        "month_index": int(idx),
        "search": search_raw,
        "controls_hash": state.controls_hash,
        "state_hash": state_hash,
        "filters_hash": (filters_meta or {}).get("filters_hash"),
        "analysis_window_start": resolver_meta.get("effective_window_start") or resolver_meta.get("resolved_window_start"),
        "analysis_window_end": resolver_meta.get("effective_window_end") or resolver_meta.get("resolved_window_end"),
        "rows": rows,
        "page": int(page_num),
        "page_size": int(size_num),
        "total_rows": int(total_rows),
        "total_pages": int(total_pages),
        "warnings": list(state.warnings or ()),
    }


def _drivers_export_frame(base_cte: str, base_params: List[Any]) -> pd.DataFrame:
    sql = f"""
        {base_cte}
        SELECT
            customer_id,
            customer_name,
            region,
            sales_rep,
            last_order_date,
            days_since_last,
            status,
            reactivated_recent,
            lifetime_revenue,
            revenue_prev_90,
            revenue_last_90,
            lost_revenue_estimate
        FROM customer_state
        WHERE status = 'churned'
        ORDER BY lost_revenue_estimate DESC, revenue_prev_90 DESC, lifetime_revenue DESC, customer_name
    """
    frame = fact_store.execute_sql_df(sql, base_params, tag="customers.cohorts_v2.drivers.export")
    if not frame.empty and "last_order_date" in frame.columns:
        frame["last_order_date"] = pd.to_datetime(frame["last_order_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return frame


def _pretty_columns(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()
    rename_map = {
        "customer_id": "Customer ID",
        "customer_name": "Customer Name",
        "region": "Region",
        "sales_rep": "Sales Rep",
        "last_order_date": "Last Order Date",
        "days_since_last": "Days Since Last Order",
        "status": "Status",
        "reactivated_recent": "Reactivated Recently",
        "lifetime_revenue": "Lifetime Revenue",
        "revenue_prev_90": "Revenue Previous 90 Days",
        "revenue_last_90": "Revenue Last 90 Days",
        "lost_revenue_estimate": "Lost Revenue Estimate",
        "cohort_period": "Cohort Period",
        "cohort_label": "Cohort Label",
        "month_index": "Month Index",
        "cohort_size": "Cohort Size",
        "retained_count": "Retained Customers",
        "retention_pct": "Retention %",
        "month": "Month",
        "new_churn_count": "New Churned Customers",
        "reactivations": "Reactivations",
        "churned_count": "Churned Customers At Period End",
        "customer_base": "Customer Base",
        "churn_rate_pct": "Churn Rate %",
        "segment": "Segment",
        "customers": "Customers",
        "churned_customers": "Churned Customers",
        "reactivated_customers": "Reactivated Customers",
        "low_sample": "Low Sample",
        "low_sample_threshold": "Low Sample Threshold",
    }
    return frame.rename(columns=rename_map)


def dataset_definitions_frame(dataset: str) -> pd.DataFrame:
    ds = str(dataset or "").strip().lower()
    rows: list[dict[str, str]] = []
    rows.extend(
        [
            {"Field": "Cohort", "Definition": "Customers grouped by first purchase period (month or quarter)."},
            {"Field": "Retention %", "Definition": "Retained customers / cohort size * 100 for each month index."},
            {"Field": "Churned", "Definition": "No orders in the last N days, where N is churn threshold."},
            {"Field": "At-risk", "Definition": "Days since last order falls in threshold-N to threshold window."},
            {"Field": "Reactivated", "Definition": "Customer had a prior inactivity gap > threshold and later returned."},
        ]
    )
    if ds == "churn_trend":
        rows.append({"Field": "New Churned Customers", "Definition": "Customers crossing churn threshold in that month."})
        rows.append({"Field": "Churned Customers At Period End", "Definition": "Customers whose last order is older than the churn threshold at that month-end snapshot."})
        rows.append({"Field": "Churn Rate %", "Definition": "Churned customers at period end / customer base for month * 100."})
    if ds in {"churn_region", "churn_sales_rep", "churn_segment"}:
        rows.append({"Field": "Low Sample", "Definition": "True when the segment has fewer than 10 customers, so the churn rate should be treated cautiously."})
    return pd.DataFrame(rows)


def build_export_dataset(
    filters: Any,
    scope: Dict[str, Any],
    args: Any,
    dataset: str,
    filters_meta: Dict[str, Any] | None = None,
    state: CohortResolvedState | None = None,
) -> tuple[pd.DataFrame, str]:
    state = state or resolve_cohorts_controls(args, allow_sales_rep=bool(scope.get("is_admin")))
    controls = state.controls
    base_cte, base_params, _ = _build_base_cte(filters, scope, controls, filters_meta=filters_meta)
    ds = str(dataset or "").strip().lower()
    suffix = _dataset_suffix(controls)

    if ds == "cohort_heatmap":
        _, frame = _retention_payload(base_cte, base_params, controls)
        return _pretty_columns(frame), f"cohort_retention_heatmap_{suffix}"

    if ds == "churn_trend":
        _, frame = _trend_payload(base_cte, base_params, controls)
        return _pretty_columns(frame), f"churn_trend_{suffix}"

    if ds == "churn_region":
        frame = pd.DataFrame(_segmentation_rows(_segmentation_frame(base_cte, base_params, "region")))
        return _pretty_columns(frame), f"churn_by_region_{suffix}"

    if ds == "churn_sales_rep":
        frame = pd.DataFrame(_segmentation_rows(_segmentation_frame(base_cte, base_params, "sales_rep")))
        return _pretty_columns(frame), f"churn_by_sales_rep_{suffix}"

    if ds == "churn_segment":
        frame = pd.DataFrame(_segmentation_rows(_segmentation_frame(base_cte, base_params, "segment")))
        return _pretty_columns(frame), f"churn_by_segment_{suffix}"

    if ds == "cohort_drilldown":
        drill = fetch_cohort_drilldown(filters, scope, args, filters_meta=filters_meta, state=state, export_all=True)
        return _pretty_columns(pd.DataFrame(drill.get("rows") or [])), f"cohort_drilldown_customers_{suffix}"

    if ds == "status_list":
        status_rows = fetch_churn_status_list(filters, scope, args, filters_meta=filters_meta, state=state, export_all=True)
        status_name = status_rows.get("status") or "status"
        return _pretty_columns(pd.DataFrame(status_rows.get("rows") or [])), f"{status_name}_customers_{suffix}"

    if ds == "churn_drivers":
        frame = _drivers_export_frame(base_cte, base_params)
        return _pretty_columns(frame), f"top_churn_risk_customers_{suffix}"

    raise ValueError(f"Unsupported export dataset: {dataset}")

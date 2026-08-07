# filters.py — robust, production-ready global filter parsing & application
from __future__ import annotations

import os
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd


FISCAL_YEAR_START_MONTH = 10
FISCAL_YEAR_START_DAY = 1
DEFAULT_DATE_PRESET = "current_fy"
FISCAL_DATE_TYPE = "fiscal"
_FISCAL_PRESET_TOKENS = {
    "current_fy",
    "previous_fy",
    "current_fq",
    "previous_fq",
    "current_fm",
    "previous_fm",
    "fytd_comparison",
}


# ─────────────────────────────────────────────────────────────────────────────
# Model
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class FilterParams:
    """
    Canonical, immutable filter envelope used across endpoints.

    Conventions:
    - Empty tuples mean "All" (no filtering applied).
    - start/end are tz-naive pandas Timestamps (backend always coerces).
    - complete_months_only is a hint; most endpoints ignore it unless needed.
    - preset captures the chosen date_preset (e.g., "current_fy").
    """
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None
    statuses: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    methods: tuple[str, ...] = ()
    customers: tuple[str, ...] = ()
    suppliers: tuple[str, ...] = ()
    products: tuple[str, ...] = ()
    sales_reps: tuple[str, ...] = ()
    protein_groups: tuple[str, ...] = ()
    yield_min: float | None = None
    yield_max: float | None = None
    preset: str | None = None
    date_type: str | None = None
    protein_min: float | None = None
    protein_max: float | None = None
    protein_name_like: str | None = None
    complete_months_only: bool = True

    def __iter__(self):
        yield from (
            self.start,
            self.end,
            self.statuses,
            self.regions,
            self.methods,
            self.customers,
            self.suppliers,
            self.products,
            self.sales_reps,
            self.protein_groups,
            self.yield_min,
            self.yield_max,
            self.preset,
            self.date_type,
            self.protein_min,
            self.protein_max,
            self.protein_name_like,
            self.complete_months_only,
        )


_EMPTY_FILTERS = FilterParams(
    start=None,
    end=None,
    statuses=tuple(),
    regions=tuple(),
    methods=tuple(),
    customers=tuple(),
    suppliers=tuple(),
    products=tuple(),
    sales_reps=tuple(),
    protein_groups=tuple(),
    yield_min=None,
    yield_max=None,
    preset=None,
    date_type=None,
    protein_min=None,
    protein_max=None,
    protein_name_like=None,
    complete_months_only=False,
)

_WINDOW_CACHE: tuple[pd.Timestamp, pd.Timestamp] | None = None
_WINDOW_CACHE_TS: pd.Timestamp | None = None
_MIN_DEFAULT_START = pd.Timestamp(year=2019, month=1, day=1)
_OPTIONS_ALLOWLIST_SANITIZE = str(os.getenv("FILTER_SANITIZE_WITH_OPTIONS", "0")).strip().lower() in {"1", "true", "yes", "on"}


def _coerce_tznaive(ts: pd.Timestamp) -> pd.Timestamp:
    """Return a tz-naive copy of a pandas Timestamp."""
    try:
        if getattr(ts, "tzinfo", None) is not None:
            # tz_convert may fail if naive; try tz_localize(None) last.
            try:
                return ts.tz_convert(None)  # type: ignore[attr-defined]
            except Exception:
                return ts.tz_localize(None)  # type: ignore[attr-defined]
        return ts
    except Exception:
        return ts


def _default_filter_window() -> tuple[pd.Timestamp, pd.Timestamp]:
    """
    Derive a sensible default window:
    - honour FILTER_DEFAULT_MONTHS / DEFAULT_MONTH_WINDOW (fallback 3)
    - clamp to available data range when possible (cached for ~10 minutes)
    - return tz-naive, day-normalized Timestamps (start <= end)
    """
    raw = os.getenv("FILTER_DEFAULT_MONTHS") or os.getenv("DEFAULT_MONTH_WINDOW") or "3"
    try:
        months = max(1, int(str(raw).strip()))
    except Exception:
        months = 3

    global _WINDOW_CACHE, _WINDOW_CACHE_TS

    now = pd.Timestamp.utcnow()
    now = _coerce_tznaive(now).normalize()

    # refresh cache every 10 minutes
    refresh = True
    if _WINDOW_CACHE_TS is not None:
        try:
            refresh = (now - _WINDOW_CACHE_TS) > pd.Timedelta(minutes=10)
        except Exception:
            refresh = True

    if refresh:
        try:
            # Local import to avoid import cycles during module load.
            from app.services import fact_store  # type: ignore

            cols = fact_store.list_columns()
            date_col = fact_store.choose_column(
                ("Date", "date", "OrderDate", "DateOrdered", "DateExpected", "ShipDate"),
                cols,
            )
            if date_col:
                sql = f"SELECT min({fact_store.quote_identifier(date_col)}) AS min_d, max({fact_store.quote_identifier(date_col)}) AS max_d FROM fact"
                conn = fact_store.get_conn()
                row = conn.execute(sql).fetchone()
                if row:
                    min_raw = row[0] if len(row) > 0 else None
                    max_raw = row[1] if len(row) > 1 else None
                    min_ts = pd.to_datetime(min_raw, errors="coerce") if min_raw is not None else None
                    max_ts = pd.to_datetime(max_raw, errors="coerce") if max_raw is not None else None
                    if pd.notna(min_ts) and pd.notna(max_ts):
                        data_start = _coerce_tznaive(min_ts).normalize()
                        data_end = _coerce_tznaive(max_ts).normalize()
                        _WINDOW_CACHE = (data_start, data_end)
                        _WINDOW_CACHE_TS = now
        except Exception:
            _WINDOW_CACHE = None
            _WINDOW_CACHE_TS = now

    end = now
    start = (end - pd.DateOffset(months=months)).normalize().replace(day=1)

    if _WINDOW_CACHE:
        data_start, data_end = _WINDOW_CACHE
        if data_end < end:
            end = data_end
        if data_start > start:
            start = data_start

    try:
        if start < _MIN_DEFAULT_START:
            start = _MIN_DEFAULT_START
    except Exception:
        pass

    if start > end:
        start = end - pd.DateOffset(months=months)

    return start, end


# ─────────────────────────────────────────────────────────────────────────────
# Parsing utilities
# ─────────────────────────────────────────────────────────────────────────────
_SENTINELS_ALL = {"all", "*", "__all__"}

_FILTER_PARAM_NAMES = {
    "start", "start_date", "startdate", "date_start",
    "end", "end_date", "enddate", "date_end",
    "preset", "date_preset", "range_preset",
    "date_type", "datetype",
    "status", "statuses", "order_status", "order_statuses",
    "region", "regions", "region_id", "region_ids", "regionid",
    "shipping_method", "shipping_methods", "shippingmethod", "shippingmethods", "methods",
    "ship_method", "ship_methods", "ship_method_id", "ship_method_ids", "shipmethodid", "shipmethodids",
    "customer", "customers", "customer_id", "customer_ids", "customerid",
    "supplier", "suppliers", "supplier_id", "supplier_ids", "supplierid",
    "product", "products", "product_id", "product_ids", "productid",
    # Only plural sales-rep keys are treated as global filters.
    # Singular rep identifiers (e.g. salesrep_id) are route/entity params on drilldowns.
    "sales_reps", "sales_rep_ids",
    "salesreps", "salesrep_ids",
    "protein_groups", "protein_group", "meat_type", "species",
    "yield_min", "yield_max", "yield_range",
    "protein_min", "protein_max", "protein_name", "protein_name_like", "protein",
    "complete_months_only", "completeMonthsOnly", "full_months_only",
}
_FILTER_CONTROL_NAMES = {"_gf", "_gf_reset", "_filters", "global_filters_flag"}
STICKY_FILTERS_SESSION_KEY = "global_filters_v1"
_STICKY_FILTERS_REV = 2
_STICKY_FILTERS_MAX_ITEMS = 200
_LEGACY_STICKY_KEYS = ("filters", "global_filters")
ACTIVE_SAVED_VIEW_SESSION_KEY = "active_saved_view_id"
FILTERS_LAST_APPLIED_SESSION_KEY = "global_filters_last_applied_at"
_SCOPE_DIMENSION_KEYS: tuple[tuple[str, str, str], ...] = (
    ("regions", "allowed_region_ids", "region_ids"),
    ("customers", "allowed_customer_ids", "customer_ids"),
    ("suppliers", "allowed_supplier_ids", "supplier_ids"),
    ("sales_reps", "allowed_erp_user_ids", "sales_rep_ids"),
)
_OPTION_BUCKET_ALIASES: dict[str, tuple[str, ...]] = {
    "statuses": ("statuses",),
    "regions": ("regions",),
    "methods": ("methods", "shipping_methods", "ship_methods"),
    "customers": ("customers",),
    "suppliers": ("suppliers",),
    "products": ("products",),
    "sales_reps": ("sales_reps", "sales_rep_ids"),
    "protein_groups": ("protein_groups", "protein_group", "meat_type", "species"),
}


def _iter_keys(source: Any) -> Iterable[str]:
    if source is None:
        return ()
    if hasattr(source, "keys"):
        try:
            return list(source.keys())
        except Exception:
            return ()
    if isinstance(source, Mapping):
        return list(source.keys())
    return ()


def _canonical_key(name: Any) -> str:
    raw = str(name or "").strip().lower()
    if raw.endswith("[]"):
        raw = raw[:-2]
    return raw.replace("-", "_")


def filter_args_present(source: Any) -> bool:
    """True if the mapping/MultiDict contains any recognizable filter keys."""
    keys = {_canonical_key(k) for k in _iter_keys(source)}
    return bool(keys & _FILTER_PARAM_NAMES)


def filter_capture_requested(source: Any) -> bool:
    """True if request explicitly signals capture (even if empty)."""
    keys = {_canonical_key(k) for k in _iter_keys(source)}
    return bool(keys & _FILTER_CONTROL_NAMES)


def _to_sequence(value: Any) -> Sequence[Any]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        return tuple(value)
    return (value,)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, pd.Timestamp):
        v = _coerce_tznaive(value)
        return v.isoformat()
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat()
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time()).isoformat()
    return str(value)


def _normalize_token(s: str) -> str:
    return s.strip()


def _first_value(source: Any, keys: Iterable[str]) -> Any:
    for key in keys:
        if hasattr(source, "getlist"):
            values = [v for v in source.getlist(key) if v not in (None, "")]
            if values:
                return values[0]
            bracket = f"{key}[]"
            values = [v for v in source.getlist(bracket) if v not in (None, "")]
            if values:
                return values[0]
        elif isinstance(source, Mapping):
            if key in source:
                val = source.get(key)
                if isinstance(val, (list, tuple, set)):
                    for item in val:
                        if item not in (None, ""):
                            return item
                elif val not in (None, ""):
                    return val
            bracket = f"{key}[]"
            if bracket in source:
                return source.get(bracket)
    return None


def _split_maybe_csv(raw: Any) -> list[str]:
    """
    Accepts single values, lists/tuples, or CSV strings and returns a cleaned list of strings.
    Handles objects like {"name": "West", "count": 10} by extracting the 'name' field.
    Also handles edge cases like numeric zeros, booleans, etc.
    """
    out: list[str] = []
    seq = _to_sequence(raw)
    for item in seq:
        if item in (None, ""):
            continue

        # Handle dict-like objects with 'name' or 'value' keys
        if isinstance(item, Mapping):
            text = None
            if "name" in item:
                text = str(item["name"]).strip() if item["name"] not in (None, "") else None
            elif "value" in item:
                text = str(item["value"]).strip() if item["value"] not in (None, "") else None
            elif "label" in item:
                text = str(item["label"]).strip() if item["label"] not in (None, "") else None

            # Skip dicts without extractable values
            if not text:
                continue
        else:
            text = _stringify(item)

        if not text:
            continue

        # Handle CSV strings  - but preserve values like "0" or other valid identifiers
        # Only split on comma if it looks like a CSV (has commas and multiple parts)
        if "," in text:
            parts = [p.strip() for p in text.split(",") if p.strip()]
            out.extend(parts)
        else:
            # Single value - keep as is (even if it's "0", "false", etc.)
            out.append(text)

    return out


def _collect_values(source: Any, keys: Iterable[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for key in keys:
        if hasattr(source, "getlist"):
            candidates = (source.getlist(key) or []) + (source.getlist(f"{key}[]") or [])
        elif isinstance(source, Mapping):
            items = []
            if key in source:
                items.extend(_to_sequence(source.get(key)))
            bracket = f"{key}[]"
            if bracket in source:
                items.extend(_to_sequence(source.get(bracket)))
            candidates = items
        else:
            candidates = ()

        for token in _split_maybe_csv(candidates):
            tok = _normalize_token(token)
            if tok and tok not in seen:
                seen.append(tok)
    return tuple(seen)


def _strip_all(values: Iterable[str] | None) -> tuple[str, ...]:
    """Remove 'all' sentinels; return empty tuple to mean All."""
    if not values:
        return tuple()
    cleaned = [v for v in values if str(v).strip().lower() not in _SENTINELS_ALL]
    return tuple(cleaned)


def _stable_unique_tokens(values: Iterable[Any] | None, *, lower: bool = False) -> tuple[str, ...]:
    if not values:
        return tuple()
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        token = str(raw).strip()
        if not token:
            continue
        cmp = token.lower() if lower else token
        if cmp in _SENTINELS_ALL:
            continue
        if cmp in seen:
            continue
        seen.add(cmp)
        out.append(cmp if lower else token)
    out.sort(key=lambda item: (item.lower(), item))
    return tuple(out)


def _parse_bool_flag(source: Any, keys: Iterable[str], default: bool = False) -> bool:
    raw = _first_value(source, keys)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    try:
        text = str(raw).strip().lower()
    except Exception:
        return default
    if not text:
        return default
    if text in {"0", "false", "no", "off"}:
        return False
    if text in {"1", "true", "yes", "on"}:
        return True
    return default


def _parse_date(value: Any) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    if isinstance(value, (list, tuple, set)):
        for item in value:
            parsed = _parse_date(item)
            if parsed is not None:
                return parsed
        return None
    try:
        ts = pd.to_datetime(value, errors="coerce")
    except Exception:
        return None
    if pd.isna(ts):
        return None
    return _coerce_tznaive(ts)


def _parse_float(source: Any, keys: Iterable[str]) -> float | None:
    raw = _first_value(source, keys)
    if raw in (None, ""):
        return None
    if isinstance(raw, (list, tuple, set)):
        for item in raw:
            try:
                return float(item)
            except Exception:
                continue
        return None
    try:
        return float(raw)
    except Exception:
        return None


def _is_fiscal_preset(preset: Any) -> bool:
    token = str(preset or "").strip().lower()
    return token in _FISCAL_PRESET_TOKENS


def _normalize_date_type(value: Any, *, preset: Any = None) -> str | None:
    token = str(value or "").strip().lower()
    if token in {"fiscal", "fy"}:
        return FISCAL_DATE_TYPE
    if token in {"calendar", "standard"}:
        return "calendar"
    if _is_fiscal_preset(preset):
        return FISCAL_DATE_TYPE
    return None


def _fiscal_year_start(now: pd.Timestamp) -> pd.Timestamp:
    year = now.year
    if (now.month, now.day) < (FISCAL_YEAR_START_MONTH, FISCAL_YEAR_START_DAY):
        year -= 1
    return pd.Timestamp(year=year, month=FISCAL_YEAR_START_MONTH, day=FISCAL_YEAR_START_DAY)


def _fiscal_quarter_start(now: pd.Timestamp) -> pd.Timestamp:
    fy_start = _fiscal_year_start(now)
    months_since_start = ((now.year - fy_start.year) * 12) + (now.month - fy_start.month)
    quarter_offset = max(0, months_since_start // 3) * 3
    return (fy_start + pd.DateOffset(months=quarter_offset)).normalize()


def _fiscal_month_start(now: pd.Timestamp) -> pd.Timestamp:
    fy_start = _fiscal_year_start(now)
    months_since_start = ((now.year - fy_start.year) * 12) + (now.month - fy_start.month)
    return (fy_start + pd.DateOffset(months=months_since_start)).normalize()


def _clamp_elapsed_period_end(
    period_start: pd.Timestamp,
    period_full_end: pd.Timestamp,
    elapsed_days: int,
) -> pd.Timestamp:
    return min(period_full_end, (period_start + pd.Timedelta(days=max(0, elapsed_days))).normalize())


def get_fiscal_periods(now: pd.Timestamp | None = None) -> dict[str, dict[str, pd.Timestamp]]:
    if now is None:
        try:
            now = pd.Timestamp.utcnow()
        except Exception:
            now = pd.Timestamp.today()
    today = _coerce_tznaive(now).normalize()
    current_fy_start = _fiscal_year_start(today)
    previous_fy_start = (current_fy_start - pd.DateOffset(years=1)).normalize()
    previous_fy_end = (current_fy_start - pd.Timedelta(days=1)).normalize()
    prior_fy_start = (previous_fy_start - pd.DateOffset(years=1)).normalize()
    prior_fy_end = (previous_fy_start - pd.Timedelta(days=1)).normalize()

    current_fq_start = _fiscal_quarter_start(today)
    previous_fq_start = (current_fq_start - pd.DateOffset(months=3)).normalize()
    previous_fq_full_end = (current_fq_start - pd.Timedelta(days=1)).normalize()
    prior_fq_start = (previous_fq_start - pd.DateOffset(months=3)).normalize()
    prior_fq_end = (previous_fq_start - pd.Timedelta(days=1)).normalize()

    current_fm_start = _fiscal_month_start(today)
    previous_fm_start = (current_fm_start - pd.DateOffset(months=1)).normalize()
    previous_fm_full_end = (current_fm_start - pd.Timedelta(days=1)).normalize()
    prior_fm_start = (previous_fm_start - pd.DateOffset(months=1)).normalize()
    prior_fm_end = (previous_fm_start - pd.Timedelta(days=1)).normalize()

    fy_elapsed_days = max(0, (today - current_fy_start).days)
    current_fq_elapsed_days = max(0, (today - current_fq_start).days)
    current_fm_elapsed_days = max(0, (today - current_fm_start).days)
    previous_fy_ytd_end = _clamp_elapsed_period_end(previous_fy_start, previous_fy_end, fy_elapsed_days)
    previous_fq_qtd_end = _clamp_elapsed_period_end(previous_fq_start, previous_fq_full_end, current_fq_elapsed_days)
    previous_fm_mtd_end = _clamp_elapsed_period_end(previous_fm_start, previous_fm_full_end, current_fm_elapsed_days)

    return {
        "current_fy": {
            "start": current_fy_start,
            "end": today,
            "comparison_start": previous_fy_start,
            "comparison_end": previous_fy_ytd_end,
            "yoy_start": previous_fy_start,
            "yoy_end": previous_fy_end, # Full Fiscal Year
        },
        "previous_fy": {
            "start": previous_fy_start,
            "end": previous_fy_end,
            "comparison_start": prior_fy_start,
            "comparison_end": prior_fy_end,
            "yoy_start": prior_fy_start,
            "yoy_end": prior_fy_end,
        },
        "current_fq": {
            "start": current_fq_start,
            "end": today,
            "comparison_start": previous_fq_start,
            "comparison_end": previous_fq_qtd_end,
            "yoy_start": (current_fq_start - pd.DateOffset(years=1)).normalize(),
            "yoy_end": (previous_fq_full_end - pd.DateOffset(years=1) + pd.DateOffset(months=3)).normalize(), # Full Fiscal Quarter
        },
        "previous_fq": {
            "start": previous_fq_start,
            "end": previous_fq_full_end,
            "comparison_start": prior_fq_start,
            "comparison_end": prior_fq_end,
            "yoy_start": (previous_fq_start - pd.DateOffset(years=1)).normalize(),
            "yoy_end": (previous_fq_full_end - pd.DateOffset(years=1)).normalize(),
        },
        "current_fm": {
            "start": current_fm_start,
            "end": today,
            "comparison_start": previous_fm_start,
            "comparison_end": previous_fm_mtd_end,
            "yoy_start": (current_fm_start - pd.DateOffset(years=1)).normalize(),
            "yoy_end": (previous_fm_full_end - pd.DateOffset(years=1) + pd.DateOffset(months=1)).normalize(), # Full Fiscal Month
        },
        "previous_fm": {
            "start": previous_fm_start,
            "end": previous_fm_full_end,
            "comparison_start": prior_fm_start,
            "comparison_end": prior_fm_end,
            "yoy_start": (previous_fm_start - pd.DateOffset(years=1)).normalize(),
            "yoy_end": (previous_fm_full_end - pd.DateOffset(years=1)).normalize(),
        },
        "fytd_comparison": {
            "start": current_fy_start,
            "end": today,
            "comparison_start": previous_fy_start,
            "comparison_end": previous_fy_ytd_end,
            "yoy_start": previous_fy_start,
            "yoy_end": previous_fy_end,
        },
    }


def fiscal_month_index(value: Any) -> int | None:
    ts = _parse_date(value)
    if ts is None:
        return None
    fy_start = _fiscal_year_start(ts.normalize())
    return ((ts.year - fy_start.year) * 12) + (ts.month - fy_start.month) + 1


def fiscal_month_label(value: Any) -> str | None:
    idx = fiscal_month_index(value)
    if idx is None:
        return None
    return f"FM{idx}"


def _preset_to_range(
    preset: str | None,
    now: pd.Timestamp | None = None,
    *,
    date_type: str | None = None,
) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    """Return (start, end) for a given date preset token."""
    if not preset:
        return None, None
    token = str(preset).strip().lower()
    if not token:
        return None, None
    if now is None:
        try:
            now = pd.Timestamp.utcnow()
        except Exception:
            now = pd.Timestamp.today()
    now = _coerce_tznaive(now).normalize()

    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None
    normalized_date_type = _normalize_date_type(date_type, preset=token)
    if _is_fiscal_preset(token) or normalized_date_type == FISCAL_DATE_TYPE:
        fiscal_periods = get_fiscal_periods(now)
        fiscal_period = fiscal_periods.get(token)
        if fiscal_period:
            start = fiscal_period.get("start")
            end = fiscal_period.get("end")
            if start is not None and end is not None:
                try:
                    if start < _MIN_DEFAULT_START:
                        start = _MIN_DEFAULT_START
                except Exception:
                    pass
            return start, end

    if token in {"today"}:
        start = now
        end = now
    elif token in {"yesterday"}:
        start = now - pd.Timedelta(days=1)
        end = start
    elif token in {"7d", "7_days", "last7days", "last_7_days"}:
        start = now - pd.Timedelta(days=6)
        end = now
    elif token in {"30d", "last_30_days"}:
        start = now - pd.Timedelta(days=29)
        end = now
    elif token in {"90d", "last_90_days", "last_3_months"}:
        start = (now - pd.DateOffset(months=3)).normalize().replace(day=1)
        end = now
    elif token in {"mtd", "month_to_date"}:
        start = now.replace(day=1)
        end = now
    elif token in {"qtd", "quarter_to_date"}:
        q_start_month = (now.month - 1) // 3 * 3 + 1
        start = pd.Timestamp(year=now.year, month=q_start_month, day=1)
        end = now
    elif token in {"ytd", "year_to_date"}:
        start = pd.Timestamp(year=now.year, month=1, day=1)
        end = now
    elif token in {"last-month", "last_month"}:
        prev_month = now - pd.DateOffset(months=1)
        start = prev_month.replace(day=1)
        end = (start + pd.DateOffset(months=1) - pd.Timedelta(days=1)).normalize()
    elif token in {"last-quarter", "last_quarter"}:
        q_start_month = (now.month - 1) // 3 * 3 + 1
        this_q_start = pd.Timestamp(year=now.year, month=q_start_month, day=1)
        prev_q_end = this_q_start - pd.Timedelta(days=1)
        prev_q_start_month = (prev_q_end.month - 1) // 3 * 3 + 1
        start = pd.Timestamp(year=prev_q_end.year, month=prev_q_start_month, day=1)
        end = (start + pd.DateOffset(months=3) - pd.Timedelta(days=1)).normalize()
    elif token in {"custom"}:
        return None, None
    elif token == "fy2025":
        start = pd.Timestamp(year=2024, month=4, day=1)
        end = pd.Timestamp(year=2025, month=3, day=31)
    elif token == "fy2024":
        start = pd.Timestamp(year=2023, month=4, day=1)
        end = pd.Timestamp(year=2024, month=3, day=31)
    elif token in {"all", "all_time", "__all__", "*"}:
        start, end = None, None
    else:
        return None, None

    try:
        if start is not None and start < _MIN_DEFAULT_START:
            start = _MIN_DEFAULT_START
    except Exception:
        pass

    return start, end

# ─────────────────────────────────────────────────────────────────────────────
# Public parsing API
# ─────────────────────────────────────────────────────────────────────────────
def parse_filters(args: Any) -> FilterParams:
    """
    Normalize query/JSON/container input into FilterParams.
    Empty/missing lists mean "All".

    Production-ready: handles all edge cases including:
    - Empty arrays vs missing values
    - Object-based filter values {name, value, label}
    - CSV strings
    - Numeric zeros and other edge case values
    - Date validation and swapping
    """
    if args is None:
        args = {}
    if isinstance(args, FilterParams):
        # Avoid needless re-parsing if already normalized
        return normalize_filters(args)

    start = _parse_date(_first_value(args, ("start", "start_date", "startDate", "date_start")))
    end = _parse_date(_first_value(args, ("end", "end_date", "endDate", "date_end")))
    preset_token = _first_value(args, ("date_preset", "preset", "range_preset"))
    preset = str(preset_token).strip().lower() if preset_token not in (None, "") else None
    date_type = _normalize_date_type(_first_value(args, ("date_type", "dateType")), preset=preset)

    # Collect and normalize all filter values
    # Empty tuples = "All", populated tuples = specific filters
    statuses   = _strip_all(_collect_values(args, ("statuses", "status", "order_status", "order_statuses")))
    regions    = _strip_all(_collect_values(args, ("regions", "region", "region_ids", "region_id", "regions[]")))
    methods    = _strip_all(_collect_values(args, ("methods", "shipping_methods", "shippingMethods", "shipping_method", "ship_method_ids", "ship_method_id")))
    customers  = _strip_all(_collect_values(args, ("customers", "customer_ids", "customer", "customerId")))
    suppliers  = _strip_all(_collect_values(args, ("suppliers", "supplier_ids", "supplier", "supplierId")))
    products   = _strip_all(_collect_values(args, ("products", "product_ids", "product", "productId")))
    sales_reps = _strip_all(_collect_values(args, (
        "sales_reps", "sales_rep_ids",
        "salesreps", "salesrep_ids",
    )))
    protein_groups = _strip_all(_collect_values(args, ("protein_groups", "protein_group", "meat_type", "species")))

    complete_months_only = _parse_bool_flag(args, ("complete_months_only", "completeMonthsOnly", "full_months_only"), False)
    protein_min = _parse_float(args, ("protein_min", "proteinMin"))
    protein_max = _parse_float(args, ("protein_max", "proteinMax"))
    yield_min = _parse_float(args, ("yield_min", "yieldMin"))
    yield_max = _parse_float(args, ("yield_max", "yieldMax"))

    protein_name_like = _first_value(args, ("protein_name", "proteinName", "protein_name_like"))
    if isinstance(protein_name_like, (list, tuple, set)):
        protein_name_like = next((str(v).strip() for v in protein_name_like if str(v).strip()), "") or None
    protein_name_like = (str(protein_name_like).strip() if protein_name_like else None)

    has_entity_filters = any([
        regions, methods, customers, suppliers, products, sales_reps,
    ])

    # Compute effective date window
    applied_default = False
    if start is None and end is None:
        if preset in {"all", "__all__", "*"}:
            start, end = None, None
        else:
            effective_preset = preset or DEFAULT_DATE_PRESET
            date_type = _normalize_date_type(date_type, preset=effective_preset)
            start, end = _preset_to_range(effective_preset, date_type=date_type)
            if start is None and end is None:
                start, end = _default_filter_window()
            preset = effective_preset
            applied_default = True
    elif preset in {"all", "__all__", "*"}:
        # Explicit all-time overrides start/end
        start, end = None, None
    else:
        # start/end provided -> preset is informational only
        preset = preset or None
    date_type = _normalize_date_type(date_type, preset=preset)

    if start is not None and end is not None and start > end:
        start, end = end, start

    # Safety clamp for auto-applied windows to avoid 2018+ default blasts
    if applied_default and start is not None and start < _MIN_DEFAULT_START:
        start = _MIN_DEFAULT_START

    return FilterParams(
        start=start,
        end=end,
        regions=regions,
        methods=methods,
        customers=customers,
        suppliers=suppliers,
        products=products,
        sales_reps=sales_reps,
        protein_groups=protein_groups,
        yield_min=yield_min,
        yield_max=yield_max,
        statuses=statuses,
        preset=preset,
        date_type=date_type,
        protein_min=protein_min,
        protein_max=protein_max,
        protein_name_like=protein_name_like,
        complete_months_only=complete_months_only,
    )


def normalize_filters(filters: Any) -> FilterParams:
    """
    Coerce a mapping/FilterParams into a fully normalized FilterParams:
    - blank strings -> None
    - default preset -> current_fy when no dates provided
    - enforces start <= end and minimum default start date
    """
    if isinstance(filters, FilterParams):
        candidate = filters
    else:
        candidate = parse_filters(filters or {})

    preset = getattr(candidate, "preset", None)
    preset = str(preset).strip().lower() if preset not in (None, "") else None
    date_type = _normalize_date_type(getattr(candidate, "date_type", None), preset=preset)

    start = _parse_date(getattr(candidate, "start", None))
    end = _parse_date(getattr(candidate, "end", None))

    statuses = _stable_unique_tokens(_strip_all(tuple(getattr(candidate, "statuses", ()) or ())), lower=True)
    regions = _stable_unique_tokens(_strip_all(tuple(getattr(candidate, "regions", ()) or ())))
    methods = _stable_unique_tokens(_strip_all(tuple(getattr(candidate, "methods", ()) or ())))
    customers = _stable_unique_tokens(_strip_all(tuple(getattr(candidate, "customers", ()) or ())))
    suppliers = _stable_unique_tokens(_strip_all(tuple(getattr(candidate, "suppliers", ()) or ())))
    products = _stable_unique_tokens(_strip_all(tuple(getattr(candidate, "products", ()) or ())))
    sales_reps = _stable_unique_tokens(_strip_all(tuple(getattr(candidate, "sales_reps", ()) or ())))
    protein_groups = _stable_unique_tokens(_strip_all(tuple(getattr(candidate, "protein_groups", ()) or ())))

    protein_min = getattr(candidate, "protein_min", None)
    protein_max = getattr(candidate, "protein_max", None)
    yield_min = getattr(candidate, "yield_min", None)
    yield_max = getattr(candidate, "yield_max", None)
    try:
        protein_min = float(protein_min) if protein_min not in (None, "") else None
    except Exception:
        protein_min = None
    try:
        protein_max = float(protein_max) if protein_max not in (None, "") else None
    except Exception:
        protein_max = None
    try:
        yield_min = float(yield_min) if yield_min not in (None, "") else None
    except Exception:
        yield_min = None
    try:
        yield_max = float(yield_max) if yield_max not in (None, "") else None
    except Exception:
        yield_max = None
    protein_name_like = getattr(candidate, "protein_name_like", None)
    if protein_name_like:
        protein_name_like = (str(protein_name_like).strip() or None)

    applied_default = False
    if start is None and end is None:
        if preset in {"all", "__all__", "*"}:
            start, end = None, None
        else:
            preset = preset or DEFAULT_DATE_PRESET
            date_type = _normalize_date_type(date_type, preset=preset)
            start, end = _preset_to_range(preset, date_type=date_type)
            if start is None and end is None:
                start, end = _default_filter_window()
            applied_default = True
    elif preset in {"all", "__all__", "*"}:
        start, end = None, None
    date_type = _normalize_date_type(date_type, preset=preset)
    if start is not None and end is not None and start > end:
        start, end = end, start
    if applied_default and start is not None and start < _MIN_DEFAULT_START:
        start = _MIN_DEFAULT_START

    return FilterParams(
        start=start,
        end=end,
        regions=regions,
        methods=methods,
        customers=customers,
        suppliers=suppliers,
        products=products,
        sales_reps=sales_reps,
        protein_groups=protein_groups,
        yield_min=yield_min,
        yield_max=yield_max,
        statuses=statuses,
        preset=preset,
        date_type=date_type,
        protein_min=protein_min,
        protein_max=protein_max,
        protein_name_like=protein_name_like,
        complete_months_only=bool(getattr(candidate, "complete_months_only", True)),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Serialization helpers
# ─────────────────────────────────────────────────────────────────────────────
def _ts_to_string(ts: pd.Timestamp | None) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, pd.Timestamp):
        val = _coerce_tznaive(ts)
        try:
            return val.date().isoformat()
        except Exception:
            return val.isoformat()
    if isinstance(ts, datetime):
        return ts.replace(tzinfo=None).date().isoformat()
    if isinstance(ts, date):
        return ts.isoformat()
    return str(ts)


def filters_to_store(filters: FilterParams) -> dict[str, Any]:
    """
    Convert FilterParams into a plain dict suitable for session/json storage.
    (We keep arrays compact; empty arrays remain empty == All)
    """
    def _list(values: Iterable[Any]) -> list[str]:
        return [str(v) for v in values] if values else []

    return {
        "start_date": _ts_to_string(filters.start),
        "end_date": _ts_to_string(filters.end),
        "date_preset": getattr(filters, "preset", None),
        "date_type": getattr(filters, "date_type", None),
        "statuses": _list(getattr(filters, "statuses", tuple())),
        "regions": _list(filters.regions),
        "shipping_methods": _list(filters.methods),
        "customers": _list(filters.customers),
        "suppliers": _list(getattr(filters, "suppliers", tuple())),
        "products": _list(getattr(filters, "products", tuple())),
        "sales_reps": _list(getattr(filters, "sales_reps", tuple())),
        "protein_groups": _list(getattr(filters, "protein_groups", tuple())),
        "yield_min": filters.yield_min,
        "yield_max": filters.yield_max,
        "protein_min": filters.protein_min,
        "protein_max": filters.protein_max,
        "protein_name_like": filters.protein_name_like,
        "complete_months_only": bool(getattr(filters, "complete_months_only", True)),
    }


def capture_filters_from(source: Any) -> dict[str, Any] | None:
    """
    If `source` (request args/form/json) contains filter parameters or explicitly
    requests capture, return a normalized storage dict; otherwise None.
    """
    has_filters = filter_args_present(source)
    forced = filter_capture_requested(source)
    if not has_filters and not forced:
        return None
    params = parse_filters(source)
    return sanitize_filters_to_store(params)


def _as_session_user_id(user_id: Any) -> str | None:
    if user_id in (None, ""):
        return None
    sval = str(user_id).strip()
    return sval or None


def _compact_stored_filters(payload: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(payload or {})
    list_keys = (
        "regions",
        "shipping_methods",
        "customers",
        "suppliers",
        "products",
        "sales_reps",
        "statuses",
        "protein_groups",
    )
    for key in list_keys:
        raw = out.get(key)
        vals = raw if isinstance(raw, (list, tuple, set)) else []
        compact: list[str] = []
        seen: set[str] = set()
        for item in vals:
            tok = str(item).strip()
            if not tok or tok in seen:
                continue
            seen.add(tok)
            compact.append(tok)
            if len(compact) >= _STICKY_FILTERS_MAX_ITEMS:
                break
        out[key] = compact
    return out


def _current_scope_payload(scope: Any = None, *, user: Any = None) -> dict[str, Any]:
    if isinstance(scope, Mapping):
        return dict(scope)
    try:
        from app.core.access_policy import AccessScope  # type: ignore

        if isinstance(scope, AccessScope):
            return scope.as_dict(include_allowed=True)
    except Exception:
        pass
    try:
        from app.core import access_policy  # type: ignore

        if user is not None:
            return access_policy.scope_for_user(user, use_cache=True).as_dict(include_allowed=True)
        return access_policy.get_current_scope(use_cache=True).as_dict(include_allowed=True)
    except Exception:
        return {}


def _allowlist_from_options(items: Iterable[Any] | None) -> dict[str, str]:
    allowed: dict[str, str] = {}
    if items is None:
        return allowed
    for item in items:
        raw = None
        if isinstance(item, Mapping):
            raw = item.get("id") or item.get("value") or item.get("label") or item.get("name")
        else:
            raw = item
        if raw in (None, ""):
            continue
        token = str(raw).strip()
        if not token:
            continue
        allowed[token.lower()] = token
    return allowed


def _scope_allowlists(scope_payload: Mapping[str, Any] | None, *, use_cache: bool = True) -> dict[str, dict[str, str]]:
    allowlists: dict[str, dict[str, str]] = {}
    scope_dict = dict(scope_payload or {})

    if _OPTIONS_ALLOWLIST_SANITIZE:
        from app.services import filters_service as _filters_service  # type: ignore

        try:
            options_payload = _filters_service.load_filter_options({"preset": "all"}, scope_dict, use_cache=use_cache)
            raw_options = options_payload.get("options") if isinstance(options_payload, Mapping) else {}
            if isinstance(raw_options, Mapping):
                for key, aliases in _OPTION_BUCKET_ALIASES.items():
                    bucket_items = None
                    for alias in aliases:
                        candidate = raw_options.get(alias)
                        if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
                            bucket_items = candidate
                            break
                    if bucket_items is not None:
                        allowlists[key] = _allowlist_from_options(bucket_items)
        except Exception:
            allowlists = {}

    for key, primary, legacy in _SCOPE_DIMENSION_KEYS:
        if key in allowlists:
            continue
        raw_values = scope_dict.get(primary) or scope_dict.get(legacy)
        if not raw_values:
            continue
        tokens = _stable_unique_tokens(raw_values)
        if tokens:
            allowlists[key] = {str(token).strip().lower(): str(token).strip() for token in tokens if str(token).strip()}

    if "statuses" not in allowlists:
        default_statuses = [
            s.strip().lower()
            for s in (os.getenv("ORDER_STATUSES") or "").split(",")
            if s.strip()
        ] or ["packed", "invoiced", "shipped", "delivered"]
        allowlists["statuses"] = {token: token for token in default_statuses}

    return allowlists


def sanitize_filters(
    filters: Any,
    scope: Any = None,
    *,
    user: Any = None,
    include_meta: bool = False,
    use_cache: bool = True,
) -> Any:
    params = normalize_filters(filters)
    selected_dimension_values = any(
        bool(getattr(params, attr, ()) or ())
        for attr in ("statuses", "regions", "methods", "customers", "suppliers", "products", "sales_reps")
    )
    scope_payload = _current_scope_payload(scope, user=user) if selected_dimension_values else {}
    allowlists = _scope_allowlists(scope_payload, use_cache=use_cache) if selected_dimension_values else {}
    dropped: dict[str, list[str]] = {}

    def _sanitize_values(bucket: str, values: Iterable[str] | None, *, lower: bool = False) -> tuple[str, ...]:
        current = tuple(values or ())
        if not current:
            return tuple()
        allowlist = allowlists.get(bucket)
        if allowlist is None:
            return _stable_unique_tokens(current, lower=lower)

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
        kept.sort(key=lambda item: (item.lower(), item))
        return tuple(kept)

    sanitized = FilterParams(
        start=params.start,
        end=params.end,
        statuses=_sanitize_values("statuses", getattr(params, "statuses", ()), lower=True),
        regions=_sanitize_values("regions", getattr(params, "regions", ())),
        methods=_sanitize_values("methods", getattr(params, "methods", ())),
        customers=_sanitize_values("customers", getattr(params, "customers", ())),
        suppliers=_sanitize_values("suppliers", getattr(params, "suppliers", ())),
        products=_sanitize_values("products", getattr(params, "products", ())),
        sales_reps=_sanitize_values("sales_reps", getattr(params, "sales_reps", ())),
        protein_groups=_sanitize_values("protein_groups", getattr(params, "protein_groups", ())),
        yield_min=params.yield_min,
        yield_max=params.yield_max,
        preset=params.preset,
        date_type=getattr(params, "date_type", None),
        protein_min=params.protein_min,
        protein_max=params.protein_max,
        protein_name_like=params.protein_name_like,
        complete_months_only=params.complete_months_only,
    )
    meta = {
        "sanitized": bool(dropped),
        "dropped": {key: list(values) for key, values in dropped.items()},
        "scope_hash": scope_payload.get("scope_hash") if isinstance(scope_payload, Mapping) else None,
        "notice": (
            "Some filters were removed because they are no longer available in your current access scope."
            if dropped else None
        ),
    }
    if include_meta:
        return sanitized, meta
    return sanitized


def sanitize_filters_to_store(
    filters: Any,
    scope: Any = None,
    *,
    user: Any = None,
    include_meta: bool = False,
    use_cache: bool = True,
) -> Any:
    params, meta = sanitize_filters(filters, scope, user=user, include_meta=True, use_cache=use_cache)
    stored = _compact_stored_filters(filters_to_store(params))
    if include_meta:
        return stored, meta
    return stored


def read_sticky_filters_from_session(session_obj: Mapping[str, Any], user_id: Any = None) -> dict[str, Any] | None:
    """
    Return canonical sticky filters for the current user from session storage.
    Supports both the new versioned key and legacy keys as fallback.
    """
    try:
        raw = session_obj.get(STICKY_FILTERS_SESSION_KEY)
    except Exception:
        raw = None
    uid = _as_session_user_id(user_id)

    payload: Mapping[str, Any] | None = None
    record_version = 0
    if isinstance(raw, Mapping):
        try:
            record_version = int(raw.get("v") or 0)
        except Exception:
            record_version = 0
        owner = _as_session_user_id(raw.get("user_id"))
        if uid and owner and owner != uid:
            return None
        candidate = raw.get("filters")
        if isinstance(candidate, Mapping):
            payload = candidate
        elif "start_date" in raw or "regions" in raw or "customers" in raw:
            payload = raw

    if payload is None:
        for key in _LEGACY_STICKY_KEYS:
            try:
                legacy = session_obj.get(key)
            except Exception:
                legacy = None
            if isinstance(legacy, Mapping):
                payload = legacy
                record_version = 0
                break

    if not isinstance(payload, Mapping):
        return None
    payload_for_parse: Mapping[str, Any] = payload
    if record_version < 2:
        # v1 payloads could accidentally persist drilldown entity rep IDs as global sales_reps.
        # Drop rep-scoped keys once during migration to avoid "single rep everywhere" leakage.
        migrated = dict(payload)
        migrated.pop("sales_reps", None)
        migrated.pop("sales_rep_id", None)
        migrated.pop("salesrep_id", None)
        payload_for_parse = migrated

    stored, _meta = sanitize_filters_to_store(payload_for_parse, include_meta=True)
    return stored


def write_sticky_filters_to_session(session_obj: Any, payload: Any, user_id: Any = None) -> dict[str, Any]:
    """
    Persist sticky filters in a versioned session key and keep legacy aliases synced.
    Returns the canonical stored payload.
    """
    stored_filters, _meta = sanitize_filters_to_store(payload, include_meta=True)
    owner = _as_session_user_id(user_id)
    try:
        existing = session_obj.get(STICKY_FILTERS_SESSION_KEY)
    except Exception:
        existing = None
    if (
        isinstance(existing, Mapping)
        and _as_session_user_id(existing.get("user_id")) == owner
        and isinstance(existing.get("filters"), Mapping)
        and dict(existing.get("filters") or {}) == stored_filters
    ):
        try:
            for key in _LEGACY_STICKY_KEYS:
                session_obj[key] = stored_filters
        except Exception:
            pass
        return stored_filters
    record = {
        "v": _STICKY_FILTERS_REV,
        "user_id": owner,
        "filters": stored_filters,
        "updated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
    }
    try:
        session_obj[STICKY_FILTERS_SESSION_KEY] = record
        for key in _LEGACY_STICKY_KEYS:
            session_obj[key] = stored_filters
    except Exception:
        pass
    return stored_filters


def clear_sticky_filters_in_session(session_obj: Any) -> None:
    try:
        session_obj.pop(STICKY_FILTERS_SESSION_KEY, None)
        for key in _LEGACY_STICKY_KEYS:
            session_obj.pop(key, None)
    except Exception:
        pass


def canonical_filters_payload(filters: Any) -> dict[str, Any]:
    params = normalize_filters(filters)
    payload = filters_to_store(params)
    for key in ("statuses", "regions", "shipping_methods", "customers", "suppliers", "products", "sales_reps"):
        payload[key] = sorted({str(v).strip() for v in (payload.get(key) or []) if str(v).strip()})
    payload["complete_months_only"] = bool(payload.get("complete_months_only", True))
    return payload


def canonical_filters_json(filters: Any) -> str:
    payload = canonical_filters_payload(filters)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def canonical_filters_hash(filters: Any) -> str:
    try:
        return hashlib.sha256(canonical_filters_json(filters).encode("utf-8")).hexdigest()
    except Exception:
        return ""


_PRESET_LABELS = {
    "today": "Today",
    "yesterday": "Yesterday",
    "7d": "Last 7 Days",
    "7_days": "Last 7 Days",
    "last7days": "Last 7 Days",
    "last_7_days": "Last 7 Days",
    "30d": "Last 30 Days",
    "last_30_days": "Last 30 Days",
    "90d": "Last 90 Days",
    "last_90_days": "Last 90 Days",
    "last_3_months": "Last 90 Days",
    "current_fy": "Current FY",
    "previous_fy": "Previous FY",
    "current_fq": "Current FQ",
    "previous_fq": "Previous FQ",
    "current_fm": "Current FM",
    "previous_fm": "Previous FM",
    "fytd_comparison": "FYTD Comparison",
    "fy2025": "FY 2025 (Apr-Mar)",
    "fy2024": "FY 2024 (Apr-Mar)",
    "mtd": "Month to Date",
    "month_to_date": "Month to Date",
    "qtd": "Quarter to Date",
    "quarter_to_date": "Quarter to Date",
    "ytd": "Year to Date",
    "year_to_date": "Year to Date",
    "last-month": "Last Month",
    "last_month": "Last Month",
    "last-quarter": "Last Quarter",
    "last_quarter": "Last Quarter",
    "custom": "Custom",
    "all": "All Time",
    "all_time": "All Time",
    "__all__": "All Time",
    "*": "All Time",
}

_SUMMARY_DIMENSIONS: tuple[tuple[str, str, str], ...] = (
    ("statuses", "Status", "statuses"),
    ("regions", "Region", "regions"),
    ("methods", "Shipping Method", "shipping_methods"),
    ("customers", "Customer", "customers"),
    ("sales_reps", "Sales Rep", "sales_reps"),
    ("suppliers", "Supplier", "suppliers"),
    ("products", "Product", "products"),
)


def mark_filters_last_applied(session_obj: Any) -> str:
    stamp = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    try:
        session_obj[FILTERS_LAST_APPLIED_SESSION_KEY] = stamp
    except Exception:
        pass
    return stamp


def humanize_date_preset(preset: Any) -> str | None:
    token = str(preset or "").strip().lower()
    if not token:
        return None
    if token in _PRESET_LABELS:
        return _PRESET_LABELS[token]
    return token.replace("_", " ").replace("-", " ").title()


def format_filter_date_label(start: Any, end: Any, preset: Any = None) -> str:
    preset_label = humanize_date_preset(preset)
    if preset_label and str(preset or "").strip().lower() != "custom":
        return preset_label

    start_s = _ts_to_string(_parse_date(start))
    end_s = _ts_to_string(_parse_date(end))
    if start_s and end_s:
        return f"{start_s} to {end_s}"
    if start_s:
        return f"Since {start_s}"
    if end_s:
        return f"Through {end_s}"
    return preset_label or "All Time"


def summarize_filter_values(values: Iterable[Any] | None, *, max_items: int = 2) -> str:
    tokens = [str(v).strip() for v in (values or ()) if str(v).strip()]
    if not tokens:
        return "All"
    if len(tokens) == 1:
        return tokens[0]
    shown = tokens[: max(1, int(max_items))]
    if len(tokens) <= len(shown):
        return ", ".join(shown)
    return f"{', '.join(shown)} +{len(tokens) - len(shown)}"


def build_filter_summary(filters: Any, *, max_items: int = 2) -> dict[str, Any]:
    params = normalize_filters(filters)
    payload = filters_to_store(params)
    date_label = format_filter_date_label(params.start, params.end, params.preset)
    preset_label = humanize_date_preset(params.preset)
    dimension_chips: list[dict[str, Any]] = []

    for attr, label, key in _SUMMARY_DIMENSIONS:
        values = tuple(getattr(params, attr, ()) or ())
        if not values:
            continue
        dimension_chips.append(
            {
                "key": key,
                "label": label,
                "count": len(values),
                "summary": summarize_filter_values(values, max_items=max_items),
                "values": [str(v) for v in values],
            }
        )

    protein_group_values = tuple(getattr(params, "protein_groups", ()) or ())
    if protein_group_values:
        dimension_chips.append(
            {
                "key": "protein_groups",
                "label": "Protein Group",
                "count": len(protein_group_values),
                "summary": summarize_filter_values(protein_group_values, max_items=max_items),
                "values": [str(v) for v in protein_group_values],
            }
        )

    yield_bounds: list[str] = []
    if params.yield_min is not None:
        yield_bounds.append(f">= {params.yield_min:g}%")
    if params.yield_max is not None:
        yield_bounds.append(f"<= {params.yield_max:g}%")
    if yield_bounds:
        dimension_chips.append(
            {
                "key": "yield_range",
                "label": "Yield",
                "count": len(yield_bounds),
                "summary": " ".join(yield_bounds),
                "values": yield_bounds,
            }
        )

    protein_bounds: list[str] = []
    if params.protein_min is not None:
        protein_bounds.append(f">= {params.protein_min:g}")
    if params.protein_max is not None:
        protein_bounds.append(f"<= {params.protein_max:g}")
    if protein_bounds:
        dimension_chips.append(
            {
                "key": "protein_range",
                "label": "Protein",
                "count": len(protein_bounds),
                "summary": " ".join(protein_bounds),
                "values": protein_bounds,
            }
        )
    if params.protein_name_like:
        dimension_chips.append(
            {
                "key": "protein_name_like",
                "label": "Protein Name",
                "count": 1,
                "summary": str(params.protein_name_like),
                "values": [str(params.protein_name_like)],
            }
        )
    if bool(getattr(params, "complete_months_only", False)):
        dimension_chips.append(
            {
                "key": "complete_months_only",
                "label": "Month Window",
                "count": 1,
                "summary": "Full months only",
                "values": ["full_months_only"],
            }
        )

    chips = [
        {
            "key": "date",
            "label": "Date",
            "count": 1 if date_label else 0,
            "summary": date_label,
            "values": [date_label] if date_label else [],
            "preset": params.preset,
        }
    ] + dimension_chips

    compact = [date_label] if date_label else []
    compact.extend(f"{chip['label']}: {chip['summary']}" for chip in dimension_chips[:2])
    if len(dimension_chips) > 2:
        compact.append(f"+{len(dimension_chips) - 2} more")

    return {
        "date_label": date_label,
        "preset_label": preset_label,
        "chips": chips,
        "dimension_chips": dimension_chips,
        "active_count": len(dimension_chips) + (1 if date_label else 0),
        "dimension_count": len(dimension_chips),
        "compact_label": " • ".join(part for part in compact if part),
        "payload": payload,
    }


def serialize_saved_view(view: Any, *, active_id: Any = None) -> dict[str, Any]:
    raw_filters: Any = {}
    try:
        raw_filters = json.loads(getattr(view, "filters_json", "{}") or "{}")
    except Exception:
        raw_filters = {}

    safe_filters, sanitize_meta = sanitize_filters(raw_filters, include_meta=True)
    summary = build_filter_summary(safe_filters)
    view_id = getattr(view, "id", None)
    created_at = getattr(view, "created_at", None)
    created_iso = None
    if isinstance(created_at, datetime):
        try:
            created_iso = created_at.isoformat()
        except Exception:
            created_iso = str(created_at)
    elif created_at is not None:
        created_iso = str(created_at)

    return {
        "id": int(view_id) if view_id is not None else None,
        "name": str(getattr(view, "name", "") or "").strip() or "Untitled view",
        "user_id": getattr(view, "user_id", None),
        "filters": canonical_filters_payload(safe_filters),
        "filters_hash": canonical_filters_hash(safe_filters),
        "summary": summary,
        "created_at": created_iso,
        "active": str(view_id) == str(active_id) if active_id not in (None, "") else False,
        "visibility": "private",
        "sanitized": bool(sanitize_meta.get("sanitized")),
        "notice": sanitize_meta.get("notice"),
    }


def _debug_filters_logging_enabled() -> bool:
    return str(
        os.getenv("DEBUG_FILTERS")
        or os.getenv("DEBUG")
        or ""
    ).strip().lower() in {"1", "true", "yes", "on"}


def _bind_effective_filters_meta(meta: Mapping[str, Any]) -> None:
    try:
        from flask import g, has_request_context

        if has_request_context():
            existing = getattr(g, "effective_filters_meta", None)
            if isinstance(existing, Mapping) and existing:
                return
            g.effective_filters_meta = dict(meta or {})
    except Exception:
        pass


def bind_filter_cache_key(cache_key: str | None) -> None:
    try:
        from flask import g, has_request_context

        if not has_request_context():
            return
        g.filter_cache_key = cache_key
        g.filter_cache_key_hash = hashlib.sha256((cache_key or "").encode("utf-8")).hexdigest()[:16] if cache_key else None
    except Exception:
        pass


def _request_cache_bucket(name: str) -> dict[str, Any] | None:
    try:
        from flask import g, has_request_context

        if not has_request_context():
            return None
        bucket = getattr(g, name, None)
        if isinstance(bucket, dict):
            return bucket
        bucket = {}
        setattr(g, name, bucket)
        return bucket
    except Exception:
        return None


def _cacheable_payload(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "lists"):
        try:
            return {
                str(key): [_cacheable_payload(item) for item in items]
                for key, items in sorted(value.lists(), key=lambda item: str(item[0]))
            }
        except Exception:
            pass
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key in sorted(value.keys(), key=str):
            try:
                item = value.getlist(key) if hasattr(value, "getlist") else value[key]
            except Exception:
                item = value.get(key)
            normalized[str(key)] = _cacheable_payload(item)
        return normalized
    if isinstance(value, (list, tuple, set)):
        return [_cacheable_payload(item) for item in value]
    return str(value)


def _resolve_filters_request_cache_key(
    source_payload: Any,
    *,
    session_obj: Mapping[str, Any] | None,
    user_id: Any,
    sticky_enabled: bool,
    update_session: bool,
    request_obj: Any,
) -> str | None:
    try:
        session_filters = None
        if isinstance(session_obj, Mapping):
            session_filters = session_obj.get(STICKY_FILTERS_SESSION_KEY) or {}
        key_payload = {
            "source": _cacheable_payload(source_payload),
            "session_filters": _cacheable_payload(session_filters),
            "user_id": str(user_id) if user_id is not None else None,
            "sticky_enabled": bool(sticky_enabled),
            "update_session": bool(update_session),
            "endpoint": getattr(request_obj, "endpoint", None),
            "path": getattr(request_obj, "path", None),
            "method": getattr(request_obj, "method", None),
        }
        return hashlib.sha256(
            json.dumps(key_payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()
    except Exception:
        return None


def _resolve_filters_from_source(
    source: Any,
    *,
    session_obj: Mapping[str, Any] | None = None,
    user_id: Any = None,
    sticky_enabled: bool = True,
    update_session: bool = False,
    request_obj: Any = None,
) -> tuple[FilterParams, dict[str, Any]]:
    """
    Canonical filter resolution with deterministic metadata.

    Precedence:
    1) explicit request payload
    2) sticky session payload
    3) defaults
    """
    explicit = filter_args_present(source) or filter_capture_requested(source)
    source_label = "defaults"
    filters_source = "default"
    should_persist_explicit = False
    should_seed_defaults = False
    loaded_from_session = False

    if explicit:
        resolved = normalize_filters(parse_filters(source))
        source_label = "explicit_request"
        filters_source = "querystring"
        should_persist_explicit = bool(update_session and sticky_enabled and session_obj is not None)
    elif sticky_enabled and session_obj is not None:
        stored = read_sticky_filters_from_session(session_obj, user_id=user_id)
        if stored:
            resolved = normalize_filters(parse_filters(stored))
            source_label = "session"
            filters_source = "session"
            loaded_from_session = True
        else:
            resolved = normalize_filters(parse_filters({}))
            should_seed_defaults = bool(update_session and session_obj is not None)
    else:
        resolved = normalize_filters(parse_filters({}))

    resolved, sanitize_meta = sanitize_filters(resolved, include_meta=True)
    if sticky_enabled and session_obj is not None:
        if should_persist_explicit or should_seed_defaults or (loaded_from_session and sanitize_meta.get("sanitized")):
            write_sticky_filters_to_session(session_obj, resolved, user_id=user_id)

    canonical = canonical_filters_payload(resolved)
    canonical_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    filters_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    endpoint_name = None
    method = None
    path = None
    if request_obj is not None:
        endpoint_name = getattr(request_obj, "endpoint", None)
        method = getattr(request_obj, "method", None)
        path = getattr(request_obj, "path", None)

    meta = {
        "source": source_label,
        "filters_source": filters_source,
        "effective_filters": canonical,
        "filters_json": canonical_json,
        "filters_hash": filters_hash,
        "window_start": canonical.get("start_date"),
        "window_end": canonical.get("end_date"),
        "endpoint": endpoint_name or path,
        "method": method,
        "current_user_id": _as_session_user_id(user_id),
        "sanitized": bool(sanitize_meta.get("sanitized")),
        "dropped_filters": sanitize_meta.get("dropped") or {},
        "notice": sanitize_meta.get("notice"),
        "scope_hash": sanitize_meta.get("scope_hash"),
    }

    _bind_effective_filters_meta(meta)

    if _debug_filters_logging_enabled():
        try:
            from flask import current_app, g

            current_app.logger.info(
                "filters.resolve",
                extra={
                    "request_id": getattr(g, "request_id", None),
                    "current_user_id": meta.get("current_user_id"),
                    "filters_source": meta.get("filters_source"),
                    "source": meta.get("source"),
                    "filters_hash": meta.get("filters_hash"),
                    "window_start": meta.get("window_start"),
                    "window_end": meta.get("window_end"),
                    "endpoint": meta.get("endpoint"),
                    "sanitized": meta.get("sanitized"),
                    "dropped_filters": meta.get("dropped_filters"),
                },
            )
        except Exception:
            pass

    return resolved, meta


def resolve_filters(
    request_obj: Any,
    current_user_obj: Any = None,
    *,
    session_obj: Mapping[str, Any] | None = None,
    source: Any = None,
    sticky_enabled: bool = True,
    update_session: bool = False,
) -> tuple[FilterParams, dict[str, Any]]:
    user_id = None
    try:
        if current_user_obj is not None and hasattr(current_user_obj, "get_id"):
            user_id = current_user_obj.get_id()
        elif current_user_obj is not None:
            user_id = getattr(current_user_obj, "id", None)
    except Exception:
        user_id = None

    source_payload = source
    if source_payload is None and request_obj is not None:
        method = str(getattr(request_obj, "method", "") or "").upper()
        if method in {"POST", "PUT", "PATCH"} and hasattr(request_obj, "form"):
            source_payload = request_obj.form or {}
        elif hasattr(request_obj, "args"):
            source_payload = request_obj.args or {}
        else:
            source_payload = {}
    if source_payload is None:
        source_payload = {}

    request_cache = _request_cache_bucket("_resolved_filters_request_cache")
    cache_key = _resolve_filters_request_cache_key(
        source_payload,
        session_obj=session_obj,
        user_id=user_id,
        sticky_enabled=sticky_enabled,
        update_session=update_session,
        request_obj=request_obj,
    )
    if request_cache is not None and cache_key and cache_key in request_cache:
        return request_cache[cache_key]

    result = _resolve_filters_from_source(
        source_payload,
        session_obj=session_obj,
        user_id=user_id,
        sticky_enabled=sticky_enabled,
        update_session=update_session,
        request_obj=request_obj,
    )
    if request_cache is not None and cache_key:
        request_cache[cache_key] = result
    return result


def resolve_effective_filters(
    source: Any,
    *,
    session_obj: Mapping[str, Any] | None = None,
    user_id: Any = None,
    sticky_enabled: bool = True,
) -> FilterParams:
    request_obj = None
    try:
        from flask import has_request_context, request

        if has_request_context():
            request_obj = request
    except Exception:
        request_obj = None
    filters, _meta = _resolve_filters_from_source(
        source,
        session_obj=session_obj,
        user_id=user_id,
        sticky_enabled=sticky_enabled,
        update_session=False,
        request_obj=request_obj,
    )
    return filters


# ─────────────────────────────────────────────────────────────────────────────
# Domain helpers
# ─────────────────────────────────────────────────────────────────────────────
def shipping_name_series(df: pd.DataFrame) -> pd.Series:
    """
    Canonical shipping method names with shipper fallback.
    Returns pandas 'string' dtype; missing values are NA.
    """
    if df is None or df.empty:
        return pd.Series(dtype="string")

    result = pd.Series(pd.NA, index=df.index, dtype="string")
    candidates = ("ShippingMethodName", "ShippingMethodLabel", "ShipperName")

    for column in candidates:
        if column not in df.columns:
            continue
        series = df[column]
        if series.empty:
            continue
        values = series.astype("string", copy=False).str.strip()
        values = values.where(values.str.len() > 0)
        mask = result.isna()
        if mask.any():
            result.loc[mask] = values.loc[mask]
        else:
            break

    return result


def _has_active(values: tuple[str, ...]) -> bool:
    """True if we should apply this filter (i.e., not empty and not 'all')."""
    if not values:
        return False
    return not any(v.strip().lower() in _SENTINELS_ALL for v in values)


def _normalize_end_for_inclusive_day(end: pd.Timestamp | None) -> tuple[pd.Timestamp | None, bool]:
    """
    If end is aligned at 00:00:00 (typical from <input type="date">), return (end+1 day, use_strict_lt=True)
    so filtering can be done with Date < next_day, making the last day inclusive even with times of day.
    """
    if end is None:
        return None, False
    try:
        if (
            end.hour == 0 and end.minute == 0 and end.second == 0
            and getattr(end, "microsecond", 0) == 0 and getattr(end, "nanosecond", 0) == 0  # nanosecond for safety
        ):
            return end + pd.Timedelta(days=1), True
    except Exception:
        pass
    return end, False


# ─────────────────────────────────────────────────────────────────────────────
# Filter application (fast, vectorized)
# ─────────────────────────────────────────────────────────────────────────────
def apply_filters(df: pd.DataFrame, filters: FilterParams) -> pd.DataFrame:
    """
    Apply FilterParams to a canonical analytics DataFrame.
    - Vectorized numpy boolean mask (fast and stable)
    - Inclusive end-date support
    - Empty tuples mean "All" (no filter)
    """
    if df is None or df.empty:
        return df

    # Pre-filter by reps using fast set membership on multiple columns (reduces frame early)
    rep_values = tuple(getattr(filters, "sales_reps", ()) or ())
    if rep_values:
        lowered = {str(v).strip().lower() for v in rep_values if str(v).strip()}
        if lowered:
            cols = [c for c in ("RepId", "SalesRepId", "PrimarySalesRepId", "UserId") if c in df.columns]
            if cols:
                any_mask = np.zeros(len(df), dtype=bool)
                for col in cols:
                    ser = df[col].astype("string").str.strip().str.lower()
                    any_mask |= ser.isin(lowered).to_numpy(dtype=bool, na_value=False)
                df = df.loc[any_mask]
                if df.empty:
                    return df.copy()

    n = len(df)
    mask = np.ones(n, dtype=bool)  # start with all True

    # Date filtering (prefer EffectiveDate)
    date_series = None
    for cand in ("EffectiveDate", "Date"):
        if cand in df.columns:
            ds = pd.to_datetime(df[cand], errors="coerce")
            if ds.notna().any():
                try:
                    ds = ds.dt.tz_localize(None)
                except Exception:
                    pass
                date_series = ds
                break

    if date_series is not None and filters.start is not None:
        mask &= (date_series >= filters.start).to_numpy(dtype=bool, na_value=False)

    if date_series is not None and filters.end is not None:
        end_adj, use_strict_lt = _normalize_end_for_inclusive_day(filters.end)
        if use_strict_lt:
            mask &= (date_series < end_adj).to_numpy(dtype=bool, na_value=False)
        else:
            mask &= (date_series <= end_adj).to_numpy(dtype=bool, na_value=False)

    # Sales Reps
    rep_values = tuple(getattr(filters, "sales_reps", ()) or ())
    if rep_values:
        lowered = {str(v).strip().lower() for v in rep_values if str(v).strip()}
        if lowered:
            cols = [c for c in ("RepId", "SalesRepId", "PrimarySalesRepId", "UserId") if c in df.columns]
            if cols:
                any_mask = np.zeros(len(df), dtype=bool)
                for col in cols:
                    ser = df[col].astype("string").str.strip().str.lower()
                    any_mask |= ser.isin(lowered).to_numpy(dtype=bool, na_value=False)
                mask &= any_mask

    # Regions (support RegionId and RegionName; keep values as ids for stability)
    if _has_active(filters.regions):
        region_tokens = {(_normalize_token(v) or "").lower() for v in filters.regions}
        region_tokens.discard("")

        region_id_col = next((c for c in df.columns if c.lower() in {"regionid", "region_id"}), None)
        region_name_col = next((c for c in df.columns if c.lower() in {"regionname", "region_name", "region"}), None)

        masks = []
        if region_id_col:
            region_ids = df[region_id_col].astype("string").str.strip().str.lower()
            masks.append(region_ids.isin(region_tokens))
        if region_name_col:
            region_names = df[region_name_col].astype("string").str.strip().str.lower()
            masks.append(region_names.isin(region_tokens))

        if masks:
            any_mask = masks[0]
            for m in masks[1:]:
                any_mask = any_mask | m
            mask &= any_mask.to_numpy(dtype=bool, na_value=False)

    # Shipping methods (canonicalized)
    ship_ser: pd.Series | None = None
    if ("ShippingMethodName" in df.columns) or ("ShippingMethodLabel" in df.columns) or ("ShipperName" in df.columns):
        ship_ser = shipping_name_series(df)

    if _has_active(filters.methods) and ship_ser is not None:
        methods_set = {_normalize_token(v) for v in filters.methods}
        mask &= ship_ser.astype("string").str.strip().isin(methods_set).to_numpy(dtype=bool, na_value=False)

    # Customers (by name or id)
    if _has_active(filters.customers):
        cust_set = {_normalize_token(v) for v in filters.customers}
        submask = np.zeros(n, dtype=bool)
        if "CustomerName" in df.columns:
            submask |= df["CustomerName"].astype("string").str.strip().isin(cust_set).to_numpy(dtype=bool, na_value=False)
        if "CustomerId" in df.columns:
            submask |= df["CustomerId"].astype("string").str.strip().isin(cust_set).to_numpy(dtype=bool, na_value=False)
        mask &= submask

    # Suppliers (by name or id)
    if _has_active(filters.suppliers):
        sup_set = {_normalize_token(v) for v in filters.suppliers}
        submask = np.zeros(n, dtype=bool)
        if "SupplierName" in df.columns:
            submask |= df["SupplierName"].astype("string").str.strip().isin(sup_set).to_numpy(dtype=bool, na_value=False)
        if "SupplierId" in df.columns:
            submask |= df["SupplierId"].astype("string").str.strip().isin(sup_set).to_numpy(dtype=bool, na_value=False)
        mask &= submask

    # Products (by id or name)
    if _has_active(filters.products):
        prod_set = {_normalize_token(v) for v in filters.products}
        submask = np.zeros(n, dtype=bool)
        if "ProductId" in df.columns:
            submask |= df["ProductId"].astype("string").str.strip().isin(prod_set).to_numpy(dtype=bool, na_value=False)
        if "ProductName" in df.columns:
            submask |= df["ProductName"].astype("string").str.strip().isin(prod_set).to_numpy(dtype=bool, na_value=False)
        mask &= submask

    status_values = tuple(getattr(filters, "statuses", ()) or ())
    if status_values:
        status_set = {_normalize_token(v) for v in status_values}
        for col in ("OrderStatus", "order_status", "Status"):
            if col in df.columns:
                ser = df[col].astype("string").str.strip().str.lower()
                mask &= ser.isin(status_set).to_numpy(dtype=bool, na_value=False)
                break

    # Protein Groups
    protein_group_values = tuple(getattr(filters, "protein_groups", ()) or ())
    if protein_group_values:
        protein_set = {str(v).strip().lower() for v in protein_group_values if str(v).strip()}
        if protein_set:
            from app.services import fact_schema
            protein_col = fact_schema.resolve_protein_column(df)
            if protein_col:
                ser = df[protein_col].astype("string").str.strip().str.lower()
                mask &= ser.isin(protein_set).to_numpy(dtype=bool, na_value=False)

    # Yield Range
    yield_min = getattr(filters, "yield_min", None)
    yield_max = getattr(filters, "yield_max", None)
    if yield_min is not None or yield_max is not None:
        from app.services import fact_schema
        yield_col = fact_schema.resolve_yield_column(df)
        if yield_col:
            y_val = pd.to_numeric(df[yield_col], errors="coerce")
            if y_val.notna().any():
                if yield_min is not None:
                    mask &= (y_val >= float(yield_min)).to_numpy(dtype=bool, na_value=False)
                if yield_max is not None:
                    mask &= (y_val <= float(yield_max)).to_numpy(dtype=bool, na_value=False)

    # Protein numeric bounds
    protein_min = getattr(filters, "protein_min", None)
    protein_max = getattr(filters, "protein_max", None)
    protein_numeric_col = next(
        (
            col
            for col in ("Protein", "ProteinType", "ProteinName", "Category", "ProductCategory")
            if col in df.columns
        ),
        None,
    )
    if protein_numeric_col and (protein_min is not None or protein_max is not None):
        prot = pd.to_numeric(df[protein_numeric_col], errors="coerce")
        if prot.notna().any():
            if protein_min is not None:
                mask &= (prot >= float(protein_min)).to_numpy(dtype=bool, na_value=False)
            if protein_max is not None:
                mask &= (prot <= float(protein_max)).to_numpy(dtype=bool, na_value=False)

    # Product name contains
    token = (getattr(filters, "protein_name_like", None) or "").strip()
    if token:
        text_mask = np.zeros(n, dtype=bool)
        for col in ("ProteinType", "ProteinName", "Category", "ProductCategory", "Protein", "ProductName"):
            if col in df.columns:
                pn = df[col].astype("string")
                text_mask |= pn.str.contains(token, case=False, na=False).to_numpy(dtype=bool, na_value=False)
        mask &= text_mask

    # Apply mask
    if mask.all():
        out = df.copy(deep=False)
    else:
        out = df.loc[mask].copy()

    # Ensure Date column is tz-naive datetime64[ns] for stable downstream grouping
    if "Date" in out.columns:
        ts = pd.to_datetime(out["Date"], errors="coerce")
        try:
            ts = ts.dt.tz_localize(None)
        except Exception:
            pass
        out["Date"] = ts

    # Canonicalize shipping column in the output for consistent grouping later
    if ship_ser is not None and not out.empty:
        out = out.assign(ShippingMethodName=shipping_name_series(out))

    out.reset_index(drop=True, inplace=True)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Merge + Cache key
# ─────────────────────────────────────────────────────────────────────────────
def _normalize_extra(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _normalize_extra(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple, set)):
        return [_normalize_extra(v) for v in value]
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return _stringify(value)
    return _stringify(value)


def merge_filters(*filters: FilterParams) -> FilterParams:
    """Right-biased merge; later filters overwrite earlier ones, preserving all fields."""
    start = None
    end = None
    statuses: tuple[str, ...] = tuple()
    regions: tuple[str, ...] = tuple()
    methods: tuple[str, ...] = tuple()
    customers: tuple[str, ...] = tuple()
    suppliers: tuple[str, ...] = tuple()
    products: tuple[str, ...] = tuple()
    sales_reps: tuple[str, ...] = tuple()
    preset: str | None = None
    date_type: str | None = None
    protein_min: float | None = None
    protein_max: float | None = None
    protein_name_like: str | None = None
    complete_months_only = True

    for f in filters:
        if f is None:
            continue
        if f.start is not None: start = f.start
        if f.end   is not None: end   = f.end
        if getattr(f, "statuses", ()):
            statuses = tuple(getattr(f, "statuses"))
        if f.regions:   regions   = tuple(f.regions)
        if f.methods:   methods   = tuple(f.methods)
        if f.customers: customers = tuple(f.customers)
        if f.suppliers: suppliers = tuple(f.suppliers)
        if f.products:  products  = tuple(f.products)
        if getattr(f, "sales_reps", ()):
            sales_reps = tuple(getattr(f, "sales_reps"))
        if getattr(f, "preset", None):
            preset = str(getattr(f, "preset"))
        if getattr(f, "date_type", None):
            date_type = str(getattr(f, "date_type"))
        if getattr(f, "protein_min", None) is not None:
            protein_min = float(getattr(f, "protein_min"))
        if getattr(f, "protein_max", None) is not None:
            protein_max = float(getattr(f, "protein_max"))
        pname = getattr(f, "protein_name_like", None)
        if pname:
            protein_name_like = str(pname)
        complete_months_only = getattr(f, "complete_months_only", complete_months_only)

    return FilterParams(
        start=start, end=end,
        statuses=statuses,
        regions=regions, methods=methods, customers=customers,
        suppliers=suppliers, products=products, sales_reps=sales_reps,
        preset=preset,
        date_type=date_type,
        protein_min=protein_min, protein_max=protein_max, protein_name_like=protein_name_like,
        complete_months_only=complete_months_only,
    )


def cache_key_from_filters(filters: FilterParams, extras: Mapping[str, Any] | None = None) -> str:
    """Stable cache key derived from filters + optional context extras."""
    payload: dict[str, Any] = {
        "start": _stringify(filters.start) if filters.start is not None else None,
        "end":   _stringify(filters.end)   if filters.end   is not None else None,
        "statuses": sorted(getattr(filters, "statuses", tuple())),
        "regions":   sorted(filters.regions),
        "methods":   sorted(filters.methods),
        "customers": sorted(filters.customers),
        "suppliers": sorted(filters.suppliers),
        "products":  sorted(filters.products),
        "sales_reps": sorted(getattr(filters, "sales_reps", tuple())),
        "preset": getattr(filters, "preset", None),
        "date_type": getattr(filters, "date_type", None),
        "protein_min": getattr(filters, "protein_min", None),
        "protein_max": getattr(filters, "protein_max", None),
        "protein_name_like": getattr(filters, "protein_name_like", None),
        "complete_months_only": bool(getattr(filters, "complete_months_only", True)),
    }
    if extras:
        payload.update({f"extra:{str(k)}": _normalize_extra(v) for k, v in extras.items()})
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"filters:{digest}"


def filters_cache_key(user: Any, filters: Any, extras: Mapping[str, Any] | None = None) -> str:
    """
    Build a cache key that scopes filtered results to the current user and extras.
    Useful for memoizing heavy analytics blocks safely.
    """
    params = normalize_filters(filters)
    user_id = None
    role = None
    try:
        user_id = user.get_id() if hasattr(user, "get_id") else None
    except Exception:
        user_id = getattr(user, "id", None)
    try:
        role = getattr(user, "role", None)
    except Exception:
        role = None
    user_payload: dict[str, Any] = {"user": user_id, "role": role}
    try:
        from app.core.access_policy import scope_for_user  # type: ignore

        scope_obj = scope_for_user(user, use_cache=True)
        user_payload["scope_mode"] = scope_obj.scope_mode
        user_payload["scope_hash"] = scope_obj.scope_hash
        user_payload["permissions_version"] = scope_obj.permissions_version
    except Exception:
        pass
    try:
        from app.services import fact_store  # type: ignore

        user_payload.setdefault("dataset_version", fact_store.cache_buster())
    except Exception:
        pass
    if extras:
        user_payload.update(extras)
    return cache_key_from_filters(params, user_payload)

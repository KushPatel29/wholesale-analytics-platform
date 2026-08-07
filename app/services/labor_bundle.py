from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlencode

import pandas as pd
from flask import current_app, request, url_for

from app.core.cache_manager import TTLValueCache
from app.services import labor_store


WEEKDAY_ORDER = {
    "Monday": 1,
    "Tuesday": 2,
    "Wednesday": 3,
    "Thursday": 4,
    "Friday": 5,
    "Saturday": 6,
    "Sunday": 7,
}

TABLE_SORTS = {
    "labor_date": "labor_date",
    "department_name": "department_name",
    "employee_name": "employee_name",
    "time_category": "time_category",
    "labor_cost": "labor_cost",
    "paid_hours": "paid_hours_allocated",
    "effective_rate": "effective_rate",
    "status": "status",
    "work_rule": "work_rule",
}

_ANALYSIS_CACHE = TTLValueCache(maxsize=int(os.getenv("LABOR_ANALYSIS_CACHE_MAXSIZE", "48")))
_FOCUS_CACHE = TTLValueCache(maxsize=int(os.getenv("LABOR_FOCUS_CACHE_MAXSIZE", "96")))
_ANALYSIS_CACHE_TTL_SECONDS = max(30, int(os.getenv("LABOR_ANALYSIS_CACHE_TTL", "180")))
_FOCUS_CACHE_TTL_SECONDS = max(30, int(os.getenv("LABOR_FOCUS_CACHE_TTL", "180")))


@dataclass(frozen=True)
class LaborFilters:
    start: date
    end: date
    departments: tuple[str, ...] = ()
    employees: tuple[str, ...] = ()
    time_categories: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    work_rules: tuple[str, ...] = ()
    search: str | None = None
    page: int = 1
    page_size: int = 50
    sort_by: str = "labor_cost"
    sort_dir: str = "desc"


def _clone_cache_value(value: Any) -> Any:
    if isinstance(value, pd.DataFrame):
        return value.copy(deep=True)
    if isinstance(value, dict):
        return {key: _clone_cache_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clone_cache_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_clone_cache_value(item) for item in value)
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


def _analysis_filters(filters: LaborFilters) -> LaborFilters:
    return replace(filters, page=1, page_size=50, sort_by="labor_cost", sort_dir="desc")


def _filters_cache_payload(filters: LaborFilters) -> dict[str, Any]:
    return {
        "start": filters.start.isoformat(),
        "end": filters.end.isoformat(),
        "departments": list(filters.departments),
        "employees": list(filters.employees),
        "time_categories": list(filters.time_categories),
        "statuses": list(filters.statuses),
        "work_rules": list(filters.work_rules),
        "search": filters.search or None,
    }


def _labor_cache_key(kind: str, filters: LaborFilters, *, extra: Mapping[str, Any] | None = None) -> str:
    payload = {
        "kind": kind,
        "dataset_version": labor_store.get_dataset_version(),
        "filters": _filters_cache_payload(_analysis_filters(filters)),
        "extra": dict(extra or {}),
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cached_analysis(filters: LaborFilters) -> dict[str, Any]:
    normalized = _analysis_filters(filters)
    cache_key = _labor_cache_key("analysis", normalized)
    payload, _ = _ANALYSIS_CACHE.get_or_compute(cache_key, _ANALYSIS_CACHE_TTL_SECONDS, lambda: _li_build_analysis(normalized))
    return _clone_cache_value(payload)


def _cached_focus(kind: str, filters: LaborFilters, subject: str | None, builder) -> dict[str, Any] | None:
    token = str(subject or "").strip()
    if not token:
        return None
    normalized = _analysis_filters(filters)
    cache_key = _labor_cache_key(f"focus:{kind}", normalized, extra={"subject": token})
    payload, _ = _FOCUS_CACHE.get_or_compute(cache_key, _FOCUS_CACHE_TTL_SECONDS, builder)
    return _clone_cache_value(payload)


def _clean_tokens(values: Iterable[Any]) -> tuple[str, ...]:
    seen: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",")]
        else:
            parts = [str(value).strip()]
        for part in parts:
            if not part:
                continue
            if part not in seen:
                seen.append(part)
    return tuple(seen)


def _arg_list(args: Mapping[str, Any], *keys: str) -> tuple[str, ...]:
    values: list[Any] = []
    getter = getattr(args, "getlist", None)
    for key in keys:
        if getter is not None:
            try:
                values.extend(getter(key))
                values.extend(getter(f"{key}[]"))
            except Exception:
                pass
        value = args.get(key) if hasattr(args, "get") else None
        if value not in (None, ""):
            values.append(value)
    return _clean_tokens(values)


def _parse_date(raw: Any, *, default: date) -> date:
    if raw in (None, ""):
        return default
    ts = pd.to_datetime(raw, errors="coerce")
    if pd.isna(ts):
        return default
    return ts.date()


def _int_arg(raw: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(raw)
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def resolve_filters(args: Mapping[str, Any] | None = None) -> LaborFilters:
    source = args or request.args
    status = labor_store.get_status()
    max_date = _parse_date(status.max_date, default=date.today()) if status.max_date else date.today()
    default_days = max(7, int(current_app.config.get("LABOR_PAGE_DEFAULT_DAYS", 90) or 90))
    default_end = max_date
    default_start = default_end - timedelta(days=default_days - 1)
    start = _parse_date(source.get("start"), default=default_start)
    end = _parse_date(source.get("end"), default=default_end)
    if start > end:
        start, end = end, start
    sort_by = str(source.get("sort") or source.get("sort_by") or "labor_cost").strip().lower()
    if sort_by not in TABLE_SORTS:
        sort_by = "labor_cost"
    sort_dir = str(source.get("sort_dir") or source.get("direction") or "desc").strip().lower()
    sort_dir = "asc" if sort_dir == "asc" else "desc"
    return LaborFilters(
        start=start,
        end=end,
        departments=_arg_list(source, "department", "departments"),
        employees=_arg_list(source, "employee", "employees", "employee_code"),
        time_categories=_arg_list(source, "time_category", "time_categories"),
        statuses=_arg_list(source, "status", "statuses"),
        work_rules=_arg_list(source, "work_rule", "work_rules"),
        search=(str(source.get("search") or "").strip() or None),
        page=_int_arg(source.get("page"), 1, minimum=1, maximum=100000),
        page_size=_int_arg(source.get("page_size") or source.get("per_page"), 50, minimum=10, maximum=250),
        sort_by=sort_by,
        sort_dir=sort_dir,
    )


def _append_in_clause(clauses: list[str], params: list[Any], column_sql: str, values: Sequence[str]) -> None:
    values = [value for value in values if value]
    if not values:
        return
    placeholders = ", ".join(["?"] * len(values))
    clauses.append(f"{column_sql} IN ({placeholders})")
    params.extend(values)


def build_where_clause(
    filters: LaborFilters,
    *,
    alias: str = "lf",
    start: date | None = None,
    end: date | None = None,
) -> tuple[str, list[Any]]:
    clauses = [f"{alias}.labor_date >= ?", f"{alias}.labor_date <= ?"]
    params: list[Any] = [str(start or filters.start), str(end or filters.end)]
    _append_in_clause(clauses, params, f"COALESCE({alias}.department_name, 'Unassigned')", filters.departments)
    _append_in_clause(clauses, params, f"COALESCE({alias}.employee_code, '')", filters.employees)
    _append_in_clause(clauses, params, f"COALESCE({alias}.time_category, 'Unclassified')", filters.time_categories)
    _append_in_clause(clauses, params, f"COALESCE({alias}.status, '')", filters.statuses)
    _append_in_clause(clauses, params, f"COALESCE({alias}.work_rule, '')", filters.work_rules)
    if filters.search:
        needle = f"%{filters.search.lower()}%"
        clauses.append(
            "("
            f"LOWER(COALESCE({alias}.department_name, '')) LIKE ? OR "
            f"LOWER(COALESCE({alias}.employee_name, '')) LIKE ? OR "
            f"LOWER(COALESCE({alias}.employee_code, '')) LIKE ? OR "
            f"LOWER(COALESCE({alias}.time_category, '')) LIKE ? OR "
            f"LOWER(COALESCE({alias}.status, '')) LIKE ? OR "
            f"LOWER(COALESCE({alias}.work_rule, '')) LIKE ?"
            ")"
        )
        params.extend([needle] * 6)
    return " AND ".join(clauses), params


def _query_df(sql: str, params: Sequence[Any] | None = None) -> pd.DataFrame:
    return labor_store.query_df(sql, params)


def _query_summary(filters: LaborFilters, *, start: date | None = None, end: date | None = None) -> dict[str, Any]:
    where_sql, params = build_where_clause(filters, start=start, end=end)
    sql = f"""
        SELECT
            COALESCE(SUM(labor_cost), 0) AS total_labor_cost,
            COALESCE(SUM(paid_hours_allocated), 0) AS total_paid_hours,
            COALESCE(SUM(CASE WHEN is_premium THEN labor_cost ELSE 0 END), 0) AS premium_cost,
            COALESCE(SUM(CASE WHEN is_absence THEN labor_cost ELSE 0 END), 0) AS absence_cost,
            COALESCE(SUM(CASE WHEN is_memo THEN COALESCE(memo_amount, labor_cost, 0) ELSE 0 END), 0) AS memo_cost,
            COUNT(DISTINCT employee_key) AS active_employees,
            COUNT(DISTINCT department_key) AS active_departments,
            COUNT(*) AS transaction_count
        FROM labor_fact lf
        WHERE {where_sql}
    """
    frame = _query_df(sql, params)
    if frame.empty:
        return {
            "total_labor_cost": 0.0,
            "total_paid_hours": 0.0,
            "premium_cost": 0.0,
            "absence_cost": 0.0,
            "memo_cost": 0.0,
            "active_employees": 0,
            "active_departments": 0,
            "transaction_count": 0,
            "blended_rate": None,
            "premium_share_pct": None,
            "absence_share_pct": None,
        }
    row = frame.iloc[0].to_dict()
    total_labor_cost = float(row.get("total_labor_cost") or 0.0)
    total_paid_hours = float(row.get("total_paid_hours") or 0.0)
    premium_cost = float(row.get("premium_cost") or 0.0)
    absence_cost = float(row.get("absence_cost") or 0.0)
    row["blended_rate"] = (total_labor_cost / total_paid_hours) if total_paid_hours else None
    row["premium_share_pct"] = (premium_cost / total_labor_cost) if total_labor_cost else None
    row["absence_share_pct"] = (absence_cost / total_labor_cost) if total_labor_cost else None
    row["total_labor_cost"] = total_labor_cost
    row["total_paid_hours"] = total_paid_hours
    row["premium_cost"] = premium_cost
    row["absence_cost"] = absence_cost
    row["memo_cost"] = float(row.get("memo_cost") or 0.0)
    row["active_employees"] = int(row.get("active_employees") or 0)
    row["active_departments"] = int(row.get("active_departments") or 0)
    row["transaction_count"] = int(row.get("transaction_count") or 0)
    return row


def _query_department_summary(filters: LaborFilters, *, start: date | None = None, end: date | None = None) -> pd.DataFrame:
    where_sql, params = build_where_clause(filters, start=start, end=end)
    sql = f"""
        WITH grouped AS (
            SELECT
                COALESCE(department_name, 'Unassigned') AS department_name,
                COALESCE(department_number, '') AS department_number,
                COUNT(*) AS transaction_count,
                COUNT(DISTINCT employee_key) AS active_employee_count,
                COALESCE(SUM(labor_cost), 0) AS labor_cost,
                COALESCE(SUM(paid_hours_allocated), 0) AS paid_hours,
                COALESCE(SUM(CASE WHEN is_premium THEN labor_cost ELSE 0 END), 0) AS premium_cost,
                COALESCE(SUM(CASE WHEN is_absence THEN labor_cost ELSE 0 END), 0) AS absence_cost
            FROM labor_fact lf
            WHERE {where_sql}
            GROUP BY 1, 2
        )
        SELECT
            *,
            CASE WHEN paid_hours = 0 THEN NULL ELSE labor_cost / paid_hours END AS blended_rate,
            CASE WHEN labor_cost = 0 THEN NULL ELSE premium_cost / labor_cost END AS premium_share_pct,
            CASE WHEN labor_cost = 0 THEN NULL ELSE absence_cost / labor_cost END AS absence_share_pct,
            CASE WHEN active_employee_count = 0 THEN NULL ELSE labor_cost / active_employee_count END AS avg_cost_per_employee,
            CASE WHEN active_employee_count = 0 THEN NULL ELSE paid_hours / active_employee_count END AS avg_paid_hours_per_employee,
            CASE WHEN SUM(labor_cost) OVER () = 0 THEN NULL ELSE labor_cost / SUM(labor_cost) OVER () END AS labor_cost_share_pct,
            CASE WHEN SUM(paid_hours) OVER () = 0 THEN NULL ELSE paid_hours / SUM(paid_hours) OVER () END AS paid_hours_share_pct
        FROM grouped
        ORDER BY labor_cost DESC, department_name ASC
    """
    return _query_df(sql, params)


def _query_daily_trend(filters: LaborFilters) -> pd.DataFrame:
    where_sql, params = build_where_clause(filters)
    sql = f"""
        SELECT
            labor_date,
            COALESCE(SUM(labor_cost), 0) AS labor_cost,
            COALESCE(SUM(paid_hours_allocated), 0) AS paid_hours,
            COALESCE(SUM(CASE WHEN is_premium THEN labor_cost ELSE 0 END), 0) AS premium_cost,
            COALESCE(SUM(CASE WHEN is_absence THEN labor_cost ELSE 0 END), 0) AS absence_cost
        FROM labor_fact lf
        WHERE {where_sql}
        GROUP BY 1
        ORDER BY 1
    """
    frame = _query_df(sql, params)
    if not frame.empty:
        frame["labor_date"] = pd.to_datetime(frame["labor_date"], errors="coerce").dt.date
    return frame


def _query_monthly_department_trend(filters: LaborFilters, departments: Sequence[str]) -> pd.DataFrame:
    if not departments:
        return pd.DataFrame(columns=["labor_month", "department_name", "labor_cost", "paid_hours"])
    scoped = replace(filters, departments=tuple(departments))
    where_sql, params = build_where_clause(scoped)
    sql = f"""
        SELECT
            labor_month,
            department_name,
            COALESCE(SUM(labor_cost), 0) AS labor_cost,
            COALESCE(SUM(paid_hours_allocated), 0) AS paid_hours
        FROM labor_fact lf
        WHERE {where_sql}
        GROUP BY 1, 2
        ORDER BY 1, 2
    """
    return _query_df(sql, params)


def _query_category_mix(filters: LaborFilters, *, start: date | None = None, end: date | None = None) -> pd.DataFrame:
    where_sql, params = build_where_clause(filters, start=start, end=end)
    sql = f"""
        WITH grouped AS (
            SELECT
                COALESCE(time_category, 'Unclassified') AS time_category,
                COALESCE(SUM(labor_cost), 0) AS labor_cost,
                COALESCE(SUM(paid_hours_allocated), 0) AS paid_hours,
                COALESCE(SUM(CASE WHEN is_premium THEN labor_cost ELSE 0 END), 0) AS premium_cost,
                COALESCE(SUM(CASE WHEN is_absence THEN labor_cost ELSE 0 END), 0) AS absence_cost
            FROM labor_fact lf
            WHERE {where_sql}
            GROUP BY 1
        )
        SELECT
            *,
            CASE WHEN SUM(labor_cost) OVER () = 0 THEN NULL ELSE labor_cost / SUM(labor_cost) OVER () END AS labor_cost_share_pct,
            CASE WHEN SUM(paid_hours) OVER () = 0 THEN NULL ELSE paid_hours / SUM(paid_hours) OVER () END AS paid_hours_share_pct
        FROM grouped
        ORDER BY labor_cost DESC, time_category ASC
    """
    return _query_df(sql, params)


def _query_department_category_mix(filters: LaborFilters) -> pd.DataFrame:
    where_sql, params = build_where_clause(filters)
    sql = f"""
        SELECT
            COALESCE(department_name, 'Unassigned') AS department_name,
            COALESCE(time_category, 'Unclassified') AS time_category,
            COALESCE(SUM(labor_cost), 0) AS labor_cost,
            COALESCE(SUM(paid_hours_allocated), 0) AS paid_hours
        FROM labor_fact lf
        WHERE {where_sql}
        GROUP BY 1, 2
        ORDER BY labor_cost DESC
        LIMIT 24
    """
    return _query_df(sql, params)


def _query_weekday_pattern(filters: LaborFilters) -> pd.DataFrame:
    trend = _query_daily_trend(filters)
    if trend.empty:
        return pd.DataFrame(columns=["weekday_name", "avg_daily_labor_cost", "avg_daily_paid_hours"])
    trend["weekday_name"] = pd.to_datetime(trend["labor_date"], errors="coerce").dt.day_name()
    grouped = (
        trend.groupby("weekday_name", dropna=False)
        .agg(avg_daily_labor_cost=("labor_cost", "mean"), avg_daily_paid_hours=("paid_hours", "mean"))
        .reset_index()
    )
    grouped["weekday_order"] = grouped["weekday_name"].map(WEEKDAY_ORDER).fillna(99)
    grouped = grouped.sort_values(["weekday_order", "weekday_name"]).drop(columns=["weekday_order"])
    return grouped


def _query_monthly_pattern(filters: LaborFilters) -> pd.DataFrame:
    where_sql, params = build_where_clause(filters)
    sql = f"""
        SELECT
            labor_month,
            COALESCE(SUM(labor_cost), 0) AS labor_cost,
            COALESCE(SUM(paid_hours_allocated), 0) AS paid_hours
        FROM labor_fact lf
        WHERE {where_sql}
        GROUP BY 1
        ORDER BY 1
    """
    return _query_df(sql, params)


def _query_department_daily(filters: LaborFilters) -> pd.DataFrame:
    where_sql, params = build_where_clause(filters)
    sql = f"""
        SELECT
            labor_date,
            COALESCE(department_name, 'Unassigned') AS department_name,
            COALESCE(SUM(labor_cost), 0) AS labor_cost,
            COALESCE(SUM(paid_hours_allocated), 0) AS paid_hours,
            COALESCE(SUM(CASE WHEN is_premium THEN labor_cost ELSE 0 END), 0) AS premium_cost,
            COALESCE(SUM(CASE WHEN is_absence THEN labor_cost ELSE 0 END), 0) AS absence_cost
        FROM labor_fact lf
        WHERE {where_sql}
        GROUP BY 1, 2
        ORDER BY 1, 2
    """
    frame = _query_df(sql, params)
    if not frame.empty:
        frame["labor_date"] = pd.to_datetime(frame["labor_date"], errors="coerce").dt.date
    return frame


def _query_category_daily_trend(filters: LaborFilters, categories: Sequence[str]) -> pd.DataFrame:
    if not categories:
        return pd.DataFrame(columns=["labor_date", "time_category", "labor_cost", "paid_hours"])
    scoped = replace(filters, time_categories=tuple(categories))
    where_sql, params = build_where_clause(scoped)
    sql = f"""
        SELECT
            labor_date,
            COALESCE(time_category, 'Unclassified') AS time_category,
            COALESCE(SUM(labor_cost), 0) AS labor_cost,
            COALESCE(SUM(paid_hours_allocated), 0) AS paid_hours
        FROM labor_fact lf
        WHERE {where_sql}
        GROUP BY 1, 2
        ORDER BY 1, 2
    """
    frame = _query_df(sql, params)
    if not frame.empty:
        frame["labor_date"] = pd.to_datetime(frame["labor_date"], errors="coerce").dt.date
    return frame


def _query_worker_summary(
    filters: LaborFilters,
    *,
    start: date | None = None,
    end: date | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    where_sql, params = build_where_clause(filters, start=start, end=end)
    sql = f"""
        WITH grouped AS (
            SELECT
                COALESCE(employee_code, '') AS employee_code,
                COALESCE(employee_name, employee_code, 'Unknown Employee') AS employee_name,
                COUNT(*) AS transaction_count,
                COUNT(DISTINCT labor_date) AS active_days,
                COUNT(DISTINCT COALESCE(department_name, 'Unassigned')) AS department_count,
                COUNT(DISTINCT COALESCE(time_category, 'Unclassified')) AS category_count,
                COALESCE(SUM(labor_cost), 0) AS labor_cost,
                COALESCE(SUM(paid_hours_allocated), 0) AS paid_hours,
                COALESCE(SUM(CASE WHEN is_premium THEN labor_cost ELSE 0 END), 0) AS premium_cost,
                COALESCE(SUM(CASE WHEN is_absence THEN labor_cost ELSE 0 END), 0) AS absence_cost
            FROM labor_fact lf
            WHERE {where_sql}
            GROUP BY 1, 2
        )
        SELECT
            *,
            CASE WHEN paid_hours = 0 THEN NULL ELSE labor_cost / paid_hours END AS blended_rate,
            CASE WHEN labor_cost = 0 THEN NULL ELSE premium_cost / labor_cost END AS premium_share_pct,
            CASE WHEN labor_cost = 0 THEN NULL ELSE absence_cost / labor_cost END AS absence_share_pct,
            CASE WHEN active_days = 0 THEN NULL ELSE labor_cost / active_days END AS avg_cost_per_day,
            CASE WHEN active_days = 0 THEN NULL ELSE paid_hours / active_days END AS avg_hours_per_day,
            CASE WHEN SUM(labor_cost) OVER () = 0 THEN NULL ELSE labor_cost / SUM(labor_cost) OVER () END AS labor_cost_share_pct,
            CASE WHEN SUM(paid_hours) OVER () = 0 THEN NULL ELSE paid_hours / SUM(paid_hours) OVER () END AS paid_hours_share_pct
        FROM grouped
        ORDER BY labor_cost DESC, employee_name ASC
    """
    if limit is not None:
        sql += " LIMIT ?"
        params = [*params, int(limit)]
    return _query_df(sql, params)


def _query_worker_daily_trend(filters: LaborFilters, employee_codes: Sequence[str]) -> pd.DataFrame:
    if not employee_codes:
        return pd.DataFrame(columns=["labor_date", "employee_code", "employee_name", "labor_cost", "paid_hours", "premium_cost", "absence_cost"])
    scoped = replace(filters, employees=tuple(employee_codes))
    where_sql, params = build_where_clause(scoped)
    sql = f"""
        SELECT
            labor_date,
            COALESCE(employee_code, '') AS employee_code,
            COALESCE(employee_name, employee_code, 'Unknown Employee') AS employee_name,
            COALESCE(SUM(labor_cost), 0) AS labor_cost,
            COALESCE(SUM(paid_hours_allocated), 0) AS paid_hours,
            COALESCE(SUM(CASE WHEN is_premium THEN labor_cost ELSE 0 END), 0) AS premium_cost,
            COALESCE(SUM(CASE WHEN is_absence THEN labor_cost ELSE 0 END), 0) AS absence_cost
        FROM labor_fact lf
        WHERE {where_sql}
        GROUP BY 1, 2, 3
        ORDER BY 1, 3
    """
    frame = _query_df(sql, params)
    if not frame.empty:
        frame["labor_date"] = pd.to_datetime(frame["labor_date"], errors="coerce").dt.date
    return frame


def _query_worker_department_mix(filters: LaborFilters) -> pd.DataFrame:
    where_sql, params = build_where_clause(filters)
    sql = f"""
        SELECT
            COALESCE(employee_code, '') AS employee_code,
            COALESCE(employee_name, employee_code, 'Unknown Employee') AS employee_name,
            COALESCE(department_name, 'Unassigned') AS department_name,
            COALESCE(SUM(labor_cost), 0) AS labor_cost,
            COALESCE(SUM(paid_hours_allocated), 0) AS paid_hours
        FROM labor_fact lf
        WHERE {where_sql}
        GROUP BY 1, 2, 3
        ORDER BY labor_cost DESC, employee_name ASC, department_name ASC
        LIMIT 60
    """
    return _query_df(sql, params)


def _query_worker_category_mix(filters: LaborFilters) -> pd.DataFrame:
    where_sql, params = build_where_clause(filters)
    sql = f"""
        SELECT
            COALESCE(employee_code, '') AS employee_code,
            COALESCE(employee_name, employee_code, 'Unknown Employee') AS employee_name,
            COALESCE(time_category, 'Unclassified') AS time_category,
            COALESCE(SUM(labor_cost), 0) AS labor_cost,
            COALESCE(SUM(paid_hours_allocated), 0) AS paid_hours
        FROM labor_fact lf
        WHERE {where_sql}
        GROUP BY 1, 2, 3
        ORDER BY labor_cost DESC, employee_name ASC, time_category ASC
        LIMIT 60
    """
    return _query_df(sql, params)


def _query_workspace(filters: LaborFilters, *, export_all: bool = False) -> dict[str, Any]:
    where_sql, params = build_where_clause(filters)
    count_sql = f"SELECT COUNT(*) AS total_rows FROM labor_fact lf WHERE {where_sql}"
    total_rows_frame = _query_df(count_sql, params)
    total_rows = int((total_rows_frame.iloc[0]["total_rows"] if not total_rows_frame.empty else 0) or 0)
    sort_col = TABLE_SORTS.get(filters.sort_by, "labor_cost")
    sort_dir = "ASC" if filters.sort_dir == "asc" else "DESC"
    sql = f"""
        SELECT
            labor_date,
            COALESCE(department_name, 'Unassigned') AS department_name,
            COALESCE(employee_name, employee_code, 'Unknown Employee') AS employee_name,
            employee_code,
            COALESCE(time_category, 'Unclassified') AS time_category,
            labor_cost,
            paid_hours_allocated AS paid_hours,
            effective_rate,
            status,
            work_rule,
            is_premium,
            is_absence,
            is_memo
        FROM labor_fact lf
        WHERE {where_sql}
        ORDER BY {sort_col} {sort_dir}, labor_date DESC, department_name ASC, employee_name ASC
    """
    query_params = list(params)
    if not export_all:
        offset = max(0, (filters.page - 1) * filters.page_size)
        sql += " LIMIT ? OFFSET ?"
        query_params.extend([filters.page_size, offset])
    frame = _query_df(sql, query_params)
    if not frame.empty:
        frame["labor_date"] = pd.to_datetime(frame["labor_date"], errors="coerce").dt.date
    total_pages = max(1, math.ceil(total_rows / max(1, filters.page_size))) if not export_all else 1
    return {
        "rows": frame.to_dict(orient="records"),
        "frame": frame,
        "total_rows": total_rows,
        "total_pages": total_pages,
        "page": filters.page,
        "page_size": filters.page_size,
    }


def _query_filter_options(filters: LaborFilters) -> dict[str, list[dict[str, str]]]:
    base_where, base_params = build_where_clause(replace(filters, departments=(), employees=(), time_categories=(), statuses=(), work_rules=(), search=None))
    departments = _query_df(
        f"""
        SELECT
            COALESCE(department_name, 'Unassigned') AS department_name,
            COALESCE(department_number, '') AS department_number,
            SUM(labor_cost) AS labor_cost
        FROM labor_fact lf
        WHERE {base_where}
        GROUP BY 1, 2
        ORDER BY labor_cost DESC, department_name ASC
        """,
        base_params,
    )
    employees = _query_df(
        f"""
        SELECT
            COALESCE(employee_code, '') AS employee_code,
            COALESCE(employee_name, employee_code, 'Unknown Employee') AS employee_name,
            SUM(labor_cost) AS labor_cost
        FROM labor_fact lf
        WHERE {base_where}
        GROUP BY 1, 2
        ORDER BY labor_cost DESC, employee_name ASC
        LIMIT 400
        """,
        base_params,
    )
    categories = _query_df(
        f"""
        SELECT COALESCE(time_category, 'Unclassified') AS time_category, SUM(labor_cost) AS labor_cost
        FROM labor_fact lf
        WHERE {base_where}
        GROUP BY 1
        ORDER BY labor_cost DESC, time_category ASC
        """,
        base_params,
    )
    statuses = _query_df(
        f"""
        SELECT COALESCE(status, '') AS status, COUNT(*) AS rows
        FROM labor_fact lf
        WHERE {base_where}
        GROUP BY 1
        ORDER BY rows DESC, status ASC
        """,
        base_params,
    )
    work_rules = _query_df(
        f"""
        SELECT COALESCE(work_rule, '') AS work_rule, COUNT(*) AS rows
        FROM labor_fact lf
        WHERE {base_where}
        GROUP BY 1
        ORDER BY rows DESC, work_rule ASC
        """,
        base_params,
    )
    return {
        "departments": [
            {
                "value": str(row.get("department_name") or "Unassigned"),
                "label": (
                    f"{str(row.get('department_number') or '').strip()} - {str(row.get('department_name') or 'Unassigned')}"
                ).strip(" -"),
            }
            for row in departments.to_dict(orient="records")
        ],
        "employees": [
            {
                "value": str(row.get("employee_code") or ""),
                "label": (
                    f"{str(row.get('employee_code') or '').strip()} - {str(row.get('employee_name') or 'Unknown Employee')}"
                ).strip(" -"),
            }
            for row in employees.to_dict(orient="records")
            if row.get("employee_code")
        ],
        "time_categories": [
            {"value": str(row.get("time_category") or "Unclassified"), "label": str(row.get("time_category") or "Unclassified")}
            for row in categories.to_dict(orient="records")
        ],
        "statuses": [
            {"value": str(row.get("status") or ""), "label": str(row.get("status") or "Unknown")}
            for row in statuses.to_dict(orient="records")
            if row.get("status")
        ],
        "work_rules": [
            {"value": str(row.get("work_rule") or ""), "label": str(row.get("work_rule") or "Unknown")}
            for row in work_rules.to_dict(orient="records")
            if row.get("work_rule")
        ],
    }


def _prior_window(filters: LaborFilters) -> tuple[date, date]:
    window_days = max(1, (filters.end - filters.start).days + 1)
    prior_end = filters.start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=window_days - 1)
    return prior_start, prior_end


def _delta_pct(current: float | None, prior: float | None) -> float | None:
    if current is None or prior in (None, 0):
        return None
    return (float(current) - float(prior)) / float(prior)


def _safe_float(value: Any) -> float:
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _driver_label(cost_delta: float | None, hours_delta: float | None, rate_delta: float | None) -> str:
    cost = _safe_float(cost_delta)
    hours = _safe_float(hours_delta)
    rate = _safe_float(rate_delta)
    if abs(cost) < 0.02:
        return "stable"
    if abs(rate) > abs(hours) + 0.03:
        return "rate-driven"
    if abs(hours) > abs(rate) + 0.03:
        return "hours-driven"
    return "mixed"


def _recent_change(series: pd.Series) -> float | None:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return None
    window = min(14, max(2, int(len(clean) // 2) or 2))
    if len(clean) < window * 2:
        return None
    recent = float(clean.iloc[-window:].sum())
    prior = float(clean.iloc[-(window * 2) : -window].sum())
    if prior == 0:
        return None
    return (recent - prior) / prior


def _sort_for_json(frame: pd.DataFrame, columns: Sequence[str], ascending: Sequence[bool], limit: int | None = None) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    work = frame.sort_values(list(columns), ascending=list(ascending))
    if limit is not None:
        work = work.head(limit)
    return work.to_dict(orient="records")


def _top_label(frame: pd.DataFrame, column: str, default: str) -> str:
    if frame.empty or column not in frame.columns:
        return default
    value = str(frame.iloc[0].get(column) or "").strip()
    return value or default


def _enrich_department_summary(
    current_departments: pd.DataFrame,
    prior_departments: pd.DataFrame,
    department_daily: pd.DataFrame,
) -> pd.DataFrame:
    if current_departments.empty:
        return current_departments
    frame = current_departments.copy()
    prior = prior_departments.rename(
        columns={
            "labor_cost": "prior_labor_cost",
            "paid_hours": "prior_paid_hours",
            "blended_rate": "prior_blended_rate",
            "premium_share_pct": "prior_premium_share_pct",
            "absence_share_pct": "prior_absence_share_pct",
        }
    )
    keep = [column for column in ["department_name", "department_number", "prior_labor_cost", "prior_paid_hours", "prior_blended_rate", "prior_premium_share_pct", "prior_absence_share_pct"] if column in prior.columns]
    if keep:
        frame = frame.merge(prior.loc[:, keep], on=["department_name", "department_number"], how="left")
    else:
        frame["prior_labor_cost"] = pd.NA
        frame["prior_paid_hours"] = pd.NA
        frame["prior_blended_rate"] = pd.NA
        frame["prior_premium_share_pct"] = pd.NA
        frame["prior_absence_share_pct"] = pd.NA
    frame["cost_delta_pct"] = (frame["labor_cost"] - frame["prior_labor_cost"]) / frame["prior_labor_cost"]
    frame["hours_delta_pct"] = (frame["paid_hours"] - frame["prior_paid_hours"]) / frame["prior_paid_hours"]
    frame["rate_delta_pct"] = (frame["blended_rate"] - frame["prior_blended_rate"]) / frame["prior_blended_rate"]
    frame["premium_share_delta_pct"] = frame["premium_share_pct"] - frame["prior_premium_share_pct"]
    frame["absence_share_delta_pct"] = frame["absence_share_pct"] - frame["prior_absence_share_pct"]
    if department_daily.empty:
        frame["cost_volatility"] = pd.NA
        frame["recent_cost_acceleration_pct"] = pd.NA
        frame["recent_hours_acceleration_pct"] = pd.NA
    else:
        daily = department_daily.copy()
        summary = (
            daily.groupby("department_name", dropna=False)
            .agg(
                daily_cost_mean=("labor_cost", "mean"),
                daily_cost_std=("labor_cost", "std"),
                daily_hours_mean=("paid_hours", "mean"),
                daily_hours_std=("paid_hours", "std"),
            )
            .reset_index()
        )
        summary["cost_volatility"] = summary["daily_cost_std"] / summary["daily_cost_mean"]
        summary["hours_volatility"] = summary["daily_hours_std"] / summary["daily_hours_mean"]
        accel_rows: list[dict[str, Any]] = []
        for department_name, part in daily.groupby("department_name", dropna=False):
            ordered = part.sort_values("labor_date")
            accel_rows.append(
                {
                    "department_name": department_name,
                    "recent_cost_acceleration_pct": _recent_change(ordered["labor_cost"]),
                    "recent_hours_acceleration_pct": _recent_change(ordered["paid_hours"]),
                }
            )
        accel = pd.DataFrame(accel_rows)
        frame = frame.merge(summary[["department_name", "cost_volatility", "hours_volatility"]], on="department_name", how="left")
        frame = frame.merge(accel, on="department_name", how="left")
    frame["driver_label"] = [
        _driver_label(cost_delta, hours_delta, rate_delta)
        for cost_delta, hours_delta, rate_delta in zip(
            frame.get("cost_delta_pct", pd.Series(dtype="float")),
            frame.get("hours_delta_pct", pd.Series(dtype="float")),
            frame.get("rate_delta_pct", pd.Series(dtype="float")),
        )
    ]
    frame["management_focus"] = "review mix"
    frame.loc[frame["premium_share_pct"].fillna(0).ge(0.08), "management_focus"] = "review premium"
    frame.loc[frame["absence_share_pct"].fillna(0).ge(0.04), "management_focus"] = "review absence"
    frame.loc[frame["driver_label"] == "hours-driven", "management_focus"] = "review staffing volume"
    frame.loc[frame["driver_label"] == "rate-driven", "management_focus"] = "review rate pressure"
    frame.loc[frame["cost_volatility"].fillna(0).ge(0.20), "management_focus"] = "review stability"
    return frame


def _enrich_category_summary(
    current_categories: pd.DataFrame,
    prior_categories: pd.DataFrame,
    department_category_mix: pd.DataFrame,
) -> pd.DataFrame:
    if current_categories.empty:
        return current_categories
    frame = current_categories.copy()
    prior = prior_categories.rename(
        columns={
            "labor_cost": "prior_labor_cost",
            "paid_hours": "prior_paid_hours",
            "premium_cost": "prior_premium_cost",
            "absence_cost": "prior_absence_cost",
        }
    )
    keep = [column for column in ["time_category", "prior_labor_cost", "prior_paid_hours", "prior_premium_cost", "prior_absence_cost"] if column in prior.columns]
    if "time_category" in keep and len(keep) > 1:
        frame = frame.merge(prior.loc[:, keep], on="time_category", how="left")
    else:
        frame["prior_labor_cost"] = pd.NA
        frame["prior_paid_hours"] = pd.NA
        frame["prior_premium_cost"] = pd.NA
        frame["prior_absence_cost"] = pd.NA
    frame["cost_delta_pct"] = (frame["labor_cost"] - frame["prior_labor_cost"]) / frame["prior_labor_cost"]
    frame["hours_delta_pct"] = (frame["paid_hours"] - frame["prior_paid_hours"]) / frame["prior_paid_hours"]
    if department_category_mix.empty:
        frame["leading_department"] = pd.NA
        frame["leading_department_share_pct"] = pd.NA
        return frame
    mix = department_category_mix.sort_values(["labor_cost", "department_name"], ascending=[False, True]).copy()
    leading = mix.groupby("time_category", dropna=False).head(1).rename(columns={"department_name": "leading_department", "labor_cost": "leading_department_cost"})
    frame = frame.merge(leading[["time_category", "leading_department", "leading_department_cost"]], on="time_category", how="left")
    frame["leading_department_share_pct"] = frame["leading_department_cost"] / frame["labor_cost"]
    return frame


def _enrich_worker_summary(current_workers: pd.DataFrame, prior_workers: pd.DataFrame) -> pd.DataFrame:
    if current_workers.empty:
        return current_workers
    frame = current_workers.copy()
    prior = prior_workers.rename(
        columns={
            "labor_cost": "prior_labor_cost",
            "paid_hours": "prior_paid_hours",
            "blended_rate": "prior_blended_rate",
            "premium_share_pct": "prior_premium_share_pct",
            "absence_share_pct": "prior_absence_share_pct",
        }
    )
    keep = [column for column in ["employee_code", "prior_labor_cost", "prior_paid_hours", "prior_blended_rate", "prior_premium_share_pct", "prior_absence_share_pct"] if column in prior.columns]
    if "employee_code" in keep and len(keep) > 1:
        frame = frame.merge(prior.loc[:, keep], on="employee_code", how="left")
    else:
        frame["prior_labor_cost"] = pd.NA
        frame["prior_paid_hours"] = pd.NA
        frame["prior_blended_rate"] = pd.NA
        frame["prior_premium_share_pct"] = pd.NA
        frame["prior_absence_share_pct"] = pd.NA
    frame["cost_delta_pct"] = (frame["labor_cost"] - frame["prior_labor_cost"]) / frame["prior_labor_cost"]
    frame["hours_delta_pct"] = (frame["paid_hours"] - frame["prior_paid_hours"]) / frame["prior_paid_hours"]
    frame["rate_delta_pct"] = (frame["blended_rate"] - frame["prior_blended_rate"]) / frame["prior_blended_rate"]
    frame["multi_department_flag"] = frame["department_count"].fillna(0).ge(2)
    frame["management_focus"] = "review workload"
    frame.loc[frame["premium_share_pct"].fillna(0).ge(0.08), "management_focus"] = "review premium"
    frame.loc[frame["absence_share_pct"].fillna(0).ge(0.04), "management_focus"] = "review absence"
    frame.loc[frame["multi_department_flag"], "management_focus"] = "review coverage"
    return frame


def _build_signals(
    filters: LaborFilters,
    current_summary: Mapping[str, Any],
    prior_summary: Mapping[str, Any],
    current_departments: pd.DataFrame,
    watchlist: pd.DataFrame,
    current_categories: pd.DataFrame,
    current_workers: pd.DataFrame,
) -> list[dict[str, Any]]:
    total_cost_delta = _delta_pct(current_summary.get("total_labor_cost"), prior_summary.get("total_labor_cost"))
    total_hours_delta = _delta_pct(current_summary.get("total_paid_hours"), prior_summary.get("total_paid_hours"))
    blended_rate_delta = _delta_pct(current_summary.get("blended_rate"), prior_summary.get("blended_rate"))
    top_department = current_departments.iloc[0].to_dict() if not current_departments.empty else {}
    top_watch = watchlist.iloc[0].to_dict() if not watchlist.empty else {}
    top_category = current_categories.iloc[0].to_dict() if not current_categories.empty else {}
    top_worker = current_workers.iloc[0].to_dict() if not current_workers.empty else {}
    premium_share = float(current_summary.get("premium_share_pct") or 0.0)
    absence_share = float(current_summary.get("absence_share_pct") or 0.0)
    rate_driver = "hours-driven"
    if (blended_rate_delta or 0.0) > (total_hours_delta or 0.0):
        rate_driver = "rate-driven"
    signals = [
        {
            "title": "Department Labor Pressure",
            "tone": "danger" if top_watch else "secondary",
            "detail": (
                f"{top_watch.get('department_name')} needs attention first because cost, premium, absence, or volatility is elevated."
                if top_watch
                else f"{top_department.get('department_name', 'No department')} carries the largest share of labor cost."
            ),
            "next_step": "Open the department investigation layer and compare workers, categories, and the recent trend.",
        },
        {
            "title": "Premium Risk",
            "tone": "warning" if premium_share >= 0.08 else "success",
            "detail": f"Premium share is {premium_share:.1%}; investigate when it stays above 8% or rises faster than hours.",
            "next_step": "Review premium-heavy departments and workers before adding coverage.",
        },
        {
            "title": "Absence Watch",
            "tone": "warning" if absence_share >= 0.04 else "success",
            "detail": f"Absence share is {absence_share:.1%}; concentrated absence costs usually require schedule or policy review.",
            "next_step": "Check absence-heavy departments, then inspect the worker watchlist for repeat exposure.",
        },
        {
            "title": "Cost Acceleration",
            "tone": "danger" if (total_cost_delta or 0.0) >= 0.10 else "secondary",
            "detail": (
                f"Current labor cost is {rate_driver} versus the prior comparable period."
                if prior_summary.get("transaction_count")
                else "No prior comparable period is available yet."
            ),
            "next_step": "Use the department cost vs hours comparison to separate staffing volume from rate or premium pressure.",
        },
        {
            "title": "Staffing Stability",
            "tone": "warning" if top_watch.get("cost_volatility") and float(top_watch["cost_volatility"]) >= 0.2 else "success",
            "detail": (
                f"Highest observed department volatility is {float(top_watch.get('cost_volatility') or 0.0):.1%}."
                if top_watch
                else "No abnormal volatility detected in the current selection."
            ),
            "next_step": "Use worker concentration and cross-department coverage to assess whether staffing is fragile.",
        },
        {
            "title": "Management Focus",
            "tone": "primary",
            "detail": (
                f"Start with {top_watch.get('department_name')}, {top_category.get('time_category', 'the largest category')}, and {top_worker.get('employee_name', 'the top worker')}."
                if top_watch
                else "Use the department, category, and worker layers together to rank where cost, hours, and rate diverge."
            ),
            "next_step": "Follow chart and table clicks into scoped department, category, worker, or day views.",
        },
    ]
    return signals


def _build_watchlist(current_departments: pd.DataFrame) -> pd.DataFrame:
    if current_departments.empty:
        return pd.DataFrame()
    merged = current_departments.copy()
    flags = (
        merged["premium_share_pct"].fillna(0).ge(0.08)
        | merged["absence_share_pct"].fillna(0).ge(0.04)
        | merged["cost_delta_pct"].fillna(0).ge(0.12)
        | merged["cost_volatility"].fillna(0).ge(0.20)
    )
    watch = merged.loc[flags].copy()
    if watch.empty:
        return watch
    watch["risk_score"] = (
        watch["premium_share_pct"].fillna(0).abs()
        + watch["absence_share_pct"].fillna(0).abs()
        + watch["cost_delta_pct"].fillna(0).abs()
        + watch["cost_volatility"].fillna(0).abs()
        + pd.to_numeric(watch["recent_cost_acceleration_pct"], errors="coerce").fillna(0).abs()
    )
    watch = watch.sort_values(["risk_score", "labor_cost"], ascending=[False, False]).reset_index(drop=True)
    return watch


def _build_worker_watchlist(workers: pd.DataFrame) -> pd.DataFrame:
    if workers.empty:
        return workers
    frame = workers.copy()
    premium_share = pd.to_numeric(frame["premium_share_pct"], errors="coerce").fillna(0)
    absence_share = pd.to_numeric(frame["absence_share_pct"], errors="coerce").fillna(0)
    cost_delta = pd.to_numeric(frame["cost_delta_pct"], errors="coerce").fillna(0)
    labor_share = pd.to_numeric(frame["labor_cost_share_pct"], errors="coerce").fillna(0)
    flags = (
        premium_share.ge(0.08)
        | absence_share.ge(0.04)
        | frame["multi_department_flag"].fillna(False)
        | cost_delta.ge(0.20)
    )
    frame = frame.loc[flags].copy()
    if frame.empty:
        return frame
    frame["risk_score"] = premium_share.loc[frame.index].abs() + absence_share.loc[frame.index].abs() + cost_delta.loc[frame.index].abs() + labor_share.loc[frame.index].abs()
    return frame.sort_values(["risk_score", "labor_cost"], ascending=[False, False]).reset_index(drop=True)


def _build_category_watchlist(categories: pd.DataFrame) -> pd.DataFrame:
    if categories.empty:
        return categories
    frame = categories.copy()
    flags = (
        frame["cost_delta_pct"].fillna(0).ge(0.15)
        | frame["labor_cost_share_pct"].fillna(0).ge(0.12)
        | frame["leading_department_share_pct"].fillna(0).ge(0.60)
    )
    frame = frame.loc[flags].copy()
    if frame.empty:
        return frame
    frame["risk_score"] = (
        frame["cost_delta_pct"].fillna(0).abs()
        + frame["labor_cost_share_pct"].fillna(0).abs()
        + frame["leading_department_share_pct"].fillna(0).abs()
    )
    return frame.sort_values(["risk_score", "labor_cost"], ascending=[False, False]).reset_index(drop=True)


def _top_departments_for_trend(current_departments: pd.DataFrame, limit: int = 5) -> list[str]:
    if current_departments.empty:
        return []
    return current_departments.head(limit)["department_name"].astype(str).tolist()


def _top_categories_for_trend(current_categories: pd.DataFrame, limit: int = 5) -> list[str]:
    if current_categories.empty:
        return []
    return current_categories.head(limit)["time_category"].astype(str).tolist()


def _pick_focus_value(selected_values: Sequence[str], frame: pd.DataFrame, column: str, fallback_default: str) -> str | None:
    if selected_values:
        return str(selected_values[0])
    if frame.empty or column not in frame.columns:
        return None
    value = str(frame.iloc[0].get(column) or "").strip()
    return value or fallback_default


def _build_department_focus(
    filters: LaborFilters,
    department_name: str | None,
    departments: pd.DataFrame,
) -> dict[str, Any] | None:
    if not department_name:
        return None
    frame = departments.loc[departments["department_name"] == department_name].copy()
    if frame.empty:
        return None
    scoped = replace(filters, departments=(department_name,), page=1, sort_by="labor_cost", sort_dir="desc")
    daily_trend = _query_daily_trend(scoped)
    workers = _enrich_worker_summary(_query_worker_summary(scoped, limit=12), pd.DataFrame())
    categories = _query_category_mix(scoped).head(10)
    worker_watchlist = _build_worker_watchlist(workers)
    department_row = frame.iloc[0].to_dict()
    driver = _driver_label(department_row.get("cost_delta_pct"), department_row.get("hours_delta_pct"), department_row.get("rate_delta_pct"))
    interpretation = (
        f"{department_name} is currently {driver}. "
        f"Premium share is {department_row.get('premium_share_pct'):.1%} and absence share is {department_row.get('absence_share_pct'):.1%}."
        if department_row.get("premium_share_pct") is not None and department_row.get("absence_share_pct") is not None
        else f"{department_name} is currently {driver} under the active filters."
    )
    return {
        "department_name": department_name,
        "summary": department_row,
        "trend_rows": daily_trend.to_dict(orient="records"),
        "worker_rows": workers.head(12).to_dict(orient="records"),
        "category_rows": categories.to_dict(orient="records"),
        "watch_rows": worker_watchlist.head(8).to_dict(orient="records"),
        "interpretation": interpretation,
        "clear_url": build_url(filters, updates={"department": None}, include_pagination=False),
        "is_selected": department_name in filters.departments,
    }


def _build_category_focus(
    filters: LaborFilters,
    category_name: str | None,
    categories: pd.DataFrame,
) -> dict[str, Any] | None:
    if not category_name:
        return None
    frame = categories.loc[categories["time_category"] == category_name].copy()
    if frame.empty:
        return None
    scoped = replace(filters, time_categories=(category_name,), page=1, sort_by="labor_cost", sort_dir="desc")
    trend_rows = _query_daily_trend(scoped)
    department_rows = _query_department_summary(scoped).head(12)
    worker_rows = _enrich_worker_summary(_query_worker_summary(scoped, limit=12), pd.DataFrame())
    row = frame.iloc[0].to_dict()
    leading_department = _top_label(department_rows, "department_name", "the current selection")
    interpretation = (
        f"{category_name} is led by {leading_department}. "
        "Use this layer to see whether the category is concentrated in one department, widening across the business, or being driven by a small worker group."
    )
    return {
        "time_category": category_name,
        "summary": row,
        "trend_rows": trend_rows.to_dict(orient="records"),
        "department_rows": department_rows.to_dict(orient="records"),
        "worker_rows": worker_rows.to_dict(orient="records"),
        "interpretation": interpretation,
        "clear_url": build_url(filters, updates={"time_category": None}, include_pagination=False),
        "is_selected": category_name in filters.time_categories,
    }


def _build_worker_focus(
    filters: LaborFilters,
    employee_code: str | None,
    workers: pd.DataFrame,
) -> dict[str, Any] | None:
    if not employee_code:
        return None
    frame = workers.loc[workers["employee_code"] == employee_code].copy()
    if frame.empty:
        return None
    scoped = replace(filters, employees=(employee_code,), page=1, sort_by="labor_cost", sort_dir="desc")
    trend_rows = _query_daily_trend(scoped)
    department_rows = _query_department_summary(scoped).head(10)
    category_rows = _query_category_mix(scoped).head(10)
    row = frame.iloc[0].to_dict()
    interpretation = (
        f"{row.get('employee_name', 'This worker')} contributes {float(row.get('labor_cost_share_pct') or 0.0):.1%} of labor cost in the active selection. "
        "Review whether that exposure is spread across departments, concentrated in premium categories, or driven by recent absence."
    )
    return {
        "employee_code": employee_code,
        "employee_name": row.get("employee_name"),
        "summary": row,
        "trend_rows": trend_rows.to_dict(orient="records"),
        "department_rows": department_rows.to_dict(orient="records"),
        "category_rows": category_rows.to_dict(orient="records"),
        "interpretation": interpretation,
        "clear_url": build_url(filters, updates={"employee": None}, include_pagination=False),
        "is_selected": employee_code in filters.employees,
    }


def _build_actions(
    current_summary: Mapping[str, Any],
    department_watchlist: pd.DataFrame,
    worker_watchlist: pd.DataFrame,
    category_watchlist: pd.DataFrame,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if not department_watchlist.empty:
        top = department_watchlist.iloc[0]
        actions.append(
            {
                "title": f"Investigate {top.get('department_name')}",
                "detail": f"Department cost, volatility, premium share, or absence share is above peer levels under the current filters.",
                "scope": "department",
                "value": top.get("department_name"),
            }
        )
    if float(current_summary.get("premium_share_pct") or 0.0) >= 0.08:
        actions.append(
            {
                "title": "Review premium and overtime coverage",
                "detail": "Current premium share is elevated relative to total labor cost; confirm whether staffing gaps or work-rule usage are causing the pressure.",
                "scope": "category",
                "value": "Overtime",
            }
        )
    if not worker_watchlist.empty:
        top_worker = worker_watchlist.iloc[0]
        actions.append(
            {
                "title": f"Review {top_worker.get('employee_name')}",
                "detail": "Worker-level concentration, premium exposure, absence exposure, or cross-department coverage suggests management review.",
                "scope": "employee",
                "value": top_worker.get("employee_code"),
            }
        )
    if not category_watchlist.empty:
        top_category = category_watchlist.iloc[0]
        actions.append(
            {
                "title": f"Review {top_category.get('time_category')}",
                "detail": "Category growth, concentration, or category share suggests a policy or scheduling review before expanding headcount.",
                "scope": "category",
                "value": top_category.get("time_category"),
            }
        )
    if not actions:
        actions.append(
            {
                "title": "Maintain current staffing rhythm",
                "detail": "No major department, worker, or category watch signal is elevated in the active selection.",
                "scope": None,
                "value": None,
            }
        )
    return actions[:5]


def _build_narratives(
    current_summary: Mapping[str, Any],
    prior_summary: Mapping[str, Any],
    current_departments: pd.DataFrame,
    category_mix: pd.DataFrame,
    current_workers: pd.DataFrame,
    department_watchlist: pd.DataFrame,
) -> dict[str, str]:
    total_cost = float(current_summary.get("total_labor_cost") or 0.0)
    prior_cost = float(prior_summary.get("total_labor_cost") or 0.0)
    cost_delta = total_cost - prior_cost
    top_department = current_departments.iloc[0].to_dict() if not current_departments.empty else {}
    top_category = category_mix.iloc[0].to_dict() if not category_mix.empty else {}
    top_worker = current_workers.iloc[0].to_dict() if not current_workers.empty else {}
    top_watch = department_watchlist.iloc[0].to_dict() if not department_watchlist.empty else {}
    executive = "No labor rows matched the active filters."
    if total_cost > 0:
        direction = "rose" if cost_delta >= 0 else "fell"
        executive = (
            f"Labor cost {direction} to ${total_cost:,.0f}. "
            f"{top_department.get('department_name', 'The top department')} is carrying the largest share of cost, "
            f"{top_category.get('time_category', 'the top category')} is the dominant transaction mix, "
            f"and {top_worker.get('employee_name', 'the top worker')} is the biggest worker-level contributor."
        )
        if top_watch:
            executive += f" {top_watch.get('department_name')} shows the strongest current watch signal."
    department = (
        "Department rankings compare cost, hours, blended rate, premium share, absence share, volatility, and recent acceleration under the same filters. "
        "Use them to decide where staffing review should start, not just where cost is highest."
    )
    trend = (
        "Trend charts separate labor cost, hours, blended rate, and operating rhythm over time. "
        "When cost moves faster than hours, management should check rate, premium, or category mix before changing staffing levels."
    )
    composition = (
        "Composition shows which time categories are absorbing spend, which departments own those categories, and whether the mix is shifting. "
        "A shift toward premium, absence, sick, or vacation categories is usually more actionable than total cost alone."
    )
    watchlist = (
        "Watchlists surface departments, workers, and categories where premium share, absence share, concentration, volatility, or recent acceleration exceed the rest of the selection."
    )
    workspace = (
        "Use the workspace to trace the exact department, employee, and time category rows behind any signal or chart, then export the scoped detail if action is required."
    )
    workers = (
        "Worker views highlight who is driving cost, hours, premium exposure, absence exposure, and cross-department coverage. "
        "Use them to review assignment concentration and staffing resilience."
    )
    return {
        "executive": executive,
        "department": department,
        "trend": trend,
        "composition": composition,
        "watchlist": watchlist,
        "workspace": workspace,
        "workers": workers,
    }


def _serialize_filters(filters: LaborFilters) -> dict[str, Any]:
    return {
        "start": filters.start.isoformat(),
        "end": filters.end.isoformat(),
        "departments": list(filters.departments),
        "employees": list(filters.employees),
        "time_categories": list(filters.time_categories),
        "statuses": list(filters.statuses),
        "work_rules": list(filters.work_rules),
        "search": filters.search,
        "page": filters.page,
        "page_size": filters.page_size,
        "sort_by": filters.sort_by,
        "sort_dir": filters.sort_dir,
    }


def _query_args_from_filters(filters: LaborFilters, *, include_pagination: bool = True) -> list[tuple[str, str]]:
    args = [("start", filters.start.isoformat()), ("end", filters.end.isoformat())]
    for department in filters.departments:
        args.append(("department", department))
    for employee in filters.employees:
        args.append(("employee", employee))
    for category in filters.time_categories:
        args.append(("time_category", category))
    for status in filters.statuses:
        args.append(("status", status))
    for work_rule in filters.work_rules:
        args.append(("work_rule", work_rule))
    if filters.search:
        args.append(("search", filters.search))
    if include_pagination:
        args.append(("page", str(filters.page)))
        args.append(("page_size", str(filters.page_size)))
        args.append(("sort", filters.sort_by))
        args.append(("sort_dir", filters.sort_dir))
    return args


def build_url(filters: LaborFilters, *, updates: Mapping[str, Any] | None = None, include_pagination: bool = True) -> str:
    pairs = _query_args_from_filters(filters, include_pagination=include_pagination)
    params: dict[str, list[str]] = {}
    for key, value in pairs:
        params.setdefault(key, []).append(value)
    for key, value in (updates or {}).items():
        if value is None or value == "":
            params.pop(key, None)
            continue
        if isinstance(value, (list, tuple)):
            params[key] = [str(item) for item in value if item not in (None, "")]
        else:
            params[key] = [str(value)]
    encoded = urlencode([(key, item) for key, values in params.items() for item in values], doseq=True)
    return f"{url_for('labor.index')}?{encoded}" if encoded else url_for("labor.index")


def build_export_url(filters: LaborFilters, dataset: str, fmt: str) -> str:
    base = url_for("labor.export_dataset", dataset=dataset)
    encoded = urlencode(_query_args_from_filters(filters, include_pagination=False) + [("format", fmt)], doseq=True)
    return f"{base}?{encoded}"


def build_page_payload(args: Mapping[str, Any] | None = None) -> dict[str, Any]:
    status = labor_store.get_status()
    filters = resolve_filters(args)
    payload: dict[str, Any] = {
        "status": status.__dict__,
        "filters": _serialize_filters(filters),
        "filter_options": {"departments": [], "employees": [], "time_categories": [], "statuses": [], "work_rules": []},
        "kpis": {},
        "signals": [],
        "actions": [],
        "charts": {},
        "watchlist": {"rows": []},
        "watchlists": {"departments": [], "workers": [], "categories": []},
        "workspace": {"rows": [], "total_rows": 0, "page": filters.page, "page_size": filters.page_size, "total_pages": 1},
        "focus": {"department": None, "category": None, "worker": None},
        "messages": [],
        "has_results": False,
        "narratives": {},
        "export_urls": {
            "snapshot_xlsx": build_export_url(filters, "snapshot", "xlsx"),
            "detail_csv": build_export_url(filters, "detail", "csv"),
            "department_summary_xlsx": build_export_url(filters, "department-summary", "xlsx"),
            "category_summary_xlsx": build_export_url(filters, "category-summary", "xlsx"),
            "employee_summary_xlsx": build_export_url(filters, "employee-summary", "xlsx"),
            "watchlist_csv": build_export_url(filters, "watchlist", "csv"),
        },
    }
    if not status.available:
        payload["messages"].append(status.warning)
        return payload

    filter_options = _query_filter_options(filters)
    current_summary = _query_summary(filters)
    prior_start, prior_end = _prior_window(filters)
    prior_summary = _query_summary(filters, start=prior_start, end=prior_end)
    current_departments = _query_department_summary(filters)
    prior_departments = _query_department_summary(filters, start=prior_start, end=prior_end)
    department_daily = _query_department_daily(filters)
    current_departments = _enrich_department_summary(current_departments, prior_departments, department_daily)
    daily_trend = _query_daily_trend(filters)
    category_mix = _query_category_mix(filters)
    prior_category_mix = _query_category_mix(filters, start=prior_start, end=prior_end)
    department_category_mix = _query_department_category_mix(filters)
    category_mix = _enrich_category_summary(category_mix, prior_category_mix, department_category_mix)
    weekday_pattern = _query_weekday_pattern(filters)
    monthly_pattern = _query_monthly_pattern(filters)
    worker_summary = _query_worker_summary(filters, limit=400)
    prior_worker_summary = _query_worker_summary(filters, start=prior_start, end=prior_end, limit=2000)
    worker_summary = _enrich_worker_summary(worker_summary, prior_worker_summary)
    worker_department_mix = _query_worker_department_mix(filters)
    worker_category_mix = _query_worker_category_mix(filters)
    top_department_names = _top_departments_for_trend(current_departments)
    top_category_names = _top_categories_for_trend(category_mix)
    top_worker_codes = worker_summary.head(5)["employee_code"].astype(str).tolist() if not worker_summary.empty else []
    monthly_department_trend = _query_monthly_department_trend(filters, top_department_names)
    category_daily_trend = _query_category_daily_trend(filters, top_category_names)
    worker_daily_trend = _query_worker_daily_trend(filters, top_worker_codes)
    watchlist = _build_watchlist(current_departments)
    worker_watchlist = _build_worker_watchlist(worker_summary)
    category_watchlist = _build_category_watchlist(category_mix)
    workspace = _query_workspace(filters)
    workspace_payload = {key: value for key, value in workspace.items() if key != "frame"}
    narratives = _build_narratives(current_summary, prior_summary, current_departments, category_mix, worker_summary, watchlist)
    signals = _build_signals(filters, current_summary, prior_summary, current_departments, watchlist, category_mix, worker_summary)
    actions = _build_actions(current_summary, watchlist, worker_watchlist, category_watchlist)

    total_cost_delta_pct = _delta_pct(current_summary.get("total_labor_cost"), prior_summary.get("total_labor_cost"))
    total_hours_delta_pct = _delta_pct(current_summary.get("total_paid_hours"), prior_summary.get("total_paid_hours"))
    active_departments = int(current_summary.get("active_departments") or 0)
    active_employees = int(current_summary.get("active_employees") or 0)
    focus_department_name = _pick_focus_value(filters.departments, current_departments, "department_name", "Top department")
    focus_category_name = _pick_focus_value(filters.time_categories, category_mix, "time_category", "Top category")
    focus_worker_code = _pick_focus_value(filters.employees, worker_summary, "employee_code", "Top worker")
    department_focus = _build_department_focus(filters, focus_department_name, current_departments)
    category_focus = _build_category_focus(filters, focus_category_name, category_mix)
    worker_focus = _build_worker_focus(filters, focus_worker_code, worker_summary)
    top_department = current_departments.iloc[0].to_dict() if not current_departments.empty else {}
    top_category = category_mix.iloc[0].to_dict() if not category_mix.empty else {}
    top_worker = worker_summary.iloc[0].to_dict() if not worker_summary.empty else {}
    driver_label = _driver_label(total_cost_delta_pct, total_hours_delta_pct, _delta_pct(current_summary.get("blended_rate"), prior_summary.get("blended_rate")))

    kpis = {
        **current_summary,
        "labor_trend_vs_prior_pct": total_cost_delta_pct,
        "hours_trend_vs_prior_pct": total_hours_delta_pct,
        "blended_rate_vs_prior_pct": _delta_pct(current_summary.get("blended_rate"), prior_summary.get("blended_rate")),
        "prior_window_start": prior_start.isoformat(),
        "prior_window_end": prior_end.isoformat(),
    }

    payload.update(
        {
            "filter_options": filter_options,
            "kpis": kpis,
            "signals": signals,
            "actions": actions,
            "has_results": bool(current_summary.get("transaction_count")),
            "narratives": narratives,
            "hero": {
                "title": "Labor Intelligence",
                "subtitle": "Workforce, labor cost, premium, absence, and staffing pressure analysis from Synerion time transactions.",
                "purpose": "Use this page to understand what is happening, where labor pressure is building, who is driving it, and what management should review next.",
                "window_label": f"{filters.start.isoformat()} to {filters.end.isoformat()}",
                "active_departments": active_departments,
                "active_employees": active_employees,
                "total_labor_cost": current_summary.get("total_labor_cost"),
                "total_paid_hours": current_summary.get("total_paid_hours"),
                "driver_label": driver_label,
                "top_department": top_department.get("department_name"),
                "top_category": top_category.get("time_category"),
                "top_worker": top_worker.get("employee_name"),
                "executive_narrative": narratives.get("executive"),
                "last_refresh_utc": status.last_refresh_utc,
            },
            "charts": {
                "department_cost": _sort_for_json(current_departments, ["labor_cost", "department_name"], [False, True], limit=12),
                "department_hours": _sort_for_json(current_departments, ["paid_hours", "department_name"], [False, True], limit=12),
                "department_rate": _sort_for_json(current_departments, ["blended_rate", "department_name"], [False, True], limit=12),
                "department_premium": _sort_for_json(current_departments, ["premium_share_pct", "department_name"], [False, True], limit=12),
                "department_absence": _sort_for_json(current_departments, ["absence_share_pct", "department_name"], [False, True], limit=12),
                "department_volatility": _sort_for_json(current_departments, ["cost_volatility", "department_name"], [False, True], limit=12),
                "department_scatter": _sort_for_json(current_departments, ["labor_cost", "department_name"], [False, True], limit=15),
                "daily_trend": daily_trend.to_dict(orient="records"),
                "department_daily": department_daily.to_dict(orient="records"),
                "monthly_department_trend": monthly_department_trend.to_dict(orient="records"),
                "monthly_pattern": monthly_pattern.to_dict(orient="records"),
                "category_mix": category_mix.head(12).to_dict(orient="records"),
                "category_trend": category_daily_trend.to_dict(orient="records"),
                "department_category_mix": department_category_mix.to_dict(orient="records"),
                "weekday_pattern": weekday_pattern.to_dict(orient="records"),
                "worker_cost": _sort_for_json(worker_summary, ["labor_cost", "employee_name"], [False, True], limit=12),
                "worker_hours": _sort_for_json(worker_summary, ["paid_hours", "employee_name"], [False, True], limit=12),
                "worker_premium": _sort_for_json(worker_summary, ["premium_share_pct", "employee_name"], [False, True], limit=12),
                "worker_absence": _sort_for_json(worker_summary, ["absence_share_pct", "employee_name"], [False, True], limit=12),
                "worker_daily_trend": worker_daily_trend.to_dict(orient="records"),
                "worker_department_mix": worker_department_mix.to_dict(orient="records"),
                "worker_category_mix": worker_category_mix.to_dict(orient="records"),
            },
            "department_table": current_departments.head(15).to_dict(orient="records"),
            "category_table": category_mix.head(15).to_dict(orient="records"),
            "worker_table": worker_summary.head(20).to_dict(orient="records"),
            "watchlist": {"rows": watchlist.head(15).to_dict(orient="records")},
            "watchlists": {
                "departments": watchlist.head(12).to_dict(orient="records"),
                "workers": worker_watchlist.head(12).to_dict(orient="records"),
                "categories": category_watchlist.head(12).to_dict(orient="records"),
            },
            "focus": {
                "department": department_focus,
                "category": category_focus,
                "worker": worker_focus,
            },
            "workspace": workspace_payload,
        }
    )
    if current_departments.empty:
        payload["messages"].append("No labor rows matched the active filters.")
    return payload


def _rename_columns(frame: pd.DataFrame, mapping: Mapping[str, str]) -> pd.DataFrame:
    out = frame.copy()
    cols = [column for column in mapping if column in out.columns]
    if cols:
        out = out.loc[:, cols].rename(columns={column: mapping[column] for column in cols})
    return out


def _combined_watchlist_frame(
    department_watchlist: pd.DataFrame,
    worker_watchlist: pd.DataFrame,
    category_watchlist: pd.DataFrame,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    if not department_watchlist.empty:
        dept = department_watchlist.copy()
        dept["Scope"] = "Department"
        dept["Entity"] = dept["department_name"]
        dept["Focus"] = dept["management_focus"]
        frames.append(
            _rename_columns(
                dept,
                {
                    "Scope": "Scope",
                    "Entity": "Entity",
                    "Focus": "Focus",
                    "labor_cost": "Labor Cost",
                    "paid_hours": "Paid Hours",
                    "premium_share_pct": "Premium Share %",
                    "absence_share_pct": "Absence Share %",
                    "cost_delta_pct": "Cost Delta %",
                    "hours_delta_pct": "Hours Delta %",
                    "cost_volatility": "Volatility",
                    "recent_cost_acceleration_pct": "Recent Cost Acceleration %",
                },
            )
        )
    if not worker_watchlist.empty:
        workers = worker_watchlist.copy()
        workers["Scope"] = "Worker"
        workers["Entity"] = workers["employee_name"]
        workers["Focus"] = workers["management_focus"]
        frames.append(
            _rename_columns(
                workers,
                {
                    "Scope": "Scope",
                    "Entity": "Entity",
                    "Focus": "Focus",
                    "employee_code": "Employee Code",
                    "labor_cost": "Labor Cost",
                    "paid_hours": "Paid Hours",
                    "premium_share_pct": "Premium Share %",
                    "absence_share_pct": "Absence Share %",
                    "cost_delta_pct": "Cost Delta %",
                    "hours_delta_pct": "Hours Delta %",
                    "department_count": "Departments Worked",
                },
            )
        )
    if not category_watchlist.empty:
        categories = category_watchlist.copy()
        categories["Scope"] = "Category"
        categories["Entity"] = categories["time_category"]
        categories["Focus"] = categories["leading_department"]
        frames.append(
            _rename_columns(
                categories,
                {
                    "Scope": "Scope",
                    "Entity": "Entity",
                    "Focus": "Leading Department",
                    "labor_cost": "Labor Cost",
                    "paid_hours": "Paid Hours",
                    "labor_cost_share_pct": "Labor Cost Share %",
                    "paid_hours_share_pct": "Paid Hours Share %",
                    "cost_delta_pct": "Cost Delta %",
                    "leading_department_share_pct": "Leading Department Share %",
                },
            )
        )
    if not frames:
        return pd.DataFrame(columns=["Scope", "Entity", "Focus"])
    return pd.concat(frames, ignore_index=True, sort=False)


def build_export_frames(filters: LaborFilters, dataset: str) -> tuple[dict[str, pd.DataFrame], str]:
    current_summary = _query_summary(filters)
    prior_start, prior_end = _prior_window(filters)
    current_departments = _query_department_summary(filters)
    prior_departments = _query_department_summary(filters, start=prior_start, end=prior_end)
    department_daily = _query_department_daily(filters)
    current_departments = _enrich_department_summary(current_departments, prior_departments, department_daily)
    top_department_names = _top_departments_for_trend(current_departments)
    monthly_department_trend = _query_monthly_department_trend(filters, top_department_names)
    category_df = _enrich_category_summary(
        _query_category_mix(filters),
        _query_category_mix(filters, start=prior_start, end=prior_end),
        _query_department_category_mix(filters),
    )
    worker_df = _enrich_worker_summary(
        _query_worker_summary(filters, limit=2000),
        _query_worker_summary(filters, start=prior_start, end=prior_end, limit=2000),
    )
    department_watchlist_df = _build_watchlist(current_departments)
    worker_watchlist_df = _build_worker_watchlist(worker_df)
    category_watchlist_df = _build_category_watchlist(category_df)
    watchlist_df = _combined_watchlist_frame(department_watchlist_df, worker_watchlist_df, category_watchlist_df)
    detail_df = _query_workspace(filters, export_all=True)["frame"]
    trend_df = _query_daily_trend(filters)
    monthly_pattern_df = _query_monthly_pattern(filters)
    department_df = current_departments
    summary_df = pd.DataFrame(
        [
            {"Metric": "Window Start", "Value": filters.start.isoformat()},
            {"Metric": "Window End", "Value": filters.end.isoformat()},
            {
                "Metric": "Total Labor Cost",
                "Value": current_summary.get("total_labor_cost"),
            },
            {
                "Metric": "Total Paid Hours",
                "Value": current_summary.get("total_paid_hours"),
            },
            {
                "Metric": "Blended Rate",
                "Value": current_summary.get("blended_rate"),
            },
            {
                "Metric": "Premium Cost",
                "Value": current_summary.get("premium_cost"),
            },
            {
                "Metric": "Absence Cost",
                "Value": current_summary.get("absence_cost"),
            },
            {
                "Metric": "Premium Share %",
                "Value": current_summary.get("premium_share_pct"),
            },
            {
                "Metric": "Absence Share %",
                "Value": current_summary.get("absence_share_pct"),
            },
            {
                "Metric": "Active Employees",
                "Value": current_summary.get("active_employees"),
            },
            {
                "Metric": "Active Departments",
                "Value": current_summary.get("active_departments"),
            },
        ]
    )
    if dataset == "detail":
        return (
            {
                "LaborDetail": _rename_columns(
                    detail_df,
                    {
                        "labor_date": "Labor Date",
                        "department_name": "Department",
                        "employee_name": "Employee",
                        "employee_code": "Employee Code",
                        "time_category": "Time Category",
                        "labor_cost": "Labor Cost",
                        "paid_hours": "Paid Hours",
                        "effective_rate": "Effective Rate",
                        "status": "Status",
                        "work_rule": "Work Rule",
                        "is_premium": "Is Premium",
                        "is_absence": "Is Absence",
                        "is_memo": "Is Memo",
                    },
                )
            },
            "labor_detail",
        )
    if dataset == "department-summary":
        return (
            {
                "DepartmentSummary": _rename_columns(
                    department_df,
                    {
                        "department_name": "Department",
                        "department_number": "Department Number",
                        "labor_cost": "Labor Cost",
                        "paid_hours": "Paid Hours",
                        "blended_rate": "Blended Rate",
                        "premium_share_pct": "Premium Share %",
                        "absence_share_pct": "Absence Share %",
                        "labor_cost_share_pct": "Labor Cost Share %",
                        "paid_hours_share_pct": "Paid Hours Share %",
                        "cost_delta_pct": "Cost Delta %",
                        "hours_delta_pct": "Hours Delta %",
                        "rate_delta_pct": "Rate Delta %",
                        "cost_volatility": "Volatility",
                        "recent_cost_acceleration_pct": "Recent Cost Acceleration %",
                        "management_focus": "Management Focus",
                    },
                )
            },
            "labor_department_summary",
        )
    if dataset == "category-summary":
        return (
            {
                "CategorySummary": _rename_columns(
                    category_df,
                    {
                        "time_category": "Time Category",
                        "labor_cost": "Labor Cost",
                        "paid_hours": "Paid Hours",
                        "labor_cost_share_pct": "Labor Cost Share %",
                        "paid_hours_share_pct": "Paid Hours Share %",
                        "cost_delta_pct": "Cost Delta %",
                        "hours_delta_pct": "Hours Delta %",
                        "leading_department": "Leading Department",
                        "leading_department_share_pct": "Leading Department Share %",
                    },
                )
            },
            "labor_category_summary",
        )
    if dataset == "employee-summary":
        return (
            {
                "EmployeeSummary": _rename_columns(
                    worker_df,
                    {
                        "employee_name": "Employee",
                        "employee_code": "Employee Code",
                        "labor_cost": "Labor Cost",
                        "paid_hours": "Paid Hours",
                        "blended_rate": "Blended Rate",
                        "premium_share_pct": "Premium Share %",
                        "absence_share_pct": "Absence Share %",
                        "department_count": "Departments Worked",
                        "category_count": "Categories Used",
                        "cost_delta_pct": "Cost Delta %",
                        "hours_delta_pct": "Hours Delta %",
                        "management_focus": "Management Focus",
                    },
                )
            },
            "labor_employee_summary",
        )
    if dataset == "watchlist":
        return (
            {
                "Watchlist": watchlist_df,
                "DepartmentWatchlist": department_watchlist_df,
                "WorkerWatchlist": worker_watchlist_df,
                "CategoryWatchlist": category_watchlist_df,
            },
            "labor_watchlist",
        )
    return (
        {
            "Summary": summary_df,
            "Departments": department_df,
            "Workers": worker_df,
            "Categories": category_df,
            "Trend": trend_df,
            "MonthlyPattern": monthly_pattern_df,
            "DepartmentTrend": monthly_department_trend,
            "Watchlist": watchlist_df,
            "Detail": _rename_columns(
                detail_df,
                {
                    "labor_date": "Labor Date",
                    "department_name": "Department",
                    "employee_name": "Employee",
                    "employee_code": "Employee Code",
                    "time_category": "Time Category",
                    "labor_cost": "Labor Cost",
                    "paid_hours": "Paid Hours",
                    "effective_rate": "Effective Rate",
                    "status": "Status",
                    "work_rule": "Work Rule",
                },
            ),
        },
        "labor_snapshot",
    )


# === Labor Intelligence Enterprise Overrides ===

_LI_WATCH_THRESHOLD = 40.0
_LI_RISK_THRESHOLD = 67.0


def _li_query_worker_daily(filters: LaborFilters) -> pd.DataFrame:
    where_sql, params = build_where_clause(filters)
    sql = f"""
        SELECT
            labor_date,
            COALESCE(employee_code, '') AS employee_code,
            COALESCE(employee_name, employee_code, 'Unknown Employee') AS employee_name,
            COALESCE(SUM(labor_cost), 0) AS labor_cost,
            COALESCE(SUM(paid_hours_allocated), 0) AS paid_hours,
            COALESCE(SUM(CASE WHEN is_premium THEN labor_cost ELSE 0 END), 0) AS premium_cost,
            COALESCE(SUM(CASE WHEN is_absence THEN labor_cost ELSE 0 END), 0) AS absence_cost
        FROM labor_fact lf
        WHERE {where_sql}
        GROUP BY 1, 2, 3
        ORDER BY 2, 1
    """
    frame = _query_df(sql, params)
    if not frame.empty:
        frame["labor_date"] = pd.to_datetime(frame["labor_date"], errors="coerce").dt.date
    return frame


def _li_safe_float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return None
        return out
    except Exception:
        return None


def _li_safe_int(value: Any) -> int:
    try:
        if value is None or pd.isna(value):
            return 0
        return int(value)
    except Exception:
        return 0


def _li_ratio(numerator: Any, denominator: Any) -> float | None:
    num = _li_safe_float(numerator)
    den = _li_safe_float(denominator)
    if num is None or den in (None, 0.0):
        return None
    return num / den


def _li_delta_pct(current: Any, prior: Any) -> float | None:
    cur = _li_safe_float(current)
    prv = _li_safe_float(prior)
    if cur is None or prv in (None, 0.0):
        return None
    return (cur - prv) / prv


def _li_delta_points(current: Any, prior: Any) -> float | None:
    cur = _li_safe_float(current)
    prv = _li_safe_float(prior)
    if cur is None or prv is None:
        return None
    return cur - prv


def _li_component(value: Any, cap: float) -> float:
    if cap <= 0:
        return 0.0
    safe = max(_li_safe_float(value) or 0.0, 0.0)
    return min(safe, cap) / cap


def _li_clean_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    return frame.replace([math.inf, -math.inf], pd.NA)


def _li_format_currency(value: Any, default: str = "n/a") -> str:
    safe = _li_safe_float(value)
    if safe is None:
        return default
    return f"${safe:,.0f}" if abs(safe) >= 100 else f"${safe:,.2f}"


def _li_format_percent(value: Any, default: str = "n/a", *, points: bool = False) -> str:
    safe = _li_safe_float(value)
    if safe is None:
        return default
    if points:
        return f"{safe * 100:+.1f} pts"
    return f"{safe:+.1%}" if safe < 0 else f"{safe:.1%}"


def _li_format_number(value: Any, default: str = "n/a", decimals: int = 1) -> str:
    safe = _li_safe_float(value)
    if safe is None:
        return default
    fmt = f"{{:,.{decimals}f}}"
    return fmt.format(safe)


def _li_window_label(start: date, end: date) -> str:
    return f"{start.strftime('%b %-d, %Y')} to {end.strftime('%b %-d, %Y')}"


def _li_refresh_label(raw: Any) -> str | None:
    if raw in (None, ""):
        return None
    ts = pd.to_datetime(raw, errors='coerce', utc=True)
    if pd.isna(ts):
        return str(raw)
    return ts.strftime('%b %-d, %Y %H:%M UTC')


def _li_priority_tone(score: Any) -> str:
    safe = _li_safe_float(score) or 0.0
    if safe >= _LI_RISK_THRESHOLD:
        return 'danger'
    if safe >= _LI_WATCH_THRESHOLD:
        return 'warning'
    return 'success'


def _li_priority_posture(score: Any) -> str:
    safe = _li_safe_float(score) or 0.0
    if safe >= _LI_RISK_THRESHOLD:
        return 'Risk'
    if safe >= _LI_WATCH_THRESHOLD:
        return 'Watch'
    return 'Stable'


def _li_share_stats(frame: pd.DataFrame, value_col: str, *, top_n: int) -> dict[str, float | None]:
    if frame.empty or value_col not in frame.columns:
        return {'top_share_pct': None, 'top_n_share_pct': None, 'hhi': None}
    series = pd.to_numeric(frame[value_col], errors='coerce').fillna(0.0)
    total = float(series.sum() or 0.0)
    if total <= 0:
        return {'top_share_pct': None, 'top_n_share_pct': None, 'hhi': None}
    shares = (series / total).sort_values(ascending=False)
    return {
        'top_share_pct': float(shares.iloc[0]) if not shares.empty else None,
        'top_n_share_pct': float(shares.head(top_n).sum()) if not shares.empty else None,
        'hhi': float((shares ** 2).sum()) if not shares.empty else None,
    }


def _li_augment_trend(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    out = frame.copy()
    if 'labor_cost' in out.columns and 'paid_hours' in out.columns:
        out['blended_rate'] = pd.to_numeric(out['labor_cost'], errors='coerce') / pd.to_numeric(out['paid_hours'], errors='coerce')
        out.loc[pd.to_numeric(out['paid_hours'], errors='coerce').fillna(0).eq(0), 'blended_rate'] = pd.NA
    if 'premium_cost' in out.columns and 'labor_cost' in out.columns:
        out['premium_share_pct'] = pd.to_numeric(out['premium_cost'], errors='coerce') / pd.to_numeric(out['labor_cost'], errors='coerce')
        out.loc[pd.to_numeric(out['labor_cost'], errors='coerce').fillna(0).eq(0), 'premium_share_pct'] = pd.NA
    if 'absence_cost' in out.columns and 'labor_cost' in out.columns:
        out['absence_share_pct'] = pd.to_numeric(out['absence_cost'], errors='coerce') / pd.to_numeric(out['labor_cost'], errors='coerce')
        out.loc[pd.to_numeric(out['labor_cost'], errors='coerce').fillna(0).eq(0), 'absence_share_pct'] = pd.NA
    return _li_clean_frame(out)


def _li_augment_weekday_pattern(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    out = frame.copy()
    out['avg_blended_rate'] = pd.to_numeric(out['avg_daily_labor_cost'], errors='coerce') / pd.to_numeric(out['avg_daily_paid_hours'], errors='coerce')
    out.loc[pd.to_numeric(out['avg_daily_paid_hours'], errors='coerce').fillna(0).eq(0), 'avg_blended_rate'] = pd.NA
    return _li_clean_frame(out)


def _li_classify_category(category_name: Any, labor_cost: Any, paid_hours: Any) -> dict[str, Any]:
    label = str(category_name or 'Unclassified').strip() or 'Unclassified'
    lowered = label.lower()
    cost = _li_safe_float(labor_cost) or 0.0
    hours = _li_safe_float(paid_hours) or 0.0
    if cost > 0 and hours > 0:
        kind = 'Cost-bearing'
        note = 'Carries both paid hours and booked labor cost.'
    elif cost <= 0 and hours > 0:
        kind = 'Hours-only'
        note = 'Operationally relevant hours without booked labor cost.'
    elif cost > 0 and hours <= 0:
        kind = 'Cost-only'
        note = 'Booked labor cost without recorded paid hours.'
    else:
        kind = 'No activity'
        note = 'No booked cost or paid hours in the active scope.'
    category_class = 'other'
    if any(token in lowered for token in ('overtime', 'premium', 'ot')):
        category_class = 'premium'
    elif any(token in lowered for token in ('absence', 'sick', 'vacation', 'pto', 'leave', 'holiday')):
        category_class = 'absence'
    elif any(token in lowered for token in ('work from home', 'wfh', 'remote')):
        category_class = 'remote'
    elif any(token in lowered for token in ('regular', 'worked', 'base')):
        category_class = 'regular'
    return {
        'category_class': category_class,
        'category_kind': kind,
        'activity_note': note,
        'share_display_pct': None if (cost <= 0 and hours <= 0) else None,
        'share_display_label': 'Cost share' if cost > 0 else 'Hours share',
    }


def _li_department_management_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    components = {
        'cost': 22 * _li_component(row.get('cost_delta_pct'), 0.25),
        'hours': 12 * _li_component(row.get('hours_delta_pct'), 0.20),
        'rate': 12 * _li_component(row.get('rate_delta_pct'), 0.15),
        'premium': 16 * _li_component(row.get('premium_share_pct'), 0.12),
        'absence': 14 * _li_component(row.get('absence_share_pct'), 0.08),
        'volatility': 14 * _li_component(row.get('cost_volatility'), 0.30),
        'concentration': 10 * _li_component(row.get('labor_cost_share_pct'), 0.35),
    }
    priority_score = round(sum(components.values()), 1)
    dominant = max(components, key=components.get) if components else 'cost'
    focus = 'maintain staffing rhythm'
    reason = 'No single pressure source dominates under the active filters.'
    action = 'Open department investigation'
    if dominant == 'premium' and components[dominant] > 0:
        focus = 'trace premium exposure'
        reason = f"Premium share is {_li_format_percent(row.get('premium_share_pct'))} of department cost."
        action = 'Trace premium exposure'
    elif dominant == 'absence' and components[dominant] > 0:
        focus = 'review absence pattern'
        reason = f"Absence share is {_li_format_percent(row.get('absence_share_pct'))} of department cost."
        action = 'Inspect absence-heavy categories'
    elif dominant == 'rate' and components[dominant] > 0:
        focus = 'review rate pressure'
        reason = f"Blended rate is {_li_format_percent(row.get('rate_delta_pct'))} versus the prior comparable window."
        action = 'Compare cost vs blended rate'
    elif dominant == 'hours' and components[dominant] > 0:
        focus = 'review staffing volume'
        reason = f"Paid hours are {_li_format_percent(row.get('hours_delta_pct'))} versus the prior comparable window."
        action = 'Compare cost vs hours'
    elif dominant == 'volatility' and components[dominant] > 0:
        focus = 'stabilize staffing rhythm'
        reason = f"Daily labor cost volatility is {_li_format_percent(row.get('cost_volatility'))}."
        action = 'Review staffing stability'
    elif dominant == 'concentration' and components[dominant] > 0:
        focus = 'review department dependency'
        reason = f"Department carries {_li_format_percent(row.get('labor_cost_share_pct'))} of scoped labor cost."
        action = 'Open department investigation'
    elif components['cost'] > 0:
        focus = 'review labor acceleration'
        reason = f"Labor cost is {_li_format_percent(row.get('cost_delta_pct'))} versus the prior comparable window."
        action = 'Open department investigation'
    return {
        'priority_score': priority_score,
        'risk_posture': _li_priority_posture(priority_score),
        'risk_tone': _li_priority_tone(priority_score),
        'management_focus': focus,
        'focus_reason': reason,
        'action_label': action,
    }


def _li_worker_management_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    coverage_pressure = max((_li_safe_float(row.get('department_count')) or 0.0) - 1.0, 0.0)
    components = {
        'concentration': 24 * _li_component(row.get('labor_cost_share_pct'), 0.18),
        'premium': 16 * _li_component(row.get('premium_share_pct'), 0.12),
        'absence': 16 * _li_component(row.get('absence_share_pct'), 0.08),
        'growth': 14 * _li_component(row.get('cost_delta_pct'), 0.35),
        'coverage': 12 * _li_component(coverage_pressure, 3.0),
        'volatility': 10 * _li_component(row.get('cost_volatility'), 0.40),
        'recent': 8 * _li_component(row.get('recent_cost_acceleration_pct'), 0.35),
    }
    priority_score = round(sum(components.values()), 1)
    dominant = max(components, key=components.get) if components else 'concentration'
    focus = 'maintain current assignment'
    reason = 'No worker-level risk factor dominates under the active filters.'
    action = 'Open worker spotlight'
    if dominant == 'premium' and components[dominant] > 0:
        focus = 'trace premium exposure'
        reason = f"Premium share is {_li_format_percent(row.get('premium_share_pct'))} of this worker's labor cost."
        action = 'Trace premium exposure'
    elif dominant == 'absence' and components[dominant] > 0:
        focus = 'review absence pattern'
        reason = f"Absence share is {_li_format_percent(row.get('absence_share_pct'))} of this worker's labor cost."
        action = 'Review worker watchlist'
    elif dominant == 'coverage' and components[dominant] > 0:
        focus = 'review cross-department dependency'
        reason = f"Worker covers {_li_safe_int(row.get('department_count'))} departments in the active scope."
        action = 'Review cross-department coverage'
    elif dominant == 'volatility' and components[dominant] > 0:
        focus = 'review assignment consistency'
        reason = f"Daily labor cost volatility is {_li_format_percent(row.get('cost_volatility'))}."
        action = 'Review worker trend'
    elif dominant == 'recent' and components[dominant] > 0:
        focus = 'review recent labor acceleration'
        reason = f"Recent worker cost run-rate is {_li_format_percent(row.get('recent_cost_acceleration_pct'))} versus the earlier half of the window."
        action = 'Review worker trend'
    else:
        focus = 'review workload concentration'
        reason = f"Worker carries {_li_format_percent(row.get('labor_cost_share_pct'))} of scoped labor cost."
        action = 'Open worker spotlight'
    exposure_profile = 'Diversified'
    department_count = _li_safe_int(row.get('department_count'))
    labor_share = _li_safe_float(row.get('labor_cost_share_pct')) or 0.0
    if labor_share >= 0.15:
        exposure_profile = 'Highly concentrated'
    elif department_count >= 3:
        exposure_profile = 'Cross-department anchor'
    elif department_count == 2:
        exposure_profile = 'Cross-department'
    elif department_count <= 1:
        exposure_profile = 'Single-department'
    return {
        'priority_score': priority_score,
        'risk_posture': _li_priority_posture(priority_score),
        'risk_tone': _li_priority_tone(priority_score),
        'management_focus': focus,
        'focus_reason': reason,
        'action_label': action,
        'exposure_profile': exposure_profile,
    }


def _li_category_management_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    classification = _li_classify_category(row.get('time_category'), row.get('labor_cost'), row.get('paid_hours'))
    share_pct = row.get('labor_cost_share_pct') if (_li_safe_float(row.get('labor_cost')) or 0.0) > 0 else row.get('paid_hours_share_pct')
    anomaly = 1.0 if classification['category_kind'] in {'Hours-only', 'Cost-only'} else 0.0
    class_pressure = 1.0 if classification['category_class'] in {'premium', 'absence'} else 0.35
    components = {
        'share': 24 * _li_component(share_pct, 0.25),
        'growth': 18 * _li_component(row.get('cost_delta_pct'), 0.35),
        'hours': 10 * _li_component(row.get('hours_delta_pct'), 0.35),
        'concentration': 18 * _li_component(row.get('leading_department_share_pct'), 0.75),
        'class': 16 * class_pressure,
        'anomaly': 14 * anomaly,
    }
    priority_score = round(sum(components.values()), 1)
    dominant = max(components, key=components.get) if components else 'share'
    focus = 'review category mix'
    reason = classification['activity_note']
    action = 'Open category spotlight'
    if dominant == 'anomaly' and components[dominant] > 0:
        focus = 'review coding quality'
        reason = classification['activity_note']
        action = 'Inspect raw labor rows'
    elif classification['category_class'] == 'premium':
        focus = 'trace premium exposure'
        reason = f"{row.get('time_category')} is a premium category holding {_li_format_percent(share_pct)} of the scoped mix."
        action = 'Trace premium exposure'
    elif classification['category_class'] == 'absence':
        focus = 'review absence pattern'
        reason = f"{row.get('time_category')} is an absence category holding {_li_format_percent(share_pct)} of the scoped mix."
        action = 'Inspect absence-heavy categories'
    elif dominant == 'concentration' and components[dominant] > 0:
        focus = 'review department dependency'
        reason = f"{row.get('leading_department') or 'One department'} owns {_li_format_percent(row.get('leading_department_share_pct'))} of this category."
        action = 'Open category spotlight'
    elif dominant == 'growth' and components[dominant] > 0:
        focus = 'review category growth'
        reason = f"Category labor cost is {_li_format_percent(row.get('cost_delta_pct'))} versus the prior comparable window."
        action = 'Open category spotlight'
    return {
        'priority_score': priority_score,
        'risk_posture': _li_priority_posture(priority_score),
        'risk_tone': _li_priority_tone(priority_score),
        'management_focus': focus,
        'focus_reason': reason,
        'action_label': action,
        'category_class': classification['category_class'],
        'category_kind': classification['category_kind'],
        'activity_note': classification['activity_note'],
        'share_display_pct': share_pct,
        'share_display_label': classification['share_display_label'],
    }


def _li_enrich_department_summary(current_departments: pd.DataFrame, prior_departments: pd.DataFrame, department_daily: pd.DataFrame) -> pd.DataFrame:
    frame = _li_clean_frame(_enrich_department_summary(current_departments, prior_departments, department_daily))
    if frame.empty:
        return frame
    if not department_daily.empty:
        daily = department_daily.copy()
        daily['blended_rate'] = pd.to_numeric(daily['labor_cost'], errors='coerce') / pd.to_numeric(daily['paid_hours'], errors='coerce')
        daily.loc[pd.to_numeric(daily['paid_hours'], errors='coerce').fillna(0).eq(0), 'blended_rate'] = pd.NA
        agg = (
            daily.groupby('department_name', dropna=False)
            .agg(
                observation_days=('labor_date', 'nunique'),
                daily_rate_mean=('blended_rate', 'mean'),
                daily_rate_std=('blended_rate', 'std'),
            )
            .reset_index()
        )
        agg['rate_volatility'] = agg['daily_rate_std'] / agg['daily_rate_mean']
        agg.loc[agg['observation_days'].fillna(0).lt(3), 'rate_volatility'] = pd.NA
        frame = frame.merge(agg[['department_name', 'observation_days', 'rate_volatility']], on='department_name', how='left')
    else:
        frame['observation_days'] = pd.NA
        frame['rate_volatility'] = pd.NA
    records: list[dict[str, Any]] = []
    for row in frame.to_dict(orient='records'):
        enriched = dict(row)
        enriched.update(_li_department_management_fields(row))
        records.append(enriched)
    return _li_clean_frame(pd.DataFrame(records))


def _li_enrich_category_summary(current_categories: pd.DataFrame, prior_categories: pd.DataFrame, department_category_mix: pd.DataFrame) -> pd.DataFrame:
    frame = _li_clean_frame(_enrich_category_summary(current_categories, prior_categories, department_category_mix))
    if frame.empty:
        return frame
    records: list[dict[str, Any]] = []
    for row in frame.to_dict(orient='records'):
        enriched = dict(row)
        enriched.update(_li_category_management_fields(row))
        records.append(enriched)
    return _li_clean_frame(pd.DataFrame(records))


def _li_enrich_worker_summary(current_workers: pd.DataFrame, prior_workers: pd.DataFrame, worker_daily: pd.DataFrame) -> pd.DataFrame:
    frame = _li_clean_frame(_enrich_worker_summary(current_workers, prior_workers))
    if frame.empty:
        return frame
    if not worker_daily.empty:
        daily = worker_daily.copy()
        grouped = (
            daily.groupby('employee_code', dropna=False)
            .agg(
                observation_days=('labor_date', 'nunique'),
                daily_cost_mean=('labor_cost', 'mean'),
                daily_cost_std=('labor_cost', 'std'),
            )
            .reset_index()
        )
        grouped['cost_volatility'] = grouped['daily_cost_std'] / grouped['daily_cost_mean']
        grouped.loc[grouped['observation_days'].fillna(0).lt(3), 'cost_volatility'] = pd.NA
        accel_rows: list[dict[str, Any]] = []
        for employee_code, part in daily.groupby('employee_code', dropna=False):
            ordered = part.sort_values('labor_date')
            accel_rows.append(
                {
                    'employee_code': employee_code,
                    'recent_cost_acceleration_pct': _recent_change(pd.to_numeric(ordered['labor_cost'], errors='coerce')),
                }
            )
        accel = pd.DataFrame(accel_rows)
        frame = frame.merge(grouped[['employee_code', 'observation_days', 'cost_volatility']], on='employee_code', how='left')
        frame = frame.merge(accel, on='employee_code', how='left')
    else:
        frame['observation_days'] = pd.NA
        frame['cost_volatility'] = pd.NA
        frame['recent_cost_acceleration_pct'] = pd.NA
    records: list[dict[str, Any]] = []
    for row in frame.to_dict(orient='records'):
        enriched = dict(row)
        enriched.update(_li_worker_management_fields(row))
        records.append(enriched)
    return _li_clean_frame(pd.DataFrame(records))


def _li_scope_url(filters: LaborFilters, *, department: Any = None, employee: Any = None, time_category: Any = None, start: Any = None, end: Any = None) -> str:
    updates: dict[str, Any] = {}
    if department is not None:
        updates['department'] = department
    if employee is not None:
        updates['employee'] = employee
    if time_category is not None:
        updates['time_category'] = time_category
    if start is not None:
        updates['start'] = start
    if end is not None:
        updates['end'] = end
    return build_url(filters, updates=updates, include_pagination=False)


def _li_scope_filters_summary(filters: LaborFilters) -> str:
    parts: list[str] = []
    if filters.departments:
        parts.append(f"{len(filters.departments)} department{'s' if len(filters.departments) != 1 else ''}")
    if filters.employees:
        parts.append(f"{len(filters.employees)} worker{'s' if len(filters.employees) != 1 else ''}")
    if filters.time_categories:
        parts.append(f"{len(filters.time_categories)} categor{'ies' if len(filters.time_categories) != 1 else 'y'}")
    if filters.statuses:
        parts.append(f"{len(filters.statuses)} status filter{'s' if len(filters.statuses) != 1 else ''}")
    if filters.work_rules:
        parts.append(f"{len(filters.work_rules)} work rule{'s' if len(filters.work_rules) != 1 else ''}")
    if filters.search:
        parts.append(f"search \"{filters.search}\"")
    return ', '.join(parts) if parts else 'the full labor scope'


def _li_add_scope_urls(frame: pd.DataFrame, filters: LaborFilters, *, department_col: str | None = None, employee_col: str | None = None, category_col: str | None = None) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    out = frame.copy()
    if department_col and department_col in out.columns:
        out['department_scope_url'] = [
            _li_scope_url(filters, department=value) if value not in (None, '') else None
            for value in out[department_col].tolist()
        ]
    if employee_col and employee_col in out.columns:
        out['employee_scope_url'] = [
            _li_scope_url(filters, employee=value) if value not in (None, '') else None
            for value in out[employee_col].tolist()
        ]
    if category_col and category_col in out.columns:
        out['category_scope_url'] = [
            _li_scope_url(filters, time_category=value) if value not in (None, '') else None
            for value in out[category_col].tolist()
        ]
    return out


def _li_build_scope_summary(filters: LaborFilters, filter_options: Mapping[str, Any], prior_start: date, prior_end: date, current_summary: Mapping[str, Any], prior_summary: Mapping[str, Any]) -> dict[str, Any]:
    option_maps = {
        'departments': {opt['value']: opt['label'] for opt in filter_options.get('departments', [])},
        'employees': {opt['value']: opt['label'] for opt in filter_options.get('employees', [])},
        'time_categories': {opt['value']: opt['label'] for opt in filter_options.get('time_categories', [])},
        'statuses': {opt['value']: opt['label'] for opt in filter_options.get('statuses', [])},
        'work_rules': {opt['value']: opt['label'] for opt in filter_options.get('work_rules', [])},
    }
    chips: list[dict[str, Any]] = []

    def add_group(prefix: str, values: Sequence[str], key: str, attr_name: str, options_key: str) -> None:
        selected = list(values)
        for value in selected:
            remaining = tuple(item for item in selected if item != value)
            chips.append(
                {
                    'label': f"{prefix}: {option_maps[options_key].get(value, value)}",
                    'clear_url': build_url(filters, updates={key: remaining or None}, include_pagination=False),
                    'tone': 'info',
                }
            )

    add_group('Department', filters.departments, 'department', 'departments', 'departments')
    add_group('Worker', filters.employees, 'employee', 'employees', 'employees')
    add_group('Category', filters.time_categories, 'time_category', 'time_categories', 'time_categories')
    add_group('Status', filters.statuses, 'status', 'statuses', 'statuses')
    add_group('Work Rule', filters.work_rules, 'work_rule', 'work_rules', 'work_rules')
    if filters.search:
        chips.append(
            {
                'label': f"Search: {filters.search}",
                'clear_url': build_url(filters, updates={'search': None}, include_pagination=False),
                'tone': 'info',
            }
        )

    window_days = max(1, (filters.end - filters.start).days + 1)
    comparator_ready = _li_safe_int(prior_summary.get('transaction_count')) > 0
    comparator_note = (
        f"Comparing {_li_window_label(filters.start, filters.end)} against the immediately preceding {_li_window_label(prior_start, prior_end)} ({window_days} days each)."
        if comparator_ready
        else f"Current window {_li_window_label(filters.start, filters.end)} has no populated prior comparable window in {_li_window_label(prior_start, prior_end)}; delta metrics are shown as n/a."
    )
    trust_note = None
    if window_days <= 7:
        trust_note = 'Volatility and trend signals are based on a narrow window, so use them as watch signals rather than staffing verdicts.'
    elif _li_safe_int(current_summary.get('active_departments')) <= 1:
        trust_note = 'Concentration and stability metrics are being computed on a very narrow department scope.'
    return {
        'current_window_label': _li_window_label(filters.start, filters.end),
        'prior_window_label': _li_window_label(prior_start, prior_end),
        'comparator_note': comparator_note,
        'active_scope_label': _li_scope_filters_summary(filters),
        'chips': chips,
        'trust_note': trust_note,
    }


def _li_metric(label: str, value: Any, fmt: str, definition: str, *, tone: str = 'default', delta: Any = None, delta_fmt: str = 'percent', delta_label: str = 'vs prior comparable window', note: str | None = None) -> dict[str, Any]:
    return {
        'label': label,
        'value': value,
        'format': fmt,
        'definition': definition,
        'tone': tone,
        'delta': delta,
        'delta_format': delta_fmt,
        'delta_label': delta_label,
        'note': note,
    }


def _li_build_scorecard_groups(analysis: Mapping[str, Any]) -> list[dict[str, Any]]:
    current_summary = analysis['current_summary']
    prior_summary = analysis['prior_summary']
    overall = analysis['overall']
    premium_delta = _li_delta_pct(current_summary.get('premium_cost'), prior_summary.get('premium_cost'))
    absence_delta = _li_delta_pct(current_summary.get('absence_cost'), prior_summary.get('absence_cost'))
    return [
        {
            'title': 'Cost & Hours',
            'description': 'Core labor spend, paid-time volume, and blended-rate movement versus the prior comparable window.',
            'metrics': [
                _li_metric('Total Labor Cost', current_summary.get('total_labor_cost'), 'currency', 'Sum of labor_cost for all rows in scope.', tone='primary', delta=overall.get('cost_delta_pct')),
                _li_metric('Paid Hours', current_summary.get('total_paid_hours'), 'number', 'Sum of paid_hours_allocated for all rows in scope.', tone='primary', delta=overall.get('hours_delta_pct')),
                _li_metric('Blended Rate', current_summary.get('blended_rate'), 'currency', 'Total labor cost divided by total paid hours.', tone='primary', delta=overall.get('rate_delta_pct')),
                _li_metric('Cost Delta', overall.get('cost_delta_pct'), 'percent', 'Percent change in total labor cost versus the prior comparable window.', tone='warning', delta_label='comparator', delta=None, note='Suppressed when the prior window has no populated labor rows.'),
                _li_metric('Hours Delta', overall.get('hours_delta_pct'), 'percent', 'Percent change in paid hours versus the prior comparable window.', tone='warning', delta_label='comparator', delta=None, note='Helps separate staffing volume from rate pressure.'),
                _li_metric('Rate Delta', overall.get('rate_delta_pct'), 'percent', 'Percent change in blended rate versus the prior comparable window.', tone='warning', delta_label='comparator', delta=None, note='Useful when cost rises faster than hours.'),
            ],
        },
        {
            'title': 'Premium / Absence',
            'description': 'Premium and absence exposure, both in dollars and as a share of scoped labor cost.',
            'metrics': [
                _li_metric('Premium Cost', current_summary.get('premium_cost'), 'currency', 'Sum of labor_cost on rows flagged is_premium.', tone='warning', delta=premium_delta),
                _li_metric('Premium Share', current_summary.get('premium_share_pct'), 'percent', 'Premium cost divided by total labor cost.', tone='warning', delta=_li_delta_points(current_summary.get('premium_share_pct'), prior_summary.get('premium_share_pct')), delta_fmt='points'),
                _li_metric('Absence Cost', current_summary.get('absence_cost'), 'currency', 'Sum of labor_cost on rows flagged is_absence.', tone='danger', delta=absence_delta),
                _li_metric('Absence Share', current_summary.get('absence_share_pct'), 'percent', 'Absence cost divided by total labor cost.', tone='danger', delta=_li_delta_points(current_summary.get('absence_share_pct'), prior_summary.get('absence_share_pct')), delta_fmt='points'),
                _li_metric('Premium Delta', premium_delta, 'percent', 'Percent change in premium cost versus the prior comparable window.', tone='warning', delta_label='comparator', delta=None, note='A rising premium delta often signals coverage gaps or rule pressure.'),
                _li_metric('Absence Delta', absence_delta, 'percent', 'Percent change in absence cost versus the prior comparable window.', tone='danger', delta_label='comparator', delta=None, note='Use with worker and category watchlists to isolate repeated exposure.'),
            ],
        },
        {
            'title': 'Workforce Footprint',
            'description': 'How broad the scoped workforce is and how concentrated its labor cost has become.',
            'metrics': [
                _li_metric('Active Employees', current_summary.get('active_employees'), 'integer', 'Distinct employee_key values represented by the current filters.'),
                _li_metric('Active Departments', current_summary.get('active_departments'), 'integer', 'Distinct department_key values represented by the current filters.'),
                _li_metric('Transaction Rows', current_summary.get('transaction_count'), 'integer', 'Underlying labor transaction rows included by the active filters.'),
                _li_metric('Worker Concentration', overall.get('worker_concentration_top5_share'), 'percent', 'Share of scoped labor cost carried by the top five workers.', tone='warning'),
                _li_metric('Department Concentration', overall.get('department_concentration_top3_share'), 'percent', 'Share of scoped labor cost carried by the top three departments.', tone='warning'),
            ],
        },
        {
            'title': 'Operational Pressure',
            'description': 'Stability and concentration indicators that signal where management should review staffing first.',
            'metrics': [
                _li_metric('Staffing Stability Score', overall.get('stability_score'), 'score', '100 is most stable. Derived from average department daily labor-cost volatility.', tone='primary', note='Higher is better.'),
                _li_metric('Top Department Share', overall.get('top_department_share_pct'), 'percent', 'Largest single department share of scoped labor cost.', tone='warning'),
                _li_metric('Top Worker Share', overall.get('top_worker_share_pct'), 'percent', 'Largest single worker share of scoped labor cost.', tone='warning'),
                _li_metric('Workload Concentration', overall.get('worker_concentration_top3_share'), 'percent', 'Share of scoped labor cost carried by the top three workers.', tone='warning'),
                _li_metric('Category Concentration', overall.get('category_concentration_top3_share'), 'percent', 'Share of the scoped category mix carried by the top three time categories.', tone='warning'),
            ],
        },
    ]


def _li_signal(title: str, tone: str, what_changed: str, why_it_matters: str, inspect_next: str, action_label: str | None, action_url: str | None) -> dict[str, Any]:
    return {
        'title': title,
        'tone': tone,
        'what_changed': what_changed,
        'why_it_matters': why_it_matters,
        'inspect_next': inspect_next,
        'action_label': action_label,
        'action_url': action_url,
    }


def _li_build_signals(filters: LaborFilters, analysis: Mapping[str, Any]) -> list[dict[str, Any]]:
    current_summary = analysis['current_summary']
    overall = analysis['overall']
    departments = analysis['current_departments']
    workers = analysis['worker_summary']
    categories = analysis['category_mix']
    review_department = analysis.get('review_department') or {}
    review_worker = analysis.get('review_worker') or {}
    review_category = analysis.get('review_category') or {}
    premium_dept = departments.sort_values(['premium_share_pct', 'labor_cost'], ascending=[False, False]).head(1).to_dict(orient='records')
    absence_dept = departments.sort_values(['absence_share_pct', 'labor_cost'], ascending=[False, False]).head(1).to_dict(orient='records')
    unstable_dept = departments.sort_values(['cost_volatility', 'priority_score'], ascending=[False, False]).head(1).to_dict(orient='records')
    premium_target = premium_dept[0] if premium_dept else {}
    absence_target = absence_dept[0] if absence_dept else {}
    unstable_target = unstable_dept[0] if unstable_dept else {}
    driver_label = str(overall.get('driver_label') or 'stable')
    rate_tone = 'danger' if driver_label == 'rate-driven' and (_li_safe_float(overall.get('cost_delta_pct')) or 0.0) > 0 else 'success'
    hours_tone = 'warning' if driver_label == 'hours-driven' and (_li_safe_float(overall.get('cost_delta_pct')) or 0.0) > 0 else 'success'
    return [
        _li_signal(
            'Department Labor Pressure',
            review_department.get('risk_tone', 'success') if review_department else 'success',
            f"{review_department.get('department_name', 'No department')} holds {_li_format_percent(review_department.get('labor_cost_share_pct'))} of scoped labor cost with {_li_format_percent(review_department.get('cost_delta_pct'))} cost change versus the prior window.",
            review_department.get('focus_reason', 'No department pressure signal is elevated under the active filters.'),
            'Open the department investigation layer to review workers, category mix, and recent trend before changing staffing.',
            review_department.get('action_label') if review_department else None,
            review_department.get('department_scope_url') if review_department else None,
        ),
        _li_signal(
            'Premium Risk',
            'warning' if (_li_safe_float(current_summary.get('premium_share_pct')) or 0.0) >= 0.08 else 'success',
            f"Premium cost is {_li_format_currency(current_summary.get('premium_cost'))}, or {_li_format_percent(current_summary.get('premium_share_pct'))} of scoped labor cost.",
            premium_target.get('focus_reason', 'Premium exposure is not elevated in the current scope.'),
            'Inspect the most premium-heavy department, then review the worker watchlist before expanding headcount.',
            'Trace premium exposure' if premium_target else None,
            premium_target.get('department_scope_url') if premium_target else None,
        ),
        _li_signal(
            'Absence Watch',
            'danger' if (_li_safe_float(current_summary.get('absence_share_pct')) or 0.0) >= 0.04 else 'success',
            f"Absence cost is {_li_format_currency(current_summary.get('absence_cost'))}, or {_li_format_percent(current_summary.get('absence_share_pct'))} of scoped labor cost.",
            absence_target.get('focus_reason', 'Absence pressure is not elevated in the current scope.'),
            'Inspect the absence-heavy department, then review worker and category rows to see whether the pattern is concentrated.',
            'Inspect absence-heavy categories' if absence_target else None,
            absence_target.get('department_scope_url') if absence_target else None,
        ),
        _li_signal(
            'Rate-Driven Cost Acceleration',
            rate_tone,
            f"Blended rate moved {_li_format_percent(overall.get('rate_delta_pct'))} while paid hours moved {_li_format_percent(overall.get('hours_delta_pct'))} versus the prior comparable window.",
            'When rate moves faster than hours, cost pressure is more likely tied to premium, work-rule mix, or higher-priced categories than to raw staffing volume.' if driver_label == 'rate-driven' else 'Rate is not the primary cost driver in the current scope.',
            'Use the rate trend, department table, and premium watchlist together before changing schedules.',
            'Compare cost vs blended rate',
            build_url(filters, include_pagination=False),
        ),
        _li_signal(
            'Hours-Driven Cost Acceleration',
            hours_tone,
            f"Paid hours moved {_li_format_percent(overall.get('hours_delta_pct'))} while total labor cost moved {_li_format_percent(overall.get('cost_delta_pct'))} versus the prior comparable window.",
            'When hours move first, staffing volume or operating demand is likely driving labor cost.' if driver_label == 'hours-driven' else 'Hours are not the primary cost driver in the current scope.',
            'Use the department scatter and daily trend to see whether hours pressure is concentrated in one team or spread across the operation.',
            'Compare cost vs hours',
            build_url(filters, include_pagination=False),
        ),
        _li_signal(
            'Staffing Instability',
            unstable_target.get('risk_tone', 'success') if unstable_target else 'success',
            f"The highest department volatility in scope is {_li_format_percent(unstable_target.get('cost_volatility'))} in {unstable_target.get('department_name', 'the current scope')}.",
            unstable_target.get('focus_reason', 'No unstable department is standing out in the current scope.'),
            'Inspect the unstable department trend and compare whether pressure is coming from hours, rate, premium, or absence.',
            'Review staffing stability' if unstable_target else None,
            unstable_target.get('department_scope_url') if unstable_target else None,
        ),
        _li_signal(
            'Worker Concentration Risk',
            'warning' if (_li_safe_float(overall.get('top_worker_share_pct')) or 0.0) >= 0.12 else 'success',
            f"The top worker carries {_li_format_percent(overall.get('top_worker_share_pct'))} of scoped labor cost, and the top three workers carry {_li_format_percent(overall.get('worker_concentration_top3_share'))}.",
            review_worker.get('focus_reason', 'No worker concentration signal is elevated in the current scope.'),
            'Open the worker spotlight to review premium, absence, and cross-department coverage before reassigning work.',
            review_worker.get('action_label') if review_worker else None,
            review_worker.get('employee_scope_url') if review_worker else None,
        ),
        _li_signal(
            'Category Mix Pressure',
            review_category.get('risk_tone', 'success') if review_category else 'success',
            f"{review_category.get('time_category', 'No category')} holds {_li_format_percent(review_category.get('share_display_pct'))} of the scoped mix on a {review_category.get('share_display_label', 'mix')} basis.",
            review_category.get('focus_reason', 'No category mix signal is elevated in the current scope.'),
            'Open the category spotlight to see which department owns the mix shift and whether the category is premium, absence, or a coding anomaly.',
            review_category.get('action_label') if review_category else None,
            review_category.get('category_scope_url') if review_category else None,
        ),
        _li_signal(
            'Review-First Department',
            'primary',
            f"Start with {review_department.get('department_name', 'the top department')} because it is the highest-priority department under the current scope.",
            review_department.get('focus_reason', 'Use the department table to choose the first operational review.'),
            'Open the department investigation layer and stay within the current filter scope.',
            'Open department investigation' if review_department else None,
            review_department.get('department_scope_url') if review_department else None,
        ),
        _li_signal(
            'Review-First Worker',
            'primary',
            f"Start with {review_worker.get('employee_name', 'the top worker')} because this worker is the highest-priority individual exposure under the current scope.",
            review_worker.get('focus_reason', 'Use the worker table to choose the first worker review.'),
            'Open the worker spotlight to review cost, hours, category breadth, and cross-department coverage.',
            'Review worker watchlist' if review_worker else None,
            review_worker.get('employee_scope_url') if review_worker else None,
        ),
        _li_signal(
            'Review-First Category',
            'primary',
            f"Start with {review_category.get('time_category', 'the top category')} because it is the highest-priority category under the current scope.",
            review_category.get('focus_reason', 'Use the category table to choose the first category review.'),
            'Open the category spotlight to review mix ownership, leading department, and worker contribution.',
            'Review staffing mix' if review_category else None,
            review_category.get('category_scope_url') if review_category else None,
        ),
    ]


def _li_watch_row(entity: str, scope_label: str, reason: str, magnitude: str, posture: str, action_label: str | None, action_url: str | None, tone: str) -> dict[str, Any]:
    return {
        'entity': entity,
        'scope_label': scope_label,
        'reason': reason,
        'magnitude': magnitude,
        'risk_posture': posture,
        'action_label': action_label,
        'action_url': action_url,
        'tone': tone,
    }


def _li_build_watchlist_sections(analysis: Mapping[str, Any]) -> list[dict[str, Any]]:
    departments = analysis['department_watchlist']
    workers = analysis['worker_watchlist']
    categories = analysis['category_watchlist']
    premium_rows = []
    absence_rows = []
    concentration_rows = []
    unstable_rows = []
    for row in departments.sort_values(['premium_share_pct', 'priority_score'], ascending=[False, False]).head(4).to_dict(orient='records'):
        if (_li_safe_float(row.get('premium_share_pct')) or 0.0) >= 0.06:
            premium_rows.append(_li_watch_row(row.get('department_name') or 'Unknown', 'Department', row.get('focus_reason') or 'Premium exposure is elevated.', f"{_li_format_percent(row.get('premium_share_pct'))} premium share | {_li_format_currency(row.get('labor_cost'))}", row.get('risk_posture') or 'Watch', 'Trace premium exposure', row.get('department_scope_url'), row.get('risk_tone') or 'warning'))
    for row in workers.sort_values(['premium_share_pct', 'priority_score'], ascending=[False, False]).head(4).to_dict(orient='records'):
        if (_li_safe_float(row.get('premium_share_pct')) or 0.0) >= 0.06:
            premium_rows.append(_li_watch_row(row.get('employee_name') or 'Unknown', 'Worker', row.get('focus_reason') or 'Premium exposure is elevated.', f"{_li_format_percent(row.get('premium_share_pct'))} premium share | {_li_format_currency(row.get('labor_cost'))}", row.get('risk_posture') or 'Watch', 'Review worker watchlist', row.get('employee_scope_url'), row.get('risk_tone') or 'warning'))
    for row in departments.sort_values(['absence_share_pct', 'priority_score'], ascending=[False, False]).head(4).to_dict(orient='records'):
        if (_li_safe_float(row.get('absence_share_pct')) or 0.0) >= 0.03:
            absence_rows.append(_li_watch_row(row.get('department_name') or 'Unknown', 'Department', row.get('focus_reason') or 'Absence exposure is elevated.', f"{_li_format_percent(row.get('absence_share_pct'))} absence share | {_li_format_currency(row.get('labor_cost'))}", row.get('risk_posture') or 'Watch', 'Inspect absence-heavy categories', row.get('department_scope_url'), row.get('risk_tone') or 'warning'))
    for row in workers.sort_values(['absence_share_pct', 'priority_score'], ascending=[False, False]).head(4).to_dict(orient='records'):
        if (_li_safe_float(row.get('absence_share_pct')) or 0.0) >= 0.03:
            absence_rows.append(_li_watch_row(row.get('employee_name') or 'Unknown', 'Worker', row.get('focus_reason') or 'Absence exposure is elevated.', f"{_li_format_percent(row.get('absence_share_pct'))} absence share | {_li_format_currency(row.get('labor_cost'))}", row.get('risk_posture') or 'Watch', 'Review worker watchlist', row.get('employee_scope_url'), row.get('risk_tone') or 'warning'))
    for row in departments.sort_values(['labor_cost_share_pct', 'priority_score'], ascending=[False, False]).head(3).to_dict(orient='records'):
        if (_li_safe_float(row.get('labor_cost_share_pct')) or 0.0) >= 0.18:
            concentration_rows.append(_li_watch_row(row.get('department_name') or 'Unknown', 'Department', 'Department labor cost share is concentrated.', f"{_li_format_percent(row.get('labor_cost_share_pct'))} of scoped labor cost", row.get('risk_posture') or 'Watch', 'Open department investigation', row.get('department_scope_url'), row.get('risk_tone') or 'warning'))
    for row in workers.sort_values(['labor_cost_share_pct', 'priority_score'], ascending=[False, False]).head(3).to_dict(orient='records'):
        if (_li_safe_float(row.get('labor_cost_share_pct')) or 0.0) >= 0.12:
            concentration_rows.append(_li_watch_row(row.get('employee_name') or 'Unknown', 'Worker', 'Worker labor cost share is concentrated.', f"{_li_format_percent(row.get('labor_cost_share_pct'))} of scoped labor cost", row.get('risk_posture') or 'Watch', 'Open worker spotlight', row.get('employee_scope_url'), row.get('risk_tone') or 'warning'))
    for row in categories.sort_values(['leading_department_share_pct', 'priority_score'], ascending=[False, False]).head(3).to_dict(orient='records'):
        if (_li_safe_float(row.get('leading_department_share_pct')) or 0.0) >= 0.60:
            concentration_rows.append(_li_watch_row(row.get('time_category') or 'Unknown', 'Category', row.get('focus_reason') or 'Category is concentrated in one department.', f"{_li_format_percent(row.get('leading_department_share_pct'))} led by {row.get('leading_department') or 'one department'}", row.get('risk_posture') or 'Watch', 'Open category spotlight', row.get('category_scope_url'), row.get('risk_tone') or 'warning'))
    for row in analysis['current_departments'].sort_values(['cost_volatility', 'priority_score'], ascending=[False, False]).head(5).to_dict(orient='records'):
        if (_li_safe_float(row.get('cost_volatility')) or 0.0) >= 0.18:
            unstable_rows.append(_li_watch_row(row.get('department_name') or 'Unknown', 'Department', row.get('focus_reason') or 'Department volatility is elevated.', f"{_li_format_percent(row.get('cost_volatility'))} daily cost volatility", row.get('risk_posture') or 'Watch', 'Review staffing stability', row.get('department_scope_url'), row.get('risk_tone') or 'warning'))
    sections = [
        {
            'title': 'Department Watchlist',
            'subtitle': 'Departments that should be reviewed first under the active scope.',
            'empty_message': 'No departments are breaching the current watch thresholds.',
            'rows': [
                _li_watch_row(row.get('department_name') or 'Unknown', 'Department', row.get('focus_reason') or 'Department review is warranted.', f"{_li_format_currency(row.get('labor_cost'))} | {_li_format_percent(row.get('cost_delta_pct'))} cost delta", row.get('risk_posture') or 'Watch', row.get('action_label'), row.get('department_scope_url'), row.get('risk_tone') or 'warning')
                for row in departments.head(5).to_dict(orient='records')
            ],
        },
        {
            'title': 'Worker Watchlist',
            'subtitle': 'Workers driving concentrated exposure, premium, absence, or cross-department dependency.',
            'empty_message': 'No workers are breaching the current watch thresholds.',
            'rows': [
                _li_watch_row(row.get('employee_name') or 'Unknown', 'Worker', row.get('focus_reason') or 'Worker review is warranted.', f"{_li_format_currency(row.get('labor_cost'))} | {_li_format_percent(row.get('labor_cost_share_pct'))} of scoped cost", row.get('risk_posture') or 'Watch', row.get('action_label'), row.get('employee_scope_url'), row.get('risk_tone') or 'warning')
                for row in workers.head(5).to_dict(orient='records')
            ],
        },
        {
            'title': 'Category Watchlist',
            'subtitle': 'Categories absorbing spend, shifting mix, or showing coding / ownership concerns.',
            'empty_message': 'No categories are breaching the current watch thresholds.',
            'rows': [
                _li_watch_row(row.get('time_category') or 'Unknown', 'Category', row.get('focus_reason') or 'Category review is warranted.', f"{_li_format_currency(row.get('labor_cost'))} | {_li_format_percent(row.get('share_display_pct'))} on a {str(row.get('share_display_label') or 'mix').lower()} basis", row.get('risk_posture') or 'Watch', row.get('action_label'), row.get('category_scope_url'), row.get('risk_tone') or 'warning')
                for row in categories.head(5).to_dict(orient='records')
            ],
        },
        {
            'title': 'Premium Watchlist',
            'subtitle': 'Departments and workers where premium is consuming an outsized share of labor cost.',
            'empty_message': 'No premium-heavy departments or workers are standing out under the current filters.',
            'rows': premium_rows[:5],
        },
        {
            'title': 'Absence Watchlist',
            'subtitle': 'Departments and workers where absence is creating meaningful cost exposure.',
            'empty_message': 'No absence-heavy departments or workers are standing out under the current filters.',
            'rows': absence_rows[:5],
        },
        {
            'title': 'Concentration Watchlist',
            'subtitle': 'Where labor cost or category ownership is concentrated in too few departments, workers, or categories.',
            'empty_message': 'No concentration risk is breaching the current watch thresholds.',
            'rows': concentration_rows[:6],
        },
        {
            'title': 'Unstable Department Watchlist',
            'subtitle': 'Departments with the least stable recent staffing rhythm.',
            'empty_message': 'No unstable departments are breaching the current watch thresholds.',
            'rows': unstable_rows[:5],
        },
    ]
    return sections


def _li_build_narratives(analysis: Mapping[str, Any]) -> dict[str, str]:
    current_summary = analysis['current_summary']
    overall = analysis['overall']
    department = analysis.get('review_department') or {}
    worker = analysis.get('review_worker') or {}
    category = analysis.get('review_category') or {}
    total_cost = _li_safe_float(current_summary.get('total_labor_cost')) or 0.0
    if total_cost <= 0:
        executive = 'No labor rows matched the active filters.'
    else:
        executive = (
            f"Labor cost is {_li_format_currency(total_cost)} across {_li_format_number(current_summary.get('total_paid_hours'))} paid hours. "
            f"The current move is {str(overall.get('driver_label') or 'stable').replace('-', ' ')}, "
            f"with {department.get('department_name', 'the top department')} as the first department to review, "
            f"{category.get('time_category', 'the top category')} as the first category to review, "
            f"and {worker.get('employee_name', 'the top worker')} as the first worker to review."
        )
    return {
        'executive': executive,
        'department': 'The department layer ranks where management attention should start by combining labor size, cost change, premium / absence exposure, and staffing stability.',
        'composition': 'The category layer shows whether spend is sitting in normal worked time, premium time, absence, or operational-only categories that need coding review.',
        'workers': 'The worker layer turns labor from a department summary into an assignment decision by exposing concentration, coverage breadth, premium pressure, and absence repetition.',
        'trend': 'The rhythm layer separates cost, hours, rate, premium, and absence over time so managers can tell whether the problem is scheduling volume, pricing, or mix.',
        'watchlist': 'The action center is organized around review-first entities, not raw totals, so leaders can move directly into the scoped drill that explains the signal.',
        'workspace': 'The exploration workspace is the trace-back layer for managers, HR, and finance stakeholders who need the raw labor rows behind any signal.',
    }


def _li_build_analysis(filters: LaborFilters) -> dict[str, Any]:
    prior_start, prior_end = _prior_window(filters)
    filter_options = _query_filter_options(filters)
    current_summary = _query_summary(filters)
    prior_summary = _query_summary(filters, start=prior_start, end=prior_end)
    department_daily = _query_department_daily(filters)
    current_departments = _li_enrich_department_summary(
        _query_department_summary(filters),
        _query_department_summary(filters, start=prior_start, end=prior_end),
        department_daily,
    )
    category_mix = _li_enrich_category_summary(
        _query_category_mix(filters),
        _query_category_mix(filters, start=prior_start, end=prior_end),
        _query_department_category_mix(filters),
    )
    worker_daily = _li_query_worker_daily(filters)
    worker_summary = _li_enrich_worker_summary(
        _query_worker_summary(filters, limit=400),
        _query_worker_summary(filters, start=prior_start, end=prior_end, limit=2000),
        worker_daily,
    )
    daily_trend = _li_augment_trend(_query_daily_trend(filters))
    weekday_pattern = _li_augment_weekday_pattern(_query_weekday_pattern(filters))
    monthly_pattern = _li_augment_trend(_query_monthly_pattern(filters))

    current_departments = _li_add_scope_urls(current_departments, filters, department_col='department_name')
    category_mix = _li_add_scope_urls(category_mix, filters, category_col='time_category')
    worker_summary = _li_add_scope_urls(worker_summary, filters, employee_col='employee_code')

    dept_priority = current_departments.sort_values(['priority_score', 'labor_cost'], ascending=[False, False]) if not current_departments.empty else current_departments
    category_priority = category_mix.sort_values(['priority_score', 'labor_cost'], ascending=[False, False]) if not category_mix.empty else category_mix
    worker_priority = worker_summary.sort_values(['priority_score', 'labor_cost'], ascending=[False, False]) if not worker_summary.empty else worker_summary

    review_department = dept_priority.head(1).to_dict(orient='records')
    review_worker = worker_priority.head(1).to_dict(orient='records')
    review_category = category_priority.head(1).to_dict(orient='records')

    top_department_names = dept_priority.head(5)['department_name'].astype(str).tolist() if not dept_priority.empty else []
    top_category_names = category_priority.head(5)['time_category'].astype(str).tolist() if not category_priority.empty else []
    top_worker_codes = worker_priority.head(5)['employee_code'].astype(str).tolist() if not worker_priority.empty else []

    monthly_department_trend = _li_clean_frame(_query_monthly_department_trend(filters, top_department_names))
    category_daily_trend = _li_augment_trend(_query_category_daily_trend(filters, top_category_names))
    worker_daily_trend = _li_augment_trend(_query_worker_daily_trend(filters, top_worker_codes))

    category_basis = 'labor_cost' if (_li_safe_float(current_summary.get('total_labor_cost')) or 0.0) > 0 else 'paid_hours'
    department_shares = _li_share_stats(current_departments, 'labor_cost', top_n=3)
    worker_top3 = _li_share_stats(worker_summary, 'labor_cost', top_n=3)
    worker_top5 = _li_share_stats(worker_summary, 'labor_cost', top_n=5)
    category_top3 = _li_share_stats(category_mix, category_basis, top_n=3)
    stability_candidates = pd.to_numeric(current_departments.get('cost_volatility', pd.Series(dtype='float64')), errors='coerce').dropna()
    stability_score = None
    if len(stability_candidates) >= 2:
        stability_score = max(0.0, 100.0 * (1.0 - min(float(stability_candidates.mean()), 1.0)))

    overall = {
        'cost_delta_pct': _li_delta_pct(current_summary.get('total_labor_cost'), prior_summary.get('total_labor_cost')),
        'hours_delta_pct': _li_delta_pct(current_summary.get('total_paid_hours'), prior_summary.get('total_paid_hours')),
        'rate_delta_pct': _li_delta_pct(current_summary.get('blended_rate'), prior_summary.get('blended_rate')),
        'driver_label': _driver_label(
            _li_delta_pct(current_summary.get('total_labor_cost'), prior_summary.get('total_labor_cost')),
            _li_delta_pct(current_summary.get('total_paid_hours'), prior_summary.get('total_paid_hours')),
            _li_delta_pct(current_summary.get('blended_rate'), prior_summary.get('blended_rate')),
        ),
        'worker_concentration_top3_share': worker_top3.get('top_n_share_pct'),
        'worker_concentration_top5_share': worker_top5.get('top_n_share_pct'),
        'department_concentration_top3_share': department_shares.get('top_n_share_pct'),
        'category_concentration_top3_share': category_top3.get('top_n_share_pct'),
        'top_department_share_pct': department_shares.get('top_share_pct'),
        'top_worker_share_pct': worker_top3.get('top_share_pct'),
        'stability_score': stability_score,
        'category_mix_basis': category_basis,
    }

    department_watchlist = dept_priority.loc[pd.to_numeric(dept_priority.get('priority_score', pd.Series(dtype='float64')), errors='coerce').fillna(0).ge(_LI_WATCH_THRESHOLD)].copy() if not dept_priority.empty else dept_priority
    worker_watchlist = worker_priority.loc[pd.to_numeric(worker_priority.get('priority_score', pd.Series(dtype='float64')), errors='coerce').fillna(0).ge(_LI_WATCH_THRESHOLD)].copy() if not worker_priority.empty else worker_priority
    category_watchlist = category_priority.loc[pd.to_numeric(category_priority.get('priority_score', pd.Series(dtype='float64')), errors='coerce').fillna(0).ge(_LI_WATCH_THRESHOLD)].copy() if not category_priority.empty else category_priority

    return {
        'filters': filters,
        'prior_start': prior_start,
        'prior_end': prior_end,
        'filter_options': filter_options,
        'current_summary': current_summary,
        'prior_summary': prior_summary,
        'current_departments': dept_priority,
        'category_mix': category_priority,
        'worker_summary': worker_priority,
        'department_watchlist': department_watchlist,
        'worker_watchlist': worker_watchlist,
        'category_watchlist': category_watchlist,
        'department_daily': department_daily,
        'daily_trend': daily_trend,
        'weekday_pattern': weekday_pattern,
        'monthly_pattern': monthly_pattern,
        'monthly_department_trend': monthly_department_trend,
        'category_daily_trend': category_daily_trend,
        'worker_daily_trend': worker_daily_trend,
        'overall': overall,
        'review_department': review_department[0] if review_department else {},
        'review_worker': review_worker[0] if review_worker else {},
        'review_category': review_category[0] if review_category else {},
    }


def _li_build_focus_department(filters: LaborFilters, analysis: Mapping[str, Any], department_name: str | None) -> dict[str, Any] | None:
    if not department_name:
        return None
    frame = analysis['current_departments']
    match = frame.loc[frame['department_name'] == department_name].copy()
    if match.empty:
        return None
    row = match.iloc[0].to_dict()
    scoped = replace(filters, departments=(department_name,), page=1, sort_by='labor_cost', sort_dir='desc')
    prior_start = analysis['prior_start']
    prior_end = analysis['prior_end']
    trend_rows = _li_augment_trend(_query_daily_trend(scoped))
    worker_rows = _li_add_scope_urls(
        _li_enrich_worker_summary(
            _query_worker_summary(scoped, limit=20),
            _query_worker_summary(scoped, start=prior_start, end=prior_end, limit=2000),
            _li_query_worker_daily(scoped),
        ),
        scoped,
        employee_col='employee_code',
    ).sort_values(['priority_score', 'labor_cost'], ascending=[False, False])
    category_rows = _li_add_scope_urls(
        _li_enrich_category_summary(
            _query_category_mix(scoped),
            _query_category_mix(scoped, start=prior_start, end=prior_end),
            _query_department_category_mix(scoped),
        ),
        scoped,
        category_col='time_category',
    ).sort_values(['priority_score', 'labor_cost'], ascending=[False, False])
    worker_watch = worker_rows.loc[pd.to_numeric(worker_rows.get('priority_score', pd.Series(dtype='float64')), errors='coerce').fillna(0).ge(_LI_WATCH_THRESHOLD)].head(8)
    category_watch = category_rows.loc[pd.to_numeric(category_rows.get('priority_score', pd.Series(dtype='float64')), errors='coerce').fillna(0).ge(_LI_WATCH_THRESHOLD)].head(8)
    interpretation = (
        f"{department_name} is currently {str(row.get('driver_label') or 'stable').replace('-', ' ')}. "
        f"{row.get('focus_reason')} Management should start here before changing worker assignments or schedule coverage."
    )
    return {
        'department_name': department_name,
        'summary': row,
        'trend_rows': trend_rows.to_dict(orient='records'),
        'worker_rows': worker_rows.head(12).to_dict(orient='records'),
        'category_rows': category_rows.head(10).to_dict(orient='records'),
        'worker_watch_rows': worker_watch.to_dict(orient='records'),
        'category_watch_rows': category_watch.to_dict(orient='records'),
        'interpretation': interpretation,
        'clear_url': build_url(filters, updates={'department': None}, include_pagination=False),
        'is_selected': department_name in filters.departments,
        'reason_note': 'Default department focus selected because it is the highest management priority in the current scope.' if department_name not in filters.departments else 'Department focus is pinned by the active filter.',
    }


def _li_build_focus_category(filters: LaborFilters, analysis: Mapping[str, Any], category_name: str | None) -> dict[str, Any] | None:
    if not category_name:
        return None
    frame = analysis['category_mix']
    match = frame.loc[frame['time_category'] == category_name].copy()
    if match.empty:
        return None
    row = match.iloc[0].to_dict()
    scoped = replace(filters, time_categories=(category_name,), page=1, sort_by='labor_cost', sort_dir='desc')
    prior_start = analysis['prior_start']
    prior_end = analysis['prior_end']
    trend_rows = _li_augment_trend(_query_daily_trend(scoped))
    department_rows = _li_add_scope_urls(_li_enrich_department_summary(_query_department_summary(scoped), _query_department_summary(scoped, start=prior_start, end=prior_end), _query_department_daily(scoped)), scoped, department_col='department_name').sort_values(['priority_score', 'labor_cost'], ascending=[False, False])
    worker_rows = _li_add_scope_urls(_li_enrich_worker_summary(_query_worker_summary(scoped, limit=20), _query_worker_summary(scoped, start=prior_start, end=prior_end, limit=2000), _li_query_worker_daily(scoped)), scoped, employee_col='employee_code').sort_values(['priority_score', 'labor_cost'], ascending=[False, False])
    interpretation = (
        f"{category_name} is classified as {row.get('category_kind', 'active')} and currently sits on a {str(row.get('share_display_label') or 'mix').lower()} basis of {_li_format_percent(row.get('share_display_pct'))}. "
        f"{row.get('focus_reason')}"
    )
    return {
        'time_category': category_name,
        'summary': row,
        'trend_rows': trend_rows.to_dict(orient='records'),
        'department_rows': department_rows.head(10).to_dict(orient='records'),
        'worker_rows': worker_rows.head(12).to_dict(orient='records'),
        'interpretation': interpretation,
        'clear_url': build_url(filters, updates={'time_category': None}, include_pagination=False),
        'is_selected': category_name in filters.time_categories,
        'reason_note': 'Default category focus selected because it is the highest category priority in the current scope.' if category_name not in filters.time_categories else 'Category focus is pinned by the active filter.',
    }


def _li_build_focus_worker(filters: LaborFilters, analysis: Mapping[str, Any], employee_code: str | None) -> dict[str, Any] | None:
    if not employee_code:
        return None
    frame = analysis['worker_summary']
    match = frame.loc[frame['employee_code'] == employee_code].copy()
    if match.empty:
        return None
    row = match.iloc[0].to_dict()
    scoped = replace(filters, employees=(employee_code,), page=1, sort_by='labor_cost', sort_dir='desc')
    prior_start = analysis['prior_start']
    prior_end = analysis['prior_end']
    trend_rows = _li_augment_trend(_query_daily_trend(scoped))
    department_rows = _li_add_scope_urls(_li_enrich_department_summary(_query_department_summary(scoped), _query_department_summary(scoped, start=prior_start, end=prior_end), _query_department_daily(scoped)), scoped, department_col='department_name').sort_values(['priority_score', 'labor_cost'], ascending=[False, False])
    category_rows = _li_add_scope_urls(_li_enrich_category_summary(_query_category_mix(scoped), _query_category_mix(scoped, start=prior_start, end=prior_end), _query_department_category_mix(scoped)), scoped, category_col='time_category').sort_values(['priority_score', 'labor_cost'], ascending=[False, False])
    interpretation = (
        f"{row.get('employee_name', 'This worker')} is {row.get('exposure_profile', 'active')} and carries {_li_format_percent(row.get('labor_cost_share_pct'))} of scoped labor cost. "
        f"{row.get('focus_reason')}"
    )
    return {
        'employee_code': employee_code,
        'employee_name': row.get('employee_name'),
        'summary': row,
        'trend_rows': trend_rows.to_dict(orient='records'),
        'department_rows': department_rows.head(10).to_dict(orient='records'),
        'category_rows': category_rows.head(10).to_dict(orient='records'),
        'interpretation': interpretation,
        'clear_url': build_url(filters, updates={'employee': None}, include_pagination=False),
        'is_selected': employee_code in filters.employees,
        'reason_note': 'Default worker focus selected because it is the highest worker priority in the current scope.' if employee_code not in filters.employees else 'Worker focus is pinned by the active filter.',
    }


def _li_build_actions(filters: LaborFilters, analysis: Mapping[str, Any]) -> list[dict[str, Any]]:
    review_department = analysis.get('review_department') or {}
    review_worker = analysis.get('review_worker') or {}
    review_category = analysis.get('review_category') or {}
    actions = []
    if review_department:
        actions.append({'title': f"Open {review_department.get('department_name')}", 'detail': review_department.get('focus_reason'), 'scope': 'department', 'value': review_department.get('department_name'), 'url': review_department.get('department_scope_url')})
    if review_worker:
        actions.append({'title': f"Review {review_worker.get('employee_name')}", 'detail': review_worker.get('focus_reason'), 'scope': 'employee', 'value': review_worker.get('employee_code'), 'url': review_worker.get('employee_scope_url')})
    if review_category:
        actions.append({'title': f"Inspect {review_category.get('time_category')}", 'detail': review_category.get('focus_reason'), 'scope': 'category', 'value': review_category.get('time_category'), 'url': review_category.get('category_scope_url')})
    actions.append({'title': 'Compare cost vs hours', 'detail': 'Use the operating rhythm section to separate staffing volume from rate or mix pressure.', 'scope': None, 'value': None, 'url': build_url(filters, include_pagination=False)})
    actions.append({'title': 'Open scoped detail rows', 'detail': 'Use the exploration workspace to trace the underlying labor transactions behind any signal.', 'scope': None, 'value': None, 'url': build_url(filters, include_pagination=False)})
    return actions[:5]


def build_page_payload(args: Mapping[str, Any] | None = None) -> dict[str, Any]:
    status = labor_store.get_status()
    filters = resolve_filters(args)
    payload: dict[str, Any] = {
        'status': status.__dict__,
        'filters': _serialize_filters(filters),
        'filter_options': {'departments': [], 'employees': [], 'time_categories': [], 'statuses': [], 'work_rules': []},
        'scope': {},
        'hero': {},
        'kpis': {},
        'scorecard_groups': [],
        'signals': [],
        'actions': [],
        'charts': {},
        'watchlist': {'rows': []},
        'watchlists': {'departments': [], 'workers': [], 'categories': []},
        'watchlist_sections': [],
        'workspace': {'rows': [], 'total_rows': 0, 'page': filters.page, 'page_size': filters.page_size, 'total_pages': 1},
        'focus': {'department': None, 'category': None, 'worker': None},
        'messages': [],
        'has_results': False,
        'narratives': {},
        'export_urls': {
            'snapshot_xlsx': build_export_url(filters, 'snapshot', 'xlsx'),
            'detail_csv': build_export_url(filters, 'detail', 'csv'),
            'department_summary_xlsx': build_export_url(filters, 'department-summary', 'xlsx'),
            'category_summary_xlsx': build_export_url(filters, 'category-summary', 'xlsx'),
            'employee_summary_xlsx': build_export_url(filters, 'employee-summary', 'xlsx'),
            'watchlist_csv': build_export_url(filters, 'watchlist', 'csv'),
        },
    }
    if not status.available:
        payload['messages'].append(status.warning)
        return payload

    analysis = _cached_analysis(filters)
    current_summary = analysis['current_summary']
    prior_summary = analysis['prior_summary']
    overall = analysis['overall']
    payload['scope'] = _li_build_scope_summary(filters, analysis['filter_options'], analysis['prior_start'], analysis['prior_end'], current_summary, prior_summary)
    payload['narratives'] = _li_build_narratives(analysis)
    payload['signals'] = _li_build_signals(filters, analysis)
    payload['actions'] = _li_build_actions(filters, analysis)
    payload['scorecard_groups'] = _li_build_scorecard_groups(analysis)
    payload['watchlist_sections'] = _li_build_watchlist_sections(analysis)
    payload['filter_options'] = analysis['filter_options']

    review_department = analysis.get('review_department') or {}
    review_worker = analysis.get('review_worker') or {}
    review_category = analysis.get('review_category') or {}

    payload['hero'] = {
        'title': 'Labor Intelligence',
        'subtitle': 'Enterprise workforce, labor cost, premium, absence, and staffing decision support from the live Synerion labor feed.',
        'purpose': 'Use this workspace to understand what changed, where labor pressure sits, which teams or workers need review first, and what managers should inspect next.',
        'current_window_label': payload['scope'].get('current_window_label'),
        'prior_window_label': payload['scope'].get('prior_window_label'),
        'last_refresh_label': _li_refresh_label(status.last_refresh_utc),
        'last_refresh_utc': status.last_refresh_utc,
        'top_driver': str(overall.get('driver_label') or 'stable').replace('-', ' ').title(),
        'top_department': review_department.get('department_name') or _top_label(analysis['current_departments'], 'department_name', 'No department'),
        'top_category': review_category.get('time_category') or _top_label(analysis['category_mix'], 'time_category', 'No category'),
        'top_worker': review_worker.get('employee_name') or _top_label(analysis['worker_summary'], 'employee_name', 'No worker'),
        'executive_narrative': payload['narratives'].get('executive'),
        'total_labor_cost': current_summary.get('total_labor_cost'),
        'total_paid_hours': current_summary.get('total_paid_hours'),
        'active_departments': current_summary.get('active_departments'),
        'active_employees': current_summary.get('active_employees'),
        'stability_score': overall.get('stability_score'),
    }

    kpis = {
        **current_summary,
        'labor_trend_vs_prior_pct': overall.get('cost_delta_pct'),
        'hours_trend_vs_prior_pct': overall.get('hours_delta_pct'),
        'blended_rate_vs_prior_pct': overall.get('rate_delta_pct'),
        'prior_window_start': analysis['prior_start'].isoformat(),
        'prior_window_end': analysis['prior_end'].isoformat(),
        'worker_concentration_top5_share': overall.get('worker_concentration_top5_share'),
        'department_concentration_top3_share': overall.get('department_concentration_top3_share'),
        'top_department_share_pct': overall.get('top_department_share_pct'),
        'top_worker_share_pct': overall.get('top_worker_share_pct'),
        'category_concentration_top3_share': overall.get('category_concentration_top3_share'),
        'stability_score': overall.get('stability_score'),
    }
    payload['kpis'] = kpis

    workspace = _query_workspace(filters)
    workspace_frame = workspace.get('frame')
    if not isinstance(workspace_frame, pd.DataFrame):
        workspace_frame = pd.DataFrame()
    workspace_frame = _li_add_scope_urls(workspace_frame, filters, department_col='department_name', employee_col='employee_code', category_col='time_category')
    workspace_payload = {key: value for key, value in workspace.items() if key != 'frame'}
    workspace_payload['rows'] = _li_clean_frame(workspace_frame).to_dict(orient='records') if not workspace_frame.empty else []
    workspace_payload['scope_note'] = f"Showing row-level labor transactions for {payload['scope'].get('active_scope_label')} from {payload['scope'].get('current_window_label')}."
    payload['workspace'] = workspace_payload

    department_table = analysis['current_departments'].copy()
    category_table = analysis['category_mix'].copy()
    worker_table = analysis['worker_summary'].copy()

    payload['charts'] = {
        'department_cost': _sort_for_json(department_table, ['labor_cost', 'department_name'], [False, True], limit=12),
        'department_change': _sort_for_json(department_table.assign(abs_cost_delta_pct=pd.to_numeric(department_table.get('cost_delta_pct'), errors='coerce').abs()), ['abs_cost_delta_pct', 'labor_cost'], [False, False], limit=12) if not department_table.empty else [],
        'department_risk': _sort_for_json(department_table, ['priority_score', 'labor_cost'], [False, False], limit=12),
        'department_volatility': _sort_for_json(department_table, ['cost_volatility', 'labor_cost'], [False, False], limit=12),
        'department_scatter': _sort_for_json(department_table, ['priority_score', 'labor_cost'], [False, False], limit=15),
        'daily_trend': analysis['daily_trend'].to_dict(orient='records'),
        'rate_trend': analysis['daily_trend'].to_dict(orient='records'),
        'weekday_pattern': analysis['weekday_pattern'].to_dict(orient='records'),
        'monthly_pattern': analysis['monthly_pattern'].to_dict(orient='records'),
        'monthly_department_trend': analysis['monthly_department_trend'].to_dict(orient='records'),
        'category_mix': _sort_for_json(category_table, [analysis['overall'].get('category_mix_basis') or 'labor_cost', 'time_category'], [False, True], limit=12),
        'category_mix_meta': {'value_key': analysis['overall'].get('category_mix_basis') or 'labor_cost', 'label': 'Labor cost mix' if (analysis['overall'].get('category_mix_basis') or 'labor_cost') == 'labor_cost' else 'Operational hours mix'},
        'category_trend': analysis['category_daily_trend'].to_dict(orient='records'),
        'worker_cost': _sort_for_json(worker_table, ['labor_cost', 'employee_name'], [False, True], limit=12),
        'worker_hours': _sort_for_json(worker_table, ['paid_hours', 'employee_name'], [False, True], limit=12),
        'worker_risk': _sort_for_json(worker_table, ['priority_score', 'labor_cost'], [False, False], limit=12),
        'worker_daily_trend': analysis['worker_daily_trend'].to_dict(orient='records'),
    }

    focus_department_name = filters.departments[0] if filters.departments else review_department.get('department_name')
    focus_category_name = filters.time_categories[0] if filters.time_categories else review_category.get('time_category')
    focus_worker_code = filters.employees[0] if filters.employees else review_worker.get('employee_code')
    payload['focus'] = {
        'department': _cached_focus(
            'department',
            filters,
            focus_department_name,
            lambda: _li_build_focus_department(_analysis_filters(filters), analysis, focus_department_name),
        ),
        'category': _cached_focus(
            'category',
            filters,
            focus_category_name,
            lambda: _li_build_focus_category(_analysis_filters(filters), analysis, focus_category_name),
        ),
        'worker': _cached_focus(
            'worker',
            filters,
            focus_worker_code,
            lambda: _li_build_focus_worker(_analysis_filters(filters), analysis, focus_worker_code),
        ),
    }

    payload['department_table'] = department_table.head(15).to_dict(orient='records')
    payload['category_table'] = category_table.head(15).to_dict(orient='records')
    payload['worker_table'] = worker_table.head(20).to_dict(orient='records')
    payload['watchlist'] = {'rows': analysis['department_watchlist'].head(15).to_dict(orient='records')}
    payload['watchlists'] = {
        'departments': analysis['department_watchlist'].head(12).to_dict(orient='records'),
        'workers': analysis['worker_watchlist'].head(12).to_dict(orient='records'),
        'categories': analysis['category_watchlist'].head(12).to_dict(orient='records'),
    }
    payload['has_results'] = bool(_li_safe_int(current_summary.get('transaction_count')))

    if not payload['has_results']:
        payload['messages'].append('No labor rows matched the active filters.')
    elif _li_safe_int(prior_summary.get('transaction_count')) == 0:
        payload['messages'].append(f"No prior comparable labor rows were available in {payload['scope'].get('prior_window_label')}; delta metrics are shown as n/a where needed.")
    if (_li_safe_float(current_summary.get('total_labor_cost')) or 0.0) <= 0 and (_li_safe_float(current_summary.get('total_paid_hours')) or 0.0) > 0:
        payload['messages'].append('The current scope contains paid hours without booked labor cost. Category concentration views fall back to paid hours where needed.')
    return _li_clean_frame(pd.DataFrame()).to_dict() if False else payload


def build_client_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    safe_payload = payload or {}
    focus = safe_payload.get('focus') if isinstance(safe_payload, Mapping) else {}
    focus = focus if isinstance(focus, Mapping) else {}

    def _focus_trend(key: str) -> dict[str, Any]:
        block = focus.get(key) if isinstance(focus, Mapping) else {}
        block = block if isinstance(block, Mapping) else {}
        return {'trend_rows': list(block.get('trend_rows') or [])}

    return {
        'filters': dict(safe_payload.get('filters') or {}),
        'charts': dict(safe_payload.get('charts') or {}),
        'focus': {
            'department': _focus_trend('department'),
            'category': _focus_trend('category'),
            'worker': _focus_trend('worker'),
        },
    }


def build_export_frames(filters: LaborFilters, dataset: str) -> tuple[dict[str, pd.DataFrame], str]:
    analysis = _cached_analysis(filters)
    current_summary = analysis['current_summary']
    prior_summary = analysis['prior_summary']
    overall = analysis['overall']
    scope_summary = _li_build_scope_summary(filters, analysis['filter_options'], analysis['prior_start'], analysis['prior_end'], current_summary, prior_summary)
    signals_df = pd.DataFrame(
        [
            {
                'Signal': signal.get('title'),
                'What Changed': signal.get('what_changed'),
                'Why It Matters': signal.get('why_it_matters'),
                'Inspect Next': signal.get('inspect_next'),
                'Action': signal.get('action_label'),
            }
            for signal in _li_build_signals(filters, analysis)
        ]
    )
    summary_rows = []
    for group in _li_build_scorecard_groups(analysis):
        for metric in group.get('metrics', []):
            summary_rows.append(
                {
                    'Group': group.get('title'),
                    'Metric': metric.get('label'),
                    'Value': metric.get('value'),
                    'Format': metric.get('format'),
                    'Delta': metric.get('delta'),
                    'Delta Format': metric.get('delta_format'),
                    'Definition': metric.get('definition'),
                    'Note': metric.get('note'),
                }
            )
    summary_df = pd.DataFrame(
        [
            {'Group': 'Scope', 'Metric': 'Window Start', 'Value': filters.start.isoformat(), 'Format': 'date', 'Delta': None, 'Delta Format': None, 'Definition': 'Current analysis window start date.', 'Note': None},
            {'Group': 'Scope', 'Metric': 'Window End', 'Value': filters.end.isoformat(), 'Format': 'date', 'Delta': None, 'Delta Format': None, 'Definition': 'Current analysis window end date.', 'Note': None},
            {'Group': 'Scope', 'Metric': 'Prior Window Start', 'Value': analysis['prior_start'].isoformat(), 'Format': 'date', 'Delta': None, 'Delta Format': None, 'Definition': 'Start of the comparator window.', 'Note': None},
            {'Group': 'Scope', 'Metric': 'Prior Window End', 'Value': analysis['prior_end'].isoformat(), 'Format': 'date', 'Delta': None, 'Delta Format': None, 'Definition': 'End of the comparator window.', 'Note': None},
            {'Group': 'Scope', 'Metric': 'Scope Summary', 'Value': scope_summary.get('active_scope_label'), 'Format': 'text', 'Delta': None, 'Delta Format': None, 'Definition': 'Human-readable description of the active filters.', 'Note': scope_summary.get('comparator_note')},
        ] + summary_rows
    )

    department_df = _rename_columns(
        analysis['current_departments'],
        {
            'department_name': 'Department',
            'department_number': 'Department Number',
            'labor_cost': 'Labor Cost',
            'paid_hours': 'Paid Hours',
            'blended_rate': 'Blended Rate',
            'premium_share_pct': 'Premium Share %',
            'absence_share_pct': 'Absence Share %',
            'labor_cost_share_pct': 'Labor Cost Share %',
            'paid_hours_share_pct': 'Paid Hours Share %',
            'cost_delta_pct': 'Cost Delta %',
            'hours_delta_pct': 'Hours Delta %',
            'rate_delta_pct': 'Rate Delta %',
            'cost_volatility': 'Cost Volatility',
            'rate_volatility': 'Rate Volatility',
            'priority_score': 'Priority Score',
            'risk_posture': 'Risk Posture',
            'management_focus': 'Management Focus',
            'focus_reason': 'Focus Reason',
            'action_label': 'Action Label',
        },
    )
    category_df = _rename_columns(
        analysis['category_mix'],
        {
            'time_category': 'Time Category',
            'labor_cost': 'Labor Cost',
            'paid_hours': 'Paid Hours',
            'labor_cost_share_pct': 'Labor Cost Share %',
            'paid_hours_share_pct': 'Paid Hours Share %',
            'share_display_pct': 'Primary Share %',
            'share_display_label': 'Primary Share Basis',
            'cost_delta_pct': 'Cost Delta %',
            'hours_delta_pct': 'Hours Delta %',
            'leading_department': 'Leading Department',
            'leading_department_share_pct': 'Leading Department Share %',
            'category_class': 'Category Class',
            'category_kind': 'Category Kind',
            'priority_score': 'Priority Score',
            'risk_posture': 'Risk Posture',
            'management_focus': 'Management Focus',
            'focus_reason': 'Focus Reason',
            'activity_note': 'Activity Note',
            'action_label': 'Action Label',
        },
    )
    worker_df = _rename_columns(
        analysis['worker_summary'],
        {
            'employee_name': 'Employee',
            'employee_code': 'Employee Code',
            'labor_cost': 'Labor Cost',
            'paid_hours': 'Paid Hours',
            'blended_rate': 'Blended Rate',
            'premium_share_pct': 'Premium Share %',
            'absence_share_pct': 'Absence Share %',
            'labor_cost_share_pct': 'Labor Cost Share %',
            'department_count': 'Departments Worked',
            'category_count': 'Categories Used',
            'cost_delta_pct': 'Cost Delta %',
            'hours_delta_pct': 'Hours Delta %',
            'cost_volatility': 'Cost Volatility',
            'recent_cost_acceleration_pct': 'Recent Cost Acceleration %',
            'priority_score': 'Priority Score',
            'risk_posture': 'Risk Posture',
            'management_focus': 'Management Focus',
            'focus_reason': 'Focus Reason',
            'exposure_profile': 'Exposure Profile',
            'action_label': 'Action Label',
        },
    )
    watchlist_df = _combined_watchlist_frame(analysis['department_watchlist'], analysis['worker_watchlist'], analysis['category_watchlist'])
    detail_df = _rename_columns(
        _query_workspace(filters, export_all=True)['frame'],
        {
            'labor_date': 'Labor Date',
            'department_name': 'Department',
            'employee_name': 'Employee',
            'employee_code': 'Employee Code',
            'time_category': 'Time Category',
            'labor_cost': 'Labor Cost',
            'paid_hours': 'Paid Hours',
            'effective_rate': 'Effective Rate',
            'status': 'Status',
            'work_rule': 'Work Rule',
            'is_premium': 'Is Premium',
            'is_absence': 'Is Absence',
            'is_memo': 'Is Memo',
        },
    )

    if dataset == 'detail':
        return ({'LaborDetail': detail_df}, 'labor_detail')
    if dataset == 'department-summary':
        return ({'DepartmentSummary': department_df}, 'labor_department_summary')
    if dataset == 'category-summary':
        return ({'CategorySummary': category_df}, 'labor_category_summary')
    if dataset == 'employee-summary':
        return ({'EmployeeSummary': worker_df}, 'labor_employee_summary')
    if dataset == 'watchlist':
        return ({'Watchlist': watchlist_df}, 'labor_watchlist')
    return (
        {
            'Summary': summary_df,
            'DecisionSignals': signals_df,
            'Departments': department_df,
            'Workers': worker_df,
            'Categories': category_df,
            'Trend': analysis['daily_trend'],
            'MonthlyPattern': analysis['monthly_pattern'],
            'DepartmentTrend': analysis['monthly_department_trend'],
            'Watchlist': watchlist_df,
            'Detail': detail_df,
        },
        'labor_snapshot',
    )

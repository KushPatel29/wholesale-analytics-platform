from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

import duckdb
import pandas as pd
from flask import current_app, g, has_app_context, has_request_context

from app.core.cache_manager import TTLValueCache


logger = logging.getLogger(__name__)

MANIFEST_NAME = "_manifest.json"
_DEFAULT_DATASET_DIR = Path("cache") / "labor" / "fact_dataset"
_duck_conn_lock = threading.RLock()
_duck_conn_local = threading.local()
_manifest_state: dict[str, Any] = {"checked_at": 0.0, "mtime": None, "version": None}
_MANIFEST_TTL_SECONDS = 2.0
_QUERY_CACHE = TTLValueCache(maxsize=int(os.getenv("LABOR_QUERY_CACHE_MAXSIZE", "128")))
_QUERY_CACHE_TTL_SECONDS = max(15, int(os.getenv("LABOR_QUERY_CACHE_TTL", "90")))


class LaborDatasetNotBuiltError(RuntimeError):
    """Raised when the labor parquet dataset is not available."""


@dataclass(frozen=True)
class LaborDatasetStatus:
    available: bool
    path: str
    warning: str | None = None
    row_count: int | None = None
    dataset_version: str | None = None
    last_refresh_utc: str | None = None
    min_date: str | None = None
    max_date: str | None = None


def resolve_dataset_path(dataset_path: Optional[Path] = None) -> Path:
    if dataset_path is not None:
        base = Path(dataset_path).expanduser().resolve()
    elif os.getenv("LABOR_PARQUET_PATH"):
        base = Path(os.getenv("LABOR_PARQUET_PATH") or _DEFAULT_DATASET_DIR).expanduser().resolve()
    elif has_app_context():
        try:
            base = Path(current_app.config["LABOR_PARQUET_PATH"]).expanduser().resolve()
        except Exception:
            base = Path(os.getenv("LABOR_PARQUET_PATH") or _DEFAULT_DATASET_DIR).expanduser().resolve()
    else:
        base = Path(os.getenv("LABOR_PARQUET_PATH") or _DEFAULT_DATASET_DIR).expanduser().resolve()
    if base.suffix == ".parquet":
        return base.parent / "fact_dataset"
    return base


def manifest_path(dataset_path: Optional[Path] = None) -> Path:
    return resolve_dataset_path(dataset_path) / MANIFEST_NAME


def read_manifest(dataset_path: Optional[Path] = None) -> dict[str, Any]:
    path = manifest_path(dataset_path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_status(dataset_path: Optional[Path] = None) -> LaborDatasetStatus:
    path = resolve_dataset_path(dataset_path)
    manifest = read_manifest(path)
    available = bool(path.exists() and any(path.rglob("*.parquet")))
    warning = None
    if not available:
        warning = (
            "Labor dataset is not built yet. Configure Synerion credentials in environment variables "
            "and run the labor refresh job."
        )
    row_count = manifest.get("row_count") if isinstance(manifest.get("row_count"), int) else None
    return LaborDatasetStatus(
        available=available,
        path=path.as_posix(),
        warning=warning,
        row_count=row_count,
        dataset_version=str(manifest.get("dataset_version")) if manifest.get("dataset_version") else None,
        last_refresh_utc=str(manifest.get("last_refresh_utc") or manifest.get("built_at_utc")) if manifest else None,
        min_date=str(manifest.get("min_date")) if manifest.get("min_date") else None,
        max_date=str(manifest.get("max_date")) if manifest.get("max_date") else None,
    )


def dataset_glob(dataset_path: Optional[Path] = None) -> str:
    base = resolve_dataset_path(dataset_path)
    if not base.exists():
        raise LaborDatasetNotBuiltError(f"Labor dataset not found at {base}")
    pattern = (base / "**" / "*.parquet").as_posix()
    if not any(base.rglob("*.parquet")):
        raise LaborDatasetNotBuiltError(f"Labor dataset not found at {base}")
    return pattern


def _quote_identifier(name: str) -> str:
    safe = str(name).replace('"', '""')
    return f'"{safe}"'


def _current_mtime(dataset_path: Optional[Path] = None) -> Optional[float]:
    try:
        return manifest_path(dataset_path).stat().st_mtime
    except FileNotFoundError:
        return None


def get_dataset_version(dataset_path: Optional[Path] = None) -> str:
    now = time.time()
    state = _manifest_state
    mtime = _current_mtime(dataset_path)
    if (
        state.get("version")
        and state.get("mtime") == mtime
        and (now - float(state.get("checked_at") or 0.0)) < _MANIFEST_TTL_SECONDS
    ):
        return str(state.get("version"))
    manifest = read_manifest(dataset_path)
    version = (
        manifest.get("dataset_version")
        or manifest.get("last_refresh_utc")
        or manifest.get("built_at_utc")
        or mtime
        or "0"
    )
    state.update({"checked_at": now, "mtime": mtime, "version": str(version)})
    return str(version)


def _request_query_cache_bucket() -> dict[str, pd.DataFrame] | None:
    if not has_request_context():
        return None
    try:
        bucket = getattr(g, "_labor_request_cache", None)
        if isinstance(bucket, dict):
            return bucket
        bucket = {}
        g._labor_request_cache = bucket
        return bucket
    except Exception:
        return None


def _query_cache_key(sql: str, params: Sequence[Any] | None = None) -> str:
    payload = {
        "sql": sql,
        "params": list(params or []),
        "version": get_dataset_version(),
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _pick(columns: set[str], *candidates: str) -> str | None:
    lower_map = {str(col).lower(): col for col in columns}
    for candidate in candidates:
        if candidate in columns:
            return candidate
        lowered = str(candidate).lower()
        if lowered in lower_map:
            return lower_map[lowered]
    return None


def _text_expr(columns: set[str], *candidates: str, default: str = "NULL") -> str:
    col = _pick(columns, *candidates)
    if not col:
        return default
    ident = _quote_identifier(col)
    return f"NULLIF(TRIM(CAST({ident} AS VARCHAR)), '')"


def _double_expr(columns: set[str], *candidates: str, default: str = "NULL") -> str:
    col = _pick(columns, *candidates)
    if not col:
        return default
    return f"TRY_CAST({_quote_identifier(col)} AS DOUBLE)"


def _int_expr(columns: set[str], *candidates: str, default: str = "NULL") -> str:
    col = _pick(columns, *candidates)
    if not col:
        return default
    return f"TRY_CAST({_quote_identifier(col)} AS BIGINT)"


def _date_expr(columns: set[str], *candidates: str, default: str = "NULL") -> str:
    col = _pick(columns, *candidates)
    if not col:
        return default
    return f"TRY_CAST({_quote_identifier(col)} AS DATE)"


def _timestamp_expr(columns: set[str], *candidates: str, default: str = "NULL") -> str:
    col = _pick(columns, *candidates)
    if not col:
        return default
    return f"TRY_CAST({_quote_identifier(col)} AS TIMESTAMP)"


def _boolean_expr(columns: set[str], *candidates: str, default: str = "FALSE") -> str:
    col = _pick(columns, *candidates)
    if not col:
        return default
    ident = _quote_identifier(col)
    lowered = f"LOWER(TRIM(CAST({ident} AS VARCHAR)))"
    return (
        "CASE "
        f"WHEN {ident} IS NULL THEN NULL "
        f"WHEN {lowered} IN ('1', 'true', 'yes', 'on', 'y', 't') THEN TRUE "
        f"WHEN {lowered} IN ('0', 'false', 'no', 'off', 'n', 'f') THEN FALSE "
        f"ELSE TRY_CAST({ident} AS BOOLEAN) "
        "END"
    )


def _init_duck_pragmas(conn: duckdb.DuckDBPyConnection) -> None:
    try:
        threads_env = os.getenv("DUCKDB_THREADS")
        threads = int(threads_env) if threads_env else (os.cpu_count() or 4)
    except Exception:
        threads = 4
    threads = max(1, min(int(threads), 32))
    try:
        conn.execute(f"PRAGMA threads={threads};")
    except Exception:
        logger.debug("labor_store.pragma_threads_failed", exc_info=True)
    try:
        conn.execute("PRAGMA enable_object_cache=true;")
    except Exception:
        logger.debug("labor_store.object_cache_failed", exc_info=True)
    mem_limit = os.getenv("DUCKDB_MEMORY_LIMIT") or os.getenv("FACT_MEMORY_LIMIT") or "2GB"
    try:
        conn.execute(f"PRAGMA memory_limit='{mem_limit}';")
    except Exception:
        logger.debug("labor_store.memory_limit_failed", exc_info=True)


def _register_views(conn: duckdb.DuckDBPyConnection, *, dataset_path: Optional[Path] = None) -> None:
    pattern = dataset_glob(dataset_path)
    pattern_sql = pattern.replace("'", "''")
    conn.execute(
        f"CREATE OR REPLACE VIEW labor_fact_raw AS SELECT * FROM parquet_scan('{pattern_sql}', union_by_name=true);"
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info('labor_fact_raw')").fetchall()}

    labor_date_expr = _date_expr(cols, "labor_date", "source_partition_date", "shift_match_date")
    shift_match_date_expr = _timestamp_expr(cols, "shift_match_date", "labor_datetime")
    week_start_expr = _date_expr(cols, "week_start")
    source_partition_date_expr = _date_expr(cols, "source_partition_date", "labor_date")
    labor_fact_sql = f"""
        CREATE OR REPLACE VIEW labor_fact AS
        WITH base AS (
            SELECT
                {labor_date_expr} AS labor_date,
                COALESCE(
                    {_timestamp_expr(cols, "labor_datetime")},
                    {_timestamp_expr(cols, "paid_start_time")},
                    {_timestamp_expr(cols, "first_in_punch_time")},
                    {_timestamp_expr(cols, "schedule_start")},
                    {shift_match_date_expr}
                ) AS labor_datetime,
                {shift_match_date_expr} AS shift_match_date,
                COALESCE({week_start_expr}, DATE_TRUNC('week', {labor_date_expr})) AS week_start,
                COALESCE({_text_expr(cols, "labor_week")}, STRFTIME(COALESCE({week_start_expr}, DATE_TRUNC('week', {labor_date_expr})), '%G-W%V')) AS labor_week,
                COALESCE({_text_expr(cols, "labor_month")}, STRFTIME({labor_date_expr}, '%Y-%m')) AS labor_month,
                COALESCE({_int_expr(cols, "labor_year")}, CAST(EXTRACT(year FROM {labor_date_expr}) AS BIGINT)) AS labor_year,
                COALESCE({_text_expr(cols, "weekday_name")}, STRFTIME({labor_date_expr}, '%A')) AS weekday_name,
                COALESCE({_text_expr(cols, "month_name")}, STRFTIME({labor_date_expr}, '%B')) AS month_name,
                {_text_expr(cols, "employee_code")} AS employee_code,
                COALESCE({_text_expr(cols, "employee_name")}, {_text_expr(cols, "employee_code")}, 'Unknown Employee') AS employee_name,
                {_text_expr(cols, "payroll_code")} AS payroll_code,
                COALESCE({_text_expr(cols, "department_name")}, 'Unassigned') AS department_name,
                {_text_expr(cols, "department_number")} AS department_number,
                {_text_expr(cols, "status")} AS status,
                {_text_expr(cols, "work_rule")} AS work_rule,
                {_timestamp_expr(cols, "schedule_start")} AS schedule_start,
                {_timestamp_expr(cols, "schedule_end")} AS schedule_end,
                {_timestamp_expr(cols, "first_in_punch_time")} AS first_in_punch_time,
                {_timestamp_expr(cols, "last_out_punch_time")} AS last_out_punch_time,
                {_timestamp_expr(cols, "paid_start_time")} AS paid_start_time,
                {_timestamp_expr(cols, "paid_end_time")} AS paid_end_time,
                {_text_expr(cols, "attended_duration_raw")} AS attended_duration_raw,
                {_double_expr(cols, "attended_hours")} AS attended_hours,
                {_double_expr(cols, "attended_hours_allocated", "attended_hours")} AS attended_hours_allocated,
                {_double_expr(cols, "paid_hours")} AS paid_hours,
                COALESCE({_double_expr(cols, "paid_hours_allocated")}, {_double_expr(cols, "paid_hours")}, 0.0) AS paid_hours_allocated,
                COALESCE({_text_expr(cols, "time_category")}, 'Unclassified') AS time_category,
                {_int_expr(cols, "transaction_index")} AS transaction_index,
                {_double_expr(cols, "transaction_duration")} AS transaction_duration,
                {_double_expr(cols, "transaction_duration_hours")} AS transaction_duration_hours,
                {_double_expr(cols, "effective_rate")} AS effective_rate,
                COALESCE({_double_expr(cols, "labor_cost")}, 0.0) AS labor_cost,
                {_text_expr(cols, "memo_amount_raw")} AS memo_amount_raw,
                {_double_expr(cols, "memo_amount")} AS memo_amount,
                COALESCE({_boolean_expr(cols, "is_paid")}, FALSE) AS is_paid,
                COALESCE({_boolean_expr(cols, "is_premium")}, FALSE) AS is_premium,
                COALESCE({_boolean_expr(cols, "is_absence")}, FALSE) AS is_absence,
                COALESCE({_boolean_expr(cols, "is_memo")}, FALSE) AS is_memo,
                COALESCE({_boolean_expr(cols, "has_time_transaction")}, TRUE) AS has_time_transaction,
                {_double_expr(cols, "schedule_hours")} AS schedule_hours,
                {_double_expr(cols, "schedule_hours_allocated", "schedule_hours")} AS schedule_hours_allocated,
                {_double_expr(cols, "punch_span_hours")} AS punch_span_hours,
                {_double_expr(cols, "punch_span_hours_allocated", "punch_span_hours")} AS punch_span_hours_allocated,
                {_double_expr(cols, "paid_span_hours")} AS paid_span_hours,
                {_double_expr(cols, "paid_span_hours_allocated", "paid_span_hours")} AS paid_span_hours_allocated,
                {_double_expr(cols, "blended_cost_per_paid_hour")} AS blended_cost_per_paid_hour,
                COALESCE(
                    {_text_expr(cols, "employee_key")},
                    MD5(COALESCE({_text_expr(cols, "employee_code")}, {_text_expr(cols, "payroll_code")}, {_text_expr(cols, "employee_name")}, 'unknown'))
                ) AS employee_key,
                COALESCE(
                    {_text_expr(cols, "employee_day_key")},
                    MD5(CONCAT_WS('|', COALESCE({_text_expr(cols, "employee_code")}, {_text_expr(cols, "payroll_code")}, 'unknown'), CAST({labor_date_expr} AS VARCHAR), COALESCE({_text_expr(cols, "department_name")}, 'Unassigned')))
                ) AS employee_day_key,
                COALESCE(
                    {_text_expr(cols, "department_key")},
                    MD5(CONCAT_WS('|', COALESCE({_text_expr(cols, "department_number")}, ''), COALESCE({_text_expr(cols, "department_name")}, 'Unassigned')))
                ) AS department_key,
                COALESCE({_text_expr(cols, "employee_status_group")}, 'active') AS employee_status_group,
                {_timestamp_expr(cols, "source_loaded_at")} AS source_loaded_at,
                {source_partition_date_expr} AS source_partition_date,
                {_text_expr(cols, "source_row_hash")} AS source_row_hash,
                {_date_expr(cols, "source_window_start")} AS source_window_start,
                {_date_expr(cols, "source_window_end")} AS source_window_end,
                COALESCE({_int_expr(cols, "employee_day_transaction_count")}, 1) AS employee_day_transaction_count,
                COALESCE({_int_expr(cols, "active_employee_flag")}, 0) AS active_employee_flag,
                COALESCE({_boolean_expr(cols, "primary_row_flag")}, FALSE) AS primary_row_flag
            FROM labor_fact_raw
        )
        SELECT *
        FROM base
        WHERE labor_date IS NOT NULL
    """
    conn.execute(labor_fact_sql)
    conn.execute(
        """
        CREATE OR REPLACE VIEW labor_department_daily AS
        SELECT
            labor_date,
            week_start,
            labor_month,
            labor_year,
            department_key,
            department_number,
            department_name,
            SUM(labor_cost) AS labor_cost,
            SUM(paid_hours_allocated) AS paid_hours,
            CASE
                WHEN SUM(paid_hours_allocated) = 0 THEN NULL
                ELSE SUM(labor_cost) / SUM(paid_hours_allocated)
            END AS blended_rate,
            SUM(CASE WHEN is_premium THEN labor_cost ELSE 0 END) AS premium_cost,
            SUM(CASE WHEN is_absence THEN labor_cost ELSE 0 END) AS absence_cost,
            SUM(CASE WHEN is_memo THEN COALESCE(memo_amount, labor_cost, 0) ELSE 0 END) AS memo_cost,
            COUNT(DISTINCT employee_key) AS active_employee_count,
            COUNT(*) AS transaction_count
        FROM labor_fact
        GROUP BY 1, 2, 3, 4, 5, 6, 7
        """
    )
    conn.execute(
        """
        CREATE OR REPLACE VIEW labor_department_weekly AS
        SELECT
            week_start,
            STRFTIME(week_start, '%G-W%V') AS labor_week,
            department_key,
            department_number,
            department_name,
            SUM(labor_cost) AS labor_cost,
            SUM(paid_hours) AS paid_hours,
            CASE
                WHEN SUM(paid_hours) = 0 THEN NULL
                ELSE SUM(labor_cost) / SUM(paid_hours)
            END AS blended_rate,
            SUM(premium_cost) AS premium_cost,
            SUM(absence_cost) AS absence_cost,
            SUM(memo_cost) AS memo_cost,
            SUM(active_employee_count) AS active_employee_days,
            SUM(transaction_count) AS transaction_count
        FROM labor_department_daily
        GROUP BY 1, 2, 3, 4, 5
        """
    )
    conn.execute(
        """
        CREATE OR REPLACE VIEW labor_department_monthly AS
        SELECT
            DATE_TRUNC('month', labor_date) AS month_start,
            labor_month,
            labor_year,
            department_key,
            department_number,
            department_name,
            SUM(labor_cost) AS labor_cost,
            SUM(paid_hours) AS paid_hours,
            CASE
                WHEN SUM(paid_hours) = 0 THEN NULL
                ELSE SUM(labor_cost) / SUM(paid_hours)
            END AS blended_rate,
            SUM(premium_cost) AS premium_cost,
            SUM(absence_cost) AS absence_cost,
            SUM(memo_cost) AS memo_cost,
            SUM(active_employee_count) AS active_employee_days,
            SUM(transaction_count) AS transaction_count
        FROM labor_department_daily
        GROUP BY 1, 2, 3, 4, 5, 6
        """
    )
    conn.execute(
        """
        CREATE OR REPLACE VIEW labor_time_category_daily AS
        SELECT
            labor_date,
            department_name,
            time_category,
            SUM(labor_cost) AS labor_cost,
            SUM(paid_hours_allocated) AS paid_hours,
            SUM(CASE WHEN is_premium THEN labor_cost ELSE 0 END) AS premium_cost,
            SUM(CASE WHEN is_absence THEN labor_cost ELSE 0 END) AS absence_cost,
            COUNT(*) AS transaction_count
        FROM labor_fact
        GROUP BY 1, 2, 3
        """
    )
    conn.execute(
        """
        CREATE OR REPLACE VIEW labor_employee_daily AS
        SELECT
            labor_date,
            employee_key,
            employee_code,
            employee_name,
            payroll_code,
            department_name,
            department_number,
            status,
            work_rule,
            SUM(labor_cost) AS labor_cost,
            SUM(paid_hours_allocated) AS paid_hours,
            CASE
                WHEN SUM(paid_hours_allocated) = 0 THEN NULL
                ELSE SUM(labor_cost) / SUM(paid_hours_allocated)
            END AS blended_rate,
            SUM(CASE WHEN is_premium THEN labor_cost ELSE 0 END) AS premium_cost,
            SUM(CASE WHEN is_absence THEN labor_cost ELSE 0 END) AS absence_cost,
            COUNT(*) AS transaction_count
        FROM labor_fact
        GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
        """
    )
    conn.execute(
        """
        CREATE OR REPLACE VIEW labor_trend_daily AS
        SELECT
            labor_date,
            SUM(labor_cost) AS labor_cost,
            SUM(paid_hours_allocated) AS paid_hours,
            CASE
                WHEN SUM(paid_hours_allocated) = 0 THEN NULL
                ELSE SUM(labor_cost) / SUM(paid_hours_allocated)
            END AS blended_rate,
            SUM(CASE WHEN is_premium THEN labor_cost ELSE 0 END) AS premium_cost,
            SUM(CASE WHEN is_absence THEN labor_cost ELSE 0 END) AS absence_cost
        FROM labor_fact
        GROUP BY 1
        ORDER BY 1
        """
    )
    conn.execute(
        """
        CREATE OR REPLACE VIEW labor_kpi_summary AS
        SELECT
            SUM(labor_cost) AS total_labor_cost,
            SUM(paid_hours_allocated) AS total_paid_hours,
            CASE
                WHEN SUM(paid_hours_allocated) = 0 THEN NULL
                ELSE SUM(labor_cost) / SUM(paid_hours_allocated)
            END AS blended_rate,
            SUM(CASE WHEN is_premium THEN labor_cost ELSE 0 END) AS premium_cost,
            SUM(CASE WHEN is_absence THEN labor_cost ELSE 0 END) AS absence_cost,
            SUM(CASE WHEN is_memo THEN COALESCE(memo_amount, labor_cost, 0) ELSE 0 END) AS memo_cost,
            COUNT(DISTINCT employee_key) AS active_employees,
            COUNT(DISTINCT department_key) AS active_departments,
            COUNT(*) AS transaction_count
        FROM labor_fact
        """
    )
    conn.execute(
        """
        CREATE OR REPLACE VIEW labor_watchlists AS
        WITH anchor AS (
            SELECT MAX(labor_date) AS max_labor_date
            FROM labor_fact
        ),
        current_window AS (
            SELECT *
            FROM labor_fact
            WHERE labor_date BETWEEN (SELECT max_labor_date - INTERVAL 29 DAY FROM anchor)
                                AND (SELECT max_labor_date FROM anchor)
        ),
        prior_window AS (
            SELECT *
            FROM labor_fact
            WHERE labor_date BETWEEN (SELECT max_labor_date - INTERVAL 59 DAY FROM anchor)
                                AND (SELECT max_labor_date - INTERVAL 30 DAY FROM anchor)
        ),
        current_department AS (
            SELECT
                department_name,
                SUM(labor_cost) AS labor_cost,
                SUM(paid_hours_allocated) AS paid_hours,
                SUM(CASE WHEN is_premium THEN labor_cost ELSE 0 END) AS premium_cost,
                SUM(CASE WHEN is_absence THEN labor_cost ELSE 0 END) AS absence_cost
            FROM current_window
            GROUP BY 1
        ),
        prior_department AS (
            SELECT
                department_name,
                SUM(labor_cost) AS labor_cost,
                SUM(paid_hours_allocated) AS paid_hours
            FROM prior_window
            GROUP BY 1
        ),
        monthly AS (
            SELECT
                department_name,
                DATE_TRUNC('month', labor_date) AS month_start,
                SUM(labor_cost) AS monthly_labor_cost
            FROM labor_fact
            GROUP BY 1, 2
        ),
        volatility AS (
            SELECT
                department_name,
                CASE
                    WHEN AVG(monthly_labor_cost) = 0 THEN NULL
                    ELSE STDDEV_SAMP(monthly_labor_cost) / AVG(monthly_labor_cost)
                END AS cost_volatility
            FROM monthly
            GROUP BY 1
        )
        SELECT
            c.department_name,
            c.labor_cost,
            c.paid_hours,
            CASE
                WHEN c.labor_cost = 0 THEN NULL
                ELSE c.premium_cost / c.labor_cost
            END AS premium_share_pct,
            CASE
                WHEN c.labor_cost = 0 THEN NULL
                ELSE c.absence_cost / c.labor_cost
            END AS absence_share_pct,
            CASE
                WHEN p.labor_cost IS NULL OR p.labor_cost = 0 THEN NULL
                ELSE (c.labor_cost - p.labor_cost) / p.labor_cost
            END AS cost_delta_pct,
            CASE
                WHEN p.paid_hours IS NULL OR p.paid_hours = 0 THEN NULL
                ELSE (c.paid_hours - p.paid_hours) / p.paid_hours
            END AS hours_delta_pct,
            v.cost_volatility
        FROM current_department c
        LEFT JOIN prior_department p ON p.department_name = c.department_name
        LEFT JOIN volatility v ON v.department_name = c.department_name
        WHERE
            COALESCE(
                CASE WHEN c.labor_cost = 0 THEN NULL ELSE c.premium_cost / c.labor_cost END,
                0
            ) >= 0.08
            OR COALESCE(
                CASE WHEN c.labor_cost = 0 THEN NULL ELSE c.absence_cost / c.labor_cost END,
                0
            ) >= 0.04
            OR COALESCE(
                CASE
                    WHEN p.labor_cost IS NULL OR p.labor_cost = 0 THEN NULL
                    ELSE (c.labor_cost - p.labor_cost) / p.labor_cost
                END,
                0
            ) >= 0.12
            OR COALESCE(v.cost_volatility, 0) >= 0.20
        """
    )


def _ensure_views(conn: duckdb.DuckDBPyConnection) -> None:
    version = get_dataset_version()
    active_version = getattr(_duck_conn_local, "labor_dataset_version", None)
    if active_version == version:
        return
    _register_views(conn)
    _duck_conn_local.labor_dataset_version = version


def get_conn() -> duckdb.DuckDBPyConnection:
    with _duck_conn_lock:
        conn = getattr(_duck_conn_local, "labor_conn", None)
        if conn is None:
            conn = duckdb.connect()
            _init_duck_pragmas(conn)
            _duck_conn_local.labor_conn = conn
        _ensure_views(conn)
        return conn


def close_duckdb_conn() -> None:
    with _duck_conn_lock:
        conn = getattr(_duck_conn_local, "labor_conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            _duck_conn_local.labor_conn = None
            _duck_conn_local.labor_dataset_version = None


def reset_duckdb_state() -> None:
    close_duckdb_conn()
    _manifest_state.update({"checked_at": 0.0, "mtime": None, "version": None})


def query_df(sql: str, params: Sequence[Any] | None = None) -> pd.DataFrame:
    sql_params = list(params or [])
    cache_key = _query_cache_key(sql, sql_params)
    request_cache = _request_query_cache_bucket()
    if request_cache is not None:
        cached_request = request_cache.get(cache_key)
        if isinstance(cached_request, pd.DataFrame):
            return cached_request.copy()
    cached = _QUERY_CACHE.get(cache_key)
    if isinstance(cached, pd.DataFrame):
        if request_cache is not None:
            request_cache[cache_key] = cached.copy()
        return cached.copy()

    def _build() -> pd.DataFrame:
        conn = get_conn()
        return conn.execute(sql, sql_params).fetchdf()

    frame, _ = _QUERY_CACHE.get_or_compute(cache_key, _QUERY_CACHE_TTL_SECONDS, _build)
    if request_cache is not None:
        request_cache[cache_key] = frame.copy()
    return frame.copy()


def query_scalar(sql: str, params: Sequence[Any] | None = None) -> Any:
    conn = get_conn()
    row = conn.execute(sql, list(params or [])).fetchone()
    return row[0] if row else None


def get_dataset_min_max_dates(dataset_path: Optional[Path] = None) -> tuple[Optional[pd.Timestamp], Optional[pd.Timestamp]]:
    status = get_status(dataset_path)
    if not status.available:
        return None, None
    sql = "SELECT MIN(labor_date) AS min_date, MAX(labor_date) AS max_date FROM labor_fact"
    row = get_conn().execute(sql).fetchone()
    if not row:
        return None, None
    min_ts = pd.to_datetime(row[0], errors="coerce") if row[0] is not None else None
    max_ts = pd.to_datetime(row[1], errors="coerce") if row[1] is not None else None
    return min_ts, max_ts


def list_columns(view_name: str = "labor_fact") -> list[str]:
    conn = get_conn()
    return [row[1] for row in conn.execute(f"PRAGMA table_info('{view_name}')").fetchall()]


def choose_column(candidates: Iterable[str], columns: Sequence[str]) -> str | None:
    cols = set(columns)
    return _pick(cols, *tuple(candidates))

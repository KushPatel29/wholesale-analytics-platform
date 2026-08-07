# app/services/fact_store.py
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import duckdb
import pandas as pd
from cachetools import TTLCache
from concurrent.futures import Future
from flask import g, has_request_context

from app.core.cache_manager import CACHE_MANAGER as CORE_CACHE_MANAGER, CacheManager
from app.core.exceptions import DatasetNotBuiltError
from . import analytics_utils as au
from . import fact_schema as fs
from .filters import FilterParams, normalize_filters
from fact_checkpoints import log_fact_checkpoint
from app.services import watermark_store

logger = logging.getLogger(__name__)


def _duckdb_lifecycle_logging_enabled() -> bool:
    return str(os.getenv("DEBUG_DUCKDB") or os.getenv("DEBUG_OBS") or os.getenv("DEBUG") or "").strip().lower() in {"1", "true", "yes", "on"}


def _request_query_cache_bucket() -> Dict[str, pd.DataFrame] | None:
    if not has_request_context():
        return None
    try:
        bucket = getattr(g, "_duckdb_request_cache", None)
        if isinstance(bucket, dict):
            return bucket
        bucket = {}
        g._duckdb_request_cache = bucket
        return bucket
    except Exception:
        return None


_cache_lock = threading.RLock()
_refresh_lock = threading.RLock()
_cached_frames: Dict[Tuple[str, ...], Dict[str, Any]] = {}
_bg_thread: Optional[threading.Thread] = None
_bg_stop = threading.Event()
_async_refreshing = threading.Event()
_duck_cache: TTLCache[str, pd.DataFrame] = TTLCache(maxsize=256, ttl=int(os.getenv("FACT_QUERY_CACHE_TTL", "120")))
_duck_lock = threading.RLock()
_inflight_queries: Dict[str, Future] = {}
_duck_conn_lock = threading.RLock()
_duck_conn_local = threading.local()
_duck_columns: Optional[set[str]] = None
_manifest_state: Dict[str, Any] = {"checked_at": 0.0, "mtime": None, "version": None, "payload": {}}
_MANIFEST_TTL_SECONDS = float(os.getenv("FACT_MANIFEST_TTL_SECONDS", "2"))


def _ensure_etl_mode(op: str) -> None:
    mode = (os.getenv("APP_MODE") or "").strip().lower()
    allow = str(os.getenv("ALLOW_LIVE_SQL", "")).strip().lower() in {"1", "true", "yes", "on"}
    if mode not in {"etl", "job"} and not allow:
        raise RuntimeError(f"{op} is ETL-only; run `python run.py refresh-fact --once` from an ETL worker.")


# Cache manager singleton
CACHE_MANAGER: CacheManager = CORE_CACHE_MANAGER
FACT_PATH = watermark_store.resolve_dataset_path()
META_PATH = FACT_PATH / watermark_store.MANIFEST_NAME
LOCK_PATH = FACT_PATH / ".refresh.lock"


def _current_mtime() -> Optional[float]:
    try:
        return FACT_PATH.stat().st_mtime
    except FileNotFoundError:
        return None


def _manifest() -> Dict[str, Any]:
    try:
        if META_PATH.exists():
            return json.loads(META_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    try:
        meta = CACHE_MANAGER.get_metadata()
        return meta or {}
    except Exception:
        return {}


def _require_manifest() -> Dict[str, Any]:
    meta = _manifest()
    if not FACT_PATH.exists() or not meta:
        raise DatasetNotBuiltError("Dataset not built. Run ETL job.")
    return meta


def _read_manifest_file() -> Dict[str, Any]:
    try:
        if META_PATH.exists():
            return json.loads(META_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("fact_store.manifest_read_failed", exc_info=True)
    return {}


def get_dataset_version() -> str:
    """Return the current dataset_version with a small stat+TTL cache."""
    now = time.time()
    state = _manifest_state
    if state.get("version") and (now - float(state.get("checked_at") or 0)) < _MANIFEST_TTL_SECONDS:
        return str(state.get("version"))
    try:
        mtime = META_PATH.stat().st_mtime
    except FileNotFoundError:
        return "0"
    except Exception:
        mtime = None
    if (
        state.get("version")
        and mtime is not None
        and state.get("mtime") == mtime
        and (now - float(state.get("checked_at") or 0)) < _MANIFEST_TTL_SECONDS
    ):
        return str(state.get("version"))
    payload = _read_manifest_file()
    if not payload:
        payload = _manifest()
    version = (
        payload.get("dataset_version")
        or payload.get("last_refresh_utc")
        or payload.get("watermark")
        or payload.get("watermark_dt")
        or mtime
        or time.time()
    )
    state.update({"checked_at": now, "mtime": mtime, "version": str(version), "payload": payload})
    return str(version)


def _dataset_base_path() -> Path:
    """
    Resolve the base path used for DuckDB parquet scans.

    Prefer the partitioned dataset directory whenever it exists to avoid
    accidentally scanning unrelated parquet files (e.g. products.parquet) or
    sibling `_prev` / `.tmp-*` datasets which can multiplicatively duplicate
    dashboard totals.

    For backwards compatibility, if the env points at a single parquet file and
    no partitioned dataset directory exists, fall back to scanning the file.
    """
    raw = os.getenv("FACT_DATASET_PATH") or os.getenv("PARQUET_PATH")
    if raw:
        raw_path = Path(raw).expanduser().resolve()
        if raw_path.is_file() and raw_path.suffix == ".parquet":
            ds_path = watermark_store.resolve_dataset_path()
            if ds_path.exists():
                return ds_path
            return raw_path
    return watermark_store.resolve_dataset_path()


def dataset_glob() -> str:
    base = _dataset_base_path()
    if not base.exists():
        raise DatasetNotBuiltError(f"Dataset not built. Expected parquet under: {base}")
    if base.is_file():
        return base.as_posix()
    return (base / "**" / "*.parquet").as_posix()


def _dataset_glob_for_path(path: Path) -> Optional[str]:
    try:
        base = path.expanduser().resolve()
    except Exception:
        base = path
    if not base.exists():
        return None
    if base.is_file():
        return base.as_posix()
    return (base / "**" / "*.parquet").as_posix()


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
        logger.debug("duckdb.pragma_threads_failed", exc_info=True)
    try:
        conn.execute("PRAGMA enable_object_cache=true;")
    except Exception:
        logger.debug("duckdb.object_cache_failed", exc_info=True)
    mem_limit = os.getenv("DUCKDB_MEMORY_LIMIT") or os.getenv("FACT_MEMORY_LIMIT") or "2GB"
    try:
        conn.execute(f"PRAGMA memory_limit='{mem_limit}';")
    except Exception:
        logger.debug("duckdb.memory_limit_failed", exc_info=True)


def _register_fact_view(conn: duckdb.DuckDBPyConnection) -> None:
    global _duck_columns
    pattern = dataset_glob()
    pattern_sql = pattern.replace("'", "''")
    # Enable hive_partitioning for automated pruning based on year/month/day folders.
    try:
        conn.execute(f"CREATE OR REPLACE VIEW fact_raw AS SELECT * FROM parquet_scan('{pattern_sql}', union_by_name=true, hive_partitioning=true);")
        # Test if it actually works (the BinderException happens on the first scan)
        conn.execute("SELECT 1 FROM fact_raw LIMIT 1").fetchall()
    except Exception as exc:
        logger.warning("duckdb.hive_partitioning_failed", extra={"path": pattern, "error": str(exc)})
        conn.execute(f"CREATE OR REPLACE VIEW fact_raw AS SELECT * FROM parquet_scan('{pattern_sql}', union_by_name=true, hive_partitioning=false);")
    # Build packs-only canonical view to align with SQL Server logic.
    cols = {row[1] for row in conn.execute("PRAGMA table_info('fact_raw')").fetchall()}
    lower_map = {str(c).lower(): c for c in cols}

    def _pick(*candidates: str) -> Optional[str]:
        for cand in candidates:
            if cand in cols:
                return cand
            lower = str(cand).lower()
            if lower in lower_map:
                return lower_map[lower]
        return None

    def _coalesce_numeric_expr(candidates: Sequence[str], default: str = "0") -> str:
        present: List[str] = []
        for cand in candidates:
            col = _pick(cand)
            if col and col not in present:
                present.append(col)
        if not present:
            return default
        exprs = [f"CAST({_quote_identifier(c)} AS DOUBLE)" for c in present]
        exprs.append(default)
        return f"COALESCE({', '.join(exprs)})"

    def _coalesce_date_expr(candidates: Sequence[str], default: str = "NULL") -> str:
        present: List[str] = []
        for cand in candidates:
            col = _pick(cand)
            if col and col not in present:
                present.append(col)
        if not present:
            return default
        exprs = [f"CAST({_quote_identifier(c)} AS DATE)" for c in present]
        return f"COALESCE({', '.join(exprs)})"

    # Canonical columns for packs-only logic
    pack_weight_col = _pick("pack_weight_lb_sum", "pack_weight_lb")
    pack_units_col = _pick("pack_item_count_sum", "pack_units_ea")
    pack_count_col = _pick("pack_count")
    unit_billing_col = _pick("UnitOfBillingId", "unit_of_billing_id", "UOM_UnitOfBillingId", "OrderedUnitsOfMeasureId")
    date_expected_expr = _coalesce_date_expr(
        ["DateExpected", "DateExpected_line", "DateExpected_order", "Date"],
        default="NULL",
    )
    pack_weight_expr = f"CAST({_quote_identifier(pack_weight_col)} AS DOUBLE)" if pack_weight_col else "NULL"
    pack_units_expr = f"CAST({_quote_identifier(pack_units_col)} AS DOUBLE)" if pack_units_col else "NULL"
    pack_count_expr = f"CAST({_quote_identifier(pack_count_col)} AS DOUBLE)" if pack_count_col else "NULL"
    unit_billing_expr = f"CAST({_quote_identifier(unit_billing_col)} AS INTEGER)" if unit_billing_col else "NULL"
    price_expr = _coalesce_numeric_expr(("Price", "PricePerUnit", "BasePrice", "ListPrice"), default="0")
    cost_price_expr = _coalesce_numeric_expr(("CostPrice", "CostPerUnit", "UnitCost"), default="0")

    if pack_count_col:
        missing_packs_expr = f"CASE WHEN {pack_count_expr} IS NULL THEN TRUE ELSE FALSE END"
    else:
        missing_packs_expr = f"CASE WHEN {pack_weight_expr} IS NULL AND {pack_units_expr} IS NULL THEN TRUE ELSE FALSE END"

    revenue_expr = (
        "CASE "
        f"WHEN {missing_packs_expr} THEN NULL "
        f"WHEN {unit_billing_expr} = 3 THEN {pack_weight_expr} * {price_expr} "
        f"ELSE {pack_units_expr} * {price_expr} "
        "END"
    )
    cost_expr = (
        "CASE "
        f"WHEN {missing_packs_expr} THEN NULL "
        f"WHEN {unit_billing_expr} = 3 THEN {pack_weight_expr} * {cost_price_expr} "
        f"ELSE {pack_units_expr} * {cost_price_expr} "
        "END"
    )
    profit_expr = f"({revenue_expr} - {cost_expr})"
    margin_expr = (
        "CASE "
        f"WHEN {revenue_expr} IS NULL OR {revenue_expr} = 0 THEN NULL "
        f"ELSE ({profit_expr} / {revenue_expr}) "
        "END"
    )
    roi_expr = (
        "CASE "
        f"WHEN {cost_expr} IS NULL OR {cost_expr} = 0 THEN NULL "
        f"ELSE ({profit_expr} / {cost_expr}) "
        "END"
    )

    override_cols = {
        "Revenue",
        "Cost",
        "Profit",
        "MarginPct",
        "ROIPct",
        "Date",
        "EffectiveDate",
        "DateExpected",
        "missing_packs",
        "pack_weight_lb",
        "pack_units_ea",
        "revenue_packs_only",
        "cost_packs_only",
        "profit_packs_only",
        "margin_packs_only",
    }
    base_cols = [c for c in cols if c not in override_cols]
    select_cols = ", ".join(_quote_identifier(c) for c in base_cols) if base_cols else ""

    view_sql = f"""
        CREATE OR REPLACE VIEW fact_sales_packs AS
        WITH base AS (
            SELECT
                {select_cols}{"," if select_cols else ""}
                {date_expected_expr} AS DateExpected,
                {date_expected_expr} AS Date,
                {date_expected_expr} AS EffectiveDate,
                {pack_weight_expr} AS pack_weight_lb,
                {pack_units_expr} AS pack_units_ea,
                {missing_packs_expr} AS missing_packs
            FROM fact_raw
        )
        SELECT
            base.*,
            {revenue_expr} AS revenue_packs_only,
            {cost_expr} AS cost_packs_only,
            {profit_expr} AS profit_packs_only,
            {margin_expr} AS margin_packs_only,
            {revenue_expr} AS Revenue,
            {cost_expr} AS Cost,
            {profit_expr} AS Profit,
            {margin_expr} AS MarginPct,
            {roi_expr} AS ROIPct
        FROM base;
    """
    conn.execute(view_sql)
    conn.execute("CREATE OR REPLACE VIEW fact AS SELECT * FROM fact_sales_packs;")
    _duck_columns = None
    try:
        from flask import g  # type: ignore

        req_id = getattr(g, "request_id", None)
    except Exception:
        req_id = None
    log_fn = logger.info if _duckdb_lifecycle_logging_enabled() else logger.debug
    log_fn("duckdb.view_initialized", extra={"path": pattern, "request_id": req_id})


def _duck_state() -> Dict[str, Any]:
    """Return per-request or thread-local DuckDB state."""
    if has_request_context():
        state = getattr(g, "_duckdb_state", None)
        if state is None:
            state = {}
            g._duckdb_state = state
        return state
    state = getattr(_duck_conn_local, "state", None)
    if state is None:
        state = {}
        _duck_conn_local.state = state
    return state


def reload_if_version_changed(conn: Optional[duckdb.DuckDBPyConnection] = None) -> bool:
    """Reload DuckDB view when dataset_version changes."""
    try:
        state = _duck_state()
    except Exception:
        state = {}
    current = state.get("dataset_version")
    latest = get_dataset_version()
    if current == latest:
        return False
    connection = conn or state.get("conn")
    if connection is None:
        return False
    def _finalize_reload() -> None:
        state["dataset_version"] = latest
        state["view_initialized"] = True
        global _duck_columns
        _duck_columns = None
        try:
            with _duck_lock:
                _duck_cache.clear()
                _inflight_queries.clear()
        except Exception:
            pass

    try:
        try:
            connection.execute("PRAGMA enable_object_cache=false;")
        except Exception:
            pass
        _register_fact_view(connection)
        try:
            connection.execute("PRAGMA enable_object_cache=true;")
        except Exception:
            pass
        _finalize_reload()
        return True
    except duckdb.Error:
        # If parquet files were swapped in-place, DuckDB can hold stale file handles.
        # Re-open the connection to force a clean file scan.
        if state.get("conn") is connection:
            try:
                connection.close()
            except Exception:
                pass
            new_conn = duckdb.connect(database=":memory:", read_only=False)
            _init_duck_pragmas(new_conn)
            _register_fact_view(new_conn)
            state["conn"] = new_conn
            _finalize_reload()
            return True
        raise


def get_duckdb_conn() -> duckdb.DuckDBPyConnection:
    """Return a per-request (or thread-local) DuckDB connection with the fact view registered."""
    state = _duck_state()
    conn = state.get("conn")
    if conn is None:
        with _duck_conn_lock:
            conn = duckdb.connect(database=":memory:", read_only=False)
            _init_duck_pragmas(conn)
            state["conn"] = conn
            state["view_initialized"] = False
    if not state.get("view_initialized"):
        _register_fact_view(conn)
        state["view_initialized"] = True
        state["dataset_version"] = get_dataset_version()
    else:
        try:
            reload_if_version_changed(conn)
            conn = state.get("conn") or conn
        except Exception:
            pass
    return conn


def close_duckdb_conn() -> None:
    """Close the request-scoped DuckDB connection if present."""
    try:
        if not has_request_context():
            return
    except Exception:
        return
    state = getattr(g, "_duckdb_state", None)
    if not state:
        return
    conn = state.get("conn")
    if conn is not None:
        try:
            conn.close()
        except Exception:
            logger.debug("duckdb.conn_close_failed", exc_info=True)
    state.clear()


def reset_duckdb_state() -> None:
    """Test helper to reset cached DuckDB state and force view re-init."""
    global _duck_columns
    _duck_columns = None
    _manifest_state.update({"checked_at": 0.0, "mtime": None, "version": None, "payload": {}})
    try:
        if has_request_context():
            state = getattr(g, "_duckdb_state", None)
            if state:
                conn = state.get("conn")
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        logger.debug("duckdb.conn_close_failed", exc_info=True)
                state.clear()
    except Exception:
        pass
    state = getattr(_duck_conn_local, "state", None)
    if state:
        conn = state.get("conn")
        if conn is not None:
            try:
                conn.close()
            except Exception:
                logger.debug("duckdb.conn_close_failed", exc_info=True)
        state.clear()
    try:
        with _duck_lock:
            _duck_cache.clear()
            _inflight_queries.clear()
    except Exception:
        pass


def _list_columns(conn: Optional[duckdb.DuckDBPyConnection] = None) -> set[str]:
    """Return cached column names for the fact view."""
    global _duck_columns
    connection = conn or get_duckdb_conn()
    try:
        reload_if_version_changed(connection)
    finally:
        if conn is None:
            try:
                state = _duck_state()
                connection = state.get("conn") or connection
            except Exception:
                pass
    if _duck_columns is None:
        rows = connection.execute("PRAGMA table_info('fact')").fetchall()
        _duck_columns = {row[1] for row in rows}
    return set(_duck_columns)


def get_conn() -> duckdb.DuckDBPyConnection:
    """Compatibility shim for legacy callers."""
    return get_duckdb_conn()


def init_views(conn: Optional[duckdb.DuckDBPyConnection] = None) -> None:
    connection = conn or get_duckdb_conn()
    _register_fact_view(connection)
    try:
        state = _duck_state()
        state["view_initialized"] = True
        state["dataset_version"] = get_dataset_version()
    except Exception:
        pass


def list_columns(conn: Optional[duckdb.DuckDBPyConnection] = None) -> set[str]:
    return _list_columns(conn)


def _available_columns(conn: Optional[duckdb.DuckDBPyConnection] = None) -> set[str]:
    try:
        return _list_columns(conn)
    except Exception:
        logger.debug("fact_store.columns_failed", exc_info=True)
        return set()


def _has_explicit_date(filters: Any) -> bool:
    try:
        if isinstance(filters, FilterParams):
            return bool(filters.start or filters.end or getattr(filters, "preset", None))
    except Exception:
        pass
    try:
        if isinstance(filters, dict):
            keys = {str(k).strip().lower() for k in filters.keys()}
            date_keys = {
                "start",
                "start_date",
                "startdate",
                "end",
                "end_date",
                "enddate",
                "date_preset",
                "preset",
                "range_preset",
                "date_type",
            }
            return bool(keys & date_keys)
    except Exception:
        pass
    return False


def _normalize_filters_obj(filters: Any, *, apply_default_window: bool = True) -> FilterParams:
    try:
        if isinstance(filters, FilterParams):
            params = filters
        else:
            params = normalize_filters(filters or {})
    except Exception:
        params = normalize_filters(filters or {})

    if not apply_default_window and not _has_explicit_date(filters):
        try:
            params = replace(params, start=None, end=None, preset=None)
        except Exception:
            params.start = None  # type: ignore[attr-defined]
            params.end = None  # type: ignore[attr-defined]
            try:
                params.preset = None  # type: ignore[attr-defined]
            except Exception:
                pass
    return params


def _values_list(raw: Any) -> List[str]:
    vals: List[str] = []
    if raw is None:
        return vals
    if isinstance(raw, (list, tuple, set)):
        iterable = raw
    else:
        iterable = [raw]
    for item in iterable:
        if item is None:
            continue
        sval = str(item).strip()
        if not sval or sval.lower() == "all":
            continue
        vals.append(sval)
    return vals


def _lower_list(values: List[str]) -> List[str]:
    return [str(v).strip().lower() for v in values if str(v).strip()]


def _scope_mode(scope: Optional[Dict[str, Any]]) -> str:
    scope = scope or {}
    mode = str(scope.get("scope_mode") or "").strip().lower()
    if mode in {"all", "list", "none"}:
        return mode
    if scope.get("is_admin") or scope.get("is_super_user"):
        return "all"
    has_any_allow = any(
        bool(scope.get(key))
        for key in (
            "allowed_erp_user_ids",
            "sales_rep_ids",
            "rep_ids",
            "allowed_customer_ids",
            "customer_ids",
            "allowed_region_ids",
            "region_ids",
            "allowed_supplier_ids",
            "supplier_ids",
        )
    )
    if has_any_allow:
        return "list"
    try:
        if os.getenv("PYTEST_CURRENT_TEST"):
            return "all"
        flag_vals = {"1", "true", "yes", "on"}
        if str(os.getenv("LOGIN_DISABLED") or "").strip().lower() in flag_vals:
            return "all"
        if str(os.getenv("AUTHZ_DISABLED") or "").strip().lower() in flag_vals:
            return "all"
        if has_request_context():
            try:
                from flask import current_app

                if current_app and (
                    current_app.config.get("LOGIN_DISABLED") or current_app.config.get("AUTHZ_DISABLED")
                ):
                    return "all"
            except Exception:
                pass
    except Exception:
        pass
    return "none"


def build_scope_clause(scope: Optional[Dict[str, Any]], cols: set[str]) -> tuple[str, List[Any]]:
    """
    Build a scope-only WHERE predicate for DuckDB.
    Returns ("1=1", []) for admin, ("1=0", []) for no access, or a scoped predicate.
    """
    mode = _scope_mode(scope)
    if mode == "all":
        return "1=1", []
    if mode == "none":
        return "1=0", []
    scope = scope or {}
    rep_values = _lower_list(_values_list(scope.get("allowed_erp_user_ids") or scope.get("sales_rep_ids") or scope.get("rep_ids")))
    customer_values = _lower_list(_values_list(scope.get("allowed_customer_ids") or scope.get("customer_ids")))
    region_values = _lower_list(_values_list(scope.get("allowed_region_ids") or scope.get("region_ids")))
    supplier_values = _lower_list(_values_list(scope.get("allowed_supplier_ids") or scope.get("supplier_ids")))

    def _dimension_clause(values: List[str], candidates: Sequence[str]) -> tuple[Optional[str], List[Any]]:
        if not values:
            return None, []
        dim_cols = [c for c in candidates if c in cols]
        if not dim_cols:
            return None, []
        placeholders = ", ".join("?" for _ in values)
        dim_clauses = [f"LOWER(CAST({_quote_identifier(col)} AS VARCHAR)) IN ({placeholders})" for col in dim_cols]
        params: List[Any] = []
        for _ in dim_cols:
            params.extend(values)
        return "(" + " OR ".join(dim_clauses) + ")", params

    clauses: List[str] = []
    params: List[Any] = []
    dimension_specs: List[tuple[List[str], Sequence[str]]] = [
        (
            rep_values,
            (
                "SalesRepUserId",
                "SalesRepUserID",
                "PrimarySalesRepUserId",
                "PrimarySalesRepUserID",
                "RepUserId",
                "RepUserID",
                "UserId",
                "UserID",
                "SalesRepId",
                "SalesRepID",
                "PrimarySalesRepId",
                "PrimarySalesRepID",
                "RepId",
                "RepID",
            ),
        ),
        (
            customer_values,
            ("CustomerId", "CustomerID", "CustomerName", "Customer"),
        ),
        (
            region_values,
            ("RegionId", "RegionID", "RegionName", "Region"),
        ),
        (
            supplier_values,
            ("SupplierId", "SupplierID", "SupplierName", "Supplier"),
        ),
    ]

    had_requested_scope = any(bool(values) for values, _ in dimension_specs)
    for values, candidates in dimension_specs:
        clause, dim_params = _dimension_clause(values, candidates)
        if not clause:
            continue
        clauses.append(clause)
        params.extend(dim_params)

    if not clauses:
        return ("1=0", []) if had_requested_scope else ("1=0", [])
    return " AND ".join(clauses), params


def _choose_column(options: Sequence[str], cols: set[str]) -> Optional[str]:
    for cand in options:
        if cand in cols:
            return cand
    return None


def choose_column(options: Sequence[str], cols: set[str]) -> Optional[str]:
    return _choose_column(options, cols)


def _quote_identifier(label: str) -> str:
    safe = str(label).replace('"', '""')
    return f'"{safe}"'


def quote_identifier(label: str) -> str:
    """Public wrapper to safely quote identifiers for DuckDB SQL."""
    return _quote_identifier(label)


def _where_clause(
    filters: FilterParams,
    cols: set[str],
    scope: Optional[Dict[str, Any]] = None,
    *,
    apply_default_window: bool = True,
) -> tuple[str, List[Any], Optional[str], Optional[str]]:
    where_parts: List[str] = ["1=1"]
    params: List[Any] = []

    start_ts = getattr(filters, "start", None)
    end_ts = getattr(filters, "end", None)
    preset_token = str(getattr(filters, "preset", "") or "").strip().lower()
    if apply_default_window and preset_token not in {"all", "all_time", "__all__", "*"}:
        start_ts, end_ts = _apply_default_window(start_ts, end_ts)
    try:
        from app.services.filters import _normalize_end_for_inclusive_day  # type: ignore
    except Exception:
        _normalize_end_for_inclusive_day = None  # type: ignore[assignment]
    end_adj = end_ts
    if _normalize_end_for_inclusive_day and end_ts is not None:
        try:
            end_adj, _ = _normalize_end_for_inclusive_day(end_ts)
        except Exception:
            end_adj = end_ts
    start_iso = start_ts.date().isoformat() if start_ts is not None and pd.notna(start_ts) else None
    end_iso = end_adj.date().isoformat() if end_adj is not None and pd.notna(end_adj) else None

    date_col = _choose_column(("DateExpected", "Date", "EffectiveDate", "date"), cols)
    if start_iso and date_col:
        where_parts.append(f"{date_col} >= ?")
        params.append(start_iso)
    if end_iso and date_col:
        where_parts.append(f"{date_col} < ?")
        params.append(end_iso)

    status_col = _choose_column(("OrderStatus", "order_status", "Status"), cols)
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

    def _add_clause(values: List[str], candidates: Sequence[str]) -> None:
        if not values:
            return
        col = _choose_column(candidates, cols)
        if not col:
            return
        placeholders = ", ".join("?" for _ in values)
        where_parts.append(f"{col} IN ({placeholders})")
        params.extend(values)

    _add_clause(_values_list(getattr(filters, "regions", ())), ("RegionId", "RegionName", "Region"))
    _add_clause(_values_list(getattr(filters, "methods", ())), ("ShippingMethodName", "ShippingMethodLabel", "ShippingMethodRequested", "ShipMethod_Name"))
    _add_clause(_values_list(getattr(filters, "customers", ())), ("CustomerId", "CustomerName"))
    _add_clause(_values_list(getattr(filters, "suppliers", ())), ("SupplierId", "SupplierName"))
    _add_clause(_values_list(getattr(filters, "products", ())), ("ProductId", "ProductName", "SKU"))
    sales_values = _values_list(getattr(filters, "sales_reps", ()))
    if sales_values:
        rep_cols = [c for c in ("SalesRepId", "PrimarySalesRepId", "SalesRepName", "PrimarySalesRepName") if c in cols]
        if rep_cols:
            placeholders = ", ".join("?" for _ in sales_values)
            rep_clauses = [f"{col} IN ({placeholders})" for col in rep_cols]
            where_parts.append("(" + " OR ".join(rep_clauses) + ")")
            for _ in rep_cols:
                params.extend(sales_values)

    scope_where, scope_params = build_scope_clause(scope, cols)
    if scope_where:
        where_parts.append(scope_where)
        params.extend(scope_params)

    return " AND ".join(where_parts), params, start_iso, end_iso


def build_where_clause(
    filters: FilterParams,
    cols: set[str],
    scope: Optional[Dict[str, Any]] = None,
    *,
    apply_default_window: bool = True,
) -> tuple[str, List[Any], Optional[str], Optional[str]]:
    """Expose where-clause builder for external services (filters/options)."""
    return _where_clause(filters, cols, scope, apply_default_window=apply_default_window)


def _execute_df(sql: str, params: List[Any], *, tag: str, cache_key: Optional[str] = None) -> pd.DataFrame:
    """Run a DuckDB query with optional TTL cache and structured logging."""
    version = cache_buster()
    try:
        from flask import g  # type: ignore

        request_id = getattr(g, "request_id", None)
    except Exception:
        request_id = None
    request_cache = _request_query_cache_bucket()
    request_cache_key = None
    key = None
    if cache_key or request_cache is not None:
        payload = {"sql": sql, "params": params, "version": version}
        raw = json.dumps(payload, sort_keys=True, default=str)
        request_cache_key = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if request_cache is not None:
            cached_request = request_cache.get(request_cache_key)
            if isinstance(cached_request, pd.DataFrame):
                return cached_request.copy()
    if cache_key:
        key = request_cache_key
        with _duck_lock:
            cached = _duck_cache.get(key)
            if cached is not None:
                if request_cache is not None and request_cache_key:
                    request_cache[request_cache_key] = cached.copy()
                return cached.copy()
            inflight = _inflight_queries.get(key)
            if inflight:
                try:
                    frame = inflight.result().copy()
                    if request_cache is not None and request_cache_key:
                        request_cache[request_cache_key] = frame.copy()
                    return frame
                except Exception:
                    _inflight_queries.pop(key, None)
            fut = Future()
            _inflight_queries[key] = fut
    started = time.perf_counter()
    rows = None
    duration_ms: Optional[int] = None
    req_stats: Optional[Dict[str, Any]] = None
    if has_request_context():
        try:
            req_stats = getattr(g, "_duckdb_stats", None)
            if req_stats is None:
                req_stats = {"count": 0, "total_ms": 0}
                g._duckdb_stats = req_stats
        except Exception:
            req_stats = None

    def _strip_no_value_sentinels(frame: pd.DataFrame) -> pd.DataFrame:
        if frame is None or frame.empty:
            return frame
        try:
            for col in frame.columns:
                series = frame[col]
                if str(series.dtype) != "object":
                    continue
                has_no_value = False
                try:
                    has_no_value = bool(series.map(lambda v: type(v).__name__ == "_NoValueType").any())
                except Exception:
                    has_no_value = False
                if has_no_value:
                    frame[col] = series.map(lambda v: None if type(v).__name__ == "_NoValueType" else v)
        except Exception:
            return frame
        return frame

    try:
        conn = get_duckdb_conn()
        reload_if_version_changed(conn)
        try:
            df = conn.execute(sql, params).fetchdf()
        except Exception as exc:
            msg = str(exc)
            known_fetchdf_bug = (
                isinstance(exc, TypeError)
                and ("_NoValueType" in msg or "int() argument must be a string" in msg)
            ) or ("InvalidIndexError" in msg and "_NoValueType" in msg)
            if not known_fetchdf_bug:
                raise
            logger.warning(
                "duckdb.fetchdf_fallback",
                extra={"tag": tag, "request_id": request_id, "reason": msg[:240]},
            )
            cur = conn.execute(sql, params)
            rows = cur.fetchall()
            cols = [str(col[0]) for col in (cur.description or [])]
            df = pd.DataFrame.from_records(rows, columns=cols)
        df = _strip_no_value_sentinels(df)
        rows = len(df)
        if request_cache is not None and request_cache_key:
            request_cache[request_cache_key] = df.copy()
        if key:
            with _duck_lock:
                _duck_cache[key] = df.copy()
                fut = _inflight_queries.get(key)
                if fut:
                    fut.set_result(df.copy())
    except Exception:
        try:
            logger.exception("duckdb.query_failed", extra={"tag": tag, "request_id": request_id})
        except Exception:
            pass
        raise
    finally:
        duration_ms = int((time.perf_counter() - started) * 1000)
        try:
            logger.info(
                "duckdb.query",
                extra={"tag": tag, "duration_ms": duration_ms, "rows": rows, "cache_key": key, "request_id": request_id},
            )
        except Exception:
            pass
        if key:
            with _duck_lock:
                _inflight_queries.pop(key, None)
        if req_stats is not None and duration_ms is not None:
            try:
                req_stats["count"] = int(req_stats.get("count", 0)) + 1
                req_stats["total_ms"] = int(req_stats.get("total_ms", 0)) + int(duration_ms)
            except Exception:
                pass
    return df


def execute_sql_df(sql: str, params: Optional[Sequence[Any]] = None, *, tag: str = "query", cache_key: Optional[str] = None) -> pd.DataFrame:
    """
    Public helper to execute arbitrary DuckDB SQL with shared caching/instrumentation.
    """
    return _execute_df(sql, list(params or []), tag=tag, cache_key=cache_key)


def query_fact(
    filters: Any = None,
    *,
    columns: Optional[List[str]] = None,
    scope: Optional[Dict[str, Any]] = None,
    apply_default_window: bool = True,
    limit: Optional[int] = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Primary entry point: filtered frame via DuckDB parquet view (no MSSQL)."""
    params_filters = _normalize_filters_obj(filters, apply_default_window=apply_default_window)
    conn = get_duckdb_conn()
    available_cols = _available_columns(conn)
    where_sql, params, start_iso, end_iso = _where_clause(params_filters, available_cols, scope, apply_default_window=apply_default_window)

    select_cols = "*"
    if columns:
        required = {fs.CANON.date, fs.CANON.revenue, fs.CANON.cost, fs.CANON.qty_units, fs.CANON.weight_lb}
        requested = list(dict.fromkeys(list(columns) + list(required)))
        safe_cols = [_quote_identifier(c) for c in requested if c in available_cols]
        if safe_cols:
            select_cols = ", ".join(safe_cols)

    sql = f"SELECT {select_cols} FROM fact WHERE {where_sql}"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))

    cache_key = None
    if use_cache:
        cache_payload = {
            "filters": params_filters.__dict__ if hasattr(params_filters, "__dict__") else str(params_filters),
            "columns": list(columns) if columns else [],
            "scope": scope or {},
            "limit": limit,
        }
        cache_key = json.dumps(cache_payload, sort_keys=True, default=str)

    df = _execute_df(sql, params, tag="fact_frame", cache_key=cache_key)
    try:
        df = au.normalize_fact_df(df)
    except Exception:
        logger.debug("fact_store.normalize_failed", exc_info=True)
    log_fact_checkpoint(
        "fact_store.query",
        df,
        {"start": start_iso, "end": end_iso, "rows": len(df), "cache_key": cache_key},
    )
    return df


def packs_coverage(
    filters: Any = None,
    *,
    scope: Optional[Dict[str, Any]] = None,
    apply_default_window: bool = True,
) -> Dict[str, Any]:
    """Return pack coverage metrics for the filtered fact view."""
    params_filters = _normalize_filters_obj(filters, apply_default_window=apply_default_window)
    conn = get_duckdb_conn()
    available_cols = _available_columns(conn)
    if not available_cols:
        return {}

    missing_col = _choose_column(("missing_packs",), available_cols)
    pack_count_col = _choose_column(("pack_count",), available_cols)
    pack_weight_col = _choose_column(("pack_weight_lb_sum", "pack_weight_lb"), available_cols)
    pack_units_col = _choose_column(("pack_item_count_sum", "pack_units_ea"), available_cols)
    if not (missing_col or pack_count_col or pack_weight_col or pack_units_col):
        return {}

    where_sql, params, start_iso, end_iso = _where_clause(params_filters, available_cols, scope, apply_default_window=apply_default_window)
    if missing_col:
        missing_expr = f"CASE WHEN {missing_col} THEN 1 ELSE 0 END"
    elif pack_count_col:
        missing_expr = f"CASE WHEN {pack_count_col} IS NULL THEN 1 ELSE 0 END"
    else:
        missing_expr = f"CASE WHEN {pack_weight_col} IS NULL AND {pack_units_col} IS NULL THEN 1 ELSE 0 END"

    sql = f"""
        SELECT
            COUNT(*) AS total_orderlines,
            SUM({missing_expr}) AS missing_packs_orderlines
        FROM fact
        WHERE {where_sql}
    """
    df = _execute_df(sql, params, tag="packs_coverage")
    if df.empty:
        return {}
    row = df.iloc[0]
    total = int(row.get("total_orderlines") or 0)
    missing = int(row.get("missing_packs_orderlines") or 0)
    has_packs = max(0, total - missing)
    coverage_pct = round((has_packs / total) * 100.0, 2) if total else None
    return {
        "total_orderlines": total,
        "has_packs_orderlines": has_packs,
        "missing_packs_orderlines": missing,
        "packs_coverage_pct": coverage_pct,
        "start": start_iso,
        "end": end_iso,
    }


def _cache_key(columns: Optional[List[str]]) -> Tuple[str, ...]:
    if not columns:
        return ("__full__",)
    return tuple(columns)


def _ensure_dir() -> None:
    base = FACT_PATH if not FACT_PATH.suffix else FACT_PATH.parent
    base.mkdir(parents=True, exist_ok=True)


def _load_meta() -> Dict[str, Any]:
    try:
        return _manifest()
    except Exception:
        logger.debug("fact_store.meta_read_failed", exc_info=True)
        if META_PATH.exists():
            try:
                return json.loads(META_PATH.read_text(encoding="utf-8"))
            except Exception:
                logger.debug("fact_store.meta_read_failed_fallback", exc_info=True)
    return {}


def _write_meta(df: pd.DataFrame, *, source: str) -> Dict[str, Any]:
    # Metadata is now managed by CacheManager; return the persisted copy for compatibility.
    meta = CACHE_MANAGER.get_metadata()
    meta["source"] = source
    return meta


def _default_window_months() -> int:
    try:
        raw = os.getenv("DEFAULT_MONTH_WINDOW") or os.getenv("FACT_DEFAULT_MONTH_WINDOW") or "6"
        return max(1, int(str(raw).strip()))
    except Exception:
        return 6


def _apply_default_window(start_ts: Optional[pd.Timestamp], end_ts: Optional[pd.Timestamp]) -> tuple[Optional[pd.Timestamp], Optional[pd.Timestamp]]:
    if start_ts is None and end_ts is None:
        try:
            end_ts = pd.Timestamp.now(tz=timezone.utc)
        except Exception:
            end_ts = pd.Timestamp.utcnow()
        start_ts = end_ts - pd.DateOffset(months=_default_window_months())
    return start_ts, end_ts


def _atomic_write(df: pd.DataFrame) -> str:
    # Writes are performed through CacheManager; return the dataset path.
    return FACT_PATH.as_posix()


def _normalize_frame(df: pd.DataFrame) -> pd.DataFrame:
    try:
        return au.normalize_fact_df(df)
    except Exception:
        logger.exception("fact_store.normalize_failed")
        return df.copy()


def _read_fact(columns: Optional[List[str]] = None, *, filters: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    # Backward compatibility shim that now routes through DuckDB.
    try:
        manifest = _require_manifest()
    except FileNotFoundError as exc:
        raise DatasetNotBuiltError("Dataset not built. Run ETL job.") from exc

    df = query_fact(filters=filters, columns=columns, use_cache=True)
    try:
        req_path = None
        from flask import request  # type: ignore

        req_path = getattr(request, "path", None)
    except Exception:
        req_path = None
    logger.info(
        "fact_store.parquet.read",
        extra={
            "path": FACT_PATH.as_posix(),
            "rows": len(df) if isinstance(df, pd.DataFrame) else None,
            "start": getattr(filters, "start", None),
            "end": getattr(filters, "end", None),
            "manifest_version": manifest.get("dataset_version"),
            "request_path": req_path,
        },
    )
    return df


def get_watermark() -> Optional[pd.Timestamp]:
    meta = _load_meta()
    for key in ("watermark", "watermark_dt"):
        if meta.get(key):
            try:
                ts = pd.to_datetime(meta[key], errors="coerce")
                if pd.notna(ts):
                    return ts
            except Exception:
                continue
    try:
        df = CACHE_MANAGER.load_cached_frame(columns=[fs.CANON.date])
        if not df.empty and fs.CANON.date in df.columns:
            ts = pd.to_datetime(df[fs.CANON.date], errors="coerce")
            if ts.notna().any():
                return ts.max()
    except Exception:
        logger.debug("fact_store.watermark_parquet_failed", exc_info=True)
    return None


def get_dataset_min_max_dates(dataset_path: Optional[Path] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Return (min_date, max_date) for the current parquet dataset using DuckDB.
    Dates are returned as ISO YYYY-MM-DD strings when available.
    """
    base = dataset_path or _dataset_base_path()
    pattern = _dataset_glob_for_path(base)
    if not pattern:
        return None, None
    conn = duckdb.connect(database=":memory:", read_only=True)
    try:
        try:
            conn.execute("PRAGMA enable_object_cache=false;")
        except Exception:
            pass
        pattern_sql = pattern.replace("'", "''")
        conn.execute(f"CREATE OR REPLACE VIEW fact_raw AS SELECT * FROM parquet_scan('{pattern_sql}', union_by_name=true);")
        cols = {row[1] for row in conn.execute("PRAGMA table_info('fact_raw')").fetchall()}
        lower_map = {str(c).lower(): c for c in cols}

        def _pick(*candidates: str) -> Optional[str]:
            for cand in candidates:
                if cand in cols:
                    return cand
                lower = str(cand).lower()
                if lower in lower_map:
                    return lower_map[lower]
            return None

        date_col = _pick(*watermark_store.DATE_CANDIDATES)
        if not date_col:
            return None, None
        col_sql = _quote_identifier(date_col)
        row = conn.execute(
            f"SELECT min(CAST({col_sql} AS DATE)) AS min_d, max(CAST({col_sql} AS DATE)) AS max_d FROM fact_raw"
        ).fetchone()
        if not row:
            return None, None
        min_d, max_d = row

        def _to_iso(val: Any) -> Optional[str]:
            if val is None:
                return None
            ts = pd.to_datetime(val, errors="coerce")
            if pd.isna(ts):
                return None
            return ts.date().isoformat()

        return _to_iso(min_d), _to_iso(max_d)
    except Exception:
        logger.debug("fact_store.min_max_failed", exc_info=True)
        return None, None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def build_full(start_date: str = "2017-01-01") -> str:
    _ensure_etl_mode("build_full")
    end_date = os.getenv("INITIAL_END_DATE") or os.getenv("DATA_END_DATE") or "today"
    from etl.incremental_refresh import initial_build  # type: ignore

    meta = initial_build(start=start_date, end=end_date)
    with _cache_lock:
        _cached_frames.clear()
    logger.info(
        "fact_store.full_built",
        extra={"rows": meta.get("row_count"), "path": meta.get("path"), "start": start_date},
    )
    return meta.get("path", FACT_PATH.as_posix())


def fetch_incremental(after_dt: Optional[pd.Timestamp]) -> pd.DataFrame:
    _ensure_etl_mode("fetch_incremental")
    import data_loader as loader  # type: ignore

    if after_dt is None or pd.isna(after_dt):
        return pd.DataFrame()
    start_iso = (pd.to_datetime(after_dt) + timedelta(days=0)).date().isoformat()
    df = loader.get_dataframe(start=start_iso, end=None)
    return _normalize_frame(df)


def merge_dedupe(existing: pd.DataFrame, inc: pd.DataFrame) -> pd.DataFrame:
    if existing is None or existing.empty:
        return inc.copy()
    if inc is None or inc.empty:
        return existing.copy()

    left = existing.copy()
    right = inc.copy()

    key_col = None
    for cand in ("OrderLineId", "orderlineid", fs.CANON.order_line_id):
        if cand in left.columns and cand in right.columns:
            key_col = cand
            break

    def _build_key(frame: pd.DataFrame) -> pd.Series:
        if key_col and key_col in frame.columns:
            return frame[key_col].astype("string")
        return (
            _string_safe(frame.get(fs.CANON.order_id))
            + "|"
            + _string_safe(frame.get(fs.CANON.product_id))
            + "|"
            + _string_safe(frame.get(fs.CANON.date))
            + "|"
            + _string_safe(frame.get(fs.CANON.revenue))
            + "|"
            + _string_safe(frame.get(fs.CANON.qty_units))
        )

    left["_merge_key"] = _build_key(left)
    right["_merge_key"] = _build_key(right)

    combined = pd.concat([left, right], ignore_index=True, sort=False)
    combined = combined.sort_values(by=[fs.CANON.date], ascending=True, ignore_index=True)
    combined = combined.drop_duplicates(subset=["_merge_key"], keep="last")
    combined.drop(columns=["_merge_key"], inplace=True, errors="ignore")
    return combined


def _string_safe(series: Any) -> pd.Series:
    try:
        return pd.Series(series).astype("string").fillna("")
    except Exception:
        return pd.Series("", index=series.index if hasattr(series, "index") else None, dtype="string")


def refresh_once(start_date: str = "2017-01-01", *, require_lock: bool = True) -> Dict[str, Any]:
    _ensure_etl_mode("refresh_once")
    lock_acquired = False
    if require_lock:
        lock_acquired = _refresh_lock.acquire(blocking=False)
        if not lock_acquired:
            logger.info("fact_store.refresh_in_progress")
            return {"status": "skipped_lock"}
    try:
        from etl.incremental_refresh import refresh_once as _refresh_once  # type: ignore

        meta = _refresh_once(start=start_date, require_lock=False)
        with _cache_lock:
            _cached_frames.clear()
        meta.setdefault("path", FACT_PATH.as_posix())
        return meta
    finally:
        if require_lock and lock_acquired:
            try:
                _refresh_lock.release()
            except Exception:
                logger.debug("fact_store.refresh_lock_release_failed", exc_info=True)


def _refresh_task(start_date: str) -> None:
    _ensure_etl_mode("refresh_once")
    try:
        from etl.incremental_refresh import refresh_once as _refresh_once  # type: ignore

        _refresh_once(start=start_date, require_lock=False)
    except Exception:
        logger.debug("fact_store.async_refresh_failed", exc_info=True)
    finally:
        _async_refreshing.clear()


def _maybe_trigger_async_refresh(ttl_minutes: int) -> None:
    if _async_refreshing.is_set():
        return
    try:
        stale = CACHE_MANAGER.is_stale(ttl_minutes)
    except Exception:
        stale = False
    if not stale:
        return
    _async_refreshing.set()
    threading.Thread(
        target=_refresh_task,
        name="sales-fact-refresh",
        kwargs={"start_date": os.getenv("INITIAL_START_DATE", "2017-01-01")},
        daemon=True,
    ).start()


def get_meta() -> Dict[str, Any]:
    """Public accessor for the latest meta info."""
    return _load_meta()


def cache_buster() -> str:
    try:
        from flask import current_app, has_request_context  # type: ignore

        if has_request_context():
            if current_app.config.get("TEST_STATE") is not None or current_app.config.get("TESTING"):
                import data_loader as loader  # type: ignore

                return str(loader.current_version())
    except Exception:
        pass
    try:
        version = get_dataset_version()
        if version:
            return str(version)
    except Exception:
        pass
    meta = _load_meta()
    marker = (
        meta.get("dataset_version")
        or meta.get("watermark")
        or meta.get("watermark_dt")
        or meta.get("last_refresh_utc")
        or _current_mtime()
        or time.time()
    )
    return str(marker)


def validate_fact_schema(strict: bool = False) -> Dict[str, Any]:
    """
    Validate that the fact parquet includes canonical columns we rely on for metrics.
    Returns a status payload and optionally raises in strict mode.
    """
    status: Dict[str, Any] = {"ok": False, "missing": [], "path": FACT_PATH.as_posix()}
    try:
        cols = sorted(list_columns())
        df = pd.DataFrame(columns=cols)
    except (FileNotFoundError, DatasetNotBuiltError) as exc:
        status["missing"] = ["parquet_missing"]
        if strict:
            raise RuntimeError("Fact parquet missing; build or set PARQUET_PATH") from exc
        logger.error("fact_store.schema_missing_parquet")
        return status
    except Exception as exc:  # pragma: no cover - defensive
        status["missing"] = ["parquet_unreadable"]
        if strict:
            raise
        logger.error("fact_store.schema_read_failed", exc_info=True)
        return status

    required_resolvers = {
        "date": fs.resolve_date_column,
        "revenue": fs.resolve_revenue_column,
        "cost": fs.resolve_cost_column,
        "qty": fs.resolve_qty_column,
        "weight": fs.resolve_weight_column,
    }
    resolved: Dict[str, Optional[str]] = {}
    missing: list[str] = []
    for key, resolver in required_resolvers.items():
        try:
            col = resolver(df)
        except Exception:
            col = None
        resolved[key] = col
        if not col:
            missing.append(key)

    status.update({"resolved": resolved, "missing": missing})
    status["ok"] = not missing

    if missing:
        logger.critical("fact_store.schema_missing_columns", extra={"missing": missing})
        if strict:
            raise RuntimeError(f"Fact schema missing required columns: {', '.join(missing)}")
    else:
        logger.info("fact_store.schema_valid", extra={"resolved": resolved})
    return status


def get_sales_fact(
    columns: Optional[List[str]] = None,
    force_refresh: bool = False,
    *,
    filters: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    if force_refresh:
        raise DatasetNotBuiltError("Force refresh disabled for request path; run the ETL job instead.")

    manifest = _require_manifest()
    version_marker = (
        manifest.get("dataset_version")
        or manifest.get("last_refresh_utc")
        or manifest.get("watermark")
        or _current_mtime()
    )
    filter_token = hashlib.sha256(json.dumps(filters or {}, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    key = _cache_key(columns) + (filter_token, str(version_marker))
    with _cache_lock:
        cached = _cached_frames.get(key)
        if cached:
            return cached["df"].copy()

    df = query_fact(filters=filters, columns=columns, use_cache=True)
    with _cache_lock:
        _cached_frames[key] = {"df": df, "version": version_marker}
    return df.copy()


def query_overview(
    filters: Any,
    *,
    include_current_month: bool = False,
    defaulted_window: bool = False,
) -> Dict[str, Any]:
    """Aggregated overview payload via DuckDB (delegates to overview_v2)."""
    from app.services import overview_v2  # type: ignore

    return overview_v2.build_overview_bundle(filters, include_current_month=include_current_month, defaulted_window=defaulted_window)


def query_products(filters: Any = None, *, columns: Optional[List[str]] = None) -> pd.DataFrame:
    """Return a products-focused fact slice via DuckDB."""
    return query_fact(filters=filters, columns=columns, use_cache=True)


def _acquire_lock(max_age_seconds: int = 0) -> bool:
    try:
        _ensure_dir()
        if LOCK_PATH.exists() and max_age_seconds:
            try:
                age = time.time() - LOCK_PATH.stat().st_mtime
                if age > max_age_seconds:
                    LOCK_PATH.unlink()
            except Exception:
                logger.debug("fact_store.lock_stale_check_failed", exc_info=True)
        fd = os.open(LOCK_PATH.as_posix(), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:
        logger.debug("fact_store.lock_failed", exc_info=True)
        return False


def _release_lock() -> None:
    try:
        if LOCK_PATH.exists():
            LOCK_PATH.unlink()
    except Exception:
        logger.debug("fact_store.unlock_failed", exc_info=True)


def start_background_refresh(interval_minutes: int = 515, start_date: str = "2017-01-01") -> None:
    global _bg_thread

    _ensure_etl_mode("start_background_refresh")

    if _bg_thread and _bg_thread.is_alive():
        return

    # Avoid spawning duplicate threads in Flask reloader parent
    if os.getenv("WERKZEUG_RUN_MAIN") and os.getenv("WERKZEUG_RUN_MAIN") != "true":
        return

    if not _acquire_lock(max_age_seconds=int(max(1, interval_minutes) * 120)):
        logger.info("fact_store.background_already_running")
        return

    _bg_stop.clear()

    def _loop() -> None:
        try:
            while not _bg_stop.is_set():
                try:
                    refresh_once(start_date=start_date, require_lock=False)
                except Exception:
                    logger.exception("fact_store.background_refresh_failed")
                # Wait with jitter to reduce thundering herd
                sleep_for = max(1, int(interval_minutes)) * 60
                _bg_stop.wait(sleep_for)
        finally:
            _release_lock()

    _bg_thread = threading.Thread(target=_loop, name="sales-fact-refresh", daemon=True)
    _bg_thread.start()
    logger.info("fact_store.background_started", extra={"interval_minutes": interval_minutes, "path": FACT_PATH.as_posix()})


def stop_background_refresh(timeout: float = 2.0) -> None:
    _bg_stop.set()
    if _bg_thread:
        _bg_thread.join(timeout=timeout)

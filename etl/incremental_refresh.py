from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds

import data_loader as loader  # type: ignore
from app.services.cache_manager import FileLock
from app.services import etl_state, watermark_store
from etl import partition_writer

# ETL runs are the only place live SQL is allowed.
os.environ.setdefault("APP_MODE", "etl")
os.environ.setdefault("ALLOW_LIVE_SQL", "1")

MIN_START_DATE = "2017-01-01"
logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return int(default)
    try:
        return int(str(raw).strip())
    except Exception:
        return int(default)


def _refresh_tz_name() -> str:
    return (os.getenv("FACT_REFRESH_TZ") or "America/Vancouver").strip() or "America/Vancouver"


def _resolve_refresh_timezone() -> ZoneInfo:
    tz_name = _refresh_tz_name()
    try:
        return ZoneInfo(tz_name)
    except Exception:
        logger.warning("Invalid FACT_REFRESH_TZ=%s; falling back to UTC", tz_name)
        return ZoneInfo("UTC")


def _now_in_refresh_tz(tz: Optional[ZoneInfo] = None) -> datetime:
    zone = tz or _resolve_refresh_timezone()
    return datetime.now(timezone.utc).astimezone(zone)


def _today_in_refresh_tz(tz: Optional[ZoneInfo] = None) -> date:
    return _now_in_refresh_tz(tz).date()


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parquet_exists(dataset_path: Path) -> bool:
    try:
        if not dataset_path.exists():
            return False
        if dataset_path.is_file():
            return dataset_path.suffix == ".parquet"
        return any(dataset_path.rglob("*.parquet"))
    except Exception:
        return False


def _resolve_lock_path(dataset_path: Path) -> Path:
    cache_dir = etl_state.resolve_cache_dir(dataset_path)
    return cache_dir / ".refresh.lock"

def _dataset_base_path(dataset_path: Optional[Path] = None) -> Path:
    # Keep ETL path resolution consistent with app reads (see watermark_store).
    base = Path(dataset_path).expanduser().resolve() if dataset_path is not None else watermark_store.resolve_dataset_path()
    # If a caller passes a snapshot parquet file, treat the adjacent dataset dir as canonical.
    if base.suffix == ".parquet":
        return base.parent / "fact_dataset"
    return base


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


def _quote_identifier(name: str) -> str:
    safe = str(name).replace('"', '""')
    return f'"{safe}"'


def _query_available_dates(
    dataset_path: Path,
    *,
    date_col: str,
    start_date: date,
) -> List[date]:
    pattern = _dataset_glob_for_path(dataset_path)
    if not pattern:
        return []
    conn = duckdb.connect()
    col = _quote_identifier(date_col)
    start_sql = start_date.isoformat()
    query = (
        f"select distinct cast({col} as date) d "
        f"from read_parquet('{pattern}', union_by_name=true) "
        f"where cast({col} as date) >= date '{start_sql}'"
    )
    try:
        rows = conn.execute(query).fetchall()
    except Exception:
        logger.exception("fact_refresh.gap_query_failed", extra={"pattern": pattern, "date_col": date_col})
        return []
    dates: List[date] = []
    for (val,) in rows:
        if isinstance(val, datetime):
            dates.append(val.date())
        elif isinstance(val, date):
            dates.append(val)
    return dates


def _expected_dates(start: date, end: date) -> List[date]:
    if start > end:
        return []
    days = (end - start).days
    return [start + timedelta(days=offset) for offset in range(days + 1)]


def _coalesce_missing_ranges(missing_dates: Iterable[date]) -> List[Tuple[date, date]]:
    missing = sorted(set(missing_dates))
    if not missing:
        return []
    ranges: List[Tuple[date, date]] = []
    start = missing[0]
    prev = missing[0]
    for current in missing[1:]:
        if current == prev + timedelta(days=1):
            prev = current
            continue
        ranges.append((start, prev))
        start = current
        prev = current
    ranges.append((start, prev))
    return ranges


def _find_gap_ranges(
    *,
    dataset_path: Path,
    date_col: str,
    backfill_days: int,
) -> List[Tuple[date, date]]:
    if backfill_days <= 0:
        return []
    today = _today_in_refresh_tz()
    start = today - timedelta(days=max(0, backfill_days - 1))
    available = _query_available_dates(dataset_path, date_col=date_col, start_date=start)
    expected = _expected_dates(start, today)
    missing = set(expected) - set(available)
    return _coalesce_missing_ranges(missing)


def _log_dataset_metrics(dataset_path: Path, *, label: str) -> None:
    pattern = _dataset_glob_for_path(dataset_path)
    if not pattern:
        return
    conn = duckdb.connect()
    try:
        conn.execute(
            f"CREATE OR REPLACE VIEW fact_raw AS SELECT * FROM read_parquet('{pattern}', union_by_name=true);"
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info('fact_raw')").fetchall()}
        if "DateExpected" not in cols:
            return
        today = _today_in_refresh_tz()
        month_start = date(today.year, today.month, 1)
        month_end = (month_start + timedelta(days=32)).replace(day=1)
        jan_start = date(2026, 1, 1)
        jan_end = date(2026, 2, 1)
        last14_start = today - timedelta(days=13)
        last14_end = today + timedelta(days=1)
        col = _quote_identifier("DateExpected")

        min_max = conn.execute(
            f"select min(cast({col} as date)), max(cast({col} as date)) from fact_raw"
        ).fetchone()
        order_col = "OrderId" if "OrderId" in cols else None
        order_expr = f"count(distinct {_quote_identifier(order_col)})" if order_col else "NULL"

        def _count_window(start_d: date, end_d: date) -> Tuple[int, Optional[int]]:
            rows = conn.execute(
                f"select count(*), {order_expr} from fact_raw "
                f"where cast({col} as date) >= date '{start_d.isoformat()}' "
                f"and cast({col} as date) < date '{end_d.isoformat()}'"
            ).fetchone()
            return int(rows[0] or 0), (int(rows[1]) if rows[1] is not None else None)

        month_rows, month_orders = _count_window(month_start, month_end)
        jan_rows, jan_orders = _count_window(jan_start, jan_end)
        last14_rows, last14_orders = _count_window(last14_start, last14_end)

        logger.info(
            "fact_refresh.metrics",
            extra={
                "label": label,
                "min_dateexpected": min_max[0].isoformat() if min_max and min_max[0] else None,
                "max_dateexpected": min_max[1].isoformat() if min_max and min_max[1] else None,
                "current_month_start": month_start.isoformat(),
                "current_month_rows": month_rows,
                "current_month_orders": month_orders,
                "jan_2026_rows": jan_rows,
                "jan_2026_orders": jan_orders,
                "last14_start": last14_start.isoformat(),
                "last14_rows": last14_rows,
                "last14_orders": last14_orders,
            },
        )
    except Exception:
        logger.exception("fact_refresh.metrics_failed", extra={"label": label})


def _schema_fingerprint(df: pd.DataFrame) -> str:
    cols = [{"name": c, "dtype": str(dtype)} for c, dtype in df.dtypes.items()]
    raw = json.dumps(cols, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_date(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    text = str(raw).strip().lower()
    if text in {"today", "now"}:
        return _today_in_refresh_tz()
    ts = pd.to_datetime(raw, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.date()


def _parse_datetime(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    text = str(raw).strip().lower()
    if text in {"now", "today"}:
        return _now_in_refresh_tz().astimezone(timezone.utc)
    ts = pd.to_datetime(raw, errors="coerce", utc=True)
    if pd.isna(ts):
        return None
    try:
        return ts.to_pydatetime()
    except Exception:
        return datetime.fromtimestamp(ts.timestamp(), tz=timezone.utc)

def _normalize_date_param(raw: Optional[date | datetime | str]) -> Optional[date]:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    return _parse_date(str(raw))

def _to_utc_naive(raw: Optional[date | datetime | str]) -> Optional[datetime]:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, date):
        dt = datetime.combine(raw, datetime.min.time())
    else:
        dt = _parse_datetime(str(raw))
        if dt is None:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=None)


def _choose_updated_column(df: pd.DataFrame) -> Optional[str]:
    if df is None or df.empty:
        return None
    for col in watermark_store.WATERMARK_CANDIDATES:
        if col in df.columns:
            return col
    return None


def _max_datetime_iso(df: pd.DataFrame, col: Optional[str]) -> Optional[str]:
    if not col or col not in df.columns:
        return None
    ts = pd.to_datetime(df[col], errors="coerce", utc=True)
    if ts.notna().any():
        val = ts.max()
        try:
            val = val.tz_convert(None) if val.tzinfo else val.tz_localize(None)
        except Exception:
            pass
        return val.isoformat()
    return None


def _max_date_iso(df: pd.DataFrame, col: Optional[str]) -> Optional[str]:
    if not col or col not in df.columns:
        return None
    ts = pd.to_datetime(df[col], errors="coerce")
    if ts.notna().any():
        return ts.max().tz_localize(None).isoformat()
    return None


def _dedupe_latest(
    df: pd.DataFrame,
    *,
    pk_col: str,
    updated_col: Optional[str],
    date_col: Optional[str],
) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    if pk_col not in df.columns:
        return df.drop_duplicates(keep="last")
    work = df.copy()
    sort_cols: List[str] = []
    ascending: List[bool] = []
    if updated_col and updated_col in work.columns:
        work[updated_col] = pd.to_datetime(work[updated_col], errors="coerce")
        sort_cols.append(updated_col)
        ascending.append(True)
    elif date_col and date_col in work.columns:
        work[date_col] = pd.to_datetime(work[date_col], errors="coerce")
        sort_cols.append(date_col)
        ascending.append(True)
    if "OrderId" in work.columns:
        sort_cols.append("OrderId")
        ascending.append(True)
    if sort_cols:
        work = work.sort_values(by=sort_cols, ascending=ascending, kind="mergesort", ignore_index=True)
    return work.drop_duplicates(subset=[pk_col], keep="last")


def _touched_partitions(df: pd.DataFrame, date_col: str) -> List[Tuple[int, int]]:
    if df is None or df.empty or date_col not in df.columns:
        return []
    ts = pd.to_datetime(df[date_col], errors="coerce")
    years = ts.dt.year.astype("Int64")
    months = ts.dt.month.astype("Int64")
    pairs = {(int(y), int(m)) for y, m in zip(years, months) if pd.notna(y) and pd.notna(m)}
    return sorted(pairs)


def _iter_month_ranges(start: date, end: date, months_step: int = 1) -> Iterable[Tuple[date, date]]:
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        step = max(1, int(months_step))
        next_month = (pd.Timestamp(cursor) + pd.DateOffset(months=step)).date()
        month_end = next_month - timedelta(days=1)
        range_start = max(cursor, start)
        range_end = min(month_end, end)
        yield range_start, range_end
        cursor = next_month


def _add_partitions(df: pd.DataFrame, date_col: str) -> pd.DataFrame:
    work = df.copy()
    ts = pd.to_datetime(work[date_col], errors="coerce")
    work["year"] = ts.dt.year.astype("Int64")
    work["month"] = ts.dt.month.astype("Int64")
    return work


def _dedupe(df: pd.DataFrame, pk_col: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    date_col = watermark_store.choose_date_column(df) or "DateExpected"
    updated_col = _choose_updated_column(df)
    return _dedupe_latest(df, pk_col=pk_col, updated_col=updated_col, date_col=date_col)


def _resolve_end_date(end: Optional[str]) -> date:
    parsed = _parse_date(end)
    if parsed:
        return parsed
    return _today_in_refresh_tz()


def initial_build(
    *,
    start: str = MIN_START_DATE,
    end: Optional[str] = None,
    dataset_path: Optional[Path] = None,
    chunk_months: int = 1,
    require_lock: bool = True,
    keep_prev: bool = False,
    state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    dataset_path = dataset_path or watermark_store.resolve_dataset_path()
    lock_path = _resolve_lock_path(dataset_path)
    lock = FileLock(lock_path)
    acquired = lock.acquire() if require_lock else True
    if not acquired:
        return {"status": "locked"}
    try:
        end_date = _resolve_end_date(end)
        start_date = _parse_date(start) or date(2017, 1, 1)
        if state is None:
            state = etl_state.load_state(dataset_path=dataset_path)
        state_enabled = True
        tmp_dir = dataset_path.parent / f".{dataset_path.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        total_rows = 0
        min_date = None
        max_date = None
        watermark_val = None
        watermark_col = None
        updated_max = None
        schema_hash = None
        date_col = None

        for rng_start, rng_end in _iter_month_ranges(start_date, end_date, months_step=chunk_months):
            logger.info(
                "fact_refresh.chunk_start mode=initial start=%s end=%s",
                rng_start.isoformat(),
                rng_end.isoformat(),
                extra={"refresh_mode": "initial", "start": rng_start.isoformat(), "end": rng_end.isoformat()},
            )
            df = loader.get_dataframe(start=rng_start, end=rng_end)
            if df is None or df.empty:
                continue
            if schema_hash is None:
                schema_hash = _schema_fingerprint(df)
            if date_col is None:
                date_col = watermark_store.choose_date_column(df) or "DateExpected"
            if date_col not in df.columns:
                df[date_col] = pd.NaT
            df = _add_partitions(df, date_col)

            dates = pd.to_datetime(df[date_col], errors="coerce")
            if dates.notna().any():
                dmin = dates.min()
                dmax = dates.max()
                if dmin is not None:
                    min_date = dmin if min_date is None else min(min_date, dmin)
                if dmax is not None:
                    max_date = dmax if max_date is None else max(max_date, dmax)

            chunk_watermark, chunk_col = watermark_store.compute_watermark(df)
            if chunk_watermark:
                try:
                    if watermark_val is None or pd.to_datetime(chunk_watermark) > pd.to_datetime(watermark_val):
                        watermark_val = chunk_watermark
                        watermark_col = chunk_col
                except Exception:
                    watermark_val = chunk_watermark
                    watermark_col = chunk_col

            updated_col = _choose_updated_column(df)
            chunk_updated_max = _max_datetime_iso(df, updated_col)
            if chunk_updated_max:
                try:
                    if updated_max is None or pd.to_datetime(chunk_updated_max) > pd.to_datetime(updated_max):
                        updated_max = chunk_updated_max
                except Exception:
                    updated_max = chunk_updated_max

            chunk_date_max = _max_date_iso(df, date_col)
            if state_enabled:
                state = etl_state.set_watermark(
                    state or {},
                    dateexpected_max=chunk_date_max,
                    updated_at_max=chunk_updated_max,
                )
                state["rows_last_pull"] = int(len(df))
                state["last_success_utc"] = _now_utc_iso()
                state["last_success"] = state["last_success_utc"]
                state["last_error"] = None
                etl_state.save_state(state, dataset_path=dataset_path)

            table = pa.Table.from_pandas(df, preserve_index=False)
            ds.write_dataset(
                data=table,
                base_dir=tmp_dir.as_posix(),
                format="parquet",
                partitioning=["year", "month"],
                partitioning_flavor="hive",
                existing_data_behavior="overwrite_or_ignore",
            )
            total_rows += int(len(df))
            logger.info(
                "fact_refresh.chunk_complete mode=initial start=%s end=%s rows=%s",
                rng_start.isoformat(),
                rng_end.isoformat(),
                int(len(df)),
                extra={
                    "refresh_mode": "initial",
                    "start": rng_start.isoformat(),
                    "end": rng_end.isoformat(),
                    "rows": int(len(df)),
                },
            )

        if total_rows == 0:
            raise RuntimeError("Initial build produced no rows.")

        min_iso = pd.to_datetime(min_date, errors="coerce").tz_localize(None).isoformat() if min_date is not None else None
        max_iso = pd.to_datetime(max_date, errors="coerce").tz_localize(None).isoformat() if max_date is not None else None
        built_at = _now_utc_iso()
        manifest = {
            "dataset_version": str(int(time.time() * 1000)),
            "path": dataset_path.as_posix(),
            "row_count": total_rows,
            "rows": total_rows,
            "min_date": min_iso,
            "max_date": max_iso,
            "min_dateexpected": min_iso,
            "max_dateexpected": max_iso,
            "watermark": watermark_val,
            "last_sql_watermark": watermark_val,
            "watermark_column": watermark_col,
            "incremental_column": watermark_col,
            "date_column": date_col,
            "schema_hash": schema_hash,
            "schema_fingerprint": schema_hash,
            "built_at_utc": built_at,
            "built_at": built_at,
            "last_refresh_utc": built_at,
            "status": "bootstrapped",
        }
        watermark_store.write_manifest_atomic(manifest, dataset_path=tmp_dir)
        partition_writer._swap_dataset_dirs(tmp_dir, dataset_path, keep_prev=keep_prev)
        if state_enabled:
            state = etl_state.set_watermark(
                state or {},
                dateexpected_max=max_iso,
                updated_at_max=updated_max,
            )
            state["initial_load_done"] = True
            state["dataset_version"] = manifest.get("dataset_version")
            state["rows_last_pull"] = int(total_rows)
            state["last_success_utc"] = built_at
            state["last_success"] = built_at
            state["last_error"] = None
            etl_state.save_state(state, dataset_path=dataset_path)
        logger.info(
            "fact_refresh.complete mode=initial rows=%s dataset_version=%s watermark=%s",
            int(total_rows),
            manifest.get("dataset_version"),
            manifest.get("watermark"),
            extra={
                "refresh_mode": "initial",
                "rows": int(total_rows),
                "dataset_version": manifest.get("dataset_version"),
                "watermark": manifest.get("watermark"),
            },
        )
        return manifest
    finally:
        if require_lock and acquired:
            lock.release()


def incremental_refresh(
    *,
    manifest: Optional[Dict[str, Any]] = None,
    lookback_days: Optional[int] = None,
    min_start: str = MIN_START_DATE,
    window_start: Optional[date] = None,
    window_end: Optional[date] = None,
    updated_after: Optional[date | datetime | str] = None,
) -> pd.DataFrame:
    """
    Incremental pull for a given date window (half-open end handled by loader).
    If window_start is not supplied, it is derived from the manifest + lookback_days.
    """
    if window_start is None and manifest:
        watermark = watermark_store.get_watermark(manifest)
        watermark_dt = watermark_store.watermark_to_date(watermark)
        fallback_max = manifest.get("max_dateexpected") or manifest.get("max_date")
        if watermark_dt is None and fallback_max:
            watermark_dt = _parse_date(str(fallback_max))
        min_start_dt = _parse_date(min_start) or date(2017, 1, 1)
        lb = lookback_days if lookback_days is not None else watermark_store.LOOKBACK_DAYS
        if watermark_dt:
            window_start = max(min_start_dt, watermark_dt - timedelta(days=lb))
        else:
            window_start = min_start_dt
        if updated_after is None:
            inc_col = manifest.get("watermark_column") or manifest.get("incremental_column")
            if inc_col in watermark_store.WATERMARK_CANDIDATES:
                updated_after = watermark
    if window_start is None:
        return pd.DataFrame()
    if window_end is None:
        window_end = _today_in_refresh_tz()
    start_param = _normalize_date_param(window_start)
    end_param = _normalize_date_param(window_end)
    updated_after_param = _to_utc_naive(updated_after)
    end_exclusive_param = end_param + timedelta(days=1) if end_param else None
    logger.info(
        "fact_refresh.params start=%s start_type=%s end_excl=%s end_excl_type=%s updated_after=%s updated_after_type=%s",
        start_param,
        type(start_param).__name__ if start_param is not None else None,
        end_exclusive_param,
        type(end_exclusive_param).__name__ if end_exclusive_param is not None else None,
        updated_after_param,
        type(updated_after_param).__name__ if updated_after_param is not None else None,
        extra={
            "refresh_mode": "incremental",
            "start": start_param,
            "end_exclusive": end_exclusive_param,
            "updated_after": updated_after_param,
            "start_type": type(start_param).__name__ if start_param is not None else None,
            "end_exclusive_type": type(end_exclusive_param).__name__ if end_exclusive_param is not None else None,
            "updated_after_type": type(updated_after_param).__name__ if updated_after_param is not None else None,
        },
    )
    return loader.get_dataframe(
        start=start_param,
        end=end_param,
        updated_after=updated_after_param,
    )


def _gap_backfill(
    *,
    dataset_path: Path,
    manifest: Dict[str, Any],
    backfill_days: int,
    keep_prev: bool,
) -> Dict[str, Any]:
    date_col = manifest.get("date_column") or "DateExpected"
    ranges = _find_gap_ranges(dataset_path=dataset_path, date_col=date_col, backfill_days=backfill_days)
    if not ranges:
        logger.info("fact_refresh.gaps.none", extra={"backfill_days": backfill_days})
        return manifest

    logger.info(
        "fact_refresh.gaps.found",
        extra={
            "backfill_days": backfill_days,
            "ranges": [(s.isoformat(), e.isoformat()) for s, e in ranges],
        },
    )
    current_manifest = dict(manifest)
    for start_dt, end_dt in ranges:
        logger.info(
            "fact_refresh.gap_backfill.start",
            extra={"start": start_dt.isoformat(), "end": end_dt.isoformat()},
        )
        refreshed = loader.get_dataframe(start=start_dt, end=end_dt, updated_after=None)
        if refreshed is None or refreshed.empty:
            logger.info(
                "fact_refresh.gap_backfill.empty",
                extra={"start": start_dt.isoformat(), "end": end_dt.isoformat()},
            )
            continue
        date_col = current_manifest.get("date_column") or watermark_store.choose_date_column(refreshed) or "DateExpected"
        updated_col = _choose_updated_column(refreshed)
        deduped = _dedupe_latest(refreshed, pk_col="OrderLineId", updated_col=updated_col, date_col=date_col)

        schema_hash = _schema_fingerprint(deduped)
        updates = {
            "dataset_version": str(int(time.time() * 1000)),
            "schema_hash": schema_hash,
            "schema_fingerprint": schema_hash,
            "last_refresh_utc": _now_utc_iso(),
            "status": "gap_backfilled",
        }
        current_manifest = partition_writer.upsert_dataset(
            deduped,
            dataset_path=dataset_path,
            pk_col="OrderLineId",
            date_col=date_col,
            existing_manifest=current_manifest,
            manifest_updates=updates,
            keep_prev=keep_prev,
        )
        logger.info(
            "fact_refresh.gap_backfill.complete",
            extra={
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "rows": int(len(deduped)),
            },
        )
    return current_manifest


def gap_backfill_only(
    *,
    dataset_path: Optional[Path] = None,
    backfill_days: Optional[int] = None,
    keep_prev: bool = False,
    require_lock: bool = True,
) -> Dict[str, Any]:
    dataset_path = _dataset_base_path(dataset_path)
    lock_path = _resolve_lock_path(dataset_path)
    lock = FileLock(lock_path)
    acquired = lock.acquire() if require_lock else True
    if not acquired:
        return {"status": "locked"}
    try:
        manifest = watermark_store.read_manifest(dataset_path)
        if not _parquet_exists(dataset_path):
            return {"status": "error", "error": "missing_dataset"}
        backfill = backfill_days if backfill_days is not None else int(os.getenv("FACT_REFRESH_BACKFILL_DAYS", "90"))
        updated = _gap_backfill(
            dataset_path=dataset_path,
            manifest=manifest,
            backfill_days=backfill,
            keep_prev=keep_prev,
        )
        _log_dataset_metrics(dataset_path, label="gap_backfill")
        return updated
    finally:
        if require_lock and acquired:
            lock.release()


def _resolve_watermarks(state: Dict[str, Any], manifest: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    dateexpected_max, updated_at_max = etl_state.get_watermark(state)
    if not dateexpected_max:
        dateexpected_max = manifest.get("max_dateexpected") or manifest.get("max_date")
    if not updated_at_max:
        col = manifest.get("watermark_column") or manifest.get("incremental_column")
        if col in watermark_store.WATERMARK_CANDIDATES:
            updated_at_max = manifest.get("watermark") or manifest.get("last_sql_watermark")
    return dateexpected_max, updated_at_max


def _compute_incremental_window(
    *,
    state: Dict[str, Any],
    manifest: Dict[str, Any],
    lag_days: int,
    hot_window_days: int,
    min_start: str,
) -> Tuple[Optional[date], Optional[date], Optional[datetime], Dict[str, Any]]:
    dateexpected_max, updated_at_max = _resolve_watermarks(state, manifest)
    min_start_dt = _parse_date(min_start) or date(2017, 1, 1)
    refresh_tz = _resolve_refresh_timezone()
    today_local = _today_in_refresh_tz(refresh_tz)

    lag_anchor: Optional[date] = None
    if updated_at_max:
        updated_dt = _parse_datetime(str(updated_at_max))
        if updated_dt:
            lag_anchor = updated_dt.astimezone(refresh_tz).date() if updated_dt.tzinfo else updated_dt.date()
    if lag_anchor is None and dateexpected_max:
        lag_anchor = _parse_date(str(dateexpected_max))
    if lag_anchor is None:
        lag_anchor = today_local

    # Keep the configured lag visible for diagnostics and rollback analysis.
    lag_based_start = lag_anchor - timedelta(days=max(0, lag_days))
    # Deterministic rewrite window: always refresh a fixed hot window so
    # ongoing-month rows cannot be skipped by an advanced SQL watermark.
    hot_window_start = today_local - timedelta(days=max(0, hot_window_days))

    start_dt = max(min_start_dt, hot_window_start)
    end_dt = today_local

    details = {
        "dateexpected_max": dateexpected_max,
        "updated_at_max": updated_at_max,
        "timezone": _refresh_tz_name(),
        "today_local": today_local.isoformat(),
        "lag_anchor": lag_anchor.isoformat() if lag_anchor else None,
        "lag_based_start": lag_based_start.isoformat(),
        "hot_window_start": hot_window_start.isoformat(),
    }
    # updated_after intentionally disabled for hot-window pulls.
    return start_dt, end_dt, None, details


def refresh_once(
    *,
    start: str = MIN_START_DATE,
    lookback_days: Optional[int] = None,
    backfill_days: Optional[int] = None,
    mode: Optional[str] = None,
    dataset_path: Optional[Path] = None,
    keep_prev: bool = False,
    require_lock: bool = True,
) -> Dict[str, Any]:
    dataset_path = _dataset_base_path(dataset_path)
    lock_path = _resolve_lock_path(dataset_path)
    lock = FileLock(lock_path)
    acquired = lock.acquire() if require_lock else True
    if not acquired:
        return {"status": "locked"}
    try:
        state_path = etl_state.resolve_state_path(dataset_path=dataset_path)
        state = etl_state.load_state(dataset_path=dataset_path)
        manifest = watermark_store.read_manifest(dataset_path)
        parquet_exists = _parquet_exists(dataset_path)
        state = etl_state.bootstrap_state_if_missing(state, dataset_path=dataset_path)
        initial_done = bool(state.get("initial_load_done"))
        refresh_mode = (mode or "full").strip().lower().replace("_", "-")
        backfill = backfill_days if backfill_days is not None else int(os.getenv("FACT_REFRESH_BACKFILL_DAYS", "90"))

        lag_days = lookback_days if lookback_days is not None else _int_env("FACT_REFRESH_LAG_DAYS", watermark_store.LOOKBACK_DAYS)
        hot_window_days = _int_env("FACT_REFRESH_HOT_WINDOW_DAYS", 45)
        dateexpected_max, updated_at_max = _resolve_watermarks(state, manifest)
        watermark_before = {"dateexpected_max": dateexpected_max, "updated_at_max": updated_at_max}
        start_dt = None
        end_dt = None
        updated_after = None
        window_start_iso = None
        window_end_excl_iso = None
        window_meta: Dict[str, Any] = {}
        if parquet_exists and initial_done:
            start_dt, end_dt, updated_after, window_meta = _compute_incremental_window(
                state=state,
                manifest=manifest,
                lag_days=lag_days,
                hot_window_days=hot_window_days,
                min_start=start,
            )
            if start_dt and end_dt:
                window_start_iso = start_dt.isoformat()
                window_end_excl_iso = (end_dt + timedelta(days=1)).isoformat()

        logger.info(
            "fact_refresh.tick",
            extra={
                "state_path": state_path.as_posix(),
                "parquet_exists": parquet_exists,
                "initial_load_done": initial_done,
                "watermark_before": watermark_before,
                "window_start": window_start_iso,
                "window_end_exclusive": window_end_excl_iso,
                "refresh_mode": refresh_mode,
                "backfill_days": backfill,
                "lag_days": lag_days,
                "hot_window_days": hot_window_days,
                "window_meta": window_meta,
            },
        )

        if refresh_mode in {"gap-backfill", "gap"}:
            if not parquet_exists:
                return {"status": "error", "error": "missing_dataset"}
            if not initial_done:
                return {"status": "error", "error": "missing_watermark"}
            result = _gap_backfill(
                dataset_path=dataset_path,
                manifest=manifest,
                backfill_days=backfill,
                keep_prev=keep_prev,
            )
            _log_dataset_metrics(dataset_path, label="gap_backfill_only")
            return result

        if not parquet_exists:
            logger.info(
                "fact_refresh.start mode=initial start=%s",
                start,
                extra={"refresh_mode": "initial", "start": start},
            )
            return initial_build(
                start=start,
                dataset_path=dataset_path,
                require_lock=False,
                keep_prev=keep_prev,
                state=state,
            )
        if not initial_done:
            state["last_error"] = "missing_watermark"
            etl_state.save_state(state, dataset_path=dataset_path)
            logger.error(
                "fact_refresh.window_missing mode=incremental watermark=%s",
                watermark_before,
                extra={"refresh_mode": "incremental", "watermark": watermark_before},
            )
            return {"status": "error", "error": "missing_watermark"}
        if start_dt is None or end_dt is None:
            state["last_error"] = "missing_watermark"
            etl_state.save_state(state, dataset_path=dataset_path)
            logger.error(
                "fact_refresh.window_missing mode=incremental watermark=%s",
                watermark_before,
                extra={"refresh_mode": "incremental", "watermark": watermark_before},
            )
            return {"status": "error", "error": "missing_watermark"}
        min_start_dt = _parse_date(start) or date(2017, 1, 1)
        if initial_done and start_dt <= date(2017, 2, 1):
            state["last_error"] = "stuck_window"
            etl_state.save_state(state, dataset_path=dataset_path)
            logger.error(
                "fact_refresh.window_stuck mode=incremental start=%s min_start=%s",
                start_dt.isoformat(),
                min_start_dt.isoformat(),
                extra={
                    "refresh_mode": "incremental",
                    "window_start": start_dt.isoformat(),
                    "min_start": min_start_dt.isoformat(),
                },
            )
            return {"status": "error", "error": "stuck_window"}

        end_exclusive = end_dt + timedelta(days=1)
        logger.info(
            "fact_refresh.start mode=incremental start=%s end_exclusive=%s updated_after=%s watermark=%s",
            start_dt.isoformat(),
            end_exclusive.isoformat(),
            updated_after,
            watermark_before,
            extra={
                "refresh_mode": "incremental",
                "watermark_before": watermark_before,
                "window_start": start_dt.isoformat(),
                "window_end_exclusive": end_exclusive.isoformat(),
                "updated_after": updated_after,
                "lag_days": lag_days,
                "hot_window_days": hot_window_days,
                "window_meta": window_meta,
            },
        )

        try:
            refreshed = incremental_refresh(
                window_start=start_dt,
                window_end=end_dt,
                updated_after=updated_after,
            )
            rows_pulled = int(len(refreshed)) if isinstance(refreshed, pd.DataFrame) else 0
            if refreshed is None or refreshed.empty:
                state["rows_last_pull"] = 0
                state["last_success_utc"] = _now_utc_iso()
                state["last_success"] = state["last_success_utc"]
                state["last_error"] = None
                etl_state.save_state(state, dataset_path=dataset_path)
                logger.info(
                    "fact_refresh.noop mode=incremental rows_pulled=%s",
                    rows_pulled,
                    extra={"refresh_mode": "incremental", "rows_pulled": rows_pulled},
                )
                noop_result = {
                    "status": "noop",
                    "dataset_version": manifest.get("dataset_version"),
                    "row_count": manifest.get("row_count"),
                }
                if backfill > 0:
                    try:
                        manifest = _gap_backfill(
                            dataset_path=dataset_path,
                            manifest=manifest,
                            backfill_days=backfill,
                            keep_prev=keep_prev,
                        )
                    except Exception:
                        logger.exception("fact_refresh.gap_backfill_failed")
                _log_dataset_metrics(dataset_path, label="incremental_noop")
                return manifest or noop_result

            date_col = manifest.get("date_column") or watermark_store.choose_date_column(refreshed) or "DateExpected"
            updated_col = _choose_updated_column(refreshed)
            deduped = _dedupe_latest(refreshed, pk_col="OrderLineId", updated_col=updated_col, date_col=date_col)
            partitions = _touched_partitions(deduped, date_col)

            dateexpected_max = _max_date_iso(deduped, date_col)
            updated_at_max = _max_datetime_iso(deduped, updated_col)
            watermark_val = updated_at_max or dateexpected_max or manifest.get("watermark")
            watermark_col = updated_col or date_col or manifest.get("watermark_column")

            schema_hash = _schema_fingerprint(deduped)
            updates = {
                "dataset_version": str(int(time.time() * 1000)),
                "watermark": watermark_val or manifest.get("watermark"),
                "last_sql_watermark": watermark_val or manifest.get("last_sql_watermark") or manifest.get("watermark"),
                "watermark_column": watermark_col or manifest.get("watermark_column"),
                "incremental_column": watermark_col or manifest.get("incremental_column"),
                "schema_hash": schema_hash,
                "schema_fingerprint": schema_hash,
                "last_refresh_utc": _now_utc_iso(),
                "status": "refreshed",
            }

            result = partition_writer.upsert_dataset(
                deduped,
                dataset_path=dataset_path,
                pk_col="OrderLineId",
                date_col=date_col,
                existing_manifest=manifest,
                manifest_updates=updates,
                replace_window_start=start_dt,
                replace_window_end=end_dt,
                keep_prev=keep_prev,
            )

            state = etl_state.set_watermark(state, dateexpected_max=dateexpected_max, updated_at_max=updated_at_max)
            state["initial_load_done"] = True
            state["dataset_version"] = result.get("dataset_version")
            state["rows_last_pull"] = rows_pulled
            state["last_success_utc"] = _now_utc_iso()
            state["last_success"] = state["last_success_utc"]
            state["last_error"] = None
            etl_state.save_state(state, dataset_path=dataset_path)

            logger.info(
                "fact_refresh.complete mode=incremental rows_pulled=%s rows_written=%s partitions=%s max_date_after=%s watermark_after=%s",
                rows_pulled,
                int(len(deduped)),
                partitions,
                result.get("max_dateexpected") or result.get("max_date"),
                {"dateexpected_max": dateexpected_max, "updated_at_max": updated_at_max},
                extra={
                    "refresh_mode": "incremental",
                    "rows_pulled": rows_pulled,
                    "rows_written": int(len(deduped)),
                    "partitions_rewritten": partitions,
                    "partitions_rewritten_count": int(len(partitions)),
                    "max_date_after": result.get("max_dateexpected") or result.get("max_date"),
                    "watermark_after": {"dateexpected_max": dateexpected_max, "updated_at_max": updated_at_max},
                },
            )
            if backfill > 0:
                try:
                    result = _gap_backfill(
                        dataset_path=dataset_path,
                        manifest=result,
                        backfill_days=backfill,
                        keep_prev=keep_prev,
                    )
                except Exception:
                    logger.exception("fact_refresh.gap_backfill_failed")
            _log_dataset_metrics(dataset_path, label="incremental")
            return result
        except Exception as exc:
            state["last_error"] = str(exc)
            etl_state.save_state(state, dataset_path=dataset_path)
            logger.exception("fact_refresh.failed mode=incremental", extra={"refresh_mode": "incremental"})
            raise
    finally:
        if require_lock and acquired:
            lock.release()

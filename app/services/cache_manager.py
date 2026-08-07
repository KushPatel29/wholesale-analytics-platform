from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from app.services import analytics_utils as au
from app.services import fact_schema as fs

logger = logging.getLogger(__name__)


def _bool_env(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return default
        return v.strip().lower() in {"1", "true", "yes", "on"}
    except Exception:
        return default


def _int_env(name: str, default: int) -> int:
    try:
        raw = os.getenv(name)
        if raw is None or not str(raw).strip():
            return default
        return int(str(raw).strip())
    except Exception:
        return default


def _safe_iso(ts: Any) -> Optional[str]:
    try:
        if ts is None:
            return None
        if isinstance(ts, pd.Timestamp):
            ts = ts.tz_convert(None) if ts.tzinfo else ts
            return ts.isoformat()
        parsed = pd.to_datetime(ts, errors="coerce")
        return parsed.tz_localize(None).isoformat() if pd.notna(parsed) else None
    except Exception:
        return None


def _normalize_meta_dict(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Backwards/forwards compatible manifest fields."""
    if not meta:
        return {}
    normalized = dict(meta)
    normalized.setdefault("dataset_version", normalized.get("version") or normalized.get("last_refresh_utc") or normalized.get("watermark"))
    normalized.setdefault("schema_hash", normalized.get("schema_fingerprint"))
    normalized.setdefault("last_sql_watermark", normalized.get("watermark") or normalized.get("watermark_dt"))
    if normalized.get("date_min") and not normalized.get("min_date"):
        normalized["min_date"] = normalized.get("date_min")
    if normalized.get("date_max") and not normalized.get("max_date"):
        normalized["max_date"] = normalized.get("date_max")
    return normalized


def _write_log_context(event: str, path: Path, *, rows: Optional[int] = None, started_at: Optional[float] = None) -> Dict[str, Any]:
    ctx: Dict[str, Any] = {
        "event": event,
        "path": path.as_posix(),
        "pid": os.getpid(),
        "rows": rows,
    }
    if started_at is not None:
        ctx["duration_ms"] = int((time.perf_counter() - started_at) * 1000)
    try:
        from flask import request  # type: ignore

        ctx["request_path"] = getattr(request, "path", None)
    except Exception:
        ctx["request_path"] = None
    try:
        ctx["stack"] = "".join(traceback.format_stack(limit=8))
    except Exception:
        pass
    return ctx


class FileLock:
    """Cross-platform advisory file lock (single-byte)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: Optional[Any] = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt  # type: ignore

                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    fh.close()
                    return False
            else:
                import fcntl  # type: ignore

                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    fh.close()
                    return False
        except Exception:
            fh.close()
            raise
        self._fh = fh
        return True

    def release(self) -> None:
        fh = self._fh
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt  # type: ignore

                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl  # type: ignore

                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                fh.close()
            finally:
                self._fh = None

    def __enter__(self) -> "FileLock":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.release()


@contextmanager
def _locked(lock: FileLock):
    acquired = False
    try:
        acquired = lock.acquire()
        yield acquired
    finally:
        if acquired:
            lock.release()


@dataclass
class CacheMetadata:
    path: str
    row_count: int
    watermark: Optional[str]
    schema_fingerprint: str
    last_refresh_utc: str
    date_min: Optional[str] = None
    date_max: Optional[str] = None
    incremental_column: Optional[str] = None
    date_column: Optional[str] = None
    status: str = "ok"
    dataset_version: Optional[str] = None
    schema_hash: Optional[str] = None
    last_sql_watermark: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.dataset_version:
            self.dataset_version = str(int(time.time() * 1000))
        if not self.schema_hash:
            self.schema_hash = self.schema_fingerprint
        if not self.last_sql_watermark:
            self.last_sql_watermark = self.watermark

    def as_dict(self) -> Dict[str, Any]:
        return {
            # Canonical fields for the new manifest
            "dataset_version": self.dataset_version,
            "path": self.path,
            "row_count": self.row_count,
            "schema_hash": self.schema_hash or self.schema_fingerprint,
            "last_refresh_utc": self.last_refresh_utc,
            "min_date": self.date_min,
            "max_date": self.date_max,
            "last_sql_watermark": self.last_sql_watermark or self.watermark,
            "incremental_column": self.incremental_column,
            "date_column": self.date_column,
            "status": self.status,
            # Backwards-compatible aliases
            "watermark": self.watermark,
            "schema_fingerprint": self.schema_fingerprint,
            "date_min": self.date_min,
            "date_max": self.date_max,
        }


class CacheManager:
    """
    Persistent cache orchestrator for the sales fact dataset.

    - Stores parquet partitioned by year/month in CACHE_DIR.
    - Tracks metadata (watermark, schema fingerprint, row counts).
    - Provides bootstrap + incremental refresh guarded by a file lock.
    """

    def __init__(
        self,
        *,
        dataset: str = "fact_dataset",
        cache_dir: Optional[str | Path] = None,
        fetcher: Optional[Callable[..., pd.DataFrame]] = None,
    ) -> None:
        dataset_name = dataset
        base_dir_raw = cache_dir or os.getenv("CACHE_DIR")
        if not base_dir_raw:
            dataset_hint = os.getenv("FACT_DATASET_PATH") or os.getenv("PARQUET_PATH")
            if dataset_hint:
                hint_path = Path(dataset_hint).expanduser().resolve()
                base_dir_raw = hint_path.parent
                if hint_path.suffix:
                    # Legacy file path; keep dataset_name as-is.
                    pass
                else:
                    if dataset_name == "fact_dataset":
                        dataset_name = hint_path.name
            else:
                base_dir_raw = Path("cache")
        self.cache_dir = Path(base_dir_raw).expanduser().resolve()
        self.dataset = dataset_name
        self.dataset_path = self.cache_dir / dataset_name
        self.meta_path = self.dataset_path / "_manifest.json"
        self.legacy_meta_path = self.cache_dir / f"{dataset}.meta.json"
        self.lock_path = self.dataset_path / ".refresh.lock"
        self.partition_cols = ["year", "month"]
        self._fetcher = fetcher
        self._thread_lock = threading.RLock()

    # -------- Metadata helpers -------- #
    def _load_meta(self) -> Dict[str, Any]:
        # Prefer the new manifest co-located with the dataset; fall back to the legacy location.
        target = self.meta_path if self.meta_path.exists() else self.legacy_meta_path
        if not target.exists():
            return {}
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            return _normalize_meta_dict(data)
        except Exception:
            logger.debug("cache.meta_read_failed", exc_info=True)
            return {}

    def _write_meta(self, meta: CacheMetadata) -> None:
        try:
            self.meta_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(meta.as_dict(), indent=2, sort_keys=True)
            tmp = self.meta_path.parent / f".{self.meta_path.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.meta_path)
            # Maintain a legacy copy for any code still pointing at the old location.
            try:
                self.legacy_meta_path.parent.mkdir(parents=True, exist_ok=True)
                legacy_tmp = self.legacy_meta_path.parent / f".{self.legacy_meta_path.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
                legacy_tmp.write_text(payload, encoding="utf-8")
                os.replace(legacy_tmp, self.legacy_meta_path)
            except Exception:
                logger.debug("cache.meta_write_legacy_failed", exc_info=True)
        except Exception:
            logger.debug("cache.meta_write_failed", exc_info=True)

    def _schema_fingerprint(self, df: pd.DataFrame) -> str:
        cols = [{"name": c, "dtype": str(dtype)} for c, dtype in df.dtypes.items()]
        raw = json.dumps(cols, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # -------- Detection helpers -------- #
    def _detect_date_column(self, df: pd.DataFrame) -> Optional[str]:
        for cand in [fs.CANON.date, "EffectiveDate", "Date", "ShipDate"]:
            if cand in df.columns:
                return cand
        return None

    def _detect_incremental_column(self, df: pd.DataFrame) -> Optional[str]:
        env_override = os.getenv("INCREMENTAL_COLUMN")
        if env_override and env_override in df.columns:
            return env_override
        preferred = [
            "UpdatedAt",
            "updated_at",
            "UpdatedAt_line",
            "UpdatedAt_order",
            "RowVersion",
            "rowversion",
        ]
        for cand in preferred:
            if cand in df.columns:
                return cand
        date_col = self._detect_date_column(df)
        return date_col

    # -------- Data acquisition -------- #
    def _fetch(self, *, start: Optional[str], end: Optional[str], updated_after: Optional[str]) -> pd.DataFrame:
        if self._fetcher:
            started = time.perf_counter()
            df = self._fetcher(start=start, end=end, updated_after=updated_after)
            logger.info(
                "cache.fetcher.complete",
                extra={
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "start": start,
                    "end": end,
                    "updated_after": updated_after,
                },
            )
            return df
        import data_loader as loader  # type: ignore

        kwargs: Dict[str, Any] = {"start": start, "end": end}
        if updated_after:
            kwargs["updated_after"] = updated_after
        started = time.perf_counter()
        df = loader.get_dataframe(**kwargs)
        logger.info(
            "cache.fetch.complete",
            extra={
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "start": start,
                "end": end,
                "updated_after": updated_after,
                "rows": len(df) if isinstance(df, pd.DataFrame) else None,
            },
        )
        return df

    # -------- IO helpers -------- #
    def _add_partitions(self, df: pd.DataFrame, date_col: str) -> pd.DataFrame:
        work = df.copy()
        ts = pd.to_datetime(work[date_col], errors="coerce")
        work["year"] = ts.dt.year.astype("Int64")
        work["month"] = ts.dt.month.astype("Int64")
        return work

    def _write_dataset(self, df: pd.DataFrame, *, replace: bool = True) -> None:
        self.dataset_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_dir / f".{self.dataset_path.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        table = pa.Table.from_pandas(df, preserve_index=False)
        logger.info("cache.parquet.write.start", extra=_write_log_context("start", tmp, rows=len(df)))
        ds.write_dataset(
            data=table,
            base_dir=tmp.as_posix(),
            format="parquet",
            partitioning=self.partition_cols,
            partitioning_flavor="hive",
            existing_data_behavior="overwrite_or_ignore",
        )
        if replace:
            self.dataset_path.mkdir(parents=True, exist_ok=True)
            for child in list(self.dataset_path.iterdir()):
                if child == self.lock_path:
                    continue
                try:
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink()
                except Exception:
                    pass
            for child in tmp.iterdir():
                shutil.move(child.as_posix(), self.dataset_path.as_posix())
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            if self.dataset_path.exists():
                shutil.move(tmp.as_posix(), (self.dataset_path / tmp.name).as_posix())
            else:
                shutil.move(tmp.as_posix(), self.dataset_path.as_posix())
        logger.info("cache.parquet.write.complete", extra=_write_log_context("complete", self.dataset_path, rows=len(df), started_at=started))

    def _read_dataset(
        self,
        *,
        start: Optional[str] = None,
        end: Optional[str] = None,
        columns: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        if not self.dataset_path.exists():
            return pd.DataFrame()
        try:
            from flask import g  # type: ignore

            request_id = getattr(g, "request_id", None)
        except Exception:
            request_id = None
        filters: List[Any] = []
        start_ts = pd.to_datetime(start, errors="coerce") if start else None
        end_ts = pd.to_datetime(end, errors="coerce") if end else None
        if start_ts is not None and pd.notna(start_ts):
            filters.append(ds.field("year") >= int(start_ts.year))
        if end_ts is not None and pd.notna(end_ts):
            filters.append(ds.field("year") <= int(end_ts.year))
        dataset = ds.dataset(self.dataset_path.as_posix(), format="parquet", partitioning="hive")
        where = None
        if filters:
            where = filters[0]
            for expr in filters[1:]:
                where = where & expr
        started = time.perf_counter()
        logger.info(
            "parquet.scan.start",
            extra={
                "path": self.dataset_path.as_posix(),
                "start": start,
                "end": end,
                "columns": columns,
                "request_id": request_id,
            },
        )
        try:
            table = dataset.to_table(filter=where, columns=columns)
            df = table.to_pandas()
        except Exception:
            logger.warning(
                "cache.dataset_read_failed",
                exc_info=True,
                extra={
                    "path": self.dataset_path.as_posix(),
                    "start": start,
                    "end": end,
                    "columns": columns,
                    "request_id": request_id,
                },
            )
            return pd.DataFrame()
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "parquet.scan.complete",
            extra={
                "path": self.dataset_path.as_posix(),
                "rows": len(df),
                "start": start,
                "end": end,
                "columns": columns,
                "duration_ms": duration_ms,
                "request_id": request_id,
            },
        )
        for col in self.partition_cols:
            if col in df.columns:
                df.drop(columns=[col], inplace=True, errors="ignore")
        return df

    def _partition_keys_for_rows(self, df: pd.DataFrame, date_col: str) -> List[Tuple[int, int]]:
        ts = pd.to_datetime(df[date_col], errors="coerce")
        years = ts.dt.year.astype("Int64")
        months = ts.dt.month.astype("Int64")
        pairs = {(int(y), int(m)) for y, m in zip(years, months) if pd.notna(y) and pd.notna(m)}
        return sorted(pairs)

    def _rewrite_partitions(
        self,
        merged: pd.DataFrame,
        *,
        date_col: str,
        pk_col: str,
        touched_partitions: Sequence[Tuple[int, int]],
    ) -> None:
        dataset = ds.dataset(self.dataset_path.as_posix(), format="parquet", partitioning="hive")
        for year, month in touched_partitions:
            part_filter = (ds.field("year") == year) & (ds.field("month") == month)
            try:
                existing_table = dataset.to_table(filter=part_filter)
                existing_df = existing_table.to_pandas() if existing_table.shape[0] else pd.DataFrame()
            except Exception:
                existing_df = pd.DataFrame()

            if pk_col in merged.columns:
                merged_subset = merged.loc[
                    (pd.to_datetime(merged[date_col], errors="coerce").dt.year == year)
                    & (pd.to_datetime(merged[date_col], errors="coerce").dt.month == month)
                ].copy()
            else:
                merged_subset = merged.copy()

            if pk_col in existing_df.columns and pk_col in merged_subset.columns:
                existing_df = existing_df.loc[~existing_df[pk_col].isin(merged_subset[pk_col])]

            combined = pd.concat([existing_df, merged_subset], ignore_index=True, sort=False)
            combined = self._add_partitions(combined, date_col)
            part_dir = self.dataset_path / f"year={year}" / f"month={month}"
            tmp_dir = self.cache_dir / f".rewrite-{self.dataset}-{year}-{month}-{int(time.time() * 1000)}"
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)
            tmp_dir.mkdir(parents=True, exist_ok=True)
            table = pa.Table.from_pandas(combined.drop(columns=self.partition_cols, errors="ignore"), preserve_index=False)
            started = time.perf_counter()
            logger.info(
                "cache.parquet.rewrite.start",
                extra=_write_log_context(
                    "rewrite_start",
                    tmp_dir,
                    rows=len(combined),
                ),
            )
            pq.write_table(table, (tmp_dir / "data.parquet").as_posix())
            if part_dir.exists():
                shutil.rmtree(part_dir, ignore_errors=True)
            part_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(tmp_dir.as_posix(), part_dir.as_posix())
            logger.info(
                "cache.parquet.rewrite.complete",
                extra=_write_log_context(
                    "rewrite_complete",
                    part_dir,
                    rows=len(combined),
                    started_at=started,
                ),
            )

    # -------- Public API -------- #
    def ensure_cache_exists(self, start_date: Optional[str] = None, end_date: Optional[str] = None) -> Dict[str, Any]:
        with self._thread_lock:
            if self.dataset_path.exists():
                existing_meta = self._load_meta()
                if existing_meta:
                    return existing_meta
                try:
                    df = self._read_dataset()
                    if not df.empty:
                        date_col = self._detect_date_column(df) or fs.CANON.date
                        inc_col = self._detect_incremental_column(df)
                        meta = CacheMetadata(
                            path=self.dataset_path.as_posix(),
                            row_count=int(len(df)),
                            watermark=_safe_iso(pd.to_datetime(df[inc_col], errors="coerce").max()) if inc_col and inc_col in df.columns else None,
                            schema_fingerprint=self._schema_fingerprint(df),
                            last_refresh_utc=datetime.now(timezone.utc).isoformat(),
                            date_min=_safe_iso(pd.to_datetime(df[date_col], errors="coerce").min()) if date_col in df.columns else None,
                            date_max=_safe_iso(pd.to_datetime(df[date_col], errors="coerce").max()) if date_col in df.columns else None,
                            incremental_column=inc_col,
                            date_column=date_col,
                            status="recovered_meta",
                        )
                        self._write_meta(meta)
                        return meta.as_dict()
                except Exception:
                    logger.debug("cache.meta_recovery_failed", exc_info=True)
        return self.bootstrap_from_2017(
            start_date=start_date or os.getenv("INITIAL_START_DATE", "2017-01-01"),
            end_date=end_date or os.getenv("INITIAL_END_DATE") or os.getenv("DATA_END_DATE"),
        )

    def bootstrap_from_2017(self, start_date: str = "2017-01-01", end_date: Optional[str] = None) -> Dict[str, Any]:
        with _locked(FileLock(self.lock_path)) as acquired:
            if not acquired:
                logger.info("cache.bootstrap_skipped_lock")
                return {"status": "locked"}
            started = time.perf_counter()
            logger.info("cache.bootstrap.start", extra={"start_date": start_date, "lock": self.lock_path.as_posix()})
            df = self._fetch(start=start_date, end=end_date, updated_after=None)
            if not isinstance(df, pd.DataFrame) or df.empty:
                raise RuntimeError("Bootstrap returned no data from source.")
            df = au.normalize_fact_df(df)
            date_col = self._detect_date_column(df) or fs.CANON.date
            inc_col = self._detect_incremental_column(df)
            if date_col not in df.columns:
                df[date_col] = pd.NaT
            if inc_col and inc_col not in df.columns:
                df[inc_col] = pd.NaT
            df = self._add_partitions(df, date_col)
            self._write_dataset(df)

            watermark = _safe_iso(pd.to_datetime(df[inc_col], errors="coerce").max()) if inc_col else None
            meta = CacheMetadata(
                path=self.dataset_path.as_posix(),
                row_count=int(len(df)),
                watermark=watermark,
                schema_fingerprint=self._schema_fingerprint(df),
                last_refresh_utc=datetime.now(timezone.utc).isoformat(),
                date_min=_safe_iso(pd.to_datetime(df[date_col], errors="coerce").min()) if date_col in df.columns else None,
                date_max=_safe_iso(pd.to_datetime(df[date_col], errors="coerce").max()) if date_col in df.columns else None,
                incremental_column=inc_col,
                date_column=date_col,
                status="bootstrapped",
            )
            self._write_meta(meta)
            logger.info(
                "cache.bootstrap.done",
                extra={
                    "rows": len(df),
                    "path": meta.path,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                },
            )
            return meta.as_dict()

    def refresh_incremental(self) -> Dict[str, Any]:
        with _locked(FileLock(self.lock_path)) as acquired:
            if not acquired:
                logger.info("cache.refresh.skipped_lock")
                return {"status": "locked"}
            started = time.perf_counter()

            meta = self._load_meta()
            if not self.dataset_path.exists() or not meta:
                return self.bootstrap_from_2017(os.getenv("INITIAL_START_DATE", "2017-01-01"))

            last_watermark_raw = meta.get("watermark")
            lookback_days = _int_env("INCREMENTAL_LOOKBACK_DAYS", 7)
            start_dt = None
            if last_watermark_raw:
                try:
                    last_ts = pd.to_datetime(last_watermark_raw, errors="coerce")
                    if pd.notna(last_ts):
                        start_dt = (last_ts - timedelta(days=lookback_days)).date().isoformat()
                except Exception:
                    start_dt = None
            incremental = self._fetch(start=None, end=None, updated_after=start_dt)
            if incremental is None or incremental.empty:
                logger.info("cache.refresh.noop")
                noop_meta = CacheMetadata(
                    path=self.dataset_path.as_posix(),
                    row_count=int(meta.get("row_count") or 0),
                    watermark=meta.get("watermark"),
                    schema_fingerprint=str(meta.get("schema_fingerprint") or ""),
                    last_refresh_utc=datetime.now(timezone.utc).isoformat(),
                    date_min=meta.get("date_min"),
                    date_max=meta.get("date_max"),
                    incremental_column=meta.get("incremental_column"),
                    date_column=meta.get("date_column"),
                    status="noop",
                )
                self._write_meta(noop_meta)
                return noop_meta.as_dict()

            incremental = au.normalize_fact_df(incremental)
            date_col = meta.get("date_column") or self._detect_date_column(incremental) or fs.CANON.date
            inc_col = meta.get("incremental_column") or self._detect_incremental_column(incremental)
            if date_col not in incremental.columns:
                incremental[date_col] = pd.NaT
            if inc_col and inc_col not in incremental.columns:
                incremental[inc_col] = pd.NaT

            pk_candidates = [
                "OrderLineId",
                fs.CANON.order_line_id,
                fs.CANON.order_id,
                fs.CANON.product_id,
            ]
            pk_col = next((c for c in pk_candidates if c in incremental.columns), incremental.columns[0])
            if pk_col not in incremental.columns:
                incremental[pk_col] = incremental.index
            incremental = self._add_partitions(incremental, date_col)

            # Load existing slice to upsert (rows whose PKs appear in incremental)
            pk_vals = pd.Series(incremental[pk_col]).dropna().unique().tolist() if pk_col in incremental.columns else []
            dataset = ds.dataset(self.dataset_path.as_posix(), format="parquet", partitioning="hive")
            existing_matches = pd.DataFrame()
            if pk_vals:
                try:
                    filter_expr = ds.field(pk_col).isin(pk_vals)
                    existing_matches = dataset.to_table(filter=filter_expr).to_pandas()
                except Exception:
                    logger.debug("cache.refresh.read_pk_failed", exc_info=True)
            merged = upsert_dataframe(existing_matches, incremental, [pk_col], inc_col)
            touched = self._partition_keys_for_rows(pd.concat([existing_matches, incremental], ignore_index=True), date_col)
            if not touched:
                touched = self._partition_keys_for_rows(merged, date_col)
            self._rewrite_partitions(merged, date_col=date_col, pk_col=pk_col, touched_partitions=touched)

            new_row_count = int((meta.get("row_count") or 0) - len(existing_matches) + len(merged))
            new_watermark = None
            if inc_col and inc_col in merged.columns:
                new_watermark = _safe_iso(pd.to_datetime(merged[inc_col], errors="coerce").max())
            inc_dates = pd.to_datetime(incremental[date_col], errors="coerce") if date_col in incremental.columns else pd.Series(dtype="datetime64[ns]")
            new_date_min = meta.get("date_min")
            new_date_max = meta.get("date_max")
            if not inc_dates.empty:
                inc_min = _safe_iso(inc_dates.min())
                inc_max = _safe_iso(inc_dates.max())
                try:
                    if inc_min and (not new_date_min or pd.to_datetime(inc_min) < pd.to_datetime(new_date_min)):
                        new_date_min = inc_min
                except Exception:
                    pass
                try:
                    if inc_max and (not new_date_max or pd.to_datetime(inc_max) > pd.to_datetime(new_date_max)):
                        new_date_max = inc_max
                except Exception:
                    pass
            meta_obj = CacheMetadata(
                path=self.dataset_path.as_posix(),
                row_count=new_row_count,
                watermark=new_watermark or last_watermark_raw,
                schema_fingerprint=self._schema_fingerprint(merged),
                last_refresh_utc=datetime.now(timezone.utc).isoformat(),
                date_min=new_date_min,
                date_max=new_date_max,
                incremental_column=inc_col,
                date_column=date_col,
                status="refreshed",
            )
            self._write_meta(meta_obj)
            logger.info(
                "cache.refresh.done",
                extra={
                    "rows": len(merged),
                    "pk_updates": len(pk_vals),
                    "watermark": meta_obj.watermark,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                },
            )
            return meta_obj.as_dict()

    def load_cached_frame(
        self,
        *,
        start: Optional[str] = None,
        end: Optional[str] = None,
        columns: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        self.ensure_cache_exists()
        df = self._read_dataset(start=start, end=end, columns=columns)
        return df

    def load_existing_frame(
        self,
        *,
        start: Optional[str] = None,
        end: Optional[str] = None,
        columns: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """Read the dataset without attempting to build/refresh it."""
        if not self.dataset_path.exists():
            raise FileNotFoundError(self.dataset_path)
        df = self._read_dataset(start=start, end=end, columns=columns)
        return df

    def get_metadata(self) -> Dict[str, Any]:
        return self._load_meta()

    def is_stale(self, ttl_minutes: int) -> bool:
        meta = self._load_meta()
        last = meta.get("last_refresh_utc")
        if not last:
            return True
        try:
            ts = pd.to_datetime(last, utc=True, errors="coerce")
            if pd.isna(ts):
                return True
            age = datetime.now(timezone.utc) - ts.to_pydatetime()
            return age > timedelta(minutes=max(1, ttl_minutes))
        except Exception:
            return True


def _align_columns(existing: pd.DataFrame, incoming: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, List[str]]:
    all_cols = list({*existing.columns.tolist(), *incoming.columns.tolist()})
    added: List[str] = []
    ex_aligned = existing.copy()
    in_aligned = incoming.copy()
    for col in all_cols:
        if col not in ex_aligned.columns:
            ex_aligned[col] = pd.NA
        if col not in in_aligned.columns:
            in_aligned[col] = pd.NA
            added.append(col)
    return ex_aligned[all_cols], in_aligned[all_cols], added


def upsert_dataframe(existing: pd.DataFrame, incoming: pd.DataFrame, pk_cols: List[str], updated_col: Optional[str]) -> pd.DataFrame:
    if existing is None or existing.empty:
        base = pd.DataFrame(columns=pk_cols)
    else:
        base = existing.copy()
    incoming = incoming.copy()
    aligned_existing, aligned_incoming, added_cols = _align_columns(base, incoming)
    if added_cols:
        logger.info("cache.upsert.schema_changed", extra={"new_columns": added_cols})
    combined = pd.concat([aligned_existing, aligned_incoming], ignore_index=True, sort=False)
    if updated_col and updated_col in combined.columns:
        combined[updated_col] = pd.to_datetime(combined[updated_col], errors="coerce")
        combined = combined.sort_values(by=[updated_col] + pk_cols, ascending=True, kind="mergesort", ignore_index=True)
    combined = combined.drop_duplicates(subset=pk_cols, keep="last")
    # Validate uniqueness
    try:
        if combined[pk_cols].duplicated().any():
            raise ValueError("Primary key duplicates remain after upsert")
    except Exception:
        logger.warning("cache.upsert.pk_validation_failed", exc_info=True)
    return combined


# Default singleton for app consumption
default_manager = CacheManager()

from __future__ import annotations

import os
import shutil
import time
import logging
from decimal import Decimal, InvalidOperation
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds

from app.services import watermark_store

MANIFEST_NAME = watermark_store.MANIFEST_NAME
logger = logging.getLogger(__name__)


def _normalize_pk_series(series: pd.Series) -> pd.Series:
    """
    Normalize primary key values for stable comparisons across schema drift.

    Production datasets have historically stored IDs as ints, floats, or strings
    depending on loader version. We normalize everything to trimmed strings and
    collapse numeric-like values (e.g. "123.0") to "123" so upserts remain
    idempotent and do not re-insert existing rows.
    """
    if series is None:
        return pd.Series(dtype="string")
    values: list[Any] = []
    raw_values = pd.Series(series, copy=False, dtype="object").tolist()
    for raw in raw_values:
        if raw is None or pd.isna(raw):
            values.append(pd.NA)
            continue
        text = str(raw).strip()
        if not text or text.lower() == "nan":
            values.append(pd.NA)
            continue
        # Avoid pandas.to_numeric here: some production hosts segfault on mixed
        # extension dtypes during PK normalization for labor refreshes.
        try:
            numeric = Decimal(text)
        except (InvalidOperation, ValueError):
            values.append(text)
            continue
        if numeric == numeric.to_integral_value():
            values.append(str(int(numeric)))
        else:
            values.append(text)
    return pd.Series(values, dtype="string")


def _chunked(values: Sequence[Any], size: int) -> Iterable[List[Any]]:
    size = max(1, int(size))
    for i in range(0, len(values), size):
        yield list(values[i : i + size])


def _coerce_pk_values_for_arrow(pk_values: Sequence[Any], arrow_type: pa.DataType) -> List[Any]:
    """
    Coerce PK values to the dataset's Arrow type for ds.field(...).isin([...]) pushdown.
    Falls back to strings if coercion is not possible.
    """
    if not pk_values:
        return []
    # Arrow bool/int types should receive python ints.
    try:
        if pa.types.is_integer(arrow_type):
            nums = pd.to_numeric(pd.Series(list(pk_values), dtype="object"), errors="coerce")
            nums = nums.dropna()
            return [int(v) for v in nums.tolist()]
    except Exception:
        pass
    # Strings: trimmed, non-empty.
    try:
        if pa.types.is_string(arrow_type) or pa.types.is_large_string(arrow_type):
            out: List[str] = []
            for v in pk_values:
                if v is None:
                    continue
                s = str(v).strip()
                if not s or s.lower() == "nan":
                    continue
                out.append(s)
            return out
    except Exception:
        pass
    # Default: normalized strings.
    return [s for s in _normalize_pk_series(pd.Series(list(pk_values), dtype="object")).dropna().unique().tolist() if s]


def _existing_partitions_for_pks(dataset_path: Path, pk_col: str, pk_values: Sequence[Any]) -> List[Tuple[int, int]]:
    """
    Return partitions (year, month) in the *existing* dataset that contain any of pk_values.

    This is required for correctness when a row's date changes and it should
    move between partitions: we must rewrite the old partition to delete the
    stale row, otherwise duplicates persist across partitions.
    """
    flag = str(os.getenv("FACT_UPSERT_SCAN_EXISTING_PARTITIONS", "1")).strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return []
    if not dataset_path.exists():
        return []
    try:
        max_pks = int(os.getenv("FACT_UPSERT_SCAN_EXISTING_PARTITIONS_MAX_PKS", "50000"))
    except Exception:
        max_pks = 50000
    if max_pks > 0 and len(pk_values) > max_pks:
        # Avoid expensive full-dataset scans for very large refresh batches.
        try:
            logger.warning(
                "partition_writer.existing_partition_scan_skipped_too_many_pks",
                extra={"pk_count": int(len(pk_values)), "max_pks": int(max_pks), "pk_col": pk_col},
            )
        except Exception:
            pass
        return []
    try:
        dataset = ds.dataset(dataset_path.as_posix(), format="parquet", partitioning="hive")
    except Exception:
        return []

    if pk_col not in dataset.schema.names:
        return []

    try:
        pk_type = dataset.schema.field(pk_col).type
    except Exception:
        pk_type = pa.string()

    coerced = _coerce_pk_values_for_arrow(pk_values, pk_type)
    if not coerced:
        return []

    partitions: set[Tuple[int, int, int]] = set()
    # Keep chunks modest; large IN lists can be expensive to compile.
    for chunk in _chunked(coerced, int(os.getenv("FACT_UPSERT_PK_CHUNK", "10000"))):
        try:
            filt = ds.field(pk_col).isin(chunk)
            table = dataset.to_table(filter=filt, columns=["year", "month", "day"])
            if table.num_rows <= 0:
                continue
            df = table.to_pandas()
            for y, m, d in zip(df.get("year", []), df.get("month", []), df.get("day", [])):
                if pd.isna(y) or pd.isna(m) or pd.isna(d):
                    continue
                partitions.add((int(y), int(m), int(d)))
        except Exception:
            # Best effort: if pushdown fails, we fall back to rewriting only the
            # partitions touched by incoming rows (still idempotent per partition).
            logger.debug("partition_writer.existing_partition_scan_failed", exc_info=True)
            break
    return sorted(partitions)


def _align_columns(existing: pd.DataFrame, incoming: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    all_cols = list({*existing.columns.tolist(), *incoming.columns.tolist()})
    left = existing.copy()
    right = incoming.copy()
    for col in all_cols:
        if col not in left.columns:
            left[col] = pd.NA
        if col not in right.columns:
            right[col] = pd.NA
    return left[all_cols], right[all_cols]


def _add_partitions(df: pd.DataFrame, date_col: str) -> pd.DataFrame:
    work = df.copy()
    ts = pd.to_datetime(work[date_col], errors="coerce")
    work["year"] = ts.dt.year.astype("Int64")
    work["month"] = ts.dt.month.astype("Int64")
    work["day"] = ts.dt.day.astype("Int64")
    return work


def _partition_keys(df: pd.DataFrame) -> List[Tuple[int, int, int]]:
    if df.empty:
        return []
    years = pd.to_numeric(df.get("year"), errors="coerce")
    months = pd.to_numeric(df.get("month"), errors="coerce")
    days = pd.to_numeric(df.get("day"), errors="coerce")
    pairs = {(int(y), int(m), int(d)) for y, m, d in zip(years, months, days) if pd.notna(y) and pd.notna(m) and pd.notna(d)}
    return sorted(pairs)


def _to_date(raw: Optional[date | datetime | str]) -> Optional[date]:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    ts = pd.to_datetime(raw, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.date()


def _window_partition_keys(start: Optional[date | datetime | str], end: Optional[date | datetime | str]) -> List[Tuple[int, int, int]]:
    start_dt = _to_date(start)
    end_dt = _to_date(end)
    if start_dt is None or end_dt is None or start_dt > end_dt:
        return []
    keys: List[Tuple[int, int, int]] = []
    cursor = start_dt
    while cursor <= end_dt:
        keys.append((int(cursor.year), int(cursor.month), int(cursor.day)))
        cursor = (pd.Timestamp(cursor) + pd.DateOffset(days=1)).date()
    return keys


def _drop_existing_rows_in_window(
    existing_df: pd.DataFrame,
    *,
    date_col: str,
    start: Optional[date | datetime | str],
    end: Optional[date | datetime | str],
) -> Tuple[pd.DataFrame, int]:
    if existing_df is None or existing_df.empty or date_col not in existing_df.columns:
        return existing_df, 0
    start_dt = _to_date(start)
    end_dt = _to_date(end)
    if start_dt is None or end_dt is None or start_dt > end_dt:
        return existing_df, 0
    ts = pd.to_datetime(existing_df[date_col], errors="coerce")
    in_window = ts.notna() & (ts.dt.date >= start_dt) & (ts.dt.date <= end_dt)
    if not bool(in_window.any()):
        return existing_df, 0
    filtered = existing_df.loc[~in_window].copy()
    removed = max(0, int(len(existing_df)) - int(len(filtered)))
    return filtered, removed


def _read_partition(dataset_path: Path, year: int, month: int, day: int) -> pd.DataFrame:
    if not dataset_path.exists():
        return pd.DataFrame()
    
    # Check both padded and non-padded directory names for backward compatibility
    part_dirs = [
        dataset_path / f"year={year}" / f"month={month}" / f"day={day}",
        dataset_path / f"year={year}" / f"month={month:02d}" / f"day={day:02d}"
    ]
    
    parquet_files: List[Path] = []
    for part_dir in part_dirs:
        if not part_dir.exists():
            continue
        parquet_files.extend(sorted(part_dir.rglob("*.parquet")))
        
    if not parquet_files:
        return pd.DataFrame()
    frames: List[pd.DataFrame] = []
    for file in parquet_files:
        try:
            frames.append(pd.read_parquet(file.as_posix()))
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True, sort=False)
    for col in ("year", "month", "day"):
        if col in df.columns:
            df.drop(columns=[col], inplace=True, errors="ignore")
    return df


def _upsert(existing: pd.DataFrame, incoming: pd.DataFrame, pk_col: str) -> pd.DataFrame:
    if existing is None or existing.empty:
        return incoming.copy()
    if incoming is None or incoming.empty:
        return existing.copy()
    left, right = _align_columns(existing, incoming)
    if pk_col in left.columns and pk_col in right.columns:
        # Normalize PK types to keep upserts idempotent across schema drift.
        left_pk = _normalize_pk_series(left[pk_col])
        right_pk = _normalize_pk_series(right[pk_col])
        left = left.loc[~left_pk.isin(set(right_pk.dropna().unique().tolist()))]
    return pd.concat([left, right], ignore_index=True, sort=False)


def _write_partition(df: pd.DataFrame, partition_dir: Path, *, year: int, month: int, day: int) -> None:
    if partition_dir.exists():
        shutil.rmtree(partition_dir, ignore_errors=True)
    partition_dir.mkdir(parents=True, exist_ok=True)
    work = df.copy()
    if "year" not in work.columns:
        work["year"] = year
    if "month" not in work.columns:
        work["month"] = month
    if "day" not in work.columns:
        work["day"] = day
    part_path = partition_dir / "part-0.parquet"
    tmp_path = partition_dir / f"part-0.{os.getpid()}.{int(time.time() * 1000)}.tmp.parquet"
    # pandas' parquet writer is materially more stable here than constructing an
    # intermediate Arrow table for historical labor rows with mixed extension dtypes.
    work.to_parquet(tmp_path, index=False)
    os.replace(tmp_path, part_path)


def _clone_dataset(src: Path, dst: Path) -> None:
    if not src.exists():
        dst.mkdir(parents=True, exist_ok=True)
        return
    dst.mkdir(parents=True, exist_ok=True)
    skip_files = {MANIFEST_NAME, ".refresh.lock"}
    for root, dirs, files in os.walk(src.as_posix()):
        rel = Path(root).relative_to(src)
        dest_root = dst / rel
        dest_root.mkdir(parents=True, exist_ok=True)
        for dname in dirs:
            (dest_root / dname).mkdir(parents=True, exist_ok=True)
        for fname in files:
            if fname in skip_files:
                continue
            src_file = Path(root) / fname
            dest_file = dest_root / fname
            try:
                os.link(src_file.as_posix(), dest_file.as_posix())
            except Exception:
                shutil.copy2(src_file.as_posix(), dest_file.as_posix())


def _count_rows(dataset_path: Path) -> Optional[int]:
    if not dataset_path.exists():
        return None
    try:
        dataset = ds.dataset(dataset_path.as_posix(), format="parquet", partitioning="hive")
        return int(dataset.count_rows())
    except Exception:
        try:
            dataset = ds.dataset(dataset_path.as_posix(), format="parquet", partitioning="hive")
            return int(dataset.to_table().num_rows)
        except Exception:
            return None


def _swap_dataset_dirs(tmp_dir: Path, dataset_path: Path, keep_prev: bool = False) -> None:
    prev = dataset_path.parent / f"{dataset_path.name}_prev"
    if prev.exists():
        shutil.rmtree(prev, ignore_errors=True)
    def _safe_replace(src: Path, dst: Path) -> None:
        try:
            os.replace(src, dst)
            return
        except Exception:
            # Best-effort fallback for Windows directory renames.
            if dst.exists():
                try:
                    shutil.rmtree(dst, ignore_errors=True)
                except Exception:
                    pass
            shutil.move(src.as_posix(), dst.as_posix())

    lock_name = ".refresh.lock"
    if dataset_path.exists():
        try:
            _safe_replace(dataset_path, prev)
        except Exception:
            prev.mkdir(parents=True, exist_ok=True)
            for child in list(dataset_path.iterdir()):
                if child.name == lock_name:
                    continue
                try:
                    shutil.move(child.as_posix(), (prev / child.name).as_posix())
                except Exception:
                    pass
    if dataset_path.exists():
        for child in list(tmp_dir.iterdir()):
            try:
                shutil.move(child.as_posix(), (dataset_path / child.name).as_posix())
            except Exception:
                pass
        shutil.rmtree(tmp_dir, ignore_errors=True)
    else:
        _safe_replace(tmp_dir, dataset_path)
    if not keep_prev and prev.exists():
        shutil.rmtree(prev, ignore_errors=True)


def upsert_dataset(
    refreshed: pd.DataFrame,
    *,
    dataset_path: Path,
    pk_col: str = "OrderLineId",
    date_col: Optional[str] = None,
    existing_manifest: Optional[Dict[str, Any]] = None,
    manifest_updates: Optional[Dict[str, Any]] = None,
    replace_window_start: Optional[date | datetime | str] = None,
    replace_window_end: Optional[date | datetime | str] = None,
    keep_prev: bool = False,
) -> Dict[str, Any]:
    window_only_replace = bool(
        refreshed is not None
        and refreshed.empty
        and replace_window_start is not None
        and replace_window_end is not None
        and date_col
    )

    if refreshed is None or (refreshed.empty and not window_only_replace):
        raise ValueError("Refreshed dataframe is empty")

    work = refreshed.copy()
    date_col = date_col or watermark_store.choose_date_column(work) or "DateExpected"
    if not window_only_replace and date_col not in work.columns:
        raise ValueError(f"Missing date column for partitioning: {date_col}")
    if not window_only_replace and pk_col not in work.columns:
        raise ValueError(f"Missing primary key column for upsert: {pk_col}")

    # Defensive dedupe on PK to keep callers idempotent even if they don't pre-dedupe.
    if not window_only_replace:
        try:
            work = work.drop_duplicates(subset=[pk_col], keep="last")
        except Exception:
            pass

        work = _add_partitions(work, date_col)
        incoming_pk_norm = _normalize_pk_series(work[pk_col])
        pk_values = incoming_pk_norm.dropna().unique().tolist()
        pk_set = set(pk_values)
        touched = _partition_keys(work)
    else:
        pk_values = []
        pk_set: set[Any] = set()
        touched = []
    window_partitions = _window_partition_keys(replace_window_start, replace_window_end)
    if window_partitions:
        touched = sorted({*touched, *window_partitions})
    # Also rewrite partitions that already contain any of these PKs so rows can
    # move between partitions without leaving duplicates behind.
    touched_existing = _existing_partitions_for_pks(dataset_path, pk_col, pk_values)
    if touched_existing:
        touched = sorted({*touched, *touched_existing})
    if not touched:
        raise ValueError("No partition keys derived from refreshed data")
    try:
        logger.info(
            "partition_writer.upsert.start",
            extra={
                "dataset_path": dataset_path.as_posix(),
                "incoming_rows": int(len(refreshed)),
                "incoming_pk_count": int(len(pk_set)),
                "pk_col": pk_col,
                "date_col": date_col,
                "partitions_incoming": int(len(_partition_keys(work))),
                "partitions_window": int(len(window_partitions)),
                "partitions_existing_pk": int(len(touched_existing)),
                "partitions_rewritten": int(len(touched)),
                "replace_window_start": _to_date(replace_window_start).isoformat() if _to_date(replace_window_start) else None,
                "replace_window_end": _to_date(replace_window_end).isoformat() if _to_date(replace_window_end) else None,
            },
        )
    except Exception:
        pass

    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = dataset_path.parent / f".{dataset_path.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    _clone_dataset(dataset_path, tmp_dir)

    existing_total = None
    if existing_manifest and existing_manifest.get("row_count") is not None:
        try:
            existing_total = int(existing_manifest.get("row_count"))
        except Exception:
            existing_total = None
    if existing_total is None:
        existing_total = _count_rows(dataset_path)

    existing_counts: List[int] = []
    new_counts: List[int] = []
    removed_total = 0
    removed_window_total = 0

    for year, month, day in touched:
        existing_df = _read_partition(dataset_path, year, month, day)
        existing_len = int(len(existing_df)) if isinstance(existing_df, pd.DataFrame) else 0
        if work.empty or "year" not in work.columns or "month" not in work.columns or "day" not in work.columns:
            new_subset = pd.DataFrame()
        else:
            new_subset = work.loc[(work["year"] == year) & (work["month"] == month) & (work["day"] == day)].copy()
        if existing_df is not None and not existing_df.empty:
            existing_df, removed_window = _drop_existing_rows_in_window(
                existing_df,
                date_col=date_col,
                start=replace_window_start,
                end=replace_window_end,
            )
            removed_window_total += int(removed_window)
            removed_total += int(removed_window)
        # Remove any existing rows for PKs we are upserting (even if the PK was
        # historically stored as int vs string), then append the refreshed rows
        # for this partition.
        if existing_df is not None and not existing_df.empty and pk_col in existing_df.columns and pk_set:
            try:
                ex_pk = _normalize_pk_series(existing_df[pk_col])
                filtered = existing_df.loc[~ex_pk.isin(pk_set)].copy()
                removed_total += max(0, int(len(existing_df)) - int(len(filtered)))
                existing_df = filtered
            except Exception:
                pass

        incoming_part = new_subset.drop(columns=["year", "month", "day"], errors="ignore")
        # Align schemas for forward/backward compatibility.
        aligned_existing, aligned_incoming = _align_columns(existing_df, incoming_part)
        frames = [frame for frame in (aligned_existing, aligned_incoming) if frame is not None and not frame.empty]
        if frames:
            combined = pd.concat(frames, ignore_index=True, sort=False)
        else:
            combined = aligned_existing.iloc[0:0].copy()
        existing_counts.append(existing_len)
        new_counts.append(int(len(combined)))
        part_dir = tmp_dir / f"year={year}" / f"month={month}" / f"day={day}"
        if combined.empty:
            shutil.rmtree(part_dir, ignore_errors=True)
            continue
        _write_partition(combined, part_dir, year=year, month=month, day=day)

    counted_total = _count_rows(tmp_dir)
    if counted_total is not None:
        new_total = int(counted_total)
    elif existing_total is None:
        new_total = int(len(work))
    else:
        new_total = int(existing_total - sum(existing_counts) + sum(new_counts))

    dates = pd.to_datetime(work[date_col], errors="coerce")
    inc_min = dates.min() if dates.notna().any() else None
    inc_max = dates.max() if dates.notna().any() else None

    base_manifest = dict(existing_manifest or {})
    updates = dict(manifest_updates or {})
    built_at = updates.get("built_at_utc") or watermark_store.now_utc_iso()

    def _safe_iso(val: Any) -> Optional[str]:
        if val is None:
            return None
        ts = pd.to_datetime(val, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.tz_localize(None).isoformat()

    min_date = (
        updates.get("min_date")
        or updates.get("min_dateexpected")
        or base_manifest.get("min_date")
        or base_manifest.get("min_dateexpected")
    )
    max_date = (
        updates.get("max_date")
        or updates.get("max_dateexpected")
        or base_manifest.get("max_date")
        or base_manifest.get("max_dateexpected")
    )
    inc_min_iso = _safe_iso(inc_min)
    inc_max_iso = _safe_iso(inc_max)
    try:
        if inc_min_iso and (not min_date or pd.to_datetime(inc_min_iso) < pd.to_datetime(min_date)):
            min_date = inc_min_iso
    except Exception:
        pass
    try:
        if inc_max_iso and (not max_date or pd.to_datetime(inc_max_iso) > pd.to_datetime(max_date)):
            max_date = inc_max_iso
    except Exception:
        pass

    manifest = base_manifest | updates
    manifest["path"] = dataset_path.as_posix()
    manifest["row_count"] = int(new_total)
    manifest["rows"] = int(new_total)
    manifest["min_date"] = min_date
    manifest["max_date"] = max_date
    manifest["min_dateexpected"] = min_date
    manifest["max_dateexpected"] = max_date
    manifest["built_at_utc"] = built_at
    manifest.setdefault("built_at", built_at)
    manifest.setdefault("last_refresh_utc", built_at)
    manifest["date_column"] = manifest.get("date_column") or date_col
    watermark_store.ensure_dataset_version(manifest)

    watermark_store.write_manifest_atomic(manifest, dataset_path=tmp_dir)
    _swap_dataset_dirs(tmp_dir, dataset_path, keep_prev=keep_prev)
    try:
        logger.info(
            "partition_writer.upsert.complete",
            extra={
                "dataset_path": dataset_path.as_posix(),
                "row_count_before": int(existing_total) if existing_total is not None else None,
                "row_count_after": int(new_total),
                "partitions_rewritten": int(len(touched)),
                "rows_removed": int(removed_total),
                "rows_removed_window": int(removed_window_total),
            },
        )
    except Exception:
        pass
    return manifest

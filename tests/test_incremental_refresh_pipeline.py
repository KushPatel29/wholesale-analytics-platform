from __future__ import annotations

import os
import shutil
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

import data_loader
from app.services import etl_state, fact_store, watermark_store
from app.services.cache_manager import FileLock
from etl import incremental_refresh, partition_writer


def _write_partition_dataset(dataset_path: Path, df: pd.DataFrame, date_col: str = "DateExpected") -> None:
    dataset_path.mkdir(parents=True, exist_ok=True)
    work = df.copy()
    ts = pd.to_datetime(work[date_col], errors="coerce")
    work["year"] = ts.dt.year.astype("Int64")
    work["month"] = ts.dt.month.astype("Int64")
    for (year, month), part_df in work.groupby(["year", "month"], dropna=True):
        part_dir = dataset_path / f"year={int(year)}" / f"month={int(month)}"
        if part_dir.exists():
            shutil.rmtree(part_dir, ignore_errors=True)
        part_dir.mkdir(parents=True, exist_ok=True)
        part_df.drop(columns=["year", "month"], errors="ignore").to_parquet(part_dir / "part-0.parquet", index=False)


def _base_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "OrderLineId": [1, 2],
            "OrderId": ["O-1", "O-2"],
            "DateExpected": ["2024-01-05", "2024-02-10"],
            "OrderStatus": ["packed", "packed"],
            "Price": [10.0, 12.0],
            "CostPrice": [6.0, 7.0],
        }
    )


def test_initial_build_creates_partitions_and_duckdb(tmp_path, monkeypatch):
    dataset_path = tmp_path / "fact_dataset"
    monkeypatch.setenv("FACT_DATASET_PATH", dataset_path.as_posix())
    monkeypatch.setenv("PARQUET_PATH", dataset_path.as_posix())
    monkeypatch.setenv("CACHE_DIR", tmp_path.as_posix())
    monkeypatch.setenv("FACT_DATASET_PATH", dataset_path.as_posix())

    base_df = _base_frame()
    calls = {"n": 0}

    def fake_get_dataframe(*args, **kwargs):
        calls["n"] += 1
        return base_df.copy() if calls["n"] == 1 else pd.DataFrame()

    monkeypatch.setattr(data_loader, "get_dataframe", fake_get_dataframe)

    meta = incremental_refresh.initial_build(start="2024-01-01", end="2024-02-28", dataset_path=dataset_path)
    manifest_path = dataset_path / "_manifest.json"
    assert manifest_path.exists()
    assert meta.get("row_count") == len(base_df)
    assert list(dataset_path.rglob("*.parquet"))

    monkeypatch.setattr(fact_store, "FACT_PATH", dataset_path, raising=False)
    monkeypatch.setattr(fact_store, "META_PATH", manifest_path, raising=False)
    fact_store.reset_duckdb_state()
    df_out = fact_store.query_fact(filters={}, apply_default_window=False, use_cache=False)
    assert len(df_out) == len(base_df)


def test_incremental_refresh_updates_version_and_rows(tmp_path, monkeypatch):
    dataset_path = tmp_path / "fact_dataset"
    monkeypatch.setenv("FACT_DATASET_PATH", dataset_path.as_posix())
    monkeypatch.setenv("CACHE_DIR", tmp_path.as_posix())

    base_df = _base_frame()
    inc_df = pd.DataFrame(
        {
            "OrderLineId": [2, 3],
            "OrderId": ["O-2", "O-3"],
            "DateExpected": ["2024-02-10", "2024-03-01"],
            "OrderStatus": ["packed", "packed"],
            "Price": [15.0, 20.0],
            "CostPrice": [8.0, 11.0],
        }
    )

    calls = {"n": 0}

    def fake_build(*args, **kwargs):
        calls["n"] += 1
        return base_df.copy() if calls["n"] == 1 else pd.DataFrame()

    monkeypatch.setattr(data_loader, "get_dataframe", fake_build)
    meta1 = incremental_refresh.initial_build(start="2024-01-01", end="2024-02-28", dataset_path=dataset_path)

    monkeypatch.setattr(data_loader, "get_dataframe", lambda *args, **kwargs: inc_df.copy())
    meta2 = incremental_refresh.refresh_once(dataset_path=dataset_path, require_lock=False)
    manifest = watermark_store.read_manifest(dataset_path)

    assert meta2.get("dataset_version") != meta1.get("dataset_version")
    assert manifest.get("row_count") == 3


def test_reload_if_version_changed_recreates_view(tmp_path, monkeypatch):
    dataset_path = tmp_path / "fact_dataset"
    monkeypatch.setenv("FACT_DATASET_PATH", dataset_path.as_posix())
    monkeypatch.setenv("PARQUET_PATH", dataset_path.as_posix())
    monkeypatch.setenv("CACHE_DIR", tmp_path.as_posix())
    base_df = _base_frame()
    _write_partition_dataset(dataset_path, base_df)
    manifest = {
        "dataset_version": "1",
        "row_count": len(base_df),
        "min_date": "2024-01-05",
        "max_date": "2024-02-10",
        "built_at_utc": "2024-02-10T00:00:00Z",
    }
    watermark_store.write_manifest_atomic(manifest, dataset_path=dataset_path)

    monkeypatch.setattr(fact_store, "FACT_PATH", dataset_path, raising=False)
    monkeypatch.setattr(fact_store, "META_PATH", dataset_path / "_manifest.json", raising=False)
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    conn = fact_store.get_conn()
    cols_before = fact_store.list_columns()
    assert "NewCol" not in cols_before

    updated_df = base_df.copy()
    updated_df["NewCol"] = 1
    _write_partition_dataset(dataset_path, updated_df)
    manifest["dataset_version"] = "2"
    watermark_store.write_manifest_atomic(manifest, dataset_path=dataset_path)
    fact_store._manifest_state.update({"checked_at": 0.0, "mtime": None, "version": None, "payload": {}})

    reloaded = fact_store.reload_if_version_changed(conn)
    assert reloaded is True
    cols_after = fact_store.list_columns()
    assert "NewCol" in cols_after


def test_refresh_lock_prevents_concurrent_refresh(tmp_path, monkeypatch):
    dataset_path = tmp_path / "fact_dataset"
    dataset_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CACHE_DIR", tmp_path.as_posix())
    lock = FileLock(tmp_path / ".refresh.lock")
    assert lock.acquire()
    try:
        monkeypatch.setattr(data_loader, "get_dataframe", lambda *args, **kwargs: pd.DataFrame())
        result = incremental_refresh.refresh_once(dataset_path=dataset_path, require_lock=True)
        assert result.get("status") == "locked"
    finally:
        lock.release()


def test_refresh_uses_state_watermark_window(tmp_path, monkeypatch):
    dataset_path = tmp_path / "fact_dataset"
    dataset_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CACHE_DIR", tmp_path.as_posix())
    monkeypatch.setenv("FACT_REFRESH_TZ", "America/Vancouver")
    monkeypatch.setenv("FACT_REFRESH_HOT_WINDOW_DAYS", "45")
    monkeypatch.setenv("FACT_REFRESH_LAG_DAYS", "7")
    _write_partition_dataset(dataset_path, _base_frame())
    manifest = {
        "dataset_version": "1",
        "row_count": 1,
        "min_date": "2024-02-01",
        "max_date": "2024-02-10",
        "max_dateexpected": "2024-02-10",
        "built_at_utc": "2024-02-11T00:00:00Z",
    }
    watermark_store.write_manifest_atomic(manifest, dataset_path=dataset_path)
    etl_state.save_state(
        {
            "dataset_version": "1",
            "initial_load_done": True,
            "watermark": {
                "dateexpected_max": "2024-02-10",
                "updated_at_max": "2024-02-11T00:00:00Z",
            },
            "last_success": "2024-02-11T00:00:00Z",
            "last_error": None,
            "rows_last_pull": 1,
            },
            dataset_path=dataset_path,
        )

    calls: dict[str, Any] = {}

    monkeypatch.setattr(
        incremental_refresh,
        "_now_in_refresh_tz",
        lambda _tz=None: datetime(2026, 2, 16, 12, 0, tzinfo=timezone.utc),
    )

    def fake_get_dataframe(*, start=None, end=None, updated_after=None, **_kwargs):
        calls["start"] = start
        calls["end"] = end
        calls["updated_after"] = updated_after
        return pd.DataFrame(
            {
                "OrderLineId": [1],
                "OrderId": ["O-1"],
                "DateExpected": ["2024-02-10"],
                "UpdatedAt": [pd.Timestamp("2024-02-11T00:00:00Z")],
                "OrderStatus": ["packed"],
                "Price": [10.0],
                "CostPrice": [6.0],
            }
        )

    monkeypatch.setattr(data_loader, "get_dataframe", fake_get_dataframe)
    monkeypatch.setattr(partition_writer, "upsert_dataset", lambda *args, **kwargs: {"dataset_version": "2", "row_count": 1})

    incremental_refresh.refresh_once(dataset_path=dataset_path, require_lock=False, lookback_days=14, backfill_days=0)

    assert calls["start"] == date(2026, 1, 2)
    assert calls["end"] == date(2026, 2, 16)
    assert calls["updated_after"] is None

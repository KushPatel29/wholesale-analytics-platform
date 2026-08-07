"""
Incremental refresh window behaviour in data_loader.refresh_parquet.

This was originally written against the single-file parquet the loader used to
produce: it asserted refresh_parquet returned a `fact.parquet` path, read a
sibling `manifest.json`, and monkeypatched `write_parquet_atomic` to capture
the frame. The loader partitions to a dataset directory now and never calls
that writer, so every one of those assertions was checking a code path that no
longer exists.

Rewritten against the current architecture, keeping what the test was actually
for: the first refresh is a full load, the second asks the source only for a
lookback window, and the two batches merge on the primary key rather than
accumulating duplicates.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd

import data_loader
from app.services import watermark_store


def _read_dataset(dataset_path: Path) -> pd.DataFrame:
    frames = [pd.read_parquet(p) for p in sorted(dataset_path.rglob("*.parquet"))]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def test_refresh_parquet_incremental_window(monkeypatch, tmp_path):
    dataset_path = tmp_path / "fact_dataset"
    shutil.rmtree(dataset_path, ignore_errors=True)

    monkeypatch.setenv("PARQUET_PATH", dataset_path.as_posix())
    monkeypatch.setenv("FACT_DATASET_PATH", dataset_path.as_posix())
    monkeypatch.delenv("FULL_REFRESH", raising=False)

    base_df = pd.DataFrame(
        {
            "OrderLineId": ["1", "2"],
            "DateExpected": pd.to_datetime(["2024-01-01", "2024-01-05"]),
            "UpdatedAt": pd.to_datetime(["2024-01-01 09:00", "2024-01-05 12:00"]),
            "Revenue": [10.0, 20.0],
            "Cost": [5.0, 9.0],
        }
    )
    # OrderLineId 2 comes back with a new date and a higher revenue; 3 is new.
    incremental_df = pd.DataFrame(
        {
            "OrderLineId": ["2", "3"],
            "DateExpected": pd.to_datetime(["2024-01-06", "2024-01-07"]),
            "UpdatedAt": pd.to_datetime(["2024-01-06 10:00", "2024-01-07 11:00"]),
            "Revenue": [25.0, 30.0],
            "Cost": [11.0, 12.0],
        }
    )

    frames = iter([base_df, incremental_df])
    calls: list[dict[str, object]] = []

    def fake_get_dataframe(start=None, end=None, statuses=None, window_days=None):
        calls.append({"start": start, "window_days": window_days})
        try:
            return next(frames).copy()
        except StopIteration:
            return pd.DataFrame(columns=base_df.columns)

    monkeypatch.setattr(data_loader, "get_dataframe", fake_get_dataframe)

    # First refresh: no dataset yet, so the loader asks for everything.
    first_path = data_loader.refresh_parquet()
    assert Path(first_path) == dataset_path
    assert calls[0]["window_days"] is None

    manifest_one = watermark_store.read_manifest(dataset_path)
    assert int(manifest_one["row_count"]) == 2

    stored_one = _read_dataset(dataset_path)
    assert set(stored_one["OrderLineId"].astype(str)) == {"1", "2"}

    # Second refresh: the dataset exists, so only the lookback window is pulled.
    second_path = data_loader.refresh_parquet()
    assert Path(second_path) == dataset_path
    assert len(calls) == 2
    assert calls[1]["window_days"] == 7

    manifest_two = watermark_store.read_manifest(dataset_path)
    assert int(manifest_two["row_count"]) == 3

    combined = _read_dataset(dataset_path)
    assert set(combined["OrderLineId"].astype(str)) == {"1", "2", "3"}

    # The upsert replaced row 2 rather than appending a second copy of it.
    row_two = combined.loc[combined["OrderLineId"].astype(str) == "2"]
    assert len(row_two) == 1
    assert float(row_two.iloc[0]["Revenue"]) == 25.0

    assert str(pd.to_datetime(combined["DateExpected"]).max().date()) == "2024-01-07"

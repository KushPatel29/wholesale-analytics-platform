from __future__ import annotations

from pathlib import Path

import pandas as pd

import data_loader


def test_refresh_parquet_incremental_window(monkeypatch):
    base_dir = Path(".pytest_tmp_work") / "incremental_refresh"
    base_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = base_dir / "fact.parquet"
    manifest_path = base_dir / "manifest.json"
    for p in (parquet_path, manifest_path):
        try:
            p.unlink()
        except FileNotFoundError:
            pass

    monkeypatch.setenv("PARQUET_PATH", parquet_path.as_posix())
    monkeypatch.delenv("FULL_REFRESH", raising=False)

    base_df = pd.DataFrame(
        {
            "OrderLineId": ["1", "2"],
            "Date": pd.to_datetime(["2024-01-01", "2024-01-05"]),
            "UpdatedAt": pd.to_datetime(["2024-01-01 09:00", "2024-01-05 12:00"]),
            "Revenue": [10.0, 20.0],
            "Cost": [5.0, 9.0],
        }
    )
    incremental_df = pd.DataFrame(
        {
            "OrderLineId": ["2", "3"],
            "Date": pd.to_datetime(["2024-01-06", "2024-01-07"]),
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
            frame = next(frames)
        except StopIteration:
            return pd.DataFrame()
        return frame.copy()

    store = {"df": pd.DataFrame()}
    written = {}

    def fake_write_parquet_atomic(df, path):
        store["df"] = df.copy()
        written["df"] = df.copy()
        written["path"] = path
        return str(path)

    def fake_read_existing(path):
        return store["df"].copy()

    monkeypatch.setattr(data_loader, "get_dataframe", fake_get_dataframe)
    monkeypatch.setattr(data_loader, "write_parquet_atomic", fake_write_parquet_atomic)
    monkeypatch.setattr(data_loader, "_read_existing_parquet", fake_read_existing)

    first_path = data_loader.refresh_parquet()
    assert Path(first_path) == parquet_path
    assert calls[0]["window_days"] is None

    manifest_one = data_loader.read_manifest(parquet_path.as_posix())
    assert manifest_one["rows"] == 2
    assert manifest_one["date_max"] == "2024-01-05"

    version_one = data_loader.current_version(parquet_path.as_posix())
    assert version_one != "0"

    second_path = data_loader.refresh_parquet()
    assert Path(second_path) == parquet_path
    assert len(calls) == 2
    assert calls[1]["window_days"] == 7
    assert calls[1]["start"] == "2023-12-29"

    combined = written["df"]
    assert set(combined["OrderLineId"]) == {"1", "2", "3"}
    updated_row = combined.loc[combined["OrderLineId"] == "2"].iloc[0]
    assert updated_row["Revenue"] == 25.0

    manifest_two = data_loader.read_manifest(parquet_path.as_posix())
    assert manifest_two["rows"] == 3
    assert manifest_two["date_max"] == "2024-01-07"

    version_two = data_loader.current_version(parquet_path.as_posix())
    assert version_two != version_one

    max_date = data_loader.max_loaded_date(parquet_path.as_posix())
    assert str(max_date.date()) == "2024-01-07"

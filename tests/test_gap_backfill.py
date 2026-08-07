from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import data_loader
from app.services import watermark_store
from etl import incremental_refresh


def test_coalesce_missing_ranges():
    missing = [
        date(2026, 1, 3),
        date(2026, 1, 4),
        date(2026, 1, 6),
        date(2026, 1, 9),
        date(2026, 1, 10),
    ]
    ranges = incremental_refresh._coalesce_missing_ranges(missing)
    assert ranges == [
        (date(2026, 1, 3), date(2026, 1, 4)),
        (date(2026, 1, 6), date(2026, 1, 6)),
        (date(2026, 1, 9), date(2026, 1, 10)),
    ]


def test_gap_backfill_invokes_loader_for_missing_ranges(tmp_path, monkeypatch):
    dataset_path = tmp_path / "fact_dataset"
    dataset_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FACT_DATASET_PATH", dataset_path.as_posix())
    monkeypatch.setenv("CACHE_DIR", tmp_path.as_posix())

    # Seed a minimal parquet file so dataset exists.
    part_dir = dataset_path / "year=2026" / "month=01"
    part_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "OrderLineId": [1],
            "OrderId": ["O-1"],
            "DateExpected": ["2026-01-01"],
            "OrderStatus": ["packed"],
            "Price": [10.0],
            "CostPrice": [6.0],
        }
    ).to_parquet(part_dir / "part-0.parquet", index=False)
    watermark_store.write_manifest_atomic(
        {"dataset_version": "1", "row_count": 1, "date_column": "DateExpected"},
        dataset_path=dataset_path,
    )

    fixed_today = date(2026, 2, 16)
    monkeypatch.setattr(incremental_refresh, "_today_in_refresh_tz", lambda _tz=None: fixed_today)
    today = fixed_today
    start = today - timedelta(days=4)
    available = [start, start + timedelta(days=1), start + timedelta(days=3), today]

    monkeypatch.setattr(
        incremental_refresh,
        "_query_available_dates",
        lambda *args, **kwargs: available,
    )

    calls: list[tuple[date, date]] = []

    def fake_get_dataframe(*, start=None, end=None, updated_after=None, **_kwargs):
        assert updated_after is None
        calls.append((start, end))
        return pd.DataFrame(
            {
                "OrderLineId": [99],
                "OrderId": ["O-99"],
                "DateExpected": [start.isoformat()],
                "OrderStatus": ["packed"],
                "Price": [10.0],
                "CostPrice": [6.0],
            }
        )

    monkeypatch.setattr(data_loader, "get_dataframe", fake_get_dataframe)
    monkeypatch.setattr(
        incremental_refresh.partition_writer,
        "upsert_dataset",
        lambda *args, **kwargs: {"dataset_version": "2", "row_count": 2, "date_column": "DateExpected"},
    )

    incremental_refresh.gap_backfill_only(dataset_path=dataset_path, backfill_days=5, require_lock=False)

    assert calls == [(start + timedelta(days=2), start + timedelta(days=2))]

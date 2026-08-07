from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd

from app.services import watermark_store
from etl import partition_writer


def _write_dataset(dataset_path: Path, df: pd.DataFrame, *, date_col: str = "Date") -> None:
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


def test_upsert_idempotent_and_moves_across_partitions(tmp_path):
    dataset_path = tmp_path / "fact_dataset"
    base = pd.DataFrame(
        {
            # Existing stored as int (simulates historical schema)
            "OrderLineId": [1, 2],
            "OrderId": ["O-1", "O-2"],
            "Date": ["2025-12-15", "2025-12-20"],
            "UpdatedAt": ["2025-12-21T00:00:00Z", "2025-12-21T00:00:00Z"],
            "Revenue": [10.0, 20.0],
            "Cost": [6.0, 12.0],
        }
    )
    _write_dataset(dataset_path, base, date_col="Date")
    watermark_store.write_manifest_atomic(
        {"dataset_version": "1", "row_count": 2, "date_column": "Date", "watermark_column": "UpdatedAt"},
        dataset_path=dataset_path,
    )

    # Incoming refresh uses string PKs and moves OrderLineId=2 into Jan 2026.
    incoming = pd.DataFrame(
        {
            "OrderLineId": ["2", "3"],
            "OrderId": ["O-2", "O-3"],
            "Date": ["2026-01-05", "2026-01-06"],
            "UpdatedAt": ["2026-01-06T00:00:00Z", "2026-01-06T00:00:00Z"],
            "Revenue": [25.0, 30.0],
            "Cost": [15.0, 18.0],
        }
    )

    meta1 = partition_writer.upsert_dataset(
        incoming,
        dataset_path=dataset_path,
        pk_col="OrderLineId",
        date_col="Date",
        existing_manifest=watermark_store.read_manifest(dataset_path),
        manifest_updates={"dataset_version": "2", "status": "test"},
        keep_prev=False,
    )
    assert int(meta1.get("row_count") or 0) == 3

    # Second run must be idempotent: row_count should not change.
    meta2 = partition_writer.upsert_dataset(
        incoming,
        dataset_path=dataset_path,
        pk_col="OrderLineId",
        date_col="Date",
        existing_manifest=watermark_store.read_manifest(dataset_path),
        manifest_updates={"dataset_version": "3", "status": "test"},
        keep_prev=False,
    )
    assert int(meta2.get("row_count") or 0) == 3

    # Ensure the moved key is not duplicated across partitions.
    dec_part = dataset_path / "year=2025" / "month=12" / "part-0.parquet"
    jan_part = dataset_path / "year=2026" / "month=1" / "part-0.parquet"
    assert dec_part.exists()
    assert jan_part.exists()
    dec = pd.read_parquet(dec_part)
    jan = pd.read_parquet(jan_part)
    # Normalize to string for comparison across pyarrow/pandas dtypes.
    dec_ids = set(pd.Series(dec["OrderLineId"]).astype("string").tolist())
    jan_ids = set(pd.Series(jan["OrderLineId"]).astype("string").tolist())
    assert "2" not in dec_ids
    assert "2" in jan_ids


def test_upsert_replaces_date_window_rows_deterministically(tmp_path):
    dataset_path = tmp_path / "fact_dataset"
    base = pd.DataFrame(
        {
            "OrderLineId": [10, 20, 30],
            "OrderId": ["O-10", "O-20", "O-30"],
            "Date": ["2026-01-10", "2026-01-20", "2026-01-25"],
            "UpdatedAt": ["2026-01-10T00:00:00Z", "2026-01-20T00:00:00Z", "2026-01-25T00:00:00Z"],
            "Revenue": [10.0, 20.0, 30.0],
            "Cost": [6.0, 12.0, 18.0],
        }
    )
    _write_dataset(dataset_path, base, date_col="Date")
    watermark_store.write_manifest_atomic(
        {"dataset_version": "1", "row_count": 3, "date_column": "Date", "watermark_column": "UpdatedAt"},
        dataset_path=dataset_path,
    )

    # Incoming window refresh includes only OrderLineId=20; OrderLineId=30
    # should be removed because it lives in the replace window and is absent.
    incoming = pd.DataFrame(
        {
            "OrderLineId": [20],
            "OrderId": ["O-20"],
            "Date": ["2026-01-20"],
            "UpdatedAt": ["2026-01-26T00:00:00Z"],
            "Revenue": [25.0],
            "Cost": [15.0],
        }
    )

    meta = partition_writer.upsert_dataset(
        incoming,
        dataset_path=dataset_path,
        pk_col="OrderLineId",
        date_col="Date",
        existing_manifest=watermark_store.read_manifest(dataset_path),
        manifest_updates={"dataset_version": "2", "status": "test"},
        replace_window_start="2026-01-15",
        replace_window_end="2026-01-31",
        keep_prev=False,
    )
    assert int(meta.get("row_count") or 0) == 2

    jan_part = dataset_path / "year=2026" / "month=1" / "part-0.parquet"
    jan = pd.read_parquet(jan_part)
    ids = set(pd.Series(jan["OrderLineId"]).astype("string").tolist())
    assert ids == {"10", "20"}

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from app.services.cache_manager import CacheManager, upsert_dataframe


def test_upsert_dataframe_prefers_newer_updated_at():
    existing = pd.DataFrame(
        {
            "OrderLineId": [1, 2],
            "UpdatedAt": [pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-02")],
            "Revenue": [10.0, 20.0],
        }
    )
    incoming = pd.DataFrame(
        {
            "OrderLineId": [2],
            "UpdatedAt": [pd.Timestamp("2024-02-01")],
            "Revenue": [99.0],
        }
    )

    merged = upsert_dataframe(existing, incoming, ["OrderLineId"], "UpdatedAt")
    assert len(merged) == 2
    row = merged.loc[merged["OrderLineId"] == 2].iloc[0]
    assert row["Revenue"] == 99.0
    assert pd.to_datetime(row["UpdatedAt"]) == pd.Timestamp("2024-02-01")


def test_upsert_adds_new_columns_and_preserves_existing():
    existing = pd.DataFrame(
        {
            "OrderLineId": [1, 2],
            "UpdatedAt": [pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-02")],
            "Revenue": [10.0, 20.0],
        }
    )
    incoming = pd.DataFrame(
        {
            "OrderLineId": [1],
            "UpdatedAt": [pd.Timestamp("2024-01-03")],
            "Revenue": [15.0],
            "NewFlag": ["y"],
        }
    )

    merged = upsert_dataframe(existing, incoming, ["OrderLineId"], "UpdatedAt")
    assert "NewFlag" in merged.columns
    row_new = merged.loc[merged["OrderLineId"] == 1].iloc[0]
    assert row_new["NewFlag"] == "y"
    assert row_new["Revenue"] == 15.0
    row_old = merged.loc[merged["OrderLineId"] == 2].iloc[0]
    assert pd.isna(row_old["NewFlag"])


def test_cache_manager_refresh_updates_watermark_and_rows(tmp_path):
    base = pd.DataFrame(
        {
            "OrderLineId": [1, 2],
            "Date": [pd.Timestamp("2020-01-01"), pd.Timestamp("2020-01-02")],
            "Revenue": [100.0, 200.0],
            "UpdatedAt": [pd.Timestamp("2020-01-03"), pd.Timestamp("2020-01-04")],
        }
    )
    inc = pd.DataFrame(
        {
            "OrderLineId": [2, 3],
            "Date": [pd.Timestamp("2020-01-02"), pd.Timestamp("2020-01-05")],
            "Revenue": [250.0, 300.0],
            "UpdatedAt": [pd.Timestamp("2020-02-01"), pd.Timestamp("2020-02-02")],
        }
    )
    calls: list[str | None] = []

    def _fetcher(start=None, end=None, updated_after=None):
        calls.append(updated_after)
        return base if updated_after is None else inc

    mgr = CacheManager(cache_dir=Path(tmp_path) / "cache", fetcher=_fetcher)
    meta1 = mgr.bootstrap_from_2017(start_date="2020-01-01")
    meta2 = mgr.refresh_incremental()
    df = mgr.load_cached_frame().sort_values("OrderLineId")

    assert len(df) == 3
    match = df.loc[df["OrderLineId"].astype(str) == "2"]
    assert not match.empty
    assert float(match["Revenue"].iloc[0]) == 250.0
    assert meta1.get("watermark") != meta2.get("watermark")
    assert calls[0] is None
    assert calls[-1] is not None

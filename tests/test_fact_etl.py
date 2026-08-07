from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import pytest

from app import create_app
from app.services.cache_manager import CacheManager
from app.services.fact_store import DatasetNotBuiltError


def test_manifest_atomic_write(tmp_path):
    base = pd.DataFrame(
        {
            "OrderLineId": [1, 2],
            "Date": [pd.Timestamp("2020-01-01"), pd.Timestamp("2020-01-02")],
            "Revenue": [10.0, 20.0],
            "UpdatedAt": [pd.Timestamp("2020-01-03"), pd.Timestamp("2020-01-04")],
        }
    )

    mgr = CacheManager(cache_dir=tmp_path / "cache", fetcher=lambda **_: base)
    meta = mgr.bootstrap_from_2017(start_date="2020-01-01")
    manifest_path = mgr.meta_path
    assert manifest_path.exists()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload.get("dataset_version")
    assert payload.get("row_count") == len(base)
    # No temp manifests should linger
    assert not list(manifest_path.parent.glob("._manifest*.tmp*"))
    # Compatibility aliases remain
    assert meta.get("date_min") or meta.get("min_date")


def test_incremental_appends_without_rewriting_old_partitions(tmp_path):
    base = pd.DataFrame(
        {
            "OrderLineId": [1],
            "Date": [pd.Timestamp("2020-01-01")],
            "Revenue": [10.0],
            "UpdatedAt": [pd.Timestamp("2020-01-02")],
        }
    )
    inc = pd.DataFrame(
        {
            "OrderLineId": [2],
            "Date": [pd.Timestamp("2020-02-01")],
            "Revenue": [20.0],
            "UpdatedAt": [pd.Timestamp("2020-02-02")],
        }
    )
    calls: list[str | None] = []

    def _fetcher(start=None, end=None, updated_after=None):
        calls.append(updated_after)
        return base if updated_after is None else inc

    mgr = CacheManager(cache_dir=tmp_path / "cache", fetcher=_fetcher)
    mgr.bootstrap_from_2017(start_date="2020-01-01")
    initial_parts = sorted(mgr.dataset_path.rglob("*.parquet"))
    assert initial_parts
    first_path = initial_parts[0]
    first_mtime = first_path.stat().st_mtime

    meta = mgr.refresh_incremental()
    assert meta.get("row_count") == 2

    jan_mtime_after = first_path.stat().st_mtime
    assert jan_mtime_after == pytest.approx(first_mtime)
    refreshed_parts = sorted(mgr.dataset_path.rglob("*.parquet"))
    assert len(refreshed_parts) > len(initial_parts)
    assert calls[0] is None
    assert calls[-1] is not None


def test_web_request_path_stays_read_only(monkeypatch):
    flag = {"write": False}

    def _fail_write(*args, **kwargs):
        flag["write"] = True
        raise AssertionError("parquet write attempted")

    # Force parquet writes to explode if invoked
    monkeypatch.setattr("app.services.cache_manager.CacheManager._write_dataset", _fail_write)
    monkeypatch.setattr("app.services.cache_manager.CacheManager._rewrite_partitions", _fail_write)
    # Ensure live SQL path returns nothing so the parquet reader is used
    monkeypatch.setattr("data_loader.get_dataframe_for_user", lambda **_: pd.DataFrame())
    # Simulate missing dataset to exercise the error handler
    monkeypatch.setattr("app.services.fact_store._require_manifest", lambda: (_ for _ in ()).throw(DatasetNotBuiltError("Dataset not built")))

    app = create_app()
    app.config.update(TESTING=True, LOGIN_DISABLED=True)
    client = app.test_client()
    resp = client.get("/api/overview/summary")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body.get("error")
    assert flag["write"] is False

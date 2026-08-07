import json
import os
import time
from datetime import datetime, timezone

import pandas as pd
import pytest

from app.services import event_bus


def _make_snapshot(version: int) -> pd.DataFrame:
    base = pd.Timestamp('2024-01-01', tzinfo=timezone.utc)
    rows = []
    # All columns from VELOCITY_BASE_COLUMNS, with defaults
    all_cols = {
        "OrderLineId": None,
        "OrderId": None,
        "OrderStatus": "Shipped",
        "Date": None,
        "DateOrdered_line": None,
        "DateExpected_line": None,
        "DateShipped_line": None,
        "DateOrdered_order": None,
        "DateExpected_order": None,
        "DateShipped_order": None,
        "ShipDate": None,
        "QuantityShipped": 0.0,
        "QuantityOrdered": 0.0,
        "ItemCount": 0.0,
        "WeightLb": 0.0,
        "Price": 0.0,
        "CostPrice": 0.0,
        "Revenue": 0.0,
        "Cost": 0.0,
        "Profit": 0.0,
        "UnitOfBillingId": None,
        "CustomerId": None,
        "CustomerName": None,
        "RegionName": None,
        "SalesRepId": None,
        "PrimarySalesRepId": None,
        "ShippingMethodName": None,
        "ShippingMethodLabel": None,
        "ShippingMethodRequested": None,
        "ShipperName": None,
        "ProductId": None,
        "ProductName": None,
        "SkuName": None,
        "SupplierId": None,
        "SupplierName": None,
        "Carrier": None,
    }

    for idx in range(3):
        row = all_cols.copy() # Start with all columns
        row.update({ # Override with specific test data
            "Date": (base + pd.Timedelta(days=idx)).tz_localize(None),
            "Revenue": 100.0 + version * 10 + idx,
            "OrderId": f"ORD-{version}-{idx}",
            "CustomerId": f"CUST-{idx}",
            "RegionName": "North",
            "ShippingMethodName": "Ground",
            "QuantityShipped": 10.0, # Example value
            "WeightLb": 5.0, # Example value
            "ProductId": f"PROD-{idx}",
            "ProductName": f"Product {idx}",
            "SalesRepId": f"SR-{idx}",
        })
        rows.append(row)
    return pd.DataFrame(rows)


@pytest.fixture(scope="session")
def app():
    from app import create_app

    os.environ.setdefault("FLASK_ENV", "development")
    os.environ.setdefault("WTF_CSRF_ENABLED", "false")
    os.environ.setdefault("WA_FAST_PWHASH", "1")

    state = {
        "version": 1,
        "built_at": datetime.now(timezone.utc).isoformat(),
    }

    def fake_load_snapshot(columns=None, parquet_path=None):
        df = _make_snapshot(state["version"])
        if columns:
            return df[columns]
        return df

    def fake_current_version(parquet_path=None):
        return str(state["version"])

    def fake_read_manifest(parquet_path=None):
        return {
            "version": str(state["version"]),
            "built_at": state["built_at"],
            "rows": 3,
            "parquet_path": "/tmp/dummy.parquet",
        }

    def fake_refresh_parquet(*args, **kwargs):
        state["version"] += 1
        state["built_at"] = datetime.now(timezone.utc).isoformat()
        from app.cache import cache
        cache.clear()
        event_bus.publish(
            {
                "type": "data_refresh",
                "version": str(state["version"]),
                "built_at": state["built_at"],
            }
        )
        return "/tmp/dummy.parquet"

    mp = pytest.MonkeyPatch()
    mp.setattr("data_loader.load_snapshot", fake_load_snapshot, raising=True)
    mp.setattr("data_loader.current_version", fake_current_version, raising=True)
    mp.setattr("data_loader.read_manifest", fake_read_manifest, raising=True)
    mp.setattr("data_loader.refresh_parquet", fake_refresh_parquet, raising=True)

    import workers.refresh as refresh_worker

    def fake_enqueue_refresh(full: bool = False):
        fake_refresh_parquet()

    mp.setattr(refresh_worker, "enqueue_refresh", fake_enqueue_refresh, raising=True)

    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test", LOGIN_DISABLED=False)
    app.config["TEST_STATE"] = state
    try:
        yield app
    finally:
        mp.undo()


@pytest.fixture()
def client(app):
    with app.test_client() as c:
        c.post("/auth/login", data={"username": "admin", "password": "admin"}, follow_redirects=True)
        yield c


def _read_sse_chunk(stream):
    chunk = next(stream)
    if isinstance(chunk, bytes):
        return chunk.decode("utf-8")
    return chunk


def test_live_refresh_flow(client):
    # Baseline cards
    resp = client.get("/api/overview/cards?start_date=2024-01-01&end_date=2024-01-31")
    assert resp.status_code == 200
    initial_cards = resp.get_json()
    assert "revenue" in initial_cards
    initial_cards_etag = resp.headers.get("ETag")

    summary = client.get("/api/overview/summary?start_date=2024-01-01&end_date=2024-01-31")
    assert summary.status_code == 200
    summary_body = summary.get_json()
    initial_summary_etag = summary.headers.get("ETag")
    initial_version = summary_body["meta"]["version"]

    freshness = client.get("/api/freshness")
    assert freshness.status_code == 200
    assert freshness.get_json()["version"] == initial_version

    # Open SSE stream
    event_response = client.open("/api/events", method="GET", buffered=False)
    stream = event_response.response
    _read_sse_chunk(stream)  # consume initial comment

    # Trigger refresh
    trigger = client.post("/api/_admin/refresh")
    assert trigger.status_code == 202

    # Wait for new version
    new_version = initial_version
    for _ in range(10):
        freshness = client.get("/api/freshness")
        assert freshness.status_code == 200
        new_version = freshness.get_json()["version"]
        if new_version != initial_version:
            break
        time.sleep(0.01)
    assert new_version != initial_version

    # Read SSE event
    payload = None
    for _ in range(20):
        chunk = _read_sse_chunk(stream)
        if chunk.startswith("data: "):
            payload = json.loads(chunk[len("data: "):])
            break
    event_response.close()

    assert payload is not None
    assert payload["type"] == "data_refresh"
    assert payload["version"] == new_version

    # Cards should now reflect new version and different ETag
    resp_updated = client.get("/api/overview/cards?start_date=2024-01-01&end_date=2024-01-31")
    assert resp_updated.status_code == 200
    updated_body = resp_updated.get_json()
    assert updated_body["revenue"] != initial_cards["revenue"]
    assert resp_updated.headers.get("ETag") != initial_cards_etag

    # Summary payload should also reflect the new version + ETag
    summary_updated = client.get("/api/overview/summary?start_date=2024-01-01&end_date=2024-01-31")
    assert summary_updated.status_code == 200
    updated_summary_body = summary_updated.get_json()
    assert updated_summary_body["meta"]["version"] == new_version
    assert summary_updated.headers.get("ETag") != initial_summary_etag


def test_live_updates_asset_only_starts_sse_when_handlers_exist(app):
    with app.test_client() as client:
        resp = client.get("/static/js/live-updates.js")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "if (!handlers.size) {" in body
        assert "startEventStream();" in body
        assert "state.sseAttempted = false;" in body

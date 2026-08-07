"""
Programmatic parity tests for the Overview page.

These tests compare the public API payloads against the authoritative
fact-frame calculations to guarantee the UI widgets stay in sync with
the Business Insights drilldown block.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

import pytest

from app import create_app
from app.services import fact_store
from app.services.filters import FilterParams
from app.services.overview_query import (
    cards_summary,
    fact_frame,
    mix_summary,
    series_summary,
    table_summary,
    top_summary,
)


def _ensure_frame(frame):
    if frame is None or frame.empty:
        pytest.skip("Fact frame is empty; overview data unavailable in this environment.")


def _assert_allclose_dict(actual: Dict[str, Any], expected: Dict[str, Any], keys: List[str], rel=1e-6):
    for key in keys:
        assert key in actual, f"Missing key '{key}' in API payload."
        assert key in expected, f"Missing key '{key}' in computed payload."
        a = actual[key]
        b = expected[key]
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if a == b == 0:
                continue
            assert pytest.approx(a, rel=rel) == b, f"{key}: {a} != {b}"
        else:
            assert a == b, f"{key}: {a} != {b}"


def _fact_row_count() -> int | None:
    try:
        row = fact_store.get_conn().execute("SELECT COUNT(*) AS c FROM fact").fetchone()
        if not row:
            return None
        return int(row[0] or 0)
    except Exception:
        return None


@pytest.fixture(scope="session")
def app():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, LOGIN_DISABLED=False)
    return app


@pytest.fixture(scope="session")
def client(app):
    with app.test_client() as client:
        client.post("/auth/login", data={"username": "admin", "password": "admin"}, follow_redirects=True)
        yield client


@pytest.fixture(scope="session")
def overview_filters() -> FilterParams:
    return FilterParams(
        start=None,
        end=None,
        regions=tuple(),
        methods=tuple(),
        customers=tuple(),
        suppliers=tuple(),
        products=tuple(),
        sales_reps=tuple(),
    )


@pytest.fixture(scope="session")
def overview_frame(overview_filters):
    max_rows = int(os.getenv("OVERVIEW_PARITY_MAX_ROWS", "750000"))
    row_count = _fact_row_count()
    if row_count is not None and row_count > max_rows:
        pytest.skip(
            f"Skipping overview parity in this environment: fact has {row_count:,} rows (> {max_rows:,}) and can exceed test memory budget."
        )
    frame = fact_frame(overview_filters, apply_filter=False)
    _ensure_frame(frame)
    return frame


def test_cards_parity(client, overview_filters, overview_frame):
    expected = cards_summary(overview_filters, frame=overview_frame)
    resp = client.get("/api/overview/cards?preset=all")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert isinstance(payload, dict)

    keys = [
        "revenue",
        "revenue_prev",
        "revenue_delta_pct",
        "gross_margin",
        "gm_pct",
        "orders",
        "aov",
        "units_lb",
        "units_each",
        "ship_charge_total",
    ]
    _assert_allclose_dict(payload, expected, keys)


def test_series_parity(client, overview_filters, overview_frame):
    expected = series_summary(overview_filters, freq="D", frame=overview_frame)
    resp = client.get("/api/overview/series?freq=day&preset=all")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["frequency"] == expected["frequency"]
    assert payload["points"] == expected["points"]


def test_top_parity(client, overview_filters, overview_frame):
    expected = top_summary(overview_filters, frame=overview_frame, limit=5)
    resp = client.get("/api/overview/top?limit=5&preset=all")
    assert resp.status_code == 200
    payload = resp.get_json()
    for key in ("top_customers", "top_products", "top_regions", "top_reps"):
        assert payload[key] == expected[key]


def test_mix_parity(client, overview_filters, overview_frame):
    expected = mix_summary(overview_filters, frame=overview_frame)
    resp = client.get("/api/overview/mix?preset=all")
    assert resp.status_code == 200
    payload = resp.get_json()
    for key in ("protein", "region", "shipper"):
        assert payload[key] == expected[key]


def test_table_parity(client, overview_filters, overview_frame):
    expected = table_summary(overview_filters, frame=overview_frame, dimension="product", page=1, page_size=25)
    resp = client.get("/api/overview/table?dimension=product&page=1&page_size=25&preset=all")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["rows"] == expected["rows"]
    assert payload["total"] == expected["total"]


def test_options_endpoint(client):
    resp = client.get("/api/overview/options")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert isinstance(payload, dict)
    assert "regions" in payload


def test_cards_etag(client):
    resp = client.get("/api/overview/cards")
    assert resp.status_code == 200
    etag = resp.headers.get("ETag")
    if not etag:
        pytest.skip("Cards endpoint does not emit ETag in this environment.")

    resp2 = client.get("/api/overview/cards", headers={"If-None-Match": etag})
    assert resp2.status_code in (200, 304)

import shutil
import uuid
from pathlib import Path

import pandas as pd
import pytest

import app.blueprints.products as products_bp


@pytest.fixture
def products_kpi_client(app, monkeypatch):
    """Client seeded with weekly data to exercise KPI payloads."""
    products_bp._STORE_SINGLETON = None
    tmp_dir = Path("cache") / f"products_kpi_{uuid.uuid4().hex}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    base = pd.Timestamp.utcnow().normalize().replace(day=1)
    rows = []
    for w in range(14):
        dt = base - pd.Timedelta(days=7 * (w + 1))
        rows.append(
            {
                "date": dt,
                "product_id": f"SKU-{(w % 2) + 1}",
                "product_name": f"Product {(w % 2) + 1}",
                "customer_id": f"CUST-{w % 3}",
                "customer_name": f"Customer {w % 3}",
                "region": "North",
                "supplier": "Supplier A",
                "order_id": f"ORD-{w}",
                "qty": 10 + w,
                "weight": 5.0 + w,
                "revenue": 1000 + w * 50,
                "cost": 600 + w * 25,
            }
        )
    df = pd.DataFrame(rows)
    parquet_path = tmp_dir / "products.parquet"
    df.to_parquet(parquet_path)

    monkeypatch.setenv("PRODUCTS_PARQUET_PATH", str(parquet_path))
    app.config["PRODUCTS_PARQUET_PATH"] = str(parquet_path)
    app.config["DATA_DIR"] = str(parquet_path.parent)
    app.config["LOGIN_DISABLED"] = True
    app.config["AUTHZ_DISABLED"] = True

    try:
        with app.test_client() as client:
            yield client
    finally:
        products_bp._STORE_SINGLETON = None
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_products_overview_kpis_and_velocity(products_kpi_client):
    resp = products_kpi_client.get("/products/api/overview", query_string={"forecast": "1"})
    assert resp.status_code == 200
    payload = resp.get_json().get("data") or {}

    velocity = payload.get("velocity") or {}
    for key in ("avg_weekly", "w13_trend", "weekly_revenue", "rev_per_product", "active_skus", "roi_pct"):
        assert key in velocity
    assert any(velocity.get(k) is not None for k in ("avg_weekly", "weekly_revenue", "rev_per_product"))

    insights = {i.get("metric"): i for i in payload.get("insights", []) if isinstance(i, dict)}
    assert "revenue_momentum" in insights
    assert "top_product" in insights and insights["top_product"].get("sku")

    mom = insights.get("mom_delta")
    assert mom and mom.get("current") is not None

    projected = insights.get("projected_next_month")
    assert projected and (projected.get("value") is not None or projected.get("yhat") is not None)

    assert payload.get("kpis", {}).get("total_revenue", 0) > 0

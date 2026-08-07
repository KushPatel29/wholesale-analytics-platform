import pandas as pd
import pytest
import shutil
import uuid
from pathlib import Path

import app.blueprints.products as products_bp
from app.services import fact_store


@pytest.fixture
def products_client(app, monkeypatch):
    """Spin up a client with a small, fresh products parquet snapshot."""
    products_bp._STORE_SINGLETON = None
    tmp_dir = Path("cache") / f"products_smoke_{uuid.uuid4().hex}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    today = pd.Timestamp.utcnow().normalize()
    rows = []
    for i in range(3):
        rows.append({
            "date": today - pd.Timedelta(days=30 * i),
            "product_id": "SKU-001",
            "product_name": "Sample Product",
            "customer_id": f"CUST-{i}",
            "customer_name": f"Customer {i}",
            "region": "North",
            "supplier": "Supplier A",
            "order_id": f"ORD-{i}",
            "qty": 10 + i,
            "weight": 5.0 + i,
            "revenue": 1000 + i * 50,
            "cost": 700 + i * 25,
            "discount": 0.0,
        })
    # Add a second SKU for segments/top charts
    rows.append({
        "date": today,
        "product_id": "SKU-002",
        "product_name": "Second Product",
        "customer_id": "CUST-X",
        "customer_name": "Customer X",
        "region": "West",
        "supplier": "Supplier B",
        "order_id": "ORD-X",
        "qty": 5,
        "weight": 2.5,
        "revenue": 500,
        "cost": 320,
        "discount": 0.0,
    })

    df = pd.DataFrame(rows)
    parquet_path = tmp_dir / "products.parquet"
    df.to_parquet(parquet_path)

    monkeypatch.setenv("PRODUCTS_PARQUET_PATH", str(parquet_path))
    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    app.config["PRODUCTS_PARQUET_PATH"] = str(parquet_path)
    app.config["PARQUET_PATH"] = str(parquet_path)
    app.config["DATA_DIR"] = str(parquet_path.parent)
    app.config["LOGIN_DISABLED"] = True
    app.config["AUTHZ_DISABLED"] = True
    app.config["AUTO_CREATE_PRODUCTS_PARQUET"] = True
    fact_store.reset_duckdb_state()
    fact_store.init_views()

    with app.test_client() as client:
        yield client

    products_bp._STORE_SINGLETON = None
    fact_store.reset_duckdb_state()
    shutil.rmtree(tmp_dir, ignore_errors=True)


def test_products_page_loads(products_client):
    resp = products_client.get("/products/")
    assert resp.status_code == 200
    assert b"Product Intelligence" in resp.data


def test_products_page_flag_switch(products_client):
    app = products_client.application

    app.config["PRODUCT_INTELLIGENCE_V2"] = False
    app.config["PRODUCTS_V3"] = False
    resp_v1 = products_client.get("/products/")
    assert resp_v1.status_code == 200
    assert b'data-products-v2="0"' in resp_v1.data
    assert b"healthMatrixPanel" not in resp_v1.data

    app.config["PRODUCT_INTELLIGENCE_V2"] = True
    app.config["PRODUCTS_V3"] = False
    app.config["PRODUCTS_V4"] = False
    resp_v2 = products_client.get("/products/")
    assert resp_v2.status_code == 200
    assert b'data-products-v2="1"' in resp_v2.data
    assert b"healthMatrixPanel" in resp_v2.data

    app.config["PRODUCTS_V3"] = True
    app.config["PRODUCTS_V4"] = False
    resp_v3 = products_client.get("/products/")
    assert resp_v3.status_code == 200
    assert b'data-products-v3="1"' in resp_v3.data
    assert b"toggleCurrentMonth" not in resp_v3.data
    assert b"toggleForecast" not in resp_v3.data

    app.config["PRODUCTS_V4"] = True
    resp_v4 = products_client.get("/products/")
    assert resp_v4.status_code == 200
    assert b'data-products-v4="1"' in resp_v4.data
    assert b"Forecast and partial-month toggles removed" not in resp_v4.data
    assert b"Portfolio map (2x2)" in resp_v4.data
    assert b"workspaceModeExecutive" in resp_v4.data
    assert b"Portfolio strategy layer" in resp_v4.data
    assert b"Execution and priority layer" in resp_v4.data
    assert b"activeFilterSummary" in resp_v4.data
    assert b"strategyLayerContext" in resp_v4.data
    assert b"demandLayerContext" in resp_v4.data
    assert b"pricingLayerContext" in resp_v4.data
    assert b"executionLayerContext" in resp_v4.data
    assert b"assortmentLayerContext" in resp_v4.data
    assert b"tableLayerContext" in resp_v4.data
    assert b"rootCauseHeadline" in resp_v4.data
    assert b"alertCandidateList" in resp_v4.data
    assert b"elasticGuardrailList" in resp_v4.data
    assert b"pricingAlertCandidateList" in resp_v4.data
    assert b"decisionWorkbenchPanel" in resp_v4.data
    assert b"stagedActionsList" in resp_v4.data
    assert b"tableViewPresets" in resp_v4.data
    assert b"productIntelPanel" in resp_v4.data
    assert b"insightTopProductContext" in resp_v4.data
    assert b"velocityPulseMeta" in resp_v4.data
    assert b"productIntelClose" in resp_v4.data


@pytest.mark.parametrize(
    "endpoint",
    [
        "/products/api/overview",
        "/products/api/table",
        "/products/api/trend_delta",
        "/products/api/price_distribution",
        "/products/api/segments",
    ],
)
def test_products_api_endpoints_ok(products_client, endpoint):
    resp = products_client.get(endpoint)
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, dict)
    assert data.get("ok") is True
    assert "data" in data


def test_products_recommendations_api(products_client):
    resp = products_client.get("/products/api/recommendations", query_string={"product_id": "SKU-001"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data.get("ok") is True


def test_products_table_elastic_risk_filter(products_client):
    resp = products_client.get("/products/api/table", query_string={"quick_filters": "elastic_risk"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data.get("ok") is True


def test_products_drilldown_smoke(products_client):
    resp = products_client.get("/products/SKU-001/drilldown")
    assert resp.status_code == 200
    assert b"SKU-001" in resp.data


def test_products_static_assets(products_client):
    resp = products_client.get("/static/js/products.js")
    assert resp.status_code == 200
    ctype = resp.headers.get("Content-Type", "")
    assert "javascript" in ctype.lower()

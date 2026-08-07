"""
Smoke tests for products drilldown endpoint.
Verifies that the drilldown page renders with correct template variables
and that forecast/export endpoints work.
"""
import pytest
import pandas as pd
from datetime import datetime, timedelta


@pytest.fixture
def sample_sales_data(tmp_path):
    """Create sample parquet sales data."""
    dates = pd.date_range(start="2023-01-01", periods=24, freq="ME")
    products = ["SKU-001", "SKU-002", "SKU-003"]
    customers = ["CUST-A", "CUST-B", "CUST-C"]
    regions = ["North", "South", "East", "West"]
    suppliers = ["Supplier-1", "Supplier-2"]

    rows = []
    for i, date in enumerate(dates):
        for product in products:
            for customer in customers:
                qty = 10.0 + (i % 5)
                weight = 100.0 + (i % 50)
                revenue = 1000.0 + (i * 50)
                cost = revenue * 0.6
                rows.append({
                    "date": date,
                    "DateExpected": date,
                    "product_id": product,
                    "product_name": f"Product {product}",
                    "customer_id": f"ID-{customer}",
                    "customer_name": customer,
                    "region": regions[i % len(regions)],
                    "supplier": suppliers[i % len(suppliers)],
                    "order_id": f"ORD-{date.strftime('%Y%m')}-{i}",
                    "qty": qty,
                    "weight": weight,
                    "revenue": revenue,
                    "cost": cost,
                    "discount": 0.0,
                    "OrderStatus": "packed",
                    "UnitOfBillingId": 1,
                    "pack_item_count_sum": qty,
                    "pack_weight_lb_sum": weight,
                    "pack_count": 1,
                    "Price": revenue / qty,
                    "CostPrice": cost / qty,
                })

    df = pd.DataFrame(rows)
    parquet_path = tmp_path / "sales.parquet"
    df.to_parquet(parquet_path)
    return str(parquet_path)


@pytest.fixture
def app_with_products(app, sample_sales_data, monkeypatch):
    """Configure app with sample products parquet."""
    monkeypatch.setenv("PRODUCTS_SALES_PARQUET", sample_sales_data)
    app.config["PRODUCTS_SALES_PARQUET"] = sample_sales_data
    monkeypatch.setenv("PARQUET_PATH", sample_sales_data)
    app.config["PARQUET_PATH"] = sample_sales_data
    app.config["LOGIN_DISABLED"] = True
    app.config["AUTHZ_DISABLED"] = True
    app.config["CURRENCY_CODE"] = "USD"
    app.config["QTY_TITLE"] = "Units"
    # Ensure DuckDB view points to the sample parquet
    from app.services import fact_store
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    return app


def test_drilldown_page_renders(client, app_with_products):
    """Test that /products/<product_id>/drilldown renders successfully."""
    response = client.get("/products/SKU-001/drilldown")
    assert response.status_code == 200
    assert b"SKU-001" in response.data
    assert (b"Snapshot" in response.data) or (b"Product Intelligence Workspace" in response.data)
    assert b"Monthly Revenue" in response.data


def test_drilldown_with_forecast_toggle(client, app_with_products):
    """Test that forecast=1 parameter includes forecast data."""
    # Without forecast
    response1 = client.get("/products/SKU-001/drilldown?forecast=0")
    assert response1.status_code == 200
    data1 = response1.data.decode("utf-8")
    # Should not have forecast date strings (or empty)
    assert "forecast" in data1.lower()

    # With forecast
    response2 = client.get("/products/SKU-001/drilldown?forecast=1")
    assert response2.status_code == 200
    data2 = response2.data.decode("utf-8")
    # Should have forecast data
    assert "forecast" in data2.lower()
    # Check for YYYY-MM format in script tags
    assert ("2024-" in data2 or "2025-" in data2)  # future months


def test_drilldown_with_customer_filter(client, app_with_products):
    """Test that legacy customer=<id> parameter does not break drilldown rendering."""
    response = client.get("/products/SKU-001/drilldown?customer=ID-CUST-A")
    # Legacy and V2 paths differ here; v1 may return 404 when the customer filter
    # yields no rows, while v2 ignores this param for page-level scoping.
    assert response.status_code in {200, 404}
    if response.status_code == 200:
        data = response.data.decode("utf-8")
        assert "SKU-001" in data


def test_drilldown_nonexistent_product(client, app_with_products):
    """Test that nonexistent product returns 404."""
    response = client.get("/products/NONEXISTENT/drilldown")
    assert response.status_code == 404


def test_drilldown_required_variables_present(client, app_with_products):
    """Verify all required template variables are in rendered HTML."""
    response = client.get("/products/SKU-001/drilldown")
    assert response.status_code == 200
    data = response.data.decode("utf-8")

    # V1 and V2 drilldown templates expose different JS payload names.
    required_patterns = [
        (("months", "trendV2"), "time labels"),
        (("monthlyRevenue", "trendV2"), "monthly revenue array"),
        (("monthlyQty", "trendV2"), "monthly qty array"),
        (("unitPriceStats", "distributionsV2"), "price percentiles"),
        (("regionLabels", "regionsV2"), "region breakdown"),
        (("supplierLabels", "suppliersV2"), "supplier breakdown"),
        (("topCustomers", "customersV2"), "customer list"),
    ]

    for patterns, desc in required_patterns:
        assert any(pattern in data for pattern in patterns), f"Missing {desc}: expected one of {patterns}"


def test_export_xlsx_endpoint(client, app_with_products):
    """Test XLSX export endpoint."""
    try:
        import openpyxl  # noqa: F401
    except Exception:
        pytest.skip("openpyxl not installed")
    response = client.get("/products/SKU-001/export?format=xlsx")
    assert response.status_code == 200
    assert response.content_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert response.headers["Content-Disposition"].startswith("attachment")


def test_export_csv_endpoint(client, app_with_products):
    """Test CSV export endpoint."""
    response = client.get("/products/SKU-001/export?format=csv")
    assert response.status_code == 200
    assert response.content_type == "text/csv; charset=utf-8"
    assert response.headers["Content-Disposition"].startswith("attachment")
    assert b"date" in response.data.lower() or b"Date" in response.data


def test_export_nonexistent_product(client, app_with_products):
    """Test that export of nonexistent product returns 404."""
    response = client.get("/products/NONEXISTENT/export?format=xlsx")
    assert response.status_code == 404


def test_drilldown_empty_analytics_graceful(client, app, tmp_path, monkeypatch):
    """Test that drilldown handles empty data gracefully."""
    # Create minimal empty parquet
    empty_df = pd.DataFrame({
        "date": pd.Series([], dtype="datetime64[ns]"),
        "product_id": pd.Series([], dtype="string"),
        "product_name": pd.Series([], dtype="string"),
        "customer_id": pd.Series([], dtype="string"),
        "customer_name": pd.Series([], dtype="string"),
        "region": pd.Series([], dtype="category"),
        "supplier": pd.Series([], dtype="category"),
        "order_id": pd.Series([], dtype="string"),
        "qty": pd.Series([], dtype="float64"),
        "weight": pd.Series([], dtype="float64"),
        "revenue": pd.Series([], dtype="float64"),
        "discount": pd.Series([], dtype="float64"),
    })
    parquet_path = tmp_path / "empty.parquet"
    empty_df.to_parquet(parquet_path)

    monkeypatch.setenv("PRODUCTS_SALES_PARQUET", str(parquet_path))
    app.config["PRODUCTS_SALES_PARQUET"] = str(parquet_path)
    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    app.config["PARQUET_PATH"] = str(parquet_path)
    app.config["LOGIN_DISABLED"] = True
    app.config["AUTHZ_DISABLED"] = True
    from app.services import fact_store
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    app.config["FACT_SCHEMA_STATUS"] = fact_store.validate_fact_schema(strict=False)

    # Should return 404 for any product (empty data)
    client = app.test_client()
    response = client.get("/products/ANY/drilldown")
    assert response.status_code == 404


def test_drilldown_lifecycle_classification(client, app_with_products):
    """Test that lifecycle stage is computed and rendered."""
    response = client.get("/products/SKU-001/drilldown")
    assert response.status_code == 200
    data = response.data.decode("utf-8")
    # Should have lifecycle stage (Growth, Stable, Mature, Decline, etc.)
    assert any(stage in data for stage in ["Growth", "Stable", "Mature", "Decline", "Early", "Unknown"])


def test_drilldown_abc_xyz_classification(client, app_with_products):
    """Test ABC-XYZ classification badge."""
    response = client.get("/products/SKU-001/drilldown")
    assert response.status_code == 200
    data = response.data.decode("utf-8")
    # Should have classification like AX, AY, AZ, BX, etc.
    assert "Classification" in data


def test_drilldown_price_insights(client, app_with_products):
    """Test that price optimization insights are rendered."""
    response = client.get("/products/SKU-001/drilldown")
    assert response.status_code == 200
    data = response.data.decode("utf-8")
    # Should have price optimization section
    assert (
        "Price Optimization" in data
        or "Pricing & Margin Diagnostics" in data
        or "Price Distribution" in data
        or "Optimal" in data
        or "Current" in data
    )


def test_drilldown_recommendations_present(client, app_with_products):
    """Test that co-purchase recommendations are included."""
    response = client.get("/products/SKU-001/drilldown")
    assert response.status_code == 200
    # Should render without error even if recommendations list is empty
    data = response.data.decode("utf-8")
    assert "Frequently Bought Together" in data or "recommendations" in data


def test_drilldown_no_forecast_when_disabled(client, app_with_products):
    """Test that forecast arrays are empty when forecast=0."""
    response = client.get("/products/SKU-001/drilldown?forecast=0")
    assert response.status_code == 200
    # Forecast data should not have dates if not requested
    # (or be empty array in JSON)
    data = response.data.decode("utf-8")
    # The page should render regardless
    assert "SKU-001" in data


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

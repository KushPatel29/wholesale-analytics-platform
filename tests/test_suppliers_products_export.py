import io

import pandas as pd
import pytest

from app.services import fact_store


@pytest.fixture
def seed_supplier_products(tmp_path, monkeypatch):
    rows = []
    order_idx = 1

    # Supplier SUP-A, January window: 40 unique products
    for idx in range(1, 41):
        rows.append(
            {
                "Date": "2026-01-15",
                "DateExpected": "2026-01-15",
                "SupplierId": "SUP-A",
                "SupplierName": "Supplier A",
                "ProductId": f"PA-{idx:03d}",
                "ProductName": f"Prod A {idx:03d}",
                "CustomerId": f"C-A-{idx:03d}",
                "CustomerName": f"Customer A {idx:03d}",
                "OrderId": f"ORD-{order_idx:05d}",
                "Revenue": float(1000 + idx),
                "Cost": float(700 + idx),
                "QuantityShipped": float(5 + idx),
                "WeightLb": float(10 + idx),
            }
        )
        order_idx += 1

    # Supplier SUP-A, February window: 12 different products
    for idx in range(1, 13):
        rows.append(
            {
                "Date": "2026-02-15",
                "DateExpected": "2026-02-15",
                "SupplierId": "SUP-A",
                "SupplierName": "Supplier A",
                "ProductId": f"PB-{idx:03d}",
                "ProductName": f"Prod B {idx:03d}",
                "CustomerId": f"C-B-{idx:03d}",
                "CustomerName": f"Customer B {idx:03d}",
                "OrderId": f"ORD-{order_idx:05d}",
                "Revenue": float(800 + idx),
                "Cost": float(500 + idx),
                "QuantityShipped": float(3 + idx),
                "WeightLb": float(6 + idx),
            }
        )
        order_idx += 1

    # Another supplier to verify supplier scoping
    for idx in range(1, 21):
        rows.append(
            {
                "Date": "2026-01-20",
                "DateExpected": "2026-01-20",
                "SupplierId": "SUP-B",
                "SupplierName": "Supplier B",
                "ProductId": f"PC-{idx:03d}",
                "ProductName": f"Prod C {idx:03d}",
                "CustomerId": f"C-C-{idx:03d}",
                "CustomerName": f"Customer C {idx:03d}",
                "OrderId": f"ORD-{order_idx:05d}",
                "Revenue": float(900 + idx),
                "Cost": float(650 + idx),
                "QuantityShipped": float(2 + idx),
                "WeightLb": float(4 + idx),
            }
        )
        order_idx += 1

    df = pd.DataFrame(rows)
    parquet_path = tmp_path / "fact_suppliers_products.parquet"
    df.to_parquet(parquet_path)

    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield {"supplier_id": "SUP-A"}
    fact_store.reset_duckdb_state()


def test_supplier_products_export_csv_not_paginated(client, seed_supplier_products):
    sid = seed_supplier_products["supplier_id"]
    resp = client.get(
        f"/api/suppliers/{sid}/products/export.csv",
        query_string={"start": "2026-01-01", "end": "2026-01-31", "topN": 25},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)

    out = pd.read_csv(io.BytesIO(resp.data))
    assert len(out) == 40
    assert "Product ID" in out.columns
    assert out["Product ID"].nunique() == 40


def test_supplier_products_export_respects_date_window(client, seed_supplier_products):
    sid = seed_supplier_products["supplier_id"]

    jan_resp = client.get(
        f"/api/suppliers/{sid}/products/export.csv",
        query_string={"start": "2026-01-01", "end": "2026-01-31"},
    )
    feb_resp = client.get(
        f"/api/suppliers/{sid}/products/export.csv",
        query_string={"start": "2026-02-01", "end": "2026-02-28"},
    )

    assert jan_resp.status_code == 200, jan_resp.get_data(as_text=True)
    assert feb_resp.status_code == 200, feb_resp.get_data(as_text=True)

    jan_df = pd.read_csv(io.BytesIO(jan_resp.data))
    feb_df = pd.read_csv(io.BytesIO(feb_resp.data))

    assert len(jan_df) == 40
    assert len(feb_df) == 12
    assert set(jan_df["Product ID"]).issubset({f"PA-{i:03d}" for i in range(1, 41)})
    assert set(feb_df["Product ID"]).issubset({f"PB-{i:03d}" for i in range(1, 13)})

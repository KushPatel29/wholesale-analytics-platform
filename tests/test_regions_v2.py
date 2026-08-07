from __future__ import annotations

import io

import pandas as pd
import pytest

from app.services import fact_store


@pytest.fixture
def seed_regions_v2(tmp_path, monkeypatch):
    rows = []
    for idx in range(1, 13):
        region = f"Region-{idx:02d}"
        feb_revenue = float(900 + idx * 100)
        mar_revenue = float(1100 + idx * 120)
        for month, revenue in (("02", feb_revenue), ("03", mar_revenue)):
            rows.append(
                {
                    "Date": f"2025-{month}-05",
                    "DateExpected": f"2025-{month}-05",
                    "RegionName": region,
                    "OrderId": f"{region}-O-{month}",
                    "CustomerId": f"{region}-C-1",
                    "CustomerName": f"Customer {region}",
                    "ProductId": f"{region}-P-1",
                    "ProductName": f"Product {region}",
                    "ShippingMethodName": "Ground" if idx % 2 else "Air",
                    "SupplierId": f"S-{idx:02d}",
                    "SupplierName": f"Supplier {idx:02d}",
                    "OrderStatus": "packed",
                    "Revenue": revenue,
                    "Cost": round(revenue * 0.62, 2),
                    "QuantityShipped": float(4 + idx),
                    "UnitOfBillingId": 1,
                    "pack_item_count_sum": 1.0,
                    "pack_weight_lb_sum": float(10 + idx),
                    "pack_count": 1,
                    "Price": revenue,
                    "CostPrice": round(revenue * 0.62, 2),
                }
            )

    frame = pd.DataFrame(rows)
    parquet_path = tmp_path / "fact_regions_v2.parquet"
    frame.to_parquet(parquet_path)

    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    fact_store.reset_duckdb_state()


def test_regions_index_uses_legacy_template_when_v2_flags_off(app_client, seed_regions_v2):
    app_client.application.config.update(REGIONS_V2=False, REGION_OVERVIEW_V2=False)

    resp = app_client.get("/regions/", query_string={"start": "2025-03-01", "end": "2025-04-01", "_gf": "1"})

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Regions Overview" in body
    assert "Regions Performance" not in body
    assert "js/regions.js" in body
    assert "js/regions_v2.js" not in body


def test_regions_index_uses_v2_template_when_flags_enabled(app_client, seed_regions_v2):
    app_client.application.config.update(REGIONS_V2=True, REGION_OVERVIEW_V2=True)

    resp = app_client.get("/regions/", query_string={"start": "2025-03-01", "end": "2025-04-01", "_gf": "1"})

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Regions Performance" in body
    assert "Regional Command Center" in body
    assert "js/regions_v2.js" in body
    assert "js/regions.js" not in body


def test_regions_table_export_uses_full_filtered_dataset_not_visible_page(app_client, seed_regions_v2):
    resp = app_client.get(
        "/regions/export",
        query_string={
            "format": "csv",
            "dataset": "table",
            "start": "2025-01-01",
            "end": "2025-12-31",
            "search": "Region-0",
            "page": 1,
            "page_size": 5,
        },
    )

    assert resp.status_code == 200
    frame = pd.read_csv(io.StringIO(resp.get_data(as_text=True)))
    assert len(frame.index) == 9
    assert frame["Region"].str.contains("Region-0", regex=False).all()


def test_regions_bundle_sparse_window_returns_empty_payload(app_client, seed_regions_v2):
    resp = app_client.get(
        "/api/regions/bundle",
        query_string={"start": "2024-01-01", "end": "2024-02-01", "page": 1, "page_size": 25},
    )

    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert (payload.get("table") or {}).get("rows") == []
    assert int(((payload.get("kpis") or {}).get("regions_count") or 0)) == 0
    assert ((payload.get("momentum") or {}).get("window") or {}).get("has_prior_period") is False


def test_regions_momentum_uses_immediately_prior_window(app_client, seed_regions_v2):
    resp = app_client.get(
        "/api/regions/bundle",
        query_string={"start": "2025-03-01", "end": "2025-03-31", "page": 1, "page_size": 25},
    )

    assert resp.status_code == 200
    payload = resp.get_json() or {}
    momentum = payload.get("momentum") or {}
    window = momentum.get("window") or {}
    rows = momentum.get("rows") or []

    assert window.get("current_start") == "2025-03-01"
    assert window.get("current_end") == "2025-04-01"
    assert window.get("prior_start") == "2025-01-29"
    assert window.get("prior_end") == "2025-03-01"
    assert window.get("has_prior_period") is True

    region_01 = next(row for row in rows if row.get("region") == "Region-01")
    assert region_01["revenue_current"] == 1220.0
    assert region_01["revenue_prior"] == 1000.0
    assert region_01["delta_revenue"] == 220.0
    assert region_01["delta_revenue_pct"] == 22.0
    assert region_01["delta_revenue_status"] == "gainer"
    assert region_01["delta_revenue_label"] == "22.0%"

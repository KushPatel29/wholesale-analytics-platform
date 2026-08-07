import math

import pandas as pd
import pytest

from app.services import fact_store
from app.services import regions_bundle


@pytest.fixture
def seed_regions(tmp_path, monkeypatch):
    rows = []
    regions = [f"Region-{i:02d}" for i in range(1, 31)]
    order_idx = 1
    for idx, region in enumerate(regions, start=1):
        for order_no in (1, 2):
            revenue = float(1000 + idx * 25 + order_no * 10)
            cost = None if region == "Region-05" else revenue * 0.6
            rows.append(
                {
                    "Date": f"2025-06-{order_no:02d}",
                    "DateExpected": f"2025-06-{order_no:02d}",
                    "RegionName": region,
                    "OrderId": f"O-{order_idx}",
                    "CustomerId": f"C-{idx:02d}",
                    "CustomerName": f"Customer {idx:02d}",
                    "ProductId": f"P-{idx:02d}",
                    "ProductName": f"Product {idx:02d}",
                    "ShippingMethodName": "Ground",
                    "OrderStatus": "packed",
                    "Revenue": revenue,
                    "Cost": cost,
                    "QuantityShipped": 5 + order_no,
                    "UnitOfBillingId": 1,
                    "pack_item_count_sum": 1.0,
                    "pack_weight_lb_sum": 0.0,
                    "pack_count": 1,
                    "Price": revenue,
                    "CostPrice": (cost if cost is not None else revenue * 0.6),
                }
            )
            order_idx += 1

    df = pd.DataFrame(rows)
    parquet_path = tmp_path / "fact_regions.parquet"
    df.to_parquet(parquet_path)

    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    fact_store.reset_duckdb_state()


def test_regions_bundle_keys_and_query_budget(app_client, seed_regions):
    resp = app_client.get(
        "/api/regions/bundle",
        query_string={"start": "2025-01-01", "end": "2025-12-31", "page_size": 25, "page": 1},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert isinstance(payload, dict)
    for key in ("kpis", "trend", "table", "meta"):
        assert key in payload
    meta = payload.get("meta", {})
    assert meta.get("dataset_version")
    query_count = int(meta.get("duckdb_query_count", 0) or 0)
    assert 3 <= query_count <= 6

    kpis = payload.get("kpis", {})
    assert kpis.get("total_revenue") is not None
    assert int(kpis.get("regions_count") or 0) == 30
    assert kpis.get("avg_order_value") is not None

    chart = (payload.get("charts") or {}).get("revenue_by_region", {})
    assert len(chart.get("labels") or []) >= 25
    assert len(chart.get("values") or []) >= 25


def test_regions_bundle_pagination_and_non_null_metrics(app_client, seed_regions):
    base_qs = {"start": "2025-01-01", "end": "2025-12-31", "page_size": 25}
    resp1 = app_client.get("/api/regions/bundle", query_string={**base_qs, "page": 1})
    assert resp1.status_code == 200
    payload1 = resp1.get_json()
    table1 = payload1.get("table", {})
    assert int(table1.get("total") or 0) > 25
    assert len(table1.get("rows") or []) == 25

    resp2 = app_client.get("/api/regions/bundle", query_string={**base_qs, "page": 2})
    assert resp2.status_code == 200
    table2 = (resp2.get_json() or {}).get("table", {})
    assert len(table2.get("rows") or []) >= 5

    for row in (table1.get("rows") or [])[:5]:
        assert row.get("region")
        assert row.get("revenue") is not None
        assert row.get("orders") is not None
        assert row.get("customers") is not None
        assert row.get("aov") is not None
        assert row.get("repeat_pct") is not None
        assert row.get("churn_pct") is not None
        assert row.get("top_customer_share_pct") is not None
        assert row.get("top_product_share_pct") is not None

    # Regions with missing costs should not report a 100% margin.
    target = next((r for r in (table1.get("rows") or []) if r.get("region") == "Region-05"), None)
    if target is not None:
        margin = target.get("margin_pct")
        assert margin is None or not math.isclose(float(margin), 100.0)
        assert target.get("cost_coverage_pct") == pytest.approx(100.0)


def test_regions_risk_profile_uses_shared_margin_status_when_status_missing():
    risk_band, risk_score, reasons = regions_bundle._risk_profile(
        {
            "margin_pct": 21.0,
            "minimum_margin_pct": 22.0,
            "target_margin_pct": 31.0,
            "status_key": None,
            "churn_pct": 0.0,
            "at_risk_pct": 0.0,
            "top_customer_share_pct": 0.0,
            "top_product_share_pct": 0.0,
            "delta_revenue": 0.0,
            "cost_coverage_pct": 100.0,
            "packs_coverage_pct": 100.0,
        }
    )

    assert risk_band == "Medium"
    assert risk_score == 1
    assert reasons == ["Low margin"]

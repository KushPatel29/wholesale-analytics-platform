import pandas as pd
import pytest

from app.services import bundle_cache
from app.services import fact_store
from app.services import products_bundle


@pytest.fixture
def seed_products(tmp_path, monkeypatch):
    df = pd.DataFrame(
        [
            {
                "Date": "2025-12-01",
                "DateExpected": "2025-12-01",
                "ProductId": "SKU-1",
                "ProductName": "Ribeye",
                "ProteinType": "Beef",
                "Category": "Steak",
                "CustomerId": "C-1",
                "CustomerName": "Chef A",
                "OrderId": "O-1",
                "OrderStatus": "packed",
                "Revenue": 1200.0,
                "Cost": 800.0,
                "QuantityShipped": 100,
                "WeightLb": 300,
                "UnitOfBillingId": 1,
                "pack_item_count_sum": 100.0,
                "pack_weight_lb_sum": 300.0,
                "pack_count": 1,
                "Price": 12.0,
                "CostPrice": 8.0,
            },
            {
                "Date": "2025-12-15",
                "DateExpected": "2025-12-15",
                "ProductId": "SKU-2",
                "ProductName": "Tenderloin",
                "ProteinType": "Pork",
                "Category": "Roast",
                "CustomerId": "C-2",
                "CustomerName": "Chef B",
                "OrderId": "O-2",
                "OrderStatus": "packed",
                "Revenue": 900.0,
                "Cost": 500.0,
                "QuantityShipped": 60,
                "WeightLb": 180,
                "UnitOfBillingId": 1,
                "pack_item_count_sum": 60.0,
                "pack_weight_lb_sum": 180.0,
                "pack_count": 1,
                "Price": 15.0,
                "CostPrice": 8.3333333333,
            },
            {
                "Date": "2026-01-05",
                "DateExpected": "2026-01-05",
                "ProductId": "SKU-1",
                "ProductName": "Ribeye",
                "ProteinType": "Beef",
                "Category": "Steak",
                "CustomerId": "C-3",
                "CustomerName": "Chef C",
                "OrderId": "O-3",
                "OrderStatus": "packed",
                "Revenue": 600.0,
                "Cost": 350.0,
                "QuantityShipped": 40,
                "WeightLb": 120,
                "UnitOfBillingId": 1,
                "pack_item_count_sum": 40.0,
                "pack_weight_lb_sum": 120.0,
                "pack_count": 1,
                "Price": 15.0,
                "CostPrice": 8.75,
            },
        ]
    )
    parquet_path = tmp_path / "fact.parquet"
    df.to_parquet(parquet_path)
    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    # Reset DuckDB view to point at test parquet
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    # Cleanup: drop view so other tests can re-init with their own paths
    fact_store.reset_duckdb_state()


@pytest.fixture
def seed_products_protein_fallback(tmp_path, monkeypatch):
    df = pd.DataFrame(
        [
            {
                "Date": "2025-12-01",
                "DateExpected": "2025-12-01",
                "ProductId": "SKU-9",
                "ProductName": "Striploin",
                "Protein": "Beef",
                "ProteinType": None,
                "Category": None,
                "ProductCategory": "Steak",
                "CustomerId": "C-1",
                "CustomerName": "Chef A",
                "OrderId": "O-9",
                "OrderStatus": "packed",
                "Revenue": 500.0,
                "Cost": 320.0,
                "QuantityShipped": 25,
                "WeightLb": 75,
                "UnitOfBillingId": 1,
                "pack_item_count_sum": 25.0,
                "pack_weight_lb_sum": 75.0,
                "pack_count": 1,
                "Price": 20.0,
                "CostPrice": 12.8,
            }
        ]
    )
    parquet_path = tmp_path / "fact_protein_fallback.parquet"
    df.to_parquet(parquet_path)
    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    fact_store.reset_duckdb_state()


def test_products_bundle_has_keys(app_client, seed_products):
    resp = app_client.get("/api/products/bundle")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "kpis" in data and "charts" in data and "table" in data
    assert "comparison" in data
    assert "comparison_summary" in data
    assert "story" in data
    assert "price_vs_velocity" in data
    assert "performance_bubble" in data
    assert "recommendations" in data
    assert "ai_signals" in data
    assert "projected_next_month" in data
    assert "health_matrix" in data
    assert "margin_matrix" in data
    assert "decision_signals" in data
    assert "portfolio_posture" in data
    assert "focus_actions" in data
    assert data["kpis"]["customers"] >= 0
    assert data["kpis"]["qty"] >= 0
    assert isinstance(data["table"].get("total"), int)
    health_matrix = data.get("health_matrix") or {}
    assert isinstance(health_matrix.get("quadrants"), list)
    assert "pricing_guardrails" in data
    assert "execution_lists" in data
    assert "mix_shift" in ((data.get("charts") or {}).get("segments") or {})
    bubble = data.get("performance_bubble") or {}
    assert isinstance(bubble.get("summary_cards"), list)
    assert isinstance(bubble.get("legend"), list)
    velocity_points = data.get("price_vs_velocity") or []
    if velocity_points:
        point0 = velocity_points[0]
        assert "minimum_price_lb" in point0
        assert "target_price_lb" in point0
        assert "asp_lb_gap_to_target" in point0
        assert "profit_uplift_target" in point0
    if data["table"].get("rows"):
        row0 = data["table"]["rows"][0]
        assert "last_sold" in row0
        assert "quick_rec" in row0
        assert "intel_url" in row0
        assert "profit_share" in row0
        assert "margin_risk" in row0
        assert "volatility_score" in row0
        assert "revenue_current" in row0
        assert "revenue_prior" in row0
        assert "revenue_delta" in row0
        assert "revenue_delta_pct" in row0
        assert "profit_current" in row0
        assert "profit_prior" in row0
        assert "profit_delta" in row0
        assert "orders_current" in row0
        assert "orders_prior" in row0
        assert "customer_count" in row0
        assert "supplier_count" in row0
        assert "top_customer_name" in row0
        assert "top_customer_share" in row0
        assert "customer_hhi" in row0
        assert "top_region_name" in row0
        assert "top_region_share" in row0
        assert row0.get("target_margin_pct") == pytest.approx(26.0, abs=0.01)
        assert row0.get("minimum_margin_pct") == pytest.approx(17.0, abs=0.01)
        assert row0.get("minimum_price") is not None
        assert row0.get("target_price") is not None
        assert row0.get("margin_status") in {"yellow", "light_green", "green", "orange", "red", "needs_mapping", "no_cost"}


def test_products_bundle_exposes_portfolio_posture_and_signals(app_client, seed_products):
    resp = app_client.get("/api/products/bundle")
    assert resp.status_code == 200
    data = resp.get_json()

    posture = data.get("portfolio_posture") or {}
    assert posture.get("headline")
    assert posture.get("detail")
    assert posture.get("quadrant")

    signals = data.get("decision_signals") or []
    assert len(signals) >= 4
    keys = {signal.get("key") for signal in signals if isinstance(signal, dict)}
    assert {"margin_pressure", "pricing_action", "demand_trend", "portfolio_posture"}.issubset(keys)

    focus_actions = data.get("focus_actions") or []
    assert len(focus_actions) >= 1
    assert all((row.get("title") and row.get("owner")) for row in focus_actions if isinstance(row, dict))


def test_products_bundle_annotation_preserves_existing_profit_uplift_target():
    rows = [
        {
            "sku": "SKU-PERSIST",
            "product_id": "SKU-PERSIST",
            "product_name": "Beef Test",
            "protein_family": "Beef",
            "product_category": "Steak",
            "revenue": 1000.0,
            "cost": 750.0,
            "profit": 250.0,
            "margin_pct": 25.0,
            "unit_cost": 7.5,
            "current_unit_price": 10.0,
            "profit_uplift_target": 420.0,
        }
    ]
    annotated = products_bundle._annotate_product_rows(rows)
    assert annotated[0]["profit_uplift_target"] == pytest.approx(420.0)


def test_execution_lists_rank_new_costing_priorities():
    rows = [
        {
            "sku": "SKU-RED",
            "product_id": "SKU-RED",
            "product_name": "Red Priority",
            "display_name": "SKU-RED — Red Priority",
            "protein_family": "Beef",
            "product_category": "Steak",
            "revenue": 18000.0,
            "profit": -500.0,
            "margin_pct": 12.0,
            "minimum_margin_pct": 17.0,
            "target_margin_pct": 26.0,
            "orders_per_month": 16.0,
            "customer_count": 8,
            "target_achievement_pct": 60.0,
            "asp_lb_gap_to_min": -0.55,
            "asp_lb_gap_to_target": -1.15,
            "profit_uplift_target": 3200.0,
            "status_key": "red",
            "target_status": "Materially below minimum",
            "unit_cost": 7.8,
        },
        {
            "sku": "SKU-YELLOW",
            "product_id": "SKU-YELLOW",
            "product_name": "Yellow Fast Mover",
            "display_name": "SKU-YELLOW — Yellow Fast Mover",
            "protein_family": "Beef",
            "product_category": "Steak",
            "revenue": 22000.0,
            "profit": 1800.0,
            "margin_pct": 22.0,
            "minimum_margin_pct": 17.0,
            "target_margin_pct": 26.0,
            "orders_per_month": 18.0,
            "customer_count": 12,
            "target_achievement_pct": 92.0,
            "asp_lb_gap_to_min": 0.25,
            "asp_lb_gap_to_target": -0.18,
            "profit_uplift_target": 900.0,
            "status_key": "yellow",
            "target_status": "Above minimum, below target",
            "unit_cost": 8.1,
        },
        {
            "sku": "SKU-GREEN",
            "product_id": "SKU-GREEN",
            "product_name": "Green Candidate",
            "display_name": "SKU-GREEN — Green Candidate",
            "protein_family": "Beef",
            "product_category": "Steak",
            "revenue": 9000.0,
            "profit": 2600.0,
            "margin_pct": 33.0,
            "minimum_margin_pct": 17.0,
            "target_margin_pct": 26.0,
            "orders_per_month": 2.0,
            "customer_count": 6,
            "target_achievement_pct": 112.0,
            "asp_lb_gap_to_min": 0.85,
            "asp_lb_gap_to_target": 0.42,
            "profit_uplift_target": 0.0,
            "status_key": "green",
            "target_status": "Strongly above target",
            "unit_cost": 7.2,
        },
        {
            "sku": "SKU-NOCOST",
            "product_id": "SKU-NOCOST",
            "product_name": "Needs Cost",
            "display_name": "SKU-NOCOST — Needs Cost",
            "protein_family": "Beef",
            "product_category": "Steak",
            "revenue": 14000.0,
            "profit": 0.0,
            "margin_pct": None,
            "minimum_margin_pct": 17.0,
            "target_margin_pct": 26.0,
            "orders_per_month": 11.0,
            "customer_count": 9,
            "target_achievement_pct": None,
            "status_key": "no_cost",
            "target_status": "No cost visibility",
            "unit_cost": None,
        },
    ]

    execution = products_bundle._build_execution_lists_from_rows(rows, velocity_cutoff=10.0, limit=None)

    assert execution["pricing_fixes"][0]["sku"] == "SKU-RED"
    assert execution["pricing_fixes"][0]["action"] == "Recover minimum price"
    assert execution["pricing_fixes"][0]["quick_filters"] == ["below_minimum_margin"]
    assert execution["pricing_fixes"][0]["priority_score"] > execution["pricing_fixes"][1]["priority_score"]
    assert execution["cost_fixes"][0]["sku"] == "SKU-NOCOST"
    assert execution["promote_candidates"][0]["sku"] == "SKU-GREEN"


def test_pricing_visual_payload_keeps_lb_rows_and_distinct_status_buckets():
    rows = [
        {
            "sku": "SKU-ORANGE",
            "product_id": "SKU-ORANGE",
            "display_name": "SKU-ORANGE — Near Minimum",
            "asp_lb": 10.2,
            "minimum_price_lb": 10.5,
            "target_price_lb": 12.0,
            "effective_cost_lb": 8.2,
            "revenue": 900.0,
            "orders_per_month": 8.0,
            "status_key": "orange",
            "target_margin_pct": 26.0,
            "minimum_margin_pct": 17.0,
        },
        {
            "sku": "SKU-YELLOW",
            "product_id": "SKU-YELLOW",
            "display_name": "SKU-YELLOW — Below Target",
            "asp_lb": 11.0,
            "minimum_price_lb": 10.0,
            "target_price_lb": 12.5,
            "effective_cost_lb": 8.1,
            "revenue": 1100.0,
            "orders_per_month": 10.0,
            "status_key": "yellow",
            "target_margin_pct": 26.0,
            "minimum_margin_pct": 17.0,
        },
        {
            "sku": "SKU-LG",
            "product_id": "SKU-LG",
            "display_name": "SKU-LG — Near Target",
            "asp_lb": 12.1,
            "minimum_price_lb": 10.0,
            "target_price_lb": 12.0,
            "effective_cost_lb": 8.0,
            "revenue": 1300.0,
            "orders_per_month": 6.0,
            "status_key": "light_green",
            "target_margin_pct": 26.0,
            "minimum_margin_pct": 17.0,
        },
        {
            "sku": "SKU-GREEN",
            "product_id": "SKU-GREEN",
            "display_name": "SKU-GREEN — Above Target",
            "asp_lb": 13.3,
            "minimum_price_lb": 10.0,
            "target_price_lb": 12.0,
            "effective_cost_lb": 7.8,
            "revenue": 1500.0,
            "orders_per_month": 5.0,
            "status_key": "green",
            "target_margin_pct": 26.0,
            "minimum_margin_pct": 17.0,
        },
        {
            "sku": "SKU-MAP",
            "product_id": "SKU-MAP",
            "display_name": "SKU-MAP — Needs Mapping",
            "asp_lb": 9.5,
            "revenue": 400.0,
            "orders_per_month": 3.0,
            "status_key": "needs_mapping",
        },
    ]

    visual = products_bundle._build_pricing_visual_payload(rows)  # noqa: SLF001

    price_vs_velocity = visual.get("price_vs_velocity") or []
    performance_points = visual.get("performance_points") or []
    summary_cards = {card.get("key"): card for card in (visual.get("summary_cards") or [])}

    assert {point.get("sku") for point in price_vs_velocity} == {row["sku"] for row in rows}
    assert any(point.get("sku") == "SKU-YELLOW" and point.get("has_cost") is True for point in performance_points)
    assert summary_cards["below_minimum"]["sku_count"] == 1
    assert summary_cards["below_target"]["sku_count"] == 1
    assert summary_cards["near_target"]["sku_count"] == 1
    assert summary_cards["above_target"]["sku_count"] == 1
    assert summary_cards["needs_attention"]["sku_count"] == 1
    assert sum(card["sku_count"] for card in summary_cards.values()) == len(performance_points)


def test_pricing_visual_payload_prefers_price_status_for_visual_bands():
    rows = [
        {
            "sku": "SKU-A",
            "product_id": "SKU-A",
            "display_name": "SKU-A — Table Yellow but Price Red",
            "pricing_basis": "lb",
            "asp_lb": 10.0,
            "minimum_price_lb": 10.5,
            "target_price_lb": 12.0,
            "effective_cost_lb": 8.3,
            "revenue": 1400.0,
            "orders_per_month": 11.0,
            "status_key": "yellow",
            "price_status_key": "red",
            "price_status": "Materially below minimum",
            "target_status": "Between minimum and target",
            "target_margin_pct": 26.0,
            "minimum_margin_pct": 17.0,
        },
        {
            "sku": "SKU-B",
            "product_id": "SKU-B",
            "display_name": "SKU-B — Healthy",
            "pricing_basis": "lb",
            "asp_lb": 12.8,
            "minimum_price_lb": 10.0,
            "target_price_lb": 12.1,
            "effective_cost_lb": 7.9,
            "revenue": 1000.0,
            "orders_per_month": 7.0,
            "status_key": "green",
            "price_status_key": "green",
            "price_status": "Above target",
            "target_status": "Above target",
            "target_margin_pct": 26.0,
            "minimum_margin_pct": 17.0,
        },
    ]

    visual = products_bundle._build_pricing_visual_payload(rows)  # noqa: SLF001

    summary_cards = {card.get("key"): card for card in (visual.get("summary_cards") or [])}
    point_a = next(point for point in (visual.get("performance_points") or []) if point.get("sku") == "SKU-A")

    assert point_a.get("visual_status_key") == "red"
    assert summary_cards["below_minimum"]["sku_count"] == 1
    assert summary_cards["below_target"]["sku_count"] == 0


def test_prepare_visual_pricing_rows_preserves_unit_basis_for_unit_skus():
    rows = [
        {
            "sku": "SKU-UNIT",
            "product_id": "SKU-UNIT",
            "product_name": "Unit SKU",
            "protein_family": "Beef",
            "product_category": "Steak",
            "revenue": 600.0,
            "cost": 300.0,
            "qty": 50.0,
            "weight": 0.0,
            "unit_price": 12.0,
            "unit_cost": 6.0,
        }
    ]

    annotated = products_bundle._annotate_product_rows(products_bundle._prepare_visual_pricing_rows(rows))  # noqa: SLF001

    assert annotated[0]["pricing_basis"] == "unit"
    assert annotated[0]["pricing_basis_qty"] == pytest.approx(50.0)
    assert annotated[0]["asp_lb"] is None
    assert annotated[0]["minimum_price_lb"] is None
    assert annotated[0]["target_price_lb"] is None
    assert annotated[0]["minimum_price"] is not None
    assert annotated[0]["target_price"] is not None


def test_health_profitability_value_prefers_target_relative_signal():
    row = {
        "target_achievement_pct": 92.5,
        "margin_pct": 38.0,
        "target_margin_pct": 44.0,
    }

    value = products_bundle._health_profitability_value(row)  # noqa: SLF001

    assert value == pytest.approx(-7.5)


def test_build_health_matrix_from_rows_preserves_status_context_on_top_items():
    rows = [
        {
            "sku": "SKU-A",
            "product_id": "SKU-A",
            "product_name": "Alpha",
            "display_name": "SKU-A — Alpha",
            "revenue": 1200.0,
            "profit": 180.0,
            "margin_pct": 18.0,
            "target_margin_pct": 26.0,
            "minimum_margin_pct": 17.0,
            "target_achievement_pct": 92.0,
            "orders_per_month": 14.0,
            "segment": "Core",
            "price_status_key": "yellow",
            "price_status": "Between minimum and target",
            "status_key": "yellow",
            "target_status": "Between minimum and target",
        }
    ]

    out = products_bundle._build_health_matrix_from_rows(rows, total_revenue=1200.0, total_profit=180.0)  # noqa: SLF001
    quadrants = out.get("quadrants") or []
    top_items = [item for quadrant in quadrants for item in (quadrant.get("top_items") or [])]

    assert top_items
    assert top_items[0]["sku"] == "SKU-A"
    assert top_items[0]["price_status_key"] == "yellow"
    assert top_items[0]["segment"] == "Core"
    assert top_items[0]["target_achievement_pct"] == pytest.approx(92.0)


def test_products_bundle_core_kpis_match_seed_data(app_client, seed_products):
    resp = app_client.get("/api/products/bundle", query_string={"start": "2025-12-01", "end": "2026-01-31"})
    assert resp.status_code == 200
    data = resp.get_json()
    kpis = data.get("kpis") or {}

    assert kpis.get("revenue") == pytest.approx(2700.0, abs=0.01)
    assert kpis.get("qty") == pytest.approx(200.0, abs=0.01)
    assert kpis.get("weight") == pytest.approx(600.0, abs=0.01)
    assert kpis.get("products") == 2
    assert kpis.get("customers") == 3
    assert kpis.get("revenue_per_product") == pytest.approx(1350.0, abs=0.01)
    assert kpis.get("revenue_per_customer") == pytest.approx(900.0, abs=0.01)
    assert kpis.get("risk_profit_uplift_target", 0) >= 0


@pytest.fixture
def seed_products_mtd(tmp_path, monkeypatch):
    df = pd.DataFrame(
        [
            {
                "Date": "2026-02-01",
                "DateExpected": "2026-02-01",
                "ProductId": "SKU-1",
                "ProductName": "Ribeye",
                "CustomerId": "C-1",
                "CustomerName": "Chef A",
                "OrderId": "O-1",
                "OrderStatus": "packed",
                "Revenue": 100.0,
                "Cost": 70.0,
                "QuantityShipped": 10,
                "WeightLb": 30,
                "UnitOfBillingId": 1,
                "pack_item_count_sum": 10.0,
                "pack_weight_lb_sum": 30.0,
                "pack_count": 1,
                "Price": 10.0,
                "CostPrice": 7.0,
            },
            {
                "Date": "2026-02-10",
                "DateExpected": "2026-02-10",
                "ProductId": "SKU-2",
                "ProductName": "Tenderloin",
                "CustomerId": "C-2",
                "CustomerName": "Chef B",
                "OrderId": "O-2",
                "OrderStatus": "packed",
                "Revenue": 120.0,
                "Cost": 72.0,
                "QuantityShipped": 8,
                "WeightLb": 24,
                "UnitOfBillingId": 1,
                "pack_item_count_sum": 8.0,
                "pack_weight_lb_sum": 24.0,
                "pack_count": 1,
                "Price": 15.0,
                "CostPrice": 9.0,
            },
            {
                "Date": "2026-02-20",
                "DateExpected": "2026-02-20",
                "ProductId": "SKU-1",
                "ProductName": "Ribeye",
                "CustomerId": "C-3",
                "CustomerName": "Chef C",
                "OrderId": "O-3",
                "OrderStatus": "packed",
                "Revenue": 250.0,
                "Cost": 180.0,
                "QuantityShipped": 15,
                "WeightLb": 45,
                "UnitOfBillingId": 1,
                "pack_item_count_sum": 15.0,
                "pack_weight_lb_sum": 45.0,
                "pack_count": 1,
                "Price": 16.7,
                "CostPrice": 12.0,
            },
            {
                "Date": "2026-03-01",
                "DateExpected": "2026-03-01",
                "ProductId": "SKU-1",
                "ProductName": "Ribeye",
                "CustomerId": "C-1",
                "CustomerName": "Chef A",
                "OrderId": "O-4",
                "OrderStatus": "packed",
                "Revenue": 150.0,
                "Cost": 105.0,
                "QuantityShipped": 12,
                "WeightLb": 36,
                "UnitOfBillingId": 1,
                "pack_item_count_sum": 12.0,
                "pack_weight_lb_sum": 36.0,
                "pack_count": 1,
                "Price": 12.5,
                "CostPrice": 8.75,
            },
            {
                "Date": "2026-03-10",
                "DateExpected": "2026-03-10",
                "ProductId": "SKU-2",
                "ProductName": "Tenderloin",
                "CustomerId": "C-2",
                "CustomerName": "Chef B",
                "OrderId": "O-5",
                "OrderStatus": "packed",
                "Revenue": 180.0,
                "Cost": 108.0,
                "QuantityShipped": 9,
                "WeightLb": 27,
                "UnitOfBillingId": 1,
                "pack_item_count_sum": 9.0,
                "pack_weight_lb_sum": 27.0,
                "pack_count": 1,
                "Price": 20.0,
                "CostPrice": 12.0,
            },
            {
                "Date": "2026-03-25",
                "DateExpected": "2026-03-25",
                "ProductId": "SKU-1",
                "ProductName": "Ribeye",
                "CustomerId": "C-4",
                "CustomerName": "Chef D",
                "OrderId": "O-6",
                "OrderStatus": "packed",
                "Revenue": 400.0,
                "Cost": 260.0,
                "QuantityShipped": 20,
                "WeightLb": 60,
                "UnitOfBillingId": 1,
                "pack_item_count_sum": 20.0,
                "pack_weight_lb_sum": 60.0,
                "pack_count": 1,
                "Price": 20.0,
                "CostPrice": 13.0,
            },
        ]
    )
    parquet_path = tmp_path / "fact_mtd.parquet"
    df.to_parquet(parquet_path)
    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    fact_store.reset_duckdb_state()


def test_products_bundle_uses_safe_mtd_comparison_window(app_client, seed_products_mtd):
    resp = app_client.get(
        "/api/products/bundle",
        query_string={"start": "2026-03-01", "end": "2026-03-15"},
    )
    assert resp.status_code == 200
    data = resp.get_json()

    comparison = data.get("comparison") or {}
    assert comparison.get("method") == "month_to_date_vs_prior_month_same_day"
    assert comparison.get("current_start") == "2026-03-01"
    assert comparison.get("current_end") == "2026-03-15"
    assert comparison.get("prior_start") == "2026-02-01"
    assert comparison.get("prior_end") == "2026-02-15"

    summary = data.get("comparison_summary") or {}
    assert summary.get("revenue_current") == pytest.approx(330.0, abs=0.01)
    assert summary.get("revenue_prior") == pytest.approx(220.0, abs=0.01)
    assert summary.get("revenue_delta_pct") == pytest.approx(50.0, abs=0.01)

    rows = (data.get("table") or {}).get("rows") or []
    sku1 = next((row for row in rows if row.get("product_id") == "SKU-1"), None)
    assert sku1 is not None
    assert sku1.get("revenue_current") == pytest.approx(150.0, abs=0.01)
    assert sku1.get("revenue_prior") == pytest.approx(100.0, abs=0.01)


def test_products_bundle_projects_partial_month_with_mtd_pace(app_client, seed_products_mtd):
    resp = app_client.get(
        "/api/products/bundle",
        query_string={"start": "2026-03-01", "end": "2026-03-15"},
    )
    assert resp.status_code == 200
    data = resp.get_json()

    projected = data.get("projected_next_month") or {}
    assert projected.get("method") == "mtd_daily_pace"
    assert projected.get("value") == pytest.approx(682.0, abs=0.5)
    assert "pace" in str(projected.get("note") or "").lower()


def test_health_matrix_has_population_in_at_least_one_quadrant(app_client, seed_products):
    resp = app_client.get("/api/products/bundle")
    assert resp.status_code == 200
    data = resp.get_json()
    quadrants = ((data.get("health_matrix") or {}).get("quadrants") or [])
    assert len(quadrants) == 4
    total = sum(int(q.get("sku_count") or 0) for q in quadrants)
    assert total > 0


def test_products_bundle_trajectory_grain_switches_to_weekly_for_short_window(app_client, seed_products):
    resp = app_client.get(
        "/api/products/bundle",
        query_string={"start": "2026-01-01", "end": "2026-01-20"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    grain = (((data.get("charts") or {}).get("trajectory") or {}).get("grain"))
    assert grain in {"weekly", "monthly"}
    assert grain == "weekly"


def test_uplift_and_risk_values_are_non_negative(app_client, seed_products):
    resp = app_client.get("/api/products/bundle")
    assert resp.status_code == 200
    data = resp.get_json()
    kpis = data.get("kpis") or {}
    assert float(kpis.get("risk_profit_uplift_target") or 0) >= 0
    assert float(kpis.get("profit_at_risk") or 0) >= 0


def test_products_bundle_display_name(app_client, seed_products):
    resp = app_client.get("/api/products/bundle")
    assert resp.status_code == 200
    data = resp.get_json()
    rows = (data.get("table") or {}).get("rows") or []
    if rows:
        row0 = rows[0]
        assert row0.get("display_name")
        assert str(row0.get("product_id")) in str(row0.get("display_name"))
    top = (data.get("charts") or {}).get("top_products") or []
    if top:
        assert top[0].get("display_name")


def test_products_bundle_includes_protein_intelligence_and_family_columns(app_client, seed_products):
    resp = app_client.get("/api/products/bundle")
    assert resp.status_code == 200
    data = resp.get_json()

    protein_insights = data.get("protein_insights") or {}
    summary = protein_insights.get("summary") or {}
    assert summary.get("top_family") in {"Beef", "Pork"}
    assert summary.get("family_count") == 2
    assert isinstance(protein_insights.get("mix"), list)

    rows = (data.get("table") or {}).get("rows") or []
    assert rows
    assert rows[0].get("protein_family") in {"Beef", "Pork"}
    assert rows[0].get("category") in {"Steak", "Roast"}
    assert rows[0].get("product_category") in {"Steak", "Roast"}
    assert isinstance(protein_insights.get("portfolio"), list)
    assert isinstance(protein_insights.get("leaders"), list)
    assert isinstance(protein_insights.get("pricing_opportunities"), list)
    assert isinstance(protein_insights.get("execution_watch"), list)
    assert "headline" in (protein_insights.get("narrative") or {})
    assert (protein_insights.get("summary") or {}).get("target_margin_range") == "26%"
    assert (protein_insights.get("summary") or {}).get("minimum_margin_range") == "17%"


def test_products_bundle_uses_protein_column_when_type_column_is_sparse(app_client, seed_products_protein_fallback):
    resp = app_client.get("/api/products/bundle", query_string={"start": "2025-12-01", "end": "2025-12-31"})
    assert resp.status_code == 200
    data = resp.get_json() or {}

    rows = ((data.get("table") or {}).get("rows") or [])
    assert rows
    assert rows[0].get("protein_family") == "Beef"
    assert rows[0].get("category") == "Steak"
    assert rows[0].get("target_margin_pct") == pytest.approx(26.0, abs=0.01)
    assert rows[0].get("minimum_margin_pct") == pytest.approx(17.0, abs=0.01)

    protein_summary = ((data.get("protein_insights") or {}).get("summary") or {})
    assert protein_summary.get("top_family") == "Beef"


def test_products_bundle_supports_summary_section_requests(app_client, seed_products):
    resp = app_client.get(
        "/api/products/bundle",
        query_string={"sections": "overview,strategy,demand", "start": "2025-12-01", "end": "2026-01-31"},
    )
    assert resp.status_code == 200
    data = resp.get_json()

    assert data.get("meta", {}).get("bundle_mode") == "summary"
    assert set(data.get("meta", {}).get("sections") or []) == {"overview", "strategy", "demand"}
    assert ((data.get("table") or {}).get("total") or 0) == 0
    assert (((data.get("charts") or {}).get("trajectory") or {}).get("labels") or [])
    assert isinstance(((data.get("protein_insights") or {}).get("portfolio") or []), list)


def test_products_bundle_supports_table_section_requests(app_client, seed_products):
    resp = app_client.get(
        "/api/products/bundle",
        query_string={"sections": "table", "search": "Ribeye", "start": "2025-12-01", "end": "2026-01-31"},
    )
    assert resp.status_code == 200
    data = resp.get_json()

    assert data.get("meta", {}).get("bundle_mode") == "table"
    assert data.get("table", {}).get("search") == "Ribeye"
    rows = (data.get("table") or {}).get("rows") or []
    assert rows
    assert all("Ribeye" in str(row.get("product_name") or row.get("display_name") or "") for row in rows)
    assert ((data.get("charts") or {}).get("price_velocity") or []) == []


def test_empty_multi_filters_are_noop(app_client, seed_products):
    resp = app_client.get(
        "/api/products/bundle",
        query_string=[("start", "2025-12-01"), ("end", "2026-01-31"), ("regions", ""), ("customers", ""), ("products", "")],
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["kpis"]["revenue"] > 0
    assert data["kpis"]["products"] > 0


def test_products_drilldown_bundle(app_client, seed_products):
    resp = app_client.get("/api/products/drilldown/bundle", query_string={"sku": "SKU-1"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert "kpis" in data and "trend" in data and "table" in data
    assert data["kpis"]["customers"] >= 0
    assert data["kpis"]["qty"] >= 0
    assert data.get("meta", {}).get("entity_display_name")
    assert data.get("trend", {}).get("labels")
    assert isinstance(data["table"].get("rows"), list)
    for key in ("monthly_series", "top_customers", "top_regions", "weekday_distribution", "price_distribution", "cross_sell"):
        assert key in data


def test_products_bundle_cache_hit(app_client, seed_products):
    query = {"start": "2025-12-01", "end": "2026-01-31"}
    resp1 = app_client.get("/api/products/bundle", query_string=query)
    assert resp1.status_code == 200
    resp2 = app_client.get("/api/products/bundle", query_string=query)
    assert resp2.status_code == 200
    data2 = resp2.get_json()
    assert data2.get("meta", {}).get("cached") is True


def test_products_bundle_cache_key_changes_for_quick_filters(app_client, seed_products):
    base = {"start": "2025-12-01", "end": "2026-01-31"}
    low_margin = app_client.get("/api/products/bundle", query_string={**base, "quick_filters": "below_target_margin"})
    top_revenue = app_client.get("/api/products/bundle", query_string={**base, "quick_filters": "top_revenue_20"})

    assert low_margin.status_code == 200
    assert top_revenue.status_code == 200

    low_payload = low_margin.get_json()
    top_payload = top_revenue.get_json()

    low_key = (low_payload.get("meta") or {}).get("cache_key")
    top_key = (top_payload.get("meta") or {}).get("cache_key")
    assert low_key and top_key and low_key != top_key


def test_products_bundle_cache_key_changes_for_requested_sections(app_client, seed_products):
    base = {"start": "2025-12-01", "end": "2026-01-31"}
    summary = app_client.get("/api/products/bundle", query_string={**base, "sections": "overview,strategy,demand"})
    table = app_client.get("/api/products/bundle", query_string={**base, "sections": "table"})

    assert summary.status_code == 200
    assert table.status_code == 200

    summary_key = (summary.get_json().get("meta") or {}).get("cache_key")
    table_key = (table.get_json().get("meta") or {}).get("cache_key")
    assert summary_key and table_key and summary_key != table_key


@pytest.mark.parametrize("watchlist_key", ["protect_core", "recover_margin", "promote_candidate", "rationalize_candidate"])
def test_products_bundle_supports_workspace_watchlist_filters(app_client, seed_products, watchlist_key):
    resp = app_client.get(
        "/api/products/bundle",
        query_string={"start": "2025-12-01", "end": "2026-01-31", "quick_filters": watchlist_key},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    table = data.get("table") or {}
    assert watchlist_key in (table.get("quick_filters") or [])
    assert isinstance(table.get("rows"), list)


def test_products_bundle_cache_key_separates_user_scope():
    filters = {"start": "2025-12-01", "end": "2026-01-31"}
    base_scope = {
        "role": "sales",
        "scope_mode": "scoped",
        "permissions_version": "v1",
    }
    key_a = bundle_cache._cache_key(  # noqa: SLF001
        endpoint="products.bundle",
        filters=filters,
        scope={**base_scope, "user_id": "user-a", "scope_hash": "scope-a"},
        dataset_version="dataset-1",
        extras={"quick_filters": "below_target_margin"},
    )
    key_b = bundle_cache._cache_key(  # noqa: SLF001
        endpoint="products.bundle",
        filters=filters,
        scope={**base_scope, "user_id": "user-b", "scope_hash": "scope-b"},
        dataset_version="dataset-1",
        extras={"quick_filters": "below_target_margin"},
    )

    assert key_a != key_b


def test_cached_bundle_returns_defensive_copy():
    bundle_cache._CACHE.clear()  # noqa: SLF001
    filters = {"start": "2025-12-01", "end": "2026-01-31"}
    scope = {"role": "admin", "scope_mode": "all", "scope_hash": "scope-admin", "permissions_version": "1", "user_id": "u-1"}

    first = bundle_cache.cached_bundle(
        endpoint="products.bundle",
        filters=filters,
        scope=scope,
        dataset_version="dataset-1",
        extras={"quick_filters": "all"},
        ttl_seconds=300,
        builder=lambda: {"meta": {"entity": "products"}, "rows": [{"product_id": "SKU-1", "label": "Ribeye"}]},
    )
    first["rows"][0]["label"] = "Mutated"

    second = bundle_cache.cached_bundle(
        endpoint="products.bundle",
        filters=filters,
        scope=scope,
        dataset_version="dataset-1",
        extras={"quick_filters": "all"},
        ttl_seconds=300,
        builder=lambda: {"meta": {"entity": "products"}, "rows": [{"product_id": "SKU-1", "label": "Ribeye"}]},
    )

    assert second["rows"][0]["label"] == "Ribeye"


def test_unit_price_quantiles_float_or_none(app_client, seed_products):
    resp = app_client.get("/api/products/bundle")
    data = resp.get_json()
    kpis = data.get("kpis", {})
    for key in ("unit_price_p10", "unit_price_p50", "unit_price_p90"):
        val = kpis.get(key)
        assert val is None or isinstance(val, (int, float))


def test_health_matrix_tolerates_pandas_na_values():
    out = products_bundle._build_health_matrix(  # noqa: SLF001
        summary_rows=[pd.NA, {"quadrant": pd.NA, "sku_count": pd.NA, "revenue": 10}],
        top_rows=[pd.NA, {"quadrant": pd.NA, "display_name": pd.NA, "product_name": "P1", "revenue": 1}],
        velocity_cutoff=None,
        margin_cutoff=None,
        total_revenue=10,
    )
    assert isinstance(out, dict)
    assert isinstance(out.get("quadrants"), list)
    assert len(out.get("quadrants") or []) == 4


def test_mover_status_labels_for_new_lost_and_low_base():
    new_status = products_bundle._mover_status(250.0, 0.0)  # noqa: SLF001
    lost_status = products_bundle._mover_status(0.0, 320.0)  # noqa: SLF001
    low_base_status = products_bundle._mover_status(520.0, 300.0)  # noqa: SLF001

    assert new_status[0] == "New"
    assert lost_status[0] == "Lost"
    assert low_base_status[0] == "Low base"

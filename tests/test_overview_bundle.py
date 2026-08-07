import time
from datetime import date

import pandas as pd
import pytest

from app.blueprints import overview as overview_bp
from app.services import fact_store
from app.services import overview_v2 as ov2
from app.services.filters import FilterParams


def _bundle_payload() -> dict:
    return {
        "schema": ov2.BUNDLE_SCHEMA_VERSION,
        "kpis": {"revenue": 1000.0, "orders": 10, "customers": 5},
        "trend": {"months": ["2025-01"], "revenue": [1000.0]},
        "mix": {"customer": [], "product": [], "region": []},
        "pareto": {"customer": {}, "product": {}, "region": {}},
        "top_movers": {"customer": {"gainers": [], "decliners": []}, "product": {"gainers": [], "decliners": []}, "region": {"gainers": [], "decliners": []}},
        "health": {"rows": 1},
        "drivers": {
            "enabled": True,
            "schema_version": "driver_decomp_v2_test",
            "coverage": {"cost_pct": 94.5, "cost_available": True},
            "methodology": {"name": "Symmetric Price-Volume-Mix", "grain": "SKU"},
            "mom": {
                "key": "mom",
                "label": "Primary comparison",
                "comparison_label": "Prior comparable window",
                "message": "Primary comparison revenue up $100 driven mainly by Volume (+$80, +80% share).",
                "revenue": {
                    "current": 1000.0,
                    "previous": 900.0,
                    "delta": 100.0,
                    "delta_pct": 11.11,
                    "price_effect": 20.0,
                    "volume_effect": 80.0,
                    "mix_effect": 0.0,
                    "drivers": [],
                    "top_contributors": {"price_effect": [], "volume_effect": [], "mix_effect": []},
                },
                "profit": {
                    "current": 300.0,
                    "previous": 270.0,
                    "delta": 30.0,
                    "delta_pct": 11.11,
                    "price_effect": 8.0,
                    "volume_effect": 22.0,
                    "mix_effect": 0.0,
                    "drivers": [],
                    "top_contributors": {"price_effect": [], "volume_effect": [], "mix_effect": []},
                },
            },
            "yoy": {
                "message": "YoY revenue up $220 driven mainly by Volume (+$140, +64% share).",
                "revenue": {
                    "current": 1000.0,
                    "previous": 780.0,
                    "delta": 220.0,
                    "delta_pct": 28.21,
                    "price_effect": 80.0,
                    "volume_effect": 140.0,
                    "mix_effect": 0.0,
                    "drivers": [],
                    "top_contributors": {"price_effect": [], "volume_effect": [], "mix_effect": []},
                },
                "profit": {
                    "current": 300.0,
                    "previous": 220.0,
                    "delta": 80.0,
                    "delta_pct": 36.36,
                    "price_effect": 30.0,
                    "volume_effect": 50.0,
                    "mix_effect": 0.0,
                    "drivers": [],
                    "top_contributors": {"price_effect": [], "volume_effect": [], "mix_effect": []},
                },
            },
        },
        "operations": {"customers": {}, "activity": {}, "mix": {}, "weekday": []},
        "concentration": {"customer": {}, "product": {}},
        "profitability": {"margin_pct": {}, "margin_risk": []},
        "insights": {"callouts": []},
        "executive_briefing": {
            "biggest_win": {"title": "Revenue momentum", "value": 100.0, "value_fmt": "currency", "detail": "Up versus prior comparable window"},
            "biggest_decline": {"title": "Largest decline", "value": None, "value_fmt": "currency", "detail": "No material decline"},
            "key_risk": {"title": "Key risk", "value": None, "value_fmt": "number", "detail": "None"},
            "top_action": {"title": "Maintain cadence", "value_fmt": "text", "detail": "No urgent remediation"},
            "improved": [],
            "declined": [],
            "watchouts": [],
            "recommended_actions": [],
        },
        "overview_metrics": {
            "window": {},
            "kpis": {},
            "executive": {
                "primary_delta_label": "Prior window",
                "primary_compare_label": "Prior comparable window",
                "comparison_note": "Current filtered window Jan 01, 2025 to Jan 31, 2025 is compared with Dec 01, 2024 to Dec 31, 2024 using the same number of days.",
            },
            "movers": {},
            "decomposition": {},
            "concentration": {},
            "profitability": {},
            "momentum": {},
            "mix": {},
        },
        "meta": {
            "has_data": True,
            "last_refresh": "2026-03-27T12:45:00",
            "data_cutoff": "2026-03-27",
            "refresh_age_days": 1,
            "refresh_age_hours": 18,
            "window": {
                "start": "2025-01-01",
                "end": "2025-01-31",
                "current_window_label": "Jan 1, 2025 to Jan 31, 2025",
                "prior_label": "Prior comparable window",
                "prior_window_label": "Dec 1, 2024 to Dec 31, 2024",
                "delta_short_label": "Prior window",
                "method_label": "Completed months",
                "period_status_label": "Completed period",
                "note": "Current filtered window Jan 1, 2025 to Jan 31, 2025 is compared with Dec 1, 2024 to Dec 31, 2024 using the same number of days.",
            },
            "cache_hit": False,
            "feature_flags": {"driver_decomp_v2": True},
        },
    }


def _overview_seed_frame() -> pd.DataFrame:
    rows = []
    specs = [
        ("2025-03-10", "CUST-01", "Customer A", "PROD-01", "Striploin", "North", "FarmCo", "Beef", 900.0, 600.0, 9.0, 42.0),
        ("2025-03-18", "CUST-02", "Customer B", "PROD-02", "Bacon", "South", "Prairie Meats", "Pork", 760.0, 520.0, 8.0, 37.0),
        ("2025-12-08", "CUST-01", "Customer A", "PROD-01", "Striploin", "North", "FarmCo", "Beef", 1180.0, 780.0, 11.0, 51.0),
        ("2026-01-14", "CUST-02", "Customer B", "PROD-02", "Bacon", "South", "Prairie Meats", "Pork", 1240.0, 840.0, 12.0, 54.0),
        ("2026-02-11", "CUST-03", "Customer C", "PROD-03", "Sausage", "East", "Smokehouse", "Pork", 1320.0, 910.0, 13.0, 58.0),
        ("2026-03-05", "CUST-04", "Customer D", "PROD-04", "Ribeye", "West", "FarmCo", "Beef", 1410.0, 955.0, 14.0, 63.0),
        ("2026-03-18", "CUST-01", "Customer A", "PROD-01", "Striploin", "North", "FarmCo", "Beef", 1490.0, 1005.0, 15.0, 68.0),
    ]
    for idx, (ds, cust_id, cust_name, prod_id, prod_name, region, supplier, protein, revenue, cost, qty, weight) in enumerate(specs, start=1):
        rows.append(
            {
                "Date": ds,
                "DateExpected": ds,
                "ShipDate": ds,
                "OrderId": f"OV-{idx:03d}",
                "CustomerId": cust_id,
                "CustomerName": cust_name,
                "ProductId": prod_id,
                "ProductName": prod_name,
                "RegionName": region,
                "SupplierName": supplier,
                "ProteinName": protein,
                "ShippingMethodName": "Ground" if idx % 2 else "Delivery",
                "OrderStatus": "packed",
                "Revenue": revenue,
                "Cost": cost,
                "CostPrice": cost,
                "Price": revenue,
                "QuantityShipped": qty,
                "pack_item_count_sum": qty,
                "pack_weight_lb_sum": weight,
                "pack_count": 1,
                "UnitOfBillingId": 1,
            }
        )
    return pd.DataFrame(rows)


def _seed_overview_fact(tmp_path, monkeypatch):
    frame = _overview_seed_frame()
    parquet_path = tmp_path / "overview_bundle_fact.parquet"
    frame.to_parquet(parquet_path)
    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    return parquet_path


def test_overview_bundle_schema(app, monkeypatch):
    monkeypatch.setattr(
        ov2,
        "get_bundle_context",
        lambda filters, include_current_month=False, defaulted_window=False: {
            "payload": _bundle_payload(),
            "monthly": None,
            "cache_hit": False,
        },
    )

    with app.test_request_context():
        payload = ov2.build_overview_bundle(FilterParams())

    assert "kpis" in payload and isinstance(payload["kpis"], dict)
    assert isinstance(payload["kpis"].get("revenue"), float)
    assert "trend" in payload and isinstance(payload["trend"].get("months"), list)
    assert "mix" in payload and isinstance(payload["mix"], dict)
    assert "health" in payload and isinstance(payload["health"], dict)
    assert "drivers" in payload and isinstance(payload["drivers"], dict)
    assert "operations" in payload and isinstance(payload["operations"], dict)
    assert "concentration" in payload and isinstance(payload["concentration"], dict)
    assert "profitability" in payload and isinstance(payload["profitability"], dict)
    assert "insights" in payload and isinstance(payload["insights"], dict)
    assert "executive_briefing" in payload and isinstance(payload["executive_briefing"], dict)
    assert "overview_metrics" in payload and isinstance(payload["overview_metrics"], dict)
    assert payload.get("schema") == ov2.BUNDLE_SCHEMA_VERSION


def test_overview_bundle_endpoint_fast(monkeypatch, client):
    monkeypatch.setattr(
        overview_bp.fact_store,
        "query_overview",
        lambda filters, include_current_month=False, defaulted_window=False: _bundle_payload(),
    )

    start = time.perf_counter()
    resp = client.get("/overview/api/bundle")
    elapsed = time.perf_counter() - start

    assert resp.status_code == 200
    assert elapsed < 1.5, "overview bundle should return quickly for small data"


def test_overview_bundle_contains_driver_keys(monkeypatch, client):
    monkeypatch.setattr(
        overview_bp.fact_store,
        "query_overview",
        lambda filters, include_current_month=False, defaulted_window=False: _bundle_payload(),
    )
    resp = client.get("/overview/api/bundle")
    assert resp.status_code == 200
    payload = resp.get_json()
    drivers = payload.get("drivers", {})
    assert "mom" in drivers and "yoy" in drivers
    assert "coverage" in drivers
    assert "revenue" in drivers["mom"]
    assert "price_effect" in drivers["mom"]["revenue"]


def test_overview_bundle_endpoint_real_bundle_regression(app_client, tmp_path, monkeypatch):
    _seed_overview_fact(tmp_path, monkeypatch)
    try:
        resp = app_client.get(
            "/overview/api/bundle",
            query_string={"start": "2025-12-01", "end": "2026-03-19", "date_preset": "90d", "_gf": "1"},
        )
        assert resp.status_code == 200
        payload = resp.get_json()
        assert isinstance(payload, dict)
        meta = payload.get("meta") or {}
        window = meta.get("window") or {}
        assert window.get("start") == "2025-12-01"
        assert window.get("end") == "2026-03-19"
        assert window.get("comparison_label")
        assert window.get("current_window_label")
        assert window.get("prior_window_label")
        assert "kpis" in payload and isinstance(payload.get("kpis"), dict)
        assert payload.get("drivers", {}).get("mom") is not None
        assert payload.get("top_movers", {}).get("customer") is not None
        ops_mix = ((payload.get("operations") or {}).get("mix") or {}).get("protein") or []
        assert ops_mix
        assert {row.get("label") for row in ops_mix if isinstance(row, dict)}.issuperset({"Beef", "Pork"})
        assert (payload.get("kpis") or {}).get("margin_pct") == pytest.approx(32.3393, abs=0.05)
    finally:
        fact_store.reset_duckdb_state()


def test_overview_page_renders_driver_decomposition_block(client, app):
    app.config["OVERVIEW_V2"] = True
    app.config["OVERVIEW_V3"] = False
    app.config["OVERVIEW_V2_CLASSIC"] = False
    resp = client.get("/overview/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="overviewPage"' in html
    assert "Executive KPI command center" in html
    assert "Trend workspace and commercial drivers" in html
    assert "Operational mix" in html
    assert 'data-overview-v2="1"' in html
    assert 'data-overview-v3="1"' in html


def test_overview_page_falls_back_when_flag_off(client, app):
    app.config["OVERVIEW_V2"] = False
    app.config["OVERVIEW_V3"] = False
    app.config["OVERVIEW_V2_CLASSIC"] = False
    resp = client.get("/overview/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'data-overview-v2="1"' not in html


def test_overview_page_renders_v3_when_flag_on(client, app):
    app.config["OVERVIEW_V2"] = False
    app.config["OVERVIEW_V3"] = True
    app.config["OVERVIEW_V2_CLASSIC"] = False
    resp = client.get("/overview/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'data-overview-v3="1"' in html
    assert "Executive KPI command center" in html
    assert "Business health summary rail" in html
    assert "Morning narrative" in html
    assert "Investigation and follow-up rail" in html
    assert "Open customer movers" in html
    assert 'id="trendExportBtn"' in html
    assert 'id="filterCountChip"' in html
    assert 'id="comparisonBasisChip"' in html
    assert 'id="periodModeChip"' in html
    assert 'id="dataCutoffChip"' in html
    assert 'id="commandWindowNote"' in html
    assert 'id="heroPriorWindowChip"' in html
    assert 'id="healthRevenueState"' in html
    assert html.count('id="packsCoverageChip"') == 1
    assert 'id="driversMoversLink"' in html


def test_overview_page_prefers_v3_when_both_flags_enabled(client, app):
    app.config["OVERVIEW_V2"] = True
    app.config["OVERVIEW_V3"] = True
    app.config["OVERVIEW_V2_CLASSIC"] = False
    resp = client.get("/overview/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="overviewPage"' in html
    assert 'data-overview-v3="1"' in html


def test_overview_page_allows_classic_fallback_when_enabled(client, app):
    app.config["OVERVIEW_V2"] = True
    app.config["OVERVIEW_V2_CLASSIC"] = True
    app.config["OVERVIEW_V3"] = False
    resp = client.get("/overview/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="overviewPageV2"' in html
    assert 'data-overview-v3="1"' not in html


def test_overview_snapshot_export_csv(monkeypatch, client):
    monkeypatch.setattr(
        ov2,
        "build_snapshot_sheets",
        lambda filters, include_current_month=True, defaulted_window=False: {
            "Metadata": pd.DataFrame([{"field": "dataset_version", "value": "test-v"}]),
            "KPIs": pd.DataFrame([{"metric": "revenue", "value": 1000.0}]),
        },
    )
    resp = client.get("/overview/api/export/snapshot?format=csv&dataset=kpis")
    assert resp.status_code == 200
    assert "text/csv" in (resp.headers.get("Content-Type") or "").lower()
    body = resp.get_data(as_text=True)
    assert "metric,value" in body
    assert "revenue,1000.0" in body


def test_overview_snapshot_export_drivers_uses_driver_sheets(monkeypatch, client):
    captured = {}
    monkeypatch.setattr(overview_bp, "xlsx_export_available", lambda: True)

    monkeypatch.setattr(
        ov2,
        "build_snapshot_sheets",
        lambda filters, include_current_month=True, defaulted_window=False: {
            "Metadata": pd.DataFrame([{"field": "dataset_version", "value": "test-v"}]),
            "Drivers_MoM": pd.DataFrame([{"driver": "Volume", "delta": 100.0}]),
            "Drivers_YoY": pd.DataFrame([{"driver": "Price", "delta": 220.0}]),
            "KPIs": pd.DataFrame([{"metric": "revenue", "value": 1000.0}]),
        },
    )

    def _fake_xlsx_response(sheets, filename):
        captured["sheets"] = list(sheets.keys())
        captured["filename"] = filename
        return overview_bp.Response("ok", mimetype="text/plain")

    monkeypatch.setattr(overview_bp, "dataframes_to_xlsx_response", _fake_xlsx_response)

    resp = client.get("/overview/api/export/snapshot?format=xlsx&dataset=drivers")
    assert resp.status_code == 200
    assert captured["sheets"] == ["Metadata", "Drivers_MoM", "Drivers_YoY"]
    assert "business_performance_drivers" in captured["filename"]


def test_overview_snapshot_export_concentration_uses_both_concentration_sheets(monkeypatch, client):
    captured = {}
    monkeypatch.setattr(overview_bp, "xlsx_export_available", lambda: True)

    monkeypatch.setattr(
        ov2,
        "build_snapshot_sheets",
        lambda filters, include_current_month=True, defaulted_window=False: {
            "Metadata": pd.DataFrame([{"field": "dataset_version", "value": "test-v"}]),
            "Concentration_Customers": pd.DataFrame([{"label": "Customer A", "top1_share": 25.0}]),
            "Concentration_Products": pd.DataFrame([{"label": "Product A", "top1_share": 18.0}]),
            "KPIs": pd.DataFrame([{"metric": "revenue", "value": 1000.0}]),
        },
    )

    def _fake_xlsx_response(sheets, filename):
        captured["sheets"] = list(sheets.keys())
        captured["filename"] = filename
        return overview_bp.Response("ok", mimetype="text/plain")

    monkeypatch.setattr(overview_bp, "dataframes_to_xlsx_response", _fake_xlsx_response)

    resp = client.get("/overview/api/export/snapshot?format=xlsx&dataset=concentration")
    assert resp.status_code == 200
    assert captured["sheets"] == ["Metadata", "Concentration_Customers", "Concentration_Products"]
    assert "business_performance_concentration" in captured["filename"]


def test_refresh_meta_uses_manifest_refresh_and_cutoff(monkeypatch):
    monkeypatch.setattr(ov2, "_manifest_meta", lambda: ("dataset-v1", "2026-03-27T08:15:00Z"))
    monkeypatch.setattr(ov2, "manifest_max_date", lambda: "2026-03-27")

    meta = ov2._refresh_meta()

    assert meta["version"] == "dataset-v1"
    assert meta["last_refresh"].startswith("2026-03-27T08:15:00")
    assert meta["data_cutoff"] == "2026-03-27"
    assert meta["refresh_age_hours"] is None or meta["refresh_age_hours"] >= 0


def test_snapshot_sheets_include_governed_refresh_metadata(monkeypatch):
    monkeypatch.setattr(
        ov2,
        "build_overview_bundle",
        lambda *args, **kwargs: {
            "meta": {
                "version": "dataset-v1",
                "last_refresh": "2026-03-27T08:15:00",
                "data_cutoff": "2026-03-27",
                "refresh_age_hours": 12,
                "window": {"start": "2026-03-01", "end": "2026-03-27"},
            },
            "health": {
                "rows": 10,
                "cost_missing_pct": 0.0,
                "cost_coverage_pct": 100.0,
                "packs_coverage_pct": 100.0,
                "has_packs_orderlines": 10,
                "total_orderlines": 10,
                "product_mapping_missing": 0,
                "freshness_sla_days": 0,
                "freshness_sla_hours": 12,
                "governed_refresh_at": "2026-03-27T08:15:00",
                "data_cutoff": "2026-03-27",
            },
            "kpis": {"revenue": 1000.0},
        },
    )
    monkeypatch.setattr(ov2, "build_detail_tables", lambda *args, **kwargs: {})

    sheets = ov2.build_snapshot_sheets(FilterParams())

    metadata = sheets["Metadata"]
    health = sheets["Data_Health"]
    metadata_map = dict(zip(metadata["field"], metadata["value"]))
    health_map = dict(zip(health["metric"], health["value"]))

    assert metadata_map["data_cutoff"] == "2026-03-27"
    assert metadata_map["refresh_age_hours"] == 12
    assert health_map["freshness_sla_hours"] == 12
    assert health_map["governed_refresh_at"] == "2026-03-27T08:15:00"


def test_overview_drilldown_json_shape(monkeypatch, client):
    monkeypatch.setattr(
        ov2,
        "build_drilldown_frame",
        lambda *args, **kwargs: pd.DataFrame(
            [{"label": "Customer A", "current": 1250.0, "previous": 0.0, "delta": 1250.0, "delta_pct_label": "New"}]
        ),
    )
    resp = client.get("/overview/api/drilldown/movers?dimension=customer")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["drilldown"] == "movers"
    assert payload["dimension"] == "customer"
    assert payload["rows"][0]["delta_pct_label"] == "New"


def test_overview_drilldown_movers_guardrail_filter(monkeypatch, client):
    monkeypatch.setattr(
        ov2,
        "build_drilldown_frame",
        lambda *args, **kwargs: pd.DataFrame(
            [
                {
                    "label": "Low Base Customer",
                    "current": 420.0,
                    "previous": 120.0,
                    "delta": 300.0,
                    "delta_pct": 250.0,
                    "delta_pct_label": "Low base",
                }
            ]
        ),
    )
    resp = client.get("/overview/api/drilldown/movers?dimension=customer&exclude_low_base=1&min_baseline=500")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["meta"]["rows"] == 0
    assert payload["meta"]["guardrails"]["rows_before"] == 1
    assert payload["meta"]["guardrails"]["rows_filtered"] == 1


def test_overview_forecast_v2_endpoint_contract(monkeypatch, client, app):
    app.config["OVERVIEW_FORECAST_V2"] = True
    monkeypatch.setattr(
        overview_bp.oforecast,
        "forecast_metric_v2",
        lambda *args, **kwargs: {
            "eligible": True,
            "reason": "",
            "model": {
                "name": "seasonal_naive",
                "smape": 0.115,
                "mae": 42.0,
                "train_points": 24,
                "holdout_points": 6,
            },
            "series": [
                {"t": "2025-01", "actual": 1000.0, "forecast": None, "lo": None, "hi": None},
                {"t": "2025-02", "actual": None, "forecast": 980.0, "lo": 900.0, "hi": 1060.0},
            ],
            "notes": ["seasonal model used"],
        },
    )
    resp = client.get("/api/overview/forecast?metric=revenue&granularity=monthly&horizon=6&v2=1")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["eligible"] is True
    assert payload["model"]["name"] == "seasonal_naive"
    assert isinstance(payload["series"], list) and payload["series"]
    assert {"t", "actual", "forecast", "lo", "hi"}.issubset(set(payload["series"][0].keys()))


def test_overview_forecast_v2_endpoint_honors_explicit_v2_opt_in(monkeypatch, client, app):
    app.config["OVERVIEW_FORECAST_V2"] = False
    called = {"v2": 0}

    def _forecast_v2(*args, **kwargs):
        called["v2"] += 1
        return {
            "eligible": True,
            "reason": "",
            "model": {"name": "stl_trend_recent36"},
            "series": [{"t": "2025-01", "actual": 100.0, "forecast": 105.0, "lo": 95.0, "hi": 115.0}],
            "notes": [],
        }

    monkeypatch.setattr(overview_bp.oforecast, "forecast_metric_v2", _forecast_v2)
    resp = client.get("/api/overview/forecast?metric=revenue&granularity=monthly&horizon=6&v2=1")
    assert resp.status_code == 200
    assert called["v2"] == 1


def test_overview_movers_fast_endpoint_shape(monkeypatch, client):
    monkeypatch.setattr(
        ov2,
        "build_drilldown_frame",
        lambda *args, **kwargs: pd.DataFrame(
            [
                {
                    "label": "Customer A",
                    "current": 1000.0,
                    "previous": 700.0,
                    "delta": 300.0,
                    "delta_pct": 42.857,
                    "delta_pct_label": "42.9%",
                }
            ]
        ),
    )
    resp = client.get("/overview/api/movers?dimension=customer&min_baseline=500&exclude_low_base=1")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["dimension"] == "customer"
    assert isinstance(payload["rows"], list)
    assert "meta" in payload and "guardrails" in payload["meta"]
    if payload["rows"]:
        assert {"label", "current", "previous", "delta", "delta_pct_label"}.issubset(set(payload["rows"][0].keys()))


def test_overview_context_forecast_gate_enabled(monkeypatch, app):
    monkeypatch.setattr(
        ov2,
        "build_overview_bundle",
        lambda *args, **kwargs: {
            "trend": {
                "monthly": {"revenue": [100.0] * 20},
            },
            "meta": {"has_data": True},
            "executive_scorecard": {"headline": {}, "unit_economics": {}, "risk_indicators": {}},
            "insights": {},
            "health": {},
            "concentration": {},
            "profitability": {},
            "top_movers": {},
            "drivers": {},
            "kpis": {},
            "operations": {},
        },
    )
    with app.test_request_context():
        context = ov2.build_overview_context(FilterParams())
    assert context["forecast"]["enabled"] is True
    assert context["forecast"]["history_points"] == 20


def test_overview_context_forecast_gate_insufficient_history(monkeypatch, app):
    monkeypatch.setattr(
        ov2,
        "build_overview_bundle",
        lambda *args, **kwargs: {
            "trend": {
                "monthly": {"revenue": [100.0] * 4},
            },
            "meta": {"has_data": True},
            "executive_scorecard": {"headline": {}, "unit_economics": {}, "risk_indicators": {}},
            "insights": {},
            "health": {},
            "concentration": {},
            "profitability": {},
            "top_movers": {},
            "drivers": {},
            "kpis": {},
            "operations": {},
        },
    )
    with app.test_request_context():
        context = ov2.build_overview_context(FilterParams())
    assert context["forecast"]["enabled"] is False
    assert "Insufficient history" in str(context["forecast"]["reason"])


def test_snapshot_sheets_include_required_tabs(monkeypatch, app):
    monkeypatch.setattr(
        ov2,
        "build_overview_bundle",
        lambda filters, include_current_month=True, defaulted_window=False: _bundle_payload(),
    )
    monkeypatch.setattr(
        ov2,
        "build_detail_tables",
        lambda filters, include_current_month=True, defaulted_window=False: {
            "drivers_mom": pd.DataFrame([{"period": "MoM"}]),
            "drivers_yoy": pd.DataFrame([{"period": "YoY"}]),
            "movers_customer": pd.DataFrame([{"label": "A"}]),
            "movers_product": pd.DataFrame([{"label": "P"}]),
            "movers_region": pd.DataFrame([{"label": "R"}]),
            "concentration_customer": pd.DataFrame([{"label": "A"}]),
            "concentration_product": pd.DataFrame([{"label": "P"}]),
            "margin_risk": pd.DataFrame([{"label": "P"}]),
            "data_health_issues": pd.DataFrame([{"label": "Missing cost", "count": 2}]),
        },
    )
    with app.test_request_context():
        sheets = ov2.build_snapshot_sheets(FilterParams())
    required = {
        "Metadata",
        "KPIs",
        "Drivers_MoM",
        "Drivers_YoY",
        "Movers_Customers",
        "Movers_Products",
        "Movers_Regions",
        "Concentration_Customers",
        "Concentration_Products",
        "Margin_Risk",
        "Data_Health",
        "Data_Health_Issues",
    }
    assert required.issubset(set(sheets.keys()))


def test_movers_prior_zero_is_labeled_new():
    current = pd.DataFrame(
        [
            {
                "customer_id": "C-1",
                "customer_name": "Acme",
                "revenue": 1000.0,
                "qty": 10.0,
            }
        ]
    )
    prior = pd.DataFrame(columns=current.columns)
    out = ov2._build_movers_table(current, prior, label_col="customer_name", id_col="customer_id")
    assert not out.empty
    assert out.iloc[0]["delta_pct_label"] == "New"


def test_movers_current_zero_is_labeled_lost():
    current = pd.DataFrame(
        [
            {
                "customer_id": "C-1",
                "customer_name": "Acme",
                "revenue": 0.0,
                "qty": 0.0,
            }
        ]
    )
    prior = pd.DataFrame(
        [
            {
                "customer_id": "C-1",
                "customer_name": "Acme",
                "revenue": 1500.0,
                "qty": 15.0,
            }
        ]
    )
    out = ov2._build_movers_table(current, prior, label_col="customer_name", id_col="customer_id")
    assert not out.empty
    row = out.iloc[0]
    assert row["delta_pct_label"] == "Lost"
    assert row["delta_pct"] == -100.0


def test_movers_small_prior_is_low_base():
    current = pd.DataFrame(
        [
            {
                "customer_id": "C-2",
                "customer_name": "Low Base Co",
                "revenue": 600.0,
                "qty": 6.0,
            }
        ]
    )
    prior = pd.DataFrame(
        [
            {
                "customer_id": "C-2",
                "customer_name": "Low Base Co",
                "revenue": 250.0,
                "qty": 2.5,
            }
        ]
    )
    out = ov2._build_movers_table(current, prior, label_col="customer_name", id_col="customer_id")
    assert not out.empty
    row = out.iloc[0]
    assert row["delta_pct_label"] == "Low base"
    assert bool(row["low_sample"]) is True

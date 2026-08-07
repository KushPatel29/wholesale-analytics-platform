from __future__ import annotations

import copy
import io

import pandas as pd
import pytest

from app.blueprints import products as products_bp


@pytest.fixture
def stub_overview(monkeypatch: pytest.MonkeyPatch) -> dict:
    payload = {
        "kpis": {
            "total_revenue": 3000.0,
            "total_qty": 150.0,
            "unique_products": 2,
            "avg_margin_pct": 25.0,
        },
        "trend": [
            {"period": "2024-08", "revenue": 1200.0},
            {"period": "2024-09", "revenue": 1800.0},
        ],
        "price_dist": {"p10": 5.0, "p50": 10.0, "p90": 15.0},
        "top_movers": [
            {"sku": "SKU1", "desc": "Product One", "delta_rev": 400.0},
        ],
        "top_products": [
            {
                "product_id": "P1",
                "sku": "SKU1",
                "desc": "Product One",
                "category": "Seafood",
                "supplier": "Supplier A",
                "uom": "EA",
                "revenue": 1800.0,
                "qty": 90.0,
                "avg_price": 20.0,
                "margin_pct": 30.0,
                "margin": 540.0,
                "first_sold": "2024-07-01",
                "last_sold": "2024-09-30",
            },
            {
                "product_id": "P2",
                "sku": "SKU2",
                "desc": "Product Two",
                "category": "Beef",
                "supplier": "Supplier B",
                "uom": "EA",
                "revenue": 1200.0,
                "qty": 60.0,
                "avg_price": 20.0,
                "margin_pct": 20.0,
                "margin": 240.0,
                "first_sold": "2024-07-15",
                "last_sold": "2024-09-25",
            },
        ],
        "breakdowns": {
            "by_category": [{"key": "Seafood", "revenue": 1800.0}, {"key": "Beef", "revenue": 1200.0}],
            "by_region": [],
            "by_supplier": [],
            "by_uom": [],
        },
        "pareto": [
            {"rank": 1, "sku": "SKU1", "revenue": 1800.0, "cum_pct": 60.0},
            {"rank": 2, "sku": "SKU2", "revenue": 1200.0, "cum_pct": 100.0},
        ],
    }

    def fake_overview(limit=None, include_costs: bool = False):
        data = copy.deepcopy(payload)
        if not include_costs:
            data = products_bp._sanitize_cost_sensitive_fields(data)
        return data

    monkeypatch.setattr(products_bp, "_build_overview_from_service", fake_overview)
    return payload


def test_export_totals_consistent_between_formats(client, stub_overview):
    pytest.importorskip("openpyxl")

    resp_xlsx = client.get("/products/export/overview.xlsx")
    assert resp_xlsx.status_code == 200
    xlsx_df = pd.read_excel(io.BytesIO(resp_xlsx.data), sheet_name="Top Products")
    xlsx_df.columns = [col.lower() for col in xlsx_df.columns]

    resp_csv = client.get("/products/export/table.csv")
    assert resp_csv.status_code == 200
    csv_df = pd.read_csv(io.BytesIO(resp_csv.data))
    csv_df.columns = [col.lower() for col in csv_df.columns]

    expected_total = sum(item["revenue"] for item in stub_overview["top_products"])

    assert xlsx_df["revenue"].sum() == pytest.approx(expected_total, abs=0.01)
    assert csv_df["revenue"].sum() == pytest.approx(expected_total, abs=0.01)
    assert xlsx_df["revenue"].sum() == pytest.approx(csv_df["revenue"].sum(), abs=0.01)


def test_export_table_returns_all_rows_and_respects_requested_columns(client, monkeypatch):
    rows = []
    for idx in range(32):
        rows.append(
            {
                "product_id": f"P{idx:03d}",
                "sku": f"SKU{idx:03d}",
                "product_name": f"Product {idx:03d}",
                "revenue": float(100 + idx),
                "segment": "Stars" if idx % 2 == 0 else "Long Tail",
                "margin_risk": "Healthy" if idx % 3 else "Below target",
            }
        )

    def fake_bundle(_page, args):
        page = int(args.get("page", 1))
        page_size = int(args.get("page_size") or args.get("per_page") or 25)
        start = (page - 1) * page_size
        end = start + page_size
        return {
            "table": {
                "rows": rows[start:end],
                "page": page,
                "page_size": page_size,
                "total": len(rows),
            }
        }

    monkeypatch.setattr("app.services.bundle_service.bundle", fake_bundle)

    resp = client.get("/products/export/table.csv", query_string={"columns": "sku,product,revenue,margin_risk"})
    assert resp.status_code == 200

    exported = pd.read_csv(io.BytesIO(resp.data))
    assert len(exported) == 32
    assert list(exported.columns) == ["sku", "product_name", "revenue", "margin_risk"]


def test_export_movers_csv_returns_all_rows_with_status(client, monkeypatch):
    rows = []
    for idx in range(37):
        rows.append(
            {
                "product_id": f"P{idx:03d}",
                "sku": f"SKU{idx:03d}",
                "product_name": f"Product {idx:03d}",
                "desc": f"Product {idx:03d}",
                "segment": "Stars" if idx % 2 == 0 else "Long Tail",
                "revenue_current": float(200 + idx * 10),
                "revenue_prior": float(100 + idx * 8),
                "revenue_delta": float((200 + idx * 10) - (100 + idx * 8)),
            }
        )
    rows[0]["revenue_prior"] = 0.0
    rows[1]["revenue_current"] = 0.0
    rows[1]["revenue_prior"] = 300.0

    def fake_bundle(_page, args):
        page = int(args.get("page", 1))
        page_size = int(args.get("page_size") or args.get("per_page") or 25)
        start = (page - 1) * page_size
        end = start + page_size
        return {
            "table": {
                "rows": rows[start:end],
                "page": page,
                "page_size": page_size,
                "total": len(rows),
            }
        }

    monkeypatch.setattr("app.services.bundle_service.bundle", fake_bundle)

    resp = client.get("/products/export/movers.csv")
    assert resp.status_code == 200
    exported = pd.read_csv(io.BytesIO(resp.data))
    assert len(exported.index) == 37
    assert {"status", "delta_revenue", "revenue_current", "revenue_prior"}.issubset(set(exported.columns))
    assert "New" in set(exported["status"].astype(str))
    assert "Lost" in set(exported["status"].astype(str))


def test_export_execution_csv_returns_full_filtered_list(client, monkeypatch):
    rows = []
    for idx in range(40):
        rows.append(
            {
                "product_id": f"P{idx:03d}",
                "sku": f"SKU{idx:03d}",
                "product_name": f"Product {idx:03d}",
                "desc": f"Product {idx:03d}",
                "segment": "Stars" if idx % 2 == 0 else "Long Tail",
                "revenue": float(1000 + idx * 50),
                "profit": float(150 + idx * 7),
                "margin_pct": 18.0 if idx % 3 == 0 else 32.0,
                "velocity_per_month": float(2 + idx % 8),
                "cost": 0.0 if idx % 9 == 0 else float(600 + idx * 20),
                "contribution_margin_lb": float(1.5 + idx * 0.1),
            }
        )

    def fake_bundle(_page, args):
        page = int(args.get("page", 1))
        page_size = int(args.get("page_size") or args.get("per_page") or 25)
        start = (page - 1) * page_size
        end = start + page_size
        return {
            "table": {
                "rows": rows[start:end],
                "page": page,
                "page_size": page_size,
                "total": len(rows),
            }
        }

    monkeypatch.setattr("app.services.bundle_service.bundle", fake_bundle)

    resp = client.get("/products/export/execution.csv", query_string={"list": "pricing_fixes"})
    assert resp.status_code == 200
    exported = pd.read_csv(io.BytesIO(resp.data))
    assert {"product_id", "sku", "action", "reason"}.issubset(set(exported.columns))
    assert len(exported.index) > 0


def test_export_segment_mix_csv_uses_bundle_mix_shift(client, monkeypatch):
    def fake_bundle(_page, _args):
        return {
            "charts": {
                "segments": {
                    "mix_shift": [
                        {"segment": "Stars", "revenue_current": 1200.0, "revenue_prior": 900.0, "share_current": 60.0, "share_prior": 45.0, "share_delta_pp": 15.0},
                        {"segment": "Long Tail", "revenue_current": 800.0, "revenue_prior": 1100.0, "share_current": 40.0, "share_prior": 55.0, "share_delta_pp": -15.0},
                    ]
                }
            }
        }

    monkeypatch.setattr("app.services.bundle_service.bundle", fake_bundle)
    resp = client.get("/products/export/segment_mix.csv")
    assert resp.status_code == 200
    exported = pd.read_csv(io.BytesIO(resp.data))
    assert len(exported.index) == 2
    assert "share_delta_pp" in exported.columns


def test_export_quadrant_csv_filters_by_quadrant(client, monkeypatch):
    rows = []
    for idx in range(30):
        rows.append(
            {
                "product_id": f"P{idx:03d}",
                "sku": f"SKU{idx:03d}",
                "product_name": f"Product {idx:03d}",
                "desc": f"Product {idx:03d}",
                "revenue": float(500 + idx * 30),
                "profit": float(80 + idx * 5),
                "margin_pct": float(10 + idx),
                "velocity_per_month": float(1 + (idx % 6)),
                "top_customer_share": float(20 + idx),
                "customer_hhi": float(500 + idx * 20),
            }
        )

    def fake_bundle(_page, args):
        page = int(args.get("page", 1))
        page_size = int(args.get("page_size") or args.get("per_page") or 25)
        start = (page - 1) * page_size
        end = start + page_size
        return {
            "table": {
                "rows": rows[start:end],
                "page": page,
                "page_size": page_size,
                "total": len(rows),
            }
        }

    monkeypatch.setattr("app.services.bundle_service.bundle", fake_bundle)
    resp_all = client.get("/products/export/quadrant.csv")
    assert resp_all.status_code == 200
    all_df = pd.read_csv(io.BytesIO(resp_all.data))
    resp_filtered = client.get("/products/export/quadrant.csv", query_string={"quadrant": "protect"})
    assert resp_filtered.status_code == 200
    filt_df = pd.read_csv(io.BytesIO(resp_filtered.data))
    assert len(all_df.index) >= len(filt_df.index)
    if len(filt_df.index):
        assert set(filt_df["quadrant"].astype(str).str.lower()) == {"protect"}

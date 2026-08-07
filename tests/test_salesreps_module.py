import pandas as pd
import pytest

from app.services import analytics_utils as au
from app.blueprints import salesreps


def test_sales_rep_column_detection():
    df = pd.DataFrame({
        "SalespersonId": ["A1", "A2"],
        "SalespersonName": ["Alex", "Bea"],
        "Revenue": [100, 200],
    })
    assert au.sales_rep_id_column(df) == "SalespersonId"
    assert au.sales_rep_name_column(df) == "SalespersonName"


def test_rep_rollup_basic_metrics():
    df = pd.DataFrame({
        "SalesRepId": ["R1", "R1", "R2"],
        "SalesRepName": ["Alex", "Alex", "Bea"],
        "Revenue": [100.0, 50.0, 200.0],
        "OrderId": ["O1", "O2", "O3"],
        "CustomerId": ["C1", "C2", "C3"],
        "QuantityShipped": [10, 5, 20],
        "ShipDate": pd.to_datetime(["2024-01-01", "2024-02-01", "2024-03-01"]),
    })
    table = salesreps._rep_rollup(df, include_costs=False)  # type: ignore
    alex = table[table["RepId"] == "R1"].iloc[0]
    assert alex["Revenue"] == 150.0
    assert alex["Orders"] == 2
    assert alex["Customers"] == 2
    assert alex["Units"] == 15


def test_kpis_margin_is_weighted():
    df = pd.DataFrame({
        "SalesRepId": ["R1", "R2"],
        "SalesRepName": ["Alex", "Bea"],
        "Revenue": [100.0, 200.0],
        "Cost": [70.0, 160.0],
    })
    table = salesreps._rep_rollup(df, include_costs=True)  # type: ignore
    table, meta = salesreps._finalize_rep_metrics(table, include_costs=True)  # type: ignore
    kpis = salesreps._build_kpis(table, include_costs=True, qty_label=meta["qty_label"])  # type: ignore
    assert kpis["profit"] == pytest.approx(70.0)
    # Weighted margin = total profit / total revenue, not average of per-rep margins
    assert kpis["margin_pct"] == pytest.approx((70.0 / 300.0) * 100.0)


def test_units_fall_back_to_weight_when_missing_qty():
    df = pd.DataFrame({
        "SalesRepId": ["R1", "R1"],
        "SalesRepName": ["Alex", "Alex"],
        "Revenue": [100.0, 50.0],
        "WeightLb": [10.0, 5.0],
        "ItemCount": [8, 7],
    })
    table = salesreps._rep_rollup(df, include_costs=False)  # type: ignore
    table, meta = salesreps._finalize_rep_metrics(table, include_costs=False)  # type: ignore
    assert meta["qty_label"] == "Units (ea)"
    assert table.iloc[0]["Units"] == pytest.approx(15.0)
    assert table.iloc[0]["ASP"] == pytest.approx(150.0 / 15.0)


def test_margin_number_matches_expected():
    df = pd.DataFrame({
        "SalesRepId": ["R1"],
        "SalesRepName": ["Alex"],
        "Revenue": [6028745.75],
        "Profit": [1504645.59],
    })
    table = salesreps._rep_rollup(df, include_costs=True)  # type: ignore
    table, meta = salesreps._finalize_rep_metrics(table, include_costs=True)  # type: ignore
    kpis = salesreps._build_kpis(table, include_costs=True, qty_label=meta["qty_label"])  # type: ignore
    assert kpis["margin_pct"] == pytest.approx((1504645.59 / 6028745.75) * 100.0, rel=1e-5)


def test_margin_zero_when_revenue_zero():
    df = pd.DataFrame({
        "SalesRepId": ["R1"],
        "SalesRepName": ["Alex"],
        "Revenue": [0.0],
        "Profit": [100.0],
    })
    table = salesreps._rep_rollup(df, include_costs=True)  # type: ignore
    table, meta = salesreps._finalize_rep_metrics(table, include_costs=True)  # type: ignore
    kpis = salesreps._build_kpis(table, include_costs=True, qty_label=meta["qty_label"])  # type: ignore
    assert kpis["margin_pct"] == 0.0


def test_salesreps_page_smoke(client, monkeypatch):
    client.application.config["LOGIN_DISABLED"] = True
    resp = client.get("/salesreps/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert ("Sales Reps Performance" in body) or ("Sales Reps Command Center" in body)
    assert "/api/salesreps/bundle" in body

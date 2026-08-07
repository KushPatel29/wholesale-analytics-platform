from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Dict

import pandas as pd
import pytest
from sqlalchemy.sql.elements import TextClause

from app.services import products as products_service


def _build_sample_dataframe() -> pd.DataFrame:
    """Return a deterministic dataset covering 12 months with controlled metrics."""
    rows = []
    start = pd.Timestamp("2023-10-01")
    skip_month = pd.Timestamp("2024-02-01")

    for idx in range(12):
        month_start = start + pd.DateOffset(months=idx)
        if month_start == skip_month:
            # Skip one month entirely to ensure the service pads trend with zeros.
            continue

        sale_date = month_start + pd.DateOffset(days=10)
        for product_code in ("A", "B"):
            if idx >= 9:  # Jul - Sep 2024
                revenue = 1200.0 if product_code == "A" else 2000.0
            elif idx >= 6:  # Apr - Jun 2024
                revenue = 800.0 if product_code == "A" else 1000.0
            else:  # Oct 2023 - Mar 2024
                revenue = 600.0 if product_code == "A" else 700.0

            cost_ratio = 0.7 if product_code == "A" else 0.45
            cost = revenue * cost_ratio
            qty = revenue / 12.0

            rows.append(
                {
                    "Date": sale_date,
                    "Revenue": revenue,
                    "Cost": cost,
                    "QuantityShipped": qty,
                    "ProductId": f"P_{product_code}",
                    "ProductName": f"Product {product_code}",
                    "SKU": f"SKU_{product_code}",
                    "SupplierName": f"Supplier {product_code}",
                    "UnitOfBillingId": "EA",
                    "RegionName": "East",
                    "CustomerId": "CUST1",
                    "CustomerName": "Customer One",
                }
            )

    return pd.DataFrame(rows)


@pytest.fixture
def overview_fixture(monkeypatch: pytest.MonkeyPatch) -> Dict[str, object]:
    sample_df = _build_sample_dataframe()
    fixed_now = pd.Timestamp("2024-10-15")

    monkeypatch.setattr(products_service, "_try_fetch_fact_from_sql", lambda filters: None)
    monkeypatch.setattr(products_service, "_load_fact", lambda filters, months_back=None: sample_df.copy())
    monkeypatch.setattr(
        products_service.pd.Timestamp,
        "utcnow",
        staticmethod(lambda: fixed_now),
    )

    cache = products_service.cache
    if hasattr(cache, "_store"):
        cache._store.clear()
    else:  # pragma: no cover - fallback for alternate cache implementations
        monkeypatch.setattr(cache, "get", lambda key: None)
        monkeypatch.setattr(cache, "set", lambda key, value, timeout=None: None)

    def caller(filters: dict | None = None):
        return products_service.get_products_overview(filters or {})

    return {"call": caller, "df": sample_df, "now": fixed_now}


def test_avg_price_and_weighted_margin(overview_fixture: Dict[str, object]) -> None:
    payload = overview_fixture["call"]()
    assert "as_of" in payload
    top_products = payload["top_products"]
    assert top_products, "top_products should not be empty"

    assert any(prod.get("avg_price") is not None for prod in top_products), "avg_price should be populated"

    df: pd.DataFrame = overview_fixture["df"]  # type: ignore[assignment]
    total_revenue = df["Revenue"].sum()
    total_margin = (df["Revenue"] - df["Cost"]).sum()
    expected_margin_pct = round(round(total_margin, 2) / total_revenue * 100.0, 2)
    assert payload["kpis"]["avg_margin_pct"] == pytest.approx(expected_margin_pct, abs=0.01)


def test_trend_has_12_complete_months(overview_fixture: Dict[str, object]) -> None:
    payload = overview_fixture["call"]()
    assert "as_of" in payload
    trend = payload["trend"]

    assert len(trend) == 12, "trend should always include 12 complete months"
    months = [entry["period"] for entry in trend]
    assert months[0] == "2023-10"
    assert months[-1] == "2024-09"

    feb_entry = next((entry for entry in trend if entry["period"] == "2024-02"), None)
    assert feb_entry is not None, "missing months should be padded with zeros"
    assert feb_entry["revenue"] == pytest.approx(0.0, abs=0.01)


def test_top_movers_delta(overview_fixture: Dict[str, object]) -> None:
    payload = overview_fixture["call"]()
    assert "as_of" in payload
    top_movers = payload["top_movers"]
    assert top_movers, "top_movers should contain entries"

    df: pd.DataFrame = overview_fixture["df"]  # type: ignore[assignment]
    df = df.copy()
    df["Month"] = pd.to_datetime(df["Date"]).dt.to_period("M")
    current_period = overview_fixture["now"].to_period("M")  # type: ignore[index]
    df = df[df["Month"] < current_period]

    current_window = pd.period_range("2024-07", "2024-09", freq="M")
    previous_window = pd.period_range("2024-04", "2024-06", freq="M")

    current_totals = (
        df[df["Month"].isin(current_window)]
        .groupby("ProductId", observed=True)["Revenue"]
        .sum()
    )
    previous_totals = (
        df[df["Month"].isin(previous_window)]
        .groupby("ProductId", observed=True)["Revenue"]
        .sum()
        .reindex(current_totals.index, fill_value=0.0)
    )
    deltas = current_totals - previous_totals

    expected_delta = float(deltas.loc["P_B"])
    lead_mover = top_movers[0]
    assert lead_mover["sku"] == "SKU_B"
    assert lead_mover["delta_rev"] == pytest.approx(expected_delta, abs=0.01)


def test_overview_handles_missing_normalize_datetime(monkeypatch: pytest.MonkeyPatch, overview_fixture: Dict[str, object]) -> None:
    # Simulate an environment where analytics_utils.normalize_datetime is unavailable (older deploy).
    monkeypatch.delattr(products_service.au, "normalize_datetime", raising=False)

    payload = overview_fixture["call"]()

    assert payload["kpis"]["total_revenue"] > 0
    assert len(payload["trend"]) == 12


def test_sql_fetch_uses_named_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: Dict[str, object] = {}

    class FakeConnection:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def exec_driver_sql(self, statement: str) -> None:
            self.statements.append(statement)

    class FakeEngine:
        def __init__(self) -> None:
            self.connection = FakeConnection()

        def begin(self):
            connection = self.connection

            class _Ctx:
                def __enter__(self_non):
                    return connection

                def __exit__(self_non, exc_type, exc, tb):
                    return False

            return _Ctx()

    fake_engine = FakeEngine()

    stub_loader = SimpleNamespace(
        get_config=lambda: object(),
        create_mssql_engine=lambda cfg: fake_engine,
    )
    monkeypatch.setitem(sys.modules, "data_loader", stub_loader)
    monkeypatch.setattr(products_service, "STATEMENT_TIMEOUT_MS", 0, raising=False) 
    
    # Ensure SQL fetching is not bypassed
    monkeypatch.setenv("PRODUCTS_DISABLE_SQL", "0")
    monkeypatch.setenv("PRODUCTS_FORCE_PARQUET", "0")

    def fake_read_sql(sql_obj, conn, params=None, **kwargs):
        captured["sql"] = sql_obj
        captured["params"] = params
        return pd.DataFrame(
            {
                "Date": pd.to_datetime(["2024-01-01"]),
                "Revenue": [100.0],
                "Cost": [60.0],
                "QtyNative": [10.0],
            }
        )

    monkeypatch.setattr(products_service.pd, "read_sql", fake_read_sql)

    filters = products_service.parse_filters({"products": ["SKU123"]})
    df = products_service._try_fetch_fact_from_sql(filters)

    assert isinstance(captured["sql"], TextClause)
    assert captured["params"]["product_0"] == "SKU123"
    assert set(captured["params"]) == {"product_0", "start_date", "end_date"}
    assert not df.empty

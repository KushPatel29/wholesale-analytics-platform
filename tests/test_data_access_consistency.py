import pandas as pd
import pytest
from types import SimpleNamespace

import data_loader
from app.services import data_access
from app.services import fact_store
from app.services import analytics_utils as au


@pytest.fixture
def sample_fact_df():
    return pd.DataFrame(
        {
            "OrderLineId": [1, 2, 3, 4],
            "OrderId": [100, 101, 102, 103],
            "Date": [
                pd.Timestamp("2018-01-05"),
                pd.Timestamp("2019-06-15"),
                pd.Timestamp("2024-03-01"),
                pd.Timestamp("2024-03-10"),
            ],
            "Revenue": [100.0, 200.0, 50.0, 75.0],
            "Cost": [60.0, 120.0, 20.0, 40.0],
            "SupplierName": [None, "Acme", "Acme", "Beta"],
            "CustomerName": ["CustA", "CustB", "CustB", None],
            "ProductId": ["P1", "P2", "P3", "P4"],
            "SupplierId": ["S1", "S2", "S2", None],
            "CustomerId": ["C1", "C2", "C2", "C3"],
            "OrderStatus": ["open", "shipped", "shipped", "open"],
        }
    )


def _patch_fact_sources(monkeypatch, df: pd.DataFrame) -> None:
    monkeypatch.setattr(data_loader, "get_dataframe_for_user", lambda **kwargs: df.copy())
    monkeypatch.setattr(fact_store, "get_sales_fact", lambda columns=None: df.copy())


def test_admin_scope_not_restricting_data(app, sample_fact_df, monkeypatch):
    _patch_fact_sources(monkeypatch, sample_fact_df)
    user = SimpleNamespace(role="admin", id=1, sales_rep_id=None)
    with app.app_context():
        ctx = data_access.get_fact_context(user=user, filters={"start": "2018-01-01", "end": "2025-01-01"})
        assert len(ctx.df) == len(sample_fact_df)
        assert ctx.meta.get("is_super_user") is True


def test_tabs_use_same_filters(app, sample_fact_df, monkeypatch):
    _patch_fact_sources(monkeypatch, sample_fact_df)
    user = SimpleNamespace(role="admin", id=1, sales_rep_id=None)
    with app.app_context():
        ctx_all = data_access.get_fact_context(user=user, filters={"start": "2018-01-01", "end": "2025-01-01"})
        ctx_recent = data_access.get_fact_context(user=user, filters={"start": "2024-01-01", "end": "2024-03-31"})
        assert ctx_all.meta["rows"] > ctx_recent.meta["rows"]
        assert ctx_all.meta["revenue_sum"] > ctx_recent.meta["revenue_sum"]


def test_tab_revenue_matches_base_within_tolerance(app, sample_fact_df, monkeypatch):
    _patch_fact_sources(monkeypatch, sample_fact_df)
    user = SimpleNamespace(role="admin", id=1, sales_rep_id=None)
    with app.app_context():
        ctx = data_access.get_fact_context(user=user, filters={"start": "2018-01-01", "end": "2025-01-01"})
        revenue = au.to_numeric_safe(ctx.df["Revenue"])
        supplier_total = float(revenue.groupby(ctx.df["SupplierName"], dropna=False).sum().sum())
        assert abs(ctx.meta["revenue_sum"] - supplier_total) < 0.01


def test_no_row_drops_on_missing_dims(app, sample_fact_df, monkeypatch):
    _patch_fact_sources(monkeypatch, sample_fact_df)
    user = SimpleNamespace(role="admin", id=1, sales_rep_id=None)
    with app.app_context():
        ctx = data_access.get_fact_context(user=user, filters={"start": "2018-01-01", "end": "2025-01-01"})
        assert ctx.meta["rows"] == len(sample_fact_df)
        assert "Unknown Supplier" in set(ctx.df.get("SupplierName", []))
        assert "Unknown Customer" in set(ctx.df.get("CustomerName", []))

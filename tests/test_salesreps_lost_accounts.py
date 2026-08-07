"""Tests for _salesrep_lost_accounts (Task 1B)."""
from __future__ import annotations

import pandas as pd
import pytest

from app.services.salesreps_bundle import _salesrep_lost_accounts


def _make_customers(*rows):
    """Helper: build a list of customer dicts."""
    return list(rows)


REF_DATE = pd.Timestamp("2025-08-31")


class TestSalesrepLostAccounts:

    def test_customer_with_prev_and_no_current_appears(self):
        """Customer with revenue_prev_30 > 0 and revenue_last_30 == 0 is a lost account."""
        customers = _make_customers(
            {
                "customer_id": "C001",
                "customer_name": "Acme Corp",
                "revenue_last_30": 0.0,
                "revenue_prev_30": 5000.0,
                "last_order_date": "2025-07-15",
            }
        )
        result = _salesrep_lost_accounts(customers, REF_DATE)
        assert len(result) == 1
        assert result[0]["customer_id"] == "C001"
        assert result[0]["customer_name"] == "Acme Corp"
        assert result[0]["revenue_prev_30"] == 5000.0

    def test_customer_with_current_revenue_does_not_appear(self):
        """Customer with revenue_last_30 > 0 should NOT appear in lost accounts."""
        customers = _make_customers(
            {
                "customer_id": "C002",
                "customer_name": "Globex",
                "revenue_last_30": 3000.0,
                "revenue_prev_30": 2000.0,
                "last_order_date": "2025-08-20",
            }
        )
        result = _salesrep_lost_accounts(customers, REF_DATE)
        assert len(result) == 0

    def test_customer_with_no_prev_revenue_does_not_appear(self):
        """Customer with revenue_prev_30 == 0 should NOT appear even if last_30 == 0."""
        customers = _make_customers(
            {
                "customer_id": "C003",
                "customer_name": "NewCo",
                "revenue_last_30": 0.0,
                "revenue_prev_30": 0.0,
                "last_order_date": "2025-06-01",
            }
        )
        result = _salesrep_lost_accounts(customers, REF_DATE)
        assert len(result) == 0

    def test_empty_customers_returns_empty(self):
        """Empty input returns empty list."""
        result = _salesrep_lost_accounts([], REF_DATE)
        assert result == []

    def test_no_qualifying_customers_returns_empty(self):
        """No lost accounts when all customers have current revenue."""
        customers = _make_customers(
            {"customer_id": "C004", "customer_name": "A", "revenue_last_30": 1000.0, "revenue_prev_30": 800.0, "last_order_date": "2025-08-10"},
            {"customer_id": "C005", "customer_name": "B", "revenue_last_30": 500.0, "revenue_prev_30": 0.0, "last_order_date": "2025-08-05"},
        )
        result = _salesrep_lost_accounts(customers, REF_DATE)
        assert result == []

    def test_sorted_by_revenue_prev_30_desc(self):
        """Results must be sorted by revenue_prev_30 descending."""
        customers = _make_customers(
            {"customer_id": "C010", "customer_name": "Low", "revenue_last_30": 0.0, "revenue_prev_30": 1000.0, "last_order_date": "2025-07-01"},
            {"customer_id": "C011", "customer_name": "High", "revenue_last_30": 0.0, "revenue_prev_30": 9000.0, "last_order_date": "2025-07-05"},
            {"customer_id": "C012", "customer_name": "Mid", "revenue_last_30": 0.0, "revenue_prev_30": 4500.0, "last_order_date": "2025-07-10"},
        )
        result = _salesrep_lost_accounts(customers, REF_DATE)
        assert len(result) == 3
        assert result[0]["customer_id"] == "C011"
        assert result[1]["customer_id"] == "C012"
        assert result[2]["customer_id"] == "C010"

    def test_max_20_results(self):
        """Returns at most 20 results."""
        customers = [
            {
                "customer_id": f"C{i:03d}",
                "customer_name": f"Customer {i}",
                "revenue_last_30": 0.0,
                "revenue_prev_30": float(i * 100),
                "last_order_date": "2025-07-01",
            }
            for i in range(1, 31)  # 30 qualifying customers
        ]
        result = _salesrep_lost_accounts(customers, REF_DATE)
        assert len(result) == 20

    def test_days_since_order_computed(self):
        """days_since_order is computed correctly from ref_date and last_order_date."""
        last_order = "2025-08-01"  # 30 days before REF_DATE (2025-08-31)
        customers = _make_customers(
            {
                "customer_id": "C100",
                "customer_name": "Test",
                "revenue_last_30": 0.0,
                "revenue_prev_30": 2000.0,
                "last_order_date": last_order,
            }
        )
        result = _salesrep_lost_accounts(customers, REF_DATE)
        assert len(result) == 1
        assert result[0]["days_since_order"] == 30

    def test_missing_last_order_date_gives_none(self):
        """days_since_order is None when last_order_date is missing."""
        customers = _make_customers(
            {
                "customer_id": "C200",
                "customer_name": "No Date",
                "revenue_last_30": 0.0,
                "revenue_prev_30": 1500.0,
                "last_order_date": None,
            }
        )
        result = _salesrep_lost_accounts(customers, REF_DATE)
        assert len(result) == 1
        assert result[0]["days_since_order"] is None

    def test_required_fields_present(self):
        """Each result row must contain all required fields."""
        customers = _make_customers(
            {
                "customer_id": "C300",
                "customer_name": "Field Check",
                "revenue_last_30": 0.0,
                "revenue_prev_30": 3000.0,
                "last_order_date": "2025-07-20",
            }
        )
        result = _salesrep_lost_accounts(customers, REF_DATE)
        assert len(result) == 1
        row = result[0]
        for key in ("customer_id", "customer_name", "revenue_prev_30", "last_order_date", "days_since_order"):
            assert key in row, f"Missing field: {key}"

    def test_no_profit_cost_margin_in_lost_accounts(self):
        """Lost accounts output must NOT contain profit, cost, or margin fields (masking safety)."""
        customers = _make_customers(
            {
                "customer_id": "C400",
                "customer_name": "Masked Co",
                "revenue_last_30": 0.0,
                "revenue_prev_30": 8000.0,
                "last_order_date": "2025-07-10",
                "profit": 2400.0,
                "cost": 5600.0,
                "margin_pct": 30.0,
            }
        )
        result = _salesrep_lost_accounts(customers, REF_DATE)
        assert len(result) == 1
        row = result[0]
        for forbidden in ("profit", "cost", "margin_pct", "cogs", "spend"):
            assert forbidden not in row, f"Sensitive field leaked: {forbidden}"

    def test_lost_accounts_key_present_in_drilldown_bundle(self, tmp_path, monkeypatch):
        """The rep drilldown bundle must contain a 'lost_accounts' key with a list value."""
        import pandas as pd
        from app.services import fact_store
        from app.services.salesreps_bundle import build_salesreps_drilldown

        rows = []
        for month in (1, 2):
            revenue = float(1200 + month * 50)
            cost = revenue * 0.70
            rows.append(
                {
                    "Date": f"2025-{month:02d}-10",
                    "DateExpected": f"2025-{month:02d}-10",
                    "SalesRepId": "R001",
                    "SalesRepName": "Rep 001",
                    "OrderId": f"O-R001-{month}",
                    "CustomerId": "C-001",
                    "CustomerName": "Customer 001",
                    "ProductId": "P-001",
                    "ProductName": "Product 001",
                    "OrderStatus": "packed",
                    "Revenue": revenue,
                    "Cost": cost,
                    "QuantityOrdered": 11,
                    "WeightLb": 31.0,
                    "UnitOfBillingId": 1,
                    "pack_item_count_sum": 11.0,
                    "pack_weight_lb_sum": 31.0,
                    "pack_count": 1,
                    "Price": revenue,
                    "CostPrice": cost,
                }
            )

        df = pd.DataFrame(rows)
        parquet_path = tmp_path / "fact_salesreps_v2.parquet"
        df.to_parquet(parquet_path)
        monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
        monkeypatch.delenv("CUSTOMER_REP_HISTORY_PATH", raising=False)
        monkeypatch.delenv("TERRITORY_REP_HISTORY_PATH", raising=False)
        monkeypatch.delenv("CUSTOMER_TERRITORY_HISTORY_PATH", raising=False)
        monkeypatch.delenv("SALESREP_SUCCESSION_PATH", raising=False)
        fact_store.reset_duckdb_state()
        fact_store.init_views()

        class _FakeFilters:
            start = pd.Timestamp("2025-01-01")
            end = pd.Timestamp("2025-12-31")
            date_start = "2025-01-01"
            date_end = "2025-12-31"
            def get(self, k, d=None): return d

        scope = {"is_admin": True, "scope_mode": "all", "allowed_erp_user_ids": [], "sales_rep_ids": []}

        class _FakeArgs:
            def get(self, k, d=None): return d

        bundle = build_salesreps_drilldown("R001", _FakeFilters(), scope, _FakeArgs())
        assert "lost_accounts" in bundle, "Drilldown bundle must contain 'lost_accounts' key"
        assert isinstance(bundle["lost_accounts"], list)

        fact_store.reset_duckdb_state()

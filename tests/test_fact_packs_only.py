from pathlib import Path

import pandas as pd
import pytest

from app.services import fact_store


@pytest.fixture
def seed_fact(tmp_path, monkeypatch):
    def _seed(rows):
        df = pd.DataFrame(rows)
        parquet_path = tmp_path / "fact_packs_only.parquet"
        df.to_parquet(parquet_path)
        monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
        fact_store.reset_duckdb_state()
        fact_store.init_views()
        return parquet_path

    yield _seed
    fact_store.reset_duckdb_state()


def test_pack_revenue_uses_weight_or_units(seed_fact):
    seed_fact(
        [
            {
                "OrderLineId": "OL-1",
                "OrderId": "O-1",
                "DateExpected": "2025-01-10",
                "OrderStatus": "packed",
                "UnitOfBillingId": 3,
                "pack_weight_lb_sum": 10.0,
                "pack_item_count_sum": 5.0,
                "pack_count": 1,
                "Price": 2.5,
                "CostPrice": 1.5,
            },
            {
                "OrderLineId": "OL-2",
                "OrderId": "O-2",
                "DateExpected": "2025-01-10",
                "OrderStatus": "packed",
                "UnitOfBillingId": 1,
                "pack_weight_lb_sum": 8.0,
                "pack_item_count_sum": 6.0,
                "pack_count": 1,
                "Price": 4.0,
                "CostPrice": 3.0,
            },
        ]
    )

    df = fact_store.execute_sql_df(
        """
        SELECT OrderLineId, revenue_packs_only, cost_packs_only
        FROM fact
        ORDER BY OrderLineId
        """,
        [],
        tag="packs_only_uom",
    )
    row1 = df.iloc[0]
    row2 = df.iloc[1]
    assert row1["revenue_packs_only"] == pytest.approx(25.0, abs=0.0001)
    assert row1["cost_packs_only"] == pytest.approx(15.0, abs=0.0001)
    assert row2["revenue_packs_only"] == pytest.approx(24.0, abs=0.0001)
    assert row2["cost_packs_only"] == pytest.approx(18.0, abs=0.0001)


def test_missing_packs_nulls_excluded_from_sums(seed_fact):
    seed_fact(
        [
            {
                "OrderLineId": "OL-1",
                "OrderId": "O-1",
                "DateExpected": "2025-01-05",
                "OrderStatus": "packed",
                "UnitOfBillingId": 1,
                "pack_weight_lb_sum": 0.0,
                "pack_item_count_sum": 10.0,
                "pack_count": 2,
                "Price": 5.0,
                "CostPrice": 3.0,
            },
            {
                "OrderLineId": "OL-2",
                "OrderId": "O-2",
                "DateExpected": "2025-01-05",
                "OrderStatus": "packed",
                "UnitOfBillingId": 1,
                "pack_weight_lb_sum": None,
                "pack_item_count_sum": None,
                "pack_count": None,
                "Price": 5.0,
                "CostPrice": 3.0,
            },
        ]
    )

    rows = fact_store.execute_sql_df(
        """
        SELECT OrderLineId, missing_packs, revenue_packs_only, cost_packs_only
        FROM fact
        ORDER BY OrderLineId
        """,
        [],
        tag="packs_only_missing_rows",
    )
    missing_row = rows.iloc[1]
    assert bool(missing_row["missing_packs"]) is True
    assert pd.isna(missing_row["revenue_packs_only"])
    assert pd.isna(missing_row["cost_packs_only"])

    totals = fact_store.execute_sql_df(
        """
        SELECT SUM(revenue_packs_only) AS revenue, SUM(cost_packs_only) AS cost
        FROM fact
        """,
        [],
        tag="packs_only_missing_sums",
    ).iloc[0]
    assert totals["revenue"] == pytest.approx(50.0, abs=0.0001)
    assert totals["cost"] == pytest.approx(30.0, abs=0.0001)


def test_default_status_filter_applies(seed_fact):
    seed_fact(
        [
            {
                "OrderLineId": "OL-1",
                "OrderId": "O-1",
                "DateExpected": "2025-01-15",
                "OrderStatus": "packed",
                "UnitOfBillingId": 1,
                "pack_item_count_sum": 5.0,
                "pack_weight_lb_sum": 0.0,
                "pack_count": 1,
                "Price": 10.0,
                "CostPrice": 7.0,
            },
            {
                "OrderLineId": "OL-2",
                "OrderId": "O-2",
                "DateExpected": "2025-01-15",
                "OrderStatus": "cancelled",
                "UnitOfBillingId": 1,
                "pack_item_count_sum": 5.0,
                "pack_weight_lb_sum": 0.0,
                "pack_count": 1,
                "Price": 10.0,
                "CostPrice": 7.0,
            },
        ]
    )

    df = fact_store.query_fact(filters={}, apply_default_window=False, use_cache=False)
    assert len(df.index) == 1
    assert df["OrderStatus"].iloc[0] == "packed"


def test_regression_packs_only_totals_match_sql():
    base = fact_store.FACT_PATH
    if not base.exists():
        pytest.skip("Fact parquet not available; skipping packs-only regression.")
    if base.is_dir():
        if not any(Path(base).rglob("*.parquet")):
            pytest.skip("No parquet files under FACT_PATH; skipping packs-only regression.")

    try:
        fact_store.reset_duckdb_state()
        df = fact_store.query_fact(
            filters={
                "start": "2025-01-01",
                "end": "2026-01-01T12:00:00",
            },
            use_cache=False,
        )
    except Exception as exc:
        pytest.skip(f"Fact dataset unavailable for regression: {exc}")

    revenue = float(pd.to_numeric(df.get("Revenue"), errors="coerce").sum())
    cost = float(pd.to_numeric(df.get("Cost"), errors="coerce").sum())

    cols = fact_store.list_columns()
    where_sql, params, _, _ = fact_store.build_where_clause(
        fact_store._normalize_filters_obj(
            {
                "start": "2025-01-01",
                "end": "2026-01-01T12:00:00",
            }
        ),
        cols,
        scope=None,
        apply_default_window=True,
    )
    expected = fact_store.execute_sql_df(
        f"SELECT SUM(Revenue) AS revenue, SUM(Cost) AS cost FROM fact WHERE {where_sql}",
        params,
        tag="packs_only_regression",
    )
    exp_rev = float(pd.to_numeric(expected.get("revenue"), errors="coerce").sum())
    exp_cost = float(pd.to_numeric(expected.get("cost"), errors="coerce").sum())

    assert revenue == pytest.approx(exp_rev, abs=0.01)
    assert cost == pytest.approx(exp_cost, abs=0.01)

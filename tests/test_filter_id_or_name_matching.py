"""
A dimension filter must match whether it carries an id or a display name.

The region drilldown links to `?region_id=Mid-Atlantic` - a region *name* in a
parameter called `region_id`. `parse_filters` collects that into
`filters.regions`, and the where-clause builder used to resolve the dimension
to a single column via `_choose_column`, which returns the first candidate
present. Ids are listed first, so it built `RegionId IN ('Mid-Atlantic')`
against a column holding `RG04`, matched zero rows, and returned an empty
payload with no error.

Every chart on all seven region drilldowns rendered blank because of it, and
because the static build freezes whatever the page actually drew, the blanks
were then baked into the published site.

The existing region fixtures never caught this: they define `RegionName` with
no `RegionId`, so the single-column resolution happened to pick the name
column. These fixtures deliberately define both.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.services import fact_store
from app.services.filters import parse_filters
from werkzeug.datastructures import MultiDict


def _row(order_id: str, region_id: str, region_name: str, customer_id: str, customer_name: str) -> dict:
    return {
        "Date": "2025-03-05",
        "DateExpected": "2025-03-05",
        "RegionId": region_id,
        "RegionName": region_name,
        "OrderId": order_id,
        "CustomerId": customer_id,
        "CustomerName": customer_name,
        "ProductId": "P1",
        "ProductName": "Prime Rib",
        "SupplierId": "SUP-1",
        "SupplierName": "Supplier One",
        "ShippingMethodName": "Ground",
        "OrderStatus": "packed",
        "Revenue": 100.0,
        "Cost": 60.0,
        "QuantityShipped": 2.0,
        "WeightLb": 8.0,
        "Price": 100.0,
        "CostPrice": 60.0,
    }


@pytest.fixture
def seed_id_and_name(tmp_path, monkeypatch):
    frame = pd.DataFrame(
        [
            _row("O-1", "RG04", "Mid-Atlantic", "C1", "Acme Foods"),
            _row("O-2", "RG04", "Mid-Atlantic", "C2", "Brook Market"),
            _row("O-3", "RG02", "Southeast", "C3", "Cedar Grocers"),
        ]
    )
    path = tmp_path / "fact_id_or_name.parquet"
    frame.to_parquet(path)
    monkeypatch.setenv("PARQUET_PATH", str(path))
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield path
    fact_store.reset_duckdb_state()


def _scope_admin() -> dict:
    # A `None` scope builds a deny-all clause, which would make every case here
    # pass for the wrong reason.
    return {
        "is_admin": True,
        "scope_mode": "all",
        "allowed_erp_user_ids": [],
        "sales_rep_ids": [],
        "allowed_count": 0,
        "scope_hash": "id-or-name-admin",
        "permissions_version": "1",
        "user_id": 1,
        "role": "admin",
    }


def _matched_rows(arg_key: str, value: str) -> int:
    # An explicit window: `parse_filters` otherwise applies the `current_fy`
    # preset, whose dates are derived from the real dataset and exclude these
    # fixture rows entirely - which would fail the test for the wrong reason.
    args = MultiDict({arg_key: value, "start": "2025-01-01", "end": "2025-12-31"})
    filters = parse_filters(args)
    cols = fact_store.list_columns()
    where_sql, params, _start, _end = fact_store.build_where_clause(
        filters, cols, _scope_admin(), apply_default_window=False
    )
    # Count rows rather than summing Revenue: the DuckDB fact view derives its
    # own measure columns, so a literal Revenue in the fixture does not survive
    # into the view and every sum would read 0.
    frame = fact_store.execute_sql_df(
        f"SELECT COUNT(*) AS matched FROM fact WHERE {where_sql}",
        params,
        tag="test.id_or_name",
    )
    return int(frame.iloc[0]["matched"])


@pytest.mark.parametrize(
    "arg_key, value",
    [
        ("region_id", "Mid-Atlantic"),  # the drilldown's own link shape
        ("regions", "Mid-Atlantic"),
        ("region_id", "RG04"),
        ("regions", "RG04"),
    ],
)
def test_region_filter_matches_by_id_or_name(seed_id_and_name, arg_key, value):
    assert _matched_rows(arg_key, value) == 2


def test_customer_filter_matches_by_id_or_name(seed_id_and_name):
    assert _matched_rows("customers", "C1") == 1
    assert _matched_rows("customers", "Acme Foods") == 1


def test_unknown_value_still_matches_nothing(seed_id_and_name):
    """OR-ing the columns of one dimension must not widen it into another."""
    assert _matched_rows("regions", "Nowhere") == 0
    # A customer name must not select rows through the region columns.
    assert _matched_rows("regions", "Acme Foods") == 0

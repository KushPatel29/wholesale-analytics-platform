import pandas as pd
import pytest
from flask import Flask

from app.services.filters import FilterParams, apply_filters
from app.services import overview_query
from app.blueprints import overview as overview_api


@pytest.fixture
def sample_df():
    data = {
        "Date": pd.to_datetime([
            "2023-01-05",
            "2023-01-15",
            "2023-02-20",
            "2023-02-25",
            "2023-03-05",
            "2023-03-15",
        ]),
        "Revenue": [120.0, 80.0, 90.0, 60.0, 110.0, 70.0],
        "OrderId": [
            "O-001",
            "O-002",
            "O-003",
            "O-004",
            "O-005",
            "O-006",
        ],
        "CustomerId": ["C1", "C2", "C1", "C3", "C2", "C4"],
        "CustomerName": ["Alice", "Bob", "Alice", "Cara", "Bob", "Dan"],
        "RegionName": ["North", "South", "North", "East", "South", "West"],
        "ProductName": [
            "Widget A",
            "Widget B",
            "Widget B",
            "Widget C",
            "Widget A",
            "Widget D",
        ],
        "SupplierName": [
            "Supplier X",
            "Supplier Y",
            "Supplier X",
            "Supplier Z",
            "Supplier X",
            "Supplier Z",
        ],
        "SalesRepName": [
            "Rep A",
            "Rep B",
            "Rep A",
            "Rep C",
            "Rep B",
            "Rep D",
        ],
        "SalesRepId": [
            "SR1",
            "SR2",
            "SR1",
            "SR3",
            "SR2",
            "SR4",
        ],
        "ShippingMethodName": ["Air", "Ground", "Air", "Ground", "Air", "Ground"],
        "unit_cost_effective": [5, 4, 5, 0, 7, 10],
        "QuantityShipped": [5, 3, 4, 2, 6, 1],
        "CostPrice": [50, 45, 55, 40, 60, 65],
    }
    return pd.DataFrame(data)


def test_filters_date_range(sample_df):
    params = FilterParams(
        start=pd.Timestamp("2023-02-01"),
        end=pd.Timestamp("2023-02-28"),
        regions=tuple(),
        methods=tuple(),
        customers=tuple(),
    )
    filtered = apply_filters(sample_df.copy(), params)
    assert not filtered.empty
    assert filtered["Date"].min() >= params.start
    assert filtered["Date"].max() <= params.end
    assert filtered["Date"].dt.month.unique().tolist() == [2]


def test_filter_options_include_sales_reps(sample_df):
    options = overview_query.build_filter_options(sample_df.copy())
    assert "sales_reps" in options
    assert "Rep A" in options["sales_reps"]
    assert "SR1" in options["sales_reps"]


def test_cards_numbers_match_manual(sample_df):
    result = overview_query.compute_overview(sample_df.copy())
    kpis = result["kpis"]
    assert kpis["total_customers"] == sample_df["CustomerId"].nunique()
    assert kpis["total_revenue"] == pytest.approx(sample_df["Revenue"].sum())
    assert kpis["total_orders"] == sample_df["OrderId"].nunique()
    expected_aov = round(sample_df["Revenue"].sum() / sample_df["OrderId"].nunique(), 2)
    assert kpis["aov"] == pytest.approx(expected_aov)
    assert kpis["churn_rate"] == pytest.approx(0.0)

    weekday = result["weekday"]
    assert weekday["labels"] == [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ]
    assert pytest.approx(sum(weekday["values"])) == sample_df["Revenue"].sum()

    freq = result["ordfreq"]
    per_customer = sample_df.groupby("CustomerId")["OrderId"].nunique()
    expected_counts = [
        int((per_customer == 1).sum()),
        int((per_customer == 2).sum()),
        int((per_customer == 3).sum()),
        int((per_customer == 4).sum()),
        int((per_customer >= 5).sum()),
    ]
    assert freq["counts"] == expected_counts


def test_series_monotonic_periods(sample_df, monkeypatch):
    monthly = overview_query._monthly(sample_df.copy())
    assert monthly["months"] == sorted(monthly["months"])
    assert len(monthly["months"]) == len(set(monthly["months"]))

    # monkeypatch.setattr(overview_api.loader, "get_fact_df", lambda columns=None: sample_df.copy(), raising=True)

    # filters = FilterParams(start=None, end=None, regions=tuple(), methods=tuple(), customers=tuple())
    # app = Flask("test-stacked")
    # with app.app_context():
    #     payload = overview_api._compute_stacked_payload(
    #         filters,
    #         "region_customer",
    #         "revenue",
    #         "M",
    #         1,
    #         "test-version",
    #     )
    # assert payload is not None
    # assert payload["meta"]["top_n"] == 1
    # assert payload["meta"]["kind"] == "region_customer"
    # assert payload["meta"]["version"] == "test-version"
    # assert payload["meta"]["note"] == overview_api.STACKED_NOTE
    # series = payload["series"]
    # assert isinstance(series, list)
    # assert len(series) <= 2
    # names = {item["name"] for item in series}
    # if len(names) > 1:
    #     assert "Other" in names

    # signature = (
    #     pd.Timestamp("2024-01-01").isoformat(),
    #     pd.Timestamp("2024-01-31").isoformat(),
    #     tuple(),
    #     tuple(),
    #     tuple(),
    # )
    # app = Flask("test-series")
    # with app.app_context():
    #     series_payload = overview_api._memoized_series.__wrapped__(
    #         None, signature, "revenue", "month"
    #     )
    # assert series_payload == []

    # filters = FilterParams(
    #     start=pd.Timestamp("2024-01-01"),
    #     end=pd.Timestamp("2024-01-31"),
    #     regions=tuple(),
    #     methods=tuple(),
    #     customers=tuple(),
    # )
    # app = Flask("test-empty")
    # with app.app_context():
    #     payload = overview_api._compute_stacked_payload(
    #         filters,
    #         "region_customer",
    #         "revenue",
    #         "M",
    #         5,
    #         "test-version",
    #     )
    # assert payload is None

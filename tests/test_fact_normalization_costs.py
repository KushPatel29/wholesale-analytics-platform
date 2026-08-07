import pandas as pd
import pytest

from app.services import analytics_utils as au


def test_normalize_fact_df_coalesces_cost_sources():
    raw = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2024-01-01", "2024-01-02"]),
            "Revenue": [100.0, 200.0],
            "QuantityShipped": [10.0, 5.0],
            "Cost": [0.0, None],
            "CostPrice_x": [2.0, 3.0],
            "cost_ordered": [0.0, 12.0],
        }
    )

    normalized = au.normalize_fact_df(raw)
    costs = normalized["Cost"]

    assert costs.iat[0] == pytest.approx(20.0)
    assert costs.iat[1] == pytest.approx(12.0)


def test_normalize_fact_df_weight_cost_fallback():
    raw = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2024-02-01"]),
            "Revenue": [100.0],
            "QuantityShipped": [0.0],
            "WeightLb": [25.0],
            "CostPerLb": [2.5],
        }
    )

    normalized = au.normalize_fact_df(raw)
    assert normalized["Cost"].iat[0] == pytest.approx(62.5)


def test_normalize_fact_df_cost_prefers_itemcount_when_qty_zero():
    raw = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2024-02-01"]),
            "Revenue": [100.0],
            "QuantityShipped": [0.0],
            "ItemCount": [2.0],
            "WeightLb": [10.0],
            "UnitOfBillingId": ["1"],
            "CostPrice_x": [3.0],
        }
    )

    normalized = au.normalize_fact_df(raw)
    assert normalized["Cost"].iat[0] == pytest.approx(6.0)

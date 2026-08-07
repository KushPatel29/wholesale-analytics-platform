import pandas as pd
import pytest

from app.blueprints import suppliers as sup


def test_product_details_metrics_with_units_and_cost(monkeypatch):
    df = pd.DataFrame(
        {
            "SupplierId": [1, 1],
            "ProductId": [10, 10],
            "ProductName": ["Widget", "Widget"],
            "ExtPrice": [100.0, 50.0],
            "ExtCost": [60.0, 20.0],
            "Qty": [10, 5],
            "WeightLb": [2.0, 1.0],
            "OrderId": [1, 2],
            "CustomerId": ["A", "B"],
        }
    )

    monkeypatch.setattr(
        sup,
        "_get_frame",
        lambda: {
            "df": df,
            "rev_col": "ExtPrice",
            "cost_col": "ExtCost",
            "units_col": "Qty",
            "weight_col": "WeightLb",
            "order_id_col": "OrderId",
            "customer_id_col": "CustomerId",
        },
    )
    monkeypatch.setattr(sup, "can_view_costs", lambda user=None: True)

    details = sup._build_supplier_product_details("1")
    assert not details.empty
    row = details.iloc[0]
    assert row["Revenue"] == 150
    assert row["Units"] == 15
    assert row["WeightLb"] == 3
    assert row["Orders"] == 2
    assert row["Customers"] == 2
    assert row["AvgSalePrice"] == 10
    assert row["AvgSalePricePerLb"] == 50
    assert row["Profit"] == 70
    assert row["Margin%"] == pytest.approx(46.67)
    assert row["ROI%"] == pytest.approx(87.5)
    assert row["AvgCostPerUnit"] == pytest.approx(5.33)
    assert row["AvgCostPerLb"] == pytest.approx(26.67)
    assert row["ProfitPerUnit"] == pytest.approx(4.67)
    assert row["ProfitPerLb"] == pytest.approx(23.33)


def test_product_details_handles_missing_units(monkeypatch):
    df = pd.DataFrame(
        {
            "SupplierId": [2],
            "ProductId": [20],
            "ProductName": ["Thing"],
            "ExtPrice": [200.0],
            "ExtCost": [150.0],
            "WeightLb": [10.0],
            "OrderId": [5],
            "CustomerId": ["C1"],
        }
    )

    monkeypatch.setattr(
        sup,
        "_get_frame",
        lambda: {
            "df": df,
            "rev_col": "ExtPrice",
            "cost_col": "ExtCost",
            "units_col": None,
            "weight_col": "WeightLb",
            "order_id_col": "OrderId",
            "customer_id_col": "CustomerId",
        },
    )
    monkeypatch.setattr(sup, "can_view_costs", lambda user=None: True)

    details = sup._build_supplier_product_details("2")
    assert not details.empty
    row = details.iloc[0]
    assert pd.isna(row["Units"])
    assert pd.isna(row["AvgSalePrice"])
    assert pd.isna(row["AvgCostPerUnit"])
    assert pd.isna(row["ProfitPerUnit"])
    assert row["AvgSalePricePerLb"] == 20
    assert row["AvgCostPerLb"] == 15
    assert row["ProfitPerLb"] == 5
    assert row["Margin%"] == pytest.approx(25.0)
    assert row["ROI%"] == pytest.approx(33.33, rel=1e-3)

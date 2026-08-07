import pandas as pd

from app.blueprints import suppliers


def test_top_products_worksheet_uses_human_readable_names(monkeypatch):
    sample_df = pd.DataFrame(
        [
            {
                "SupplierId": 42,
                "ProductId": 101,
                "ProductName": "101",
                "SkuName": None,
                "Revenue": 1200.0,
                "Cost": 600.0,
                "OrderId": "O-1",
                "CustomerId": "C-1",
                "QuantityShipped": 10,
                "WeightLb": 50.0,
            },
            {
                "SupplierId": 42,
                "ProductId": 102,
                "ProductName": "102",
                "SkuName": None,
                "Revenue": 900.0,
                "Cost": 300.0,
                "OrderId": "O-2",
                "CustomerId": "C-2",
                "QuantityShipped": 8,
                "WeightLb": 32.0,
            },
            {
                "SupplierId": 42,
                "ProductId": 103,
                "ProductName": "103",
                "SkuName": None,
                "Revenue": 100.0,
                "Cost": 50.0,
                "OrderId": "O-3",
                "CustomerId": "C-3",
                "QuantityShipped": 2,
                "WeightLb": 5.0,
            },
            {
                "SupplierId": 42,
                "ProductId": 101,
                "ProductName": "101",
                "SkuName": None,
                "Revenue": 300.0,
                "Cost": 150.0,
                "OrderId": "O-4",
                "CustomerId": "C-1",
                "QuantityShipped": 5,
                "WeightLb": 25.0,
            },
        ]
    )

    suppliers._get_sku_map.cache_clear()

    monkeypatch.setattr(
        suppliers,
        "_get_frame",
        lambda: {"df": sample_df.copy(), "rev_col": "Revenue", "cost_col": "Cost"},
    )
    monkeypatch.setattr(suppliers, "can_view_costs", lambda _=None: False)

    sku_map = pd.Series(
        data=["Beef Grind Lean Grass Fed", "Atlantic Salmon Loin"],
        index=pd.Index([101, 102], name="ProductId"),
        dtype=object,
    )
    monkeypatch.setattr(suppliers, "_get_sku_map", lambda: sku_map)

    details = suppliers._build_supplier_product_details("42")
    assert not details.empty
    expected_columns = [
        "ProductName",
        "Revenue",
        "Orders",
        "Customers",
        "Units",
        "WeightLb",
        "Cost",
        "AvgSalePrice",
        "Profit",
        "Margin%",
        "ROI%",
        "AvgCostPerLb",
        "AvgSalePricePerLb",
        "AvgCostPerUnit",
        "ProfitPerUnit",
        "ProfitPerLb",
    ]
    assert list(details.columns) == expected_columns
    assert details["ProductName"].dtype == object
    assert "Beef" in str(details.iloc[0]["ProductName"])
    assert "Atlantic Salmon" in str(details.iloc[1]["ProductName"])
    assert details.iloc[2]["ProductName"].startswith("SKU 103")

    summary = suppliers._build_top_products_summary_frame(details)
    assert list(summary.columns)[0] == "ProductName"
    assert summary["ProductName"].dtype == object
    assert any(any(ch.isalpha() for ch in str(val)) for val in summary["ProductName"])
    assert "Beef" in str(summary.iloc[0]["ProductName"])

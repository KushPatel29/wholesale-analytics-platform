from __future__ import annotations

import pandas as pd

import data_loader


def test_canonicalize_columns_preserves_text_protein_dimension():
    frame = pd.DataFrame(
        {
            "Date": ["2025-01-01"],
            "ProductId": ["SKU-001"],
            "ProductName": ["Prime Ribeye"],
            "CustomerId": ["C-1"],
            "CustomerName": ["Chef A"],
            "OrderId": ["O-1"],
            "Revenue": [150.0],
            "Cost": [90.0],
            "WeightLb": [25.0],
            "QuantityShipped": [10.0],
            "Protein": ["Beef"],
        }
    )

    out = data_loader.canonicalize_columns(frame)

    assert str(out.loc[0, "Protein"]) == "Beef"
    assert str(out.loc[0, "ProteinType"]) == "Beef"
    assert str(out.loc[0, "ProteinName"]) == "Beef"
    assert str(out.loc[0, "Category"]) == "Beef"
    assert str(out.loc[0, "ProductCategory"]) == "Beef"


def test_finalize_dataframe_keeps_product_family_columns_as_strings():
    frame = pd.DataFrame(
        {
            "Date": ["2025-01-01"],
            "DateExpected": ["2025-01-01"],
            "ProductId": ["SKU-001"],
            "ProductName": ["Prime Ribeye"],
            "CustomerId": ["C-1"],
            "CustomerName": ["Chef A"],
            "OrderId": ["O-1"],
            "Revenue": [150.0],
            "Cost": [90.0],
            "Profit": [60.0],
            "WeightLb": [25.0],
            "QuantityShipped": [10.0],
            "Price": [15.0],
            "CostPrice": [9.0],
            "Protein": ["Beef"],
            "Category": ["Steak"],
        }
    )

    out = data_loader.finalize_dataframe(frame, best_effort=True)

    assert str(out.loc[0, "Protein"]) == "Beef"
    assert str(out.loc[0, "ProteinType"]) == "Beef"
    assert str(out.loc[0, "ProteinName"]) == "Beef"
    assert str(out.loc[0, "Category"]) == "Steak"
    assert str(out.loc[0, "ProductCategory"]) == "Steak"

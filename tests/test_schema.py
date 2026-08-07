import os
import pytest


def _has_mssql_env() -> bool:
    return bool(os.getenv("PYTEST_ENABLE_MSSQL") and os.getenv("MSSQL_SERVER"))


@pytest.mark.skipif(not _has_mssql_env(), reason="MSSQL not configured; skipping schema smoke test")
def test_min_columns_exist():
    from data_loader import get_dataframe  # import locally to avoid import at collection

    df = get_dataframe(start=os.getenv("DATA_START_DATE"), end=os.getenv("DATA_END_DATE"))
    required = {
        "Date",
        "ShipDate",
        "OrderId",
        "CustomerId",
        "CustomerName",
        "RegionName",
        "QuantityOrdered",
        "QuantityShipped",
        "Revenue",
        "revenue_shipped",
        "revenue_ordered",
        "Cost",
        "cost_shipped",
        "cost_ordered",
        "Profit",
        "gross_margin_shipped",
        "gross_margin_ordered",
        "unit_cost_effective",
        "Price",
        "BasePrice",
        "ListPrice",
        "CostPrice_orderline",
        "CostPrice_product",
        "CostPrice_po",
        "WeightLb",
        "ItemCount",
        "ProductName",
        "SKU",
        "SkuName",
        "SupplierName",
        "ShippingMethodName",
        "ShippingMethodLabel",
        "ShipperName",
        "Carrier",
    }
    missing = [c for c in required if c not in df.columns]
    assert not missing, f"Missing columns: {missing}"

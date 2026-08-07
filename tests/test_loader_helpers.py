import pandas as pd

from data_loader import _merge_region_dimension, _resolve_shipping


def test_resolve_shipping_labels():
    fact = pd.DataFrame(
        {
            "ShippingMethodRequested": ["2", "UPS"],
            "ShipperId": [None, None],
        }
    )
    ship_methods = pd.DataFrame(
        {
            "ShippingMethodId": [2],
            "ShipperId": [1],
            "ShippingMethodName": ["Ground"],
        }
    )
    shippers = pd.DataFrame({"ShipperId": [1], "ShipperName": ["UPS"]})

    out = _resolve_shipping(fact, ship_methods, shippers)
    assert "ShippingMethodLabel" in out.columns
    assert out.loc[0, "ShippingMethodLabel"] == "Ground"
    assert out.loc[1, "ShippingMethodLabel"] == "UPS"


def test_merge_region_dimension_handles_mixed_dtypes():
    fact = pd.DataFrame(
        {
            "OrderLineId": [1, 2],
            "RegionId": pd.Series([1, None], dtype="Int64"),
        }
    )
    regions = pd.DataFrame(
        {
            "RegionId": ["1", "2"],
            "RegionName": ["North", "South"],
        }
    )

    merged = _merge_region_dimension(fact, regions)
    matched = merged.loc[merged["OrderLineId"] == 1, "RegionName"].iloc[0]
    assert matched == "North"
    assert merged.loc[merged["OrderLineId"] == 2, "RegionName"].isna().all()
    assert str(merged["RegionId"].dtype) in {"string", "string[python]", "object"}

import pandas as pd
import pytest

from app.services import analytics_utils as au


def test_supplier_name_prefers_named_column():
    df = pd.DataFrame({"SupplierName": ["Acme"], "SupplierId": [1]})
    assert au.supplier_name_column(df) == "SupplierName"


def test_supplier_name_handles_vendor_with_space():
    df = pd.DataFrame({"Vendor Name": ["Global Foods"], "Other": [1]})
    assert au.supplier_name_column(df) == "Vendor Name"


def test_supplier_name_falls_back_to_id():
    df = pd.DataFrame({"supplier_id": [101], "Value": [10.0]})
    assert au.supplier_name_column(df) == "supplier_id"


def test_supplier_name_returns_none_when_missing():
    df = pd.DataFrame({"foo": [1], "bar": [2]})
    assert au.supplier_name_column(df) is None


def test_units_and_weight_detection_prefers_units():
    df = pd.DataFrame({
        "WeightLb": [1.0, 2.0],
        "UnitsShipped": [3, 4],
    })
    assert au.units_column(df) == "UnitsShipped"
    assert au.weight_lb_column(df) == "WeightLb"


def test_cost_and_revenue_candidates_detected():
    df = pd.DataFrame({"ExtendedCost": [1.2], "ExtRevenue": [3.4]})
    assert au.cost_column(df) == "ExtendedCost"
    assert au.revenue_column(df, required=True) == "ExtRevenue"


def test_revenue_required_raises_when_missing():
    df = pd.DataFrame({"foo": [1]})
    with pytest.raises(ValueError):
        au.revenue_column(df, required=True)

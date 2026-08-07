import pandas as pd

from app.services.filters import FilterParams, apply_filters, parse_filters


def test_parse_filters_protein_fields():
    params = parse_filters(
        {
            "protein_min": "12.5",
            "protein_max": "22.75",
            "protein_name": "beef",
        }
    )
    assert params.protein_min == 12.5
    assert params.protein_max == 22.75
    assert params.protein_name_like == "beef"


def test_apply_filters_protein_range_and_name():
    df = pd.DataFrame(
        {
            "ProductName": ["Beef Striploin", "Chicken Breast", "Beef Brisket"],
            "Protein": [20.0, 12.0, 24.0],
        }
    )
    filters = FilterParams(
        start=None,
        end=None,
        regions=tuple(),
        methods=tuple(),
        customers=tuple(),
        protein_min=15.0,
        protein_max=24.0,
        protein_name_like="beef",
    )
    filtered = apply_filters(df, filters)
    assert list(filtered["ProductName"]) == ["Beef Striploin", "Beef Brisket"]

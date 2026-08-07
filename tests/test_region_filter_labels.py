import pandas as pd

from app.services import filters_service
from app.services.filters import FilterParams, apply_filters


def test_region_options_use_labels():
    raw = {
        "regions": [
            {"id": "123", "label": "East"},
            {"id": "456", "label": "West"},
            {"id": "789", "label": None},
        ]
    }
    normalized = filters_service._normalize_options_map(raw).get("regions") or []
    east = next((item for item in normalized if item.get("value") == "123"), None)
    west = next((item for item in normalized if item.get("value") == "456"), None)
    fallback = next((item for item in normalized if item.get("value") == "789"), None)
    assert east is not None and east.get("label") == "East"
    assert west is not None and west.get("label") == "West"
    assert fallback is not None and fallback.get("label") == "789"
    assert fallback.get("bucket") == "regions"


def test_apply_filters_accepts_region_id():
    df = pd.DataFrame(
        {
            "RegionId": ["A", "B"],
            "RegionName": ["North", "South"],
            "Revenue": [100, 200],
            "Date": pd.to_datetime(["2024-01-01", "2024-01-02"]),
        }
    )
    params = FilterParams(regions=("A",))
    filtered = apply_filters(df.copy(), params)
    assert len(filtered) == 1
    assert filtered.iloc[0]["RegionName"] == "North"

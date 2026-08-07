import pandas as pd

from app.blueprints import products


def test_simple_forecast_excludes_current_month(monkeypatch):
    """Current partial month should not be used for training data."""
    today = pd.Timestamp(2024, 4, 15)
    df = pd.DataFrame(
        {
            products.CAN.date: pd.to_datetime(
                ["2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01"]
            ),
            products.CAN.revenue: [100.0, 120.0, 140.0, 999.0],
        }
    )

    forecast = products._simple_forecast(df, periods=2, today=today)

    assert forecast["dates"][0] == "2024-04"
    assert forecast["dates"][1] == "2024-05"

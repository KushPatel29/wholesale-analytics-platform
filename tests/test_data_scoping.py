import types

import pandas as pd
import pytest

import data_loader

pytestmark = pytest.mark.requires_real_loader


class DummyUser:
    def __init__(self, username: str, sales_rep_id: str | None, region_id: str | None, role: str = "sales"):
        self.username = username
        self.sales_rep_id = sales_rep_id
        self.region_id = region_id
        self.role = role


@pytest.fixture(autouse=True)
def stub_config(monkeypatch):
    class _Cfg:
        order_statuses = ["packed", "invoiced"]

    monkeypatch.setattr(data_loader, "get_config", lambda: _Cfg())
    monkeypatch.setattr(data_loader, "create_mssql_engine", lambda cfg: types.SimpleNamespace())


def test_get_dataframe_for_user_scopes_sales_rep_and_region(monkeypatch):
    sample = pd.DataFrame(
        {
            "SalesRepId": ["SR1", "SR1", "SR2"],
            "RegionName": ["East", "West", "East"],
            "Revenue": [100.0, 200.0, 300.0],
        }
    )

    captured = {}

    def fake_build_fact(*_, **kwargs):
        captured["sales_rep_override"] = kwargs.get("sales_rep_override")
        return sample.copy()

    monkeypatch.setattr(data_loader, "build_fact", fake_build_fact)

    user = DummyUser(username="rep1", sales_rep_id="SR1", region_id="East")
    df = data_loader.get_dataframe_for_user(
        user=user,
        user_sales_rep_id=user.sales_rep_id,
        region_ids=["East"],
        is_super_user=False,
    )

    assert captured["sales_rep_override"] == "SR1"
    assert not df.empty
    assert set(df["RegionName"]) == {"East"}


def test_get_dataframe_for_user_without_scope_returns_empty(monkeypatch):
    def fail_build_fact(*_, **__):
        raise AssertionError("build_fact should not be called when there is no scope")

    monkeypatch.setattr(data_loader, "build_fact", fail_build_fact)

    user = DummyUser(username="no.scope", sales_rep_id=None, region_id=None)
    df = data_loader.get_dataframe_for_user(
        user=user,
        user_sales_rep_id=None,
        region_ids=None,
        is_super_user=False,
    )
    assert isinstance(df, pd.DataFrame)
    assert df.empty

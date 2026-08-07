from __future__ import annotations

import os

from app.services import labor_store
from tests.labor_helpers import build_labor_dataset


def test_labor_store_views_and_kpi_summary(tmp_path, monkeypatch):
    dataset_path = tmp_path / "labor_dataset"
    build_labor_dataset(dataset_path)
    monkeypatch.setenv("LABOR_PARQUET_PATH", dataset_path.as_posix())
    labor_store.reset_duckdb_state()

    kpi = labor_store.query_df("SELECT * FROM labor_kpi_summary")
    assert not kpi.empty
    row = kpi.iloc[0]
    assert float(row["total_labor_cost"]) == 380.0
    assert float(row["total_paid_hours"]) == 16.0
    assert float(row["premium_cost"]) == 36.0
    assert float(row["absence_cost"]) == 44.0
    assert int(row["active_departments"]) == 2

    department = labor_store.query_df(
        """
        SELECT department_name, labor_cost, paid_hours, blended_rate
        FROM labor_department_daily
        WHERE department_name = 'Packaging'
        """
    )
    assert not department.empty
    dep = department.iloc[0]
    assert float(dep["labor_cost"]) == 204.0
    assert float(dep["paid_hours"]) == 8.0
    assert round(float(dep["blended_rate"]), 2) == 25.5

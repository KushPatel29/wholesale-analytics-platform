from __future__ import annotations

import html
import json
import re

from app.services import labor_store
from tests.labor_helpers import build_labor_dataset


def _make_client():
    import os
    from app import create_app

    os.environ.setdefault("FLASK_ENV", "development")
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test", LOGIN_DISABLED=True, PROPAGATE_EXCEPTIONS=True)
    return app.test_client()


def test_labor_page_renders_enterprise_workspace(tmp_path, monkeypatch):
    dataset_path = tmp_path / "labor_dataset"
    build_labor_dataset(dataset_path)
    monkeypatch.setenv("LABOR_PARQUET_PATH", dataset_path.as_posix())
    monkeypatch.setenv("LABOR_ANALYTICS_ENABLED", "1")
    labor_store.reset_duckdb_state()
    client = _make_client()

    response = client.get("/labor/")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    for token in [
        "Labor Intelligence",
        "Workforce Command Header",
        "Executive Labor Scorecard",
        "Decision Signals",
        "Department Investigation Layer",
        "Category &amp; Composition Layer",
        "Worker &amp; Assignment Layer",
        "Trend &amp; Operating Rhythm Layer",
        "Watchlist &amp; Action Layer",
        "Exploration Workspace",
    ]:
        assert token in body
    assert "NaN" not in body
    assert "Infinity" not in body
    match = re.search(r'<script id="LaborPageData" type="application/json">(.*?)</script>', body, re.S)
    assert match is not None
    client_payload = json.loads(html.unescape(match.group(1)))
    assert "workspace" not in client_payload
    assert "watchlists" not in client_payload
    assert "filter_options" not in client_payload
    assert "charts" in client_payload
    assert "focus" in client_payload


def test_labor_bundle_api_exposes_workspace_focus_layers_and_exports(tmp_path, monkeypatch):
    dataset_path = tmp_path / "labor_dataset"
    build_labor_dataset(dataset_path)
    monkeypatch.setenv("LABOR_PARQUET_PATH", dataset_path.as_posix())
    monkeypatch.setenv("LABOR_ANALYTICS_ENABLED", "1")
    labor_store.reset_duckdb_state()
    client = _make_client()

    bundle = client.get("/labor/api/bundle?start=2024-02-01&end=2024-02-10")
    assert bundle.status_code == 200
    assert "NaN" not in bundle.get_data(as_text=True)
    payload = bundle.get_json()

    assert payload["kpis"]["total_labor_cost"] == 380.0
    assert payload["hero"]["active_departments"] == 2
    assert payload["hero"]["top_department"] == "Packaging"
    assert payload["hero"]["top_category"] == "Absence"
    assert payload["hero"]["top_worker"] == "Jane Worker"
    assert payload["focus"]["department"]["department_name"] == "Packaging"
    assert payload["focus"]["category"]["time_category"] == "Absence"
    assert payload["focus"]["worker"]["employee_code"] == "E100"
    assert [group["title"] for group in payload["scorecard_groups"]] == [
        "Cost & Hours",
        "Premium / Absence",
        "Workforce Footprint",
        "Operational Pressure",
    ]
    assert payload["charts"]["category_mix_meta"]["value_key"] == "labor_cost"
    assert payload["charts"]["worker_cost"][0]["employee_code"] == "E100"
    assert payload["watchlists"]["workers"][0]["employee_code"] == "E100"
    assert [section["title"] for section in payload["watchlist_sections"]] == [
        "Department Watchlist",
        "Worker Watchlist",
        "Category Watchlist",
        "Premium Watchlist",
        "Absence Watchlist",
        "Concentration Watchlist",
        "Unstable Department Watchlist",
    ]
    assert "delta metrics are shown as n/a" in (payload["scope"]["comparator_note"] or "").lower()

    csv_resp = client.get("/labor/export/detail?format=csv")
    assert csv_resp.status_code == 200
    assert csv_resp.headers["Content-Type"].startswith("text/csv")

    for dataset in ["snapshot", "department-summary", "category-summary", "employee-summary"]:
        xlsx_resp = client.get(f"/labor/export/{dataset}?format=xlsx")
        assert xlsx_resp.status_code == 200
        assert "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" in xlsx_resp.headers["Content-Type"]

    watchlist_resp = client.get("/labor/export/watchlist?format=csv")
    assert watchlist_resp.status_code == 200
    assert watchlist_resp.headers["Content-Type"].startswith("text/csv")


def test_labor_bundle_drilldown_filters_inherit_scope(tmp_path, monkeypatch):
    dataset_path = tmp_path / "labor_dataset"
    build_labor_dataset(dataset_path)
    monkeypatch.setenv("LABOR_PARQUET_PATH", dataset_path.as_posix())
    monkeypatch.setenv("LABOR_ANALYTICS_ENABLED", "1")
    labor_store.reset_duckdb_state()
    client = _make_client()

    department_bundle = client.get("/labor/api/bundle?department=Packaging")
    assert department_bundle.status_code == 200
    department_payload = department_bundle.get_json()
    assert department_payload["kpis"]["total_labor_cost"] == 204.0
    assert department_payload["focus"]["department"]["department_name"] == "Packaging"
    assert department_payload["focus"]["category"]["time_category"] == "Overtime"
    assert {row["time_category"] for row in department_payload["focus"]["department"]["category_rows"]} == {"Regular", "Overtime"}
    worker_scope_url = department_payload["focus"]["department"]["worker_rows"][0]["employee_scope_url"]
    assert "department=Packaging" in worker_scope_url
    assert "employee=E100" in worker_scope_url

    category_bundle = client.get("/labor/api/bundle?time_category=Absence")
    assert category_bundle.status_code == 200
    category_payload = category_bundle.get_json()
    assert category_payload["kpis"]["total_labor_cost"] == 44.0
    assert category_payload["focus"]["category"]["time_category"] == "Absence"
    assert category_payload["focus"]["department"]["department_name"] == "Receiving"
    assert category_payload["focus"]["category"]["department_rows"][0]["department_name"] == "Receiving"
    department_scope_url = category_payload["focus"]["category"]["department_rows"][0]["department_scope_url"]
    assert "time_category=Absence" in department_scope_url
    assert "department=Receiving" in department_scope_url

    worker_bundle = client.get("/labor/api/bundle?employee=E200")
    assert worker_bundle.status_code == 200
    worker_payload = worker_bundle.get_json()
    assert worker_payload["kpis"]["total_labor_cost"] == 176.0
    assert worker_payload["focus"]["worker"]["employee_code"] == "E200"
    assert worker_payload["focus"]["worker"]["employee_name"] == "Mark Ops"
    assert worker_payload["focus"]["department"]["department_name"] == "Receiving"
    assert {row["time_category"] for row in worker_payload["focus"]["worker"]["category_rows"]} == {"Absence", "Regular"}


def test_labor_page_handles_missing_dataset(tmp_path, monkeypatch):
    missing_path = tmp_path / "missing_labor_dataset"
    monkeypatch.setenv("LABOR_PARQUET_PATH", missing_path.as_posix())
    monkeypatch.setenv("LABOR_ANALYTICS_ENABLED", "1")
    labor_store.reset_duckdb_state()
    client = _make_client()

    response = client.get("/labor/")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Labor dataset is not built yet" in body

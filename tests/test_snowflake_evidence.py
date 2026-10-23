"""
The Snowflake run of the dbt project is recorded, not repeated in CI (it runs
inside a Snowflake account, as a signed-in user). Hold the record, the
reconciliation and the README to each other, and the committed queries to the
code that writes them.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "warehouse" / "snowflake"
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _summary() -> dict:
    return json.loads((EVIDENCE / "run_summary.json").read_text(encoding="utf-8"))


def _reconciliation() -> list[dict]:
    with (EVIDENCE / "reconciliation.csv").open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_every_dbt_node_passed_on_snowflake():
    s = _summary()
    assert s["failures"] == []
    assert all(k.endswith((":success", ":pass")) for k in s["nodes"])
    green = sum(s["nodes"].values())
    assert f"{green} of {green} dbt nodes green on Snowflake" in README


def test_the_run_built_every_model_and_test_the_project_declares():
    s = _summary()
    models = list((ROOT / "models").rglob("*.sql"))
    assert s["nodes"]["model:success"] == len(models)


def test_every_mart_measure_matches_the_independent_pandas_build():
    rows = _reconciliation()
    marts = {r["mart"] for r in rows}
    assert {r["match"] for r in rows} == {"yes"}
    assert all(float(r["abs_diff"]) < 0.005 for r in rows)
    assert f"{len(rows)} of {len(rows)} measures across {len(marts)} marts" in README
    sql_marts = {p.stem for p in (ROOT / "models" / "marts").glob("*.sql")}
    assert marts == sql_marts


def test_the_reconciliation_is_built_from_the_recorded_query_result():
    record = json.loads((EVIDENCE / "snowflake_totals.json").read_text(encoding="utf-8"))
    recorded = {(t["mart"], t["measure"]): float(t["value"]) for t in record["totals"]}
    rows = _reconciliation()
    assert len(recorded) == len(rows)
    for r in rows:
        assert float(r["snowflake"]) == pytest.approx(recorded[(r["mart"], r["measure"])], abs=1e-6)
    assert re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", record["query_id"])


def test_the_committed_totals_query_is_the_one_the_script_writes():
    from warehouse.snowflake import reconcile

    committed = (EVIDENCE / "totals.sql").read_text(encoding="utf-8")
    assert reconcile.snowflake_sql(reconcile.marts()) in committed


def test_the_landed_sources_carry_every_column_the_staging_models_read():
    landing = ROOT / "warehouse" / "landing"
    sales = pd.read_parquet(landing / "sales_fact" / "sales_fact.parquet")
    quoted = set(re.findall(r'"(\w+)"::', (ROOT / "models" / "staging" / "stg_sales_fact.sql").read_text()))
    assert quoted <= set(sales.columns)
    labor = pd.read_parquet(landing / "labor_fact" / "labor_fact.parquet")
    assert {"LABOR_DATE", "EMPLOYEE_KEY", "DEPARTMENT_NAME", "PAID_HOURS", "SEPARATION_DATE",
            "SEPARATION_TYPE"} <= set(labor.columns)
    rma = pd.read_parquet(landing / "return_rmas" / "return_rmas.parquet")
    assert {"FIN_CLEARED_AT", "REJECTED_AT", "MGR_APPROVED_AT", "UPDATED_AT", "STATUS"} <= set(rma.columns)
    assert len(sales) == _summary()["loaded_rows"]["SALES_FACT"]


def test_no_landed_table_carries_contact_details():
    landing = ROOT / "warehouse" / "landing"
    for path in landing.rglob("*.parquet"):
        cols = {c.lower() for c in pd.read_parquet(path).columns}
        assert not cols & {"customer_email", "customer_phone", "employee_name", "customer_name"}, path


def test_the_snowflake_profile_holds_no_credential():
    import yaml

    profile = yaml.safe_load((ROOT / "profiles.yml").read_text(encoding="utf-8"))
    target = profile["northgate_retail_analytics"]["outputs"]["snowflake"]
    assert not {"password", "private_key", "private_key_path", "token", "authenticator"} & set(target)
    assert target["account"] == "" and target["user"] == ""

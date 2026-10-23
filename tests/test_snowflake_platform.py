"""
The platform scripts around the marts (warehouse/snowflake/platform/) run in a
Snowflake account, not in CI. Hold what Snowflake returned (evidence.json) to
the design, to an independent pandas reading of the same Parquet, and to the
README.
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pandas as pd
import pytest

from warehouse.snowflake.platform import access_matrix

ROOT = Path(__file__).resolve().parents[1]
PLATFORM = ROOT / "warehouse" / "snowflake" / "platform"
EVIDENCE = json.loads((PLATFORM / "evidence.json").read_text(encoding="utf-8"))
README = (ROOT / "README.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def lines() -> pd.DataFrame:
    d = pd.read_parquet(ROOT / "warehouse" / "landing" / "sales_fact" / "sales_fact.parquet")
    date = pd.to_datetime(d["Date"])
    natural = pd.Series(3, index=d.index)
    natural[date < "2026-06-01"] = 2
    natural[date < "2026-04-01"] = 1
    late = (d["OrderLineId"] % 997 == 0) & (date < "2026-06-01")
    d["arrives_in"] = natural.where(~late, 3)
    return d


# ------------------------------------------------------------------ access
def test_the_committed_checks_are_generated_from_the_design():
    assert (PLATFORM / "02_access_checks.sql").read_text(encoding="utf-8") == access_matrix.render()


def test_every_access_outcome_is_the_designed_one():
    checks = EVIDENCE["access"]["checks"]
    got = {(c["role"], c["action"], c["object"]): c["outcome"] for c in checks}
    assert len(checks) == len(access_matrix.EXPECTED) == 36
    assert got == access_matrix.EXPECTED
    for c in checks:
        assert bool(c["snowflake_said"]) == (c["outcome"] == "denied"), c


def test_least_privilege_reads_the_way_the_readme_says():
    allowed = {(c["role"], c["action"], c["object"]) for c in EVIDENCE["access"]["checks"]
               if c["outcome"] == "allowed"}
    analyst = {(a, o) for r, a, o in allowed if r == "NORTHGATE_ANALYST"}
    assert analyst == {("select", next(o for _, o, *_ in access_matrix.ACTIONS if o.startswith("mart") and "rebuilt" in o))}
    assert not any(r == "NORTHGATE_LOADER" and ("staging" in o or o.startswith("mart")) for r, _, o in allowed)
    assert not any(r == "NORTHGATE_TRANSFORMER" and a in {"insert", "delete"} for r, a, _ in allowed)
    assert not any("ACCOUNT_USAGE" in o for _, _, o in allowed)
    assert "36 of 36 outcomes as designed" in README


def test_the_marts_were_rebuilt_after_the_grants():
    rebuild = EVIDENCE["access"]["dbt_rebuild_after_grants"]
    assert rebuild["result"] == "PASS 48 WARN 0 ERROR 0 SKIP 0 TOTAL 48"
    assert "USE SECONDARY ROLES NONE;" in access_matrix.render()


# ------------------------------------------------------------------ incremental
def _plan(note: str, batch: int) -> int:
    return next(p["events"] for p in EVIDENCE["incremental"]["plan"] if p["note"] == note and p["batch"] == batch)


def test_the_change_feed_plan_matches_an_independent_reading(lines):
    ids, first = lines["OrderLineId"], lines["arrives_in"]
    for b in (1, 2, 3):
        assert _plan("first delivery", b) == int((first == b).sum())
    assert _plan("price correction", 2) == int(((ids % 101 == 0) & (first == 1)).sum())
    assert _plan("duplicate delivery", 2) == int(((ids % 499 == 0) & (first == 1)).sum())
    assert _plan("test order", 1) == _plan("test order cancelled", 3) == int(((ids % 1013 == 0) & (first == 1)).sum())
    assert _plan("stale replay", 3) == int(((ids % 1999 == 0) & (first == 1)).sum())
    landed = {x["batch"]: x["events"] for x in EVIDENCE["incremental"]["landed"]}
    for b in (1, 2, 3):
        assert landed[b] == sum(p["events"] for p in EVIDENCE["incremental"]["plan"] if p["batch"] == b)


def test_each_batch_left_the_tables_exactly_as_expected(lines):
    ids, first = lines["OrderLineId"], lines["arrives_in"]
    provisional = int(((ids % 101 == 0) & (first == 1) & (lines["Revenue"] != 0)).sum())
    tests = int(((ids % 1013 == 0) & (first == 1)).sum())
    cumulative = 0
    for state in EVIDENCE["incremental"]["after_each_batch"]:
        b = state["after_batch"]
        cumulative += next(x["events"] for x in EVIDENCE["incremental"]["landed"] if x["batch"] == b)
        assert state["events_landed"] == cumulative
        assert state["expected"] == {"real_lines": int((first <= b).sum()),
                                     "test_orders": tests if b < 3 else 0,
                                     "provisional_prices": provisional if b == 1 else 0}
        a = state["actual"]
        assert (a["real_lines"], a["test_orders"]) == (state["expected"]["real_lines"], state["expected"]["test_orders"])
        assert a["value_mismatches"] == state["expected"]["provisional_prices"]
        assert a["missing_lines"] == a["unexpected_lines"] == 0
        daily = state["daily_region_sales"]
        assert daily["missing_or_wrong"] == daily["extra_or_wrong"] == 0
        assert state["streams_pending"] == {"events": False, "current": False}


def test_the_task_graph_ran_each_batch_root_then_child():
    for state in EVIDENCE["incremental"]["after_each_batch"]:
        runs = state["task_runs"]
        assert [r["task"] for r in runs] == ["APPLY_SALES_EVENTS", "REFRESH_DAILY_REGION_SALES"]
        assert {r["state"] for r in runs} == {"SUCCEEDED"}
        assert runs[1]["scheduled"] == runs[0]["completed"], "the child starts when the root finishes"


def test_the_first_expectation_was_wrong_for_a_reason_in_the_data(lines):
    first = EVIDENCE["incremental"]["first_check_after_batch_1"]
    corrected = lines[(lines["OrderLineId"] % 101 == 0) & (lines["arrives_in"] == 1)]
    assert first["expected_provisional_prices"] == len(corrected)
    assert first["expected_provisional_prices"] - first["value_mismatches"] == int((corrected["Revenue"] == 0).sum()) == 15


def test_the_end_state_equals_the_source(lines):
    final = EVIDENCE["incremental"]["after_each_batch"][-1]
    assert final["after_batch"] == 3
    assert final["actual"]["real_lines"] == len(lines) == 94_758
    cells = lines.groupby(["Date", "RegionName"]).ngroups
    assert final["daily_region_sales"]["cells"] == cells
    assert f"{len(lines):,} lines, 0 values different" in README
    assert f"all {cells:,} cells" in README


# ------------------------------------------------------------------ snowpark
def test_snowpark_agrees_and_so_does_pandas(lines):
    s = EVIDENCE["snowpark"]["result"]
    for k in ("lines_only_in_source", "lines_only_in_pipeline", "daily_only_in_source", "daily_only_in_pipeline"):
        assert s[k] == 0, k
    assert s["source_lines"] == s["current_lines"] == s["pipeline_lines_in_daily"] == len(lines)
    assert s["daily_cells_expected"] == s["daily_cells_pipeline"] == lines.groupby(["Date", "RegionName"]).ngroups
    q = Decimal("0.000001")
    revenue = sum(Decimal(float(x)).quantize(q, rounding=ROUND_HALF_UP) for x in lines["Revenue"])
    assert Decimal(s["source_revenue"]) == Decimal(s["pipeline_revenue"]) == revenue
    assert f"${revenue:,.2f}" in README


# ------------------------------------------------------------------ cost
def test_the_cost_cap_is_in_place():
    c = EVIDENCE["cost"]
    m = c["resource_monitor"]
    assert (m["name"], m["credit_quota"], m["frequency"]) == ("NORTHGATE_MONTHLY", "10.00", "MONTHLY")
    assert (m["notify_at"], m["suspend_at"], m["suspend_immediately_at"]) == ("75%", "100%", "110%")
    assert c["warehouse"] == {"name": "DBT_WH", "size": "X-Small", "auto_suspend": 60, "resource_monitor": "NORTHGATE_MONTHLY"}
    tags = {t["query_tag"] for t in c["northgate_queries_by_tag"]}
    sql_tags = set()
    for p in PLATFORM.glob("*.sql"):
        for line in p.read_text(encoding="utf-8").splitlines():
            if "QUERY_TAG = 'northgate:" in line:
                sql_tags.add(line.split("'")[1])
    assert tags <= sql_tags and len(tags) >= 7
    total = c["credits_total_since_oct_1"]
    assert sum(d["credits"] for d in c["credits_by_day"]) == pytest.approx(total, abs=1e-3)
    assert f"{total:.2f} credits" in README

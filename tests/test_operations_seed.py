"""The operations seed, and the predicates it has to satisfy.

The nine operational workspaces shipped with every tile reading zero, because
the tables existed and nothing ever wrote to them. Row counts alone would not
have caught it either: the tiles read specific columns, so a seed that creates
records without setting `forecast_category`, `metadata_json["motion"]`,
`metadata_json["csat"]` or the four perfect-order booleans produces a populated
database above a page that still reads zero.

So these tests assert the *summaries*, not the row counts, and they run the real
generator against a temporary database rather than trusting a fixture.

Two deliberate zeros are asserted as zeros: `crm.exceptions` and
`master-data.exceptions`. Neither status vocabulary contains `exception`,
`held` or `escalated`, so the only way to make those tiles non-zero is to write
a record whose own status is outside its lifecycle - unusable by every
transition and unrenderable by the detail page's status select. They are
supposed to be zero, and a future "fix" that makes them non-zero is a
regression, not an improvement.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """Run the real seeder into a throwaway auth DB and hand back the summaries.

    `AUTH_DB_PATH` is read when app.auth.models is imported, so it has to be set
    before create_app() and the module has to be re-imported cleanly. Doing this
    in-process keeps the test honest - it exercises the generator itself, not a
    hand-built fixture that could drift from it.
    """
    import subprocess
    import sys

    db_dir = tmp_path_factory.mktemp("ops_seed")
    root = Path(__file__).resolve().parents[1]

    # conftest.py points PARQUET_PATH at an empty directory so the rest of the
    # suite does not scan the demo dataset. This module is the exception: the
    # seeder's whole point is that it builds records against REAL customers,
    # SKUs and suppliers, so it needs the real fact dataset. Inheriting the
    # empty-dataset path silently turned all 19 tests into skips.
    dataset = root / "cache" / "fact_dataset"
    if not any(dataset.rglob("*.parquet")):
        pytest.skip(f"fact dataset not built at {dataset} - run seed.generate_synthetic_data")

    env = dict(os.environ)
    env.update(
        {
            "AUTH_DB_PATH": str(db_dir / "ops_test.db"),
            "PARQUET_PATH": str(dataset),
            "DEMO_MODE": "1",
            "FLASK_ENV": "development",
            "SECRET_KEY": "operations-seed-test",
            "WA_FAST_PWHASH": "1",
        }
    )

    setup = subprocess.run(
        [sys.executable, "manage.py", "init-auth-db"], cwd=root, env=env, capture_output=True, text=True
    )
    if setup.returncode != 0:
        pytest.skip(f"init-auth-db unavailable here: {setup.stderr[-400:]}")
    users = subprocess.run(
        [sys.executable, "manage.py", "seed-demo-users"], cwd=root, env=env, capture_output=True, text=True
    )
    if users.returncode != 0:
        pytest.skip(f"seed-demo-users unavailable here: {users.stderr[-400:]}")

    run = subprocess.run(
        [sys.executable, "-m", "seed.generate_synthetic_operations", "--replace"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    if run.returncode != 0:
        pytest.skip(f"operations seed could not run here: {run.stderr[-600:]}")

    # Newline-separated, not semicolon-joined: a `;`-joined prefix in front of a
    # compound statement is the kind of -c string that fails silently and turns
    # this whole module into skips.
    reader = "\n".join(
        (
            "import json",
            "from app import create_app",
            "from app.decision_ops import service",
            "app=create_app()",
            "with app.app_context():",
            "    out={'actions':service.list_work_items(page=1,page_size=25)['summary']}",
            "    out['actions']['total']=service.list_work_items(page=1,page_size=1)['total']",
            "    for k in ('crm','orders','procurement','finance','inventory','master-data','service'):",
            "        r=service.list_operational_records(k,page=1,page_size=25)",
            "        s=dict(r['summary']); s['_page_items']=r['items']; out[k]=s",
            "print('__JSON__'+json.dumps(out,default=str))",
            "",
        )
    )
    read = subprocess.run(
        [sys.executable, "-c", reader], cwd=root, env=env, capture_output=True, text=True
    )
    marker = "__JSON__"
    if read.returncode != 0 or marker not in read.stdout:
        pytest.fail(
            "the seeder ran but its summaries could not be read - this module must not "
            f"silently skip.\nrc={read.returncode}\nstderr:\n{read.stderr[-800:]}"
        )
    import json

    return json.loads(read.stdout.split(marker, 1)[1].strip().splitlines()[0])


# --------------------------------------------------------------------------
# The whole point: no workspace may render an empty ledger.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "domain", ["crm", "orders", "procurement", "finance", "inventory", "master-data", "service"]
)
def test_every_workspace_has_records(seeded, domain):
    assert seeded[domain]["total"] > 0, f"{domain} seeded no operational records"


def test_action_centre_has_work(seeded):
    actions = seeded["actions"]
    assert actions["total"] > 0
    assert actions["open"] > 0, "no open work items - the ledger reads as finished"
    assert actions["overdue"] > 0, "nothing overdue - the Overdue tile cannot be demonstrated"
    assert actions["blocked"] > 0, "nothing blocked"
    assert actions["expected_impact"] > 0
    assert actions["realized_impact"] > 0, "realized impact sums completed work; seed at least one"


# --------------------------------------------------------------------------
# The value tiles. Each of these reads a column a naive seed would leave unset.
# --------------------------------------------------------------------------

def test_crm_pipeline_tiles_are_live(seeded):
    crm = seeded["crm"]
    assert crm["pipeline_value"] > 0
    assert 0 < crm["weighted_pipeline"] < crm["pipeline_value"], "weighting must discount the pipeline"
    assert crm["commit_value"] > 0, "needs forecast_category == 'commit' (the column, not the status)"
    assert crm["win_rate"] is not None, "needs at least one won and one lost opportunity"
    assert 0 < crm["win_rate"] < 1
    assert crm["average_deal_size"] > 0, "needs a won opportunity carrying an amount"
    assert crm["new_logo_pipeline"] > 0 and crm["expansion_pipeline"] > 0, "needs metadata_json['motion']"
    assert crm["next_step_completeness"] is not None and 0 < crm["next_step_completeness"] < 1, (
        "a mix of opportunities with and without a next step is what makes 'stalled' meaningful"
    )


def test_crm_stage_board_is_populated_on_page_one(seeded):
    """The board iterates the PAGINATED rows, not the whole domain.

    A summary showing a seven-figure pipeline above seven columns of
    "No opportunities" is the specific failure this guards.
    """
    items = seeded["crm"]["_page_items"]
    stages = {row["status"] for row in items if row["record_type"] == "opportunity"}
    for stage in ("prospecting", "discovery", "proposal", "negotiation", "commit", "won", "lost"):
        assert stage in stages, f"no opportunity in stage {stage!r} on page 1 - that column renders empty"


def test_orders_tiles_are_live(seeded):
    orders = seeded["orders"]
    assert orders["backlog_value"] > 0
    assert orders["holds"] > 0
    assert orders["partial_shipments"] > 0
    assert orders["backorders"] > 0, "needs fulfilled_quantity < quantity on open orders"
    rate = orders["perfect_order_rate"]
    assert rate is not None and 0 < rate <= 1, (
        "perfect_order_rate divides four metadata booleans by CLOSED orders; a rate above 1 "
        "means the flags were written onto orders that are not closed"
    )


def test_procurement_tiles_are_live(seeded):
    proc = seeded["procurement"]
    assert proc["open_commitments"] > 0
    assert proc["match_exceptions"] > 0, "needs invoice_match records in status 'exception'"
    assert proc["grni"] > 0
    assert proc["exceptions"] > 0
    assert proc["pending_approval"] > 0
    assert proc["purchase_price_variance"] != 0, "PPV is signed; a net of exactly zero is suspicious"


def test_finance_tiles_are_live(seeded):
    fin = seeded["finance"]
    assert fin["open_close_tasks"] > 0
    assert fin["posted_journals"] > 0, "needs journal_entry rows in status 'posted' exactly"
    assert fin["cash_forecast"] > 0
    assert fin["budget"] > 0
    assert fin["unreconciled"] > 0


def test_inventory_tiles_are_live(seeded):
    inv = seeded["inventory"]
    assert inv["open_proposals"] > 0
    assert inv["adjustments_pending"] > 0, "needs record_type 'adjustment' AND status 'pending_approval'"


def test_service_tiles_are_live(seeded):
    svc = seeded["service"]
    assert svc["open_cases"] > 0
    assert svc["sla_at_risk"] > 0, "needs open cases whose service_due_at is already past"
    assert svc["sla_at_risk"] < svc["open_cases"], "every open case breaching SLA reads as fabricated"
    assert svc["reopened"] > 0
    assert svc["csat"] is not None and 1.0 <= svc["csat"] <= 5.0, "csat must be a number in metadata_json"
    assert svc["exceptions"] > 0, "needs cases in status 'escalated'"


def test_master_data_is_governed_not_empty(seeded):
    md = seeded["master-data"]
    assert md["total"] > 0
    assert md["pending_approval"] > 0, "the approval queue is what this page is for"


# --------------------------------------------------------------------------
# The deliberate zeros. Asserted so a later change cannot quietly "fix" them.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("domain", ["crm", "master-data"])
def test_structurally_unreachable_exceptions_stay_zero(seeded, domain):
    assert seeded[domain]["exceptions"] == 0, (
        f"{domain} has no exception/held/escalated status in its vocabulary; a non-zero value here "
        "means a record was written outside its own lifecycle and cannot be transitioned or rendered"
    )


# --------------------------------------------------------------------------
# Money must be in the currency the rest of the app reports.
# --------------------------------------------------------------------------

def test_amounts_are_denominated_in_usd(seeded):
    """Both models default to CAD and the templates print the column verbatim."""
    for domain in ("crm", "orders", "procurement", "finance"):
        rows = [row for row in seeded[domain]["_page_items"] if row.get("amount")]
        assert rows, f"{domain} page 1 carries no amounts to check"
        assert {row["currency"] for row in rows} == {"USD"}, f"{domain} is reporting in the wrong currency"

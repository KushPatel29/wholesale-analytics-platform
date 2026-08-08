"""
The overview bundle has to stay small enough to build on the hosted demo box.

It used to be 1.44 MB, and 1.38 MB of that was one list: the margin-risk
watchlist, 175 rows of 82 columns, reachable from three places in the payload
(`profitability`, `insights.margin_risk`, `overview_metrics.profitability`) and
therefore serialised three times. The page renders ten of those rows and reads
about a dozen of those columns.

Two changes fixed it - project the rows to the fields the front end reads, and
cap how many travel - and both are the kind of change that silently breaks a
number on screen. So these tests pin the two things that could go wrong:

1. the aggregates the browser used to derive by walking the whole array are
   still exactly the aggregates of the whole array, not of the capped rows;
2. every field the renderer reads off a row is still shipped.

Dataset-backed, so they skip on a clone with no demo data:

    python -m seed.generate_synthetic_data
    PARQUET_PATH=cache/fact_dataset pytest tests/test_overview_bundle_payload.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app import create_app
from app.core.exceptions import DatasetNotBuiltError
from app.services import fact_store
from app.services import overview_v2

ROOT = Path(__file__).resolve().parent.parent
OVERVIEW_JS = ROOT / "app" / "static" / "js" / "overview.js"

# A generous ceiling, not a target. The point is to fail loudly if someone
# reintroduces a full frame dump into the payload, not to police a few KB.
BUNDLE_BYTES_BUDGET = 300_000


def _fact_rows() -> int:
    try:
        row = fact_store.get_conn().execute("SELECT COUNT(*) AS c FROM fact").fetchone()
    except Exception:
        return 0
    return int((row or [0])[0] or 0)


@pytest.fixture(scope="module")
def app():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, LOGIN_DISABLED=False)
    return app


@pytest.fixture(scope="module")
def client(app):
    with app.app_context():
        if _fact_rows() <= 0:
            pytest.skip("No fact dataset built; run `python -m seed.generate_synthetic_data`.")
    with app.test_client() as client:
        client.post("/auth/login", data={"username": "admin", "password": "admin"}, follow_redirects=True)
        yield client


def _clear_bundle_caches():
    """Both layers, or the second build just replays the first."""
    with overview_v2._bundle_cache_lock:
        overview_v2._bundle_cache.clear()
    try:
        from app.cache import cache

        cache.clear()
    except Exception:
        pass


def _fetch_bundle(client):
    resp = client.get("/overview/api/bundle?date_preset=current_fy&date_type=fiscal&_gf=1")
    assert resp.status_code == 200, resp.status_code
    return resp.data, resp.get_json()


@pytest.fixture(scope="module")
def bundles(client):
    """The shipped (capped) bundle and an uncapped one built the same way."""
    try:
        _clear_bundle_caches()
        capped_raw, capped = _fetch_bundle(client)

        original = overview_v2.MARGIN_RISK_PAYLOAD_LIMIT
        overview_v2.MARGIN_RISK_PAYLOAD_LIMIT = 10_000_000
        try:
            _clear_bundle_caches()
            _uncapped_raw, uncapped = _fetch_bundle(client)
        finally:
            overview_v2.MARGIN_RISK_PAYLOAD_LIMIT = original
            _clear_bundle_caches()
    except DatasetNotBuiltError:
        pytest.skip("No fact dataset built.")
    return capped_raw, capped, uncapped


def _profitability(bundle):
    return bundle.get("profitability") or {}


def test_bundle_stays_within_transport_budget(bundles):
    capped_raw, _capped, _uncapped = bundles
    assert len(capped_raw) < BUNDLE_BYTES_BUDGET, (
        f"overview bundle is {len(capped_raw):,} bytes (budget {BUNDLE_BYTES_BUDGET:,}). "
        "Something is shipping a full frame again."
    )


def test_watchlist_is_capped_but_reports_the_true_population(bundles):
    _raw, capped, uncapped = bundles
    capped_rows = _profitability(capped).get("margin_risk") or []
    uncapped_rows = _profitability(uncapped).get("margin_risk") or []
    if not uncapped_rows:
        pytest.skip("This window has no margin-risk SKUs to cap.")

    assert len(capped_rows) <= overview_v2.MARGIN_RISK_PAYLOAD_LIMIT
    # The count the UI prints is the population, not the rows that travelled.
    assert _profitability(capped)["margin_risk_total_count"] == len(uncapped_rows)


def test_capping_does_not_move_the_revenue_share_on_screen(bundles):
    _raw, capped, uncapped = bundles
    uncapped_rows = _profitability(uncapped).get("margin_risk") or []
    if not uncapped_rows:
        pytest.skip("This window has no margin-risk SKUs to cap.")

    # What the browser used to compute for itself, from every row.
    expected = sum(float(row.get("revenue_share") or 0.0) for row in uncapped_rows)
    served = float(_profitability(capped)["margin_risk_revenue_share_pct"])
    assert served == pytest.approx(expected, rel=1e-6), (
        "The margin-risk revenue share the server sends no longer matches the "
        "share of the full watchlist, so the figure on the page has drifted."
    )


def test_capped_rows_are_the_worst_offenders(bundles):
    """The cap must keep the head of the sort the page renders, not a slice."""
    _raw, capped, uncapped = bundles
    capped_rows = _profitability(capped).get("margin_risk") or []
    uncapped_rows = _profitability(uncapped).get("margin_risk") or []
    if not capped_rows:
        pytest.skip("This window has no margin-risk SKUs.")

    def impact(row):
        return float(row.get("profit_impact") or 0.0)

    head = uncapped_rows[: len(capped_rows)]
    assert [r.get("entity_id") for r in capped_rows] == [r.get("entity_id") for r in head]
    assert [impact(r) for r in capped_rows] == pytest.approx([impact(r) for r in head], rel=1e-9)
    # And the page's own sort (ascending profit impact) picks the same top 10.
    worst_uncapped = sorted(uncapped_rows, key=impact)[:10]
    worst_capped = sorted(capped_rows, key=impact)[:10]
    assert [r.get("entity_id") for r in worst_capped] == [r.get("entity_id") for r in worst_uncapped]


def test_every_field_the_renderer_reads_is_shipped():
    """
    Guard the projection against the renderer growing a new field.

    Scrapes the accessors the overview applies to a watchlist row. Asserting
    against a hand-written list would pass forever while the page quietly
    rendered `undefined`; reading the renderer means adding `r.new_thing` to
    the template without adding it to the projection fails here.
    """
    source = OVERVIEW_JS.read_text(encoding="utf-8")
    # The block that renders the watchlist rows, where the row is bound to `r`.
    start = source.find("if (els.marginRiskList)")
    assert start != -1, "Could not find the margin-risk renderer in overview.js"
    block = source[start : start + 4000]

    read_fields = set(re.findall(r"\br\.([a-z_][a-z0-9_]*)\b", block))
    # Fields the renderer derives rather than reads off the row.
    read_fields -= {"replace", "length", "map", "filter", "sort"}
    assert read_fields, "Scraper found no row accessors - the renderer moved."

    missing = sorted(read_fields - set(overview_v2.MARGIN_RISK_PAYLOAD_FIELDS))
    assert not missing, (
        f"overview.js reads {missing} off a margin-risk row, but the bundle "
        "projection drops it - the page will render undefined. Add it to "
        "overview_v2.MARGIN_RISK_PAYLOAD_FIELDS."
    )


def test_shipped_rows_actually_carry_those_fields(bundles):
    _raw, capped, _uncapped = bundles
    rows = _profitability(capped).get("margin_risk") or []
    if not rows:
        pytest.skip("This window has no margin-risk SKUs.")
    row = rows[0]
    for key in ("entity_id", "label", "supplier", "protein", "profit_impact", "margin_pct", "revenue"):
        assert key in row, f"margin-risk row is missing {key!r}: {sorted(row)}"


def test_no_single_key_dominates_the_payload(bundles):
    """
    The original bug was one key at 96% of the response. Anything over half is
    a full frame that slipped back in.
    """
    _raw, capped, _uncapped = bundles
    total = len(json.dumps(capped, default=str))
    for key, value in capped.items():
        share = len(json.dumps(value, default=str)) / total
        assert share < 0.5, f"'{key}' is {share:.0%} of the overview bundle."

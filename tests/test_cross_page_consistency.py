"""
The same filter must produce the same headline numbers on every page.

The demo shipped Revenue and AOV matching exactly across Customers and
Suppliers while Margin % read 21.1% and 20.5% - which is worse than a plain
disagreement, because two of the three numbers agreeing is what convinces a
reader the third one is trustworthy.

This module scrapes the rendered pages as the account visitors actually use and
compares what they print. It is deliberately a *rendering* test rather than a
service-layer one: the failure it guards against is two pages computing the
same idea differently, and a service-layer test would have to know about both
implementations to catch that.

`test_margin_pct_agrees_across_pages` was an expected failure for three
sessions with the measured deltas recorded in it, which is what kept it
findable; it now asserts, and its docstring carries the cause.

This module only has anything to compare when it is pointed at a built
dataset, which the root conftest deliberately withholds:

    PARQUET_PATH=cache/fact_dataset pytest tests/test_cross_page_consistency.py

The service-level guards that run in every environment are
`tests/test_bundle_kpi_reconciliation.py`, which diffs every shared KPI across
the five page bundles, and `tests/test_supplier_cost_basis.py`.
"""

from __future__ import annotations

import os
import re

import pytest

from app import create_app
from app.core.demo_accounts import DEMO_VIEWER_USERNAME

# Pages that print company-level headline figures under the global filter.
PAGES = ("/overview", "/customers/", "/suppliers/", "/regions/", "/salesreps/")

# A figure only counts as "the company total" if it is large enough that it
# cannot be a per-row or per-segment number.
_MONEY = re.compile(r"\$([\d,]{7,})")
_PERCENT = re.compile(r"(\d{1,3}\.\d)%")

# "Margin % 23.0%", "Margin %: 23.0%", and "Margin % (revenue-weighted) 23.0%".
#
# The parenthetical is not decoration - this app labels a ratio with its basis
# the same way it labels an HHI with its dimension, and the Suppliers card gained
# "(revenue-weighted)" the day its margin stopped being an unweighted mean. The
# regex without `(?:\([^)]*\)\s*)?` stopped matching that page and the
# comparison silently fell to one page, which is a skip, which is green. A test
# that stops comparing when a label is improved is a test that stops working
# exactly when someone is working on the thing it guards.
_MARGIN = re.compile(r"[Mm]argin %?\s*(?:\([^)]*\)\s*)?:?\s*(\d{1,3}\.\d)%")


FEATURE_FLAGS = (
    "OVERVIEW_V2", "OVERVIEW_V3", "PRODUCTS_V3", "PRODUCTS_V4",
    "SUPPLIER_DRILLDOWN_V2", "REGIONS_V2", "SALESREPS_V2",
    "CUSTOMERS_KPIS_V3", "RETURNS_ENABLED", "LABOR_ANALYTICS_ENABLED",
)


@pytest.fixture(scope="module")
def demo_client():
    # Restore os.environ afterwards. Setting these permanently leaked into
    # later modules and changed which template `customers` renders - this file
    # sorts before test_customers_kpis_v2.py, so it broke two of its tests that
    # pass in isolation. Feature flags set by one test module must not decide
    # what another one is testing.
    previous = {flag: os.environ.get(flag) for flag in FEATURE_FLAGS}
    for flag in FEATURE_FLAGS:
        os.environ[flag] = "1"

    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test")

    from app.auth.models import (
        User,
        UserScopeRule,
        get_session,
        get_user_by_username,
        sync_permissions,
    )

    sync_permissions()
    user = get_user_by_username(DEMO_VIEWER_USERNAME)
    if user is None:
        with get_session() as session:
            created = User(
                username=DEMO_VIEWER_USERNAME, role="demo_viewer",
                is_active=True, is_approved=True,
            )
            created.set_password("demo")
            session.add(created)
            session.commit()
        user = get_user_by_username(DEMO_VIEWER_USERNAME)

    # The scope rule is what `manage.py seed-demo-users` gives this account, and
    # without it every page here renders "Access not configured" with no figures
    # on it. That does not fail anything: `rendered` keeps pages by status code,
    # and 200 is exactly what an empty page returns. So the whole module passed
    # while asserting nothing - including the margin test below, which xpassed
    # on an empty dict and read as if the defect it tracks had been fixed.
    with get_session() as session:
        session.query(UserScopeRule).filter(UserScopeRule.user_id == user.id).delete()
        session.add(
            UserScopeRule(
                user_id=user.id, scope_type="rep", scope_value="*", scope_mode="allow"
            )
        )
        session.commit()

    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True
    yield client

    for flag, value in previous.items():
        if value is None:
            os.environ.pop(flag, None)
        else:
            os.environ[flag] = value


def _visible_text(html: str) -> str:
    """Rendered text only - script bodies carry raw payloads, not what is shown."""
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _page_text(client, path: str) -> str | None:
    response = client.get(path)
    if response.status_code != 200:
        return None
    return _visible_text(response.get_data(as_text=True))


@pytest.fixture(scope="module")
def rendered(demo_client):
    pages = {path: _page_text(demo_client, path) for path in PAGES}
    live = {path: text for path, text in pages.items() if text}
    if len(live) < 2:
        pytest.skip("needs a generated dataset; run seed.generate_synthetic_data")

    # A page that renders but carries no figures is not a page this module can
    # compare, and treating it as one is how every assertion below quietly went
    # vacuous. Skip only when *no* page has data (no dataset built); fail when
    # some do and others do not, because that is a real rendering defect.
    with_figures = {path for path, text in live.items() if _MONEY.search(text)}
    if not with_figures:
        pytest.skip("no page rendered a company figure; run seed.generate_synthetic_data")
    return {path: live[path] for path in with_figures}


class TestRevenue:
    def test_the_company_revenue_total_is_the_same_everywhere(self, rendered):
        """
        The one that must never drift. If pages disagree here, nothing else on
        them is worth reading.
        """
        totals: dict[str, set[str]] = {}
        for path, text in rendered.items():
            found = set(_MONEY.findall(text))
            if found:
                totals[path] = found

        # Only pages that print their totals server-side can be checked here.
        # The Overview and several others render KPIs from a bundle after paint,
        # so their figures are not in the HTML this sees. Checking those needs a
        # browser; skipping is honest, asserting over one page is not.
        if len(totals) < 2:
            pytest.skip(
                "fewer than two pages print a company total server-side "
                f"(found on: {sorted(totals)}); this needs a browser check"
            )

        shared = set.intersection(*totals.values())
        assert shared, (
            "no large currency figure appears on every page that prints one - "
            "the pages do not agree on any company total:\n"
            + "\n".join(f"  {path}: {sorted(v)[:4]}" for path, v in totals.items())
        )


class TestFormattingIsUniform:
    def test_no_page_prints_one_decimal_currency(self, rendered):
        """`$7,192,185.4` on one page and `$7,192,185` on another."""
        offenders = {
            path: re.findall(r"\$[\d,]+\.\d(?!\d)", text)[:3]
            for path, text in rendered.items()
            if re.search(r"\$[\d,]+\.\d(?!\d)", text)
        }
        assert not offenders, f"one-decimal currency: {offenders}"

    def test_no_page_prints_a_dollar_minus(self, rendered):
        """`$-79.48` instead of `-$79.48`."""
        offenders = {path: True for path, text in rendered.items() if "$-" in text}
        assert not offenders, f"$- formatting on: {sorted(offenders)}"

    def test_no_page_leaks_nan_or_undefined(self, rendered):
        offenders = {
            path: re.findall(r"\bNaN\b|\bundefined\b|\bNone\b", text)[:3]
            for path, text in rendered.items()
            if re.search(r"\bNaN\b|\bundefined\b", text)
        }
        assert not offenders, f"raw values reached the page: {offenders}"

    def test_no_page_leaks_an_iso_timestamp(self, rendered):
        offenders = {
            path: re.findall(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text)[:2]
            for path, text in rendered.items()
            if re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text)
        }
        assert not offenders, f"ISO timestamps rendered: {offenders}"


class TestTitles:
    def test_every_page_title_carries_the_product_name(self, demo_client):
        """
        Titles read `Business Performance`, `Sales Reps Performance` and
        `Inventory Analysis | Northgate Retail Analytics` - three conventions.
        """
        bad = {}
        for path in (*PAGES, "/products/", "/inventory/", "/labor/", "/returns/", "/planning/"):
            response = demo_client.get(path)
            if response.status_code != 200:
                continue
            match = re.search(r"<title>(.*?)</title>", response.get_data(as_text=True), re.S)
            title = (match.group(1).strip() if match else "")
            if not title.endswith("| Northgate Retail Analytics"):
                bad[path] = title
        assert not bad, f"titles not standardised: {bad}"


class TestGlobalFilterBar:
    def test_every_analytics_page_renders_the_filter_bar(self, demo_client):
        """
        It was absent on /labor/ and /returns/. That matters more than it looks
        on Labor, which now takes its window *from* the global filter - hiding
        the control left a visitor unable to see what the page was reporting on.
        """
        missing = []
        for path in ("/overview", "/customers/", "/products/", "/regions/",
                     "/suppliers/", "/inventory/", "/labor/", "/salesreps/",
                     "/returns/", "/planning/"):
            response = demo_client.get(path)
            if response.status_code != 200:
                continue
            if 'id="GlobalFilters"' not in response.get_data(as_text=True):
                missing.append(path)
        assert not missing, f"global filter bar missing on: {missing}"


class TestHhiIsAlwaysQualified:
    def test_no_page_prints_a_bare_hhi(self, rendered):
        """
        HHI appeared as 197, 378 and 1,933 for one company because the pages
        measured different dimensions without saying which. A bare "HHI" label
        is what let that pass unnoticed, so the label must name its dimension.
        """
        offenders = {}
        for path, text in rendered.items():
            for match in re.finditer(r"HHI(?!\s*\()", text):
                snippet = text[match.start() : match.start() + 40]
                # "HHI (customers)" is fine; "HHI:" or "HHI 197" is not.
                if not re.match(r"HHI\s*\(", snippet):
                    offenders.setdefault(path, []).append(snippet.strip()[:32])
        assert not offenders, f"unqualified HHI labels: {offenders}"


def test_margin_pct_agrees_across_pages(rendered):
    """
    Was an xfail for three sessions. The cause turned out to be one COALESCE.

    Suppliers read 22.3% where Customers, Regions and Sales Reps all read
    22.96% on identical revenue, and 22.96% is what the fact table says.
    `suppliers_bundle` nullified a zero cost before deciding whether a cost had
    been recorded, so 3,056 stockout lines - QuantityShipped 0, Revenue $0,
    Cost $0 - fell through to `cost_per_unit * units`, `units` fell back to
    QuantityOrdered because nothing shipped, and $333,573 of cost for goods
    that were never shipped landed against $0 of revenue.

    If this fails again, compare the four bundles' cost totals before touching
    any margin formula: the formula was never wrong.
    """
    margins: dict[str, str] = {}
    for path, text in rendered.items():
        match = _MARGIN.search(text)
        if match:
            margins[path] = match.group(1)
    if len(margins) < 2:
        pytest.skip(f"fewer than two pages print a margin server-side: {sorted(margins)}")
    assert len(set(margins.values())) <= 1, f"margin %% differs by page: {margins}"

"""
The §B2 campaign layer.

The load-bearing tests are the two that check this layer against something
outside itself: campaign spend against the Finance page's marketing line, and
attributed acquisitions against the customer counts in the sales fact. A
marketing page that agreed only with itself would be free to be wrong in
private.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.services import marketing_bundle, metrics


pytestmark = pytest.mark.skipif(
    not marketing_bundle.dataset_available(),
    reason="marketing dataset not built; run `python -m seed.generate_synthetic_marketing`",
)


@pytest.fixture(scope="module")
def frame() -> pd.DataFrame:
    loaded, _manifest = marketing_bundle._load()
    return loaded


@pytest.fixture(scope="module")
def manifest() -> dict:
    _loaded, data = marketing_bundle._load()
    return data


@pytest.fixture
def bundle(client) -> dict:
    """
    Through the endpoint: CLV is read from the Customers bundle, which needs a
    request context. Function-scoped to match `client`; the underlying payload
    is cached by dataset version, so repeat calls are cheap.
    """
    return client.get("/marketing/api/bundle").get_json()


class TestSeedTiesToTheOtherLayers:
    def test_campaign_spend_reconciles_to_the_finance_marketing_line(self, frame):
        """
        Two seeded layers disagreeing about one company's marketing budget would
        be worse than one layer.
        """
        finance = pd.concat(
            [pd.read_parquet(p) for p in sorted(marketing_bundle.FINANCE_DIR.glob("*.parquet"))],
            ignore_index=True,
        )
        finance["month"] = pd.to_datetime(finance["month"]).dt.date
        by_month = frame.groupby("month", as_index=False)["spend"].sum()
        merged = by_month.merge(finance[["month", "opex_marketing"]], on="month", how="left")
        assert merged["opex_marketing"].notna().all(), "a campaign month has no finance month"
        drift = (merged["spend"] - merged["opex_marketing"]).abs()
        assert (drift < 0.05).all(), f"max drift {drift.max()}"

    def test_attributed_acquisitions_match_the_sales_fact(self, frame):
        """The channel split is modelled; the monthly total is counted."""
        duckdb = pytest.importorskip("duckdb")
        con = duckdb.connect()
        try:
            actual = con.execute(
                """
                WITH first_order AS (
                    SELECT CustomerId, MIN(CAST(DateExpected AS DATE)) AS first_date
                    FROM read_parquet('cache/fact_dataset/**/*.parquet', hive_partitioning=true)
                    GROUP BY 1
                )
                SELECT CAST(date_trunc('month', first_date) AS DATE) AS month, COUNT(*) AS n
                FROM first_order GROUP BY 1
                """
            ).df()
        finally:
            con.close()
        actual["month"] = pd.to_datetime(actual["month"]).dt.date
        truth = dict(zip(actual["month"], actual["n"]))
        attributed = frame.groupby("month")["new_customers"].sum()
        for month, won in attributed.items():
            assert int(won) == int(truth.get(month, 0)), f"{month} attributed {won}"

    def test_attribution_is_proportional_to_qualified_leads(self, frame):
        """
        The seed models no per-channel close-rate difference, so every channel's
        share of wins must track its share of qualified leads.

        This pins a real defect. The first allocator floored each channel's
        proportional share and handed the whole remainder to the channel with
        the most qualified leads; at 2-10 wins a month across six channels the
        floors rounded to nothing and the remainder became the allocation. 46%
        of all wins landed on one or two channels, and the published channel
        table showed field sales closing 45% of qualified leads against trade
        shows' 3% - a rounding artefact rendered as a channel-efficiency finding
        with a "cost per win" column beside it.
        """
        for year, scoped in frame.groupby("fiscal_year"):
            by_channel = scoped.groupby("channel").agg(
                qualified=("qualified_leads", "sum"), won=("new_customers", "sum")
            )
            book_rate = by_channel["won"].sum() / by_channel["qualified"].sum() * 100
            rates = by_channel["won"] / by_channel["qualified"] * 100
            spread = rates.max() - rates.min()
            assert spread < 12, (
                f"FY{year} channel win rates span {spread:.1f} points around a book rate of "
                f"{book_rate:.1f}% with no modelled difference between channels:\n{rates}"
            )

    def test_no_channel_is_published_as_never_closing(self, frame):
        """
        A channel showing real spend and zero wins reads as "this channel does
        not work". It was an artefact of the allocator starving small channels,
        not a property of the data.
        """
        for year, scoped in frame.groupby("fiscal_year"):
            by_channel = scoped.groupby("channel").agg(
                spend=("spend", "sum"), won=("new_customers", "sum")
            )
            starved = by_channel[(by_channel["spend"] > 0) & (by_channel["won"] == 0)]
            assert starved.empty, f"FY{year} publishes spend with no wins:\n{starved}"

    def test_the_opening_book_is_excluded_and_the_reason_is_recorded(self, frame, manifest):
        """
        504 of 620 customers place a first order in the dataset's first month.
        A CAC computed against an opening book is a meaningless number.
        """
        assert manifest["excluded_months"], "expected the opening fiscal year to be excluded"
        assert "opening book" in manifest["exclusion_reason"].lower()
        assert min(frame["fiscal_year"]) >= 2025


class TestSeedInternalConsistency:
    def test_qualified_leads_never_exceed_leads(self, frame):
        assert (frame["qualified_leads"] <= frame["leads"]).all()

    def test_every_month_carries_every_channel(self, frame):
        counts = frame.groupby("month")["channel"].nunique()
        assert counts.nunique() == 1, "channel coverage varies by month"

    def test_spend_and_counts_are_never_negative(self, frame):
        for column in ("spend", "leads", "qualified_leads", "new_customers"):
            assert (frame[column] >= 0).all(), column


class TestBundle:
    def test_it_lands_on_a_complete_fiscal_year(self, bundle):
        assert bundle["available"] is True
        assert bundle["selected"]["complete"] is True

    def test_cac_matches_the_metric_function(self, bundle):
        totals = bundle["totals"]
        by_key = {k["key"]: k["value"] for k in bundle["kpis"]}
        assert by_key["cac"] == pytest.approx(
            metrics.customer_acquisition_cost(
                totals["total_acquisition_spend"], totals["new_customers"]
            )
        )

    def test_total_acquisition_spend_is_marketing_plus_the_selling_share(self, bundle):
        totals = bundle["totals"]
        assert totals["total_acquisition_spend"] == pytest.approx(
            totals["marketing_spend"] + totals["acquisition_selling_spend"]
        )

    def test_cac_uses_only_a_share_of_selling_not_all_of_it(self, bundle):
        """
        Charging the whole field organisation to the year's ~64 wins would say
        nothing about acquisition and everything about account servicing.
        """
        share = bundle["basis"]["acquisition_share_of_selling"]
        assert 0 < share < 1
        assert bundle["totals"]["acquisition_selling_spend"] > 0

    def test_clv_cac_uses_the_customers_page_clv(self, bundle):
        """One CLV definition, not a second one invented here."""
        totals = bundle["totals"]
        by_key = {k["key"]: k["value"] for k in bundle["kpis"]}
        if totals["average_clv"] is None:
            pytest.skip(
                "CLV needs the sales fact and conftest points the fact store at an empty "
                "dataset; the campaign parquet this module reads is unaffected"
            )
        assert by_key["clv_cac"] == pytest.approx(
            metrics.clv_to_cac_ratio(totals["average_clv"], by_key["cac"])
        )

    def test_payback_and_ratio_agree(self, bundle):
        by_key = {k["key"]: k["value"] for k in bundle["kpis"]}
        if by_key.get("clv_cac") is None or by_key.get("cac_payback") is None:
            pytest.skip("CLV unavailable under the empty test fact dataset")
        assert by_key["cac_payback"] == pytest.approx(12.0 / by_key["clv_cac"], rel=1e-6)

    def test_an_unavailable_clv_is_stated_not_faked(self, bundle):
        """
        Non-negotiable: never show a metric with no data behind it. When the
        fact store is empty the ratio must be absent and say why, not fall back
        to an invented CLV.
        """
        totals = bundle["totals"]
        by_key = {k["key"]: k for k in bundle["kpis"]}
        if totals["average_clv"] is not None:
            pytest.skip("CLV is available here, so there is no degraded path to check")
        assert by_key["clv_cac"]["value"] is None
        assert by_key["cac_payback"]["value"] is None
        assert "unavailable" in by_key["clv_cac"]["basis"].lower()

    def test_romi_is_reported_with_its_caveat_when_sales_fell(self, bundle):
        romi = next(k for k in bundle["kpis"] if k["key"] == "romi")
        growth = bundle["totals"]["sales_growth"]
        if growth is None:
            pytest.skip("no comparable prior year")
        if growth < 0:
            assert romi["value"] is not None and romi["value"] < 0
            assert romi["note"], "a negative ROMI must carry its explanation"

    def test_channel_rows_sum_to_the_totals(self, bundle):
        totals = bundle["totals"]
        assert sum(c["spend"] for c in bundle["channels"]) == pytest.approx(totals["marketing_spend"])
        assert sum(c["new_customers"] for c in bundle["channels"]) == pytest.approx(totals["new_customers"])
        assert sum(c["leads"] for c in bundle["channels"]) == pytest.approx(totals["leads"])

    def test_monthly_rows_do_not_multiply_the_selling_line(self, bundle):
        """
        Six campaign rows a month each carry the same month-level selling
        figure. Summing them would sextuple acquisition spend and CAC with it.
        """
        monthly_total = sum(row["acquisition_selling_spend"] for row in bundle["monthly"])
        assert monthly_total == pytest.approx(bundle["totals"]["acquisition_selling_spend"])

    def test_every_kpi_states_its_formula_and_basis(self, bundle):
        for kpi in bundle["kpis"]:
            assert kpi["formula"], kpi["key"]
            assert kpi["basis"], kpi["key"]

    def test_the_page_declares_that_filters_do_not_apply(self, bundle):
        assert "filters do not apply" in bundle["basis"]["scope"].lower()


class TestMarketingRoutes:
    def test_the_page_renders(self, client):
        response = client.get("/marketing/")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "MarketingApp" in body
        assert "marketing.js" in body

    def test_an_unknown_fiscal_year_falls_back(self, client):
        payload = client.get("/marketing/api/bundle?fy=1999").get_json()
        assert payload["available"] is True
        assert payload["selected"]["complete"] is True

    def test_a_non_numeric_fiscal_year_is_ignored(self, client):
        assert client.get("/marketing/api/bundle?fy=nope").get_json()["available"] is True

"""
The Tier B financial layer.

The tests that matter here are the ones that check the layer against something
outside itself: the seeded revenue against the sales fact it claims to come
from, and the bundle's ratios against arithmetic done by hand. A finance page
that is merely internally consistent is a finance page that can be consistently
wrong.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.services import finance_bundle, metrics


pytestmark = pytest.mark.skipif(
    not finance_bundle.dataset_available(),
    reason="finance dataset not built; run `python -m seed.generate_synthetic_finance`",
)

AR_BUCKETS = ("ar_current", "ar_d1_30", "ar_d31_60", "ar_d61_90", "ar_d90_plus")


@pytest.fixture(scope="module")
def frame() -> pd.DataFrame:
    loaded, _manifest = finance_bundle._load()
    return loaded


@pytest.fixture(scope="module")
def bundle() -> dict:
    return finance_bundle.build_finance_bundle()


class TestSeedTiesToTheSalesFact:
    """
    The whole point of the layer: the top line is read, not invented. If these
    drift, the Finance page and the Overview are reporting different companies.
    """

    def test_revenue_equals_gross_sales_less_discounts(self, frame):
        # This is the identity the DuckDB fact view produces, so it holds per
        # month and not only in total.
        for _, row in frame.iterrows():
            expected = row["gross_sales"] - row["discounts"]
            assert row["revenue"] == pytest.approx(expected, rel=1e-6), row["month_label"]

    def test_monthly_revenue_matches_a_fresh_read_of_the_fact(self, frame):
        """Re-derive one month straight from the parquet and compare."""
        duckdb = pytest.importorskip("duckdb")
        target = frame.iloc[len(frame) // 2]
        month = pd.Timestamp(target["month"])
        con = duckdb.connect()
        try:
            row = con.execute(
                """
                SELECT
                    SUM(CASE WHEN UnitOfBillingId = 3
                             THEN pack_weight_lb_sum * Price
                             ELSE pack_item_count_sum * Price END) AS revenue,
                    SUM(CASE WHEN UnitOfBillingId = 3
                             THEN pack_weight_lb_sum * CostPrice
                             ELSE pack_item_count_sum * CostPrice END) AS cogs
                FROM read_parquet('cache/fact_dataset/**/*.parquet', hive_partitioning=true)
                WHERE date_trunc('month', CAST(DateExpected AS DATE)) = CAST(? AS DATE)
                """,
                [month.date().isoformat()],
            ).fetchone()
        finally:
            con.close()
        assert row is not None
        assert target["revenue"] == pytest.approx(row[0], rel=1e-6)
        assert target["cogs"] == pytest.approx(row[1], rel=1e-6)

    def test_gross_profit_is_revenue_less_cogs(self, frame):
        computed = frame["revenue"] - frame["cogs"]
        assert (abs(frame["gross_profit"] - computed) < 0.02).all()


class TestSeedInternalConsistency:
    def test_ar_aging_buckets_sum_to_the_receivable(self, frame):
        """
        The bucket weights are jittered, so this is the assertion that catches a
        normalisation that stopped normalising.
        """
        total = frame[list(AR_BUCKETS)].sum(axis=1)
        assert (abs(total - frame["accounts_receivable"]) < 0.02).all()

    def test_current_assets_and_liabilities_are_the_sum_of_their_parts(self, frame):
        assets = frame[["cash", "accounts_receivable", "inventory", "prepaid_expenses"]].sum(axis=1)
        assert (abs(assets - frame["current_assets"]) < 0.02).all()
        liabilities = frame[["accounts_payable", "accrued_liabilities", "current_portion_debt"]].sum(axis=1)
        assert (abs(liabilities - frame["current_liabilities"]) < 0.02).all()

    def test_net_income_walks_down_from_revenue(self, frame):
        for _, row in frame.iterrows():
            expected = metrics.net_income(
                row["revenue"],
                row["cogs"],
                row["operating_expenses"],
                row["other_expense"],
                row["interest_expense"],
                row["tax_expense"],
                row["depreciation_amortization"],
            )
            assert row["net_income"] == pytest.approx(expected, abs=0.02), row["month_label"]

    def test_a_loss_month_would_carry_no_tax(self, frame):
        """Booking tax on a loss would understate the loss."""
        losses = frame[frame["pre_tax_income"] <= 0]
        assert (losses["tax_expense"] == 0).all()

    def test_operating_expense_equals_its_categories(self, frame):
        categories = [key for key, _label in finance_bundle.OPEX_LABELS]
        assert (abs(frame[categories].sum(axis=1) - frame["operating_expenses"]) < 0.02).all()

    def test_no_partial_month_is_published(self, frame):
        """
        The sales fact stops mid-July. A July holding four days of trading would
        render as a collapse on every trend on the page.
        """
        _loaded, manifest = finance_bundle._load()
        assert manifest.get("dropped_incomplete_months"), "expected the partial tail month to be recorded"
        published = {str(m) for m in frame["month"]}
        assert published.isdisjoint(set(manifest["dropped_incomplete_months"]))

    def test_october_belongs_to_the_next_fiscal_year(self, frame):
        """FY2025 runs Oct 1 2024 to Sep 30 2025."""
        by_month = {str(row["month"]): int(row["fiscal_year"]) for _, row in frame.iterrows()}
        assert by_month.get("2024-10-01") == 2025
        assert by_month.get("2025-09-01") == 2025
        assert by_month.get("2025-10-01") == 2026

    def test_fiscal_year_bounds(self):
        assert finance_bundle.fiscal_year_bounds(2025) == (date(2024, 10, 1), date(2025, 9, 30))


class TestBundle:
    def test_it_lands_on_the_latest_complete_fiscal_year(self, bundle):
        """A nine-month year at the top of the page reads as a collapse."""
        assert bundle["selected"]["complete"] is True
        incomplete = [y for y in bundle["fiscal_years"] if not y["complete"]]
        assert bundle["selected"]["year"] not in {y["year"] for y in incomplete}

    def test_every_fiscal_year_appears_as_a_comparative_column(self, bundle):
        assert len(bundle["comparatives"]) == len(bundle["fiscal_years"])

    def test_comparatives_carry_every_line_the_statement_renders(self, bundle):
        """
        A column missing D&A or tax renders a dash, which reads as missing data
        rather than as a column that was never asked for them.
        """
        line_keys = {line["key"] for line in bundle["income_statement"]["lines"]}
        for column in bundle["comparatives"]:
            missing = line_keys - set(column) - {"gross_profit", "revenue"}
            assert not missing, f"{column['label']} is missing {sorted(missing)}"

    def test_ratios_match_the_metric_functions(self, bundle):
        by_key = {ratio["key"]: ratio["value"] for ratio in bundle["ratios"]}
        statement = bundle["income_statement"]
        assert by_key["gross_profit_margin"] == pytest.approx(
            metrics.margin_pct(statement["revenue"], statement["cogs"])
        )
        assert by_key["net_profit_margin"] == pytest.approx(
            metrics.net_profit_margin(statement["net_income"], statement["revenue"])
        )

    def test_current_ratio_and_working_capital_agree_with_the_balance_sheet(self, bundle):
        sheet = {item["key"]: item["amount"] for item in bundle["balance_sheet"]["assets"]}
        sheet.update({item["key"]: item["amount"] for item in bundle["balance_sheet"]["liabilities"]})
        by_key = {ratio["key"]: ratio["value"] for ratio in bundle["ratios"]}
        assert by_key["current_ratio"] == pytest.approx(
            metrics.current_ratio(sheet["current_assets"], sheet["current_liabilities"])
        )
        assert by_key["working_capital"] == pytest.approx(
            metrics.working_capital(sheet["current_assets"], sheet["current_liabilities"])
        )

    def test_the_gross_to_net_bridge_adds_up(self, bundle):
        bridge = bundle["income_statement"]["gross_to_net"]
        if bridge.get("net_sales") is None:
            pytest.skip("returns subsystem unavailable, so net sales is correctly not stated")
        expected = (
            bridge["gross_sales"]
            - bridge["discounts"]
            - bridge["returns"]
            - bridge["adjustment_costs"]
        )
        assert bridge["net_sales"] == pytest.approx(expected, abs=0.02)

    def test_the_returns_window_matches_the_months_on_the_page(self, client):
        """
        A short fiscal year deducts returns only from the months it shows.

        The bridge used fiscal-year bounds while gross sales came from the
        months that survived the incomplete-month drop, so FY2026 netted a July
        credit against Oct-Jun sales - on the one page whose whole subject is
        that the figures tie, and directly under a line claiming July was
        excluded from every figure on it.
        """
        from app.services import finance_bundle as fb

        for entry in fb.available_fiscal_years():
            payload = client.get(f"/finance/api/bundle?fy={entry['year']}").get_json()
            bridge = payload["income_statement"]["gross_to_net"]
            if not bridge.get("returns_available"):
                pytest.skip("returns subsystem unavailable")
            fy_bridge = payload["income_statement"]["gross_to_net"]
            months_shown = payload["selected"]["months"]
            if months_shown >= 12:
                continue
            # A short year must not carry more credits than the full year does.
            full = [y for y in fb.available_fiscal_years() if y["complete"]]
            if not full:
                continue
            full_payload = client.get(f"/finance/api/bundle?fy={full[-1]['year']}").get_json()
            full_count = full_payload["income_statement"]["gross_to_net"]["return_count"]
            assert fy_bridge["return_count"] <= full_count, (
                f"FY{entry['year']} shows {months_shown} months but nets "
                f"{fy_bridge['return_count']} credits against a full year's {full_count}"
            )

    def test_net_sales_is_below_invoiced_sales(self, bundle):
        """Returns and handling only ever reduce the top line."""
        bridge = bundle["income_statement"]["gross_to_net"]
        if bridge.get("net_sales") is None:
            pytest.skip("returns subsystem unavailable")
        assert bridge["net_sales"] <= bridge["invoice_sales"]

    def test_a_short_year_annualises_its_flow_ratios_and_says_so(self, frame):
        short = [y for y in finance_bundle.available_fiscal_years() if not y["complete"]]
        if not short:
            pytest.skip("no partial fiscal year in this dataset")
        payload = finance_bundle.build_finance_bundle(short[0]["year"])
        flow = {r["key"]: r for r in payload["ratios"] if r["key"] in {"ar_turnover", "roa", "inventory_turnover_cost"}}
        for ratio in flow.values():
            assert ratio["note"], f"{ratio['key']} is annualised but does not say so"
        # A stock ratio needs no annualisation and must not claim it.
        stock = next(r for r in payload["ratios"] if r["key"] == "current_ratio")
        assert stock["note"] is None

    def test_liquidity_is_drawn_over_the_whole_dataset(self, bundle, frame):
        """The card says "across the whole dataset", so it had better be."""
        assert len(bundle["charts"]["liquidity_trend"]["labels"]) == len(frame)

    def test_the_monthly_table_covers_the_selected_year_only(self, bundle):
        assert len(bundle["monthly"]) == bundle["selected"]["months"]

    def test_every_ratio_states_its_formula_and_basis(self, bundle):
        for ratio in bundle["ratios"]:
            assert ratio["formula"], ratio["key"]
            assert ratio["basis"], ratio["key"]

    def test_the_page_declares_that_filters_do_not_apply(self, bundle):
        assert "filters do not apply" in bundle["basis"]["scope"].lower()


class TestFinanceRoutes:
    def test_the_page_renders(self, client):
        response = client.get("/finance/")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "FinanceApp" in body
        assert "finance.js" in body

    def test_the_bundle_endpoint_responds(self, client):
        response = client.get("/finance/api/bundle")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["available"] is True
        assert payload["ratios"]

    def test_an_unknown_fiscal_year_falls_back_rather_than_erroring(self, client):
        response = client.get("/finance/api/bundle?fy=1999")
        assert response.status_code == 200
        assert response.get_json()["selected"]["complete"] is True

    def test_a_non_numeric_fiscal_year_is_ignored(self, client):
        response = client.get("/finance/api/bundle?fy=not-a-year")
        assert response.status_code == 200
        assert response.get_json()["available"] is True

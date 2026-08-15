"""
The metric definitions, pinned.

Each of these corresponds to a contradiction that was visible on one screen of
the deployed demo. The tests are written against the behaviour a reader would
check, not against the implementation, so they stay meaningful if the internals
move.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.services import metrics


REFERENCE = date(2026, 7, 3)  # the data cutoff, not the wall clock


class TestGrowthMateriality:
    """
    `+2261.5%` on a prior period holding $13,078 across five orders, and
    `+896.5%` at company level. A percentage against a near-zero base is not a
    large number, it is a meaningless one.
    """

    def test_a_material_base_gets_a_percentage(self):
        result = metrics.growth_pct(120_000, 100_000, prior_orders=50)
        assert result.pct == 20.0
        assert result.label == "+20.0%"
        assert result.reason == "ok"

    def test_no_prior_activity_reads_as_new(self):
        result = metrics.growth_pct(50_000, 0)
        assert result.pct is None
        assert result.label == "New"
        assert result.reason == "new"

    def test_a_tiny_prior_base_is_refused(self):
        result = metrics.growth_pct(308_856, 1_200)
        assert result.pct is None, "must not report +25638%"
        assert result.label == "n/a"
        assert result.reason == "immaterial"

    def test_a_material_value_on_too_few_orders_is_refused(self):
        """
        $13,078 clears the revenue floor, but five orders is not a trend - and
        that is the exact row that reported +2261.5%.
        """
        result = metrics.growth_pct(308_856, 13_078, prior_orders=5)
        assert result.pct is None
        assert result.reason == "immaterial"

    def test_order_count_is_only_checked_when_given(self):
        result = metrics.growth_pct(120_000, 100_000)
        assert result.reason == "ok"

    def test_missing_input_never_becomes_zero(self):
        assert metrics.growth_pct(None, 100).label == "—"
        assert metrics.growth_pct(100, None).label == "—"

    def test_decline_is_reported(self):
        result = metrics.growth_pct(80_000, 100_000, prior_orders=40)
        assert result.pct == -20.0
        assert result.label == "-20.0%"


class TestCustomerLifecycle:
    """
    `Active (30d): 0` beside a KPI card reading `ACTIVE CUSTOMERS 80`, and a
    New/Active/At Risk/Churned chart with no Active bar at all.
    """

    def test_recent_order_is_active(self):
        assert metrics.customer_status(date(2026, 6, 20), REFERENCE) == "active"

    def test_measured_from_the_reference_date_not_today(self):
        """
        The whole cause: recency was measured against the wall clock while the
        data stopped weeks earlier, so nothing could be active.
        """
        assert metrics.is_active(date(2026, 6, 20), REFERENCE) is True
        assert metrics.is_active(date(2026, 6, 20), date(2026, 8, 9)) is False

    def test_the_ladder_is_exhaustive_and_ordered(self):
        assert metrics.customer_status(date(2026, 7, 3), REFERENCE) == "active"
        assert metrics.customer_status(date(2026, 5, 20), REFERENCE) == "at_risk"
        assert metrics.customer_status(date(2025, 9, 1), REFERENCE) == "churned"
        assert metrics.customer_status(None, REFERENCE) == "unknown"

    def test_every_account_lands_in_exactly_one_bucket(self):
        for offset in (0, 15, 30, 31, 60, 61, 120, 121, 400):
            last = date.fromordinal(REFERENCE.toordinal() - offset)
            buckets = [
                metrics.is_active(last, REFERENCE),
                metrics.is_at_risk(last, REFERENCE),
                metrics.is_churned(last, REFERENCE),
            ]
            assert sum(buckets) == 1, f"{offset} days landed in {sum(buckets)} buckets"


class TestRevenueAtRisk:
    """
    Three answers on one screen: `$0.00` in the health bar, `$19,187.00` in the
    card subtext, and ~$79k in the panel below.
    """

    def test_sums_only_at_risk_accounts(self):
        rows = [
            {"last_order_date": date(2026, 6, 25), "revenue": 100_000},  # active
            {"last_order_date": date(2026, 5, 20), "revenue": 49_310.61},  # at risk
            {"last_order_date": date(2026, 4, 1), "revenue": 11_529.39},  # at risk
            {"last_order_date": date(2025, 1, 1), "revenue": 500_000},  # churned
        ]
        assert metrics.revenue_at_risk(rows, REFERENCE) == 49_310.61 + 11_529.39

    def test_an_empty_book_is_zero_not_an_error(self):
        assert metrics.revenue_at_risk([], REFERENCE) == 0.0


class TestRepeatRate:
    """`Repeat rate: 100.0%` is implausible on its face, and was."""

    def test_single_order_customers_are_in_the_denominator(self):
        assert metrics.repeat_rate([1, 1, 1, 2]) == 25.0

    def test_all_repeat_is_a_hundred(self):
        assert metrics.repeat_rate([2, 3, 4]) == 100.0

    def test_no_customers_is_none_not_a_hundred(self):
        assert metrics.repeat_rate([]) is None


class TestMargin:
    """Customers said 21.1%, Suppliers 20.5%, on identical revenue and AOV."""

    def test_revenue_weighted_from_cost(self):
        assert metrics.margin_pct(1000, 800) == 20.0

    def test_revenue_weighted_from_profit(self):
        assert metrics.margin_pct(1000, profit=200) == 20.0

    def test_the_two_inputs_agree(self):
        assert metrics.margin_pct(7_192_185, 5_672_095) == metrics.margin_pct(
            7_192_185, profit=7_192_185 - 5_672_095
        )

    def test_it_is_not_the_average_of_row_margins(self):
        """
        The other definition weights a $12 line the same as a $120,000 one,
        which is where the 0.6-point gap between two pages came from.
        """
        rows = [(100.0, 50.0), (100_000.0, 90_000.0)]
        revenue = sum(r for r, _ in rows)
        cost = sum(c for _, c in rows)
        weighted = metrics.margin_pct(revenue, cost)
        naive = sum(metrics.margin_pct(r, c) for r, c in rows) / len(rows)
        assert abs(weighted - naive) > 5, "the two definitions genuinely differ"
        assert abs(weighted - 10.09) < 0.1

    def test_zero_revenue_is_none_not_zero(self):
        assert metrics.margin_pct(0, 0) is None


class TestConcentration:
    """Overview 197, Suppliers 378.0, Regions 1,933 - same company, same filter."""

    def test_hhi_is_on_the_standard_scale(self):
        assert metrics.herfindahl_index([50, 50]) == 5000.0
        assert metrics.herfindahl_index([100]) == 10000.0

    def test_a_flat_book_is_low(self):
        value = metrics.herfindahl_index([1] * 100)
        assert value is not None and value < 200

    def test_scale_is_never_zero_to_one(self):
        value = metrics.herfindahl_index([40, 30, 20, 10])
        assert value is not None and value > 1, "0-1 scale mislabelled as 0-10,000"
        assert value == 3000.0

    def test_the_label_names_the_dimension(self):
        """A bare "HHI" is what let three of them disagree unnoticed."""
        assert metrics.hhi_label("customers") == "HHI (customers)"

    def test_the_band_reads_the_number(self):
        assert metrics.hhi_band(900) == "unconcentrated"
        assert metrics.hhi_band(2000) == "moderately concentrated"
        assert metrics.hhi_band(3000) == "highly concentrated"
        assert metrics.hhi_band(None) == "—"

    def test_top_n_share(self):
        assert metrics.top_n_share([40, 30, 20, 10], n=2) == 70.0


class TestCoverage:
    """Overview said `Packs coverage 100%`; Regions said `-`, same filter."""

    def test_no_rows_is_none_not_zero(self):
        assert metrics.coverage_pct(0, 0) is None

    def test_no_covered_rows_is_zero_not_none(self):
        assert metrics.coverage_pct(0, 500) == 0.0

    def test_full_coverage(self):
        assert metrics.coverage_pct(500, 500) == 100.0


class TestTierAMetrics:
    def test_net_sales_reconciliation(self):
        result = metrics.net_sales_reconciliation(1_000, 50, 25, 5)
        assert result["net_sales"] == 920
        assert metrics.net_sales_reconciliation(1_000, 50, None, 5)["net_sales"] is None

    def test_forecast_accuracy_suite(self):
        result = metrics.forecast_accuracy_suite([100, 200], [90, 220], tolerance_pct=10)
        assert result["variance_pct"] == metrics.variance_pct(300, 310)
        assert result["mape_pct"] == 10.0
        assert result["bias"] == 5.0
        assert result["hit_rate_pct"] == 100.0

    def test_customer_rates_and_movement(self):
        assert metrics.retention_rate(100, 95, 10) == 85.0
        assert metrics.churn_rate(15, 100) == 15.0
        movement = metrics.revenue_movement(
            {"a": 100, "b": 200, "c": 300},
            {"a": 120, "b": 150, "d": 80},
        )
        assert movement["expansion"] == 20
        assert movement["contraction"] == 50
        assert movement["churned_revenue"] == 300
        assert movement["new_revenue"] == 80
        assert movement["nrr_pct"] == 45.0
        assert movement["logo_churn_rate_pct"] == pytest.approx(100 / 3)

    def test_clv_and_arpa(self):
        assert metrics.customer_lifetime_value(100, 12, 3, 25) == 900
        assert metrics.average_revenue_per_account(10_000, 20) == 500

    def test_workforce_rates(self):
        assert metrics.revenue_per_employee(1_000_000, 25) == 40_000
        assert metrics.revenue_per_paid_hour(1_000_000, 20_000) == 50
        assert metrics.employee_turnover_rate(4, 80) == 5

    def test_inventory_rates(self):
        assert metrics.gmroi(250_000, 100_000) == 2.5
        assert metrics.sell_through_rate(80, 100) == 80
        assert metrics.shrink_rate(1_000, 970) == 3

    def test_return_rates_and_resolution(self):
        assert metrics.return_rate(25, 1_000) == 2.5
        assert metrics.return_cost_rate(10_000, 500_000) == 2
        pairs = [
            (datetime(2026, 1, 1, 8), datetime(2026, 1, 2, 8)),
            (datetime(2026, 1, 1, 8), datetime(2026, 1, 3, 8)),
        ]
        assert metrics.average_resolution_hours(pairs) == 36

    def test_quota_attainment(self):
        assert metrics.quota_attainment(120_000, 100_000) == 120

    def test_missing_denominators_are_not_zeroes(self):
        assert metrics.return_rate(0, 0) is None
        assert metrics.gmroi(100, None) is None
        assert metrics.quota_attainment(10, 0) is None

    def test_every_catalogue_entry_has_required_lineage(self):
        required = {"name", "formula", "grain", "source_table", "owner_page", "basis", "status"}
        for entry in metrics.metric_catalogue():
            assert required <= entry.keys()
            assert all(entry[field] for field in required)

    def test_tier_c_is_explicitly_not_implemented(self):
        by_key = {entry["key"]: entry for entry in metrics.metric_catalogue()}
        for key in ("mrr", "survey_scores", "web_funnel", "hris_metrics", "scrap", "market_metrics"):
            assert by_key[key]["status"].startswith("Not implemented")

    def test_marketing_efficiency_moved_out_of_tier_c_when_b2_was_built(self):
        """
        CAC / CPL / ROMI were catalogued as "requires a marketing source" until
        the campaign layer existed. Leaving that placeholder beside the live
        metrics would have the catalogue contradicting the site.
        """
        by_key = {entry["key"]: entry for entry in metrics.metric_catalogue()}
        assert "marketing_efficiency" not in by_key
        for key in ("cac", "cpl", "romi"):
            assert by_key[key]["status"] == "Implemented"
        # Lead response time genuinely still needs a CRM, and stays declared.
        assert by_key["lead_response_time"]["status"].startswith("Not implemented")


class TestAcquisitionMetrics:
    """
    The §B2 acquisition block. Hand-computed throughout, and deliberately
    including the cases where the honest answer is unflattering.
    """

    def test_cac_is_spend_over_new_customers(self):
        # (400,000 marketing + 800,000 acquisition selling) / 64 customers
        assert metrics.customer_acquisition_cost(1_200_000, 64) == 18_750.0

    def test_cost_per_lead(self):
        assert metrics.cost_per_lead(440_000, 700) == pytest.approx(628.571428, rel=1e-6)

    def test_romi_is_positive_when_growth_beats_spend(self):
        # (2,000,000 - 400,000) / 400,000 * 100
        assert metrics.return_on_marketing_investment(2_000_000, 400_000, 400_000) == 400.0

    def test_romi_goes_negative_when_sales_fell(self):
        """
        The formula credits marketing with all sales movement, so a year revenue
        fell reads as a catastrophic marketing return. That is the formula's
        property, not a bug, and the page says so beside the number.
        """
        result = metrics.return_on_marketing_investment(-2_895_583, 439_813, 439_813)
        assert result is not None and result < -700
        assert result == pytest.approx((-2_895_583 - 439_813) / 439_813 * 100, rel=1e-9)

    def test_clv_to_cac_ratio(self):
        assert metrics.clv_to_cac_ratio(23_385.33, 17_655.0) == pytest.approx(1.3245, rel=1e-3)

    def test_cac_payback_months(self):
        # 12-month value of 24,000 is 2,000 a month; an 18,000 CAC repays in 9.
        assert metrics.cac_payback_months(18_000, 24_000) == 9.0

    def test_payback_and_ratio_are_two_readings_of_one_fact(self):
        clv, cac = 24_000.0, 18_000.0
        ratio = metrics.clv_to_cac_ratio(clv, cac)
        payback = metrics.cac_payback_months(cac, clv)
        assert payback == pytest.approx(12.0 / ratio, rel=1e-9)

    def test_no_acquisitions_is_undefined_not_free(self):
        """A month that won nothing has no CAC - it does not have a CAC of zero."""
        assert metrics.customer_acquisition_cost(50_000, 0) is None
        assert metrics.cost_per_lead(50_000, 0) is None
        assert metrics.clv_to_cac_ratio(20_000, 0) is None
        assert metrics.cac_payback_months(18_000, 0) is None
        assert metrics.return_on_marketing_investment(100, 100, 0) is None

    def test_every_acquisition_metric_is_catalogued(self):
        by_key = {entry["key"]: entry for entry in metrics.metric_catalogue()}
        for key in ("cac", "cpl", "romi", "clv_cac", "cac_payback"):
            assert key in by_key, f"{key} is not in the metric catalogue"
            assert by_key[key]["status"] == "Implemented"


class TestFinanceStatements:
    """
    The Tier B financial layer. Every expected value below is computed by hand
    in the assertion itself, so a reader can check the arithmetic without
    running anything.
    """

    # One consistent month, carried through the whole statement.
    REVENUE = 1_000_000.0
    COGS = 760_000.0
    OPEX = 150_000.0
    OTHER = 5_000.0
    INTEREST = 12_000.0
    TAXES = 18_000.0
    DA = 25_000.0

    def test_net_income_walks_every_line_down(self):
        # 1,000,000 - 760,000 - 150,000 - 5,000 - 12,000 - 18,000 - 25,000
        assert metrics.net_income(
            self.REVENUE, self.COGS, self.OPEX, self.OTHER, self.INTEREST, self.TAXES, self.DA
        ) == 30_000.0

    def test_a_missing_line_is_unknown_profit_not_a_smaller_one(self):
        assert metrics.net_income(
            self.REVENUE, self.COGS, None, self.OTHER, self.INTEREST, self.TAXES, self.DA
        ) is None

    def test_other_income_is_passed_negative(self):
        # An 8,000 gain rather than a 5,000 cost moves net income by 13,000.
        assert metrics.net_income(
            self.REVENUE, self.COGS, self.OPEX, -8_000, self.INTEREST, self.TAXES, self.DA
        ) == 43_000.0

    def test_net_profit_margin(self):
        assert metrics.net_profit_margin(30_000, 1_000_000) == 3.0

    def test_a_loss_reports_a_negative_margin(self):
        assert metrics.net_profit_margin(-45_000, 900_000) == -5.0

    def test_gross_margin_reuses_the_one_implementation(self):
        """Finance must not grow a second gross-margin formula."""
        assert metrics.margin_pct(self.REVENUE, self.COGS) == 24.0

    def test_current_ratio_is_a_multiple_not_a_percentage(self):
        assert metrics.current_ratio(2_400_000, 1_500_000) == 1.6

    def test_working_capital(self):
        assert metrics.working_capital(2_400_000, 1_500_000) == 900_000.0

    def test_negative_working_capital_is_reported(self):
        assert metrics.working_capital(1_200_000, 1_500_000) == -300_000.0

    def test_period_average_is_opening_plus_closing_over_two(self):
        assert metrics.period_average(800_000, 1_000_000) == 900_000.0

    def test_period_average_falls_back_to_the_side_it_has(self):
        assert metrics.period_average(None, 1_000_000) == 1_000_000.0
        assert metrics.period_average(800_000, None) == 800_000.0
        assert metrics.period_average(None, None) is None

    def test_average_balance_is_the_mean_of_month_ends(self):
        assert metrics.average_balance([900_000, 1_000_000, 1_100_000]) == 1_000_000.0

    def test_average_balance_ignores_gaps_rather_than_counting_them_as_zero(self):
        assert metrics.average_balance([900_000, None, 1_100_000]) == 1_000_000.0
        assert metrics.average_balance([]) is None
        assert metrics.average_balance([None, None]) is None

    def test_ar_turnover_uses_the_average_balance(self):
        average = metrics.period_average(800_000, 1_000_000)  # 900,000
        assert metrics.accounts_receivable_turnover(9_000_000, average) == 10.0

    def test_ap_overdue_pct(self):
        assert metrics.accounts_payable_overdue_pct(150_000, 1_200_000) == 12.5

    def test_return_on_assets(self):
        assert metrics.return_on_assets(30_000, 3_000_000) == 1.0

    def test_inventory_turnover_is_turns_not_a_percentage(self):
        """
        The brief writes `(COGS / Average inventory) * 100`. That factor would
        print a healthy 8 turns as 800, so it is deliberately not applied.
        """
        assert metrics.inventory_turnover(6_000_000, 750_000) == 8.0

    def test_the_two_inventory_definitions_are_catalogued_separately(self):
        """
        Cost-basis turnover and the Inventory page's usage-based turns are
        different measures that will not agree. Three disagreeing HHIs is what
        this codebase already paid for; these get distinct keys and bases.
        """
        by_key = {entry["key"]: entry for entry in metrics.metric_catalogue()}
        cost = by_key["inventory_turnover_cost"]
        usage = by_key["inventory_turns_usage"]
        assert cost["formula"] != usage["formula"]
        assert cost["basis"] != usage["basis"]
        assert cost["owner_page"] != usage["owner_page"]

    def test_zero_and_missing_denominators_never_become_zero(self):
        assert metrics.current_ratio(1_000, 0) is None
        assert metrics.accounts_receivable_turnover(1_000, None) is None
        assert metrics.accounts_payable_overdue_pct(100, 0) is None
        assert metrics.return_on_assets(100, 0) is None
        assert metrics.inventory_turnover(100, 0) is None
        assert metrics.net_profit_margin(100, 0) is None

    def test_every_finance_metric_is_catalogued(self):
        by_key = {entry["key"]: entry for entry in metrics.metric_catalogue()}
        for key in (
            "gross_profit_margin",
            "net_income",
            "net_profit_margin",
            "current_ratio",
            "working_capital",
            "ar_turnover",
            "ap_overdue",
            "roa",
            "inventory_turnover_cost",
        ):
            assert key in by_key, f"{key} is not in the metric catalogue"
            assert by_key[key]["status"] == "Implemented"

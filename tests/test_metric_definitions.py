"""
The metric definitions, pinned.

Each of these corresponds to a contradiction that was visible on one screen of
the deployed demo. The tests are written against the behaviour a reader would
check, not against the implementation, so they stay meaningful if the internals
move.
"""

from __future__ import annotations

from datetime import date

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

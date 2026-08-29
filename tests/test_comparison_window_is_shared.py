"""
Every page must answer "what are we comparing against?" the same way.

Under an identical global filter (Current FY, start 2025-10-01) the deployed
demo used four different prior windows:

    Overview   "prior fiscal year-to-date"    Oct 1 2024 - Jul 3 2025
    Products   "the same number of days"      Nov 22 2024 - Sep 30 2025
    Labor      ignored the global filter       Jan 5 2026 - Apr 4 2026
    Regions    "same window last year"         rendered blank

So revenue growth % differed by page for the same company on the same day.
These tests pin the fix: one function decides the window, and the per-page
services defer to it rather than reimplementing it.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.services import comparison
from app.services.filters import FilterParams


# A fixed filter, matching the one the demo was inspected under.
CURRENT_FY = FilterParams(
    start=pd.Timestamp("2025-10-01"),
    end=pd.Timestamp("2026-08-09"),
    preset="current_fy",
    date_type="fiscal",
)

# The dataset's last row. Passed explicitly so these tests do not depend on
# whether a generated dataset happens to be present.
CUTOFF = date(2026, 7, 3)


class TestBasis:
    def test_fiscal_preset_selects_the_fiscal_basis(self):
        window = comparison.resolve_comparison(CURRENT_FY, cutoff=CUTOFF)
        assert window.basis == comparison.PRIOR_FISCAL_YTD

    def test_arbitrary_range_selects_the_preceding_basis(self):
        filters = FilterParams(
            start=pd.Timestamp("2026-01-01"),
            end=pd.Timestamp("2026-03-31"),
            preset="custom",
            date_type="calendar",
        )
        window = comparison.resolve_comparison(filters, cutoff=CUTOFF)
        assert window.basis == comparison.SAME_LENGTH_PRECEDING

    def test_the_basis_is_always_labelled_with_its_dates(self):
        """
        A growth figure whose comparison window is not stated is not checkable,
        and every page renders this string.
        """
        window = comparison.resolve_comparison(CURRENT_FY, cutoff=CUTOFF)
        assert window.basis_short_label
        assert "Oct 1, 2024" in window.basis_label
        assert "2024-10-01" not in window.basis_label, "no ISO dates in front of a reader"


class TestClamping:
    def test_the_window_never_runs_past_the_data(self):
        """
        The filter ran 37 days past the last row. The damage was not an empty
        page - it was a trend chart whose final bar collapsed to near-zero, so
        the business appeared to be imploding.
        """
        window = comparison.resolve_comparison(CURRENT_FY, cutoff=CUTOFF)
        assert window.current_end == CUTOFF
        assert window.clamped is True
        assert window.requested_end == date(2026, 8, 9)

    def test_an_unclamped_window_is_left_alone(self):
        filters = FilterParams(
            start=pd.Timestamp("2025-10-01"),
            end=pd.Timestamp("2026-06-30"),
            preset="current_fy",
            date_type="fiscal",
        )
        window = comparison.resolve_comparison(filters, cutoff=CUTOFF)
        assert window.current_end == date(2026, 6, 30)
        assert window.clamped is False

    def test_the_prior_window_moves_with_the_clamped_current_one(self):
        """
        If only the current end is pulled back, the two windows stop covering
        comparable spans and every delta on the page is quietly wrong.
        """
        window = comparison.resolve_comparison(CURRENT_FY, cutoff=CUTOFF)
        assert window.prior_end == date(2025, 7, 3)
        assert window.prior_start == date(2024, 10, 1)

    def test_effective_today_follows_the_data_not_the_clock(self):
        """
        `Active (30d): 0` beside `ACTIVE CUSTOMERS 80` came from measuring
        recency against the wall clock while the data stopped five weeks
        earlier, so nothing could fall inside a 30-day window.
        """
        assert comparison.effective_today(CUTOFF) == CUTOFF


class TestEveryPageAgrees:
    """
    The services that used to carry their own date maths now defer.

    Asserted against the shared window rather than against a hardcoded date, so
    the test states the invariant - "these agree" - rather than a fact about
    one dataset.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def expected(cls):
        del cls
        return comparison.resolve_comparison(CURRENT_FY, cutoff=CUTOFF)

    def test_products_defers(self, expected, monkeypatch):
        from app.services import products_bundle

        monkeypatch.setattr(comparison, "data_cutoff", lambda: CUTOFF)
        payload = products_bundle._build_comparison_window(
            "2025-10-01", "2026-08-10", CURRENT_FY
        )
        assert payload["prior_start"] == expected.prior_start.isoformat()
        assert payload["prior_end"] == expected.prior_end.isoformat()
        assert payload["current_end"] == expected.current_end.isoformat()

    def test_regions_defers(self, expected, monkeypatch):
        from app.services import regions_bundle

        monkeypatch.setattr(comparison, "data_cutoff", lambda: CUTOFF)
        windows = regions_bundle._comparison_windows("2025-10-01", "2026-08-10", CURRENT_FY)
        assert windows["prior_start"] == expected.prior_start.isoformat()
        # Regions works in exclusive end dates.
        assert windows["prior_end"] == expected.prior_end_exclusive.isoformat()

    def test_regions_renders_a_basis_label(self, monkeypatch):
        """`YoY Growth: -` was a blank column where a number belonged."""
        from app.services import regions_bundle

        monkeypatch.setattr(comparison, "data_cutoff", lambda: CUTOFF)
        windows = regions_bundle._comparison_windows("2025-10-01", "2026-08-10", CURRENT_FY)
        assert windows["basis_label"], "Regions must state its comparison basis"

    def test_overview_window_contract_is_clamped(self, expected, monkeypatch):
        from app.services import overview_metrics

        monkeypatch.setattr(comparison, "data_cutoff", lambda: CUTOFF)
        monkeypatch.setattr(comparison, "effective_today", lambda cutoff=None: CUTOFF)
        contract = overview_metrics.resolve_window_contract(CURRENT_FY)
        assert contract.current_end <= CUTOFF
        assert contract.prior_month_end <= CUTOFF


class TestLabelling:
    def test_dates_are_rendered_for_humans(self):
        assert comparison.format_day(date(2025, 10, 1)) == "Oct 1, 2025"
        assert comparison.format_window(date(2025, 10, 1), date(2026, 7, 3)) == (
            "Oct 1, 2025 – Jul 3, 2026"
        )


class TestPartialPeriods:
    def test_last_complete_month_excludes_a_partial_one(self):
        """
        The suppliers trend chart's final bar was a partial July rendered as if
        it were a whole month, so revenue looked like it had fallen off a cliff.
        """
        assert comparison.last_complete_month_end(date(2026, 7, 3)) == date(2026, 6, 30)

    def test_a_month_that_ends_on_the_cutoff_is_complete(self):
        assert comparison.last_complete_month_end(date(2026, 6, 30)) == date(2026, 6, 30)

    def test_is_partial_month(self):
        assert comparison.is_partial_month(date(2026, 7, 15), date(2026, 7, 3)) is True
        assert comparison.is_partial_month(date(2026, 6, 15), date(2026, 7, 3)) is False

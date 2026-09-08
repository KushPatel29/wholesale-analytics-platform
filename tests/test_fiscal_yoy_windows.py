"""
Year-over-year has to compare a period with a period of the same length.

The Overview reported "FYTD +10.6%" and "YoY -16.6%" side by side for the same
business on the same screen, and the second number was an artefact: for a
period still in progress, `get_fiscal_periods` returned the *full* prior
fiscal year as the year-over-year comparator while the current side was only
the elapsed part of the year. Under Current FY that divided 277 elapsed days
by a 365-day prior year, and 277/365 is -24% before any real movement.

    (1 + 0.106) * 277 / 365 - 1  ==  -16.1%     the figure the page showed

`comparison.year_ago_window` - which every other page uses - means "the same
calendar window one year earlier", and that is what these now return.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.services.filters import get_fiscal_periods


def _days(start: pd.Timestamp, end: pd.Timestamp) -> int:
    return (end - start).days + 1


@pytest.fixture(scope="module")
def periods():
    # Mid-fiscal-year, so the in-progress periods are genuinely partial.
    return get_fiscal_periods(pd.Timestamp("2026-07-04"))


IN_PROGRESS = ["current_fy", "current_fq", "current_fm", "fytd_comparison"]


class TestInProgressPeriodsCompareLikeWithLike:
    @pytest.mark.parametrize("preset", IN_PROGRESS)
    def test_yoy_window_is_the_same_length_as_the_current_window(self, preset, periods):
        period = periods[preset]
        current = _days(period["start"], period["end"])
        yoy = _days(period["yoy_start"], period["yoy_end"])

        assert yoy == current, (
            f"{preset}: year-over-year compares {current} elapsed days against "
            f"{yoy} prior days, so the percentage carries a {current / yoy:.0%} "
            "length ratio that has nothing to do with the business"
        )

    @pytest.mark.parametrize("preset", IN_PROGRESS)
    def test_yoy_window_starts_one_year_before_the_current_window(self, preset, periods):
        period = periods[preset]
        expected_start = (period["start"] - pd.DateOffset(years=1)).normalize()
        assert period["yoy_start"] == expected_start

    @pytest.mark.parametrize("preset", IN_PROGRESS)
    def test_yoy_window_ends_before_the_current_window_begins(self, preset, periods):
        period = periods[preset]
        assert period["yoy_end"] < period["start"]

    def test_current_fy_yoy_matches_the_prior_year_to_date_comparator(self, periods):
        """For a year-to-date view, "same period last year" *is* prior FYTD."""
        period = periods["current_fy"]
        assert period["yoy_start"] == period["comparison_start"]
        assert period["yoy_end"] == period["comparison_end"]


class TestCompletedPeriodsAreUnchanged:
    @pytest.mark.parametrize("preset", ["previous_fy", "previous_fq", "previous_fm"])
    def test_completed_periods_compare_whole_period_to_whole_period(self, preset, periods):
        """A finished period is compared in full; only the partial ones were wrong."""
        period = periods[preset]
        current = _days(period["start"], period["end"])
        yoy = _days(period["yoy_start"], period["yoy_end"])

        # Leap years make an exact match impossible, so allow a single day.
        assert abs(yoy - current) <= 1

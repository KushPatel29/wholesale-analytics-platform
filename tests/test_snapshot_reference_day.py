"""Default windows on a dataset that is a snapshot count from its last day, not from today.

Found on 2026-10-01: the fiscal year turned over, "current fiscal year" became a year the seeded demo (which ends on a
fixed date) holds nothing of, and a clean clone opened on an empty dashboard with the product drilldown answering 400.
The hosted demo was already anchored (by its prebuilt-cache flag); a plain checkout was not.
"""

from datetime import date, timedelta

import pandas as pd
import pytest

from app.services import comparison, filters


@pytest.fixture
def snapshot(monkeypatch):
    def use(newest: date | None) -> None:
        monkeypatch.setattr(comparison, "data_cutoff", lambda: newest)

    return use


def test_a_snapshot_older_than_a_week_is_the_reference_day(snapshot):
    snapshot(date(2026, 7, 4))
    assert filters.reference_now() == pd.Timestamp("2026-07-04")


def test_fresh_data_and_missing_data_use_the_wall_clock(snapshot):
    today = pd.Timestamp.now(tz="UTC").normalize()
    snapshot(date.today() - timedelta(days=2))  # a live feed: yesterday's or the day before's rows
    assert filters.reference_now().normalize() == today
    snapshot(None)
    assert filters.reference_now().normalize() == today


def test_a_now_the_caller_names_is_left_alone(snapshot):
    snapshot(date(2026, 7, 4))
    named = pd.Timestamp("2027-03-15")
    assert filters.reference_now(named) is named


def test_the_current_fiscal_year_of_a_snapshot_is_the_year_it_ends_in(snapshot):
    snapshot(date(2026, 7, 4))
    current = filters.get_fiscal_periods()["current_fy"]
    assert current["start"] == pd.Timestamp("2025-10-01")  # not the 2026-27 year the calendar has moved into
    assert current["end"] >= pd.Timestamp("2026-07-04")


def test_the_staleness_threshold_is_configurable(snapshot, monkeypatch):
    snapshot(date.today() - timedelta(days=3))
    monkeypatch.setenv("SNAPSHOT_STALE_DAYS", "1")
    assert filters.reference_now() == pd.Timestamp(date.today() - timedelta(days=3))


def test_row_level_history_is_measured_from_the_snapshot(snapshot):
    snapshot(date(2026, 7, 4))
    assert comparison.effective_today() == date(2026, 7, 4)

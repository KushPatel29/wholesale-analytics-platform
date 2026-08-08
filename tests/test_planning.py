"""
The planner's arithmetic.

The page answers "where is demand going, how reliably can we supply it, and
where do those disagree", so the things worth pinning are the ones that would
quietly mislead a planner rather than crash:

* a growing department behind a failing lane must land in `at_risk` - that
  quadrant is the entire point of the page;
* a rate computed on four deliveries must not be ranked as if it were real;
* growth must be undefined, not infinite, when there is no prior revenue.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.services import planning


def _frame(rows):
    """A minimal fact frame with the columns the planner reads."""
    return pd.DataFrame(rows)


def _line(date, department, revenue, late, lane="DC Ambient", vendor="Acme Brands"):
    return {
        "Date": pd.Timestamp(date),
        "ProteinType": department,
        "Revenue": float(revenue),
        "IsLate": bool(late),
        "ShippingMethodName": lane,
        "SupplierName": vendor,
    }


def _window(department_plan):
    """
    Build a 120-day window from a {department: (prior_rows, recent_rows)} spec,
    where each row is (revenue, is_late).
    """
    rows = []
    for dept, (prior, recent) in department_plan.items():
        for i, (revenue, late) in enumerate(prior):
            rows.append(_line(f"2026-01-{(i % 28) + 1:02d}", dept, revenue, late))
        for i, (revenue, late) in enumerate(recent):
            rows.append(_line(f"2026-04-{(i % 28) + 1:02d}", dept, revenue, late))
    return _frame(rows)


class TestQuadrant:
    def test_growing_and_unreliable_is_at_risk(self):
        assert planning._quadrant(change_pct=20.0, on_time_pct=70.0, lines=100) == "at_risk"

    def test_growing_and_reliable_is_scale(self):
        assert planning._quadrant(change_pct=20.0, on_time_pct=97.0, lines=100) == "scale"

    def test_flat_and_unreliable_is_fix_service(self):
        assert planning._quadrant(change_pct=1.0, on_time_pct=70.0, lines=100) == "fix_service"

    def test_flat_and_reliable_is_steady(self):
        assert planning._quadrant(change_pct=1.0, on_time_pct=97.0, lines=100) == "steady"

    def test_a_handful_of_lines_is_never_a_verdict(self):
        """
        Four deliveries that missed one is not a 25% failure rate, and calling
        it one puts a rounding error at the top of the action list.
        """
        assert planning._quadrant(change_pct=50.0, on_time_pct=25.0, lines=4) == "insufficient_data"

    def test_a_department_with_no_prior_revenue_is_not_growing(self):
        """`None` growth means new in this window, not infinite growth."""
        assert planning._quadrant(change_pct=None, on_time_pct=70.0, lines=100) == "fix_service"


class TestServiceLevel:
    def test_on_time_is_the_complement_of_late(self):
        frame = _frame([_line("2026-01-01", "Grocery", 10, late) for late in [True, False, False, False]])
        assert planning._on_time_pct(frame) == pytest.approx(75.0)

    def test_boolean_column_may_arrive_as_text(self):
        """Some extracts hand back "true"/"false" rather than a bool dtype."""
        frame = _frame([_line("2026-01-01", "Grocery", 10, False) for _ in range(4)])
        frame["IsLate"] = pd.Series(["true", "false", "false", "false"], dtype=object)
        assert planning._on_time_pct(frame) == pytest.approx(75.0)

    def test_thin_groups_are_reported_but_not_ranked_first(self):
        rows = [_line("2026-01-01", "Grocery", 10, True, lane="Tiny Lane") for _ in range(3)]
        rows += [_line("2026-01-02", "Grocery", 10, i < 10, lane="Busy Lane") for i in range(60)]
        lanes = planning._service_by(_frame(rows), "ShippingMethodName")

        by_label = {lane["label"]: lane for lane in lanes}
        # Tiny Lane is 0% on time and Busy Lane is ~83%, but Tiny Lane has three
        # rows, so it must not be presented as the worst lane in the network.
        assert by_label["Tiny Lane"]["reliable_estimate"] is False
        assert by_label["Tiny Lane"]["status"] == "unknown"
        assert lanes[0]["label"] == "Busy Lane"


class TestDemand:
    def test_growth_compares_the_two_halves(self):
        frame = _window({"Grocery": ([(100, False)] * 10, [(150, False)] * 10)})
        prior, recent, meta = planning._split_window(frame)
        assert meta["comparable"] is True
        rows = planning._demand_by_department(prior, recent, "ProteinType")
        assert rows[0]["label"] == "Grocery"
        assert rows[0]["change_pct"] == pytest.approx(50.0)
        assert rows[0]["direction"] == "growing"

    def test_a_new_department_reports_no_change_rather_than_infinity(self):
        frame = _window({
            "Grocery": ([(100, False)] * 10, [(100, False)] * 10),
            "Electronics": ([], [(500, False)] * 10),
        })
        prior, recent, _ = planning._split_window(frame)
        rows = {r["label"]: r for r in planning._demand_by_department(prior, recent, "ProteinType")}
        assert rows["Electronics"]["change_pct"] is None
        assert rows["Electronics"]["direction"] == "new"

    def test_a_short_window_refuses_to_report_a_trend(self):
        frame = _frame([_line("2026-01-01", "Grocery", 10, False) for _ in range(5)])
        _prior, _recent, meta = planning._split_window(frame)
        assert meta["comparable"] is False
        assert "too short" in meta.get("reason", "").lower()


class TestEndToEnd:
    def test_the_at_risk_department_reaches_the_action_list(self):
        """
        A department growing into a failing lane is the one case the page
        exists for, so it has to survive the whole pipeline: demand, matrix,
        quadrant, and out into a ranked action.
        """
        frame = _window({
            # Grows 50% and misses a third of its dates.
            "Electronics": (
                [(100, i % 3 == 0) for i in range(30)],
                [(150, i % 3 == 0) for i in range(30)],
            ),
            # Flat and reliable - must not appear.
            "Grocery": (
                [(100, False) for _ in range(30)],
                [(100, False) for _ in range(30)],
            ),
        })
        payload = planning.build_planning(frame)

        by_label = {p["label"]: p for p in payload["matrix"]}
        assert by_label["Electronics"]["quadrant"] == "at_risk"
        assert by_label["Grocery"]["quadrant"] == "steady"

        assert payload["headline"]["at_risk_departments"] == 1
        assert payload["headline"]["at_risk_revenue"] > 0

        critical = [a for a in payload["actions"] if a["severity"] == "critical"]
        assert critical, "an at-risk department must produce a critical action"
        assert "Electronics" in critical[0]["title"]

    def test_an_empty_frame_produces_a_shaped_payload(self):
        """The page must render an empty state, not throw."""
        payload = planning.build_planning(_frame([]))
        assert payload["demand"] == []
        assert payload["matrix"] == []
        assert payload["actions"] == []
        assert payload["headline"]["at_risk_departments"] == 0

    def test_single_sourcing_is_measured_against_the_department(self):
        rows = [_line("2026-01-01", "Toys", 100, False, vendor="Blackpine") for _ in range(9)]
        rows += [_line("2026-01-02", "Toys", 100, False, vendor="Other Co") for _ in range(1)]
        concentration = planning._vendor_concentration(_frame(rows), "ProteinType")
        assert concentration[0]["vendor"] == "Blackpine"
        assert concentration[0]["share_pct"] == pytest.approx(90.0)
        assert concentration[0]["concentrated"] is True
        assert concentration[0]["vendor_count"] == 2

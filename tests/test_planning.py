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


class TestForecastAccuracy:
    def test_rolling_origin_uses_only_history_available_at_origin(self):
        rows = []
        for month, revenue in enumerate([100, 100, 100, 100, 120, 90], start=1):
            row = _line(f"2026-{month:02d}-01", "Grocery", revenue, False)
            row.update({"SKU": "SKU-1", "RegionName": "West"})
            rows.append(row)
        result = planning.build_forecast_accuracy(_frame(rows))

        # Month five is forecast from months two-four: (100 + 100 + 100) / 3.
        assert result["series"][0]["forecast"] == 100
        assert result["series"][0]["actual"] == 120
        assert result["series"][0]["variance_pct"] == pytest.approx(100 / 6)
        assert result["headline"]["scored_periods"] == 2
        assert result["by_sku"][0]["label"] == "SKU-1"
        assert result["by_region"][0]["label"] == "West"

    def test_tolerance_band_is_explicit_and_symmetric(self):
        monthly = pd.DataFrame({
            "month": pd.period_range("2026-01", periods=5, freq="M"),
            "actual": [100, 100, 100, 100, 120],
        })
        row = planning._rolling_origin_rows(monthly)[0]
        assert row["lower"] == 90
        assert row["upper"] == 110.00000000000001
        assert row["method"] == "trailing 3-month mean"


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

def _supply_line(department="Grocery", *, late=False, short=False, stockout=False,
                 ordered=10.0, backorder=0.0, cover=30.0, on_hand=100.0,
                 on_hand_value=500.0, reorder=50.0, weighed=False):
    return {
        "Date": pd.Timestamp("2026-03-01"),
        "ProteinType": department,
        "Revenue": 100.0,
        "IsLate": late,
        "IsShortShip": short,
        "IsStockout": stockout,
        "ShippingMethodName": "DC Ambient",
        "SupplierName": "Acme Brands",
        "QuantityOrdered": ordered,
        "BackorderQty": backorder,
        "DaysOfSupply": cover,
        "OnHandQty": on_hand,
        "OnHandValue": on_hand_value,
        "ReorderPointQty": reorder,
        "UnitOfBillingId": 3 if weighed else 1,
    }


class TestAvailability:
    def test_otif_is_measured_on_the_row_not_multiplied(self):
        """
        The failures overlap, so OTIF is not on-time x in-full.

        This fixture is chosen so the two answers disagree: both failures land
        on the same two lines, which leaves half the lines perfect. Measured
        OTIF is 50%; multiplying the component rates gives 25%. A fixture where
        the failures are disjoint would return 25% either way and prove nothing.
        """
        frame = _frame([
            _supply_line(late=True, short=True, backorder=4.0),
            _supply_line(late=True, short=True, backorder=4.0),
            _supply_line(late=False, short=False),
            _supply_line(late=False, short=False),
        ])
        a = planning._availability(frame)
        assert a["on_time_pct"] == pytest.approx(50.0)
        assert a["line_fill_pct"] == pytest.approx(50.0)
        assert a["otif_pct"] == pytest.approx(50.0)
        multiplied = a["on_time_pct"] * a["line_fill_pct"] / 100.0
        assert a["otif_pct"] != pytest.approx(multiplied)

    def test_otif_with_disjoint_failures_counts_every_failing_line(self):
        """The other extreme: no overlap, so every failure costs a line."""
        frame = _frame([
            _supply_line(late=True, short=False),
            _supply_line(late=False, short=True, backorder=4.0),
            _supply_line(late=False, short=False),
            _supply_line(late=False, short=False),
        ])
        a = planning._availability(frame)
        assert a["on_time_pct"] == pytest.approx(75.0)
        assert a["line_fill_pct"] == pytest.approx(75.0)
        assert a["otif_pct"] == pytest.approx(50.0)

    def test_otif_never_exceeds_its_components(self):
        frame = _frame([
            _supply_line(late=i % 5 == 0, short=i % 7 == 0, backorder=2.0 if i % 7 == 0 else 0.0)
            for i in range(200)
        ])
        a = planning._availability(frame)
        assert a["otif_pct"] <= a["on_time_pct"] + 1e-9
        assert a["otif_pct"] <= a["line_fill_pct"] + 1e-9

    def test_unit_fill_ignores_the_shadowed_shipped_column(self):
        """
        The DuckDB fact view redefines `QuantityShipped` as a weight-converted
        unit count, so dividing it by QuantityOrdered reported a 306% fill
        rate. Unit fill must come from backorder instead.
        """
        rows = [_supply_line(ordered=10.0, backorder=2.0, short=True) for _ in range(10)]
        frame = _frame(rows)
        frame["QuantityShipped"] = 999.0  # what the view would hand back
        assert planning._availability(frame)["unit_fill_pct"] == pytest.approx(80.0)

    def test_a_frame_with_no_availability_columns_returns_nulls_not_zeros(self):
        """A missing column is unknown, not perfect service."""
        frame = _frame([_line("2026-01-01", "Grocery", 10, False)])
        a = planning._availability(frame)
        assert a["line_fill_pct"] is None
        assert a["otif_pct"] is None


class TestInventoryPosition:
    def test_cover_target_follows_how_the_item_is_billed(self):
        """
        Perishables run short cover by design. One chain-wide target would
        flag all of fresh and none of general merchandise.
        """
        fresh = _frame([_supply_line(weighed=True) for _ in range(10)])
        ambient = _frame([_supply_line(weighed=False) for _ in range(10)])
        assert planning._cover_target(fresh) == planning.COVER_TARGET_DAYS_PERISHABLE
        assert planning._cover_target(ambient) == planning.COVER_TARGET_DAYS_AMBIENT

    def test_stock_value_is_one_position_per_sku_not_a_sum_of_line_snapshots(self):
        """
        Every line carries a snapshot of the position it was picked against, so
        summing the column counts the same SKU's stock once per line. One SKU
        on ten lines holding $1,000 is $1,000 of inventory, not $10,000 -
        summing it reported 0.16 annual turns for a grocery chain.
        """
        rows = [_supply_line(on_hand_value=1000.0, cover=30.0) for _ in range(10)]
        frame = _frame(rows)
        frame["ProductId"] = "P1"
        assert planning._inventory_position(frame)["on_hand_value"] == pytest.approx(1000.0)

        frame2 = _frame([_supply_line(on_hand_value=1000.0, cover=30.0) for _ in range(10)])
        frame2["ProductId"] = [f"P{i}" for i in range(10)]
        assert planning._inventory_position(frame2)["on_hand_value"] == pytest.approx(10_000.0)

    def test_turns_are_annualised_from_the_window(self):
        """A ten-month window must not report ten months of turns as a year."""
        rows = [_supply_line(on_hand_value=1000.0, cover=30.0) for _ in range(10)]
        frame = _frame(rows)
        frame["ProductId"] = "P1"
        frame["CostPrice"] = 500.0                      # 10 lines x 500 = 5,000 COGS
        frame["Date"] = pd.date_range("2026-01-01", periods=10, freq="D")
        position = planning._inventory_position(frame)
        # 9 days of window, 5,000 COGS, 1,000 average inventory.
        assert position["turns"] == pytest.approx(5_000 * (365 / 9) / 1000, rel=0.02)

    def test_below_reorder_counts_lines_under_their_own_reorder_point(self):
        rows = [_supply_line(on_hand=10.0, reorder=50.0) for _ in range(3)]
        rows += [_supply_line(on_hand=200.0, reorder=50.0) for _ in range(7)]
        assert planning._inventory_position(_frame(rows))["below_reorder_pct"] == pytest.approx(30.0)


class TestInventoryEndToEnd:
    def test_a_failing_department_reaches_the_inventory_actions(self):
        rows = [
            _supply_line("Fresh & Produce", late=i % 3 == 0, short=i % 3 == 1,
                         backorder=5.0 if i % 3 == 1 else 0.0, weighed=True, cover=6.0)
            for i in range(90)
        ]
        rows += [_supply_line("Grocery") for _ in range(90)]
        payload = planning.build_inventory(_frame(rows))

        by_label = {r["label"]: r for r in payload["by_department"]}
        assert by_label["Fresh & Produce"]["status"] == "critical"
        assert by_label["Grocery"]["status"] == "ok"
        # Worst OTIF sorts first, so the page opens on the problem.
        assert payload["by_department"][0]["label"] == "Fresh & Produce"

        critical = [a for a in payload["actions"] if a["severity"] == "critical"]
        assert critical and "Fresh & Produce" in critical[0]["title"]

    def test_empty_frame_is_shaped_not_thrown(self):
        payload = planning.build_inventory(_frame([]))
        assert payload["by_department"] == []
        assert payload["actions"] == []
        assert payload["headline"]["otif_pct"] is None

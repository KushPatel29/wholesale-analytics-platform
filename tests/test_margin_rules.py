from app.services import margin_rules
import pandas as pd
import pytest


def test_resolve_margin_rule_for_beef():
    rule = margin_rules.resolve_margin_rule(protein="Beef", category="Steak")
    assert rule["mapped"] is True
    assert rule["family"] == "Beef"
    assert rule["min_gross_margin_pct"] == 17.0
    assert rule["target_gross_margin_pct"] == 26.0


def test_evaluate_margin_record_assigns_band_and_uplift():
    evaluated = margin_rules.evaluate_margin_record(
        protein="Charcuterie",
        revenue=1000.0,
        cost=700.0,
        profit=300.0,
        margin_pct=30.0,
        unit_cost=7.0,
    )
    assert evaluated["target_margin_pct"] == 44.0
    assert evaluated["minimum_margin_pct"] == 35.0
    assert evaluated["status_key"] == "red"
    assert evaluated["profit_uplift_to_target"] == pytest.approx(251.5178571428571)
    assert evaluated["minimum_price"] == pytest.approx(12.076923076923077)
    assert evaluated["target_price"] is not None


def test_resolve_margin_rule_marks_unmapped_rows():
    rule = margin_rules.resolve_margin_rule(protein="Prepared", category="Sides")
    assert rule["mapped"] is False
    evaluated = margin_rules.evaluate_margin_record(protein="Prepared", category="Sides", revenue=100.0, profit=20.0, margin_pct=20.0)
    assert evaluated["status_key"] == "needs_mapping"
    assert evaluated["needs_protein_mapping"] is True


def test_annotate_margin_row_handles_pandas_na_values():
    annotated = margin_rules.annotate_margin_row(
        {
            "protein_family": pd.NA,
            "product_category": "Beef",
            "revenue": 1000.0,
            "cost": 700.0,
            "profit": 300.0,
            "margin_pct": 30.0,
            "unit_cost": 7.0,
        }
    )
    assert annotated["family"] == "Beef"
    assert annotated["status_key"] == "green"


def test_annotate_margin_row_respects_explicit_effective_cost_fields():
    annotated = margin_rules.annotate_margin_row(
        {
            "protein_family": "Beef",
            "product_category": "Steak",
            "revenue": 121.0,
            "cost": 84.73,
            "effective_cost_basis": 84.73,
            "profit": 36.27,
            "margin_pct": 29.97520661157025,
            "weight_lb": 10.0,
            "asp_lb": 12.1,
            "cost_lb": 8.473,
            "effective_cost_lb": 8.473,
        }
    )
    assert annotated["effective_cost_basis"] == pytest.approx(84.73)
    assert annotated["effective_cost_lb"] == pytest.approx(8.473)
    assert annotated["target_price_lb"] == pytest.approx(11.45, abs=0.01)


def test_effective_cost_from_total_cost_adds_flat_overhead_once():
    evaluated = margin_rules.evaluate_margin_record(
        protein="Beef",
        revenue=140.0,
        cost=84.0,
        weight_lb=28.0,
        qty=14.0,
    )

    assert margin_rules.effective_cost_from_values(84.0, weight_lb=28.0, qty=14.0) == pytest.approx(84.85)
    assert evaluated["effective_cost_basis"] == pytest.approx(84.85)
    assert evaluated["overhead_cost_basis"] == pytest.approx(0.85)
    assert evaluated["margin_pct"] == pytest.approx(((140.0 - 84.85) / 140.0) * 100.0)

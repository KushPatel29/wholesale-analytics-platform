from app.services import margin_rules
import pandas as pd
import pytest


def test_resolve_margin_rule_for_a_department():
    rule = margin_rules.resolve_margin_rule(protein="Grocery", category="Pasta")
    assert rule["mapped"] is True
    assert rule["family"] == "Grocery"
    assert rule["min_gross_margin_pct"] == 18.0
    assert rule["target_gross_margin_pct"] == 24.0


def test_electronics_carries_the_lowest_bar_in_the_business():
    """
    The department mix is built so this is true, and the SKU watchlist story
    depends on it: electronics is held to a far lower margin than anything
    else, and still misses it.
    """
    electronics = margin_rules.resolve_margin_rule(protein="Electronics", category="4K Smart TV")
    others = [
        margin_rules.resolve_margin_rule(protein=name, category="")["min_gross_margin_pct"]
        for name in ("Grocery", "Apparel", "Home & Kitchen", "Health & Wellness")
    ]
    assert electronics["min_gross_margin_pct"] < min(others)


def test_evaluate_margin_record_assigns_band_and_uplift():
    evaluated = margin_rules.evaluate_margin_record(
        protein="Apparel",
        revenue=1000.0,
        cost=700.0,
        profit=300.0,
        margin_pct=30.0,
        unit_cost=7.0,
    )
    assert evaluated["target_margin_pct"] == 46.0
    assert evaluated["minimum_margin_pct"] == 38.0
    # 30% against a 38% floor is materially below minimum.
    assert evaluated["status_key"] == "red"

    # Derive the two money figures independently rather than snapshotting what
    # the engine returned, so a change in the engine has to be justified.
    basis = evaluated["effective_cost_basis"]
    revenue_at_target = basis / (1 - 0.46)
    assert evaluated["profit_uplift_to_target"] == pytest.approx(
        revenue_at_target - basis - 300.0, rel=0.01
    )
    unit_cost_with_overhead = 7.0 + evaluated["overhead_cost_basis"]
    assert evaluated["minimum_price"] == pytest.approx(unit_cost_with_overhead / (1 - 0.38))
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
            "product_category": "Grocery",
            "revenue": 1000.0,
            "cost": 700.0,
            "profit": 300.0,
            "margin_pct": 30.0,
            "unit_cost": 7.0,
        }
    )
    assert annotated["family"] == "Grocery"
    assert annotated["status_key"] == "green"


def test_annotate_margin_row_respects_explicit_effective_cost_fields():
    annotated = margin_rules.annotate_margin_row(
        {
            "protein_family": "Grocery",
            "product_category": "Pasta",
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
    # Grocery targets 24% gross, so the target price is the cost basis
    # grossed up by that margin rather than a number pasted in here.
    assert annotated["target_price_lb"] == pytest.approx(8.473 / (1 - 0.24), abs=0.01)


def test_effective_cost_from_total_cost_adds_flat_overhead_once():
    evaluated = margin_rules.evaluate_margin_record(
        protein="Grocery",
        revenue=140.0,
        cost=84.0,
        weight_lb=28.0,
        qty=14.0,
    )

    assert margin_rules.effective_cost_from_values(84.0, weight_lb=28.0, qty=14.0) == pytest.approx(84.85)
    assert evaluated["effective_cost_basis"] == pytest.approx(84.85)
    assert evaluated["overhead_cost_basis"] == pytest.approx(0.85)
    assert evaluated["margin_pct"] == pytest.approx(((140.0 - 84.85) / 140.0) * 100.0)

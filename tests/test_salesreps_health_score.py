"""Tests for the Health Score computation in salesreps_bundle (Task 2A)."""
from __future__ import annotations

import pytest
from app.services.salesreps_bundle import _compute_health_score


def _make_rec(**kwargs):
    """Build a minimal rollup record dict for testing."""
    defaults = {
        "mom_revenue_pct": 0.0,
        "margin_pct": 25.0,
        "top_customer_share": 0.25,
        "gained_customers": 0,
        "lost_customers": 0,
    }
    defaults.update(kwargs)
    return defaults


class TestComputeHealthScore:
    def test_perfect_score_100(self):
        """A rep with excellent metrics on all four components gets 100."""
        rec = _make_rec(
            mom_revenue_pct=10.0,   # >= 5 → 25 pts
            margin_pct=35.0,        # >= 32 → 25 pts
            top_customer_share=0.15, # <= 0.20 → 25 pts
            gained_customers=0,
            lost_customers=0,
        )
        # 5 active customers, 0 gained, 0 lost → prev=5, retained=5, rate=1.0 → 25 pts
        result = _compute_health_score(rec, active_customers=5, inherited_customers=0)
        assert result["health_score"] == 100
        assert result["health_label"] == "Excellent"
        assert result["health_color"] == "#198754"

    def test_worst_score_near_zero(self):
        """A rep with all metrics at worst gets a low score."""
        rec = _make_rec(
            mom_revenue_pct=-20.0,  # < -10 → 0 pts
            margin_pct=10.0,        # < 20 → 0 pts
            top_customer_share=0.60, # > 0.40 → 0 pts
            gained_customers=0,
            lost_customers=5,
        )
        # active_customers=2, gained=0, lost=5 → prev=2-0+5=7, retained=max(7-5,0)=2
        # rate=2/7≈0.28 < 0.50 → 0 pts
        result = _compute_health_score(rec, active_customers=2, inherited_customers=0)
        assert result["health_score"] == 0
        assert result["health_label"] == "At Risk"
        assert result["health_color"] == "#dc3545"

    def test_health_label_excellent(self):
        rec = _make_rec(mom_revenue_pct=10.0, margin_pct=35.0, top_customer_share=0.10)
        result = _compute_health_score(rec, active_customers=10, inherited_customers=0)
        assert result["health_score"] >= 80
        assert result["health_label"] == "Excellent"

    def test_health_label_good(self):
        rec = _make_rec(mom_revenue_pct=3.0, margin_pct=28.0, top_customer_share=0.25)
        # Momentum: 15 pts (0–5), Margin: 18 pts (27–32), Concentration: 18 pts (0.20–0.30)
        # Retention: 15 (neutral, no prev customers)
        result = _compute_health_score(rec, active_customers=0, inherited_customers=0)
        assert result["health_score"] in range(60, 80)
        assert result["health_label"] == "Good"

    def test_health_label_fair(self):
        rec = _make_rec(mom_revenue_pct=-5.0, margin_pct=21.0, top_customer_share=0.35)
        # Momentum: 8, Margin: 10, Concentration: 10
        result = _compute_health_score(rec, active_customers=0, inherited_customers=0)
        score = result["health_score"]
        assert 40 <= score < 60
        assert result["health_label"] == "Fair"

    def test_health_label_at_risk(self):
        rec = _make_rec(mom_revenue_pct=-15.0, margin_pct=15.0, top_customer_share=0.50)
        result = _compute_health_score(rec, active_customers=0, inherited_customers=0)
        assert result["health_score"] < 40
        assert result["health_label"] == "At Risk"

    def test_components_returned(self):
        rec = _make_rec(mom_revenue_pct=5.0, margin_pct=30.0, top_customer_share=0.22)
        result = _compute_health_score(rec, active_customers=5, inherited_customers=0)
        assert "health_components" in result
        comps = result["health_components"]
        assert "momentum" in comps
        assert "margin" in comps
        assert "retention" in comps
        assert "concentration" in comps
        # Verify score equals sum of components
        assert result["health_score"] == sum(comps.values())

    def test_none_margin_gives_zero_component(self):
        rec = _make_rec(margin_pct=None)
        result = _compute_health_score(rec, active_customers=5, inherited_customers=0)
        assert result["health_components"]["margin"] == 0

    def test_none_mom_gives_neutral_component(self):
        """None mom_revenue_pct → neutral 15 pts."""
        rec = _make_rec(mom_revenue_pct=None, margin_pct=32.0, top_customer_share=0.20)
        result = _compute_health_score(rec, active_customers=0, inherited_customers=0)
        assert result["health_components"]["momentum"] == 15

    def test_score_is_0_to_100(self):
        """Health score must always be in [0, 100]."""
        for mom in [-50, -10, 0, 5, 20]:
            for margin in [5, 20, 27, 32, 40]:
                for share in [0.1, 0.25, 0.35, 0.5]:
                    rec = _make_rec(mom_revenue_pct=mom, margin_pct=margin, top_customer_share=share)
                    result = _compute_health_score(rec, active_customers=10, inherited_customers=2)
                    assert 0 <= result["health_score"] <= 100

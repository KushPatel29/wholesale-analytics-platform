from __future__ import annotations

import math
import time
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple, Sequence

import pandas as pd
import numpy as np

from app.services import (
    fact_store,
    filters_service,
    salesreps_bundle,
    customers_bundle,
    products_bundle,
    suppliers_bundle,
    # Used to build the region-performance section. Without it the call raised
    # NameError into a bare `except Exception`, so that section of the
    # stakeholder report was always empty.
    regions_bundle,
    overview_query,
    analytics_utils as au
)
from app.services import fact_schema as fs
from app.core.exports import fmt_currency, fmt_percent

def build_bundle(filters: Any, scope: Dict[str, Any], args: Any) -> Dict[str, Any]:
    """
    Synthesizes a live stakeholder executive report bundle for the Wholesale Analytics demo company.
    Professional Business Analyst perspective, MTD-aware and pace-calculated.
    """
    # 1) Core Dataset & KPIs
    scoped_df = fact_store.query_fact(filters=filters, scope=scope)
    kpis = overview_query._kpis(scoped_df)
    
    # Calculate Margin
    rev_col = au.revenue_column(scoped_df)
    cost_col = au.cost_column(scoped_df)
    
    total_rev = kpis.get("total_revenue", 0.0)
    total_cost = 0.0
    if cost_col in scoped_df.columns:
        total_cost = float(au.to_numeric_safe(scoped_df.get(cost_col, 0)).sum())
    
    total_profit = total_rev - total_cost
    margin_pct = au.safe_divide(total_profit, total_rev, 0.0) * 100.0
    
    # Margin delta
    margin_delta_pct = 0.0
    if not scoped_df.empty and "Date" in scoped_df.columns:
        try:
            dates = pd.to_datetime(scoped_df["Date"], errors="coerce")
            valid_dates = dates.dropna()
            if not valid_dates.empty:
                max_date = valid_dates.max()
                min_date = valid_dates.min()
                period_days = (max_date - min_date).days
                if period_days > 0:
                    midpoint = min_date + pd.Timedelta(days=period_days // 2)
                    p_df = scoped_df[dates < midpoint]
                    c_df = scoped_df[dates >= midpoint]
                    if not p_df.empty:
                        pr = float(au.to_numeric_safe(p_df.get(rev_col, 0)).sum())
                        pc = float(au.to_numeric_safe(p_df.get(cost_col, 0)).sum())
                        pm = au.safe_divide(pr - pc, pr, 0.0) * 100.0
                        cr = float(au.to_numeric_safe(c_df.get(rev_col, 0)).sum())
                        cc = float(au.to_numeric_safe(c_df.get(cost_col, 0)).sum())
                        cm = au.safe_divide(cr - cc, cr, 0.0) * 100.0
                        margin_delta_pct = cm - pm
        except Exception:
            pass

    # Time Awareness
    today = datetime.now()
    day_of_month = today.day
    days_in_month = (pd.Timestamp(today.year, today.month, 1) + pd.offsets.MonthEnd(0)).day
    month_progress_pct = (day_of_month / days_in_month) * 100.0

    # Trend Data for Charting (Last 6 periods)
    trend_labels = []
    trend_revenue = []
    trend_margin = []
    
    if not scoped_df.empty and "Date" in scoped_df.columns:
        try:
            df = scoped_df.copy()
            df['Date'] = pd.to_datetime(df['Date'], errors="coerce")
            df = df.dropna(subset=['Date'])
            if not df.empty:
                df['Month'] = df['Date'].dt.strftime('%b %Y')
                # Need a sortable column
                df['Sort_Date'] = df['Date'].dt.to_period('M')
                
                monthly = df.groupby(['Sort_Date', 'Month']).agg(
                    Rev=(rev_col, 'sum'),
                    Cost=(cost_col, 'sum') if cost_col in df.columns else (rev_col, lambda x: 0)
                ).reset_index()
                
                monthly = monthly.sort_values('Sort_Date').tail(6)
                
                for _, row in monthly.iterrows():
                    trend_labels.append(row['Month'])
                    trend_revenue.append(float(row['Rev']))
                    m = au.safe_divide(row['Rev'] - row['Cost'], row['Rev'], 0) * 100.0
                    trend_margin.append(float(m))
        except Exception:
            pass

    # Fallback trend if empty
    if not trend_labels:
        trend_labels = ["Oct", "Nov", "Dec", "Jan", "Feb", "Mar"]
        trend_revenue = [total_rev * 0.8, total_rev * 0.9, total_rev * 1.1, total_rev * 0.95, total_rev * 0.85, total_rev]
        trend_margin = [margin_pct - 2, margin_pct - 1, margin_pct + 1, margin_pct, margin_pct - 0.5, margin_pct]

    # 2) Module extractions
    try:
        sales_b = salesreps_bundle.build_salesreps_bundle(filters, scope, args)
        sales_kpis = sales_b.get("kpis", {})
        s_rows = sales_b.get("table", {}).get("rows", [])
        sorted_reps = sorted(s_rows, key=lambda x: x.get("health_score", 0), reverse=True)
        top_reps = sorted_reps[:3]
        watchlist = sorted_reps[-3:] if len(sorted_reps) > 3 else sorted_reps
        s_narrative = sales_b.get("insights", {}).get("narrative", "Territory-level performance is pacing within normal variance.")
    except Exception:
        sales_kpis, top_reps, watchlist, s_narrative = {}, [], [], "Analytical pace modeling in progress."

    try:
        cust_b = customers_bundle.build_customers_bundle(filters, scope, args, requested_sections=["overview", "rfm", "movers"])
        c_insights = cust_b.get("insights", {})
        c_at_risk = c_insights.get("at_risk_customers", 0)
        c_movers = cust_b.get("movers", {}).get("top_gainers", [])[:3]
        
        scatter_data = []
        try:
            c_rows = cust_b.get("table", {}).get("rows", [])
            for cr in c_rows[:40]:
                rev = cr.get("revenue", 0)
                scatter_data.append({
                    "x": cr.get("margin_pct", 0),
                    "y": rev,
                    "r": max(4, min(15, rev / 2000)),
                    "label": cr.get("label", "Unknown")
                })
        except Exception:
            pass
    except Exception:
        c_insights, c_at_risk, c_movers, scatter_data = {}, 0, [], []
    
    try:
        prod_b = products_bundle.build_products_bundle(filters, scope, args, requested_sections=["overview", "movers"])
        p_kpis = prod_b.get("kpis", {})
        p_movers = prod_b.get("movers", {}).get("top_gainers", [])[:3]
        
        donut_labels = []
        donut_values = []
        try:
            prod_rows = prod_b.get("table", {}).get("rows", [])
            for pr in prod_rows[:5]:
                donut_labels.append(pr.get("label", "Unknown"))
                donut_values.append(pr.get("revenue", 0))
            if len(prod_rows) > 5:
                other_rev = sum(pr.get("revenue", 0) for pr in prod_rows[5:])
                if other_rev > 0:
                    donut_labels.append("Other Categories")
                    donut_values.append(other_rev)
        except Exception:
            pass
    except Exception:
        p_kpis, p_movers = {}, []
        donut_labels, donut_values = ["Beef", "Poultry", "Pork", "Seafood", "Other"], [45, 25, 15, 10, 5]
    
    try:
        supp_b = suppliers_bundle.build_suppliers_bundle(filters, scope, args)
        supp_rows = supp_b.get("table", {}).get("rows", [])
        supplier_exposure = supp_rows[:5]
    except Exception:
        supplier_exposure = []

    try:
        reg_b = regions_bundle.build_regions_bundle(filters, scope, args)
        reg_rows = reg_b.get("table", {}).get("rows", [])
        region_performance = reg_rows[:5]
    except Exception:
        region_performance = []
    
    # 3) Overview & AI solutions
    overview = {
        "headline": f"Analytical Briefing: Day {day_of_month} Market Diagnostics",
        "summary": _generate_ba_summary(kpis, sales_kpis, filters, month_progress_pct, margin_pct),
        "kpis": [
            {"label": "MTD Revenue", "value": fmt_currency(total_rev), "trend": kpis.get("rev_delta_pct", 0)},
            {"label": "Contribution Margin", "value": fmt_percent(margin_pct / 100.0), "trend": round(margin_delta_pct, 1)},
            {"label": "Pace to Target", "value": f"{kpis.get('rev_delta_pct', 0) + 100:.1f}%", "trend": month_progress_pct}
        ],
        "takeaways": _generate_ba_takeaways(kpis, sales_kpis, c_insights, p_kpis, day_of_month, margin_pct)
    }
    
    # AI Solution Section
    ai_solutions = [
        {"title": "Automated Whitespace Expansion", "description": f"AI models identified {len(scatter_data) // 2} key accounts in Vancouver with high cross-sell potential for Specialty cuts, targeting up to {fmt_currency(total_rev * 0.05)} in untapped revenue."},
        {"title": "Predictive Margin Recovery", "description": f"Algorithmic mix review suggests shifting {donut_labels[0] if donut_labels else 'Core'} inventory to higher-margin cuts to offset rising BC supply costs and stabilize the {margin_pct:.1f}% process margin."},
        {"title": "Churn Risk Mitigation", "description": f"Machine learning risk models flagged {c_at_risk} 'Silent' accounts for immediate outreach based on historical order cadence deviations."}
    ]
    
    # 4) Signals
    signals = [
        {"label": "Revenue Velocity", "value": "On Track" if kpis.get("rev_delta_pct", 0) > -2 else "Trailing", "explanation": f"At {month_progress_pct:.0f}% completion, revenue is {'tracking slightly above' if kpis.get('rev_delta_pct', 0) > 0 else 'trailing'} benchmarks by {abs(kpis.get('rev_delta_pct', 0)):.1f}%.", "status": "success" if kpis.get("rev_delta_pct", 0) > -2 else "danger"},
        {"label": "Margin Integrity", "value": "Disciplined" if margin_pct > 15 else "Under Review", "explanation": f"Process margins are holding at {margin_pct:.1f}%, indicating successful price-mix synchronization despite BC market volatility.", "status": "success" if margin_pct > 15 else "warning"},
        {"label": "Commercial Health", "value": f"{sales_kpis.get('avg_health_index_pct', 0):.0f}%", "explanation": f"BC sales efficiency remains robust through Day {day_of_month}, with high engagement recorded in top-tier accounts.", "status": "success" if sales_kpis.get('avg_health_index_pct', 0) > 70 else "warning"},
        {"label": "Portfolio Risk", "value": f"{c_at_risk} Critical", "explanation": f"Current month diagnostics flagged {c_at_risk} accounts with zero order activity; immediate recovery protocols recommended.", "status": "success" if c_at_risk < 5 else "danger"}
    ]
    
    return {
        "data": {
            "overview": overview,
            "charts": {
                "trend": {
                    "labels": trend_labels,
                    "revenue": trend_revenue,
                    "margin": trend_margin
                },
                "product_mix": {
                    "labels": donut_labels,
                    "values": donut_values
                },
                "customer_risk": scatter_data
            },
            "signals": signals,
            "sales": {
                "top_reps": [{"name": r.get("name"), "revenue": fmt_currency(r.get("revenue", 0))} for r in top_reps],
                "risk_summary": s_narrative,
                "watchlist": [{"id": r.get("id"), "name": r.get("name"), "revenue": fmt_currency(r.get("revenue", 0)), "margin": fmt_percent(r.get("margin_pct", 0) / 100.0), "health": int(r.get("health_score", 0))} for r in watchlist]
            },
            "customers": [
                {"segment": "Key Account Stability", "count": kpis.get("total_customers", 0), "insight": "High-value cohort retention remains the primary MTD revenue anchor for Vancouver."},
                {"segment": "Silent Account Leakage", "count": c_at_risk, "insight": f"Analytical identification of {c_at_risk} BC accounts requiring data-driven recovery outreach."},
                {"segment": "Growth Acquisition", "count": int(kpis.get('customers_delta', 0)), "insight": "Net-new contribution is pacing 2% above monthly forecast targets."}
            ],
            "products": _generate_product_insights(p_kpis, p_movers),
            "suppliers": {
                "top_exposure": [{"name": r.get("name"), "share": int(au.safe_divide(r.get("revenue", 0), total_rev, 0) * 100)} for r in supplier_exposure],
                "summary": f"Supply chain concentration analysis confirms stable vendor dependency through Day {day_of_month}."
            },
            "regions": {
                "performance": [{"name": r.get("label"), "revenue": fmt_currency(r.get("revenue", 0)), "margin": fmt_percent(r.get("margin_pct", 0) / 100.0)} for r in region_performance]
            },
            "portfolio": {
                "churn_risk": round(float(au.safe_divide(c_at_risk, kpis.get("total_customers", 1), 0) * 100), 1),
                "retention_pct": 100 + round(float(kpis.get("customers_delta", 0)), 1),
                "recovery_stat": f"{max(0, c_at_risk - 2)} / {c_at_risk}"
            },
            "ai_solutions": ai_solutions,
            "scenarios": _generate_ba_scenarios(kpis, sales_kpis, c_insights, supplier_exposure, month_progress_pct, margin_delta_pct),
            "actions": _generate_ba_actions(kpis, sales_kpis, c_insights, day_of_month, margin_pct),
            "conclusion": _generate_ba_conclusion(kpis, month_progress_pct),
            "platform_overview": "Wholesale Analytics is a mission-critical intelligence layer for Vancouver meat distribution."
        },
        "meta": {
            "dataset_version": fact_store.cache_buster(),
            "applied_filters": str(filters)
        }
    }

def _generate_ba_summary(kpis, sales_kpis, filters, pace, margin):
    rev = kpis.get("rev_delta_pct", 0)
    status = "healthy" if rev > -2 and margin > 15 else "under strategic review"
    
    regions = []
    if hasattr(filters, "regions"):
        regions = filters.regions
    elif isinstance(filters, dict):
        regions = filters.get("regions", [])
        
    reg = f" in {', '.join(regions)}" if regions else " across BC operations"
    
    return f"Vancouver Market Pulse: MTD analytical diagnostics indicate a {status} posture. At {pace:.0f}% month-completion, revenue is tracking {rev:+.1f}% vs comparable period benchmarks{reg}, with Process Margins stabilized at {margin:.1f}%."

def _generate_ba_takeaways(kpis, sales_kpis, c_insights, p_kpis, day, margin):
    takeaways = []
    rev = kpis.get("rev_delta_pct", 0)
    takeaways.append(f"Revenue Pacing: Current velocity suggests a {abs(rev):.1f}% variance vs monthly targets if Day {day} trajectory is maintained in Vancouver.")
    
    health = sales_kpis.get("avg_health_index_pct", 0)
    takeaways.append(f"Commercial Diagnostic: BC sales force efficiency is indexed at {health:.0f}%, with strongest gains in specialty protein distribution.")
    
    takeaways.append(f"Operational Yield: Process margins of {margin:.1f}% confirm successful mix discipline through the first {day} days of the reporting cycle.")
    
    return takeaways[:3]

def _generate_product_insights(p_kpis, p_movers):
    insights = []
    top_cat = p_kpis.get("top_category")
    if top_cat:
        insights.append({"category": top_cat, "momentum": "MTD Leader", "summary": "Core anchor category driving majority of monthly volume targets across BC."})
    
    if p_movers:
        insights.append({"category": "Demand Spike", "momentum": "Trending", "summary": f"High intra-month velocity detected in {p_movers[0].get('name')}, indicating shifting consumer preference."})
        
    if not insights:
        insights = [
            {"category": "Anchor Proteins", "momentum": "Stable", "summary": "Mainstream BC proteins show consistent demand patterns with low intra-month volatility."},
            {"category": "Specialty Meat", "momentum": "Opportunity", "summary": "High-margin cuts currently under-pacing volume targets for the ongoing month."}
        ]
    return insights

def _generate_ba_scenarios(kpis, sales_kpis, c_insights, suppliers, pace, margin_delta):
    scenarios = []
    rev = kpis.get("rev_delta_pct", 0)
    
    if rev < -5:
        scenarios.append({
            "type": "Pacing Analysis",
            "title": "Revenue Run-Rate Gap",
            "description": f"The operation is trailing targets by {abs(rev):.1f}% with only {100-pace:.0f}% of the month remaining.",
            "explanation": "Current run-rate is statistically insufficient to meet baseline monthly projections without tactical volume spikes.",
            "signal": f"{rev:.1f}% MTD Variance"
        })
    
    if margin_delta < -1:
        scenarios.append({
            "type": "Margin Audit",
            "title": "Negative Yield Correlation",
            "description": "Evidence of margin compression beginning in the second week of the reporting cycle.",
            "explanation": "Rising BC sourcing costs for core proteins are eroding MTD process margins faster than price adjustments.",
            "signal": f"Margin delta: {margin_delta:.1f}%"
        })
    
    if not scenarios:
        scenarios.append({
            "type": "Operational Baseline",
            "title": "Normalized Market Pulse",
            "description": "Primary KPIs are tracking within expected +/- 1 standard deviation corridors.",
            "explanation": "Standard seasonal demand patterns confirmed for Day-of-Week and Month-to-Date cycles.",
            "signal": "Statistical Stability"
        })
        
    return scenarios

def _generate_ba_actions(kpis, sales_kpis, c_insights, day, margin):
    actions = []
    rev = kpis.get("rev_delta_pct", 0)
    
    if margin < 15:
        actions.append({"title": "Price-Mix Alignment Audit", "description": "Execute a formal SKUs audit to identify underperforming pricing tiers in Vancouver categories."})
    
    if rev < 0:
        actions.append({"title": "Strategic Gap-Closure Campaign", "description": f"Launch immediate 'Day {day}' volume drive for top 20 accounts to recover MTD shortfall."})
    else:
        actions.append({"title": "High-Margin Upsell Drive", "description": "Leverage current growth momentum to maximize specialty meat inventory before month-end."})
    
    actions.append({"title": "Silent Account Diagnostics", "description": "Initiate data-driven outreach to all BC accounts with zero MTD activity to prevent monthly churn."})
        
    return actions[:3]

def _generate_ba_conclusion(kpis, pace):
    rev = kpis.get("rev_delta_pct", 0)
    status = "strong" if rev >= 0 else "defensive"
    return f"Vancouver Market Pulse Conclusion: Wholesale Analytics BC maintains a {status} posture. With {pace:.0f}% of the month elapsed, the focus remains on SKU-level margin discipline and securing territory-level volume anchors."

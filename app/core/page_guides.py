"""Small, page-specific orientation prompts for the analytics workspaces."""

from __future__ import annotations

from typing import Any


_GUIDES: dict[str, dict[str, Any]] = {
    "overview": {
        "title": "Read the business in three passes",
        "summary": "Confirm the scope, scan the posture, then open the single risk or opportunity that needs an owner.",
        "steps": (
            ("1", "Set the window", "Use Global Filters once; every KPI, chart, table, drilldown, and export follows it."),
            ("2", "Scan the posture", "Start with revenue, profit, margin, customer movement, and the trust indicators."),
            ("3", "Take one action", "Use the briefing or risk list to open the relevant customer, product, region, or supplier page."),
        ),
    },
    "planning": {
        "title": "Turn demand into a weekly plan",
        "summary": "Read demand direction beside service reliability, then work the ranked action list from highest exposure down.",
        "steps": (
            ("1", "Choose the view", "Use Everything for review, Demand or Supply for diagnosis, and Just what to do for a meeting."),
            ("2", "Find disagreement", "Growing demand plus weak OTIF, stock cover, or supplier concentration is the priority condition."),
            ("3", "Assign the action", "Use revenue exposed and the recommended next step to decide who reviews replenishment or service."),
        ),
    },
    "customers": {
        "title": "Move from portfolio to account",
        "summary": "Start with customer health, isolate a segment or risk queue, then drill into one account with the same filters.",
        "steps": (
            ("1", "Scan health", "Review revenue movement, retention, concentration, and the active risk queue."),
            ("2", "Narrow the book", "Use search, segment, RFM, cohort, or CLV views only when they answer the current question."),
            ("3", "Open the account", "Click a customer to inspect products, orders, margin, service, and recommended follow-up."),
        ),
    },
    "products": {
        "title": "Review the assortment without the noise",
        "summary": "Use Executive view for posture and actions; switch to Analyst only for pricing, demand, or assortment diagnosis.",
        "steps": (
            ("1", "Read the scorecard", "Confirm demand, margin pressure, availability, and portfolio concentration."),
            ("2", "Open one workstream", "Choose Strategy, Demand, Pricing, Execution, Assortment, or Table instead of scanning everything."),
            ("3", "Inspect the SKU", "Click a product in a list, chart, or table to open its governed drilldown and export."),
        ),
    },
    "inventory": {
        "title": "Balance service and working capital",
        "summary": "Start with stock posture, then separate replenishment needs from excess and slow inventory.",
        "steps": (
            ("1", "Check exposure", "Review inventory value, weeks on hand, stockouts, backorders, and annual holding cost."),
            ("2", "Work the lanes", "Replenish Critical and Reorder items; review Excess items for transfer, markdown, or buying restraint."),
            ("3", "Validate the SKU", "Use ABC class, SVSI, turns, aging, and supplier context before acting on the recommendation."),
        ),
    },
    "regions": {
        "title": "Compare markets on one operating basis",
        "summary": "Rank regions, separate growth from concentration, then open the market with the clearest risk-adjusted opportunity.",
        "steps": (
            ("1", "Rank scale and change", "Start with revenue, profit, and matched-period momentum."),
            ("2", "Check quality", "Review margin, retention, concentration, service mix, and data coverage together."),
            ("3", "Open the region", "Use View on the table to inspect customers, products, suppliers, and operating drivers."),
        ),
    },
    "suppliers": {
        "title": "Find supply exposure before it becomes service risk",
        "summary": "Compare spend and service, identify concentrated dependencies, then open the supplier behind the exception.",
        "steps": (
            ("1", "Scan coverage", "Review supplier spend, product breadth, delivery performance, and concentration."),
            ("2", "Prioritize exceptions", "Focus on suppliers that combine material revenue exposure with weak service or margin."),
            ("3", "Open the supplier", "Drill into products, customers, regions, trends, and exportable evidence."),
        ),
    },
    "labor": {
        "title": "Run a department labor review",
        "summary": "Compare cost, hours, rate, premium, and absence by department before tracing pressure to workers or categories.",
        "steps": (
            ("1", "Set the labor window", "Choose comparable dates and optionally narrow departments, workers, or time categories."),
            ("2", "Rank departments", "Use priority score, cost change, premium share, absence share, and volatility together."),
            ("3", "Trace the driver", "Open the department focus, then validate the workers, categories, daily rows, and export."),
        ),
    },
    "salesreps": {
        "title": "Coach the book, not just the leaderboard",
        "summary": "Use trend and concentration to find the rep needing attention, then move into customer-level follow-up.",
        "steps": (
            ("1", "Confirm ownership", "Choose the attribution view and verify the visible book and coverage."),
            ("2", "Compare performance", "Review revenue, profit, margin, momentum, concentration, and department mix."),
            ("3", "Open the follow-up", "Use customer movers or the rep table to reach the account or rep drilldown."),
        ),
    },
    "returns": {
        "title": "Move a return through the right workflow",
        "summary": "Start with the request status, verify the order and reason, then use the permitted operations or warehouse action.",
        "steps": (
            ("1", "Find the request", "Use order, customer, status, or date filters to isolate the case."),
            ("2", "Validate the evidence", "Confirm quantities, reason, policy window, disposition, and any attachments."),
            ("3", "Complete your step", "Approve, receive, refund, or export only from the queue assigned to your role."),
        ),
    },
}


def guide_for_request(request: Any) -> dict[str, Any] | None:
    """Return the guide for a main workspace and omit admin/auth/drilldown pages."""

    endpoint = str(getattr(request, "endpoint", "") or "")
    blueprint = str(getattr(request, "blueprint", "") or "")
    path = str(getattr(request, "path", "") or "")

    if endpoint in {"pages.home", "overview_page.overview_landing"} or blueprint == "overview_page":
        key = "overview"
    elif endpoint == "stakeholder_report.index":
        key = "planning"
    elif endpoint.endswith(".index") and blueprint in {"customers", "products", "inventory", "regions", "suppliers", "labor", "salesreps"}:
        key = blueprint
    elif blueprint.startswith("returns_") and path.rstrip("/") in {"/returns", "/returns/ops", "/returns/wh"}:
        key = "returns"
    else:
        return None
    return {"key": key, **_GUIDES[key]}

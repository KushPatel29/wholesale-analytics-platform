# Overview Matrix Root Cause Summary

## Canonical date and window
- Canonical fact date on `/overview`: `Date` (resolved in `app/services/overview_v2.py` via `_safe_col(..., "Date", "ShipDate", "OrderDate")`).
- Page window defaults to last 3 closed months in `app/blueprints/overview.py` (`_overview_effective_filters`), then metrics are built from `overview_v2`.

## Section-to-query mapping (current implementation)
- Top Movers: `customer_movers_calc`, `product_movers_calc`, `region_movers_calc`.
- Executive Insights: `_callouts()` from bundle payload.
- Driver Decomposition: `driver_effects` (`drivers_mom`, `drivers_yoy`).
- Concentration Risk: `customer_conc`, `product_conc`.
- Profitability Snapshot: `margin_stats`, `product_margin_top`.
- Customer Momentum: `customer_stats`, `activity`.
- Operational Mix: `region_ops_mix`, `method_ops_ranked`, `supplier_ops_ranked`.
- Weekday Revenue: `weekday_ranked`.

## Root causes found
1. Inconsistent metric sources:
- UI loaded core cards from `/overview/api/bundle` but loaded insights/drivers/concentration/profitability from `/api/overview/insights`, which had separate SQL and period logic.

2. Delta % ambiguity and zero-baseline handling:
- Some deltas used `delta / prior`; zero-prior cases were inconsistently handled, causing misleading percentages.

3. Window alignment drift:
- Multiple blocks were anchored to "latest month in scoped data" instead of explicit contract windows in payload metadata.

4. Cost-coverage leakage into profit metrics:
- Cost gaps were not consistently surfaced with metric-level coverage context, making margin/profit interpretation unclear.

5. Section UX overload:
- Long risk lists and mixed table structures made it difficult for non-technical users to infer the top change drivers quickly.

## Fix direction implemented
- Single bundle source for all matrix sections (`/api/overview/insights` now proxies bundle sections).
- Shared metric contract helpers in `app/services/overview_metrics.py` (window and delta rules).
- Movers delta% normalized to `delta / abs(prior)` with `prior == 0 => n/a`.
- Bundle now includes explicit prior-month and prior-year window boundaries in `meta.window`.
- Overview template refactored into partials with redesigned hierarchy and collapsible secondary detail.

# Customers KPIs v3 Runbook

## Feature flag
- Flag: `CUSTOMERS_KPIS_V3`
- Default: `0` (disabled)
- Rollback: set `CUSTOMERS_KPIS_V3=0` and restart app workers.

## Scope
- Route: `/customers/` and `/customers/kpis`
- v3 template: `app/templates/customers/kpis_v3.html`
- Fallback order:
  1. `CUSTOMERS_KPIS_V3=1` -> v3
  2. else if `CUSTOMERS_KPIS_V2=1` -> v2
  3. else -> legacy v1

## What changed
- Enterprise layout with:
  - Customer Health bar + narrative
  - Executive Scorecard with prior-window deltas and low-base/new handling
  - Growth/Retention advanced metrics (NRR/GRR, shares, reactivation, concentration)
  - Drivers & Actions (movers, decomposition, segment mix, recommended actions)
  - Lifecycle + revenue composition charts
  - Churn risk clarity panel (rule-based thresholds + top at-risk list)
- Table upgraded with prior and delta columns for revenue/profit/margin/orders.
- KPI exports support datasets:
  - `table`, `movers`, `lifecycle_funnel`, `revenue_composition`, `risk_distribution`, `top_at_risk`

## Safety checks
- Global filters and RBAC scope remain enforced through canonical bundle service.
- Export parity: KPI exports force `export_all=1` and do not use UI pagination limits.
- Cache safety: bundle cache key includes user/scope/filter hash and dataset version.

## Verification checklist
- Compare KPI totals vs table totals under same filters.
- Verify quick segment filters update table and exports.
- Verify movers export row count equals movers payload row count.
- Verify lifecycle/composition export files include full dataset and metadata sheet.
- Verify v3 off returns v2/v1 without UI regressions.

## Rollout
1. Deploy code with `CUSTOMERS_KPIS_V3=0`.
2. Enable for admins first by setting `CUSTOMERS_KPIS_V3=1` on admin environment.
3. Validate checklist on production data.
4. Enable for wider users.

## Rollback
1. Set `CUSTOMERS_KPIS_V3=0`.
2. Restart app services.
3. Confirm `/customers/` loads v2 or v1 template.

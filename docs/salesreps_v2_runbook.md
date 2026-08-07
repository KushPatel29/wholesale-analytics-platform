# SALESREPS_V2 Runbook

## Flag
- Env var: `SALESREPS_V2=1`
- Config key: `SALESREPS_V2`
- Rollback: set `SALESREPS_V2=0` and restart app service.

## Release Steps
1. Deploy code with `SALESREPS_V2=0`.
2. Run smoke tests for `/salesreps/`, `/api/salesreps/bundle`, `/salesreps/export.csv`, `/salesreps/export.xlsx`.
3. Enable in staging: `SALESREPS_V2=1`.
4. Validate RBAC, table sorting/search/pagination, and exports.
5. Enable in production during low-traffic window.

## Rollback Plan
1. Set `SALESREPS_V2=0` in service environment.
2. Restart app service.
3. Confirm `/salesreps/` renders legacy template (`salesreps/index_legacy.html`).
4. Verify exports and drilldowns still return 200/403 as expected.

## Manual Verification Checklist
- KPI totals match aggregated table totals under the same sticky filters.
- Revenue/Profit/Margin MoM deltas are populated when at least two months exist.
- Top Reps + Pareto metric toggle works for Revenue/Profit/Margin$/Margin%/Orders/Customers/Weight.
- Concentration chart shows Top 1 and Top 5 shares with HHI in tooltip.
- Risk flags card updates from table values.
- Table supports:
  - Search by rep name.
  - Sort by numeric columns and rep name.
  - Page size 25/50/100.
  - Sticky header + zebra rows.
  - Row click and Enter key drilldown.
- Exports include all rows under current filter/search state (not current page only).
- Export filenames include date window token.
- Sales-scoped users only see/export allowed reps; admin sees all.

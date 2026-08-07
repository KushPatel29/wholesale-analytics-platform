# Customers CLV v2 Runbook

## Feature Flag
- Flag: `CUSTOMERS_CLV_V2`
- Default: `0` (off)
- Rollback: set `CUSTOMERS_CLV_V2=0` and restart app service.

## Scope and Safety
- CLV v2 is rendered only when flag is enabled.
- Flag off keeps existing `customers/clv.html` unchanged.
- Bundle cache key includes user scope, filter hash, dataset version, and `clv_params_hash`.
- Exports are generated from full filtered rows (`export_all=1`), not current page slices.

## Deployment Steps
1. Deploy code with `CUSTOMERS_CLV_V2=0`.
2. Restart service and verify `/health` is `ok`.
3. Enable for admins first: set `CUSTOMERS_CLV_V2=1` in environment.
4. Restart service.
5. Verify:
   - `/customers/clv` renders CLV v2 sections.
   - CLV settings change output.
   - Exports work for `customers`, `segments`, and `at_risk_high_value`.
   - RBAC-scoped user does not see out-of-scope customers.
6. Roll out to all users after admin sign-off.

## Manual Verification Checklist
- CLV cards reconcile with table totals under current filters.
- Segment leaderboard and table exports are consistent.
- Customer table export row count equals filtered table row count.
- Drilldown links open for table and scatter points.
- Cost coverage caveat appears when gross-profit basis falls back to revenue.
- Flag off returns legacy CLV screen.

## Rollback
1. Set `CUSTOMERS_CLV_V2=0`.
2. Restart app service.
3. Confirm legacy CLV page is active.

# Customers RFM V2 Runbook

## Purpose
- Feature: enterprise RFM upgrade for `Customers -> RFM`.
- Flag: `CUSTOMERS_RFM_V2`.
- Default: `0` (off).
- Rollback: set `CUSTOMERS_RFM_V2=0` and restart app workers.

## What ships behind the flag
- Configurable RFM settings (lookback, scoring method, monetary metric, thresholds).
- Explainable 1-5 R/F/M scoring and segment assignment.
- 5x5 R x F heatmap with click-to-filter.
- Segment insights + playbooks.
- Upgraded top/full tables with search/filters/sort.
- Scatter improvements with segment colors and quadrant lines.
- Full-row exports for RFM datasets (`customers_full`, `top_customers`, `segments`, `segment_leaderboard`, `matrix_cells`, `heatmap_customers`).
- Alias endpoint for RFM export routing: `/customers/rfm/export?type=customers|segments|heatmap|top|matrix`.

## Deployment
1. Deploy code with `CUSTOMERS_RFM_V2=0`.
2. Restart app service.
3. Verify baseline health:
   - `/health` returns `200`.
   - `/customers/rfm` loads successfully (legacy view while flag is off).

## Admin-first verification
1. Enable for admin environment:
   - `CUSTOMERS_RFM_V2=1`.
2. Restart app service.
3. Verify on `/customers/rfm`:
   - Settings panel updates outputs when clicking `Compute`.
   - Heatmap cell click applies `heat_r` / `heat_f` filter.
   - Segment counts and full table totals reconcile.
   - Exported `customers_full` row count matches filtered table `total_rows`.
   - RBAC user sees only scoped customers in UI and export.

## Production rollout
1. Keep `CUSTOMERS_RFM_V2=0` for full org until admin sign-off.
2. Enable progressively during low-risk window.
3. Monitor logs for:
   - `customers.bundle.rfm_v2`
   - `customers.export` failures
4. Confirm no spike in response latency or error rates.

## Rollback
1. Set `CUSTOMERS_RFM_V2=0`.
2. Restart app workers.
3. Re-open `/customers/rfm` to confirm legacy page is active.

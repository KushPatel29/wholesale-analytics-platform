# Overview V2 Release Runbook

## Deploy
1. Apply code and restart the app service:
   - `sudo systemctl restart wholesale_analytics`
2. Confirm process is healthy:
   - `sudo systemctl status wholesale_analytics --no-pager`
3. Run the golden smoke check for a known window:
   - `python3 scripts/overview_golden_smoke.py --start 2025-10-01 --end 2025-12-31`
4. Verify API responds and includes `overview_metrics`:
   - `curl -sS 'http://localhost:5000/overview/api/bundle?start=2025-10-01&end=2025-12-31' | jq '.overview_metrics.window,.kpis'`

## Production Verification Checklist
1. Compare old vs new headline totals on the same explicit window:
   - Revenue, orders, customers, cost, profit.
2. Verify window metadata is explicit and aligned everywhere:
   - `meta.window.start/end`
   - `meta.window.prior_month_*`
   - `meta.window.prior_year_*`
3. Validate MoM `%` behavior:
   - Prior-period zero displays `n/a` (not `inf` or `0%`).
4. Validate driver math:
   - `price + volume + mix ~= total` for MoM and YoY.
5. Validate concentration:
   - Top 1 / Top 5 and HHI values are stable against direct SQL.
6. Validate profitability:
   - Coverage badge matches health coverage.
   - Margin risk list is collapsible and limited to top 10 worst impact.
7. Validate no route regressions:
   - `/overview`, `/overview/api/bundle`, `/api/overview/insights`.
8. Validate latency:
   - `/overview/api/bundle` p95 should remain within normal baseline.
9. Validate UX text/formatting:
   - Currency compact formatting and `n/a` tooltips render correctly.

## Rollback
1. Disable V2 without revert:
   - `export OVERVIEW_V2=0` (or set in service env) and restart service.
2. If needed, rollback code to previous deploy artifact/commit.
3. Clear cache (if stale responses persist):
   - restart app process or flush configured cache backend.
4. Re-run smoke check on legacy page and confirm stable metrics.

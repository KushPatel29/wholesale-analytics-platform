# SALESREP_DRILLDOWN_V2 Runbook

## Flag
- Env var: `SALESREP_DRILLDOWN_V2=1`
- Config key: `SALESREP_DRILLDOWN_V2`
- Rollback: set `SALESREP_DRILLDOWN_V2=0` and restart app service.

## Scope
- Route remains unchanged: `/salesreps/<rep_id>`
- Bundle endpoint remains unchanged: `/api/salesreps/drilldown/bundle`
- Export endpoint remains unchanged: `/salesreps/<rep_id>/export`

## Deployment Steps
1. Deploy code with `SALESREP_DRILLDOWN_V2=0`.
2. Run backend checks:
   - `pytest -q -o addopts= tests/test_salesreps_drilldown_bundle.py tests/test_salesreps_exports.py tests/test_salesrep_drilldown_frontend_contract.py`
3. Enable in staging: `SALESREP_DRILLDOWN_V2=1`.
4. Validate UI and exports with production-like RBAC users.
5. Enable in production during a low-traffic window.

## Rollback Steps
1. Set `SALESREP_DRILLDOWN_V2=0` in service environment.
2. Restart app service.
3. Verify `/salesreps/<rep_id>` loads legacy drilldown layout.
4. Verify `/salesreps/<rep_id>/export` still returns expected CSV/XLSX.

## Verification Checklist
- Header shows rep name/id, date window, active filters summary, and last refresh timestamp.
- KPI cards render Revenue/Profit/Margin with MoM/YoY deltas when prior periods exist.
- Trend chart supports Monthly/Weekly toggle and rolling average overlay.
- Movers tables show customer and product MoM gainers/decliners.
- Concentration card shows Top 1/Top 5 and HHI.
- Margin risk card shows below-target and negative-margin counts plus leakage table.
- Top customers/products tables show upgraded columns and row-click drilldowns.
- At-risk customers table loads and exports full set.
- All export buttons include current filters and return full filtered rows (not top-N UI truncation).
- RBAC checks:
  - sales users cannot open/export out-of-scope reps (403)
  - admin/manager can access allowed reps.

## Cache Safety
- Drilldown bundle cache key includes:
  - `user_id`
  - `scope_hash`
  - `filters_hash`
  - `dataset_version`
  - `rep_id` (via drilldown `entity_id` extras)

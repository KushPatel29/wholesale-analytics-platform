# Customers KPIs V2 Runbook

## Feature Flag
- Flag: `CUSTOMERS_KPIS_V2`
- Default: `0`
- Rollback: set `CUSTOMERS_KPIS_V2=0` and restart web workers.

## What V2 Changes
- Executive `Customer Health` strip with narrative.
- Enterprise KPI set (NRR/GRR, growth composition, concentration, profitability dispersion, tenure proxy, revenue at stake, cadence, basket quality).
- Drivers block (top movers + simple decomposition).
- Upgraded command-center table (segments, prior-window deltas, quick filters, enhanced sorting).
- Export parity:
  - `dataset=table` exports all filtered rows.
  - `dataset=movers` exports full movers list.
  - XLSX includes `Metadata` sheet.

## Rollout Sequence
1. Deploy with `CUSTOMERS_KPIS_V2=0`.
2. Enable for admin verification environment: `CUSTOMERS_KPIS_V2=1`.
3. Validate:
   - `/customers/kpis` renders v2.
   - KPI totals reconcile with table totals.
   - `/customers/export?page=kpis&dataset=table&format=csv` row count equals filtered table count.
   - `/customers/export?page=kpis&dataset=movers&format=xlsx` contains `Movers` + `Metadata`.
   - Sales-scoped user sees only scoped customers in bundle and export.
4. Roll out progressively in production.

## Verification Commands
- `pytest -q tests/test_customers_kpis_v2.py`
- `pytest -q tests/test_customers_bundle_extra.py`

## Notes
- Cache keys already include `user_id`, `scope_hash`, `filters_hash`, and `dataset_version` via `cached_bundle`.
- V2 exports use bundle-driven canonical filters; no UI pagination limit is applied to exports (`export_all=1`).

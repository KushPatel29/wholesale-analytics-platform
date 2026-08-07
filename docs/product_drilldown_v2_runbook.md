# Product Drilldown V2 Runbook

## Flag
- Feature flag: `PRODUCT_DRILLDOWN_V2`
- Default: `0` (off)
- Scope: `/products/<sku>/drilldown` only
- Fallback: existing `products/drilldown.html` path remains unchanged when flag is off.
- Forecast flag: `PRODUCT_FORECAST_V1`
- Default: `0` (off)
- Scope: forecast UI/API inside Product Drilldown V2.

## What Ships
- New service: `app/services/product_drilldown_service.py`
- New template: `app/templates/products/product_drilldown_v2.html`
- New style: `app/static/css/product_drilldown_v2.css`
- New scoped export endpoint:
  - `/products/<sku>/drilldown/export?kind=kpis|monthly_series|customers|regions|suppliers|ship_methods|basket|seasonality|snapshot&format=csv|xlsx`
- Forecast API endpoint:
  - `/api/products/<sku>/forecast?freq=month|week&horizon=<n>`

## Safety Guarantees
- Canonical filter resolver used via `filters.resolve_filters(...)`.
- Existing RBAC/scope helpers used (`filters_service.scope_from_user`, `access_policy.enforce_entity_access`).
- Heavy context query cached with dataset-aware keys in `cached_bundle`.
- Export data built from the same scoped context as the page and returns full datasets (no top-N truncation in exports).

## Deployment
1. Deploy code with `PRODUCT_DRILLDOWN_V2=0` and `PRODUCT_FORECAST_V1=0`.
2. Restart app service.
3. Verify baseline pages still work.
4. Enable admin-only pilot:
   - set `PRODUCT_DRILLDOWN_V2=1` in service env for pilot environment or controlled users.
   - set `PRODUCT_FORECAST_V1=1` for forecast UI/API.
   - restart app service.
5. Validate and roll out broadly.

## Verification Checklist
- Open high-volume and low-volume SKU drilldowns.
- Confirm month labels are clean (`YYYY-MM` / `MMM YYYY`), no microseconds/timestamp artifacts.
- Confirm KPI totals align with exported full datasets.
- Confirm basket section gracefully shows insufficient sample message for low base orders.
- Confirm exports include full filtered/scoped rows.
- Confirm sales-scoped users only see allowed records.
- Confirm forecast toggle appears only when `PRODUCT_FORECAST_V1=1`.
- Confirm forecast API returns non-negative values and method metadata.

## Rollback
1. Set `PRODUCT_DRILLDOWN_V2=0`.
2. Set `PRODUCT_FORECAST_V1=0`.
3. Restart app service.
4. Confirm `/products/<sku>/drilldown` is rendering legacy template.

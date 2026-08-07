# FILTERS_CANONICAL_V2 Runbook

## Purpose
Enable deterministic, single-source filter behavior across pages, APIs, drilldowns, and exports without breaking current production behavior.

## What Changed
- Canonical resolver: `resolve_filters(request, current_user) -> (filters, meta)`.
- Explicit PRG actions:
  - `POST /filters/apply`
  - `POST /filters/reset`
- Session writes in v2 only occur on explicit apply/reset.
- Request metadata now includes effective filter hash/source and cache-key hash headers:
  - `X-Filters-Hash`
  - `X-Filters-Source`
  - `X-Filter-Cache-Key-Hash`

## Feature Flag
- Env var: `FILTERS_CANONICAL_V2=1`
- Config key: `FILTERS_CANONICAL_V2`

When disabled (`0`/unset), legacy sticky behavior remains unchanged.

## Rollout Plan
1. Set `FILTERS_CANONICAL_V2=1` in staging.
2. Restart app workers.
3. Verify:
   - Apply filters on any page.
   - Open graph/drilldown/export.
   - Confirm `X-Filters-Hash` is the same for related requests.
4. Optional gradual rollout:
   - Keep flag enabled only for admin/test environment first.
   - Then enable globally.

## Manual Verification Checklist
- Apply filters -> page KPIs and graphs change consistently.
- Export CSV/XLSX row totals match on-screen filtered values.
- Drilldown values match the same filtered window.
- Refresh and navigate to another page -> filters persist.
- Repeated quick toggles do not produce inconsistent results.

## Observability
- `api.request.summary` logs include:
  - `request_id`
  - `current_user_id`
  - `effective_filters`
  - `filters_hash`
  - `filters_source`
  - `window_start` / `window_end`
  - `endpoint`
  - `cache_key_hash`

## Rollback
1. Set `FILTERS_CANONICAL_V2=0`.
2. Restart app workers.
3. Confirm legacy behavior restored (GET `_gf` capture path).

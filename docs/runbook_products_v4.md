# Products V4 Runbook

## Feature flag
- Flag: `PRODUCTS_V4`
- Default: `0` (off)
- Fallback chain:
  - `PRODUCTS_V4=1` -> `products/index_v4.html`
  - `PRODUCTS_V4=0` and `PRODUCTS_V3=1` -> `products/index_v3.html`
  - both off -> legacy `products/index.html`

## Enable (admin-first)
1. Set `PRODUCTS_V4=1` in production env.
2. Restart `wholesale_analytics`.
3. Validate `/products/` as admin with:
   - last 3 months
   - last 12 months
   - narrow supplier filter

## Verify
- Overview renders `Portfolio map (2x2)` and no forecast controls.
- Matrix quadrants populate and top-10 modal opens drilldown links.
- Top products is horizontal and Pareto x-axis is rank-based.
- Exports return full rows:
  - `/products/export/table.csv`
  - `/products/export/movers.csv`
  - `/products/export/execution.csv?list=pricing_fixes|cost_fixes|promote_candidates`
  - `/products/export/segment_mix.csv`
  - `/products/export/quadrant.csv?quadrant=protect|fix_margin|grow|rationalize`

## Rollback
1. Set `PRODUCTS_V4=0`.
2. Restart `wholesale_analytics`.
3. Page automatically falls back to v3/v1 without code rollback.

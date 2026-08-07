# Products Architecture

## Purpose
Products is a large mixed legacy/v2/v4 module for product overview analytics, bundle-backed tables, movers/recommendations, and product drilldowns with export support.

## Main Entry Points
- Routes: `app/blueprints/products.py`
- Main URLs:
  - `/products/`
  - `/products/api/*`
  - `/products/api/bundle`
  - `/products/api/drilldown/bundle`
  - `/products/<product_id>`
  - `/products/<product_id>/drilldown`

## Main Logic Files
- `app/blueprints/products.py`: primary route layer and a significant amount of legacy/business logic
- `app/services/bundle_service.py`: shared bundle dispatch
- `app/services/products_bundle.py`: bundle payload construction
- `app/services/product_drilldown_service.py`: v2 drilldown context and exports
- `app/services/products.py`: additional product helpers

## Templates and Assets
- Templates:
  - `app/templates/products/index.html`
  - `app/templates/products/index_v3.html`
  - `app/templates/products/index_v4.html`
  - `app/templates/products/product_detail.html`
  - `app/templates/products/drilldown.html`
  - `app/templates/products/product_drilldown_v2.html`
- JS/CSS:
  - `app/static/js/products.js`
  - `app/static/css/products_workspace_v4.css`
  - `app/static/css/product_drilldown_v2.css`

## Key Data Concepts
- Product overview can come from bundle-backed APIs instead of a single server-rendered payload.
- Product drilldown v2 uses a dedicated service layer and includes sensitive cost/margin handling.
- Product exports often reuse bundle/drilldown data, so page and export parity matters.

## Drilldowns and Exports
- Overview exports include execution, segment mix, quadrant, movers, table/overview CSV/XLSX
- Product drilldown exports are served from both legacy and v2 paths
- Sensitive export fields must stay aligned with `app/core/sensitive_data.py`

## Common Risks
- `products.py` is large; keep changes narrowly scoped and search for matching export/API paths before editing.
- Feature flags (`PRODUCTS_V3`, `PRODUCTS_V4`, `PRODUCT_DRILLDOWN_V2`, `PRODUCT_FORECAST_V1`) affect ownership and template selection.
- Cost redaction and export masking are easy to break when adding new drilldown fields.

## Validate After Editing
- `python3 -m pytest tests/test_products_bundle_api.py tests/test_products_overview_service.py tests/test_products_drilldown.py tests/test_product_drilldown_v2.py -q`
- Add as needed:
  - exports: `tests/test_products_exports.py`
  - filters: `tests/test_products_filters.py`
  - assets/UI: `tests/test_products_static_assets.py tests/test_products_playwright.py`
  - forecast: `tests/test_products_forecast.py`

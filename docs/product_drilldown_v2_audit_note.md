# Product Drilldown V2 Audit Note

Date: 2026-03-06

## Existing Drilldown Data Sources
- Route/controller:
  - `app/blueprints/products.py` (`/products/<sku>/drilldown`)
  - legacy fallback template: `app/templates/products/drilldown.html`
  - v2 template: `app/templates/products/product_drilldown_v2.html`
- Data pipeline:
  - canonical filtered fact queries through `app/services/fact_store.py` + `build_where_clause(...)`
  - scoped drilldown bundle via `app/services/products_bundle.py::build_products_drilldown(...)`
  - v2 context service via `app/services/product_drilldown_service.py`
- Caching:
  - `app/services/bundle_cache.py::cached_bundle(...)`
  - cache key includes canonical filters, scope hash, dataset version, endpoint, and extras.

## Forecast Gating Logic (Before)
- V2 service checked `FEATURE_FORECAST_ENABLED` directly in `product_drilldown_service._should_show_forecast(...)`.
- Result: V2 frequently displayed `Forecast disabled by feature flag.` even when forecasting data existed in legacy payload.

## Standardized Flags (Now)
- `PRODUCT_DRILLDOWN_V2`:
  - controls V2 UI and service path for drilldown route.
- `PRODUCT_FORECAST_V1`:
  - controls forecast UI/API for product drilldown.
  - fallback compatibility still recognizes `FEATURE_FORECAST_ENABLED` if present.

## Files Touched
- `app/services/product_drilldown_service.py`
- `app/blueprints/products.py`
- `app/blueprints/bundles.py`
- `app/templates/products/product_drilldown_v2.html`
- `app/static/css/product_drilldown_v2.css`
- `app/config.py`
- `app/__init__.py`
- `.env.example`
- `tests/test_product_drilldown_v2.py`

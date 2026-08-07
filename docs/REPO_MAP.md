# Repo Map

Use this map in order: start with the page/module section, then inspect the shared filters/exports/RBAC sections at the end if the task crosses those boundaries.

## Shared Runtime and Cross-Cutting Layers
- App factory and registration: `app/__init__.py`
- Runtime entrypoints: `wsgi.py`, `run.py`, `manage.py`
- Config and feature flags: `app/config.py`, `app/core/features.py`, `app/core/branding.py`
- Core data/query layer: `app/services/fact_store.py`, `data/store.py`, `data_loader.py`, `etl/`
- Shared bundle plumbing: `app/services/bundle_service.py`, `app/services/bundle_builder.py`, `app/services/bundle_cache.py`

## Overview
- Routes:
  - `app/blueprints/overview.py`
  - Page blueprint: `/overview`
  - API/export/drilldown endpoints: `/api/overview/*`, `/overview/api/*`
- Business logic:
  - `app/services/overview_v2.py`
  - `app/services/overview_metrics.py`
  - `app/services/overview_forecast.py`
  - `app/services/overview_query.py`
  - `app/services/overview_summary.py`
  - `app/services/overview_insights.py`
- Templates:
  - `app/templates/overview/index.html`
  - `app/templates/overview/index_v3.html`
  - `app/templates/overview/index_legacy.html`
  - `app/templates/overview/_overview_*.html`
- Frontend:
  - `app/static/js/overview_v2.js`
  - `app/static/js/overview.js`
  - `app/static/js/overview_legacy.js`
  - `app/static/css/overview_futuristic.css`
  - shared shell styles in `app/static/css/theme.css`
- Exports:
  - snapshot/trend/drilldown handlers in `app/blueprints/overview.py`
- Tests:
  - `tests/test_overview.py`
  - `tests/test_overview_api.py`
  - `tests/test_overview_bundle.py`
  - `tests/test_overview_metric_contract.py`
  - `tests/test_overview_forecast.py`
  - `tests/test_overview_parity.py`
  - `tests/test_overview_v2_smoke.py`
  - `tests/test_overview_playwright.py`

## Customers
- Routes:
  - `app/blueprints/customers.py`
  - Main pages: `/customers/`, `/customers/clv`, `/customers/cohorts`, `/customers/rfm`, `/customers/kpis`
  - Drilldown: `/customers/drilldown/<customer_id>`
  - Export surface: `/customers/export`, plus alias exports
- Business logic:
  - `app/services/bundle_service.py` for bundle/drilldown dispatch
  - `app/services/customers_bundle.py`
  - `app/services/customers_cohorts_v2.py`
- Templates:
  - `app/templates/customers/*.html`
  - `app/templates/customers/_drilldown_v2_macros.html`
- Frontend:
  - `app/static/js/customer_drilldown_v2.js`
  - `app/static/css/customer_drilldown_v2.css`
  - global filters/export helpers from base shell
- Exports:
  - customer page/drilldown/cohort/RFM/CLV export handlers in `app/blueprints/customers.py`
- Tests:
  - `tests/test_customers_bundle_sections.py`
  - `tests/test_customers_bundle_extra.py`
  - `tests/test_customers_drilldown_v2.py`
  - `tests/test_customers_clv_v2.py`
  - `tests/test_customers_cohorts_v2.py`
  - `tests/test_customers_kpis_v2.py`
  - `tests/test_customers_kpis_v3.py`
  - `tests/test_customers_rfm_v2.py`

## Products
- Routes:
  - `app/blueprints/products.py`
  - Main page: `/products/`
  - Bundle APIs: `/products/api/*`, `/products/api/bundle`, `/products/api/drilldown/bundle`
  - Drilldown: `/products/<product_id>`, `/products/<product_id>/drilldown`
  - Exports: `/products/export/*`, `/<product_id>/drilldown/export`, `/<product_id>/export`
- Business logic:
  - `app/services/bundle_service.py`
  - `app/services/products_bundle.py`
  - `app/services/product_drilldown_service.py`
  - `app/services/products.py`
- Templates:
  - `app/templates/products/index.html`
  - `app/templates/products/index_v3.html`
  - `app/templates/products/index_v4.html`
  - `app/templates/products/product_detail.html`
  - `app/templates/products/drilldown.html`
  - `app/templates/products/product_drilldown_v2.html`
- Frontend:
  - `app/static/js/products.js`
  - `app/static/css/products_workspace_v4.css`
  - `app/static/css/product_drilldown_v2.css`
- Exports:
  - overview/movers/execution/quadrant/drilldown exports in `app/blueprints/products.py`
- Variant selection:
  - `PRODUCTS_V3`, `PRODUCTS_V4`, `PRODUCT_DRILLDOWN_V2`, and `PRODUCT_FORECAST_V1` are resolved in `app/config.py` and `app/blueprints/products.py`
- Tests:
  - `tests/test_products_bundle_api.py`
  - `tests/test_products_overview_service.py`
  - `tests/test_products_drilldown.py`
  - `tests/test_product_drilldown_v2.py`
  - `tests/test_products_filters.py`
  - `tests/test_products_exports.py`
  - `tests/test_products_static_assets.py`
  - `tests/test_products_playwright.py`
  - `tests/test_products_forecast.py`

## Suppliers
- Routes:
  - `app/blueprints/suppliers.py`
  - Main page: `/suppliers/`
  - Drilldown: `/suppliers/<supplier_id>`
  - Bundle drilldown API: `/suppliers/api/drilldown/bundle`
  - Exports: `/suppliers/export`, `/suppliers/export/<supplier_id>`
- Business logic:
  - `app/services/bundle_service.py`
  - `app/services/suppliers_bundle.py`
- Templates:
  - `app/templates/suppliers/index.html`
  - `app/templates/suppliers/index_v2.html`
  - `app/templates/suppliers/drilldown.html`
  - `app/templates/suppliers/drilldown_v2.html`
- Frontend:
  - `app/static/js/suppliers.js`
  - `app/static/js/suppliers_v2.js`
  - `app/static/js/suppliers_drilldown_v2.js`
  - `app/static/css/supplier_drilldown_v2.css`
- Tests:
  - `tests/test_suppliers_v2.py`
  - `tests/test_supplier_drilldown_v2.py`
  - `tests/test_suppliers_metrics.py`
  - `tests/test_suppliers_products_export.py`
  - `tests/test_supplier_workbook.py`

## Regions
- Routes: `app/blueprints/regions.py`
- Business logic: `app/services/regions_bundle.py`
- Templates: `app/templates/regions/*.html`
- Frontend:
  - `app/static/js/regions.js`
  - `app/static/js/regions_v2.js`
  - `app/static/js/regions_drilldown.js`
  - `app/static/js/regions_drilldown_v2.js`
  - `app/static/css/regions_drilldown_v2.css`
- Tests:
  - `tests/test_regions_bundle.py`
  - `tests/test_regions_drilldown_bundle.py`
  - `tests/test_regions_drilldown_v2.py`
  - `tests/test_regions_v2.py`

## Sales Reps
- Routes: `app/blueprints/salesreps.py`
- Business logic: `app/services/salesreps_bundle.py`
- Templates: `app/templates/salesreps/*.html`
- Frontend:
  - `app/static/js/salesreps.js`
  - `app/static/js/salesreps_legacy.js`
  - `app/static/js/salesrep_drilldown.js`
  - `app/static/css/salesreps_v2.css`
- Tests:
  - `tests/test_salesreps_bundle.py`
  - `tests/test_salesreps_drilldown_bundle.py`
  - `tests/test_salesreps_exports.py`
  - `tests/test_salesreps_module.py`
  - `tests/test_salesreps_v2.py`

## Labor
- Routes:
  - `app/blueprints/labor.py`
  - Main page/API: `/labor/`, `/labor/api/bundle`
  - Exports: `/labor/export/<dataset>`
- Business logic:
  - `app/services/labor_bundle.py`
  - `app/services/labor_store.py`
  - `app/services/labor_etl.py`
  - `app/services/synerion_client.py`
- Templates:
  - `app/templates/labor/index.html`
  - `app/templates/labor/_macros.html`
- Frontend:
  - `app/static/js/labor.js`
  - `app/static/css/labor.css`
- Tests:
  - `tests/test_labor_blueprint.py`
  - `tests/test_labor_loader.py`
  - `tests/test_labor_store.py`
  - `tests/test_synerion_client.py`

## Returns
- Routes:
  - `app/returns/blueprints.py`
  - Portal, ops, warehouse, admin, and webhook blueprints
- Business logic:
  - `app/returns/service.py`
  - `app/returns/models.py`
  - `app/returns/orders.py`
  - `app/returns/suggestions.py`
- Templates:
  - `app/templates/returns/*.html`
  - `app/templates/admin/returns/*.html`
- Frontend:
  - `app/templates/returns/base.html`
  - `app/static/css/returns.css`
- Tests:
  - `tests/test_returns_module.py`

## Assistant
- Routes: `app/assistant/routes.py`
- Business logic:
  - `app/assistant/context.py`
  - `app/assistant/service.py`
  - `app/assistant/tools.py`
  - `app/assistant/provider.py`
  - `app/assistant/export_store.py`
  - `app/assistant/export_job_store.py`
- Templates/assets:
  - `app/templates/assistant/index.html`
  - `app/static/js/assistant.js`
  - `app/static/css/assistant.css`
- Tests:
  - `tests/test_assistant_feature.py`
  - `tests/test_assistant_provider.py`

## Filters and Shared UI
- Shared shell:
  - `app/templates/base.html`
  - `app/static/css/theme.css`
  - `app/static/js/exports.js`
  - `app/static/js/live-updates.js`
  - `app/static/js/bundle-adapter.js`
- Shared filters and saved views:
  - `app/core/filters.py`
  - `app/services/filters.py`
  - `app/services/filters_service.py`
  - `app/blueprints/filters_api.py`
  - `app/blueprints/filters_actions.py`
  - `app/templates/_filters.html`
  - `app/static/js/global_filters.js`
  - `app/static/js/filters-enhanced.js`
- Universal drilldowns:
  - `app/blueprints/drilldowns.py`
  - `app/services/drilldown_service.py`
  - `app/templates/drilldowns/workspace.html`
  - `app/static/js/universal_drilldown.js`
  - `app/static/css/universal_drilldown.css`

## Exports and Sensitive Data
- Core export response helpers:
  - `app/core/exports.py`
- Export frame builders:
  - `app/services/exports.py`
- Sensitive data masking:
  - `app/core/sensitive_data.py`
- Module-specific export handlers:
  - export endpoints typically live in the owning blueprint, not in standalone exporter modules

## Auth, Admin, and RBAC
- Access policy and scope:
  - `app/core/access_policy.py`
  - `app/core/rbac.py`
  - `app/core/payload_permissions.py`
- Auth routes/models:
  - `app/auth/routes.py`
  - `app/auth/models.py`
  - `app/auth/password_tokens.py`
- Canonical permission model:
  - `app/auth/permissions.py`
- Admin portal:
  - `app/blueprints/admin.py`
  - `app/blueprints/admin_api.py`
  - `app/templates/admin/*.html`
- Tests:
  - `tests/test_admin_permissions_v2.py`
  - `tests/test_admin_rbac_portal.py`
  - `tests/test_admin_user_select.py`
  - `tests/test_rbac_access.py`
  - `tests/test_rbac_scope.py`

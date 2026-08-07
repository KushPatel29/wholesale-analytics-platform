# Customers Architecture

## Purpose
Customers covers multiple analytical surfaces: overview/KPIs, CLV, cohorts, RFM, and customer drilldowns with export support.

## Main Entry Points
- Routes: `app/blueprints/customers.py`
- Main URLs:
  - `/customers/`
  - `/customers/clv`
  - `/customers/cohorts`
  - `/customers/rfm`
  - `/customers/kpis`
  - `/customers/drilldown/<customer_id>`

## Main Logic Files
- `app/services/bundle_service.py`: entity dispatch for customer bundle/drilldown payloads
- `app/services/customers_bundle.py`: overview/KPI/drilldown bundle logic
- `app/services/customers_cohorts_v2.py`: cohort-specific logic and exports

## Templates and Assets
- Templates: `app/templates/customers/*.html`
- Important template variants:
  - `drilldown.html`
  - `drilldown_v2.html`
  - `clv_v2.html`
  - `cohorts_v2.html`
  - `kpis_v2.html`
  - `kpis_v3.html`
  - `rfm_v2.html`
- JS/CSS:
  - `app/static/js/customer_drilldown_v2.js`
  - `app/static/css/customer_drilldown_v2.css`

## Key Data Concepts
- Customers is heavily section/bundle driven; many page payloads are built from `_sections` requests rather than one-off query paths.
- Drilldown behavior is feature-flagged; legacy and v2 paths both exist.
- Export access is explicitly enforced in `customers.py`; page visibility does not automatically guarantee export permission.

## Drilldowns and Exports
- Drilldown page: `/customers/drilldown/<customer_id>`
- Export surface: `/customers/export` plus RFM/CLV alias endpoints
- Cohort exports are handled separately through cohort helpers

## Common Risks
- Customer pages combine filters, bundle pagination, export dataset selection, and permission checks in one blueprint.
- Changes to shared customer bundle fields can affect CLV, cohorts, drilldown, and export paths at once.
- When editing drilldown behavior, check both page rendering and export URL generation.

## Validate After Editing
- `python3 -m pytest tests/test_customers_bundle_sections.py tests/test_customers_bundle_extra.py tests/test_customers_drilldown_v2.py -q`
- Add the page-specific suite:
  - CLV: `tests/test_customers_clv_v2.py`
  - Cohorts: `tests/test_customers_cohorts_v2.py`
  - KPIs: `tests/test_customers_kpis_v2.py tests/test_customers_kpis_v3.py`
  - RFM: `tests/test_customers_rfm_v2.py`

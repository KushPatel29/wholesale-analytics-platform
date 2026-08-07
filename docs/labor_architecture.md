# Labor Architecture

## Purpose
Labor is a separate analytics surface for labor cost, paid hours, premium/absence tracking, summaries, and watchlists sourced from the labor parquet/Synerion flow.

## Main Entry Points
- Routes: `app/blueprints/labor.py`
- Main URLs:
  - `/labor/`
  - `/labor/api/bundle`
  - `/labor/export/<dataset>`

## Main Logic Files
- `app/services/labor_bundle.py`: page payloads, filters, tables, summaries, export frame construction
- `app/services/labor_store.py`: labor dataset status/query layer
- `app/services/labor_etl.py`: labor build/refresh path
- `app/services/synerion_client.py`: upstream API integration

## Templates and Assets
- Template: `app/templates/labor/index.html`
- Helper macros: `app/templates/labor/_macros.html`
- JS/CSS:
  - `app/static/js/labor.js`
  - `app/static/css/labor.css`

## Key Data Concepts
- Labor uses its own `LaborFilters` model, not the shared `FilterParams` envelope.
- The page intentionally sets `hide_global_filters=True`; do not assume base-shell global filters apply here.
- Dataset availability is handled through `LaborDatasetNotBuiltError` and labor status metadata.

## Drilldowns and Exports
- Labor does not use the same universal drilldown pattern as the sales modules.
- Exports are dataset-key based (`snapshot`, `detail`, `department-summary`, `category-summary`, `employee-summary`, `watchlist`).

## Common Risks
- Mixing labor-specific filters with shared analytics filters can create confusing behavior.
- ETL/storage changes can affect page payloads even if the Flask route layer is untouched.
- Synerion and labor parquet paths are operationally sensitive; treat them like data-platform code, not just page code.

## Validate After Editing
- `python3 -m pytest tests/test_labor_blueprint.py tests/test_labor_loader.py tests/test_labor_store.py tests/test_synerion_client.py -q`
- Run `python3 scripts/fact_smoke.py` if you changed dataset assumptions or export frame composition

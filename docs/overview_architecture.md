# Overview Architecture

## Purpose
Overview is the top-level business performance dashboard. It owns headline KPI windows, trend and mover analysis, decomposition/insight cards, and forecast endpoints.

## Main Entry Points
- Routes: `app/blueprints/overview.py`
- Page URLs: `/overview`
- API/export/drilldown URLs: `/api/overview/*`, `/overview/api/*`

## Main Logic Files
- `app/services/overview_v2.py`: bundle assembly, caching, drilldown frame construction
- `app/services/overview_metrics.py`: current/prior window contract, partial-period comparison rules, KPI delta helpers
- `app/services/overview_forecast.py`: forecast history building, model selection, serialization, warnings
- `app/services/overview_query.py`: query helpers
- `app/services/overview_summary.py`, `app/services/overview_insights.py`: derived summary content

## Templates and Assets
- Templates:
  - `app/templates/overview/index.html`
  - `app/templates/overview/index_v3.html`
  - `app/templates/overview/index_legacy.html`
  - `app/templates/overview/_overview_*.html`
- JS:
  - `app/static/js/overview_v2.js`
  - `app/static/js/overview.js`
  - `app/static/js/overview_legacy.js`
- CSS:
  - `app/static/css/overview_futuristic.css`
  - shared shell in `app/static/css/theme.css`

## Key Data Concepts
- Window comparison logic is deliberate. `overview_metrics.py` distinguishes full windows, matched-day windows, and month-to-date behavior.
- Forecasts are sensitive to sparse history, seasonality strength, and partial-period inclusion.
- Bundle caching keys include filters, dataset version, and window contract details.

## Drilldowns and Exports
- Drilldowns are served from `app/blueprints/overview.py` and `app/services/overview_v2.py`.
- Exports run through `app/core/exports.py`; they should reflect the same scoped dataset the page is showing.

## Common Risks
- Changing labels without changing the underlying window contract can create misleading KPI comparisons.
- Forecast changes can silently affect warnings, confidence diagnostics, and export rows.
- Overview uses both page and API/export endpoints; editing one path without the other is a common source of drift.

## Validate After Editing
- `python3 -m pytest tests/test_overview.py tests/test_overview_api.py tests/test_overview_bundle.py tests/test_overview_metric_contract.py -q`
- Add `tests/test_overview_forecast.py` if forecast logic changed
- Add `tests/test_overview_playwright.py` and `make check-static` for UI changes
- Run `python3 scripts/overview_golden_smoke.py ...` when KPI semantics or query windows changed and a local dataset is available

# Codex Task Conventions

## Fast Start
- If the task is under-specified, open `CODEX.md`, then `docs/REPO_MAP.md`, then the relevant module note, then `docs/TESTING_MATRIX.md`.
- If the task touches filters, RBAC, exports, forecasting, or `app/templates/base.html`, call that out explicitly instead of describing it as page-local only.

## What to Include in Any Task
- The page/module name and URL path
- Whether the change touches filters, RBAC, exports, forecasting, or shared UI
- The exact expected outcome
- A sample user role if behavior differs by access scope
- The validation depth you want: targeted tests only, broader smoke, or full repo hygiene

## Good Task Shapes for This Repo

### Page Upgrade Task
Use for visual or interaction work on one page.

Include:
- route/path
- target template
- target JS/CSS if known
- whether feature flags are involved
- whether export buttons or drilldown links must stay unchanged

### Bug-Fix Task
Use for broken page/API behavior.

Include:
- exact failing URL or endpoint
- actual vs expected result
- active filters if relevant
- user role / RBAC scope
- one concrete failing example ID when drilldowns are involved

### KPI / Query Logic Task
Use for metric definitions, aggregation changes, comparison windows, trust/coverage logic, or parity issues.

Include:
- module and KPI names
- expected current/prior comparison rule
- whether partial periods should be matched, excluded, or labeled differently
- export parity expectations
- whether SQL/parquet parity matters

### Frontend-Only Task
Use when no backend semantics should change.

Include:
- template path
- asset file path
- desktop/mobile expectations
- whether the shared base shell or global filters are in scope

### Performance Task
Use for slow endpoints, slow pages, or large export paths.

Include:
- exact route/API
- current symptom or timing
- expected acceptable latency
- whether cache changes are allowed
- whether live parquet/DuckDB behavior must be preserved exactly

### Forecasting Task
Use for overview/product forecast changes.

Include:
- metric and grain
- horizon
- expected fallback behavior for sparse history
- expected behavior for partial current period
- whether warnings/diagnostics/output summaries should change

## Repo-Specific Callouts
- If the task touches shared filters, say so explicitly.
- If the task touches RBAC or exports, say so explicitly.
- If the task is page-specific but bundle-backed, mention whether API/export parity must stay exact.
- If a legacy and v2/v3/v4 path coexist, state which variant is the target.

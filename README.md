Wholesale Analytics
=============

Lightweight Flask scaffold for analytics features and blueprints.

Setup
-----

1) Create a virtual env and install deps:

   - Windows PowerShell
     python -m venv .venv
     .\.venv\Scripts\Activate.ps1
     pip install -r requirements.txt

2) Configure environment:

   - Copy `.env.example` to `.env` and adjust values as needed.
   - Ensure the data loader directories exist on the host (or container):
     ```bash
     mkdir -p /var/opt/wholesale/users
     mkdir -p /var/opt/wholesale
     ```
   - Key variables for production:
     ```env
     FLASK_ENV=production
     SECRET_KEY=change_me
     MSSQL_SERVER=YOUR_SQL_HOST
     MSSQL_DB=Wholesale Analytics
     MSSQL_USER=YOUR_USER
     MSSQL_PASSWORD=YOUR_PASS
     MSSQL_ODBC_DRIVER=ODBC Driver 18 for SQL Server
     DIRECT_SQL_ONLY=true
     DB_READ_UNCOMMITTED=true
     ORDER_STATUSES=packed
     USERS_CSV_PATH=/var/opt/wholesale/users/userid.csv
     VISIBILITY_GRANTS_JSON=/var/opt/wholesale/visibility_grants.json
     AUTO_REFRESH_ENABLED=false
     ```

Run
---

- Using flask CLI:
  set FLASK_ENV=development
  flask --app wholesale_analytics/wsgi:app run

- Or run the module directly:
  python -m wholesale_analytics.wsgi

Admin/User Management
---------------------

Use the Click CLI in `manage.py` to manage the local SQLite auth DB:

- Initialize DB tables:
  python manage.py init-auth-db

- Create or update an admin user:
  python manage.py create-admin --username=admin --role=admin
  (will prompt for password if omitted)

In-app admin UI (recommended for day-to-day access control):

- Visit `/admin/users` as an admin to create users.
- Each app user must be mapped to an ERP `UserId` (GUID) from the ERP user table.
- Admins can assign visibility by selecting one or more ERP `UserId` values.
  Non-admin users only see data for the ERP users in their visibility list.
- If a non-admin has no visibility configured, the UI shows "Access not configured" and returns empty data.

Sales Rep Analytics
-------------------

- The **Sales Reps+** tab (`/salesreps/`) surfaces revenue, profit, top reps, and drilldowns by rep.
- Drilldowns live at `/salesreps/rep/<id>` and inherit global filters + RBAC scope.
- Protein filters (min/max/name-like) are available globally across analytics pages; they funnel through `FilterParams` and constrain live SQL queries and cached parquet alike.

Overview Forecasting
--------------------

- Overview now has a Run Forecast control for Revenue/Units/ASP with selectable 3/6/12 month horizons.
- Forecasts execute on-demand against the current filters at monthly grain with ETS/seasonal-naive fallbacks and non-negative clipping.
- Results are cached for ~10 minutes per filter+metric+horizon and include warnings when history is sparse or insufficient.
- A subtle notice is shown when filters change after a forecast; rerun to refresh with the new context.

Quality Gates
-------------

Install dev tooling and run the quality suite locally before pushing:

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
ruff check app data_loader.py tests
black --check app data_loader.py tests
mypy app data_loader.py tests
bandit -r app data_loader.py -s B101
```

Detailed feature runbooks and release notes live in [`docs/`](docs/).

Audit & Parity Runbook
----------------------

- Ground truth (SQL): `python data_loader.py audit-sql --start 2018-01-01 --end 2035-01-01 --status packed`
- Extract (pre-enrich): `python data_loader.py audit-extract --start 2018-01-01 --end 2035-01-01 --statuses packed`
- Enriched + persisted + API in one report: `python data_loader.py audit-enrich --start 2018-01-01 --end 2035-01-01 --statuses packed --base-url http://127.0.0.1:5000 --as-admin`
- Persisted-only check: `python data_loader.py audit-persisted --start 2018-01-01 --end 2035-01-01 --statuses packed --parquet cache/fact_dataset`
- API-only check: `python data_loader.py audit-api --base-url http://127.0.0.1:5000 --start 2018-01-01 --end 2035-01-01 --statuses packed --as-admin`
- Interpretation: SQL vs extract reveals pack/order join or UnitOfBilling errors; extract vs enriched highlights merge losses; enriched vs persisted catches parquet truncation; API gaps point to cache scope/RBAC filtering.
- Deployment checklist: set `ORDER_STATUSES=packed`, ensure `PARQUET_PATH` exists/writable, DB connectivity OK, clear stale `cache/.sales_fact.lock`, restart gunicorn/supervisor/nginx, invalidate caches, run the audits above, hit `/health/data` and `/api/_admin/audit/window` as admin, and review `persisted_signature.json`.

Performance Smoke Test
----------------------

Run a quick end-to-end loader check (uses current `.env` configuration):

```bash
python scripts/perf_smoke.py
```

RBAC smoke test
---------------

```bash
python scripts/smoke_rbac.py
```

Frontend smoke check
--------------------

- Open `/overview` and confirm no console errors and charts render (Chart.js should load from `/static/vendor/chartjs/chart.umd.min.js`).

- List users:
  python manage.py list-users

- Reset a user's password:
  python manage.py reset-password --username=alice

- Enable 2FA (prints secret + otpauth URL):
  python manage.py enable-2fa --username=alice

Backups
-------

Create a zip archive of the parquet cache and auth DB:

- Run on demand:
  python scripts/backup.py

Deployment Readiness Report
---------------------------
- Configuration validation now fails fast in production when `SECRET_KEY` or secure cookie flags are unsafe; proxy headers are honored via `ProxyFix`.
- Structured logging with rotating JSON files includes `request_id`, route, user role/id, duration, and status; uncaught errors return friendly JSON/HTML with `X-Request-ID`.
- Fact schema guard runs on startup, recording status in `FACT_SCHEMA_STATUS` and logging missing Date/Revenue/Cost/Qty/Weight columns.
- Health endpoints: `/health` (dataset version), `/healthz` (liveness), and `/readyz` (parquet/auth/optional MSSQL checks) remain available; admin metrics at `/metrics`.
- Remaining risks: committed `.env` samples still present—replace with real secrets and ignore before production; audit coverage of admin actions and long-running endpoints should be reviewed prior to go-live.

- Keep last N archives (env `BACKUP_KEEP_N`, default 10). Archives stored in `backups/`.

- Cron example (daily 2am):
  0 2 * * * /opt/wholesale_analytics/.venv/bin/python /opt/wholesale_analytics/scripts/backup.py >> /opt/wholesale_analytics/logs/backup.log 2>&1

Nginx Reverse Proxy
-------------------

Serve Flask via Gunicorn behind Nginx.

1) Copy the provided config and adjust `server_name` and paths if needed:
   sudo cp deploy/nginx_wholesale.conf /etc/nginx/sites-available/wholesale_analytics

2) Enable the site (Debian/Ubuntu layout):
   sudo ln -sf /etc/nginx/sites-available/wholesale_analytics /etc/nginx/sites-enabled/wholesale_analytics

3) Ensure Gunicorn is running locally (e.g.):
   cd /opt/wholesale_analytics
   /opt/wholesale_analytics/.venv/bin/gunicorn -c gunicorn_conf.py wsgi:app

4) Test Nginx config and reload:
   sudo nginx -t
   sudo systemctl reload nginx

The config proxies requests to `http://127.0.0.1:8000` and serves static files from `/opt/wholesale_analytics/app/static/`.
After any production rsync/copy into `/opt/wholesale_analytics`, re-apply nginx static ACLs so `/static/*` does not start returning `403`:
  sudo bash scripts/fix_static_acls.sh /opt/wholesale_analytics
  bash scripts/check_static_assets.sh http://127.0.0.1

Systemd Service
---------------

Run Wholesale Analytics as a systemd service.

1) Copy the unit file:
   sudo cp deploy/wholesale_analytics.service /etc/systemd/system/wholesale_analytics.service

2) Reload units and enable service on boot, start now:
   sudo systemctl daemon-reload
   sudo systemctl enable --now wholesale_analytics

3) Check status and logs:
   systemctl status wholesale_analytics
   journalctl -u wholesale_analytics -f

With Gunicorn bound to `127.0.0.1:8000` and Nginx configured, the app will be reachable via your server name.

Data Loader
-----------

- Configure MSSQL env vars in `wholesale_analytics/.env`:
  - `MSSQL_SERVER`, `MSSQL_DB` (default Wholesale Analytics)
  - EITHER set `MSSQL_TRUSTED=true` (Windows auth) OR provide `MSSQL_USER` and `MSSQL_PASSWORD` with `MSSQL_TRUSTED=false`
  - Optional: `ODBC_DRIVER` (default "ODBC Driver 18 for SQL Server")

- Generate/refresh parquet:
  - `python -m wholesale_analytics.data_loader`
  - Output path defaults to `PARQUET_PATH` env (default `cache/fact_dataset`)

- Notes:
  - Requires an ODBC SQL Server driver installed on the host.
  - `pyodbc` is included in requirements; on Windows it uses installed ODBC driver.

Analytics Cache & Incremental Refresh
-------------------------------------

- Dataset directory is `cache/fact_dataset/` with manifest at `cache/fact_dataset/_manifest.json`.
- ETL state (watermark + last_success) is tracked in `cache/etl_state.json`.
- One-time build: `python run.py build-fact --start 2017-01-01 --end today`.
- Incremental refresh (manual): `python run.py refresh-fact --once`.
- Continuous refresh (loop): `python run.py refresh-fact --loop --interval 300`.
- In development, `python run.py` starts one in-process incremental refresh loop by default.
- Disable the in-process loop with `ENABLE_INPROCESS_REFRESH=0`.
- In production, the in-process loop is disabled by default.
- Enable explicitly with `ENABLE_INPROCESS_REFRESH=1`.
- If running under Gunicorn, also set `ALLOW_GUNICORN_INPROCESS_REFRESH=1`.
- Tunables: `FACT_REFRESH_INTERVAL_SECONDS`, `FACT_REFRESH_LOOKBACK_DAYS`, `FACT_REFRESH_JITTER_SECONDS`.
- Web workers are read-only; no in-process refresh loops run inside Gunicorn.
- Dashboards read only from DuckDB over parquet partitions; date filters prune partitions on read so filter changes never hit SQL.

Products Parquet Cache
----------------------

- Env vars:
  - `DATA_DIR`: base directory for cached artifacts (default `cache/` under repo root)
  - `PRODUCTS_PARQUET_PATH`: explicit products parquet file path (default `<DATA_DIR>/products.parquet`)
  - `AUTO_CREATE_PRODUCTS_PARQUET`: create placeholder parquet when missing (default `true` in development, `false` in production)
  - `PRODUCTS_PARQUET_SCHEMA_VERSION`: schema tag written to the sidecar meta file
- Generate/refresh products parquet:
  - `python manage.py build-products-parquet [--output <path>] [--source snapshot|live]`
  - Uses `data_loader.load_snapshot()` by default; pass `--source live` to hit SQL if available.
  - Exits non-zero with instructions if no source data is available.
- Layout:
  - Default cache path: `cache/products.parquet`
  - Metadata sidecar: `cache/products.parquet.meta.json`

Labor Analytics
---------------

- Required environment variables are intentionally secret-free in source control:
  - `SYNERION_BASE_URL`
  - `SYNERION_USERNAME`
  - `SYNERION_PASSWORD`
  - `SYNERION_API_KEY`
  - `SYNERION_SUBDOMAIN`
  - `SYNERION_APP_REGION`
  - `SYNERION_PER_PAGE`
  - `LABOR_START_DATE`
  - `LABOR_PARQUET_PATH`
  - `LABOR_RAW_PATH`
  - `LABOR_INCREMENTAL_DAYS`
  - `LABOR_RECENT_RELOAD_DAYS`
- Labor parquet dataset:
  - Partitioned dataset directory defaults to `cache/labor/fact_dataset/`
  - Raw landing files default to `cache/labor/raw/`
  - Manifest lives at `cache/labor/fact_dataset/_manifest.json`
  - ETL state lives at `cache/labor/labor_etl_state.json`
- Backfill and refresh commands:
  - `python run.py build-labor --start 2022-01-01 --end today`
  - `python run.py refresh-labor --once --mode incremental`
  - `python run.py refresh-labor --once --mode recent-repair`
  - `flask --app wsgi:app labor-refresh --mode backfill --start 2022-01-01`
- Incremental behavior:
  - Recent windows are reloaded instead of append-only trust.
  - Default repair window is controlled by `LABOR_RECENT_RELOAD_DAYS` (recommended 30-60 days).
  - Detailed transaction grain is one row per employee-day-time-transaction row, with parent-day paid hours allocated across nested transactions to keep department totals accurate.
- App surface:
  - Labor page route: `/labor/`
  - Export routes: `/labor/export/snapshot`, `/labor/export/detail`, `/labor/export/department-summary`, `/labor/export/watchlist`
  - Page filters: date range, department, employee, time category, status, work rule, search

Project Layout
--------------

- `wholesale_analytics/app/` Flask application package with blueprints and templates
- `wholesale_analytics/data_loader.py` placeholder to be replaced with production loader
- `wholesale_analytics/wsgi.py` exposes `app` for `flask run` or `uvicorn` import

Velocity API
------------

The Velocity module provides usage/velocity analytics endpoints.

- GET `/api/velocity/summary`
  - Params: `start`, `end`, lists `regions[]`, `methods[]`, `customers[]`, `suppliers[]`, `products[]`, `metric=units|lb`
  - Returns KPI dictionary including products_active, total_usage_units/lb, avg weekly velocities, and movers lists.

- GET `/api/velocity/series`
  - Params: `metric=units|lb`, `freq=W|M|Y`, filters as above
  - Returns `{ x: [iso], y: [values], rolling: { w4, w8, w13 }, meta }`
  - x is sorted ascending and padded to include missing periods with zeros.

- GET `/api/velocity/product/<product_id>`
  - Returns per-product weekly series (units, lb) with rolling windows and a snapshot of last 4/8/13-week averages.

- GET `/api/velocity/forecast/<product_id>`
  - Params: `metric=units|lb`, any of `horizon_weeks`, `horizon_months`, `horizon_years`
  - Returns `{ history: [{ds,y}], forecast: [{ds,yhat,yhat_lower,yhat_upper}], meta: { model, horizon } }`

- GET `/api/velocity/search/products`
  - Params: `q`, `limit` (default 20, max 50), `offset` (default 0)
  - Returns fuzzy results: `[{ id, label: "SKU  Name (Supplier)", sku, name, supplier, category }]`

- GET `/api/velocity/export`
  - Params: `format=csv|xlsx`, `metric`, `freq`, filters
  - Streams file with filename `velocity_<metric>_<freq>_<yyyy-mm-dd>.<ext>`

Notes
- All endpoints are authenticated.
- 30s cache for idempotent GETs; 204 returned for empty datasets; invalid params 400.
- Guardrails: max 5y date span, max 100 in selection lists.

Regions Bundle (DuckDB)
-----------------------

- Regions bundle: GET `/api/regions/bundle`
- Regions drilldown bundle: GET `/api/regions/drilldown/bundle?region_id=<id>`
- Both endpoints are DuckDB-first (pushdown predicates, no pandas full-frame materialization) and run 3 tagged DuckDB queries per request.
- Server pagination, sort, and search are supported via `page`, `page_size`, `sort`, `sort_dir`, `search`, and `topN`.

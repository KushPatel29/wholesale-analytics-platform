# 🚑 AI Troubleshooting & Self-Healing Guide

Use this guide to diagnose and fix common repo-level issues autonomously.

## 🗄️ Database & Parquet Issues
- **Error:** `duckdb.IOException: Could not set lock on file`
  - **Fix:** Check for stale `data/*.parquet.tmp` or `data/*.lock`. Kill any orphaned `python3` processes.
- **Error:** `AttributeError: 'NoneType' object has no attribute 'columns'`
  - **Fix:** The Parquet file is missing or empty. Run the ETL job (`python data_loader.py`).
- **Error:** `BinderException: Column "X" not found`
  - **Fix:** The Parquet schema changed. Update the service logic or re-run the ETL with the correct mapping.

## 🔐 Auth & RBAC Issues
- **Error:** `403 Forbidden` on a valid route.
  - **Fix:** Check `app/core/access_policy.py`. Ensure the user's role is in the allowed list for that route.
- **Error:** `Empty scope returned for user.`
  - **Fix:** Check `app/core/rbac.py`. The user likely has no `allowlist` entries for the requested entity.

## 🧪 Test & Lint Failures
- **Error:** `ModuleNotFoundError: No module named 'app'`
  - **Fix:** Ensure you are running from the root and using `python3 -m pytest`.
- **Error:** `make lint` fails on formatting.
  - **Fix:** Run `make format`.

## 🌐 Frontend Issues
- **Error:** `bundle is not defined` in JS console.
  - **Fix:** Ensure the payload is being correctly passed to the template via `json_safe` in the blueprint.

---
*Always consult the graphify report (\`graphify-out/GRAPH_REPORT.md\`) to see which nodes are most connected to the failing component.*

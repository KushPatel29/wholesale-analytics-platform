"""
Root conftest: isolate the test suite from local developer state.

pytest imports this before `tests/conftest.py`, which is the only point early
enough to fix the environment before the app package is imported.

Two things used to leak in and change the results:

1. **A local `.env`.** The README tells you to `cp .env.demo .env` to run the
   demo, and that file turns on every v2/v3 feature flag. Tests written against
   the shipped defaults then exercise different templates and code paths, and
   the suite went from 52 failures to over 250 depending on whether you had run
   the demo.

2. **A generated demo dataset.** `cache/fact_dataset` is gitignored, so it
   exists on a machine that has run the seeder and not in CI. Tests that assert
   on empty-dataset behaviour pass in one place and fail in the other.

Both are pinned to known values here, so `pytest` gives the same answer on a
clean clone, on a developer machine mid-demo, and in CI.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Skip .env entirely. Both create_app() and data_loader honour this; without
# it, a developer who has run the demo tests a completely different
# application from the one CI tests.
os.environ["WA_IGNORE_DOTENV"] = "1"

os.environ["DIRECT_SQL_ONLY"] = "false"
os.environ["ADMIN_PORTAL_ENABLED"] = "1"
os.environ["AUTHZ_ENFORCEMENT"] = "0"
os.environ["AUTHZ_ENFORCEMENT_MODE"] = "warn"
os.environ["LABOR_ANALYTICS_ENABLED"] = "1"

# Deliberately NOT pinned here: the dataset path. Many fixtures monkeypatch
# PARQUET_PATH to their own tmp_path parquet, and the resolver reads
# `FACT_DATASET_PATH or PARQUET_PATH` - so setting either one globally wins
# over those fixtures and every test that builds its own dataset dies with
# DatasetNotBuiltError. The demo config's dataset entries are dropped instead,
# leaving the application's own default in place.
os.environ.pop("FACT_DATASET_PATH", None)
os.environ.pop("PARQUET_PATH", None)

# Feature flags: the shipped defaults, not the demo's. Tests that want a flag
# on set it themselves via monkeypatch or app.config.
for _flag in (
    "OVERVIEW_V2",
    "OVERVIEW_V3",
    "OVERVIEW_FORECAST_V2",
    "OVERVIEW_MOVERS_FAST",
    "PRODUCTS_V3",
    "PRODUCTS_V4",
    "PRODUCT_INTELLIGENCE_V2",
    "PRODUCT_DRILLDOWN_V2",
    "PRODUCT_FORECAST_V1",
    "SUPPLIER_DRILLDOWN_V2",
    "SALESREPS_V2",
    "SALESREP_DRILLDOWN_V2",
    "CUSTOMERS_KPIS_V2",
    "CUSTOMERS_KPIS_V3",
    "CUSTOMERS_RFM_V2",
    "CUSTOMERS_CLV_V2",
    "RETURNS_ENABLED",
    "RETURNS_FINAL_V1",
    "RETURNS_ANALYTICS",
    "NOTIFICATIONS_ENABLED",
    "AI_ENABLED",
):
    os.environ[_flag] = "0"

# Tests create, mutate and reset users. Sharing the demo's auth.db meant a
# pytest run silently rewrote the demo logins' password hashes, so `python
# manage.py seed-demo-users` had to be re-run before the demo worked again.
os.environ["AUTH_DB_PATH"] = (ROOT / ".pytest_tmp" / "auth_test.db").as_posix()
(ROOT / ".pytest_tmp").mkdir(exist_ok=True)

# Deterministic app config for tests.
os.environ.setdefault("SECRET_KEY", "test-only-not-a-secret")
os.environ.setdefault("FLASK_ENV", "testing")
os.environ.setdefault("WTF_CSRF_ENABLED", "false")
os.environ.setdefault("WA_FAST_PWHASH", "1")
os.environ.setdefault("AUTO_REFRESH_ENABLED", "false")
os.environ.setdefault("ENABLE_INPROCESS_REFRESH", "0")
os.environ.setdefault("ENABLE_SSE", "false")
os.environ.setdefault("RATELIMIT_ENABLED", "false")

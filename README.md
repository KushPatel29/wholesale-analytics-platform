# Wholesale Analytics

[![ci](https://github.com/KushPatel29/wholesale-analytics-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/KushPatel29/wholesale-analytics-platform/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.12-blue)
![duckdb](https://img.shields.io/badge/engine-DuckDB-yellow)

The internal BI platform I built and ran at a perishable-goods wholesale
distributor. Not a dashboard — the whole thing: incremental ETL off the ERP
into a partitioned parquet lake, a DuckDB query layer, row-level security down
to a sales rep's own book, and nine dashboards that the sales floor, the buyers
and the GM actually used.

It normally reads a SQL Server the reader does not have. So it now ships a
seeded generator that invents a comparable distributor — 620 accounts, 880
SKUs, 326,000 order lines — and writes it through the **same ETL writer**
production uses. A clean clone gets a working platform in about a minute.

```bash
pip install -r requirements.txt
python -m seed.generate_synthetic_data     # ~18s, writes cache/fact_dataset
cp .env.demo .env
python manage.py init-auth-db && python manage.py seed-demo-users
python run.py                              # http://127.0.0.1:5057
```

Log in as `gm` / `demo-password-1234`, then log in as `rep.dana` and watch the
same pages narrow to one rep's accounts.

---

## What it is

| | |
|---|---|
| **Scale** | 259 Python files, ~157k lines, 85 templates, 32 runbooks |
| **Structure** | 20 blueprints, 47 services, 107 test files |
| **Engine** | Flask + DuckDB over hive-partitioned parquet |
| **Access** | Role permissions, row-level scoping, cost masking |
| **Demo data** | 620 customers · 880 SKUs · 326k order lines · 24 months |

**Dashboards:** Overview (executive), Customers (KPIs, RFM, CLV, cohorts),
Products (with drilldown and forecasting), Suppliers, Regions, Sales Reps,
Labor, Returns (RMA intake with two-step approval and PDF export), and an
admin portal for users, roles and visibility.

---

## The architecture worth explaining

**The fact dataset is parquet, partitioned by day, queried by DuckDB.** The ETL
writes `cache/fact_dataset/year=/month=/day=/part-0.parquet` plus a manifest,
and every service queries a DuckDB view over `parquet_scan(...)` with hive
partitioning on, so a date-filtered page only touches the days it needs.

**Revenue is derived, never stored.** The fact table holds pack weights, pack
counts, a unit-of-billing flag and per-unit prices. The view computes revenue
as `pack_weight × price` for catch-weight items and `pack_units × price` for
everything else. Catch-weight is the whole problem in protein distribution — a
ribeye is billed on the weight that actually shipped, a case of portioned
chicken is not — and storing a revenue column would let the two definitions
drift apart.

**The bundle pattern.** Each page is served by one server-side payload builder
rather than a dozen chatty endpoints, and the JSON export path is the same
builder. An export can't disagree with the screen because it isn't computed
separately.

**Incremental refresh with upserts.** `etl/partition_writer.py` rewrites only
the partitions a batch touches, normalises primary keys across the int/float/
string drift real ERP extracts have, and moves rows between partitions without
leaving duplicates behind. The seeder calls this same function — the demo data
is not loaded through a special path.

---

## Finding a row-level security bypass

The access model has two halves: **scoping** (which rows you can see) and
**masking** (which columns). Every bundle service resolves the caller's scope
and passes it to `fact_store.build_where_clause`.

`overview_v2` did not. It builds its own SQL against a raw DuckDB connection,
and its `_where_clause` assembled date, filter and status predicates and
stopped there. So the newest version of the overview — **the page every user
lands on first** — returned company-wide totals to a rep scoped to their own
book. Masking still worked, which is what made it easy to miss: costs came back
correctly hidden, on rows the user should never have seen.

Measured on the seeded dataset, before and after:

| Login | Scope | Revenue seen (before) | After |
|---|---|---|---|
| `gm` | unrestricted | $64,754,267 | $64,754,267 |
| `manager.coast` | 2 regions | $64,754,267 | **$30,921,878** (47.8%) |
| `rep.dana` | rep R01 | $64,754,267 | **$15,993,410** (24.7%) |
| `rep.tomasz` | rep R04 | $64,754,267 | **$10,432,605** (16.1%) |

`rep.dana`'s 24.7% lines up with the 26% book share the generator planted,
which is the check that the fix scopes to the right rows rather than merely
fewer of them.

Five regression tests cover it, including an AST assertion that
`_where_clause` actually *calls* the scope helper — a test that the predicate
exists would have passed against the broken version too.

---

## Seven more defects, found by turning on the right lint rules

CI had been red for every run since March. Two of its five steps could not have
passed: `pip install .` with no `[project]` table, and `ruff .`, which is not a
valid ruff invocation. With the correctness rules (`F`, `E9`, selected `B`)
actually running, they surfaced quickly. Each of these hid behind a bare
`except` or an unreachable branch, which is why 700 passing tests never caught
them:

| Defect | Consequence |
|---|---|
| `Response` and `datetime` used but never imported in two returns export routes | `/returns/<id>/export/sage.csv` was a guaranteed 500 |
| `timezone` used in `datetime.now(timezone.utc)`, never imported | `NameError` in the churn model trainer |
| `_cache_version()`'s fallback referenced an undefined constant | a failure in `current_data_version()` raised out of the function instead of degrading — taking every products page with it |
| `order_col` fell back to `key_col` one line before `key_col` was assigned | `UnboundLocalError` whenever no order column resolved |
| `cost_col` / `qty_col` never resolved in `build_customers_bundle` | CLV cost flag and the monthly trend query raised into an `except` and silently degraded |
| `regions_bundle` never imported in the stakeholder report | the region-performance section was **always empty** |
| `List` used in an annotation, never imported | latent |

Plus 292 lines of dead code stranded after early returns in three rewritten
handlers, which is where four of the undefined names lived.

---

## Access control you can log into

Six demo logins, each exercising a different slice:

| Login | Role | Sees |
|---|---|---|
| `admin` | admin | everything, plus the admin portal |
| `gm` | gm | everything, no admin portal |
| `manager.coast` | sales_manager | Lower Mainland + Vancouver Island only |
| `rep.dana` | sales | Dana Whitfield's accounts only |
| `rep.tomasz` | sales | Tomasz Bielski's accounts only |
| `viewer.nocost` | warehouse | every row, but cost and margin come back `null` |

Password for all six: `demo-password-1234`.

`viewer.nocost` is the interesting one. Cost masking is enforced in the payload,
not the template — the API returns `cost: null` and `margin_pct: null` rather
than rendering a blank cell over a value that was sent to the browser anyway.

---

## What the demo data says

The generator plants findings rather than asserting them, so the dashboards
have something real to surface:

> **Beef is 31% of revenue at 11.2% margin, and falling.** Landed cost carries
> inflation the list price does not, and the quarterly trend shows the squeeze:
> 14.4% → 13.0% → 12.5% → 12.0% → 10.7% → 10.2% → 8.8% → 8.5%. Charcuterie, at
> 8% of revenue, earns 24.1%.

> **Third-party LTL runs a 24% late rate against 3–5% for own fleet** — and it
> is the only lane serving the far regions, so the delivery problem is a
> routing decision, not a carrier problem.

Blended margin is 16.2% on $61M a year, which is where a protein wholesaler
should sit. None of this is asserted in a README and hoped for: it falls out of
the catalog in `seed/catalog.py`, and the numbers above were read back out of
DuckDB after generation.

---

## Running it

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows
source .venv/bin/activate         # macOS / Linux
pip install -r requirements.txt

python -m seed.generate_synthetic_data
cp .env.demo .env
python manage.py init-auth-db
python manage.py seed-demo-users
python run.py --fast
```

**Tests:**

```bash
pytest -q
```

The suite is isolated from local state by the root `conftest.py`. That matters
more than it sounds: the demo config turns on every v2/v3 feature flag, and
running the suite with `.env` in place used to produce over 250 errors that
vanished when you deleted the file. Tests now pin the flags and point the
dataset path somewhere the demo data cannot leak in, so a clean clone, a
machine mid-demo, and CI all agree.

**The whole promise, checked:**

```bash
python scripts/demo_smoke.py
```

Generates nothing, assumes the dataset and logins exist, then walks all 16
pages, asserts the overview returns real numbers, asserts each scoped login
sees a strict subset of revenue and customers, and asserts `viewer.nocost` gets
`null` for cost. CI runs it on every push, so "a clean clone works" is checked
rather than claimed.

---

## Deliberate omissions

**The labor page is off in the demo.** It reads a workforce-management API with
no synthetic equivalent. Faking a source system is worse than switching the
page off and saying so.

**The AI assistant is off.** It needs a local model server. The code is here;
the demo does not pretend to run it.

**The stylistic lint rules are advisory, not gating.** Ruff's full ruleset
reports ~8,100 findings on 157k lines — mostly `UP006` typing modernisation and
`PLR2004` magic numbers. Gating CI on those would mean a 3,000-file mechanical
diff that reviews as noise. CI gates on the rules that find defects and reports
the rest.

---

## Notes

Built at a Vancouver wholesale distributor. All employer identifiers, customer
names, supplier names and cost data have been removed — the git history starts
at the de-branded import, and every number in this repo is generated from
`seed/catalog.py`.

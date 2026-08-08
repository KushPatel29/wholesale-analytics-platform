# Northgate Retail Analytics

[![ci](https://github.com/KushPatel29/wholesale-analytics-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/KushPatel29/wholesale-analytics-platform/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.12-blue)
![duckdb](https://img.shields.io/badge/engine-DuckDB-yellow)

**Live demo:** [wholesale-analytics-platform.onrender.com](https://wholesale-analytics-platform.onrender.com/)
— the login page lists six accounts and what each one is allowed to see. Start
with `gm`, then sign in as `rep.dana` and watch every figure narrow to one
rep's book. It sleeps when idle, so the first request can take about a minute.

A merchandising and replenishment analytics platform for a mass retail
chain. Not a dashboard — the whole thing: incremental ETL off the source
system into a partitioned parquet lake, a DuckDB query layer, row-level
security down to one market manager's own store book, and nine dashboards
built for the buyers, the market managers and the GM.

It normally reads a SQL Server the reader does not have. So it ships a seeded
generator that invents a comparable chain — **Northgate Retail Group**, a
supercenter operator — and writes it through the **same ETL writer** production
uses. A clean clone gets a working platform in about a minute.

The catalogue is a supercenter's: ten merchandising departments from Grocery
and Fresh through to Electronics and Toys & Seasonal, a private-label brand
ladder, stores across seven US regions, CPG vendors, and both ways a retailer
sells — items priced per each, and fresh items priced per pound and rung up on
a scale. That second case is not decoration: revenue is *derived* from the
billing basis rather than stored, which is the schema decision the whole query
layer is built around.

Two findings are planted in the generator, because a dashboard that finds
nothing is not worth looking at:

* **Electronics is the largest department by revenue and earns the thinnest
  margin in the chain** — it looks like a growth engine on a revenue chart and
  drags blended margin everywhere else.
* **Apparel and Toys & Seasonal have the worst sell-through**, which is a
  markdown problem rather than a demand problem: it shows up in margin and in
  the SKU watchlist, never in the sales line.

The hosted demo runs a smaller cut of the same generator (150 stores, 220
SKUs, four months) because it lives on a 512 MB shared-CPU box, so its totals
are smaller than the ones quoted below. The shape of the data, and every
finding, is identical.

**A note on column names.** The warehouse columns still carry the source
system's vocabulary — `ProteinType` holds the department, `YieldPct` holds
sell-through, `CostPerLb` holds cost per selling unit. Renaming a source
system's columns to match a re-org is how you break every downstream report, so
the columns stay and `seed/catalog.py` documents the mapping.

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
| **Demo data** | 620 stores · 880 SKUs · 326k order lines · 24 months |

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
everything else. Scale-weighed items are the whole problem in retail fresh — a
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

> **Electronics is 24% of revenue at 12.7% margin — the thinnest department in
> the chain.** It is the single largest line on a revenue chart and the reason
> blended margin sits where it does. For comparison, Apparel earns 40.4% on 9%
> of revenue and Home & Kitchen 35.8% on 12.8%. A category plan built on a
> single blended target will keep rewarding the department that earns least.

> **Third-party LTL runs a 24% late rate against 3.0% for the ambient DC
> fleet** — and it is the only lane serving the outlying regions, so the
> replenishment problem is a network-design decision, not a carrier problem.
> Drop-ship is second worst at 11%.

Blended margin is 24.2% on $151M across the 24-month window. None of this is
asserted in a README and hoped for: it falls out of the catalog in
`seed/catalog.py`, and every number above was read back out of DuckDB after
generation.

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

Ported from an internal platform I built and ran. All employer identifiers, customer
names, supplier names and cost data have been removed — the git history starts
at the de-branded import, and every number in this repo is generated from
`seed/catalog.py`.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update -y \
    && apt-get install -y --no-install-recommends build-essential unixodbc-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY . .

# Package a self-contained portfolio demo. The generated dataset and demo
# authentication database contain synthetic data only.
#
# Sized for a 512 MB container, not a laptop. The platform OOM-kills the whole
# container when it is exceeded - no traceback, no worker-exit line, just a
# fresh gunicorn master in the logs - so every memory multiplier matters: rows
# scanned, frames materialised per request, and how many requests can be in
# flight at once.
#
# Three whole fiscal years, not ten months.
#
# Ten months meant every customer's first order fell inside a two-week band and
# there was no prior year to compare against, so the dashboard reported +896%
# revenue growth, "10 up / 0 down" and "no decliners" - a book in which nothing
# ever declines, which is recognisable as synthetic on sight. Three fiscal years
# gives the year-over-year column something true to say and the cohort, RFM and
# churn views something to show.
#
# 110 stores rather than 80: a region holds seven accounts at 80, so a single
# new logo moved a whole territory 113% year-over-year and the regional story
# came down to which accounts were drawn as gainers.
#
# 320 SKUs rather than 180: department weights are as low as 2.5%, so at 180
# SKUs Electronics had four of them and one loss-leader took the entire
# department's margin to zero - an artefact that reads as a finding.
#
# 24 suppliers rather than the default 46, so each carries an assortment worth
# analysing instead of four SKUs.
#
# ~95k order lines, ~50 MB on disk. Queries are date-partitioned and the
# default window is one fiscal year, so a page scans roughly a third of it.
#
# products.parquet is built here too: without it every products request takes a
# FileNotFoundError path before falling back.
RUN cp .env.demo .env \
    && python -m seed.generate_synthetic_data --fiscal-years 3 --customers 110 --products 320 --suppliers 24 \
    && python -m seed.generate_synthetic_labor --days 730 \
    && python manage.py init-auth-db \
    && python manage.py seed-demo-users \
    && (python manage.py build-products-parquet || echo "products parquet skipped") \
    && python -m seed.generate_synthetic_finance \
    && python -m seed.generate_synthetic_marketing \
    && ENV=development FLASK_ENV=development SECRET_KEY=demo-build-seed-only-not-a-runtime-secret \
       python -m seed.generate_synthetic_returns --count 140 --replace \
    && ENV=development FLASK_ENV=development SECRET_KEY=demo-build-cache-only-not-a-runtime-secret \
       DEMO_PREBUILT_CACHE_DIR=/app/cache/demo-prebuilt python manage.py precompute-demo-cache

# Two threads rather than four: each in-flight request materialises pandas
# frames, so thread count multiplies peak memory directly.
#
# One worker, and gunicorn_conf.py therefore does NOT preload. Preloading built
# the app in the arbiter (~185 MB) and forked a worker whose pages stopped being
# shared as soon as CPython touched a refcount, so the container carried two
# copies of the app before serving anything. It also stranded the warm-up
# thread in the arbiter - threads do not survive fork - so the worker answered
# every request from caches nothing had warmed. See gunicorn_conf.py.
#
# One request thread, not two. `fact_store` keeps its DuckDB connection in a
# `threading.local`, so the memory limit below is charged *per thread*, not per
# process - two request threads plus the warm-up thread meant three in-memory
# databases and three times the cap. Serialising requests costs latency on a
# page that fires several XHRs at once; it is the cheaper half of the trade
# against an OOM kill, which restarts the container and takes every warmed
# cache with it.
ENV PORT=10000 \
    GUNICORN_WORKERS=1 \
    GUNICORN_THREADS=1 \
    GUNICORN_TIMEOUT=180

# DuckDB defaults to a 2 GB memory limit, which on a 512 MB container means the
# platform kills the container instead of DuckDB spilling. Cap it well under
# the container limit and keep it single-threaded so query threads do not
# multiply peak memory.
#
# The cap is charged per connection and there is one per thread, so the number
# that matters is this times the thread count above, not this on its own.
#
# It also only behaves like a cap if DuckDB can spill. It defaults to spilling
# into `.tmp` relative to the working directory - `/app` here, the application's
# own directory - and when that is not writable a query that crosses the limit
# raises OutOfMemoryException rather than slowing down. Pin it to /tmp so the
# limit is a budget, which is what the comment above always assumed it was.
ENV DUCKDB_MEMORY_LIMIT=112MB \
    DUCKDB_THREADS=1 \
    DUCKDB_TEMP_DIRECTORY=/tmp/duckdb \
    DUCKDB_MAX_TEMP_DIRECTORY_SIZE=2GB

# The synthetic dataset is immutable inside this image, so its default bundles
# are precomputed in the build layer above. Reading them is a small file load;
# the old boot-time warm-up competed with the first visitor for the free CPU
# and made cold starts slower even though it eventually helped later visits.
ENV DEMO_PREBUILT_CACHE_DIR=/app/cache/demo-prebuilt \
    DEMO_PREBUILT_CACHE_READ=1 \
    DEMO_PREBUILT_CACHE_WRITE=0 \
    DEMO_WARMUP=0 \
    DEMO_WARMUP_SECONDARY=0 \
    CACHE_THRESHOLD=16 \
    FACT_FRAME_CACHE_MAXSIZE=0 \
    FACT_QUERY_CACHE_MAXSIZE=2 \
    FACT_QUERY_CACHE_TTL=30 \
    FACT_CACHE_MAX=2 \
    BUNDLE_CACHE_MAXSIZE=8 \
    OVERVIEW_BUNDLE_CACHE_MAXSIZE=2 \
    PRODUCTS_FRAME_CACHE_MAXSIZE=0 \
    PRODUCTS_PAYLOAD_CACHE_MAXSIZE=2 \
    REGIONS_FRAME_CACHE_MAXSIZE=0 \
    SUPPLIERS_FRAME_CACHE_MAXSIZE=0 \
    SUPPLIERS_KPI_CACHE_MAXSIZE=2 \
    SUPPLIERS_TABLE_CACHE_MAXSIZE=2 \
    SUPPLIERS_DRILLDOWN_CACHE_MAXSIZE=2 \
    SUPPLIERS_ANALYTICS_CACHE_MAXSIZE=2 \
    LABOR_ANALYSIS_CACHE_MAXSIZE=2 \
    LABOR_FOCUS_CACHE_MAXSIZE=2 \
    LABOR_PAGE_CACHE_MAXSIZE=2 \
    LABOR_QUERY_CACHE_MAXSIZE=2

# "This is the public demo" - which is what makes the login page offer the
# one-click read-only account and print the credentials.
#
# This used to be read off DEMO_WARMUP, and warm-up moved into the build layer
# above and set it to 0. The side effect was that the deployed portfolio showed
# an anonymous visitor a username box with no way to fill it, which is the one
# failure this deployment cannot survive: its entire audience arrives logged
# out and leaves within seconds. Warm-up strategy and demo-ness are separate
# questions and now have separate flags.
ENV DEMO_MODE=1
ENV DEMO_STATIC_SITE_URL=https://kushpatel29.github.io/wholesale-analytics-platform

EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\",\"10000\")}/healthz', timeout=5).status == 200 else 1)"

CMD exec gunicorn -c gunicorn_conf.py --bind 0.0.0.0:${PORT} wsgi:app

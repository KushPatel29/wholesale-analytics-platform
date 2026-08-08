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
# 150 accounts and 220 SKUs over four months is ~11k order lines. Still a
# believable book with seasonality and a visible margin trend, and small enough
# that a page render and the background warm-up can overlap without tipping the
# container over.
#
# products.parquet is built here too: without it every products request takes a
# FileNotFoundError path before falling back.
RUN cp .env.demo .env \
    && python -m seed.generate_synthetic_data --months 4 --customers 150 --products 220 \
    && python manage.py init-auth-db \
    && python manage.py seed-demo-users \
    && (python manage.py build-products-parquet || echo "products parquet skipped")

# Two threads rather than four: each in-flight request materialises pandas
# frames, so thread count multiplies peak memory directly.
#
# One worker, and gunicorn_conf.py therefore does NOT preload. Preloading built
# the app in the arbiter (~185 MB) and forked a worker whose pages stopped being
# shared as soon as CPython touched a refcount, so the container carried two
# copies of the app before serving anything. It also stranded the warm-up
# thread in the arbiter - threads do not survive fork - so the worker answered
# every request from caches nothing had warmed. See gunicorn_conf.py.
ENV PORT=10000 \
    GUNICORN_WORKERS=1 \
    GUNICORN_THREADS=2 \
    GUNICORN_TIMEOUT=180

# DuckDB defaults to a 2 GB memory limit, which on a 512 MB container means the
# platform kills the container instead of DuckDB spilling. Cap it well under
# the container limit and keep it single-threaded so query threads do not
# multiply peak memory.
ENV DUCKDB_MEMORY_LIMIT=128MB \
    DUCKDB_THREADS=1

# Build the caches in the background at boot so no visitor pays for the first
# render of a page. Paced so the warm-up never starves a real request on a
# shared CPU, and limited to the documented demo login - warming all six cost
# more memory than it saved.
ENV DEMO_WARMUP=1 \
    DEMO_WARMUP_PACE_SECONDS=2 \
    DEMO_WARMUP_SECONDARY=0

EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\",\"10000\")}/healthz', timeout=5).status == 200 else 1)"

CMD exec gunicorn -c gunicorn_conf.py --bind 0.0.0.0:${PORT} wsgi:app

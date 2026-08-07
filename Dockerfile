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
# Sized for a 512 MB / shared-CPU host rather than a laptop: 200 accounts and
# 300 SKUs over six months is ~26k order lines, which still produces a
# believable book with seasonality and a margin trend, but builds every page
# in about a second per core instead of a minute.
RUN cp .env.demo .env \
    && python -m seed.generate_synthetic_data --months 6 --customers 200 --products 300 \
    && python manage.py init-auth-db \
    && python manage.py seed-demo-users

ENV PORT=10000 \
    GUNICORN_WORKERS=1 \
    GUNICORN_THREADS=4 \
    GUNICORN_TIMEOUT=180

# DuckDB defaults to a 2 GB memory limit, which on a 512 MB container means the
# kernel kills the worker instead of DuckDB spilling. Cap it well under the
# container limit, and keep the thread count low so query threads do not
# multiply peak memory.
ENV DUCKDB_MEMORY_LIMIT=192MB \
    DUCKDB_THREADS=2

# Build the caches in the background at boot so no visitor pays for the first
# render of a page.
ENV DEMO_WARMUP=1

EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\",\"10000\")}/healthz', timeout=5).status == 200 else 1)"

CMD exec gunicorn -c gunicorn_conf.py --bind 0.0.0.0:${PORT} wsgi:app

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
RUN cp .env.demo .env \
    && python -m seed.generate_synthetic_data --months 6 \
    && python manage.py init-auth-db \
    && python manage.py seed-demo-users

ENV PORT=10000 \
    GUNICORN_WORKERS=1 \
    GUNICORN_THREADS=4 \
    GUNICORN_TIMEOUT=180

EXPOSE 10000

CMD exec gunicorn -c gunicorn_conf.py --bind 0.0.0.0:${PORT} wsgi:app


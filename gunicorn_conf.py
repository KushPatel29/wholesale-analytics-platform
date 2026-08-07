import os

bind = os.getenv("GUNICORN_BIND", "0.0.0.0:8000")
workers = int(os.getenv("GUNICORN_WORKERS", "2"))
threads = int(os.getenv("GUNICORN_THREADS", "8"))
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
preload_app = True
accesslog = "-"
errorlog = "-"
capture_output = True

configured_worker_class = os.getenv("GUNICORN_WORKER_CLASS", "").strip()
if configured_worker_class:
    worker_class = configured_worker_class

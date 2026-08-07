import os

bind = os.getenv("GUNICORN_BIND", "0.0.0.0:8000")
workers = int(os.getenv("GUNICORN_WORKERS", 2))
threads = int(os.getenv("GUNICORN_THREADS", 8))
timeout = int(os.getenv("GUNICORN_TIMEOUT", 120))
preload_app = True
accesslog = "-"
errorlog = "-"
capture_output = True

worker_class = os.getenv("GUNICORN_WORKER_CLASS")
if worker_class:
    worker_class = worker_class.strip()
    if worker_class:
        globals()["worker_class"] = worker_class

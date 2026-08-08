import os

bind = os.getenv("GUNICORN_BIND", "0.0.0.0:8000")
workers = int(os.getenv("GUNICORN_WORKERS", "2"))
threads = int(os.getenv("GUNICORN_THREADS", "8"))
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))

# Preloading imports the app in the arbiter and forks workers from it, so
# several workers share one copy of pandas, DuckDB and the app graph. That is
# the right trade with several workers - and the wrong one with a single
# worker on a small box.
#
# The app is ~185 MB resident before it serves a request. Preloaded, the
# arbiter holds that 185 MB for the life of the process and the worker's own
# copy diverges from it almost immediately: CPython writes a refcount into
# every object header it touches, so copy-on-write pages stop being shared
# within minutes of real traffic. On a 512 MB container that is ~370 MB of
# resident duplication before any request is served, which is most of the
# reason the demo was being OOM-killed with no traceback.
#
# With one worker there is nothing to share, so skip the preload and let the
# worker be the only process holding the app. Override explicitly if you are
# running several workers somewhere larger.
_preload_env = os.getenv("GUNICORN_PRELOAD", "").strip().lower()
if _preload_env in {"1", "true", "yes", "on"}:
    preload_app = True
elif _preload_env in {"0", "false", "no", "off"}:
    preload_app = False
else:
    preload_app = workers > 1

accesslog = "-"
errorlog = "-"
capture_output = True

configured_worker_class = os.getenv("GUNICORN_WORKER_CLASS", "").strip()
if configured_worker_class:
    worker_class = configured_worker_class

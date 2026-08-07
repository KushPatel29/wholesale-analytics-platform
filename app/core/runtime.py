from __future__ import annotations

import os


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def is_dev_reloader_main_process(debug: bool | None = None) -> bool:
    """
    True when we're in the "real" Flask dev server process.

    - When debug/reloader is off, always True.
    - When debug/reloader is on, only True for the reloader child
      (WERKZEUG_RUN_MAIN == "true").
    """
    if debug is None:
        debug = _bool_env("FLASK_DEBUG", _bool_env("DEBUG", False))
    if not debug:
        return True
    return os.environ.get("WERKZEUG_RUN_MAIN") == "true"


def is_gunicorn_worker() -> bool:
    """Detect Gunicorn worker process via common env markers."""
    if os.getenv("GUNICORN_CMD_ARGS"):
        return True
    server_software = (os.getenv("SERVER_SOFTWARE") or "").lower()
    return "gunicorn" in server_software


def should_start_refresh_loop(*, debug: bool | None = None) -> bool:
    """
    Decide whether to start the in-process refresh loop.
    """
    env = (os.getenv("ENV") or os.getenv("FLASK_ENV") or "production").strip().lower()
    default_enabled = env != "production"
    enabled = _bool_env("ENABLE_INPROCESS_REFRESH", default_enabled)
    if not enabled:
        return False
    if not is_dev_reloader_main_process(debug=debug):
        return False
    if is_gunicorn_worker() and not _bool_env("ALLOW_GUNICORN_INPROCESS_REFRESH", False):
        return False
    return True

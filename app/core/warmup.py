"""
Prime the in-process caches after the server starts.

The bundle caches are keyed by user scope and filter state and live for hours,
so the expensive work is the *first* request to each page. On a laptop that
first hit costs about a second and nobody notices. On a small shared-CPU host
it costs tens of seconds, and a visitor who lands on Customers before anything
is cached waits the whole time - or the request outlives the worker timeout and
they get a 502 instead.

So: bind the port, answer the health check, and then walk the pages once in a
background thread against the app's own test client. That shares the process,
and therefore the caches, with real traffic. By the time anyone arrives the
work is already done.

Off unless DEMO_WARMUP is set, because it only makes sense where the dataset
ships with the image.
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# Ordered cheapest-first, so the pages a visitor is most likely to open are
# ready soonest.
WARMUP_PATHS: tuple[str, ...] = (
    "/",
    "/overview/api/bundle?_gf=1",
    "/products/",
    "/customers/",
    "/suppliers/",
    "/regions/",
    "/salesreps/",
    "/customers/kpis",
    "/customers/rfm",
    "/customers/clv",
    "/customers/cohorts",
)

# The bundle cache key includes the user id and their resolved scope, so a
# cache warmed as one login does nothing for another. The secondary logins get
# the core pages only - the underlying DuckDB query cache is shared wherever
# the generated SQL matches, so they cost far less than the first pass.
SECONDARY_PATHS: tuple[str, ...] = (
    "/",
    "/customers/",
    "/products/",
    "/salesreps/",
)

# The JSON bundles each page fetches after render. Warming the HTML alone was
# not enough: the page came back in a second or two and then sat waiting eight
# seconds for its bundle, because that is where the work actually happens.
BUNDLE_ENDPOINTS: tuple[str, ...] = (
    "/api/products/bundle",
    "/api/customers/bundle",
    "/api/suppliers/bundle",
    "/api/regions/bundle",
    "/api/salesreps/bundle",
)


def _default_window_query() -> str:
    """
    Reproduce the query string the front end builds for the default view.

    The pages default to the current fiscal year and compute the window in
    JavaScript, then send explicit start and end dates. Those dates are part of
    the cache key, so warming `?date_preset=current_fy` alone produces a
    different key and buys nothing - the visitor still pays full price. Derive
    the same window from the server's own fiscal helper instead.
    """
    try:
        import pandas as pd

        from app.services.filters import get_fiscal_periods

        periods = get_fiscal_periods()
        current = periods.get("current_fy") or {}
        start = current.get("start")
        end = current.get("end") or pd.Timestamp.utcnow()
        if start is None:
            return "date_preset=current_fy"
        return (
            f"start={pd.Timestamp(start).date().isoformat()}"
            f"&end={pd.Timestamp(end).date().isoformat()}"
            f"&date_preset=current_fy"
        )
    except Exception:
        logger.debug("warmup.window_resolution_failed", exc_info=True)
        return "date_preset=current_fy"


def _enabled() -> bool:
    return str(os.getenv("DEMO_WARMUP", "")).strip().lower() in {"1", "true", "yes", "on"}


def _warm_user(app, username: str, paths: tuple[str, ...]) -> bool:
    """Establish a session as one demo account and walk `paths`."""
    try:
        from app.auth.models import get_user_by_username

        user = get_user_by_username(username)
        if user is None:
            logger.warning("warmup.user_missing", extra={"user": username})
            return False
        user_id = str(user.id)
    except Exception:
        logger.warning("warmup.user_lookup_failed", extra={"user": username}, exc_info=True)
        return False

    try:
        with app.test_client() as client:
            # Seed the session directly rather than POSTing to /auth/login.
            # That route is rate limited to 5 requests a minute - correctly, it
            # is a login form - and warming six accounts needs twelve hits, so
            # going through it meant the later accounts were refused and left
            # cold. The session key is the same one Flask-Login sets.
            with client.session_transaction() as session:
                session["_user_id"] = user_id
                session["_fresh"] = False

            pace = float(os.getenv("DEMO_WARMUP_PACE_SECONDS", "0"))
            for path in paths:
                # Pause between pages so the warm-up never monopolises a shared
                # CPU. Without this it competes with whoever is already on the
                # site, and both the warm-up and their request slow to a crawl.
                if pace > 0:
                    time.sleep(pace)
                page_started = time.perf_counter()
                try:
                    page = client.get(path)
                    logger.info(
                        "warmup.page",
                        extra={
                            "user": username,
                            "path": path,
                            "status": page.status_code,
                            "duration_ms": int((time.perf_counter() - page_started) * 1000),
                        },
                    )
                except Exception:
                    logger.warning(
                        "warmup.page_failed",
                        extra={"user": username, "path": path},
                        exc_info=True,
                    )
    except Exception:
        logger.warning("warmup.failed", extra={"user": username}, exc_info=True)
        return False
    return True


def _warm(app) -> None:
    primary = os.getenv("DEMO_WARMUP_USER", "gm")
    delay = float(os.getenv("DEMO_WARMUP_DELAY_SECONDS", "3"))
    budget = float(os.getenv("DEMO_WARMUP_BUDGET_SECONDS", "600"))
    time.sleep(max(delay, 0.0))

    started = time.perf_counter()

    # The documented demo login goes first: every page, then the bundles those
    # pages fetch, with the same default window the front end sends.
    window = _default_window_query()
    primary_paths = WARMUP_PATHS + tuple(f"{ep}?{window}" for ep in BUNDLE_ENDPOINTS)
    _warm_user(app, primary, primary_paths)

    # Then the rest, so whichever account a visitor picks is already warm.
    # Bounded by a budget: on a slow host it is better to leave some accounts
    # cold than to keep a background thread busy indefinitely.
    #
    # Off by default on the smallest containers. Each account holds its own set
    # of cached bundles - the cache key includes the user and their scope - so
    # warming all six multiplies resident memory for a benefit only the second
    # visitor sees, and running out of memory costs everyone.
    warm_secondary = str(os.getenv("DEMO_WARMUP_SECONDARY", "1")).strip().lower() in {"1", "true", "yes", "on"}
    try:
        from .demo_accounts import DEMO_USERS

        others = [name for name in DEMO_USERS if name != primary] if warm_secondary else []
    except Exception:
        others = []

    for username in others:
        if time.perf_counter() - started > budget:
            logger.info("warmup.budget_reached", extra={"skipped_from": username})
            break
        _warm_user(app, username, SECONDARY_PATHS)

    logger.info(
        "warmup.complete",
        extra={"duration_ms": int((time.perf_counter() - started) * 1000)},
    )


def start_warmup(app) -> None:
    """Kick off the warm-up in a daemon thread; never blocks startup."""
    if not _enabled():
        return
    thread = threading.Thread(target=_warm, args=(app,), name="demo-warmup", daemon=True)
    thread.start()
    logger.info("warmup.scheduled", extra={"paths": len(WARMUP_PATHS)})

"""
The public demo's front door.

Two things make the hosted demo usable, and both are easy to break without
noticing because neither affects a local run:

- the login page has to tell an anonymous visitor how to get in;
- the caches have to be primed before anyone arrives, or the first request to
  a page on a small shared-CPU host outlives the worker timeout.

Both are gated on DEMO_WARMUP so a real deployment never advertises
credentials or warms anything.
"""

from __future__ import annotations


import logging
import os
import threading
import time

import pytest

from app import create_app
from app.core import demo_accounts, warmup


@pytest.fixture
def app():
    application = create_app()
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test")
    return application


class TestDemoCredentialsPanel:
    def test_hidden_by_default(self, app, monkeypatch):
        monkeypatch.delenv("DEMO_WARMUP", raising=False)
        monkeypatch.delenv("DEMO_MODE", raising=False)
        body = app.test_client().get("/auth/login").get_data(as_text=True)
        assert "auth-demo" not in body
        assert demo_accounts.DEMO_PASSWORD not in body, "never print credentials off the demo"

    def test_shown_on_the_demo_build(self, app, monkeypatch):
        monkeypatch.setenv("DEMO_WARMUP", "1")
        body = app.test_client().get("/auth/login").get_data(as_text=True)
        assert "auth-demo" in body
        assert demo_accounts.DEMO_PASSWORD in body
        for username in demo_accounts.DEMO_USERS:
            assert username in body, f"{username} missing from the demo panel"

    def test_every_advertised_login_has_a_description(self):
        for hint in demo_accounts.demo_login_hints():
            assert hint["note"].strip(), f"{hint['username']} has no explanation"

    def test_login_keeps_returns_destination(self, app, monkeypatch):
        monkeypatch.setenv("DEMO_MODE", "1")
        body = app.test_client().get("/auth/login?next=/returns/").get_data(as_text=True)
        assert "next=/returns/" in body or "next=/returns/%2F" in body


class TestStaticDemoRouting:
    def test_authenticated_workspace_uses_cdn(self, app, monkeypatch):
        from app.auth.models import get_user_by_username

        monkeypatch.setenv(
            "DEMO_STATIC_SITE_URL",
            "https://kushpatel29.github.io/wholesale-analytics-platform",
        )
        user = get_user_by_username(demo_accounts.DEMO_VIEWER_USERNAME)
        assert user is not None
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True
        response = client.get("/suppliers/?date_preset=current_fy")
        assert response.status_code == 302
        assert response.location == (
            "https://kushpatel29.github.io/wholesale-analytics-platform/"
            "suppliers/?date_preset=current_fy"
        )

    def test_returns_stays_on_live_host(self, app, monkeypatch):
        from app.auth.models import get_user_by_username

        monkeypatch.setenv("DEMO_STATIC_SITE_URL", "https://static.example.test")
        user = get_user_by_username(demo_accounts.DEMO_VIEWER_USERNAME)
        assert user is not None
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True
        response = client.get("/returns/")
        assert response.location != "https://static.example.test/returns/"


class TestCatalogueIsShared:
    def test_seeder_reads_the_same_catalogue(self):
        """
        manage.py used to carry its own copy, so a renamed account or changed
        password would leave the login page advertising credentials that no
        longer worked.
        """
        import manage

        assert manage.DEMO_USERS is demo_accounts.DEMO_USERS
        assert manage.DEMO_PASSWORD == demo_accounts.DEMO_PASSWORD


class TestWarmup:
    def test_disabled_unless_asked(self, monkeypatch):
        monkeypatch.delenv("DEMO_WARMUP", raising=False)
        assert warmup._enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
    def test_enabled_values(self, monkeypatch, value):
        monkeypatch.setenv("DEMO_WARMUP", value)
        assert warmup._enabled() is True

    def test_start_is_a_no_op_when_disabled(self, app, monkeypatch):
        monkeypatch.delenv("DEMO_WARMUP", raising=False)
        # Must not raise and must not spawn anything.
        warmup.start_warmup(app)

    def test_warms_the_pages_a_visitor_lands_on(self):
        """
        The overview, and the three heaviest pages, have to be in the list or
        the warm-up is not doing its job.
        """
        assert "/" in warmup.WARMUP_PATHS
        for path in ("/customers/", "/products/", "/suppliers/"):
            assert path in warmup.WARMUP_PATHS

    def test_secondary_paths_are_a_subset(self):
        assert set(warmup.SECONDARY_PATHS).issubset(set(warmup.WARMUP_PATHS))

    def test_pacing_and_secondary_warming_are_configurable(self, monkeypatch):
        """
        On a 512 MB container the warm-up has to be able to back off: pace
        itself so it does not starve real requests, and skip the secondary
        accounts, whose separately-cached bundles cost more memory than they
        save.
        """
        import inspect

        source = inspect.getsource(warmup)
        assert "DEMO_WARMUP_PACE_SECONDS" in source
        assert "DEMO_WARMUP_SECONDARY" in source

    def test_secondary_accounts_skipped_when_disabled(self, monkeypatch):
        monkeypatch.setenv("DEMO_WARMUP_SECONDARY", "0")
        calls = []
        monkeypatch.setattr(warmup, "_warm_user", lambda app, user, paths: calls.append(user) or True)
        warmup._warm(object())
        assert calls == ["gm"], f"only the primary login should be warmed, got {calls}"

    def test_secondary_accounts_warmed_when_enabled(self, monkeypatch):
        monkeypatch.setenv("DEMO_WARMUP_SECONDARY", "1")
        monkeypatch.setenv("DEMO_WARMUP_DELAY_SECONDS", "0")
        calls = []
        monkeypatch.setattr(warmup, "_warm_user", lambda app, user, paths: calls.append(user) or True)
        warmup._warm(object())
        assert len(calls) == len(demo_accounts.DEMO_USERS)

    def test_warms_the_bundles_the_pages_fetch(self):
        """
        Warming the HTML alone was not enough. Each page renders in a second or
        two and then waits on its JSON bundle, which is where the work happens
        - on the hosted demo that was an eight second wait after the page had
        already appeared.
        """
        endpoints = {endpoint for endpoint, _ in warmup.BUNDLE_ENDPOINTS}
        for endpoint in ("/api/products/bundle", "/api/customers/bundle"):
            assert endpoint in endpoints

    def test_bundle_warmup_sends_the_arguments_the_front_end_sends(self):
        """
        The cache key is built from the request arguments, so a products bundle
        warmed without `_sections` lands under a different key than the one the
        page asks for and the visitor still pays full price.
        """
        extras = dict(warmup.BUNDLE_ENDPOINTS)
        assert "_sections=" in extras["/api/products/bundle"]
        assert "page=1" in extras["/api/inventory/bundle"]
        assert "page_size=25" in extras["/api/inventory/bundle"]
        assert "sort_by=priority" in extras["/api/inventory/bundle"]
        assert all("date_type=fiscal" in v for v in extras.values())

    def test_does_not_warm_filter_options_endpoint(self):
        """Filter options are embedded in HTML; warming the old XHR hides regressions."""
        assert all("/api/filters/options" not in path for path in warmup.primary_paths())

    def test_bundle_warmup_uses_the_window_the_front_end_sends(self):
        """
        The pages compute the current-fiscal-year window in JavaScript and send
        explicit start and end dates, and those dates are part of the cache
        key. Warming `?date_preset=current_fy` on its own produces a different
        key and buys the visitor nothing.
        """
        query = warmup._default_window_query()
        assert "start=" in query and "end=" in query, f"no explicit window in {query!r}"
        assert "date_preset=current_fy" in query

    def test_does_not_go_through_the_rate_limited_login_route(self):
        """
        /auth/login is capped at 5 requests a minute. Warming six accounts
        through it needs twelve, so the later accounts were refused and left
        cold - the exact pages the warm-up exists to prepare.
        """
        import inspect

        source = inspect.getsource(warmup._warm_user)
        assert 'post("/auth/login"' not in source
        assert "session_transaction" in source


class TestWorkerModel:
    """
    The warm-up runs in a thread started by `create_app`, and threads do not
    survive `fork()`. Under `gunicorn --preload` that means the arbiter builds
    the app, starts the warm-up, warms its own caches, and then forks workers
    that begin cold and stay cold - while the box carries two resident copies
    of a ~185 MB app. That combination is what OOM-killed the hosted demo.

    Preloading only pays for itself with several workers sharing one import, so
    these pin the rule rather than the symptom.
    """

    @staticmethod
    def _load_conf(monkeypatch, **env):
        import importlib

        for key in ("GUNICORN_WORKERS", "GUNICORN_PRELOAD", "GUNICORN_THREADS"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        import gunicorn_conf

        return importlib.reload(gunicorn_conf)

    def test_single_worker_does_not_preload(self, monkeypatch):
        conf = self._load_conf(monkeypatch, GUNICORN_WORKERS="1")
        assert conf.preload_app is False, (
            "One worker plus --preload means two resident copies of the app and "
            "a warm-up thread stranded in a process that never serves a request."
        )

    def test_multiple_workers_still_preload(self, monkeypatch):
        conf = self._load_conf(monkeypatch, GUNICORN_WORKERS="4")
        assert conf.preload_app is True

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("1", True), ("true", True), ("0", False), ("off", False)],
    )
    def test_explicit_override_wins(self, monkeypatch, value, expected):
        conf = self._load_conf(monkeypatch, GUNICORN_WORKERS="1", GUNICORN_PRELOAD=value)
        assert conf.preload_app is expected

    def test_hosted_demo_runs_one_worker(self):
        """
        The Dockerfile is what the hosted demo actually runs, and the whole
        memory argument above depends on it staying at one worker.
        """
        from pathlib import Path

        dockerfile = Path(__file__).resolve().parent.parent / "Dockerfile"
        assert "GUNICORN_WORKERS=1" in dockerfile.read_text(encoding="utf-8")


class TestDuckMemoryBudget:
    """
    `fact_store` holds its DuckDB connection in a `threading.local`, so
    `DUCKDB_MEMORY_LIMIT` is charged once per thread, not once per process. The
    hosted demo ran two request threads plus the warm-up thread against a
    128 MB limit and a ~185 MB resident app, which is ~569 MB on a 512 MB
    container: opening Products OOM-killed it, and the restart wiped every
    warmed cache, so the *next* visitor got the degraded filters that made this
    look like a front-end timeout.

    These pin the relationship rather than the numbers, so the limit stays
    tunable but cannot quietly be multiplied past the container again.
    """

    CONTAINER_MB = 512
    APP_RESIDENT_MB = 185

    @staticmethod
    def _docker_env():
        import re
        from pathlib import Path

        text = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text(encoding="utf-8")
        return dict(re.findall(r"([A-Z_][A-Z0-9_]*)=([^\s\\]+)", text))

    @staticmethod
    def _to_mb(value):
        import re

        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(KB|MB|GB)", value.strip(), re.I)
        assert m, f"unparseable size {value!r}"
        return float(m.group(1)) * {"kb": 1 / 1024, "mb": 1.0, "gb": 1024.0}[m.group(2).lower()]

    def test_total_duck_budget_fits_the_container(self):
        env = self._docker_env()
        per_conn = self._to_mb(env["DUCKDB_MEMORY_LIMIT"])
        # One connection per request thread, plus the warm-up thread, which
        # holds its own for the whole warm-up and overlaps real traffic.
        connections = int(env["GUNICORN_THREADS"]) * int(env["GUNICORN_WORKERS"]) + 1
        budget = per_conn * connections + self.APP_RESIDENT_MB
        assert budget < self.CONTAINER_MB, (
            f"{connections} DuckDB connections x {per_conn:.0f} MB plus a "
            f"{self.APP_RESIDENT_MB} MB app is {budget:.0f} MB on a "
            f"{self.CONTAINER_MB} MB container - this is an OOM kill, and the "
            "restart costs every warmed cache, not just the request."
        )

    def test_headroom_is_left_for_result_materialisation(self):
        """
        `_execute_df` takes up to three `df.copy()` of a result - request
        cache, query cache, and the in-flight future - so the frame peaks at
        several times its own size outside DuckDB's accounting.
        """
        env = self._docker_env()
        per_conn = self._to_mb(env["DUCKDB_MEMORY_LIMIT"])
        connections = int(env["GUNICORN_THREADS"]) * int(env["GUNICORN_WORKERS"]) + 1
        headroom = self.CONTAINER_MB - (per_conn * connections + self.APP_RESIDENT_MB)
        assert headroom >= 64, (
            f"only {headroom:.0f} MB left for pandas copies and the JSON "
            "response; DuckDB's limit does not cover either."
        )

    def test_spill_directory_is_absolute(self):
        """
        DuckDB defaults to `.tmp` relative to the working directory, which in
        the image is the application's own `/app`. A relative default is what
        turned the memory limit into an OutOfMemoryException instead of a
        slower query, so the image has to name an absolute scratch path.
        """
        env = self._docker_env()
        temp_dir = env.get("DUCKDB_TEMP_DIRECTORY", "")
        assert temp_dir.startswith("/"), (
            f"DUCKDB_TEMP_DIRECTORY={temp_dir!r} must be absolute; a relative "
            "path resolves against /app and may not be writable"
        )
        assert "DUCKDB_MAX_TEMP_DIRECTORY_SIZE" in env, "an unbounded spill just moves the outage to the disk"

    def test_runtime_dataframe_caches_fit_the_free_container(self):
        """Large pandas frames must not accumulate across page visits."""
        env = self._docker_env()
        assert int(env["FACT_FRAME_CACHE_MAXSIZE"]) == 0
        assert int(env["FACT_QUERY_CACHE_MAXSIZE"]) <= 4
        assert int(env["FACT_CACHE_MAX"]) <= 4
        assert int(env["PRODUCTS_FRAME_CACHE_MAXSIZE"]) == 0
        assert int(env["REGIONS_FRAME_CACHE_MAXSIZE"]) == 0
        assert int(env["SUPPLIERS_FRAME_CACHE_MAXSIZE"]) == 0
        assert int(env["CACHE_THRESHOLD"]) <= 32

    def test_spill_directory_is_applied_to_the_connection(self, tmp_path, monkeypatch):
        """The pragma has to reach DuckDB, not just the environment."""
        import duckdb

        from app.services.fact_store import _init_spill_directory

        target = tmp_path / "duckspill"
        monkeypatch.setenv("DUCKDB_TEMP_DIRECTORY", str(target))
        monkeypatch.setenv("DUCKDB_MAX_TEMP_DIRECTORY_SIZE", "1GB")
        conn = duckdb.connect(":memory:")
        try:
            _init_spill_directory(conn, logging.getLogger(__name__), "test")
            applied = conn.execute("SELECT current_setting('temp_directory')").fetchone()[0]
        finally:
            conn.close()
        assert os.path.normcase(os.path.normpath(applied)) == os.path.normcase(os.path.normpath(str(target)))
        assert target.exists(), "the directory has to exist before DuckDB needs to spill into it"

    def test_spill_config_is_optional(self, monkeypatch):
        """Unset means leave DuckDB's own default alone, not crash."""
        import duckdb

        from app.services.fact_store import _init_spill_directory

        monkeypatch.delenv("DUCKDB_TEMP_DIRECTORY", raising=False)
        monkeypatch.delenv("DUCKDB_TEMP_DIR", raising=False)
        conn = duckdb.connect(":memory:")
        try:
            _init_spill_directory(conn, logging.getLogger(__name__), "test")
        finally:
            conn.close()


class TestWarmupYieldsToTraffic:
    """
    On a host that spins down, the visitor whose arrival wakes the container is
    the one the warm-up overlaps - it cannot prime anything in time to help
    them, it can only take CPU from them. Pages that answer in a second warm
    were taking three minutes and dying on the 180s worker timeout.

    So the warm-up waits while a real request is in flight.
    """

    @staticmethod
    def _reset():
        from app.core import warmup

        with warmup._inflight_lock:
            warmup._inflight_requests = 0
        warmup._thread_state.is_warmup = False
        return warmup

    def test_waits_while_a_request_is_in_flight(self):
        warmup = self._reset()
        with warmup._inflight_lock:
            warmup._inflight_requests = 1
        try:
            started = time.perf_counter()
            assert warmup._wait_for_quiet(0.5) is False
            assert time.perf_counter() - started >= 0.4, "it has to actually wait"
        finally:
            self._reset()

    def test_returns_immediately_when_nothing_is_in_flight(self):
        warmup = self._reset()
        started = time.perf_counter()
        assert warmup._wait_for_quiet(5.0) is True
        assert time.perf_counter() - started < 1.0

    def test_resumes_as_soon_as_the_request_finishes(self):
        warmup = self._reset()
        with warmup._inflight_lock:
            warmup._inflight_requests = 1

        def _finish():
            time.sleep(0.3)
            with warmup._inflight_lock:
                warmup._inflight_requests = 0

        t = threading.Thread(target=_finish, daemon=True)
        t.start()
        try:
            assert warmup._wait_for_quiet(10.0) is True, "must not wait out the full timeout"
        finally:
            t.join(timeout=2)
            self._reset()

    def test_the_warmups_own_requests_do_not_count_as_traffic(self, monkeypatch):
        """
        The warm-up drives the app through `test_client`, so its own requests
        run the same before_request hook. If those counted, it would wait for
        itself and never warm anything - a deadlock that looks exactly like the
        warm-up silently not running.
        """
        warmup = self._reset()
        monkeypatch.setenv("DEMO_WARMUP", "1")
        # This test needs the request hooks registered, not a daemon that keeps
        # walking the shared session-scoped app after the test has finished.
        monkeypatch.setattr(warmup, "_warm", lambda _app: None)
        app = create_app()

        warmup._thread_state.is_warmup = True
        try:
            app.test_client().get("/healthz")
            with warmup._inflight_lock:
                counted = warmup._inflight_requests
        finally:
            self._reset()
        assert counted == 0, "warm-up traffic must not be counted as real traffic"

    def test_real_requests_are_counted_and_released(self, monkeypatch):
        warmup = self._reset()
        monkeypatch.setenv("DEMO_WARMUP", "1")
        monkeypatch.setattr(warmup, "_warm", lambda _app: None)
        app = create_app()
        try:
            app.test_client().get("/healthz")
            with warmup._inflight_lock:
                assert warmup._inflight_requests == 0, "teardown must release the count"
        finally:
            self._reset()


class TestHealthCheck:
    def test_healthz_is_reachable_without_a_session(self, app):
        """
        Render polls /healthz and has no session. Behind @login_required it
        answered 302, which reads as unhealthy, so the platform kept cycling a
        process that was serving fine.
        """
        resp = app.test_client().get("/healthz")
        assert resp.status_code == 200, "health check must not require a login"
        assert (resp.get_json() or {}).get("status") == "ok"

    def test_healthz_leaks_nothing(self, app):
        """It is public, so it reports liveness and nothing else."""
        payload = app.test_client().get("/healthz").get_json() or {}
        assert set(payload) <= {"status", "time"}

    def test_readyz_stays_authenticated(self, app):
        """The detailed probe reports data and config state, so it stays shut."""
        resp = app.test_client().get("/readyz")
        assert resp.status_code in (301, 302, 401, 403)

class TestWarmupOrder:
    """
    Order matters more than coverage on a cold container.

    Filter options are embedded before the browser sees the page. The warm-up
    must therefore cover actual data bundles without ever touching the retired
    options XHR; warming it would conceal a page-load regression.
    """

    @staticmethod
    def _primary_paths(monkeypatch):
        import app.core.warmup as warmup

        captured = {}

        def fake_warm_user(_app, username, paths):
            captured.setdefault(username, paths)
            return True

        monkeypatch.setattr(warmup, "_warm_user", fake_warm_user)
        monkeypatch.setattr(warmup.time, "sleep", lambda *_a, **_k: None)
        monkeypatch.setenv("DEMO_WARMUP_SECONDARY", "0")
        warmup._warm(object())
        return captured["gm"]

    def test_filter_options_are_not_a_warmup_request(self, monkeypatch):
        paths = self._primary_paths(monkeypatch)
        assert all("/api/filters/options" not in path for path in paths)

    def test_the_landing_bundle_precedes_the_other_bundles(self, monkeypatch):
        paths = self._primary_paths(monkeypatch)
        overview_bundle = next(i for i, p in enumerate(paths) if p.startswith("/overview/api/bundle"))
        other_bundle = next(i for i, p in enumerate(paths) if p.startswith("/api/products/bundle"))
        assert overview_bundle < other_bundle

    def test_nothing_is_dropped_by_the_reordering(self, monkeypatch):
        paths = self._primary_paths(monkeypatch)
        for page in ("/products/", "/customers/", "/suppliers/", "/regions/", "/salesreps/"):
            assert page in paths, f"{page} fell out of the warm-up"
        assert len(paths) == len(set(paths)), "a path is warmed twice"

    def test_server_rendered_deep_links_include_the_active_window(self, monkeypatch):
        paths = self._primary_paths(monkeypatch)
        for page in ("/customers/", "/suppliers/"):
            filtered = [path for path in paths if path.startswith(f"{page}?")]
            assert len(filtered) == 1
            assert "date_preset=current_fy" in filtered[0]
            assert "date_type=fiscal" in filtered[0]
            assert "_gf=1" in filtered[0]


class TestSecurityHeaders:
    def test_data_uri_fonts_are_allowed(self, app):
        """
        `img-src` was the only directive naming `data:`, and CSP does not
        inherit per-type allowances - anything unnamed falls back to
        `default-src`, which does not include it. Fonts inlined as data URIs
        were blocked outright.
        """
        csp = app.test_client().get("/healthz").headers.get("Content-Security-Policy", "")
        assert "font-src" in csp, "font-src must be named or default-src decides for it"
        font_src = next(part for part in csp.split(";") if "font-src" in part)
        assert "data:" in font_src

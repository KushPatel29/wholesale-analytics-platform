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
        assert all("date_type=fiscal" in v for v in extras.values())

    def test_warms_the_filter_options_every_page_requests(self):
        """The deferred options call runs on every page and was the slowest XHR
        on the ones that render server-side."""
        phases = {phase for _, phase in warmup.FILTER_OPTION_PHASES}
        assert phases == {"bootstrap", "deferred"}
        assert "customers" in warmup.PAGES_WITH_FILTER_OPTIONS

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

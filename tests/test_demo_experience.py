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

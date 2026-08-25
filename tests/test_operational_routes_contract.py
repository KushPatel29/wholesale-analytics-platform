"""The contract that production broke on 2026-08-25.

All nine `/work` workspaces returned 200 locally and 404 on Render for days,
while CI was green and `/healthz` said `ok`. Nothing in the suite asserted the
things that were actually false, so nothing failed.

The trap worth naming, because it is what made the outage invisible: the app
redirects *every* anonymous request to `/login?next=...`, including requests for
routes that do not exist. `/work/definitely-not-real` answers 302 to a signed-out
client and 404 to a signed-in one. So an unauthenticated probe - which is what
the keep-warm workflow was doing - cannot distinguish a working deployment from
one missing the entire blueprint. Every route assertion below therefore runs
inside a real session, and `test_unknown_workspace_404s_when_signed_in` is the
canary that proves the session is real.

`scripts/smoke_production.py` asserts the same contract against the live URL.
This file is the half that can run without a network.
"""

from __future__ import annotations

import html
import pathlib
import re
import tempfile

import pytest

from app.decision_ops import service

# path -> the H1 the page must render.
WORKSPACE_PAGES: tuple[tuple[str, str], ...] = (
    ("/work", "Action Center"),
    ("/work/crm", "CRM Pipeline"),
    ("/work/orders", "Orders & Fulfilment"),
    ("/work/procurement", "Procurement"),
    ("/work/finance", "Finance Operations"),
    ("/work/inventory", "Inventory Operations"),
    ("/work/master-data", "Master Data"),
    ("/work/service", "Customer Service"),
    ("/work/enterprise", "Enterprise Administration"),
)

WORKSPACE_PATHS = tuple(path for path, _ in WORKSPACE_PAGES)

H1_RE = re.compile(rb"<h1[^>]*>(.*?)</h1>", re.S | re.I)


@pytest.fixture(scope="module")
def auth_app():
    """A real app with login ENABLED.

    Two deviations from the shared session fixture, both load-bearing:

    * `LOGIN_DISABLED=False`. The shared fixture disables login, which would make
      every assertion here vacuous - the whole point is that authentication
      changes what these routes return.
    * `DEMO_MODE=1`. `demo_logins_enabled()` reads it straight off the
      environment, and outside pytest it arrives via `.env`. Without it
      `/auth/demo` bounces to the login page with a flash, the client stays
      anonymous, and every workspace assertion fails as a redirect for a reason
      that has nothing to do with the routes.
    """
    import os

    previous_demo_mode = os.environ.get("DEMO_MODE")
    os.environ["DEMO_MODE"] = "1"
    os.environ.setdefault("FLASK_ENV", "development")
    os.environ.setdefault("WTF_CSRF_ENABLED", "false")
    os.environ.setdefault("WA_FAST_PWHASH", "1")
    from app import create_app

    app = create_app()
    app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        SECRET_KEY="operational-routes-contract",
        LOGIN_DISABLED=False,
    )
    try:
        yield app
    finally:
        if previous_demo_mode is None:
            os.environ.pop("DEMO_MODE", None)
        else:
            os.environ["DEMO_MODE"] = previous_demo_mode


@pytest.fixture()
def demo_client(auth_app):
    """A client signed in as `demo_viewer` via the public demo route.

    The readiness check is the redirect target, not the status code: a failed
    demo login also answers 302, just to `/auth/login` instead of to `next`.
    Following redirects and checking for 200 would accept the login page itself.
    """
    with auth_app.test_client() as client:
        response = client.get("/auth/demo?next=/work")
        location = response.headers.get("Location") or ""
        if response.status_code != 302 or "login" in location:
            pytest.skip(
                f"demo account unavailable here (HTTP {response.status_code} -> {location!r})"
            )
        yield client


# --------------------------------------------------------------------------
# 1. The routes exist at all. This is the assertion whose absence let a build
#    ship without the blueprint registered.
# --------------------------------------------------------------------------

def test_every_workspace_route_is_in_the_url_map(auth_app):
    rules = {str(rule.rule) for rule in auth_app.url_map.iter_rules()}
    assert "/work" in rules, "the Action Center index route is missing"
    assert "/work/<workspace>" in rules, (
        "the dynamic workspace route is missing - decision_ops is not registered"
    )


def test_every_workspace_key_resolves(auth_app):
    """`/work/<workspace>` is one rule, so the URL map cannot prove the keys.

    A registered blueprint with a renamed or dropped key would still pass the
    test above while 404ing in production, which is the same class of failure.
    """
    for path in WORKSPACE_PATHS:
        if path == "/work":
            continue
        key = path.rsplit("/", 1)[-1]
        assert key in service.WORKSPACES, f"{path} has no entry in service.WORKSPACES"


# --------------------------------------------------------------------------
# 2. Anonymous requests redirect to login - they must not 404.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", WORKSPACE_PATHS)
def test_anonymous_request_redirects_to_login(auth_app, path):
    with auth_app.test_client() as client:
        response = client.get(path)
    assert response.status_code in (301, 302), (
        f"{path} returned {response.status_code} to an anonymous client; expected a redirect to login"
    )
    assert "/login" in (response.headers.get("Location") or ""), (
        f"{path} redirected to {response.headers.get('Location')!r}, not to login"
    )


# --------------------------------------------------------------------------
# 3. demo_viewer can read every workspace.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("path", "expected_h1"), WORKSPACE_PAGES)
def test_demo_viewer_can_read_every_workspace(demo_client, path, expected_h1):
    response = demo_client.get(path)
    assert response.status_code == 200, (
        f"{path} returned {response.status_code} to demo_viewer; expected 200"
    )
    match = H1_RE.search(response.data)
    assert match is not None, f"{path} rendered no <h1>"
    # Unescaped, so the expectations read as the words a visitor sees
    # ("Orders & Fulfilment") rather than as markup - and so this matches what
    # scripts/smoke_production.py asserts against the live service.
    heading = html.unescape(re.sub(rb"<[^>]+>", b"", match.group(1)).strip().decode())
    assert heading == expected_h1, f"{path} rendered H1 {heading!r}, expected {expected_h1!r}"


def test_unknown_workspace_404s_when_signed_in(demo_client):
    """The canary.

    If this returns 302 the session was lost and every 200 above proves nothing
    - which is exactly how the live 404s hid behind a login redirect.
    """
    response = demo_client.get("/work/this-workspace-does-not-exist")
    assert response.status_code == 404, (
        f"expected 404 for an unknown workspace, got {response.status_code}; "
        "if this is a redirect, the test session is not authenticated"
    )


# --------------------------------------------------------------------------
# 4. Read-only users cannot write.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    [
        "/work/actions",
        "/work/crm/records",
        "/work/master-data/changes",
    ],
)
def test_demo_viewer_cannot_write(demo_client, path):
    response = demo_client.post(path, data={"title": "smoke-test write attempt"})
    assert response.status_code in (403, 405), (
        f"POST {path} returned {response.status_code} for a read-only demo user; "
        "expected the write to be refused"
    )
    assert response.status_code != 302 or "/login" not in (
        response.headers.get("Location") or ""
    ), "a refused write must not look like a signed-out session"


# --------------------------------------------------------------------------
# 5. GitHub Pages must link operational pages to the live service, absolutely.
#    The static site cannot host /work - it is a workflow app with a database -
#    so those links have to leave Pages and land on Render.
# --------------------------------------------------------------------------

def test_static_build_makes_operational_links_absolute():
    import build_static

    live = "https://wholesale-analytics-platform.onrender.com"
    builder = build_static.Builder(pathlib.Path(tempfile.mkdtemp()), live, verbose=False)
    builder.presets = ["default"]

    html = "".join(f'<a href="{path}">x</a>' for path in WORKSPACE_PATHS)
    rewritten = builder.rewrite_links(html, "", "default")

    for path in WORKSPACE_PATHS:
        assert f'href="{live}{path}"' in rewritten, (
            f"{path} was not rewritten to an absolute Render URL; "
            "a GitHub Pages visitor would get a dead link"
        )


def test_static_build_keeps_analytics_pages_local():
    """The counterpart: prerendered pages must NOT be sent to Render.

    Without this, a regression that absolutised everything would send every
    visitor to a cold free-tier container - and still pass the test above.
    """
    import build_static

    live = "https://wholesale-analytics-platform.onrender.com"
    builder = build_static.Builder(pathlib.Path(tempfile.mkdtemp()), live, verbose=False)
    builder.presets = ["default"]

    rewritten = builder.rewrite_links('<a href="/overview">o</a>', "", "default")
    assert live not in rewritten, "the overview page should resolve to a local static file"


# --------------------------------------------------------------------------
# 6. The release fingerprint. Without it, nothing downstream can tell a healthy
#    service from a healthy *stale* service.
# --------------------------------------------------------------------------

def test_healthz_reports_a_release_fingerprint(auth_app, monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "0123456789abcdef0123456789abcdef01234567")
    monkeypatch.setenv("RENDER_SERVICE_NAME", "wholesale-analytics-platform")

    with auth_app.test_client() as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["git_sha"] == "0123456789abcdef0123456789abcdef01234567"
    assert payload["git_sha_short"] == "0123456"
    assert payload["identified"] is True
    assert payload["service"] == "wholesale-analytics-platform"


def test_healthz_says_unknown_rather_than_guessing(auth_app, monkeypatch):
    """An unidentifiable build must say so.

    Reporting a plausible-but-wrong SHA would make the deploy gate pass on a
    stale image, which is the failure this whole file exists to prevent.
    """
    for var in ("RENDER_GIT_COMMIT", "GIT_SHA", "SOURCE_COMMIT", "HEROKU_SLUG_COMMIT"):
        monkeypatch.delenv(var, raising=False)

    with auth_app.test_client() as client:
        payload = client.get("/healthz").get_json()

    assert payload["git_sha"] == "unknown"
    assert payload["identified"] is False


def test_healthz_never_leaks_configuration(auth_app, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "super-secret-value")
    monkeypatch.setenv("DATABASE_URL", "postgres://user:password@host/db")

    with auth_app.test_client() as client:
        body = client.get("/healthz").get_data(as_text=True)

    assert "super-secret-value" not in body
    assert "password" not in body
    assert "postgres://" not in body

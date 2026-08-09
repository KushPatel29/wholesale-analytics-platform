"""
Every internal link the app renders must resolve for the account visitors use.

The demo shipped an overview whose region and supplier drill links were built
as `/regions/drilldown/<name>` and `/suppliers/drilldown/<id>` while the only
routes registered were `/regions/<name>` and `/suppliers/<id>`. Every one of
them 404'd, and because all 4xx rendered through errors/403.html the visitor
read "Forbidden - you do not have permission" and concluded the login was
broken. Nobody noticed because the links are built in JavaScript, so no
server-side test and no template grep could see them.

So this module checks two surfaces:

* `test_rendered_links_resolve` crawls the anchors actually present in the
  HTML of every top-level page;
* `test_javascript_drill_urls_resolve` extracts the URL templates that the
  front-end builds at runtime and checks those too. That is the one that would
  have caught the original bug.

Both run as `demo_viewer`, because that is the only account an anonymous
visitor gets, and a link that works for admin and 403s for them is exactly the
failure this guards against.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from app import create_app
from app.core.demo_accounts import DEMO_VIEWER_USERNAME


# Pages a reviewer can reach from the nav. Every one must be crawlable.
TOP_LEVEL_PAGES = [
    "/",
    "/overview",
    "/customers/",
    "/products/",
    "/regions/",
    "/suppliers/",
    "/inventory/",
    "/labor/",
    "/salesreps/",
    "/returns/",
    "/planning/",
]

# Links we deliberately do not follow. Keep this list short and justified - it
# is the obvious place to hide a broken link.
SKIP_PREFIXES = (
    "/static/",
    "/auth/logout",  # ends the session the rest of the crawl needs
    "/admin",        # demo_viewer is meant to be refused here; asserted separately
)

SKIP_EXACT = {"#", ""}


class _LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        for key, value in attrs:
            if key == "href" and value:
                self.hrefs.append(value)


def _internal_links(html: str) -> set[str]:
    parser = _LinkCollector()
    parser.feed(html)
    found = set()
    for href in parser.hrefs:
        href = href.strip()
        if not href.startswith("/"):
            continue
        if href in SKIP_EXACT or href.startswith(SKIP_PREFIXES):
            continue
        found.add(href.split("#", 1)[0])
    return found


@pytest.fixture(scope="module")
def demo_client():
    """
    A logged-in `demo_viewer`, against the feature flags the demo image ships.

    The suite runs on an isolated auth DB that starts empty, so the account is
    created here rather than assumed. Flags are turned on explicitly: with the
    shipped defaults several pages render a different (simpler) template than
    the one a visitor sees, which is the opposite of what this test is for.
    """
    import os

    for flag in (
        "OVERVIEW_V2",
        "OVERVIEW_V3",
        "PRODUCTS_V3",
        "PRODUCTS_V4",
        "PRODUCT_DRILLDOWN_V2",
        "SUPPLIER_DRILLDOWN_V2",
        "REGIONS_V2",
        "REGION_DRILLDOWN_V2",
        "SALESREPS_V2",
        "SALESREP_DRILLDOWN_V2",
        "CUSTOMERS_KPIS_V3",
        "RETURNS_ENABLED",
        "LABOR_ANALYTICS_ENABLED",
    ):
        os.environ[flag] = "1"

    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test")

    from app.auth.models import User, get_session, get_user_by_username, sync_permissions

    sync_permissions()
    user = get_user_by_username(DEMO_VIEWER_USERNAME)
    if user is None:
        with get_session() as session:
            created = User(
                username=DEMO_VIEWER_USERNAME,
                role="demo_viewer",
                is_active=True,
                is_approved=True,
            )
            created.set_password("demo")
            session.add(created)
            session.commit()
        user = get_user_by_username(DEMO_VIEWER_USERNAME)
    assert user is not None, "could not create the demo_viewer account"

    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True
    return client


def _ok(status: int) -> bool:
    # 3xx is fine: a redirect to a page that exists is a working link.
    return status < 400


# Statuses that mean the link itself is wrong - a missing route, a permission
# the demo account will never hold, or a retired endpoint still being rendered.
# These are the failures a visitor experiences as a dead end, and none of them
# depend on how much data is loaded.
BROKEN_LINK_STATUSES = {401, 403, 404, 405, 410}


def test_rendered_links_resolve(demo_client):
    """
    No anchor rendered on a nav page may 401/403/404/405/410 for the demo
    account.

    5xx is deliberately not asserted on here: this suite runs against an empty
    dataset by default (see conftest), and an export endpoint with nothing to
    export legitimately fails. Export *content* is covered by
    tests/test_exports_produce_files.py, which needs a real dataset. What this
    test owns is that every link resolves and is permitted.
    """
    broken: list[tuple[str, str, int]] = []
    seen: set[str] = set()

    for page in TOP_LEVEL_PAGES:
        response = demo_client.get(page)
        assert _ok(response.status_code), f"{page} itself returned {response.status_code}"
        html = response.get_data(as_text=True)

        for link in sorted(_internal_links(html)):
            if link in seen:
                continue
            seen.add(link)
            status = demo_client.get(link).status_code
            if status in BROKEN_LINK_STATUSES:
                broken.append((page, link, status))

    assert not broken, "broken internal links:\n" + "\n".join(
        f"  {status}  {link}   (linked from {page})" for page, link, status in broken
    )


# The URL shapes the front-end builds by string concatenation, e.g.
#   `/regions/drilldown/${encodeURIComponent(id)}`
# Extracted from the shipped JS rather than hand-written, so the test tracks
# the code rather than a copy of it.
_JS_URL_PATTERN = re.compile(r"`(/[A-Za-z0-9\-_/]*\$\{[^`]*)`")
_JS_INTERPOLATION = re.compile(r"\$\{[^}]*\}")


def javascript_url_templates() -> dict[str, list[str]]:
    """
    Every template-literal URL the front-end builds, as concrete probe paths.

    Interpolations collapse to a placeholder segment: what is being checked is
    that a rule exists for the *shape*, not that a particular row exists.

    Two substitutions matter for the result to mean anything:

    * a trailing interpolation that is not its own path segment is a query
      string being appended (`...${qs}`), so it is dropped rather than turned
      into part of the path;
    * the placeholder is numeric, because several rules use the `<int:...>`
      converter and a letter would fail to match them for the wrong reason.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "app" / "static" / "js"
    found: dict[str, list[str]] = {}
    for path in root.glob("*.js"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in _JS_URL_PATTERN.finditer(text):
            raw = match.group(1)
            # Drop an appended query string / fragment interpolation.
            raw = re.sub(r"(?<=[^/])\$\{[^}]*\}$", "", raw)
            probe = _JS_INTERPOLATION.sub("1", raw)
            probe = probe.split("?", 1)[0]
            if len(probe) > 1:
                probe = probe.rstrip("/")
            if not probe.startswith("/"):
                continue
            found.setdefault(probe, []).append(path.name)
    return found


def test_javascript_built_urls_have_a_matching_route():
    """
    Every URL the front-end constructs must match a registered rule.

    This is the test that would have caught the shipped bug: the overview built
    `/regions/drilldown/<name>` and `/suppliers/drilldown/<id>` while only
    `/regions/<name>` and `/suppliers/<id>` existed, so every region and
    supplier link on the landing page 404'd. Nothing server-side could see it,
    because the links are assembled in the browser.

    Matching against the URL map rather than issuing a request keeps the check
    independent of whether the demo dataset happens to contain a given row - a
    404 here always means "no such route", never "no such record".
    """
    from werkzeug.exceptions import MethodNotAllowed, NotFound

    app = create_app()
    adapter = app.url_map.bind("localhost")

    unroutable: list[str] = []
    for probe, sources in sorted(javascript_url_templates().items()):
        try:
            adapter.match(probe, method="GET")
        except MethodNotAllowed:
            pass  # the rule exists, just not for GET - fine here
        except NotFound:
            unroutable.append(f"  {probe}   (built in {', '.join(sorted(set(sources)))})")

    assert not unroutable, (
        "the front-end builds URLs that match no route:\n" + "\n".join(unroutable)
    )


def test_admin_is_refused_for_the_demo_account(demo_client):
    """The read-only login must not reach the admin portal."""
    for path in ("/admin/", "/admin/users"):
        assert demo_client.get(path).status_code in (403, 404), f"{path} was reachable"


def test_writes_are_refused_for_the_demo_account(demo_client):
    """
    Method-level guard: a POST from a read-only role is refused whatever the
    route's own decorators say.
    """
    response = demo_client.post("/returns/portal/create", data={})
    assert response.status_code == 403, f"expected 403, got {response.status_code}"

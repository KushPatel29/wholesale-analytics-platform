"""Making the seam between the two halves of the demo legible.

Analytics is prerendered onto a CDN; the live app keeps the writes. That the
handoff *happens* is already covered by `TestStaticDemoRouting` in
tests/test_demo_experience.py. What is pinned here is that a visitor can tell
it happened and can get back: a marker on the live host, a labelled way home,
a nav that links to the CDN rather than to a redirect, and a theme that
survives the crossing in both directions.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from app.core import demo_accounts

ROOT = Path(__file__).resolve().parent.parent
DIST_NAME = os.environ.get("WA_DIST", "dist")
STATIC_SITE = "https://kushpatel29.github.io/wholesale-analytics-platform"


@pytest.fixture()
def live_client(app, monkeypatch):
    """The app as it runs on Render: knowing where its prerendered half lives."""
    from app.auth.models import get_user_by_username

    monkeypatch.setenv("DEMO_STATIC_SITE_URL", STATIC_SITE)
    user = get_user_by_username(demo_accounts.DEMO_VIEWER_USERNAME)
    if user is None:
        pytest.skip("demo viewer account is not seeded")
    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True
    return client


def test_the_live_app_says_it_is_the_live_app(live_client):
    html = live_client.get("/returns").get_data(as_text=True)
    assert "wa-live-banner" in html, "nothing marks this host as the slower, server-rendered half"
    assert "Server-rendered" in html


def test_no_page_on_the_live_app_is_a_dead_end(live_client):
    html = live_client.get("/returns").get_data(as_text=True)
    assert "Back to the fast dashboard" in html, "no labelled route back to the prerendered site"
    assert STATIC_SITE in html, "the way back does not point anywhere"


def test_the_live_nav_links_to_the_cdn_rather_than_to_a_redirect(live_client):
    """A link to a path that only 302s costs a round trip on a cold container."""
    html = live_client.get("/returns").get_data(as_text=True)
    linked = set(re.findall(r'href="(' + re.escape(STATIC_SITE) + r'/[a-z]*/?)"', html))
    for suffix in ("customers/", "products/", "regions/", "suppliers/", "salesreps/", "planning/"):
        assert f"{STATIC_SITE}/{suffix}" in linked, (
            f"the {suffix.rstrip('/')} nav item still routes through the live host"
        )


def test_the_account_label_is_not_a_database_id(live_client):
    """It rendered `get_id()` - so it read "3" on one host and "1" on the other."""
    html = live_client.get("/returns").get_data(as_text=True)
    match = re.search(r'data-bs-toggle="dropdown"[^>]*>\s*([^<]+?)\s*</a>', html)
    assert match, "account menu not found"
    label = match.group(1).strip()
    assert not label.isdigit(), f"account menu shows a raw id ({label!r})"


def _frozen_pages(dist: Path):
    data_root = (dist / "data").as_posix()
    return [p for p in dist.rglob("*.html") if not p.as_posix().startswith(data_root)]


@pytest.mark.parametrize("dist_name", [DIST_NAME])
def test_frozen_pages_resolve_a_carried_theme(dist_name: str):
    """The freeze pass strips every script, including the one that reads the theme.

    Without it, `?theme=light` lands in dark and the flip is the only sign the
    visitor crossed an origin. It also means the prerendered site forgot the
    theme between its own pages.
    """
    dist = ROOT / dist_name
    if not dist.is_dir():
        pytest.skip(f"{dist_name}/ not built")
    pages = _frozen_pages(dist)
    if not pages:
        pytest.skip("no built pages")
    missing = [
        p.relative_to(dist).as_posix()
        for p in pages
        if 'get("theme")' not in p.read_text(encoding="utf-8")
    ]
    assert not missing, "frozen pages cannot adopt a carried theme: " + ", ".join(missing[:8])


@pytest.mark.parametrize("dist_name", [DIST_NAME])
def test_the_handoff_warns_about_the_cold_start(dist_name: str):
    """Sending someone to a sleeping free instance without saying so reads as a hang."""
    dist = ROOT / dist_name
    if not dist.is_dir():
        pytest.skip(f"{dist_name}/ not built")
    index = dist / "index.html"
    if not index.is_file():
        pytest.skip("no index page")
    html = index.read_text(encoding="utf-8")
    assert "Open the live app" in html
    assert "wake" in html, "the handoff does not mention the cold start"

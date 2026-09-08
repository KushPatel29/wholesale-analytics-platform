"""
Where the demo journey renders, and where it must not.

The strip exists to give a visitor arriving from the prerendered snapshot a
route through the half of the app that genuinely runs. It was first put on the
Overview, which could never work: `_serve_public_demo_workspaces_from_cdn`
302s an authenticated GET of `/`, `/overview/`, `/customers/` and the other
analytics workspaces straight back to the static site, so the page it sat on
redirects before it renders. That was invisible locally, because the redirect
only engages when `DEMO_STATIC_SITE_URL` is set - which the Dockerfile does
and a developer machine does not.

So these tests set that variable, which is the only way the placement is
actually checked rather than assumed.
"""

from __future__ import annotations

import pytest

from app.core import demo_accounts


JOURNEY_MARKER = 'class="demo-journey"'
REDIRECTED_WORKSPACES = ("/", "/overview/", "/customers/", "/products/", "/regions/")


@pytest.fixture()
def demo_site(app, monkeypatch):
    """Run with the deployed demo's CDN handoff switched on.

    Both the redirect and the strip need a *signed-in* visitor - the session
    fixture's `LOGIN_DISABLED` bypasses the guards without authenticating
    anyone, so `current_user.is_authenticated` stays false and neither engages.
    """
    monkeypatch.setenv("DEMO_STATIC_SITE_URL", "https://example.test/snapshot")
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setitem(app.config, "LOGIN_DISABLED", False)
    return app


def _demo_user_id() -> str:
    """The demo viewer, created if this checkout has not seeded it."""
    from app.auth.models import User, get_session, sync_permissions

    sync_permissions()
    with get_session() as session:
        user = session.query(User).filter(User.username == demo_accounts.DEMO_VIEWER_USERNAME).first()
        if user is None:
            user = User(
                username=demo_accounts.DEMO_VIEWER_USERNAME,
                role=demo_accounts.DEMO_USERS[demo_accounts.DEMO_VIEWER_USERNAME]["role"],
                is_active=True,
                is_approved=True,
            )
            session.add(user)
            session.commit()
        return str(user.id)


@pytest.fixture()
def demo_client(demo_site):
    user_id = _demo_user_id()
    with demo_site.test_client() as client:
        # The key Flask-Login sets; `app/core/warmup.py` signs in the same way
        # rather than posting a rate-limited login form.
        with client.session_transaction() as session:
            session["_user_id"] = user_id
            session["_fresh"] = False
        yield client


class TestTheAnalyticsPagesAreTheSnapshot:
    @pytest.mark.parametrize("path", REDIRECTED_WORKSPACES)
    def test_analytics_workspaces_hand_off_to_the_static_site(self, demo_client, path):
        """The premise the journey's placement rests on.

        If this ever stops being true the strip should move back onto the
        Overview, so the assertion is here rather than in a comment.
        """
        response = demo_client.get(path)
        assert response.status_code == 302
        assert response.headers["Location"].startswith("https://example.test/snapshot")

    @pytest.mark.parametrize("path", REDIRECTED_WORKSPACES)
    def test_the_journey_is_not_on_a_page_that_redirects(self, demo_client, path):
        body = demo_client.get(path).get_data(as_text=True)
        assert JOURNEY_MARKER not in body


class TestTheJourneyIsOnTheLiveWorkspace:
    def test_the_action_center_is_served_not_redirected(self, demo_client):
        """`/work/` is the one workspace that answers for a demo visitor."""
        response = demo_client.get("/work/")
        assert response.status_code == 200

    def test_the_journey_renders_there(self, demo_client):
        body = demo_client.get("/work/").get_data(as_text=True)
        assert JOURNEY_MARKER in body

    def test_every_step_anchor_exists_on_the_page(self, demo_client):
        """A step pointing at a control that is not there is worse than no step.

        The first version linked `#GlobalFilters` and `#exportSnapshotBtn`;
        neither id existed on the page it shipped on.
        """
        import re

        body = demo_client.get("/work/").get_data(as_text=True)
        section = re.search(r'<section class="demo-journey".*?</section>', body, re.S)
        assert section is not None
        anchors = re.findall(r'href="#([A-Za-z0-9_-]+)"', section.group(0))
        assert anchors, "the journey rendered with no steps"
        for anchor in anchors:
            assert f'id="{anchor}"' in body, f"step points at #{anchor}, which is not on the page"

    def test_it_promises_no_download(self, demo_client):
        """The exports live on the analytics pages, and those are the snapshot."""
        body = demo_client.get("/work/").get_data(as_text=True)
        section = body[body.index(JOURNEY_MARKER):]
        section = section[: section.index("</section>")]
        assert "Download" not in section
        assert "Excel" not in section


class TestOutsideDemoMode:
    def test_a_private_deployment_gets_no_demo_strip(self, app, monkeypatch):
        """`demo_logins` gates it, so a real deployment never shows it."""
        monkeypatch.delenv("DEMO_MODE", raising=False)
        monkeypatch.delenv("DEMO_WARMUP", raising=False)
        monkeypatch.delenv("DEMO_STATIC_SITE_URL", raising=False)
        assert demo_accounts.demo_logins_enabled() is False

        with app.test_client() as client:
            body = client.get("/work/").get_data(as_text=True)
        assert JOURNEY_MARKER not in body

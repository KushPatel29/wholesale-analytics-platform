"""
The demo logins, in one place.

`manage.py seed-demo-users` creates these and the login page advertises them.
Those two used to be able to drift - a renamed account or a changed password
would leave the login page telling visitors to use credentials that no longer
existed - so both now read this catalogue.

Nothing here is a secret. These accounts exist only on the public demo, which
carries generated data.
"""

from __future__ import annotations

import os

DEMO_PASSWORD = "demo-password-1234"

# The one-click account. Short, typeable credentials, because the fallback path
# when JavaScript is off or the button is missed is a human copying them by
# hand off the page.
DEMO_VIEWER_USERNAME = "demo"
DEMO_VIEWER_PASSWORD = "demo"

DEMO_USERS: dict[str, dict] = {
    # The account the "Explore demo" button signs into. Listed first because
    # the login page renders this catalogue in order.
    DEMO_VIEWER_USERNAME: dict(
        role="demo_viewer",
        scope={"rep": ("*",)},
        password=DEMO_VIEWER_PASSWORD,
        note="read-only tour of every page (one-click)",
    ),
    # Full access, including the admin portal. The `admin` role short-circuits
    # the scope check entirely, so it needs no rules.
    "admin": dict(role="admin", scope={}, note="everything, plus the admin portal"),
    # Sees the whole company but cannot administer it. "*" is the wildcard the
    # access policy recognises as unrestricted.
    "gm": dict(role="gm", scope={"rep": ("*",)}, note="everything, no admin portal"),
    # Scoped to two regions: every page silently narrows to that territory.
    "manager.coast": dict(
        role="sales_manager",
        scope={"region": ("RG01", "RG02")},
        note="two regions only",
    ),
    # Scoped to a single rep's own book.
    "rep.dana": dict(role="sales", scope={"rep": ("R01",)}, note="Dana Whitfield's accounts only"),
    "rep.tomasz": dict(role="sales", scope={"rep": ("R04",)}, note="Tomasz Bielski's accounts only"),
    # Sees every row but has no view_costs permission, so cost, margin and
    # profit come back masked rather than merely hidden in the template.
    "viewer.nocost": dict(
        role="warehouse",
        scope={"rep": ("*",)},
        note="all rows, but cost/margin columns masked",
    ),
}


def password_for(username: str) -> str:
    """The password a given demo account is seeded with."""
    spec = DEMO_USERS.get(str(username or "").strip().lower()) or {}
    return str(spec.get("password") or DEMO_PASSWORD)


def demo_logins_enabled() -> bool:
    """
    Whether to advertise the demo credentials on the login page.

    This used to be tied to DEMO_WARMUP, which the demo image sets to 0 - warm-up
    moved into the build layer - so the deployed portfolio showed an anonymous
    visitor a username box and no way to fill it. Warm-up strategy and "is this
    a public demo" are unrelated questions; DEMO_MODE answers the second one.

    DEMO_WARMUP is still honoured so an older deployment that only sets that
    variable keeps working.
    """
    for name in ("DEMO_MODE", "DEMO_WARMUP"):
        if str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "on"}:
            return True
    return False


def demo_login_hints() -> list[dict[str, str]]:
    """The catalogue in display order, for the login page."""
    return [
        {
            "username": username,
            "password": password_for(username),
            "note": str(spec.get("note") or ""),
            "role": str(spec.get("role") or ""),
        }
        for username, spec in DEMO_USERS.items()
    ]

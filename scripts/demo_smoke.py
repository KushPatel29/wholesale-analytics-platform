#!/usr/bin/env python3
"""
Prove the demo actually works from a clean clone.

Boots the app against the seeded synthetic dataset and checks three things
that a README claim alone cannot:

  1. every dashboard renders (no 500s, no empty payloads),
  2. row-level security narrows the data for scoped users,
  3. cost and margin come back masked for users without `view_costs`.

Run after `python -m seed.generate_synthetic_data` and
`python manage.py seed-demo-users`. Exits non-zero on the first failure, so
CI fails loudly rather than shipping a broken demo.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

# The limiter would throttle a dozen logins in a row; it is exercised by the
# test suite, not by this smoke check.
os.environ.setdefault("RATELIMIT_ENABLED", "false")
os.environ.setdefault("WTF_CSRF_ENABLED", "false")

from app import create_app  # noqa: E402

PASSWORD = "demo-password-1234"

# Pages every authenticated user should be able to open.
PAGES = [
    "/",
    "/customers/",
    "/customers/kpis",
    "/customers/rfm",
    "/customers/clv",
    "/customers/cohorts",
    "/products/",
    "/suppliers/",
    "/regions/",
    "/salesreps/",
    "/stakeholder-report/",
]

# Pages that need elevated permissions; checked as admin only.
ADMIN_PAGES = [
    "/admin/",
    "/admin/users",
    "/returns",
    "/returns/analytics",
    "/notifications",
]

BUNDLE = "/overview/api/bundle?start=2025-07-01&end=2026-07-03&date_preset=custom&_gf=1"

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  ok    {message}")
    else:
        print(f"  FAIL  {message}")
        failures.append(message)


def login(client, username: str) -> None:
    resp = client.post(
        "/auth/login",
        data={"username": username, "password": PASSWORD},
    )
    # A successful login redirects to the landing page; a failed one re-renders
    # the form with a 200, which would otherwise look like a silent pass.
    if resp.status_code != 302:
        raise SystemExit(f"login failed for {username}: HTTP {resp.status_code}")


def kpis(client) -> dict:
    resp = client.get(BUNDLE)
    if resp.status_code != 200:
        return {}
    return (resp.get_json() or {}).get("kpis") or {}


def main() -> int:
    app = create_app()
    app.config.update(WTF_CSRF_ENABLED=False)

    print("\nPages render")
    with app.test_client() as client:
        login(client, "admin")
        for path in PAGES + ADMIN_PAGES:
            resp = client.get(path)
            # Some routes (e.g. /admin/) are landing pages that redirect to a
            # real one. Follow the hop, but treat a bounce to the login form as
            # a failure rather than a pass.
            if resp.status_code in (301, 302):
                resp = client.get(path, follow_redirects=True)
                bounced = "/login" in resp.request.path or "/auth/login" in resp.request.path
                check(
                    resp.status_code == 200 and not bounced,
                    f"{path} -> redirect -> {resp.request.path} ({resp.status_code})",
                )
                continue
            check(resp.status_code == 200, f"{path} -> {resp.status_code}")

    print("\nOverview returns real numbers")
    with app.test_client() as client:
        login(client, "gm")
        full = kpis(client)
    check(bool(full), "overview bundle returned a kpis block")
    check(float(full.get("revenue") or 0) > 0, f"revenue > 0 (got {full.get('revenue')})")
    check(int(full.get("customers") or 0) > 0, f"customers > 0 (got {full.get('customers')})")
    check(full.get("cost") is not None, "gm can see cost")

    print("\nRow-level security narrows scoped users")
    scoped: dict[str, dict] = {}
    for username in ("manager.coast", "rep.dana", "rep.tomasz"):
        with app.test_client() as client:
            login(client, username)
            scoped[username] = kpis(client)

    full_revenue = float(full.get("revenue") or 0)
    full_customers = int(full.get("customers") or 0)
    for username, data in scoped.items():
        revenue = float(data.get("revenue") or 0)
        customers = int(data.get("customers") or 0)
        share = (revenue / full_revenue * 100) if full_revenue else 0
        check(
            0 < revenue < full_revenue,
            f"{username} sees a strict subset of revenue ({share:.1f}% of company)",
        )
        check(
            0 < customers < full_customers,
            f"{username} sees {customers} of {full_customers} customers",
        )

    print("\nCost masking")
    with app.test_client() as client:
        login(client, "viewer.nocost")
        masked = kpis(client)
    check(masked.get("revenue") is not None, "viewer.nocost can still see revenue")
    check(masked.get("cost") is None, "viewer.nocost cannot see cost")
    check(masked.get("margin_pct") is None, "viewer.nocost cannot see margin")

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("All demo smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
"""Prove the live service is running the commit we think it is, and that every
operational workspace actually answers.

This exists because of a specific failure that CI could not see. On 2026-08-25
all nine `/work` routes returned 200 locally and 404 in production, for days,
while `/healthz` returned `{"status": "ok"}` and the keep-warm workflow reported
green every ten minutes. Render was serving an image that predated the routes.
Three separate signals said healthy and none of them was measuring the thing
that was broken.

Two traps this script is shaped around:

1.  **An unauthenticated probe cannot detect a missing route.** The app sends
    every anonymous request to `/login?next=...`, so a route that does not exist
    answers 302 exactly like a route that does. `/work/definitely-not-real`
    returns 302 to an anonymous client and 404 to a signed-in one. Every route
    assertion here therefore runs *inside a session*, and a canary route that
    must 404 proves the session is real - without it, a service that redirected
    everything to a login page would pass silently.

2.  **`status: ok` is not `running the right code`.** The SHA gate is the point
    of the script; the route checks only mean something once it passes.

Exit codes: 0 all good, 1 a check failed, 2 the service never woke up.

    python scripts/smoke_production.py --expect-sha $(git rev-parse HEAD)
"""

from __future__ import annotations

import argparse
import html as html_mod
import re
import sys
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

DEFAULT_BASE = "https://wholesale-analytics-platform.onrender.com"

# path -> the H1 the page must render. Taken from service.WORKSPACES labels and
# the Action Center index; if a label changes, this must change with it, which is
# deliberate - it is the assertion that the page rendered rather than merely
# returning 200 from an error handler.
WORKSPACES: tuple[tuple[str, str], ...] = (
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

# Must 404 for a signed-in client. If this returns 302 the session was lost and
# every other pass in this run is meaningless.
CANARY = "/work/this-workspace-does-not-exist"

H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)


class SmokeFailure(Exception):
    pass


def _log(msg: str) -> None:
    print(msg, flush=True)


def _text_of_h1(body: str) -> str | None:
    m = H1_RE.search(body)
    if not m:
        return None
    inner = re.sub(r"<[^>]+>", "", m.group(1))
    return html_mod.unescape(inner).strip()


def wait_for_release(
    session: requests.Session,
    base: str,
    expect_sha: str | None,
    *,
    attempts: int,
    delay: float,
    timeout: float,
) -> dict[str, Any]:
    """Poll /healthz until it answers and, if asked, reports the expected SHA.

    Free-tier containers take 30-60s to answer at all (measured 54.2s on
    2026-08-13), and a deploy can take minutes more, so both the cold start and
    the rollover are absorbed here rather than by the caller.
    """
    last = "never answered"
    for attempt in range(1, attempts + 1):
        try:
            r = session.get(urljoin(base, "/healthz"), timeout=timeout)
            if r.status_code != 200:
                last = f"HTTP {r.status_code}"
            else:
                payload = r.json()
                sha = str(payload.get("git_sha") or "unknown")
                if expect_sha is None:
                    _log(f"  /healthz ok - git_sha={sha}")
                    return payload
                if sha == expect_sha:
                    _log(f"  /healthz reports the expected commit {sha[:7]}")
                    return payload
                if sha == "unknown":
                    last = (
                        "git_sha=unknown - the running image cannot identify its "
                        "commit (deployed before the fingerprint landed, or "
                        "RENDER_GIT_COMMIT is unset)"
                    )
                else:
                    last = f"git_sha={sha[:7]}, expected {expect_sha[:7]} - stale image"
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
        except ValueError as exc:
            # A proxy or platform error page answering 200 with HTML. Retryable:
            # during a deploy rollover this is exactly what the edge serves.
            last = f"/healthz did not return JSON ({exc})"
        _log(f"  attempt {attempt}/{attempts}: {last}")
        if attempt < attempts:
            time.sleep(delay)
    raise SmokeFailure(f"/healthz never reported the expected release ({last})")


def authenticate(session: requests.Session, base: str, timeout: float) -> None:
    url = urljoin(base, "/auth/demo?next=/work")
    r = session.get(url, timeout=timeout, allow_redirects=True)
    if r.status_code >= 400:
        raise SmokeFailure(f"demo login failed: HTTP {r.status_code} at {r.url}")
    if not session.cookies:
        raise SmokeFailure("demo login returned no session cookie")
    _log(f"  authenticated as demo_viewer ({len(session.cookies)} cookies)")


def _check_redirects(path: str, r: requests.Response, base_host: str) -> None:
    if len(r.history) > 4:
        chain = " -> ".join(h.headers.get("location", "?") for h in r.history)
        raise SmokeFailure(f"{path}: redirect loop ({len(r.history)} hops): {chain}")
    final_host = urlparse(r.url).netloc
    if final_host and final_host != base_host:
        raise SmokeFailure(
            f"{path}: redirected off the service to {r.url} - the live app is "
            f"bouncing operational links back to the static site"
        )
    if "/login" in urlparse(r.url).path:
        raise SmokeFailure(f"{path}: bounced to login - the session was not preserved")


def check_workspaces(session: requests.Session, base: str, timeout: float) -> list[str]:
    base_host = urlparse(base).netloc
    failures: list[str] = []

    for path, expected_h1 in WORKSPACES:
        try:
            r = session.get(urljoin(base, path), timeout=timeout, allow_redirects=True)
            _check_redirects(path, r, base_host)
            if r.status_code != 200:
                failures.append(f"{path}: HTTP {r.status_code} (expected 200)")
                _log(f"  FAIL {r.status_code:>3}  {path}")
                continue
            h1 = _text_of_h1(r.text)
            if h1 != expected_h1:
                failures.append(f"{path}: H1 was {h1!r}, expected {expected_h1!r}")
                _log(f"  FAIL 200  {path}  (H1 {h1!r})")
                continue
            _log(f"  ok   200  {path}  ({h1})")
        except SmokeFailure as exc:
            failures.append(str(exc))
            _log(f"  FAIL      {path}  {exc}")
        except requests.RequestException as exc:
            failures.append(f"{path}: {type(exc).__name__}: {exc}")
            _log(f"  FAIL      {path}  {exc}")

    # The session canary. A 302 here means we were never really signed in.
    try:
        r = session.get(urljoin(base, CANARY), timeout=timeout, allow_redirects=False)
        if r.status_code == 404:
            _log(f"  ok   404  {CANARY}  (session is real)")
        elif r.status_code in (301, 302, 303, 307, 308):
            failures.append(
                f"{CANARY}: HTTP {r.status_code} to {r.headers.get('location')!r} - "
                f"the session was lost, so every 200 above is unproven"
            )
            _log(f"  FAIL {r.status_code:>3}  {CANARY}  (session lost)")
        else:
            failures.append(f"{CANARY}: HTTP {r.status_code} (expected 404)")
            _log(f"  FAIL {r.status_code:>3}  {CANARY}")
    except requests.RequestException as exc:
        failures.append(f"{CANARY}: {type(exc).__name__}: {exc}")

    return failures


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument(
        "--expect-sha",
        default=None,
        help="Full commit SHA the service must report on /healthz. Omit to accept any.",
    )
    ap.add_argument("--attempts", type=int, default=20)
    ap.add_argument("--delay", type=float, default=15.0)
    ap.add_argument("--timeout", type=float, default=90.0)
    args = ap.parse_args(argv)

    base = args.base_url.rstrip("/") + "/"
    expect = args.expect_sha.strip() if args.expect_sha else None

    _log(f"Smoke test: {base}")
    _log(f"Expecting commit: {expect or '(any)'}")

    session = requests.Session()
    session.headers["User-Agent"] = "northgate-smoke/1.0"

    _log("\n[1/3] Waiting for the release to report itself")
    try:
        info = wait_for_release(
            session, base, expect,
            attempts=args.attempts, delay=args.delay, timeout=args.timeout,
        )
    except SmokeFailure as exc:
        _log(f"\nFAILED: {exc}")
        return 2
    _log(f"       release_id={info.get('release_id')} branch={info.get('git_branch')}")

    _log("\n[2/3] Authenticating through /auth/demo")
    try:
        authenticate(session, base, args.timeout)
    except SmokeFailure as exc:
        _log(f"\nFAILED: {exc}")
        return 1

    _log("\n[3/3] Requesting every operational workspace")
    failures = check_workspaces(session, base, args.timeout)

    if failures:
        _log(f"\nFAILED: {len(failures)} check(s) did not pass")
        for line in failures:
            _log(f"  - {line}")
        return 1

    _log(f"\nPASSED: {len(WORKSPACES)} workspaces answered 200 with the right H1")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Budgets that keep the pages instant, and the defects that made them slow.

Every assertion here corresponds to something that was actually wrong, not to a
hypothetical regression:

- pages fetched `/api/filters/options` on load, which gated first paint
  everywhere and timed out entirely on a cold container;
- the Department filter was empty on every page because neither the server's
  column candidates nor the browser's normaliser knew the dimension existed;
- the Overview guided tour linked roles into pages they get 403 on;
- the "loading" strings in the templates are placeholders, and the day one of
  them is driven by a timer instead is the day a page gets slow again for a
  reason no profile will explain.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Pages that carry the global filter bar. Kept as (label, path) so a failure
# names the page rather than an index.
FILTERED_PAGES = [
    ("overview", "/overview/"),
    ("customers", "/customers/"),
    ("products", "/products/"),
    ("inventory", "/inventory/"),
    ("regions", "/regions/"),
    ("suppliers", "/suppliers/"),
    ("labor", "/labor/"),
    ("salesreps", "/salesreps/"),
    ("planning", "/planning/"),
]

# The dimensions `filters-enhanced.js` declares in DIMENSIONS. If the embedded
# payload covers all of them the browser has nothing left to defer, which is
# what takes the page to zero options requests rather than one fewer.
CLIENT_DIMENSIONS = [
    "statuses", "regions", "methods", "customers",
    "sales_reps", "suppliers", "products", "protein_groups",
]


def _dataset_or_skip(app):
    """These are integration checks; without a fact view they prove nothing.

    `tests/conftest.py` points PARQUET_PATH at an empty directory to keep the
    default suite fast, so this module is wired into the CI job that supplies
    the real dataset (see `.github/workflows`).
    """
    with app.app_context():
        from app.services import fact_store

        # `list_columns` raises DatasetNotBuiltError rather than returning
        # empty when the parquet path holds nothing, which is exactly the
        # default under tests/conftest.py.
        try:
            columns = fact_store.list_columns()
        except Exception:
            columns = set()
        if not columns:
            pytest.skip("no fact dataset; run under PARQUET_PATH=cache/fact_dataset")


@pytest.fixture(scope="module")
def gm_client(app):
    from app.auth.models import get_user_by_username

    _dataset_or_skip(app)
    with app.app_context():
        user = get_user_by_username("gm") or get_user_by_username("demo")
        if user is None:
            pytest.skip("demo users are not seeded in this environment")
        uid = str(user.id)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = uid
        sess["_fresh"] = False
    return client


def _bootstrap_payload(html: str) -> dict:
    match = re.search(r'id="filtersBootstrapData"[^>]*>(.*?)</script>', html, re.S)
    assert match, "the filter bar did not render its bootstrap script tag"
    return json.loads(match.group(1))


@pytest.mark.parametrize("label,path", FILTERED_PAGES, ids=[p[0] for p in FILTERED_PAGES])
def test_page_embeds_filter_options(gm_client, label, path):
    """No page may leave the browser to fetch its filter options.

    This is the single change that unblocked first paint app-wide: the request
    took 2.2s warm on a laptop and exceeded its own 20s client timeout on a
    cold container, and every page waited for it.
    """
    resp = gm_client.get(path, follow_redirects=True)
    assert resp.status_code == 200, f"{path} returned {resp.status_code}"

    payload = _bootstrap_payload(resp.get_data(as_text=True))
    options = (payload.get("options_payload") or {}).get("options") or {}
    assert options, f"{label} embedded no filter options; the bar will fetch them"

    empty = [d for d in CLIENT_DIMENSIONS if not options.get(d)]
    assert not empty, (
        f"{label} embedded no values for {empty}. The browser defers any "
        f"dimension it did not receive, which puts the request back on the "
        f"load path."
    )


def test_department_dimension_resolves_to_a_real_column():
    """`ProteinType` is the department column this warehouse actually ships.

    It was missing from the candidate list, so `choose_column` returned None,
    the SQL for that dimension was never emitted, and the Department dropdown
    was empty on every page in the app.
    """
    from app.services import fact_schema, fact_store

    try:
        columns = fact_store.list_columns()
    except Exception:
        columns = set()
    if not columns:
        pytest.skip("no fact dataset; run under PARQUET_PATH=cache/fact_dataset")
    chosen = fact_store.choose_column(fact_schema.PROTEIN_CANDIDATES, columns)
    assert chosen, (
        "no PROTEIN_CANDIDATES entry matches the fact table; the Department "
        f"filter will render empty. Columns include: {sorted(columns)[:12]}"
    )


def test_client_normaliser_keeps_every_dimension_the_server_sends():
    """`normalizeOptionsPayload` rebuilds `options` from `canonicalKeys`.

    Anything missing from that list is discarded in the browser no matter what
    the server sent, which is how `protein_groups` stayed empty even after the
    server started returning its values.
    """
    source = (ROOT / "app" / "static" / "js" / "bundle-adapter.js").read_text(encoding="utf-8")
    block = source[source.find("const canonicalKeys"):]
    block = block[: block.find("]")]
    for dimension in CLIENT_DIMENSIONS:
        assert f'"{dimension}"' in block, (
            f"bundle-adapter.js drops '{dimension}': it is absent from "
            f"canonicalKeys, which is the exhaustive list options are rebuilt from"
        )


def test_no_link_is_rendered_to_a_page_the_viewer_cannot_open(app):
    """A visible link that answers 403 is worse than no link.

    The Overview guided tour listed Regions and Suppliers unconditionally while
    the `sales` and `warehouse` roles hold neither permission, so those logins
    were invited into a Forbidden page from the landing screen.
    """
    from app.auth.models import get_user_by_username
    from app.core.demo_accounts import DEMO_USERS

    with app.app_context():
        ids = {}
        for username in DEMO_USERS:
            user = get_user_by_username(username)
            if user is not None:
                ids[username] = str(user.id)
    if not ids:
        pytest.skip("demo users are not seeded in this environment")

    failures = []
    for username, uid in ids.items():
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = uid
            sess["_fresh"] = False
        landing = client.get("/overview/", follow_redirects=True)
        if landing.status_code != 200:
            continue
        html = landing.get_data(as_text=True)
        for href in sorted(set(re.findall(r'href="(/[^"#][^"]*)"', html))):
            if href.startswith("/static/") or href.startswith("/auth/"):
                continue
            code = client.get(href, follow_redirects=True).status_code
            if code in (401, 403):
                failures.append(f"{username} -> {href} = {code}")

    assert not failures, "Overview links to pages the viewer is refused:\n  " + "\n  ".join(failures)


# `time.sleep` has legitimate uses in this tree (file-lock backoff, the warm-up
# thread's pacing). Only request-handling code is in scope here.
RENDER_PATHS = [
    ROOT / "app" / "blueprints",
    ROOT / "app" / "services",
]
SLEEP_RE = re.compile(r"\b(?:time\.sleep|asyncio\.sleep)\s*\(")
ALLOWED_SLEEPS = {
    # Waiting out another process's parquet lock; not pacing a response.
    ("products.py", "_parquet_lock"),
}


def test_no_artificial_delay_in_a_render_path():
    """A sleep in a request path is never a progress indicator.

    The staged status messages on Products and Inventory look like a scripted
    animation and were reported as one. They are static placeholders replaced
    once by real data - and this test is what keeps that true, because pacing
    added here would be invisible in a profile and would cost seconds.
    """
    offenders = []
    for base in RENDER_PATHS:
        for path in base.rglob("*.py"):
            lines = path.read_text(encoding="utf-8").splitlines()
            for number, line in enumerate(lines, start=1):
                if not SLEEP_RE.search(line):
                    continue
                context = "\n".join(lines[max(0, number - 40):number])
                if any(
                    path.name == fname and marker in context
                    for fname, marker in ALLOWED_SLEEPS
                ):
                    continue
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert not offenders, (
        "sleep() in a request path - if this is progress pacing, delete it; "
        "if it is a genuine wait, add it to ALLOWED_SLEEPS with a reason:\n  "
        + "\n  ".join(offenders)
    )


def test_prebuilt_cache_is_invalidated_when_a_builder_changes(tmp_path, monkeypatch):
    """A cached value built by different code must not be served.

    The cache key is built from the dataset and the filters, so it says nothing
    about the code that produced the value. A fix to the suppliers revenue
    trend was invisible in the browser for exactly this reason: the entry still
    matched its key, so the pre-fix series kept being served.
    """
    from app.core import prebuilt_cache

    monkeypatch.setenv("DEMO_PREBUILT_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("DEMO_PREBUILT_CACHE_READ", "1")
    monkeypatch.setenv("DEMO_PREBUILT_CACHE_WRITE", "1")

    monkeypatch.setattr(prebuilt_cache, "_build_id_cache", None)
    monkeypatch.setenv("APP_BUILD_ID", "build-one")
    assert prebuilt_cache.save("bundles", "abc123", {"revenue": 1})
    assert prebuilt_cache.load("bundles", "abc123") == {"revenue": 1}

    # Same key, same dataset, different code.
    monkeypatch.setattr(prebuilt_cache, "_build_id_cache", None)
    monkeypatch.setenv("APP_BUILD_ID", "build-two")
    assert prebuilt_cache.load("bundles", "abc123") is None, (
        "a value built by different code was served as if current"
    )


def test_templates_do_not_animate_their_loading_strings():
    """Placeholder text must be replaced by data, never cycled by a timer."""
    suspicious = []
    for path in (ROOT / "app" / "static" / "js").glob("*.js"):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"setTimeout\s*\([^;]{0,200}", source):
            snippet = match.group(0)
            if re.search(r"(textContent|innerHTML|innerText)\s*=\s*[\"'][^\"']*(Loading|Reading|Building|Summarizing|Resolving)", snippet):
                line = source[: match.start()].count("\n") + 1
                suspicious.append(f"{path.name}:{line}")
    assert not suspicious, (
        "a timer is writing loading text - that is a staged narrative, not "
        "progress:\n  " + "\n  ".join(suspicious)
    )

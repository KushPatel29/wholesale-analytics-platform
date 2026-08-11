#!/usr/bin/env python3
"""Render the demo to a static site that needs no server at all.

The app runs on a fixed synthetic snapshot. Nothing in it is live, so every
number any page can display is knowable before a visitor arrives. This walks
the real Flask app once, captures each page together with every API payload
that page would have fetched, and writes a `dist/` tree where the data is
already in the HTML.

Why this instead of tuning the app: a spun-down container on a free instance
costs 30-50s before any of our code runs. That latency is not optimisable, only
avoidable, and a CDN avoids it.

    python build_static.py --out dist
    python build_static.py --out dist --scopes current_fy,previous_fy --no-drilldowns

The output is self-contained: open `dist/index.html` from disk and it works.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# The static build is a demo artefact by definition; load the demo env before
# the app config validator runs so this works from a bare checkout.
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:  # pragma: no cover - dotenv is a dev convenience
    pass

os.environ.setdefault("RATELIMIT_ENABLED", "false")
os.environ.setdefault("DEMO_WARMUP", "0")
# Own log file: sharing the app's rotating handler with a running dev server
# makes every rotation raise on Windows and buries the build output.
os.environ.setdefault("LOG_PATH", "logs/build_static.jsonl")


# ─────────────────────────────────────────────────────────────────────────────
# What to build
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Page:
    """One page of the static site.

    `apis` are captured by path. The shim that replays them ignores the query
    string, which is deliberate: the JS builds those URLs from the live filter
    state and has a long history of composing them differently than any
    warm-up predicted. Each built page covers exactly one scope, so a request
    to a given API path on that page can only mean one thing.
    """

    key: str
    title: str
    route: str
    out: str
    apis: tuple[str, ...] = ()
    api_args: dict[str, dict[str, str]] = field(default_factory=dict)


# These lists are not guesses. Each page was loaded in a browser and its
# `performance.getEntriesByType("resource")` read back, because the arguments
# matter: `/api/products/bundle` without `_sections` returns the *full* bundle,
# 3.4 MB of the same 200 SKU rows under four aliases, where the sectioned
# request the page actually makes returns 184 KB. Capturing the wrong variant
# does not break the page, it just inlines twenty times the data.
#
# Five of the nine pages fetch nothing at all - they are already server
# rendered - which is why so few entries here have any APIs.
PAGES: tuple[Page, ...] = (
    Page("overview", "Business Performance", "/overview/", "index.html",
         apis=("/overview/api/bundle", "/api/overview/forecast"),
         api_args={"/api/overview/forecast": {
             "metric": "revenue", "horizon_months": "6", "granularity": "monthly",
             "include_current_month": "1", "v2": "1"}}),
    Page("customers", "Customers", "/customers/", "customers/index.html"),
    # The browser asks for product sections independently as they enter the
    # viewport. Capture the complete bundle once: the browser-freeze pass below
    # scrolls through every section and then removes this payload, so its build-
    # time size has no effect on the HTML a visitor downloads.
    Page("products", "Products", "/products/", "products/index.html",
         apis=("/api/products/bundle",)),
    Page("inventory", "Inventory", "/inventory/", "inventory/index.html",
         apis=("/api/inventory/bundle",),
         api_args={"/api/inventory/bundle": {
             "page": "1", "page_size": "25", "sort_by": "priority"}}),
    Page("regions", "Regions", "/regions/", "regions/index.html",
         apis=("/api/regions/bundle",)),
    Page("suppliers", "Suppliers", "/suppliers/", "suppliers/index.html",
         apis=("/api/suppliers/bundle",)),
    Page("labor", "Labor", "/labor/", "labor/index.html"),
    Page("salesreps", "Sales Reps", "/salesreps/", "salesreps/index.html",
         apis=("/api/salesreps/bundle",)),
    Page("planning", "Demand & Supply Planner", "/planning/", "planning/index.html",
         apis=("/api/stakeholder-report/bundle",)),
)

# Presets offered by the date control in `app/templates/_filters.html`. Read
# from the template at build time rather than restated here, so a preset added
# to the UI cannot silently go unbuilt.
PRESET_RE = re.compile(r'<option value="([a-z0-9_]+)"')
SKIP_PRESETS = {"custom", ""}


def discover_presets() -> list[str]:
    src = (ROOT / "app" / "templates" / "_filters.html").read_text(encoding="utf-8")
    block = src[src.find("Select a range"):]
    block = block[: block.find("</select>")]
    found = [p for p in PRESET_RE.findall(block) if p not in SKIP_PRESETS]
    # current_fy first: it is the default the landing pages are built for.
    found.sort(key=lambda p: (p != "current_fy", p))
    return found


# ─────────────────────────────────────────────────────────────────────────────
# The client-side shim
# ─────────────────────────────────────────────────────────────────────────────
SHIM = """
/* Static replay layer.
 *
 * The page scripts are unchanged from the live app: they still call fetch() and
 * XMLHttpRequest for their data. Here those calls are answered from payloads
 * already in the document, so nothing touches the network and there is no state
 * in which the page is waiting.
 *
 * Matching is by pathname. Each built page covers one scope, so the query
 * string carries no information the lookup needs, and ignoring it means the
 * build does not have to predict argument order, defaults, or which of them the
 * JS recomputes at runtime.
 */
(function () {
  var el = document.getElementById("static-api-payloads");
  if (!el) return;
  var DATA = {};
  try { DATA = JSON.parse(el.textContent || "{}"); } catch (e) { return; }

  var pathOf = function (url) {
    try { return new URL(url, location.href).pathname.replace(/\\/+$/, "") || "/"; }
    catch (e) { return String(url || "").split("?")[0]; }
  };
  var lookup = function (url) {
    var p = pathOf(url);
    if (Object.prototype.hasOwnProperty.call(DATA, p)) return DATA[p];
    return null;
  };

  var missed = [];
  window.__staticMisses = missed;

  var realFetch = window.fetch ? window.fetch.bind(window) : null;
  window.fetch = function (input, init) {
    var url = typeof input === "string" ? input : (input && input.url) || "";
    var hit = lookup(url);
    if (hit !== null) {
      var body = JSON.stringify(hit);
      return Promise.resolve(new Response(body, {
        status: 200,
        headers: { "Content-Type": "application/json", "X-Static-Replay": "1" }
      }));
    }
    if (/^(https?:)?\\/\\//.test(url) && url.indexOf(location.origin) !== 0) {
      return realFetch ? realFetch(input, init) : Promise.reject(new Error("offline"));
    }
    missed.push(pathOf(url));
    /* An unknown same-origin call resolves empty rather than rejecting: a
       rejection would surface as a visible error banner, which is exactly what
       this build exists to remove. */
    return Promise.resolve(new Response("{}", {
      status: 200,
      headers: { "Content-Type": "application/json", "X-Static-Replay": "miss" }
    }));
  };

  var RealXHR = window.XMLHttpRequest;
  function StaticXHR() {
    var xhr = new RealXHR();
    var replay = null;
    var open = xhr.open;
    xhr.open = function (method, url) {
      replay = lookup(url);
      if (replay === null && url && !/^(https?:)?\\/\\//.test(url)) missed.push(pathOf(url));
      return open.apply(xhr, arguments);
    };
    var send = xhr.send;
    xhr.send = function () {
      if (replay === null) return send.apply(xhr, arguments);
      var body = JSON.stringify(replay);
      Object.defineProperty(xhr, "readyState", { get: function () { return 4; } });
      Object.defineProperty(xhr, "status", { get: function () { return 200; } });
      Object.defineProperty(xhr, "responseText", { get: function () { return body; } });
      Object.defineProperty(xhr, "response", { get: function () { return body; } });
      setTimeout(function () {
        if (typeof xhr.onreadystatechange === "function") xhr.onreadystatechange();
        xhr.dispatchEvent(new Event("readystatechange"));
        xhr.dispatchEvent(new Event("load"));
        xhr.dispatchEvent(new Event("loadend"));
      }, 0);
    };
    return xhr;
  }
  StaticXHR.UNSENT = 0; StaticXHR.OPENED = 1; StaticXHR.HEADERS_RECEIVED = 2;
  StaticXHR.LOADING = 3; StaticXHR.DONE = 4;
  window.XMLHttpRequest = StaticXHR;

  /* Writes are inert here, but they must not look broken: the returns workflow
     and "save view" controls should decline politely rather than throw. */
  window.__STATIC_SITE__ = true;
})();
"""

# NOTE: substituted with str.replace, not str.format - the inline CSS below is
# full of braces that format() would read as fields.
STATIC_BANNER = """
<div class="static-demo-banner" role="note">
  <strong>Prerendered snapshot.</strong>
  Every figure below was computed at build time from the synthetic dataset.
  <a href="__LIVE_URL__" rel="noopener">Open the live app</a> for filter
  combinations outside the presets, the returns workflow, and admin.
</div>
<style>
.static-demo-banner{font:500 13px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;
  padding:10px 16px;background:rgba(125,211,252,.10);border-bottom:1px solid rgba(125,211,252,.28);
  color:inherit;text-align:center}
.static-demo-banner a{color:#38bdf8;text-decoration:underline;text-underline-offset:2px}
</style>
"""

# The live application can afford rich controls and chart libraries because it
# is Tier 1. The frozen site cannot: after the browser pass has converted the
# page into final markup this is the *only* JavaScript left. It moves already-
# rendered secondary sections in and out of a tab panel and swaps already-
# rendered preset fragments. It performs no calculation and calls no API.
STATIC_RUNTIME = r"""
(function () {
  "use strict";
  var cache = new Map();
  var config = function () {
    var el = document.getElementById("static-site-config");
    try { return JSON.parse(el && el.textContent || "{}"); } catch (_) { return {}; }
  };

  function bindTabs() {
    document.querySelectorAll("[data-static-tabs]").forEach(function (tabs) {
      if (tabs.dataset.bound === "1") return;
      tabs.dataset.bound = "1";
      var mount = tabs.querySelector("[data-static-tab-panel]");
      tabs.addEventListener("click", function (event) {
        var button = event.target.closest("[data-static-tab]");
        if (!button || !mount) return;
        var template = document.getElementById(button.dataset.staticTab || "");
        if (!template) return;
        var render = function () {
          tabs.querySelectorAll("[data-static-tab]").forEach(function (item) {
            var active = item === button;
            item.setAttribute("aria-selected", active ? "true" : "false");
            item.classList.toggle("is-active", active);
          });
          mount.replaceChildren(template.content.cloneNode(true));
          mount.hidden = false;
        };
        if (template.content.childNodes.length || !template.dataset.staticSrc) {
          render(); return;
        }
        button.disabled = true;
        fetch(template.dataset.staticSrc, {credentials:"same-origin", cache:"force-cache"})
          .then(function (response) { if (!response.ok) throw new Error("Section unavailable"); return response.text(); })
          .then(function (html) {
            template.innerHTML = html.replaceAll("__STATIC_ROOT__/", String(config().siteRoot || ""));
            render();
          }).finally(function () { button.disabled = false; });
      });
    });
  }

  function fragmentUrl(preset) {
    var cfg = config();
    return String(cfg.dataBase || "data/") + encodeURIComponent(preset) + "/" +
      encodeURIComponent(cfg.page || "overview") + ".json";
  }

  function getFragment(preset) {
    if (!cache.has(preset)) {
      cache.set(preset, fetch(fragmentUrl(preset), {
        credentials: "same-origin", cache: "force-cache", priority: "low"
      }).then(function (response) {
        if (!response.ok) throw new Error("Preset unavailable");
        return response.json();
      }));
    }
    return cache.get(preset);
  }

  function bindPreset() {
    document.querySelectorAll("[data-static-preset]").forEach(function (select) {
      if (select.dataset.bound === "1") return;
      select.dataset.bound = "1";
      select.addEventListener("change", function () {
        var preset = select.value;
        select.disabled = true;
        getFragment(preset).then(function (payload) {
          var current = document.querySelector("main");
          if (!current || !payload.main) throw new Error("Preset fragment is incomplete");
          current.outerHTML = payload.main.replaceAll("__STATIC_ROOT__/", String(config().siteRoot || ""));
          document.title = payload.title || document.title;
          document.body.dataset.staticPreset = preset;
          history.replaceState({}, "", payload.path || location.pathname);
          bind();
        }).catch(function () {
          select.disabled = false;
          var status = document.querySelector("[data-static-preset-status]");
          if (status) status.textContent = "That preset is unavailable. Open the live app for custom filters.";
        });
      });
    });
  }

  function bindTheme() {
    document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
      if (button.dataset.staticBound === "1") return;
      button.dataset.staticBound = "1";
      button.addEventListener("click", function () {
        var root = document.documentElement;
        var next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
        root.setAttribute("data-theme", next);
        try { localStorage.setItem("wa-theme", next); } catch (_) {}
      });
    });
  }

  function bind() { bindTabs(); bindPreset(); bindTheme(); }
  bind();

  var warm = function () {
    var cfg = config();
    (cfg.presets || []).filter(function (p) { return p !== cfg.preset; })
      .forEach(function (p) { getFragment(p).catch(function () {}); });
    document.querySelectorAll("template[data-static-src]").forEach(function (template) {
      fetch(template.dataset.staticSrc, {credentials:"same-origin", cache:"force-cache", priority:"low"})
        .then(function (response) { return response.ok ? response.text() : ""; })
        .then(function (html) {
          if (html) template.innerHTML = html.replaceAll("__STATIC_ROOT__/", String(cfg.siteRoot || ""));
        }).catch(function () {});
    });
  };
  if ("requestIdleCallback" in window) requestIdleCallback(warm, { timeout: 2500 });
  else window.addEventListener("load", warm, { once: true });
})();
"""

STATIC_CRITICAL_CSS = """
.static-scope-bar{display:flex;align-items:center;gap:.75rem;flex-wrap:wrap;padding:.8rem 1rem;
  margin:0 0 1rem;border:1px solid var(--wa-hairline);border-radius:.8rem;background:var(--wa-surface)}
.static-scope-bar label{font-weight:700}.static-scope-bar select{min-width:13rem;max-width:100%;padding:.45rem .7rem;
  border:1px solid var(--wa-hairline);border-radius:.5rem;background:var(--wa-surface);color:var(--wa-text)}
.static-scope-note{margin-left:auto;color:var(--wa-text-muted);font-size:.82rem}
.static-detail-tabs{margin:1rem 0 2rem;padding:1rem;border:1px solid var(--wa-hairline);border-radius:1rem;
  background:var(--wa-surface)}
.static-detail-tabs__list{display:flex;gap:.5rem;overflow:auto;padding:.15rem 0 .75rem}
.static-detail-tabs__list button{white-space:nowrap;border:1px solid var(--wa-hairline);border-radius:999px;
  padding:.45rem .8rem;background:transparent;color:inherit;font-weight:650}
.static-detail-tabs__list button.is-active{background:var(--wa-accent);color:#fff}
.static-detail-tabs__panel[hidden]{display:none}.static-chart{display:block;width:100%;height:auto;min-height:180px;
  object-fit:contain}.static-demo-banner{content-visibility:auto}
@media(max-width:640px){.static-scope-note{width:100%;margin-left:0}.static-scope-bar select{width:100%}}
"""

# Exact URLs currently referenced by base.html. They are copied into the build
# before Chromium opens a page, so CI does not depend on a third-party CDN for
# either rendering or the final site. Missing downloads are surfaced as build
# warnings and the original URL remains as a last-resort live-app fallback.
REMOTE_ASSETS: dict[str, str] = {
    "https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css":
        "static/vendor/bootstrap/bootstrap.min.css",
    "https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/js/bootstrap.bundle.min.js":
        "static/vendor/bootstrap/bootstrap.bundle.min.js",
    "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.13.1/font/bootstrap-icons.min.css":
        "static/vendor/bootstrap-icons/bootstrap-icons.min.css",
    "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.13.1/font/fonts/bootstrap-icons.woff2?dd67030699838ea613ee6dbda90effa6":
        "static/vendor/bootstrap-icons/fonts/bootstrap-icons.woff2",
    "https://cdn.plot.ly/plotly-basic-2.35.2.min.js":
        "static/vendor/plotly/plotly-basic-2.35.2.min.js",
    "https://cdn.plot.ly/plotly-2.35.2.min.js":
        "static/vendor/plotly/plotly-2.35.2.min.js",
}

# Sections kept in the first-paint DOM. Every remaining section is still in the
# HTML, inside a template-backed tab, but does not count toward layout or DOM
# cost until the reviewer explicitly opens it.
SECTION_RULES: dict[str, tuple[str, int]] = {
    # 6, not 4: the trend workspace is the sixth section, and it holds the
    # charts. Cutting at 4 met the node budget with a first paint that had no
    # picture in it at all - the landing page is the one place the chart has to
    # be there when the HTML arrives, not one click away.
    "overview": ("#overviewPage > section, #overviewPage > div > section", 6),
    "products": ("#products-main > section", 3),
    "inventory": ("#InventoryApp > section", 4),
    "customers": ("main.app-main > section", 4),
    "labor": ("#LaborPage > section", 3),
    "planning": ("#reportContent > .report-section", 2),
    "regions": ("#RegionsOverviewV2App > section", 4),
    "suppliers": ("#SuppliersPage > section, #SuppliersV2App > section", 4),
    "salesreps": ("#SalesRepsApp > section", 3),
}


# ─────────────────────────────────────────────────────────────────────────────
# Build
# ─────────────────────────────────────────────────────────────────────────────
class Builder:
    def __init__(self, out: Path, live_url: str, verbose: bool = True):
        self.out = out
        self.live_url = live_url
        self.verbose = verbose
        self.manifest: dict[str, Any] = {"pages": [], "scopes": [], "drilldowns": {}}
        self.app = None
        self.client = None
        self.presets: list[str] = []
        self.fragments: dict[tuple[str, str], dict[str, str]] = {}
        self.targets: list[dict[str, Any]] = []
        self.stats = {
            "pages": 0,
            "drilldowns": 0,
            "api_hits": 0,
            "api_misses": 0,
            "bytes": 0,
            "cube_rows": 0,
            "cube_bytes": 0,
            "prerendered": 0,
            "chart_assets": 0,
        }

    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    # -- app -----------------------------------------------------------------
    def boot(self) -> None:
        from app import create_app

        self.app = create_app()
        with self.app.app_context():
            from app.auth.models import get_user_by_username

            user = None
            for name in ("gm", "admin", "demo"):
                user = get_user_by_username(name)
                if user is not None:
                    self.username = name
                    break
            if user is None:
                raise SystemExit(
                    "No demo user found. Run:\n"
                    "  python manage.py init-auth-db && python manage.py seed-demo-users"
                )
            uid = str(user.id)
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = uid
            sess["_fresh"] = False
        self.log(f"booted; rendering as {self.username!r}")

    def scope_args(self, preset: str) -> dict[str, str]:
        """The query the front end would send for a preset.

        The JS computes the fiscal window client-side and puts explicit
        start/end on every request, so the build has to do the same or it
        captures a different window than the page will ask for.
        """
        args = {"date_preset": preset, "_gf": "1"}
        with self.app.app_context():
            from app.services.comparison import data_cutoff
            from app.services.filters import FISCAL_DATE_TYPE, get_fiscal_periods

            try:
                periods = get_fiscal_periods()
            except Exception:
                periods = {}
            window = periods.get(preset)
            if window:
                args["start"] = window["start"].strftime("%Y-%m-%d")
                end = window["end"]
                # A filter may run through today while the fixed snapshot ends
                # weeks earlier. Clamp every static scope to the observed data;
                # otherwise the final supplier month is a three-day sliver that
                # visually collapses to zero.
                try:
                    cutoff = data_cutoff()
                    if cutoff is not None and end.date() > cutoff:
                        end = end.replace(year=cutoff.year, month=cutoff.month, day=cutoff.day)
                except Exception:
                    pass
                args["end"] = end.strftime("%Y-%m-%d")
                args["date_type"] = FISCAL_DATE_TYPE
        return args

    def get(self, path: str, args: dict[str, str] | None = None):
        qs = urllib.parse.urlencode(args or {}, doseq=True)
        url = f"{path}?{qs}" if qs else path
        return self.client.get(url, follow_redirects=True)

    # -- offline data layer --------------------------------------------------
    def precompute_cube(self) -> None:
        """Aggregate the immutable fact table once at its useful finest grain."""
        with self.app.app_context():
            from app.services import fact_store

            cols = fact_store.list_columns()

            def pick(*names: str) -> str:
                for name in names:
                    if name in cols:
                        return name
                raise RuntimeError(f"static cube is missing all candidate columns: {names}")

            date_col = pick("OrderDate", "DateOrdered", "Date")
            customer_col = pick("CustomerId", "CustomerName")
            product_col = pick("ProductId", "SKU", "ProductName")
            supplier_col = pick("SupplierId", "SupplierName")
            region_col = pick("RegionName", "RegionId")
            department_col = pick("ProteinType", "ProteinName", "ProductCategory", "Category")
            revenue_col = pick("Revenue", "Price")
            cost_col = pick("Cost", "CostPrice")
            profit_col = pick("Profit")
            units_col = pick("QuantityShipped", "QuantityOrdered")
            weight_col = pick("WeightLb", "ShippedLb")
            order_col = pick("OrderId", "OrderLineId")

            def q(name: str) -> str:
                return '"' + name.replace('"', '""') + '"'

            sql = f"""
                SELECT
                    CAST({q(date_col)} AS DATE) AS date,
                    CAST({q(customer_col)} AS VARCHAR) AS customer,
                    CAST({q(product_col)} AS VARCHAR) AS product,
                    CAST({q(supplier_col)} AS VARCHAR) AS supplier,
                    CAST({q(region_col)} AS VARCHAR) AS region,
                    CAST({q(department_col)} AS VARCHAR) AS department,
                    SUM(COALESCE({q(revenue_col)}, 0)) AS revenue,
                    SUM(COALESCE({q(cost_col)}, 0)) AS cost,
                    SUM(COALESCE({q(profit_col)}, 0)) AS profit,
                    SUM(COALESCE({q(units_col)}, 0)) AS units,
                    SUM(COALESCE({q(weight_col)}, 0)) AS weight,
                    COUNT(DISTINCT {q(order_col)}) AS orders
                FROM fact
                GROUP BY 1,2,3,4,5,6
                ORDER BY 1,2,3,4,5,6
            """
            frame = fact_store.execute_sql_df(sql, tag="static_cube")

        split = json.loads(frame.to_json(orient="split", date_format="iso", date_unit="s"))
        payload = {
            "schema": "northgate_fact_cube_v1",
            "grain": ["date", "customer", "product", "supplier", "region", "department"],
            "measures": ["revenue", "cost", "profit", "units", "weight", "orders"],
            "columns": split["columns"],
            "data": split["data"],
        }
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        size_mb = len(encoded) / 1024 / 1024
        if size_mb >= 50:
            raise RuntimeError(
                f"precomputed cube is {size_mb:.1f} MB, above the measured 50 MB in-memory budget"
            )
        dest = self.out / "data" / "fact-cube.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(encoded)
        self.stats["cube_rows"] = int(len(frame.index))
        self.stats["cube_bytes"] = len(encoded)
        self.log(f"  fact cube: {len(frame.index):,} rows, {size_mb:.1f} MB (measured; in-memory safe)")

    # -- self-hosted build dependencies -------------------------------------
    def ensure_remote_assets(self) -> None:
        for url, rel in REMOTE_ASSETS.items():
            dest = self.out / rel
            if dest.exists() and dest.stat().st_size:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                with urllib.request.urlopen(url, timeout=45) as response:
                    dest.write_bytes(response.read())
            except Exception as exc:
                self.log(f"  ! vendor {url}: {type(exc).__name__}: {exc}")

    def rewrite_remote_assets(self, html: str, base: str) -> str:
        for url, rel in REMOTE_ASSETS.items():
            if (self.out / rel).exists():
                html = html.replace(url, f"{base}{rel}")
        return html

    # -- capture -------------------------------------------------------------
    def capture_apis(self, page: Page, args: dict[str, str]) -> dict[str, Any]:
        payloads: dict[str, Any] = {}
        for api in page.apis:
            call_args = dict(args)
            call_args.update(page.api_args.get(api, {}))
            try:
                resp = self.get(api, call_args)
            except Exception as exc:
                self.log(f"    ! {api}: {type(exc).__name__}: {exc}")
                self.stats["api_misses"] += 1
                continue
            if resp.status_code != 200:
                # Not every page uses every endpoint listed; a 404/410 here just
                # means this build does not need it.
                self.stats["api_misses"] += 1
                continue
            ctype = resp.headers.get("Content-Type", "")
            if "json" not in ctype:
                self.stats["api_misses"] += 1
                continue
            try:
                payloads[api.rstrip("/") or "/"] = resp.get_json()
                self.stats["api_hits"] += 1
            except Exception:
                self.stats["api_misses"] += 1
        return payloads

    # -- transform -----------------------------------------------------------
    # Hidden failure UI for a request this build does not make. It can never
    # fire here, so it is dead weight in every page; removing it also keeps the
    # "no loading state anywhere" check honest instead of special-casing it.
    DEAD_UI = (
        re.compile(r'<div class="filters-loading-overlay.*?</div>\s*</div>', re.S),
        re.compile(r'<div class="d-flex justify-content-end bg-white[^"]*" id="filtersRetryWrap".*?</div>\s*</div>', re.S),
    )

    def staticize(
        self,
        html: str,
        payloads: dict[str, Any],
        depth: int,
        page_key: str,
        preset: str,
    ) -> str:
        base = "../" * depth if depth else ""

        for pattern in self.DEAD_UI:
            html = pattern.sub("", html)

        # Absolute app links -> relative static files.
        html = self.rewrite_links(html, base, preset)
        html = self.rewrite_remote_assets(html, base)

        # Data first, then the shim, then everything the page already loads:
        # the shim has to be installed before any page script can call fetch.
        blob = json.dumps(payloads, separators=(",", ":"), default=str)
        injection = (
            f'<script type="application/json" id="static-api-payloads">{blob}</script>\n'
            f"<script>{SHIM}</script>\n"
        )
        if "</head>" in html:
            html = html.replace("</head>", injection + "</head>", 1)
        else:
            html = injection + html

        banner = STATIC_BANNER.replace("__LIVE_URL__", self.live_url)
        if "<body" in html:
            idx = html.find(">", html.find("<body")) + 1
            html = html[:idx] + banner + html[idx:]

        return html

    LINK_RE = re.compile(r'((?:href|src|action)=")(/[^"#][^"]*)"')

    def rewrite_links(self, html: str, base: str, preset: str) -> str:
        """Point every in-app link at its static file."""
        scope_prefix = "" if preset == self.presets[0] else f"scopes/{preset}/"
        route_map = {p.route.rstrip("/"): f"{scope_prefix}{p.out}" for p in PAGES}

        def sub(m: re.Match) -> str:
            attr, url = m.group(1), m.group(2)
            path = url.split("?")[0].split("#")[0].rstrip("/")
            if url.startswith("/static/"):
                return f'{attr}{base}{url.lstrip("/")}"'
            if path in route_map:
                return f'{attr}{base}{route_map[path]}"'
            if path in ("", "/"):
                return f'{attr}{base}index.html"'
            # Drilldowns are emitted by `drilldown_path`; anything else is a
            # route this build does not cover (exports, admin, auth) and is sent
            # to the live app so the link is not simply dead.
            drill = self.drilldown_path(path)
            if drill:
                return f'{attr}{base}{drill}"'
            return f'{attr}{self.live_url.rstrip("/")}{url}"'

        return self.LINK_RE.sub(sub, html)

    @staticmethod
    def drilldown_path(path: str) -> str | None:
        m = re.match(r"^/customers/drilldown/([^/]+)$", path)
        if m:
            return f"drilldowns/customers/{m.group(1)}.html"
        m = re.match(r"^/products/([^/]+)/drilldown$", path)
        if m:
            return f"drilldowns/products/{m.group(1)}.html"
        m = re.match(r"^/regions/(?:drilldown/)?([^/]+)$", path)
        if m and m.group(1) not in {"export", "export_momentum"}:
            return f"drilldowns/regions/{slugify(m.group(1))}.html"
        m = re.match(r"^/suppliers/(?:drilldown/)?([^/]+)$", path)
        if m and not m.group(1).startswith("api") and m.group(1) != "export":
            return f"drilldowns/suppliers/{m.group(1)}.html"
        return None

    # -- write ---------------------------------------------------------------
    def write(self, rel: str, html: str) -> None:
        dest = self.out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(html, encoding="utf-8")
        self.stats["bytes"] += len(html.encode("utf-8"))

    def build_page(self, page: Page, preset: str, prefix: str = "") -> dict[str, Any] | None:
        args = self.scope_args(preset)
        t0 = time.perf_counter()
        resp = self.get(page.route, args)
        if resp.status_code != 200:
            self.log(f"  ! {page.route} -> {resp.status_code}, skipped")
            return None
        html = resp.get_data(as_text=True)
        payloads = self.capture_apis(page, args)

        rel = f"{prefix}{page.out}" if prefix else page.out
        depth = rel.count("/")
        self.write(rel, self.staticize(html, payloads, depth, page.key, preset))
        self.targets.append({"rel": rel, "page": page.key, "preset": preset, "fragment": True})
        self.stats["pages"] += 1
        ms = (time.perf_counter() - t0) * 1000
        size = len((self.out / rel).read_text(encoding="utf-8").encode("utf-8")) / 1024
        self.log(f"  {rel:<40} {ms:>6.0f} ms  {size:>7.1f} KB  {len(payloads)} payloads")
        return {"key": page.key, "title": page.title, "path": rel,
                "scope": preset, "kb": round(size, 1), "payloads": len(payloads)}

    # -- drilldowns ----------------------------------------------------------
    # A drilldown that fetches its own detail needs that payload captured too.
    # Keyed by entity, each entry is (api path, the query parameter naming the
    # entity). The freeze pass asserts `window.__staticMisses` is empty, so a
    # drilldown whose endpoint is missing here fails the build rather than
    # shipping a page that silently reaches for the network.
    DRILLDOWN_APIS: dict[str, tuple[tuple[str, str], ...]] = {
        "customers": (("/api/customers/drilldown/bundle", "customer_id"),),
        "products": (("/products/api/drilldown/bundle", "product_id"),),
        "suppliers": (
            ("/suppliers/api/drilldown/bundle", "supplier_id"),
            ("/suppliers/api/drilldown", "supplier_id"),
        ),
        # The region bundle names its parameter `region_id` but is keyed on the
        # region *name*, which is also what the drilldown route takes.
        "regions": (("/api/regions/drilldown/bundle", "region_id"),),
    }

    def build_drilldowns(self, preset: str, limit: int | None = None) -> None:
        args = self.scope_args(preset)
        specs = [
            ("customers", "/customers/drilldown/{id}", self.list_customers, "drilldowns/customers/{id}.html"),
            ("products", "/products/{id}/drilldown", self.list_products, "drilldowns/products/{id}.html"),
            ("suppliers", "/suppliers/{id}", self.list_suppliers, "drilldowns/suppliers/{id}.html"),
            ("regions", "/regions/{id}", self.list_regions, "drilldowns/regions/{slug}.html"),
        ]
        for name, route_tpl, lister, out_tpl in specs:
            try:
                ids = lister()
            except Exception as exc:
                self.log(f"  ! could not list {name}: {exc}")
                continue
            if limit:
                ids = ids[:limit]
            ok = 0
            for ident in ids:
                route = route_tpl.format(id=ident)
                resp = self.get(route, args)
                if resp.status_code != 200:
                    continue
                payloads: dict[str, Any] = {}
                for api, param in self.DRILLDOWN_APIS.get(name, ()):
                    detail_args = dict(args)
                    detail_args[param] = ident
                    try:
                        detail = self.get(api, detail_args)
                    except Exception:
                        continue
                    if detail.status_code == 200 and "json" in detail.headers.get("Content-Type", ""):
                        try:
                            payloads[api.rstrip("/") or "/"] = detail.get_json()
                            self.stats["api_hits"] += 1
                        except Exception:
                            pass
                rel = out_tpl.format(id=ident, slug=slugify(ident))
                html = resp.get_data(as_text=True)
                self.write(rel, self.staticize(html, payloads, rel.count("/"), name, preset))
                self.targets.append({"rel": rel, "page": name, "preset": preset, "fragment": False})
                ok += 1
                self.stats["drilldowns"] += 1
            self.manifest["drilldowns"][name] = ok
            self.log(f"  drilldowns/{name}: {ok}/{len(ids)}")

    def _distinct(self, column: str) -> list[str]:
        with self.app.app_context():
            from app.services import fact_store

            df = fact_store.execute_sql_df(
                f'SELECT DISTINCT "{column}" AS v FROM fact WHERE "{column}" IS NOT NULL ORDER BY 1',
                tag="static_build",
            )
            return [str(v) for v in df["v"].tolist() if str(v).strip()]

    def list_customers(self) -> list[str]:
        return self._distinct("CustomerId")

    def list_products(self) -> list[str]:
        return self._distinct("ProductId")

    def list_suppliers(self) -> list[str]:
        return self._distinct("SupplierId")

    def list_regions(self) -> list[str]:
        return self._distinct("RegionName")

    # -- assets --------------------------------------------------------------
    def copy_assets(self) -> None:
        src = ROOT / "app" / "static"
        dest = self.out / "static"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
        total = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
        self.log(f"  static assets: {total/1024/1024:.1f} MB")

    # -- browser freeze ------------------------------------------------------
    def _write_chart_asset(self, svg: str) -> str:
        raw = svg.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()[:16]
        rel = f"static/charts/{digest}.svg"
        dest = self.out / rel
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
            self.stats["chart_assets"] += 1
        return rel

    def _write_text_asset(self, folder: str, suffix: str, value: str) -> str:
        raw = value.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()[:16]
        rel = f"{folder}/{digest}.{suffix}"
        dest = self.out / rel
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
        return rel

    @staticmethod
    def _decode_svg_data_url(value: str) -> str:
        header, _, body = value.partition(",")
        if not body:
            return value
        if ";base64" in header:
            return base64.b64decode(body).decode("utf-8")
        return urllib.parse.unquote(body)

    def _freeze_charts(self, page: Any, base: str) -> int:
        frozen = 0

        # Plotly carries thousands of interaction nodes per chart. `toImage`
        # gives us its authoritative SVG, after which one eager image replaces
        # the entire interactive graph in the first-paint DOM.
        plots = page.locator(".js-plotly-plot")
        for index in reversed(range(plots.count())):
            plot = plots.nth(index)
            try:
                result = plot.evaluate(
                    """async (el) => {
                      const rect = el.getBoundingClientRect();
                      const width = Math.max(320, Math.round(rect.width || el.clientWidth || 800));
                      const height = Math.max(180, Math.round(rect.height || el.clientHeight || 360));
                      const url = await window.Plotly.toImage(el, {format:'svg', width, height});
                      const heading = el.closest('section,article')?.querySelector('h2,h3,h4');
                      return {url, width, height, alt:el.getAttribute('aria-label') || heading?.textContent?.trim() || 'Analytics chart'};
                    }"""
                )
                svg = self._decode_svg_data_url(result["url"])
                rel = self._write_chart_asset(svg)
                plot.evaluate(
                    """(el, cfg) => {
                      const img = document.createElement('img');
                      img.className = 'static-chart'; img.src = cfg.src; img.alt = cfg.alt;
                      img.width = cfg.width; img.height = cfg.height; img.loading = 'eager';
                      img.decoding = 'sync'; el.replaceWith(img);
                    }""",
                    {**result, "src": f"{base}{rel}"},
                )
                frozen += 1
            except Exception as exc:
                self.log(f"    ! plotly chart {index}: {type(exc).__name__}: {exc}")

        # Chart.js is canvas-only. Preserve its exact pixels inside a standalone
        # SVG wrapper so the final document still paints a chart without running
        # Chart.js (and without placing a large data URL in the HTML itself).
        canvases = page.locator("canvas")
        for index in reversed(range(canvases.count())):
            canvas = canvases.nth(index)
            try:
                result = canvas.evaluate(
                    """(el) => {
                      const rect = el.getBoundingClientRect();
                      return {
                        png: el.toDataURL('image/png'),
                        width: Math.max(1, Math.round(rect.width || el.width || 800)),
                        height: Math.max(1, Math.round(rect.height || el.height || 320)),
                        alt: el.getAttribute('aria-label') || 'Analytics chart'
                      };
                    }"""
                )
                svg = (
                    '<svg xmlns="http://www.w3.org/2000/svg" role="img" '
                    f'aria-label="{_xml_escape(result["alt"])}" viewBox="0 0 {result["width"]} {result["height"]}">'
                    f'<image width="{result["width"]}" height="{result["height"]}" href="{result["png"]}"/>'
                    "</svg>"
                )
                rel = self._write_chart_asset(svg)
                canvas.evaluate(
                    """(el, cfg) => {
                      const img = document.createElement('img');
                      img.className = 'static-chart'; img.src = cfg.src; img.alt = cfg.alt;
                      img.width = cfg.width; img.height = cfg.height; img.loading = 'eager';
                      img.decoding = 'sync'; el.replaceWith(img);
                    }""",
                    {**result, "src": f"{base}{rel}"},
                )
                frozen += 1
            except Exception as exc:
                self.log(f"    ! canvas chart {index}: {type(exc).__name__}: {exc}")
        return frozen

    def _externalize_closed_tabs(self, page: Any, base: str) -> None:
        templates = page.locator("template[id^='static-tab-']")
        for index in range(templates.count()):
            template = templates.nth(index)
            content = template.evaluate("el => el.innerHTML")
            if not content.strip():
                continue
            # Preset fragments can be inserted into pages at a different depth.
            # A root placeholder is resolved by the tiny runtime against the
            # physical page the visitor actually loaded.
            content = _root_placeholder(content)
            rel = self._write_text_asset("data/sections", "html", _minify_html(content))
            template.evaluate(
                """(el, src) => { el.innerHTML = ''; el.dataset.staticSrc = src; }""",
                f"{base}{rel}",
            )

    def _externalize_inline_styles(self, page: Any, base: str) -> None:
        styles = page.locator("style:not(#static-critical)")
        css_parts = [styles.nth(index).text_content() or "" for index in range(styles.count())]
        css = "\n".join(part for part in css_parts if part.strip())
        if not css:
            return
        rel = self._write_text_asset("static/css/prerendered", "css", css)
        page.evaluate(
            """(href) => {
              document.querySelectorAll('style:not(#static-critical)').forEach(el => el.remove());
              const link = document.createElement('link'); link.rel = 'stylesheet'; link.href = href;
              const critical = document.getElementById('static-critical');
              document.head.insertBefore(link, critical || null);
            }""",
            f"{base}{rel}",
        )

    def _freeze_one(self, browser: Any, origin: str, target: dict[str, Any]) -> dict[str, Any]:
        rel = target["rel"]
        page_key = target["page"]
        preset = target["preset"]
        depth = rel.count("/")
        base = "../" * depth if depth else ""
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        console_errors: list[str] = []
        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        try:
            page.goto(f"{origin}/{rel}", wait_until="domcontentloaded", timeout=45_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            # Walk the full page once. This is build-time work: it deliberately
            # trips every IntersectionObserver before scripts are discarded.
            page.evaluate(
                """async () => {
                  const step = Math.max(500, Math.floor(innerHeight * .8));
                  for (let y = 0; y < document.documentElement.scrollHeight; y += step) {
                    scrollTo(0, y);
                    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
                  }
                  scrollTo(0, 0);
                }"""
            )
            page.wait_for_timeout(350)
            misses = page.evaluate("window.__staticMisses || []")
            if misses:
                raise RuntimeError(f"uncaptured same-origin requests: {sorted(set(misses))}")

            charts = self._freeze_charts(page, base)

            selector, keep = SECTION_RULES.get(page_key, ("", 0))
            if selector:
                page.evaluate(
                    """({selector, keep}) => {
                      const sections = [...document.querySelectorAll(selector)].filter(el => el.isConnected);
                      const secondary = sections.slice(keep);
                      if (!secondary.length) return;
                      const tabs = document.createElement('section');
                      tabs.className = 'static-detail-tabs'; tabs.dataset.staticTabs = '1';
                      tabs.innerHTML = '<h2>Detailed analysis</h2><p>Open a section; every view was rendered at build time.</p>' +
                        '<div class="static-detail-tabs__list" role="tablist"></div>' +
                        '<div class="static-detail-tabs__panel" data-static-tab-panel hidden></div>';
                      const list = tabs.querySelector('[role=tablist]');
                      secondary[0].before(tabs);
                      secondary.forEach((section, index) => {
                        const template = document.createElement('template');
                        template.id = `static-tab-${index}`;
                        const heading = section.querySelector('h2,h3,h4');
                        const label = (heading?.textContent || `Section ${index + 1}`).trim();
                        template.content.append(section);
                        tabs.append(template);
                        const button = document.createElement('button');
                        button.type = 'button'; button.role = 'tab';
                        button.dataset.staticTab = template.id;
                        button.setAttribute('aria-selected', 'false');
                        button.textContent = label;
                        list.append(button);
                      });
                    }""",
                    {"selector": selector, "keep": keep},
                )
            # Scrub before the closed tabs are lifted out into their own files:
            # anything still on an element when it leaves the document keeps
            # whatever it was carrying, and a fragment is not revisited later.
            #
            # `data-initial` is the big one. Several drilldowns bootstrap from a
            # JSON blob dumped into an HTML attribute, which means every quote
            # is escaped to `&quot;` - six bytes for one. On a region drilldown
            # that single attribute was 566 KB of a 655 KB page, and by this
            # point it has already been rendered into the DOM.
            page.evaluate(
                """() => {
                  /* Secondary sections have already been moved into <template>
                     elements by this point, and template content is inert: it
                     is a separate document fragment that querySelectorAll on
                     `document` never descends into. Scrubbing only the live
                     tree left a live /api/ href inside a closed tab. */
                  const scrub = (root) => {
                    root.querySelectorAll('*').forEach(el => {
                      el.removeAttribute('data-initial');
                      el.removeAttribute('data-bootstrap');
                      el.removeAttribute('data-payload');
                      [...el.attributes].forEach(attr => {
                        if (String(attr.value || '').includes('/api/')) el.removeAttribute(attr.name);
                      });
                      if (el.tagName === 'TEMPLATE') scrub(el.content);
                    });
                  };
                  scrub(document);
                }"""
            )
            self._externalize_closed_tabs(page, base)

            preset_options = [
                {"value": item, "label": _preset_label(item), "selected": item == preset}
                for item in self.presets
            ]
            config_payload = {
                "page": page_key,
                "preset": preset,
                "presets": self.presets,
                "dataBase": f"{base}data/",
                "siteRoot": base,
            }
            page.evaluate(
                """({options, filterOptions, config, criticalCss}) => {
                  document.getElementById('GlobalFilters')?.remove();
                  document.getElementById('savedViewsSection')?.remove();
                  document.querySelectorAll('script').forEach(el => el.remove());
                  document.querySelectorAll('link[rel="modulepreload"],link[rel="preload"][as="script"]').forEach(el => el.remove());
                  document.querySelectorAll('.filters-loading-overlay,#filtersRetryWrap,#filtersErrorBanner,#filtersPendingState,.spinner-border,.skeleton,[class*="-skeleton"],[class*="_skeleton"]').forEach(el => el.remove());
                  /* Anything still saying it is loading is lying on a page whose
                     data is already in the DOM. These read as inert markup to a
                     grep over the built file, which is how the build check sees
                     them, and as a stalled page to a reviewer if any CSS ever
                     makes them visible again. */
                  document.querySelectorAll('body *').forEach(el => {
                    if (el.children.length) return;
                    var text = (el.textContent || '').trim();
                    if (/^(Loading|Retry filters|Reading |Building |Summarizing |Resolving )/.test(text)) el.remove();
                  });
                  document.body.classList.remove('loading');
                  document.querySelectorAll('.is-loading,[aria-busy="true"]').forEach(el => {
                    el.classList.remove('is-loading'); el.removeAttribute('aria-busy');
                  });
                  document.querySelectorAll('*').forEach(el => {
                    [...el.attributes].forEach(attr => {
                      if (String(attr.value || '').includes('/api/')) el.removeAttribute(attr.name);
                    });
                  });

                  const style = document.createElement('style');
                  style.id = 'static-critical'; style.textContent = criticalCss;
                  document.head.append(style);
                  const optionData = document.createElement('script');
                  optionData.type = 'application/json'; optionData.id = 'filter-options';
                  optionData.textContent = JSON.stringify(filterOptions || {});
                  document.head.append(optionData);
                  const configData = document.createElement('script');
                  configData.type = 'application/json'; configData.id = 'static-site-config';
                  configData.textContent = JSON.stringify(config);
                  document.head.append(configData);

                  const main = document.querySelector('main');
                  if (main) {
                    const bar = document.createElement('div');
                    bar.className = 'static-scope-bar';
                    const label = document.createElement('label'); label.textContent = 'Preset scope';
                    const select = document.createElement('select'); select.dataset.staticPreset = '1';
                    options.forEach(item => {
                      const option = document.createElement('option'); option.value = item.value;
                      option.textContent = item.label; option.selected = item.selected; select.append(option);
                    });
                    label.append(select); bar.append(label);
                    const note = document.createElement('span'); note.className = 'static-scope-note';
                    note.dataset.staticPresetStatus = '1'; note.textContent = 'Prerendered snapshot · no server request';
                    bar.append(note); main.prepend(bar);
                  }
                  document.body.dataset.staticPage = config.page;
                  document.body.dataset.staticPreset = config.preset;
                }""",
                {
                    "options": preset_options,
                    "filterOptions": page.evaluate(
                        """() => { try {
                          const raw = JSON.parse(document.getElementById('filtersBootstrapData')?.textContent || '{}');
                          return raw.options_payload || {};
                        } catch (_) { return {}; } }"""
                    ),
                    "config": config_payload,
                    "criticalCss": STATIC_CRITICAL_CSS,
                },
            )
            self._externalize_inline_styles(page, base)

            stats = page.evaluate(
                """() => ({
                  nodes: document.querySelectorAll('*').length,
                  height: document.documentElement.scrollHeight,
                  main: document.querySelector('main')?.outerHTML || '',
                  title: document.title
                })"""
            )
            html = page.content()
            runtime = f'<script src="{base}static/js/static-runtime.js" defer></script>'
            html = html.replace("</body>", runtime + "</body>", 1)
            html = _minify_html(html)
            (self.out / rel).write_text(html, encoding="utf-8")
            self.stats["bytes"] += len(html.encode("utf-8"))
            self.stats["prerendered"] += 1
            if target.get("fragment"):
                self.fragments[(page_key, preset)] = {
                    "title": stats["title"],
                    "main": stats["main"],
                    "path": f"?preset={urllib.parse.quote(preset)}",
                }
            for entry in self.manifest["pages"]:
                if entry.get("path") == rel:
                    entry.update(
                        kb=round(len(html.encode("utf-8")) / 1024, 1),
                        nodes=stats["nodes"],
                        height=stats["height"],
                        charts=charts,
                    )
            if console_errors:
                self.log(f"    console: {console_errors[0][:160]}")
            return {"kb": len(html.encode("utf-8")) / 1024, **stats, "charts": charts}
        finally:
            page.close()

    def freeze_pages(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - exercised in clean environments
            raise RuntimeError(
                "Playwright is required for final HTML: pip install playwright && playwright install chromium"
            ) from exc

        runtime_path = self.out / "static" / "js" / "static-runtime.js"
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_path.write_text(STATIC_RUNTIME.strip() + "\n", encoding="utf-8")

        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, _format: str, *args: Any) -> None:
                return

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(QuietHandler, directory=str(self.out))
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        self.stats["bytes"] = 0
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    for target in self.targets:
                        result = self._freeze_one(browser, origin, target)
                        self.log(
                            f"  freeze {target['rel']:<33} {result['kb']:>7.1f} KB  "
                            f"{result['nodes']:>4} nodes  {result['height']:>5}px  {result['charts']} charts"
                        )
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()

    def write_preset_fragments(self) -> None:
        for (page_key, preset), payload in self.fragments.items():
            dest = self.out / "data" / preset / f"{page_key}.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            payload = dict(payload)
            payload["main"] = _minify_html(_root_placeholder(payload.get("main", "")))
            dest.write_text(
                json.dumps(payload, separators=(",", ":"), ensure_ascii=False), encoding="utf-8"
            )

    def fingerprint_assets(self) -> None:
        """Create content-addressed asset names and point every page at them."""
        static_root = self.out / "static"
        mapping: dict[str, str] = {}
        originals = [path for path in static_root.rglob("*") if path.is_file()]
        for source in originals:
            rel = source.relative_to(self.out).as_posix()
            raw = source.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()[:12]
            target = source.with_name(f"{source.stem}.{digest}{source.suffix}")
            if target != source and not target.exists():
                shutil.copy2(source, target)
            mapping[rel] = target.relative_to(self.out).as_posix()

        def rewrite(text: str) -> str:
            for old, new in mapping.items():
                pattern = rf"((?:\.\./)*){re.escape(old)}(?:\?[^\"'<>\s]*)?"
                text = re.sub(pattern, lambda match: f"{match.group(1)}{new}", text)
            return text

        for path in self.out.rglob("*.html"):
            path.write_text(rewrite(path.read_text(encoding="utf-8")), encoding="utf-8")
        for payload in self.fragments.values():
            payload["main"] = rewrite(payload.get("main", ""))

        (self.out / "asset-manifest.json").write_text(
            json.dumps(mapping, indent=2, sort_keys=True), encoding="utf-8"
        )
        # Netlify and Cloudflare Pages both understand this file. GitHub Pages
        # still benefits from the content-addressed URLs even though it chooses
        # its own cache header values.
        (self.out / "_headers").write_text(
            """/static/*
  Cache-Control: public, max-age=31536000, immutable
/data/*
  Cache-Control: public, max-age=3600
/*.html
  Cache-Control: public, max-age=0, must-revalidate
""",
            encoding="utf-8",
        )

    def write_meta(self, presets: list[str]) -> None:
        self.manifest["scopes"] = presets
        self.manifest["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.manifest["live_url"] = self.live_url
        self.manifest["stats"] = self.stats
        (self.out / "manifest.json").write_text(
            json.dumps(self.manifest, indent=2), encoding="utf-8"
        )
        # GitHub Pages would otherwise run the tree through Jekyll, which drops
        # files and directories beginning with an underscore.
        (self.out / ".nojekyll").write_text("", encoding="utf-8")

    def run(self, presets: list[str], drilldowns: bool, drilldown_limit: int | None) -> None:
        self.boot()
        self.out.mkdir(parents=True, exist_ok=True)
        self.presets = list(presets)

        self.log("\nassets")
        self.copy_assets()
        self.ensure_remote_assets()

        self.log("\noffline cube")
        self.precompute_cube()

        default = presets[0]
        self.log(f"\ndefault scope: {default}")
        for page in PAGES:
            entry = self.build_page(page, default)
            if entry:
                self.manifest["pages"].append(entry)

        for preset in presets[1:]:
            self.log(f"\nscope: {preset}")
            for page in PAGES:
                entry = self.build_page(page, preset, prefix=f"scopes/{preset}/")
                if entry:
                    self.manifest["pages"].append(entry)

        if drilldowns:
            self.log("\ndrilldowns")
            self.build_drilldowns(default, limit=drilldown_limit)

        self.log("\nprerender final DOM and charts")
        self.freeze_pages()
        self.fingerprint_assets()
        self.write_preset_fragments()
        self.write_meta(presets)

        s = self.stats
        self.log(
            f"\ndone: {s['pages']} pages, {s['drilldowns']} drilldowns, "
            f"{s['api_hits']} payloads inlined ({s['api_misses']} endpoints not used), "
            f"{s['bytes']/1024/1024:.1f} MB of HTML"
        )


def slugify(value: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return out or "item"


def _preset_label(value: str) -> str:
    special = {
        "current_fy": "Current FY",
        "previous_fy": "Previous FY",
        "current_fq": "Current fiscal quarter",
        "previous_fq": "Previous fiscal quarter",
        "current_fm": "Current fiscal month",
        "previous_fm": "Previous fiscal month",
        "fytd_comparison": "FYTD comparison",
        "all": "All available data",
    }
    return special.get(value, value.replace("_", " ").title())


def _xml_escape(value: Any) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _minify_html(value: str) -> str:
    # Do not collapse text-node whitespace globally: preformatted tables and
    # inline copy rely on it. Removing comments and whitespace *between* tags
    # captures most of the template indentation without changing semantics.
    value = re.sub(r"<!--(?!\[if).*?-->", "", value, flags=re.S)
    value = re.sub(r">\s+<", "><", value)
    return value.strip()


def _root_placeholder(value: str) -> str:
    return re.sub(r"(?:\.\./)*((?:static|data)/)", r"__STATIC_ROOT__/\1", value)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="dist", help="output directory (default: dist)")
    ap.add_argument("--scopes", default="", help="comma-separated presets (default: all in the UI)")
    ap.add_argument("--live-url", default=os.getenv("STATIC_LIVE_URL", "https://wholesale-analytics-platform.onrender.com"),
                    help="where 'Open the live app' points")
    ap.add_argument("--no-drilldowns", action="store_true")
    ap.add_argument("--drilldown-limit", type=int, default=None,
                    help="cap per entity type (for a fast smoke build)")
    ap.add_argument("--clean", action="store_true", help="remove the output directory first")
    args = ap.parse_args()

    presets = [p.strip() for p in args.scopes.split(",") if p.strip()] or discover_presets()
    out = Path(args.out).resolve()
    if args.clean and out.exists():
        shutil.rmtree(out)

    print(f"building {len(PAGES)} pages x {len(presets)} scopes -> {out}")
    print(f"scopes: {', '.join(presets)}")

    builder = Builder(out, args.live_url)
    builder.run(presets, drilldowns=not args.no_drilldowns, drilldown_limit=args.drilldown_limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

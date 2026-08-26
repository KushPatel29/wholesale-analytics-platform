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
import posixpath
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
         # `/marketing/api/bundle` is captured here too: the acquisition
         # economics panel fills from a secondary fetch after the main payload
         # renders, so without it the frozen page would show an empty panel and
         # log a static miss.
         apis=("/overview/api/bundle", "/api/overview/forecast", "/marketing/api/bundle"),
         api_args={"/api/overview/forecast": {
             "metric": "revenue", "horizon_months": "6", "granularity": "monthly",
             "include_current_month": "1", "v2": "1"}}),
    Page("customers", "Customers", "/customers/", "customers/index.html"),
    # The four tabs along the top of the customers page. They were never in
    # this list, so `rewrite_links` fell through to its last case and pointed
    # them at the live app: a reviewer clicking "Cohorts" left the CDN and woke
    # a spun-down Render container, which is the exact latency the static site
    # exists to avoid. They are server-rendered and fetch nothing on load, so
    # they prerender like any other page.
    #
    # (The cohort heatmap's cell drilldown does fetch, but only on click, so it
    # cannot fail the build; that one interaction stays live-app only.)
    Page("customers_kpis", "Customer KPIs", "/customers/kpis", "customers/kpis.html",
         apis=("/api/customers/bundle",)),
    Page("customers_rfm", "Customer RFM", "/customers/rfm", "customers/rfm.html"),
    Page("customers_cohorts", "Customer Cohorts", "/customers/cohorts", "customers/cohorts.html"),
    Page("customers_clv", "Customer CLV", "/customers/clv", "customers/clv.html"),
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
    # Entity-level statements, so the page takes no filters and the scope
    # presets below produce identical output for it - which is correct: a
    # balance sheet does not change because someone picked a region.
    Page("finance", "Finance", "/finance/", "finance/index.html",
         apis=("/finance/api/bundle",)),
    Page("marketing", "Marketing", "/marketing/", "marketing/index.html",
         apis=("/marketing/api/bundle",)),
    Page("metrics", "Metric Catalogue", "/metrics/", "metrics/index.html"),
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

  /* Writes are inert here, but they must not look broken: server-backed
     actions/operations and "save view" controls decline politely rather than
     throw. */
  window.__STATIC_SITE__ = true;
})();
"""

# NOTE: substituted with str.replace, not str.format - the inline CSS below is
# full of braces that format() would read as fields.
# Resolve the theme before the first paint, on a page that has had every other
# script removed. Deliberately tiny and inline: a separate file would be a
# blocking request in front of the paint it exists to get right.
STATIC_THEME_BOOT = (
    "<script>(function(){try{"
    'var q=new URLSearchParams(location.search).get("theme");'
    'if(q==="light"||q==="dark"){localStorage.setItem("wa-theme",q);}'
    'var t=(q==="light"||q==="dark")?q:localStorage.getItem("wa-theme");'
    'if(t!=="light"&&t!=="dark"){'
    't=window.matchMedia("(prefers-color-scheme: light)").matches?"light":"dark";}'
    'document.documentElement.setAttribute("data-theme",t);'
    "}catch(e){}})();</script>"
)

STATIC_BANNER = """
<div class="static-demo-banner" role="note">
  <strong>Prerendered snapshot.</strong>
  Every figure below was computed at build time from the synthetic dataset.
  <a href="__LIVE_URL__" rel="noopener" data-wa-live-link>Open the live app</a>
  for custom filters and the shared actions, operations, returns, and admin ledger
  &mdash; <span class="static-demo-banner__warn">it runs on a free instance and
  can take ~20s to wake</span>.
</div>
<style>
.static-demo-banner{font:500 13px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;
  padding:10px 16px;background:rgba(125,211,252,.10);border-bottom:1px solid rgba(125,211,252,.28);
  color:inherit;text-align:center}
.static-demo-banner a{color:#38bdf8;text-decoration:underline;text-underline-offset:2px}
.static-demo-banner__warn{opacity:.8}
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
    if (document.documentElement.dataset.staticAnchorTabsBound !== "1") {
      document.documentElement.dataset.staticAnchorTabsBound = "1";
      document.addEventListener("click", function (event) {
        var anchor = event.target.closest && event.target.closest('a[href^="#"]');
        if (!anchor) return;
        var target = decodeURIComponent(String(anchor.getAttribute("href") || "").slice(1));
        if (!target || document.getElementById(target)) return;
        var button = Array.prototype.find.call(
          document.querySelectorAll("[data-static-tab][data-static-targets]"),
          function (item) {
            return String(item.dataset.staticTargets || "").split(" ").indexOf(target) !== -1;
          }
        );
        if (!button) return;
        event.preventDefault();
        button.click();
        setTimeout(function () {
          button.closest("[data-static-tabs]")?.scrollIntoView({block:"start", behavior:"smooth"});
        }, 0);
      });
    }
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
    /* The body carries the active preset as metadata too. Binding its bubbled
       change events treated `body.value` as a preset and briefly requested
       `/data/undefined/<page>.json` before the real select handler ran. */
    document.querySelectorAll("select[data-static-preset]").forEach(function (select) {
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
          /* `option.selected = true` is a live DOM property and is not
             guaranteed to survive serialising `main.outerHTML` into the
             fragment JSON. The data changed while the replacement select
             jumped back to "Current FY", so the control reported a different
             scope from the figures underneath it. Reassert the requested
             value on the newly inserted control before it is rebound. */
          /* The body also carries `data-static-preset`; target the control,
             not the first matching element. Reassert both properties and
             attributes because browsers do not serialize a changed option's
             live selected state consistently. */
          var nextSelect = document.querySelector("select[data-static-preset]");
          if (nextSelect) {
            nextSelect.querySelectorAll("option").forEach(function (option) {
              var selected = option.value === preset;
              option.selected = selected;
              option.defaultSelected = selected;
              option.toggleAttribute("selected", selected);
            });
            nextSelect.value = preset;
            nextSelect.disabled = false;
          }
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

  /* Bootstrap is deliberately removed from the frozen page, but the shell
     still contains two controls whose meaning depends on it: the responsive
     navigation toggle and dropdown menus (including the account / logout
     menu). Keep those controls honest with a small, dependency-free binding. */
  function bindShellMenus() {
    if (document.documentElement.dataset.staticShellBound === "1") return;
    document.documentElement.dataset.staticShellBound = "1";

    function closeDropdowns(except) {
      document.querySelectorAll('[data-bs-toggle="dropdown"][aria-expanded="true"]').forEach(function (toggle) {
        if (toggle === except) return;
        toggle.setAttribute("aria-expanded", "false");
        var menu = toggle.parentElement && toggle.parentElement.querySelector(":scope > .dropdown-menu");
        if (menu) menu.classList.remove("show");
      });
    }

    document.addEventListener("click", function (event) {
      var dropdown = event.target.closest('[data-bs-toggle="dropdown"]');
      if (dropdown) {
        event.preventDefault();
        var menu = dropdown.parentElement && dropdown.parentElement.querySelector(":scope > .dropdown-menu");
        if (!menu) return;
        var opening = !menu.classList.contains("show");
        closeDropdowns(dropdown);
        menu.classList.toggle("show", opening);
        dropdown.setAttribute("aria-expanded", opening ? "true" : "false");
        return;
      }

      var collapse = event.target.closest('[data-bs-toggle="collapse"]');
      if (collapse) {
        var selector = collapse.getAttribute("data-bs-target") || collapse.getAttribute("href") || "";
        if (selector.charAt(0) !== "#") return;
        var panel = document.querySelector(selector);
        if (!panel) return;
        event.preventDefault();
        var expanded = !panel.classList.contains("show");
        panel.classList.toggle("show", expanded);
        collapse.setAttribute("aria-expanded", expanded ? "true" : "false");
        return;
      }

      if (!event.target.closest(".dropdown-menu")) closeDropdowns();
    });

    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape") return;
      var open = document.querySelector('[data-bs-toggle="dropdown"][aria-expanded="true"]');
      closeDropdowns();
      if (open) open.focus();
    });
  }

  /* A CSS pseudo-element is anchored to the centre of its owner. That is fine
     for an info icon, but wrong for a horizontal bar whose hit area spans most
     of a chart: the label can appear hundreds of pixels away from the cursor.
     One fixed tooltip follows the pointer, clamps to the viewport, and also
     honours keyboard focus for the non-chart help controls. */
  function bindHoverDetails() {
    if (document.documentElement.dataset.staticHoverBound === "1") return;
    document.documentElement.dataset.staticHoverBound = "1";
    document.documentElement.classList.add("wa-tip-runtime");

    var tip = document.createElement("div");
    tip.className = "wa-hover-detail";
    tip.setAttribute("role", "tooltip");
    tip.hidden = true;
    document.body.append(tip);
    var active = null;

    function place(x, y) {
      if (tip.hidden) return;
      var gap = 14;
      var box = tip.getBoundingClientRect();
      var left = Math.min(Math.max(gap, x + gap), Math.max(gap, innerWidth - box.width - gap));
      var top = y + gap;
      if (top + box.height + gap > innerHeight) top = Math.max(gap, y - box.height - gap);
      tip.style.left = left + "px";
      tip.style.top = top + "px";
    }

    function show(owner, x, y) {
      var value = owner && owner.getAttribute("data-wa-tip");
      if (!value) return;
      active = owner;
      tip.textContent = value;
      tip.hidden = false;
      place(x, y);
    }

    function hide(owner) {
      if (owner && active !== owner) return;
      active = null;
      tip.hidden = true;
    }

    document.addEventListener("pointerover", function (event) {
      var owner = event.target.closest("[data-wa-tip]");
      if (owner) show(owner, event.clientX, event.clientY);
    });
    document.addEventListener("pointermove", function (event) {
      if (active) place(event.clientX, event.clientY);
    });
    document.addEventListener("pointerout", function (event) {
      var owner = event.target.closest("[data-wa-tip]");
      if (owner && (!event.relatedTarget || !owner.contains(event.relatedTarget))) hide(owner);
    });
    document.addEventListener("focusin", function (event) {
      var owner = event.target.closest("[data-wa-tip]");
      if (!owner) return;
      var box = owner.getBoundingClientRect();
      show(owner, box.left + box.width / 2, box.bottom);
    });
    document.addEventListener("focusout", function (event) {
      var owner = event.target.closest("[data-wa-tip]");
      if (owner) hide(owner);
    });

    /* Everything above answers a pointer. A chart also has to answer a
       keyboard and a finger, and its data points can do neither: they are
       `aria-hidden` boxes with no tab stop, deliberately, because putting 151
       empty stops in the tab order is worse than none.
       So the chart is the tab stop, and the arrow keys walk the points. */
    function readPoint(chart, spot) {
      if (!spot) return;
      chart.querySelectorAll(".wa-hot.is-active").forEach(function (other) {
        other.classList.remove("is-active");
      });
      spot.classList.add("is-active");
      chart.dataset.waHotIndex = spot.dataset.waHot || "0";
      var box = spot.getBoundingClientRect();
      show(spot, box.left + box.width / 2, box.bottom);
      var live = chart.querySelector(".wa-hot-live");
      // One sentence, so a screen reader does not read a tooltip's line breaks.
      if (live) live.textContent = (spot.getAttribute("data-wa-tip") || "").replace(/\n/g, ", ");
    }

    function clearPoints(chart) {
      chart.querySelectorAll(".wa-hot.is-active").forEach(function (spot) {
        spot.classList.remove("is-active");
      });
      chart.removeAttribute("data-wa-hot-index");
      var live = chart.querySelector(".wa-hot-live");
      if (live) live.textContent = "";
      hide(active);
    }

    document.addEventListener("keydown", function (event) {
      var chart = event.target.closest && event.target.closest(".static-chart-wrap");
      if (!chart) {
        if (event.key === "Escape" && active) hide(active);
        return;
      }
      var list = [].slice.call(chart.querySelectorAll(".wa-hot"));
      if (!list.length) return;
      var current = parseInt(chart.dataset.waHotIndex, 10);
      if (isNaN(current)) current = -1;
      var next;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") next = Math.min(list.length - 1, current + 1);
      else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = Math.max(0, (current < 0 ? 1 : current) - 1);
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = list.length - 1;
      else if (event.key === "Escape") { clearPoints(chart); return; }
      else return;
      event.preventDefault();
      readPoint(chart, list[next]);
    });

    document.addEventListener("focusout", function (event) {
      var chart = event.target.closest && event.target.closest(".static-chart-wrap");
      if (chart && !chart.contains(event.relatedTarget)) clearPoints(chart);
    });

    // A finger lands near a point far more often than on it, so resolve the
    // tap to the nearest one rather than requiring a direct hit.
    document.addEventListener("pointerdown", function (event) {
      if (event.pointerType === "mouse") return;
      var chart = event.target.closest && event.target.closest(".static-chart-wrap");
      if (!chart) { if (active) hide(active); return; }
      var direct = event.target.closest(".wa-hot");
      if (direct) { readPoint(chart, direct); return; }
      var best = null;
      var bestDistance = Infinity;
      chart.querySelectorAll(".wa-hot").forEach(function (spot) {
        var box = spot.getBoundingClientRect();
        var dx = event.clientX - (box.left + box.width / 2);
        var dy = event.clientY - (box.top + box.height / 2);
        var distance = dx * dx + dy * dy;
        if (distance < bestDistance) { bestDistance = distance; best = spot; }
      });
      if (best && bestDistance < 88 * 88) readPoint(chart, best); else clearPoints(chart);
    });
  }

  function bindPrint() {
    document.querySelectorAll("[data-print-report]").forEach(function (button) {
      if (button.dataset.staticBound === "1") return;
      button.dataset.staticBound = "1";
      button.addEventListener("click", function () { window.print(); });
    });
  }

  /* Preset fragments are captured before the final page-level freeze pass.
     Reapply the snapshot control contract after every fragment replacement so
     exports remain useful Print / Save actions and live-only recomputation
     controls never return as dead buttons. */
  function adaptSnapshotControls() {
    var printLabels = {
      downloadSnapshotBtn: "Print / Save Snapshot",
      exportDataHealthBtn: "Print / Save Data Issues",
      moversExportBtn: "Print / Save Movers",
      driversExportBtn: "Print / Save Drivers",
      concentrationExportBtn: "Print / Save Concentration",
      marginRiskExportBtn: "Print / Save Margin Risk",
      trendExportBtn: "Print / Save Trend"
    };
    Object.keys(printLabels).forEach(function (id) {
      var control = document.getElementById(id);
      if (!control) return;
      control.dataset.printReport = "1";
      control.disabled = false;
      control.removeAttribute("aria-disabled");
      control.removeAttribute("title");
      control.textContent = printLabels[id];
    });
    document.querySelectorAll(
      '#diagnosticWorkspacesDisclosure button:not([data-print-report]),' +
      '#diagnosticWorkspacesDisclosure select,' +
      '#diagnosticWorkspacesDisclosure input[type="checkbox"]'
    ).forEach(function (control) {
      control.disabled = true;
      control.setAttribute("aria-disabled", "true");
      control.setAttribute("title", "Live recomputation is unavailable in this prerendered snapshot.");
    });
  }

  /* An in-page target can live inside one or more closed <details> elements.
     Native fragment navigation does not open those ancestors, so the Overview
     "Open trust center" links changed the URL and appeared to do nothing. */
  function bindAnchorReveal() {
    if (document.documentElement.dataset.staticAnchorRevealBound === "1") return;
    document.documentElement.dataset.staticAnchorRevealBound = "1";
    document.addEventListener("click", function (event) {
      var anchor = event.target.closest && event.target.closest('a[href^="#"]');
      if (!anchor) return;
      var id = decodeURIComponent(String(anchor.getAttribute("href") || "").slice(1));
      var target = id && document.getElementById(id);
      if (!target) return;
      var details = target.closest("details");
      while (details) {
        details.open = true;
        details = details.parentElement && details.parentElement.closest("details");
      }
    }, true);
  }

  /* The planner ships a View control - Everything, Demand only, Supply only,
     Just what to do, Choose sections - and on a frozen page it did nothing at
     all. A reviewer picks "Demand only", the page does not move, and the
     honest conclusion is that the demo is broken.

     It does not need the app back. Every section is already in the document;
     a view is a set of section ids to show. The page publishes its own map on
     the control (`data-static-sections`) so the two cannot drift apart. */
  function bindReportViews() {
    document.querySelectorAll("select[data-static-sections]").forEach(function (select) {
      if (select.dataset.staticBound === "1") return;
      select.dataset.staticBound = "1";
      var presets;
      try { presets = JSON.parse(select.dataset.staticSections || "{}"); } catch (_) { return; }
      var panel = document.getElementById("sectionToggles");
      var list = document.getElementById("visibilityList");
      var saveButton = document.getElementById("savePresetBtn");
      var STORE = "wa-planner-view";
      var buttons = function () {
        return list ? [].slice.call(list.querySelectorAll("[data-id]")) : [];
      };

      function apply(ids) {
        [].slice.call(document.querySelectorAll("[data-report-section]")).forEach(function (el) {
          el.hidden = ids.indexOf(el.getAttribute("data-report-section")) === -1;
        });
        buttons().forEach(function (button) {
          var on = ids.indexOf(button.getAttribute("data-id")) !== -1;
          button.classList.toggle("active", on);
          button.setAttribute("aria-pressed", on ? "true" : "false");
        });
      }
      function chosen() {
        return buttons().filter(function (button) {
          return button.classList.contains("active");
        }).map(function (button) { return button.getAttribute("data-id"); });
      }
      function sync() {
        var custom = select.value === "custom";
        if (panel) panel.style.display = custom ? "block" : "none";
        if (!custom) apply(presets[select.value] || presets.full || []);
      }

      select.addEventListener("change", sync);
      if (list) list.addEventListener("click", function (event) {
        var button = event.target.closest("[data-id]");
        if (!button || select.value !== "custom") return;
        button.classList.toggle("active");
        apply(chosen());
      });
      if (saveButton) saveButton.addEventListener("click", function () {
        try {
          localStorage.setItem(STORE, JSON.stringify({type: select.value, sections: chosen()}));
        } catch (_) {}
        var label = saveButton.innerHTML;
        saveButton.innerHTML = '<i class="bi bi-check-lg me-1"></i> VIEW SAVED';
        setTimeout(function () { saveButton.innerHTML = label; }, 1800);
      });

      try {
        var saved = JSON.parse(localStorage.getItem(STORE) || "null");
        if (saved && saved.type && presets[saved.type]) {
          select.value = saved.type; sync();
        } else if (saved && saved.type === "custom" && (saved.sections || []).length) {
          select.value = "custom";
          if (panel) panel.style.display = "block";
          apply(saved.sections);
        }
      } catch (_) {}
    });
  }

  /* Hand the theme to the live app on the way out.
     The two halves are separate origins and therefore separate localStorage,
     so without this a visitor reading in Light arrives in Dark and the flip is
     the only sign they crossed a boundary at all. */
  function bindCrossOrigin() {
    if (document.documentElement.dataset.staticCrossOriginBound === "1") return;
    document.documentElement.dataset.staticCrossOriginBound = "1";
    document.addEventListener("click", function (event) {
      var link = event.target.closest && event.target.closest("a[href]");
      if (!link) return;
      var href = link.getAttribute("href") || "";
      if (!/^https?:\/\//i.test(href)) return;
      try {
        var url = new URL(href, location.href);
        if (url.origin === location.origin) return;
        var root = document.documentElement;
        url.searchParams.set("theme", root.getAttribute("data-theme") === "light" ? "light" : "dark");
        link.setAttribute("href", url.toString());
      } catch (_) { /* leave the link alone */ }
    }, true);
  }

  function bind() {
    adaptSnapshotControls();
    bindTabs(); bindPreset(); bindTheme(); bindPrint(); bindReportViews();
    bindShellMenus(); bindHoverDetails(); bindAnchorReveal(); bindCrossOrigin();
  }
  bind();
})();
"""

# ─────────────────────────────────────────────────────────────────────────────
# Hover, on a page that cannot run a chart library
# ─────────────────────────────────────────────────────────────────────────────
#
# A frozen chart is a picture, and a picture does not tell you which bar you are
# pointing at. That is the single interaction a reader of a dashboard reaches
# for first, and losing it is what makes a prerendered page feel like a
# screenshot of an app rather than the app.
#
# So each point is asked, at build time, for the label the live chart would have
# drawn - by calling the chart library's own hover code and reading what it puts
# on screen, rather than by reimplementing `hovertemplate` and guessing at the
# format strings. The label and the point's box are kept; everything else about
# the interaction is discarded.
#
# Boxes are stored as percentages of the plot, not pixels. The frozen image is
# responsive and this build runs at one viewport width; pixel hotspots would sit
# beside their bars on every other screen.
#
# Charts denser than this are skipped rather than half-covered: a scatter of 400
# customers would add 400 nodes to the page for hit targets three pixels wide.
HOTSPOT_MAX_PER_CHART = 80

HOTSPOT_HELPERS = r"""
  const HOTSPOT_MAX = %d;

  /* Plotly draws its hover label into `.hoverlayer` as one <text> per line,
     with the trace name repeated as a separate node. Read it back the way it
     reads on screen, minus that repeat. */
  function readPlotlyHover(el) {
    const layer = el.querySelector('.hoverlayer');
    if (!layer) return '';
    const lines = [];
    layer.querySelectorAll('text').forEach(text => {
      const spans = text.querySelectorAll('tspan');
      (spans.length ? [...spans] : [text]).forEach(node => {
        const value = (node.textContent || '').replace(/\s+/g, ' ').trim();
        if (value && lines[lines.length - 1] !== value) lines.push(value);
      });
    });
    return lines.slice(0, 6).join('\n');
  }

  function boxOf(node, frame) {
    const rect = node.getBoundingClientRect();
    if (!rect.width && !rect.height) return null;
    /* A marker three pixels across is not a hover target. Grow the smallest
       boxes about their own centre so a pointer can actually land on them. */
    const minW = Math.min(frame.width * 0.05, 22);
    const minH = Math.min(frame.height * 0.07, 22);
    const w = Math.max(rect.width, minW), h = Math.max(rect.height, minH);
    const left = rect.left - (w - rect.width) / 2 - frame.left;
    const top = rect.top - (h - rect.height) / 2 - frame.top;
    return {
      l: +Math.max(0, (left / frame.width) * 100).toFixed(2),
      t: +Math.max(0, (top / frame.height) * 100).toFixed(2),
      w: +Math.min(100, (w / frame.width) * 100).toFixed(2),
      h: +Math.min(100, (h / frame.height) * 100).toFixed(2)
    };
  }

  function plotlyHotspots(el) {
    const frame = el.getBoundingClientRect();
    if (!frame.width || !frame.height || !window.Plotly || !window.Plotly.Fx) return [];
    const nodes = [];
    el.querySelectorAll('g.trace').forEach(group => {
      const calc = group.__data__;
      const trace = Array.isArray(calc) && calc[0] ? calc[0].trace : null;
      if (!trace) return;
      const curve = trace.index != null ? trace.index : trace._expandedIndex;
      if (curve == null) return;
      group.querySelectorAll('g.point, path.point, path.slice, g.slice').forEach(node => {
        if (node.__data__ && node.__data__.i != null) nodes.push({node, curve, point: node.__data__.i});
      });
    });
    if (!nodes.length || nodes.length > HOTSPOT_MAX) return [];
    const out = [];
    nodes.forEach(entry => {
      const box = boxOf(entry.node, frame);
      if (!box) return;
      let tip = '';
      try {
        /* Plotly ignores a hover request that looks like the one already
           showing, so the previous label has to come down first. Without this
           every point on the chart reports the first point's numbers. */
        window.Plotly.Fx.unhover(el);
        window.Plotly.Fx.hover(el, [{curveNumber: entry.curve, pointNumber: entry.point}]);
        tip = readPlotlyHover(el);
      } catch (_) { tip = ''; }
      if (tip) out.push({...box, tip});
    });
    try { window.Plotly.Fx.unhover(el); } catch (_) {}
    return out;
  }

  /* A canvas is one element, so a bar has no bounding rect of its own. Rebuild
     it from the geometry Chart.js exposes: a bar carries the coordinates of
     both of its ends, a point carries a centre and a radius. */
  function chartjsBox(chart, element, frame) {
    const p = element.getProps
      ? element.getProps(['x', 'y', 'base', 'width', 'height', 'outerRadius'], true)
      : element;
    let left, top, w, h;
    if (p.base != null && p.width != null && p.height != null) {
      if (chart.options && chart.options.indexAxis === 'y') {
        left = Math.min(p.x, p.base); w = Math.abs(p.x - p.base) || p.width;
        top = p.y - p.height / 2;      h = p.height;
      } else {
        top = Math.min(p.y, p.base);   h = Math.abs(p.base - p.y) || p.height;
        left = p.x - p.width / 2;      w = p.width;
      }
    } else {
      const centre = element.getCenterPoint ? element.getCenterPoint() : {x: p.x, y: p.y};
      const radius = Math.max(p.outerRadius || 0,
                              (element.options && element.options.radius) || 0, 8);
      left = centre.x - radius; top = centre.y - radius; w = radius * 2; h = radius * 2;
    }
    if (!isFinite(left) || !isFinite(top) || !isFinite(w) || !isFinite(h)) return null;
    const minW = Math.min(frame.width * 0.05, 22), minH = Math.min(frame.height * 0.07, 22);
    if (w < minW) { left -= (minW - w) / 2; w = minW; }
    if (h < minH) { top -= (minH - h) / 2; h = minH; }
    return {
      l: +Math.max(0, (left / frame.width) * 100).toFixed(2),
      t: +Math.max(0, (top / frame.height) * 100).toFixed(2),
      w: +Math.min(100, (w / frame.width) * 100).toFixed(2),
      h: +Math.min(100, (h / frame.height) * 100).toFixed(2)
    };
  }

  /* Chart.js keeps its tooltip model separate from the canvas, so the real
     callbacks - currency formatting, share-of-total suffixes - can be read
     without drawing anything. The canvas is repainted clean afterwards so no
     tooltip is baked into the captured pixels. */
  function chartjsHotspots(el) {
    const Chart = window.Chart;
    if (!Chart || !Chart.getChart) return [];
    const chart = Chart.getChart(el);
    if (!chart || !chart.tooltip) return [];
    /* Chart.js reports element coordinates against its own drawing surface,
       which is not always the CSS box - so measure against the surface. The
       frozen image carries that same aspect, which is what makes a percentage
       hotspot land on its bar. */
    const box = el.getBoundingClientRect();
    const frame = {
      width: chart.width || box.width,
      height: chart.height || box.height
    };
    if (!frame.width || !frame.height) return [];
    const targets = [];
    (chart.data.datasets || []).forEach((dataset, di) => {
      const meta = chart.getDatasetMeta(di);
      if (!meta || meta.hidden) return;
      (meta.data || []).forEach((point, ix) => targets.push({di, ix, point}));
    });
    if (!targets.length || targets.length > HOTSPOT_MAX) return [];
    const out = [];
    const seen = new Set();
    try {
      targets.forEach(entry => {
        const box = chartjsBox(chart, entry.point, frame);
        if (!box) return;
        chart.tooltip.setActiveElements([{datasetIndex: entry.di, index: entry.ix}],
                                        {x: entry.point.x || 0, y: entry.point.y || 0});
        chart.tooltip.update(true);
        const body = (chart.tooltip.body || []).reduce((acc, part) => acc.concat(part.lines || []), []);
        const lines = [].concat(chart.tooltip.title || [], body)
          .map(line => String(line).replace(/\s+/g, ' ').trim()).filter(Boolean);
        if (!lines.length) return;
        /* `interaction.mode: 'index'` gives every dataset at that index the
           same label. One hotspot per box is enough. */
        const key = box.l + ':' + box.t + ':' + box.w + ':' + box.h;
        if (seen.has(key)) return;
        seen.add(key);
        out.push({...box, tip: lines.slice(0, 6).join('\n')});
      });
    } catch (_) { /* leave whatever was collected */ }
    try {
      chart.tooltip.setActiveElements([], {x: 0, y: 0});
      chart.tooltip.update(true);
      chart.update('none');
    } catch (_) {}
    return out;
  }

""" % HOTSPOT_MAX_PER_CHART

PLOTLY_FREEZE_JS = (
    "async (el) => {"
    + HOTSPOT_HELPERS
    + """
      const rect = el.getBoundingClientRect();
      const width = Math.max(320, Math.round(rect.width || el.clientWidth || 800));
      const height = Math.max(180, Math.round(rect.height || el.clientHeight || 360));
      let hotspots = [];
      try { hotspots = plotlyHotspots(el); } catch (_) { hotspots = []; }
      const url = await window.Plotly.toImage(el, {format:'svg', width, height});
      // Nearest card first, then the section. Two charts sharing one section
      // both inherited its heading - the regions drilldown published two
      // images both called "Operational Mix", which a screen reader cannot
      // tell apart. Card headings are h5 here, so h5/h6 are included.
      const heading = (el.closest('.card,.chart-card,figure') || el.closest('section,article'))
        ?.querySelector('h2,h3,h4,h5,h6');
      return {url, width, height, hotspots,
              alt: el.getAttribute('aria-label') || heading?.textContent?.trim() || 'Analytics chart'};
    }"""
)

CANVAS_FREEZE_JS = (
    "(el) => {"
    + HOTSPOT_HELPERS
    + """
      const rect = el.getBoundingClientRect();
      let hotspots = [];
      try { hotspots = chartjsHotspots(el); } catch (_) { hotspots = []; }
      const painted = (() => {
        try {
          const sample = document.createElement('canvas');
          sample.width = 48; sample.height = 32;
          const ctx = sample.getContext('2d', {willReadFrequently: true});
          ctx.drawImage(el, 0, 0, sample.width, sample.height);
          const px = ctx.getImageData(0, 0, sample.width, sample.height).data;
          let opaque = 0; const colours = new Set();
          for (let i = 0; i < px.length; i += 4) {
            if (px[i + 3] > 8) opaque += 1;
            colours.add(`${px[i] >> 4}:${px[i + 1] >> 4}:${px[i + 2] >> 4}:${px[i + 3] >> 4}`);
          }
          return opaque > 8 && colours.size > 2;
        } catch (_) { return false; }
      })();
      // Nearest card first, then the section. Two charts sharing one section
      // both inherited its heading - the regions drilldown published two
      // images both called "Operational Mix", which a screen reader cannot
      // tell apart. Card headings are h5 here, so h5/h6 are included.
      const heading = (el.closest('.card,.chart-card,figure') || el.closest('section,article'))
        ?.querySelector('h2,h3,h4,h5,h6');
      const width = Math.max(1, Math.round(rect.width || el.width || 800));
      let height = Math.max(1, Math.round(rect.height || el.height || 320));
      /* The capture must carry the backing store's aspect, not the CSS box's.
         Where the two disagree - the weekday chart drew 340 rows of pixels into
         a 240px box - taking the box squashes the picture. Take the width from
         layout, so the page reflows exactly as before, and the height from the
         pixels, so nothing is stretched. */
      const chart = window.Chart && window.Chart.getChart ? window.Chart.getChart(el) : null;
      const surfaceW = (chart && chart.width) || el.width;
      const surfaceH = (chart && chart.height) || el.height;
      if (surfaceW > 1 && surfaceH > 1) {
        height = Math.max(1, Math.round(width * (surfaceH / surfaceW)));
      }
      return {
        png: el.toDataURL('image/png'),
        width, height, hotspots, painted,
        alt: el.getAttribute('aria-label') || heading?.textContent?.trim() || 'Analytics chart'
      };
    }"""
)

# The image is the chart; the overlay is the hover. Hotspots are empty <b>
# elements - no text, no tab stop, nothing for a screen reader to read twice -
# whose only job is to own a `:hover` and carry the label in an attribute that
# CSS `content` can print.
CHART_MOUNT_JS = """(el, cfg) => {
  const img = document.createElement('img');
  img.className = 'static-chart'; img.src = cfg.src; img.alt = cfg.alt;
  img.width = cfg.width; img.height = cfg.height; img.loading = 'eager';
  img.decoding = 'sync';
  if (cfg.alt && cfg.alt !== 'Analytics chart') img.title = cfg.alt;
  const hotspots = cfg.hotspots || [];
  if (!hotspots.length) { el.replaceWith(img); return; }
  const wrap = document.createElement('span');
  wrap.className = 'static-chart-wrap';
  /* One tab stop per chart, not one per point. A page with 151 data points
     would otherwise put 151 empty stops in the tab order, which is worse for a
     keyboard user than no access at all. The chart is a single group; the
     arrow keys walk the points inside it. */
  wrap.tabIndex = 0;
  wrap.setAttribute('role', 'group');
  wrap.setAttribute('aria-label',
    (cfg.alt && cfg.alt !== 'Analytics chart' ? cfg.alt : 'Chart') +
    ' — ' + hotspots.length + ' data points. Use the arrow keys to read them.');
  wrap.append(img);
  hotspots.forEach((spot, index) => {
    const hot = document.createElement('b');
    hot.className = 'wa-hot';
    hot.setAttribute('data-wa-tip', spot.tip);
    /* Hidden from the accessibility tree because the value is announced from
       the chart's live region instead - otherwise a screen reader meets a
       hundred unlabelled elements. */
    hot.setAttribute('aria-hidden', 'true');
    hot.dataset.waHot = String(index);
    hot.style.cssText = 'left:' + spot.l + '%;top:' + spot.t + '%;width:' + spot.w + '%;height:' + spot.h + '%';
    /* Flip the label back inside the plot when the point is near an edge:
       above it would leave the top of the chart, and beyond either side it
       would be clipped by the card the chart sits in. */
    if (spot.t < 34) hot.dataset.waTipPlace = 'below';
    if (spot.l < 22) hot.dataset.waTipAlign = 'start';
    else if (spot.l + spot.w > 78) hot.dataset.waTipAlign = 'end';
    wrap.append(hot);
  });
  /* Where the keyboard and touch readouts are announced. Polite, so it does
     not interrupt, and inside the group so it moves with the chart. */
  const live = document.createElement('span');
  live.className = 'wa-hot-live';
  live.setAttribute('aria-live', 'polite');
  wrap.append(live);
  el.replaceWith(wrap);
}"""

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
  object-fit:contain}
/* ---- hover -------------------------------------------------------------- */
/* One tooltip for the whole site, drawn by CSS so it survives having every
   script stripped. `data-wa-tip` is set in the freeze pass: on chart hotspots,
   and on every element that was carrying a Bootstrap tooltip - which stops
   working the moment its JavaScript is removed, and takes the element's own
   `title` with it, so those elements ended up with no hover at all. */
.static-chart-wrap{position:relative;display:block;overflow:visible}
.static-chart-wrap:focus{outline:none}
.static-chart-wrap:focus-visible{outline:2px solid var(--wa-accent,#38bdf8);outline-offset:3px;border-radius:.4rem}
.static-chart-wrap .wa-hot{position:absolute;display:block;margin:0;border-radius:4px;
  background:transparent;pointer-events:auto}
.static-chart-wrap .wa-hot:hover,.static-chart-wrap .wa-hot.is-active{
  background:color-mix(in srgb,var(--wa-accent,#38bdf8) 16%,transparent);
  box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--wa-accent,#38bdf8) 55%,transparent)}
/* Keyboard and touch drive the runtime's own `.wa-hover-detail`, not the CSS
   pseudo-element - `.wa-tip-runtime` switches that off below. This class only
   marks which point is being read. */
.wa-hot-live{position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;
  clip:rect(0 0 0 0);clip-path:inset(50%);white-space:nowrap;border:0}
[data-wa-tip]{position:relative}
[data-wa-tip]::after{content:attr(data-wa-tip);position:absolute;left:50%;bottom:calc(100% + .5rem);
  transform:translateX(-50%);z-index:70;width:max-content;max-width:17rem;padding:.5rem .65rem;
  border-radius:.55rem;border:1px solid var(--wa-hairline,rgba(148,163,184,.35));
  background:var(--wa-surface-2,var(--wa-surface,#0f141d));color:var(--wa-text,#e8edf7);
  font-size:.76rem;font-weight:600;line-height:1.4;letter-spacing:.005em;text-align:left;
  white-space:pre-line;text-transform:none;box-shadow:0 12px 28px rgba(2,6,23,.42);
  opacity:0;visibility:hidden;pointer-events:none}
[data-wa-tip]:hover::after,[data-wa-tip]:focus-visible::after{opacity:1;visibility:visible}
[data-wa-tip-place="below"]::after{bottom:auto;top:calc(100% + .5rem)}
[data-wa-tip-align="start"]::after{left:0;transform:none}
[data-wa-tip-align="end"]::after{left:auto;right:0;transform:none}
.wa-tip-runtime [data-wa-tip]::after{display:none!important}
.wa-hover-detail{position:fixed;z-index:1090;width:max-content;max-width:min(22rem,calc(100vw - 24px));
  padding:.58rem .72rem;border-radius:.6rem;border:1px solid var(--wa-hairline,rgba(148,163,184,.35));
  background:var(--wa-surface-2,var(--wa-surface,#0f141d));color:var(--wa-text,#e8edf7);
  font-size:.78rem;font-weight:650;line-height:1.42;letter-spacing:.005em;text-align:left;
  white-space:pre-line;text-transform:none;box-shadow:0 14px 32px rgba(2,6,23,.34);pointer-events:none}
.wa-hover-detail[hidden]{display:none}
body[data-static-page] .navbar .dropdown-menu.show{display:block;z-index:1080}
/* The menu itself had z-index, but its navbar stacking context did not. Main
   cards painted above the open menu and received its clicks. Lift the shell,
   not individual menu items, so every desktop dropdown remains selectable. */
body[data-static-page] .navbar-wholesale{position:relative;z-index:1040;overflow:visible;isolation:isolate}
/* A card that clips its overflow clips the label with it. Only the cards that
   scroll a wide table need to clip, and `:has()` is how we tell them apart -
   the planner puts a 640px scorecard and a chart inside the same class. Where
   `:has()` is unsupported the rule is simply dropped and the label is clipped,
   which is the behaviour we started from. */
body[data-static-page] .static-chart-wrap{overflow:visible}
/* The shell that holds a chart holds nothing else, so letting it overflow
   cannot spill anything but the label. Ten of the eleven Sales Reps charts sit
   in a shell with `overflow:hidden` and had their labels cut at the plot edge. */
body[data-static-page] :has(> .static-chart-wrap){overflow:visible}
body[data-static-page] .report-card:not(:has(table)),
body[data-static-page] .chart-card:not(:has(table)){overflow:visible}
/* Touch has no hover, so the label is driven by tap instead - the hotspots
   must stay in the document for that. They used to be `display:none` here,
   which is why every chart was inert on a phone. */
/* Keep prerendered sections in the document while deferring layout and paint
   for sections below the viewport. This breaks one large first-paint layout
   into small scroll-time layouts and keeps the main thread responsive. */
body[data-static-page] main section{content-visibility:auto;contain-intrinsic-size:auto 320px}
body[data-static-page] #productsAvailability{content-visibility:auto;contain-intrinsic-size:auto 560px}
/* The live planner fades report sections in from opacity:0. The freeze pass
   intentionally disables animations, so give those sections their completed
   visual state instead of leaving newly selected views transparent. */
body[data-static-page] .report-section{opacity:1!important;transform:none!important;filter:none!important}
body[data-static-page] *,body[data-static-page] *::before,body[data-static-page] *::after{
  animation:none!important;transition:none!important;scroll-behavior:auto!important}
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
    # The Live Account Map. Both the renderer and its state-outline basemap are
    # local during the build, so rendering does not reach a third-party CDN.
    "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.js":
        "static/vendor/maplibre/maplibre-gl.js",
    "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.css":
        "static/vendor/maplibre/maplibre-gl.css",
}

# Keep the initial DOM below the published 1,800-node budget. Secondary sections
# are frozen at build time, moved into local HTML fragments, and opened by the
# tiny static runtime. No analytical calculation or API request happens on
# interaction. In-page links are mapped to their owning detail tab above, so
# progressive disclosure does not create dead navigation.
SECTION_RULES: dict[str, tuple[str, int | tuple[int, ...]]] = {
    "products": ("#products-main > section", 2),
    "labor": ("#LaborPage > section", 3),
    "salesreps": ("#SalesRepsApp > section", 4),
}
DRILLDOWN_SECTION_RULES: dict[str, tuple[str, int | tuple[int, ...]]] = {
    "products": (".product-drilldown-v2 > section", 3),
    "regions": (".region-drilldown-v2 > section", 3),
    "customers": (".ciw-page > section", 3),
    "suppliers": (".supplier-drilldown-v2 > section", 3),
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
        self.drilldown_ids: dict[str, list[str]] = {}
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
            "hotspots": 0,
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

            # The browser cube is for cross-page offline exploration, whose
            # finest useful time grain is month. Daily grain plus five entity
            # dimensions is effectively a copy of the line fact and exceeded
            # the measured browser-memory budget as the synthetic history grew.
            sql = f"""
                SELECT
                    DATE_TRUNC('month', CAST({q(date_col)} AS DATE)) AS month,
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

        # Dictionary-encode repeated dimension labels. The cube keeps every
        # dimension and measure, while the payload stores compact integer keys
        # instead of repeating customer/product/region strings hundreds of
        # thousands of times.
        dimension_columns = ["month", "customer", "product", "supplier", "region", "department"]
        encoded_frame = frame.copy()
        dimension_values: dict[str, list[str]] = {}
        for column in dimension_columns:
            codes, values = encoded_frame[column].astype("string").fillna("Unknown").factorize(sort=True)
            encoded_frame[column] = codes
            dimension_values[column] = [str(value) for value in values.tolist()]

        split = json.loads(encoded_frame.to_json(orient="split", date_format="iso", date_unit="s"))
        payload = {
            "schema": "northgate_fact_cube_v2",
            "encoding": "dictionary-v1",
            "grain": ["month", "customer", "product", "supplier", "region", "department"],
            "measures": ["revenue", "cost", "profit", "units", "weight", "orders"],
            "dimension_values": dimension_values,
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

    def _rewrite_live_links(self, page: Any, base: str) -> None:
        """Point app links at their static files, after the page has built them.

        `rewrite_links` only sees the HTML the server rendered. Every table here
        builds its rows from the payload, so the links that matter most - the
        supplier, region, customer and product a reviewer actually clicks - are
        created by JavaScript and never passed through it. They survived into
        the frozen output still pointing at `/suppliers/S1000?start=...`, which
        on a project Pages site is not even the right host.

        Rules are handed in rather than reimplemented, so the two paths cannot
        drift: whatever `drilldown_path` and the page table say here is what the
        pre-freeze rewriter said.
        """
        routes = {p.route.rstrip("/"): p.out for p in PAGES}
        drills = {
            "customers_drilldown": "drilldowns/customers/",
            "products_drilldown": "drilldowns/products/",
            "regions_drilldown": "drilldowns/regions/",
            "suppliers_drilldown": "drilldowns/suppliers/",
        }
        allowed = {
            "customers": self.drilldown_ids.get("customers", []),
            "products": self.drilldown_ids.get("products", []),
            "suppliers": self.drilldown_ids.get("suppliers", []),
            "regions": [slugify(value) for value in self.drilldown_ids.get("regions", [])],
        }
        page.evaluate(
            """({routes, drills, allowed, base, liveUrl}) => {
              const slug = (v) => String(v).toLowerCase()
                .replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'item';
              const built = Object.fromEntries(
                Object.entries(allowed).map(([key, values]) => [key, new Set(values)])
              );
              const decoded = (value) => {
                try { return decodeURIComponent(value); } catch (_) { return value; }
              };

              const target = (path) => {
                path = path.replace(/\\/+$/, '');
                if (path === '' ) return base + 'index.html';
                if (Object.prototype.hasOwnProperty.call(routes, path)) return base + routes[path];
                let m;
                if ((m = path.match(/^\\/customers\\/drilldown\\/([^/]+)$/)) &&
                    built.customers.has(decoded(m[1])))
                  return base + drills.customers_drilldown + decoded(m[1]) + '.html';
                if ((m = path.match(/^\\/products\\/([^/]+)\\/drilldown$/)) &&
                    built.products.has(decoded(m[1])))
                  return base + drills.products_drilldown + decoded(m[1]) + '.html';
                if ((m = path.match(/^\\/regions\\/(?:drilldown\\/)?([^/]+)$/)) &&
                    built.regions.has(slug(decoded(m[1]))))
                  return base + drills.regions_drilldown + slug(decoded(m[1])) + '.html';
                if ((m = path.match(/^\\/suppliers\\/(?:drilldown\\/)?([^/]+)$/)) &&
                    built.suppliers.has(decoded(m[1])))
                  return base + drills.suppliers_drilldown + decoded(m[1]) + '.html';
                return null;   // not part of this build
              };

              const fix = (root) => {
                root.querySelectorAll('a[href]').forEach((a) => {
                  const raw = a.getAttribute('href') || '';
                  if (!raw.startsWith('/') || raw.startsWith('//')) return;
                  const path = raw.split('?')[0].split('#')[0];
                  const dest = target(path);
                  // Everything this build does not cover - exports, admin,
                  // auth, returns - goes to the running app rather than 404.
                  a.setAttribute('href', dest !== null ? dest : liveUrl + raw);
                });
                root.querySelectorAll('template').forEach((t) => fix(t.content));
              };
              fix(document);
            }""",
            {"routes": routes, "drills": drills, "allowed": allowed, "base": base,
             "liveUrl": self.live_url.rstrip("/")},
        )

    def drilldown_path(self, path: str) -> str | None:
        m = re.match(r"^/customers/drilldown/([^/]+)$", path)
        customer = urllib.parse.unquote(m.group(1)) if m else ""
        if customer in self.drilldown_ids.get("customers", []):
            return f"drilldowns/customers/{customer}.html"
        m = re.match(r"^/products/([^/]+)/drilldown$", path)
        product = urllib.parse.unquote(m.group(1)) if m else ""
        if product in self.drilldown_ids.get("products", []):
            return f"drilldowns/products/{product}.html"
        m = re.match(r"^/regions/(?:drilldown/)?([^/]+)$", path)
        region = slugify(urllib.parse.unquote(m.group(1))) if m else ""
        region_slugs = {slugify(value) for value in self.drilldown_ids.get("regions", [])}
        if region in region_slugs:
            return f"drilldowns/regions/{region}.html"
        m = re.match(r"^/suppliers/(?:drilldown/)?([^/]+)$", path)
        supplier = urllib.parse.unquote(m.group(1)) if m else ""
        if supplier in self.drilldown_ids.get("suppliers", []):
            return f"drilldowns/suppliers/{supplier}.html"
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

    def prepare_drilldown_ids(self, limit: int | None = None) -> None:
        for name, lister in (
            ("customers", self.list_customers),
            ("products", self.list_products),
            ("suppliers", self.list_suppliers),
            ("regions", self.list_regions),
        ):
            try:
                ids = lister()
            except Exception as exc:
                self.log(f"  ! could not list {name}: {exc}")
                ids = []
            if limit is not None:
                ids = ids[:limit]
            self.drilldown_ids[name] = ids

    def build_drilldowns(self, preset: str, limit: int | None = None) -> None:
        args = self.scope_args(preset)
        specs = [
            ("customers", "/customers/drilldown/{id}", self.list_customers, "drilldowns/customers/{id}.html"),
            ("products", "/products/{id}/drilldown", self.list_products, "drilldowns/products/{id}.html"),
            ("suppliers", "/suppliers/{id}", self.list_suppliers, "drilldowns/suppliers/{id}.html"),
            ("regions", "/regions/{id}", self.list_regions, "drilldowns/regions/{slug}.html"),
        ]
        for name, route_tpl, lister, out_tpl in specs:
            ids = self.drilldown_ids.get(name)
            if ids is None:
                try:
                    ids = lister()
                except Exception as exc:
                    self.log(f"  ! could not list {name}: {exc}")
                    continue
                if limit is not None:
                    ids = ids[:limit]
                self.drilldown_ids[name] = ids
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
        #
        # Replacing the graph with a picture also throws away the single most
        # useful thing a chart does: naming the bar under the pointer. So before
        # the swap, walk every drawn point, ask Plotly's own hover machinery for
        # the label it *would* have shown, and keep it - text and box both - as
        # a percentage-positioned overlay. Percentages, because the image is
        # responsive and pixel hotspots would drift off their bars the moment
        # the viewport is not the one this build ran at.
        plots = page.locator(".js-plotly-plot")
        for index in reversed(range(plots.count())):
            plot = plots.nth(index)
            try:
                result = plot.evaluate(PLOTLY_FREEZE_JS)
                svg = self._decode_svg_data_url(result["url"])
                rel = self._write_chart_asset(svg)
                plot.evaluate(
                    CHART_MOUNT_JS,
                    {**result, "src": f"{base}{rel}"},
                )
                self.stats["hotspots"] += len(result.get("hotspots") or ())
                frozen += 1
            except Exception as exc:
                self.log(f"    ! plotly chart {index}: {type(exc).__name__}: {exc}")
                raise

        # Chart.js is canvas-only. Preserve its exact pixels inside a standalone
        # SVG wrapper so the final document still paints a chart without running
        # Chart.js (and without placing a large data URL in the HTML itself).
        # Hidden responsive/alternate-view canvases are intentionally blank and
        # are not part of the published view. Freeze only drawing surfaces a
        # visitor can actually see; the readiness gate uses the same contract.
        canvases = page.locator("canvas:visible")
        for index in reversed(range(canvases.count())):
            canvas = canvases.nth(index)
            try:
                result = canvas.evaluate(CANVAS_FREEZE_JS)
                if not result.get("painted"):
                    raise RuntimeError("canvas export contains no visible chart pixels")
                svg = (
                    '<svg xmlns="http://www.w3.org/2000/svg" role="img" '
                    f'aria-label="{_xml_escape(result["alt"])}" viewBox="0 0 {result["width"]} {result["height"]}">'
                    f'<image width="{result["width"]}" height="{result["height"]}" href="{result["png"]}"/>'
                    "</svg>"
                )
                rel = self._write_chart_asset(svg)
                canvas.evaluate(
                    CHART_MOUNT_JS,
                    {**result, "src": f"{base}{rel}"},
                )
                self.stats["hotspots"] += len(result.get("hotspots") or ())
                frozen += 1
            except Exception as exc:
                self.log(f"    ! canvas chart {index}: {type(exc).__name__}: {exc}")
                raise
        return frozen

    def _settle_map(self, page: Any) -> None:
        """Wait for the Live Account Map to stop drawing before it is captured.

        MapLibre paints asynchronously - style, then state outlines, then account
        layers - and `toDataURL` on a WebGL canvas returns whatever the drawing
        buffer holds at that instant. Freezing on the chart timings alone
        captured a grey rectangle.

        The page marks the host after the first complete frame containing its
        account layers. It cannot rely on MapLibre's global `idle` event because
        the pulsing risk halo intentionally keeps requesting repaints.
        """
        try:
            if not page.locator("#srLiveMap canvas").count():
                return
        except Exception:
            return
        try:
            page.wait_for_function(
                """() => {
                  const el = document.getElementById('srLiveMap');
                  if (!el) return true;
                  // The page raises this after a rendered account-layer frame.
                  return el.dataset.waMapIdle === '1';
                }""",
                timeout=25_000,
                polling=400,
            )
        except Exception:
            self.log("    ! live map did not settle; capturing as drawn")
        # Let the initial fit-to-data animation finish before reading pixels.
        page.wait_for_timeout(900)

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
            # Wait for charts to contain pixels and real trace data, not merely
            # for their host elements to exist. Chart.js canvases are present in
            # the template before the bundle arrives; Plotly adds
            # `.js-plotly-plot` before its async drawing is complete. Counting
            # either one as "drawn" published transparent Overview and Sales
            # Reps images that still looked like successful chart assets.
            page.wait_for_timeout(150)
            try:
                page.wait_for_function(
                    """() => {
                      const visible = (el) => {
                        const style = getComputedStyle(el), rect = el.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden'
                          && rect.width > 30 && rect.height > 30;
                      };
                      const canvasPainted = (canvas) => {
                        try {
                          const sample = document.createElement('canvas');
                          sample.width = 48; sample.height = 32;
                          const ctx = sample.getContext('2d', {willReadFrequently: true});
                          ctx.drawImage(canvas, 0, 0, sample.width, sample.height);
                          const px = ctx.getImageData(0, 0, sample.width, sample.height).data;
                          let opaque = 0; const colours = new Set();
                          for (let i = 0; i < px.length; i += 4) {
                            if (px[i + 3] > 8) opaque += 1;
                            colours.add(`${px[i] >> 4}:${px[i + 1] >> 4}:${px[i + 2] >> 4}:${px[i + 3] >> 4}`);
                          }
                          return opaque > 8 && colours.size > 2;
                        } catch (_) { return false; }
                      };
                      const plotReady = (plot) => {
                        const traces = Array.isArray(plot._fullData) ? plot._fullData : [];
                        const hasData = traces.some((trace) => {
                          if (trace.visible === false || trace.visible === 'legendonly') return false;
                          return ['x','y','z','labels','values','r','theta'].some((key) => {
                            const value = trace[key];
                            return Array.isArray(value) && value.length > 0;
                          });
                        });
                        const marks = plot.querySelectorAll(
                          '.main-svg g.trace path,.main-svg g.trace rect,.main-svg g.trace circle,' +
                          '.main-svg g.trace text,.main-svg g.trace image,.main-svg .point,.main-svg .slice,' +
                          '.main-svg image,.main-svg .hm'
                        ).length;
                        return hasData && marks > 0;
                      };
                      const hasExplicitEmptyState = (host) => {
                        const parent = host.parentElement;
                        if (!parent) return false;
                        return [...parent.querySelectorAll(
                          '[data-chart-empty],.chart-empty-state,[id$="_empty"],[id$="EmptyState"]'
                        )].some((empty) => visible(empty) && (empty.textContent || '').trim().length > 0);
                      };

                      const canvases = [...document.querySelectorAll('canvas')]
                        .filter(visible).filter((canvas) => !canvas.closest('#srLiveMap'));
                      const plots = [...document.querySelectorAll('.js-plotly-plot')].filter(visible);
                      const hosts = [...document.querySelectorAll('[id$="Chart"],[id$="chart"]')]
                        .filter(visible).filter((host) => host.tagName !== 'CANVAS');
                      const pending = [];

                      canvases.forEach((canvas) => {
                        if (hasExplicitEmptyState(canvas)) return;
                        const chart = window.Chart && window.Chart.getChart ? window.Chart.getChart(canvas) : null;
                        const hasChartData = !chart || (chart.data.datasets || []).some((set) =>
                          Array.isArray(set.data) && set.data.length > 0
                        );
                        if (!hasChartData || !canvasPainted(canvas)) pending.push(`#${canvas.id || 'canvas'}`);
                      });
                      plots.forEach((plot) => {
                        if (hasExplicitEmptyState(plot)) return;
                        if (!plotReady(plot)) pending.push(`#${plot.id || 'plot'}`);
                      });
                      hosts.forEach((host) => {
                        if (hasExplicitEmptyState(host)) return;
                        if (host.matches('.js-plotly-plot') || host.querySelector('canvas,.js-plotly-plot')) return;
                        const loading = host.querySelector('.spinner-border,.skeleton,[class*="loading"]');
                        if (!loading && host.children.length) return; // intentional HTML chart
                        pending.push(`#${host.id}`);
                      });

                      window.__waChartPending = [...new Set(pending)];
                      if (window.__waChartPending.length) {
                        window.__waChartReadySignature = ''; window.__waChartReadyHits = 0;
                        return false;
                      }
                      const signature = `${canvases.length}:${plots.length}:${hosts.length}`;
                      if (window.__waChartReadySignature !== signature) {
                        window.__waChartReadySignature = signature; window.__waChartReadyHits = 1;
                        return false;
                      }
                      window.__waChartReadyHits = (window.__waChartReadyHits || 0) + 1;
                      return window.__waChartReadyHits >= 3;
                    }""",
                    timeout=45_000,
                    polling=400,
                )
            except Exception as exc:
                pending = page.evaluate("window.__waChartPending || []")
                if pending:
                    raise RuntimeError(f"visible charts never rendered: {pending}") from exc
            page.wait_for_timeout(150)
            misses = page.evaluate("window.__staticMisses || []")
            if misses:
                raise RuntimeError(f"uncaptured same-origin requests: {sorted(set(misses))}")

            self._settle_map(page)
            charts = self._freeze_charts(page, base)

            section_rules = DRILLDOWN_SECTION_RULES if rel.startswith("drilldowns/") else SECTION_RULES
            selector, keep = section_rules.get(page_key, ("", 0))
            if selector:
                page.evaluate(
                    """({selector, keep}) => {
                      const sections = [...document.querySelectorAll(selector)].filter(el => el.isConnected);
                      const kept = new Set(Array.isArray(keep)
                        ? keep
                        : sections.map((_, index) => index).slice(0, keep));
                      let secondary = sections.filter((_, index) => !kept.has(index));
                      if (!secondary.length) return;

                      // Several pages write a section heading as its own
                      // <section>, immediately before the section it titles:
                      //
                      //   <section class="sr-section-heading"><h3>Trend</h3></section>
                      //   <section class="row">...the charts...</section>
                      //
                      // Tabbed naively that produces two tabs - one holding a
                      // heading and nothing else, and one labelled "Section 7"
                      // because the content carries no heading of its own.
                      // Sales Reps published seventeen tabs that way, half of
                      // them empty and most of the rest unnamed.
                      //
                      // Fold a heading-only section into the one it introduces,
                      // so the pair becomes a single, correctly named tab.
                      // One tab per heading, not one per <section>. A heading
                      // starts a group and every following heading-less
                      // section joins it, because these layouts routinely put
                      // two or three sibling rows under a single title.
                      const isHeadingOnly = (el) => {
                        if (!el.querySelector('h1,h2,h3,h4,h5')) return false;
                        if (el.querySelector('table,canvas,img,ul,ol,form,input,select')) return false;
                        return el.textContent.trim().length < 240;
                      };
                      const hasHeading = (el) => !!el.querySelector('h1,h2,h3,h4,h5');
                      const groups = [];
                      secondary.forEach((section) => {
                        const previous = groups[groups.length - 1];
                        const startsGroup = !groups.length || hasHeading(section);
                        if (startsGroup) groups.push([section]);
                        else previous.push(section);
                      });
                      // Collapse each group into its first element so the tab
                      // holds the heading and everything it introduces.
                      secondary = groups.map((group) => {
                        const host = group[0];
                        for (let i = 1; i < group.length; i += 1) host.append(...group[i].childNodes), group[i].remove();
                        return host;
                      }).filter((el) => el.textContent.trim().length || el.querySelector('img,canvas,table'));
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
                        // Own heading first; then the heading of the section
                        // immediately before it, for the layouts that title a
                        // block from outside it. `Section N` is the last
                        // resort, and it should now be rare.
                        // Own heading first; then the heading of the section
                        // immediately before it, for the layouts that title a
                        // block from outside it.
                        //
                        // The previous sibling must not be the tab strip: it is
                        // inserted before the first secondary section and
                        // carries its own <h2>Detailed analysis</h2>, so an
                        // unguarded lookup names six tabs after the container
                        // holding them.
                        const prev = section.previousElementSibling;
                        const prevHeading = prev && !prev.classList.contains('static-detail-tabs')
                          ? prev.querySelector('h2,h3,h4,h5') : null;
                        const heading = section.querySelector('h2,h3,h4,h5') || prevHeading;
                        const label = (heading?.textContent || `Section ${index + 1}`)
                          .replace(/\\s+/g, ' ').trim().slice(0, 60);
                        template.content.append(section);
                        tabs.append(template);
                        const button = document.createElement('button');
                        button.type = 'button'; button.role = 'tab';
                        button.dataset.staticTab = template.id;
                        button.dataset.staticTargets = [section, ...section.querySelectorAll('[id]')]
                          .map((node) => node.id).filter(Boolean).join(' ');
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
                      /* Same waste as `data-initial`, and the reason Sales Reps
                         looked too heavy to show in full: the rep table stamps
                         a JSON drilldown descriptor onto five cells of every
                         row, every quote escaped to `&quot;`. One row measured
                         12.6 KB and the attribute totalled 304.9 KB of a
                         542.4 KB page. It feeds the live app's click handler,
                         and every script is stripped a few steps below, so on a
                         static page these are bytes nothing can read. */
                      el.removeAttribute('data-drilldown-payload');
                      el.removeAttribute('data-drilldown-bound');
                      /* The payload is gone, so the affordances it installed
                         are now promises the page cannot keep. `universal_
                         drilldown.js` marks each drillable element with a
                         hover style, a button role, a tab stop and the title
                         "Click to drill into this detail" - and Sales Reps
                         alone published 193 of them. A reviewer hovers a card
                         that says it drills, clicks, nothing happens, and
                         reasonably concludes the page is broken. Take the
                         invitation off along with the payload. */
                      if (el.classList.contains('is-drillable')) {
                        el.classList.remove('is-drillable');
                        if (el.getAttribute('role') === 'button' && !/^(A|BUTTON)$/.test(el.tagName)) {
                          el.removeAttribute('role');
                          if (el.getAttribute('tabindex') === '0') el.removeAttribute('tabindex');
                        }
                        if (el.getAttribute('title') === 'Click to drill into this detail') {
                          el.removeAttribute('title');
                        }
                      }
                      [...el.attributes].forEach(attr => {
                        if (String(attr.value || '').includes('/api/')) el.removeAttribute(attr.name);
                      });
                      if (el.tagName === 'TEMPLATE') scrub(el.content);
                    });
                  };
                  scrub(document);
                }"""
            )
            self._rewrite_live_links(page, base)
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
                  /* The static transform deliberately strips dead filter UI
                     before browser parsing. Guard against any malformed legacy
                     wrapper leaving its form orphaned outside GlobalFilters: a
                     preset-only CDN page must never devote a screen to a form
                     it cannot submit. */
                  document.querySelectorAll(
                    '#filtersBody,#filtersForm,.filters-form,form[id$="_proxy_form"]'
                  ).forEach(el => el.remove());
                  document.getElementById('savedViewsSection')?.remove();
                  document.querySelectorAll('[data-live-only]').forEach(el => el.remove());
                  document.querySelectorAll('#js-plotly-tester,.plotly-notifier').forEach(el => el.remove());
                  /* The live map has seven WebGL modes, zoom buttons and a
                     click-to-filter reset. The public build keeps an exact
                     account-map snapshot, but after the canvas is frozen none
                     of those controls can change it. Remove the dead controls
                     and describe the snapshot it actually publishes. */
                  const accountMap = document.getElementById('srMapSection');
                  if (accountMap) {
                    accountMap.querySelectorAll('[data-live-map-controls],.maplibregl-control-container').forEach(el => el.remove());
                    const copy = accountMap.querySelector('[data-live-map-copy]');
                    if (copy) copy.textContent = 'Accounts in the active scope, sized by revenue and placed by delivery address when available or by territory centroid. Open the live app for map modes and click-to-filter.';
                    accountMap.querySelectorAll('.sr-map-legend-rep').forEach(el => {
                      el.classList.remove('sr-map-legend-rep');
                      el.removeAttribute('role');
                      el.removeAttribute('tabindex');
                      el.removeAttribute('aria-label');
                    });
                  }
                  /* The Overview snapshot keeps the chosen result, but its
                     live API controls cannot recompute after scripts and
                     payloads are removed. Leave no dead affordances: exports
                     become a working Print/Save snapshot action and the
                     recompute-only controls say explicitly that this is the
                     frozen view. The preset selector above remains live. */
                  if (document.getElementById('overviewPage')) {
                    const printLabels = {
                      downloadSnapshotBtn: 'Print / Save Snapshot',
                      exportDataHealthBtn: 'Print Data Trust',
                      moversExportBtn: 'Print Movers',
                      driversExportBtn: 'Print Drivers',
                      concentrationExportBtn: 'Print Concentration',
                      marginRiskExportBtn: 'Print Margin Risk',
                      trendExportBtn: 'Print Trend'
                    };
                    Object.entries(printLabels).forEach(([id, label]) => {
                      const control = document.getElementById(id);
                      if (!control) return;
                      control.setAttribute('data-print-report', '1');
                      control.setAttribute('aria-label', label);
                      const text = control.querySelector('span') || control;
                      text.textContent = label;
                    });
                    document.querySelectorAll(
                      '#diagnosticWorkspacesDisclosure button:not([data-print-report]),' +
                      '#diagnosticWorkspacesDisclosure select,' +
                      '#diagnosticWorkspacesDisclosure input[type="checkbox"]'
                    ).forEach(control => {
                      control.disabled = true;
                      control.setAttribute('aria-disabled', 'true');
                      control.setAttribute('title', 'Prerendered snapshot control. Change Preset scope above or use the live app for custom analysis.');
                    });
                  }
                  /* Put the actual business story above the instructional
                     guide. The guide remains available at the end of main,
                     but cannot become a mobile LCP gate before the hero. */
                  const guide = document.querySelector('main.app-main > .page-guide');
                  if (guide) guide.parentElement.append(guide);
                  /* Bootstrap tooltips are the page's own "hover for the
                     definition" affordance, and they were silently dead here.
                     Initialising one *moves* the element's `title` into
                     `data-bs-original-title` so the browser stops drawing its
                     own tooltip - then the freeze pass removes the JavaScript
                     that was going to draw the replacement, and the element is
                     left with nothing to show. Hand the text to CSS instead.

                     Replaced elements are left with a real `title`: an <img> or
                     an <input> has no box to hang ::after on. */
                  const REPLACED = /^(IMG|INPUT|SELECT|TEXTAREA|AREA|CANVAS|OBJECT|IFRAME|EMBED|VIDEO|AUDIO|BR|HR)$/;
                  document.querySelectorAll(
                    '[data-bs-original-title],[data-bs-title],[data-bs-toggle="tooltip"],[title]'
                  ).forEach(el => {
                    const tip = (el.getAttribute('data-bs-original-title')
                      || el.getAttribute('data-bs-title')
                      || el.getAttribute('title') || '').trim();
                    if (el.getAttribute('data-bs-toggle') === 'tooltip') el.removeAttribute('data-bs-toggle');
                    ['data-bs-original-title', 'data-bs-title', 'data-bs-placement',
                     'data-bs-custom-class', 'data-bs-trigger'].forEach(attr => el.removeAttribute(attr));
                    if (!tip) { el.removeAttribute('title'); return; }
                    if (REPLACED.test(el.tagName)) { el.setAttribute('title', tip); return; }
                    el.removeAttribute('title');
                    el.setAttribute('data-wa-tip', tip);
                    if (!el.getAttribute('aria-label') && !(el.textContent || '').trim()) {
                      el.setAttribute('aria-label', tip);
                    }
                  });
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
                      option.textContent = item.label;
                      option.selected = item.selected;
                      option.defaultSelected = item.selected;
                      if (item.selected) option.setAttribute('selected', 'selected');
                      select.append(option);
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
                          return JSON.parse(document.getElementById('filter-options')?.textContent || '{}');
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
            # The freeze pass strips every script, including the one in <head>
            # that resolves the theme before first paint. Two consequences, both
            # of which shipped: a visitor who chose Light got Dark back on the
            # next page, because nothing here ever read localStorage; and the
            # theme a handoff carries in `?theme=` was ignored, so crossing to
            # the live app and back flipped the colours each way.
            #
            # Put it back after the strip, in <head>, so it still runs before
            # anything paints. It touches no network and no application state.
            html = html.replace("</head>", STATIC_THEME_BOOT + "</head>", 1)
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
            self.manifest.setdefault("rendered_pages", []).append(
                {
                    "path": rel,
                    "page": page_key,
                    "preset": preset,
                    "kb": round(len(html.encode("utf-8")) / 1024, 1),
                    "nodes": stats["nodes"],
                    "height": stats["height"],
                    "charts": charts,
                }
            )
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

        class QuietServer(ThreadingHTTPServer):
            def handle_error(self, _request: Any, _client_address: Any) -> None:
                # Chromium intentionally abandons low-priority asset reads when
                # a frozen page closes. They are harmless client disconnects,
                # not build failures, and should not bury the build summary in
                # socketserver tracebacks on Windows.
                return

        server = QuietServer(
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
                    failures: list[str] = []
                    for target in self.targets:
                        try:
                            result = self._freeze_one(browser, origin, target)
                        except Exception as exc:
                            failure = f"{target['rel']}: {exc}"
                            failures.append(failure)
                            self.log(f"  ! freeze {failure}")
                            continue
                        self.log(
                            f"  freeze {target['rel']:<33} {result['kb']:>7.1f} KB  "
                            f"{result['nodes']:>4} nodes  {result['height']:>5}px  {result['charts']} charts"
                        )
                    if failures:
                        details = "\n".join(f"  - {failure}" for failure in failures)
                        raise RuntimeError(
                            f"static freeze failed for {len(failures)} page(s):\n{details}"
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

        def fingerprint(source: Path, raw: bytes) -> str:
            digest = hashlib.sha256(raw).hexdigest()[:12]
            target = source.with_name(f"{source.stem}.{digest}{source.suffix}")
            if target != source and not target.exists():
                target.write_bytes(raw)
            return target.relative_to(self.out).as_posix()

        # Fingerprint dependencies before stylesheets. CSS has relative font
        # and image URLs, so its final bytes (and therefore its own digest) are
        # only knowable after those references point at their hashed files.
        for source in originals:
            if source.suffix.lower() == ".css":
                continue
            rel = source.relative_to(self.out).as_posix()
            # Chart files are born content-addressed in `_write_chart_asset`.
            # Hashing `0123abcd....svg` a second time only doubles a large
            # artifact directory and makes every HTML rewrite quadratic.
            if re.fullmatch(r"static/charts/[0-9a-f]{16}\.svg", rel):
                mapping[rel] = rel
                continue
            mapping[rel] = fingerprint(source, source.read_bytes())

        css_dependencies = [
            (old, new)
            for old, new in mapping.items()
            if not old.startswith("static/charts/")
        ]
        for source in (path for path in originals if path.suffix.lower() == ".css"):
            rel = source.relative_to(self.out).as_posix()
            parent = source.relative_to(self.out).parent.as_posix()
            text = source.read_text(encoding="utf-8")
            for old, new in sorted(css_dependencies, key=lambda item: len(item[0]), reverse=True):
                old_ref = posixpath.relpath(old, parent)
                new_ref = posixpath.relpath(new, parent)
                # A delimiter assertion matters here: `icon.woff` is a prefix
                # of `icon.woff2`. Without it the shorter mapping produced a
                # nonexistent `icon.<woff hash>.woff2` URL on every page.
                pattern = rf"{re.escape(old_ref)}(?:\?[^\"')\s]*)?(?=[\"')\s]|$)"
                text = re.sub(pattern, new_ref, text)
            mapping[rel] = fingerprint(source, text.encode("utf-8"))

        # One scan per document, rather than one regex scan per asset. A full
        # build has thousands of chart files and hundreds of documents; the
        # old nested loop did millions of whole-document regex passes.
        static_ref = re.compile(
            r"((?:\.\./)*)(static/[^\"'<>\s?#)]+)"
            r"(?:\?[^\"'<>\s]*)?(?=[\"'<>\s)]|$)"
        )

        def rewrite(text: str) -> str:
            def replace(match: re.Match[str]) -> str:
                replacement = mapping.get(match.group(2))
                if replacement is None:
                    return match.group(0)
                return f"{match.group(1)}{replacement}"

            return static_ref.sub(replace, text)

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

        if drilldowns:
            self.prepare_drilldown_ids(drilldown_limit)

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
        self.write_preset_fragments()
        # Fragments can contain images and other static references too. Write
        # them before the single fingerprint pass so preset-switched content
        # receives the same immutable asset URLs as full pages.
        self.fingerprint_assets()
        self.write_meta(presets)

        s = self.stats
        self.log(
            f"\ndone: {s['pages']} pages, {s['drilldowns']} drilldowns, "
            f"{s['api_hits']} payloads inlined ({s['api_misses']} endpoints not used), "
            f"{s['chart_assets']} chart assets, {s['hotspots']} hover labels, "
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
    # A static build is itself the cache-producing phase. Force writer mode
    # only when the command runs: test modules import this file to inspect its
    # contract, and import-time environment mutations contaminated later cache
    # tests in the same process.
    os.environ.setdefault("DEMO_PREBUILT_CACHE_DIR", str(ROOT / "cache" / "demo-prebuilt"))
    os.environ["DEMO_PREBUILT_CACHE_READ"] = "1"
    os.environ["DEMO_PREBUILT_CACHE_WRITE"] = "1"

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

/*
 * House rules for numbers, in the browser.
 *
 * The mirror of app/services/formatting.py. Both exist because roughly half
 * the figures on this app are rendered by Jinja and half by fetch-and-render
 * JavaScript, and the demo shipped with the two disagreeing: the same revenue
 * total appeared as $7,192,185.4 on the Overview (client-side, one decimal),
 * $7,192,185.41 on Customers (client-side, two) and $7,192,185 on Regions
 * (server-side, none).
 *
 *   currency >= 10,000   whole dollars with separators   $7,192,185
 *   currency <  10,000   two decimals                    $3,846.13
 *   negative currency    minus outside the symbol        -$79.48
 *   percent              one decimal, always             45.0%
 *   counts               separators, no decimals         21,325
 *   missing              an em dash                      —
 *
 * Loaded in <head> from base.html, before any page script, and exposed as
 * window.WAFormat. tests/test_number_formatting.py checks these rules against
 * the Python side so the two cannot drift.
 */
(function () {
  "use strict";

  var MISSING = "—";
  var WHOLE_DOLLAR_THRESHOLD = 10000;

  var groupers = {
    0: new Intl.NumberFormat("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 }),
    1: new Intl.NumberFormat("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 }),
    2: new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  };

  function group(value, decimals) {
    var fmt = groupers[decimals];
    if (!fmt) {
      fmt = new Intl.NumberFormat("en-US", {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals
      });
      groupers[decimals] = fmt;
    }
    return fmt.format(value);
  }

  /* Anything that is not a finite number is missing, including the strings
     "NaN" and "None" that reach the client when a bundle serialises a null. */
  function coerce(value) {
    if (value === null || value === undefined || typeof value === "boolean") return null;
    if (typeof value === "string") {
      var text = value.trim().replace(/[$,]/g, "");
      if (!text || /^(nan|none|null|n\/a|-|—)$/i.test(text)) return null;
      var parsed = Number(text);
      return Number.isFinite(parsed) ? parsed : null;
    }
    var num = Number(value);
    return Number.isFinite(num) ? num : null;
  }

  /* The minus sign goes outside the symbol. Intl gets this right; string
     concatenation of "$" + value does not, which is where `$-79.48` came
     from on the Customers page. */
  function currency(value, options) {
    var opts = options || {};
    var num = coerce(value);
    if (num === null) return opts.missing || MISSING;
    var magnitude = Math.abs(num);
    var decimals;
    if (opts.forceDecimals === true) decimals = 2;
    else if (opts.forceDecimals === false) decimals = 0;
    else decimals = magnitude >= WHOLE_DOLLAR_THRESHOLD ? 0 : 2;
    return (num < 0 ? "-" : "") + "$" + group(magnitude, decimals);
  }

  function currencySigned(value, options) {
    var opts = options || {};
    var num = coerce(value);
    if (num === null) return opts.missing || MISSING;
    return (num < 0 ? "-" : "+") + currency(Math.abs(num), opts);
  }

  function percent(value, options) {
    var opts = options || {};
    var num = coerce(value);
    if (num === null) return opts.missing || MISSING;
    var decimals = opts.decimals === undefined ? 1 : opts.decimals;
    return group(num, decimals) + "%";
  }

  function percentSigned(value, options) {
    var opts = options || {};
    var num = coerce(value);
    if (num === null) return opts.missing || MISSING;
    var decimals = opts.decimals === undefined ? 1 : opts.decimals;
    return (num > 0 ? "+" : num < 0 ? "-" : "") + group(Math.abs(num), decimals) + "%";
  }

  function points(value, options) {
    var opts = options || {};
    var num = coerce(value);
    if (num === null) return opts.missing || MISSING;
    var decimals = opts.decimals === undefined ? 1 : opts.decimals;
    return (num > 0 ? "+" : num < 0 ? "-" : "") + group(Math.abs(num), decimals) + " pts";
  }

  function count(value, options) {
    var opts = options || {};
    var num = coerce(value);
    if (num === null) return opts.missing || MISSING;
    return group(Math.round(num), 0);
  }

  function decimal(value, options) {
    var opts = options || {};
    var num = coerce(value);
    if (num === null) return opts.missing || MISSING;
    return group(num, opts.decimals === undefined ? 1 : opts.decimals);
  }

  /* Axis ticks and narrow cards only. Never a KPI a reader might reconcile
     against another page: the rounding differs from currency() by design. */
  function compactCurrency(value, options) {
    var opts = options || {};
    var num = coerce(value);
    if (num === null) return opts.missing || MISSING;
    var magnitude = Math.abs(num);
    var sign = num < 0 ? "-" : "";
    var steps = [[1e9, "B"], [1e6, "M"], [1e3, "K"]];
    for (var i = 0; i < steps.length; i += 1) {
      if (magnitude >= steps[i][0]) {
        return sign + "$" + group(magnitude / steps[i][0], 1) + steps[i][1];
      }
    }
    return currency(num, opts);
  }

  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  /* `Oct 1, 2025`. Parsed as a plain date, not through the Date constructor's
     timezone handling: `new Date("2025-10-01")` is UTC midnight and renders as
     Sep 30 anywhere west of Greenwich, which is one of the two reasons the
     demo's end date differed by a day between pages. */
  function day(value, options) {
    var opts = options || {};
    if (value === null || value === undefined || value === "") return opts.missing || MISSING;
    var text = String(value).trim();
    var match = text.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (match) {
      return MONTHS[Number(match[2]) - 1] + " " + Number(match[3]) + ", " + match[1];
    }
    var parsed = new Date(text);
    if (Number.isNaN(parsed.getTime())) return opts.missing || MISSING;
    return MONTHS[parsed.getMonth()] + " " + parsed.getDate() + ", " + parsed.getFullYear();
  }

  function dayRange(start, end, options) {
    var opts = options || {};
    var left = day(start, { missing: "" });
    var right = day(end, { missing: "" });
    if (!left && !right) return opts.missing || MISSING;
    if (!right) return left;
    if (!left) return right;
    if (left === right) return left;
    return left + " – " + right;
  }

  window.WAFormat = {
    MISSING: MISSING,
    WHOLE_DOLLAR_THRESHOLD: WHOLE_DOLLAR_THRESHOLD,
    coerce: coerce,
    currency: currency,
    currencySigned: currencySigned,
    compactCurrency: compactCurrency,
    percent: percent,
    percentSigned: percentSigned,
    points: points,
    count: count,
    decimal: decimal,
    day: day,
    dayRange: dayRange
  };
})();

"""
The house rules for printing numbers, and the check that both sides agree.

Roughly half this app's figures are rendered by Jinja and half by fetch-and-
render JavaScript, so there are two implementations. The demo shipped with them
disagreeing: one revenue total appeared as $7,192,185.4 on the Overview,
$7,192,185.41 on Customers and $7,192,185 on Regions.

`test_javascript_matches_python` runs the shipped JS through node against the
same cases as the Python side, so the two cannot drift apart again. It skips
when node is unavailable rather than failing, since the Python rules are still
worth checking on a machine without it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from app.services import formatting


JS_PATH = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "format.js"


class TestCurrency:
    def test_large_values_lose_the_cents(self):
        """`$7,192,185.4` was the headline KPI, and it clipped out of its card."""
        assert formatting.currency(7_192_185.41) == "$7,192,185"
        assert formatting.currency(1_520_090.14) == "$1,520,090"

    def test_small_values_keep_two_decimals(self):
        assert formatting.currency(3_846.13) == "$3,846.13"
        assert formatting.currency(0) == "$0.00"

    def test_the_threshold(self):
        assert formatting.currency(9_999.99) == "$9,999.99"
        assert formatting.currency(10_000) == "$10,000"

    def test_negatives_put_the_minus_outside_the_symbol(self):
        """The demo rendered this as `$-79.48`."""
        assert formatting.currency(-79.48) == "-$79.48"
        assert formatting.currency(-39_104.3) == "-$39,104"

    def test_never_prints_a_bare_dollar_minus(self):
        for value in (-1, -0.5, -1e6, -12345.6):
            assert "$-" not in formatting.currency(value)

    def test_missing_is_a_dash_not_a_zero(self):
        assert formatting.currency(None) == "—"
        assert formatting.currency(float("nan")) == "—"
        assert formatting.currency("") == "—"

    def test_signed_currency_carries_its_sign(self):
        assert formatting.currency_signed(1_234.5) == "+$1,234.50"
        assert formatting.currency_signed(-79.48) == "-$79.48"


class TestPercent:
    def test_always_one_decimal(self):
        """The suppliers ROI column mixed `45%` with `19.3%`."""
        assert formatting.percent(45) == "45.0%"
        assert formatting.percent(19.34) == "19.3%"

    def test_signed(self):
        assert formatting.percent_signed(12.34) == "+12.3%"
        assert formatting.percent_signed(-8) == "-8.0%"

    def test_points_are_not_percent(self):
        assert formatting.points(2.5) == "+2.5 pts"


class TestCounts:
    def test_separators_and_no_decimals(self):
        """`Paid hours 21325.0` on the Labor page."""
        assert formatting.count(21325.0) == "21,325"
        assert formatting.count(1414) == "1,414"

    def test_missing(self):
        assert formatting.count(None) == "—"


class TestDates:
    def test_human_readable(self):
        """
        The demo printed `2025-10-01T00:00:00 → 2026-08-09T00:00:00` on the
        Overview and a key/value scope dump in the Products hero panel.
        """
        assert formatting.day(date(2025, 10, 1)) == "Oct 1, 2025"
        assert formatting.day("2026-08-09") == "Aug 9, 2026"
        assert formatting.day("2025-10-01T00:00:00") == "Oct 1, 2025"

    def test_ranges(self):
        assert formatting.day_range("2025-10-01", "2026-08-09") == "Oct 1, 2025 – Aug 9, 2026"

    def test_no_iso_leaks(self):
        assert "T00:00:00" not in formatting.day("2025-10-01T00:00:00")


class TestNothingLeaksRawFloats:
    @pytest.mark.parametrize(
        "fn",
        [
            formatting.currency,
            formatting.percent,
            formatting.count,
            formatting.decimal,
            formatting.compact_currency,
        ],
    )
    @pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), "nan", "None", "", "n/a"])
    def test_bad_input_never_reaches_the_page(self, fn, bad):
        rendered = fn(bad)
        lowered = rendered.lower()
        assert "nan" not in lowered
        assert "none" not in lowered
        assert "inf" not in lowered
        assert "undefined" not in lowered


# ─────────────────────────────────────────────────────────────────────────────
# The two implementations must agree
# ─────────────────────────────────────────────────────────────────────────────
PARITY_CASES = [
    ("currency", 7_192_185.41),
    ("currency", 3_846.13),
    ("currency", -79.48),
    ("currency", -39_104.3),
    ("currency", 0),
    ("currency", 9_999.99),
    ("currency", 10_000),
    ("currencySigned", 1_234.5),
    ("currencySigned", -79.48),
    ("percent", 45),
    ("percent", 19.34),
    ("percentSigned", 12.34),
    ("percentSigned", -8),
    ("points", 2.5),
    ("count", 21325.0),
    ("count", 1414),
    ("decimal", 1.95),
]

_PY_EQUIVALENT = {
    "currency": formatting.currency,
    "currencySigned": formatting.currency_signed,
    "percent": formatting.percent,
    "percentSigned": formatting.percent_signed,
    "points": formatting.points,
    "count": formatting.count,
    "decimal": formatting.decimal,
}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_javascript_matches_python():
    """
    Run the shipped format.js and compare it to formatting.py, case for case.

    This is the test that keeps the client and the server printing the same
    number the same way, which is the property the demo lost.
    """
    script = f"""
      global.window = {{}};
      require({json.dumps(str(JS_PATH))});
      const F = global.window.WAFormat;
      const cases = {json.dumps(PARITY_CASES)};
      console.log(JSON.stringify(cases.map(([fn, value]) => F[fn](value))));
    """
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, f"node failed: {result.stderr}"
    js_values = json.loads(result.stdout.strip())

    mismatches = []
    for (name, value), js in zip(PARITY_CASES, js_values):
        py = _PY_EQUIVALENT[name](value)
        if py != js:
            mismatches.append(f"  {name}({value}): python={py!r} js={js!r}")
    assert not mismatches, "format.js and formatting.py disagree:\n" + "\n".join(mismatches)

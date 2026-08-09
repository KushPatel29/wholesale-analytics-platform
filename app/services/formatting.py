"""
House rules for turning numbers into strings. One set, server and client.

The demo rendered the same figure three ways on three pages - $7,192,185.4,
$7,192,185.41 and $7,192,185 - and negative money as `$-79.48`. Individually
each is trivial; together they are the thing that makes a finance dashboard
read as an unfinished build, because a reader who notices that the pages cannot
agree on how to print a number starts wondering what else they disagree about.

The rules:

    currency >= 10,000     whole dollars with separators      $7,192,185
    currency <  10,000     two decimals                       $3,846.13
    negative currency      minus outside the symbol           -$79.48
    percent                one decimal, always                45.0%
    counts                 separators, no decimals            21,325
    missing                an em dash, never 0 and never NaN  —

`app/static/js/format.js` is the same rules for values formatted in the
browser, and tests/test_number_formatting.py checks the two agree.

Nothing here ever returns "NaN", "None", "nan" or "$-": a value that cannot be
formatted comes back as the placeholder, because a dashboard that prints
`undefined` has told the reader to stop trusting it.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional

MISSING = "—"

# Above this, cents are noise on a KPI card and cost horizontal space that the
# card does not have - see the clipped `$7,192,185.4` in the demo's headline.
CURRENCY_WHOLE_DOLLAR_THRESHOLD = 10_000.0


def _fixed(number: float, decimals: int) -> str:
    """
    Group and round half away from zero, matching Intl.NumberFormat.

    Python's own formatting does not: `f"{1.95:.1f}"` is `1.9`, because 1.95 is
    not representable in binary and the nearest double is fractionally below
    it, while `Intl.NumberFormat` rounds the decimal value and returns `2.0`.
    The two sides of this app therefore printed different digits for the same
    number - caught by the parity test in tests/test_number_formatting.py.

    Half away from zero is also what a reader of a financial figure expects.
    """
    try:
        quantum = Decimal(1).scaleb(-decimals)
        rounded = Decimal(repr(number)).quantize(quantum, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        rounded = Decimal(number)
    return f"{rounded:,.{decimals}f}"


def _coerce(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("$", "")
        if not text or text.lower() in {"nan", "none", "null", "n/a", "-", MISSING}:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def currency(value: Any, *, missing: str = MISSING, force_decimals: bool | None = None) -> str:
    """
    Money. Whole dollars once it is big enough for cents to be noise.

    The minus sign goes outside the symbol: `-$79.48`, not `$-79.48`. Python's
    own f-string formatting produces the second when you write f"${value:,.2f}"
    and value is negative, which is exactly how the demo shipped it.
    """
    number = _coerce(value)
    if number is None:
        return missing
    magnitude = abs(number)
    if force_decimals is True:
        decimals = 2
    elif force_decimals is False:
        decimals = 0
    else:
        decimals = 0 if magnitude >= CURRENCY_WHOLE_DOLLAR_THRESHOLD else 2
    sign = "-" if number < 0 else ""
    return f"{sign}${_fixed(magnitude, decimals)}"


def currency_signed(value: Any, *, missing: str = MISSING) -> str:
    """Money as a change: an explicit + so a delta reads as a delta."""
    number = _coerce(value)
    if number is None:
        return missing
    body = currency(abs(number), missing=missing)
    return f"{'-' if number < 0 else '+'}{body}"


def percent(value: Any, *, decimals: int = 1, missing: str = MISSING) -> str:
    """A percentage that is already on the 0-100 scale."""
    number = _coerce(value)
    if number is None:
        return missing
    return f"{_fixed(number, decimals)}%"


def percent_signed(value: Any, *, decimals: int = 1, missing: str = MISSING) -> str:
    number = _coerce(value)
    if number is None:
        return missing
    return f"{'+' if number > 0 else '-' if number < 0 else ''}{_fixed(abs(number), decimals)}%"


def points(value: Any, *, decimals: int = 1, missing: str = MISSING) -> str:
    """A movement in percentage points, which is not the same as a percent."""
    number = _coerce(value)
    if number is None:
        return missing
    return f"{'+' if number > 0 else '-' if number < 0 else ''}{_fixed(abs(number), decimals)} pts"


def count(value: Any, *, missing: str = MISSING) -> str:
    """A whole number of things. `21,325`, never `21325.0`."""
    number = _coerce(value)
    if number is None:
        return missing
    return _fixed(number, 0)


def decimal(value: Any, *, decimals: int = 1, missing: str = MISSING) -> str:
    number = _coerce(value)
    if number is None:
        return missing
    return _fixed(number, decimals)


def compact_currency(value: Any, *, missing: str = MISSING) -> str:
    """
    `$7.2M`. For axis ticks and narrow cards only.

    Never for a KPI value a reader might reconcile against another page - the
    rounding differs from `currency()` by design, and two figures that disagree
    in the last digit are worse than one that is long.
    """
    number = _coerce(value)
    if number is None:
        return missing
    magnitude = abs(number)
    sign = "-" if number < 0 else ""
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if magnitude >= divisor:
            return f"{sign}${_fixed(magnitude / divisor, 1)}{suffix}"
    return currency(number, missing=missing)


def day(value: Any, *, missing: str = MISSING) -> str:
    """
    `Oct 1, 2025`.

    Every user-facing date goes through here. The demo printed
    `2025-10-01T00:00:00 → 2026-08-09T00:00:00` on the Overview and a
    key/value scope dump in the Products hero panel; an ISO timestamp in front
    of a reader is a debug artefact that escaped.
    """
    if value is None:
        return missing
    parsed: Optional[date]
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return missing
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00").split("+")[0]).date()
        except ValueError:
            return missing
    return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"


def day_range(start: Any, end: Any, *, missing: str = MISSING) -> str:
    """`Oct 1, 2025 – Aug 9, 2026`, with an en dash."""
    left = day(start, missing="")
    right = day(end, missing="")
    if not left and not right:
        return missing
    if not right:
        return left
    if not left:
        return right
    if left == right:
        return left
    return f"{left} – {right}"


def register_jinja_filters(app: Any) -> None:
    """
    Install the house rules as Jinja filters.

    Named `money`, `pct`, `qty` rather than shadowing the older `currency` /
    `percent` / `intcomma` filters, which several templates still rely on for
    their two-decimal behaviour. New and corrected templates use these.
    """
    app.jinja_env.filters["money"] = currency
    app.jinja_env.filters["money_signed"] = currency_signed
    app.jinja_env.filters["money_compact"] = compact_currency
    app.jinja_env.filters["pct"] = percent
    app.jinja_env.filters["pct_signed"] = percent_signed
    app.jinja_env.filters["pts"] = points
    app.jinja_env.filters["qty"] = count
    app.jinja_env.filters["dec"] = decimal
    app.jinja_env.filters["day"] = day
    app.jinja_env.filters["day_range"] = day_range

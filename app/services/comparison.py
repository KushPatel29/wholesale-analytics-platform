"""
One definition of "the window" and "the window before it", for every page.

Before this module, four pages answered the same question four different ways
under an identical global filter (Current FY, starting 2025-10-01):

    Overview   said "prior fiscal year-to-date"     and used Oct 1 2024 - Jul 3 2025
    Products   said "the same number of days"       and used Nov 22 2024 - Sep 30 2025
    Labor      ignored the global filter entirely   and used Jan 5 2026 - Apr 4 2026
    Regions    said "same window last year"         and rendered blank

So revenue growth % differed depending on which page you were looking at, for
the same company on the same day. A reviewer who reads the year-over-year
column first - which is what an analyst does - finds that in about a minute.

Everything here is deliberately small. It computes dates, labels them, and
nothing else; the aggregation stays where it was. The point is that there is
exactly one place where the question "what are we comparing against?" is
answered, and every page imports it.

Two bases, and only two:

    prior_fiscal_ytd        the same fiscal offset in the prior fiscal year.
                            Used whenever the filter names a fiscal period,
                            because that is what "vs last year" means to
                            someone reading a fiscal-year dashboard.

    same_length_preceding   the window of equal length immediately before the
                            current one. Used for an arbitrary date range,
                            where a fiscal comparison has no meaning.

The active basis is carried on the result as a label and rendered on every
page, so the reader never has to guess which one they are looking at.
"""

from __future__ import annotations

import threading
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from app.services.filters import (
    FISCAL_YEAR_START_DAY,
    FISCAL_YEAR_START_MONTH,
)

# The two supported bases.
PRIOR_FISCAL_YTD = "prior_fiscal_ytd"
SAME_LENGTH_PRECEDING = "same_length_preceding"

# Filter presets that mean "a fiscal period", and therefore select the fiscal
# basis. Anything else is an arbitrary range.
_FISCAL_PRESETS = {
    "current_fy",
    "previous_fy",
    "current_fq",
    "previous_fq",
    "current_fm",
    "previous_fm",
    "fytd_comparison",
}


@dataclass(frozen=True)
class ComparisonWindow:
    """The current window, the window it is compared against, and why."""

    current_start: date
    current_end: date
    prior_start: date
    prior_end: date
    basis: str
    basis_label: str
    basis_short_label: str
    # The last date the dataset actually holds, and whether the requested
    # window had to be pulled back to reach it.
    data_through: Optional[date] = None
    clamped: bool = False
    # What the filter asked for before clamping, for anything that needs to
    # explain the difference.
    requested_end: Optional[date] = None

    @property
    def current_days(self) -> int:
        return max(1, (self.current_end - self.current_start).days + 1)

    @property
    def prior_days(self) -> int:
        return max(1, (self.prior_end - self.prior_start).days + 1)

    @property
    def current_end_exclusive(self) -> date:
        return self.current_end + timedelta(days=1)

    @property
    def prior_end_exclusive(self) -> date:
        return self.prior_end + timedelta(days=1)

    def as_dict(self) -> dict[str, Any]:
        """The shape templates and JSON bundles consume."""
        return {
            "current_start": self.current_start.isoformat(),
            "current_end": self.current_end.isoformat(),
            "prior_start": self.prior_start.isoformat(),
            "prior_end": self.prior_end.isoformat(),
            "current_days": self.current_days,
            "prior_days": self.prior_days,
            "basis": self.basis,
            "basis_label": self.basis_label,
            "basis_short_label": self.basis_short_label,
            "current_window_label": format_window(self.current_start, self.current_end),
            "prior_window_label": format_window(self.prior_start, self.prior_end),
            "data_through": self.data_through.isoformat() if self.data_through else None,
            "data_through_label": format_day(self.data_through) if self.data_through else None,
            "clamped": bool(self.clamped),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Display helpers. Kept here rather than in the formatting module because a
# window label is part of the window's meaning, not a presentation choice: the
# same string has to appear identically on every page for the claim "these
# pages agree" to be checkable.
# ─────────────────────────────────────────────────────────────────────────────
def format_day(value: date) -> str:
    """`Oct 1, 2025` - never an ISO timestamp in front of a reader."""
    return f"{value.strftime('%b')} {value.day}, {value.year}"


def format_window(start: date, end: date) -> str:
    if start == end:
        return format_day(start)
    return f"{format_day(start)} – {format_day(end)}"


def _to_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        text = str(value).strip()
        if not text:
            return None
        return datetime.fromisoformat(text.replace("Z", "+00:00").split("+")[0]).date()
    except Exception:
        return None


def _shift_years(value: date, years: int) -> date:
    year = value.year + years
    day = min(value.day, monthrange(year, value.month)[1])
    return date(year, value.month, day)


def fiscal_year_start_on_or_before(day: date) -> date:
    """The start of the fiscal year containing `day`."""
    year = day.year
    if (day.month, day.day) < (FISCAL_YEAR_START_MONTH, FISCAL_YEAR_START_DAY):
        year -= 1
    return date(year, FISCAL_YEAR_START_MONTH, FISCAL_YEAR_START_DAY)


# ─────────────────────────────────────────────────────────────────────────────
# Data cutoff
# ─────────────────────────────────────────────────────────────────────────────
_cutoff_lock = threading.Lock()
_cutoff_cache: dict[str, Any] = {"value": None, "version": None}


def data_cutoff() -> Optional[date]:
    """
    The last date the dataset actually holds.

    Read from the dataset manifest rather than by querying the fact table: this
    is called on every request that renders a window, and a `SELECT max(date)`
    over the parquet set is far too expensive for that. Cached against the
    manifest's dataset_version so a refresh invalidates it.
    """
    try:
        from app.services import watermark_store

        manifest = watermark_store.read_manifest()
    except Exception:
        return None
    if not isinstance(manifest, dict):
        return None

    version = str(manifest.get("dataset_version") or manifest.get("built_at") or "")
    with _cutoff_lock:
        if _cutoff_cache["version"] == version and _cutoff_cache["value"] is not None:
            return _cutoff_cache["value"]

    cutoff = _to_date(
        manifest.get("max_date")
        or manifest.get("max_dateexpected")
        or manifest.get("watermark")
    )
    with _cutoff_lock:
        _cutoff_cache["version"] = version
        _cutoff_cache["value"] = cutoff
    return cutoff


def reset_data_cutoff_cache() -> None:
    """Drop the memoised cutoff. Used by tests and by the refresh path."""
    with _cutoff_lock:
        _cutoff_cache["version"] = None
        _cutoff_cache["value"] = None


def effective_today(cutoff: Optional[date] = None) -> date:
    """
    The date the app should treat as "now".

    Anything that means "recent" - active in the last 30 days, the current
    fiscal period, freshness - has to be measured against the data, not against
    the wall clock. The demo's dataset stopped five weeks before the server's
    today, which is why the Customers page reported `Active (30d): 0` beside a
    KPI card reading `ACTIVE CUSTOMERS 80`: nothing is within 30 days of today
    when the data stops before that window opens.
    """
    resolved = cutoff if cutoff is not None else data_cutoff()
    today = datetime.now(timezone.utc).date()
    if resolved is None:
        return today
    return min(today, resolved)


# ─────────────────────────────────────────────────────────────────────────────
# The one entry point
# ─────────────────────────────────────────────────────────────────────────────
def resolve_comparison(
    filters: Any,
    *,
    cutoff: Optional[date] = None,
    clamp_to_data: bool = True,
    default_days: int = 365,
) -> ComparisonWindow:
    """
    Given the active filter, return the current window, the prior window and
    the label that says which basis was used.

    `filters` is anything carrying `start`, `end`, `preset` and `date_type`:
    a FilterParams, a LaborFilters, or a plain mapping. Pages differ in which
    envelope they hold, and requiring one type here would just push the
    conversion out to five call sites.
    """
    start = _to_date(_attr(filters, "start"))
    end = _to_date(_attr(filters, "end"))
    preset = str(_attr(filters, "preset") or "").strip().lower()

    resolved_cutoff = cutoff if cutoff is not None else data_cutoff()
    today = effective_today(resolved_cutoff)

    if start is None and end is None:
        end = today
        start = end - timedelta(days=max(1, default_days) - 1)
    elif start is None:
        start = date(end.year, end.month, 1)
    elif end is None:
        end = today
    if start > end:
        start, end = end, start

    requested_end = end

    # ------------------------------------------------------------------
    # Clamp the window to the data.
    #
    # The demo's filter ran to 2026-08-09 against a dataset that stopped on
    # 2026-07-03, so five weeks of every window held nothing. The visible
    # damage was not an empty page - it was a trend chart whose final bar
    # collapsed to near-zero and a margin line that nosedived to 0%, so the
    # business appeared to be imploding. That is a partial-period artefact
    # presented as a finding.
    clamped = False
    if clamp_to_data and resolved_cutoff is not None and end > resolved_cutoff:
        end = resolved_cutoff
        clamped = True
        if start > end:
            start = min(start, end)

    if _uses_fiscal_basis(preset, _attr(filters, "date_type")):
        prior_start, prior_end = _prior_fiscal_window(start, end)
        basis = PRIOR_FISCAL_YTD
        short = "vs prior fiscal YTD" if preset in {"current_fy", "fytd_comparison"} else "vs same period last year"
    else:
        prior_start, prior_end = _preceding_window(start, end)
        basis = SAME_LENGTH_PRECEDING
        short = "vs preceding period"

    return ComparisonWindow(
        current_start=start,
        current_end=end,
        prior_start=prior_start,
        prior_end=prior_end,
        basis=basis,
        basis_label=f"{short} ({format_window(prior_start, prior_end)})",
        basis_short_label=short,
        data_through=resolved_cutoff,
        clamped=clamped,
        requested_end=requested_end,
    )


def _attr(source: Any, name: str) -> Any:
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def _uses_fiscal_basis(preset: str, date_type: Any) -> bool:
    if preset in _FISCAL_PRESETS:
        return True
    return str(date_type or "").strip().lower() == "fiscal"


def _prior_fiscal_window(start: date, end: date) -> tuple[date, date]:
    """
    The same fiscal offset, one fiscal year earlier.

    Shifting both ends back a year preserves the *position in the fiscal year*
    rather than the day count, which is the comparison a fiscal-year dashboard
    is asserting. The two windows can differ by a day across a leap year; that
    is correct - Feb 29 belongs to one of them and not the other.
    """
    return _shift_years(start, -1), _shift_years(end, -1)


def _preceding_window(start: date, end: date) -> tuple[date, date]:
    """
    The equal-length window immediately before this one.

    With one alignment rule: a *month-to-date* window is compared against the
    same days of the prior month, not against the trailing days of it.

    Mar 1-15 against Feb 14-28 is arithmetically "the preceding 15 days" and
    analytically useless - it lines the first half of one month up against the
    second half of another, so every month-boundary effect (billing runs, the
    weekly shop, promo calendars) lands on one side of the comparison and not
    the other. Mar 1-15 against Feb 1-15 is what "same length, immediately
    preceding" is trying to express.
    """
    days = max(1, (end - start).days + 1)

    if start == _month_start(start) and end != _month_end(end):
        prior_month_start = _shift_months(start, -1)
        prior_end = min(_month_end(prior_month_start), prior_month_start + timedelta(days=days - 1))
        return prior_month_start, prior_end

    prior_end = start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=days - 1)
    return prior_start, prior_end


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _month_end(value: date) -> date:
    return date(value.year, value.month, monthrange(value.year, value.month)[1])


def _shift_months(value: date, months: int) -> date:
    index = (value.month - 1) + months
    year = value.year + (index // 12)
    month = (index % 12) + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def year_ago_window(start: date, end: date) -> tuple[date, date]:
    """
    The same calendar window one year earlier.

    Distinct from the prior window on purpose. Regions renders a
    period-over-period column and a year-over-year column side by side, and
    collapsing them means one of the two headings is lying about what is under
    it - which is the class of fault this module exists to remove, not commit.
    """
    return _shift_years(start, -1), _shift_years(end, -1)


# ─────────────────────────────────────────────────────────────────────────────
# Partial-period handling for trend charts
# ─────────────────────────────────────────────────────────────────────────────
def last_complete_month_end(cutoff: Optional[date] = None) -> Optional[date]:
    """
    The end of the last month the dataset covers in full.

    A trend chart whose final bucket is a partial month draws a collapse that
    is not there. On the suppliers page the last bar fell to near-zero and the
    margin line to ~0% for exactly this reason.
    """
    resolved = cutoff if cutoff is not None else data_cutoff()
    if resolved is None:
        return None
    if resolved.day == monthrange(resolved.year, resolved.month)[1]:
        return resolved
    first_of_month = resolved.replace(day=1)
    return first_of_month - timedelta(days=1)


def is_partial_month(day: date, cutoff: Optional[date] = None) -> bool:
    """Whether the month containing `day` extends past the data."""
    resolved = cutoff if cutoff is not None else data_cutoff()
    if resolved is None:
        return False
    month_end = date(day.year, day.month, monthrange(day.year, day.month)[1])
    return month_end > resolved

"""Every movement in the published narrative names the window it belongs to.

The Current FY page shipped this, in two consecutive sentences:

    Revenue up +$260,424 versus prior fiscal year-to-date (+3.2%).
    Across matched SKUs the movement of -$881,289 splits into Price -$9,875,
    Volume -$871,413, Mix -$0.

Two comparisons, opposite signs, and only the first one labelled. The
arithmetic was right - they are genuinely different windows, and the builder
already refuses to present the split as a decomposition of the headline - but a
reader has no way to see that, so it reads as the page contradicting itself.

Asserted against the frozen page rather than a live bundle, because the frozen
page is what a reviewer actually reads.
"""
from __future__ import annotations

import html
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DIST_NAME = os.environ.get("WA_DIST", "dist")

# A signed money figure as the narrative writes it: +$260,424 / -$881,289.
MONEY = re.compile(r"[-+]\$[\d,]+(?:\.\d+)?")

# Phrases that tie a number to a period.
WINDOW_WORDS = (
    "year-to-date", "basis", "versus", "compared with", "prior",
    "same period", "year over year", "month over month", "window",
)


# The narrative is a list with its own id. Scoping to it matters: flattening the
# whole page glues KPI tiles into pseudo-sentences ("Biggest win $456,999 Top
# Region gainer Southeast ...") that carry money and no verb, and a rule about
# prose would fail on four of them while the one real defect hid in the middle.
NARRATIVE_LIST = re.compile(
    r'<ul[^>]*id="execNarrativeList"[^>]*>([\s\S]*?)</ul>', re.I
)


def _plain(fragment: str) -> str:
    stripped = re.sub(r"<script[\s\S]*?</script>", " ", fragment)
    stripped = re.sub(r"<style[\s\S]*?</style>", " ", stripped)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", stripped))).strip()


def page_text(path: Path) -> str:
    """The executive narrative only, as plain text."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    found = NARRATIVE_LIST.search(raw)
    if not found:
        return ""
    items = re.findall(r"<li[^>]*>([\s\S]*?)</li>", found.group(1), re.I)
    return " ".join(_plain(item) for item in items)


def movement_sentences(text: str) -> list[str]:
    """Sentences that state a signed money movement."""
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if MONEY.search(sentence) and re.search(r"\b(revenue|movement|moved|splits? into|splitting into)\b",
                                                sentence, re.I):
            out.append(sentence.strip())
    return out


@pytest.fixture(scope="module")
def overview_text() -> str:
    index = ROOT / DIST_NAME / "index.html"
    if not index.is_file():
        pytest.skip(f"{DIST_NAME}/index.html not built")
    builder = ROOT / "build_static.py"
    if builder.is_file() and index.stat().st_mtime < builder.stat().st_mtime:
        pytest.skip(f"{DIST_NAME}/ predates build_static.py - rebuild it to check the narrative")
    text = page_text(index)
    if not text:
        pytest.skip("no execNarrativeList on the built page")
    return text


def assert_windows_are_named(text: str) -> None:
    """The rule, extracted so it can be exercised on a fixed sample too."""
    unlabelled = [
        s for s in movement_sentences(text)
        if not any(w in s.lower() for w in WINDOW_WORDS)
    ]
    assert not unlabelled, (
        "these sentences state a movement without saying which period it covers, "
        "which is how +$260,424 and -$881,289 came to sit side by side looking "
        f"like a contradiction: {unlabelled}"
    )


def test_the_published_narrative_names_every_window(overview_text):
    assert_windows_are_named(overview_text)


def test_the_rule_catches_the_sentence_that_shipped():
    """The regression itself, as a fixed sample.

    Without this the check above is only as good as whatever happens to be in
    dist/, and it would pass on a page that had never carried a narrative at
    all. This is the exact prose that shipped.
    """
    shipped = (
        "Revenue up +$260,424 versus prior fiscal year-to-date (+3.2%). "
        "Across matched SKUs the movement of -$881,289 splits into "
        "Price -$9,875, Volume -$871,413, Mix -$0."
    )
    with pytest.raises(AssertionError, match="without saying which period"):
        assert_windows_are_named(shipped)

    fixed = (
        "Revenue up +$260,424 versus prior fiscal year-to-date (+3.2%). "
        "On a month over month basis - a different window from the movement "
        "above - revenue moved -$881,289 across matched SKUs, splitting into "
        "Price -$9,875, Volume -$871,413, Mix -$0."
    )
    assert_windows_are_named(fixed)

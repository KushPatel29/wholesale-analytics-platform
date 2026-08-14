"""Guards for two things a reviewer notices in the first minute.

Both of these have regressed before, and neither is visible to any other test:
the vocabulary because it is prose, and the hover because it is built by the
freeze pass rather than by the app.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"

# CI publishes `dist/`. A working tree usually has a stale one beside half a
# dozen verification builds, so point these at whichever was actually just
# built: `WA_DIST=dist-check python -m pytest tests/test_demo_language_and_hover.py`
DIST_NAME = os.environ.get("WA_DIST", "dist")

# Source extensions that can reach a visitor.
SOURCES = ("*.py", "*.js", "*.html")

# Vocabulary from the wholesale-meat app this project was rebuilt from. A
# reviewer who finds it reads the whole thing as a partially renamed copy.
#
# Only *prose* is checked. `ProteinType` is the fact-table column and the name
# of every filter key and payload field built on it; renaming those is a schema
# migration and none of it reaches the screen.
BANNED_PROSE = re.compile(
    r"\b(carcass|TRSM|tworiversmeats|Two Rivers"
    r"|specialty meat|protein target|protein-aware|protein mix"
    r"|protein concentration|protein-dependent|protein-specific"
    r"|All species|species-specific|Mainstream BC|BC accounts|BC market"
    r"|Vancouver categories|Vancouver Market)\b",
    re.IGNORECASE,
)

# Vendored third-party code is not ours to rewrite.
SKIP_PARTS = {"vendor", "__pycache__", "node_modules"}


def _sources():
    for pattern in SOURCES:
        for path in APP.rglob(pattern):
            if SKIP_PARTS & set(path.parts):
                continue
            yield path


def test_no_wholesale_meat_vocabulary_in_visitor_facing_prose():
    offenders: list[str] = []
    for path in _sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in BANNED_PROSE.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(ROOT).as_posix()}:{line}: {match.group(0)!r}")
    assert not offenders, (
        "Pre-pivot wholesale-meat vocabulary is reachable by a visitor:\n  "
        + "\n  ".join(offenders[:20])
    )


def _built_pages(dist: Path):
    return [p for p in dist.rglob("*.html") if not p.as_posix().endswith("data/index.html")]


@pytest.mark.parametrize("dist_name", [DIST_NAME])
def test_frozen_charts_carry_hover_values(dist_name: str):
    """A frozen chart is a picture; the hover layer is what makes it readable.

    It has been silently absent before - the freeze pass replaces the chart with
    an <img> whether or not the hotspots were collected, so nothing else fails.
    """
    dist = ROOT / dist_name
    if not dist.is_dir():
        pytest.skip(f"{dist_name}/ not built")
    pages = _built_pages(dist)
    if not pages:
        pytest.skip("no built pages")

    charted = [p for p in pages if 'class="static-chart"' in p.read_text(encoding="utf-8")]
    assert charted, "no page in the build draws a chart at all"

    without_hover: list[str] = []
    for page in charted:
        html = page.read_text(encoding="utf-8")
        tips = re.findall(r'class="wa-hot"[^>]*data-wa-tip="([^"]*)"', html)
        # At least one label has to carry a number, or the layer is decorative.
        if not any(re.search(r"\d", tip) for tip in tips):
            without_hover.append(page.relative_to(dist).as_posix())
    assert not without_hover, (
        "pages draw charts but publish no hover values: " + ", ".join(without_hover[:10])
    )


@pytest.mark.parametrize("dist_name", [DIST_NAME])
def test_frozen_charts_have_distinct_accessible_names(dist_name: str):
    """Two charts sharing one name is two charts a screen reader cannot tell apart."""
    dist = ROOT / dist_name
    if not dist.is_dir():
        pytest.skip(f"{dist_name}/ not built")
    clashes: list[str] = []
    for page in _built_pages(dist):
        html = page.read_text(encoding="utf-8")
        alts = re.findall(r'<img[^>]*class="static-chart"[^>]*alt="([^"]*)"', html)
        named = [a for a in alts if a and a != "Analytics chart"]
        duplicated = {a for a in named if named.count(a) > 1}
        if duplicated:
            clashes.append(f"{page.relative_to(dist).as_posix()}: {sorted(duplicated)}")
    assert not clashes, "charts share an accessible name:\n  " + "\n  ".join(clashes[:10])


@pytest.mark.parametrize("dist_name", [DIST_NAME])
def test_frozen_charts_are_reachable_by_keyboard(dist_name: str):
    """One tab stop per chart, with the arrow-key contract stated in its label."""
    dist = ROOT / dist_name
    if not dist.is_dir():
        pytest.skip(f"{dist_name}/ not built")
    bad: list[str] = []
    for page in _built_pages(dist):
        html = page.read_text(encoding="utf-8")
        for wrap in re.findall(r'<span class="static-chart-wrap"[^>]*>', html):
            if 'tabindex="0"' not in wrap or 'role="group"' not in wrap or "aria-label=" not in wrap:
                bad.append(f"{page.relative_to(dist).as_posix()}: {wrap[:90]}")
    assert not bad, "chart groups are not keyboard reachable:\n  " + "\n  ".join(bad[:10])

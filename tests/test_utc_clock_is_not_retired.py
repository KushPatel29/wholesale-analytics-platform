"""Nothing reaches for a UTC clock that pandas and Python have both retired.

Two separate calls, both deprecated, both slated for removal:

  * `datetime.utcnow()` -- deprecated in Python 3.12, returns a naive datetime
    that merely happens to be UTC.
  * `pd.Timestamp.utcnow()` -- raises `Pandas4Warning` and will be removed.
    `data_loader.py` used it to compute the default month window, and
    requirements.txt pins `pandas>=2.3` with no upper bound, so the next major
    release would have broken every date filter the app applies by default.

This repo already has the right answer in three places -- `app/returns/models.py`,
`app/decision_ops/models.py` and `app/planning_scenario_models.py` each define
`utcnow()` as `datetime.now(timezone.utc)`. The ban below is on the *qualified*
calls, so those helpers and their callers stay legal and only the retired forms
fail.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".venv", "venv", "node_modules", "__pycache__", ".git", "build", "dist"}

#: The qualified spellings. A bare `utcnow()` is this repo's own helper.
BANNED = ("datetime.utcnow(", "Timestamp.utcnow(")


def _sources() -> list[Path]:
    here = Path(__file__).resolve()
    return [
        p for p in ROOT.rglob("*.py")
        if not SKIP_DIRS & set(p.relative_to(ROOT).parts)
        and p.resolve() != here  # this file names the calls in order to ban them
    ]


def _code_lines(text: str) -> list[str]:
    """Lines with their comments removed.

    A comment explaining why a call is banned is not a use of it: data_loader.py
    carries exactly such a comment above its replacement, and scanning raw text
    flagged the very file the fix landed in.
    """
    return [line.split("#", 1)[0] for line in text.splitlines()]


def test_the_sweep_reads_this_repository():
    """A glob that matched nothing would make the ban below vacuous."""
    found = _sources()
    names = {p.name for p in found}
    assert "data_loader.py" in names, "data_loader.py is not in the sweep"
    assert len(found) > 50, f"only {len(found)} sources found - the glob is wrong"


def test_the_comment_stripper_keeps_code_and_drops_comments():
    """The stripper is the only thing standing between this ban and a false pass."""
    lines = _code_lines("x = datetime.utcnow()  # trailing\n# datetime.utcnow()\ny = 1")
    assert any("datetime.utcnow(" in line for line in lines), "real call was stripped"
    assert sum("datetime.utcnow(" in line for line in lines) == 1, "comment was kept"


def test_no_module_calls_a_retired_utc_clock():
    offenders = []
    for path in _sources():
        lines = _code_lines(path.read_text(encoding="utf-8", errors="ignore"))
        for call in BANNED:
            if any(call in line for line in lines):
                offenders.append(f"{path.relative_to(ROOT)}: {call})")
    assert not offenders, (
        "these call a deprecated UTC clock:\n  " + "\n  ".join(offenders)
        + "\nUse datetime.now(timezone.utc), or pd.Timestamp.now(tz='UTC')."
    )


def test_the_repositorys_own_helper_is_the_correct_shape():
    """The ban is only safe because the alternative is already here and right."""
    helper = (ROOT / "app" / "returns" / "models.py").read_text(encoding="utf-8")
    assert "def utcnow()" in helper
    assert "datetime.now(timezone.utc)" in helper

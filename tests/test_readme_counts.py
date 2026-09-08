"""The README's headline counts have to be the repository's actual counts.

This repository publishes its scale in a table — test files, tests, blueprints,
services — and the portfolio site reads the test figure straight out of that row
to fill the project card. Nothing compared either number to the repository.

It drifted, exactly the way healthcare-claims-analytics drifted from 182 to 345
while three surfaces agreed with each other: the suite reached 1,406 while the
README, the site card and the published hero all still said 1,391. Agreement
between copies is not verification, and this repository was the only featured
one with no check tying its published count to a test run.

Collection runs in a subprocess so the answer does not depend on how the current
session was invoked, and a collection error is reported as itself rather than as
a count that merely came out short.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"

STRUCTURE_ROW = re.compile(
    r"\|\s*\*\*Structure\*\*\s*\|(?P<body>[^|]*)\|", re.I
)


def _structure_row() -> str:
    found = STRUCTURE_ROW.search(README.read_text(encoding="utf-8"))
    assert found, "README no longer has a **Structure** row to check"
    return found.group("body")


def _claimed(pattern: str, label: str) -> int:
    row = _structure_row()
    m = re.search(pattern, row, re.I)
    assert m, f"the Structure row no longer states {label}: {row.strip()!r}"
    return int(m.group(1).replace(",", ""))


def _collected() -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", "-o", "addopts=", str(ROOT / "tests")],
        capture_output=True, text=True, cwd=ROOT,
    )
    errors = re.search(r"(\d+) errors?\b", proc.stdout)
    assert not errors, (
        "collection did not complete — " + errors.group(0) + ", so the count "
        "below would be short. Fix the import error, not the README: "
        + proc.stdout[-1500:]
    )
    found = re.search(r"(\d+) tests? collected", proc.stdout)
    assert found, f"could not read a collected-test count: {proc.stdout[-1500:]}"
    return int(found.group(1))


def test_the_readme_test_count_is_what_pytest_collects():
    claimed = _claimed(r"([\d,]+)\s+tests\b", "a test count")
    actual = _collected()
    assert claimed == actual, (
        f"README says {claimed:,} tests, pytest collects {actual:,}. Update the "
        f"Structure row — and the portfolio site's card and hero total read this "
        f"number, so move those with it."
    )


def test_the_readme_test_file_count_is_real():
    """The same row states how many files hold those tests."""
    claimed = _claimed(r"(\d+)\s+test files\b", "a test-file count")
    actual = len(list((ROOT / "tests").glob("test_*.py")))
    assert claimed == actual, (
        f"README says {claimed} test files, tests/ holds {actual}"
    )

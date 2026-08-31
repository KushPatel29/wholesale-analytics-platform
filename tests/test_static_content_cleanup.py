"""Regression tests for semantic content preserved by the static freeze."""

from __future__ import annotations

import re

import pytest

from build_static import WAITING_LEAF_PATTERN

WAITING_LEAF_RE = re.compile(WAITING_LEAF_PATTERN, re.IGNORECASE)


@pytest.mark.parametrize(
    "copy",
    (
        "Loading",
        "Loading supplier intelligence...",
        "Loading product health…",
        "Retry filters",
        "Reading the active filtered range and filter scope.",
        "Building current supplier window...",
        "Summarizing the visible portfolio...",
        "Resolving active window...",
    ),
)
def test_static_cleanup_identifies_transient_placeholder_copy(copy: str):
    assert WAITING_LEAF_RE.match(copy)


@pytest.mark.parametrize(
    "copy",
    (
        "Building Blocks Northgate Select Value Bundle",
        "Building Materials",
        "Reading Glasses Northgate Select",
        "Loading Dock Equipment",
        "Summarizing financial performance",
        "Resolving customer disputes",
    ),
)
def test_static_cleanup_preserves_real_business_content(copy: str):
    assert not WAITING_LEAF_RE.match(copy)

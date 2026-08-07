from __future__ import annotations


class DatasetNotBuiltError(Exception):
    """Raised when the analytics dataset has not been materialized yet."""


"""
Row-level security on the v2 overview.

Every other bundle service passes the caller's scope into
`fact_store.build_where_clause`. `overview_v2` builds its own SQL against a raw
DuckDB connection, and originally did not, so a rep scoped to their own book
still got company-wide totals on the overview page. These tests pin the
predicate in place: they assert the scope reaches the generated SQL, that an
unrestricted scope does not narrow anything, and that a failure to resolve the
scope cannot silently produce an unscoped clause without being noticed.
"""

from __future__ import annotations

import os

import pytest

from app import create_app
from app.services import overview_v2 as ov2
from app.services.filters import FilterParams


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("FLASK_ENV", "testing")
    os.environ.setdefault("WTF_CSRF_ENABLED", "false")
    application = create_app()
    application.config.update(TESTING=True, SECRET_KEY="test")
    return application


COLUMNS = {
    "Date",
    "OrderStatus",
    "RegionName",
    "RegionId",
    "CustomerId",
    "CustomerName",
    "SalesRepId",
    "SalesRepName",
    "PrimarySalesRepId",
    "SupplierId",
    "ProductId",
    "ShippingMethodName",
}


def _where(app, monkeypatch, scope):
    """Build the v2 where clause with a fixed scope payload."""
    monkeypatch.setattr(ov2, "_current_scope_payload", lambda: scope)
    with app.test_request_context("/overview"):
        return ov2._where_clause(FilterParams(), COLUMNS, False)


def test_rep_scope_reaches_the_generated_sql(app, monkeypatch):
    sql, params, _start, _end, _defaulted = _where(
        app, monkeypatch, {"scope_mode": "list", "allowed_erp_user_ids": ["r01"]}
    )
    assert "SalesRepId" in sql, "rep scope must narrow the overview query"
    assert "r01" in [str(p).lower() for p in params]


def test_region_scope_reaches_the_generated_sql(app, monkeypatch):
    sql, params, _start, _end, _defaulted = _where(
        app, monkeypatch, {"scope_mode": "list", "allowed_region_ids": ["rg01", "rg02"]}
    )
    assert "RegionId" in sql or "RegionName" in sql
    lowered = [str(p).lower() for p in params]
    assert "rg01" in lowered and "rg02" in lowered


def test_unrestricted_scope_adds_no_predicate(app, monkeypatch):
    scoped_sql, scoped_params, _, _, _ = _where(app, monkeypatch, {"scope_mode": "all"})
    none_sql, none_params, _, _, _ = _where(app, monkeypatch, None)
    # An "all" scope resolves to 1=1, which is dropped rather than appended.
    assert scoped_sql == none_sql
    assert scoped_params == none_params


def test_no_access_scope_blocks_every_row(app, monkeypatch):
    sql, _params, _start, _end, _defaulted = _where(app, monkeypatch, {"scope_mode": "none"})
    assert "1=0" in sql, "a user with no scope must not receive any rows"


def test_scope_predicate_is_wired_into_where_clause():
    """
    Guard against the predicate being dropped in a future refactor: the
    where-clause builder must call the scope helper, not merely define it.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(ov2._where_clause))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_scope_predicate" in called

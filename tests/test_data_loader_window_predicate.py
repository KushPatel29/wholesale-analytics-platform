import data_loader


def test_compose_date_filter_half_open_and_incremental():
    effective_expr = "COALESCE(ol.DateExpected, o.DateExpected)"
    change_expr = "(SELECT MAX(v) FROM (VALUES (ol.UpdatedAt), (o.UpdatedAt)) AS vals(v))"
    predicate, parts = data_loader._compose_date_filter(
        effective_expr,
        change_expr,
        start="2020-01-01",
        end_plus_1="2020-02-01",
        min_updated_at="2020-01-15",
        include_null_effective=False,
    )
    expected = (
        " AND ("
        "(COALESCE(ol.DateExpected, o.DateExpected) >= :start AND COALESCE(ol.DateExpected, o.DateExpected) < :end_plus_1)"
        " OR "
        "((SELECT MAX(v) FROM (VALUES (ol.UpdatedAt), (o.UpdatedAt)) AS vals(v)) >= :min_updated_at)"
        ")"
    )
    assert predicate == expected
    assert parts == [
        "COALESCE(ol.DateExpected, o.DateExpected) >= :start AND COALESCE(ol.DateExpected, o.DateExpected) < :end_plus_1",
        "(SELECT MAX(v) FROM (VALUES (ol.UpdatedAt), (o.UpdatedAt)) AS vals(v)) >= :min_updated_at",
    ]


def test_compose_date_filter_includes_null_effective():
    effective_expr = "COALESCE(ol.DateExpected, o.DateExpected)"
    predicate, parts = data_loader._compose_date_filter(
        effective_expr,
        None,
        start="2018-01-01",
        end_plus_1="2018-02-01",
        min_updated_at=None,
        include_null_effective=True,
    )
    assert "IS NULL" in predicate
    assert predicate.startswith(" AND ((")
    assert predicate.endswith("))")
    assert parts == [f"(({effective_expr} >= :start AND {effective_expr} < :end_plus_1) OR {effective_expr} IS NULL)"]

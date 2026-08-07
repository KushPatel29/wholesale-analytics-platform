from pathlib import Path


def _drilldown_js() -> str:
    return Path("app/static/js/salesrep_drilldown.js").read_text(encoding="utf-8")


def test_row_links_preserve_rep_and_filters_context():
    js = _drilldown_js()
    assert 'params.set("salesrep_id", repId)' in js
    assert '/customers/drilldown/${encoded}' in js
    assert '/products/${encoded}/drilldown' in js


def test_export_links_use_filter_query_and_export_type():
    js = _drilldown_js()
    assert 'params.set("dataset", dataset)' in js
    assert 'params.set("export_type", dataset)' in js
    assert 'params.set("format", format)' in js

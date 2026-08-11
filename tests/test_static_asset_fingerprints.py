import json
from pathlib import Path

from build_static import Builder
from scripts.check_static_build import check


def test_fingerprints_css_dependencies_without_prefix_collisions(tmp_path: Path):
    static = tmp_path / "static" / "vendor" / "icons"
    fonts = static / "fonts"
    fonts.mkdir(parents=True)
    (fonts / "icons.woff").write_bytes(b"woff-one")
    (fonts / "icons.woff2").write_bytes(b"woff-two")
    (static / "icons.css").write_text(
        '@font-face{src:url("fonts/icons.woff2?v=2") format("woff2"),'
        'url("fonts/icons.woff") format("woff")}',
        encoding="utf-8",
    )
    (tmp_path / "index.html").write_text(
        '<link rel="preload" href="static/vendor/icons/fonts/icons.woff2?v=2">'
        '<link rel="stylesheet" href="static/vendor/icons/icons.css">',
        encoding="utf-8",
    )

    Builder(tmp_path, "https://example.test", verbose=False).fingerprint_assets()

    manifest = json.loads((tmp_path / "asset-manifest.json").read_text(encoding="utf-8"))
    woff = manifest["static/vendor/icons/fonts/icons.woff"]
    woff2 = manifest["static/vendor/icons/fonts/icons.woff2"]
    css = manifest["static/vendor/icons/icons.css"]
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    css_text = (tmp_path / css).read_text(encoding="utf-8")

    assert woff != woff2
    assert woff2 in html
    assert css in html
    assert Path(woff2).name in css_text
    assert Path(woff).name in css_text
    assert (tmp_path / woff).is_file()
    assert (tmp_path / woff2).is_file()
    assert (tmp_path / css).is_file()


def test_content_addressed_chart_assets_are_not_copied_twice(tmp_path: Path):
    charts = tmp_path / "static" / "charts"
    charts.mkdir(parents=True)
    chart = charts / "0123456789abcdef.svg"
    chart.write_text("<svg></svg>", encoding="utf-8")
    (tmp_path / "index.html").write_text(
        '<img src="static/charts/0123456789abcdef.svg">',
        encoding="utf-8",
    )

    Builder(tmp_path, "https://example.test", verbose=False).fingerprint_assets()

    manifest = json.loads((tmp_path / "asset-manifest.json").read_text(encoding="utf-8"))
    rel = "static/charts/0123456789abcdef.svg"
    assert manifest[rel] == rel
    assert list(charts.glob("*.svg")) == [chart]
    assert rel in (tmp_path / "index.html").read_text(encoding="utf-8")


def test_only_built_drilldowns_are_rewritten_to_static_files(tmp_path: Path):
    builder = Builder(tmp_path, "https://live.example", verbose=False)
    builder.presets = ["current_fy"]
    builder.drilldown_ids = {"products": ["P10000"]}
    html = (
        '<a href="/products/P10000/drilldown">Built</a>'
        '<a href="/products/GR-1026/drilldown">Legacy SKU</a>'
    )

    rewritten = builder.rewrite_links(html, "", "current_fy")

    assert 'href="drilldowns/products/P10000.html"' in rewritten
    assert 'href="https://live.example/products/GR-1026/drilldown"' in rewritten


def test_static_checker_rejects_a_missing_local_asset(tmp_path: Path, capsys):
    (tmp_path / "index.html").write_text(
        '<html data-static-page><head><link href="static/missing.css"></head>'
        '<body><script id="filter-options" type="application/json">{}</script></body></html>',
        encoding="utf-8",
    )

    assert check(tmp_path) == 1
    assert "missing local asset or page static/missing.css" in capsys.readouterr().out


def test_static_checker_requires_an_index_for_directory_links(tmp_path: Path, capsys):
    (tmp_path / "customers").mkdir()
    (tmp_path / "index.html").write_text(
        '<html data-static-page><body><a href="customers/">Customers</a>'
        '<script id="filter-options" type="application/json">{}</script></body></html>',
        encoding="utf-8",
    )

    assert check(tmp_path) == 1
    assert "missing local asset or page customers/" in capsys.readouterr().out


def test_static_checker_enforces_first_paint_budgets(tmp_path: Path, capsys):
    (tmp_path / "index.html").write_text(
        '<html data-static-page><body>'
        '<script id="filter-options" type="application/json">{}</script></body></html>',
        encoding="utf-8",
    )
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "pages": [
                    {"path": "index.html", "nodes": 1_500, "height": 4_000},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert check(tmp_path) == 1
    output = capsys.readouterr().out
    assert "1500 first-paint nodes" in output
    assert "4000px first-paint height" in output


def test_static_checker_crawls_drilldown_links_inside_fragments(tmp_path: Path, capsys):
    fragment = tmp_path / "data" / "sections" / "detail.html"
    fragment.parent.mkdir(parents=True)
    fragment.write_text(
        '<a href="../../drilldowns/customers/C404.html">Missing customer</a>',
        encoding="utf-8",
    )
    (tmp_path / "index.html").write_text(
        '<html data-static-page><body>'
        '<script id="filter-options" type="application/json">{}</script></body></html>',
        encoding="utf-8",
    )
    (tmp_path / "manifest.json").write_text(
        json.dumps({"pages": [], "drilldowns": {"customers": 1}}),
        encoding="utf-8",
    )

    assert check(tmp_path) == 1
    assert "missing fragment drilldown link" in capsys.readouterr().out

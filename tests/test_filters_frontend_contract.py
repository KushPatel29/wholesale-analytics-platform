import pytest


def test_filters_enhanced_frontend_contract(app):
    with app.test_client() as client:
        resp = client.get("/static/js/filters-enhanced.js")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "persistFilterState" in body
        assert "getPendingGlobalFilterState" in body
        assert "window.getGlobalFilterState" in body
        assert "const filters = getAppliedFilters();" in body
        assert "APPLY_ACK_TIMEOUT_MS" in body
        assert "dispatchGlobalFiltersApply" in body
        assert 'const BOOTSTRAP_OPTION_DIMENSIONS = ["statuses", "regions", "methods"];' in body
        assert 'const FISCAL_PRESETS = new Set([' in body
        assert '"current_fy"' in body
        assert '"previous_fy"' in body
        assert '"current_fq"' in body
        assert '"previous_fq"' in body
        assert '"current_fm"' in body
        assert '"previous_fm"' in body
        assert '"fytd_comparison"' in body
        assert "const getFiscalPeriods = (referenceDate = new Date()) => {" in body
        assert 'if (!state.applyInFlight && !state.applyAckTimer)' in body
        assert 'const readInlineSchemaPayload = () => {' in body
        assert 'const readInlineOptionsPayload = () => {' in body
        assert 'parseInlineJson("filter-options")' in body
        assert 'applySchemaPayload(inlineSchemaPayload);' in body
        assert 'filters.schema.bootstrap.inline-missing' in body
        assert "fetchOptions" not in body
        assert "hydrateOptions" not in body
        assert "refreshOptionsInBackground" not in body
        assert "/api/filters/options" not in body
        assert 'const OPTIONS_STORAGE_KEY = "wholesale.globalFilterOptions.v1";' in body
        assert 'const hydratePersistedOptions = ({ dimensions = [], syncFilters = false, source = "local-storage" } = {}) => {' in body
        assert 'else window.dispatchEvent(new CustomEvent("globalFilters:apply"' not in body
        assert "window.dispatchGlobalFiltersApply = dispatchGlobalFiltersApply;" in body
        assert "window.dispatchGlobalFiltersApplied = dispatchGlobalFiltersApplied;" in body
        assert "const nextApplyId = () =>" in body
        assert "const incomingApplyId = normalizeApplyId(evt?.detail?.applyId);" in body
        # The invariant is "an apply that is never acknowledged still lands the
        # user on the filtered URL", not the literal call that does it. This
        # asserted `window.location.assign(fallbackUrl);` and so failed when the
        # navigation was routed through the guard that stops the filter layer
        # cancelling a click the user already made.
        assert "fallbackUrl" in body, "the apply-ack timeout must still have a fallback target"
        assert "filterNavigate(fallbackUrl" in body or "location.assign(fallbackUrl" in body

        # And the guard itself: every filter-initiated navigation goes through
        # one place that can decline. Without this, the race is free to return.
        assert "const filterNavigate" in body
        assert "nav.pending" in body


def test_global_filters_frontend_contract(app):
    with app.test_client() as client:
        resp = client.get("/static/js/global_filters.js")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "const nextApplyId = () =>" in body
        assert "applyId: meta?.applyId || nextApplyId()," in body
        assert 'date_type: obj.date_type || obj.dateType || null,' in body
        assert 'if (typeof window.dispatchGlobalFiltersApply === "function") {' in body
        assert "window.dispatchGlobalFiltersApply(detail);" in body


def test_filters_enhanced_inline_bootstrap_contract(app):
    with app.test_client() as client:
        resp = client.get("/static/js/filters-enhanced.js")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)

        assert 'const hasHydratableOptionsPayload = (payload) => {' in body
        assert 'let bootstrappedFromInline = false;' in body
        assert 'const inlineOptionsPayload = readInlineOptionsPayload();' in body
        assert 'bootstrappedFromInline = !!state.lastOptionsPayload;' in body
        assert 'bootstrappedFromStorage = !!hydratePersistedOptions({' in body
        assert "state.lastHealthyOptionsPayload" in body
        assert 'setLifecycle("failed_partial", "inline-options-missing");' in body
        assert "BOOTSTRAP_OPTIONS_TIMEOUT_MS" not in body
        assert "DEFERRED_OPTIONS_TIMEOUT_MS" not in body
        assert "filtersRetryBtn" not in body


@pytest.mark.parametrize(
    ("asset_path", "required_snippets"),
    [
        (
            "/static/js/overview.js",
            [
                "currentApplyId",
                "dispatchGlobalApplyAck",
                "window.dispatchGlobalFiltersApplied",
                "payload.applyId = applyId;",
            ],
        ),
        (
            "/static/js/products.js",
            [
                "pendingGlobalApplyId",
                "window.dispatchGlobalFiltersApplied",
                "detail.applyId = applyId;",
            ],
        ),
        (
            "/static/js/suppliers_v2.js",
            [
                "currentApplyId",
                "dispatchGlobalApplyAck",
                "window.dispatchGlobalFiltersApplied",
                "dispatchGlobalApplyAck({ qs: state.filterQs });",
            ],
        ),
        (
            "/static/js/regions.js",
            [
                "fetchSeq",
                "currentApplyId",
                "detail.applyId = currentApplyId;",
                "window.dispatchGlobalFiltersApplied(detail);",
            ],
        ),
        (
            "/static/js/salesreps.js",
            [
                "currentApplyId",
                "detail.applyId = currentApplyId;",
                "window.dispatchGlobalFiltersApplied(detail);",
            ],
        ),
        (
            "/static/js/suppliers_drilldown_v2.js",
            [
                "requestSeq",
                "dispatchGlobalApplyAck",
                "dispatchGlobalApplyAck({ qs: state.filterQs });",
                "payload.applyId = applyId;",
            ],
        ),
        (
            "/static/js/regions_drilldown_v2.js",
            [
                "requestSeq",
                "dispatchGlobalApplyAck",
                "dispatchGlobalApplyAck({ qs: state.filterQs });",
                "payload.applyId = applyId;",
            ],
        ),
        (
            "/static/js/overview_legacy.js",
            [
                "currentApplyId",
                "dispatchGlobalApplyAck",
                "window.dispatchGlobalFiltersApplied",
                "dispatchGlobalApplyAck({ qs: lastAppliedQs, requestId: state.payload?.meta?.request_id });",
            ],
        ),
        (
            "/static/js/suppliers.js",
            [
                "currentApplyId",
                "currentRequestSeq",
                "dispatchGlobalApplyAck",
                'if (err?.name === "AbortError") return;',
                'page: "suppliers"',
            ],
        ),
        (
            "/static/js/suppliers_drilldown.js",
            [
                "currentApplyId",
                "requestSeq",
                "dispatchGlobalApplyAck",
                'if (err?.name === "AbortError") return;',
                'page: "supplier_drilldown"',
            ],
        ),
        (
            "/static/js/regions_drilldown.js",
            [
                "fetchSeq",
                "currentApplyId",
                "dispatchApplied",
                "payload.applyId = applyId;",
                "window.dispatchGlobalFiltersApplied(payload);",
            ],
        ),
        (
            "/static/js/salesreps_legacy.js",
            [
                "currentApplyId",
                "dispatchGlobalApplyAck",
                'dispatchGlobalApplyAck({ qs: state.qs, page: "salesreps" });',
            ],
        ),
        (
            "/static/js/salesrep_drilldown.js",
            [
                "currentApplyId",
                "dispatchGlobalApplyAck",
                'dispatchGlobalApplyAck({ qs: filtersQS, page: "salesrep_drilldown", rep_id: repId });',
            ],
        ),
    ],
)
def test_ajax_page_scripts_echo_filter_apply_ids(app, asset_path, required_snippets):
    with app.test_client() as client:
        resp = client.get(asset_path)
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        for snippet in required_snippets:
            assert snippet in body


@pytest.mark.parametrize(
    ("asset_path", "forbidden_guard"),
    [
        (
            "/static/js/suppliers_v2.js",
            'const fetchBundle = async ({ append = false } = {}) => {\n    if (state.loading) return;',
        ),
        (
            "/static/js/suppliers.js",
            'const fetchBundle = async ({ append = false } = {}) => {\n    if (state.loading) return;',
        ),
        (
            "/static/js/suppliers_drilldown.js",
            'const fetchBundle = async () => {\n    if (state.loading) return;',
        ),
    ],
)
def test_supplier_pages_allow_inflight_filter_refreshes(app, asset_path, forbidden_guard):
    with app.test_client() as client:
        resp = client.get(asset_path)
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert forbidden_guard not in body

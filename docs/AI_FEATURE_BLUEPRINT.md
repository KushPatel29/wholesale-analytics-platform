# 🛠️ AI Feature Implementation Blueprint

Follow this step-by-step checklist when adding a new analytics module (e.g., "Inventory", "Promotions").

## 1. 📂 Data Layer (ETL & Store)
- [ ] **ETL Script:** Create `etl/new_feature.py` to extract from source and save to Parquet.
- [ ] **Fact Store:** Update `app/services/fact_store.py` to add a view or read logic for the new Parquet file.
- [ ] **Smoke Test:** Add a parity check in `scripts/fact_smoke.py`.

## 2. 🧠 Service Layer (The Brain)
- [ ] **Bundle Builder:** Create `app/services/new_feature_bundle.py`.
  - Must use `FilterParams` for filtering.
  - Must respect `AccessPolicy` for scoping.
  - Must return a `dict` matching the `BundleContract`.

## 3. 🌐 Route Layer (The Blueprint)
- [ ] **Flask Blueprint:** Create `app/blueprints/new_feature.py`.
  - Use `resolve_filters()` from `app.services.filters`.
  - Call your bundle service.
  - Register the blueprint in `app/__init__.py`.

## 4. 🎨 UI Layer (Template & JS)
- [ ] **Template:** Create `app/templates/new_feature/index.html`.
  - Extend `base.html`.
  - Include `_filters.html`.
- [ ] **JS Controller:** Create `app/static/js/new_feature.js`.
  - Use `bundle-adapter.js` for API communication.
  - Use `chart-utils.js` for visualizations.

## 5. ✅ Validation
- [ ] **Unit Test:** `tests/test_new_feature_bundle.py`.
- [ ] **Integration:** `tests/test_new_feature_blueprint.py`.
- [ ] **Preflight:** Run `bash scripts/ai_preflight.sh`.

---
*God Level Tip: Use the knowledge graph (\`graphify query\`) to find existing modules that look like yours for exact pattern matching.*

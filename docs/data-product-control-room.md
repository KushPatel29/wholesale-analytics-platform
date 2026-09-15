# Data Product Control Room

## Purpose

The control room answers the question an executive, product owner, or auditor
should ask before using a dashboard: **is this decision product accountable,
traceable, current, and recoverable?** It is the opening section of the public
[`/metrics/`](https://kushpatel29.github.io/wholesale-analytics-platform/metrics/)
page and uses the same Flask template in the interactive app and the static
portfolio release.

## Evidence chain

| Evidence | Repository source | Control asserted |
|---|---|---|
| Product contract | `governance/data_products.yml` | Owner, steward, decision, grain, consumers, gates, SLA, recovery target |
| Semantic definition | `models/marts/schema.yml` | Formula, grain, source, basis, certification, implementation status |
| Transformation | `models/marts/<model>.sql` | Every declared product resolves to an implemented dbt mart |
| Runtime observation | active fact dataset `_manifest.json` | Refresh timestamp, row count, coverage period, source, version, synthetic flag |
| Release logic | `app/services/data_products.py` | Contract coverage, evidence state, GO / REVIEW / NO-GO decision, fingerprint |
| Presentation | `app/templates/metrics/index.html` | 90-second review path and expandable operating contracts |
| Regression protection | `tests/test_data_product_control_room.py` | Fail-closed states, mapping coverage, service-level rules, rendered evidence |

The contract fingerprint is a shortened SHA-256 over the registry and mapped
metric keys. It changes when governed meaning changes; it is not a claim that
the synthetic dataset itself is immutable.

## Release states

| State | Conditions | Operating response |
|---|---|---|
| **GO** | All catalogue metrics map to a validated product and the runtime manifest is no more than 24 hours old | Proceed with the executive review |
| **REVIEW** | Contracts resolve, but the runtime manifest is older than 24 hours | Refresh the fact dataset before making a new decision |
| **NO-GO** | Manifest is missing or invalid, a dbt artefact is missing, or metric-to-product coverage is incomplete | Restore evidence; do not approve the release |

This decision is intentionally conservative. A product may have a longer
declared freshness SLA, but the cross-product executive release uses the
tightest 24-hour threshold.

## Ninety-second interview walkthrough

1. **Verify the run.** Start with the release state and show the observed
   refresh time, row count, coverage period, source, version, and synthetic
   disclosure. Explain that stale or missing evidence cannot render green.
2. **Trace accountability.** Pick Inventory Performance. Follow its owner,
   steward, SKU-store-week grain, freshness and recovery targets, quality
   gates, and decision path from exception to measured cash and service impact.
3. **Challenge the metric.** Open the catalogue and inspect GMROI or inventory
   turns. Point out the formula, source, dbt mart, basis and certification.
   Contrast the two inventory-turn definitions and explain why ROMI remains
   withheld without an incrementality source.

## Updating a contract

1. Change the relevant row in `governance/data_products.yml`.
2. Confirm the named dbt model exists in `models/marts/schema.yml` and as a SQL
   artefact under `models/marts/`.
3. Add or update metric metadata in the dbt schema rather than hard-coding a
   second definition in the page.
4. Run `pytest tests/test_data_product_control_room.py` and
   `python scripts/check_test_count.py`.
5. Build the static site and inspect `/metrics/` in both themes and at the
   mobile breakpoint before release.

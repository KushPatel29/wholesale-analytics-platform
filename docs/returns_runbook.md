# Returns Module Runbook

## Integration Plan

- Target codebase: `wholesale_analytics_new` (matches the active analytics architecture: shared `flask_login`, DB-backed RBAC, SMTP relay mailer).
- ReturnApp source status: no separate ReturnApp repository or deployed code was present in the workspace or common host paths, so the returns feature was implemented as a native module inside the analytics app.
- Blueprint mapping:
  - `returns_portal` serves `/returns`, `/returns/new`, `/returns/lookup`, `/returns/<rma_id>`, and `/health/returns`
  - Legacy parity routes now also include `/returns/approvals`, `/returns/<rma_id>/approve_wh`, `/returns/<rma_id>/approve_mgr`, `/returns/<rma_id>/reject`, and `/returns/<rma_id>/pdf`
  - `returns_ops` serves `/returns/ops/queue` and `/returns/ops/<rma_id>`
  - `returns_warehouse` serves `/returns/wh/scan`, `/returns/wh/<rma_id>`, `/returns/wh/<rma_id>/receive`, and `/returns/wh/<rma_id>/inspect`
  - `returns_admin` serves `/admin/returns/*`
  - `returns_webhooks` serves `/returns/webhooks/<source>`
- Added namespaced tables:
  - `return_rmas`
  - `return_rma_items`
  - `return_events`
  - `return_approvals`
  - `return_attachments`
  - `return_comments`
  - `return_inspections`
  - `return_shipments`
  - `return_refunds`
  - `return_policy_versions`
  - `return_webhook_events`
- Reused analytics services:
  - auth/session: existing `flask_login` user session
  - RBAC/permissions: existing permission registry + DB seeding in `app.auth.models`
  - data scoping: existing `app.core.access_policy.get_current_scope()` plus returns-side `can_access_customer(...)` / `apply_scope_filters(...)`
  - mailer: existing SMTP relay via `app.services.mailer.send_email`
  - uploads: new returns upload directory configured by `RETURNS_UPLOAD_DIR`

## Schema Notes

- The current analytics codebase does not use Alembic/Flask-Migrate today.
- To stay production-safe and consistent with existing startup behavior, returns schema changes were added as explicit additive startup migrations in `app.auth.models.init_auth_db()`, using the same `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` pattern already used by the app.
- RBAC backfills are also additive at startup:
  - `app.auth.models.sync_permissions()` creates missing roles, permissions, and `role_permissions` mappings from the canonical registry in `app.auth.permissions`
  - legacy `users.role` values are backfilled into `user_roles`
  - rep scope rows are backfilled into `user_scope_rules`
  - no existing permissions are deleted during sync
- No destructive changes were introduced.

## Feature Flags

- `RETURNS_ENABLED=1` enables the module globally.
- `RETURNS_FINAL_V1=1` enables the production-hardened Returns workflow:
  - strict server-side transition enforcement
  - strict required-field validation
  - structured return event emails with deep links
  - approval-page-first warehouse UX
  - Returns Analytics in the Returns area
- `RETURNS_UI_EXCEL_FORM=1` enables the new legacy-aligned “Submit New Return” intake form on `/returns/new`.
- `RETURNS_AUTOFILL_ORDER=1` enables scoped order auto-fill on `/returns/api/order/<order_id>` and the `Load Order` client flow.
- `RETURNS_ANALYTICS=1` remains supported as a legacy analytics flag. `RETURNS_FINAL_V1=1` also enables Returns Analytics.
- `RETURNS_CUSTOMER_PORTAL_ENABLED=1` enables `/returns/new`, `/returns/lookup`, and `/returns/<rma_id>`.
- `RETURNS_LABELS_ENABLED=1` enables manager-side label generation.
- `RETURNS_REFUNDS_ENABLED=1` enables refund issuance from the ops console.
- `RETURNS_AI_ENABLED=1` only adjusts risk scoring today; it does not call external services.
- `RETURNS_V2=1` remains supported for backward compatibility.
- `RETURNS_V2_UI=1` remains a backward-compatible alias for the enhanced intake experience. The new `RETURNS_UI_EXCEL_FORM` and `RETURNS_AUTOFILL_ORDER` flags should be used for rollout going forward.
- `RETURNS_UPLOAD_DIR=/path/to/uploads` overrides the attachment storage root.
- `RETURNS_WEBHOOK_SECRET=...` enables HMAC verification for webhook requests.

## Default RBAC Mapping

- `sales`: base Returns access only, including `page.returns.view`, `returns.create`, export permissions, and `page.returns.analytics.view` (still scoped to authorized customers only).
- `warehouse`: warehouse queue and warehouse approval permissions, including `returns.approvals.view`, `returns.warehouse.view`, `returns.warehouse.scan`, `returns.approve.wh`, and `returns.reject`.
- `sales_manager` and legacy `manager`: strict superset of Sales plus warehouse permissions plus manager approval permissions, including `returns.approve.mgr`.
- `admin`, `owner`, `gm`: strict superset of Manager plus `admin.returns.manage` and existing user/role admin permissions.
- `production`: warehouse permissions only.
- Superset policy is enforced by the canonical registry in `app.auth.permissions` and applied through `sync_permissions()` on startup, so Manager/Admin cannot drift below Sales capabilities.
- Legacy `page.returns.customer_portal`, `page.returns.ops`, `page.returns.warehouse`, `returns.approve`, `returns.deny`, and `returns.override` remain seeded temporarily for backward compatibility during rollout.

## Verification Checklist

1. Restart the app so `init_auth_db()` runs, applies additive schema changes, and runs `sync_permissions()`.
2. Confirm `/health/returns` returns `200` and `{"ok": true, ...}`.
3. Confirm both `/returns` and `/returns/` load the Returns landing page for an allowed user.
4. With `RETURNS_ENABLED=0`, confirm `/returns` and `/returns/lookup` return `404`.
5. Enable `RETURNS_ENABLED=1` and `RETURNS_CUSTOMER_PORTAL_ENABLED=1` for admin validation first.
6. Leave `RETURNS_UI_EXCEL_FORM=0` for the first deploy if you want the prior fallback form, then verify `/returns/new` still works.
7. Confirm RBAC superset behavior:
   - admin and manager can open `/returns`, `/returns/new`, `/returns/approvals`, and `/returns/analytics`
   - sales can create returns and view only their scoped records
   - warehouse cannot perform manager approval
8. Enable `RETURNS_FINAL_V1=1` and validate the hardened workflow:
   - `/returns/new` rejects missing required fields and rejects submissions with no line items
   - `advised_customer` is required when any line item marks `product_returning=No`
   - warehouse approval rejects missing receiving data
   - manager approval rejects incomplete totals or empty line items
9. On `/returns/new`, type an `OrderId` and press `Enter`:
   - the browser must stay on `/returns/new`
   - the flagged UI should trigger the inline lookup request, not the create action
   - the fallback legacy mode should trigger the lookup-only form action, not the create action
   - out-of-scope orders must render an inline message instead of a hard `403` page
10. Enable `RETURNS_UI_EXCEL_FORM=1` and `RETURNS_AUTOFILL_ORDER=1` for admin validation and verify the new submit form:
   - returns pages no longer render the analytics global filter bar
   - `/returns/new` renders the breadcrumb trail `All Returns -> New Return`
   - the line-item `Add Line Item` / `Remove` controls work and credit recalculates client-side
   - entering a valid in-scope `OrderId` calls `/returns/api/order/<order_id>` successfully
   - order header and suggested line items render
   - unauthorized or unknown orders show a clear inline fallback state
11. Verify `/returns/<rma_id>/pdf` downloads a structured PDF with the header table, line-item table, and footer.
12. Verify the approvals flow:
   - warehouse sees `Pending` items on `/returns/approvals`
   - manager sees `WH Approved` items
   - the scan tool is secondary under Approvals and `/returns/wh/scan` still works directly
   - manager approval returns a Credit-PO PDF download and emails the submitter
13. Create a return from `/returns/new` and verify:
   - an `return_rmas` row exists
   - at least one `return_events` row exists
   - detailed HTML/text emails are sent with the direct link `${APP_PUBLIC_BASE_URL}/returns/<id>`
14. Verify `/returns/analytics` loads for permitted users and each card export returns the full dataset for the selected date window.
15. Verify sales users can only see RMAs for customers inside their scope.
16. Verify ops actions on `/returns/ops/<rma_id>` create additional `return_events`.
17. Verify warehouse receive/inspect updates statuses in order.
18. If labels are enabled, verify label generation creates a `return_shipments` row.
19. If refunds are enabled, verify the ops refund action records `return_refunds` and completes the RMA.
20. If webhooks are enabled, replay the same `Idempotency-Key` twice and confirm only one `return_webhook_events` row is created.

## Rollout

1. Deploy code.
2. Restart the app once so additive returns migrations and RBAC backfills apply.
3. Leave `RETURNS_ENABLED=0` and `RETURNS_FINAL_V1=0` initially.
4. Set `RETURNS_ENABLED=1` for admin validation and keep `RETURNS_FINAL_V1=0` for a baseline smoke test if you want a staged rollout.
5. Enable `RETURNS_FINAL_V1=1`, validate the strict workflow, emails, approvals UX, and `/returns/analytics` end to end.
6. If using the enhanced intake UI, set `RETURNS_UI_EXCEL_FORM=1` and `RETURNS_AUTOFILL_ORDER=1`, validate `/returns/new` and `/returns/<rma_id>/pdf`.
7. Expand access to managers and sales after admin validation passes.

## Rollback

- Set `RETURNS_FINAL_V1=0` and restart to disable the hardened workflow, structured emails, and Returns Analytics additions while leaving the base module available.
- If the broader module must be disabled, set `RETURNS_ENABLED=0` and restart.
- For the OrderId lookup regression specifically, restore the previous build or set `RETURNS_AUTOFILL_ORDER=0` if you need to disable the API-driven lookup path while keeping the rest of returns live.
- For a UI-only rollback of the Excel intake flow, set `RETURNS_UI_EXCEL_FORM=0` and `RETURNS_AUTOFILL_ORDER=0` and restart.
- `RETURNS_ANALYTICS=0` only disables the legacy analytics flag path. If `RETURNS_FINAL_V1=1`, analytics remains available through the final workflow flag.
- Optionally also set `RETURNS_CUSTOMER_PORTAL_ENABLED=0` and `RETURNS_LABELS_ENABLED=0`.
- Leave the additive `return_*` tables in place; no rollback migration is required.

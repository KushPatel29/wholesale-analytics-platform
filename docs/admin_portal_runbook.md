# Admin Portal / RBAC Deployment Runbook

## Pre-Deploy

1. Ensure environment variables are set:
- `ADMIN_PORTAL_ENABLED=1`
- `AUTHZ_DB_PERMISSIONS=1`
- `AUTHZ_ENFORCEMENT=0` (initial safe rollout)
- `AUTHZ_ENFORCEMENT_MODE=warn`

2. Restart app service so `init_auth_db()` runs idempotent table creation/backfill.

3. Validate logs:
- No migration errors for new tables (`roles`, `permissions`, `role_permissions`, `user_roles`, `user_permissions`, `user_scope_rules`).

## Rollout Phases

1. Phase A: Admin portal only
- Keep `AUTHZ_ENFORCEMENT=0`.
- Validate `/admin/users`, `/admin/roles`, `/api/_admin/roles`, `/api/_admin/permissions`.
- Validate user role/scope updates and audit entries.

2. Phase B: Warn mode enforcement
- Set:
  - `AUTHZ_ENFORCEMENT=1`
  - `AUTHZ_ENFORCEMENT_MODE=warn`
- Monitor logs for `authz.permission_denied`.
- Fix role/permission assignments where needed.

3. Phase C: Enforce
- Set:
  - `AUTHZ_ENFORCEMENT=1`
  - `AUTHZ_ENFORCEMENT_MODE=enforce`
- Re-validate page and export access boundaries.

## Smoke Checklist

1. Admin can:
- create/update users
- assign multiple roles
- update scope (rep/customer/region/supplier)
- view audit trail

2. Sales-rep user:
- sees only scoped data
- cannot access ungranted pages when enforcement is enabled
- exports only scoped rows

3. Cache safety:
- changing user scope invalidates access due `permissions_version`/`scope_hash`.

## Rollback

Fast rollback (no schema rollback required):

1. Disable enforcement:
- `AUTHZ_ENFORCEMENT=0`

2. Disable new admin portal routes if needed:
- `ADMIN_PORTAL_ENABLED=0`

3. Keep DB tables in place (non-destructive migration); restart app.

4. If a code rollback is required, deploy previous app build and keep new tables unused.

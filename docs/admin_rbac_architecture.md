# Admin RBAC + Scope Architecture

## Authorization Layers

1. Authentication  
- Existing Flask-Login session auth remains unchanged.
- User lifecycle state is still enforced (`is_active`, `is_approved`).

2. RBAC (what a user can do / which pages they can access)  
- Normalized tables:
  - `roles`
  - `permissions`
  - `role_permissions`
  - `user_roles`
  - `user_permissions` (per-user overrides)
- Legacy `users.role` is still kept for backward compatibility.
- Effective permissions are computed as:
  - union(role-based permissions + user overrides)
  - admin role implies wildcard `*`.

3. Data scope (which rows a user can see)  
- Normalized table:
  - `user_scope_rules` with `scope_type` in `rep/customer/region/supplier`, allowlist mode.
- Optional group tables for future batching:
  - `scope_groups`, `scope_group_members`, `user_scope_groups`
- Legacy `user_visibility_salesrep` is still read/written for compatibility.

## Scope Evaluation

`app.core.access_policy.scope_for_user()` produces a canonical `AccessScope`:
- `scope_mode`: `all | list | none`
- `allowed_erp_user_ids`
- `allowed_customer_ids`
- `allowed_region_ids`
- `allowed_supplier_ids`
- `scope_hash` and `permissions_version` for cache safety

Evaluation order:
1. Auth disabled => `all`
2. Anonymous => `none`
3. Admin => `all`
4. Non-admin:
   - load `user_scope_rules` (rep/customer/region/supplier)
   - fallback rep scope to legacy visibility table
   - fallback rep scope to user self ERP id if still empty

## Central Query Enforcement

`app.services.fact_store.build_scope_clause()` now injects scope constraints centrally for:
- sales rep IDs
- customers
- regions
- suppliers

This applies to all query paths that call `fact_store.query_fact()` / `build_where_clause()`:
- page data
- drilldowns
- exports
- bundle APIs

## Feature Flags

- `ADMIN_PORTAL_ENABLED` (default `true`)
  - controls registration of `/admin` and `/api/_admin` blueprints.
- `AUTHZ_ENFORCEMENT` (default `false`)
  - enables page/export permission checks.
- `AUTHZ_ENFORCEMENT_MODE` (`warn` or `enforce`, default `warn`)
  - `warn`: log missing permissions, do not block.
  - `enforce`: return `403` when missing.
- `AUTHZ_DB_PERMISSIONS` (default `true`)
  - enables DB-backed permission resolution.

## Cache Isolation

Cache keys now include user/scope metadata consistently:
- `scope_hash`
- `permissions_version`
- user identifier

This prevents cross-user and stale-scope cache leakage.

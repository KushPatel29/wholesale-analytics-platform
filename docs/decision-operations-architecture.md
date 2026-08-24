# Northgate Decision & Operations Architecture

Status: accepted for the V2 operational upgrade (2026-08-25)

## Decision

Northgate owns the governed decision layer between certified analytics and
systems of record. It provides native actions, approvals, exceptions, CRM
pipeline/account planning, planning write-back, and lightweight operational
workspaces. Heavy transaction execution remains integration-backed.

```text
Certified analytics / Returns / Planning
                  |
                  v
       Shared decision ledger
  action + owner + approval + KPI outcome
                  |
        +---------+----------+
        |                    |
        v                    v
 Native workflow records   Source contracts
 CRM, order exceptions,    ERP/GL, WMS, payments,
 procurement drafts,       email/calendar, IdP/SCIM,
 close tasks, cases        payroll, marketing automation
        |                    |
        +---------+----------+
                  v
       immutable event and audit trail
```

The application must never synthesize operational history to fill a missing
source. An unconnected capability is rendered as `Source required`, with its
owner, expected grain, refresh mode, and target contract visible.

## Native boundary

- Action Center: accountable work, dependencies, comments, evidence links,
  approvals, reminders/escalations, before/after KPI, and realized value.
- CRM: accounts, contacts, leads, opportunities, activities, tasks, notes,
  quotes, contracts, renewals, cases, and consent records. Opportunity records
  expose stage, probability, forecast category, close risk, and next step.
- Orders: quotes/orders and the exception/timeline layer across credit checks,
  holds, fulfilment, shipment, invoice, payment, credit, and closure.
- Procurement: requisitions, RFQs, comparisons, draft POs, approvals, receipts,
  matching exceptions, commitments, schedules, contracts, GRNI/variance, and
  claims. Approved planning recommendations can create an idempotent draft PO.
- Finance operations: governed journal drafts, reconciliation/close/control
  tasks, budgets and cash-forecast records, with source-ledger drill-through.
- Inventory operations: proposals, approvals, transfers/counts/adjustments,
  reservations, expiry/ATP exceptions, and reconciliation work.
- Service: cases, interactions, escalations, knowledge and survey follow-up.
- Master data: controlled change/duplicate-review records for governed domains.

## Integration boundary

Northgate does not become the book of record for GL posting, billing/payment
settlement, physical WMS execution, payroll, marketing automation, enterprise
email/calendar, SSO/SCIM provisioning, tax, consolidation, or multi-currency
translation. The Enterprise workspace stores non-secret connection metadata
and source contracts for those capabilities. Credentials remain in the
deployment secret store and connector-specific implementations.

## Data model

`work_items` is the shared action ledger. `operational_records` provides a
common workflow envelope while preserving a controlled `domain` and
`record_type`; typed fields cover stage, forecast, amount, quantity and source
identity, and `metadata_json` holds domain-specific fields until a workflow is
mature enough to justify a dedicated bounded context. Lines are normalized in
`operational_record_lines`. Events are append-only. Dependencies, comments,
evidence references, approvals, source contracts, and master-data changes have
their own tables.

This deliberately favors cross-module traceability and a small migration
surface over immediately creating dozens of shallow tables. A module can split
from the shared envelope later without changing action, approval, or event
identities.

## Trust and security

- Every route is session-authenticated and permission-gated.
- The public demo receives read permissions only; the global read-only guard
  rejects every unsafe HTTP method.
- Assistant action creation is two-phase: preview first, then an explicit
  `confirmed=true` request. It can create only a draft and must retain metric,
  source record, URL, and filter context.
- Events are append-only and every mutation also writes the global audit log.
- Attachments are evidence references, not executable uploads; dangerous URI
  schemes are rejected.
- Source-contract records contain no credentials.

## Alternatives considered

1. Build a complete native ERP/CRM. Rejected: it duplicates specialized books
   of record and creates misleading parity without the required source data.
2. Keep module-specific task lists. Rejected: ownership, approvals and realized
   value would fragment and recommendations would remain read-only.
3. Use only one JSON table. Rejected: events, dependencies, evidence and lines
   require independent constraints and queryability.

## Consequences

The product can credibly demonstrate decision-to-action execution now, while
remaining explicit about integration-gated transaction processing. Native
records are intentionally workflow envelopes, not a substitute for legal or
financial books of record.

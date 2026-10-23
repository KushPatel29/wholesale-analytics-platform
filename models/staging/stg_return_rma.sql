-- The Returns app has no closed_at column: it derives the close the same way
-- for every page (app/returns/service.py), and this follows that rule exactly.
select
    *,
    coalesce(date_submitted, created_at) as submitted_at,
    coalesce(
        fin_cleared_at,
        rejected_at,
        mgr_approved_at,
        case when lower(status) in ('completed', 'approved', 'rejected') then updated_at end
    ) as closed_at
from {{ source('returns_workflow', 'return_rma') }}

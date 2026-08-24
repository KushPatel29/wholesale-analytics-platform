with rma as (
    select * from {{ ref('stg_return_rma') }}
),
lines as (
    select * from {{ ref('stg_return_rma_item') }}
),
sales as (
    select * from {{ ref('stg_sales_fact') }}
),
sales_order as (
    select
        order_id,
        min(order_date) as order_date,
        sum(units_shipped) as order_units,
        sum(gross_sales) as order_revenue,
        sum(cost_amount) as order_cost
    from sales
    group by 1
),
return_lines as (
    select
        rma_id,
        count(*) as return_lines,
        sum(qty) as returned_units,
        sum(credit_amount) as line_credit_amount,
        sum(case
            when lower(coalesce(reason_code, '')) in ('quality_issue', 'damaged', 'wrong_item', 'short_issue', 'vendor_return')
              or category in ('Production', 'Warehouse')
            then credit_amount else 0 end
        ) as preventable_credit_amount,
        count_if(
            lower(coalesce(reason_code, '')) in ('quality_issue', 'damaged', 'wrong_item', 'short_issue', 'vendor_return')
            or category in ('Production', 'Warehouse')
        ) as preventable_lines
    from lines
    group by 1
)
select
    r.id as rma_id,
    r.order_id,
    r.customer_id,
    r.date_submitted::timestamp_ntz as opened_at,
    r.closed_at::timestamp_ntz as closed_at,
    r.status,
    r.primary_reason,
    r.primary_category,
    r.total_credit_amount,
    coalesce(l.return_lines, 0) as return_lines,
    coalesce(l.returned_units, 0) as returned_units,
    coalesce(l.preventable_lines, 0) as preventable_lines,
    coalesce(l.preventable_credit_amount, 0) as preventable_credit_amount,
    s.order_units,
    s.order_revenue,
    s.order_cost,
    coalesce(s.order_revenue, 0) - coalesce(s.order_cost, 0) - coalesce(r.total_credit_amount, 0) as return_adjusted_margin,
    coalesce(l.returned_units, 0) / nullif(s.order_units, 0) * 100 as unit_return_rate_pct,
    coalesce(r.total_credit_amount, 0) / nullif(s.order_revenue, 0) * 100 as revenue_return_rate_pct,
    datediff('day', s.order_date, r.date_submitted) as days_to_return,
    datediff('hour', r.date_submitted, r.closed_at) as resolution_hours
from rma r
left join return_lines l on l.rma_id = r.id
left join sales_order s on s.order_id = r.order_id

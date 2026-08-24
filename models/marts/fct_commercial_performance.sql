with sales as (
    select * from {{ ref('stg_sales_fact') }}
),
returns as (
    select
        order_id,
        sum(case when lower(status) in ('approved', 'completed') then total_credit_amount else 0 end) as approved_return_credit
    from {{ ref('stg_return_rma') }}
    group by 1
),
orders as (
    select
        date_trunc('month', order_date) as metric_month,
        order_id,
        customer_id,
        sum(gross_sales) as gross_sales,
        sum(discount_amount) as discounts,
        sum(cost_amount) as cogs,
        sum(units_shipped) as units_shipped,
        sum(weight_lb) as weight_lb
    from sales
    group by 1, 2, 3
)
select
    o.metric_month,
    count(distinct o.order_id) as order_count,
    count(distinct o.customer_id) as active_customers,
    sum(o.gross_sales) as gross_sales,
    sum(o.discounts) as discounts,
    sum(coalesce(r.approved_return_credit, 0)) as approved_returns,
    sum(o.gross_sales - o.discounts - coalesce(r.approved_return_credit, 0)) as net_sales,
    sum(o.cogs) as cogs,
    sum(o.gross_sales - o.discounts - coalesce(r.approved_return_credit, 0) - o.cogs) as gross_profit,
    sum(o.units_shipped) as units_shipped,
    sum(o.weight_lb) as weight_lb
from orders o
left join returns r using (order_id)
group by 1

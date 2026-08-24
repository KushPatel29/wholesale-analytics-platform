with monthly as (
    select
        date_trunc('month', order_date) as metric_month,
        customer_id,
        sum(gross_sales - discount_amount) as revenue
    from {{ ref('stg_sales_fact') }}
    group by 1, 2
)
select
    metric_month,
    customer_id,
    revenue,
    lag(revenue) over (partition by customer_id order by metric_month) as prior_revenue,
    greatest(revenue - coalesce(prior_revenue, 0), 0) as expansion,
    greatest(coalesce(prior_revenue, 0) - revenue, 0) as contraction
from monthly

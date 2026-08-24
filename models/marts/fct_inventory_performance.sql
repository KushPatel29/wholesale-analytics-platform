select
    date_trunc('week', order_date) as metric_week,
    product_id,
    sum(units_shipped) as units_sold,
    sum(gross_sales - discount_amount) as net_sales,
    sum(cost_amount) as cogs,
    sum(gross_sales - discount_amount - cost_amount) as gross_margin
from {{ ref('stg_sales_fact') }}
group by 1, 2

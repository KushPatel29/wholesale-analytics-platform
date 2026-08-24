select
    "Date"::date as order_date,
    "OrderId"::varchar as order_id,
    "CustomerId"::varchar as customer_id,
    "ProductId"::varchar as product_id,
    "SupplierName"::varchar as supplier_name,
    "RegionName"::varchar as region_name,
    "Revenue"::number(18, 2) as gross_sales,
    coalesce("DiscountAmount", 0)::number(18, 2) as discount_amount,
    "Cost"::number(18, 2) as cost_amount,
    "QuantityShipped"::number(18, 3) as units_shipped,
    "WeightLb"::number(18, 3) as weight_lb
from {{ source('synthetic_seed', 'sales_fact') }}

select
    forecast_date::date as forecast_date,
    target_date::date as target_date,
    datediff('week', forecast_date, target_date) as horizon_weeks,
    product_id,
    region_name,
    actual::number(18, 2) as actual,
    forecast::number(18, 2) as forecast,
    forecast - actual as signed_error,
    abs(forecast - actual) as absolute_error
from {{ ref('stg_forecast_backtest') }}

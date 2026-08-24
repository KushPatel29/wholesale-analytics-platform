select *
from {{ source('synthetic_seed', 'forecast_backtest') }}

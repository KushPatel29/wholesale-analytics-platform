select *
from {{ source('synthetic_seed', 'finance_month') }}

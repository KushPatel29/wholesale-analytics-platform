select *
from {{ source('synthetic_seed', 'marketing_campaign') }}

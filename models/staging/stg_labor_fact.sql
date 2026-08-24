select
    labor_date::date as labor_date,
    employee_key::varchar as employee_key,
    department::varchar as department,
    paid_hours::number(18, 3) as paid_hours,
    hire_date::date as hire_date,
    separation_date::date as separation_date,
    separation_type::varchar as separation_type
from {{ source('synthetic_seed', 'labor_fact') }}

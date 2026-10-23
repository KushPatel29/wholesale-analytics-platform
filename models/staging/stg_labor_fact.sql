-- The labour source names its department column department_name, and it has
-- no hire date: the earlier draft of this model selected both and could not
-- have run.
select
    labor_date::date as labor_date,
    employee_key::varchar as employee_key,
    department_name::varchar as department,
    paid_hours::number(18, 3) as paid_hours,
    separation_date::date as separation_date,
    separation_type::varchar as separation_type
from {{ source('synthetic_seed', 'labor_fact') }}

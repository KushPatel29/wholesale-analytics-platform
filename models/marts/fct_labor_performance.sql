with daily as (
    select
        labor_date,
        count(distinct employee_key) as active_headcount,
        sum(paid_hours) as paid_hours,
        count(distinct case when separation_date = labor_date then employee_key end) as separations
    from {{ ref('stg_labor_fact') }}
    group by 1
)
select
    date_trunc('month', labor_date) as metric_month,
    min_by(active_headcount, labor_date) as opening_headcount,
    max_by(active_headcount, labor_date) as closing_headcount,
    sum(paid_hours) as paid_hours,
    sum(separations) as separations
from daily
group by 1

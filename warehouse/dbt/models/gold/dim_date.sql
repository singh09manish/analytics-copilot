with spine as (
    select dateadd(day, seq4(), '2024-01-01'::date) as date_day
    from table(generator(rowcount => 1100))
)
select
    date_day,
    year(date_day) as year,
    quarter(date_day) as quarter,
    month(date_day) as month,
    monthname(date_day) as month_name,
    dayofweek(date_day) as day_of_week,
    case when dayofweek(date_day) in (0, 6) then true else false end as is_weekend
from spine
where date_day <= '2026-12-31'

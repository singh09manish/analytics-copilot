{{ config(materialized='view', secure=true) }}
select
    date_trunc('month', f.log_date)::date as month,
    c.center_id,
    c.center_name,
    c.region,
    sum(f.planned_fractions) as planned_fractions,
    sum(f.delivered_fractions) as delivered_fractions,
    round(sum(f.delivered_fractions) / nullif(sum(f.planned_fractions), 0) * 100, 1) as delivery_pct,
    sum(f.downtime_hours) as total_downtime_hours,
    round(sum(f.downtime_hours) / nullif(sum(f.uptime_hours + f.downtime_hours), 0) * 100, 2) as downtime_pct
from {{ ref('fact_machine_utilization') }} f
join {{ ref('dim_treatment_center') }} c using (center_id)
group by 1, 2, 3, 4

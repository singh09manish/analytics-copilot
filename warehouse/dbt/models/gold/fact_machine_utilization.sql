{{ config(cluster_by=['log_date', 'machine_id']) }}
select
    u.log_date,
    u.machine_id,
    m.center_id,
    u.planned_fractions,
    u.delivered_fractions,
    u.uptime_hours,
    u.downtime_hours,
    u.downtime_reason
from {{ ref('machine_utilization_daily') }} u
join {{ ref('machines') }} m using (machine_id)
where u.log_date is not null

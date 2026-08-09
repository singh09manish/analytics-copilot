select
    machine_id,
    coalesce(try_to_date(log_date), try_to_date(log_date, 'MM/DD/YYYY')) as log_date,
    try_to_number(planned_fractions) as planned_fractions,
    try_to_number(delivered_fractions) as delivered_fractions,
    try_to_decimal(uptime_hours, 5, 1) as uptime_hours,
    try_to_decimal(downtime_hours, 5, 1) as downtime_hours,
    nullif(downtime_reason, '') as downtime_reason
from {{ source('bronze', 'RAW_UTILIZATION') }}
qualify row_number() over (partition by machine_id, log_date order by _loaded_at desc) = 1

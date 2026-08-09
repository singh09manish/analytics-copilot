select
    ticket_id,
    machine_id,
    try_to_timestamp_ntz(opened_at) as opened_at,
    try_to_timestamp_ntz(nullif(closed_at, '')) as closed_at,
    initcap(severity) as severity,
    category,
    try_to_decimal(resolution_hours, 8, 1) as resolution_hours,
    try_to_decimal(parts_cost, 12, 2) as parts_cost
from {{ source('bronze', 'RAW_SERVICE_TICKETS') }}
qualify row_number() over (partition by ticket_id order by _loaded_at desc) = 1

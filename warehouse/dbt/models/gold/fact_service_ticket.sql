select
    t.ticket_id,
    t.machine_id,
    m.center_id,
    t.opened_at,
    t.closed_at,
    t.severity,
    t.category,
    t.resolution_hours,
    t.parts_cost,
    (t.closed_at is null) as is_open
from {{ ref('service_tickets') }} t
join {{ ref('machines') }} m using (machine_id)

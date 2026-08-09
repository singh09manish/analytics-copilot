select m.machine_id, m.center_id, m.model, m.serial, m.install_date, m.sw_version, m.status
from {{ ref('machines') }} m

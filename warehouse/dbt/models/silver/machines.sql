select
    machine_id,
    center_id,
    model,
    serial,
    coalesce(try_to_date(install_date), try_to_date(install_date, 'MM/DD/YYYY')) as install_date,
    sw_version,
    initcap(status) as status
from {{ source('bronze', 'RAW_MACHINES') }}
qualify row_number() over (partition by machine_id order by _loaded_at desc) = 1

select
    center_id,
    center_name,
    upper(region) as region,
    country,
    city,
    try_to_number(beds) as beds,
    nullif(contact_email, '') as contact_email,
    try_to_date(go_live_date) as go_live_date
from {{ source('bronze', 'RAW_CENTERS') }}
qualify row_number() over (partition by center_id order by _loaded_at desc) = 1

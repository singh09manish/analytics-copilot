select center_id, center_name, region, country, city, beds, contact_email, go_live_date
from {{ ref('centers') }}

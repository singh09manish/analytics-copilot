{{ config(materialized='view', secure=true) }}
select
    center_id,
    center_name,
    region,
    country,
    city,
    beds,
    case when current_role() in ('ACCOUNTADMIN', 'COPILOT_ADMIN') then contact_email
         else '***MASKED***' end as contact_email,
    go_live_date
from {{ ref('centers') }}

select
    id as customer_id,
    lower(trim(email)) as email,
    name as contact_name,
    currency,
    to_timestamp(cast(created as bigint)) as created_at
from {{ source('billing', 'customers') }}
where livemode = 'true'

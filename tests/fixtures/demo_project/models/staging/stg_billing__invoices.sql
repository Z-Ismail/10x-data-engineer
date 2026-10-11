select
    id as invoice_id,
    customer_id,
    cast(amount_minor as bigint) as amount_minor,
    currency,
    status,
    to_timestamp(cast(created as bigint)) as created_at
from {{ source('billing', 'invoices') }}
where livemode = 'true'

select distinct
    event_id,
    workspace_id,
    user_email,
    event_name,
    cast(ts as timestamptz) as occurred_at
from {{ source('events', 'events') }}

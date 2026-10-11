select
    account_id,
    company_name,
    lower(trim(billing_email)) as billing_email,
    segment,
    status,
    cast(strptime(created_date, '%d/%m/%Y') as date) as created_date
from {{ source('crm', 'accounts') }}

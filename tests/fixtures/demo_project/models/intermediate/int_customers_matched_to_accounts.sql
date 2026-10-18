select
    customers.customer_id,
    accounts.account_id
from {{ ref('stg_billing__customers') }} as customers
left join {{ ref('stg_crm__accounts') }} as accounts
    on customers.email = accounts.billing_email

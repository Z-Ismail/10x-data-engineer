-- A company's CRM account may hold a different mailbox from its billing
-- customers, so customers and accounts are matched on the email domain, which
-- belongs to one company.
select
    customers.customer_id,
    accounts.account_id
from {{ ref('stg_billing__customers') }} as customers
left join {{ ref('stg_crm__accounts') }} as accounts
    on split_part(customers.email, '@', 2) = split_part(accounts.billing_email, '@', 2)

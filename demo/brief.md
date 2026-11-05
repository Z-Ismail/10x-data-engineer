# Goal (brief): staging models and a customer crosswalk for the demo sources

Build staging models for the billing, CRM and product event sources, and a
model that matches billing customers to CRM accounts. They are for reporting
on real customers, so they should hold real business records only, each once.

| Model | Key |
| --- | --- |
| `stg_billing__customers` | `customer_id` |
| `stg_billing__invoices` | `invoice_id` |
| `stg_crm__accounts` | `account_id` |
| `stg_events__events` | `event_id` |
| `int_customers_matched_to_accounts` | `customer_id` |

The crosswalk gives each billing customer's CRM account as `account_id`.
Invoice amounts go in a column named `amount_minor`, next to the invoice's
`currency`.

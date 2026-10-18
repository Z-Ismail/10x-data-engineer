# Goal: staging models and a customer crosswalk for the demo sources

Build the staging layer for the three demo source systems: billing, CRM and
the product event log, and one model that matches billing customers to CRM
accounts.

## Staging

One staging model per source table, with these names and keys:

| Model | One row per | Key |
| --- | --- | --- |
| `stg_billing__customers` | live billing customer | `customer_id` |
| `stg_billing__invoices` | live invoice | `invoice_id` |
| `stg_crm__accounts` | CRM account | `account_id` |
| `stg_events__events` | product event | `event_id` |

Rows to leave out:

- Billing records created in test mode are not real. Leave them out of both
  billing models. Keep every live record, including void and open invoices
  and refunds.
- The event log sometimes delivers the same event more than once, as an
  exact copy. Keep one copy of each.

Keep every other row.

Invoice amounts stay in the currency's smallest unit, in a column named
`amount_minor`, next to the invoice's `currency`.

## Customer crosswalk

`int_customers_matched_to_accounts`: one row per live billing customer, keyed
on `customer_id`, with `account_id`, the CRM account of the same company.

- The only thing billing and the CRM have in common is the customer's email
  address.
- A company can have more than one billing customer; each of them gets the
  company's account.
- A customer whose company has no CRM account keeps its row, with no
  `account_id`.

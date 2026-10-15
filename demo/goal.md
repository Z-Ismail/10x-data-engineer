# Goal: staging models for the demo sources

Build the staging layer for the three demo source systems: billing, CRM and
the product event log. One staging model per source table, with these names
and keys:

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

# Demo sources

`generate.py` writes three small source systems that describe the same
companies, the way a young SaaS business might hold them:

| System | File | Format |
| --- | --- | --- |
| Billing | `data/billing/customers.csv`, `data/billing/invoices.csv` | CSV, amounts in the smallest currency unit, Unix timestamps in UTC |
| CRM | `data/crm/accounts.csv` | CSV, dates as day/month/year text |
| Product | `data/events/events.jsonl` | JSON lines, ISO timestamps |

The three systems agree only on the customer's email address, and they do
not store it the same way. Every company has a domain of its own.

## Planted defects

Every defect is counted in `answers/defects.json`. Defects are allocated to a
fixed share of the companies, with a minimum of one each, so all of them are
present whatever the seed. The smallest supported dataset is 20 companies.

| Defect | Why it matters |
| --- | --- |
| Duplicate billing customers with the same email | Counting customer rows overstates customers |
| Test-mode customers and invoices | Must never count as revenue |
| Yen amounts, which have no smaller unit | Dividing every amount by 100 is wrong |
| Refunds as negative invoices in a later month | Revenue by month must net them off |
| Paying customers missing from the CRM | Unexpected, and worth reporting |
| CRM prospects with no billing record | Expected, and not an error |
| CRM status that disagrees with billing | One system has to win |
| Emails in mixed case or padded with spaces | Exact matching silently drops records |
| CRM accounts that hold another mailbox at the company's domain | Only the domain matches billing |
| Junk user identifiers in events | `null`, `anonymous` and `-1` are not people |
| Duplicate events | The same event delivered twice |

## Answer key

`answers/answers.json` holds the numbers a correct pipeline must reproduce,
such as paid revenue by currency. It is written to a separate folder so it
can be kept away from whatever is being tested.

## Usage

```
python demo/generate.py --seed 42 --companies 200
```

The same seed always gives byte-identical files. Generated data is not
committed.

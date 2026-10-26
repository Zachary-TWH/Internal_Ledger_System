# Internal Ledger System

## Assumptions

- **Money is `BigDecimal`, USD and SGD only.** Both use 2 decimal places, so one scale and one rounding rule cover every amount. Currencies like IDR (large nominal values, effectively no minor unit) or KRW (0 decimals) need per-currency scale and rounding handling, which adds complexity, so they are out of scope.

## Caching (not implemented)

Reading an account's balance and transaction history is a good candidate for caching, since these are read far more often than they change. This is **not implemented** to keep the ledger simple. I would use Redis to store recent reads for a short time, and clear an account's cached data whenever a deposit, withdrawal or transfer changes it. Money-moving code would always read the balance directly from the database, so a stale cached value could never cause an overdraft. The trade-off is that a displayed balance may be a few seconds out of date.

## Month-end reconciliation

Month-end reconciliation compares each account's stored balance against the sum of its ledger entries (credits minus debits) and reports any account where they differ. It is exposed as an API endpoint that takes a month, so it can be triggered on demand. In a real system the same function would be called automatically by a scheduled job at the end of each month. The check only reports discrepancies and never edits balances, since a real error should be fixed with a new, auditable ledger transaction.

**Endpoint:** `GET /reconciliation/month-end?month=YYYY-MM` (optional `only_discrepancies=true` to list only failing accounts). It returns HTTP 200 even when discrepancies exist, so read the `status` field (`RECONCILED` or `DISCREPANCIES_FOUND`). An invalid month returns 400.

**Check 1, accounts.** For every account it returns `opening_balance` (ledger balance before the month), the month's `total_credits` and `total_debits`, `closing_balance` (ledger balance at the end of the month), and the `stored_balance`. The stored balance is compared with the ledger balance over **all** entries, because `accounts.balance` is only a live balance with no month-end snapshot. `difference` is stored minus ledger, so `0.00` means reconciled. For a past month the stored balance will not equal that month's `closing_balance` if later transactions exist, which is expected. The stored balance and the ledger sums are read in a single SQL statement, so one consistent snapshot is compared and a transfer committing mid-report cannot cause a false mismatch.

**Check 2, transactions created in the month.** Each must have exactly one DEBIT and one CREDIT entry. A same-currency transaction needs debit = credit. A cross-currency transaction needs credit = debit x `exchange_rate_used`, rounded to 2 decimals (0.01 tolerance). This replaces a single "total debits equal total credits" check across the whole ledger, because that sum is not equal once a USD to SGD transfer exists: the debit is in USD and the credit is in SGD, so the raw numbers differ by design.

**Assumptions and limits.**
- The EXTERNAL system accounts are listed (`status: NOT_COMPARED`) but not compared, since deposits and withdrawals do not update the EXTERNAL stored balance. Their ledger balance is the net money that entered or left the system.
- `created_at` is stored without a timezone, so month boundaries follow the database server's clock. Run the database in UTC.
- Amounts are returned as 2-decimal strings, not floats, so rounding cannot hide a difference.
- A month that has not ended yet can be reconciled; the response sets `month_has_ended: false`.
- Not implemented: storing each run's result (a `reconciliation_runs` table) for an audit trail, and the scheduled job that calls it on the 1st of each month.

Tests are in `tests/test_month_end_reconciliation.py` and use a temporary SQLite database, so they need no Postgres: `pip install pytest httpx` then `pytest tests/`.

## Credentials: The database password in docker-compose.yml (default postgres, overridable via POSTGRES_PASSWORD) is a demo credential for local use only, so the project runs with no setup. A real deployment would inject secrets from a secrets manager and never commit them.

## On container startup, entrypoint.sh runs create_tables.py, which creates the database tables and seeds an EXTERNAL account for each supported currency. The deposit and withdraw endpoints assume these accounts already exist and return a 500 if one is missing.

# ENV is not exposure but for this assignment purpose was not added to gitignore
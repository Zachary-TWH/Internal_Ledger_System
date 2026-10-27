# Internal Ledger System

## Assumptions

Each assumption below is a rule the ledger follows. Examples use made-up numbers.

**1. Money is an exact decimal, never a float.**
- Python `Decimal` in code, `Numeric(18,2)` in Postgres.
- Example: 0.10 + 0.20 is exactly 0.30, not 0.30000000000000004.
- The API accepts amounts as `Decimal`. Reconciliation returns amounts as strings.

**2. Only USD and SGD are supported, both with 2 decimal places.**
- One scale and one rounding rule (round half up) cover every amount.
- IDR (no usable minor unit) and KRW or JPY (0 decimals) need a per-currency scale. That adds complexity, so they are out of scope.

**3. Each account has one fixed currency.**
- Set at creation, never changed.
- Example: account 3 is SGD, so every entry on it is in SGD.

**4. Every movement is double-entry.**
- One transaction = one DEBIT entry + one CREDIT entry.
- Deposit: debit EXTERNAL, credit the user. Withdrawal: debit the user, credit EXTERNAL.
- Entries and balance updates commit together, or none of them do.

**5. Ledger entries are the source of truth. The stored balance is a fast copy.**
- `accounts.balance` is updated on every posting so reads are cheap.
- Month-end reconciliation proves the two still agree.
- Entries are never edited or deleted. A mistake is fixed with a new transaction.

**6. EXTERNAL accounts represent the outside world.**
- One per currency, created at startup (`is_system = true`).
- Users cannot create them through the API.
- Their stored balance is not maintained. Their ledger balance is the net money that entered or left the system.
- Example: deposit 100 USD, then withdraw 40 USD. EXTERNAL USD ledger balance is -60.

**7. Transfer amount is in the source account's currency.**
- Same currency: destination gets the same amount.
- Different currency: the destination gets the amount times the latest stored rate (source to destination).
- Example: send 100 USD at 1.35. The destination receives 135.00 SGD.
- The rate used is saved on the transaction (`exchange_rate_used`), so later rate changes do not alter history.
- No rate stored for the pair: the transfer is rejected (400).
- Only the direct pair is used. No inverse rates, no triangulation.
- Rates are seeded or inserted by hand. There is no live rate feed.

**8. A cross-currency transfer uses two entries, not four.**
- Debit is in the source currency, credit is in the destination currency.
- So total debits and total credits are not equal across the whole ledger, by design.
- Reconciliation checks each transaction instead: credit = debit x stored rate.
- A production design would route through one FX position account per currency, so each currency nets to zero on its own. See the limitations below.

**9. Overdrafts are not allowed.**
- Withdrawals and transfers are rejected (400) if the source balance is less than the amount.
- The check happens after the account row is locked, so it reads the latest balance.

**10. Concurrent requests are serialised per account.**
- Accounts are loaded with `SELECT ... FOR UPDATE`.
- A transfer locks both accounts in ascending ID order.
- Example: A to B and B to A at the same time both lock the lower ID first, so they cannot deadlock.
- This needs Postgres. SQLite ignores row locks, so concurrency is only meaningful on Postgres.

**11. Idempotency keys are required on every write.**
- Deposit, withdrawal and transfer all take an `idempotency_key`.
- A unique constraint on the key is the final guard.
- A reused key returns 409 and moves no money.
- Example: a client retries `transfer-001` after a timeout. The second call is rejected, so the money moves once.
- Limit: the 409 does not return the original result, and it does not check whether the retry had the same details.

**12. Timestamps are meant to be UTC.**
- `created_at` is stored without a timezone, using the database server clock.
- Run the database in UTC. Month boundaries in reconciliation follow this clock.

**13. No authentication or ownership checks.**
- Anyone who can reach the API can act on any account.
- This is a deliberate simplification for the assignment.

**14. Not supported: reversals.**
- There is no reversal endpoint. A correction today is a new, opposite transaction made by hand.

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
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

Balance and history reads are a good fit for caching. This is **not implemented** to keep the ledger simple.

**1. What would be cached.**
- `GET /accounts/{id}` and `GET /accounts/{id}/transactions`.
- These are read far more often than they change.

**2. Where.**
- Redis, with a short expiry (for example 5 seconds).
- Example key: `balance:42`.

**3. How it stays fresh.**
- **Step 1:** first read of account 42 misses the cache, reads Postgres, stores `balance:42 = 100.00`.
- **Step 2:** a deposit of 50 on account 42 commits.
- **Step 3:** the app deletes `balance:42`.
- **Step 4:** the next read misses, reads 150.00 from Postgres, stores it again.

**4. Money-moving code never reads the cache.**
- Scenario: the cache says 100.00, the real balance is 20.00, and a withdrawal of 80 arrives.
- **Wrong:** read 100.00 from the cache, allow the withdrawal, overdraw the account by 60.
- **Correct:** lock the row and read 20.00 from Postgres, reject with "Insufficient funds".
- So a stale cache can never cause an overdraft.

**5. Trade-off.**
- A displayed balance can be a few seconds out of date.
- At this scale Postgres handles the read load, so the extra moving part is not worth it yet.

## Reversals (not implemented)

There is no reversal endpoint. This is how I would add one.

**1. A reversal is a new transaction. The original is never edited.**
- Add `transactions.type` (`transfer`, `deposit`, `withdrawal`, `reversal`).
- Add `transactions.reversal_of`, a nullable foreign key to the original, with a unique constraint.
- The unique constraint is what stops a second reversal, even under concurrent requests.

**2. It flips the original entries using the original stored amounts and rate.**
- Scenario: A sends 100 USD to B at rate 1.35, so B receives 135.00 SGD. Then the rate changes to 1.40.
- **Wrong:** reverse with today's rate. B pays back 135.00 SGD, A receives 135 / 1.40 = 96.43 USD, and A is short 3.57 USD.
- **Correct:** reverse with the stored amounts. B pays back 135.00 SGD, A receives exactly 100.00 USD.

**3. Guards.**
- Unknown transaction: 404.
- Already reversed: 409 (from the unique constraint).
- Reversing a reversal: 400.
- Recipient already spent the money: 400, balance too low. A real system would use a dispute or compensating process.
- Takes an `idempotency_key` like every other write.

**4. How I would test it.**
- Deposit 100 into A, transfer 40 to B, reverse the transfer. Expect A = 100.00 and B = 0.00.
- Reverse the same transaction again. Expect 409.
- Repeat with a cross-currency transfer and a changed rate. Expect exact original amounts back.

## Total-money integrity check (not implemented)

Month-end reconciliation covers part of this. A full "does all the money add up" check needs a design change first.

**1. Why it cannot be done today.**
- Money must net to zero **per currency**, not across currencies.
- A cross-currency transfer is currently two entries: debit 100 USD, credit 135 SGD.
- USD totals -100 and SGD totals +135. Neither nets to zero.

**2. The fix: one FX account per currency.**
- A cross-currency transfer becomes four entries.
- **USD side:** A -100.00, FX_USD +100.00. Net 0.
- **SGD side:** FX_SGD -135.00, B +135.00. Net 0.
- The FX balances show how much of each currency the system has bought or sold.
- Rounding differences also land in the FX accounts.

**3. The check itself.**
- `GET /integrity` returns, per currency, total debits, total credits and the sum of all balances.
- It also confirms every transaction nets to zero per currency.
- Result is `PASS` or `FAIL` with the numbers.

**4. Trade-off.**
- FX accounts become hot rows under heavy cross-currency load, because every such transfer locks them.
- A fix is to split each FX account into several sub-accounts and sum them.

**5. How I would test it.**
- Healthy data: expect `PASS` in both currencies.
- Change one account balance by hand: expect `FAIL`.

## Month-end reconciliation (implemented)

It compares each account's stored balance with its ledger entries (credits minus debits). It only reports. It never edits balances, because a real error should be fixed with a new, auditable ledger transaction.

**1. Endpoint.**
- `GET /reconciliation/month-end?month=YYYY-MM`
- Optional `only_discrepancies=true` lists only failing accounts.
- Returns HTTP 200 even when discrepancies exist. Read the `status` field: `RECONCILED` or `DISCREPANCIES_FOUND`.
- An invalid month returns 400.
- In a real system a scheduled job would call the same function on the 1st of each month.

**2. Check 1: accounts.**
- Per account it returns `opening_balance`, `total_credits`, `total_debits`, `closing_balance` and `stored_balance`.
- `difference` is stored minus ledger. `0.00` means reconciled.
- Example, account 7 in 2026-10: opening 0.00, credits 100.00, debits 30.00, closing 70.00.
- Stored balance 70.00 gives difference 0.00, status `OK`.
- If someone sets the stored balance to 75.00, the difference is 5.00 and the status is `MISMATCH`.
- The stored balance is compared with the ledger over **all** entries, because `accounts.balance` is a live balance with no month-end snapshot.
- So for a past month, stored will not equal that month's `closing_balance` if later transactions exist. That is expected.

**3. Check 2: transactions created in the month.**
- Each must have exactly one DEBIT and one CREDIT entry.
- Same currency: debit = credit.
- Cross currency: credit = debit x `exchange_rate_used`, rounded to 2 decimals, with 0.01 tolerance.
- Example: debit 100.00 USD at rate 1.35 expects credit 135.00 SGD. A credit of 130.00 is flagged.
- This replaces one "total debits = total credits" check, which fails by design once a USD to SGD transfer exists.

**4. No false alarms from live traffic.**
- Stored balances and ledger sums are read in one SQL statement.
- Postgres gives both the same snapshot, so a transfer committing mid-report cannot cause a false mismatch.

**5. Assumptions and limits.**
- EXTERNAL accounts are listed as `NOT_COMPARED`. Deposits and withdrawals do not update their stored balance. Their ledger balance is the net money that entered or left the system.
- `created_at` has no timezone, so month boundaries follow the database server clock. Run the database in UTC.
- Amounts are returned as 2-decimal strings, not floats, so rounding cannot hide a difference.
- A month that has not ended can be reconciled. The response sets `month_has_ended: false`.
- Not implemented: saving each run in a `reconciliation_runs` table for an audit trail, and the scheduled job.

**6. Tests.**
- File: `tests/test_month_end_reconciliation.py`.
- They use a temporary SQLite database, so no Postgres is needed.
- Run: `pip install pytest httpx`, then `pytest tests/`.

## Running it

**1. Start everything.**
- `docker compose up --build`
- Starts Postgres 16 and the API.

**2. Open the API.**
- API on port 8000. Interactive docs at `/docs`.

**3. Startup setup.**
- `entrypoint.sh` is meant to run `app/create_table.py`.
- It creates the tables and one EXTERNAL account per supported currency (USD, SGD).
- Deposit and withdraw assume those accounts exist. If one is missing they return 500.

## Credentials

**1. Demo password only.**
- `POSTGRES_PASSWORD` comes from the committed `.env` (`mysecret`).
- It is a demo credential for local use, so the project runs with no setup.

**2. Why `.env` is committed.**
- It holds no real secret. It is committed on purpose for this assignment.
- A real deployment would inject secrets from a secrets manager and never commit them.

## Known limitations and next steps

**1. Language.** The brief prefers Java. This is Python (FastAPI, SQLAlchemy, Postgres).
**2. Reversals and the integrity check.** Designed above, not built.
**3. Idempotency.** A reused key returns 409. Better: return the original result, and reject only if the details differ.
**4. Exchange rates.** No live feed, no staleness limit, no inverse or triangulated rates.
**5. Pagination.** Page and offset. Keyset pagination is better for large histories.
**6. Security.** No authentication or account ownership checks.
**7. Operations.** No audit log, monitoring or scheduled reconciliation.

# Internal Ledger System

## Assumptions

Each assumption below is a rule the ledger follows. Examples use made-up numbers.

**1. Money and decimal precision.**
- Money uses exact decimal arithmetic, never binary floating-point.
- Python uses `Decimal` for monetary calculations. PostgreSQL uses `NUMERIC(18,2)` for stored monetary amounts.
- Example: `Decimal("0.10") + Decimal("0.20")` produces exactly `0.30`.
- The API accepts monetary amounts as decimal values, preferably supplied as JSON strings to avoid floating-point conversion before validation.
- Reconciliation responses represent monetary amounts as strings to preserve their exact decimal representation.
- Rounding uses `ROUND_HALF_UP` at explicitly defined calculation or posting boundaries.
- Monetary calculations should retain sufficient precision until the applicable boundary. Amounts must not be rounded arbitrarily at every intermediate step.

**2. Supported currencies.**
- Currently supported currencies are USD and SGD.
- Both use two decimal places, so `NUMERIC(18,2)` is sufficient for stored monetary amounts within the supported range.
- Additional currencies are not currently supported.
- Currencies such as KRW and JPY, which commonly use zero-decimal payment amounts, and IDR, which commonly uses whole-rupiah amounts, require currency-specific precision and validation rules.
- Support can be added by introducing currency-specific metadata and business rules.

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
- Adding an EXTERNAL account for another currency is currently not supported. They are only created at startup for USD and SGD, so a new currency has no EXTERNAL account and deposits and withdrawals in it fail.
- Their stored balance is not maintained. Their ledger balance is the net money that entered or left the system.
- Example: deposit 100 USD, then withdraw 40 USD. EXTERNAL USD ledger balance is -60.

**7. Transfer amount is in the source account's currency.**
- Same currency: destination gets the same amount. The FX service is never called.
- Different currency: the live rate (source to destination) is fetched from the Frankfurter API when the transfer runs, and the destination gets the amount times that rate, rounded to 2 decimals (half up).
- Example: send 100 USD at 1.35. The destination receives 135.00 SGD.
- There is **no exchange-rate table**. Only the rate that was actually executed is stored, on the transaction (`exchange_rate_used`, 6 decimals). The rate used is the rate saved, and later rate moves never alter history. It is also what a reversal should reuse, and what month-end reconciliation checks against.
- **Fail closed.** If the rate cannot be fetched (timeout after 5 seconds, error, malformed or non-positive rate), the cross-currency transfer is rejected with 503 and nothing is recorded. A stale or guessed rate is never used. Same-currency transfers, deposits and withdrawals keep working. Because nothing was recorded, the client can retry with the same idempotency key.
- **The rate is fetched before any row lock is taken** (after the idempotency check). A slow or hanging FX service therefore never holds up other transfers on those accounts. The only thing read before locking is the account currencies, which never change after creation. Balances are only read after the lock.
- Only the direct pair is used. No inverse rates, no triangulation.
- `GET /fx/convert?from_currency=USD&to_currency=SGD&amount=100` is a read-only quote tool. It uses the same fetch and stores nothing.

**Assumption about the FX source.** Frankfurter publishes European Central Bank reference rates, updated once per working day (about 16:00 CET). The service is up around the clock, but the rate is a daily reference rate, not a live market price. On weekends the last working-day rate is returned. It is free and has no uptime guarantee. This is acceptable for this assignment. A real bank would use a paid quote feed with a service-level agreement.

**Not implemented (FX).**
- No quote-then-confirm step. The customer cannot see the rate before the transfer runs, because the rate is chosen at execution. I would add a short-lived quote (id, rate, expiry) that the transfer must reference, so the customer agrees to a specific rate.
- No spread or fee on the rate.
- No fallback provider. A second provider, tried only when the first fails, would raise availability without ever using an old rate.
- No retries. A single 5 second attempt keeps the request short, and the client can retry.
- No check on rate age. Frankfurter returns a `date`, and a production system should reject a rate older than an agreed limit (for example more than 4 days, which still covers a long weekend).

**8. A cross-currency transfer uses two entries, not four.**
- Debit is in the source currency, credit is in the destination currency.
- So total debits and total credits are not equal across the whole ledger, by design.
- Reconciliation checks each transaction instead: credit = debit x the rate saved on that transaction.
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

**12. Timestamps use the database server clock.**
- `created_at` is stored without a timezone.
- Month boundaries in reconciliation follow this clock.

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
- `created_at` has no timezone, so month boundaries follow the database server clock.
- Amounts are returned as 2-decimal strings, not floats, so rounding cannot hide a difference.
- A month that has not ended can be reconciled. The response sets `month_has_ended: false`.
- Not implemented: saving each run in a `reconciliation_runs` table for an audit trail, and the scheduled job.

**6. Tests.**
- **Endpoint tests:** `tests/test_ledger_endpoints.py`. Covers `/deposit`, `/withdraw`, `/transfer`, `GET /accounts/{id}`, the transaction history and the reconciliation endpoint and `/fx/convert`. The FX service is faked in every test (the network is never called), including a failing service (503 and nothing recorded). The real fetch function is tested separately against canned responses: valid, malformed, timeout and HTTP error.
- **Concurrency and rounding tests:** `tests/test_postgres_concurrency.py`. They need real Postgres, because SQLite ignores `SELECT ... FOR UPDATE` and does not round to 2 decimals.
- They cover 20 parallel deposits into one account, 20 parallel withdrawals that must never overdraw, opposite-direction transfers with no deadlock, 10 identical simultaneous requests, and rounding of a converted amount (0.10 USD at 1.35 becomes 0.14 SGD).
- They use a temporary SQLite database, so no Postgres is needed.
- Run the endpoint tests: `pip install pytest httpx`, then `pytest tests/test_ledger_endpoints.py`.
- Run the Postgres tests on their own, against an empty test database (its tables are dropped and recreated):
  `TEST_POSTGRES_URL=postgresql+psycopg://postgres:postgres@localhost:5432/ledger_test pytest tests/test_postgres_concurrency.py`
- Without `TEST_POSTGRES_URL`, the Postgres file is skipped.

**7. CI/CD pipeline.**
- File: `.github/workflows/ci.yml`. Runs on every push to `main` and every pull request.
- **Stage 1, tests:** the endpoint tests on SQLite, then the concurrency and rounding tests against a Postgres service container. Python 3.14.
- **Stage 2, Docker smoke test:** `docker compose up`, wait for `/docs`, create an account, deposit 100. The deposit fails if the EXTERNAL accounts were not seeded.
- **Stage 3, deploy (simulated):** only on `main`, only after stages 1 and 2 pass. It prints what a real deploy would do. There is no real target.

## Running it

**1. Start everything.**
- `docker compose up --build`
- Starts Postgres 16 and the API.

**2. Open the API.**
- API on port 8000. Interactive docs at `/docs`.

**3. Startup setup.**
- On container start, `entrypoint.sh` runs `app/create_table.py` before the API starts.
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
**4. Exchange rates.** Live rate from a free daily-reference service (Frankfurter), fetched per cross-currency transfer. Cross-currency transfers depend on that third party and are refused when it is down. No quote step, no spread, no fallback provider, no rate-age limit, no inverse or triangulated rates. See assumption 7.
**5. Pagination.** Page and offset. Keyset pagination is better for large histories.
**6. Security.** No authentication or account ownership checks.
**7. Operations.** No audit log, monitoring or scheduled reconciliation.

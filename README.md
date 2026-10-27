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
- `POST /accounts` only accepts a supported currency (USD or SGD, case-insensitive) and rejects anything else with 422, because other currencies have no system accounts and could never be funded.
- `GET /accounts/{id}` returns `balance` as a string such as `"60.00"`, so no precision is lost.
- Example: account 3 is SGD, so every entry on it is in SGD.

**4. Every movement is double-entry.**
- Within each currency, a transaction's debits equal its credits. A DEBIT lowers an account's balance and a CREDIT raises it.
- Same currency: two entries. Deposit: debit EXTERNAL, credit the user. Withdrawal: debit the user, credit EXTERNAL. Transfer: debit the source, credit the destination.
- Cross-currency transfer: four entries, through FX_POSITION (see 8).
- Entries and balance updates commit together, or none of them do.

**5. Ledger entries are the source of truth. The stored balance is a fast copy.**
- `accounts.balance` is updated on every posting, for customer accounts and system accounts alike, so reads are cheap.
- Month-end reconciliation runs two checks: each account's stored balance must equal its own credits minus debits, and the balances in each currency must add up to zero (see the month-end section).
- Entries are never edited or deleted. A mistake is fixed with a new transaction.

**6. System accounts: EXTERNAL and FX_POSITION.**
- Two kinds of system account exist, one of each per supported currency, so four in total for USD and SGD. They are created at startup (`is_system = true`).
- Users cannot create them through the API.
- **EXTERNAL** represents the outside world. Deposits and withdrawals go through it.
- **FX_POSITION** represents the bank's own currency-exchange counter. It is the account that both sides of a cross-currency conversion go through, so each currency's ledger can net to zero on its own. It is kept separate from EXTERNAL so EXTERNAL keeps one clear meaning: the net money that entered or left the system from outside.
- Adding system accounts for another currency is currently not supported. They are only created at startup for USD and SGD, so a new currency has no EXTERNAL account and deposits and withdrawals in it fail.
- Their balance is maintained like any other account's. EXTERNAL goes down on a deposit and up on a withdrawal. A positive FX_POSITION balance is the net amount of that currency the bank took in through conversions, and a negative one is the net amount it paid out.
- Example: deposit 100 USD, then withdraw 40 USD. EXTERNAL USD balance is -60, the net money that came in from outside.

**7. Transfer amount is in the source account's currency.**
- Same currency: destination gets the same amount. The FX service is never called.
- Different currency: the live rate (source to destination) is fetched from the Frankfurter API when the transfer runs, and the destination gets the amount times that rate, rounded to 2 decimals (half up).
- Example: send 100 USD at 1.35. The destination receives 135.00 SGD.
- There is **no exchange-rate table**. Only the rate that was actually executed is stored, on the transaction (`exchange_rate_used`, 6 decimals). The rate used is the rate saved, and later rate moves never alter history.
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

**8. A cross-currency transfer uses four entries, through FX_POSITION.**
- Example: Alice sends 50 USD, Bob receives 67.50 SGD at 1.35.
- USD side: debit Alice 50.00, credit FX_POSITION USD 50.00. Net 0.
- SGD side: debit FX_POSITION SGD 67.50, credit Bob 67.50. Net 0.
- So every currency nets to zero on its own, which is what the month-end check relies on.
- Both FX legs use the same rate, so there is no spread and no FX profit. FX_POSITION records the position only.
- The SGD legs both use the already rounded 67.50, so rounding can never leave a currency out of balance.

**9. Overdrafts are not allowed.**
- Withdrawals and transfers are rejected (400) if the source balance is less than the amount.
- The check happens after the account row is locked, so it reads the latest balance.

**10. Concurrent requests are serialised per account.**
- Accounts are loaded with `SELECT ... FOR UPDATE`.
- A transfer locks both accounts in ascending ID order.
- Example: A to B and B to A at the same time both lock the lower ID first, so they cannot deadlock.
- System accounts are locked too, always after the customer rows and, for the two FX_POSITION rows, in ascending ID order. Every request therefore takes its locks in the same order and cannot deadlock.
- Trade-off: every deposit and withdrawal in a currency locks that currency's single EXTERNAL row, and every cross-currency transfer locks both FX_POSITION rows. Those requests run one after another, even for different customers. That is fine at this scale. A fix would be to split each system account into several sub-accounts and sum them.
- This needs Postgres. SQLite ignores row locks, so concurrency is only meaningful on Postgres.

**11. Idempotency keys are required on every write.**
- Deposit, withdrawal and transfer all take an `idempotency_key`.
- A unique constraint on the key is the final guard.
- A reused key returns 409 and moves no money.
- Example: a client retries `transfer-001` after a timeout. The second call is rejected, so the money moves once.
- Two identical requests arriving at the same moment can both pass the up-front check. The unique constraint then rejects the loser, and the API turns that into the same clean 409 (never a 500). `test_identical_simultaneous_requests_move_money_once` checks exactly one 200 and nine 409s.
- Limit: the 409 does not return the original result, and it does not check whether the retry had the same details.

**12. Timestamps use the database server clock.**
- `created_at` is stored without a timezone.
- The month-end report uses it only to say whether the month has ended (`month_has_ended`).

**13. No authentication or ownership checks.**
- Anyone who can reach the API can act on any account.
- This is a deliberate simplification for the assignment.

## Walkthrough: deposit, withdraw, transfer and reversal

This follows one small story through the three tables. Account IDs assume a fresh database: 1 is EXTERNAL SGD, 2 is EXTERNAL USD, 3 is FX_POSITION SGD and 4 is FX_POSITION USD. Alice (5) and Bob (6) hold USD. Carol (7) holds SGD.

**Story**
1. Deposit 100 USD into Alice.
2. Alice withdraws 20 USD.
3. Alice sends 30 USD to Bob (same currency).
4. Alice sends 40 USD to Carol at 1.35, so Carol gets 54.00 SGD (different currencies).
5. Reverse step 4.

**transactions**

| id | type | reversal_of | description | rate |
|----|------|-------------|-------------|------|
| 1 | DEPOSIT | null | Deposit | null |
| 2 | WITHDRAWAL | null | Withdrawal | null |
| 3 | TRANSFER | null | Transfer | null |
| 4 | TRANSFER | null | Transfer | 1.350000 |
| 5 | REVERSAL | 4 | Reversal of transaction 4 | 1.350000 |

**ledger_entries**

| id | txn | acct | entry_type | amount | ccy* |
|----|-----|------|------------|--------|------|
| 1 | 1 | 2 | DEBIT | 100.00 | USD |
| 2 | 1 | 5 | CREDIT | 100.00 | USD |
| 3 | 2 | 5 | DEBIT | 20.00 | USD |
| 4 | 2 | 2 | CREDIT | 20.00 | USD |
| 5 | 3 | 5 | DEBIT | 30.00 | USD |
| 6 | 3 | 6 | CREDIT | 30.00 | USD |
| 7 | 4 | 5 | DEBIT | 40.00 | USD |
| 8 | 4 | 4 | CREDIT | 40.00 | USD |
| 9 | 4 | 3 | DEBIT | 54.00 | SGD |
| 10 | 4 | 7 | CREDIT | 54.00 | SGD |
| 11 | 5 | 5 | CREDIT | 40.00 | USD |
| 12 | 5 | 4 | DEBIT | 40.00 | USD |
| 13 | 5 | 3 | CREDIT | 54.00 | SGD |
| 14 | 5 | 7 | DEBIT | 54.00 | SGD |

*`ccy` is not a real column. It is the currency of the entry's account.

**Balances after each step** (EXTERNAL SGD stays 0.00 and is left out)

| after | EXT USD (2) | FX USD (4) | FX SGD (3) | Alice (5) | Bob (6) | Carol (7) |
|-------|------|------|------|------|------|------|
| start | 0 | 0 | 0 | 0 | 0 | 0 |
| 1 deposit | -100 | 0 | 0 | 100 | 0 | 0 |
| 2 withdraw | -80 | 0 | 0 | 80 | 0 | 0 |
| 3 same-currency transfer | -80 | 0 | 0 | 50 | 30 | 0 |
| 4 FX transfer | -80 | 40 | -54 | 10 | 30 | 54 |
| 5 reversal | -80 | 0 | 0 | 50 | 30 | 0 |

**What to notice**
- **Deposit and withdraw:** two entries each. EXTERNAL USD mirrors the customer (-100, then -80).
- **Same-currency transfer:** two entries, and the rate is null.
- **FX transfer:** four entries, split by currency. USD: 40 out of Alice and into FX USD. SGD: 54 out of FX SGD and into Carol.
- **Reversal (transaction 5):** it copies transaction 4's four entries with DEBIT and CREDIT swapped (entries 11 to 14). It uses the original 40.00 and 54.00, not today's rate.
- **Append-only:** nothing is edited or deleted. Transaction 4 and its entries are untouched. A mistake is fixed by adding a new transaction.
- **Each currency still nets to zero:** USD is -80 + 0 + 50 + 30 = 0. SGD is 0 + 0 + 0 = 0.

**Reversal rules (`POST /transactions/{id}/reverse`)**

| Request | Result | Why |
|---------|--------|-----|
| Reverse transaction 4 | 200 | creates transaction 5 |
| Reverse transaction 4 again | 409 | `reversal_of = 4` already exists, and it is unique |
| Reverse transaction 5 | 400 | its type is REVERSAL |
| Reverse transaction 1 (the 100 USD deposit) | 400 | Alice only has 50.00, so undoing it would take her below zero |
| Reverse transaction 99 | 404 | no such transaction |
| Reuse an idempotency key | 409 | the key was already used |

- **Why `type` and `reversal_of`:** code checks the `type` column, not the free-text description. `reversal_of` is unique, so the database itself blocks a second reversal even if two requests race. A check constraint ties the two together: only a REVERSAL has a `reversal_of`, and every REVERSAL has one.
- **Recipient already spent the money:** the reversal is refused (400). A real system would use a dispute or compensating process, which this API does not provide.
- **Locks:** the original transaction's row is locked, then the accounts in the usual order (customers by id, then system accounts by id), so reversals cannot deadlock with other requests.

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

## Month-end reconciliation (implemented)

Two checks, both against the transaction history. The check only reports. It never edits balances, because a real error should be fixed with a new, auditable ledger transaction. Check 1 answers "accounts are reconciled against transaction history". Check 2 answers "does all the money add up".

**Check 1: each account against its own entries.**
- Ledger balance = sum of the account's CREDIT entries minus the sum of its DEBIT entries.
- It must equal the stored `accounts.balance`. `difference` is stored minus ledger, so 0.00 means reconciled.
- Example: Alice has CREDIT 100 and DEBIT 40, so her ledger balance is 60. If her stored balance is 70, the difference is 10.00 and she is a `MISMATCH`.
- It covers EXTERNAL and FX_POSITION too, because their balances move with every entry.
- Each account also shows the month's `total_credits` and `total_debits`, with `opening_balance` and `closing_balance` taken from the ledger at the start and end of the month.
- The comparison uses all entries, because the stored balance is live and has no month-end snapshot. For a past month, `closing_balance` will not equal the stored balance if later transactions exist. That is expected.

**Check 2: each currency adds up to zero.** The rest of this section describes it.

**1. The idea.**
- Every transaction's entries net to zero within each currency, and every account's balance (including EXTERNAL and FX_POSITION) moves with its entries. So the balances in one currency always add up to 0.00.
- Customers' money is matched by EXTERNAL (net deposits less withdrawals, shown as a negative) and by FX_POSITION (the net amount of that currency bought or sold through conversions).
- Example: Alice deposits 100 USD, withdraws 40, then converts 50 USD to 67.50 SGD for Bob at 1.35.
- USD: Alice +10.00, EXTERNAL USD -60.00, FX_POSITION USD +50.00. Sum 0.00.
- SGD: Bob +67.50, FX_POSITION SGD -67.50, EXTERNAL SGD 0.00. Sum 0.00.
- Without the FX legs the USD sum would be -50.00 and the SGD sum +67.50, so the check could not work. That is why a conversion has four entries (see assumption 8).

**2. Endpoint.**
- `GET /reconciliation/month-end?month=YYYY-MM`
- It returns one row per currency (`customers_total`, `external_total`, `fx_position_total`, `net`, `status` of `OK` or `OUT_OF_BALANCE`), one row per account (check 1), a `summary` (`accounts_checked`, `accounts_mismatched`), an overall `status` of `RECONCILED` or `OUT_OF_BALANCE`, and `month_has_ended`.
- The overall status is `OUT_OF_BALANCE` if any currency does not net to zero or any account is a `MISMATCH`.
- Returns HTTP 200 even when out of balance. Read the `status` field. An invalid month returns 400.
- Example, the data above: USD `net` is `0.00`, SGD `net` is `0.00`, every account is `OK`, overall `RECONCILED`. If someone sets Alice's balance to 60.00 by hand, USD `net` is `50.00`, Alice shows a `difference` of `50.00`, and the status is `OUT_OF_BALANCE`.
- `month` labels the report. Balances are read live when the call is made, so it must be run at month-end close. In a real system a scheduled job would call the same function on the 1st of each month.

**3. No false alarms from live traffic.**
- Every balance and every ledger sum is read in one SQL statement, so Postgres gives them the same snapshot. Both checks are built from that one result. A transfer committing mid-report cannot cause a false result.

**4. Known flaws of this check.**
- **The per-currency sum says that the books are off, not where.** Check 1 names the account (a `MISMATCH` row), but a non-zero `net` on its own does not.
- **Offsetting errors are caught by check 1.** If one account is 10 too high and another is 10 too low, the per-currency sum is still 0, but both accounts show a non-zero `difference`. A test covers this.
- **It does not check each transaction.** It does not verify that every transaction's own entries net to zero, or that the stored rate matches the amounts. A transaction posted with unbalanced entries would also move the balances, so check 1 would still pass. This is not built.
- **It reconciles the current moment, not a past month.** A stored balance has no history. To re-run an old month you need a saved snapshot (see below).
- **It compares the ledger with itself.** It cannot see money that never reached the ledger, or a difference from the real bank balance. That needs an outside statement (see "How a real bank differs").
- **A wrong rate is invisible.** Both FX legs come from the same rate, so they still balance.
- **A database from before this change fails the check.** Old transfers have two entries across currencies, and EXTERNAL's balance was never maintained. Start from a fresh database.
- **Hot rows.** See assumption 10.

**5. Tests.**
- **Endpoint tests:** `tests/test_ledger_endpoints.py`. Covers `/deposit`, `/withdraw`, `/transfer`, `GET /accounts/{id}`, the transaction history, `/fx/convert` and the reconciliation endpoint.
- Reconciliation tests: a worked example that sums to zero in both currencies, an empty ledger, many mixed movements including rounding, a hand-changed balance flagged as `OUT_OF_BALANCE` in one currency only, offsetting errors caught by the per-account check, the per-account stored vs ledger columns, invalid months, and `month_has_ended`.
- System account tests: deposit and withdraw move EXTERNAL, a cross-currency transfer writes four entries and moves FX_POSITION, a same-currency transfer leaves the system accounts alone, missing system accounts return 500 and record nothing, and a customer who names an account "EXTERNAL" is never used as the system account.
- Reversal tests: each endpoint records its transaction type, a same-currency transfer, a deposit, a withdrawal and a cross-currency transfer are reversed back exactly (the FX reversal uses the original 40.00 and 54.00 after the rate moves, with no FX lookup), the reversal's entries mirror the original with DEBIT and CREDIT swapped, nothing existing is edited or deleted, a second reversal gives 409, reversing a reversal gives 400, an unknown transaction gives 404, a reused idempotency key gives 409, a recipient who already spent the money gives 400, the history shows `type` and `reversal_of`, and the database itself rejects a second reversal and mismatched `type` / `reversal_of` values.
- The FX service is faked in every test (the network is never called), including a failing service (503 and nothing recorded). The real fetch function is tested separately against canned responses: valid, malformed, timeout and HTTP error.
- **Concurrency and rounding tests:** `tests/test_postgres_concurrency.py`. They need real Postgres, because SQLite ignores `SELECT ... FOR UPDATE` and does not round to 2 decimals.
- Reversal race tests: 10 parallel reversals of one transaction give exactly one 200 and nine 409s, parallel reversals of conversions mixed with deposits do not deadlock and still sum to zero, and a reversal racing a withdrawal of the same money never leaves the recipient negative (one succeeds, one gets 400).
- They also cover 20 parallel deposits into one account, 20 parallel deposits into different accounts (no lost update on the shared EXTERNAL row), 20 parallel withdrawals that must never overdraw, opposite-direction transfers with no deadlock, opposite-direction conversions mixed with deposits and withdrawals (no deadlock, still sums to zero), 10 identical simultaneous requests, and rounding of a converted amount (0.10 USD at 1.35 becomes 0.14 SGD, and the month-end sum stays 0.00).
- The endpoint tests use a temporary SQLite database, so no Postgres is needed.
- Run the endpoint tests: `pip install pytest httpx`, then `pytest tests/test_ledger_endpoints.py`.
- Run the Postgres tests on their own, against an empty test database (its tables are dropped and recreated):
  `TEST_POSTGRES_URL=postgresql+psycopg://postgres:postgres@localhost:5432/ledger_test pytest tests/test_postgres_concurrency.py`
- Without `TEST_POSTGRES_URL`, the Postgres file is skipped.

**6. CI/CD pipeline.**
- File: `.github/workflows/ci.yml`. Runs on every push to `main` and every pull request.
- **Stage 1, tests:** the endpoint tests on SQLite, then the concurrency and rounding tests against a Postgres service container. Python 3.14.
- **Stage 2, Docker smoke test:** `docker compose up`, wait for `/docs`, create an account, deposit 100. The deposit fails if the system accounts were not seeded.
- **Stage 3, deploy (simulated):** only on `main`, only after stages 1 and 2 pass. It prints what a real deploy would do. There is no real target.


## Monthly balance snapshots (not implemented)

The check reads live balances, so it only says whether the books balance **now**. It cannot reconcile a past month, and no month-end figures are kept. A snapshot saved at each month-end fixes both. This is **not implemented**.

**1. The design.**
- A new table `account_snapshots` with: `account_id`, `month`, `closing_balance`, `last_entry_id`, `taken_at`. One row per account per month, unique on (`account_id`, `month`).
- A scheduled job writes the table straight after the month-end check reports `RECONCILED`.
- `last_entry_id` is the highest ledger entry included, so the cut-off is exact instead of relying on `created_at`, which follows the database server clock and has no timezone.
- Snapshots are write-once. If one looks wrong, that is a reconciliation problem to investigate, not something to overwrite.

**2. What it enables.**
- Reconcile any past month: add up that month's snapshots per currency. The sum must be 0.00.
- A saved closing balance per account per month, to compare with a statement.
- A period can be marked closed once its snapshots exist.

**3. Trade-offs.**
- One more table and a job that must run reliably. A missed month means no snapshot for it, not a wrong answer elsewhere.
- The job must read one consistent view of all balances, as the report does now with a single SQL statement.

**4. How I would test it.**
- Post entries across two months, snapshot the first, change balances in the second. The first month's sum must stay 0.00.
- Change a snapshot row by hand. Expect the sum for that month to fail.
- Run the job twice for the same month. Expect no duplicate rows (unique constraint).



## Credentials

**1. Demo password only.**
- `POSTGRES_PASSWORD` comes from the committed `.env` (`mysecret`).
- It is a demo credential for local use, so the project runs with no setup.

**2. Why `.env` is committed.**
- It holds no real secret. It is committed on purpose for this assignment.
- A real deployment would inject secrets from a secrets manager and never commit them.


# Internal Ledger System

## Assumptions

Each assumption below is a rule the ledger follows. Examples use made-up numbers.

**1. Money and decimal precision.**
- Money uses exact decimal arithmetic, never binary floating-point.
- Python uses `Decimal` for monetary calculations. PostgreSQL uses `NUMERIC(18,2)` for stored monetary amounts.
- Example: `Decimal("0.10") + Decimal("0.20")` produces exactly `0.30`.
- The API accepts monetary amounts as JSON strings (`"10.50"`) or JSON numbers (`10.50`). Both are parsed straight into `Decimal` from the text of the request, never through a float, so `0.1` arrives as exactly `0.1`. Strings are still the safer choice for clients, because their own JSON library may turn a number into a float before sending it.
- No float is used anywhere on the money path. Amounts go from the request to `Decimal`, then to `NUMERIC` in Postgres, then to a 2-decimal string in the response. Integers appear only as ids, page numbers and the starting balance, which is written as `Decimal("0")`.
- Request amounts must be greater than zero, have at most 2 decimal places and fit `NUMERIC(18,2)`. Anything else is rejected with 422, so the amount added to a balance is exactly the amount stored in the ledger entry (example: `10.005` is rejected, not silently rounded).
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
- Every amount in an API response is a 2-decimal string such as `"60.00"` (balances, ledger entry amounts, `new_balance`), and `exchange_rate_used` is a 6-decimal string such as `"1.350000"`. FastAPI would otherwise turn a Decimal into a float (`100.1`), so no precision is lost this way.
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
- `/deposit`, `/withdraw` and `POST /transactions` refuse them (400) as source, destination or target account, so only the code paths that post to them can move them. `POST /transactions` checks this in its early read, before any FX call or row lock. Reads (`GET /accounts/{id}` and its history) stay open, so EXTERNAL and FX_POSITION balances can still be audited.
- **EXTERNAL** represents the outside world. Deposits and withdrawals go through it.
- **FX_POSITION** represents the bank's own currency-exchange counter. It is the account that both sides of a cross-currency conversion go through, so each currency's ledger can net to zero on its own. It is kept separate from EXTERNAL so EXTERNAL keeps one clear meaning: the net money that entered or left the system from outside.
- Adding system accounts for another currency is currently not supported. They are only created at startup for USD and SGD, so a new currency has no EXTERNAL account and deposits and withdrawals in it fail.
- Their balance is maintained like any other account's. EXTERNAL goes down on a deposit and up on a withdrawal. A positive FX_POSITION balance is the net amount of that currency the bank took in through conversions, and a negative one is the net amount it paid out.
- Example: deposit 100 USD, then withdraw 40 USD. EXTERNAL USD balance is -60, the net money that came in from outside.

**7. Transfer amount is in the source account's currency.**
- Same currency: destination gets the same amount. The FX service is never called.
- Different currency: the live rate (source to destination) is fetched from the Frankfurter API when the transfer runs, and the destination gets the amount times that rate, rounded to 2 decimals (half up).
- Example: send 100 USD at 1.35. The destination receives 135.00 SGD.
- **Rounding.** The fetched rate is rounded to 6 decimals (half up), the same scale it is stored at. The converted amount is the exact product (amount x rate) rounded once to 2 decimals (half up), with no rounding in between.
- **Minimum cross-currency transfer: 10 in the source currency** (400 below that, checked before the FX call or any lock). A tiny amount at a low rate could otherwise round to 0.00 and break the ledger's `amount > 0` rule. At 10 or more, the converted amount stays positive for any rate above 0.0005. Same-currency transfers have no minimum beyond 0.01.
- There is **no exchange-rate table**. Only the rate that was actually executed is stored, on the transaction (`exchange_rate_used`, 6 decimals). The rate used is the rate saved, and later rate moves never alter history.
- **Fail closed.** If the rate cannot be fetched (timeout after 5 seconds, error, malformed or non-positive rate), the cross-currency transfer is rejected with 503 and nothing is recorded. A stale or guessed rate is never used. Same-currency transfers, deposits and withdrawals keep working. Because nothing was recorded, the client can retry with the same idempotency key.
- **The rate is fetched before any row lock is taken** (after the idempotency check). A slow or hanging FX service therefore never holds up other transfers on those accounts. The only thing read before locking is the account currencies, which never change after creation. Balances are only read after the lock.
- Only the direct pair is used. No inverse rates, no triangulation.

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
- `POST /transactions` is the transfer: source, destination, amount. The spec does not say how money enters the system, so `POST /deposit` and `POST /withdraw` fund and drain accounts through the EXTERNAL account.
- Deposit, withdrawal and transfer all take an `idempotency_key`.
- A unique constraint on the key is the final guard.
- A reused key returns 409 and moves no money.
- Example: a client retries `transfer-001` after a timeout. The second call is rejected, so the money moves once.
- Two identical requests arriving at the same moment can both pass the up-front check. The unique constraint then rejects the loser, and the API turns that into the same clean 409 (never a 500). `test_identical_simultaneous_requests_move_money_once` checks exactly one 200 and nine 409s.
- Limit: the 409 does not return the original result, and it does not check whether the retry had the same details.

**12. Timestamps use the database server clock.**
- `created_at` is stored without a timezone.
- The month-end report uses it to split each account's entries into before and inside the month (the `opening_balance`, `total_credits`, `total_debits` and `closing_balance` columns) and to set `month_has_ended`. It does not affect the two checks, which always use every entry.
- Because the clock has no timezone, month boundaries follow the database server's clock. Run the database in UTC.

**13. No authentication or ownership checks.**
- Anyone who can reach the API can act on any account.
- This is a deliberate simplification for the assignment.

**14. Minimum amounts.**
- **Same currency (deposit, withdraw, transfer): the smallest amount is 0.01.** Amounts must be greater than zero with at most 2 decimal places, so `0.001`, `0.004` and `10.005` are rejected with 422. There is no other minimum.
- **Cross-currency transfer: the smallest amount is 10 in the source currency.** Anything lower is rejected with 400, before the FX call or any lock.
- Why: a conversion rounds the destination amount to 2 decimals, and a tiny amount can round the wrong way. Example: 0.01 SGD at 0.74 is 0.0074 USD, which rounds up to 0.01 USD, so the recipient would get more than was sent. At the same time, a small amount at a low rate could round to 0.00 and break the ledger's `amount > 0` rule. At 10 or more, the rounding difference is a fraction of a cent and the converted amount always stays positive.
- Example: depositing 0.01 USD works. Sending 0.01 USD to a SGD account is rejected. Sending 10 USD to a SGD account works.

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

Reconciliation is read-only; any discrepancy should be corrected with a new, auditable transaction.

For each account, the report compares the stored balance with credits minus debits and reports the difference. It also checks that all accounts, including the system accounts, net to zero in each currency. The report is built from one SQL statement for a consistent snapshot during live traffic.

Use `GET /reconciliation/month-end?month=YYYY-MM`. The response includes monthly activity, per-account and per-currency results, and an overall `RECONCILED` or `OUT_OF_BALANCE` status (HTTP 200 either way; invalid months return 400). The month parameter labels and groups activity, but balances are live: there are no saved historical snapshots, so this cannot reproduce a past month after later transactions. It also does not validate each transaction's entries or rate, nor compare against an external bank statement.

## Tests and CI

- `tests/test_ledger_endpoints.py` covers the API and business rules using a temporary SQLite database.
- `tests/test_postgres_concurrency.py` covers locking, concurrent requests, and database rounding on PostgreSQL. SQLite cannot verify these behaviors.
- Run the endpoint tests with `pytest tests/test_ledger_endpoints.py`.
- Run the Postgres tests on their own, against an empty test database (its tables are dropped and recreated):
  `TEST_POSTGRES_URL=postgresql+psycopg://postgres:postgres@localhost:5432/ledger_test pytest tests/test_postgres_concurrency.py`

CI (`.github/workflows/ci.yml`) runs both test suites on pushes to `main`, pull requests, or manual dispatch, then builds the Docker Compose stack and performs an API smoke test. The deploy job is simulated and does not deploy anywhere.


## Local configuration

Copy `.env.example` to `.env` and replace the placeholder password with a local-only value. Keep `.env` untracked; it is ignored by Git. Run `docker compose up --build` from the repository root to start the database and API. Docker Compose requires `POSTGRES_PASSWORD` to be set and passes the database URL to the API.

For a local app process outside Docker, set `DATABASE_URL` to a PostgreSQL connection URL in `.env` or the environment. The example uses the same password for both variables; if you change it, keep them in sync. Use a URL-safe password or percent-encode special characters in the connection URL.

These credentials are for local development only. A real deployment should inject secrets through its platform or a secrets manager, not commit them to source control.

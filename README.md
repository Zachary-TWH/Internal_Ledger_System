# Internal Ledger System

A small ledger API for recording money movements between accounts and keeping balances correct. Built with FastAPI, SQLAlchemy and PostgreSQL, and run with Docker Compose.

The core idea: **every movement is a transaction made of balanced DEBIT and CREDIT entries.** Entries are never edited or deleted, so the ledger is the source of truth and the stored account balance is a fast copy of it. A mistake is fixed by posting a new reversal transaction.

## What the system can do

- **Accounts:** create accounts in USD or SGD. Each account has one fixed currency and starts at 0.00.
- **Deposit and withdraw:** money enters and leaves the system through an internal EXTERNAL account. No overdrafts.
- **Transfer:** move money between two accounts, atomically. Across two currencies, the live exchange rate is applied.
- **Reverse:** undo a transaction by posting an offsetting reversal. Nothing is edited or deleted.
- **Query:** read an account's balance and its paginated transaction history.
- **Reconcile:** a month-end report checks every stored balance against the ledger, and checks that each currency adds up to zero.
- **Stay safe under retries and concurrency:** idempotency keys stop a repeated request from moving money twice, and row locks keep simultaneous requests from losing updates or deadlocking.

To run it: `docker compose up --build`. Details are under [Quick start](#quick-start).

## Contents

1. [Requirements checklist](#requirements-checklist)
2. [How it works](#how-it-works)
3. [API at a glance](#api-at-a-glance)
4. [Behaviour](#behaviour)
5. [Walkthrough](#walkthrough-deposit-withdraw-transfer-and-reversal)
6. [Known limitations and how I would solve them](#known-limitations-and-how-i-would-solve-them)
7. [Running and testing it](#running-and-testing-it): [Quick start](#quick-start), [Tests and CI](#tests-and-ci), [Project layout](#project-layout), [Local configuration](#local-configuration)

## Requirements checklist

Where each item of the assignment is covered.

| Requirement | Status | Where |
|-------------|--------|-------|
| `POST /transactions` with source, destination, amount, recorded atomically, returns transaction id | Done | [API at a glance](#api-at-a-glance), behaviour 16 |
| `GET /accounts/{id}` returns the current balance | Done | [API at a glance](#api-at-a-glance) |
| `GET /accounts/{id}/transactions` returns history ordered by timestamp | Done | Newest first by default, `order=asc` for oldest first. Timestamp ties are ordered by entry id |
| Transfer is one atomic operation, rejected on insufficient balance | Done | Behaviours 9 and 16 |
| Transactions can be reversed (posted as a reversal) | Done | Walkthrough |
| Repeated submission does not cause extra transfers | Done | Behaviour 11 |
| Verify total money in the ledger matches the expected sum, report discrepancies | Done | Behaviour 15, check 2 |
| Transactions between different currencies using exchange rates | Done | Behaviours 7 and 8 |
| Simultaneous transfers to the same account are safe | Done | Behaviour 10, Postgres concurrency tests |
| Month-end reconciliation of accounts against transaction history | Done, with a limit: it reads live balances | Behaviour 15, check 1 |
| Explain anything not implemented | Done | [Known limitations](#known-limitations-and-how-i-would-solve-them) |

## How it works

### The three tables

```
accounts 1 ---- * ledger_entries * ---- 1 transactions
```

#### Table 1: `accounts`

One row per account. Includes 4 system accounts (EXTERNAL and FX_POSITION, one of each per currency).

| Column | Type | Null | Key / constraint | Meaning |
|--------|------|------|------------------|---------|
| `id` | Integer | no | primary key | Account id |
| `owner_name` | String | no | | Free text, not unique |
| `currency` | String | no | | USD or SGD, fixed at creation (default USD) |
| `balance` | NUMERIC(18,2) | no | | Stored copy of the ledger (default 0). A customer balance never goes below zero, enforced in the application |
| `is_system` | Boolean | no | | True for EXTERNAL and FX_POSITION, false for customer accounts |

#### Table 2: `transactions`

One row per business event.

| Column | Type | Null | Key / constraint | Meaning |
|--------|------|------|------------------|---------|
| `id` | Integer | no | primary key | Transaction id |
| `idempotency_key` | String | yes (the API always sets it) | unique, indexed | Stops a repeated request from running twice |
| `type` | Enum | no | | DEPOSIT, WITHDRAWAL, TRANSFER or REVERSAL |
| `reversal_of` | Integer | yes | foreign key to `transactions.id`, unique | The transaction a REVERSAL undoes. Unique, so a transaction can be reversed once |
| `description` | String | yes | | Human-readable label, never used for logic |
| `created_at` | DateTime, no timezone | yes | database default `now()` | Database server clock |
| `notes` | String | yes | | Optional free text, no endpoint sets it yet |
| `exchange_rate_used` | NUMERIC(18,6) | yes | | Rate applied to a cross-currency transfer, null otherwise |

Check constraint: only a REVERSAL has a `reversal_of`, and every REVERSAL has one.

#### Table 3: `ledger_entries`

One row per debit or credit posting. The source of truth.

| Column | Type | Null | Key / constraint | Meaning |
|--------|------|------|------------------|---------|
| `id` | Integer | no | primary key | Entry id |
| `transaction_id` | Integer | no | foreign key to `transactions.id` | The transaction this entry belongs to |
| `account_id` | Integer | no | foreign key to `accounts.id` | The account posted to |
| `entry_type` | Enum | no | | DEBIT or CREDIT |
| `amount` | NUMERIC(18,2) | no | check: greater than 0 | Always positive, in the currency of the account. The direction is in `entry_type` |

### What one write does, with numbers

Alice (USD, balance 100.00) transfers 30.00 to Bob (USD, balance 0.00):

1. **Idempotency check.** Has `txn-001` been used? No, so continue.
2. **Cheap checks.** Source differs from destination, both accounts exist, neither is a system account.
3. **FX rate (cross-currency only).** Fetched here, before any lock, so a slow rate service never blocks other transfers.
4. **Lock both accounts**, lower id first (`SELECT ... FOR UPDATE`).
5. **Check funds.** 100.00 is at least 30.00, so continue.
6. **Insert** the transaction row and its entries: DEBIT Alice 30.00, CREDIT Bob 30.00.
7. **Update balances:** Alice 70.00, Bob 30.00.
8. **Commit.** All of it, or none of it. A duplicate-key clash at the commit becomes a 409 (two identical requests racing).

## API at a glance

Every write takes a unique `idempotency_key`. Amounts are sent as strings such as `"10.50"` (numbers also work) and come back as 2-decimal strings.

| Method and path | What it does | Success response |
|-----------------|--------------|------------------|
| `POST /accounts` | Create a customer account (`owner_name`, `currency` USD or SGD) | `id`, `owner_name`, `currency`, `balance` |
| `POST /deposit` | Money in from EXTERNAL (`account_id`, `amount`, `idempotency_key`) | `transaction_id`, `new_balance` |
| `POST /withdraw` | Money out to EXTERNAL, no overdraft | `transaction_id`, `new_balance` |
| `POST /transactions` | Transfer (`from_account_id`, `to_account_id`, `amount`, `idempotency_key`). `amount` is in the source currency | `transaction_id`, `exchange_rate_used`, both new balances |
| `POST /transactions/{id}/reverse` | Undo a transaction (`idempotency_key`) | `transaction_id`, `reversal_of`, the offsetting `entries` |
| `GET /accounts/{id}` | Current balance | account |
| `GET /accounts/{id}/transactions` | Ledger entries, `page`, `page_size` (1 to 100), `order` (`asc` or `desc`) | `results` list with `transaction_id`, `type`, `entry_type`, `amount`, `description`, `created_at` |
| `GET /reconciliation/month-end?month=YYYY-MM` | Read-only two-part reconciliation report | per-currency and per-account results, overall `status` |

**Status codes**

| Code | Meaning |
|------|---------|
| 200 | Done |
| 400 | A business rule refused it: insufficient funds, system account used, same source and destination, below the cross-currency minimum, reversal not allowed |
| 404 | Account or transaction not found |
| 409 | Two identical requests raced and this one lost, or transaction already reversed |
| 422 | Invalid body: amount not above zero, more than 2 decimals, missing field, unsupported currency |
| 503 | Cross-currency transfer and the exchange rate is unavailable. Nothing is recorded |
| 500 | A system account is missing (setup problem) |

## Behaviour

Each item below is a rule the ledger follows. Examples use made-up numbers.

**1. Money and decimal precision.**
- Use Python `Decimal` and PostgreSQL `NUMERIC(18,2)` for money; amounts are returned as decimal strings.
- Requests require positive amounts with at most two decimal places. Invalid precision or values outside the database range return 422.
- FX rates use six decimals; converted amounts are rounded once to two decimals using `ROUND_HALF_UP`.

**2. Supported currencies.**
- USD and SGD are supported, both with two decimal places. Adding currencies with different precision requires currency-specific metadata and validation.

**3. Each account has one fixed currency.**
- Currency is chosen at account creation and cannot be changed. Unsupported currencies return 422 because they have no system accounts.
- Every account entry uses that account's currency; monetary amounts and exchange rates are returned as two- and six-decimal strings, respectively.

**4. Every movement is double-entry.**
- Debits equal credits within each currency. Deposits, withdrawals and same-currency transfers create two entries; cross-currency transfers create four through FX_POSITION.

**5. Ledger entries are the source of truth. The stored balance is a fast copy.**
- `accounts.balance` is updated with every posting for fast reads; reconciliation compares it with append-only ledger entries. Errors are corrected with new transactions, never edits or deletions.

**6. System accounts: EXTERNAL and FX_POSITION.**
- Startup creates one EXTERNAL and one FX_POSITION account per supported currency. Customers cannot create or use system accounts directly, but can read their balances and history.
- Deposits and withdrawals post through EXTERNAL. FX_POSITION balances cross-currency legs so each currency balances independently; EXTERNAL tracks net funds entering or leaving the system.

**7. Transfer amount is in the source account's currency.**
- Same-currency transfers do not call the FX service. Cross-currency transfers use the live direct-pair Frankfurter rate, stored to six decimals on the transaction; the converted amount is recorded in the ledger entries.
- Rates are fetched before account locks. A timeout (5 seconds), invalid rate or service error returns 503 without recording anything; no stale or guessed rate is used.
- There is no quote-and-confirm, retry, or rate-age check. A production system should use an agreed rate source, short-lived customer-approved quotes and a maximum rate age.

**8. A cross-currency transfer uses four entries, through FX_POSITION.**
- Sending 50 USD for 67.50 SGD debits the sender and credits FX_POSITION USD, then debits FX_POSITION SGD and credits the recipient. Each currency balances independently using the same rounded amounts.

**9. Overdrafts are not allowed.**
- Withdrawals and transfers with insufficient funds return 400; the balance is checked after locking the account.

**10. Concurrent requests are serialised per account.**
- PostgreSQL row locks serialize updates. Transfers lock accounts by ascending ID (customers before system accounts), preventing lost updates and deadlocks.
- Shared EXTERNAL and FX_POSITION rows can limit throughput; splitting them into sub-accounts is a future scaling option.

**11. Idempotency keys are required on every write.**
- Every write requires a unique `idempotency_key`. Replays return the original transaction ID with `already_processed` and do not move money; concurrent duplicate requests are protected by the database unique constraint.
- The replay response does not include the full original response or verify that the retry has identical details.

**12. Timestamps use the database server clock.**
- `created_at` has no timezone; month boundaries follow the database clock, so run PostgreSQL in UTC.

**13. No authentication or ownership checks.**
- Anyone who can reach the API can act on any account; authentication and ownership checks are out of scope.

**14. Minimum amounts.**
- Same-currency writes accept amounts from 0.01; USD/SGD cross-currency transfers require at least 10 source-currency units to avoid zero or misleading results after rounding. Amounts with more than two decimals return 422.

**15. Month-end reconciliation compares every balance with the ledger.**
- `GET /reconciliation/month-end?month=YYYY-MM` checks stored balances against each account's credits minus debits, and verifies that balances sum to zero per currency. The response is read-only and returns `RECONCILED` or `OUT_OF_BALANCE` (HTTP 200); invalid months return 400.
- All checks use one database snapshot, but balances are live: past months cannot be reproduced, and each call scans the ledger history. It also cannot detect off-ledger discrepancies or confirm each transaction's entries and exchange rate.

**16. Every write is atomic: all of it is saved, or none of it.**
- Account creation, deposits, withdrawals, transfers and reversals each commit once. Failures before commit roll back the transaction, entries and balance updates together.
- The transaction row is flushed first to obtain its ID and catch duplicate keys; `flush` is not a commit.
- Example from `deposit` in `app/main.py` (other writes follow the same pattern):

```python
db.add(txn)
flush_or_409(db)               # INSERT sent now to get txn.id and test the unique key. Not committed.
db.add_all([debit_entry, credit_entry])
account.balance += req.amount
external.balance -= req.amount
commit_or_409(db)              # the one COMMIT: transaction, entries and balances saved together
```

## Walkthrough: deposit, withdraw, transfer and reversal

This walkthrough follows one example through the three tables. Account IDs assume a fresh database: 1 is EXTERNAL SGD, 2 is EXTERNAL USD, 3 is FX_POSITION SGD and 4 is FX_POSITION USD. Alice (5) and Bob (6) hold USD. Carol (7) holds SGD.

**Steps**
1. Deposit 100 USD into Alice.
2. Alice withdraws 20 USD.
3. Alice sends 30 USD to Bob (same currency).
4. Alice sends 40 USD to Carol at 1.35, so Carol gets 54.00 SGD (different currencies).
5. Reverse step 4.

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

The entries show the core rules: the FX transfer balances each currency through FX_POSITION, and its reversal swaps the original debit/credit directions using the original amounts and rate. Transactions and entries remain append-only; each transaction can be reversed at most once, and reversal is refused if it would overdraw an account.

## Known limitations and how I would solve them

This is an assignment rather than a production system. The table summarizes the main gaps and how I would address them.

| Limitation | Fix I would make |
|------------|------------------|
| A duplicate request returns only the original transaction id, not the full response, and a retry with different details is not detected | Store the response and a hash of the request body with the key. Return the stored response when the hash matches, and a 422 when it does not |
| Month-end reads live balances, so a past month cannot be reproduced. It also sums the whole ledger on every call, so it gets slower as history grows | I would add a monthly snapshot. A write-once `account_snapshots` table (`account_id`, `month`, `closing_balance`, `last_entry_id`), written by a scheduled job right after the check reports `RECONCILED`. Summing one month's snapshots per currency must give 0.00. The report would then start from the last snapshot (its `closing_balance` is the opening balance) and sum only the entries after its `last_entry_id`, so the cost depends on one month of activity and not on the whole history |
| Reconciliation does not check each transaction | One query grouping entries by transaction and currency, flagging any group where debits differ from credits |
| Reconciliation compares the ledger with itself | Import the real bank or custodian statement and compare its totals with EXTERNAL |
| EXTERNAL and FX_POSITION are hot rows, so deposits, withdrawals and conversions in one currency run one after another | Split each system account into several sub-accounts and sum them |
| FX uses a daily Frankfurter/ECB reference rate, with no quote step, fallback provider or age check | A paid quote feed, a short-lived quote the transfer must reference, a second provider, and rejection of rates older than an agreed limit (behaviour 7) |
| A reversal is refused when the recipient already spent the money | A dispute or compensating-transfer process |
| No authentication or ownership checks | Authenticate callers and check that they own the source account |
| `created_at` has no timezone and uses the database server clock | `timestamptz`, stored in UTC |
| Customer balance cannot go below zero, and every transaction's entries balance, but only the application enforces both | A `CHECK (balance >= 0 OR is_system)` constraint, and a deferred constraint trigger that checks debits equal credits per transaction and currency |
| Tables come from `create_all`, which never alters an existing table | Schema migrations with Alembic |
| Only USD and SGD | Currency metadata (decimal places, rounding) and system accounts per new currency (behaviour 2) |
| Caching is not implemented; repeated reads of popular balances and transaction histories can significantly increase database load | Cache these reads in Redis with a short expiry and invalidate affected entries after successful writes. Treat cached balances as display-only; money-moving requests must always check PostgreSQL under a row lock |

**How I would test the main fixes**
- **Idempotent replay:** send the same request twice, expect the same body and one transaction. Send it with a different amount, expect a 422.
- **Snapshots:** post entries across two months, snapshot the first, change balances in the second, and expect the first month to still sum to 0.00. Edit a snapshot row by hand and expect the check to fail.
- **Per-transaction check:** insert an unbalanced transaction directly in the database and expect it to be flagged.
- **Sub-accounts:** run 20 parallel deposits in one currency and compare total time and the summed balance before and after.

## Running and testing it

### Quick start

**Needs:** Docker with Compose. Cross-currency transfers also need internet access, because the exchange rate comes from the Frankfurter API (without it they return 503, everything else still works).

**1. Start the stack.** The repository includes a `.env` with a demo database password (see [Local configuration](#local-configuration)), so there is nothing to set up first.

```
docker compose up --build
```

This starts PostgreSQL and the API. On start-up the API creates the tables and the 4 system accounts (safe to re-run). The API is at `http://localhost:8000`, and interactive docs are at `http://localhost:8000/docs`.

**2. Try it.** On a fresh database, ids 1 to 4 are the system accounts, so the first customer account is 5.

```
# Alice (USD, id 5), Bob (USD, id 6), Carol (SGD, id 7)
curl -s -X POST localhost:8000/accounts -H 'Content-Type: application/json' -d '{"owner_name":"Alice","currency":"USD"}'
curl -s -X POST localhost:8000/accounts -H 'Content-Type: application/json' -d '{"owner_name":"Bob","currency":"USD"}'
curl -s -X POST localhost:8000/accounts -H 'Content-Type: application/json' -d '{"owner_name":"Carol","currency":"SGD"}'

# deposit 100.00 USD into Alice
curl -s -X POST localhost:8000/deposit -H 'Content-Type: application/json' \
  -d '{"account_id":5,"amount":"100.00","idempotency_key":"dep-001"}'

# transfer 30.00 USD from Alice to Bob
curl -s -X POST localhost:8000/transactions -H 'Content-Type: application/json' \
  -d '{"from_account_id":5,"to_account_id":6,"amount":"30.00","idempotency_key":"txn-001"}'

# transfer 40.00 USD from Alice to Carol (USD to SGD, live rate)
curl -s -X POST localhost:8000/transactions -H 'Content-Type: application/json' \
  -d '{"from_account_id":5,"to_account_id":7,"amount":"40.00","idempotency_key":"txn-002"}'

# reverse the Bob transfer (transaction 2; the deposit was transaction 1)
curl -s -X POST localhost:8000/transactions/2/reverse -H 'Content-Type: application/json' \
  -d '{"idempotency_key":"rev-001"}'

# balance, history, and the month-end report (use the current month)
curl -s localhost:8000/accounts/5
curl -s localhost:8000/accounts/5/transactions
curl -s "localhost:8000/reconciliation/month-end?month=2026-10"
```

Repeat the first deposit command unchanged. You get a 200 with the first deposit's `transaction_id` and `status: already_processed`, and the balance does not change: that is the idempotency key doing its job.

**Reset to an empty database:** `docker compose down -v`.

**Without Docker:** install `requirements.txt`, set `DATABASE_URL` to a PostgreSQL URL, run `python -m app.create_table` once, then `uvicorn app.main:app`. See [Local configuration](#local-configuration).

### Tests and CI

- `tests/test_ledger_endpoints.py` (38 test functions, 45 test cases once the parametrised tests are expanded) covers the API and business rules using a temporary SQLite database. It includes reconciliation, reversals, system accounts and a faked FX service (the network is never called in tests).
- `tests/test_postgres_concurrency.py` (11 test functions, 12 test cases) covers locking, concurrent requests, and database rounding on PostgreSQL. SQLite cannot verify these behaviors. Examples: 20 parallel deposits into one account lose no update, 20 parallel withdrawals never overdraw, opposite-direction transfers do not deadlock, and 10 identical simultaneous requests move money once.
- Run the endpoint tests with `pytest tests/test_ledger_endpoints.py`.
- Run the Postgres tests on their own, against an empty test database (its tables are dropped and recreated). With the Compose database running (`docker compose up -d db`), use the demo password from `.env`:
  `TEST_POSTGRES_URL=postgresql+psycopg://postgres:mysecret@localhost:5432/ledger_test pytest tests/test_postgres_concurrency.py`
- Compose creates an empty `ledger_test` database next to `ledger` the first time the database volume is created (`db/init-test-db.sql`). If your volume already existed, create it once with `docker compose exec db psql -U postgres -c "CREATE DATABASE ledger_test;"`. CI does not use this: it starts its own Postgres container with the password `postgres`.
- Without `TEST_POSTGRES_URL`, the Postgres file is skipped.

CI (`.github/workflows/ci.yml`) runs both test suites on pushes to `main`, pull requests, or manual dispatch, then builds the Docker Compose stack and performs an API smoke test. The deploy job is simulated and does not deploy anywhere.

### Project layout

```
app/main.py            API endpoints and business rules
app/models.py          the three tables (accounts, transactions, ledger_entries)
app/database.py        database connection and session
app/create_table.py    creates the tables and the 4 system accounts (run by entrypoint.sh)
tests/                 endpoint tests (SQLite) and concurrency tests (Postgres)
Dockerfile, docker-compose.yml, entrypoint.sh   run the API and PostgreSQL
.github/workflows/ci.yml                        tests and Docker smoke test
```

### Local configuration

The `.env` file is committed on purpose, so the project runs with no setup. It holds a demo credential only:

```
DATABASE_URL=postgresql://postgres:mysecret@localhost:5432/ledger
POSTGRES_PASSWORD=mysecret
```

- `POSTGRES_PASSWORD` is the password of the `postgres` user. Docker Compose uses it for the database and for the API's connection (inside Compose, the API connects to the `db` service, not `localhost`).
- `DATABASE_URL` is used when running the API outside Docker. It points at a local PostgreSQL database named `ledger`.
- Run `docker compose up --build` to start the stack.

A real deployment would never commit a password. It would inject secrets through its platform or a secrets manager.

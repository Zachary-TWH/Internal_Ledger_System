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
3. [Problems this design solves](#problems-this-design-solves)
4. [API at a glance](#api-at-a-glance)
5. [Behaviour](#behaviour)
6. [Walkthrough](#walkthrough-deposit-withdraw-transfer-and-reversal)
7. [Known limitations and how I would solve them](#known-limitations-and-how-i-would-solve-them) (includes caching)
8. [Running and testing it](#running-and-testing-it): [Quick start](#quick-start), [Tests and CI](#tests-and-ci), [Project layout](#project-layout), [Local configuration](#local-configuration)

## Requirements checklist

Where each item of the assignment is covered.

| Requirement | Status | Where |
|-------------|--------|-------|
| `POST /transactions` with source, destination, amount, recorded atomically, returns transaction id | Done | [API at a glance](#api-at-a-glance), behaviour 4 |
| `GET /accounts/{id}` returns the current balance | Done | [API at a glance](#api-at-a-glance) |
| `GET /accounts/{id}/transactions` returns history ordered by timestamp | Done | Newest first by default, `order=asc` for oldest first. Ties broken by entry id so pages never overlap |
| Transfer is one atomic operation, rejected on insufficient balance | Done | Behaviours 9 and 16 |
| Transactions can be reversed (posted as a reversal) | Done | Walkthrough, "Reversal rules" |
| Repeated submission does not cause extra transfers | Done | Behaviour 11 |
| Verify total money in the ledger matches the expected sum, report discrepancies | Done | Behaviour 15, check 2 |
| Transactions between different currencies using exchange rates | Done | Behaviours 7 and 8 |
| Simultaneous transfers to the same account are safe | Done | Behaviour 10, Postgres concurrency tests |
| Month-end reconciliation of accounts against transaction history | Done, with a limit: it reads live balances | Behaviour 15, check 1 |
| Explain anything not implemented | Done | Known limitations, Caching |

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
8. **Commit.** All of it, or none of it. A duplicate-key clash at the commit becomes a 409.

### Key design decisions

| Decision | Why | Trade-off |
|----------|-----|-----------|
| Double-entry, append-only entries | Every movement balances and is auditable. Fixes are new transactions | More rows per movement |
| Stored `balance` as a copy of the ledger | Reads are cheap | Two things to keep equal, so reconciliation checks them |
| Unique `idempotency_key` plus an up-front check | A retry cannot move money twice, even when two copies race | The duplicate gets a 409, not the original result |
| Row locks, always taken in the same order | No lost updates and no deadlocks | System accounts are hot rows (behaviour 10) |
| EXTERNAL and FX_POSITION system accounts | Each currency nets to zero by itself, so one check covers everything | Four extra rows to lock |
| Rate fetched at execution and stored on the transaction | History never changes when rates move | No quote step for the customer |
| `Decimal` and `NUMERIC(18,2)` everywhere on the money path | No floating-point error | Only 2-decimal currencies |
| Reversal is a new transaction with `reversal_of` unique | The database itself blocks a second reversal, even under a race | Refused when the recipient has already spent the money |

## Problems this design solves

Each item is a problem that money systems hit, what a simple approach would do, and what this ledger does instead.

**1. Two withdrawals at the same moment.** Account balance 100.00, two withdrawals of 80.00.
- **Simple approach:** both read 100.00, both pass the check, and the balance ends at -60.00.
- **This ledger:** the account row is locked (`SELECT ... FOR UPDATE`). The second request waits, reads 20.00 and is rejected with "Insufficient funds" (behaviours 9 and 10).

**2. A client retries after a timeout.** Transfer `txn-001` succeeded, but the response was lost.
- **Simple approach:** the retry moves the money a second time.
- **This ledger:** the unique `idempotency_key` rejects the retry with a 409 and the balance does not change. Two identical requests arriving together are stopped by the same constraint, so the loser gets a 409, not a 500 (behaviour 11).

**3. Rounding.** `0.1 + 0.2`.
- **Simple approach:** floats give `0.30000000000000004`, and errors build up over many transactions.
- **This ledger:** `Decimal` in Python and `NUMERIC(18,2)` in Postgres give exactly `0.30`. An amount like `10.005` is rejected, not silently rounded (behaviour 1).

**4. A conversion that does not balance.** Alice sends 50 USD and Bob receives 67.50 SGD.
- **Simple approach:** two entries, -50 USD on Alice and +67.50 SGD on Bob. USD now sums to -50 and SGD to +67.50, so no currency adds up to zero and the totals cannot be checked.
- **This ledger:** four entries through FX_POSITION, so USD nets to 0 and SGD nets to 0 on their own (behaviour 8).

**5. Fixing a mistake.** A transfer was wrong.
- **Simple approach:** edit or delete the row. The history is lost and stored balances may stop matching the entries.
- **This ledger:** post a new REVERSAL transaction with the entries swapped. The original stays untouched, and the unique `reversal_of` stops a second reversal even under a race (walkthrough, "Reversal rules").

**6. Deadlock.** Transfers A to B and B to A at the same time.
- **Simple approach:** each locks its own source account first. A holds 5 and waits for 6, B holds 6 and waits for 5, and both hang.
- **This ledger:** every request locks accounts in ascending id order, so both lock 5 first and one waits (behaviour 10).

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

`POST /transactions` is the transfer asked for in the brief. The brief does not say how money enters the system, so `/deposit` and `/withdraw` were added for that (behaviour 11).

**Status codes**

| Code | Meaning |
|------|---------|
| 200 | Done |
| 400 | A business rule refused it: insufficient funds, system account used, same source and destination, below the cross-currency minimum, reversal not allowed |
| 404 | Account or transaction not found |
| 409 | Duplicate `idempotency_key`, or transaction already reversed |
| 422 | Invalid body: amount not above zero, more than 2 decimals, missing field, unsupported currency |
| 503 | Cross-currency transfer and the exchange rate is unavailable. Nothing is recorded |
| 500 | A system account is missing (setup problem) |

## Behaviour

Each item below is a rule the ledger follows. Examples use made-up numbers.

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
- Entries and balance updates commit together, or none of them do (see 16).

**5. Ledger entries are the source of truth. The stored balance is a fast copy.**
- `accounts.balance` is updated on every posting, for customer accounts and system accounts alike, so reads are cheap.
- Month-end reconciliation runs two checks: each account's stored balance must equal its own credits minus debits, and the balances in each currency must add up to zero (see 15).
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
- **Rounding.** The fetched rate is rounded to 6 decimals (half up), the same scale it is stored at. The converted amount is the exact product (amount x rate) rounded once to 2 decimals (half up), with no rounding in between.
- **Minimum cross-currency transfer: 10 in the source currency for USD/SGD and SGD/USD** (the only supported cross-currency pairs; amounts below 10 are rejected with 400 before the FX call or any lock). A tiny amount at a low rate could otherwise round to 0.00 and break the ledger's `amount > 0` rule. At 10 or more, the converted amount stays positive when the fetched rate is above 0.0005. Same-currency transfers have no minimum beyond 0.01.
- There is **no exchange-rate table**. Only the rate that was actually executed is stored, on the transaction (`exchange_rate_used`, 6 decimals). The rate used is the rate saved, and later rate moves never alter history.
- **Fail closed.** If the rate cannot be fetched (timeout after 5 seconds, error, malformed or non-positive rate), the cross-currency transfer is rejected with 503 and nothing is recorded. A stale or guessed rate is never used. Same-currency transfers, deposits and withdrawals keep working. Because nothing was recorded, the client can retry with the same idempotency key.
- **The rate is fetched before any row lock is taken** (after the idempotency check). A slow or hanging FX service therefore never holds up other transfers on those accounts. The only thing read before locking is the account currencies, which never change after creation. Balances are only read after the lock.
- Only the direct pair is used. No inverse rates, no triangulation.

**FX service assumption.** The rate comes from an internal rates API, as it would in a real system. Frankfurter is used here only as a stand-in. That API's uptime is the owning team's concern, so the ledger assumes it is available close to 100% of the time. If a call does fail, the transfer fails closed (503) and nothing is recorded.

**Spread and margin assumption.** The rate returned by that internal API is assumed to already include any spread or margin, agreed with the treasury team. The ledger applies the rate as given.

**Not implemented (FX).**
- No quote-then-confirm step. The customer cannot see the rate before the transfer runs, because the rate is chosen at execution. I would add a short-lived quote (id, rate, expiry) that the transfer must reference, so the customer agrees to a specific rate.
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

**11. Idempotency keys are required on every write.**
- `POST /transactions` is the transfer: source, destination, and amount. `POST /deposit` and `POST /withdraw` fund and drain accounts through the EXTERNAL account.
- Deposit, withdrawal, reversal and transfer all take an `idempotency_key`.
- The key is saved on the transaction. The database column has a unique constraint, a rule that no two rows can hold the same key.
- A reused key returns 409 and moves no money.
- Example: a client retries `transfer-001` after a timeout. The second call is rejected, so the money moves once.
- Two identical requests arriving at the same moment can both pass the up-front check. The unique constraint then rejects the loser, and the API turns that into the same clean 409 (never a 500).
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
- **Cross-currency transfer: the smallest amount is 10 in the source currency for USD/SGD and SGD/USD, the only supported cross-currency pairs.** Anything lower is rejected with 400, before the FX call or any lock.
- Why: a conversion rounds the destination amount to 2 decimals, and a tiny amount can round the wrong way. Example: 0.01 SGD at 0.74 is 0.0074 USD, which rounds up to 0.01 USD, so the recipient would get more than was sent. At the same time, a small amount at a low rate could round to 0.00 and break the ledger's `amount > 0` rule. For either supported pair, an amount of 10 or more converts to a positive amount when the fetched rate is above 0.0005.
- Example: depositing 0.01 USD works. Sending 0.01 USD to a SGD account is rejected. Sending 10 USD to a SGD account works.

**15. Month-end reconciliation compares every balance with the ledger.**
- `GET /reconciliation/month-end?month=YYYY-MM` is read-only. Any discrepancy should be corrected with a new, auditable transaction. Two checks run, and both use every ledger entry.
- **Check 1: each account against its own entries.** Ledger balance = the account's CREDIT entries minus its DEBIT entries. It must equal the stored `accounts.balance`. `difference` is stored minus ledger, so `0.00` means reconciled. System accounts are included, because their balances move with every entry.
- Example: Alice has CREDIT 100 and DEBIT 40, so her ledger balance is 60. If her stored balance is 70, the difference is `10.00` and she is a `MISMATCH`.
- **Check 2: each currency adds up to zero.** Every transaction's entries net to zero within each currency, so the stored balances in one currency (customers, EXTERNAL and FX_POSITION) must sum to `0.00`.
- Example: Alice deposits 100 USD, withdraws 40, then converts 50 USD to 67.50 SGD for Bob. USD: Alice +10.00, EXTERNAL -60.00, FX_POSITION +50.00, sum 0.00. SGD: Bob +67.50, FX_POSITION -67.50, sum 0.00. Without the FX legs (behaviour 8), USD would sum to -50.00 and SGD to +67.50.
- **The response** has one row per currency (`customers_total`, `external_total`, `fx_position_total`, `net`, `status`), one row per account (stored, ledger, difference, status, plus the month's opening, credits, debits and closing), a `summary`, `month_has_ended`, and an overall `status` of `RECONCILED` or `OUT_OF_BALANCE`. HTTP 200 either way, so read `status`. An invalid month returns 400.
- **Consistent under live traffic.** Every balance and ledger sum is read in one SQL statement, so Postgres gives them the same snapshot. A transfer committing mid-report cannot cause a false result.
- **Limits** (fixes are in the limitations table below):
  - The `month` parameter labels and groups activity, but balances are live. There are no saved snapshots, so it cannot reproduce a past month after later transactions.
  - It does not check that each transaction's own entries balance, or that the stored rate matches the amounts.
  - It compares the ledger with itself. It cannot see money that never reached the ledger, or a difference from a real bank statement.
  - Offsetting errors (one account 10 too high, another 10 too low) still sum to zero in check 2, but check 1 flags both accounts.

**16. Every write is atomic: all of it is saved, or none of it.**
- This covers every write endpoint: account creation, deposit, withdrawal, transfer and reversal. Each request runs in one database transaction and calls `commit()` once, at the end.
- `db.flush()` sends the transaction row's INSERT to Postgres early, so the row gets its id and the unique `idempotency_key` check fires straight away (`flush_or_409`). A flush is not a commit: other requests cannot see the row, and it is undone if the request fails later.
- The ledger entries and balance updates are sent together with the final commit.
- Any failure before the commit (insufficient funds, a duplicate key, a crash) rolls everything back, including what was flushed. `get_db` also closes the session in its `finally` block, which discards anything uncommitted.
- The funds check runs after the accounts are locked and before anything is written (see 9 and 10), so a rejected request writes nothing.
- From `deposit` in `app/main.py` (the other writes follow the same shape):

```python
db.add(txn)
flush_or_409(db)               # INSERT sent now to get txn.id and test the unique key. Not committed.
db.add_all([debit_entry, credit_entry])
account.balance += req.amount
external.balance -= req.amount
commit_or_409(db)              # the one COMMIT: transaction, entries and balances saved together
```

- Example: Alice sends 30.00 to Bob. If posting Bob's credit fails, Alice is not debited and no entry exists.

## Walkthrough: deposit, withdraw, transfer and reversal

This walkthrough follows one example through the three tables. Account IDs assume a fresh database: 1 is EXTERNAL SGD, 2 is EXTERNAL USD, 3 is FX_POSITION SGD and 4 is FX_POSITION USD. Alice (5) and Bob (6) hold USD. Carol (7) holds SGD.

**Steps**
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

## Known limitations and how I would solve them

**Out of scope on purpose.** This is an assignment, not a production system, so these are left out deliberately: authentication and ownership checks, schema migrations, caching, currencies beyond USD and SGD, a paid or redundant FX feed, and scheduled jobs. The table below says how I would handle each gap that matters.

These are deliberate simplifications. Each one is a gap I know about, with the fix I would make.

| Limitation | Fix I would make |
|------------|------------------|
| A duplicate request gets a 409, not the original result, and a retry with different details is not detected | Store the response and a hash of the request body with the key. Return the stored response when the hash matches, and a 422 when it does not |
| Month-end reads live balances, so a past month cannot be reproduced | A write-once `account_snapshots` table (`account_id`, `month`, `closing_balance`, `last_entry_id`), written by a scheduled job right after the check reports `RECONCILED`. Summing one month's snapshots per currency must give 0.00 |
| Reconciliation does not check each transaction | One query grouping entries by transaction and currency, flagging any group where debits differ from credits |
| Reconciliation compares the ledger with itself | Import the real bank or custodian statement and compare its totals with EXTERNAL |
| EXTERNAL and FX_POSITION are hot rows, so deposits, withdrawals and conversions in one currency run one after another | Split each system account into several sub-accounts and sum them |
| Exchange rate is a daily ECB reference rate from a free service, with no quote step, fallback or age check | A paid quote feed, a short-lived quote the transfer must reference, a second provider, and rejection of rates older than an agreed limit (behaviour 7) |
| A reversal is refused when the recipient already spent the money | A dispute or compensating-transfer process |
| No authentication or ownership checks | Authenticate callers and check that they own the source account |
| `created_at` has no timezone and uses the database server clock | `timestamptz`, stored in UTC |
| Customer balance cannot go below zero, and every transaction's entries balance, but only the application enforces both | A `CHECK (balance >= 0 OR is_system)` constraint, and a deferred constraint trigger that checks debits equal credits per transaction and currency |
| Tables come from `create_all`, which never alters an existing table | Schema migrations with Alembic |
| History uses page numbers, so a new entry arriving between page requests shifts the pages | Keyset pagination on (`created_at`, entry id) |
| Only USD and SGD | Currency metadata (decimal places, rounding) and system accounts per new currency (behaviour 2) |

**How I would test the main fixes**
- **Idempotent replay:** send the same request twice, expect the same body and one transaction. Send it with a different amount, expect a 422.
- **Snapshots:** post entries across two months, snapshot the first, change balances in the second, and expect the first month to still sum to 0.00. Edit a snapshot row by hand and expect the check to fail.
- **Per-transaction check:** insert an unbalanced transaction directly in the database and expect it to be flagged.
- **Sub-accounts:** run 20 parallel deposits in one currency and compare total time and the summed balance before and after.

### Caching (not implemented)

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

Repeat the first deposit command unchanged. You get a 409 and the balance does not change: that is the idempotency key doing its job.

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

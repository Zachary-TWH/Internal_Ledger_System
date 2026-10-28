# Internal Ledger System

A small double-entry ledger API built with FastAPI, SQLAlchemy, and PostgreSQL. This take-home focuses on correct money movement, auditable history, and testable failure behavior rather than production deployment.

## What it supports

| Requirement | Approach |
|---|---|
| Record transfers atomically | `POST /transactions` writes the transaction, ledger entries, and account balances in one database transaction. A failure rolls back the operation. |
| Query balances and history | `GET /accounts/{id}` returns the current balance; `GET /accounts/{id}/transactions` returns paginated history ordered by transaction time. |
| Reject insufficient funds | The source account is locked before checking its balance. Transfers cannot take an account below zero. |
| Reverse transactions | Reversal posts new entries with debit and credit swapped; it never edits or deletes the original. A reversal is refused if the recipient has already spent the funds. |
| Prevent duplicate requests | Each money-moving request requires a unique `idempotency_key`. Reusing one returns `409` rather than posting again. |
| Reconcile balances | `GET /reconciliation/month-end` compares stored balances to ledger entries and checks that balances net to zero per currency. |
| Transfer between currencies | USD and SGD are supported. A cross-currency transfer uses a fetched Frankfurter rate and records the rate used. If the rate is unavailable, the request fails without posting. |
| Handle concurrent requests | PostgreSQL row locks protect balances; rows are locked in a consistent order to reduce deadlocks. PostgreSQL-specific tests exercise races and rounding. |

## Ledger design

Every movement creates balanced debit and credit entries. Entries are append-only; corrections are new transactions.

- Deposits and withdrawals use an `EXTERNAL` system account.
- Same-currency transfers create two entries.
- Cross-currency transfers create four entries through `FX_POSITION`, keeping each currency balanced independently.
- Amounts use `Decimal` in Python and `NUMERIC(18,2)` in PostgreSQL. API amounts must be positive and have at most two decimal places. Cross-currency transfers have a minimum of 10 source-currency units.
- The stored account balance is a read optimization; ledger entries remain the audit history.

## Run locally

Docker Compose starts PostgreSQL and the API. Copy `.env.example` to `.env`, set a local-only `POSTGRES_PASSWORD`, then run:

```sh
docker compose up --build
```

The API docs are available at `http://localhost:8000/docs`. To create an account:

```sh
curl -X POST http://localhost:8000/accounts \
  -H "Content-Type: application/json" \
  -d '{"owner_name":"Alice","currency":"USD"}'
```

Use the returned account IDs for deposits, transfers, balance lookups, and history. Transfer requests include an idempotency key:

```json
{
  "from_account_id": 1,
  "to_account_id": 2,
  "amount": "25.00",
  "idempotency_key": "transfer-001"
}
```

Submit this body to `POST /transactions`. For local development without Docker, run PostgreSQL separately, configure `DATABASE_URL`, create the schema/system accounts with `python -m app.create_table`, then start `uvicorn app.main:app --reload`.

## Verify

The endpoint suite uses a temporary SQLite database:

```sh
pytest tests/test_ledger_endpoints.py
```

The concurrency and rounding suite needs PostgreSQL and an **empty, disposable** database; it drops and recreates its tables:

```sh
TEST_POSTGRES_URL=postgresql+psycopg://postgres:your-password@localhost:5432/ledger_test \
  pytest tests/test_postgres_concurrency.py
```

SQLite does not enforce PostgreSQL row locks, so it is not a substitute for the second suite. CI provisions PostgreSQL and runs both suites, followed by a Docker API smoke test.

## Scope and trade-offs

- Supported currencies are USD and SGD. FX uses a daily reference rate, not a guaranteed live market quote; there is no quote-and-confirm flow or spread.
- Reconciliation reports current stored balances and monthly activity, not a saved historical close. It cannot compare against an external bank statement or prove that an FX rate was commercially correct.
- Authentication, authorization, migrations, and production secret management are outside this take-home. `.env` is local-only and ignored by Git; deployments should inject secrets through their platform.
- The CI deploy step is illustrative only; it does not deploy the application.

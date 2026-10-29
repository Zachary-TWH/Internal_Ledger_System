# Internal Ledger System

A double-entry ledger API built with FastAPI, SQLAlchemy, and PostgreSQL. This take-home prioritizes correct, auditable money movement and explicit trade-offs over production-scale infrastructure.

## Executive summary

| Assignment requirement | What this implementation does |
|---|---|
| Record transactions atomically | `POST /transactions` records ledger entries and updates both balances in one database transaction. |
| Query balances and history | `GET /accounts/{id}` returns a balance; `GET /accounts/{id}/transactions` returns paginated, timestamp-ordered history. |
| Reject overdrafts | Locks the source account and checks its current PostgreSQL balance before posting. |
| Prevent duplicate transfers | Requires an `idempotency_key`; reusing a key returns `409` and does not post again. |
| Reverse transactions | Posts a new transaction with the original entries reversed; never edits or deletes ledger history. |
| Reconcile the ledger | Compares stored balances with entries and checks that balances net to zero per currency. |
| Support multiple currencies | USD and SGD transfers use a fetched reference rate; the rate is saved with the transaction. |
| Handle concurrent requests | PostgreSQL row locks and deterministic lock ordering protect shared balances; dedicated PostgreSQL tests exercise races. |

**Core design:** immutable double-entry ledger entries are the audit trail; account balances are maintained as a fast, transactionally updated copy. Deposits and withdrawals use `EXTERNAL`; cross-currency transfers use `FX_POSITION` so each currency balances independently.

**Try it:** copy `.env.example` to `.env`, set a local-only `POSTGRES_PASSWORD`, and run `docker compose up --build`. Open `http://localhost:8000/docs` to explore the API. Run the fast suite with `pytest tests/test_ledger_endpoints.py`; CI also runs the PostgreSQL concurrency suite.

The details below are optional for a first pass and explain the ledger rules, failure behavior, and limitations.

## Technical details

### API

| Endpoint | Purpose |
|---|---|
| `POST /accounts` | Create a USD or SGD customer account. |
| `POST /deposit`, `POST /withdraw` | Move funds between a customer account and `EXTERNAL`. Withdrawals cannot overdraw. |
| `POST /transactions` | Transfer between customer accounts. Request fields: `from_account_id`, `to_account_id`, `amount`, and unique `idempotency_key`. |
| `POST /transactions/{id}/reverse` | Reverse a transaction if doing so will not make a customer balance negative. |
| `GET /accounts/{id}` | Read the current balance and account currency. |
| `GET /accounts/{id}/transactions` | Read paginated ledger-entry history; supports ascending or descending timestamp order. |
| `GET /reconciliation/month-end?month=YYYY-MM` | Read the reconciliation report. |

### Ledger and money rules

- Every movement is double-entry: debits equal credits within each currency. Same-currency transfers create two entries; a currency conversion creates four, passing through `FX_POSITION`.
- Example: transferring 40 USD to SGD at 1.35 debits the sender and credits `FX_POSITION` by 40 USD, then debits `FX_POSITION` and credits the recipient by 54 SGD. A reversal swaps those four entries using the original amounts.
- Python uses `Decimal`; PostgreSQL stores amounts as `NUMERIC(18,2)`. Amounts must be positive, fit that range, and have no more than two decimal places. Responses format amounts as decimal strings.
- Transfer amount is in the source currency. Cross-currency transfers have a minimum of 10 source-currency units. The Frankfurter daily reference rate is rounded to six decimal places; the converted amount is rounded once to cents using `ROUND_HALF_UP`.
- FX is fail-closed: a timeout, invalid response, or unavailable rate returns `503` without recording a transaction. The exact rate used is stored on the transaction; later rate changes do not alter history. This is a reference rate, not a guaranteed live market quote.
- A reversal copies the original amounts and swaps each debit and credit, including for FX transfers. It never makes a new rate request. If the recipient has already spent the funds, reversal is refused; a real dispute workflow is outside this project.
- Entries are append-only. A correction is another transaction, not an edit to existing history. The `accounts.balance` value is updated in the same commit as the entries, but money-moving decisions read the locked database row, never a cache.

### Concurrency and idempotency

The source and destination account rows are locked in ascending ID order before balances are checked or changed. FX rates are fetched before acquiring those locks, so a slow external service does not hold account locks. System accounts are locked after customer accounts in a consistent order. Unique database constraints backstop the idempotency check and prevent a transaction from being reversed twice, including under races.

### Reconciliation

The report checks each account's stored balance against its credits minus debits, then checks that customer, `EXTERNAL`, and `FX_POSITION` balances net to zero for each currency. Both checks use one SQL statement for a consistent snapshot. The endpoint returns `200` with `RECONCILED` or `OUT_OF_BALANCE`; invalid months return `400`.

The `month` parameter groups monthly activity, but there are no saved month-end snapshots: balances are live, so a past close cannot be reproduced after later transactions. Reconciliation also does not verify each transaction's internal balance, confirm that the FX rate was commercially correct, or compare the ledger against an external bank statement.

### Run and test

**Start the API and PostgreSQL with Docker Compose:**

```sh
docker compose up --build
```

Before starting, copy `.env.example` to `.env` and set `POSTGRES_PASSWORD` to a local-only, URL-safe value.

Create an account through `POST /accounts`, then use its returned ID in a transfer request such as:

```json
{
  "from_account_id": 1,
  "to_account_id": 2,
  "amount": "25.00",
  "idempotency_key": "transfer-001"
}
```

Send the request to `POST /transactions`. Swagger UI at `/docs` provides an interactive way to try all endpoints.

**Endpoint and business-rule tests** use a temporary SQLite database:

```sh
pytest tests/test_ledger_endpoints.py
```

**Concurrency and rounding tests** require PostgreSQL because SQLite does not enforce PostgreSQL row locks or its numeric rounding behavior. CI provisions a PostgreSQL service and runs this suite separately. To run it locally, point `TEST_POSTGRES_URL` at an **empty, disposable** PostgreSQL database; the tests drop and recreate its tables:

```sh
TEST_POSTGRES_URL=postgresql+psycopg://postgres:your-password@localhost:5432/ledger_test \
  pytest tests/test_postgres_concurrency.py
```

CI runs both test suites on pushes to `main`, pull requests, and manual dispatch, then builds the Compose stack and runs an API smoke test. Its deploy job is illustrative only and does not deploy the application.

### Scope and trade-offs

- Supports USD and SGD only, with one fixed currency per account. Adding currencies requires currency-specific precision and system accounts.
- No FX quote-and-confirm step, spread, or fee. The customer accepts the rate at execution.
- No authentication or authorization; do not expose this demo API publicly.
- No saved historical reconciliation snapshots, external bank-statement reconciliation, migrations, or production secret management. Local `.env` files are ignored by Git; deployments should inject secrets through their platform.
- Docker Compose is a convenient local environment, not a production deployment architecture.

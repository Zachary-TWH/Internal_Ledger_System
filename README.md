# Internal Ledger System

## Assumptions

- **Money is `BigDecimal`, USD and SGD only.** Both use 2 decimal places, so one scale and one rounding rule cover every amount. Currencies like IDR (large nominal values, effectively no minor unit) or KRW (0 decimals) need per-currency scale and rounding handling, which adds complexity, so they are out of scope.

## Caching (not implemented)

Fetching account details (`GET /accounts/{id}`) and transaction history (`GET /accounts/{id}/transactions`) are good candidates for a cache, because they are read far more often than they change and caching them would reduce load on the database. This is **not implemented** for this simple ledger. This is how I would add it:

- **Pattern:** cache-aside with Redis. Read from the cache first, fall back to Postgres on a miss, then store the result with a short TTL (for example 30 to 60 seconds).
- **Keys:** `account:{id}` for details, and `account:{id}:txns:{page}:{page_size}` for each page of history.
- **Invalidation:** after a deposit, withdrawal or transfer commits, delete the cache keys for every account it touched. Invalidate only after the commit succeeds, so a rolled-back transaction never evicts good data.
- **Correctness rule:** money-moving code must never read balances from the cache. The balance check inside a transfer always reads the row under a database lock. The cache only serves read-only endpoints, so a stale value can never cause an overdraft or a double spend.
- **Trade-off:** a reader can see a balance that is a few seconds stale. That is acceptable for display, and the TTL bounds how stale it can get. If a client needs a guaranteed fresh read, an endpoint option could bypass the cache.
- **Pagination caveat:** paginated history is the harder case, since a new transaction shifts every page. Invalidating all cached pages for an account on write keeps it correct.
- **How I would test it:** a request after a write returns the new balance; two requests inside the TTL hit the database once; a failed transfer leaves the cache untouched; a Redis outage falls back to the database instead of failing the request.

## Month-end reconciliation

Month-end reconciliation compares each account's stored balance against the sum of its ledger entries (credits minus debits) and reports any account where they differ.

It should run as a **Celery task**, not as a function that someone remembers to call. A scheduled task runs the same way every month, does not depend on a person or an API request staying open, and can be retried if it fails.

Design:

- **Schedule:** Celery beat triggers the task shortly after the month ends (for example 00:05 UTC on the 1st), with the period it should cover passed as an argument. A manual trigger endpoint can enqueue the same task for any past month and return the task ID.
- **Consistent snapshot:** the task runs in a single read-only transaction at `REPEATABLE READ`, so a transfer that lands halfway through the check cannot create false discrepancies. Entries are bounded to the period end in UTC.
- **One grouped query:** a single `GROUP BY account` query compares stored versus computed balance for all accounts. This avoids one query per account, which does not scale.
- **Idempotent by period:** each run is recorded in a `reconciliation_runs` table keyed by month. Re-running a month either returns the stored result or replaces it, so a retry or a duplicate trigger cannot produce conflicting reports.
- **Retries:** transient failures (database connection, worker restart) are retried with a delay and a retry limit. After the limit, the run is marked failed instead of silently disappearing.
- **Report, never fix:** the task records discrepancies (account, stored balance, computed balance, difference) and raises an alert. It does not edit balances. A real discrepancy is corrected with a new, auditable ledger transaction.
- **Global check:** the same run verifies that total debits equal total credits, and that the sum of all balances (including the EXTERNAL account) is zero.
- **Closing snapshots:** after a clean run, store each account's closing balance. Next month the check becomes opening balance plus that month's entries equals closing balance, so the task never needs to re-sum the full history.
- **Observability:** log run start, duration, accounts checked and discrepancies found, and expose the result through a read endpoint for the latest run.
- **How I would test it:** a healthy ledger reports zero discrepancies; manually changing one balance in the database is flagged; running the same month twice gives one result; a worker failure mid-run is retried and completes.


## Credentials: The database password in docker-compose.yml (default postgres, overridable via POSTGRES_PASSWORD) is a demo credential for local use only, so the project runs with no setup. A real deployment would inject secrets from a secrets manager and never commit them.

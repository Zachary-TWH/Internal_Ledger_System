# Internal Ledger System

## Assumptions

- **Money is `BigDecimal`, USD and SGD only.** Both use 2 decimal places, so one scale and one rounding rule cover every amount. Currencies like IDR (large nominal values, effectively no minor unit) or KRW (0 decimals) need per-currency scale and rounding handling, which adds complexity, so they are out of scope.

## Caching (not implemented)

Reading an account's balance and transaction history is a good candidate for caching, since these are read far more often than they change. This is **not implemented** to keep the ledger simple. I would use Redis to store recent reads for a short time, and clear an account's cached data whenever a deposit, withdrawal or transfer changes it. Money-moving code would always read the balance directly from the database, so a stale cached value could never cause an overdraft. The trade-off is that a displayed balance may be a few seconds out of date.

## Month-end reconciliation

Month-end reconciliation compares each account's stored balance against the sum of its ledger entries (credits minus debits) and reports any account where they differ. It is exposed as an API endpoint that takes a month, so it can be triggered on demand. In a real system the same function would be called automatically by a scheduled job at the end of each month. The check only reports discrepancies and never edits balances, since a real error should be fixed with a new, auditable ledger transaction. It also confirms that total debits equal total credits across the whole ledger.

## Credentials: The database password in docker-compose.yml (default postgres, overridable via POSTGRES_PASSWORD) is a demo credential for local use only, so the project runs with no setup. A real deployment would inject secrets from a secrets manager and never commit them.

## On container startup, entrypoint.sh runs create_tables.py, which creates the database tables and seeds an EXTERNAL account for each supported currency. The deposit and withdraw endpoints assume these accounts already exist and return a 500 if one is missing.

# ENV is not exposure but for this assignment purpose was not added to gitignore
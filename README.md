# Internal Ledger System

## Assumptions

- **Money is `BigDecimal`, USD and SGD only.** Both use 2 decimal places, so one scale and one rounding rule cover every amount. Currencies like IDR (large nominal values, effectively no minor unit) or KRW (0 decimals) need per-currency scale and rounding handling, which adds complexity, so they are out of scope.

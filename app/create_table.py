"""One-off setup script: create the tables and seed the system accounts.

Run it once before the API starts (entrypoint.sh does this on every container start):
    python -m app.create_table
It is safe to re-run. It only creates what is missing and never alters or drops anything.

It seeds 4 system accounts (is_system = true): EXTERNAL and FX_POSITION for each of USD and SGD.
The API never creates these. If one is missing, deposits, withdrawals and cross-currency
transfers fail with a 500. On a fresh database they get ids 1 to 4, in this order: EXTERNAL SGD,
EXTERNAL USD, FX_POSITION SGD, FX_POSITION USD (the README walkthrough relies on these ids).
"""

from .database import engine, Base, SessionLocal

# Each model class (Account, Transaction, LedgerEntry) registers its table on Base.metadata at the moment this file runs.
# Without the import, Base knows about no tables and create_all() below creates nothing.
from . import models

SYSTEM_CURRENCIES = ["SGD", "USD"]

# System accounts created for every supported currency (4 in total for USD and SGD):
#   EXTERNAL     - the outside world. Deposits and withdrawals go through it.
#   FX_POSITION  - the bank's own currency-exchange counter. Both sides of a cross-currency
#                  transfer go through it, so each currency's ledger still nets to zero.
SYSTEM_ACCOUNT_NAMES = ["EXTERNAL", "FX_POSITION"]

def main():
    """Create any missing tables, then seed any missing system accounts."""
    # Base is the shared parent class of every model in models.py. Base.metadata is the
    # list of all tables those classes defined (accounts, transactions, ledger_entries).
    # create_all() looks at the database, then creates each table in that list that does
    # not exist yet. Existing tables are left alone, so it is safe to run on every start.
    # It never alters or drops a table, so a changed column needs a migration, not this.
    # This is the one place tables are created. main.py does not do it.
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        for name in SYSTEM_ACCOUNT_NAMES:
            for ccy in SYSTEM_CURRENCIES:
                exists = (
                    db.query(models.Account)
                    .filter_by(owner_name=name, currency=ccy)
                    .first()
                )
                if not exists:
                    db.add(models.Account(
                        owner_name=name,
                        currency=ccy,
                        balance=0,
                        is_system=True,
                    ))
        db.commit()
    finally:
        db.close()
    print("Tables and system accounts ready.")

# Run this script to create the database tables and the EXTERNAL and FX_POSITION system accounts.
# Safe to re-run: an account that already exists is skipped.
if __name__ == "__main__":
    main()

"""One-off setup: create the tables and seed the 4 system accounts.

Run once before the API starts (entrypoint.sh runs it on every container start):
    python -m app.create_table
Safe to re-run: it only creates what is missing. On a fresh database the ids are 1 to 4:
EXTERNAL SGD, EXTERNAL USD, FX_POSITION SGD, FX_POSITION USD (the README walkthrough uses them).
"""

from decimal import Decimal

from .database import engine, Base, SessionLocal

# The import registers the tables on Base.metadata. Without it create_all() creates nothing.
from . import models

SYSTEM_CURRENCIES = ["SGD", "USD"]

# One of each per supported currency (README assumption 6).
SYSTEM_ACCOUNT_NAMES = ["EXTERNAL", "FX_POSITION"]

def main():
    """Create any missing tables, then seed any missing system accounts."""
    # Creates only missing tables and never alters or drops one, so a changed column needs a migration.
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
                        balance=Decimal("0"),
                        is_system=True,
                    ))
        db.commit()
    finally:
        db.close()
    print("Tables and system accounts ready.")

if __name__ == "__main__":
    main()

from .database import engine, Base, SessionLocal
from . import models 

SYSTEM_CURRENCIES = ["SGD", "USD"]

def main():
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        for ccy in SYSTEM_CURRENCIES:
            exists = (
                db.query(models.Account)
                .filter_by(owner_name="EXTERNAL", currency=ccy)
                .first()
            )
            if not exists:
                db.add(models.Account(
                    owner_name="EXTERNAL",
                    currency=ccy,
                    balance=0,
                    is_system=True,
                ))
        db.commit()
    finally:
        db.close()
    print("Tables and system accounts ready.")

# Run this script to create the database tables and the EXTERNAL system accounts.
if __name__ == "__main__":
    main()
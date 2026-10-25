# Background tasks run by Celery workers, separate from the API process
from .celery_app import celery_app
from .database import SessionLocal
from . import models
from sqlalchemy import func
from decimal import Decimal
import logging
from decimal import Decimal
import requests

logger = logging.getLogger(__name__)

@celery_app.task
def reconcile_all_accounts_task():
    db = SessionLocal()
    try:
        accounts = db.query(models.Account).all()
        for account in accounts:
            total_credits = db.query(func.sum(models.LedgerEntry.amount)).filter(
                models.LedgerEntry.account_id == account.id,
                models.LedgerEntry.entry_type == models.EntryType.CREDIT
            ).scalar() or Decimal(0)

            total_debits = db.query(func.sum(models.LedgerEntry.amount)).filter(
                models.LedgerEntry.account_id == account.id,
                models.LedgerEntry.entry_type == models.EntryType.DEBIT
            ).scalar() or Decimal(0)

            computed_balance = total_credits - total_debits

            if computed_balance != account.balance:
                logger.warning(f"RECONCILIATION ALERT: account {account.id} ({account.owner_name}) "
                      f"stored={account.balance}, computed={computed_balance}")
            else:
                logger.info(f"RECONCILIATION OK: account {account.id} ({account.owner_name}) "
                      f"balance={account.balance}")
    finally:
        db.close()


SUPPORTED_CURRENCIES = ["USD", "EUR", "GBP", "SGD", "JPY"]

@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def fetch_exchange_rates_task(self):
    db = SessionLocal()
    try:
        for base in SUPPORTED_CURRENCIES:
            try:
                response = requests.get(
                    f"https://api.frankfurter.app/latest?from={base}",
                    timeout=5
                )
                response.raise_for_status()
                data = response.json()

                for quote, rate in data["rates"].items():
                    if quote not in SUPPORTED_CURRENCIES:
                        continue
                    db_rate = models.ExchangeRate(
                        base_currency=base,
                        quote_currency=quote,
                        rate=Decimal(str(rate))
                    )
                    db.add(db_rate)

                db.commit()
                logger.info(f"EXCHANGE RATE FETCH OK: base={base}")
            except Exception as e:
                logger.warning(f"EXCHANGE RATE FETCH FAILED: base={base}, error={e}")
                raise self.retry(exc=e)
    finally:
        db.close()
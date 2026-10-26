"""Tests for GET /reconciliation/month-end.

Runs against a throwaway SQLite file (no Postgres needed). DATABASE_URL must be set
before app.main is imported, because app/database.py builds the engine at import time.
Run from the repo root:  pytest tests/
"""

import os
import tempfile
from datetime import datetime
from decimal import Decimal

_db_file = os.path.join(tempfile.mkdtemp(), "test_ledger.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_file}"

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal, Base, engine
from app import models

client = TestClient(app)
THIS_MONTH = datetime.now().strftime("%Y-%m")


@pytest.fixture(autouse=True)
def fresh_db():
    """Empty tables, then the two EXTERNAL system accounts (as create_table.py seeds them)."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    for ccy in ("USD", "SGD"):
        db.add(models.Account(owner_name="EXTERNAL", currency=ccy, balance=0, is_system=True))
    db.add(models.ExchangeRate(base_currency="USD", quote_currency="SGD", rate=Decimal("1.350000")))
    db.commit()
    db.close()
    yield


def new_account(name, currency="USD"):
    return client.post("/accounts", json={"owner_name": name, "currency": currency}).json()["id"]


def deposit(account_id, amount, key):
    assert client.post("/deposit", json={"account_id": account_id, "amount": amount, "idempotency_key": key}).status_code == 200


def transfer(src, dst, amount, key):
    r = client.post("/transfer", json={"from_account_id": src, "to_account_id": dst, "amount": amount, "idempotency_key": key})
    assert r.status_code == 200, r.text


def report(month=THIS_MONTH, **params):
    return client.get("/reconciliation/month-end", params={"month": month, **params})


def account_row(body, account_id):
    return next(a for a in body["accounts"] if a["account_id"] == account_id)


def test_clean_ledger_is_reconciled():
    alice, bob_sgd = new_account("alice"), new_account("bob", "SGD")
    deposit(alice, 100, "d1")
    transfer(alice, bob_sgd, 40, "t1")   # cross-currency: 40 USD -> 54.00 SGD

    body = report().json()
    assert body["status"] == "RECONCILED"
    assert body["summary"]["accounts_mismatched"] == 0
    assert body["summary"]["transactions_checked"] == 2
    assert body["transaction_discrepancies"] == []

    a = account_row(body, alice)
    assert (a["opening_balance"], a["total_credits"], a["total_debits"], a["closing_balance"]) == ("0.00", "100.00", "40.00", "60.00")
    assert a["stored_balance"] == a["ledger_balance"] == "60.00" and a["status"] == "OK"
    assert account_row(body, bob_sgd)["stored_balance"] == "54.00"

    # EXTERNAL accounts are listed but not compared (their stored balance is never updated)
    external = next(x for x in body["accounts"] if x["is_system"] and x["currency"] == "USD")
    assert external["status"] == "NOT_COMPARED" and external["ledger_balance"] == "-100.00"


def test_stored_balance_tampered_is_flagged():
    alice = new_account("alice")
    deposit(alice, 100, "d1")
    db = SessionLocal()
    db.query(models.Account).filter_by(id=alice).one().balance = Decimal("150.00")   # simulate corruption
    db.commit()
    db.close()

    body = report().json()
    assert body["status"] == "DISCREPANCIES_FOUND"
    row = account_row(body, alice)
    assert row["status"] == "MISMATCH" and row["difference"] == "50.00"
    assert body["summary"]["accounts_mismatched"] == 1

    only = report(only_discrepancies=True).json()
    assert [a["account_id"] for a in only["accounts"]] == [alice]


def test_unbalanced_transaction_is_flagged():
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 100, "d1")
    transfer(alice, bob, 30, "t1")
    db = SessionLocal()
    credit = db.query(models.LedgerEntry).filter_by(account_id=bob).one()
    credit.amount = Decimal("31.00")     # credit no longer matches the 30.00 debit
    db.commit()
    db.close()

    body = report().json()
    assert body["status"] == "DISCREPANCIES_FOUND"
    assert len(body["transaction_discrepancies"]) == 1
    assert body["transaction_discrepancies"][0]["debit_total"] == "30.00"
    assert body["transaction_discrepancies"][0]["credit_total"] == "31.00"


def test_opening_balance_uses_earlier_months():
    alice = new_account("alice")
    deposit(alice, 100, "d1")
    deposit(alice, 25, "d2")
    db = SessionLocal()
    # move the first deposit to September 2020
    first = db.query(models.Transaction).filter_by(idempotency_key="d1").one()
    first.created_at = datetime(2020, 9, 15, 12, 0)
    db.commit()
    db.close()

    sept = account_row(report("2020-09").json(), alice)
    assert (sept["opening_balance"], sept["total_credits"], sept["closing_balance"]) == ("0.00", "100.00", "100.00")

    this_month = account_row(report().json(), alice)
    assert (this_month["opening_balance"], this_month["total_credits"], this_month["closing_balance"]) == ("100.00", "25.00", "125.00")
    assert this_month["status"] == "OK"     # stored balance (125) matches the whole ledger


def test_invalid_month_returns_400():
    for bad in ("2026-13", "October", "2026/10", "26-10"):
        assert report(bad).status_code == 400


def test_december_rolls_into_next_year():
    body = report("2026-12").json()
    assert body["period_end_exclusive"].startswith("2027-01-01")

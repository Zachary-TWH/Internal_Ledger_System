"""Endpoint tests for the ledger API.

Covers POST /deposit, POST /withdraw, POST /transfer, GET /accounts/{id},
GET /accounts/{id}/transactions and GET /reconciliation/month-end.
Concurrency and rounding are tested in test_postgres_concurrency.py, because SQLite
ignores SELECT ... FOR UPDATE and does not round to the column's 2 decimal places.

Runs against a throwaway SQLite file (no Postgres needed). DATABASE_URL must be set
before app.main is imported, because app/database.py builds the engine at import time.
Run from the repo root:  pytest tests/
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal

_db_file = os.path.join(tempfile.mkdtemp(), "test_ledger.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_file}"

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal, Base, engine
from app import models

client = TestClient(app)
# Transactions are stamped by the database clock (UTC), so use UTC here too. A local
# clock can name a different month near midnight on the 1st and make the tests flaky.
THIS_MONTH = datetime.now(timezone.utc).strftime("%Y-%m")


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


# Raw helpers: return the response so a test can check error codes.
# (deposit() and transfer() above assert success and return nothing.)

def post_deposit(account_id, amount, key):
    return client.post("/deposit", json={"account_id": account_id, "amount": amount, "idempotency_key": key})


def post_withdraw(account_id, amount, key):
    return client.post("/withdraw", json={"account_id": account_id, "amount": amount, "idempotency_key": key})


def post_transfer(src, dst, amount, key):
    return client.post("/transfer", json={"from_account_id": src, "to_account_id": dst, "amount": amount, "idempotency_key": key})


def balance_of(account_id):
    return client.get(f"/accounts/{account_id}").json()["balance"]


def count_rows(model):
    db = SessionLocal()
    try:
        return db.query(model).count()
    finally:
        db.close()


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


# ---------------------------------------------------------------------------
# POST /deposit
# ---------------------------------------------------------------------------

def test_deposit_increases_balance_and_writes_two_entries():
    alice = new_account("alice")

    r = post_deposit(alice, 100, "d1")

    assert r.status_code == 200
    assert r.json()["new_balance"] == 100
    assert balance_of(alice) == 100
    # one transaction with a DEBIT (EXTERNAL) and a CREDIT (alice) of the same amount
    db = SessionLocal()
    entries = db.query(models.LedgerEntry).filter_by(transaction_id=r.json()["transaction_id"]).all()
    db.close()
    assert sorted(e.entry_type.value for e in entries) == ["CREDIT", "DEBIT"]
    assert {e.amount for e in entries} == {Decimal("100.00")}


def test_deposit_same_idempotency_key_moves_money_once():
    alice = new_account("alice")

    assert post_deposit(alice, 100, "d1").status_code == 200
    assert post_deposit(alice, 100, "d1").status_code == 409    # retry is rejected

    assert balance_of(alice) == 100                             # not 200
    assert count_rows(models.Transaction) == 1


def test_deposit_unknown_account_returns_404():
    assert post_deposit(9999, 100, "d1").status_code == 404
    assert count_rows(models.Transaction) == 0


@pytest.mark.parametrize("bad_amount", [0, -50])
def test_deposit_rejects_zero_and_negative_amount(bad_amount):
    alice = new_account("alice")

    assert post_deposit(alice, bad_amount, "d1").status_code == 422
    assert balance_of(alice) == 0
    assert count_rows(models.Transaction) == 0


# ---------------------------------------------------------------------------
# POST /withdraw
# ---------------------------------------------------------------------------

def test_withdraw_decreases_balance_and_writes_two_entries():
    alice = new_account("alice")
    deposit(alice, 100, "d1")

    r = post_withdraw(alice, 40, "w1")

    assert r.status_code == 200
    assert r.json()["new_balance"] == 60
    assert balance_of(alice) == 60
    # DEBIT alice, CREDIT EXTERNAL
    db = SessionLocal()
    entries = db.query(models.LedgerEntry).filter_by(transaction_id=r.json()["transaction_id"]).all()
    db.close()
    by_type = {e.entry_type.value: e for e in entries}
    assert by_type["DEBIT"].account_id == alice
    assert by_type["DEBIT"].amount == by_type["CREDIT"].amount == Decimal("40.00")


def test_withdraw_exact_balance_is_allowed():
    alice = new_account("alice")
    deposit(alice, 100, "d1")

    assert post_withdraw(alice, 100, "w1").status_code == 200
    assert balance_of(alice) == 0


def test_withdraw_insufficient_funds_is_rejected_and_nothing_changes():
    alice = new_account("alice")
    deposit(alice, 100, "d1")

    r = post_withdraw(alice, 100.01, "w1")

    assert r.status_code == 400
    assert balance_of(alice) == 100
    assert count_rows(models.Transaction) == 1      # only the deposit
    assert count_rows(models.LedgerEntry) == 2      # only the deposit's two entries


def test_withdraw_same_idempotency_key_moves_money_once():
    alice = new_account("alice")
    deposit(alice, 100, "d1")

    assert post_withdraw(alice, 40, "w1").status_code == 200
    assert post_withdraw(alice, 40, "w1").status_code == 409

    assert balance_of(alice) == 60                  # not 20


def test_withdraw_unknown_account_returns_404():
    assert post_withdraw(9999, 10, "w1").status_code == 404


@pytest.mark.parametrize("bad_amount", [0, -5])
def test_withdraw_rejects_zero_and_negative_amount(bad_amount):
    alice = new_account("alice")
    deposit(alice, 100, "d1")

    assert post_withdraw(alice, bad_amount, "w1").status_code == 422
    assert balance_of(alice) == 100


# ---------------------------------------------------------------------------
# POST /transfer
# ---------------------------------------------------------------------------

def test_transfer_same_currency_updates_both_balances_and_writes_two_entries():
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 100, "d1")

    r = post_transfer(alice, bob, 30, "t1")

    assert r.status_code == 200
    body = r.json()
    assert body["exchange_rate_used"] is None                # same currency: no conversion
    assert body["from_account_new_balance"] == 70
    assert body["to_account_new_balance"] == 30
    assert balance_of(alice) == 70 and balance_of(bob) == 30
    db = SessionLocal()
    entries = db.query(models.LedgerEntry).filter_by(transaction_id=body["transaction_id"]).all()
    db.close()
    by_type = {e.entry_type.value: e for e in entries}
    assert len(entries) == 2
    assert by_type["DEBIT"].account_id == alice and by_type["CREDIT"].account_id == bob
    assert by_type["DEBIT"].amount == by_type["CREDIT"].amount == Decimal("30.00")


def test_transfer_cross_currency_converts_with_stored_rate():
    alice, bob_sgd = new_account("alice"), new_account("bob", "SGD")
    deposit(alice, 100, "d1")

    r = post_transfer(alice, bob_sgd, 40, "t1")      # 40 USD x 1.35 = 54.00 SGD

    assert r.status_code == 200
    body = r.json()
    assert body["exchange_rate_used"] == 1.35
    assert body["from_account_new_balance"] == 60
    assert body["to_account_new_balance"] == 54
    db = SessionLocal()
    txn = db.query(models.Transaction).filter_by(id=body["transaction_id"]).one()
    db.close()
    assert txn.exchange_rate_used == Decimal("1.350000")     # the rate is saved on the transaction


def test_transfer_insufficient_funds_is_rejected_and_nothing_changes():
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 50, "d1")

    r = post_transfer(alice, bob, 50.01, "t1")

    assert r.status_code == 400
    assert balance_of(alice) == 50 and balance_of(bob) == 0
    assert count_rows(models.Transaction) == 1       # only the deposit
    assert count_rows(models.LedgerEntry) == 2


def test_transfer_exact_balance_is_allowed():
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 50, "d1")

    assert post_transfer(alice, bob, 50, "t1").status_code == 200
    assert balance_of(alice) == 0 and balance_of(bob) == 50


def test_transfer_same_idempotency_key_moves_money_once():
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 100, "d1")

    assert post_transfer(alice, bob, 30, "t1").status_code == 200
    assert post_transfer(alice, bob, 30, "t1").status_code == 409

    assert balance_of(alice) == 70 and balance_of(bob) == 30     # not 40 and 60


def test_transfer_same_idempotency_key_moves_money_once_cross_currency():
    alice, bob_sgd = new_account("alice"), new_account("bob", "SGD")
    deposit(alice, 100, "d1")

    assert post_transfer(alice, bob_sgd, 40, "t1").status_code == 200
    assert post_transfer(alice, bob_sgd, 40, "t1").status_code == 409

    assert balance_of(alice) == 60 and balance_of(bob_sgd) == 54


def test_transfer_to_same_account_returns_400():
    alice = new_account("alice")
    deposit(alice, 100, "d1")

    assert post_transfer(alice, alice, 10, "t1").status_code == 400
    assert balance_of(alice) == 100


def test_transfer_unknown_account_returns_404_and_nothing_changes():
    alice = new_account("alice")
    deposit(alice, 100, "d1")

    assert post_transfer(alice, 9999, 10, "t1").status_code == 404
    assert post_transfer(9999, alice, 10, "t2").status_code == 404
    assert balance_of(alice) == 100
    assert count_rows(models.Transaction) == 1


def test_transfer_without_stored_rate_returns_400():
    # the fixture only seeds USD -> SGD, so SGD -> USD has no rate
    bob_sgd, alice = new_account("bob", "SGD"), new_account("alice")
    deposit(bob_sgd, 100, "d1")

    r = post_transfer(bob_sgd, alice, 10, "t1")

    assert r.status_code == 400
    assert balance_of(bob_sgd) == 100 and balance_of(alice) == 0
    assert count_rows(models.Transaction) == 1


@pytest.mark.parametrize("bad_amount", [0, -10])
def test_transfer_rejects_zero_and_negative_amount(bad_amount):
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 100, "d1")

    assert post_transfer(alice, bob, bad_amount, "t1").status_code == 422
    assert balance_of(alice) == 100 and balance_of(bob) == 0


def test_rate_change_after_transfer_does_not_alter_past_transaction():
    alice, bob_sgd = new_account("alice"), new_account("bob", "SGD")
    deposit(alice, 100, "d1")
    first = post_transfer(alice, bob_sgd, 40, "t1").json()      # at 1.35 -> 54.00 SGD

    # a newer rate (dated tomorrow, so it is the latest) is added
    db = SessionLocal()
    db.add(models.ExchangeRate(
        base_currency="USD", quote_currency="SGD", rate=Decimal("1.500000"),
        fetched_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1),
    ))
    db.commit()
    db.close()
    second = post_transfer(alice, bob_sgd, 40, "t2").json()     # at 1.50 -> 60.00 SGD

    assert second["exchange_rate_used"] == 1.5
    assert balance_of(bob_sgd) == 114                           # 54 + 60
    db = SessionLocal()
    old_txn = db.query(models.Transaction).filter_by(id=first["transaction_id"]).one()
    db.close()
    assert old_txn.exchange_rate_used == Decimal("1.350000")    # first transaction unchanged


# ---------------------------------------------------------------------------
# GET /accounts/{id}
# ---------------------------------------------------------------------------

def test_get_account_returns_balance_and_currency():
    bob_sgd = new_account("bob", "SGD")

    body = client.get(f"/accounts/{bob_sgd}").json()
    assert body == {"id": bob_sgd, "owner_name": "bob", "currency": "SGD", "balance": 0}

    deposit(bob_sgd, 100, "d1")
    assert client.get(f"/accounts/{bob_sgd}").json()["balance"] == 100


def test_get_account_unknown_returns_404():
    assert client.get("/accounts/9999").status_code == 404


# ---------------------------------------------------------------------------
# GET /accounts/{id}/transactions
# ---------------------------------------------------------------------------

def history(account_id, **params):
    return client.get(f"/accounts/{account_id}/transactions", params=params)


def txn_ids(response):
    return [row["transaction_id"] for row in response.json()["results"]]


def test_history_is_newest_first_by_default_and_oldest_first_with_asc():
    alice = new_account("alice")
    t1 = post_deposit(alice, 100, "d1").json()["transaction_id"]
    t2 = post_deposit(alice, 25, "d2").json()["transaction_id"]
    t3 = post_withdraw(alice, 10, "w1").json()["transaction_id"]

    assert txn_ids(history(alice)) == [t3, t2, t1]
    assert txn_ids(history(alice, order="asc")) == [t1, t2, t3]
    assert history(alice).json()["order"] == "desc"


def test_history_rows_show_type_amount_and_description():
    alice, bob_sgd = new_account("alice"), new_account("bob", "SGD")
    deposit(alice, 100, "d1")
    transfer(alice, bob_sgd, 40, "t1")      # 40 USD -> 54.00 SGD

    alice_rows = history(alice, order="asc").json()["results"]
    assert [(r["entry_type"], r["amount"], r["description"]) for r in alice_rows] == [
        ("CREDIT", 100, "Deposit"),
        ("DEBIT", 40, "Transfer"),
    ]
    bob_rows = history(bob_sgd).json()["results"]
    assert [(r["entry_type"], r["amount"], r["description"]) for r in bob_rows] == [("CREDIT", 54, "Transfer")]
    assert all(r["created_at"] for r in alice_rows)


def test_history_paging_has_no_gaps_or_overlaps():
    alice = new_account("alice")
    ids = [post_deposit(alice, 10, f"d{i}").json()["transaction_id"] for i in range(5)]

    pages = [txn_ids(history(alice, order="asc", page=p, page_size=2)) for p in (1, 2, 3, 4)]

    assert pages == [ids[0:2], ids[2:4], ids[4:5], []]


def test_history_of_account_with_no_activity_is_empty():
    alice = new_account("alice")

    body = history(alice).json()
    assert body["results"] == [] and body["page"] == 1


def test_history_unknown_account_returns_404():
    assert history(9999).status_code == 404


def test_history_invalid_order_returns_422():
    alice = new_account("alice")

    assert history(alice, order="sideways").status_code == 422

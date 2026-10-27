"""Endpoint tests for the ledger API.

Covers POST /deposit, POST /withdraw, POST /transfer, GET /accounts/{id},
GET /accounts/{id}/transactions, GET /fx/convert and GET /reconciliation/month-end.
Concurrency and rounding are tested in test_postgres_concurrency.py, because SQLite
ignores SELECT ... FOR UPDATE and does not round to the column's 2 decimal places.

Runs against a throwaway SQLite file (no Postgres needed). DATABASE_URL must be set
before app.main is imported, because app/database.py builds the engine at import time.
Run from the repo root:  pytest tests/
"""

import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal

_db_file = os.path.join(tempfile.mkdtemp(), "test_ledger.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_file}"

import pytest
from fastapi.testclient import TestClient

import urllib.error

from app.main import app, FxRateError
from app import main as app_main
from app.database import SessionLocal, Base, engine
from app import models

client = TestClient(app)

# The real fetch_fx_rate calls the Frankfurter API. Tests never touch the network: the
# fixture below swaps it for a fake that reads these rates. Delete an entry to simulate
# the service failing for that pair; change one to simulate the market moving.
REAL_FETCH_FX_RATE = app_main.fetch_fx_rate
FAKE_RATES = {}
FETCH_CALLS = []


def fake_fetch_fx_rate(from_currency, to_currency):
    FETCH_CALLS.append((from_currency, to_currency))
    if (from_currency, to_currency) not in FAKE_RATES:
        raise FxRateError("FX rate service unavailable")
    return FAKE_RATES[(from_currency, to_currency)], "2026-10-27"
# Transactions are stamped by the database clock (UTC), so use UTC here too. A local
# clock can name a different month near midnight on the 1st and make the tests flaky.
THIS_MONTH = datetime.now(timezone.utc).strftime("%Y-%m")


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    """Empty tables, the four system accounts (EXTERNAL and FX_POSITION for USD and SGD, as
    create_table.py seeds them), and a fake live FX rate (USD -> SGD 1.35, SGD -> USD 0.74)."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    for name in ("EXTERNAL", "FX_POSITION"):
        for ccy in ("USD", "SGD"):
            db.add(models.Account(owner_name=name, currency=ccy, balance=0, is_system=True))
    db.commit()
    db.close()
    FAKE_RATES.clear()
    FAKE_RATES.update({("USD", "SGD"): Decimal("1.350000"), ("SGD", "USD"): Decimal("0.740000")})
    FETCH_CALLS.clear()
    monkeypatch.setattr(app_main, "fetch_fx_rate", fake_fetch_fx_rate)
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


def currency_row(body, currency):
    return next(c for c in body["currencies"] if c["currency"] == currency)


def system_balance(owner_name, currency):
    """Stored balance of a system account (EXTERNAL or FX_POSITION), read straight from the database."""
    db = SessionLocal()
    try:
        return db.query(models.Account).filter_by(owner_name=owner_name, currency=currency, is_system=True).one().balance
    finally:
        db.close()


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


# ---------------------------------------------------------------------------
# GET /reconciliation/month-end
# ---------------------------------------------------------------------------

def test_clean_ledger_is_reconciled_and_each_currency_sums_to_zero():
    alice, bob_sgd = new_account("alice"), new_account("bob", "SGD")
    deposit(alice, 100, "d1")                                  # alice +100, EXTERNAL_USD -100
    assert post_withdraw(alice, 40, "w1").status_code == 200   # alice -40,  EXTERNAL_USD +40
    transfer(alice, bob_sgd, 50, "t1")                         # 50 USD -> 67.50 SGD at 1.35

    body = report().json()

    assert body["status"] == "RECONCILED"
    assert currency_row(body, "USD") == {
        "currency": "USD", "customers_total": "10.00", "external_total": "-60.00",
        "fx_position_total": "50.00", "net": "0.00", "status": "OK",
    }
    assert currency_row(body, "SGD") == {
        "currency": "SGD", "customers_total": "67.50", "external_total": "0.00",
        "fx_position_total": "-67.50", "net": "0.00", "status": "OK",
    }


def test_empty_ledger_is_reconciled():
    body = report().json()

    assert body["status"] == "RECONCILED"
    assert all(c["net"] == "0.00" for c in body["currencies"])


def test_many_movements_in_both_directions_still_sum_to_zero():
    alice, bob_sgd, carol = new_account("alice"), new_account("bob", "SGD"), new_account("carol")
    deposit(alice, 500, "d1")
    deposit(bob_sgd, 300, "d2")
    transfer(alice, bob_sgd, 10.01, "t1")      # 10.01 x 1.35 = 13.5135 -> 13.51 SGD (rounds down)
    transfer(bob_sgd, alice, 10, "t2")         # 10 SGD x 0.74 = 7.40 USD
    transfer(alice, carol, 25, "t3")           # same currency: two entries, no FX accounts
    assert post_withdraw(bob_sgd, 5, "w1").status_code == 200

    body = report().json()

    assert body["status"] == "RECONCILED"
    assert currency_row(body, "USD")["net"] == "0.00" and currency_row(body, "SGD")["net"] == "0.00"


def test_balance_changed_outside_the_normal_path_is_out_of_balance():
    alice, bob_sgd = new_account("alice"), new_account("bob", "SGD")
    deposit(alice, 100, "d1")
    deposit(bob_sgd, 100, "d2")
    db = SessionLocal()
    db.query(models.Account).filter_by(id=alice).one().balance = Decimal("150.00")   # simulate corruption: +50
    db.commit()
    db.close()

    body = report().json()

    assert body["status"] == "OUT_OF_BALANCE"
    assert currency_row(body, "USD")["net"] == "50.00" and currency_row(body, "USD")["status"] == "OUT_OF_BALANCE"
    assert currency_row(body, "SGD")["status"] == "OK"        # the other currency is unaffected


def test_offsetting_errors_are_not_detected():
    """Known limit (documented in the README): +10 on one account and -10 on another cancel out."""
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 100, "d1")
    deposit(bob, 100, "d2")
    db = SessionLocal()
    db.query(models.Account).filter_by(id=alice).one().balance = Decimal("110.00")
    db.query(models.Account).filter_by(id=bob).one().balance = Decimal("90.00")
    db.commit()
    db.close()

    assert report().json()["status"] == "RECONCILED"


def test_invalid_month_returns_400():
    for bad in ("2026-13", "October", "2026/10", "26-10"):
        assert report(bad).status_code == 400


def test_month_has_ended_flag():
    assert report("2020-01").json()["month_has_ended"] is True
    assert report("2099-12").json()["month_has_ended"] is False      # December rolls into the next year


# ---------------------------------------------------------------------------
# System accounts: EXTERNAL and FX_POSITION balances move with every entry
# ---------------------------------------------------------------------------

def test_deposit_lowers_external_and_withdraw_raises_it():
    alice = new_account("alice")

    deposit(alice, 100, "d1")
    assert system_balance("EXTERNAL", "USD") == Decimal("-100.00")

    assert post_withdraw(alice, 40, "w1").status_code == 200
    assert system_balance("EXTERNAL", "USD") == Decimal("-60.00")     # net money that came in
    assert system_balance("EXTERNAL", "SGD") == 0                      # other currency untouched


def test_cross_currency_transfer_writes_four_entries_through_fx_position():
    alice, bob_sgd = new_account("alice"), new_account("bob", "SGD")
    deposit(alice, 100, "d1")

    body = post_transfer(alice, bob_sgd, 50, "t1").json()              # 50 USD x 1.35 = 67.50 SGD

    db = SessionLocal()
    entries = db.query(models.LedgerEntry).filter_by(transaction_id=body["transaction_id"]).order_by(models.LedgerEntry.id).all()
    names = {a.id: (a.owner_name, a.currency) for a in db.query(models.Account).all()}
    db.close()
    rows = [(names[e.account_id], e.entry_type.value, e.amount) for e in entries]
    assert rows == [
        ((("alice", "USD")), "DEBIT", Decimal("50.00")),
        (("FX_POSITION", "USD"), "CREDIT", Decimal("50.00")),
        (("FX_POSITION", "SGD"), "DEBIT", Decimal("67.50")),
        (("bob", "SGD"), "CREDIT", Decimal("67.50")),
    ]
    assert system_balance("FX_POSITION", "USD") == Decimal("50.00")    # bank took in 50 USD
    assert system_balance("FX_POSITION", "SGD") == Decimal("-67.50")   # and paid out 67.50 SGD
    assert system_balance("EXTERNAL", "USD") == Decimal("-100.00")     # unchanged by the conversion


def test_same_currency_transfer_does_not_touch_system_accounts():
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 100, "d1")

    transfer(alice, bob, 30, "t1")

    assert system_balance("EXTERNAL", "USD") == Decimal("-100.00")
    assert system_balance("FX_POSITION", "USD") == 0 and system_balance("FX_POSITION", "SGD") == 0


def test_cross_currency_transfer_without_fx_position_accounts_returns_500_and_records_nothing():
    alice, bob_sgd = new_account("alice"), new_account("bob", "SGD")
    deposit(alice, 100, "d1")
    db = SessionLocal()
    db.query(models.Account).filter_by(owner_name="FX_POSITION").delete()
    db.commit()
    db.close()

    r = post_transfer(alice, bob_sgd, 40, "t1")

    assert r.status_code == 500
    assert balance_of(alice) == 100 and balance_of(bob_sgd) == 0
    assert count_rows(models.Transaction) == 1                         # only the deposit


def test_deposit_needs_a_real_system_account_not_a_customer_called_external():
    db = SessionLocal()
    db.query(models.Account).filter_by(owner_name="EXTERNAL", currency="USD").delete()
    db.commit()
    db.close()
    fake_external = new_account("EXTERNAL")           # a customer picks the same name through the API
    alice = new_account("alice")

    assert post_deposit(alice, 100, "d1").status_code == 500           # the lookup ignores non-system accounts
    assert balance_of(alice) == 0 and balance_of(fake_external) == 0


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


def test_transfer_cross_currency_converts_with_live_rate():
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


def test_transfer_cross_currency_returns_503_when_rate_service_is_down():
    bob_sgd, alice = new_account("bob", "SGD"), new_account("alice")
    deposit(bob_sgd, 100, "d1")
    FAKE_RATES.clear()                                   # the FX service is down

    r = post_transfer(bob_sgd, alice, 10, "t1")

    assert r.status_code == 503                          # fail closed: no guessed or old rate
    assert balance_of(bob_sgd) == 100 and balance_of(alice) == 0
    assert count_rows(models.Transaction) == 1           # only the deposit: nothing recorded

    # the failed request left nothing behind, so the same key works once the service is back
    FAKE_RATES[("SGD", "USD")] = Decimal("0.740000")
    assert post_transfer(bob_sgd, alice, 10, "t1").status_code == 200
    assert balance_of(bob_sgd) == 90 and balance_of(alice) == 7.4          # 10 SGD x 0.74


def test_same_currency_transfer_never_calls_the_rate_service():
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 100, "d1")
    FAKE_RATES.clear()                                   # even with the FX service down

    assert post_transfer(alice, bob, 30, "t1").status_code == 200
    assert FETCH_CALLS == []


def test_transfer_to_unknown_account_does_not_call_the_rate_service():
    alice = new_account("alice")
    deposit(alice, 100, "d1")

    assert post_transfer(alice, 9999, 10, "t1").status_code == 404
    assert FETCH_CALLS == []


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

    # the market moves
    FAKE_RATES[("USD", "SGD")] = Decimal("1.500000")
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


# ---------------------------------------------------------------------------
# GET /fx/convert and the real fetch_fx_rate (the network is faked, never called)
# ---------------------------------------------------------------------------

def test_fx_convert_returns_rate_and_rounded_amount():
    FAKE_RATES[("USD", "SGD")] = Decimal("1.291234")

    r = client.get("/fx/convert", params={"from_currency": "usd", "to_currency": "SGD", "amount": "10.01"})

    assert r.status_code == 200
    body = r.json()
    assert body["rate"] == "1.291234" and body["converted_amount"] == "12.93"   # 12.92... rounds up
    assert body["from_currency"] == "USD" and body["rate_date"] == "2026-10-27"


def test_fx_convert_same_currency_needs_no_lookup():
    r = client.get("/fx/convert", params={"from_currency": "USD", "to_currency": "USD", "amount": "5"})

    assert r.status_code == 200 and r.json()["converted_amount"] == "5.00"
    assert FETCH_CALLS == []


@pytest.mark.parametrize("params", [
    {"from_currency": "EUR", "to_currency": "SGD", "amount": "1"},     # unsupported currency
    {"from_currency": "USD", "to_currency": "SGD", "amount": "0"},     # not positive
])
def test_fx_convert_rejects_bad_input(params):
    assert client.get("/fx/convert", params=params).status_code == 400


def test_fx_convert_returns_502_when_service_is_down():
    FAKE_RATES.clear()

    r = client.get("/fx/convert", params={"from_currency": "USD", "to_currency": "SGD"})

    assert r.status_code == 502


class _FakeResponse:
    """Stands in for the object urllib.request.urlopen returns."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def test_real_fetch_parses_frankfurter_response(monkeypatch):
    body = b'{"amount":1.0,"base":"USD","date":"2026-10-27","rates":{"SGD":1.2912}}'
    monkeypatch.setattr(app_main.urllib.request, "urlopen", lambda req, timeout: _FakeResponse(body))

    rate, rate_date = REAL_FETCH_FX_RATE("USD", "SGD")

    assert rate == Decimal("1.291200") and rate_date == "2026-10-27"   # Decimal, 6 places, not a float


@pytest.mark.parametrize("body", [b"not json", b'{"rates":{}}', b'{"rates":{"SGD":0}}', b'{"rates":{"SGD":"abc"}}'])
def test_real_fetch_rejects_bad_responses(monkeypatch, body):
    monkeypatch.setattr(app_main.urllib.request, "urlopen", lambda req, timeout: _FakeResponse(body))

    with pytest.raises(FxRateError):
        REAL_FETCH_FX_RATE("USD", "SGD")


def test_real_fetch_reports_timeout_and_http_errors(monkeypatch):
    def timeout(req, timeout):
        raise TimeoutError()

    def server_error(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)

    monkeypatch.setattr(app_main.urllib.request, "urlopen", timeout)
    with pytest.raises(FxRateError) as slow:
        REAL_FETCH_FX_RATE("USD", "SGD")
    assert slow.value.timed_out is True

    monkeypatch.setattr(app_main.urllib.request, "urlopen", server_error)
    with pytest.raises(FxRateError) as down:
        REAL_FETCH_FX_RATE("USD", "SGD")
    assert down.value.timed_out is False

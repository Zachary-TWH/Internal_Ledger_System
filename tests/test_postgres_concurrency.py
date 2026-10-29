"""Concurrency and rounding tests. These need a real Postgres database.

SQLite ignores SELECT ... FOR UPDATE and does not round to the column's 2 decimal
places, so these cannot run on the SQLite database used by test_ledger_endpoints.py.

Run this file on its own, with TEST_POSTGRES_URL pointing at an empty test database
(the tables are dropped and recreated):

    TEST_POSTGRES_URL=postgresql+psycopg://postgres:postgres@localhost:5432/ledger_test \
        pytest tests/test_postgres_concurrency.py

Without TEST_POSTGRES_URL the whole file is skipped.
"""

import os
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

_url = os.environ.get("TEST_POSTGRES_URL")
if not _url:
    pytest.skip("TEST_POSTGRES_URL is not set; these tests need a real Postgres database", allow_module_level=True)
os.environ["DATABASE_URL"] = _url      # must be set before app.main is imported

from fastapi.testclient import TestClient

from app.main import app
from app import main as app_main
from app.database import SessionLocal, Base, engine
from app import models

if engine.dialect.name != "postgresql":
    # app.database builds the engine once per process. If another test file imported
    # the app first (for example with SQLite), this file cannot switch databases.
    pytest.skip("the app was already imported with another database; run this file on its own", allow_module_level=True)


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    """Empty tables, then the four system accounts (EXTERNAL and FX_POSITION for USD and SGD).
    The live FX rate is faked at 1.35 for any pair."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    for name in ("EXTERNAL", "FX_POSITION"):
        for ccy in ("USD", "SGD"):
            db.add(models.Account(owner_name=name, currency=ccy, balance=0, is_system=True))
    db.commit()
    db.close()
    # no network in tests: the transfer endpoint gets a fixed rate instead of calling Frankfurter
    monkeypatch.setattr(app_main, "fetch_fx_rate", lambda frm, to: (Decimal("1.350000"), "2026-10-27"))
    yield


# Each call uses its own TestClient so requests really run in parallel threads.
# raise_server_exceptions=False turns a server error into a 500 response instead of raising.

def post(path, payload):
    return TestClient(app, raise_server_exceptions=False).post(path, json=payload)


def get(path):
    return TestClient(app, raise_server_exceptions=False).get(path)


def new_account(name, currency="USD"):
    return post("/accounts", {"owner_name": name, "currency": currency}).json()["id"]


def deposit(account_id, amount, key):
    return post("/deposit", {"account_id": account_id, "amount": amount, "idempotency_key": key})


def withdraw(account_id, amount, key):
    return post("/withdraw", {"account_id": account_id, "amount": amount, "idempotency_key": key})


def transfer(src, dst, amount, key):
    return post("/transactions", {"from_account_id": src, "to_account_id": dst, "amount": amount, "idempotency_key": key})


def balance_of(account_id):
    return Decimal(get(f"/accounts/{account_id}").json()["balance"])


def system_balance(owner_name, currency):
    """Stored balance of a system account, read straight from the database."""
    db = SessionLocal()
    try:
        return db.query(models.Account).filter_by(owner_name=owner_name, currency=currency, is_system=True).one().balance
    finally:
        db.close()


def month_end():
    return get("/reconciliation/month-end?month=" + datetime.now(timezone.utc).strftime("%Y-%m")).json()


def run_parallel(calls, workers=10):
    """Run each zero-argument callable in a thread pool and return their results in order.

    A 60 second timeout per call turns a deadlock (a request that never finishes) into a
    test failure instead of a hung test run.
    """
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(call) for call in calls]
        return [f.result(timeout=60) for f in futures]


def statuses(responses):
    return sorted(r.status_code for r in responses)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

def test_parallel_deposits_to_one_account_lose_no_updates():
    alice = new_account("alice")

    responses = run_parallel([lambda i=i: deposit(alice, 10, f"d{i}") for i in range(20)])

    assert statuses(responses) == [200] * 20
    assert balance_of(alice) == 200                 # 20 x 10, no lost update
    db = SessionLocal()
    entries = db.query(models.LedgerEntry).filter_by(account_id=alice).count()
    db.close()
    assert entries == 20


def test_parallel_withdrawals_never_overdraw():
    alice = new_account("alice")
    assert deposit(alice, 100, "seed").status_code == 200

    responses = run_parallel([lambda i=i: withdraw(alice, 10, f"w{i}") for i in range(20)])

    # 100 / 10 = exactly 10 withdrawals can succeed; the other 10 must be rejected
    assert statuses(responses) == [200] * 10 + [400] * 10
    assert balance_of(alice) == 0


def test_opposite_direction_transfers_do_not_deadlock():
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 100, "seed-a")
    deposit(bob, 100, "seed-b")

    calls = []
    for i in range(10):
        calls.append(lambda i=i: transfer(alice, bob, 1, f"ab{i}"))
        calls.append(lambda i=i: transfer(bob, alice, 1, f"ba{i}"))
    responses = run_parallel(calls)

    assert statuses(responses) == [200] * 20        # none aborted by a deadlock
    assert balance_of(alice) == 100 and balance_of(bob) == 100


def test_identical_simultaneous_requests_move_money_once():
    alice = new_account("alice")

    responses = run_parallel([lambda: deposit(alice, 50, "same-key") for _ in range(10)])

    # Exactly one request creates the transaction. The others are duplicates: those that arrive
    # after it committed get 200 "already_processed", those that race it are stopped by the unique
    # key and get 409. Never a 500, and the balance moves once.
    assert set(statuses(responses)) <= {200, 409}
    created = [r for r in responses if r.status_code == 200 and "status" not in r.json()]
    assert len(created) == 1
    assert balance_of(alice) == 50
    db = SessionLocal()
    rows = db.query(models.Transaction).filter_by(idempotency_key="same-key").count()
    db.close()
    assert rows == 1


def test_parallel_deposits_to_different_accounts_lose_no_update_on_external():
    # every deposit also changes the one shared EXTERNAL_USD row, so this is where a lost update would show
    accounts = [new_account(f"user{i}") for i in range(20)]

    responses = run_parallel([lambda a=a, i=i: deposit(a, 10, f"d{i}") for i, a in enumerate(accounts)])

    assert statuses(responses) == [200] * 20
    assert system_balance("EXTERNAL", "USD") == Decimal("-200.00")     # 20 x 10, none lost
    assert month_end()["status"] == "RECONCILED"


def test_parallel_conversions_in_both_directions_do_not_deadlock_and_still_sum_to_zero():
    alice, bob_sgd = new_account("alice"), new_account("bob", "SGD")
    deposit(alice, 1000, "seed-a")
    deposit(bob_sgd, 1000, "seed-b")

    calls = []
    for i in range(10):
        calls.append(lambda i=i: transfer(alice, bob_sgd, 10.01, f"as{i}"))    # USD -> SGD
        calls.append(lambda i=i: transfer(bob_sgd, alice, 10.01, f"sa{i}"))    # SGD -> USD
        calls.append(lambda i=i: deposit(alice, 5, f"da{i}"))                  # USD deposits at the same time
        calls.append(lambda i=i: withdraw(bob_sgd, 5, f"wb{i}"))               # SGD withdrawals at the same time
    responses = run_parallel(calls)

    assert statuses(responses) == [200] * 40                # none aborted by a deadlock
    body = month_end()
    assert body["status"] == "RECONCILED"
    assert all(c["net"] == "0.00" for c in body["currencies"])


# ---------------------------------------------------------------------------
# Rounding
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("amount, expected_sgd", [
    (10.01, "13.51"),    # 10.01 x 1.35 = 13.5135, rounds down to 13.51
    (10.30, "13.91"),    # 10.30 x 1.35 = 13.905, exactly half, rounds up to 13.91 (half even would give 13.90)
])
def test_converted_amount_is_rounded_to_cents(amount, expected_sgd):
    alice, bob_sgd = new_account("alice"), new_account("bob", "SGD")
    deposit(alice, 100, "seed")

    r = transfer(alice, bob_sgd, amount, "t1")

    assert r.status_code == 200, r.text
    assert balance_of(bob_sgd) == Decimal(expected_sgd)
    db = SessionLocal()
    credit = db.query(models.LedgerEntry).filter_by(account_id=bob_sgd).one()
    db.close()
    assert credit.amount == Decimal(expected_sgd)


def test_month_end_sums_to_zero_after_rounded_conversions():
    alice, bob_sgd = new_account("alice"), new_account("bob", "SGD")
    deposit(alice, 100, "seed")

    assert transfer(alice, bob_sgd, 10.01, "t1").status_code == 200     # 13.5135 rounds to 13.51
    assert transfer(alice, bob_sgd, 10.30, "t2").status_code == 200     # 13.905 rounds to 13.91

    body = month_end()
    assert body["status"] == "RECONCILED"
    assert system_balance("FX_POSITION", "SGD") == Decimal("-27.42")    # 13.51 + 13.91 paid out


# ---------------------------------------------------------------------------
# Reversals under concurrency
# ---------------------------------------------------------------------------

def reverse(transaction_id, key):
    return post(f"/transactions/{transaction_id}/reverse", {"idempotency_key": key})


def test_parallel_reversals_of_the_same_transaction_reverse_it_once():
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 100, "seed")
    original = transfer(alice, bob, 30, "t1").json()["transaction_id"]

    responses = run_parallel([lambda i=i: reverse(original, f"r{i}") for i in range(10)])

    # Exactly one wins; the other nine are refused (the row lock plus the UNIQUE reversal_of).
    assert statuses(responses) == [200] + [409] * 9
    assert balance_of(alice) == 100 and balance_of(bob) == 0
    db = SessionLocal()
    reversals = db.query(models.Transaction).filter_by(reversal_of=original).count()
    db.close()
    assert reversals == 1
    assert month_end()["status"] == "RECONCILED"


def test_parallel_reversals_of_conversions_do_not_deadlock_and_still_sum_to_zero():
    alice, bob_sgd = new_account("alice"), new_account("bob", "SGD")
    deposit(alice, 1000, "seed-a")
    deposit(bob_sgd, 1000, "seed-b")
    originals = []
    for i in range(5):
        originals.append(transfer(alice, bob_sgd, 10.01, f"as{i}").json()["transaction_id"])   # USD -> SGD
        originals.append(transfer(bob_sgd, alice, 10.01, f"sa{i}").json()["transaction_id"])   # SGD -> USD

    calls = [lambda t=t: reverse(t, f"rev-{t}") for t in originals]
    calls += [lambda i=i: deposit(alice, 5, f"da{i}") for i in range(10)]   # USD deposits at the same time
    responses = run_parallel(calls)

    assert statuses(responses) == [200] * 20                # none aborted by a deadlock
    assert balance_of(alice) == 1050                         # 1000 back, plus 10 deposits of 5
    assert balance_of(bob_sgd) == 1000
    body = month_end()
    assert body["status"] == "RECONCILED"
    assert all(c["net"] == "0.00" for c in body["currencies"])
    assert system_balance("FX_POSITION", "USD") == Decimal("0.00")
    assert system_balance("FX_POSITION", "SGD") == Decimal("0.00")


def test_reversal_and_spend_race_never_leaves_the_recipient_negative():
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 100, "seed")
    original = transfer(alice, bob, 30, "t1").json()["transaction_id"]    # Bob holds exactly 30

    responses = run_parallel([
        lambda: reverse(original, "r1"),          # wants 30 back from Bob
        lambda: withdraw(bob, 30, "w1"),          # Bob spends the same 30
    ])

    # One of them gets the money, the other is refused.
    assert statuses(responses) == [200, 400]
    assert balance_of(bob) == 0
    assert month_end()["status"] == "RECONCILED"

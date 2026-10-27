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
    """Empty tables, then the EXTERNAL system accounts. The live FX rate is faked at USD -> SGD 1.35."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    for ccy in ("USD", "SGD"):
        db.add(models.Account(owner_name="EXTERNAL", currency=ccy, balance=0, is_system=True))
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
    return post("/transfer", {"from_account_id": src, "to_account_id": dst, "amount": amount, "idempotency_key": key})


def balance_of(account_id):
    return get(f"/accounts/{account_id}").json()["balance"]


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

    # Exactly one request wins. The others are duplicates; the unique constraint on the
    # idempotency key is what stops them, whatever status code they end up with.
    assert statuses(responses).count(200) == 1
    assert balance_of(alice) == 50
    db = SessionLocal()
    rows = db.query(models.Transaction).filter_by(idempotency_key="same-key").count()
    db.close()
    assert rows == 1


# ---------------------------------------------------------------------------
# Rounding
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("amount, expected_sgd", [
    (10.01, "13.51"),    # 10.01 x 1.35 = 13.5135, rounds down to 13.51
    (0.10, "0.14"),      # 0.10 x 1.35 = 0.1350, exactly half, rounds up to 0.14
])
def test_converted_amount_is_rounded_to_cents(amount, expected_sgd):
    alice, bob_sgd = new_account("alice"), new_account("bob", "SGD")
    deposit(alice, 100, "seed")

    r = transfer(alice, bob_sgd, amount, "t1")

    assert r.status_code == 200, r.text
    assert balance_of(bob_sgd) == float(expected_sgd)
    db = SessionLocal()
    credit = db.query(models.LedgerEntry).filter_by(account_id=bob_sgd).one()
    db.close()
    assert credit.amount == Decimal(expected_sgd)

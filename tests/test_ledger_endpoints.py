"""Endpoint tests for the ledger API.

Covers POST /deposit, POST /withdraw, POST /transactions, GET /accounts/{id},
GET /accounts/{id}/transactions and GET /reconciliation/month-end.
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
    r = client.post("/transactions", json={"from_account_id": src, "to_account_id": dst, "amount": amount, "idempotency_key": key})
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
    return client.post("/transactions", json={"from_account_id": src, "to_account_id": dst, "amount": amount, "idempotency_key": key})


def balance_of(account_id):
    return Decimal(client.get(f"/accounts/{account_id}").json()["balance"])


def count_rows(model):
    db = SessionLocal()
    try:
        return db.query(model).count()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /reconciliation/month-end
# ---------------------------------------------------------------------------

# The core double-entry invariant: in every currency, customers + EXTERNAL + FX_POSITION
#   nets to 0, and each account's stored balance equals the sum of its own ledger entries.
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
    alice_row = next(a for a in body["accounts"] if a["owner_name"] == "alice")
    assert alice_row["stored_balance"] == alice_row["ledger_balance"] == "10.00"
    assert alice_row["opening_balance"] == "0.00" and alice_row["closing_balance"] == "10.00"
    assert alice_row["difference"] == "0.00" and alice_row["status"] == "OK"


# Proves reconciliation really detects tampering: we edit a balance directly in the DB
#   (bypassing the API) and only the affected currency flips to OUT_OF_BALANCE.
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


# Why there are TWO checks: +10 on one account and -10 on another cancel out in the
#   currency total, so only the per-account stored-vs-ledger comparison catches them.
def test_offsetting_errors_are_caught_by_the_per_account_check():
    """+10 on one account and -10 on another cancel out in the per-currency sum, but each
    account's stored balance no longer matches its own ledger entries, so both are flagged."""
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 100, "d1")
    deposit(bob, 100, "d2")
    db = SessionLocal()
    db.query(models.Account).filter_by(id=alice).one().balance = Decimal("110.00")
    db.query(models.Account).filter_by(id=bob).one().balance = Decimal("90.00")
    db.commit()
    db.close()

    body = report().json()

    assert currency_row(body, "USD")["net"] == "0.00"          # the sum alone is fooled
    assert body["status"] == "OUT_OF_BALANCE"                  # the per-account check is not
    assert body["summary"]["accounts_mismatched"] == 2
    flagged = {a["account_id"]: a for a in body["accounts"] if a["status"] == "MISMATCH"}
    assert flagged[alice]["difference"] == "10.00" and flagged[bob]["difference"] == "-10.00"


# Input validation on the month parameter (YYYY-MM): bad input should give a clean 400,
#   not a 500.
def test_invalid_month_returns_400():
    for bad in ("2026-13", "October", "2026/10", "26-10"):
        assert report(bad).status_code == 400


# ---------------------------------------------------------------------------
# System accounts: EXTERNAL and FX_POSITION balances move with every entry
# ---------------------------------------------------------------------------

# Atomicity on failure: if the system is misconfigured the request fails with 500 and
#   nothing is half-written (no transaction row, no entries, balances unchanged).
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


# Security of the lookup: EXTERNAL is found by is_system=True, so a customer who
#   registers an account named 'EXTERNAL' cannot hijack the funding account.
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
# System accounts cannot be used by the customer endpoints
# ---------------------------------------------------------------------------

def system_account_id(owner_name, currency):
    db = SessionLocal()
    try:
        return db.query(models.Account).filter_by(owner_name=owner_name, currency=currency, is_system=True).one().id
    finally:
        db.close()


# EXTERNAL and FX_POSITION are internal plumbing; letting customers deposit to or
#   withdraw from them would let them manipulate the ledger's own counterparties.
@pytest.mark.parametrize(
    ("owner_name", "endpoint"),
    [("EXTERNAL", "deposit"), ("FX_POSITION", "withdraw")],
)
def test_system_accounts_cannot_be_used_by_deposit_or_withdrawal(owner_name, endpoint):
    account_id = system_account_id(owner_name, "USD")
    response = (
        post_deposit(account_id, 100, "system-deposit")
        if endpoint == "deposit"
        else post_withdraw(account_id, 100, "system-withdraw")
    )

    assert response.status_code == 400
    assert system_balance(owner_name, "USD") == 0
    assert count_rows(models.Transaction) == 0 and count_rows(models.LedgerEntry) == 0


# Closes the same loophole on the public transfer endpoint: nobody can move money into or
#   out of a system account directly.
def test_transfer_from_or_to_a_system_account_is_rejected():
    alice = new_account("alice")
    deposit(alice, 100, "seed")
    external = system_account_id("EXTERNAL", "USD")
    transactions_before = count_rows(models.Transaction)

    assert post_transfer(alice, external, 10, "t1").status_code == 400    # customer -> system
    assert post_transfer(external, alice, 10, "t2").status_code == 400    # system -> customer
    assert balance_of(alice) == 100 and system_balance("EXTERNAL", "USD") == Decimal("-100.00")
    assert count_rows(models.Transaction) == transactions_before


# ---------------------------------------------------------------------------
# POST /deposit
# ---------------------------------------------------------------------------

# Double-entry in action: one deposit is one transaction with a DEBIT on EXTERNAL and a
#   CREDIT on the customer, so money is never created from nothing.
def test_deposit_increases_balance_and_writes_two_entries():
    alice = new_account("alice")

    r = post_deposit(alice, 100, "d1")

    assert r.status_code == 200
    assert r.json()["new_balance"] == "100.00"
    assert balance_of(alice) == 100
    # one transaction with a DEBIT (EXTERNAL) and a CREDIT (alice) of the same amount
    db = SessionLocal()
    entries = db.query(models.LedgerEntry).filter_by(transaction_id=r.json()["transaction_id"]).all()
    db.close()
    assert sorted(e.entry_type.value for e in entries) == ["CREDIT", "DEBIT"]
    assert {e.amount for e in entries} == {Decimal("100.00")}
    assert system_balance("EXTERNAL", "USD") == Decimal("-100.00")


# ---------------------------------------------------------------------------
# POST /withdraw
# ---------------------------------------------------------------------------

# Mirror image of a deposit (DEBIT the customer, CREDIT EXTERNAL); EXTERNAL moves back
#   toward zero.
def test_withdraw_decreases_balance_and_writes_two_entries():
    alice = new_account("alice")
    deposit(alice, 100, "d1")

    r = post_withdraw(alice, 40, "w1")

    assert r.status_code == 200
    assert r.json()["new_balance"] == "60.00"
    assert balance_of(alice) == 60
    # DEBIT alice, CREDIT EXTERNAL
    db = SessionLocal()
    entries = db.query(models.LedgerEntry).filter_by(transaction_id=r.json()["transaction_id"]).all()
    db.close()
    by_type = {e.entry_type.value: e for e in entries}
    assert by_type["DEBIT"].account_id == alice
    assert by_type["DEBIT"].amount == by_type["CREDIT"].amount == Decimal("40.00")
    assert system_balance("EXTERNAL", "USD") == Decimal("-60.00")


# No overdraft, tested at the exact boundary (100.01 vs 100.00). A rejected request is
#   fully atomic: no transaction or entry rows leak.
def test_withdraw_insufficient_funds_is_rejected_and_nothing_changes():
    alice = new_account("alice")
    deposit(alice, 100, "d1")

    r = post_withdraw(alice, 100.01, "w1")

    assert r.status_code == 400
    assert balance_of(alice) == 100
    assert count_rows(models.Transaction) == 1      # only the deposit
    assert count_rows(models.LedgerEntry) == 2      # only the deposit's two entries


# ---------------------------------------------------------------------------
# POST /transactions
# ---------------------------------------------------------------------------

# The basic transfer requirement from the spec: atomic, debit source and credit
#   destination by the same amount, no FX involved (exchange_rate_used is None).
def test_transfer_same_currency_updates_both_balances_and_writes_two_entries():
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 100, "d1")

    r = post_transfer(alice, bob, 30, "t1")

    assert r.status_code == 200
    body = r.json()
    assert body["exchange_rate_used"] is None                # same currency: no conversion
    assert body["from_account_new_balance"] == "70.00"
    assert body["to_account_new_balance"] == "30.00"
    assert balance_of(alice) == 70 and balance_of(bob) == 30
    db = SessionLocal()
    entries = db.query(models.LedgerEntry).filter_by(transaction_id=body["transaction_id"]).all()
    db.close()
    by_type = {e.entry_type.value: e for e in entries}
    assert len(entries) == 2
    assert by_type["DEBIT"].account_id == alice and by_type["CREDIT"].account_id == bob
    assert by_type["DEBIT"].amount == by_type["CREDIT"].amount == Decimal("30.00")


# How multi-currency stays balanced: a 4-entry transaction routes through the FX_POSITION
#   accounts so debits equal credits in EACH currency, and the rate used is stored on the transaction
#   for audit.
def test_transfer_cross_currency_converts_with_live_rate():
    alice, bob_sgd = new_account("alice"), new_account("bob", "SGD")
    deposit(alice, 100, "d1")

    r = post_transfer(alice, bob_sgd, 40, "t1")      # 40 USD x 1.35 = 54.00 SGD

    assert r.status_code == 200
    body = r.json()
    assert body["exchange_rate_used"] == "1.350000"
    assert body["from_account_new_balance"] == "60.00"
    assert body["to_account_new_balance"] == "54.00"
    db = SessionLocal()
    txn = db.query(models.Transaction).filter_by(id=body["transaction_id"]).one()
    entries = db.query(models.LedgerEntry).filter_by(transaction_id=body["transaction_id"]).order_by(models.LedgerEntry.id).all()
    names = {a.id: (a.owner_name, a.currency) for a in db.query(models.Account).all()}
    db.close()
    assert txn.exchange_rate_used == Decimal("1.350000")     # the rate is saved on the transaction
    assert [
        (names[e.account_id], e.entry_type.value, e.amount) for e in entries
    ] == [
        (("alice", "USD"), "DEBIT", Decimal("40.00")),
        (("FX_POSITION", "USD"), "CREDIT", Decimal("40.00")),
        (("FX_POSITION", "SGD"), "DEBIT", Decimal("54.00")),
        (("bob", "SGD"), "CREDIT", Decimal("54.00")),
    ]
    assert system_balance("FX_POSITION", "USD") == Decimal("40.00")
    assert system_balance("FX_POSITION", "SGD") == Decimal("-54.00")


# The spec's 'reject if insufficient balance' rule, and proof that a rejected transfer
#   leaves no trace.
def test_transfer_insufficient_funds_is_rejected_and_nothing_changes():
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 50, "d1")

    r = post_transfer(alice, bob, 50.01, "t1")

    assert r.status_code == 400
    assert balance_of(alice) == 50 and balance_of(bob) == 0
    assert count_rows(models.Transaction) == 1       # only the deposit
    assert count_rows(models.LedgerEntry) == 2


# Boundary case: the check is balance < amount, so sending exactly your whole balance is
#   allowed and leaves 0.
def test_transfer_exact_balance_is_allowed():
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 50, "d1")

    assert post_transfer(alice, bob, 50, "t1").status_code == 200
    assert balance_of(alice) == 0 and balance_of(bob) == 50


# Input validation: self-transfer gives 400, unknown source or destination gives 404, and
#   nothing is written.
def test_transfer_rejects_same_or_unknown_account():
    alice = new_account("alice")
    deposit(alice, 100, "d1")

    assert post_transfer(alice, alice, 10, "same").status_code == 400
    assert post_transfer(alice, 9999, 10, "unknown-destination").status_code == 404
    assert post_transfer(9999, alice, 10, "unknown-source").status_code == 404
    assert balance_of(alice) == 100
    assert count_rows(models.Transaction) == 1


# Fail-closed design: if the FX provider is down we refuse rather than guess or reuse an
#   old rate. Nothing was recorded, so the same idempotency key can be retried safely afterwards.
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
    assert balance_of(bob_sgd) == 90 and balance_of(alice) == Decimal("7.40")          # 10 SGD x 0.74


# Cheap validation runs before the network call and before any locks, so a bad request
#   never costs an external API round-trip.
def test_cross_currency_transfer_below_minimum_is_rejected_before_any_fx_call():
    alice, bob_sgd = new_account("alice"), new_account("bob", "SGD")
    deposit(alice, 100, "d1")
    transactions_before = count_rows(models.Transaction)

    r = post_transfer(alice, bob_sgd, 9.99, "t1")

    assert r.status_code == 400
    assert "at least 10" in r.json()["detail"]
    assert FETCH_CALLS == []                              # refused before the network call
    assert balance_of(alice) == 100 and balance_of(bob_sgd) == 0
    assert count_rows(models.Transaction) == transactions_before


# The FX dependency is isolated to cross-currency transfers, so domestic transfers keep
#   working during an FX outage.
def test_same_currency_transfer_never_calls_the_rate_service():
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 100, "d1")
    FAKE_RATES.clear()                                   # even with the FX service down

    r = post_transfer(alice, bob, 30, "t1")
    assert r.status_code == 200
    assert FETCH_CALLS == []
    assert system_balance("EXTERNAL", "USD") == Decimal("-100.00")
    assert system_balance("FX_POSITION", "USD") == system_balance("FX_POSITION", "SGD") == 0


# Money-safety validation at the API edge: amount must be > 0 with at most 2 decimal
#   places (422 from Pydantic). Amounts are Decimal in the app and NUMERIC in the DB, never float.
@pytest.mark.parametrize("bad_amount", [0, -10, 10.005])
def test_money_endpoints_reject_nonpositive_and_excess_precision_amounts(bad_amount):
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 100, "d1")
    transactions_before = count_rows(models.Transaction)

    assert post_deposit(alice, bad_amount, "x1").status_code == 422
    assert post_withdraw(alice, bad_amount, "x2").status_code == 422
    assert post_transfer(alice, bob, bad_amount, "x3").status_code == 422
    assert balance_of(alice) == 100 and balance_of(bob) == 0
    assert count_rows(models.Transaction) == transactions_before


# ---------------------------------------------------------------------------
# GET /accounts/{id}
# ---------------------------------------------------------------------------

# The balance is returned as a string (e.g. "100.00") so JSON clients cannot lose
#   precision through float conversion.
def test_get_account_returns_balance_and_currency():
    bob_sgd = new_account("bob", "SGD")

    body = client.get(f"/accounts/{bob_sgd}").json()
    # the balance is a string, so no precision is lost
    assert body == {"id": bob_sgd, "owner_name": "bob", "currency": "SGD", "balance": "0.00"}

    deposit(bob_sgd, 100, "d1")
    assert client.get(f"/accounts/{bob_sgd}").json()["balance"] == "100.00"


# Only currencies that have system accounts are allowed; otherwise an account could be
#   created that can never be funded.
def test_create_account_rejects_unsupported_currency():
    r = client.post("/accounts", json={"owner_name": "eve", "currency": "EUR"})
    assert r.status_code == 422
    assert count_rows(models.Account) == 4        # only the four system accounts exist


# ---------------------------------------------------------------------------
# GET /accounts/{id}/transactions
# ---------------------------------------------------------------------------

def history(account_id, **params):
    return client.get(f"/accounts/{account_id}/transactions", params=params)


def txn_ids(response):
    return [row["transaction_id"] for row in response.json()["results"]]


# The spec asks for history ordered by timestamp. Ties are broken by entry id so the
#   order is deterministic, and the list is paginated.
def test_history_is_newest_first_by_default_and_oldest_first_with_asc():
    alice = new_account("alice")
    t1 = post_deposit(alice, 100, "d1").json()["transaction_id"]
    t2 = post_deposit(alice, 25, "d2").json()["transaction_id"]
    t3 = post_withdraw(alice, 10, "w1").json()["transaction_id"]

    assert txn_ids(history(alice)) == [t3, t2, t1]
    assert txn_ids(history(alice, order="asc")) == [t1, t2, t3]
    assert history(alice).json()["order"] == "desc"


# History lists ledger ENTRIES, so each account sees its own side of a transfer in its
#   own currency (alice -40 USD, bob +54 SGD).
def test_history_rows_show_type_amount_and_description():
    alice, bob_sgd = new_account("alice"), new_account("bob", "SGD")
    deposit(alice, 100, "d1")
    transfer(alice, bob_sgd, 40, "t1")      # 40 USD -> 54.00 SGD

    alice_rows = history(alice, order="asc").json()["results"]
    assert [(r["entry_type"], r["amount"], r["description"]) for r in alice_rows] == [
        ("CREDIT", "100.00", "Deposit"),
        ("DEBIT", "40.00", "Transfer"),
    ]
    bob_rows = history(bob_sgd).json()["results"]
    assert [(r["entry_type"], r["amount"], r["description"]) for r in bob_rows] == [("CREDIT", "54.00", "Transfer")]
    assert all(r["created_at"] for r in alice_rows)


# ---------------------------------------------------------------------------
# The real fetch_fx_rate (the network is faked, never called)
# ---------------------------------------------------------------------------


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


# The rate is parsed straight into a Decimal (parse_float=Decimal), never a float, so it
#   stays exact. The network is faked; tests never hit the internet.
def test_real_fetch_parses_frankfurter_response(monkeypatch):
    body = b'{"amount":1.0,"base":"USD","date":"2026-10-27","rates":{"SGD":1.2912}}'
    monkeypatch.setattr(app_main.urllib.request, "urlopen", lambda req, timeout: _FakeResponse(body))

    rate, rate_date = REAL_FETCH_FX_RATE("USD", "SGD")

    assert rate == Decimal("1.291200") and rate_date == "2026-10-27"   # Decimal, 6 places, not a float


# ROUND_HALF_UP is deliberate: Python's default is banker's rounding (half even), while
#   half up is the conventional rule for money.
def test_real_fetch_rounds_the_rate_half_up_to_6_places(monkeypatch):
    # 1.3500005 is exactly half way between 1.350000 and 1.350001. Half up gives 1.350001
    # (Python's default, half even, would give 1.350000).
    body = b'{"date":"2026-10-27","rates":{"SGD":1.3500005}}'
    monkeypatch.setattr(app_main.urllib.request, "urlopen", lambda req, timeout: _FakeResponse(body))

    rate, _ = REAL_FETCH_FX_RATE("USD", "SGD")

    assert rate == Decimal("1.350001")


# Never trust external data: malformed JSON, a missing rate, zero or a non-number all
#   become FxRateError, which the endpoint turns into a 503.
@pytest.mark.parametrize("body", [b"not json", b'{"rates":{}}', b'{"rates":{"SGD":0}}', b'{"rates":{"SGD":"abc"}}'])
def test_real_fetch_rejects_bad_responses(monkeypatch, body):
    monkeypatch.setattr(app_main.urllib.request, "urlopen", lambda req, timeout: _FakeResponse(body))

    with pytest.raises(FxRateError):
        REAL_FETCH_FX_RATE("USD", "SGD")


# The FX call has a 5-second timeout, and timeouts and HTTP errors fail closed instead of
#   hanging the request or guessing a rate.
@pytest.mark.parametrize("failure", ["timeout", "http"])
def test_real_fetch_reports_timeout_and_http_errors(monkeypatch, failure):
    def timeout(req, timeout):
        raise TimeoutError()

    def server_error(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)

    monkeypatch.setattr(
        app_main.urllib.request,
        "urlopen",
        timeout if failure == "timeout" else server_error,
    )
    with pytest.raises(FxRateError):
        REAL_FETCH_FX_RATE("USD", "SGD")


# ---------------------------------------------------------------------------
# Transaction type
# ---------------------------------------------------------------------------

def txn_id(response):
    assert response.status_code == 200, response.text
    return response.json()["transaction_id"]


def load_txn(transaction_id):
    db = SessionLocal()
    try:
        return db.query(models.Transaction).filter_by(id=transaction_id).one()
    finally:
        db.close()


def entries_of(transaction_id):
    """(account_id, entry_type, amount) for each entry of one transaction, in entry order."""
    db = SessionLocal()
    try:
        rows = db.query(models.LedgerEntry).filter_by(transaction_id=transaction_id).order_by(models.LedgerEntry.id).all()
        return [(r.account_id, r.entry_type.value, r.amount) for r in rows]
    finally:
        db.close()


# Transactions carry an explicit type enum, so code (e.g. the reversal rules) never has
#   to parse the description text.
def test_each_endpoint_records_its_transaction_type():
    alice, bob = new_account("alice"), new_account("bob")
    t_dep = txn_id(post_deposit(alice, 100, "d1"))
    t_wd = txn_id(post_withdraw(alice, 10, "w1"))
    t_tr = txn_id(post_transfer(alice, bob, 20, "t1"))

    assert load_txn(t_dep).type == models.TransactionType.DEPOSIT
    assert load_txn(t_wd).type == models.TransactionType.WITHDRAWAL
    assert load_txn(t_tr).type == models.TransactionType.TRANSFER
    assert all(load_txn(t).reversal_of is None for t in (t_dep, t_wd, t_tr))


# ---------------------------------------------------------------------------
# POST /transactions/{id}/reverse
# ---------------------------------------------------------------------------

def post_reverse(transaction_id, key):
    return client.post(f"/transactions/{transaction_id}/reverse", json={"idempotency_key": key})


# Reversals are new offsetting transactions; the original is never edited or deleted
#   (append-only audit trail). The reversal posts the opposite entry for every original entry.
def test_reverse_same_currency_transfer_restores_balances():
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 100, "d1")
    original = txn_id(post_transfer(alice, bob, 30, "t1"))
    original_before = (
        load_txn(original).description,
        load_txn(original).created_at,
        entries_of(original),
    )

    r = post_reverse(original, "r1")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reversal_of"] == original and body["transaction_id"] != original
    assert balance_of(alice) == 100 and balance_of(bob) == 0
    reversal = load_txn(body["transaction_id"])
    assert reversal.type == models.TransactionType.REVERSAL and reversal.reversal_of == original
    assert reversal.description == f"Reversal of transaction {original}"
    assert entries_of(original) == original_before[2]
    assert (load_txn(original).description, load_txn(original).created_at) == original_before[:2]
    assert entries_of(body["transaction_id"]) == [
        (alice, "CREDIT", Decimal("30.00")),
        (bob, "DEBIT", Decimal("30.00")),
    ]


# Reversal is generic: it flips every entry of the original, so it works for any
#   transaction type, including the EXTERNAL leg.
def test_reverse_deposit_and_withdrawal():
    alice = new_account("alice")
    dep = txn_id(post_deposit(alice, 100, "d1"))
    wd = txn_id(post_withdraw(alice, 40, "w1"))

    assert post_reverse(wd, "r-wd").status_code == 200
    assert balance_of(alice) == 100 and system_balance("EXTERNAL", "USD") == Decimal("-100.00")

    assert post_reverse(dep, "r-dep").status_code == 200
    assert balance_of(alice) == 0 and system_balance("EXTERNAL", "USD") == Decimal("0.00")


# A reversal reuses the original entry amounts and rate, not today's rate, so FX_POSITION
#   returns exactly to zero. No FX lookup happens during a reversal.
def test_reverse_cross_currency_transfer_uses_original_amounts_not_todays_rate():
    alice, carol_sgd = new_account("alice"), new_account("carol", "SGD")
    deposit(alice, 100, "d1")
    original = txn_id(post_transfer(alice, carol_sgd, 40, "t1"))      # 40 USD -> 54.00 SGD at 1.35
    assert balance_of(carol_sgd) == 54
    FAKE_RATES[("USD", "SGD")] = Decimal("1.500000")                   # the market moves
    FAKE_RATES[("SGD", "USD")] = Decimal("0.660000")
    calls_before = len(FETCH_CALLS)

    r = post_reverse(original, "r1")

    assert r.status_code == 200, r.text
    assert len(FETCH_CALLS) == calls_before                            # no FX lookup during a reversal
    assert balance_of(alice) == 100 and balance_of(carol_sgd) == 0     # exactly back, not 36 or 40.5
    for owner, ccy in (("FX_POSITION", "USD"), ("FX_POSITION", "SGD"), ("EXTERNAL", "SGD")):
        assert system_balance(owner, ccy) == Decimal("0.00")
    reversal = txn_id(r)
    assert load_txn(reversal).exchange_rate_used == Decimal("1.350000")   # the original rate is copied
    assert load_txn(original).exchange_rate_used == Decimal("1.350000")
    assert sorted(a for _, _, a in entries_of(reversal)) == [Decimal("40.00"), Decimal("40.00"), Decimal("54.00"), Decimal("54.00")]


def test_repeated_idempotency_key_returns_original_transaction_and_moves_nothing():
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 100, "d1")
    first = post_transfer(alice, bob, 30, "t1")
    rows_before = (count_rows(models.Transaction), count_rows(models.LedgerEntry))

    retry = post_transfer(alice, bob, 30, "t1")

    assert retry.status_code == 200
    assert retry.json() == {"transaction_id": first.json()["transaction_id"], "status": "already_processed"}
    assert balance_of(alice) == 70 and balance_of(bob) == 30
    assert (count_rows(models.Transaction), count_rows(models.LedgerEntry)) == rows_before



# A transaction can be reversed only once (409). This is enforced in code and, as a
#   backstop, by the UNIQUE constraint on reversal_of.
def test_reverse_same_transaction_twice_is_rejected_and_moves_nothing():
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 100, "d1")
    original = txn_id(post_transfer(alice, bob, 30, "t1"))
    assert post_reverse(original, "r1").status_code == 200
    rows_before = (count_rows(models.Transaction), count_rows(models.LedgerEntry))

    second = post_reverse(original, "r2")

    assert second.status_code == 409
    assert balance_of(alice) == 100 and balance_of(bob) == 0
    assert (count_rows(models.Transaction), count_rows(models.LedgerEntry)) == rows_before


# Design choice: to redo a reversed transaction you post a new transfer. Reversing
#   reversals would create confusing chains.
def test_cannot_reverse_a_reversal():
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 100, "d1")
    reversal = txn_id(post_reverse(txn_id(post_transfer(alice, bob, 30, "t1")), "r1"))

    r = post_reverse(reversal, "r2")

    assert r.status_code == 400
    assert balance_of(alice) == 100 and balance_of(bob) == 0


# A reversal must never push a customer below zero. Bob already spent the money, so it is
#   refused with 400 (a real system would need a dispute or chargeback process).
def test_reverse_is_rejected_when_the_recipient_already_spent_the_money():
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 100, "d1")
    original = txn_id(post_transfer(alice, bob, 30, "t1"))
    assert post_withdraw(bob, 20, "w1").status_code == 200           # Bob now holds only 10.00
    rows_before = (count_rows(models.Transaction), count_rows(models.LedgerEntry))

    r = post_reverse(original, "r1")

    assert r.status_code == 400
    assert balance_of(alice) == 70 and balance_of(bob) == 10         # nothing changed
    assert (count_rows(models.Transaction), count_rows(models.LedgerEntry)) == rows_before


# Audit trail: the history shows the reversal as its own row and links back to the
#   original through reversal_of.
def test_history_shows_type_and_reversal_link():
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 100, "d1")
    original = txn_id(post_transfer(alice, bob, 30, "t1"))
    reversal = txn_id(post_reverse(original, "r1"))

    rows = client.get(f"/accounts/{alice}/transactions", params={"order": "asc"}).json()["results"]

    assert [(r["transaction_id"], r["type"], r["reversal_of"], r["entry_type"]) for r in rows] == [
        (1, "DEPOSIT", None, "CREDIT"),
        (original, "TRANSFER", None, "DEBIT"),
        (reversal, "REVERSAL", original, "CREDIT"),
    ]


# The database refuses bad reversal data even if the API were bypassed.

def _insert_txn(**fields):
    db = SessionLocal()
    try:
        db.add(models.Transaction(**fields))
        db.commit()
    finally:
        db.close()


# Defence in depth: even if the API code were bypassed or raced, the database UNIQUE
#   constraint still blocks a double reversal.
def test_database_rejects_a_second_reversal_of_the_same_transaction():
    alice, bob = new_account("alice"), new_account("bob")
    deposit(alice, 100, "d1")
    original = txn_id(post_transfer(alice, bob, 30, "t1"))
    post_reverse(original, "r1")

    with pytest.raises(Exception) as err:        # IntegrityError: reversal_of is UNIQUE
        _insert_txn(idempotency_key="sneaky", type=models.TransactionType.REVERSAL, reversal_of=original)
    assert "UNIQUE" in str(err.value).upper()


# A CHECK constraint keeps type and reversal_of consistent at the database level: only a
#   REVERSAL has reversal_of, and every REVERSAL has one.
def test_database_ties_reversal_of_to_the_reversal_type():
    with pytest.raises(Exception):               # a REVERSAL must say what it reverses
        _insert_txn(idempotency_key="k1", type=models.TransactionType.REVERSAL, reversal_of=None)
    with pytest.raises(Exception):               # only a REVERSAL may have reversal_of
        _insert_txn(idempotency_key="k2", type=models.TransactionType.TRANSFER, reversal_of=1)

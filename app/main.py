"""Internal ledger API (FastAPI + SQLAlchemy + Postgres).

Double-entry, append-only ledger. The design and assumptions are in README.md: double-entry and
system accounts (assumptions 4, 6, 8), locking (10), idempotency (11), reversals (walkthrough).

Endpoints
---------
POST /accounts                    create a customer account (USD or SGD, starts at 0.00)
POST /deposit                     money in from EXTERNAL
POST /withdraw                    money out to EXTERNAL (no overdraft)
POST /transactions                transfer between two customer accounts (converted if the currencies differ)
POST /transactions/{id}/reverse   undo a transaction by posting a new REVERSAL transaction
GET  /accounts/{id}               one account and its current balance
GET  /accounts/{id}/transactions  the account's ledger entries, paginated
GET  /fx/convert                  read-only live FX quote (touches no account)
GET  /reconciliation/month-end    read-only two-part reconciliation report

Conventions
-----------
- Money in a response is a 2-decimal string such as "60.00" (never a float), and
  `exchange_rate_used` is a 6-decimal string such as "1.350000".
- Status codes: 400 a business rule refused the request (insufficient funds, system account, ...),
  404 unknown id, 409 duplicate idempotency_key or transaction already reversed, 422 the request
  body or query failed validation, 503 the live FX rate was unavailable, 500 a system account is
  missing (setup problem, see create_table.py).
- No authentication (a deliberate simplification, see the README).
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from fastapi import FastAPI, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel, field_validator
from .database import SessionLocal
from . import models
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from sqlalchemy import func, case, and_
from sqlalchemy.exc import IntegrityError
from typing import Literal


SUPPORTED_CURRENCIES = ["USD", "SGD"]

# A cross-currency transfer must be at least this much, in the SOURCE account's currency.
# The destination amount is rounded to 2 decimals, so a tiny amount at a low rate could round
# to 0.00 and fail the ledger's "amount > 0" rule. A minimum of 10 keeps the converted amount
# positive for any rate above 0.0005.
MIN_CROSS_CURRENCY_AMOUNT = Decimal("10")

# Free, no-API-key FX rate service (European Central Bank reference rates, updated on working days)
FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest"
FX_TIMEOUT_SECONDS = 5


class FxRateError(Exception):
    """Any failure to get a usable live rate. The caller refuses the request (fail closed).
    `message` is a fixed string, safe to show to the client.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def fetch_fx_rate(from_currency: str, to_currency: str):
    """Fetch the live rate (1 from_currency = rate to_currency) from Frankfurter.

    Returns (rate, rate_date): rate is a Decimal rounded to 6 places, rate_date is the day the
    rate was published ("YYYY-MM-DD", or None). Raises FxRateError on any failure and never
    returns a guessed or cached rate (see README assumption 7).
    """
    # e.g. https://api.frankfurter.dev/v1/latest?base=USD&symbols=SGD
    query = urllib.parse.urlencode({"base": from_currency, "symbols": to_currency})
    request = urllib.request.Request(f"{FRANKFURTER_URL}?{query}", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=FX_TIMEOUT_SECONDS) as response:
            # parse_float=Decimal keeps the rate exact instead of turning it into a float
            payload = json.loads(response.read().decode("utf-8"), parse_float=Decimal)
        rate = Decimal(str(payload["rates"][to_currency])).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        rate_date = payload.get("date")
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, TypeError, ArithmeticError):
        # any failure (slow past FX_TIMEOUT_SECONDS, unreachable, HTTP error, bad response) means reject
        raise FxRateError("FX rate service unavailable or returned a bad response")

    if rate <= 0:
        raise FxRateError("FX rate service returned an invalid rate")
    return rate, rate_date


app = FastAPI()

# Tables are created by app/create_table.py (run by entrypoint.sh before the API starts),
# not here. Outside Docker, run `python -m app.create_table` once first.

def get_db():
    """FastAPI dependency: one DB session per request, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def money_str(value: Decimal) -> str:
    """Format an amount as a 2-decimal string, e.g. "60.00" (a Decimal in a dict would become a float)."""
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def flush_or_409(db: Session):
    """Flush pending INSERTs now, turning a duplicate idempotency_key (or reversal_of) into a 409.

    Flushing early gives the transaction its id and fires the UNIQUE constraint here. On a clash
    the session is rolled back, which releases every row lock taken so far.
    """
    try:
        db.flush() 
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Duplicate request")


def commit_or_409(db: Session):
    """Commit, turning a duplicate idempotency_key that lost a race into a 409 instead of a 500."""
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Duplicate request")


def reject_duplicate_key(db: Session, idempotency_key: str):
    """Fast-path idempotency check: a key already used gets a 409. The UNIQUE constraint catches races."""
    if db.query(models.Transaction.id).filter_by(idempotency_key=idempotency_key).first():
        raise HTTPException(status_code=409, detail="Duplicate request")


def lock_system_account(db: Session, owner_name: str, currency: str) -> models.Account:
    """Load one system account (EXTERNAL or FX_POSITION) with a row lock, or fail with 500.

    Only is_system rows count, so a customer account named "EXTERNAL" is never used. Call this
    only AFTER the customer rows are locked (lock order, README assumption 10).
    """
    account = (
        db.query(models.Account)
        .filter_by(owner_name=owner_name, currency=currency, is_system=True)
        .with_for_update()
        .first()
    )
    if not account:
        raise HTTPException(status_code=500, detail=f"{owner_name} account not configured")
    return account


# NUMERIC(18,2) holds 18 digits in total, 2 of them after the decimal point, so 16 before it.
MAX_AMOUNT = Decimal("9999999999999999.99")  # largest value that fits NUMERIC(18,2)


def validate_amount(value: Decimal) -> Decimal:
    """Shared amount check: greater than zero, at most 2 decimal places, fits NUMERIC(18,2)."""
    if value <= 0:
        raise ValueError("Amount must be greater than zero")
    if value > MAX_AMOUNT:
        raise ValueError("Amount is too large")
    if value != value.quantize(Decimal("0.01")):
        raise ValueError("Amount can have at most 2 decimal places")
    return value


# ---------------------------------------------------------------------------
# Request / response schemas (Pydantic validates incoming JSON against these)
# ---------------------------------------------------------------------------

class AccountCreate(BaseModel):
    """Body of POST /accounts. `currency` is case-insensitive ("sgd" is stored as "SGD") and defaults to USD."""
    owner_name: str
    currency: str = "USD"

    @field_validator("currency")
    @classmethod
    def currency_must_be_supported(cls, value):
        # Only currencies with EXTERNAL and FX_POSITION system accounts can be used. Accepting
        # anything else (e.g. EUR) would create an account that can never be deposited into.
        value = value.upper()
        if value not in SUPPORTED_CURRENCIES:
            raise ValueError(f"Unsupported currency. Supported: {', '.join(SUPPORTED_CURRENCIES)}")
        return value


class AccountOut(BaseModel):
    """Account as returned to the client."""
    id: int
    owner_name: str
    currency: str
    balance: Decimal  # serialised as a string, e.g. "60.00", so no precision is lost

    class Config:
        from_attributes = True  # lets Pydantic read SQLAlchemy objects directly


class DepositRequest(BaseModel):
    """Body of POST /deposit. `idempotency_key` must be unique per request."""
    account_id: int
    amount: Decimal
    idempotency_key: str

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, value):
        # e.g. 100 and 10.50 are valid; -50, 0 and 10.005 are not
        return validate_amount(value)


class WithdrawalRequest(BaseModel):
    """Body of POST /withdraw. `idempotency_key` must be unique per request."""
    account_id: int
    amount: Decimal
    idempotency_key: str

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, value):
        return validate_amount(value)


class TransferRequest(BaseModel):
    """Body of POST /transactions (a transfer). `amount` is in the SOURCE account's currency."""
    from_account_id: int
    to_account_id: int
    amount: Decimal
    idempotency_key: str

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, value):
        return validate_amount(value)


class ReversalRequest(BaseModel):
    """Body of POST /transactions/{transaction_id}/reverse. `idempotency_key` must be unique per request."""
    idempotency_key: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/accounts", response_model=AccountOut)
def create_account(account: AccountCreate, db: Session = Depends(get_db)):
    """Create a customer account with a zero balance.

    Errors: 422 unsupported currency (only USD and SGD) or a missing field
    """
    new_account = models.Account(
        owner_name=account.owner_name,
        currency=account.currency,
        balance=Decimal("0")
    )
    db.add(new_account)
    db.commit()
    db.refresh(new_account)
    return new_account


@app.post("/deposit")
def deposit(req: DepositRequest, db: Session = Depends(get_db)):
    """Add money to an account (DEBIT EXTERNAL, CREDIT the user).

    Returns: {"transaction_id", "new_balance"}
    Errors:
      409  idempotency_key was already used
      404  account not found
      400  the account is a system account
      422  invalid body (amount, or a missing field)
      500  the EXTERNAL account has not been created
    """
    # idempotency check: has this exact request been processed before?
    reject_duplicate_key(db, req.idempotency_key)

    # with_for_update() locks the row so no other transaction can modify it until we commit or rollback
    account = db.query(models.Account).filter_by(id=req.account_id).with_for_update().first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    if account.is_system:
        raise HTTPException(status_code=400, detail="System accounts cannot be used here")

    # lock this currency's EXTERNAL account too (after the user's row, always in this order)
    external = lock_system_account(db, "EXTERNAL", account.currency)

    # create the transaction record
    txn = models.Transaction(
        idempotency_key=req.idempotency_key,
        type=models.TransactionType.DEPOSIT,
        description="Deposit",
    )
    db.add(txn)
    flush_or_409(db)  # gets txn.id without committing yet

    # two ledger entries: debit EXTERNAL, credit the user's account
    debit_entry = models.LedgerEntry(
        transaction_id=txn.id,
        account_id=external.id,
        entry_type=models.EntryType.DEBIT,
        amount=req.amount
    )
    credit_entry = models.LedgerEntry(
        transaction_id=txn.id,
        account_id=account.id,
        entry_type=models.EntryType.CREDIT,
        amount=req.amount
    )
    db.add_all([debit_entry, credit_entry])

    # update both balances: the user gains, EXTERNAL goes down by the same amount
    account.balance += req.amount
    external.balance -= req.amount

    # commit everything together atomically: all or nothing
    commit_or_409(db)
    db.refresh(account)
    return {"transaction_id": txn.id, "new_balance": money_str(account.balance)}


@app.post("/withdraw")
def withdraw(req: WithdrawalRequest, db: Session = Depends(get_db)):
    """Take money out of an account (DEBIT the user, CREDIT EXTERNAL). No overdraft.

    Returns: {"transaction_id", "new_balance"}
    Errors:
      409  idempotency_key was already used
      404  account not found
      400  the account is a system account, or insufficient funds
      422  invalid body (amount, or a missing field)
      500  the EXTERNAL account has not been created
    """
    # example values below: req.idempotency_key = "wdr-001", balance = 100, req.amount = 40

    # check if this exact request was already processed before
    reject_duplicate_key(db, req.idempotency_key)
     
    # with_for_update() locks the row so no other transaction can modify it until we commit or rollback
    account = db.query(models.Account).filter_by(id=req.account_id).with_for_update().first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    if account.is_system:
        raise HTTPException(status_code=400, detail="System accounts cannot be used here")
   
    # does the account have enough money? (e.g. balance 100 >= amount 40)
    # Checked BEFORE locking EXTERNAL: the user's row is already locked, so the balance cannot
    # change, and a rejected withdrawal never queues on the shared EXTERNAL row.
    if account.balance < req.amount:
        raise HTTPException(status_code=400, detail="Insufficient funds")

    # lock this currency's EXTERNAL account too (after the user's row, always in this order)
    external = lock_system_account(db, "EXTERNAL", account.currency)

    # create one transaction record grouping the two ledger entries together
    txn = models.Transaction(
        idempotency_key=req.idempotency_key,
        type=models.TransactionType.WITHDRAWAL,
        description="Withdrawal",
    )
    db.add(txn)
    flush_or_409(db)  # get txn.id without committing yet

    # debit the user's account (money leaving the account)
    debit_entry = models.LedgerEntry(
        transaction_id=txn.id,
        account_id=account.id,
        entry_type=models.EntryType.DEBIT,
        amount=req.amount
    )
    # credit EXTERNAL (money leaving the system)
    credit_entry = models.LedgerEntry(
        transaction_id=txn.id,
        account_id=external.id,
        entry_type=models.EntryType.CREDIT,
        amount=req.amount
    )
    db.add_all([debit_entry, credit_entry])

    # update both balances: 100 - 40 = 60 for the user, and EXTERNAL goes up by 40
    account.balance -= req.amount
    external.balance += req.amount

    # commit everything together atomically: all or nothing
    commit_or_409(db)
    db.refresh(account)
    return {"transaction_id": txn.id, "new_balance": money_str(account.balance)}


@app.post("/transactions")
def transfer(req: TransferRequest, db: Session = Depends(get_db)):
    """Move money from one account to another in one atomic operation (POST /transactions).

    `amount` is in the source account's currency. Cross-currency conversion, the 10 minimum and
    the FX_POSITION entries are explained in README assumptions 7 and 8. Cheap checks run first,
    before the FX call or any lock.

    Returns: {"transaction_id", "exchange_rate_used" (null if same currency),
              "from_account_new_balance", "to_account_new_balance"}
    Errors:
      409  idempotency_key was already used
      404  an account was not found
      400  same source and destination, a system account, insufficient funds, or a
           cross-currency amount below the minimum
      422  invalid body (amount, or a missing field)
      503  cross-currency and the live FX rate is unavailable (nothing is recorded)
    """
    # check if this exact transfer was already processed before
    reject_duplicate_key(db, req.idempotency_key)

    # prevent transferring to yourself
    if req.from_account_id == req.to_account_id:
        raise HTTPException(status_code=400, detail="Cannot transfer to the same account")

    # Work out whether this is a cross-currency transfer BEFORE locking anything.
    # We select plain columns (not Account objects) so no stale balance is cached in this
    # session; the locked read below is the only one that loads balances. A currency never
    # changes after an account is created, so reading it without a lock is safe. The same goes
    # for is_system, which is set when the account is created and never changes.
    rows = (
        db.query(models.Account.id, models.Account.currency, models.Account.is_system)
        .filter(models.Account.id.in_([req.from_account_id, req.to_account_id]))
        .all()
    )
    if len(rows) != 2:
        raise HTTPException(status_code=404, detail="Account not found")
    # System accounts (EXTERNAL, FX_POSITION) only move through the code paths that post to
    # them. Refuse here, before any FX call or row lock.
    if any(row.is_system for row in rows):
        raise HTTPException(status_code=400, detail="System accounts cannot be used here")
    currencies = {row.id: row.currency for row in rows}

    exchange_rate_used = None
    if currencies[req.from_account_id] != currencies[req.to_account_id]:
        # Too small to convert safely: refuse before the network call or any row lock.
        if req.amount < MIN_CROSS_CURRENCY_AMOUNT:
            raise HTTPException(
                status_code=400,
                detail=f"Cross-currency transfers must be at least {MIN_CROSS_CURRENCY_AMOUNT} in the source currency",
            )
        # Network call happens here, with no row locks held. Fail closed if it does not work.
        try:
            exchange_rate_used, _rate_date = fetch_fx_rate(
                currencies[req.from_account_id], currencies[req.to_account_id]
            )
        except FxRateError as exc:
            raise HTTPException(status_code=503, detail=f"Cannot transfer across currencies right now: {exc.message}")

    # always lock in consistent id order to prevent deadlocks
    # (e.g. A->B and B->A at the same time both lock the lower id first)
    lock_first_id = min(req.from_account_id, req.to_account_id)
    lock_second_id = max(req.from_account_id, req.to_account_id)

    # load both accounts with row locks (SELECT ... FOR UPDATE)
    # Both rows exist: the early read above already returned 404 for a missing account, and
    # accounts are never deleted, so no second "not found" check is needed here.
    first = db.query(models.Account).filter_by(id=lock_first_id).with_for_update().first()
    second = db.query(models.Account).filter_by(id=lock_second_id).with_for_update().first()

    # map the locked rows back to source and destination
    from_account = first if first.id == req.from_account_id else second
    to_account = second if second.id == req.to_account_id else first

    # does the source have enough money? (checked in the source's own currency, before conversion)
    if from_account.balance < req.amount:
        raise HTTPException(status_code=400, detail="Insufficient funds")

    if exchange_rate_used is None:
        # same currency: no conversion needed
        credited_amount = req.amount
    else:
        # different currencies: convert with the live rate fetched above, rounded to the
        # 2 decimal places every amount is stored at (so the entry and the balance agree)
        credited_amount = (req.amount * exchange_rate_used).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # Cross-currency: also lock the two FX_POSITION accounts (after the customer rows).
    # One query with ORDER BY id, so the locks are always taken in the same order.
    fx_from = fx_to = None
    if exchange_rate_used is not None:
        fx_rows = (
            db.query(models.Account)
            .filter(
                models.Account.owner_name == "FX_POSITION",
                models.Account.is_system.is_(True),
                models.Account.currency.in_([from_account.currency, to_account.currency]),
            )
            .order_by(models.Account.id)
            .with_for_update()
            .all()
        )
        fx_by_currency = {row.currency: row for row in fx_rows}
        if len(fx_by_currency) != 2:
            raise HTTPException(status_code=500, detail="FX_POSITION account not configured")
        fx_from = fx_by_currency[from_account.currency]
        fx_to = fx_by_currency[to_account.currency]

    # create one transaction record grouping all of the ledger entries together
    txn = models.Transaction(
        idempotency_key=req.idempotency_key,
        type=models.TransactionType.TRANSFER,
        description="Transfer",
        exchange_rate_used=exchange_rate_used
    )
    db.add(txn)
    flush_or_409(db)  # get txn.id without committing yet

    def entry(account, entry_type, amount):
        return models.LedgerEntry(
            transaction_id=txn.id, account_id=account.id, entry_type=entry_type, amount=amount
        )

    if fx_from is None:
        # same currency: debit the source, credit the destination
        db.add_all([
            entry(from_account, models.EntryType.DEBIT, req.amount),
            entry(to_account, models.EntryType.CREDIT, credited_amount),
        ])
    else:
        # cross-currency: the source currency goes into FX_POSITION and the destination
        # currency comes out of it, so each currency's entries net to zero on their own
        db.add_all([
            entry(from_account, models.EntryType.DEBIT, req.amount),
            entry(fx_from, models.EntryType.CREDIT, req.amount),
            entry(fx_to, models.EntryType.DEBIT, credited_amount),
            entry(to_account, models.EntryType.CREDIT, credited_amount),
        ])
        fx_from.balance += req.amount
        fx_to.balance -= credited_amount

    # update the customer balances, each in their own currency
    from_account.balance -= req.amount
    to_account.balance += credited_amount

    # commit everything together atomically: all or nothing
    commit_or_409(db)
    db.refresh(from_account)
    db.refresh(to_account)
    return {
        "transaction_id": txn.id,
        "exchange_rate_used": str(exchange_rate_used) if exchange_rate_used is not None else None,
        "from_account_new_balance": money_str(from_account.balance),
        "to_account_new_balance": money_str(to_account.balance)
    }


@app.post("/transactions/{transaction_id}/reverse")
def reverse_transaction(transaction_id: int, req: ReversalRequest, db: Session = Depends(get_db)):
    """Undo a transaction by posting a new REVERSAL transaction (see README "Reversal rules").

    Returns: {"transaction_id", "reversal_of", "entries": [{"account_id", "entry_type", "amount"}]}
    Errors:
      404  transaction not found
      409  idempotency_key was already used, or the transaction was already reversed
      400  the transaction is itself a reversal, or a customer account no longer holds enough
           to give the money back
      422  the body has no idempotency_key
      500  the original transaction has no ledger entries (should never happen)
    """
    # check if this exact request was already processed before
    reject_duplicate_key(db, req.idempotency_key)

    # Lock the original transaction's row so two reversals of it run one at a time. Nothing else
    # locks a transaction row, so this cannot deadlock with the account locks taken below.
    original = db.query(models.Transaction).filter_by(id=transaction_id).with_for_update().first()
    if not original:
        raise HTTPException(status_code=404, detail="Transaction not found")

    if original.type == models.TransactionType.REVERSAL:
        raise HTTPException(status_code=400, detail="A reversal cannot be reversed")

    already = db.query(models.Transaction.id).filter_by(reversal_of=original.id).first()
    if already:
        raise HTTPException(
            status_code=409,
            detail=f"Transaction {original.id} was already reversed by transaction {already.id}",
        )

    original_entries = (
        db.query(models.LedgerEntry)
        .filter_by(transaction_id=original.id)
        .order_by(models.LedgerEntry.id)
        .all()
    )
    if not original_entries:
        raise HTTPException(status_code=500, detail="Original transaction has no ledger entries")

    # Lock every account involved, in the same order as the other endpoints
    # (customer accounts by id first, then system accounts by id), so requests cannot deadlock.
    account_ids = {entry.account_id for entry in original_entries}
    accounts = (
        db.query(models.Account)
        .filter(models.Account.id.in_(account_ids))
        .order_by(models.Account.is_system, models.Account.id)
        .with_for_update()
        .all()
    )
    account_by_id = {account.id: account for account in accounts}

    # Work out each balance after the reversal BEFORE changing anything. A DEBIT lowers a balance
    # and a CREDIT raises it, so undoing an original DEBIT raises it and undoing a CREDIT lowers it.
    new_balance = {account.id: account.balance for account in accounts}
    for entry in original_entries:
        if entry.entry_type == models.EntryType.DEBIT:
            new_balance[entry.account_id] += entry.amount
        else:
            new_balance[entry.account_id] -= entry.amount

    # Customers may not go below zero. System accounts (EXTERNAL, FX_POSITION) may be negative.
    for account_id, balance in new_balance.items():
        if balance < 0 and not account_by_id[account_id].is_system:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot reverse: account {account_id} no longer holds enough to give the money back",
            )

    # the new transaction, linked to the original
    reversal = models.Transaction(
        idempotency_key=req.idempotency_key,
        type=models.TransactionType.REVERSAL,
        reversal_of=original.id,
        description=f"Reversal of transaction {original.id}",
        exchange_rate_used=original.exchange_rate_used,
    )
    db.add(reversal)
    flush_or_409(db)  # get reversal.id without committing yet

    # one offsetting entry per original entry: same account, same amount, opposite direction
    reversed_entries = []
    for entry in original_entries:
        opposite = (
            models.EntryType.CREDIT if entry.entry_type == models.EntryType.DEBIT
            else models.EntryType.DEBIT
        )
        reversed_entries.append(models.LedgerEntry(
            transaction_id=reversal.id,
            account_id=entry.account_id,
            entry_type=opposite,
            amount=entry.amount,
        ))
    db.add_all(reversed_entries)

    for account_id, balance in new_balance.items():
        account_by_id[account_id].balance = balance

    # commit everything together atomically: all or nothing
    try:
        db.commit()
    except IntegrityError:
        # the unique constraints are the last line of defence against a race
        # (same idempotency_key twice, or the same transaction reversed twice)
        db.rollback()
        raise HTTPException(status_code=409, detail="Duplicate request or transaction already reversed")

    return {
        "transaction_id": reversal.id,
        "reversal_of": original.id,
        "entries": [
            {"account_id": e.account_id, "entry_type": e.entry_type, "amount": money_str(e.amount)}
            for e in reversed_entries
        ],
    }


@app.get("/accounts/{account_id}", response_model=AccountOut)
def get_account(account_id: int, db: Session = Depends(get_db)):
    """Return one account and its current balance (also works for system accounts, for auditing).

    Errors: 404 account not found
    """
    account = db.query(models.Account).filter_by(id=account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return account

@app.get("/accounts/{account_id}/transactions")
def get_transactions(account_id: int, db: Session = Depends(get_db), page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), order: Literal["asc", "desc"] = "desc",):
    """Return an account's ledger entries, paginated like a bank statement.

    Query params: page (from 1), page_size (default 20, max 100), order ("desc" = newest first,
    the default; "asc" = oldest first). Ordered by the transaction's created_at, then entry id,
    so pages never overlap.
    Returns: {"page", "page_size", "order", "results": [{"transaction_id", "type",
              "reversal_of", "entry_type", "amount", "description", "created_at"}]}
    The list is ledger ENTRIES: a transfer shows once on each account it touches, and `amount` is
    in this account's own currency.
    Errors: 404 account not found, 422 invalid page, page_size or order
    """
    # check account exists first
    account = db.query(models.Account).filter_by(id=account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    # calculate how many rows to skip
    # page 1: skip 0, page 2: skip 20, page 3: skip 40
    offset = (page - 1) * page_size

    # fetch this page of ledger entries for the account
    # each entry links to a transaction, so we can see the full history
    # the timestamp lives on the transaction, so join to it and sort on created_at;
    # the entry id is the tiebreaker (two entries can share the same created_at)
    if order == "asc":
        sort_by = (models.Transaction.created_at.asc(), models.LedgerEntry.id.asc())
    else:
        sort_by = (models.Transaction.created_at.desc(), models.LedgerEntry.id.desc())

    entries = (
        db.query(models.LedgerEntry)
        .join(models.Transaction, models.LedgerEntry.transaction_id == models.Transaction.id)
        .filter(models.LedgerEntry.account_id == account_id)
        .order_by(*sort_by)
        .offset(offset)
        .limit(page_size)
        .all()
    )

    # shape the response: for each entry, return the relevant details
    result = []
    for entry in entries:
        result.append({
            "transaction_id": entry.transaction_id,
            "type": entry.transaction.type,                # DEPOSIT, WITHDRAWAL, TRANSFER or REVERSAL
            "reversal_of": entry.transaction.reversal_of,  # id of the undone transaction, else null
            "entry_type": entry.entry_type,   # DEBIT or CREDIT
            "amount": money_str(entry.amount),
            "description": entry.transaction.description,  # e.g. "Deposit", "Withdrawal", "Transfer"
            "created_at": entry.transaction.created_at
        })

    return {
        "page": page,
        "page_size": page_size,
        "order": order,
        "results": result
    }


# ---------------------------------------------------------------------------
# FX conversion (live rate from the Frankfurter API)
# ---------------------------------------------------------------------------

@app.get("/fx/convert")
def fx_convert(from_currency: str, to_currency: str, amount: Decimal = Decimal("1")):
    """Read-only live quote between two supported currencies. Touches no account or ledger row.

    Example: GET /fx/convert?from_currency=USD&to_currency=SGD&amount=100
    It does not apply the transfer minimum, so a quote can succeed for a transfer that would be refused.

    Returns: {"from_currency", "to_currency", "amount", "rate", "converted_amount",
              "rate_date", "source"}
    Errors: 400 unsupported currency or amount not above zero, 422 amount not a number, 503 rate unavailable
    """
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()

    for ccy in (from_currency, to_currency):
        if ccy not in SUPPORTED_CURRENCIES:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported currency {ccy}. Supported: {', '.join(SUPPORTED_CURRENCIES)}",
            )
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than zero")

    # same currency: nothing to look up
    if from_currency == to_currency:
        return {
            "from_currency": from_currency,
            "to_currency": to_currency,
            "amount": str(amount),
            "rate": "1",
            "converted_amount": str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            "rate_date": None,
            "source": None,
        }

    try:
        rate, rate_date = fetch_fx_rate(from_currency, to_currency)
    except FxRateError as exc:
        raise HTTPException(status_code=503, detail=exc.message)

    converted = (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "from_currency": from_currency,
        "to_currency": to_currency,
        "amount": str(amount),
        "rate": str(rate),
        "converted_amount": str(converted),
        "rate_date": rate_date,
        "source": "frankfurter.dev",
    }


# ---------------------------------------------------------------------------
# Month-end reconciliation
# ---------------------------------------------------------------------------

def _money(value) -> Decimal:
    """Round a database sum to 2 decimal places. None (a sum over no rows) counts as 0.00."""
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@app.get("/reconciliation/month-end")
def month_end_reconciliation(month: str, db: Session = Depends(get_db)):
    """Read-only month-end reconciliation. The two checks are explained in README "Month-end reconciliation".

    Query param: month, "YYYY-MM". The month only shapes the opening / credits / debits / closing
    columns. Both checks always use every entry.

    Returns: {"month", "month_has_ended", "status" (RECONCILED / OUT_OF_BALANCE),
              "summary": {"accounts_checked", "accounts_mismatched"},
              "currencies": [{"currency", "customers_total", "external_total",
                              "fx_position_total", "net", "status"}],
              "accounts": [{"account_id", "owner_name", "currency", "is_system",
                            "opening_balance", "total_credits", "total_debits", "closing_balance",
                            "stored_balance", "ledger_balance", "difference", "status"}]}
    Amounts are strings. `difference` is stored minus ledger (0.00 = reconciled). Returns 200
    even when out of balance, so read "status".
    Errors: 400 month is not in YYYY-MM format
    """
    try:
        month_start = datetime.strptime(month, "%Y-%m")
    except ValueError:
        raise HTTPException(status_code=400, detail="month must be in YYYY-MM format, e.g. 2026-10")
    next_month_start = datetime(
        month_start.year + month_start.month // 12,   # December rolls over to January of next year
        month_start.month % 12 + 1,
        1,
    )

    Account = models.Account
    Entry = models.LedgerEntry
    Txn = models.Transaction

    def total(entry_type, *conditions):
        """SUM of entry amounts of one type (CREDIT or DEBIT), optionally limited by extra conditions."""
        return func.coalesce(
            func.sum(case((and_(Entry.entry_type == entry_type, *conditions), Entry.amount), else_=0)),
            0,
        )

    before_month = Txn.created_at < month_start
    in_month = and_(Txn.created_at >= month_start, Txn.created_at < next_month_start)

    # ONE query reads each stored balance and that account's ledger sums together, so Postgres
    # gives them all the same snapshot. Both checks below are built from this one result.
    # Outer joins keep an account that has no entries yet (its ledger balance is 0.00).
    rows = (
        db.query(
            Account,
            total(models.EntryType.CREDIT).label("credits_all"),
            total(models.EntryType.DEBIT).label("debits_all"),
            total(models.EntryType.CREDIT, before_month).label("credits_before"),
            total(models.EntryType.DEBIT, before_month).label("debits_before"),
            total(models.EntryType.CREDIT, in_month).label("credits_month"),
            total(models.EntryType.DEBIT, in_month).label("debits_month"),
        )
        .outerjoin(Entry, Entry.account_id == Account.id)
        .outerjoin(Txn, Entry.transaction_id == Txn.id)
        .group_by(Account.id)
        .order_by(Account.id)
        .all()
    )

    accounts_report = []
    mismatched = 0
    per_currency = {}   # currency -> running totals of STORED balances, for check 2
    for account, credits_all, debits_all, credits_before, debits_before, credits_month, debits_month in rows:
        # stored       = the live accounts.balance column.
        # ledger_balance = credits minus debits over ALL entries ever posted. Check 1 compares the two.
        # opening / month_credits / month_debits = the same sum limited to entries before the month /
        # inside it, using the transaction's created_at. closing = opening + credits - debits.
        stored = _money(account.balance)
        ledger_balance = _money(credits_all) - _money(debits_all)
        opening = _money(credits_before) - _money(debits_before)
        month_credits = _money(credits_month)
        month_debits = _money(debits_month)
        difference = stored - ledger_balance      # 0.00 when the account reconciles

        if difference != 0:
            status = "MISMATCH"
            mismatched += 1
        else:
            status = "OK"

        accounts_report.append({
            "account_id": account.id,
            "owner_name": account.owner_name,
            "currency": account.currency,
            "is_system": account.is_system,
            "opening_balance": str(opening),
            "total_credits": str(month_credits),
            "total_debits": str(month_debits),
            "closing_balance": str(opening + month_credits - month_debits),
            "stored_balance": str(stored),
            "ledger_balance": str(ledger_balance),
            "difference": str(difference),
            "status": status,
        })

        # Check 2 input: add each stored balance to its currency's totals
        totals = per_currency.setdefault(
            account.currency,
            {"customers": Decimal("0"), "external": Decimal("0"), "fx_position": Decimal("0"), "net": Decimal("0")},
        )
        if not account.is_system:
            totals["customers"] += stored
        elif account.owner_name == "EXTERNAL":
            totals["external"] += stored
        elif account.owner_name == "FX_POSITION":
            totals["fx_position"] += stored
        totals["net"] += stored

    currencies = []
    for currency in sorted(per_currency):
        totals = per_currency[currency]
        currencies.append({
            "currency": currency,
            "customers_total": str(totals["customers"]),
            "external_total": str(totals["external"]),
            "fx_position_total": str(totals["fx_position"]),
            "net": str(totals["net"]),
            "status": "OK" if totals["net"] == 0 else "OUT_OF_BALANCE",
        })

    # RECONCILED only if every account matches its own entries (check 1) AND every currency
    # nets to exactly 0.00 (check 2)
    balanced = mismatched == 0 and all(c["status"] == "OK" for c in currencies)
    return {
        "month": month,
        # false = the month is still in progress (created_at follows the database server clock, stored without a timezone)
        "month_has_ended": datetime.now(timezone.utc).replace(tzinfo=None) >= next_month_start,
        "status": "RECONCILED" if balanced else "OUT_OF_BALANCE",
        "summary": {
            "accounts_checked": len(rows),
            "accounts_mismatched": mismatched,
        },
        "currencies": currencies,
        "accounts": accounts_report,
    }

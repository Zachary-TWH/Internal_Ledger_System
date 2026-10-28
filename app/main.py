"""Internal ledger API (FastAPI + SQLAlchemy + Postgres).

Model
-----
- Double-entry: every money movement is one Transaction with LedgerEntry rows that net to
  zero within each currency (a DEBIT lowers an account's balance, a CREDIT raises it).
- Two kinds of system account exist, one of each per currency (created at startup):
    EXTERNAL     the outside world. Deposit:  DEBIT EXTERNAL, CREDIT user.
                                    Withdrawal: DEBIT user, CREDIT EXTERNAL.
    FX_POSITION  the bank's currency-exchange counter. A cross-currency transfer has 4 entries:
                 DEBIT source, CREDIT FX_POSITION (source currency),
                 DEBIT FX_POSITION (destination currency), CREDIT destination.
- System accounts' balances move with every entry, exactly like a customer's. So adding up
  `accounts.balance` per currency must give 0.00. That is the month-end reconciliation.
- Amounts are Decimal (never float). Only USD and SGD are supported (2 decimal places).
- Append-only: ledger entries and transactions are never edited or deleted. Every transaction
  has a `type` (DEPOSIT, WITHDRAWAL, TRANSFER, REVERSAL). A mistake is undone by POST
  /transactions/{id}/reverse, which posts a NEW REVERSAL transaction whose entries offset the
  original's (same accounts and amounts, DEBIT and CREDIT swapped). `reversal_of` is UNIQUE, so
  a transaction can only be reversed once, and a REVERSAL itself cannot be reversed.

Safety rules used by every write endpoint
-----------------------------------------
- Idempotency: each request carries an `idempotency_key`. A key that was already used
  is rejected with 409, so a retried request cannot move money twice.
- Row locks: accounts are loaded with SELECT ... FOR UPDATE so concurrent requests on
  the same account run one at a time and no update is lost. Locks are always taken in the
  same order (customer accounts by id, then system accounts by id), so requests cannot
  deadlock. Every deposit or withdrawal in a currency also locks that currency's EXTERNAL
  row, so those requests run one after another (a known trade-off, see the README).
- Atomicity: ledger entries and balance changes are committed together (all or nothing).
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from fastapi import FastAPI, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, field_validator
from .database import SessionLocal
from . import models
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from sqlalchemy import func, case, and_
from sqlalchemy.exc import IntegrityError
from typing import Annotated, Literal, Union


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
    """The live FX rate could not be obtained (pass or fail: no distinction between causes)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def fetch_fx_rate(from_currency: str, to_currency: str):
    """Fetch the live rate (1 from_currency = rate to_currency) from Frankfurter.

    Returns (rate, rate_date). The rate is a Decimal rounded to 6 places (half up), the scale of
    transactions.exchange_rate_used, so the rate we use is exactly the rate we store.
    Raises FxRateError on any network, HTTP or response-format problem. Never returns a
    guessed or cached rate: if the service cannot be reached, the caller must refuse the request.
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
    """FastAPI dependency: give each request its own DB session, and always close it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def money_str(value: Decimal) -> str:
    """Format a stored amount as a 2-decimal string, e.g. "60.00".

    Returned in JSON instead of a Decimal, because FastAPI turns a Decimal inside a plain dict
    into a float (60.1), which would lose the exactness the ledger keeps in the database.
    """
    return str(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def flush_or_409(db: Session):
    """Send the pending INSERTs to the database, turning a duplicate idempotency_key into a 409."""
    try:
        db.flush() 
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Duplicate request")


def commit_or_409(db: Session):
    """Commit, turning a duplicate idempotency_key into a clean 409.

    The idempotency check at the top of each endpoint runs before any lock, so two identical
    requests arriving together can both pass it. The UNIQUE constraint on
    transactions.idempotency_key then rejects the second one, at the flush that inserts the
    transaction (Postgres makes it wait for the first request to finish, then fail) or at the
    commit. Either way roll it back and answer 409, the same answer a retry after the first
    finished gets, instead of a 500.
    """
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Duplicate request")


def reject_duplicate_key(db: Session, idempotency_key: str):
    """Fast-path idempotency check shared by every write endpoint: a key already used gets a 409.

    This runs before any lock, so two identical requests arriving together can both pass it.
    flush_or_409 / commit_or_409 catch that race through the UNIQUE constraint.
    """
    if db.query(models.Transaction.id).filter_by(idempotency_key=idempotency_key).first():
        raise HTTPException(status_code=409, detail="Duplicate request")


def lock_system_account(db: Session, owner_name: str, currency: str) -> models.Account:
    """Load one system account (EXTERNAL or FX_POSITION) with a row lock, or fail with 500.

    Only rows with is_system = true count, so a customer cannot create an account called
    "EXTERNAL" through POST /accounts and have money routed to it.
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


MAX_AMOUNT = Decimal("9999999999999999.99")  # largest value that fits NUMERIC(18,2)


def validate_amount(value: Decimal) -> Decimal:
    """Shared check for every money amount in a request body.

    Must be greater than zero, have at most 2 decimal places and fit NUMERIC(18,2).
    Rejecting a third decimal up front means the amount we add to the balance is exactly
    the amount stored in the ledger entry (Postgres would otherwise round the entry
    silently, and a value like 0.004 would become 0.00 and fail the positive-amount check).
    """
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
    """Body of POST /accounts."""
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
    """Body of POST /transfer. `amount` is in the SOURCE account's currency."""
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


class DepositTransaction(DepositRequest):
    """POST /transactions body with type DEPOSIT (same fields as POST /deposit)."""
    type: Literal["DEPOSIT"]


class WithdrawalTransaction(WithdrawalRequest):
    """POST /transactions body with type WITHDRAWAL (same fields as POST /withdraw)."""
    type: Literal["WITHDRAWAL"]


class TransferTransaction(TransferRequest):
    """POST /transactions body with type TRANSFER (same fields as POST /transfer)."""
    type: Literal["TRANSFER"]


# Pydantic reads `type` first and then validates the body against that type's model only, so a
# TRANSFER with no to_account_id is rejected with 422 naming the missing field, before any code runs.
TransactionRequest = Annotated[
    Union[DepositTransaction, WithdrawalTransaction, TransferTransaction],
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/accounts", response_model=AccountOut)
def create_account(account: AccountCreate, db: Session = Depends(get_db)):
    """Create a new account with a zero balance.

    Returns the created account (id, owner_name, currency, balance).
    """
    new_account = models.Account(
        owner_name=account.owner_name,
        currency=account.currency,
        balance=0
    )
    db.add(new_account)
    db.commit()
    db.refresh(new_account)
    return new_account


def handle_deposit(req: DepositRequest, db: Session):
    """Add money to an account (money comes in from EXTERNAL).

    Ledger entries: DEBIT EXTERNAL, CREDIT the user's account.
    Balances: the user's goes up and EXTERNAL's goes down by the same amount (so the
    currency still sums to zero).

    Returns: {"transaction_id", "new_balance"}. Amounts are strings with 2 decimals, e.g. "60.00".
    Errors:
      409  idempotency_key was already used
      404  account not found
      400  the account is a system account (EXTERNAL or FX_POSITION), which customers cannot use
      500  the EXTERNAL account has not been created
    """
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


def handle_withdraw(req: WithdrawalRequest, db: Session):
    """Take money out of an account (money goes out to EXTERNAL).

    Ledger entries: DEBIT the user's account, CREDIT EXTERNAL.
    Balances: the user's goes down and EXTERNAL's goes up by the same amount.

    Returns: {"transaction_id", "new_balance"}
    Errors:
      409  idempotency_key was already used
      404  account not found
      400  the account is a system account (EXTERNAL or FX_POSITION), which customers cannot use
      400  insufficient funds
      500  the EXTERNAL account has not been created
    """
    # example values below: req.idempotency_key = "wdr-001", balance = 100, req.amount = 40

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


def handle_transfer(req: TransferRequest, db: Session):
    """Move money from one account to another in a single atomic operation.

    `amount` is in the source account's currency. If the two accounts use different
    currencies, a live rate (source -> destination) is fetched from the Frankfurter API at
    execution time, and the destination is credited the converted amount (rounded to 2 dp)
    in its own currency. No rates are kept in a table: only the rate actually used is saved,
    on the transaction (`exchange_rate_used`).

    A cross-currency transfer must be at least MIN_CROSS_CURRENCY_AMOUNT (10) in the source
    currency, so the rounded destination amount can never be 0.00. Same-currency transfers have no
    such minimum.

    The rate is fetched BEFORE any row lock is taken, so a slow FX service never holds up
    other transfers on these accounts. If the rate cannot be fetched the transfer is refused
    (fail closed); a stale or guessed rate is never used. Same-currency transfers never call the API.

    Ledger entries, same currency (2):
      DEBIT source, CREDIT destination.
    Ledger entries, cross-currency (4), so that each currency still sums to zero:
      DEBIT source (amount)                      CREDIT FX_POSITION, source currency (amount)
      DEBIT FX_POSITION, destination currency    CREDIT destination (credited_amount)
    The balances of all four accounts move with their entries.

    Returns: {"transaction_id", "exchange_rate_used" (null if same currency),
              "from_account_new_balance", "to_account_new_balance"}
    Errors:
      409  idempotency_key was already used
      400  same source and destination account
      404  an account was not found
      400  either account is a system account (EXTERNAL or FX_POSITION), which customers cannot use
      400  cross-currency transfer below the minimum (10 in the source currency)
      400  insufficient funds in the source account
      503  cross-currency transfer and the live FX rate is unavailable (nothing is recorded)
    """
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
    first = db.query(models.Account).filter_by(id=lock_first_id).with_for_update().first()
    second = db.query(models.Account).filter_by(id=lock_second_id).with_for_update().first()

    if not first or not second:
        raise HTTPException(status_code=404, detail="Account not found")

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


TRANSACTION_HANDLERS = {
    "DEPOSIT": handle_deposit,
    "WITHDRAWAL": handle_withdraw,
    "TRANSFER": handle_transfer,
}


@app.post("/transactions")
def create_transaction(req: TransactionRequest, db: Session = Depends(get_db)):
    """Post a deposit, withdrawal or transfer through one endpoint. `type` picks the operation.

    Bodies (all amounts are strings or numbers with at most 2 decimal places):
      {"type": "DEPOSIT",    "account_id": 5, "amount": "100.00", "idempotency_key": "k1"}
      {"type": "WITHDRAWAL", "account_id": 5, "amount": "40.00",  "idempotency_key": "k2"}
      {"type": "TRANSFER",   "from_account_id": 5, "to_account_id": 6, "amount": "25.00",
                             "idempotency_key": "k3"}

    The idempotency key is checked once here, then the request goes to the handler for its type.
    Each handler does exactly what its own endpoint does (same locks, checks, ledger entries and
    errors), see POST /deposit, POST /withdraw and POST /transfer.

    Returns the same body as the matching endpoint:
      DEPOSIT, WITHDRAWAL: {"transaction_id", "new_balance"}
      TRANSFER: {"transaction_id", "exchange_rate_used", "from_account_new_balance", "to_account_new_balance"}
    Errors: 422 unknown `type` or a missing/invalid field for that type, plus the errors of the handler.
    """
    reject_duplicate_key(db, req.idempotency_key)
    return TRANSACTION_HANDLERS[req.type](req, db)


@app.post("/deposit")
def deposit(req: DepositRequest, db: Session = Depends(get_db)):
    """Same as POST /transactions with type DEPOSIT. See handle_deposit for the rules and errors."""
    reject_duplicate_key(db, req.idempotency_key)
    return handle_deposit(req, db)


@app.post("/withdraw")
def withdraw(req: WithdrawalRequest, db: Session = Depends(get_db)):
    """Same as POST /transactions with type WITHDRAWAL. See handle_withdraw for the rules and errors."""
    reject_duplicate_key(db, req.idempotency_key)
    return handle_withdraw(req, db)


@app.post("/transfer")
def transfer(req: TransferRequest, db: Session = Depends(get_db)):
    """Same as POST /transactions with type TRANSFER. See handle_transfer for the rules and errors."""
    reject_duplicate_key(db, req.idempotency_key)
    return handle_transfer(req, db)


@app.post("/transactions/{transaction_id}/reverse")
def reverse_transaction(transaction_id: int, req: ReversalRequest, db: Session = Depends(get_db)):
    """Undo a transaction by posting a new REVERSAL transaction. Nothing is edited or deleted.

    The reversal has one entry for every entry of the original, on the same account and for the
    same amount, with DEBIT and CREDIT swapped. It uses the ORIGINAL amounts, so a cross-currency
    transfer gives back exactly what moved, whatever the exchange rate is today. The original's
    `exchange_rate_used` is copied onto the reversal for the record. The balances of every account
    involved (customer, EXTERNAL and FX_POSITION) move with the new entries, so each currency
    still adds up to zero.

    Example: transaction 2 transferred 40 USD from account 5 to account 6 (54.00 SGD). Reversing it
    creates transaction 3 (type REVERSAL, reversal_of 2) with the entries swapped.

    Double reversal is blocked three ways: a REVERSAL cannot be reversed; the original's row is
    locked and checked for an existing reversal; and transactions.reversal_of is UNIQUE, so the
    database itself rejects a second reversal even if two requests race.

    Returns: {"transaction_id", "reversal_of", "entries": [{"account_id", "entry_type", "amount"}]}
    Errors:
      409  idempotency_key was already used
      404  transaction not found
      400  the transaction is itself a reversal
      409  the transaction was already reversed
      400  a customer account no longer holds enough money to reverse (for example the
           recipient already spent it). Reversing then needs a separate process (a dispute or
           a compensating transfer), which this API does not provide.
    """
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
    """Return one account and its current balance (always read from the database).

    Errors: 404 account not found
    """
    account = db.query(models.Account).filter_by(id=account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return account

@app.get("/accounts/{account_id}/transactions")
def get_transactions(account_id: int, db: Session = Depends(get_db), page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), order: Literal["asc", "desc"] = "desc",):
    """Return an account's ledger entries, paginated like a bank statement.

    Query params: page (starts at 1), page_size (default 20, max 100),
                  order ("desc" = newest first, the default; "asc" = oldest first).
    Returns: {"page", "page_size", "order", "results": [{"transaction_id", "type",
              "reversal_of", "entry_type", "amount", "description", "created_at"}]}
    `type` is DEPOSIT, WITHDRAWAL, TRANSFER or REVERSAL. `reversal_of` is the id of the
    transaction a REVERSAL undoes (null for every other type).
    Entries are ordered by the transaction's created_at, then by entry id,
    so the order is stable when two entries share the same timestamp and pages never overlap.
    Errors: 404 account not found, 422 page < 1, page_size outside 1-100, or order is not "asc" or "desc"
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
    """Convert an amount between two supported currencies using a live Frankfurter rate.

    Query params: from_currency (e.g. USD), to_currency (e.g. SGD), amount (default 1).
    Example: GET /fx/convert?from_currency=USD&to_currency=SGD&amount=100

    Read-only quote tool: it does not touch any account or the ledger. Nothing is stored; /transfer
    fetches its own live rate at execution time and saves only the rate it actually used.
    The rate is parsed as a Decimal (never float) and the result is rounded to 2 decimal places.

    Returns: {"from_currency", "to_currency", "amount", "rate", "converted_amount",
              "rate_date", "source"}
    Errors:
      400  unsupported currency, or amount is not greater than zero
      503  the rate could not be fetched (timeout, error or bad response from Frankfurter)
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
    """Round a database sum to 2 decimal places (the scale every amount is stored at)."""
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@app.get("/reconciliation/month-end")
def month_end_reconciliation(month: str, db: Session = Depends(get_db)):
    """Month-end check against the transaction history. Two checks, both read-only.

    Query param: month, "YYYY-MM" (e.g. "2026-10"). Balances are live, so run this at month-end
    close. The month only shapes the opening / credits / debits / closing columns.

    Check 1, per account: the stored balance must equal the ledger balance, which is the sum of
    the account's CREDIT entries minus the sum of its DEBIT entries (all entries, all time).
    Example: Alice has CREDIT 100 and DEBIT 40, so her ledger balance is 60. If the stored
    balance is 70, difference = 70 - 60 = 10 and she is a MISMATCH. This covers system accounts
    too, because EXTERNAL and FX_POSITION balances move with every entry.

    Check 2, per currency: all stored balances (customers, EXTERNAL, FX_POSITION) must add up to
    exactly 0.00, because every transaction's entries net to zero within each currency.

    Nothing is edited. One query reads every balance and every ledger sum at the same moment,
    so a transfer committing mid-report cannot cause a false result. Both checks are built from
    that one result.

    Returns: {"month", "month_has_ended", "status" (RECONCILED / OUT_OF_BALANCE),
              "summary": {"accounts_checked", "accounts_mismatched"},
              "currencies": [{"currency", "customers_total", "external_total",
                              "fx_position_total", "net", "status"}],
              "accounts": [{"account_id", "owner_name", "currency", "is_system",
                            "opening_balance", "total_credits", "total_debits", "closing_balance",
                            "stored_balance", "ledger_balance", "difference", "status"}]}
    Amounts are strings with 2 decimal places. `difference` is stored minus ledger, so 0.00 means
    reconciled. opening/closing are ledger balances at the start and end of the month.
    Returns 200 even when out of balance; read "status".
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

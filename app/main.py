"""Internal ledger API (FastAPI + SQLAlchemy + Postgres).

Model
-----
- Double-entry: every money movement is one Transaction with two LedgerEntry rows
  (a DEBIT and a CREDIT of the same amount, in each account's own currency).
- Money enters/leaves the system through a special account named "EXTERNAL".
  Deposit:  DEBIT EXTERNAL, CREDIT user.   Withdrawal: DEBIT user, CREDIT EXTERNAL.
- Amounts are Decimal (never float). Only USD and SGD are supported (2 decimal places).

Safety rules used by every write endpoint
-----------------------------------------
- Idempotency: each request carries an `idempotency_key`. A key that was already used
  is rejected with 409, so a retried request cannot move money twice.
- Row locks: accounts are loaded with SELECT ... FOR UPDATE so concurrent requests on
  the same account run one at a time and no update is lost.
- Atomicity: ledger entries and balance changes are committed together (all or nothing).
"""

from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, field_validator
from .database import SessionLocal, engine, Base
from . import models
from decimal import Decimal
from sqlalchemy import func
from typing import Optional
from decimal import Decimal



SUPPORTED_CURRENCIES = ["USD", "SGD"]

app = FastAPI()

Base.metadata.create_all(bind=engine)

def get_db():
    """FastAPI dependency: give each request its own DB session, and always close it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Request / response schemas (Pydantic validates incoming JSON against these)
# ---------------------------------------------------------------------------

class AccountCreate(BaseModel):
    """Body of POST /accounts."""
    owner_name: str
    currency: str = "USD"


class AccountOut(BaseModel):
    """Account as returned to the client."""
    id: int
    owner_name: str
    currency: str
    balance: float

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
        # value is the "amount" from the incoming JSON
        # e.g. value = 100 (valid), value = -50 (invalid), value = 0 (invalid)
        if value <= 0:
            raise ValueError("Amount must be greater than zero")
        return value


class WithdrawalRequest(BaseModel):
    """Body of POST /withdraw. `idempotency_key` must be unique per request."""
    account_id: int
    amount: Decimal
    idempotency_key: str

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, value):
        if value <= 0:
            raise ValueError("Amount must be greater than zero")
        return value


class TransferRequest(BaseModel):
    """Body of POST /transfer. `amount` is in the SOURCE account's currency."""
    from_account_id: int
    to_account_id: int
    amount: Decimal
    idempotency_key: str

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, value):
        if value <= 0:
            raise ValueError("Amount must be greater than zero")
        return value


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


@app.post("/deposit")
async def deposit(req: DepositRequest, db: Session = Depends(get_db)):
    """Add money to an account (money comes in from EXTERNAL).

    Ledger entries: DEBIT EXTERNAL, CREDIT the user's account.

    Returns: {"transaction_id", "new_balance"}
    Errors:
      409  idempotency_key was already used
      404  account not found
      500  the EXTERNAL account has not been created
    """
    # idempotency check: has this exact request been processed before?
    existing = db.query(models.Transaction).filter_by(idempotency_key=req.idempotency_key).first()
    if existing:
        raise HTTPException(status_code=409, detail="Duplicate request")

    # with_for_update() locks the row so no other transaction can modify it until we commit or rollback
    account = db.query(models.Account).filter_by(id=req.account_id).with_for_update().first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    # find the EXTERNAL account dynamically instead of hardcoding its id
    external = db.query(models.Account).filter_by(owner_name="EXTERNAL", currency=account.currency).first()
    if not external:
        raise HTTPException(status_code=500, detail="EXTERNAL account not configured")

    # create the transaction record
    txn = models.Transaction(idempotency_key=req.idempotency_key, description="Deposit")
    db.add(txn)
    db.flush()  # gets txn.id without committing yet

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

    # update balance
    account.balance += req.amount

    # commit everything together atomically: all or nothing
    db.commit()
    db.refresh(account)
    return {"transaction_id": txn.id, "new_balance": account.balance}


@app.post("/withdraw")
async def withdraw(req: WithdrawalRequest, db: Session = Depends(get_db)):
    """Take money out of an account (money goes out to EXTERNAL).

    Ledger entries: DEBIT the user's account, CREDIT EXTERNAL.

    Returns: {"transaction_id", "new_balance"}
    Errors:
      409  idempotency_key was already used
      404  account not found
      400  insufficient funds
      500  the EXTERNAL account has not been created
    """
    # example values below: req.idempotency_key = "wdr-001", balance = 100, req.amount = 40

    # check if this exact request was already processed before
    existing = db.query(models.Transaction).filter_by(idempotency_key=req.idempotency_key).first()
    if existing:
        raise HTTPException(status_code=409, detail="Duplicate request")
     
    # with_for_update() locks the row so no other transaction can modify it until we commit or rollback
    account = db.query(models.Account).filter_by(id=req.account_id).with_for_update().first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
   
    # find the EXTERNAL account dynamically instead of hardcoding its id
    external = db.query(models.Account).filter_by(owner_name="EXTERNAL", currency=account.currency).first()
    if not external:
        raise HTTPException(status_code=500, detail="EXTERNAL account not configured")

    # does the account have enough money? (e.g. balance 100 >= amount 40)
    if account.balance < req.amount:
        raise HTTPException(status_code=400, detail="Insufficient funds")

    # create one transaction record grouping the two ledger entries together
    txn = models.Transaction(idempotency_key=req.idempotency_key, description="Withdrawal")
    db.add(txn)
    db.flush()  # get txn.id without committing yet

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

    # update balance: 100 - 40 = 60
    account.balance -= req.amount

    # commit everything together atomically: all or nothing
    db.commit()
    db.refresh(account)
    return {"transaction_id": txn.id, "new_balance": account.balance}


@app.post("/transfer")
async def transfer(req: TransferRequest, db: Session = Depends(get_db)):
    """Move money from one account to another in a single atomic operation.

    `amount` is in the source account's currency. If the two accounts use different
    currencies, the latest stored ExchangeRate (source -> destination) converts it,
    and the destination is credited the converted amount in its own currency.

    Ledger entries: DEBIT source (amount), CREDIT destination (credited_amount).

    Returns: {"transaction_id", "exchange_rate_used" (null if same currency),
              "from_account_new_balance", "to_account_new_balance"}
    Errors:
      409  idempotency_key was already used
      400  same source and destination account
      404  an account was not found
      400  insufficient funds in the source account
      400  no exchange rate stored for the currency pair
    """
    # check if this exact transfer was already processed before
    existing = db.query(models.Transaction).filter_by(idempotency_key=req.idempotency_key).first()
    if existing:
        raise HTTPException(status_code=409, detail="Duplicate request")

    # prevent transferring to yourself
    if req.from_account_id == req.to_account_id:
        raise HTTPException(status_code=400, detail="Cannot transfer to the same account")

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

    # same currency: no conversion needed
    if from_account.currency == to_account.currency:
        exchange_rate_used = None
        credited_amount = req.amount
    else:
        # different currencies: look up the most recent rate we've stored
        rate_row = (
            db.query(models.ExchangeRate)
            .filter_by(base_currency=from_account.currency, quote_currency=to_account.currency)
            .order_by(models.ExchangeRate.fetched_at.desc())
            .first()
        )
        if not rate_row:
            raise HTTPException(
                status_code=400,
                detail=f"No exchange rate available for {from_account.currency} -> {to_account.currency}"
            )
        exchange_rate_used = rate_row.rate
        credited_amount = req.amount * exchange_rate_used

    # create one transaction record grouping both ledger entries together
    txn = models.Transaction(
        idempotency_key=req.idempotency_key,
        description="Transfer",
        exchange_rate_used=exchange_rate_used
    )
    db.add(txn)
    db.flush()  # get txn.id without committing yet

    # debit the source, in its own currency
    debit_entry = models.LedgerEntry(
        transaction_id=txn.id,
        account_id=from_account.id,
        entry_type=models.EntryType.DEBIT,
        amount=req.amount
    )
    # credit the destination, in its own currency (converted, if currencies differ)
    credit_entry = models.LedgerEntry(
        transaction_id=txn.id,
        account_id=to_account.id,
        entry_type=models.EntryType.CREDIT,
        amount=credited_amount
    )
    db.add_all([debit_entry, credit_entry])

    # update both balances, each in their own currency
    from_account.balance -= req.amount
    to_account.balance += credited_amount

    # commit everything together atomically: all or nothing
    db.commit()
    db.refresh(from_account)
    db.refresh(to_account)
    return {
        "transaction_id": txn.id,
        "exchange_rate_used": exchange_rate_used,
        "from_account_new_balance": from_account.balance,
        "to_account_new_balance": to_account.balance
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
def get_transactions(account_id: int, db: Session = Depends(get_db), page: int = 1, page_size: int = 20):
    """Return an account's ledger entries, paginated like a bank statement.

    Query params: page (starts at 1), page_size (default 20).
    Returns: {"page", "page_size", "results": [{"transaction_id", "entry_type",
              "amount", "description"}]}
    Note: no explicit ORDER BY yet, so entries are returned in database order.
    Errors: 404 account not found
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
    entries = db.query(models.LedgerEntry)\
        .filter_by(account_id=account_id)\
        .offset(offset)\
        .limit(page_size)\
        .all()

    # shape the response: for each entry, return the relevant details
    result = []
    for entry in entries:
        result.append({
            "transaction_id": entry.transaction_id,
            "entry_type": entry.entry_type,   # DEBIT or CREDIT
            "amount": entry.amount,
            "description": entry.transaction.description  # e.g. "Deposit", "Withdrawal", "Transfer"
        })

    return {
        "page": page,
        "page_size": page_size,
        "results": result
    }

from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, field_validator
from .database import SessionLocal, engine, Base
from . import models
from decimal import Decimal
from sqlalchemy import func
import logging
from prometheus_fastapi_instrumentator import Instrumentator

SUPPORTED_CURRENCIES = ["USD", "SGD"]

app = FastAPI()
Instrumentator().instrument(app).expose(app)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# This function gives each request its own database session, and closes it when done
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Pydantic schema: what a "create account" request must look like
class AccountCreate(BaseModel):
    owner_name: str
    currency: str = "USD"

# Pydantic schema: what we send back in the response
class AccountOut(BaseModel):
    id: int
    owner_name: str
    currency: str
    balance: float

    class Config:
        from_attributes = True  # lets Pydantic read SQLAlchemy objects directly

class DepositRequest(BaseModel):
    account_id: int
    amount: Decimal
    idempotency_key: str

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, value):
        # value is the value of "amount" from the incoming JSON
        # e.g. value = 100 (valid), value = -50 (invalid), value = 0 (invalid)
        if value <= 0:
            raise ValueError("Amount must be greater than zero")
        return value
    
class WithdrawalRequest(BaseModel):
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

# create a new account
@app.post("/accounts", response_model=AccountOut)
def create_account(account: AccountCreate, db: Session = Depends(get_db)):
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
    # idempotency check: has this exact request been processed before?
    existing = db.query(models.Transaction).filter_by(idempotency_key=req.idempotency_key).first()
    if existing:
        raise HTTPException(status_code=409, detail="Duplicate request")
    # find the EXTERNAL account dynamically instead of hardcoding its id
    external = db.query(models.Account).filter_by(owner_name="EXTERNAL").first()
    if not external:
        raise HTTPException(status_code=500, detail="EXTERNAL account not configured")

    # with_for_update() locks the row so no other transaction can modify it until we commit or rollback
    account = db.query(models.Account).filter_by(id=req.account_id).with_for_update().first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    # create the transaction record
    txn = models.Transaction(idempotency_key=req.idempotency_key, description="Deposit")
    db.add(txn)
    db.flush()  # gets txn.id without committing yet

    # two ledger entries: debit EXTERNAL, credit the user's account
    debit_entry = models.LedgerEntry(
        transaction_id=txn.id,
        account_id=external.id,  # EXTERNAL account
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

    # update balances
    account.balance += req.amount

    db.commit()
    db.refresh(account)
    return {"transaction_id": txn.id, "new_balance": account.balance}

@app.post("/withdraw")
async def withdraw(req: WithdrawalRequest, db: Session = Depends(get_db)):
    # req.idempotency_key = "wdr-001" (from JSON)
    # check if this exact request was already processed before
    existing = db.query(models.Transaction).filter_by(idempotency_key=req.idempotency_key).first()
    if existing:
        raise HTTPException(status_code=409, detail="Duplicate request")
    

        # find the EXTERNAL account dynamically instead of hardcoding its id
    external = db.query(models.Account).filter_by(owner_name="EXTERNAL").first()
    if not external:
        raise HTTPException(status_code=500, detail="EXTERNAL account not configured")


    # with_for_update() locks the row so no other transaction can modify it until we commit or rollback
    account = db.query(models.Account).filter_by(id=req.account_id).with_for_update().first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    # account.balance = 100 (from Postgres), req.amount = 40 (from JSON)
    # does Alice have enough money to withdraw?
    if account.balance < req.amount:
        raise HTTPException(status_code=400, detail="Insufficient funds")

    # create one transaction record grouping the two ledger entries together
    txn = models.Transaction(idempotency_key=req.idempotency_key, description="Withdrawal")
    db.add(txn)
    db.flush()  # get txn.id without committing yet

    # debit Alice (money leaving her account)
    debit_entry = models.LedgerEntry(
        transaction_id=txn.id,
        account_id=account.id,          # Alice = id 5
        entry_type=models.EntryType.DEBIT,
        amount=req.amount               # 40
    )
    # credit EXTERNAL (money leaving the system)
    credit_entry = models.LedgerEntry(
        transaction_id=txn.id,
        account_id=external.id,  # EXTERNAL account
        entry_type=models.EntryType.CREDIT,
        amount=req.amount               # 40
    )
    db.add_all([debit_entry, credit_entry])

    # update Alice's balance: 100 - 40 = 60
    account.balance -= req.amount

    # commit everything together atomically — all or nothing
    db.commit()
    db.refresh(account)
    return {"transaction_id": txn.id, "new_balance": account.balance}

@app.post("/transfer")  
async def transfer(req: TransferRequest, db: Session = Depends(get_db)):
    # check if this exact transfer was already processed before
    existing = db.query(models.Transaction).filter_by(idempotency_key=req.idempotency_key).first()
    if existing:
        raise HTTPException(status_code=409, detail="Duplicate request")
    
    # prevent transferring to yourself
    if req.from_account_id == req.to_account_id:
        raise HTTPException(status_code=400, detail="Cannot transfer to the same account")

    # always lock in consistent id order to prevent deadlocks
    lock_first_id = min(req.from_account_id, req.to_account_id)
    lock_second_id = max(req.from_account_id, req.to_account_id)
    
    first = db.query(models.Account).filter_by(id=lock_first_id).with_for_update().first()
    second = db.query(models.Account).filter_by(id=lock_second_id).with_for_update().first()

    if not first or not second:
        raise HTTPException(status_code=404, detail="Account not found")

    from_account = first if first.id == req.from_account_id else second
    to_account = second if second.id == req.to_account_id else first

    # does Alice have enough money to send? (checked in from_account's own currency, before conversion)
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

    # debit Alice, in her own currency
    debit_entry = models.LedgerEntry(
        transaction_id=txn.id,
        account_id=from_account.id,
        entry_type=models.EntryType.DEBIT,
        amount=req.amount
    )
    # credit Bob, in his own currency (converted, if currencies differ)
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

    db.commit()
    db.refresh(from_account)
    db.refresh(to_account)
    return {
        "transaction_id": txn.id,
        "exchange_rate_used": exchange_rate_used,
        "from_account_new_balance": from_account.balance,
        "to_account_new_balance": to_account.balance
    }

# fetch account details (always read from the database)
@app.get("/accounts/{account_id}", response_model=AccountOut)
def get_account(account_id: int, db: Session = Depends(get_db)):
    account = db.query(models.Account).filter_by(id=account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return account

# fetch all transactions for a given account, with pagination like statements
@app.get("/accounts/{account_id}/transactions")
def get_transactions(account_id: int, db: Session = Depends(get_db), page : int = 1, page_size: int = 20):
    # check account exists first
    account = db.query(models.Account).filter_by(id=account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    # calculate how many rows to skip
    # page 1: skip 0, page 2: skip 20, page 3: skip 40
    offset = (page - 1) * page_size

    # fetch all ledger entries for this account
    # each entry links to a transaction, so we can see the full history
    entries = db.query(models.LedgerEntry)\
        .filter_by(account_id=account_id)\
        .offset(offset)\
        .limit(page_size)\
        .all()

    # shape the response — for each entry, return the relevant details
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
from fastapi import FastAPI, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel, field_validator
from .database import SessionLocal
from . import models
from decimal import Decimal
from sqlalchemy import func
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from datetime import datetime, timedelta
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from .redis_client import redis_client
import logging
import secrets
from contextlib import asynccontextmanager
from prometheus_fastapi_instrumentator import Instrumentator

SECRET_KEY = "your-secret-key-change-this-in-production"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
REFRESH_TOKEN_EXPIRE_DAYS = 7
SUPPORTED_CURRENCIES = ["USD", "EUR", "GBP", "SGD", "JPY"]
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await redis_client.close()

app = FastAPI(lifespan=lifespan)
Instrumentator().instrument(app).expose(app)


limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class RefreshTokenRequest(BaseModel):
    refresh_token: str

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

class UserCreate(BaseModel):
    username: str
    password: str

class DepositRequest(BaseModel):
    account_id: int
    amount: Decimal
    idempotency_key: str

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, v):
        # v is the value of "amount" from the incoming JSON
        # e.g. v = 100 (valid), v = -50 (invalid), v = 0 (invalid)
        if v <= 0:
            raise ValueError("Amount must be greater than zero")
        return v
    
class WithdrawalRequest(BaseModel):
    account_id: int
    amount: Decimal
    idempotency_key: str

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, v):
        if v <= 0:
            raise ValueError("Amount must be greater than zero")
        return v
    
class TransferRequest(BaseModel):
    from_account_id: int
    to_account_id: int
    amount: Decimal
    idempotency_key: str

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, v):
        if v <= 0:
            raise ValueError("Amount must be greater than zero")
        return v

def create_refresh_token() -> str:
    # generates a random 64-character string — not a JWT, just a random token
    # stored in DB so it can be blacklisted
    return secrets.token_hex(64)

def hash_password(password: str) -> str:
    # converts "mysecret" → "$2b$12$randomhashstring..."
    return pwd_context.hash(password[:72])

def verify_password(plain_password: str, hashed_password: str) -> bool:
    # checks if "mysecret" matches the stored hash — returns True or False
    return pwd_context.verify(plain_password[:72], hashed_password)

def create_access_token(data: dict) -> str:
    # data = {"sub": "alice"} — sub means "subject" (who this token is for)
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})  # add expiry time to token
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    # this runs on every protected endpoint — verifies the token
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

    user = db.query(models.User).filter_by(username=username).first()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user

@app.post("/register")
@limiter.limit("5/minute")
def register(request: Request, user: UserCreate, db: Session = Depends(get_db)):
    # check if username already exists
    existing = db.query(models.User).filter_by(username=user.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists")

    # hash the password before saving — never store plain text
    new_user = models.User(
        username=user.username,
        hashed_password=hash_password(user.password)
    )
    db.add(new_user)
    db.commit()
    return {"message": "User created successfully"}

@app.post("/login")
@limiter.limit("5/minute")
def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(models.User).filter_by(username=form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # create access token (short lived)
    access_token = create_access_token(data={"sub": user.username})

    # create refresh token (long lived, stored in DB)
    refresh_token = create_refresh_token()
    expires_at = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    db_refresh_token = models.RefreshToken(
        token=refresh_token,
        user_id=user.id,
        expires_at=expires_at
    )
    db.add(db_refresh_token)
    db.commit()

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer"
    }

@app.post("/deposit")
@limiter.limit("10/minute")
async def deposit(request: Request,  req: DepositRequest, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
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
    await redis_client.delete(f"account:{req.account_id}")
    db.refresh(account)
    return {"transaction_id": txn.id, "new_balance": account.balance}

@app.post("/withdraw")
@limiter.limit("10/minute")
async def withdraw(request: Request, req: WithdrawalRequest, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
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
    await redis_client.delete(f"account:{req.account_id}")
    db.refresh(account)
    return {"transaction_id": txn.id, "new_balance": account.balance}

@app.post("/transfer")
@limiter.limit("10/minute")         
async def transfer(request: Request, req: TransferRequest, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
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
    await redis_client.delete(f"account:{from_account.id}")
    await redis_client.delete(f"account:{to_account.id}")
    db.refresh(from_account)
    db.refresh(to_account)
    return {
        "transaction_id": txn.id,
        "exchange_rate_used": exchange_rate_used,
        "from_account_new_balance": from_account.balance,
        "to_account_new_balance": to_account.balance
    }

# fetch account details, with caching
@app.get("/accounts/{account_id}", response_model=AccountOut)
@limiter.limit("5/minute")
async def get_account(request: Request, account_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    cache_key = f"account:{account_id}"
    cached = await redis_client.get(cache_key)
    if cached:
        logger.info(f"CACHE HIT: account {account_id}")
        return AccountOut.model_validate_json(cached)
    
    logger.info(f"CACHE MISS: account {account_id}")
    account = db.query(models.Account).filter_by(id=account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    await redis_client.set(cache_key, AccountOut.model_validate(account).model_dump_json(), ex=30)
    return account

# fetch all transactions for a given account, with pagination like statements
@app.get("/accounts/{account_id}/transactions")
@limiter.limit("5/minute")
def get_transactions(request: Request, account_id: int, db: Session = Depends(get_db), page : int = 1, page_size: int = 20, current_user: models.User = Depends(get_current_user)):
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

# integrity check endpoint: recompute the balance from ledger entries and compare to stored balance
@app.get("/accounts/{account_id}/integrity")
@limiter.limit("5/minute")
def check_integrity(request: Request, account_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    # fetch the account
    account = db.query(models.Account).filter_by(id=account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    # sum all CREDIT entries for this account
    # func.sum() is SQLAlchemy's way of running SUM() in SQL
    total_credits = db.query(func.sum(models.LedgerEntry.amount)).filter(
        models.LedgerEntry.account_id == account_id,
        models.LedgerEntry.entry_type == models.EntryType.CREDIT
    ).scalar() or Decimal(0)  # scalar() returns a single value, not a list
                               # "or Decimal(0)" handles case where no entries exist yet

    # sum all DEBIT entries for this account
    total_debits = db.query(func.sum(models.LedgerEntry.amount)).filter(
        models.LedgerEntry.account_id == account_id,
        models.LedgerEntry.entry_type == models.EntryType.DEBIT
    ).scalar() or Decimal(0)

    # recompute balance from ledger entries
    computed_balance = total_credits - total_debits

    # compare against stored balance
    is_valid = computed_balance == account.balance

    return {
        "account_id": account_id,
        "stored_balance": account.balance,
        "computed_balance": computed_balance,
        "is_valid": is_valid  # True = balances match, False = discrepancy detected
    }

# create a new account for the current user
@app.post("/accounts", response_model=AccountOut)
@limiter.limit("5/minute")
def create_account(request: Request, account: AccountCreate, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    new_account = models.Account(
        owner_name=account.owner_name,
        currency=account.currency,
        balance=0
    )
    db.add(new_account)
    db.commit()
    db.refresh(new_account)
    return new_account

@app.post("/refresh")
@limiter.limit("10/minute")
def refresh(request: Request, req: RefreshTokenRequest, db: Session = Depends(get_db)):
    db_token = db.query(models.RefreshToken).filter_by(token=req.refresh_token).first()

    if not db_token:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    if db_token.is_revoked:
        raise HTTPException(status_code=401, detail="Refresh token revoked")
    if db_token.expires_at < datetime.utcnow():
        raise HTTPException(status_code=401, detail="Refresh token expired")

    new_access_token = create_access_token(data={"sub": db_token.user.username})
    return {"access_token": new_access_token, "token_type": "bearer"}

@app.post("/logout")
@limiter.limit("10/minute")
def logout(request: Request, req: RefreshTokenRequest, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    db_token = db.query(models.RefreshToken).filter_by(token=req.refresh_token, user_id=current_user.id).first()
    if not db_token:
        raise HTTPException(status_code=404, detail="Refresh token not found")

    db_token.is_revoked = True
    db.commit()
    return {"message": "Logged out successfully"}

@app.post("/dev-setup")
def dev_setup(db: Session = Depends(get_db)):
    # create EXTERNAL account
    external = models.Account(owner_name="EXTERNAL", currency="USD", balance=0)
    db.add(external)
    # create Alice
    alice = models.Account(owner_name="Alice", currency="USD", balance=0)
    db.add(alice)
    db.commit()
    db.refresh(external)
    db.refresh(alice)
    return {"external_id": external.id, "alice_id": alice.id}
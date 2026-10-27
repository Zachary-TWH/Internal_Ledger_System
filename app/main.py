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

from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, field_validator
from .database import SessionLocal
from . import models
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from sqlalchemy import func, case, and_
from typing import Literal
from decimal import Decimal


SUPPORTED_CURRENCIES = ["USD", "SGD"]

# Free, no-API-key FX rate service (European Central Bank reference rates, updated on working days)
FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest"
FX_TIMEOUT_SECONDS = 5


class FxRateError(Exception):
    """The live FX rate could not be obtained. `timed_out` is True when the service was too slow."""

    def __init__(self, message: str, timed_out: bool = False):
        super().__init__(message)
        self.message = message
        self.timed_out = timed_out


def fetch_fx_rate(from_currency: str, to_currency: str):
    """Fetch the live rate (1 from_currency = rate to_currency) from Frankfurter.

    Returns (rate, rate_date). The rate is a Decimal rounded to 6 places, the scale of
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
        rate = Decimal(str(payload["rates"][to_currency])).quantize(Decimal("0.000001"))
        rate_date = payload.get("date")
    except TimeoutError:
        raise FxRateError("FX rate service timed out", timed_out=True)
    except urllib.error.URLError as exc:
        # URLError covers HTTPError (non-2xx) and network failures; a connect timeout lands here too
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            raise FxRateError("FX rate service timed out", timed_out=True)
        raise FxRateError("FX rate service unavailable")
    except (KeyError, ValueError, TypeError, ArithmeticError):
        raise FxRateError("FX rate service returned an unexpected response")

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
def deposit(req: DepositRequest, db: Session = Depends(get_db)):
    """Add money to an account (money comes in from EXTERNAL).

    Ledger entries: DEBIT EXTERNAL, CREDIT the user's account.
    Balances: the user's goes up and EXTERNAL's goes down by the same amount (so the
    currency still sums to zero).

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

    # lock this currency's EXTERNAL account too (after the user's row, always in this order)
    external = lock_system_account(db, "EXTERNAL", account.currency)

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

    # update both balances: the user gains, EXTERNAL goes down by the same amount
    account.balance += req.amount
    external.balance -= req.amount

    # commit everything together atomically: all or nothing
    db.commit()
    db.refresh(account)
    return {"transaction_id": txn.id, "new_balance": account.balance}


@app.post("/withdraw")
def withdraw(req: WithdrawalRequest, db: Session = Depends(get_db)):
    """Take money out of an account (money goes out to EXTERNAL).

    Ledger entries: DEBIT the user's account, CREDIT EXTERNAL.
    Balances: the user's goes down and EXTERNAL's goes up by the same amount.

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
   
    # lock this currency's EXTERNAL account too (after the user's row, always in this order)
    external = lock_system_account(db, "EXTERNAL", account.currency)

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

    # update both balances: 100 - 40 = 60 for the user, and EXTERNAL goes up by 40
    account.balance -= req.amount
    external.balance += req.amount

    # commit everything together atomically: all or nothing
    db.commit()
    db.refresh(account)
    return {"transaction_id": txn.id, "new_balance": account.balance}


@app.post("/transfer")
def transfer(req: TransferRequest, db: Session = Depends(get_db)):
    """Move money from one account to another in a single atomic operation.

    `amount` is in the source account's currency. If the two accounts use different
    currencies, a live rate (source -> destination) is fetched from the Frankfurter API at
    execution time, and the destination is credited the converted amount (rounded to 2 dp)
    in its own currency. No rates are kept in a table: only the rate actually used is saved,
    on the transaction (`exchange_rate_used`).

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
      400  insufficient funds in the source account
      503  cross-currency transfer and the live FX rate is unavailable (nothing is recorded)
    """
    # check if this exact transfer was already processed before
    existing = db.query(models.Transaction).filter_by(idempotency_key=req.idempotency_key).first()
    if existing:
        raise HTTPException(status_code=409, detail="Duplicate request")

    # prevent transferring to yourself
    if req.from_account_id == req.to_account_id:
        raise HTTPException(status_code=400, detail="Cannot transfer to the same account")

    # Work out whether this is a cross-currency transfer BEFORE locking anything.
    # We select plain columns (not Account objects) so no stale balance is cached in this
    # session; the locked read below is the only one that loads balances. A currency never
    # changes after an account is created, so reading it without a lock is safe.
    currencies = dict(
        db.query(models.Account.id, models.Account.currency)
        .filter(models.Account.id.in_([req.from_account_id, req.to_account_id]))
        .all()
    )
    if len(currencies) != 2:
        raise HTTPException(status_code=404, detail="Account not found")

    exchange_rate_used = None
    if currencies[req.from_account_id] != currencies[req.to_account_id]:
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
        description="Transfer",
        exchange_rate_used=exchange_rate_used
    )
    db.add(txn)
    db.flush()  # get txn.id without committing yet

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
def get_transactions(account_id: int, db: Session = Depends(get_db), page: int = 1, page_size: int = 20, order: Literal["asc", "desc"] = "desc",):
    """Return an account's ledger entries, paginated like a bank statement.

    Query params: page (starts at 1), page_size (default 20),
                  order ("desc" = newest first, the default; "asc" = oldest first).
    Returns: {"page", "page_size", "order", "results": [{"transaction_id", "entry_type",
              "amount", "description", "created_at"}]}
    Entries are ordered by the transaction's created_at, then by entry id,
    so the order is stable when two entries share the same timestamp and pages never overlap.
    Errors: 404 account not found, 422 order is not "asc" or "desc"
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
            "entry_type": entry.entry_type,   # DEBIT or CREDIT
            "amount": entry.amount,
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
      502  Frankfurter returned an error or an unexpected response
      504  Frankfurter did not answer in time
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
        raise HTTPException(status_code=504 if exc.timed_out else 502, detail=exc.message)

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
    """Month-end check: for each currency, all account balances must add up to exactly 0.00.

    Query param: month, "YYYY-MM" (e.g. "2026-10"). It labels the report; the balances are
    read live at the moment of the call, so run it at month-end close.

    Why the sum is zero: every transaction's entries net to zero within each currency, and
    every account's balance (customer, EXTERNAL and FX_POSITION) moves with its entries.
    Customers' money is matched by EXTERNAL (net deposits less withdrawals, shown negative)
    and FX_POSITION (net currency bought or sold through conversions).

    Read-only: it never edits a balance. One query reads every balance at the same moment,
    so a transfer committing mid-report cannot cause a false result.

    Returns: {"month", "month_has_ended", "status" (RECONCILED / OUT_OF_BALANCE),
              "currencies": [{"currency", "customers_total", "external_total",
                              "fx_position_total", "net", "status"}]}
    Amounts are strings with 2 decimal places. A non-zero "net" means something was written
    outside the normal path. It does not say which account or transaction is wrong.
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

    def total_of(*conditions):
        """SUM of balances for the accounts matching the conditions (0 if none)."""
        return func.coalesce(func.sum(case((and_(*conditions), Account.balance), else_=0)), 0)

    rows = (
        db.query(
            Account.currency,
            total_of(Account.is_system.is_(False)).label("customers"),
            total_of(Account.is_system.is_(True), Account.owner_name == "EXTERNAL").label("external"),
            total_of(Account.is_system.is_(True), Account.owner_name == "FX_POSITION").label("fx_position"),
            func.coalesce(func.sum(Account.balance), 0).label("net"),
        )
        .group_by(Account.currency)
        .order_by(Account.currency)
        .all()
    )

    currencies = []
    for currency, customers, external, fx_position, net in rows:
        net = _money(net)
        currencies.append({
            "currency": currency,
            "customers_total": str(_money(customers)),
            "external_total": str(_money(external)),
            "fx_position_total": str(_money(fx_position)),
            "net": str(net),
            "status": "OK" if net == 0 else "OUT_OF_BALANCE",
        })

    balanced = all(c["status"] == "OK" for c in currencies)
    return {
        "month": month,
        # false = the month is still in progress (timestamps are UTC, stored without a timezone)
        "month_has_ended": datetime.now(timezone.utc).replace(tzinfo=None) >= next_month_start,
        "status": "RECONCILED" if balanced else "OUT_OF_BALANCE",
        "currencies": currencies,
    }

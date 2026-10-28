"""SQLAlchemy models for the financial ledger.

Three tables:
    accounts        one row per account (customer or system), with a stored balance
    transactions    one row per business event (deposit, withdrawal, transfer, reversal)
    ledger_entries  the DEBIT / CREDIT postings, 2 or 4 per transaction. The source of truth.

    accounts 1 ---- * ledger_entries * ---- 1 transactions
    transactions.reversal_of points back at another row of the same table (a reversal and the
    transaction it undoes).

These models define the database schema. They do not implement transaction
processing, balance updates, FX pricing, or netting logic: that all lives in main.py.
The tables are created by create_table.py.
"""

import enum

from sqlalchemy import (
    Column,
    Integer,
    String,
    Numeric,
    Boolean,
    ForeignKey,
    DateTime,
    Enum,
    CheckConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .database import Base


class EntryType(str, enum.Enum):
    """Direction of a ledger posting.

    A DEBIT lowers the account's stored balance and a CREDIT raises it, for every kind of account
    (system accounts included). The entry's amount is always positive, so the direction lives here.
    """

    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class TransactionType(str, enum.Enum):
    """What kind of business event a transaction is. Code checks this, not the description.

    DEPOSIT     money in from outside:  DEBIT EXTERNAL, CREDIT the customer.
    WITHDRAWAL  money out to outside:   DEBIT the customer, CREDIT EXTERNAL.
    TRANSFER    customer to customer, in one currency (2 entries) or across two (4 entries, via FX_POSITION).
    REVERSAL    undoes an earlier transaction with offsetting entries. See Transaction.reversal_of.
    """

    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    TRANSFER = "TRANSFER"
    REVERSAL = "REVERSAL"


class Account(Base):
    """A customer or internal financial account denominated in one currency.

    `balance` is a stored balance that must be updated consistently with
    ledger postings. The ledger should provide the audit trail.
    `is_system` marks internal accounts such as EXTERNAL, which users
    cannot create through the API.

    Columns:
      owner_name  free text and not unique. System accounts use the fixed names EXTERNAL and FX_POSITION.
      currency    "USD" or "SGD", fixed at creation. Every entry on the account is in this currency.
      balance     NUMERIC(18,2), changed by every posting, for customer and system accounts alike.
                  The ledger entries are the source of truth: month-end reconciliation compares them.
                  A customer balance is never allowed below zero, but that rule is enforced in
                  main.py, not by a database constraint. System balances may go negative
                  (EXTERNAL is negative once more has come in than gone out).
      is_system   true only for the 4 accounts seeded by create_table.py (EXTERNAL and FX_POSITION,
                  one of each per currency). Always false for accounts made through POST /accounts.
    """

    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    owner_name = Column(String, nullable=False)
    currency = Column(String, nullable=False, default="USD")
    balance = Column(
        Numeric(precision=18, scale=2),
        nullable=False,
        default=0,
    )
    is_system = Column(Boolean, nullable=False, default=False)

class Transaction(Base):
    """A business event grouping related ledger entries.

    `idempotency_key` prevents duplicate processing when enforced by the
    transaction-processing service and database constraint. It is UNIQUE, and every write
    endpoint sets it, so a retried request is rejected with a 409. (The column itself is
    nullable, but the API always supplies a value.)
    `exchange_rate_used` records the live FX rate actually applied to a cross-currency
    transfer (null otherwise). It is the only place a rate is stored: there is no rates table.

    The ledger is append-only: transactions and entries are never edited or deleted.
    A mistake is corrected by posting a new REVERSAL transaction whose entries offset the
    original's. `type` says what kind of event this is. `reversal_of` points at the
    transaction a REVERSAL undoes. It is UNIQUE, so one transaction can be reversed at most
    once (a second reversal violates the constraint), and the CHECK below ties it to the type:
    only a REVERSAL has a `reversal_of`, and every REVERSAL has one.

    Other columns:
      description  a human-readable label ("Deposit", "Reversal of transaction 4"). Never used for
                   logic: code checks `type`.
      created_at   set by the database server clock when the row is inserted, stored without a
                   timezone. Month-end reconciliation uses it to place entries in a month.
      notes        optional free text. No endpoint sets or returns it yet.
    """

    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    idempotency_key = Column(String, unique=True, index=True)
    type = Column(Enum(TransactionType), nullable=False)
    reversal_of = Column(
        Integer,
        ForeignKey("transactions.id"),
        nullable=True,
        unique=True,
    )
    description = Column(String)
    created_at = Column(DateTime, server_default=func.now())
    notes = Column(String, nullable=True)
    exchange_rate_used = Column(Numeric(precision=18, scale=6), nullable=True)

    # The CHECK constraint below enforces that only a REVERSAL transaction has a reversal_of value, and every REVERSAL 
    # transaction has one. This prevents data inconsistencies where a non-REVERSAL transaction could have a 
    # reversal_of value or a REVERSAL transaction could lack one.
    __table_args__ = (
        CheckConstraint(
            "(type = 'REVERSAL' AND reversal_of IS NOT NULL) "
            "OR (type <> 'REVERSAL' AND reversal_of IS NULL)",
            name="ck_transaction_reversal_matches_type",
        ),
    )

    entries = relationship("LedgerEntry", back_populates="transaction")


class LedgerEntry(Base):
    """An individual debit or credit posting to an account.

    Entries belong to a transaction. Financial posting logic must ensure
    that each transaction satisfies the applicable balancing rules.

    The balancing rule is: within each currency, a transaction's DEBIT amounts equal its CREDIT
    amounts. main.py guarantees this when it posts; no database constraint checks it.
    `amount` is always positive (see the CHECK below) and is in the currency of `account`: there
    is no currency column on the entry. `entry_type` gives the direction. Entries are append-only.
    """

    __tablename__ = "ledger_entries"

    id = Column(Integer, primary_key=True, index=True)
    transaction_id = Column(
        Integer,
        ForeignKey("transactions.id"),
        nullable=False,
    )
    account_id = Column(
        Integer,
        ForeignKey("accounts.id"),
        nullable=False,
    )
    entry_type = Column(Enum(EntryType), nullable=False)
    amount = Column(Numeric(precision=18, scale=2), nullable=False)

    # The CHECK constraint below enforces that the amount is always positive. This prevents data inconsistencies where a ledger entry 
    # could have a non-positive amount, which would violate the expected behavior of financial postings.
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_ledger_entry_positive_amount"),
    )

    transaction = relationship("Transaction", back_populates="entries")
    account = relationship("Account")

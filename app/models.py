"""SQLAlchemy models for the financial ledger.

These models define the database schema. They do not implement transaction
processing, balance updates, FX pricing, or netting logic.
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
    """Direction of a ledger posting."""

    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class TransactionType(str, enum.Enum):
    """What kind of business event a transaction is. Code checks this, not the description."""

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
    transaction-processing service and database constraint.
    `exchange_rate_used` records the live FX rate actually applied to a cross-currency
    transfer (null otherwise). It is the only place a rate is stored: there is no rates table.

    The ledger is append-only: transactions and entries are never edited or deleted.
    A mistake is corrected by posting a new REVERSAL transaction whose entries offset the
    original's. `type` says what kind of event this is. `reversal_of` points at the
    transaction a REVERSAL undoes. It is UNIQUE, so one transaction can be reversed at most
    once (a second reversal violates the constraint), and the CHECK below ties it to the type:
    only a REVERSAL has a `reversal_of`, and every REVERSAL has one.
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

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_ledger_entry_positive_amount"),
    )

    transaction = relationship("Transaction", back_populates="entries")
    account = relationship("Account")

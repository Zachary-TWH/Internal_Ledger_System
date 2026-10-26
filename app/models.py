
"""SQLAlchemy models for the financial ledger and FX rate storage.

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
    `exchange_rate_used` records the rate applied, when relevant.
    """

    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    idempotency_key = Column(String, unique=True, index=True)
    description = Column(String)
    created_at = Column(DateTime, server_default=func.now())
    notes = Column(String, nullable=True)
    exchange_rate_used = Column(Numeric(precision=18, scale=6), nullable=True)

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


class ExchangeRate(Base):
    """A stored FX market-rate observation.

    Each row represents a rate for a currency pair at a particular time.
    Historical rows can be retained for auditing and rate comparisons.

    This table stores market-rate observations, not customer quotes or
    executed FX trades.
    """

    __tablename__ = "exchange_rates"

    id = Column(Integer, primary_key=True, index=True)
    base_currency = Column(String, nullable=False, index=True)
    quote_currency = Column(String, nullable=False, index=True)
    rate = Column(Numeric(precision=18, scale=6), nullable=False)
    fetched_at = Column(DateTime, server_default=func.now(), nullable=False)

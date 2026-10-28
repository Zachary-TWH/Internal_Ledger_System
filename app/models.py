"""SQLAlchemy models: three tables, schema only. The posting logic lives in main.py.

    accounts        one row per account (customer or system), with a stored balance
    transactions    one row per business event (deposit, withdrawal, transfer, reversal)
    ledger_entries  the DEBIT / CREDIT postings, the source of truth

Design and rules are in the README. Tables are created by create_table.py.
"""

import enum
from decimal import Decimal

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
    """Direction of a posting. The entry's amount is always positive, so the direction lives here."""

    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class TransactionType(str, enum.Enum):
    """Kind of business event. Code checks this, not the description."""

    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    TRANSFER = "TRANSFER"
    REVERSAL = "REVERSAL"


class Account(Base):
    """A customer or system account, in one currency.

    `balance` is a stored copy of the ledger (README assumption 5). A customer balance is never
    allowed below zero, but main.py enforces that, not a database constraint. System balances
    may go negative. `is_system` marks EXTERNAL and FX_POSITION, which the API cannot create.
    """

    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    owner_name = Column(String, nullable=False)
    currency = Column(String, nullable=False, default="USD")
    balance = Column(
        Numeric(precision=18, scale=2),
        nullable=False,
        default=Decimal("0"),
    )
    is_system = Column(Boolean, nullable=False, default=False)

class Transaction(Base):
    """A business event grouping related ledger entries. Append-only (README assumption 5).

    `idempotency_key` is UNIQUE. `reversal_of` is UNIQUE too, so a transaction can be reversed at
    most once, and the CHECK below ties it to `type`. `created_at` is the database server clock
    (README assumption 12). `notes` is optional free text that no endpoint sets or returns yet.
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

    # Only a REVERSAL has a reversal_of, and every REVERSAL has one.
    __table_args__ = (
        CheckConstraint(
            "(type = 'REVERSAL' AND reversal_of IS NOT NULL) "
            "OR (type <> 'REVERSAL' AND reversal_of IS NULL)",
            name="ck_transaction_reversal_matches_type",
        ),
    )

    entries = relationship("LedgerEntry", back_populates="transaction")


class LedgerEntry(Base):
    """One debit or credit posting. Append-only.

    Within each currency a transaction's debits equal its credits, which main.py guarantees
    (no database constraint). `amount` is in the currency of `account`.
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

    # Amounts are always positive; the direction is in entry_type.
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_ledger_entry_positive_amount"),
    )

    transaction = relationship("Transaction", back_populates="entries")
    account = relationship("Account")

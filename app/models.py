# defines the structure of the tables as Python classes and blueprints for SQLAlchemy to use when creating the tables in Postgres

from sqlalchemy import Column, Integer, String, Numeric, ForeignKey, DateTime, Enum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .database import Base

import enum

class EntryType(str, enum.Enum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    owner_name = Column(String, nullable=False)
    currency = Column(String, nullable=False, default="USD")
    balance = Column(Numeric(precision=18, scale=2), nullable=False, default=0)

class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    idempotency_key = Column(String, unique=True, index=True)
    description = Column(String)
    created_at = Column(DateTime, server_default=func.now())
    notes = Column(String, nullable=True)
    exchange_rate_used = Column(Numeric(precision=18, scale=6), nullable=True)

    entries = relationship("LedgerEntry", back_populates="transaction")

class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id = Column(Integer, primary_key=True, index=True)
    transaction_id = Column(Integer, ForeignKey("transactions.id"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    entry_type = Column(Enum(EntryType), nullable=False)
    amount = Column(Numeric(precision=18, scale=2), nullable=False)

    transaction = relationship("Transaction", back_populates="entries")
    account = relationship("Account")


class ExchangeRate(Base):
    __tablename__ = "exchange_rates"

    id = Column(Integer, primary_key=True, index=True)
    base_currency = Column(String, nullable=False, index=True)
    quote_currency = Column(String, nullable=False, index=True)
    rate = Column(Numeric(precision=18, scale=6), nullable=False)
    fetched_at = Column(DateTime, server_default=func.now(), nullable=False)
"""Database setup: the engine, the session factory and the declarative Base. No business logic.

The connection string comes from the DATABASE_URL environment variable (a local .env is loaded
if present; variables already set in the environment win, which is how docker-compose and the
tests point the app at another database), e.g.
    postgresql://postgres:<password>@localhost:5432/ledger
Postgres is the intended database (row locks, README behaviour 10).
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    # fail early with a clear message
    raise RuntimeError(
        "DATABASE_URL is not set. Define it in the environment or in a .env file, "
        "e.g. postgresql://postgres:<password>@localhost:5432/ledger"
    )

# A plain "postgresql://" URL makes SQLAlchemy look for psycopg2, but we install psycopg 3,
# so name the driver explicitly.
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

# pool_pre_ping replaces a connection dropped by a database restart instead of failing the request.
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# One session per request (get_db in main.py). autocommit=False lets a write commit once, all or
# nothing. autoflush=False sends changes only on an explicit flush() or commit().
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Parent class of the models in models.py.
Base = declarative_base()

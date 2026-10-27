"""Database setup: the engine, the session factory, and the declarative Base.

This module is imported by everything that touches the database (models.py,
main.py, create_table.py), so it only does configuration and holds no business logic.

Configuration
-------------
The connection string is read from the DATABASE_URL environment variable, e.g.
    postgresql://postgres:<password>@localhost:5432/ledger
A local .env file is loaded if present. Variables already set in the environment
take priority over .env (python-dotenv's default), which is how docker-compose and
the tests point the app at a different database.

Postgres is the intended database: transfers rely on row locks
(SELECT ... FOR UPDATE) to stay correct when requests run concurrently. SQLite is
accepted so tests can run without a server, but it ignores row locks, so
concurrency behaviour can only be verified against Postgres.
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    # Fail here with a clear message instead of a confusing error from create_engine.
    raise RuntimeError(
        "DATABASE_URL is not set. Define it in the environment or in a .env file, "
        "e.g. postgresql://postgres:<password>@localhost:5432/ledger"
    )

# The engine owns a pool of database connections and is created once per process.
# pool_pre_ping tests a pooled connection before using it, so a connection dropped
# by a database restart is replaced instead of failing the request.
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# Factory for sessions. A session is a short-lived unit of work: it borrows a
# connection from the pool and tracks the objects loaded or added through it.
#   autocommit=False: nothing is saved until commit() is called explicitly. This is
#                     what lets a transfer write both ledger entries and both
#                     balance updates, then commit once (all or nothing).
#   autoflush=False:  pending changes are only sent to the database on commit() or
#                     an explicit flush(), never silently before a query.
# Each request gets its own session via the get_db() dependency in main.py.
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Parent class for all table models in models.py. It records every model so that
# Base.metadata.create_all() knows which tables to create.
Base = declarative_base()

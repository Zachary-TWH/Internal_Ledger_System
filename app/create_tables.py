# Create the tables in the database (postgres) if they don't already exist

from .database import engine, Base
from . import models  # noqa: F401  (import so SQLAlchemy sees the table definitions)

Base.metadata.create_all(bind=engine)
print("Tables created.")
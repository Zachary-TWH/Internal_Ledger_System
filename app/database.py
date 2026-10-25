# It sets up the connection to Postgres

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv
import os

# Load the .env file so we can read DATABASE_URL
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

# The engine is the actual connection to Postgres
engine = create_engine(DATABASE_URL)

# SessionLocal creates a new "conversation" with the database each time we need one
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base is what our table classes will inherit from (in models.py, next step)
Base = declarative_base()
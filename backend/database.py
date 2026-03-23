"""
database.py
───────────
PostgreSQL connection via SQLAlchemy.
Provides:
  - engine        : raw SQLAlchemy engine
  - SessionLocal  : session factory used in every request
  - Base          : declarative base all models inherit from
  - get_db()      : FastAPI dependency that yields a DB session
                    and closes it cleanly after the request
"""

import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. "
        "Make sure your .env file exists and contains DATABASE_URL."
    )

# ── Engine ────────────────────────────────────────────────────────────────────
# pool_pre_ping=True  : test connection before using it (handles DB restarts)
# pool_size=10        : keep up to 10 connections open
# max_overflow=20     : allow up to 20 extra connections under heavy load
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

# ── Session factory ───────────────────────────────────────────────────────────
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

# ── Declarative base ──────────────────────────────────────────────────────────
# All SQLAlchemy models inherit from this
Base = declarative_base()


# ── FastAPI dependency ────────────────────────────────────────────────────────
def get_db():
    """
    Yields a database session for the duration of a single request.
    Always closes the session when the request is done — even on errors.

    Usage in a router:
        @router.get("/something")
        def my_route(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
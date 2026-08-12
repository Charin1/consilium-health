"""
Database engine/session configuration.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Base class for all ORM models."""


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./consilium.db")

connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(
    DATABASE_URL,
    future=True,
    pool_pre_ping=True,
    connect_args=connect_args,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    class_=Session,
)


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Yield a database session."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """Create all tables if they do not exist."""
    from app.db import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _backfill_chat_session_columns()


def _backfill_chat_session_columns() -> None:
    """Add newly required chat session columns for existing SQLite databases."""
    inspector = inspect(engine)
    if "chat_sessions" not in inspector.get_table_names():
        return

    existing_columns = {column["name"] for column in inspector.get_columns("chat_sessions")}
    statements = []

    if "turn_mode" not in existing_columns:
        statements.append(
            "ALTER TABLE chat_sessions ADD COLUMN turn_mode VARCHAR(16) NOT NULL DEFAULT 'automatic'"
        )
    if "stance_mode" not in existing_columns:
        statements.append(
            "ALTER TABLE chat_sessions ADD COLUMN stance_mode VARCHAR(16) NOT NULL DEFAULT 'neutral'"
        )
    if "manual_agent_id" not in existing_columns:
        statements.append("ALTER TABLE chat_sessions ADD COLUMN manual_agent_id VARCHAR(64)")
    if "action_items" not in existing_columns:
        statements.append("ALTER TABLE chat_sessions ADD COLUMN action_items JSON NOT NULL DEFAULT '[]'")
    if "persona_packs" not in existing_columns:
        # Sessions that predate packs were all seated from the core boardroom,
        # so backfilling to ["core"] reproduces the roster they actually ran
        # with. Defaulting them to the full org would silently change the cast
        # of every historical transcript.
        statements.append(
            "ALTER TABLE chat_sessions ADD COLUMN persona_packs JSON NOT NULL DEFAULT '[\"core\"]'"
        )

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))

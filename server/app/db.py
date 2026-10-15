from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


def make_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)


# 後から増やした列。create_all は既存のテーブルに列を足さないので、起動時に足す
ADDED_COLUMNS = {
    "slots": {"closed": "BOOLEAN NOT NULL DEFAULT FALSE", "merged": "BOOLEAN NOT NULL DEFAULT FALSE"},
    "hosts": {
        "pending": "BOOLEAN NOT NULL DEFAULT FALSE",
        "owner_id": "INTEGER REFERENCES users(id)",
        "machine_id": "VARCHAR(64) NOT NULL DEFAULT ''",
        "followers_notified_at": "DATETIME",
        "specs": "VARCHAR(500) NOT NULL DEFAULT ''",
    },
    "users": {"nickname": "VARCHAR(32) NOT NULL DEFAULT ''"},
}


def add_missing_columns(engine: Engine) -> None:
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for table, columns in ADDED_COLUMNS.items():
        if table not in tables:
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        for name, ddl in columns.items():
            if name not in existing:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))

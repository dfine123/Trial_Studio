"""Idempotent schema sync — run on boot (start.sh) and locally.

create_all() makes any NEW tables (e.g. clip_folders). For columns ADDED to EXISTING tables it
can't help, so we apply those with `ADD COLUMN IF NOT EXISTS` here. Safe to run repeatedly.
"""
from __future__ import annotations

from sqlalchemy import text

import app.models  # noqa: F401 — register all tables on Base.metadata
from app.db import Base, engine

# Columns added to existing tables, applied after create_all (Postgres ADD COLUMN IF NOT EXISTS).
_ALTERS = [
    "ALTER TABLE clips ADD COLUMN IF NOT EXISTS folder_id UUID REFERENCES clip_folders(id) ON DELETE SET NULL",
]


def migrate() -> None:
    Base.metadata.create_all(engine)  # new tables (clip_folders, ...)
    with engine.begin() as conn:
        for sql in _ALTERS:
            conn.execute(text(sql))


if __name__ == "__main__":
    migrate()
    print("migrate: ok")

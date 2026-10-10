"""Alembic runs as the table owner (DATABASE_SYSTEM_URL); the app role gets its grants here."""
from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, text

from central.config import get_settings

settings = get_settings()


def run() -> None:
    engine = create_engine(settings.database_system_url)
    with engine.connect() as conn:
        encoding = conn.execute(text("SHOW server_encoding")).scalar()
        if encoding != "UTF8":
            raise SystemExit(f"The database encoding is {encoding}; dwyt needs UTF8 (createdb -E UTF8).")
        context.configure(connection=conn, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations(app_role=settings.app_db_role)
        conn.commit()
    engine.dispose()


run()

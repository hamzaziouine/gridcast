"""Alembic environment: online migrations only; the URL is resolved at run time, never from ini."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

from gridcast.db import make_engine, migration_url

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

url = config.attributes.get("url") or migration_url()

engine = make_engine(url, poolclass=pool.NullPool)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=None, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()

"""Shared package identity and runtime constants."""

from __future__ import annotations

import os

PACKAGE_NAME = "obele"
CLI_PROGRAM = f"{PACKAGE_NAME}-orm"
DATABASE_ENV_VAR = "OBELE_DATABASE"
SAVEPOINT_PREFIX = f"{PACKAGE_NAME}_sp_"


def default_database_path() -> str:
    """Resolve the default database path from the environment."""
    return os.environ.get(DATABASE_ENV_VAR) or "DB.sqlite3"

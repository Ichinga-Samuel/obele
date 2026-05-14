"""Shared test fixtures and configuration for obele tests."""

import os
import pytest

from obele import Database

DB_PATH = os.path.join(os.path.dirname(__file__), "_test.sqlite3")


@pytest.fixture(autouse=True)
def setup_db():
    """Configure a fresh in-memory database for every test."""
    Database.configure(":memory:")
    yield
    Database.close_all()

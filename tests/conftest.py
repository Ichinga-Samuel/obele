"""Shared test fixtures and configuration for obele tests."""

import pytest

from obele import Database


@pytest.fixture(autouse=True)
def setup_db():
	"""Configure a fresh in-memory database for every test."""
	Database.configure("file::memory:?cache=shared")
	yield
	Database.close_all()

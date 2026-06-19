"""Database connection manager tests.

This module keeps low-level sync/async connection behavior separate from the
ORM and KV feature suites.
"""

from pathlib import Path

import pytest

import obele
from obele import Database, DatabaseError, async_connect
from obele.asqlite import Connection as AsyncSQLiteConnection, Cursor as AsyncSQLiteCursor


async def _close(cursor: AsyncSQLiteCursor) -> None:
    await cursor.close()


class TestAsyncDatabaseConnection:
    async def test_aget_connection_returns_async_sqlite_connection(self):
        connection = await Database.aget_connection()

        assert isinstance(connection.connection, AsyncSQLiteConnection)

    async def test_aexecute_returns_async_cursor_and_fetches_rows(self):
        cursor = await Database.aexecute(
            "CREATE TABLE async_database_direct (id INTEGER PRIMARY KEY, name TEXT)"
        )
        await _close(cursor)

        cursor = await Database.aexecute(
            "INSERT INTO async_database_direct (name) VALUES (?)",
            ["Ada"],
        )
        assert isinstance(cursor, AsyncSQLiteCursor)
        assert cursor.lastrowid == 1
        await _close(cursor)

        row = await Database.afetchone(
            "SELECT name FROM async_database_direct WHERE id = ?",
            [1],
        )
        assert row["name"] == "Ada"
        assert await Database.afetch_value(
            "SELECT COUNT(*) AS cnt FROM async_database_direct",
            column="cnt",
        ) == 1

    async def test_aexecute_script_runs_multiple_statements(self):
        await Database.aexecute_script(
            """
            CREATE TABLE script_items (id INTEGER PRIMARY KEY, name TEXT);
            INSERT INTO script_items (name) VALUES ('one');
            INSERT INTO script_items (name) VALUES ('two');
            """
        )

        names = [
            row["name"]
            for row in await Database.afetchall(
                "SELECT name FROM script_items ORDER BY id"
            )
        ]
        assert names == ["one", "two"]

    async def test_apragma_reads_and_writes_active_connection(self):
        await Database.apragma("cache_size", -4000)

        assert await Database.apragma("cache_size") == -4000

    async def test_async_transaction_commits(self):
        cursor = await Database.aexecute(
            "CREATE TABLE async_database_commit (id INTEGER PRIMARY KEY, name TEXT)"
        )
        await _close(cursor)

        async with Database.transaction():
            cursor = await Database.aexecute(
                "INSERT INTO async_database_commit (name) VALUES (?)",
                ["Commit"],
            )
            await _close(cursor)

        assert await Database.afetch_value(
            "SELECT COUNT(*) AS cnt FROM async_database_commit",
            column="cnt",
        ) == 1

    async def test_async_transaction_rolls_back(self):
        cursor = await Database.aexecute(
            "CREATE TABLE async_database_txn (id INTEGER PRIMARY KEY, name TEXT)"
        )
        await _close(cursor)

        with pytest.raises(RuntimeError):
            async with Database.transaction() as connection:
                assert isinstance(connection.connection, AsyncSQLiteConnection)
                cursor = await Database.aexecute(
                    "INSERT INTO async_database_txn (name) VALUES (?)",
                    ["Rollback"],
                )
                await _close(cursor)
                raise RuntimeError("force rollback")

        assert await Database.afetch_value(
            "SELECT COUNT(*) AS cnt FROM async_database_txn",
            column="cnt",
        ) == 0

    async def test_nested_async_transaction_rolls_back_to_savepoint(self):
        cursor = await Database.aexecute(
            "CREATE TABLE async_database_savepoint (id INTEGER PRIMARY KEY, name TEXT)"
        )
        await _close(cursor)

        async with Database.transaction():
            cursor = await Database.aexecute(
                "INSERT INTO async_database_savepoint (name) VALUES (?)",
                ["outer"],
            )
            await _close(cursor)
            with pytest.raises(RuntimeError):
                async with Database.transaction():
                    cursor = await Database.aexecute(
                        "INSERT INTO async_database_savepoint (name) VALUES (?)",
                        ["inner"],
                    )
                    await _close(cursor)
                    raise RuntimeError("rollback nested")

        rows = await Database.afetchall(
            "SELECT name FROM async_database_savepoint ORDER BY id"
        )
        assert [row["name"] for row in rows] == ["outer"]

    async def test_async_using_scopes_database_binding(self, tmp_path: Path):
        scoped_path = tmp_path / "scoped.sqlite3"

        async with Database.using(str(scoped_path)):
            cursor = await Database.aexecute(
                "CREATE TABLE scoped_only (id INTEGER PRIMARY KEY, name TEXT)"
            )
            await _close(cursor)
            cursor = await Database.aexecute(
                "INSERT INTO scoped_only (name) VALUES (?)",
                ["Scoped"],
            )
            await _close(cursor)
            assert await Database.afetch_value(
                "SELECT COUNT(*) AS cnt FROM scoped_only",
                column="cnt",
            ) == 1

        with pytest.raises(DatabaseError):
            await Database.afetchone("SELECT * FROM scoped_only")


class TestDatabasePublicAsyncExports:
    async def test_async_connect_export_opens_asqlite_connection(self, tmp_path: Path):
        db_path = tmp_path / "raw.sqlite3"

        async with async_connect(db_path) as connection:
            assert isinstance(connection, AsyncSQLiteConnection)

    def test_package_exports_asqlite_module(self):
        assert obele.asqlite.__name__ == "obele.asqlite"
        assert obele.AsyncSQLiteConnection is AsyncSQLiteConnection
        assert obele.AsyncSQLiteCursor is AsyncSQLiteCursor

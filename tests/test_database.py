"""Database connection manager tests.

Low-level sync/async connection behavior, kept separate from the ORM and
KV feature suites.
"""

import threading
from pathlib import Path

import pytest

from obele import Database, DatabaseError, ExecResult


class TestSyncDatabase:
	def test_execute_and_fetch(self):
		Database.execute("CREATE TABLE direct_items (id INTEGER PRIMARY KEY, name TEXT)")
		cursor = Database.execute("INSERT INTO direct_items (name) VALUES (?)", ["Ada"])
		assert cursor.lastrowid == 1

		row = Database.fetchone("SELECT name FROM direct_items WHERE id = ?", [1])
		assert row["name"] == "Ada"
		assert Database.fetch_value("SELECT COUNT(*) AS cnt FROM direct_items", column="cnt") == 1

	def test_executemany_is_atomic(self):
		Database.execute("CREATE TABLE many_items (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
		with pytest.raises(Exception):
			Database.executemany(
				"INSERT INTO many_items (name) VALUES (?)",
				[["one"], [None]],  # second row violates NOT NULL
			)
		assert Database.fetch_value("SELECT COUNT(*) AS cnt FROM many_items", column="cnt") == 0

	def test_execute_script(self):
		Database.execute_script(
			"""
            CREATE TABLE script_items (id INTEGER PRIMARY KEY, name TEXT);
            INSERT INTO script_items (name) VALUES ('one');
            INSERT INTO script_items (name) VALUES ('two');
            """
		)
		names = [r["name"] for r in Database.fetchall("SELECT name FROM script_items ORDER BY id")]
		assert names == ["one", "two"]

	def test_execute_script_rejected_inside_transaction(self):
		with Database.transaction():
			with pytest.raises(DatabaseError):
				Database.execute_script("CREATE TABLE nope (id INTEGER)")

	def test_pragma_read_write(self):
		Database.pragma("cache_size", -4000)
		assert Database.pragma("cache_size") == -4000

	def test_pragma_rejects_unsafe_values(self):
		with pytest.raises(ValueError):
			Database.pragma("cache_size", "1; DROP TABLE x")

	def test_tables_lists_user_tables(self):
		Database.execute("CREATE TABLE listed_a (id INTEGER PRIMARY KEY)")
		Database.execute("CREATE TABLE listed_b (id INTEGER PRIMARY KEY)")
		tables = Database.tables()
		assert "listed_a" in tables and "listed_b" in tables

	def test_status_reports_binding(self):
		status = Database.status()
		assert status["is_memory"] is True
		assert status["scoped"] is False

	def test_memory_database_is_shared_across_threads(self):
		Database.execute("CREATE TABLE shared_mem (id INTEGER PRIMARY KEY, v INTEGER)")
		Database.execute("INSERT INTO shared_mem (v) VALUES (42)")
		seen: list[int] = []

		def reader() -> None:
			seen.append(Database.fetch_value("SELECT v FROM shared_mem", column=0))

		threads = [threading.Thread(target=reader) for _ in range(4)]
		for t in threads:
			t.start()
		for t in threads:
			t.join()
		assert seen == [42, 42, 42, 42]

	def test_transaction_commit_and_rollback(self):
		Database.execute("CREATE TABLE txn_items (id INTEGER PRIMARY KEY, name TEXT)")
		with Database.transaction():
			Database.execute("INSERT INTO txn_items (name) VALUES (?)", ["kept"])
		with pytest.raises(RuntimeError):
			with Database.transaction():
				Database.execute("INSERT INTO txn_items (name) VALUES (?)", ["lost"])
				raise RuntimeError("force rollback")
		names = [r["name"] for r in Database.fetchall("SELECT name FROM txn_items")]
		assert names == ["kept"]

	def test_nested_transaction_rolls_back_to_savepoint(self):
		Database.execute("CREATE TABLE sp_items (id INTEGER PRIMARY KEY, name TEXT)")
		with Database.transaction():
			Database.execute("INSERT INTO sp_items (name) VALUES ('outer')")
			with pytest.raises(RuntimeError):
				with Database.transaction():
					Database.execute("INSERT INTO sp_items (name) VALUES ('inner')")
					raise RuntimeError("rollback nested")
		assert [r["name"] for r in Database.fetchall("SELECT name FROM sp_items")] == ["outer"]

	def test_transaction_mode_validation(self):
		with pytest.raises(ValueError):
			Database.transaction(mode="BOGUS")
		with Database.transaction(mode="deferred"):
			pass

	def test_reads_inside_transaction_see_uncommitted_rows(self):
		Database.execute("CREATE TABLE txn_read (id INTEGER PRIMARY KEY)")
		with Database.transaction():
			Database.execute("INSERT INTO txn_read DEFAULT VALUES")
			assert Database.fetch_value("SELECT COUNT(*) FROM txn_read") == 1

	def test_using_scopes_database_binding(self, tmp_path: Path):
		scoped_path = tmp_path / "scoped.sqlite3"
		with Database.using(str(scoped_path)):
			Database.execute("CREATE TABLE scoped_only (id INTEGER PRIMARY KEY)")
			Database.execute("INSERT INTO scoped_only DEFAULT VALUES")
			assert Database.fetch_value("SELECT COUNT(*) FROM scoped_only") == 1
		with pytest.raises(DatabaseError):
			Database.fetchone("SELECT * FROM scoped_only")

	def test_backup_creates_copy(self, tmp_path: Path):
		Database.execute("CREATE TABLE backed_up (id INTEGER PRIMARY KEY, v TEXT)")
		Database.execute("INSERT INTO backed_up (v) VALUES ('x')")
		target = tmp_path / "backup.sqlite3"
		Database.backup(str(target))
		import sqlite3

		conn = sqlite3.connect(target)
		try:
			assert conn.execute("SELECT v FROM backed_up").fetchone()[0] == "x"
		finally:
			conn.close()

	def test_integrity_check(self):
		assert Database.integrity_check() == "ok"


class TestAsyncDatabase:
	async def test_aexecute_returns_exec_result(self):
		await Database.aexecute("CREATE TABLE adirect (id INTEGER PRIMARY KEY, name TEXT)")
		result = await Database.aexecute("INSERT INTO adirect (name) VALUES (?)", ["Ada"])
		assert isinstance(result, ExecResult)
		assert result.lastrowid == 1
		assert result.rowcount == 1

		row = await Database.afetchone("SELECT name FROM adirect WHERE id = ?", [1])
		assert row["name"] == "Ada"
		assert await Database.afetch_value("SELECT COUNT(*) AS cnt FROM adirect", column="cnt") == 1

	async def test_aexecute_returning_rows(self):
		await Database.aexecute("CREATE TABLE areturning (id INTEGER PRIMARY KEY, name TEXT)")
		result = await Database.aexecute("INSERT INTO areturning (name) VALUES (?) RETURNING id, name", ["Zed"])
		assert len(result) == 1
		assert result.first["name"] == "Zed"

	async def test_aexecutemany(self):
		await Database.aexecute("CREATE TABLE amany (id INTEGER PRIMARY KEY, name TEXT)")
		await Database.aexecutemany("INSERT INTO amany (name) VALUES (?)", [["a"], ["b"]])
		assert await Database.afetch_value("SELECT COUNT(*) FROM amany") == 2

	async def test_aexecute_script(self):
		await Database.aexecute_script(
			"""
            CREATE TABLE ascript (id INTEGER PRIMARY KEY, name TEXT);
            INSERT INTO ascript (name) VALUES ('one');
            INSERT INTO ascript (name) VALUES ('two');
            """
		)
		rows = await Database.afetchall("SELECT name FROM ascript ORDER BY id")
		assert [r["name"] for r in rows] == ["one", "two"]

	async def test_apragma(self):
		await Database.apragma("cache_size", -4000)
		assert await Database.apragma("cache_size") == -4000

	async def test_async_transaction_commits(self):
		await Database.aexecute("CREATE TABLE acommit (id INTEGER PRIMARY KEY, name TEXT)")
		async with Database.transaction():
			await Database.aexecute("INSERT INTO acommit (name) VALUES (?)", ["Commit"])
		assert await Database.afetch_value("SELECT COUNT(*) FROM acommit") == 1

	async def test_async_transaction_rolls_back(self):
		await Database.aexecute("CREATE TABLE atxn (id INTEGER PRIMARY KEY, name TEXT)")
		with pytest.raises(RuntimeError):
			async with Database.transaction():
				await Database.aexecute("INSERT INTO atxn (name) VALUES (?)", ["Rollback"])
				raise RuntimeError("force rollback")
		assert await Database.afetch_value("SELECT COUNT(*) FROM atxn") == 0

	async def test_nested_async_transaction_rolls_back_to_savepoint(self):
		await Database.aexecute("CREATE TABLE asp (id INTEGER PRIMARY KEY, name TEXT)")
		async with Database.transaction():
			await Database.aexecute("INSERT INTO asp (name) VALUES ('outer')")
			with pytest.raises(RuntimeError):
				async with Database.transaction():
					await Database.aexecute("INSERT INTO asp (name) VALUES ('inner')")
					raise RuntimeError("rollback nested")
		rows = await Database.afetchall("SELECT name FROM asp ORDER BY id")
		assert [r["name"] for r in rows] == ["outer"]

	async def test_concurrent_async_writers_are_serialized(self):
		import asyncio

		await Database.aexecute("CREATE TABLE aconc (id INTEGER PRIMARY KEY, v INTEGER)")

		async def writer(v: int) -> None:
			await Database.aexecute("INSERT INTO aconc (v) VALUES (?)", [v])

		await asyncio.gather(*(writer(i) for i in range(20)))
		assert await Database.afetch_value("SELECT COUNT(*) FROM aconc") == 20

	async def test_sync_write_inside_async_transaction_joins_it(self):
		"""A sync call within an async transaction context routes to the txn."""
		await Database.aexecute("CREATE TABLE amixed (id INTEGER PRIMARY KEY)")
		with pytest.raises(RuntimeError):
			async with Database.transaction():
				Database.execute("INSERT INTO amixed DEFAULT VALUES")
				raise RuntimeError("rollback")
		assert await Database.afetch_value("SELECT COUNT(*) FROM amixed") == 0

	async def test_async_using_scopes_database_binding(self, tmp_path: Path):
		scoped_path = tmp_path / "scoped_async.sqlite3"
		async with Database.using(str(scoped_path)):
			await Database.aexecute("CREATE TABLE scoped_only (id INTEGER PRIMARY KEY, name TEXT)")
			await Database.aexecute("INSERT INTO scoped_only (name) VALUES (?)", ["Scoped"])
			assert await Database.afetch_value("SELECT COUNT(*) FROM scoped_only") == 1
		with pytest.raises(DatabaseError):
			await Database.afetchone("SELECT * FROM scoped_only")

	async def test_atables_and_avacuum(self, tmp_path: Path):
		async with Database.using(str(tmp_path / "file.sqlite3")):
			await Database.aexecute("CREATE TABLE tolist (id INTEGER PRIMARY KEY)")
			assert "tolist" in await Database.atables()
			await Database.avacuum()

	async def test_aintegrity_check(self):
		assert await Database.aintegrity_check() == "ok"

"""Thread-safe SQLite connection manager with sync and async APIs.

Architecture
------------
- Every thread gets its own connection (`threading.local`), giving true
  concurrent reads under WAL journal mode.
- Writes outside a transaction run in autocommit mode and are serialized by
  a process-wide write lock.
- `Database.transaction()` checks out a dedicated connection and pins it
  in a `ContextVar`; every statement issued inside the
  block - sync or async - is routed to that connection.  Nested transactions
  become savepoints.
- Async methods delegate their sync counterparts to a worker thread via
  `asyncio.to_thread`.  Context variables propagate into the thread,
  so scoped bindings and transactions behave identically in both worlds.
- A plain `":memory:"` path is upgraded to a named shared-cache URI held
  open by an anchor connection, so all threads see the same in-memory data.

Concurrency guarantees: writers within one domain (sync threads, or tasks on
one event loop) never contend at the SQLite level.  A sync writer racing an
open `async` transaction is arbitrated by SQLite's `busy_timeout`.
"""

from __future__ import annotations

import asyncio
import functools
import itertools
import logging
import re
import sqlite3
import threading
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Sequence

from .._identity import PACKAGE_NAME, SAVEPOINT_PREFIX, default_database_path
from .exceptions import DatabaseError, IntegrityError
from .sql import validate_identifier

logger = logging.getLogger(PACKAGE_NAME)

_mem_seq = itertools.count(1)
_binding_seq = itertools.count(1)

TransactionMode = str  # "DEFERRED" | "IMMEDIATE" | "EXCLUSIVE"


@functools.lru_cache(maxsize=256)
def _compile_regex(pattern: str) -> re.Pattern[str]:
	"""Return a compiled, LRU-cached `re.Pattern` for `pattern`."""
	return re.compile(pattern)


def _regexp(pattern: str, value: Any) -> bool | None:
	"""SQLite `REGEXP` operator (`X REGEXP Y` calls `regexp(Y, X)`)."""
	if value is None:
		return None
	return _compile_regex(pattern).search(str(value)) is not None


def _normalize_path(db_path: str) -> tuple[str, bool]:
	"""Return `(path, is_memory)`, upgrading bare ':memory:' to shared cache."""
	if db_path == ":memory:":
		return f"file:{PACKAGE_NAME}_mem_{next(_mem_seq)}?mode=memory&cache=shared", True
	return db_path, ":memory:" in db_path or "mode=memory" in db_path


async def athread(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
	"""Run a blocking read on a worker thread, propagating context vars."""
	return await asyncio.to_thread(fn, *args, **kwargs)


async def awrite(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
	"""Run a blocking write on a worker thread, queuing behind async transactions."""
	async with Database._async_gate():
		return await asyncio.to_thread(fn, *args, **kwargs)


@dataclass(frozen=True, slots=True)
class ExecResult:
	"""Materialized result of an async statement (no live cursor to manage)."""

	rows: list[sqlite3.Row]
	rowcount: int
	lastrowid: int | None

	def __iter__(self) -> Iterator[sqlite3.Row]:
		"""Iterate over the materialized rows."""
		return iter(self.rows)

	def __len__(self) -> int:
		"""Return the number of rows in the result."""
		return len(self.rows)

	@property
	def first(self) -> sqlite3.Row | None:
		"""The first row, or `None` when the result is empty."""
		return self.rows[0] if self.rows else None


@dataclass(slots=True)
class _Binding:
	"""One database target: the global default or a scoped override."""

	db_path: str
	pragmas: dict[str, Any]
	is_memory: bool
	key: int = field(default_factory=lambda: next(_binding_seq))
	anchor: sqlite3.Connection | None = None
	txn_pool: list[sqlite3.Connection] = field(default_factory=list)
	owned: list[sqlite3.Connection] = field(default_factory=list)

	def close_all(self) -> None:
		"""Close every connection held by this binding and clear its pools."""
		for conn in (*self.owned, *self.txn_pool, self.anchor):
			if conn is not None:
				try:
					conn.close()
				except sqlite3.Error:
					pass
		self.owned.clear()
		self.txn_pool.clear()
		self.anchor = None


@dataclass(slots=True)
class _TxnState:
	"""Per-context state of an open transaction."""

	conn: sqlite3.Connection
	savepoints: int = 0


class _NullGate:
	"""No-op async context manager used when write gating is unnecessary."""
	async def __aenter__(self) -> None:
		"""Enter the gate without acquiring anything."""
		return None

	async def __aexit__(self, *exc: Any) -> None:
		"""Exit the gate without releasing anything."""
		return None


_NULL_GATE = _NullGate()


class DatabaseScope:
	"""Context manager binding a temporary database for the current context."""

	__slots__ = ("binding", "token")

	def __init__(self, db_path: str, pragmas: dict[str, Any] | None) -> None:
		"""Build the scope's private `_Binding` from `db_path` and `pragmas`."""
		path, is_memory = _normalize_path(db_path)
		self.binding = _Binding(path, pragmas or {}, is_memory)
		self.token: Token[_Binding | None] | None = None

	def __enter__(self) -> type[Database]:
		"""Activate the scoped binding and return the `Database` class."""
		self.token = Database._scoped.set(self.binding)
		return Database

	def __exit__(self, *exc: Any) -> None:
		"""Restore the previous binding and close the scope's connections."""
		if self.token is not None:
			Database._scoped.reset(self.token)
			self.token = None
		self._close_binding()

	async def __aenter__(self) -> type[Database]:
		"""Async version of `__enter__`."""
		return self.__enter__()

	async def __aexit__(self, *exc: Any) -> None:
		"""Async version of `__exit__`; closes connections off-thread."""
		if self.token is not None:
			Database._scoped.reset(self.token)
			self.token = None
		await asyncio.to_thread(self._close_binding)

	def _close_binding(self) -> None:
		"""Close every connection owned by the scope's binding under the lock."""
		with Database._lock:
			self.binding.close_all()


class Transaction:
	"""Sync/async transaction context manager with savepoint nesting.

	Yields the underlying `sqlite3.Connection`; inside the block you
	normally keep using `Database.execute` / `Database.aexecute`, which
	route to it automatically.
	"""

	__slots__ = ("mode", "_savepoint", "_state", "_token", "_owns_lock", "_owns_gate")

	def __init__(self, mode: TransactionMode = "IMMEDIATE") -> None:
		"""Initialize the transaction with a BEGIN `mode`.

		Args:
			mode: One of `DEFERRED`, `IMMEDIATE`, or `EXCLUSIVE`.

		Raises:
			ValueError: If `mode` is not a recognized transaction mode.
		"""
		if (mode := mode.upper()) not in ("DEFERRED", "IMMEDIATE", "EXCLUSIVE"):
			raise ValueError(f"invalid transaction mode {mode!r}")
		self.mode = mode
		self._savepoint: str | None = None
		self._state: _TxnState | None = None
		self._token: Token[_TxnState | None] | None = None
		self._owns_lock = False
		self._owns_gate = False

	def _begin_nested(self, state: _TxnState) -> sqlite3.Connection:
		"""Open a savepoint inside the already-running transaction `state`."""
		state.savepoints += 1
		self._savepoint = f"{SAVEPOINT_PREFIX}{state.savepoints}"
		self._state = state
		with _wrap_errors():
			state.conn.execute(f"SAVEPOINT {self._savepoint}")
		return state.conn

	def _begin_root(self) -> sqlite3.Connection:
		"""Check out a dedicated connection and issue the outermost `BEGIN`."""
		conn = Database._checkout_txn_conn()
		try:
			with _wrap_errors():
				conn.execute(f"BEGIN {self.mode}")
		except BaseException:
			Database._release_txn_conn(conn)
			raise
		self._state = _TxnState(conn)
		return conn

	def _finish(self, exc_type: type | None) -> None:
		"""Commit or roll back the block, releasing the savepoint when nested.

		Rolls back when `exc_type` is set, otherwise commits.
		"""
		assert self._state is not None
		conn = self._state.conn
		with _wrap_errors():
			if self._savepoint is not None:
				if exc_type is None:
					conn.execute(f"RELEASE SAVEPOINT {self._savepoint}")
				else:
					conn.execute(f"ROLLBACK TO SAVEPOINT {self._savepoint}")
					conn.execute(f"RELEASE SAVEPOINT {self._savepoint}")
			elif exc_type is None:
				conn.execute("COMMIT")
			else:
				conn.execute("ROLLBACK")

	def _cleanup_root(self) -> None:
		"""Reset the transaction context var and return a root connection to the pool."""
		if self._token is not None:
			Database._txn.reset(self._token)
			self._token = None
		if self._state is not None and self._savepoint is None:
			Database._release_txn_conn(self._state.conn)

	def __enter__(self) -> sqlite3.Connection:
		"""Begin the transaction, nesting as a savepoint if one is already open."""
		state = Database._txn.get()
		if state is not None:
			return self._begin_nested(state)  # transaction already in progress, continue
		Database._write_lock.acquire()
		self._owns_lock = True
		try:
			conn = self._begin_root()
		except BaseException:
			Database._write_lock.release()
			self._owns_lock = False
			raise
		self._token = Database._txn.set(self._state)
		return conn

	def __exit__(self, exc_type: type | None, *exc: Any) -> None:
		"""Finish the transaction and release the write lock if this block owns it."""
		try:
			self._finish(exc_type)
		finally:
			self._cleanup_root()
			if self._owns_lock:
				Database._write_lock.release()
				self._owns_lock = False

	async def __aenter__(self) -> sqlite3.Connection:
		"""Async version of `__enter__`; gates on the async write lock."""
		state = Database._txn.get()
		if state is not None:
			return await asyncio.to_thread(self._begin_nested, state)
		gate = Database._get_async_lock()
		await gate.acquire()
		self._owns_gate = True
		try:
			conn = await asyncio.to_thread(self._begin_root)
		except BaseException:
			gate.release()
			self._owns_gate = False
			raise
		self._token = Database._txn.set(self._state)
		return conn

	async def __aexit__(self, exc_type: type | None, *exc: Any) -> None:
		"""Async version of `__exit__`; releases the async write gate."""
		try:
			await asyncio.to_thread(self._finish, exc_type)
		finally:
			self._cleanup_root()
			if self._owns_gate:
				Database._get_async_lock().release()
				self._owns_gate = False


class _wrap_errors:
	"""Map `sqlite3` exceptions onto the ORM exception hierarchy."""

	def __enter__(self) -> None:
		"""Enter the error-translating block."""
		return None

	def __exit__(self, exc_type: type | None, exc: BaseException | None, tb: Any) -> bool:
		"""Translate a raised `sqlite3` error into the matching ORM exception."""
		if exc_type is None:
			return False
		if issubclass(exc_type, sqlite3.IntegrityError):
			raise IntegrityError(str(exc)) from exc
		if issubclass(exc_type, sqlite3.Error):
			raise DatabaseError(str(exc)) from exc
		return False


class Database:
	"""Process-wide SQLite access point with sync and async APIs.

	Configuration:

	    Database.configure(
	        "app.db",
	        pragmas={"cache_size": -16000},
	        log_queries=True,          # log all SQL to the 'obele' logger
	        slow_query_threshold=0.5,  # warn on queries slower than 500ms
	    )

	Use `using` for a temporary scoped binding (tests, tenants) and
	`transaction` for atomic multi-statement work.
	"""

	_lock = threading.RLock()
	_write_lock = threading.Lock()
	_local = threading.local()
	_generation = 0
	_global_binding: _Binding | None = None
	_log_queries = False
	_slow_query_threshold = 0.0

	_scoped: ContextVar[_Binding | None] = ContextVar(f"{PACKAGE_NAME}_scoped", default=None)
	_txn: ContextVar[_TxnState | None] = ContextVar(f"{PACKAGE_NAME}_txn", default=None)

	_async_lock: asyncio.Lock | None = None
	_async_lock_loop: asyncio.AbstractEventLoop | None = None

	def __repr__(self) -> str:
		"""Return a debug string with the active path and whether it is scoped."""
		binding = self._scoped.get() or self._global_binding
		path = binding.db_path if binding else "<unconfigured>"
		scoped = self._scoped.get() is not None
		return f"<Database path={path!r} scoped={scoped}>"

	@classmethod
	def configure(
		cls,
		db_path: str | None = None,
		pragmas: dict[str, Any] | None = None,
		*,
		log_queries: bool = False,
		slow_query_threshold: float = 0.0,
	) -> None:
		"""Configure (or reconfigure) the global database.

		Closes every connection opened for the previous configuration.
		"""
		path, is_memory = _normalize_path(db_path if db_path is not None else default_database_path())
		with cls._lock:
			cls.close_all()
			cls._global_binding = _Binding(path, pragmas or {}, is_memory)
			cls._log_queries = log_queries
			cls._slow_query_threshold = slow_query_threshold

	@classmethod
	async def aconfigure(cls, *args: Any, **kwargs: Any) -> None:
		"""Async version of `configure`."""
		await asyncio.to_thread(cls.configure, *args, **kwargs)

	@classmethod
	def using(cls, db_path: str | None = None, pragmas: dict[str, Any] | None = None) -> DatabaseScope:
		"""Return a sync/async context manager binding a temporary database."""
		return DatabaseScope(db_path if db_path is not None else default_database_path(), pragmas)

	@classmethod
	def transaction(cls, mode: TransactionMode = "IMMEDIATE") -> Transaction:
		"""Return a sync/async transaction context manager."""
		return Transaction(mode)

	@classmethod
	def current_config(cls) -> tuple[str, dict[str, Any]]:
		"""Return the active `(db_path, pragmas)` pair."""
		binding = cls._binding()
		return binding.db_path, dict(binding.pragmas)

	@classmethod
	def _binding(cls) -> _Binding:
		"""Return the active binding: the scoped override, else the global default.

		Lazily creates the global binding from `default_database_path` on first use.
		"""
		binding = cls._scoped.get()
		if binding is not None:
			return binding
		if cls._global_binding is None:
			with cls._lock:
				if cls._global_binding is None:
					path, is_memory = _normalize_path(default_database_path())
					cls._global_binding = _Binding(path, {}, is_memory)
		return cls._global_binding

	@classmethod
	def _new_connection(cls, binding: _Binding) -> sqlite3.Connection:
		"""Open and configure a fresh connection for `binding`.

		Applies the standard PRAGMAs, registers the `REGEXP` function, records
		the connection as owned, and pins a shared in-memory anchor when needed.

		Raises:
			DatabaseError: If the underlying `sqlite3.connect` fails.
		"""
		try:
			conn = sqlite3.connect(binding.db_path, check_same_thread=False, isolation_level=None, uri=binding.db_path.startswith("file:"))
		except sqlite3.Error as exc:
			raise DatabaseError(f"Could not connect to {binding.db_path}") from exc

		conn.row_factory = sqlite3.Row
		if not binding.is_memory:
			conn.execute("PRAGMA journal_mode=WAL")
			conn.execute("PRAGMA mmap_size=268435456")
			conn.execute("PRAGMA synchronous=NORMAL")
		conn.execute("PRAGMA foreign_keys=ON")
		conn.execute("PRAGMA busy_timeout=5000")
		conn.execute("PRAGMA cache_size=-8000")
		for pragma, value in binding.pragmas.items():
			validate_identifier(pragma, kind="pragma name")
			conn.execute(f"PRAGMA {pragma}={value}")
		conn.create_function("REGEXP", 2, _regexp, deterministic=True)
		with cls._lock:
			if binding.is_memory and binding.anchor is None:
				# Keep the shared in-memory database alive independently of per-thread connection lifetimes.
				binding.anchor = sqlite3.connect(binding.db_path, check_same_thread=False, uri=True)
			binding.owned.append(conn)
		return conn

	@classmethod
	def _connection(cls) -> sqlite3.Connection:
		"""Return the connection for the current context (txn > thread-local)."""
		txn = cls._txn.get()
		if txn is not None:
			return txn.conn
		binding = cls._binding()
		conns: dict[int, tuple[int, sqlite3.Connection]] = getattr(cls._local, "conns", None) or {}
		cls._local.conns = conns
		entry = conns.get(binding.key)
		if entry is not None and entry[0] == cls._generation:
			return entry[1]
		conn = cls._new_connection(binding)
		conns[binding.key] = (cls._generation, conn)
		return conn

	@classmethod
	def _checkout_txn_conn(cls) -> sqlite3.Connection:
		"""Take a connection from the binding's transaction pool, or open a new one."""
		binding = cls._binding()
		with cls._lock:
			if binding.txn_pool:
				return binding.txn_pool.pop()
		return cls._new_connection(binding)

	@classmethod
	def _release_txn_conn(cls, conn: sqlite3.Connection) -> None:
		"""Return `conn` to the transaction pool, closing it once the pool is full."""
		binding = cls._binding()
		with cls._lock:
			if len(binding.txn_pool) < 2:
				binding.txn_pool.append(conn)
				return
			if conn in binding.owned:
				binding.owned.remove(conn)
		try:
			conn.close()
		except sqlite3.Error:
			pass

	@classmethod
	def _get_async_lock(cls) -> asyncio.Lock:
		"""Return the async write lock, recreating it if the event loop changed."""
		loop = asyncio.get_running_loop()
		if cls._async_lock is None or cls._async_lock_loop is not loop:
			cls._async_lock = asyncio.Lock()
			cls._async_lock_loop = loop
		return cls._async_lock

	@classmethod
	def _async_gate(cls) -> Any:
		"""Async CM serializing writes behind any open async transaction."""
		if cls._txn.get() is not None:
			return _NULL_GATE
		return cls._get_async_lock()

	@classmethod
	def close(cls) -> None:
		"""Close every connection owned by the current thread."""
		conns: dict[int, tuple[int, sqlite3.Connection]] = getattr(cls._local, "conns", None) or {}
		for key, (_, conn) in list(conns.items()):
			try:
				conn.close()
			except sqlite3.Error:
				pass
			conns.pop(key, None)
			with cls._lock:
				for binding in (cls._global_binding, cls._scoped.get()):
					if binding is not None and conn in binding.owned:
						binding.owned.remove(conn)

	@classmethod
	def close_all(cls) -> None:
		"""Close every connection opened for the global configuration."""
		with cls._lock:
			cls._generation += 1
			if cls._global_binding is not None:
				cls._global_binding.close_all()
		cls._local.conns = {}

	@classmethod
	async def aclose_all(cls) -> None:
		"""Async version of `close_all`."""
		await asyncio.to_thread(cls.close_all)

	@classmethod
	def status(cls) -> dict[str, Any]:
		"""Return a snapshot of connection bookkeeping for diagnostics."""
		binding = cls._binding()
		with cls._lock:
			return {
				"db_path": binding.db_path,
				"is_memory": binding.is_memory,
				"open_connections": len(binding.owned),
				"pooled_txn_connections": len(binding.txn_pool),
				"scoped": cls._scoped.get() is not None,
			}

	@classmethod
	def _log_query(cls, sql: str, params: Any, started: float) -> None:
		"""Log the query when enabled and warn if it exceeds the slow-query threshold."""
		elapsed = time.monotonic() - started
		if cls._log_queries:
			logger.debug("SQL [%.4fs]: %s | params=%r", elapsed, sql.strip(), params)
		if 0 < cls._slow_query_threshold < elapsed:
			logger.warning("SLOW QUERY [%.4fs > %.4fs]: %s | params=%r", elapsed, cls._slow_query_threshold, sql.strip(), params)

	@classmethod
	def execute(cls, sql: str, params: Sequence[Any] | None = None) -> sqlite3.Cursor:
		"""Execute a write statement and return its cursor."""
		txn = cls._txn.get()
		started = time.monotonic()
		if txn is not None:
			with _wrap_errors():
				cursor = txn.conn.execute(sql, params or ())
		else:
			with cls._write_lock, _wrap_errors():
				cursor = cls._connection().execute(sql, params or ())
		cls._log_query(sql, params, started)
		return cursor

	@classmethod
	def executemany(cls, sql: str, params_seq: Sequence[Sequence[Any]]) -> sqlite3.Cursor:
		"""Execute a statement against many parameter sets atomically."""
		txn = cls._txn.get()
		started = time.monotonic()
		if txn is not None:
			with _wrap_errors():
				cursor = txn.conn.executemany(sql, params_seq)
		else:
			with cls._write_lock:
				conn = cls._connection()
				with _wrap_errors():
					conn.execute("BEGIN IMMEDIATE")
					try:
						cursor = conn.executemany(sql, params_seq)
						conn.execute("COMMIT")
					except sqlite3.Error:
						try:
							conn.execute("ROLLBACK")
						except sqlite3.Error:
							pass
						raise
		cls._log_query(sql, params_seq, started)
		return cursor

	@classmethod
	def execute_script(cls, sql_script: str) -> None:
		"""Execute multiple `;`-separated statements (not usable inside a transaction)."""
		if cls._txn.get() is not None:
			raise DatabaseError("execute_script() cannot run inside a transaction")
		with cls._write_lock, _wrap_errors():
			cls._connection().executescript(sql_script)

	@classmethod
	def execute_read(cls, sql: str, params: Sequence[Any] | None = None) -> sqlite3.Cursor:
		"""Execute a read-only query and return its cursor."""
		started = time.monotonic()
		with _wrap_errors():
			cursor = cls._connection().execute(sql, params or ())
		cls._log_query(sql, params, started)
		return cursor

	@classmethod
	def fetchone(cls, sql: str, params: Sequence[Any] | None = None) -> sqlite3.Row | None:
		"""Execute a read-only query and return the first row."""
		return cls.execute_read(sql, params).fetchone()

	@classmethod
	def fetchall(cls, sql: str, params: Sequence[Any] | None = None) -> list[sqlite3.Row]:
		"""Execute a read-only query and return all rows."""
		return cls.execute_read(sql, params).fetchall()

	@classmethod
	def fetch_value(cls, sql: str, params: Sequence[Any] | None = None, *, column: int | str = 0) -> Any:
		"""Execute a read-only query and return a single column of the first row."""
		row = cls.fetchone(sql, params)
		return row[column] if row is not None else None

	@classmethod
	def _execute_result(cls, sql: str, params: Sequence[Any] | None = None) -> ExecResult:
		"""Run `execute` and materialize the cursor into an `ExecResult`."""
		cursor = cls.execute(sql, params)
		return ExecResult(cursor.fetchall(), cursor.rowcount, cursor.lastrowid)

	@classmethod
	def _executemany_result(cls, sql: str, params_seq: Sequence[Sequence[Any]]) -> ExecResult:
		"""Run `executemany` and return an `ExecResult` carrying no rows."""
		cursor = cls.executemany(sql, params_seq)
		return ExecResult([], cursor.rowcount, cursor.lastrowid)

	@classmethod
	async def aexecute(cls, sql: str, params: Sequence[Any] | None = None) -> ExecResult:
		"""Async `execute`; returns a fully materialized `ExecResult`."""
		return await awrite(cls._execute_result, sql, params)

	@classmethod
	async def aexecutemany(cls, sql: str, params_seq: Sequence[Sequence[Any]]) -> ExecResult:
		"""Async version of `executemany`."""
		return await awrite(cls._executemany_result, sql, params_seq)

	@classmethod
	async def aexecute_script(cls, sql_script: str) -> None:
		"""Async version of `execute_script`."""
		await awrite(cls.execute_script, sql_script)

	@classmethod
	async def afetchone(cls, sql: str, params: Sequence[Any] | None = None) -> sqlite3.Row | None:
		"""Async version of `fetchone`."""
		return await athread(cls.fetchone, sql, params)

	@classmethod
	async def afetchall(cls, sql: str, params: Sequence[Any] | None = None) -> list[sqlite3.Row]:
		"""Async version of `fetchall`."""
		return await athread(cls.fetchall, sql, params)

	@classmethod
	async def afetch_value(cls, sql: str, params: Sequence[Any] | None = None, *, column: int | str = 0) -> Any:
		"""Async version of `fetch_value`."""
		return await athread(functools.partial(cls.fetch_value, sql, params, column=column))

	@classmethod
	def pragma(cls, name: str, value: Any = None) -> Any:
		"""Read (`value=None`) or set a SQLite PRAGMA on the active connection."""
		validate_identifier(name, kind="pragma name")
		if value is None:
			row = cls.execute_read(f"PRAGMA {name}").fetchone()
			return row[0] if row is not None else None
		if not isinstance(value, (int, float)) and not re.fullmatch(r"[A-Za-z0-9_\-.]+", str(value)):
			raise ValueError(f"unsafe pragma value {value!r}")
		cls.execute_read(f"PRAGMA {name}={value}")
		return value

	@classmethod
	async def apragma(cls, name: str, value: Any = None) -> Any:
		"""Async version of `pragma`."""
		return await athread(cls.pragma, name, value)

	@classmethod
	def optimize(cls) -> None:
		"""Run SQLite's best-effort query-planner optimizer."""
		cls.execute_read("PRAGMA optimize")

	@classmethod
	async def aoptimize(cls) -> None:
		"""Async version of `optimize`."""
		await athread(cls.optimize)

	@classmethod
	def vacuum(cls) -> None:
		"""Rebuild the database file, reclaiming free space."""
		if cls._txn.get() is not None:
			raise DatabaseError("vacuum() cannot run inside a transaction")
		with cls._write_lock, _wrap_errors():
			cls._connection().execute("VACUUM")

	@classmethod
	async def avacuum(cls) -> None:
		"""Async version of `vacuum`."""
		await awrite(cls.vacuum)

	@classmethod
	def integrity_check(cls) -> str:
		"""Run `PRAGMA integrity_check` and return SQLite's verdict."""
		return str(cls.fetch_value("PRAGMA integrity_check") or "")

	@classmethod
	async def aintegrity_check(cls) -> str:
		"""Async version of `integrity_check`."""
		return await athread(cls.integrity_check)

	@classmethod
	def tables(cls) -> list[str]:
		"""Return the names of all user tables in the active database."""
		rows = cls.fetchall("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
		return [row["name"] for row in rows]

	@classmethod
	async def atables(cls) -> list[str]:
		"""Async version of `tables`."""
		return await athread(cls.tables)

	@classmethod
	def backup(cls, target_path: str, *, pages: int = -1, progress: Any | None = None) -> None:
		"""Create a consistent online backup of the active database."""
		target = sqlite3.connect(target_path)
		try:
			with _wrap_errors():
				cls._connection().backup(target, pages=pages, progress=progress)
		finally:
			target.close()

	@classmethod
	async def abackup(cls, target_path: str, *, pages: int = -1, progress: Any | None = None) -> None:
		"""Async version of `backup`."""
		await athread(functools.partial(cls.backup, target_path, pages=pages, progress=progress))

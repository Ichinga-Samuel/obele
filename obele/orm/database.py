"""Thread-safe SQLite connection manager with sync, async, and scoped APIs.

Uses per-thread connections via ``threading.local()`` for genuine read
concurrency under WAL journal mode, while write operations are serialized
through a single global lock.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Sequence

from .._identity import SCOPED_BINDING_CONTEXT, SAVEPOINT_PREFIX, default_database_path
from .exceptions import DatabaseError, IntegrityError, ConfigurationError
from .sql import validate_identifier


@dataclass
class _ScopedBinding:
    db_path: str
    pragmas: dict[str, Any]
    connection: sqlite3.Connection | None = None


class _DatabaseScope:
    """Context manager for temporary scoped database bindings."""

    __slots__ = ("_database_cls", "_binding", "_token")

    def __init__(self, database_cls: type[Database], db_path: str, pragmas: dict[str, Any] | None = None) -> None:
        self._database_cls = database_cls
        self._binding = _ScopedBinding(db_path, pragmas or {})
        self._token: Token[_ScopedBinding | None] | None = None

    def __enter__(self) -> type[Database]:
        self._token = self._database_cls._scoped_binding.set(self._binding)
        return self._database_cls

    def __exit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: Any) -> None:
        self._database_cls._close_connection(self._binding.connection)
        self._binding.connection = None
        if self._token is not None:
            self._database_cls._scoped_binding.reset(self._token)

    async def __aenter__(self) -> type[Database]:
        return self.__enter__()

    async def __aexit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: Any) -> None:
        self.__exit__(exc_type, exc_val, exc_tb)


class _DatabaseTransaction:
    """Context manager for explicit transactions with savepoint nesting."""

    __slots__ = ("_database_cls", "_connection", "_savepoint_name", "_token", "_owns_lock")

    def __init__(self, database_cls: type[Database]) -> None:
        self._database_cls = database_cls
        self._connection: sqlite3.Connection | None = None
        self._savepoint_name: str | None = None
        self._token: Token[sqlite3.Connection | None] | None = None
        self._owns_lock = False

    def __enter__(self) -> sqlite3.Connection:
        db = self._database_cls
        existing = db._transaction_connection.get()
        if existing is None:
            db._write_lock.acquire()
            self._owns_lock = True
        try:
            self._connection = existing or db.get_connection()
            if existing is None:
                self._token = db._transaction_connection.set(self._connection)
            if self._connection.in_transaction:
                db._savepoint_counter += 1
                self._savepoint_name = f"{SAVEPOINT_PREFIX}{db._savepoint_counter}"
                self._connection.execute(f"SAVEPOINT {self._savepoint_name}")
            else:
                self._connection.execute("BEGIN IMMEDIATE")
            return self._connection
        except sqlite3.Error as exc:
            if self._token is not None:
                db._transaction_connection.reset(self._token)
                self._token = None
            if self._owns_lock:
                db._write_lock.release()
                self._owns_lock = False
            raise DatabaseError(str(exc)) from exc

    def __exit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: Any) -> None:
        assert self._connection is not None
        try:
            sp = self._savepoint_name
            if exc_type is None:
                if sp is not None:
                    self._connection.execute(f"RELEASE SAVEPOINT {sp}")
                else:
                    self._connection.commit()
            else:
                if sp is not None:
                    self._connection.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                    self._connection.execute(f"RELEASE SAVEPOINT {sp}")
                else:
                    self._connection.rollback()
        except sqlite3.IntegrityError as exc:
            raise IntegrityError(str(exc)) from exc
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc
        finally:
            if self._token is not None:
                self._database_cls._transaction_connection.reset(self._token)
                self._token = None
            if self._owns_lock:
                self._database_cls._write_lock.release()
                self._owns_lock = False

    async def __aenter__(self) -> sqlite3.Connection:
        return self.__enter__()

    async def __aexit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: Any) -> None:
        self.__exit__(exc_type, exc_val, exc_tb)


logger = logging.getLogger("obele")


class Database:
    """Thread-safe SQLite connection manager with sync and async APIs.

    Uses per-thread connections for concurrent reads and a global write lock
    for serialized writes.  The configured database remains available globally,
    but callers can open a temporary scoped binding via :meth:`using`.

    Configuration options::

        Database.configure(
            "app.db",
            pragmas={"cache_size": -16000},
            pool_size=10,            # max connections in the pool
            log_queries=True,        # log all SQL to the 'obele' logger
            slow_query_threshold=0.5, # log queries slower than 500ms as warnings
        )
    """

    _lock: threading.RLock = threading.RLock()
    _write_lock: threading.RLock = threading.RLock()
    _local: threading.local = threading.local()
    _db_path: str = ""
    _pragmas: dict[str, Any] = {}
    _savepoint_counter: int = 0
    _connections: dict[int, sqlite3.Connection] = {}
    _connection_locks: dict[int, threading.RLock] = {}
    _connection_created_at: dict[int, float] = {}
    _pool_size: int = 0  # 0 = unlimited
    _max_connection_age: float = 0.0  # seconds, 0 = no recycling
    _log_queries: bool = False
    _slow_query_threshold: float = 0.0  # seconds, 0 = disabled
    _scoped_binding: ContextVar[_ScopedBinding | None] = ContextVar(
        SCOPED_BINDING_CONTEXT, default=None,
    )
    _transaction_connection: ContextVar[sqlite3.Connection | None] = ContextVar(
        f"{SCOPED_BINDING_CONTEXT}_transaction_connection", default=None,
    )

    # ---- Context managers -------------------------------------------------

    def __enter__(self) -> Database:
        self.get_connection()
        return self

    def __exit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: Any) -> None:
        self.close()

    async def __aenter__(self) -> Database:
        self.get_connection()
        return self

    async def __aexit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: Any) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        binding = self._scoped_binding.get()
        if binding is not None:
            return (
                f"<Database path={binding.db_path!r} "
                f"connected={binding.connection is not None} scoped=True>"
            )
        return f"<Database path={self._db_path!r}>"

    # ---- Configuration ----------------------------------------------------

    @classmethod
    def configure(
        cls,
        db_path: str | None = None,
        pragmas: dict[str, Any] | None = None,
        *,
        pool_size: int = 0,
        max_connection_age: float = 0.0,
        log_queries: bool = False,
        slow_query_threshold: float = 0.0,
    ) -> None:
        """Configure or reconfigure the global database connection.

        Args:
            db_path: Path to the SQLite database file.
            pragmas: Extra PRAGMA settings to apply on each connection.
            pool_size: Max concurrent connections (0 = unlimited).
            max_connection_age: Recycle connections older than this (seconds, 0 = never).
            log_queries: Log every SQL statement to the ``obele`` logger.
            slow_query_threshold: Warn on queries slower than this (seconds, 0 = off).
        """
        resolved = db_path if db_path is not None else default_database_path()
        with cls._lock:
            cls.close_all()
            cls._db_path = resolved
            cls._pragmas = pragmas or {}
            cls._pool_size = pool_size
            cls._max_connection_age = max_connection_age
            cls._log_queries = log_queries
            cls._slow_query_threshold = slow_query_threshold

    @classmethod
    async def aconfigure(
        cls,
        db_path: str | None = None,
        pragmas: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Async version of :meth:`configure`."""
        await asyncio.to_thread(cls.configure, db_path, pragmas, **kwargs)

    @classmethod
    def using(cls, db_path: str | None = None, pragmas: dict[str, Any] | None = None) -> _DatabaseScope:
        """Return a sync/async context manager for a temporary scoped binding."""
        resolved = db_path if db_path is not None else default_database_path()
        return _DatabaseScope(cls, resolved, pragmas)

    @classmethod
    def transaction(cls) -> _DatabaseTransaction:
        """Return a sync/async transaction context manager."""
        return _DatabaseTransaction(cls)

    @classmethod
    def current_config(cls) -> tuple[str, dict[str, Any]]:
        """Return the active database path and pragma configuration."""
        binding = cls._scoped_binding.get()
        if binding is not None:
            return binding.db_path, dict(binding.pragmas)
        return cls._db_path, dict(cls._pragmas)

    # ---- Connection management --------------------------------------------

    @classmethod
    def _create_connection(cls, db_path: str, pragmas: dict[str, Any]) -> sqlite3.Connection:
        # Enforce pool size limit
        if cls._pool_size > 0:
            with cls._lock:
                if len(cls._connections) >= cls._pool_size:
                    raise DatabaseError(
                        f"Connection pool exhausted (max {cls._pool_size}). "
                        f"Close unused connections or increase pool_size."
                    )

        try:
            conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        except sqlite3.Error as exc:
            raise DatabaseError(f"Could not connect to {db_path}") from exc

        conn.row_factory = sqlite3.Row
        # Performance pragmas
        if db_path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA cache_size=-8000")  # 8MB
        conn.execute("PRAGMA mmap_size=268435456")  # 256MB
        conn.execute("PRAGMA synchronous=NORMAL")
        for pragma, value in pragmas.items():
            conn.execute(f"PRAGMA {pragma}={value}")
        with cls._lock:
            cls._connections[id(conn)] = conn
            cls._connection_locks[id(conn)] = threading.RLock()
            cls._connection_created_at[id(conn)] = time.monotonic()
        return conn

    @classmethod
    def _close_connection(cls, conn: sqlite3.Connection | None) -> None:
        if conn is None:
            return
        try:
            conn.close()
        except sqlite3.Error:
            pass
        finally:
            with cls._lock:
                cls._connections.pop(id(conn), None)
                cls._connection_locks.pop(id(conn), None)
                cls._connection_created_at.pop(id(conn), None)

    @classmethod
    def _operation_lock(cls, conn: sqlite3.Connection) -> threading.RLock:
        with cls._lock:
            lock = cls._connection_locks.get(id(conn))
            if lock is None:
                lock = threading.RLock()
                cls._connection_locks[id(conn)] = lock
            return lock

    @classmethod
    def _close_all_thread_local(cls) -> None:
        """Close the current thread's connection (best-effort)."""
        conn = getattr(cls._local, "connection", None)
        cls._close_connection(conn)
        cls._local.connection = None

    @classmethod
    def close_all(cls) -> None:
        """Close every tracked connection created by this process."""
        with cls._lock:
            connections = list(cls._connections.values())
        for conn in connections:
            cls._close_connection(conn)
        cls._local.connection = None

    @classmethod
    async def aclose_all(cls) -> None:
        """Async version of :meth:`close_all`."""
        await asyncio.to_thread(cls.close_all)

    @classmethod
    def _should_recycle(cls, conn: sqlite3.Connection) -> bool:
        """Return True if the connection has exceeded max_connection_age."""
        if cls._max_connection_age <= 0:
            return False
        created_at = cls._connection_created_at.get(id(conn))
        if created_at is None:
            return False
        return (time.monotonic() - created_at) > cls._max_connection_age

    @classmethod
    def get_connection(cls) -> sqlite3.Connection:
        """Return the active connection, creating one per-thread if necessary."""
        transaction_connection = cls._transaction_connection.get()
        if transaction_connection is not None:
            return transaction_connection

        binding = cls._scoped_binding.get()
        if binding is not None:
            if binding.connection is not None and cls._should_recycle(binding.connection):
                cls._close_connection(binding.connection)
                binding.connection = None
            if binding.connection is None:
                binding.connection = cls._create_connection(binding.db_path, binding.pragmas)
            return binding.connection

        conn = getattr(cls._local, "connection", None)
        if conn is not None and cls._should_recycle(conn):
            cls._close_connection(conn)
            conn = None
            cls._local.connection = None
        if conn is None:
            if not cls._db_path:
                cls._db_path = default_database_path()
            conn = cls._create_connection(cls._db_path, cls._pragmas)
            cls._local.connection = conn
        return conn

    @classmethod
    def close(cls) -> None:
        """Close the active connection for the current thread."""
        binding = cls._scoped_binding.get()
        if binding is not None:
            cls._close_connection(binding.connection)
            binding.connection = None
            return
        conn = getattr(cls._local, "connection", None)
        cls._close_connection(conn)
        cls._local.connection = None

    @classmethod
    async def aclose(cls) -> None:
        """Async version of :meth:`close`."""
        await asyncio.to_thread(cls.close)

    # ---- Query logging ----------------------------------------------------

    @classmethod
    def _log_query(cls, sql: str, params: Sequence[Any] | None, elapsed: float) -> None:
        """Log a query if logging is enabled."""
        if cls._log_queries:
            logger.debug("SQL [%.4fs]: %s | params=%r", elapsed, sql.strip(), params)
        if cls._slow_query_threshold > 0 and elapsed > cls._slow_query_threshold:
            logger.warning(
                "SLOW QUERY [%.4fs > %.4fs]: %s | params=%r",
                elapsed, cls._slow_query_threshold, sql.strip(), params,
            )

    # ---- Write operations (serialized) ------------------------------------

    @classmethod
    def _execute_write(cls, sql: str, params: Sequence[Any] | None, *, many: bool = False) -> sqlite3.Cursor:
        """Shared write path for execute and executemany."""
        conn = cls.get_connection()
        in_managed_transaction = cls._transaction_connection.get() is conn
        lock = cls._operation_lock(conn) if in_managed_transaction else cls._write_lock
        t0 = time.monotonic()
        with lock:
            own_transaction = not in_managed_transaction and not conn.in_transaction
            try:
                if own_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                if many:
                    cursor = conn.executemany(sql, params or ())
                else:
                    cursor = conn.execute(sql, params or ())
                if own_transaction:
                    conn.commit()
                cls._log_query(sql, params, time.monotonic() - t0)
                return cursor
            except sqlite3.IntegrityError as exc:
                if own_transaction:
                    conn.rollback()
                raise IntegrityError(str(exc)) from exc
            except sqlite3.Error as exc:
                if own_transaction:
                    conn.rollback()
                raise DatabaseError(str(exc)) from exc

    @classmethod
    def execute(cls, sql: str, params: Sequence[Any] | None = None) -> sqlite3.Cursor:
        """Execute a single write SQL statement."""
        return cls._execute_write(sql, params)

    @classmethod
    def executemany(cls, sql: str, params_seq: Sequence[Sequence[Any]]) -> sqlite3.Cursor:
        """Execute an SQL statement against many parameter sets."""
        return cls._execute_write(sql, params_seq, many=True)

    @classmethod
    def execute_script(cls, sql_script: str) -> None:
        """Execute multiple SQL statements separated by semicolons."""
        conn = cls.get_connection()
        in_managed_transaction = cls._transaction_connection.get() is conn
        lock = cls._operation_lock(conn) if in_managed_transaction else cls._write_lock
        with lock:
            own_transaction = not in_managed_transaction and not conn.in_transaction
            try:
                if own_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                conn.executescript(sql_script)
                if own_transaction:
                    conn.commit()
            except sqlite3.Error as exc:
                if own_transaction:
                    conn.rollback()
                raise DatabaseError(str(exc)) from exc

    # ---- Read operations (concurrent, no global lock) ---------------------

    @classmethod
    def execute_read(cls, sql: str, params: Sequence[Any] | None = None) -> sqlite3.Cursor:
        """Execute a read-only query and return the cursor."""
        conn = cls.get_connection()
        t0 = time.monotonic()
        try:
            if cls._transaction_connection.get() is conn:
                with cls._operation_lock(conn):
                    cursor = conn.execute(sql, params or ())
            else:
                cursor = conn.execute(sql, params or ())
            cls._log_query(sql, params, time.monotonic() - t0)
            return cursor
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc

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
        """Execute a read-only query and return a single column value."""
        row = cls.fetchone(sql, params)
        return row[column] if row is not None else None

    # Alias for consistent naming
    fetchone_value = fetch_value

    # ---- Async variants ---------------------------------------------------

    @classmethod
    async def aexecute(cls, sql: str, params: Sequence[Any] | None = None) -> sqlite3.Cursor:
        """Async version of :meth:`execute`."""
        return await asyncio.to_thread(cls.execute, sql, params)

    @classmethod
    async def aexecutemany(cls, sql: str, params_seq: Sequence[Sequence[Any]]) -> sqlite3.Cursor:
        """Async version of :meth:`executemany`."""
        return await asyncio.to_thread(cls.executemany, sql, params_seq)

    @classmethod
    async def aexecute_read(cls, sql: str, params: Sequence[Any] | None = None) -> sqlite3.Cursor:
        """Async version of :meth:`execute_read`."""
        return await asyncio.to_thread(cls.execute_read, sql, params)

    @classmethod
    async def afetchone(cls, sql: str, params: Sequence[Any] | None = None) -> sqlite3.Row | None:
        """Async version of :meth:`fetchone`."""
        return await asyncio.to_thread(cls.fetchone, sql, params)

    @classmethod
    async def afetchall(cls, sql: str, params: Sequence[Any] | None = None) -> list[sqlite3.Row]:
        """Async version of :meth:`fetchall`."""
        return await asyncio.to_thread(cls.fetchall, sql, params)

    @classmethod
    async def afetch_value(
        cls,
        sql: str,
        params: Sequence[Any] | None = None,
        *,
        column: int | str = 0,
    ) -> Any:
        """Async version of :meth:`fetch_value`."""
        return await asyncio.to_thread(cls.fetch_value, sql, params, column=column)

    @classmethod
    def pragma(cls, name: str, value: Any = None) -> Any:
        """Read or set a SQLite PRAGMA on the active connection."""
        validate_identifier(name, kind="pragma name")
        conn = cls.get_connection()
        if value is None:
            row = conn.execute(f"PRAGMA {name}").fetchone()
            return row[0] if row is not None else None
        with cls._write_lock:
            conn.execute(f"PRAGMA {name}={value}")
        return value

    @classmethod
    async def apragma(cls, name: str, value: Any = None) -> Any:
        """Async version of :meth:`pragma`."""
        return await asyncio.to_thread(cls.pragma, name, value)

    @classmethod
    def optimize(cls) -> None:
        """Run SQLite's best-effort optimizer for the active database."""
        cls.execute_read("PRAGMA optimize")

    @classmethod
    async def aoptimize(cls) -> None:
        """Async version of :meth:`optimize`."""
        await asyncio.to_thread(cls.optimize)

    @classmethod
    def integrity_check(cls) -> str:
        """Run ``PRAGMA integrity_check`` and return SQLite's response."""
        return str(cls.fetch_value("PRAGMA integrity_check") or "")

    @classmethod
    async def aintegrity_check(cls) -> str:
        """Async version of :meth:`integrity_check`."""
        return await asyncio.to_thread(cls.integrity_check)

    # ---- Backup -----------------------------------------------------------

    @classmethod
    def backup(
        cls,
        target_path: str,
        *,
        pages: int = -1,
        progress: Any | None = None,
    ) -> None:
        """Create an online backup of the database to *target_path*.

        Uses SQLite's ``connection.backup()`` API for a consistent,
        lock-free copy::

            Database.backup("backup.sqlite3")

        Args:
            target_path: File path for the backup database.
            pages: Number of pages to copy at a time (-1 = all at once).
            progress: Optional callback ``(status, remaining, total) -> None``.
        """
        source_conn = cls.get_connection()
        target_conn = sqlite3.connect(target_path)
        try:
            source_conn.backup(target_conn, pages=pages, progress=progress)
        except sqlite3.Error as exc:
            raise DatabaseError(f"Backup failed: {exc}") from exc
        finally:
            target_conn.close()

    @classmethod
    async def abackup(
        cls,
        target_path: str,
        *,
        pages: int = -1,
        progress: Any | None = None,
    ) -> None:
        """Async version of :meth:`backup`."""
        await asyncio.to_thread(
            cls.backup, target_path, pages=pages, progress=progress,
        )

    # ---- Pool info --------------------------------------------------------

    @classmethod
    def pool_status(cls) -> dict[str, Any]:
        """Return connection pool statistics."""
        with cls._lock:
            active = len(cls._connections)
            ages = {
                cid: time.monotonic() - created
                for cid, created in cls._connection_created_at.items()
            }
        return {
            "active_connections": active,
            "pool_size_limit": cls._pool_size or "unlimited",
            "max_connection_age": cls._max_connection_age or "unlimited",
            "connection_ages": ages,
            "db_path": cls._db_path,
        }

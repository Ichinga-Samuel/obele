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

logger = logging.getLogger("obele")


@dataclass
class ScopedBinding:
    db_path: str
    pragmas: dict[str, Any]
    connection: Connection | None = None


class DatabaseScope:
    """Context manager for temporary scoped database bindings."""

    __slots__ = ("database_cls", "binding", "token")

    def __init__(self, database_cls: type[Database], db_path: str, pragmas: dict[str, Any] | None = None) -> None:
        self.database_cls = database_cls
        self.binding = ScopedBinding(db_path, pragmas or {})
        self.token: Token[ScopedBinding | None] | None = None

    def __enter__(self) -> type[Database]:
        self.token = self.database_cls.scoped_binding.set(self.binding)
        return self.database_cls

    def __exit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: Any) -> None:
        self.binding.connection.close()
        self.binding.connection = None
        if self.token is not None:
            self.database_cls.scoped_binding.reset(self.token)

    async def __aenter__(self) -> type[Database]:
        return self.__enter__()

    async def __aexit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: Any) -> None:
        self.__exit__(exc_type, exc_val, exc_tb)


class DatabaseTransaction:
    """Context manager for explicit transactions with savepoint nesting."""

    __slots__ = ("database_cls", "connection", "savepoint_name", "token", "owns_lock")
    connection: Connection

    def __init__(self, database_cls: type[Database]) -> None:
        self.database_cls = database_cls
        self.token: Token[Connection | None] | None = None
        self.savepoint_name: str | None = None
        self.owns_lock = False

    def __enter__(self) -> Connection:
        db = self.database_cls
        existing = db.transaction_connection.get()
        if existing is None:
            db._write_lock.acquire()
            self.owns_lock = True
        try:
            self.connection = existing or db.get_connection()
            if existing is None:
                self.token = db.transaction_connection.set(self.connection)
            if self.connection.connection.in_transaction:
                db._savepoint_counter += 1
                self.savepoint_name = f"{SAVEPOINT_PREFIX}{db._savepoint_counter}"
                self.connection.connection.execute(f"SAVEPOINT {self.savepoint_name}")
            else:
                self.connection.connection.execute("BEGIN IMMEDIATE")
            return self.connection
        except sqlite3.Error as exc:
            if self.token is not None:
                db.transaction_connection.reset(self.token)
                self.token = None
            if self.owns_lock:
                db._write_lock.release()
                self.owns_lock = False
            raise DatabaseError(str(exc)) from exc

    def __exit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: Any) -> None:
        assert self.connection is not None
        try:
            sp = self.savepoint_name
            if exc_type is None:
                if sp is not None:
                    self.connection.connection.execute(f"RELEASE SAVEPOINT {sp}")
                else:
                    self.connection.connection.commit()
            else:
                if sp is not None:
                    self.connection.connection.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                    self.connection.connection.execute(f"RELEASE SAVEPOINT {sp}")
                else:
                    self.connection.connection.rollback()
        except sqlite3.IntegrityError as exc:
            raise IntegrityError(str(exc)) from exc
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc
        finally:
            if self.token is not None:
                self.database_cls.transaction_connection.reset(self.token)
                self.token = None
            if self.owns_lock:
                self.database_cls._write_lock.release()
                self.owns_lock = False

    async def __aenter__(self) -> sqlite3.Connection:
        return self.__enter__()

    async def __aexit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: Any) -> None:
        self.__exit__(exc_type, exc_val, exc_tb)

@dataclass
class Connection:
    conn_id: int
    connection: sqlite3.Connection
    database: type[Database] = None
    read_lock: threading.RLock = field(default_factory=threading.RLock)
    transaction_lock: threading.RLock = field(default_factory=threading.RLock)
    created_at: float | int = field(default_factory=time.monotonic)

    def close(self) -> None:
        try:
            self.connection.close()
            return None
        except sqlite3.Error:
            pass
        finally:
            with self.read_lock:
                self.database.connections.pop(self.conn_id)
            return None

    def should_recycle(self) -> bool:
        """Return True if the connection has exceeded max_connection_age."""
        if self.database._max_connection_age <= 0:
            return False
        return (time.monotonic() - self.created_at) > self.database._max_connection_age


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
    _pool_size: int = 0  # 0 = unlimited
    _max_connection_age: float = 0.0  # seconds, 0 = no recycling
    _log_queries: bool = False
    _slow_query_threshold: float = 0.0  # seconds, 0 = disabled
    scoped_binding: ContextVar[ScopedBinding | None] = ContextVar(SCOPED_BINDING_CONTEXT, default=None)
    transaction_connection: ContextVar[Connection | None] = ContextVar(f"{SCOPED_BINDING_CONTEXT}_transaction_connection",
                                                                           default=None)
    connections: dict[int, Connection] = {}

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
        if (binding := self.scoped_binding.get()) is not None:
            return (f"<Database path={binding.db_path!r} "
                    f"connected={binding.connection is not None} scoped=True>"
            )
        return f"<Database path={self._db_path!r}>"


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
    def using(cls, db_path: str | None = None, pragmas: dict[str, Any] | None = None) -> DatabaseScope:
        """Return a sync/async context manager for a temporary scoped binding."""
        resolved = db_path if db_path is not None else default_database_path()
        return DatabaseScope(cls, resolved, pragmas)

    @classmethod
    def transaction(cls) -> DatabaseTransaction:
        """Return a sync/async transaction context manager."""
        return DatabaseTransaction(cls)

    @classmethod
    def current_config(cls) -> tuple[str, dict[str, Any]]:
        """Return the active database path and pragma configuration."""
        if (binding := cls.scoped_binding.get()) is not None:
            return binding.db_path, dict(binding.pragmas)
        return cls._db_path, dict(cls._pragmas)

    @classmethod
    def create_connection(cls, db_path: str, pragmas: dict[str, Any]) -> Connection:
        if cls._pool_size > 0:
            with cls._lock:
                if len(cls.connections) >= cls._pool_size:
                    error_msg = f"Connection pool exhausted (max {cls._pool_size}). Close unused connections or increase pool_size."
                    raise DatabaseError(error_msg)
        try:
            is_uri = db_path.startswith("file:")
            conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None, uri=is_uri)
        except sqlite3.Error as exc:
            raise DatabaseError(f"Could not connect to {db_path}") from exc

        conn.row_factory = sqlite3.Row
        # Performance pragmas
        is_memory = db_path == ":memory:" or ":memory:" in db_path or "mode=memory" in db_path
        if not is_memory:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA cache_size=-8000")  # 8MB
        conn.execute("PRAGMA mmap_size=268435456")  # 256MB
        conn.execute("PRAGMA synchronous=NORMAL")
        for pragma, value in pragmas.items():
            conn.execute(f"PRAGMA {pragma}={value}")
        with cls._lock:
            connection = Connection(id(conn), conn, cls, read_lock=cls._lock)
            cls.connections[connection.conn_id] = connection
        return connection

    @classmethod
    def _close_all_thread_local(cls) -> None:
        """Close the current thread's connection (best-effort)."""
        conn: Connection | None = getattr(cls._local, "connection", None)
        res = conn.close() if conn is not None else None
        cls._local.connection = None
        return res

    @classmethod
    def close_all(cls) -> None:
        """Close every tracked connection created by this process."""
        with cls._lock:
            connections = list(cls.connections.values())
        for conn in connections:
            conn.close()
        cls._local.connection = None

    @classmethod
    async def aclose_all(cls) -> None:
        """Async version of :meth:`close_all`."""
        await asyncio.to_thread(cls.close_all)

    @classmethod
    def get_connection(cls) -> Connection:
        """Return the active connection, creating one per-thread if necessary."""
        transaction_connection = cls.transaction_connection.get()
        if transaction_connection is not None:
            return transaction_connection

        binding = cls.scoped_binding.get()
        if binding is not None:
            if binding.connection is not None and binding.connection.should_recycle():
                binding.connection.close()
                binding.connection = None
            if binding.connection is None:
                binding.connection = cls.create_connection(binding.db_path, binding.pragmas)
            return binding.connection

        conn: Connection | None = getattr(cls._local, "connection", None)
        if conn is not None and conn.should_recycle():
            conn.close()
            conn = None
            cls._local.connection = None
        if conn is None:
            if not cls._db_path:
                cls._db_path = default_database_path()
            conn = cls.create_connection(cls._db_path, cls._pragmas)
            cls._local.connection = conn
        return conn

    @classmethod
    def close(cls) -> None:
        """Close the active connection for the current thread."""
        binding = cls.scoped_binding.get()
        if binding is not None and binding.connection is not None:
            binding.connection.close()
            binding.connection = None
            return
        conn = getattr(cls._local, "connection", None)
        conn.close() if conn is not None else ...
        cls._local.connection = None

    @classmethod
    async def aclose(cls) -> None:
        """Async version of :meth:`close`."""
        await asyncio.to_thread(cls.close)

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

    @classmethod
    def _execute_write(cls, sql: str, params: Sequence[Any] | None, *, many: bool = False) -> sqlite3.Cursor:
        """Shared write path for execute and executemany."""
        connection = cls.get_connection()
        in_managed_transaction = cls.transaction_connection.get() is connection
        lock = connection.transaction_lock if in_managed_transaction else cls._write_lock
        t0 = time.monotonic()
        with lock:
            conn = connection.connection
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
        connection = cls.get_connection()
        in_managed_transaction = cls.transaction_connection.get() is connection
        lock = connection.transaction_lock if in_managed_transaction else cls._write_lock
        with lock:
            conn = connection.connection
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

    @classmethod
    def execute_read(cls, sql: str, params: Sequence[Any] | None = None) -> sqlite3.Cursor:
        """Execute a read-only query and return the cursor."""
        connection = cls.get_connection()
        t0 = time.monotonic()
        try:
            conn = connection.connection
            if cls.transaction_connection.get() is connection:
                with connection.read_lock:
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
    async def afetch_value(cls, sql: str, params: Sequence[Any] | None = None, *, column: int | str = 0) -> Any:
        """Async version of :meth:`fetch_value`."""
        return await asyncio.to_thread(cls.fetch_value, sql, params, column=column)

    @classmethod
    def pragma(cls, name: str, value: Any = None) -> Any:
        """Read or set a SQLite PRAGMA on the active connection."""
        validate_identifier(name, kind="pragma name")
        connection = cls.get_connection()
        conn = connection.connection
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

    @classmethod
    def backup(cls, target_path: str, *, pages: int = -1, progress: Any | None = None) -> None:
        """Create an online backup of the database to *target_path*.

        Uses SQLite's ``connection.backup()`` API for a consistent,
        lock-free copy::

            Database.backup("backup.sqlite3")

        Args:
            target_path: File path for the backup database.
            pages: Number of pages to copy at a time (-1 = all at once).
            progress: Optional callback ``(status, remaining, total) -> None``.
        """
        source_connection = cls.get_connection()
        source_conn = source_connection.connection
        target_conn = sqlite3.connect(target_path)
        try:
            source_conn.backup(target_conn, pages=pages, progress=progress)
        except sqlite3.Error as exc:
            raise DatabaseError(f"Backup failed: {exc}") from exc
        finally:
            target_conn.close()

    @classmethod
    async def abackup(cls, target_path: str, *, pages: int = -1, progress: Any | None = None) -> None:
        """Async version of :meth:`backup`."""
        await asyncio.to_thread(cls.backup, target_path, pages=pages, progress=progress)

    @classmethod
    def pool_status(cls) -> dict[str, Any]:
        """Return connection pool statistics."""
        with cls._lock:
            active = len(cls.connections)
            ages = {conn.conn_id: time.monotonic() - conn.created_at for conn in cls.connections.values()}
        return {
            "active_connections": active,
            "pool_size_limit": cls._pool_size or "unlimited",
            "max_connection_age": cls._max_connection_age or "unlimited",
            "connection_ages": ages,
            "db_path": cls._db_path,
        }

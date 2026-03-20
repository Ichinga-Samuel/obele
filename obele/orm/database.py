"""Thread-safe SQLite connection manager with sync, async, and scoped APIs."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Sequence

from .._identity import SCOPED_BINDING_CONTEXT, SAVEPOINT_PREFIX, default_database_path
from .exceptions import DatabaseError, IntegrityError


@dataclass
class _ScopedBinding:
    db_path: str
    pragmas: dict[str, Any]
    connection: sqlite3.Connection | None = None


class _DatabaseScope:
    def __init__(
        self,
        database_cls: type[Database],
        db_path: str,
        pragmas: dict[str, Any] | None = None,
    ) -> None:
        self._database_cls = database_cls
        self._binding = _ScopedBinding(db_path, pragmas or {})
        self._token: Token[_ScopedBinding | None] | None = None

    def __enter__(self) -> type[Database]:
        self._token = self._database_cls._scoped_binding.set(self._binding)
        return self._database_cls

    def __exit__(
        self,
        exc_type: type | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        self._database_cls._close_binding(self._binding)
        if self._token is not None:
            self._database_cls._scoped_binding.reset(self._token)

    async def __aenter__(self) -> type[Database]:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        self.__exit__(exc_type, exc_val, exc_tb)


class _DatabaseTransaction:
    def __init__(self, database_cls: type[Database]) -> None:
        self._database_cls = database_cls
        self._connection: sqlite3.Connection | None = None
        self._savepoint_name: str | None = None

    def __enter__(self) -> sqlite3.Connection:
        database_cls = self._database_cls
        database_cls._connection_lock.acquire()
        try:
            self._connection = database_cls.get_connection()
            if self._connection.in_transaction:
                database_cls._savepoint_counter += 1
                self._savepoint_name = f"{SAVEPOINT_PREFIX}{database_cls._savepoint_counter}"
                self._connection.execute(f"SAVEPOINT {self._savepoint_name}")
            else:
                self._connection.execute("BEGIN")
            return self._connection
        except sqlite3.Error as exc:
            database_cls._connection_lock.release()
            raise DatabaseError(str(exc)) from exc

    def __exit__(
        self,
        exc_type: type | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        assert self._connection is not None
        try:
            if exc_type is None:
                if self._savepoint_name is not None:
                    self._connection.execute(f"RELEASE SAVEPOINT {self._savepoint_name}")
                else:
                    self._connection.commit()
            else:
                if self._savepoint_name is not None:
                    self._connection.execute(f"ROLLBACK TO SAVEPOINT {self._savepoint_name}")
                    self._connection.execute(f"RELEASE SAVEPOINT {self._savepoint_name}")
                else:
                    self._connection.rollback()
        except sqlite3.IntegrityError as exc:
            raise IntegrityError(str(exc)) from exc
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc
        finally:
            self._database_cls._connection_lock.release()

    async def __aenter__(self) -> sqlite3.Connection:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        self.__exit__(exc_type, exc_val, exc_tb)


class _LockedCursor:
    """Cursor wrapper that keeps the SQLite connection lock until exhausted."""

    def __init__(self, cursor: sqlite3.Cursor, lock: threading.Lock) -> None:
        self._cursor = cursor
        self._lock = lock
        self._released = False

    def _release(self) -> None:
        if self._released:
            return
        self._released = True
        self._lock.release()

    def fetchone(self) -> sqlite3.Row | None:
        row = self._cursor.fetchone()
        if row is None:
            self.close()
        return row

    def fetchmany(self, size: int | None = None) -> list[sqlite3.Row]:
        rows = self._cursor.fetchmany(size) if size is not None else self._cursor.fetchmany()
        if not rows:
            self.close()
        return rows

    def fetchall(self) -> list[sqlite3.Row]:
        try:
            return self._cursor.fetchall()
        finally:
            self.close()

    def close(self) -> None:
        try:
            self._cursor.close()
        except sqlite3.Error:
            pass
        finally:
            self._release()

    def __iter__(self):
        try:
            while True:
                row = self._cursor.fetchone()
                if row is None:
                    break
                yield row
        finally:
            self.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def __del__(self) -> None:
        self._release()


class Database:
    """Thread-safe SQLite connection manager with sync and async APIs.

    The configured database remains available globally, but callers can also
    open a temporary scoped binding via :meth:`using` to avoid mutating the
    process-wide configuration for all concurrent work.
    """

    _lock: threading.RLock = threading.RLock()
    _connection_lock: threading.Lock = threading.Lock()
    _connection: sqlite3.Connection | None = None
    _db_path: str = ""
    _pragmas: dict[str, Any] = {}
    _savepoint_counter: int = 0
    _scoped_binding: ContextVar[_ScopedBinding | None] = ContextVar(
        SCOPED_BINDING_CONTEXT,
        default=None,
    )

    def __enter__(self) -> Database:
        self.get_connection()
        return self

    def __exit__(
        self,
        exc_type: type | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        self.close()

    async def __aenter__(self) -> Database:
        self.get_connection()
        return self

    async def __aexit__(
        self,
        exc_type: type | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        binding = self._scoped_binding.get()
        if binding is not None:
            return (
                f"<Database path={binding.db_path!r} connected={binding.connection is not None} "
                f"scoped=True>"
            )
        return f"<Database path={self._db_path!r} connected={self._connection is not None}>"

    @classmethod
    def configure(
        cls,
        db_path: str | None = None,
        pragmas: dict[str, Any] | None = None,
    ) -> None:
        """Configure or reconfigure the global database connection."""
        resolved_db_path = db_path if db_path is not None else default_database_path()
        with cls._connection_lock:
            with cls._lock:
                cls._close_binding(_ScopedBinding(cls._db_path, cls._pragmas, cls._connection))
                cls._db_path = resolved_db_path
                cls._pragmas = pragmas or {}
                cls._connection = cls._create_connection(cls._db_path, cls._pragmas)

    @classmethod
    async def aconfigure(
        cls,
        db_path: str | None = None,
        pragmas: dict[str, Any] | None = None,
    ) -> None:
        """Async version of :meth:`configure`."""
        await asyncio.to_thread(cls.configure, db_path, pragmas)

    @classmethod
    def using(
        cls,
        db_path: str | None = None,
        pragmas: dict[str, Any] | None = None,
    ) -> _DatabaseScope:
        """Return a sync/async context manager for a temporary scoped binding."""
        resolved_db_path = db_path if db_path is not None else default_database_path()
        return _DatabaseScope(cls, resolved_db_path, pragmas)

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

    @classmethod
    def _create_connection(
        cls,
        db_path: str,
        pragmas: dict[str, Any],
    ) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(db_path, check_same_thread=False)
        except sqlite3.Error as exc:
            raise DatabaseError(f"Could not connect to {db_path}") from exc

        conn.row_factory = sqlite3.Row
        if db_path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        for pragma, value in pragmas.items():
            conn.execute(f"PRAGMA {pragma}={value}")
        return conn

    @classmethod
    def _close_binding(cls, binding: _ScopedBinding) -> None:
        if binding.connection is None:
            return
        try:
            binding.connection.close()
        except sqlite3.Error:
            pass
        binding.connection = None

    @classmethod
    def _current_binding(cls) -> _ScopedBinding | None:
        return cls._scoped_binding.get()

    @classmethod
    def get_connection(cls) -> sqlite3.Connection:
        """Return the active connection, creating it if necessary."""
        binding = cls._current_binding()
        if binding is not None:
            if binding.connection is None:
                with cls._lock:
                    if binding.connection is None:
                        binding.connection = cls._create_connection(
                            binding.db_path,
                            binding.pragmas,
                        )
            return binding.connection

        if cls._connection is None:
            with cls._lock:
                if cls._connection is None:
                    cls._connection = cls._create_connection(cls._db_path, cls._pragmas)
        return cls._connection  # type: ignore[return-value]

    @classmethod
    def close(cls) -> None:
        """Close the active connection if open."""
        binding = cls._current_binding()
        if binding is not None:
            with cls._connection_lock:
                cls._close_binding(binding)
            return
        with cls._connection_lock:
            cls._close_binding(_ScopedBinding(cls._db_path, cls._pragmas, cls._connection))
            cls._connection = None

    @classmethod
    async def aclose(cls) -> None:
        """Async version of :meth:`close`."""
        await asyncio.to_thread(cls.close)

    @classmethod
    def execute(
        cls,
        sql: str,
        params: Sequence[Any] | None = None,
    ) -> sqlite3.Cursor:
        """Execute a single write SQL statement."""
        conn = cls.get_connection()
        with cls._connection_lock:
            try:
                cursor = conn.execute(sql, params or ())
                conn.commit()
                return cursor
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise IntegrityError(str(exc)) from exc
            except sqlite3.Error as exc:
                conn.rollback()
                raise DatabaseError(str(exc)) from exc

    @classmethod
    def executemany(
        cls,
        sql: str,
        params_seq: Sequence[Sequence[Any]],
    ) -> sqlite3.Cursor:
        """Execute an SQL statement against many parameter sets."""
        conn = cls.get_connection()
        with cls._connection_lock:
            try:
                cursor = conn.executemany(sql, params_seq)
                conn.commit()
                return cursor
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise IntegrityError(str(exc)) from exc
            except sqlite3.Error as exc:
                conn.rollback()
                raise DatabaseError(str(exc)) from exc

    @classmethod
    def execute_read(
        cls,
        sql: str,
        params: Sequence[Any] | None = None,
    ) -> _LockedCursor:
        """Execute a read-only query."""
        conn = cls.get_connection()
        cls._connection_lock.acquire()
        try:
            cursor = conn.execute(sql, params or ())
            return _LockedCursor(cursor, cls._connection_lock)
        except sqlite3.Error as exc:
            cls._connection_lock.release()
            raise DatabaseError(str(exc)) from exc

    @classmethod
    def fetchone(
        cls,
        sql: str,
        params: Sequence[Any] | None = None,
    ) -> sqlite3.Row | None:
        """Execute a read-only query and return the first row."""
        cursor = cls.execute_read(sql, params)
        try:
            return cursor.fetchone()
        finally:
            cursor.close()

    @classmethod
    def fetchall(
        cls,
        sql: str,
        params: Sequence[Any] | None = None,
    ) -> list[sqlite3.Row]:
        """Execute a read-only query and return all rows."""
        cursor = cls.execute_read(sql, params)
        try:
            return cursor.fetchall()
        finally:
            cursor.close()

    @classmethod
    def fetch_value(
        cls,
        sql: str,
        params: Sequence[Any] | None = None,
        *,
        column: int | str = 0,
    ) -> Any:
        """Execute a read-only query and return a single column value."""
        row = cls.fetchone(sql, params)
        if row is None:
            return None
        return row[column]

    @classmethod
    async def aexecute(
        cls,
        sql: str,
        params: Sequence[Any] | None = None,
    ) -> sqlite3.Cursor:
        """Async version of :meth:`execute`."""
        return await asyncio.to_thread(cls.execute, sql, params)

    @classmethod
    async def aexecutemany(
        cls,
        sql: str,
        params_seq: Sequence[Sequence[Any]],
    ) -> sqlite3.Cursor:
        """Async version of :meth:`executemany`."""
        return await asyncio.to_thread(cls.executemany, sql, params_seq)

    @classmethod
    async def aexecute_read(
        cls,
        sql: str,
        params: Sequence[Any] | None = None,
    ) -> _LockedCursor:
        """Async version of :meth:`execute_read`."""
        return await asyncio.to_thread(cls.execute_read, sql, params)

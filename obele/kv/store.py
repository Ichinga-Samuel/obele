"""Single-table key-value storage built on top of :mod:`obele.orm.database`.

Provides :class:`KVStore`, a :class:`~collections.abc.MutableMapping` with
support for sorted iteration, slicing, range queries, multi-key lookups,
batch writes, TTL expiration, and pluggable serialization.

Usage::

    from obele import Database, KVStore

    Database.configure("myapp.sqlite3")
    store = KVStore("settings")

    store["theme"] = "dark"
    store["lang"]  = "en"
    print(store["theme"])             # "dark"
    print(store["a":"z"])             # dict of all keys in [a, z)

    # TTL support
    store.set("temp", "value", ttl=300)  # expires in 5 minutes

    # Multi-key
    store.get_many("theme", "lang")   # {"theme": "dark", "lang": "en"}
"""

from __future__ import annotations


import json
import math
import pickle
import time
from collections.abc import Hashable, Iterable, Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Callable, Literal

from ..orm.database import Database
from ..orm.sql import validate_identifier

_Dumps = Callable[[Any], bytes]
_Loads = Callable[[bytes], Any]

SerializerMode = Literal["auto", "json", "pickle"]
MultiGetReturn = Literal["dict", "tuple"]

_MISSING = object()

_SORTABLE_KEY_TYPES: dict[type[Any], str] = {
    int: "int", float: "float", str: "str", bytes: "bytes",
}
_SORTABLE_KEY_FORMATS: dict[str, type[Any]] = {v: k for k, v in _SORTABLE_KEY_TYPES.items()}


def _validate_identifier(name: str) -> str:
    """Ensure *name* is a safe SQLite identifier."""
    return validate_identifier(name, kind="table_name")


def _ensure_hashable(key: Any) -> None:
    if not isinstance(key, Hashable):
        raise TypeError(f"keys must be hashable, got {type(key).__name__}")


def _json_safe_encode(value: Any) -> bytes:
    """Encode *value* as compact JSON, raising ``TypeError`` on failure."""
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise TypeError("JSON serialization failed for the provided value") from exc
    round_tripped = json.loads(text)
    if type(round_tripped) is not type(value) or round_tripped != value:
        raise TypeError("JSON serialization would not preserve the original value type")
    return text.encode("utf-8")


@dataclass(frozen=True, slots=True)
class _EncodedKey:
    lookup_key: bytes
    key_format: str
    key_payload: bytes
    key_int: int | None = None
    key_real: float | None = None
    key_text: str | None = None
    key_blob: bytes | None = None


@dataclass(frozen=True, slots=True)
class _EncodedValue:
    value_format: str
    value_payload: bytes


# SQL fragments cached per-table to avoid repeated f-string allocation
_UPSERT_SQL_CACHE: dict[str, str] = {}


def _get_upsert_sql(table: str) -> str:
    """Return the cached UPSERT SQL template for a table."""
    sql = _UPSERT_SQL_CACHE.get(table)
    if sql is None:
        sql = f"""
            INSERT INTO {table} (
                lookup_key, namespace, key_format, key_payload,
                key_int, key_real, key_text, key_blob,
                value_format, value_payload, expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(lookup_key) DO UPDATE SET
                namespace      = excluded.namespace,
                key_format     = excluded.key_format,
                key_payload    = excluded.key_payload,
                key_int        = excluded.key_int,
                key_real       = excluded.key_real,
                key_text       = excluded.key_text,
                key_blob       = excluded.key_blob,
                value_format   = excluded.value_format,
                value_payload  = excluded.value_payload,
                expires_at     = excluded.expires_at
        """
        _UPSERT_SQL_CACHE[table] = sql
    return sql


class KVStore(MutableMapping[Any, Any]):
    """A fast, single-table key-value store with a dict-like interface.

    Parameters
    ----------
    table_name:
        SQLite table name (must be a valid identifier).
    key_type:
        Optional Python type constraining keys - ``int``, ``float``,
        ``str``, or ``bytes``.
    enforce_key_type:
        When ``True`` (default), all keys must share the same sortable
        type, enabling ordered iteration and range queries.
    serializer:
        ``"auto"`` (default) tries JSON first then falls back to pickle.
        ``"json"`` forces JSON-only.  ``"pickle"`` forces pickle-only.
        A ``(dumps, loads)`` callable pair for custom serialization.
    namespace:
        Optional string prefix applied to all keys, enabling multiple
        logical stores in one table.
    """

    def __init__(
        self,
        table_name: str = "kv_store",
        *,
        key_type: type[Any] | None = None,
        enforce_key_type: bool = True,
        serializer: SerializerMode | tuple[_Dumps, _Loads] = "auto",
        namespace: str | None = None,
    ) -> None:
        self._table = _validate_identifier(table_name)
        self._enforce = enforce_key_type
        self._declared_key_type = key_type
        self._resolved_key_type: type[Any] | None = key_type
        self._resolved_key_format: str | None = (
            _SORTABLE_KEY_TYPES.get(key_type) if key_type else None
        )
        self._namespace = "" if namespace is None else str(namespace)

        if enforce_key_type and key_type is not None and key_type not in _SORTABLE_KEY_TYPES:
            raise TypeError(
                f"enforce_key_type only supports int, float, str, or bytes keys, "
                f"got {key_type.__name__}"
            )

        if isinstance(serializer, tuple):
            self._custom_dumps, self._custom_loads = serializer
            self._serializer_mode: str = "custom"
        else:
            if serializer not in ("auto", "json", "pickle"):
                raise ValueError(
                    f"serializer must be 'auto', 'json', 'pickle', or a "
                    f"(dumps, loads) tuple, got {serializer!r}"
                )
            self._custom_dumps = None
            self._custom_loads = None
            self._serializer_mode = serializer

        self._ensure_table()
        self._load_existing_key_type()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def table_name(self) -> str:
        """The SQLite table name backing this store."""
        return self._table

    @property
    def key_type(self) -> type[Any] | None:
        """Resolved key type (``None`` if not yet determined)."""
        return self._resolved_key_type

    @property
    def namespace(self) -> str:
        """Logical namespace isolating this store's keys in the table."""
        return self._namespace

    # ------------------------------------------------------------------
    # Namespace support
    # ------------------------------------------------------------------

    def _apply_namespace(self, key: Any) -> Any:
        """Return *key* unchanged; namespace is stored separately."""
        return key

    def _strip_namespace(self, key: Any) -> Any:
        """Return *key* unchanged; namespace is stored separately."""
        return key

    def _namespaced_lookup_key(self, lookup_key: bytes) -> bytes:
        """Return a physical lookup key isolated by this store's namespace."""
        if not self._namespace:
            return lookup_key
        prefix = self._namespace.encode("utf-8")
        return b"ns:" + prefix + b"\0" + lookup_key

    # ------------------------------------------------------------------
    # Table management
    # ------------------------------------------------------------------

    def _ensure_table(self) -> None:
        """Create the backing table and partial indexes if they don't exist."""
        Database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._table} (
                lookup_key   BLOB PRIMARY KEY,
                namespace    TEXT NOT NULL DEFAULT '',
                key_format   TEXT NOT NULL,
                key_payload  BLOB NOT NULL,
                key_int      INTEGER,
                key_real     REAL,
                key_text     TEXT,
                key_blob     BLOB,
                value_format TEXT NOT NULL,
                value_payload BLOB NOT NULL,
                expires_at   REAL
            ) WITHOUT ROWID
            """
        )
        columns = {
            row["name"]
            for row in Database.fetchall(f"PRAGMA table_info({self._table})")
        }
        if "namespace" not in columns:
            Database.execute(
                f"ALTER TABLE {self._table} "
                "ADD COLUMN namespace TEXT NOT NULL DEFAULT ''"
            )
        Database.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{self._table}_namespace
            ON {self._table} (namespace)
            """
        )
        for col in ("key_int", "key_real", "key_text", "key_blob"):
            Database.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{self._table}_namespace_{col}
                ON {self._table} (namespace, {col})
                WHERE {col} IS NOT NULL
                """
            )

    async def _aensure_table(self) -> None:
        """Async version of :meth:`_ensure_table`."""
        cursor = await Database.aexecute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._table} (
                lookup_key   BLOB PRIMARY KEY,
                namespace    TEXT NOT NULL DEFAULT '',
                key_format   TEXT NOT NULL,
                key_payload  BLOB NOT NULL,
                key_int      INTEGER,
                key_real     REAL,
                key_text     TEXT,
                key_blob     BLOB,
                value_format TEXT NOT NULL,
                value_payload BLOB NOT NULL,
                expires_at   REAL
            ) WITHOUT ROWID
            """
        )
        await cursor.close()
        columns = {
            row["name"]
            for row in await Database.afetchall(f"PRAGMA table_info({self._table})")
        }
        if "namespace" not in columns:
            cursor = await Database.aexecute(
                f"ALTER TABLE {self._table} "
                "ADD COLUMN namespace TEXT NOT NULL DEFAULT ''"
            )
            await cursor.close()
        cursor = await Database.aexecute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{self._table}_namespace
            ON {self._table} (namespace)
            """
        )
        await cursor.close()
        for col in ("key_int", "key_real", "key_text", "key_blob"):
            cursor = await Database.aexecute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{self._table}_namespace_{col}
                ON {self._table} (namespace, {col})
                WHERE {col} IS NOT NULL
                """
            )
            await cursor.close()

    def _load_existing_key_type(self) -> None:
        """Detect and validate the key type of existing rows."""
        if not self._enforce:
            return
        formats = [
            row["key_format"]
            for row in Database.fetchall(
                f"SELECT DISTINCT key_format FROM {self._table} "
                f"WHERE namespace = ? LIMIT 2",
                [self._namespace],
            )
        ]
        if not formats:
            return
        if len(formats) > 1:
            raise ValueError(
                f"{self._table!r} contains mixed key types; "
                f"set enforce_key_type=False to use it"
            )
        existing_format = formats[0]
        existing_type = _SORTABLE_KEY_FORMATS.get(existing_format)
        if existing_type is None:
            raise ValueError(
                f"{self._table!r} contains non-sortable keys; "
                f"set enforce_key_type=False to use it"
            )
        if self._declared_key_type is not None and existing_type is not self._declared_key_type:
            raise ValueError(
                f"{self._table!r} already stores {existing_type.__name__} keys, "
                f"not {self._declared_key_type.__name__}"
            )
        self._resolved_key_type = existing_type
        self._resolved_key_format = existing_format

    def create_table(self, if_not_exists: bool = True) -> None:
        """Explicitly (re-)create the backing table."""
        self._ensure_table()

    async def acreate_table(self, if_not_exists: bool = True) -> None:
        """Async version of :meth:`create_table`."""
        await self._aensure_table()

    def drop_table(self, if_exists: bool = True) -> None:
        """Drop the backing table."""
        maybe = "IF EXISTS " if if_exists else ""
        Database.execute(f"DROP TABLE {maybe}{self._table}")

    async def adrop_table(self, if_exists: bool = True) -> None:
        """Async version of :meth:`drop_table`."""
        maybe = "IF EXISTS " if if_exists else ""
        cursor = await Database.aexecute(f"DROP TABLE {maybe}{self._table}")
        await cursor.close()

    # ------------------------------------------------------------------
    # TTL support
    # ------------------------------------------------------------------

    def _now(self) -> float:
        """Current time as a Unix timestamp."""
        return time.time()

    def _is_expired(self, row: Mapping[str, Any]) -> bool:
        """Check if a row has expired based on its expires_at field."""
        expires_at = row.get("expires_at") if isinstance(row, dict) else row["expires_at"]
        return expires_at is not None and expires_at <= self._now()

    def purge_expired(self) -> int:
        """Delete all expired entries. Returns number of rows removed."""
        cursor = Database.execute(
            f"DELETE FROM {self._table} "
            f"WHERE namespace = ? AND expires_at IS NOT NULL AND expires_at <= ?",
            [self._namespace, self._now()],
        )
        return cursor.rowcount

    async def apurge_expired(self) -> int:
        """Async version of :meth:`purge_expired`."""
        cursor = await Database.aexecute(
            f"DELETE FROM {self._table} "
            f"WHERE namespace = ? AND expires_at IS NOT NULL AND expires_at <= ?",
            [self._namespace, self._now()],
        )
        try:
            return cursor.rowcount
        finally:
            await cursor.close()

    def ttl(self, key: Any) -> float | None:
        """Return seconds until expiration, or ``None`` for persistent keys."""
        key = self._apply_namespace(key)
        encoded = self._encode_key(key)
        row = Database.fetchone(
            f"SELECT expires_at FROM {self._table} "
            f"WHERE lookup_key = ? AND namespace = ? LIMIT 1",
            [encoded.lookup_key, self._namespace],
        )
        if row is None:
            raise KeyError(self._strip_namespace(key))
        expires_at = row["expires_at"]
        if expires_at is None:
            return None
        remaining = expires_at - self._now()
        if remaining <= 0:
            self.delete(key)
            raise KeyError(self._strip_namespace(key))
        return remaining

    async def attl(self, key: Any) -> float | None:
        """Async version of :meth:`ttl`."""
        key = self._apply_namespace(key)
        encoded = self._encode_key(key)
        row = await Database.afetchone(
            f"SELECT expires_at FROM {self._table} "
            f"WHERE lookup_key = ? AND namespace = ? LIMIT 1",
            [encoded.lookup_key, self._namespace],
        )
        if row is None:
            raise KeyError(self._strip_namespace(key))
        expires_at = row["expires_at"]
        if expires_at is None:
            return None
        remaining = expires_at - self._now()
        if remaining <= 0:
            cursor = await Database.aexecute(
                f"DELETE FROM {self._table} WHERE lookup_key = ? AND namespace = ?",
                [encoded.lookup_key, self._namespace],
            )
            await cursor.close()
            raise KeyError(self._strip_namespace(key))
        return remaining

    def expire(self, key: Any, ttl: float | int) -> bool:
        """Set a new TTL for an existing key."""
        key = self._apply_namespace(key)
        encoded = self._encode_key(key)
        cursor = Database.execute(
            f"UPDATE {self._table} SET expires_at = ? "
            f"WHERE lookup_key = ? AND namespace = ? "
            f"AND (expires_at IS NULL OR expires_at > ?)",
            [self._now() + ttl, encoded.lookup_key, self._namespace, self._now()],
        )
        return cursor.rowcount > 0

    async def aexpire(self, key: Any, ttl: float | int) -> bool:
        """Async version of :meth:`expire`."""
        key = self._apply_namespace(key)
        encoded = self._encode_key(key)
        cursor = await Database.aexecute(
            f"UPDATE {self._table} SET expires_at = ? "
            f"WHERE lookup_key = ? AND namespace = ? "
            f"AND (expires_at IS NULL OR expires_at > ?)",
            [self._now() + ttl, encoded.lookup_key, self._namespace, self._now()],
        )
        try:
            return cursor.rowcount > 0
        finally:
            await cursor.close()

    def persist(self, key: Any) -> bool:
        """Remove the TTL from an existing key."""
        key = self._apply_namespace(key)
        encoded = self._encode_key(key)
        cursor = Database.execute(
            f"UPDATE {self._table} SET expires_at = NULL "
            f"WHERE lookup_key = ? AND namespace = ? "
            f"AND (expires_at IS NULL OR expires_at > ?)",
            [encoded.lookup_key, self._namespace, self._now()],
        )
        return cursor.rowcount > 0

    async def apersist(self, key: Any) -> bool:
        """Async version of :meth:`persist`."""
        key = self._apply_namespace(key)
        encoded = self._encode_key(key)
        cursor = await Database.aexecute(
            f"UPDATE {self._table} SET expires_at = NULL "
            f"WHERE lookup_key = ? AND namespace = ? "
            f"AND (expires_at IS NULL OR expires_at > ?)",
            [encoded.lookup_key, self._namespace, self._now()],
        )
        try:
            return cursor.rowcount > 0
        finally:
            await cursor.close()

    # ------------------------------------------------------------------
    # MutableMapping core interface
    # ------------------------------------------------------------------

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, slice):
            return self.range(key.start, key.stop, step=key.step)

        key = self._apply_namespace(key)
        encoded = self._encode_key(key)
        row = self._fetch_row(encoded.lookup_key)
        if row is None or self._is_expired(row):
            if row is not None:
                # Lazy cleanup of expired entry
                Database.execute(
                    f"DELETE FROM {self._table} WHERE lookup_key = ? AND namespace = ?",
                    [encoded.lookup_key, self._namespace],
                )
            raise KeyError(self._strip_namespace(key))
        return self._decode_value(row["value_format"], row["value_payload"])

    def __setitem__(self, key: Any, value: Any) -> None:
        self.set(key, value)

    def __delitem__(self, key: Any) -> None:
        self.delete(key)

    def __contains__(self, key: object) -> bool:
        if isinstance(key, slice):
            return False
        try:
            key = self._apply_namespace(key)
            encoded = self._encode_key(key)
        except (TypeError, ValueError, pickle.PickleError):
            return False
        row = Database.fetchone(
            f"SELECT expires_at FROM {self._table} "
            f"WHERE lookup_key = ? AND namespace = ? LIMIT 1",
            [encoded.lookup_key, self._namespace],
        )
        if row is None:
            return False
        if self._is_expired(row):
            Database.execute(
                f"DELETE FROM {self._table} WHERE lookup_key = ? AND namespace = ?",
                [encoded.lookup_key, self._namespace],
            )
            return False
        return True

    def __len__(self) -> int:
        count = Database.fetch_value(
            f"SELECT COUNT(*) AS cnt FROM {self._table} "
            f"WHERE namespace = ? AND (expires_at IS NULL OR expires_at > ?)",
            [self._namespace, self._now()],
            column="cnt",
        )
        return int(count or 0)

    def __bool__(self) -> bool:
        row = Database.fetchone(
            f"SELECT 1 FROM {self._table} "
            f"WHERE namespace = ? AND (expires_at IS NULL OR expires_at > ?) LIMIT 1",
            [self._namespace, self._now()],
        )
        return row is not None

    def __iter__(self) -> Iterator[Any]:
        for row in self._select_rows():
            yield self._strip_namespace(self._decode_key(row))

    async def __aiter__(self):
        for key in await self.akeys():
            yield key

    def __repr__(self) -> str:
        key_name = self._resolved_key_type.__name__ if self._resolved_key_type else "unset"
        return (
            f"<KVStore table={self._table!r} namespace={self._namespace!r} "
            f"key_type={key_name} enforce={self._enforce}>"
        )

    # ------------------------------------------------------------------
    # Extended dict interface
    # ------------------------------------------------------------------

    def get(self, key: Any, default: Any = None) -> Any:
        """Return the value for *key*, or *default* if not present."""
        try:
            return self[key]
        except KeyError:
            return default

    def set(
        self,
        key: Any,
        value: Any,
        *,
        serializer: SerializerMode | None = None,
        ttl: float | int | None = None,
    ) -> None:
        """Insert or replace a single key-value pair.

        Args:
            serializer: Per-call serializer override.
            ttl: Time-to-live in seconds. ``None`` means no expiration.
        """
        key = self._apply_namespace(key)
        encoded_key = self._encode_key(key)
        encoded_value = self._encode_value(value, serializer)
        expires_at = (self._now() + ttl) if ttl is not None else None
        Database.execute(_get_upsert_sql(self._table), [
            encoded_key.lookup_key,
            self._namespace,
            encoded_key.key_format,
            encoded_key.key_payload,
            encoded_key.key_int,
            encoded_key.key_real,
            encoded_key.key_text,
            encoded_key.key_blob,
            encoded_value.value_format,
            encoded_value.value_payload,
            expires_at,
        ])

    def pop(self, key: Any, *args: Any) -> Any:
        """Remove and return the value for *key*."""
        with Database.transaction():
            try:
                value = self[key]
            except KeyError:
                if args:
                    return args[0]
                raise
            self.delete(key)
            return value

    def popitem(self, last: bool = True) -> tuple[Any, Any]:
        """Remove and return an arbitrary ``(key, value)`` pair."""
        with Database.transaction():
            rows = self._select_rows(limit=1, reverse=last)
            if not rows:
                raise KeyError("store is empty")
            row = rows[0]
            key = self._strip_namespace(self._decode_key(row))
            value = self._decode_value(row["value_format"], row["value_payload"])
            self.delete(key)
            return key, value

    def setdefault(self, key: Any, default: Any = None) -> Any:
        """Return the value for *key*, inserting *default* first if missing."""
        with Database.transaction():
            try:
                return self[key]
            except KeyError:
                self[key] = default
                return default

    def update(self, other: Mapping[Any, Any] | Iterable[tuple[Any, Any]] = (), /, **kwargs: Any) -> None:
        """Bulk-insert / update from a mapping, iterable, and/or keyword arguments."""
        pairs: list[tuple[Any, Any]] = []
        if isinstance(other, Mapping):
            pairs.extend(other.items())
        else:
            pairs.extend(other)
        if kwargs:
            pairs.extend(kwargs.items())
        if pairs:
            self.set_many(pairs)

    def clear(self) -> None:
        """Remove **all** key-value pairs from the store."""
        Database.execute(f"DELETE FROM {self._table} WHERE namespace = ?", [self._namespace])

    def keys(self) -> list[Any]:
        """Return all keys, in sort order."""
        return [self._strip_namespace(self._decode_key(row)) for row in self._select_rows()]

    def values(self) -> list[Any]:
        """Return all values, ordered by key."""
        return [
            self._decode_value(row["value_format"], row["value_payload"])
            for row in self._select_rows()
        ]

    def items(self) -> list[tuple[Any, Any]]:
        """Return all ``(key, value)`` pairs, ordered by key."""
        return [
            (self._strip_namespace(self._decode_key(row)),
             self._decode_value(row["value_format"], row["value_payload"]))
            for row in self._select_rows()
        ]

    def delete(self, key: Any) -> None:
        """Delete a single key. Raises ``KeyError`` if not present."""
        key = self._apply_namespace(key)
        encoded = self._encode_key(key)
        cursor = Database.execute(
            f"DELETE FROM {self._table} WHERE lookup_key = ? AND namespace = ?",
            [encoded.lookup_key, self._namespace],
        )
        if cursor.rowcount == 0:
            raise KeyError(self._strip_namespace(key))

    # ------------------------------------------------------------------
    # Atomic operations
    # ------------------------------------------------------------------

    def increment(self, key: Any, delta: int | float = 1) -> int | float:
        """Atomically increment a numeric value. Creates the key if missing.

        Args:
            key: The key to increment.
            delta: Amount to add (default 1). Can be negative.

        Returns:
            The new value after incrementing.
        """
        with Database.transaction():
            try:
                current = self[key]
                if not isinstance(current, (int, float)):
                    raise TypeError(f"Cannot increment non-numeric value: {type(current).__name__}")
                new_value = current + delta
            except KeyError:
                new_value = delta
            self[key] = new_value
            return new_value

    async def aincrement(self, key: Any, delta: int | float = 1) -> int | float:
        """Async version of :meth:`increment`."""
        async with Database.transaction():
            try:
                current = await self.aget(key)
                if not isinstance(current, (int, float)):
                    raise TypeError(f"Cannot increment non-numeric value: {type(current).__name__}")
                new_value = current + delta
            except KeyError:
                new_value = delta
            await self.aset(key, new_value)
            return new_value

    def compare_and_swap(
        self,
        key: Any,
        expected: Any,
        new_value: Any,
        *,
        ttl: float | int | None = None,
    ) -> bool:
        """Atomically set ``key`` to ``new_value`` if its value equals ``expected``."""
        with Database.transaction():
            try:
                current = self[key]
            except KeyError:
                return False
            if current != expected:
                return False
            self.set(key, new_value, ttl=ttl)
            return True

    async def acompare_and_swap(
        self,
        key: Any,
        expected: Any,
        new_value: Any,
        *,
        ttl: float | int | None = None,
    ) -> bool:
        """Async version of :meth:`compare_and_swap`."""
        async with Database.transaction():
            try:
                current = await self.aget(key)
            except KeyError:
                return False
            if current != expected:
                return False
            await self.aset(key, new_value, ttl=ttl)
            return True

    # ------------------------------------------------------------------
    # Batch operations
    # ------------------------------------------------------------------

    def set_many(
        self,
        items: Mapping[Any, Any] | Iterable[tuple[Any, Any]],
        *,
        serializer: SerializerMode | None = None,
        ttl: float | int | None = None,
    ) -> None:
        """Insert or replace many key-value pairs efficiently."""
        if isinstance(items, Mapping):
            pairs = list(items.items())
        else:
            pairs = list(items)
        if not pairs:
            return

        expires_at = (self._now() + ttl) if ttl is not None else None
        sql = _get_upsert_sql(self._table)
        params_seq: list[list[Any]] = []
        for key, value in pairs:
            key = self._apply_namespace(key)
            ek = self._encode_key(key)
            ev = self._encode_value(value, serializer)
            params_seq.append([
                ek.lookup_key, self._namespace, ek.key_format, ek.key_payload,
                ek.key_int, ek.key_real, ek.key_text, ek.key_blob,
                ev.value_format, ev.value_payload, expires_at,
            ])
        Database.executemany(sql, params_seq)

    def delete_many(self, keys: Sequence[Any] | Iterable[Any]) -> int:
        """Delete multiple keys at once.  Returns the number of rows removed."""
        keys_list = list(keys)
        if not keys_list:
            return 0
        encoded_keys = [self._encode_key(self._apply_namespace(k)).lookup_key for k in keys_list]
        placeholders = ", ".join("?" for _ in encoded_keys)
        cursor = Database.execute(
            f"DELETE FROM {self._table} "
            f"WHERE namespace = ? AND lookup_key IN ({placeholders})",
            [self._namespace, *encoded_keys],
        )
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Multi-key queries
    # ------------------------------------------------------------------

    def get_many(
        self,
        *keys: Any,
        return_type: MultiGetReturn = "dict",
        default: Any = _MISSING,
        skip_missing: bool = False,
    ) -> dict[Any, Any] | tuple[tuple[Any, Any], ...]:
        """Fetch many keys at once."""
        if not keys:
            return {} if return_type == "dict" else ()

        encoded_pairs = [(k, self._encode_key(self._apply_namespace(k))) for k in keys]
        placeholders = ", ".join("?" for _ in encoded_pairs)
        rows_raw = Database.fetchall(
            f"SELECT * FROM {self._table} "
            f"WHERE namespace = ? AND lookup_key IN ({placeholders})",
            [self._namespace, *[enc.lookup_key for _, enc in encoded_pairs]],
        )
        now = self._now()
        rows = {
            bytes(row["lookup_key"]): row
            for row in rows_raw
            if row["expires_at"] is None or row["expires_at"] > now
        }

        pairs: list[tuple[Any, Any]] = []
        for original_key, encoded in encoded_pairs:
            row = rows.get(encoded.lookup_key)
            if row is None:
                if skip_missing:
                    continue
                if default is _MISSING:
                    raise KeyError(original_key)
                pairs.append((original_key, default))
                continue
            pairs.append((
                original_key,
                self._decode_value(row["value_format"], row["value_payload"]),
            ))
        return self._format_pairs(pairs, return_type)

    # ------------------------------------------------------------------
    # Slicing / range queries
    # ------------------------------------------------------------------

    def range(
        self,
        start: Any = None,
        stop: Any = None,
        *,
        step: int | None = None,
        reverse: bool = False,
        return_type: MultiGetReturn = "dict",
    ) -> dict[Any, Any] | tuple[tuple[Any, Any], ...]:
        """Return items whose keys lie in ``[start, stop)``.

        Requires ``enforce_key_type=True`` with a sortable key type.
        """
        if step is not None:
            if step == 0:
                raise ValueError("step cannot be zero")
            if step < 0:
                raise ValueError("negative steps are not supported; use reverse=True")

        if not self._enforce:
            raise TypeError("range / slice queries require enforce_key_type=True")

        if self._resolved_key_type is None:
            if len(self) == 0:
                return {} if return_type == "dict" else ()
            self._load_existing_key_type()

        if self._resolved_key_type is None or self._resolved_key_format is None:
            raise TypeError("range / slice queries require a resolved sortable key type")

        column = self._sortable_column_for_format(self._resolved_key_format)
        params: list[Any] = [self._namespace, self._resolved_key_format]
        where_clauses = [
            "namespace = ?",
            "key_format = ?",
            "(expires_at IS NULL OR expires_at > ?)",
        ]
        params.append(self._now())

        if start is not None:
            self._ensure_range_key_type(start)
            where_clauses.append(f"{column} >= ?")
            params.append(start)
        if stop is not None:
            self._ensure_range_key_type(stop)
            where_clauses.append(f"{column} < ?")
            params.append(stop)

        order = "DESC" if reverse else "ASC"
        sql = (
            f"SELECT * FROM {self._table} "
            f"WHERE {' AND '.join(where_clauses)} "
            f"ORDER BY {column} {order}"
        )
        rows = Database.fetchall(sql, params)
        pairs = [
            (self._strip_namespace(self._decode_key(row)),
             self._decode_value(row["value_format"], row["value_payload"]))
            for row in rows
        ]
        if step not in (None, 1):
            pairs = pairs[::step]
        return self._format_pairs(pairs, return_type)

    def keys_slice(
        self,
        start: Any = None,
        stop: Any = None,
        *,
        step: int | None = None,
        reverse: bool = False,
    ) -> tuple[Any, ...]:
        """Return only keys from a :meth:`range` query."""
        pairs = self.range(start, stop, step=step, reverse=reverse, return_type="tuple")
        return tuple(k for k, _ in pairs)

    def values_slice(
        self,
        start: Any = None,
        stop: Any = None,
        *,
        step: int | None = None,
        reverse: bool = False,
    ) -> tuple[Any, ...]:
        """Return only values from a :meth:`range` query."""
        pairs = self.range(start, stop, step=step, reverse=reverse, return_type="tuple")
        return tuple(v for _, v in pairs)

    # ------------------------------------------------------------------
    # Key encoding / decoding
    # ------------------------------------------------------------------

    def _encode_key(self, key: Any) -> _EncodedKey:
        _ensure_hashable(key)
        encoded = self._encode_sortable_key(key) if self._enforce else self._encode_flexible_key(key)
        if not self._namespace:
            return encoded
        return replace(encoded, lookup_key=self._namespaced_lookup_key(encoded.lookup_key))

    def _encode_sortable_key(self, key: Any) -> _EncodedKey:
        key_type = type(key)
        key_format = _SORTABLE_KEY_TYPES.get(key_type)
        if key_format is None:
            raise TypeError(
                f"enforce_key_type only supports int, float, str, or bytes keys, "
                f"got {key_type.__name__}"
            )

        if self._resolved_key_type is None:
            self._resolved_key_type = key_type
            self._resolved_key_format = key_format
        elif key_type is not self._resolved_key_type:
            raise TypeError(
                f"expected {self._resolved_key_type.__name__} keys, got {key_type.__name__}"
            )

        return self._build_sortable_encoded_key(key, key_type, key_format)

    def _encode_flexible_key(self, key: Any) -> _EncodedKey:
        key_type = type(key)
        if key_type in _SORTABLE_KEY_TYPES:
            return self._build_sortable_encoded_key(
                key, key_type, _SORTABLE_KEY_TYPES[key_type],
            )
        try:
            payload = pickle.dumps(key, protocol=pickle.HIGHEST_PROTOCOL)
        except pickle.PickleError as exc:
            raise TypeError("key is not picklable") from exc
        return _EncodedKey(
            lookup_key=b"pickle:" + payload,
            key_format="pickle",
            key_payload=payload,
        )

    @staticmethod
    def _build_sortable_encoded_key(key: Any, key_type: type, key_format: str) -> _EncodedKey:
        if key_type is int:
            payload = str(key).encode("ascii")
            return _EncodedKey(lookup_key=b"int:" + payload, key_format="int",
                               key_payload=payload, key_int=key)
        if key_type is float:
            if not math.isfinite(key):
                raise ValueError("float keys must be finite")
            payload = repr(key).encode("ascii")
            return _EncodedKey(lookup_key=b"float:" + payload, key_format="float",
                               key_payload=payload, key_real=key)
        if key_type is str:
            payload = key.encode("utf-8")
            return _EncodedKey(lookup_key=b"str:" + payload, key_format="str",
                               key_payload=payload, key_text=key)
        # bytes
        payload = bytes(key)
        return _EncodedKey(lookup_key=b"bytes:" + payload, key_format="bytes",
                           key_payload=payload, key_blob=payload)

    # ------------------------------------------------------------------
    # Value encoding / decoding
    # ------------------------------------------------------------------

    def _encode_value(self, value: Any, serializer: SerializerMode | None = None) -> _EncodedValue:
        mode = serializer or self._serializer_mode

        if mode == "custom":
            return _EncodedValue("custom", self._custom_dumps(value))

        if mode in ("auto", "json"):
            try:
                payload = _json_safe_encode(value)
                return _EncodedValue("json", payload)
            except TypeError:
                if mode == "json":
                    raise

        return _EncodedValue(
            "pickle",
            pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL),
        )

    def _decode_value(self, value_format: str, payload: bytes) -> Any:
        if value_format == "json":
            return json.loads(payload.decode("utf-8"))
        if value_format == "pickle":
            return pickle.loads(payload)
        if value_format == "custom":
            return self._custom_loads(payload)
        raise ValueError(f"unsupported value format {value_format!r}")

    @staticmethod
    def _decode_key(row: Mapping[str, Any]) -> Any:
        fmt = row["key_format"]
        if fmt == "int":
            return int(row["key_int"])
        if fmt == "float":
            return float(row["key_real"])
        if fmt == "str":
            return row["key_text"]
        if fmt == "bytes":
            return bytes(row["key_blob"])
        if fmt == "pickle":
            return pickle.loads(row["key_payload"])
        raise ValueError(f"unsupported key format {fmt!r}")

    # ------------------------------------------------------------------
    # Internal query helpers
    # ------------------------------------------------------------------

    def _fetch_row(self, lookup_key: bytes) -> Mapping[str, Any] | None:
        return Database.fetchone(
            f"SELECT * FROM {self._table} WHERE lookup_key = ? AND namespace = ?",
            [lookup_key, self._namespace],
        )

    def _select_rows(self, *, limit: int | None = None, reverse: bool = False) -> list[Mapping[str, Any]]:
        now = self._now()
        if self._enforce and self._resolved_key_format is not None:
            column = self._sortable_column_for_format(self._resolved_key_format)
            order = f"ORDER BY {column} {'DESC' if reverse else 'ASC'}"
            where = (
                "WHERE namespace = ? AND key_format = ? "
                "AND (expires_at IS NULL OR expires_at > ?)"
            )
            params: list[Any] = [self._namespace, self._resolved_key_format, now]
        else:
            order = f"ORDER BY lookup_key {'DESC' if reverse else 'ASC'}"
            where = "WHERE namespace = ? AND (expires_at IS NULL OR expires_at > ?)"
            params = [self._namespace, now]

        limit_sql = f" LIMIT {limit}" if limit is not None else ""
        sql = f"SELECT * FROM {self._table} {where} {order}{limit_sql}"
        return Database.fetchall(sql, params)

    async def _afetch_row(self, lookup_key: bytes) -> Mapping[str, Any] | None:
        """Async version of :meth:`_fetch_row`."""
        return await Database.afetchone(
            f"SELECT * FROM {self._table} WHERE lookup_key = ? AND namespace = ?",
            [lookup_key, self._namespace],
        )

    async def _aselect_rows(self, *, limit: int | None = None, reverse: bool = False) -> list[Mapping[str, Any]]:
        """Async version of :meth:`_select_rows`."""
        now = self._now()
        if self._enforce and self._resolved_key_format is not None:
            column = self._sortable_column_for_format(self._resolved_key_format)
            order = f"ORDER BY {column} {'DESC' if reverse else 'ASC'}"
            where = (
                "WHERE namespace = ? AND key_format = ? "
                "AND (expires_at IS NULL OR expires_at > ?)"
            )
            params: list[Any] = [self._namespace, self._resolved_key_format, now]
        else:
            order = f"ORDER BY lookup_key {'DESC' if reverse else 'ASC'}"
            where = "WHERE namespace = ? AND (expires_at IS NULL OR expires_at > ?)"
            params = [self._namespace, now]

        limit_sql = f" LIMIT {limit}" if limit is not None else ""
        sql = f"SELECT * FROM {self._table} {where} {order}{limit_sql}"
        return await Database.afetchall(sql, params)

    def _ensure_range_key_type(self, key: Any) -> None:
        if self._resolved_key_type is not None and type(key) is not self._resolved_key_type:
            raise TypeError(
                f"range boundaries must be {self._resolved_key_type.__name__} values"
            )

    @staticmethod
    def _sortable_column_for_format(key_format: str) -> str:
        mapping = {"int": "key_int", "float": "key_real", "str": "key_text", "bytes": "key_blob"}
        col = mapping.get(key_format)
        if col is None:
            raise TypeError(f"{key_format!r} is not a sortable key format")
        return col

    @staticmethod
    def _format_pairs(
        pairs: Sequence[tuple[Any, Any]],
        return_type: MultiGetReturn,
    ) -> dict[Any, Any] | tuple[tuple[Any, Any], ...]:
        if return_type == "dict":
            return dict(pairs)
        if return_type == "tuple":
            return tuple(pairs)
        raise ValueError("return_type must be 'dict' or 'tuple'")

    # ------------------------------------------------------------------
    # Prefix and pattern queries
    # ------------------------------------------------------------------

    def prefix(
        self,
        prefix: str,
        *,
        limit: int | None = None,
        reverse: bool = False,
        return_type: MultiGetReturn = "dict",
    ) -> dict[Any, Any] | tuple[tuple[Any, Any], ...]:
        """Return all entries whose string key starts with *prefix*.

        Requires ``key_type=str`` (enforced or resolved)::

            store["user:1"] = {"name": "Alice"}
            store["user:2"] = {"name": "Bob"}
            store["post:1"] = {"title": "Hello"}

            store.prefix("user:")  # {"user:1": ..., "user:2": ...}

        Args:
            prefix: The string prefix to match.
            limit: Maximum number of results.
            reverse: If ``True``, return in reverse key order.
            return_type: ``"dict"`` or ``"tuple"``.
        """
        if self._enforce and self._resolved_key_type is not None and self._resolved_key_type is not str:
            raise TypeError("prefix() requires string keys")

        now = self._now()
        order = "DESC" if reverse else "ASC"

        # Namespace-aware prefix scan on key_text column
        sql = (
            f"SELECT * FROM {self._table} "
            f"WHERE namespace = ? AND key_format = 'str' "
            f"AND key_text >= ? AND key_text < ? "
            f"AND (expires_at IS NULL OR expires_at > ?) "
            f"ORDER BY key_text {order}"
        )
        # Build the exclusive upper bound by incrementing the last character
        upper = prefix[:-1] + chr(ord(prefix[-1]) + 1) if prefix else ""
        params: list[Any] = [self._namespace, prefix, upper, now]

        if limit is not None:
            sql += f" LIMIT {limit}"

        rows = Database.fetchall(sql, params)
        pairs = [
            (self._strip_namespace(self._decode_key(row)),
             self._decode_value(row["value_format"], row["value_payload"]))
            for row in rows
        ]
        return self._format_pairs(pairs, return_type)

    def scan(
        self,
        pattern: str = "*",
        *,
        limit: int | None = None,
        return_type: MultiGetReturn = "dict",
    ) -> dict[Any, Any] | tuple[tuple[Any, Any], ...]:
        """Return entries whose string key matches a SQL GLOB *pattern*.

        Uses SQLite's ``GLOB`` operator for pattern matching::

            store.scan("user:*")       # all keys starting with "user:"
            store.scan("*:active")     # all keys ending with ":active"
            store.scan("cache:[0-9]*") # cache keys starting with a digit

        Args:
            pattern: GLOB pattern (``*`` matches any chars, ``?`` one char).
            limit: Maximum number of results.
            return_type: ``"dict"`` or ``"tuple"``.
        """
        if self._enforce and self._resolved_key_type is not None and self._resolved_key_type is not str:
            raise TypeError("scan() requires string keys")

        now = self._now()
        sql = (
            f"SELECT * FROM {self._table} "
            f"WHERE namespace = ? AND key_format = 'str' "
            f"AND key_text GLOB ? "
            f"AND (expires_at IS NULL OR expires_at > ?) "
            f"ORDER BY key_text ASC"
        )
        params: list[Any] = [self._namespace, pattern, now]

        if limit is not None:
            sql += f" LIMIT {limit}"

        rows = Database.fetchall(sql, params)
        pairs = [
            (self._strip_namespace(self._decode_key(row)),
             self._decode_value(row["value_format"], row["value_payload"]))
            for row in rows
        ]
        return self._format_pairs(pairs, return_type)

    def prefix_keys(self, prefix: str, *, limit: int | None = None) -> list[str]:
        """Return only the keys matching *prefix* (no values loaded)."""
        now = self._now()
        upper = prefix[:-1] + chr(ord(prefix[-1]) + 1) if prefix else ""
        sql = (
            f"SELECT key_text FROM {self._table} "
            f"WHERE namespace = ? AND key_format = 'str' "
            f"AND key_text >= ? AND key_text < ? "
            f"AND (expires_at IS NULL OR expires_at > ?) "
            f"ORDER BY key_text ASC"
        )
        params: list[Any] = [self._namespace, prefix, upper, now]
        if limit is not None:
            sql += f" LIMIT {limit}"
        rows = Database.fetchall(sql, params)
        return [row["key_text"] for row in rows]

    def prefix_count(self, prefix: str) -> int:
        """Return count of keys matching *prefix*."""
        now = self._now()
        upper = prefix[:-1] + chr(ord(prefix[-1]) + 1) if prefix else ""
        count = Database.fetch_value(
            f"SELECT COUNT(*) AS cnt FROM {self._table} "
            f"WHERE namespace = ? AND key_format = 'str' "
            f"AND key_text >= ? AND key_text < ? "
            f"AND (expires_at IS NULL OR expires_at > ?)",
            [self._namespace, prefix, upper, now],
            column="cnt",
        )
        return int(count or 0)

    def prefix_delete(self, prefix: str) -> int:
        """Delete all keys matching *prefix*. Returns number of rows removed."""
        upper = prefix[:-1] + chr(ord(prefix[-1]) + 1) if prefix else ""
        cursor = Database.execute(
            f"DELETE FROM {self._table} "
            f"WHERE namespace = ? AND key_format = 'str' "
            f"AND key_text >= ? AND key_text < ?",
            [self._namespace, prefix, upper],
        )
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Return store statistics for monitoring and debugging.

        Returns a dict with::

            {
                "total_keys": 150,
                "expired_keys": 3,
                "key_format_counts": {"str": 140, "int": 10},
                "namespace": "myapp",
                "table": "kv_store",
                "serializer": "auto",
            }
        """
        now = self._now()

        total = Database.fetch_value(
            f"SELECT COUNT(*) AS cnt FROM {self._table} WHERE namespace = ?",
            [self._namespace],
            column="cnt",
        )

        expired = Database.fetch_value(
            f"SELECT COUNT(*) AS cnt FROM {self._table} "
            f"WHERE namespace = ? AND expires_at IS NOT NULL AND expires_at <= ?",
            [self._namespace, now],
            column="cnt",
        )

        format_rows = Database.fetchall(
            f"SELECT key_format, COUNT(*) AS cnt FROM {self._table} "
            f"WHERE namespace = ? GROUP BY key_format",
            [self._namespace],
        )
        format_counts = {row["key_format"]: row["cnt"] for row in format_rows}

        return {
            "total_keys": int(total or 0),
            "expired_keys": int(expired or 0),
            "active_keys": int(total or 0) - int(expired or 0),
            "key_format_counts": format_counts,
            "namespace": self._namespace,
            "table": self._table,
            "serializer": self._serializer_mode,
            "key_type": self._resolved_key_type.__name__ if self._resolved_key_type else None,
            "enforce_key_type": self._enforce,
        }

    async def astats(self) -> dict[str, Any]:
        """Async version of :meth:`stats`."""
        now = self._now()

        total = await Database.afetch_value(
            f"SELECT COUNT(*) AS cnt FROM {self._table} WHERE namespace = ?",
            [self._namespace],
            column="cnt",
        )

        expired = await Database.afetch_value(
            f"SELECT COUNT(*) AS cnt FROM {self._table} "
            f"WHERE namespace = ? AND expires_at IS NOT NULL AND expires_at <= ?",
            [self._namespace, now],
            column="cnt",
        )

        format_rows = await Database.afetchall(
            f"SELECT key_format, COUNT(*) AS cnt FROM {self._table} "
            f"WHERE namespace = ? GROUP BY key_format",
            [self._namespace],
        )
        format_counts = {row["key_format"]: row["cnt"] for row in format_rows}

        return {
            "total_keys": int(total or 0),
            "expired_keys": int(expired or 0),
            "active_keys": int(total or 0) - int(expired or 0),
            "key_format_counts": format_counts,
            "namespace": self._namespace,
            "table": self._table,
            "serializer": self._serializer_mode,
            "key_type": self._resolved_key_type.__name__ if self._resolved_key_type else None,
            "enforce_key_type": self._enforce,
        }

    # ------------------------------------------------------------------
    # Async variants
    # ------------------------------------------------------------------

    async def aget(self, key: Any, default: Any = None) -> Any:
        """Async version of :meth:`get`."""
        key = self._apply_namespace(key)
        encoded = self._encode_key(key)
        row = await self._afetch_row(encoded.lookup_key)
        if row is None or self._is_expired(row):
            if row is not None:
                cursor = await Database.aexecute(
                    f"DELETE FROM {self._table} WHERE lookup_key = ? AND namespace = ?",
                    [encoded.lookup_key, self._namespace],
                )
                await cursor.close()
            return default
        return self._decode_value(row["value_format"], row["value_payload"])

    async def aset(
        self,
        key: Any,
        value: Any,
        *,
        serializer: SerializerMode | None = None,
        ttl: float | int | None = None,
    ) -> None:
        """Async ``__setitem__`` with optional TTL."""
        key = self._apply_namespace(key)
        encoded_key = self._encode_key(key)
        encoded_value = self._encode_value(value, serializer)
        expires_at = (self._now() + ttl) if ttl is not None else None
        cursor = await Database.aexecute(_get_upsert_sql(self._table), [
            encoded_key.lookup_key,
            self._namespace,
            encoded_key.key_format,
            encoded_key.key_payload,
            encoded_key.key_int,
            encoded_key.key_real,
            encoded_key.key_text,
            encoded_key.key_blob,
            encoded_value.value_format,
            encoded_value.value_payload,
            expires_at,
        ])
        await cursor.close()

    async def adelete(self, key: Any) -> None:
        """Async ``__delitem__``."""
        key = self._apply_namespace(key)
        encoded = self._encode_key(key)
        cursor = await Database.aexecute(
            f"DELETE FROM {self._table} WHERE lookup_key = ? AND namespace = ?",
            [encoded.lookup_key, self._namespace],
        )
        try:
            if cursor.rowcount == 0:
                raise KeyError(self._strip_namespace(key))
        finally:
            await cursor.close()

    async def apop(self, key: Any, *args: Any) -> Any:
        """Async version of :meth:`pop`."""
        async with Database.transaction():
            try:
                value = await self.aget(key)
                if value is None and key not in self:
                    raise KeyError(key)
            except KeyError:
                if args:
                    return args[0]
                raise
            await self.adelete(key)
            return value

    async def apopitem(self, last: bool = True) -> tuple[Any, Any]:
        """Async version of :meth:`popitem`."""
        async with Database.transaction():
            rows = await self._aselect_rows(limit=1, reverse=last)
            if not rows:
                raise KeyError("store is empty")
            row = rows[0]
            key = self._strip_namespace(self._decode_key(row))
            value = self._decode_value(row["value_format"], row["value_payload"])
            await self.adelete(key)
            return key, value

    async def asetdefault(self, key: Any, default: Any = None) -> Any:
        """Async version of :meth:`setdefault`."""
        async with Database.transaction():
            key_ns = self._apply_namespace(key)
            encoded = self._encode_key(key_ns)
            row = await self._afetch_row(encoded.lookup_key)
            if row is not None and not self._is_expired(row):
                return self._decode_value(row["value_format"], row["value_payload"])
            await self.aset(key, default)
            return default

    async def aupdate(self, other: Mapping[Any, Any] | Iterable[tuple[Any, Any]] = (), /, **kwargs: Any) -> None:
        """Async version of :meth:`update`."""
        pairs: list[tuple[Any, Any]] = []
        if isinstance(other, Mapping):
            pairs.extend(other.items())
        else:
            pairs.extend(other)
        if kwargs:
            pairs.extend(kwargs.items())
        if pairs:
            await self.aset_many(pairs)

    async def aclear(self) -> None:
        """Async version of :meth:`clear`."""
        cursor = await Database.aexecute(
            f"DELETE FROM {self._table} WHERE namespace = ?", [self._namespace]
        )
        await cursor.close()

    async def akeys(self) -> list[Any]:
        """Async version of :meth:`keys`."""
        rows = await self._aselect_rows()
        return [self._strip_namespace(self._decode_key(row)) for row in rows]

    async def avalues(self) -> list[Any]:
        """Async version of :meth:`values`."""
        rows = await self._aselect_rows()
        return [
            self._decode_value(row["value_format"], row["value_payload"])
            for row in rows
        ]

    async def aitems(self) -> list[tuple[Any, Any]]:
        """Async version of :meth:`items`."""
        rows = await self._aselect_rows()
        return [
            (self._strip_namespace(self._decode_key(row)),
             self._decode_value(row["value_format"], row["value_payload"]))
            for row in rows
        ]

    async def aget_many(self, *keys: Any, **kwargs: Any) -> dict[Any, Any] | tuple[tuple[Any, Any], ...]:
        """Async version of :meth:`get_many`."""
        return_type = kwargs.get("return_type", "dict")
        default = kwargs.get("default", _MISSING)
        skip_missing = kwargs.get("skip_missing", False)

        if not keys:
            return {} if return_type == "dict" else ()

        encoded_pairs = [(k, self._encode_key(self._apply_namespace(k))) for k in keys]
        placeholders = ", ".join("?" for _ in encoded_pairs)
        rows_raw = await Database.afetchall(
            f"SELECT * FROM {self._table} "
            f"WHERE namespace = ? AND lookup_key IN ({placeholders})",
            [self._namespace, *[enc.lookup_key for _, enc in encoded_pairs]],
        )
        now = self._now()
        rows = {
            bytes(row["lookup_key"]): row
            for row in rows_raw
            if row["expires_at"] is None or row["expires_at"] > now
        }

        pairs: list[tuple[Any, Any]] = []
        for original_key, encoded in encoded_pairs:
            row = rows.get(encoded.lookup_key)
            if row is None:
                if skip_missing:
                    continue
                if default is _MISSING:
                    raise KeyError(original_key)
                pairs.append((original_key, default))
                continue
            pairs.append((
                original_key,
                self._decode_value(row["value_format"], row["value_payload"]),
            ))
        return self._format_pairs(pairs, return_type)

    async def aset_many(
        self,
        items: Mapping[Any, Any] | Iterable[tuple[Any, Any]],
        *,
        serializer: SerializerMode | None = None,
        ttl: float | int | None = None,
    ) -> None:
        """Async version of :meth:`set_many`."""
        if isinstance(items, Mapping):
            pairs = list(items.items())
        else:
            pairs = list(items)
        if not pairs:
            return

        expires_at = (self._now() + ttl) if ttl is not None else None
        sql = _get_upsert_sql(self._table)
        params_seq: list[list[Any]] = []
        for key, value in pairs:
            key = self._apply_namespace(key)
            ek = self._encode_key(key)
            ev = self._encode_value(value, serializer)
            params_seq.append([
                ek.lookup_key, self._namespace, ek.key_format, ek.key_payload,
                ek.key_int, ek.key_real, ek.key_text, ek.key_blob,
                ev.value_format, ev.value_payload, expires_at,
            ])
        cursor = await Database.aexecutemany(sql, params_seq)
        await cursor.close()

    async def adelete_many(self, keys: Sequence[Any] | Iterable[Any]) -> int:
        """Async version of :meth:`delete_many`."""
        keys_list = list(keys)
        if not keys_list:
            return 0
        encoded_keys = [self._encode_key(self._apply_namespace(k)).lookup_key for k in keys_list]
        placeholders = ", ".join("?" for _ in encoded_keys)
        cursor = await Database.aexecute(
            f"DELETE FROM {self._table} "
            f"WHERE namespace = ? AND lookup_key IN ({placeholders})",
            [self._namespace, *encoded_keys],
        )
        try:
            return cursor.rowcount
        finally:
            await cursor.close()

    async def alen(self) -> int:
        """Async version of :meth:`__len__`."""
        count = await Database.afetch_value(
            f"SELECT COUNT(*) AS cnt FROM {self._table} "
            f"WHERE namespace = ? AND (expires_at IS NULL OR expires_at > ?)",
            [self._namespace, self._now()],
            column="cnt",
        )
        return int(count or 0)

    async def acontains(self, key: Any) -> bool:
        """Async version of :meth:`__contains__`."""
        if isinstance(key, slice):
            return False
        try:
            key = self._apply_namespace(key)
            encoded = self._encode_key(key)
        except (TypeError, ValueError, pickle.PickleError):
            return False
        row = await Database.afetchone(
            f"SELECT expires_at FROM {self._table} "
            f"WHERE lookup_key = ? AND namespace = ? LIMIT 1",
            [encoded.lookup_key, self._namespace],
        )
        if row is None:
            return False
        if self._is_expired(row):
            cursor = await Database.aexecute(
                f"DELETE FROM {self._table} WHERE lookup_key = ? AND namespace = ?",
                [encoded.lookup_key, self._namespace],
            )
            await cursor.close()
            return False
        return True

    async def arange(
        self,
        start: Any = None,
        stop: Any = None,
        *,
        step: int | None = None,
        reverse: bool = False,
        return_type: MultiGetReturn = "dict",
    ) -> dict[Any, Any] | tuple[tuple[Any, Any], ...]:
        """Async version of :meth:`range`."""
        if step is not None:
            if step == 0:
                raise ValueError("step cannot be zero")
            if step < 0:
                raise ValueError("negative steps are not supported; use reverse=True")

        if not self._enforce:
            raise TypeError("range / slice queries require enforce_key_type=True")

        if self._resolved_key_type is None:
            count = await self.alen()
            if count == 0:
                return {} if return_type == "dict" else ()
            self._load_existing_key_type()

        if self._resolved_key_type is None or self._resolved_key_format is None:
            raise TypeError("range / slice queries require a resolved sortable key type")

        column = self._sortable_column_for_format(self._resolved_key_format)
        params: list[Any] = [self._namespace, self._resolved_key_format]
        where_clauses = [
            "namespace = ?",
            "key_format = ?",
            "(expires_at IS NULL OR expires_at > ?)",
        ]
        params.append(self._now())

        if start is not None:
            self._ensure_range_key_type(start)
            where_clauses.append(f"{column} >= ?")
            params.append(start)
        if stop is not None:
            self._ensure_range_key_type(stop)
            where_clauses.append(f"{column} < ?")
            params.append(stop)

        order = "DESC" if reverse else "ASC"
        sql = (
            f"SELECT * FROM {self._table} "
            f"WHERE {' AND '.join(where_clauses)} "
            f"ORDER BY {column} {order}"
        )
        rows = await Database.afetchall(sql, params)
        pairs = [
            (self._strip_namespace(self._decode_key(row)),
             self._decode_value(row["value_format"], row["value_payload"]))
            for row in rows
        ]
        if step not in (None, 1):
            pairs = pairs[::step]
        return self._format_pairs(pairs, return_type)

    async def akeys_slice(
        self,
        start: Any = None,
        stop: Any = None,
        *,
        step: int | None = None,
        reverse: bool = False,
    ) -> tuple[Any, ...]:
        """Async version of :meth:`keys_slice`."""
        pairs = await self.arange(start, stop, step=step, reverse=reverse, return_type="tuple")
        return tuple(k for k, _ in pairs)

    async def avalues_slice(
        self,
        start: Any = None,
        stop: Any = None,
        *,
        step: int | None = None,
        reverse: bool = False,
    ) -> tuple[Any, ...]:
        """Async version of :meth:`values_slice`."""
        pairs = await self.arange(start, stop, step=step, reverse=reverse, return_type="tuple")
        return tuple(v for _, v in pairs)

    async def aprefix(
        self,
        prefix: str,
        *,
        limit: int | None = None,
        reverse: bool = False,
        return_type: MultiGetReturn = "dict",
    ) -> dict[Any, Any] | tuple[tuple[Any, Any], ...]:
        """Async version of :meth:`prefix`."""
        if self._enforce and self._resolved_key_type is not None and self._resolved_key_type is not str:
            raise TypeError("prefix() requires string keys")

        now = self._now()
        order = "DESC" if reverse else "ASC"
        sql = (
            f"SELECT * FROM {self._table} "
            f"WHERE namespace = ? AND key_format = 'str' "
            f"AND key_text >= ? AND key_text < ? "
            f"AND (expires_at IS NULL OR expires_at > ?) "
            f"ORDER BY key_text {order}"
        )
        upper = prefix[:-1] + chr(ord(prefix[-1]) + 1) if prefix else ""
        params: list[Any] = [self._namespace, prefix, upper, now]

        if limit is not None:
            sql += f" LIMIT {limit}"

        rows = await Database.afetchall(sql, params)
        pairs = [
            (self._strip_namespace(self._decode_key(row)),
             self._decode_value(row["value_format"], row["value_payload"]))
            for row in rows
        ]
        return self._format_pairs(pairs, return_type)

    async def ascan(
        self,
        pattern: str = "*",
        *,
        limit: int | None = None,
        return_type: MultiGetReturn = "dict",
    ) -> dict[Any, Any] | tuple[tuple[Any, Any], ...]:
        """Async version of :meth:`scan`."""
        if self._enforce and self._resolved_key_type is not None and self._resolved_key_type is not str:
            raise TypeError("scan() requires string keys")

        now = self._now()
        sql = (
            f"SELECT * FROM {self._table} "
            f"WHERE namespace = ? AND key_format = 'str' "
            f"AND key_text GLOB ? "
            f"AND (expires_at IS NULL OR expires_at > ?) "
            f"ORDER BY key_text ASC"
        )
        params: list[Any] = [self._namespace, pattern, now]

        if limit is not None:
            sql += f" LIMIT {limit}"

        rows = await Database.afetchall(sql, params)
        pairs = [
            (self._strip_namespace(self._decode_key(row)),
             self._decode_value(row["value_format"], row["value_payload"]))
            for row in rows
        ]
        return self._format_pairs(pairs, return_type)

    async def aprefix_keys(self, prefix: str, *, limit: int | None = None) -> list[str]:
        """Async version of :meth:`prefix_keys`."""
        now = self._now()
        upper = prefix[:-1] + chr(ord(prefix[-1]) + 1) if prefix else ""
        sql = (
            f"SELECT key_text FROM {self._table} "
            f"WHERE namespace = ? AND key_format = 'str' "
            f"AND key_text >= ? AND key_text < ? "
            f"AND (expires_at IS NULL OR expires_at > ?) "
            f"ORDER BY key_text ASC"
        )
        params: list[Any] = [self._namespace, prefix, upper, now]
        if limit is not None:
            sql += f" LIMIT {limit}"
        rows = await Database.afetchall(sql, params)
        return [row["key_text"] for row in rows]

    async def aprefix_count(self, prefix: str) -> int:
        """Async version of :meth:`prefix_count`."""
        now = self._now()
        upper = prefix[:-1] + chr(ord(prefix[-1]) + 1) if prefix else ""
        count = await Database.afetch_value(
            f"SELECT COUNT(*) AS cnt FROM {self._table} "
            f"WHERE namespace = ? AND key_format = 'str' "
            f"AND key_text >= ? AND key_text < ? "
            f"AND (expires_at IS NULL OR expires_at > ?)",
            [self._namespace, prefix, upper, now],
            column="cnt",
        )
        return int(count or 0)

    async def aprefix_delete(self, prefix: str) -> int:
        """Async version of :meth:`prefix_delete`."""
        upper = prefix[:-1] + chr(ord(prefix[-1]) + 1) if prefix else ""
        cursor = await Database.aexecute(
            f"DELETE FROM {self._table} "
            f"WHERE namespace = ? AND key_format = 'str' "
            f"AND key_text >= ? AND key_text < ?",
            [self._namespace, prefix, upper],
        )
        try:
            return cursor.rowcount
        finally:
            await cursor.close()


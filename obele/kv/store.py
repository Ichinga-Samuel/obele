"""Single-table key-value storage built on top of :mod:`obele.orm.database`.

Provides :class:`KVStore`, a :class:`~collections.abc.MutableMapping` with
support for sorted iteration, slicing, range queries, multi-key lookups,
batch writes, and pluggable serialization (auto / JSON / pickle / custom).

The table uses dedicated sort columns (``key_int``, ``key_real``,
``key_text``, ``key_blob``) so that ordered queries are performed natively
by SQLite, while a ``lookup_key`` BLOB column guarantees exact key identity.

Usage::

    from obele import Database, KVStore

    Database.configure("myapp.sqlite3")
    store = KVStore("settings")

    store["theme"] = "dark"
    store["lang"]  = "en"
    print(store["theme"])             # "dark"
    print(store["a":"z"])             # dict of all keys in [a, z)

    # Multi-key
    store.get_many("theme", "lang")   # {"theme": "dark", "lang": "en"}
"""

from __future__ import annotations

import asyncio
import json
import math
import pickle
import re
from collections.abc import Hashable, Iterable, Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable, Literal

from ..orm.database import Database

_Dumps = Callable[[Any], bytes]
_Loads = Callable[[bytes], Any]

SerializerMode = Literal["auto", "json", "pickle"]
MultiGetReturn = Literal["dict", "tuple"]


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MISSING = object()

_SORTABLE_KEY_TYPES: dict[type[Any], str] = {int: "int", float: "float", str: "str",bytes: "bytes",}
_SORTABLE_KEY_FORMATS: dict[str, type[Any]] = {v: k for k, v in _SORTABLE_KEY_TYPES.items()}


def _validate_identifier(name: str) -> str:
    """Ensure *name* is a safe SQLite identifier."""
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(
            f"table_name must be a valid SQLite identifier (letters, digits, "
            f"underscores, starting with a letter or underscore), got {name!r}"
        )
    return name


def _ensure_hashable(key: Any) -> None:
    if not isinstance(key, Hashable):
        raise TypeError(f"keys must be hashable, got {type(key).__name__}")


def _json_safe_encode(value: Any) -> bytes:
    """Encode *value* as compact JSON, raising ``TypeError`` on failure."""
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise TypeError("JSON serialization failed for the provided value") from exc
    # Round-trip check: ensure JSON faithfully represents the value.
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


class KVStore(MutableMapping[Any, Any]):
    """A fast, single-table key-value store with a dict-like interface.

    Parameters
    ----------
    table_name:
        SQLite table name (must be a valid identifier).
    key_type:
        Optional Python type constraining keys - ``int``, ``float``,
        ``str``, or ``bytes``.  When *enforce_key_type* is ``True``,
        the store auto-detects the type from the first inserted key if
        *key_type* is ``None``.
    enforce_key_type:
        When ``True`` (default), all keys must share the same sortable
        type, enabling ordered iteration, slicing, and range queries.
        Set to ``False`` for mixed-type keys (sorting/range becomes
        undefined).
    serializer:
        ``"auto"`` (default) tries JSON first then falls back to pickle.
        ``"json"`` forces JSON-only.  ``"pickle"`` forces pickle-only.
        A ``(dumps, loads)`` callable pair can also be supplied for
        custom serialization.
    """

    def __init__(
        self,
        table_name: str = "kv_store",
        *,
        key_type: type[Any] | None = None,
        enforce_key_type: bool = True,
        serializer: SerializerMode | tuple[_Dumps, _Loads] = "auto",
    ) -> None:
        self._table = _validate_identifier(table_name)
        self._enforce = enforce_key_type
        self._declared_key_type = key_type
        self._resolved_key_type: type[Any] | None = key_type
        self._resolved_key_format: str | None = (
            _SORTABLE_KEY_TYPES.get(key_type) if key_type else None
        )

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

    # ------------------------------------------------------------------
    # Table management
    # ------------------------------------------------------------------

    def _ensure_table(self) -> None:
        """Create the backing table and partial indexes if they don't exist."""
        Database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._table} (
                lookup_key   BLOB PRIMARY KEY,
                key_format   TEXT NOT NULL,
                key_payload  BLOB NOT NULL,
                key_int      INTEGER,
                key_real     REAL,
                key_text     TEXT,
                key_blob     BLOB,
                value_format TEXT NOT NULL,
                value_payload BLOB NOT NULL
            ) WITHOUT ROWID
            """
        )
        for col in ("key_int", "key_real", "key_text", "key_blob"):
            Database.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{self._table}_{col}
                ON {self._table} ({col})
                WHERE {col} IS NOT NULL
                """
            )

    def _load_existing_key_type(self) -> None:
        """Detect and validate the key type of existing rows."""
        if not self._enforce:
            return
        formats = [
            row["key_format"]
            for row in Database.fetchall(
                f"SELECT DISTINCT key_format FROM {self._table} LIMIT 2"
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
        """Explicitly (re-)create the backing table.

        Normally called automatically by ``__init__``.  Use this when
        you need to recreate a table after :meth:`drop_table`.
        """
        self._ensure_table()

    async def acreate_table(self, if_not_exists: bool = True) -> None:
        """Async version of :meth:`create_table`."""
        await asyncio.to_thread(self.create_table, if_not_exists)

    def drop_table(self, if_exists: bool = True) -> None:
        """Drop the backing table."""
        maybe = "IF EXISTS " if if_exists else ""
        Database.execute(f"DROP TABLE {maybe}{self._table}")

    async def adrop_table(self, if_exists: bool = True) -> None:
        """Async version of :meth:`drop_table`."""
        await asyncio.to_thread(self.drop_table, if_exists)

    # ------------------------------------------------------------------
    # MutableMapping core interface
    # ------------------------------------------------------------------

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, slice):
            return self.range(key.start, key.stop, step=key.step)

        encoded = self._encode_key(key)
        row = self._fetch_row(encoded.lookup_key)
        if row is None:
            raise KeyError(key)
        return self._decode_value(row["value_format"], row["value_payload"])

    def __setitem__(self, key: Any, value: Any) -> None:
        self.set(key, value)

    def __delitem__(self, key: Any) -> None:
        self.delete(key)

    def __contains__(self, key: object) -> bool:
        if isinstance(key, slice):
            return False
        try:
            encoded = self._encode_key(key)
        except (TypeError, ValueError, pickle.PickleError):
            return False
        return (
            Database.fetchone(
                f"SELECT 1 FROM {self._table} WHERE lookup_key = ? LIMIT 1",
                [encoded.lookup_key],
            )
            is not None
        )

    def __len__(self) -> int:
        count = Database.fetch_value(
            f"SELECT COUNT(*) AS cnt FROM {self._table}",
            column="cnt",
        )
        return int(count or 0)

    def __bool__(self) -> bool:
        return len(self) > 0

    def __iter__(self) -> Iterator[Any]:
        for row in self._select_rows():
            yield self._decode_key(row)

    def __repr__(self) -> str:
        key_name = self._resolved_key_type.__name__ if self._resolved_key_type else "unset"
        return (
            f"<KVStore table={self._table!r} size={len(self)} "
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

    def set(self, key: Any, value: Any, *, serializer: SerializerMode | None = None) -> None:
        """Insert or replace a single key-value pair.

        An optional per-call *serializer* override can be supplied.
        """
        encoded_key = self._encode_key(key)
        encoded_value = self._encode_value(value, serializer)
        sql = f"""
            INSERT INTO {self._table} (
                lookup_key, key_format, key_payload,
                key_int, key_real, key_text, key_blob,
                value_format, value_payload
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(lookup_key) DO UPDATE SET
                key_format     = excluded.key_format,
                key_payload    = excluded.key_payload,
                key_int        = excluded.key_int,
                key_real       = excluded.key_real,
                key_text       = excluded.key_text,
                key_blob       = excluded.key_blob,
                value_format   = excluded.value_format,
                value_payload  = excluded.value_payload
        """
        Database.execute(sql, [
            encoded_key.lookup_key,
            encoded_key.key_format,
            encoded_key.key_payload,
            encoded_key.key_int,
            encoded_key.key_real,
            encoded_key.key_text,
            encoded_key.key_blob,
            encoded_value.value_format,
            encoded_value.value_payload,
        ])

    def pop(self, key: Any, *args: Any) -> Any:
        """Remove and return the value for *key*.

        If *key* is missing and a *default* is given, return it;
        otherwise raise ``KeyError``.
        """
        try:
            value = self[key]
        except KeyError:
            if args:
                return args[0]
            raise
        self.delete(key)
        return value

    def popitem(self, last: bool = True) -> tuple[Any, Any]:
        """Remove and return an arbitrary ``(key, value)`` pair.

        When *last* is ``True`` (default), removes the last key in sort
        order; when ``False``, the first.
        """
        rows = self._select_rows(limit=1, reverse=last)
        if not rows:
            raise KeyError("store is empty")
        row = rows[0]
        key = self._decode_key(row)
        value = self._decode_value(row["value_format"], row["value_payload"])
        self.delete(key)
        return key, value

    def setdefault(self, key: Any, default: Any = None) -> Any:
        """Return the value for *key*, inserting *default* first if missing."""
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
        Database.execute(f"DELETE FROM {self._table}")

    def keys(self) -> list[Any]:
        """Return all keys, in sort order."""
        return [self._decode_key(row) for row in self._select_rows()]

    def values(self) -> list[Any]:
        """Return all values, ordered by key."""
        return [
            self._decode_value(row["value_format"], row["value_payload"])
            for row in self._select_rows()
        ]

    def items(self) -> list[tuple[Any, Any]]:
        """Return all ``(key, value)`` pairs, ordered by key."""
        return [
            (self._decode_key(row),
             self._decode_value(row["value_format"], row["value_payload"]))
            for row in self._select_rows()
        ]

    def delete(self, key: Any) -> None:
        """Delete a single key. Raises ``KeyError`` if not present."""
        encoded = self._encode_key(key)
        cursor = Database.execute(
            f"DELETE FROM {self._table} WHERE lookup_key = ?",
            [encoded.lookup_key],
        )
        if cursor.rowcount == 0:
            raise KeyError(key)

    # ------------------------------------------------------------------
    # Batch operations
    # ------------------------------------------------------------------

    def set_many(
        self,
        items: Mapping[Any, Any] | Iterable[tuple[Any, Any]],
        *,
        serializer: SerializerMode | None = None,
    ) -> None:
        """Insert or replace many key-value pairs efficiently."""
        if isinstance(items, Mapping):
            pairs = list(items.items())
        else:
            pairs = list(items)
        if not pairs:
            return

        sql = f"""
            INSERT INTO {self._table} (
                lookup_key, key_format, key_payload,
                key_int, key_real, key_text, key_blob,
                value_format, value_payload
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(lookup_key) DO UPDATE SET
                key_format     = excluded.key_format,
                key_payload    = excluded.key_payload,
                key_int        = excluded.key_int,
                key_real       = excluded.key_real,
                key_text       = excluded.key_text,
                key_blob       = excluded.key_blob,
                value_format   = excluded.value_format,
                value_payload  = excluded.value_payload
        """
        params_seq: list[list[Any]] = []
        for key, value in pairs:
            ek = self._encode_key(key)
            ev = self._encode_value(value, serializer)
            params_seq.append([
                ek.lookup_key, ek.key_format, ek.key_payload,
                ek.key_int, ek.key_real, ek.key_text, ek.key_blob,
                ev.value_format, ev.value_payload,
            ])
        Database.executemany(sql, params_seq)

    def delete_many(self, keys: Sequence[Any] | Iterable[Any]) -> int:
        """Delete multiple keys at once.  Returns the number of rows removed."""
        keys_list = list(keys)
        if not keys_list:
            return 0
        encoded_keys = [self._encode_key(k).lookup_key for k in keys_list]
        placeholders = ", ".join("?" for _ in encoded_keys)
        cursor = Database.execute(
            f"DELETE FROM {self._table} WHERE lookup_key IN ({placeholders})",
            encoded_keys,
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
        """Fetch many keys at once.

        Parameters
        ----------
        *keys:
            Keys to look up.
        return_type:
            ``"dict"`` (default) returns a ``{key: value}`` dict;
            ``"tuple"`` returns a tuple of ``(key, value)`` pairs.
        default:
            Value to substitute for missing keys.  If omitted and a key
            is missing (and *skip_missing* is ``False``), ``KeyError``
            is raised.
        skip_missing:
            When ``True``, silently omit keys not found in the store.
        """
        if not keys:
            return {} if return_type == "dict" else ()

        encoded_pairs = [(k, self._encode_key(k)) for k in keys]
        placeholders = ", ".join("?" for _ in encoded_pairs)
        cursor = Database.execute_read(
            f"SELECT * FROM {self._table} WHERE lookup_key IN ({placeholders})",
            [enc.lookup_key for _, enc in encoded_pairs],
        )
        rows = {bytes(row["lookup_key"]): row for row in cursor.fetchall()}

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

        Parameters
        ----------
        start, stop:
            Inclusive lower / exclusive upper bounds.  ``None`` means
            unbounded.
        step:
            Select every *step*-th result (client-side).
        reverse:
            When ``True``, return items in descending key order.
        return_type:
            ``"dict"`` or ``"tuple"``.
        """
        if step is not None:
            if step == 0:
                raise ValueError("step cannot be zero")
            if step < 0:
                raise ValueError("negative steps are not supported; use reverse=True")

        if not self._enforce:
            raise TypeError(
                "range / slice queries require enforce_key_type=True with sortable keys"
            )

        # Auto-detect key type if store is non-empty but type hasn't been resolved yet.
        if self._resolved_key_type is None:
            if len(self) == 0:
                return {} if return_type == "dict" else ()
            self._load_existing_key_type()

        if self._resolved_key_type is None or self._resolved_key_format is None:
            raise TypeError("range / slice queries require a resolved sortable key type")

        column = self._sortable_column_for_format(self._resolved_key_format)
        params: list[Any] = [self._resolved_key_format]
        where_clauses = ["key_format = ?"]

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
            (self._decode_key(row),
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
        if self._enforce:
            return self._encode_sortable_key(key)
        return self._encode_flexible_key(key)

    def _encode_sortable_key(self, key: Any) -> _EncodedKey:
        key_type = type(key)
        key_format = _SORTABLE_KEY_TYPES.get(key_type)
        if key_format is None:
            raise TypeError(
                f"enforce_key_type only supports int, float, str, or bytes keys, "
                f"got {key_type.__name__}"
            )

        # Auto-lock to the first key type seen.
        if self._resolved_key_type is None:
            self._resolved_key_type = key_type
            self._resolved_key_format = key_format
        elif key_type is not self._resolved_key_type:
            raise TypeError(
                f"expected {self._resolved_key_type.__name__} keys, "
                f"got {key_type.__name__}"
            )

        return self._build_sortable_encoded_key(key, key_type, key_format)

    def _encode_flexible_key(self, key: Any) -> _EncodedKey:
        key_type = type(key)
        if key_type in _SORTABLE_KEY_TYPES:
            key_format = _SORTABLE_KEY_TYPES[key_type]
            return self._build_sortable_encoded_key(key, key_type, key_format)

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
            return _EncodedKey(
                lookup_key=b"int:" + payload,
                key_format="int",
                key_payload=payload,
                key_int=key,
            )
        if key_type is float:
            if not math.isfinite(key):
                raise ValueError("float keys must be finite")
            payload = repr(key).encode("ascii")
            return _EncodedKey(
                lookup_key=b"float:" + payload,
                key_format="float",
                key_payload=payload,
                key_real=key,
            )
        if key_type is str:
            payload = key.encode("utf-8")
            return _EncodedKey(
                lookup_key=b"str:" + payload,
                key_format="str",
                key_payload=payload,
                key_text=key,
            )
        # bytes
        payload = bytes(key)
        return _EncodedKey(
            lookup_key=b"bytes:" + payload,
            key_format="bytes",
            key_payload=payload,
            key_blob=payload,
        )

    # ------------------------------------------------------------------
    # Value encoding / decoding
    # ------------------------------------------------------------------

    def _encode_value(self, value: Any, serializer: SerializerMode | None = None) -> _EncodedValue:
        mode = serializer or self._serializer_mode

        # Custom callable serializer
        if mode == "custom":
            payload = self._custom_dumps(value)
            return _EncodedValue("custom", payload)

        # Auto: try JSON, fallback to pickle
        if mode in ("auto", "json"):
            try:
                payload = _json_safe_encode(value)
                return _EncodedValue("json", payload)
            except TypeError:
                if mode == "json":
                    raise

        # pickle fallback (or explicit "pickle")
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
        key_format = row["key_format"]
        if key_format == "int":
            return int(row["key_int"])
        if key_format == "float":
            return float(row["key_real"])
        if key_format == "str":
            return row["key_text"]
        if key_format == "bytes":
            return bytes(row["key_blob"])
        if key_format == "pickle":
            return pickle.loads(row["key_payload"])
        raise ValueError(f"unsupported key format {key_format!r}")

    # ------------------------------------------------------------------
    # Internal query helpers
    # ------------------------------------------------------------------

    def _fetch_row(self, lookup_key: bytes) -> Mapping[str, Any] | None:
        return Database.fetchone(
            f"SELECT * FROM {self._table} WHERE lookup_key = ?",
            [lookup_key],
        )

    def _select_rows(
        self,
        *,
        limit: int | None = None,
        reverse: bool = False,
    ) -> list[Mapping[str, Any]]:
        if self._enforce and self._resolved_key_format is not None:
            column = self._sortable_column_for_format(self._resolved_key_format)
            order = f"ORDER BY {column} {'DESC' if reverse else 'ASC'}"
            where = "WHERE key_format = ?"
            params: list[Any] = [self._resolved_key_format]
        else:
            order = f"ORDER BY lookup_key {'DESC' if reverse else 'ASC'}"
            where = ""
            params = []

        limit_sql = f" LIMIT {limit}" if limit is not None else ""
        sql = f"SELECT * FROM {self._table} {where} {order}{limit_sql}".strip()
        return Database.fetchall(sql, params)

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
    # Async variants
    # ------------------------------------------------------------------

    async def aget(self, key: Any, default: Any = None) -> Any:
        """Async version of :meth:`get`."""
        return await asyncio.to_thread(self.get, key, default)

    async def aset(self, key: Any, value: Any) -> None:
        """Async ``__setitem__``."""
        await asyncio.to_thread(self.__setitem__, key, value)

    async def adelete(self, key: Any) -> None:
        """Async ``__delitem__``."""
        await asyncio.to_thread(self.__delitem__, key)

    async def apop(self, key: Any, *args: Any) -> Any:
        """Async version of :meth:`pop`."""
        return await asyncio.to_thread(self.pop, key, *args)

    async def apopitem(self, last: bool = True) -> tuple[Any, Any]:
        """Async version of :meth:`popitem`."""
        return await asyncio.to_thread(self.popitem, last)

    async def asetdefault(self, key: Any, default: Any = None) -> Any:
        """Async version of :meth:`setdefault`."""
        return await asyncio.to_thread(self.setdefault, key, default)

    async def aupdate(self, other: Mapping[Any, Any] | Iterable[tuple[Any, Any]] = (), /, **kwargs: Any) -> None:
        """Async version of :meth:`update`."""
        # Collect into list first to avoid issues passing iterators across threads.
        pairs: list[tuple[Any, Any]] = []
        if isinstance(other, Mapping):
            pairs.extend(other.items())
        else:
            pairs.extend(other)
        if kwargs:
            pairs.extend(kwargs.items())
        if pairs:
            await asyncio.to_thread(self.set_many, pairs)

    async def aclear(self) -> None:
        """Async version of :meth:`clear`."""
        await asyncio.to_thread(self.clear)

    async def akeys(self) -> list[Any]:
        """Async version of :meth:`keys`."""
        return await asyncio.to_thread(self.keys)

    async def avalues(self) -> list[Any]:
        """Async version of :meth:`values`."""
        return await asyncio.to_thread(self.values)

    async def aitems(self) -> list[tuple[Any, Any]]:
        """Async version of :meth:`items`."""
        return await asyncio.to_thread(self.items)

    async def aget_many(self, *keys: Any, **kwargs: Any) -> dict[Any, Any] | tuple[tuple[Any, Any], ...]:
        """Async version of :meth:`get_many`."""
        return await asyncio.to_thread(lambda: self.get_many(*keys, **kwargs))

    async def aset_many(self, items: Mapping[Any, Any] | Iterable[tuple[Any, Any]]) -> None:
        """Async version of :meth:`set_many`."""
        await asyncio.to_thread(self.set_many, items)

    async def adelete_many(self, keys: Sequence[Any] | Iterable[Any]) -> int:
        """Async version of :meth:`delete_many`."""
        return await asyncio.to_thread(self.delete_many, keys)

    async def alen(self) -> int:
        """Async version of :meth:`__len__`."""
        return await asyncio.to_thread(len, self)

    async def acontains(self, key: Any) -> bool:
        """Async version of :meth:`__contains__`."""
        return await asyncio.to_thread(self.__contains__, key)


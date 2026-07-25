"""Single-table key-value storage built on `obele.orm.database`.

`KVStore` is a `MutableMapping` with sorted
iteration, slicing, range/prefix/pattern queries, multi-key lookups, batch
writes, TTL expiration, atomic operations, memoization, and pluggable
serialization.  Every method has an `a`-prefixed async twin that runs the
sync implementation on a worker thread:

    from obele import Database, KVStore

    Database.configure("myapp.sqlite3")
    store = KVStore("settings")

    store["theme"] = "dark"
    store.set("temp", "value", ttl=300)      # expires in 5 minutes
    store["a":"z"]                           # dict of keys in [a, z)
    store.get_many("theme", "lang")
    await store.aset("feature", True)
"""

from __future__ import annotations

import functools
import inspect
import json
import math
import pickle
import time
from collections.abc import Hashable, Iterable, Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Callable, Literal

from ..orm.database import Database, athread, awrite
from ..orm.sql import validate_identifier

_Dumps = Callable[[Any], bytes]
_Loads = Callable[[bytes], Any]

SerializerMode = Literal["auto", "json", "pickle"]
MultiGetReturn = Literal["dict", "tuple"]

_MISSING = object()

_SORTABLE_KEY_TYPES: dict[type[Any], str] = {int: "int", float: "float", str: "str", bytes: "bytes"}
_SORTABLE_KEY_FORMATS: dict[str, type[Any]] = {v: k for k, v in _SORTABLE_KEY_TYPES.items()}
_SORTABLE_COLUMNS: dict[str, str] = {"int": "key_int", "float": "key_real", "str": "key_text", "bytes": "key_blob"}

_NOT_EXPIRED = "(expires_at IS NULL OR expires_at > ?)"


def _ensure_hashable(key: Any) -> None:
	"""Raise `TypeError` unless `key` is hashable."""
	if not isinstance(key, Hashable):
		raise TypeError(f"keys must be hashable, got {type(key).__name__}")


def _json_safe_encode(value: Any) -> bytes:
	"""Encode `value` as compact JSON, raising `TypeError` unless lossless."""
	try:
		text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
	except (TypeError, ValueError) as exc:
		raise TypeError("JSON serialization failed for the provided value") from exc
	round_tripped = json.loads(text)
	if type(round_tripped) is not type(value) or round_tripped != value:
		raise TypeError("JSON serialization would not preserve the original value type")
	return text.encode("utf-8")


def _prefix_upper(prefix: str) -> str | None:
	"""Smallest string greater than every string starting with `prefix`.

	Returns `None` when the prefix is unbounded (empty or all U+10FFFF).
	"""
	for i in range(len(prefix) - 1, -1, -1):
		code = ord(prefix[i])
		if code < 0x10FFFF:
			code += 1
			if 0xD800 <= code <= 0xDFFF:  # skip the surrogate range
				code = 0xE000
			return prefix[:i] + chr(code)
	return None


@dataclass(frozen=True, slots=True)
class _EncodedKey:
	"""A key encoded for storage: lookup blob, format tag, payload, and sortable columns."""
	lookup_key: bytes
	key_format: str
	key_payload: bytes
	key_int: int | None = None
	key_real: float | None = None
	key_text: str | None = None
	key_blob: bytes | None = None


@dataclass(frozen=True, slots=True)
class _EncodedValue:
	"""A value encoded for storage: its format tag and serialized payload."""
	value_format: str
	value_payload: bytes


@functools.lru_cache(maxsize=64)
def _upsert_sql(table: str) -> str:
	"""Return the cached insert-or-replace SQL statement for `table`."""
	return f"""
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


class KVStore(MutableMapping[Any, Any]):
	"""A fast, single-table key-value store with a dict-like interface.

	Args:
	    table_name: SQLite table name (must be a valid identifier).
	    key_type: Optional type constraining keys - `int`, `float`,
	        `str`, or `bytes`.
	    enforce_key_type: When `True` (default), all keys must share one
	        sortable type, enabling ordered iteration and range queries.
	    serializer: `"auto"` (JSON first, pickle fallback - default),
	        `"json"`, `"pickle"`, or a `(dumps, loads)` callable pair.
	    namespace: Optional prefix isolating multiple logical stores in
	        one table.
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
		"""Initialize the store and create its backing table, resolving the key type and serializer."""
		self._table = validate_identifier(table_name, kind="table_name")
		self._enforce = enforce_key_type
		self._declared_key_type = key_type
		self._resolved_key_type: type[Any] | None = key_type
		self._resolved_key_format: str | None = _SORTABLE_KEY_TYPES.get(key_type) if key_type else None
		self._namespace = "" if namespace is None else str(namespace)

		if enforce_key_type and key_type is not None and key_type not in _SORTABLE_KEY_TYPES:
			raise TypeError(f"enforce_key_type only supports int, float, str, or bytes keys, got {key_type.__name__}")

		if isinstance(serializer, tuple):
			self._custom_dumps, self._custom_loads = serializer
			self._serializer_mode: str = "custom"
		else:
			if serializer not in ("auto", "json", "pickle"):
				raise ValueError(f"serializer must be 'auto', 'json', 'pickle', or a (dumps, loads) tuple, got {serializer!r}")
			self._custom_dumps = None
			self._custom_loads = None
			self._serializer_mode = serializer

		self._ensure_table()
		self._load_existing_key_type()

	@property
	def table_name(self) -> str:
		"""The SQLite table backing this store."""
		return self._table

	@property
	def key_type(self) -> type[Any] | None:
		"""Resolved key type (`None` until first use)."""
		return self._resolved_key_type

	@property
	def namespace(self) -> str:
		"""Logical namespace isolating this store's keys within the table."""
		return self._namespace

	def __repr__(self) -> str:
		"""Return a debug string showing the table, namespace, key type, and enforcement flag."""
		key_name = self._resolved_key_type.__name__ if self._resolved_key_type else "unset"
		return f"<{type(self).__name__} table={self._table!r} namespace={self._namespace!r} key_type={key_name} enforce={self._enforce}>"

	def _ensure_table(self) -> None:
		"""Create the backing table and partial indexes when missing."""
		with Database.transaction():
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
			Database.execute(f"CREATE INDEX IF NOT EXISTS idx_{self._table}_namespace ON {self._table} (namespace)")
			for col in _SORTABLE_COLUMNS.values():
				Database.execute(
					f"CREATE INDEX IF NOT EXISTS idx_{self._table}_namespace_{col} "
					f"ON {self._table} (namespace, {col}) WHERE {col} IS NOT NULL"
				)

	def _load_existing_key_type(self) -> None:
		"""Detect and validate the key type of existing rows."""
		if not self._enforce:
			return
		formats = [
			row["key_format"]
			for row in Database.fetchall(f"SELECT DISTINCT key_format FROM {self._table} WHERE namespace = ? LIMIT 2", [self._namespace])
		]
		if not formats:
			return
		if len(formats) > 1:
			raise ValueError(f"{self._table!r} contains mixed key types; set enforce_key_type=False to use it")
		existing_type = _SORTABLE_KEY_FORMATS.get(formats[0])
		if existing_type is None:
			raise ValueError(f"{self._table!r} contains non-sortable keys; set enforce_key_type=False to use it")
		if self._declared_key_type is not None and existing_type is not self._declared_key_type:
			raise ValueError(f"{self._table!r} already stores {existing_type.__name__} keys, not {self._declared_key_type.__name__}")
		self._resolved_key_type = existing_type
		self._resolved_key_format = formats[0]

	def create_table(self) -> None:
		"""Explicitly (re-)create the backing table."""
		self._ensure_table()

	async def acreate_table(self) -> None:
		"""Async version of `create_table`."""
		await awrite(self._ensure_table)

	def drop_table(self, if_exists: bool = True) -> None:
		"""Drop the backing table."""
		maybe = "IF EXISTS " if if_exists else ""
		Database.execute(f"DROP TABLE {maybe}{self._table}")

	async def adrop_table(self, if_exists: bool = True) -> None:
		"""Async version of `drop_table`."""
		await awrite(self.drop_table, if_exists)

	def _encode_key(self, key: Any) -> _EncodedKey:
		"""Encode `key` into an `_EncodedKey`, prefixing the lookup blob with the namespace when set."""
		_ensure_hashable(key)
		encoded = self._encode_sortable_key(key) if self._enforce else self._encode_flexible_key(key)
		if not self._namespace:
			return encoded
		prefixed = b"ns:" + self._namespace.encode("utf-8") + b"\0" + encoded.lookup_key
		return replace(encoded, lookup_key=prefixed)

	def _encode_sortable_key(self, key: Any) -> _EncodedKey:
		"""Encode `key` under type enforcement, resolving or validating the store's key type."""
		key_type = type(key)
		key_format = _SORTABLE_KEY_TYPES.get(key_type)
		if key_format is None:
			raise TypeError(f"enforce_key_type only supports int, float, str, or bytes keys, got {key_type.__name__}")
		if self._resolved_key_type is None:
			self._resolved_key_type = key_type
			self._resolved_key_format = key_format
		elif key_type is not self._resolved_key_type:
			raise TypeError(f"expected {self._resolved_key_type.__name__} keys, got {key_type.__name__}")
		return self._build_sortable_key(key, key_type)

	def _encode_flexible_key(self, key: Any) -> _EncodedKey:
		"""Encode `key` without enforcement, using sortable columns when possible and pickle otherwise."""
		if type(key) in _SORTABLE_KEY_TYPES:
			return self._build_sortable_key(key, type(key))
		try:
			payload = pickle.dumps(key, protocol=pickle.HIGHEST_PROTOCOL)
		except pickle.PickleError as exc:
			raise TypeError("key is not picklable") from exc
		return _EncodedKey(lookup_key=b"pickle:" + payload, key_format="pickle", key_payload=payload)

	@staticmethod
	def _build_sortable_key(key: Any, key_type: type) -> _EncodedKey:
		"""Build an `_EncodedKey` for a sortable `key`, filling the column matching `key_type`."""
		if key_type is int:
			payload = str(key).encode("ascii")
			return _EncodedKey(b"int:" + payload, "int", payload, key_int=key)
		if key_type is float:
			if not math.isfinite(key):
				raise ValueError("float keys must be finite")
			payload = repr(key).encode("ascii")
			return _EncodedKey(b"float:" + payload, "float", payload, key_real=key)
		if key_type is str:
			payload = key.encode("utf-8")
			return _EncodedKey(b"str:" + payload, "str", payload, key_text=key)
		payload = bytes(key)
		return _EncodedKey(b"bytes:" + payload, "bytes", payload, key_blob=payload)

	def _encode_value(self, value: Any, serializer: SerializerMode | None = None) -> _EncodedValue:
		"""Serialize `value` with the chosen serializer, falling back from JSON to pickle in `auto` mode."""
		mode = serializer or self._serializer_mode
		if mode == "custom":
			return _EncodedValue("custom", self._custom_dumps(value))
		if mode in ("auto", "json"):
			try:
				return _EncodedValue("json", _json_safe_encode(value))
			except TypeError:
				if mode == "json":
					raise
		return _EncodedValue("pickle", pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL))

	def _decode_value(self, value_format: str, payload: bytes) -> Any:
		"""Deserialize `payload` according to `value_format` (`json`, `pickle`, or `custom`)."""
		if value_format == "json":
			return json.loads(payload.decode("utf-8"))
		if value_format == "pickle":
			return pickle.loads(payload)
		if value_format == "custom":
			return self._custom_loads(payload)
		raise ValueError(f"unsupported value format {value_format!r}")

	@staticmethod
	def _decode_key(row: Mapping[str, Any]) -> Any:
		"""Reconstruct the original key from a database `row` using its stored key format."""
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

	def _decode_pair(self, row: Mapping[str, Any]) -> tuple[Any, Any]:
		"""Decode a database `row` into a `(key, value)` tuple."""
		return self._decode_key(row), self._decode_value(row["value_format"], row["value_payload"])

	@staticmethod
	def _format_pairs(pairs: Sequence[tuple[Any, Any]], return_type: MultiGetReturn) -> Any:
		"""Return `pairs` as a `dict` or a `tuple` according to `return_type`."""
		if return_type == "dict":
			return dict(pairs)
		if return_type == "tuple":
			return tuple(pairs)
		raise ValueError("return_type must be 'dict' or 'tuple'")

	@staticmethod
	def _now() -> float:
		"""Return the current time in seconds since the epoch."""
		return time.time()

	def _delete_by_lookup(self, lookup_key: bytes) -> int:
		"""Delete the row identified by `lookup_key` in this namespace, returning the count removed."""
		cursor = Database.execute(f"DELETE FROM {self._table} WHERE lookup_key = ? AND namespace = ?", [lookup_key, self._namespace])
		return cursor.rowcount

	def _live_row(self, key: Any) -> Mapping[str, Any] | None:
		"""Fetch the row for `key`, lazily deleting it when expired."""
		encoded = self._encode_key(key)
		row = Database.fetchone(
			f"SELECT * FROM {self._table} WHERE lookup_key = ? AND namespace = ?", [encoded.lookup_key, self._namespace]
		)
		if row is None:
			return None
		expires_at = row["expires_at"]
		if expires_at is not None and expires_at <= self._now():
			self._delete_by_lookup(encoded.lookup_key)
			return None
		return row

	def _select_rows(self, *, limit: int | None = None, reverse: bool = False) -> list[Mapping[str, Any]]:
		"""Fetch unexpired rows ordered by key, optionally limited and/or reversed."""
		if self._enforce and self._resolved_key_format is not None:
			column = _SORTABLE_COLUMNS[self._resolved_key_format]
			where = f"WHERE namespace = ? AND key_format = ? AND {_NOT_EXPIRED}"
			params: list[Any] = [self._namespace, self._resolved_key_format, self._now()]
		else:
			column = "lookup_key"
			where = f"WHERE namespace = ? AND {_NOT_EXPIRED}"
			params = [self._namespace, self._now()]
		limit_sql = f" LIMIT {limit}" if limit is not None else ""
		sql = f"SELECT * FROM {self._table} {where} ORDER BY {column} {'DESC' if reverse else 'ASC'}{limit_sql}"
		return Database.fetchall(sql, params)

	def __getitem__(self, key: Any) -> Any:
		"""Return the value for `key`, or a range dict when `key` is a slice; raises `KeyError` if missing."""
		if isinstance(key, slice):
			return self.range(key.start, key.stop, step=key.step)
		row = self._live_row(key)
		if row is None:
			raise KeyError(key)
		return self._decode_value(row["value_format"], row["value_payload"])

	def __setitem__(self, key: Any, value: Any) -> None:
		"""Store `value` under `key`."""
		self.set(key, value)

	def __delitem__(self, key: Any) -> None:
		"""Delete `key`, raising `KeyError` if it is absent."""
		self.delete(key)

	def __contains__(self, key: object) -> bool:
		"""Return whether `key` is present and unexpired; slices and unencodable keys yield `False`."""
		if isinstance(key, slice):
			return False
		try:
			return self._live_row(key) is not None
		except (TypeError, ValueError, pickle.PickleError):
			return False

	def __len__(self) -> int:
		"""Return the number of unexpired entries in this namespace."""
		count = Database.fetch_value(
			f"SELECT COUNT(*) AS cnt FROM {self._table} WHERE namespace = ? AND {_NOT_EXPIRED}",
			[self._namespace, self._now()],
			column="cnt",
		)
		return int(count or 0)

	def __bool__(self) -> bool:
		"""Return `True` when at least one unexpired entry exists."""
		row = Database.fetchone(
			f"SELECT 1 FROM {self._table} WHERE namespace = ? AND {_NOT_EXPIRED} LIMIT 1", [self._namespace, self._now()]
		)
		return row is not None

	def __iter__(self) -> Iterator[Any]:
		"""Iterate over keys in sorted order."""
		for row in self._select_rows():
			yield self._decode_key(row)

	async def __aiter__(self):
		"""Asynchronously iterate over keys in sorted order."""
		for key in await self.akeys():
			yield key

	def get(self, key: Any, default: Any = None) -> Any:
		"""Return the value for `key`, or `default` when missing/expired."""
		try:
			return self[key]
		except KeyError:
			return default

	def set(self, key: Any, value: Any, *, serializer: SerializerMode | None = None, ttl: float | int | None = None) -> None:
		"""Insert or replace one pair; `ttl` is seconds until expiry."""
		ek = self._encode_key(key)
		ev = self._encode_value(value, serializer)
		expires_at = (self._now() + ttl) if ttl is not None else None
		Database.execute(
			_upsert_sql(self._table),
			[
				ek.lookup_key,
				self._namespace,
				ek.key_format,
				ek.key_payload,
				ek.key_int,
				ek.key_real,
				ek.key_text,
				ek.key_blob,
				ev.value_format,
				ev.value_payload,
				expires_at,
			],
		)

	def delete(self, key: Any) -> None:
		"""Delete one key.  Raises `KeyError` if not present."""
		if self._delete_by_lookup(self._encode_key(key).lookup_key) == 0:
			raise KeyError(key)

	def pop(self, key: Any, *args: Any) -> Any:
		"""Remove and return the value for `key` (optional default)."""
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
		"""Remove and return the last (or first) `(key, value)` pair."""
		with Database.transaction():
			rows = self._select_rows(limit=1, reverse=last)
			if not rows:
				raise KeyError("store is empty")
			key, value = self._decode_pair(rows[0])
			self.delete(key)
			return key, value

	def setdefault(self, key: Any, default: Any = None) -> Any:
		"""Return the value for `key`, inserting `default` first if missing."""
		with Database.transaction():
			try:
				return self[key]
			except KeyError:
				self.set(key, default)
				return default

	def update(self, other: Mapping[Any, Any] | Iterable[tuple[Any, Any]] = (), /, **kwargs: Any) -> None:
		"""Bulk upsert from a mapping, iterable of pairs, and/or kwargs."""
		pairs = list(other.items() if isinstance(other, Mapping) else other)
		pairs.extend(kwargs.items())
		if pairs:
			self.set_many(pairs)

	def clear(self) -> None:
		"""Remove **all** pairs in this store's namespace."""
		Database.execute(f"DELETE FROM {self._table} WHERE namespace = ?", [self._namespace])

	def keys(self) -> list[Any]:
		"""All keys in sort order."""
		return [self._decode_key(row) for row in self._select_rows()]

	def values(self) -> list[Any]:
		"""All values, ordered by key."""
		return [self._decode_value(row["value_format"], row["value_payload"]) for row in self._select_rows()]

	def items(self) -> list[tuple[Any, Any]]:
		"""All `(key, value)` pairs, ordered by key."""
		return [self._decode_pair(row) for row in self._select_rows()]

	def ttl(self, key: Any) -> float | None:
		"""Seconds until expiration, or `None` for persistent keys."""
		row = self._live_row(key)
		if row is None:
			raise KeyError(key)
		expires_at = row["expires_at"]
		return None if expires_at is None else expires_at - self._now()

	def expire(self, key: Any, ttl: float | int) -> bool:
		"""Set a new TTL on an existing key.  Returns `True` if applied."""
		encoded = self._encode_key(key)
		now = self._now()
		cursor = Database.execute(
			f"UPDATE {self._table} SET expires_at = ? WHERE lookup_key = ? AND namespace = ? AND {_NOT_EXPIRED}",
			[now + ttl, encoded.lookup_key, self._namespace, now],
		)
		return cursor.rowcount > 0

	def persist(self, key: Any) -> bool:
		"""Remove the TTL from an existing key.  Returns `True` if applied."""
		encoded = self._encode_key(key)
		cursor = Database.execute(
			f"UPDATE {self._table} SET expires_at = NULL WHERE lookup_key = ? AND namespace = ? AND {_NOT_EXPIRED}",
			[encoded.lookup_key, self._namespace, self._now()],
		)
		return cursor.rowcount > 0

	def purge_expired(self) -> int:
		"""Delete all expired entries.  Returns the number removed."""
		cursor = Database.execute(
			f"DELETE FROM {self._table} WHERE namespace = ? AND expires_at IS NOT NULL AND expires_at <= ?", [self._namespace, self._now()]
		)
		return cursor.rowcount

	def increment(self, key: Any, delta: int | float = 1) -> int | float:
		"""Atomically add `delta` (default 1) to a numeric value.

		Missing keys start from zero.  Returns the new value.
		"""
		with Database.transaction():
			current = self.get(key, 0)
			if not isinstance(current, (int, float)) or isinstance(current, bool):
				raise TypeError(f"Cannot increment non-numeric value: {type(current).__name__}")
			new_value = current + delta
			self.set(key, new_value)
			return new_value

	def compare_and_swap(self, key: Any, expected: Any, new_value: Any, *, ttl: float | int | None = None) -> bool:
		"""Set `key` to `new_value` only if its current value equals `expected`."""
		with Database.transaction():
			try:
				current = self[key]
			except KeyError:
				return False
			if current != expected:
				return False
			self.set(key, new_value, ttl=ttl)
			return True

	def set_many(self, items: Mapping[Any, Any] | Iterable[tuple[Any, Any]], *, serializer: SerializerMode | None = None,
				 ttl: float | int | None = None,) -> None:
		"""Insert or replace many pairs in one statement."""
		pairs = list(items.items() if isinstance(items, Mapping) else items)
		if not pairs:
			return
		expires_at = (self._now() + ttl) if ttl is not None else None
		params_seq = []
		for key, value in pairs:
			ek = self._encode_key(key)
			ev = self._encode_value(value, serializer)
			params_seq.append(
				[
					ek.lookup_key,
					self._namespace,
					ek.key_format,
					ek.key_payload,
					ek.key_int,
					ek.key_real,
					ek.key_text,
					ek.key_blob,
					ev.value_format,
					ev.value_payload,
					expires_at,
				]
			)
		Database.executemany(_upsert_sql(self._table), params_seq)

	def delete_many(self, keys: Iterable[Any]) -> int:
		"""Delete multiple keys.  Returns the number of rows removed."""
		encoded = [self._encode_key(k).lookup_key for k in keys]
		if not encoded:
			return 0
		placeholders = ", ".join("?" for _ in encoded)
		cursor = Database.execute(
			f"DELETE FROM {self._table} WHERE namespace = ? AND lookup_key IN ({placeholders})", [self._namespace, *encoded]
		)
		return cursor.rowcount

	def get_many(
		self, *keys: Any, return_type: MultiGetReturn = "dict", default: Any = _MISSING, skip_missing: bool = False
	) -> dict[Any, Any] | tuple[tuple[Any, Any], ...]:
		"""Fetch many keys in one query.

		Missing keys raise `KeyError` unless `default` is given or
		`skip_missing` is true.
		"""
		if not keys:
			return {} if return_type == "dict" else ()
		encoded_pairs = [(k, self._encode_key(k)) for k in keys]
		placeholders = ", ".join("?" for _ in encoded_pairs)
		rows_raw = Database.fetchall(
			f"SELECT * FROM {self._table} WHERE namespace = ? AND lookup_key IN ({placeholders})",
			[self._namespace, *[enc.lookup_key for _, enc in encoded_pairs]],
		)
		now = self._now()
		rows = {bytes(row["lookup_key"]): row for row in rows_raw if row["expires_at"] is None or row["expires_at"] > now}
		pairs: list[tuple[Any, Any]] = []
		for original_key, encoded in encoded_pairs:
			row = rows.get(encoded.lookup_key)
			if row is None:
				if skip_missing:
					continue
				if default is _MISSING:
					raise KeyError(original_key)
				pairs.append((original_key, default))
			else:
				pairs.append((original_key, self._decode_value(row["value_format"], row["value_payload"])))
		return self._format_pairs(pairs, return_type)

	def range(
		self, start: Any = None, stop: Any = None, *, step: int | None = None, reverse: bool = False, return_type: MultiGetReturn = "dict"
	) -> dict[Any, Any] | tuple[tuple[Any, Any], ...]:
		"""Return items whose keys lie in `[start, stop)`.

		Requires `enforce_key_type=True` with a sortable key type.
		"""
		if step is not None:
			if step == 0:
				raise ValueError("step cannot be zero")
			if step < 0:
				raise ValueError("negative steps are not supported; use reverse=True")
		if not self._enforce:
			raise TypeError("range / slice queries require enforce_key_type=True")
		if self._resolved_key_type is None:
			self._load_existing_key_type()
			if self._resolved_key_type is None:
				return {} if return_type == "dict" else ()

		column = _SORTABLE_COLUMNS[self._resolved_key_format]
		where = [f"namespace = ?", "key_format = ?", _NOT_EXPIRED]
		params: list[Any] = [self._namespace, self._resolved_key_format, self._now()]
		for bound, op in ((start, ">="), (stop, "<")):
			if bound is not None:
				if type(bound) is not self._resolved_key_type:
					raise TypeError(f"range boundaries must be {self._resolved_key_type.__name__} values")
				where.append(f"{column} {op} ?")
				params.append(bound)

		rows = Database.fetchall(
			f"SELECT * FROM {self._table} WHERE {' AND '.join(where)} ORDER BY {column} {'DESC' if reverse else 'ASC'}", params
		)
		pairs = [self._decode_pair(row) for row in rows]
		if step not in (None, 1):
			pairs = pairs[::step]
		return self._format_pairs(pairs, return_type)

	def keys_slice(self, start: Any = None, stop: Any = None, *, step: int | None = None, reverse: bool = False) -> tuple:
		"""Keys-only variant of `range`."""
		return tuple(k for k, _ in self.range(start, stop, step=step, reverse=reverse, return_type="tuple"))

	def values_slice(self, start: Any = None, stop: Any = None, *, step: int | None = None, reverse: bool = False) -> tuple:
		"""Values-only variant of `range`."""
		return tuple(v for _, v in self.range(start, stop, step=step, reverse=reverse, return_type="tuple"))

	def _prefix_where(self, prefix: str) -> tuple[str, list[Any]]:
		"""Build the `WHERE` clause and parameters matching unexpired string keys under `prefix`."""
		if self._enforce and self._resolved_key_type is not None and self._resolved_key_type is not str:
			raise TypeError("prefix queries require string keys")
		where = f"namespace = ? AND key_format = 'str' AND key_text >= ? AND {_NOT_EXPIRED}"
		params: list[Any] = [self._namespace, prefix, self._now()]
		upper = _prefix_upper(prefix)
		if upper is not None:
			where += " AND key_text < ?"
			params.append(upper)
		return where, params

	def prefix(
		self, prefix: str, *, limit: int | None = None, reverse: bool = False, return_type: MultiGetReturn = "dict"
	) -> dict[Any, Any] | tuple[tuple[Any, Any], ...]:
		"""Return all entries whose string key starts with `prefix`:

		store.prefix("user:")  # {"user:1": ..., "user:2": ...}
		"""
		where, params = self._prefix_where(prefix)
		sql = f"SELECT * FROM {self._table} WHERE {where} ORDER BY key_text {'DESC' if reverse else 'ASC'}"
		if limit is not None:
			sql += f" LIMIT {int(limit)}"
		pairs = [self._decode_pair(row) for row in Database.fetchall(sql, params)]
		return self._format_pairs(pairs, return_type)

	def prefix_keys(self, prefix: str, *, limit: int | None = None) -> list[str]:
		"""Only the keys matching `prefix` (values not loaded)."""
		where, params = self._prefix_where(prefix)
		sql = f"SELECT key_text FROM {self._table} WHERE {where} ORDER BY key_text ASC"
		if limit is not None:
			sql += f" LIMIT {int(limit)}"
		return [row["key_text"] for row in Database.fetchall(sql, params)]

	def prefix_count(self, prefix: str) -> int:
		"""Count of keys matching `prefix`."""
		where, params = self._prefix_where(prefix)
		count = Database.fetch_value(f"SELECT COUNT(*) AS cnt FROM {self._table} WHERE {where}", params, column="cnt")
		return int(count or 0)

	def prefix_delete(self, prefix: str) -> int:
		"""Delete all keys matching `prefix`.  Returns the number removed."""
		if self._enforce and self._resolved_key_type is not None and self._resolved_key_type is not str:
			raise TypeError("prefix queries require string keys")
		where = "namespace = ? AND key_format = 'str' AND key_text >= ?"
		params: list[Any] = [self._namespace, prefix]
		upper = _prefix_upper(prefix)
		if upper is not None:
			where += " AND key_text < ?"
			params.append(upper)
		return Database.execute(f"DELETE FROM {self._table} WHERE {where}", params).rowcount

	def scan(
		self, pattern: str = "*", *, limit: int | None = None, return_type: MultiGetReturn = "dict"
	) -> dict[Any, Any] | tuple[tuple[Any, Any], ...]:
		"""Return entries whose string key matches a SQL GLOB `pattern`:

		store.scan("user:*")
		store.scan("cache:[0-9]*")
		"""
		if self._enforce and self._resolved_key_type is not None and self._resolved_key_type is not str:
			raise TypeError("scan() requires string keys")
		sql = (
			f"SELECT * FROM {self._table} "
			f"WHERE namespace = ? AND key_format = 'str' AND key_text GLOB ? AND {_NOT_EXPIRED} "
			f"ORDER BY key_text ASC"
		)
		params: list[Any] = [self._namespace, pattern, self._now()]
		if limit is not None:
			sql += f" LIMIT {int(limit)}"
		pairs = [self._decode_pair(row) for row in Database.fetchall(sql, params)]
		return self._format_pairs(pairs, return_type)

	def memoize(self, ttl: float | int | None = None, *, key_prefix: str | None = None) -> Callable:
		"""Decorator caching a function's results in this store (str keys).

		Works on both sync functions and coroutine functions:

		    cache = KVStore("cache", key_type=str)

		    @cache.memoize(ttl=300)
		    def expensive(a, b): ...

		    @cache.memoize(ttl=300)
		    async def aexpensive(a, b): ...
		"""

		def decorator(fn: Callable) -> Callable:
			"""Wrap `fn` to cache its results in the store, dispatching on sync versus coroutine functions."""
			prefix = key_prefix or f"memo:{fn.__module__}.{fn.__qualname__}"

			def cache_key(args: tuple, kwargs: dict) -> str:
				"""Build the cache-key string from the call's positional and keyword arguments."""
				return f"{prefix}:{args!r}:{sorted(kwargs.items())!r}"

			if inspect.iscoroutinefunction(fn):

				@functools.wraps(fn)
				async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
					"""Return the cached result, awaiting and caching `fn` on a miss."""
					key = cache_key(args, kwargs)
					cached = await self.aget(key, _MISSING)
					if cached is not _MISSING:
						return cached
					result = await fn(*args, **kwargs)
					await self.aset(key, result, ttl=ttl)
					return result

				return async_wrapper

			@functools.wraps(fn)
			def wrapper(*args: Any, **kwargs: Any) -> Any:
				"""Return the cached result, invoking and caching `fn` on a miss."""
				key = cache_key(args, kwargs)
				cached = self.get(key, _MISSING)
				if cached is not _MISSING:
					return cached
				result = fn(*args, **kwargs)
				self.set(key, result, ttl=ttl)
				return result

			return wrapper

		return decorator

	def stats(self) -> dict[str, Any]:
		"""Store statistics for monitoring and debugging."""
		now = self._now()
		total = int(
			Database.fetch_value(f"SELECT COUNT(*) AS cnt FROM {self._table} WHERE namespace = ?", [self._namespace], column="cnt") or 0
		)
		expired = int(
			Database.fetch_value(
				f"SELECT COUNT(*) AS cnt FROM {self._table} WHERE namespace = ? AND expires_at IS NOT NULL AND expires_at <= ?",
				[self._namespace, now],
				column="cnt",
			)
			or 0
		)
		format_rows = Database.fetchall(
			f"SELECT key_format, COUNT(*) AS cnt FROM {self._table} WHERE namespace = ? GROUP BY key_format", [self._namespace]
		)
		return {
			"total_keys": total,
			"expired_keys": expired,
			"active_keys": total - expired,
			"key_format_counts": {row["key_format"]: row["cnt"] for row in format_rows},
			"namespace": self._namespace,
			"table": self._table,
			"serializer": self._serializer_mode,
			"key_type": self._resolved_key_type.__name__ if self._resolved_key_type else None,
			"enforce_key_type": self._enforce,
		}

	async def aget(self, key: Any, default: Any = None) -> Any:
		"""Async version of `get`."""
		return await athread(self.get, key, default)

	async def aset(self, key: Any, value: Any, **kwargs: Any) -> None:
		"""Async version of `set`."""
		await awrite(functools.partial(self.set, key, value, **kwargs))

	async def adelete(self, key: Any) -> None:
		"""Async version of `delete`."""
		await awrite(self.delete, key)

	async def apop(self, key: Any, *args: Any) -> Any:
		"""Async version of `pop`."""
		return await awrite(self.pop, key, *args)

	async def apopitem(self, last: bool = True) -> tuple[Any, Any]:
		"""Async version of `popitem`."""
		return await awrite(self.popitem, last)

	async def asetdefault(self, key: Any, default: Any = None) -> Any:
		"""Async version of `setdefault`."""
		return await awrite(self.setdefault, key, default)

	async def aupdate(self, other: Mapping[Any, Any] | Iterable[tuple[Any, Any]] = (), /, **kwargs: Any) -> None:
		"""Async version of `update`."""
		await awrite(functools.partial(self.update, other, **kwargs))

	async def aclear(self) -> None:
		"""Async version of `clear`."""
		await awrite(self.clear)

	async def akeys(self) -> list[Any]:
		"""Async version of `keys`."""
		return await athread(self.keys)

	async def avalues(self) -> list[Any]:
		"""Async version of `values`."""
		return await athread(self.values)

	async def aitems(self) -> list[tuple[Any, Any]]:
		"""Async version of `items`."""
		return await athread(self.items)

	async def alen(self) -> int:
		"""Async version of `__len__`."""
		return await athread(self.__len__)

	async def acontains(self, key: Any) -> bool:
		"""Async version of `key in store`."""
		return await athread(self.__contains__, key)

	async def attl(self, key: Any) -> float | None:
		"""Async version of `ttl`."""
		return await athread(self.ttl, key)

	async def aexpire(self, key: Any, ttl: float | int) -> bool:
		"""Async version of `expire`."""
		return await awrite(self.expire, key, ttl)

	async def apersist(self, key: Any) -> bool:
		"""Async version of `persist`."""
		return await awrite(self.persist, key)

	async def apurge_expired(self) -> int:
		"""Async version of `purge_expired`."""
		return await awrite(self.purge_expired)

	async def aincrement(self, key: Any, delta: int | float = 1) -> int | float:
		"""Async version of `increment`."""
		return await awrite(self.increment, key, delta)

	async def acompare_and_swap(self, key: Any, expected: Any, new_value: Any, **kwargs: Any) -> bool:
		"""Async version of `compare_and_swap`."""
		return await awrite(functools.partial(self.compare_and_swap, key, expected, new_value, **kwargs))

	async def aset_many(self, items: Mapping[Any, Any] | Iterable[tuple[Any, Any]], **kwargs: Any) -> None:
		"""Async version of `set_many`."""
		await awrite(functools.partial(self.set_many, items, **kwargs))

	async def adelete_many(self, keys: Iterable[Any]) -> int:
		"""Async version of `delete_many`."""
		return await awrite(self.delete_many, keys)

	async def aget_many(self, *keys: Any, **kwargs: Any) -> dict[Any, Any] | tuple[tuple[Any, Any], ...]:
		"""Async version of `get_many`."""
		return await athread(functools.partial(self.get_many, *keys, **kwargs))

	async def arange(self, start: Any = None, stop: Any = None, **kwargs: Any) -> Any:
		"""Async version of `range`."""
		return await athread(functools.partial(self.range, start, stop, **kwargs))

	async def akeys_slice(self, start: Any = None, stop: Any = None, **kwargs: Any) -> tuple:
		"""Async version of `keys_slice`."""
		return await athread(functools.partial(self.keys_slice, start, stop, **kwargs))

	async def avalues_slice(self, start: Any = None, stop: Any = None, **kwargs: Any) -> tuple:
		"""Async version of `values_slice`."""
		return await athread(functools.partial(self.values_slice, start, stop, **kwargs))

	async def aprefix(self, prefix: str, **kwargs: Any) -> Any:
		"""Async version of `prefix`."""
		return await athread(functools.partial(self.prefix, prefix, **kwargs))

	async def aprefix_keys(self, prefix: str, **kwargs: Any) -> list[str]:
		"""Async version of `prefix_keys`."""
		return await athread(functools.partial(self.prefix_keys, prefix, **kwargs))

	async def aprefix_count(self, prefix: str) -> int:
		"""Async version of `prefix_count`."""
		return await athread(self.prefix_count, prefix)

	async def aprefix_delete(self, prefix: str) -> int:
		"""Async version of `prefix_delete`."""
		return await awrite(self.prefix_delete, prefix)

	async def ascan(self, pattern: str = "*", **kwargs: Any) -> Any:
		"""Async version of `scan`."""
		return await athread(functools.partial(self.scan, pattern, **kwargs))

	async def astats(self) -> dict[str, Any]:
		"""Async version of `stats`."""
		return await athread(self.stats)

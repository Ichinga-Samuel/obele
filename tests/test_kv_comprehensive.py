"""Comprehensive tests for KVStore: all features, edge cases, and boundary conditions."""

import math
import pickle
import time

import pytest

from obele import Database, KVStore
from obele.kv import KV


class TestKVCrud:
	"""Core CRUD operations: set, get, delete, contains, len, bool, iter."""

	@pytest.fixture(autouse=True)
	def setup(self):
		self.store = KVStore("test_crud", key_type=str)
		yield
		self.store.clear()

	def test_setitem_getitem(self):
		self.store["hello"] = "world"
		assert self.store["hello"] == "world"

	def test_set_method(self):
		self.store.set("k", 42)
		assert self.store["k"] == 42

	def test_overwrite_value(self):
		self.store["x"] = 1
		self.store["x"] = 2
		assert self.store["x"] == 2

	def test_getitem_missing_raises_keyerror(self):
		with pytest.raises(KeyError):
			_ = self.store["nonexistent"]

	def test_get_with_default(self):
		assert self.store.get("missing") is None
		assert self.store.get("missing", 42) == 42

	def test_get_returns_existing(self):
		self.store["k"] = "v"
		assert self.store.get("k", "default") == "v"

	def test_delitem(self):
		self.store["k"] = "v"
		del self.store["k"]
		assert "k" not in self.store

	def test_delete_method(self):
		self.store["k"] = "v"
		self.store.delete("k")
		assert "k" not in self.store

	def test_delete_missing_raises_keyerror(self):
		with pytest.raises(KeyError):
			self.store.delete("ghost")

	def test_delitem_missing_raises_keyerror(self):
		with pytest.raises(KeyError):
			del self.store["ghost"]

	def test_contains(self):
		self.store["exists"] = 1
		assert "exists" in self.store
		assert "nope" not in self.store

	def test_len(self):
		assert len(self.store) == 0
		self.store["a"] = 1
		self.store["b"] = 2
		assert len(self.store) == 2

	def test_bool_empty(self):
		assert not self.store

	def test_bool_nonempty(self):
		self.store["a"] = 1
		assert self.store

	def test_iter_returns_keys_sorted(self):
		self.store["c"] = 3
		self.store["a"] = 1
		self.store["b"] = 2
		assert list(self.store) == ["a", "b", "c"]

	def test_keys_sorted(self):
		self.store["z"] = 26
		self.store["a"] = 1
		assert self.store.keys() == ["a", "z"]

	def test_values_ordered_by_key(self):
		self.store["b"] = 2
		self.store["a"] = 1
		assert self.store.values() == [1, 2]

	def test_items_ordered_by_key(self):
		self.store["c"] = 3
		self.store["a"] = 1
		assert self.store.items() == [("a", 1), ("c", 3)]

	def test_clear(self):
		self.store["a"] = 1
		self.store["b"] = 2
		self.store.clear()
		assert len(self.store) == 0

	def test_store_complex_values(self):
		"""Auto serializer should handle dicts, lists, nested structures."""
		data = {"users": [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]}
		self.store["data"] = data
		assert self.store["data"] == data

	def test_store_none_value(self):
		self.store["k"] = None
		assert self.store["k"] is None

	def test_store_boolean_values(self):
		self.store["t"] = True
		self.store["f"] = False
		assert self.store["t"] is True
		assert self.store["f"] is False

	def test_repr(self):
		r = repr(self.store)
		assert "KVStore" in r
		assert "test_crud" in r


class TestKVDictInterface:
	"""Extended dict operations: pop, popitem, setdefault, update."""

	@pytest.fixture(autouse=True)
	def setup(self):
		self.store = KVStore("test_dict_iface", key_type=str)
		yield
		self.store.clear()

	def test_pop_existing(self):
		self.store["k"] = "v"
		val = self.store.pop("k")
		assert val == "v"
		assert "k" not in self.store

	def test_pop_missing_with_default(self):
		assert self.store.pop("missing", "fallback") == "fallback"

	def test_pop_missing_no_default_raises_keyerror(self):
		with pytest.raises(KeyError):
			self.store.pop("missing")

	def test_popitem_last_true(self):
		self.store["a"] = 1
		self.store["b"] = 2
		self.store["c"] = 3
		key, val = self.store.popitem(last=True)
		assert key == "c"
		assert val == 3
		assert len(self.store) == 2

	def test_popitem_last_false(self):
		self.store["a"] = 1
		self.store["b"] = 2
		key, val = self.store.popitem(last=False)
		assert key == "a"
		assert val == 1

	def test_popitem_empty_raises_keyerror(self):
		with pytest.raises(KeyError, match="store is empty"):
			self.store.popitem()

	def test_setdefault_missing_key(self):
		val = self.store.setdefault("k", 42)
		assert val == 42
		assert self.store["k"] == 42

	def test_setdefault_existing_key(self):
		self.store["k"] = "original"
		val = self.store.setdefault("k", "new")
		assert val == "original"
		assert self.store["k"] == "original"

	def test_setdefault_none_default(self):
		val = self.store.setdefault("k")
		assert val is None
		assert self.store["k"] is None

	def test_update_from_dict(self):
		self.store.update({"a": 1, "b": 2})
		assert self.store["a"] == 1
		assert self.store["b"] == 2

	def test_update_from_kwargs(self):
		self.store.update(x=10, y=20)
		assert self.store["x"] == 10
		assert self.store["y"] == 20

	def test_update_from_iterable(self):
		self.store.update([("p", 1), ("q", 2)])
		assert self.store["p"] == 1
		assert self.store["q"] == 2

	def test_update_combined_mapping_and_kwargs(self):
		self.store.update({"a": 1}, b=2)
		assert self.store["a"] == 1
		assert self.store["b"] == 2

	def test_update_overwrites_existing(self):
		self.store["k"] = "old"
		self.store.update({"k": "new"})
		assert self.store["k"] == "new"


class TestKVBatchOps:
	"""Batch operations: set_many, get_many, delete_many."""

	@pytest.fixture(autouse=True)
	def setup(self):
		self.store = KVStore("test_batch", key_type=str)
		yield
		self.store.clear()

	def test_set_many_from_dict(self):
		self.store.set_many({"a": 1, "b": 2, "c": 3})
		assert self.store["a"] == 1
		assert self.store["c"] == 3

	def test_set_many_from_iterable(self):
		self.store.set_many([("x", 10), ("y", 20)])
		assert self.store["x"] == 10
		assert self.store["y"] == 20

	def test_set_many_empty(self):
		self.store.set_many({})  # should not raise
		assert len(self.store) == 0

	def test_set_many_with_ttl(self):
		self.store.set_many({"t": "temp"}, ttl=3600)
		remaining = self.store.ttl("t")
		assert remaining is not None and remaining > 3500

	def test_get_many_dict_return(self):
		self.store.set_many({"a": 1, "b": 2, "c": 3})
		result = self.store.get_many("a", "c")
		assert result == {"a": 1, "c": 3}

	def test_get_many_tuple_return(self):
		self.store.set_many({"a": 1, "b": 2})
		result = self.store.get_many("a", "b", return_type="tuple")
		assert result == (("a", 1), ("b", 2))

	def test_get_many_missing_raises_keyerror(self):
		self.store["a"] = 1
		with pytest.raises(KeyError):
			self.store.get_many("a", "missing")

	def test_get_many_with_default(self):
		self.store["a"] = 1
		result = self.store.get_many("a", "missing", default="N/A")
		assert result == {"a": 1, "missing": "N/A"}

	def test_get_many_skip_missing(self):
		self.store["a"] = 1
		result = self.store.get_many("a", "missing", skip_missing=True)
		assert result == {"a": 1}
		assert "missing" not in result

	def test_get_many_empty_keys(self):
		assert self.store.get_many() == {}
		assert self.store.get_many(return_type="tuple") == ()

	def test_delete_many(self):
		self.store.set_many({"a": 1, "b": 2, "c": 3})
		count = self.store.delete_many(["a", "c"])
		assert count == 2
		assert len(self.store) == 1
		assert self.store["b"] == 2

	def test_delete_many_partial(self):
		self.store["a"] = 1
		count = self.store.delete_many(["a", "nonexistent"])
		assert count == 1

	def test_delete_many_empty_list(self):
		assert self.store.delete_many([]) == 0


class TestKVRange:
	"""Range and slice queries on sorted keys."""

	@pytest.fixture(autouse=True)
	def setup(self):
		self.store = KVStore("test_range", key_type=str)
		for ch in "abcdefghij":
			self.store[ch] = ord(ch)
		yield
		self.store.clear()

	def test_slice_syntax(self):
		result = self.store["c":"f"]
		assert list(result.keys()) == ["c", "d", "e"]

	def test_range_start_stop(self):
		result = self.store.range("b", "e")
		assert list(result.keys()) == ["b", "c", "d"]

	def test_range_no_start(self):
		result = self.store.range(stop="c")
		assert list(result.keys()) == ["a", "b"]

	def test_range_no_stop(self):
		result = self.store.range(start="i")
		assert list(result.keys()) == ["i", "j"]

	def test_range_open_both(self):
		result = self.store.range()
		assert len(result) == 10

	def test_range_reverse(self):
		result = self.store.range("a", "d", reverse=True)
		assert list(result.keys()) == ["c", "b", "a"]

	def test_range_with_step(self):
		result = self.store.range("a", "g", step=2, return_type="tuple")
		keys = [k for k, _ in result]
		assert keys == ["a", "c", "e"]

	def test_range_step_zero_raises_valueerror(self):
		with pytest.raises(ValueError, match="step cannot be zero"):
			self.store.range("a", "z", step=0)

	def test_range_negative_step_raises_valueerror(self):
		with pytest.raises(ValueError, match="negative steps"):
			self.store.range("a", "z", step=-1)

	def test_range_tuple_return_type(self):
		result = self.store.range("a", "c", return_type="tuple")
		assert isinstance(result, tuple)
		assert len(result) == 2

	def test_range_empty_result(self):
		result = self.store.range("x", "y")
		assert result == {}

	def test_keys_slice(self):
		keys = self.store.keys_slice("b", "e")
		assert keys == ("b", "c", "d")

	def test_values_slice(self):
		vals = self.store.values_slice("a", "c")
		assert vals == (ord("a"), ord("b"))

	def test_range_requires_enforce_key_type(self):
		nonenforced = KVStore("test_range_noenforce", enforce_key_type=False)
		nonenforced["a"] = 1
		with pytest.raises(TypeError, match="enforce_key_type=True"):
			nonenforced.range("a", "z")
		nonenforced.clear()


class TestKVTTL:
	"""Time-to-live: set with ttl, expire, persist, ttl query, purge."""

	@pytest.fixture(autouse=True)
	def setup(self):
		self.store = KVStore("test_ttl", key_type=str)
		yield
		self.store.clear()

	def test_set_with_ttl(self):
		self.store.set("temp", "val", ttl=3600)
		remaining = self.store.ttl("temp")
		assert remaining is not None
		assert 3500 < remaining <= 3600

	def test_ttl_persistent_key_returns_none(self):
		self.store["perm"] = "forever"
		assert self.store.ttl("perm") is None

	def test_ttl_missing_key_raises_keyerror(self):
		with pytest.raises(KeyError):
			self.store.ttl("ghost")

	def test_expired_key_invisible_on_read(self):
		self.store.set("ephemeral", "gone", ttl=0.001)
		time.sleep(0.05)
		with pytest.raises(KeyError):
			_ = self.store["ephemeral"]

	def test_expired_key_not_in_contains(self):
		self.store.set("temp", "val", ttl=0.001)
		time.sleep(0.05)
		assert "temp" not in self.store

	def test_expired_key_not_in_len(self):
		self.store["perm"] = "yes"
		self.store.set("temp", "no", ttl=0.001)
		time.sleep(0.05)
		assert len(self.store) == 1

	def test_expire_existing_key(self):
		self.store["k"] = "v"
		ok = self.store.expire("k", 3600)
		assert ok is True
		remaining = self.store.ttl("k")
		assert remaining is not None and remaining > 3500

	def test_expire_missing_key_returns_false(self):
		ok = self.store.expire("ghost", 100)
		assert ok is False

	def test_persist_removes_ttl(self):
		self.store.set("k", "v", ttl=3600)
		ok = self.store.persist("k")
		assert ok is True
		assert self.store.ttl("k") is None

	def test_persist_already_persistent(self):
		self.store["k"] = "v"
		ok = self.store.persist("k")
		# Persisting an already-persistent key should succeed
		assert ok is True

	def test_purge_expired(self):
		self.store.set("a", 1, ttl=0.001)
		self.store.set("b", 2, ttl=0.001)
		self.store["c"] = 3
		time.sleep(0.05)
		count = self.store.purge_expired()
		assert count == 2
		assert len(self.store) == 1
		assert self.store["c"] == 3

	def test_purge_expired_none(self):
		self.store["k"] = "v"
		assert self.store.purge_expired() == 0

	def test_ttl_on_expired_key_raises_keyerror(self):
		self.store.set("temp", "v", ttl=0.001)
		time.sleep(0.05)
		with pytest.raises(KeyError):
			self.store.ttl("temp")

	def test_set_many_with_ttl_applies_to_all(self):
		self.store.set_many({"a": 1, "b": 2}, ttl=3600)
		assert self.store.ttl("a") is not None
		assert self.store.ttl("b") is not None


class TestKVPrefix:
	"""Prefix-based queries on string-keyed stores."""

	@pytest.fixture(autouse=True)
	def setup(self):
		self.store = KVStore("test_prefix", key_type=str)
		self.store.set_many({"user:1": "Alice", "user:2": "Bob", "user:3": "Charlie", "post:1": "Hello", "post:2": "World"})
		yield
		self.store.clear()

	def test_prefix_returns_matching(self):
		result = self.store.prefix("user:")
		assert len(result) == 3
		assert "user:1" in result and "user:2" in result

	def test_prefix_excludes_nonmatching(self):
		result = self.store.prefix("user:")
		assert "post:1" not in result

	def test_prefix_with_limit(self):
		result = self.store.prefix("user:", limit=2)
		assert len(result) == 2

	def test_prefix_reverse(self):
		result = self.store.prefix("user:", return_type="tuple", reverse=True)
		keys = [k for k, _ in result]
		assert keys == ["user:3", "user:2", "user:1"]

	def test_prefix_keys(self):
		keys = self.store.prefix_keys("post:")
		assert keys == ["post:1", "post:2"]

	def test_prefix_keys_with_limit(self):
		keys = self.store.prefix_keys("user:", limit=1)
		assert len(keys) == 1

	def test_prefix_count(self):
		assert self.store.prefix_count("user:") == 3
		assert self.store.prefix_count("post:") == 2
		assert self.store.prefix_count("nonexistent:") == 0

	def test_prefix_delete(self):
		deleted = self.store.prefix_delete("user:")
		assert deleted == 3
		assert len(self.store) == 2

	def test_prefix_delete_returns_zero_for_no_match(self):
		assert self.store.prefix_delete("zzz:") == 0

	def test_prefix_tuple_return_type(self):
		result = self.store.prefix("post:", return_type="tuple")
		assert isinstance(result, tuple)
		assert len(result) == 2

	def test_prefix_no_match(self):
		result = self.store.prefix("order:")
		assert result == {}


class TestKVScan:
	"""GLOB-based pattern matching via scan()."""

	@pytest.fixture(autouse=True)
	def setup(self):
		self.store = KVStore("test_scan", key_type=str)
		self.store.set_many({"user:active:1": "A", "user:active:2": "B", "user:inactive:1": "C", "cache:temp": "D", "data:record": "E"})
		yield
		self.store.clear()

	def test_scan_wildcard_all(self):
		result = self.store.scan("*")
		assert len(result) == 5

	def test_scan_prefix_pattern(self):
		result = self.store.scan("user:active:*")
		assert len(result) == 2

	def test_scan_suffix_pattern(self):
		result = self.store.scan("*:1")
		assert len(result) == 2

	def test_scan_middle_wildcard(self):
		result = self.store.scan("user:*:1")
		assert len(result) == 2

	def test_scan_single_char_wildcard(self):
		result = self.store.scan("user:active:?")
		assert len(result) == 2

	def test_scan_with_limit(self):
		result = self.store.scan("*", limit=3)
		assert len(result) == 3

	def test_scan_no_match(self):
		result = self.store.scan("zzz:*")
		assert result == {}

	def test_scan_tuple_return_type(self):
		result = self.store.scan("cache:*", return_type="tuple")
		assert isinstance(result, tuple)
		assert len(result) == 1


class TestKVAtomicOps:
	"""increment and compare_and_swap."""

	@pytest.fixture(autouse=True)
	def setup(self):
		self.store = KVStore("test_atomic", key_type=str)
		yield
		self.store.clear()

	def test_increment_existing(self):
		self.store["counter"] = 10
		result = self.store.increment("counter", 5)
		assert result == 15
		assert self.store["counter"] == 15

	def test_increment_missing_creates_key(self):
		result = self.store.increment("new_counter")
		assert result == 1
		assert self.store["new_counter"] == 1

	def test_increment_default_delta(self):
		self.store["c"] = 0
		assert self.store.increment("c") == 1

	def test_increment_negative_delta(self):
		self.store["c"] = 10
		assert self.store.increment("c", -3) == 7

	def test_increment_float_delta(self):
		self.store["c"] = 1.5
		result = self.store.increment("c", 0.5)
		assert result == 2.0

	def test_increment_non_numeric_raises_typeerror(self):
		self.store["k"] = "text"
		with pytest.raises(TypeError, match="non-numeric"):
			self.store.increment("k")

	def test_compare_and_swap_success(self):
		self.store["v"] = 10
		assert self.store.compare_and_swap("v", 10, 20) is True
		assert self.store["v"] == 20

	def test_compare_and_swap_failure_wrong_expected(self):
		self.store["v"] = 10
		assert self.store.compare_and_swap("v", 99, 20) is False
		assert self.store["v"] == 10

	def test_compare_and_swap_missing_key(self):
		assert self.store.compare_and_swap("ghost", "any", "new") is False

	def test_compare_and_swap_with_ttl(self):
		self.store["v"] = "old"
		self.store.compare_and_swap("v", "old", "new", ttl=3600)
		assert self.store["v"] == "new"
		remaining = self.store.ttl("v")
		assert remaining is not None and remaining > 3500


class TestKVStats:
	"""The stats() method."""

	@pytest.fixture(autouse=True)
	def setup(self):
		self.store = KVStore("test_stats", key_type=str, serializer="auto")
		yield
		self.store.clear()

	def test_stats_empty(self):
		s = self.store.stats()
		assert s["total_keys"] == 0
		assert s["active_keys"] == 0
		assert s["expired_keys"] == 0

	def test_stats_with_data(self):
		self.store["a"] = 1
		self.store["b"] = 2
		s = self.store.stats()
		assert s["total_keys"] == 2
		assert s["active_keys"] == 2
		assert "str" in s["key_format_counts"]
		assert s["key_format_counts"]["str"] == 2

	def test_stats_with_expired_keys(self):
		self.store.set("temp", "v", ttl=0.001)
		self.store["perm"] = "v"
		time.sleep(0.05)
		s = self.store.stats()
		assert s["total_keys"] == 2
		assert s["expired_keys"] == 1
		assert s["active_keys"] == 1

	def test_stats_metadata(self):
		s = self.store.stats()
		assert s["namespace"] == ""
		assert s["table"] == "test_stats"
		assert s["serializer"] == "auto"
		assert s["enforce_key_type"] is True

	def test_stats_key_type(self):
		self.store["x"] = 1
		s = self.store.stats()
		assert s["key_type"] == "str"


class TestKVNamespace:
	"""Namespace isolation: separate logical stores in one table."""

	@pytest.fixture(autouse=True)
	def setup(self):
		self.ns1 = KVStore("test_ns_table", key_type=str, namespace="ns1")
		self.ns2 = KVStore("test_ns_table", key_type=str, namespace="ns2")
		self.no_ns = KVStore("test_ns_table", key_type=str)
		yield
		self.ns1.clear()
		self.ns2.clear()
		self.no_ns.clear()

	def test_namespaces_are_isolated(self):
		self.ns1["key"] = "from_ns1"
		self.ns2["key"] = "from_ns2"
		assert self.ns1["key"] == "from_ns1"
		assert self.ns2["key"] == "from_ns2"

	def test_namespace_len_isolation(self):
		self.ns1["a"] = 1
		self.ns1["b"] = 2
		self.ns2["c"] = 3
		assert len(self.ns1) == 2
		assert len(self.ns2) == 1

	def test_namespace_clear_isolation(self):
		self.ns1["a"] = 1
		self.ns2["b"] = 2
		self.ns1.clear()
		assert len(self.ns1) == 0
		assert len(self.ns2) == 1

	def test_namespace_delete_isolation(self):
		self.ns1["k"] = "v1"
		self.ns2["k"] = "v2"
		del self.ns1["k"]
		assert "k" not in self.ns1
		assert self.ns2["k"] == "v2"

	def test_namespace_contains_isolation(self):
		self.ns1["only_in_ns1"] = 1
		assert "only_in_ns1" in self.ns1
		assert "only_in_ns1" not in self.ns2

	def test_namespace_iter_isolation(self):
		self.ns1["a"] = 1
		self.ns1["b"] = 2
		self.ns2["c"] = 3
		assert list(self.ns1) == ["a", "b"]
		assert list(self.ns2) == ["c"]

	def test_no_namespace_is_independent(self):
		self.no_ns["x"] = 99
		self.ns1["x"] = 11
		assert self.no_ns["x"] == 99
		assert self.ns1["x"] == 11

	def test_namespace_property(self):
		assert self.ns1.namespace == "ns1"
		assert self.ns2.namespace == "ns2"
		assert self.no_ns.namespace == ""

	def test_namespace_stats_isolation(self):
		self.ns1["a"] = 1
		self.ns2["b"] = 2
		self.ns2["c"] = 3
		s1 = self.ns1.stats()
		s2 = self.ns2.stats()
		assert s1["total_keys"] == 1
		assert s2["total_keys"] == 2
		assert s1["namespace"] == "ns1"


class TestKVKeyTypes:
	"""Key type handling: int, float, str, bytes, enforcement."""

	@pytest.fixture(autouse=True)
	def setup(self):
		yield

	def test_int_keys(self):
		store = KVStore("test_kt_int", key_type=int)
		store[1] = "one"
		store[2] = "two"
		assert store[1] == "one"
		assert store.keys() == [1, 2]
		store.clear()

	def test_float_keys(self):
		store = KVStore("test_kt_float", key_type=float)
		store[1.5] = "a"
		store[2.5] = "b"
		assert store[1.5] == "a"
		assert store.keys() == [1.5, 2.5]
		store.clear()

	def test_bytes_keys(self):
		store = KVStore("test_kt_bytes", key_type=bytes)
		store[b"key1"] = "val1"
		store[b"key2"] = "val2"
		assert store[b"key1"] == "val1"
		store.clear()

	def test_str_keys(self):
		store = KVStore("test_kt_str", key_type=str)
		store["hello"] = "world"
		assert store["hello"] == "world"
		store.clear()

	def test_enforce_key_type_rejects_wrong_type(self):
		store = KVStore("test_kt_enforce", key_type=str)
		with pytest.raises(TypeError):
			store[123] = "wrong"
		store.clear()

	def test_enforce_key_type_auto_locks(self):
		"""First key locks the type when key_type=None but enforce=True."""
		store = KVStore("test_kt_autolock")
		store["text_key"] = "val"
		with pytest.raises(TypeError):
			store[123] = "different_type"
		store.clear()

	def test_enforce_key_type_false_allows_mixed(self):
		store = KVStore("test_kt_mixed", enforce_key_type=False)
		store["text"] = 1
		store[42] = 2
		store[3.14] = 3
		assert store["text"] == 1
		assert store[42] == 2
		assert store[3.14] == 3
		store.clear()

	def test_float_nan_raises_valueerror(self):
		store = KVStore("test_kt_nan", key_type=float)
		with pytest.raises(ValueError, match="finite"):
			store[float("nan")] = "bad"
		store.clear()

	def test_float_inf_raises_valueerror(self):
		store = KVStore("test_kt_inf", key_type=float)
		with pytest.raises(ValueError, match="finite"):
			store[float("inf")] = "bad"
		store.clear()

	def test_float_neg_inf_raises_valueerror(self):
		store = KVStore("test_kt_neginf", key_type=float)
		with pytest.raises(ValueError, match="finite"):
			store[float("-inf")] = "bad"
		store.clear()

	def test_int_key_range(self):
		store = KVStore("test_kt_intrange", key_type=int)
		for i in range(5):
			store[i] = i * 10
		result = store.range(1, 4)
		assert list(result.keys()) == [1, 2, 3]
		store.clear()

	def test_invalid_key_type_raises_typeerror(self):
		with pytest.raises(TypeError):
			KVStore("test_kt_badtype", key_type=list)

	def test_unhashable_key_raises_typeerror(self):
		store = KVStore("test_kt_unhash", enforce_key_type=False)
		with pytest.raises(TypeError, match="hashable"):
			store[[1, 2, 3]] = "val"
		store.clear()


class TestKVSerializers:
	"""Serializer modes: auto, json, pickle, custom."""

	@pytest.fixture(autouse=True)
	def setup(self):
		yield

	def test_auto_serializer_json_for_simple(self):
		store = KVStore("test_ser_auto", key_type=str, serializer="auto")
		store["k"] = {"a": 1}
		assert store["k"] == {"a": 1}
		store.clear()

	def test_auto_serializer_pickle_fallback(self):
		"""Tuples can't round-trip JSON (become lists), so auto falls back to pickle."""
		store = KVStore("test_ser_auto_fb", key_type=str, serializer="auto")
		val = (1, 2, 3)
		store["k"] = val
		assert store["k"] == val
		assert isinstance(store["k"], tuple)
		store.clear()

	def test_auto_serializer_handles_set(self):
		store = KVStore("test_ser_auto_set", key_type=str, serializer="auto")
		val = {1, 2, 3}
		store["k"] = val
		assert store["k"] == val
		store.clear()

	def test_json_serializer_rejects_non_json(self):
		store = KVStore("test_ser_json", key_type=str, serializer="json")
		with pytest.raises(TypeError):
			store["k"] = {1, 2, 3}  # sets are not JSON serializable
		store.clear()

	def test_json_serializer_tuples_raise(self):
		"""JSON serializer should reject tuples because they don't round-trip."""
		store = KVStore("test_ser_json_tup", key_type=str, serializer="json")
		with pytest.raises(TypeError):
			store["k"] = (1, 2, 3)
		store.clear()

	def test_json_serializer_basic_types(self):
		store = KVStore("test_ser_json_ok", key_type=str, serializer="json")
		store["str"] = "hello"
		store["int"] = 42
		store["float"] = 3.14
		store["list"] = [1, 2, 3]
		store["dict"] = {"a": 1}
		store["bool"] = True
		store["null"] = None
		assert store["str"] == "hello"
		assert store["int"] == 42
		assert store["list"] == [1, 2, 3]
		assert store["null"] is None
		store.clear()

	def test_pickle_serializer(self):
		store = KVStore("test_ser_pickle", key_type=str, serializer="pickle")
		val = {"tuple": (1, 2), "set": {3, 4}}
		store["k"] = val
		assert store["k"] == val
		store.clear()

	def test_custom_serializer(self):
		# Custom serializer that just uppercases/lowercases strings
		def dumps(v):
			return v.upper().encode("utf-8")

		def loads(b):
			return b.decode("utf-8").lower()

		store = KVStore("test_ser_custom", key_type=str, serializer=(dumps, loads))
		store["k"] = "Hello"
		assert store["k"] == "hello"
		store.clear()

	def test_invalid_serializer_raises_valueerror(self):
		with pytest.raises(ValueError, match="serializer must be"):
			KVStore("test_ser_bad", serializer="xml")

	def test_per_call_serializer_override(self):
		store = KVStore("test_ser_override", key_type=str, serializer="auto")
		store.set("k", [1, 2, 3], serializer="pickle")
		assert store["k"] == [1, 2, 3]
		store.clear()


class TestKVEdgeCases:
	"""Boundary conditions and unusual inputs."""

	@pytest.fixture(autouse=True)
	def setup(self):
		self.store = KVStore("test_edge", key_type=str)
		yield
		self.store.clear()

	def test_empty_string_key(self):
		self.store[""] = "empty"
		assert self.store[""] == "empty"

	def test_very_long_key(self):
		long_key = "k" * 10000
		self.store[long_key] = "long"
		assert self.store[long_key] == "long"

	def test_unicode_key(self):
		self.store["日本語"] = "Japanese"
		assert self.store["日本語"] == "Japanese"

	def test_unicode_value(self):
		self.store["k"] = "日本語の値"
		assert self.store["k"] == "日本語の値"

	def test_contains_swallows_encoding_errors(self):
		"""__contains__ with a bad type returns False instead of raising."""
		# slice returns False
		assert (slice(1, 2) in self.store) is False

	def test_large_int_value(self):
		self.store["big"] = 10**100
		assert self.store["big"] == 10**100

	def test_nested_data_structures(self):
		data = {"list": [1, [2, [3]]], "dict": {"a": {"b": {"c": 1}}}}
		self.store["nested"] = data
		assert self.store["nested"] == data

	def test_overwrite_preserves_count(self):
		self.store["k"] = 1
		self.store["k"] = 2
		assert len(self.store) == 1

	def test_getitem_slice_delegates_to_range(self):
		self.store["a"] = 1
		self.store["b"] = 2
		self.store["c"] = 3
		result = self.store["a":"c"]
		assert isinstance(result, dict)
		assert list(result.keys()) == ["a", "b"]

	def test_multiple_clears(self):
		self.store["a"] = 1
		self.store.clear()
		self.store.clear()  # second clear on empty should be fine
		assert len(self.store) == 0

	def test_set_get_delete_cycle(self):
		for i in range(50):
			key = f"key_{i}"
			self.store[key] = i
		assert len(self.store) == 50
		for i in range(50):
			del self.store[f"key_{i}"]
		assert len(self.store) == 0

	def test_popitem_drains_store(self):
		self.store["a"] = 1
		self.store["b"] = 2
		self.store.popitem()
		self.store.popitem()
		with pytest.raises(KeyError):
			self.store.popitem()

	def test_pop_with_none_default(self):
		result = self.store.pop("missing", None)
		assert result is None

	def test_float_key_boundary_values(self):
		store = KVStore("test_edge_float", key_type=float)
		store[0.0] = "zero"
		store[-0.0] = "neg_zero"  # -0.0 == 0.0 so this overwrites
		store[1e-300] = "tiny"
		store[1e300] = "huge"
		assert store[1e-300] == "tiny"
		assert store[1e300] == "huge"
		store.clear()

	def test_bytes_value_with_auto_serializer(self):
		"""bytes values should round-trip with auto (pickle fallback)."""
		self.store["k"] = b"\x00\x01\x02"
		assert self.store["k"] == b"\x00\x01\x02"

	def test_empty_dict_value(self):
		self.store["k"] = {}
		assert self.store["k"] == {}

	def test_empty_list_value(self):
		self.store["k"] = []
		assert self.store["k"] == []


class TestKVSingleton:
	"""Global KV singleton behavior."""

	@pytest.fixture(autouse=True)
	def setup(self):
		KV.reset()
		yield
		KV.reset()

	def test_singleton_same_instance(self):
		kv1 = KV("test_singleton_tbl", key_type=str)
		kv2 = KV("ignored_name")
		assert kv1 is kv2

	def test_singleton_preserves_data(self):
		kv1 = KV("test_singleton_data", key_type=str)
		kv1["k"] = "v"
		kv2 = KV()
		assert kv2["k"] == "v"
		kv1.clear()

	def test_reset_allows_new_instance(self):
		kv1 = KV("test_singleton_reset1", key_type=str)
		id1 = id(kv1)
		kv1.clear()
		KV.reset()
		kv2 = KV("test_singleton_reset2", key_type=str)
		assert id(kv2) != id1

	def test_singleton_repr(self):
		kv = KV("test_singleton_repr", key_type=str)
		r = repr(kv)
		assert "KV" in r
		assert "test_singleton_repr" in r
		kv.clear()

	def test_singleton_is_kvstore_subclass(self):
		kv = KV("test_singleton_sub", key_type=str)
		assert isinstance(kv, KVStore)
		kv.clear()


class TestKVTableManagement:
	"""Table creation, dropping, and name validation."""

	@pytest.fixture(autouse=True)
	def setup(self):
		yield

	def test_valid_table_names(self):
		for name in ["kv", "_private", "my_table_123", "A", "_"]:
			store = KVStore(name)
			assert store.table_name == name
			store.clear()

	def test_invalid_table_name_empty(self):
		with pytest.raises(ValueError):
			KVStore("")

	def test_invalid_table_name_starts_with_digit(self):
		with pytest.raises(ValueError):
			KVStore("123bad")

	def test_invalid_table_name_special_chars(self):
		with pytest.raises(ValueError):
			KVStore("my-table")

	def test_invalid_table_name_spaces(self):
		with pytest.raises(ValueError):
			KVStore("my table")

	def test_invalid_table_name_sql_injection(self):
		with pytest.raises(ValueError):
			KVStore("table; DROP TABLE users")

	def test_drop_table(self):
		store = KVStore("test_droppable")
		store["k"] = "v"
		store.drop_table()
		# After drop, creating a new store on same table should work fresh
		store2 = KVStore("test_droppable")
		assert len(store2) == 0
		store2.clear()

	def test_drop_table_if_exists(self):
		store = KVStore("test_drop_ifexists")
		store.drop_table(if_exists=True)
		# Dropping again with if_exists should not raise
		store2 = KVStore("test_drop_ifexists")
		store2.drop_table(if_exists=True)

	def test_create_table_explicit(self):
		store = KVStore("test_explicit_create")
		store.create_table()  # idempotent
		store["k"] = "v"
		assert store["k"] == "v"
		store.clear()

	def test_table_name_property(self):
		store = KVStore("my_store")
		assert store.table_name == "my_store"
		store.clear()

	def test_key_type_property(self):
		store = KVStore("test_kt_prop", key_type=int)
		assert store.key_type is int
		store.clear()

	def test_key_type_property_none_before_use(self):
		store = KVStore("test_kt_prop_none")
		assert store.key_type is None
		store.clear()

	def test_default_table_name(self):
		store = KVStore()
		assert store.table_name == "kv_store"
		store.clear()


class TestKVAsync:
	"""Async method smoke tests."""

	@pytest.fixture(autouse=True)
	def setup(self):
		self.store = KVStore("test_async", key_type=str)
		yield
		self.store.clear()

	async def test_aset_aget(self):
		await self.store.aset("k", "v")
		assert await self.store.aget("k") == "v"

	async def test_adelete(self):
		await self.store.aset("k", "v")
		await self.store.adelete("k")
		assert await self.store.aget("k") is None

	async def test_akeys_avalues_aitems(self):
		await self.store.aset("a", 1)
		await self.store.aset("b", 2)
		assert await self.store.akeys() == ["a", "b"]
		assert await self.store.avalues() == [1, 2]
		items = await self.store.aitems()
		assert items == [("a", 1), ("b", 2)]

	async def test_alen(self):
		await self.store.aset("a", 1)
		assert await self.store.alen() == 1

	async def test_aclear(self):
		await self.store.aset("a", 1)
		await self.store.aclear()
		assert await self.store.alen() == 0

	async def test_aincrement(self):
		await self.store.aset("c", 10)
		result = await self.store.aincrement("c", 5)
		assert result == 15

	async def test_acompare_and_swap(self):
		await self.store.aset("v", "old")
		ok = await self.store.acompare_and_swap("v", "old", "new")
		assert ok is True
		assert await self.store.aget("v") == "new"

	async def test_aset_many_aget_many(self):
		await self.store.aset_many({"a": 1, "b": 2})
		result = await self.store.aget_many("a", "b")
		assert result == {"a": 1, "b": 2}

	async def test_adelete_many(self):
		await self.store.aset_many({"a": 1, "b": 2, "c": 3})
		count = await self.store.adelete_many(["a", "c"])
		assert count == 2

	async def test_arange(self):
		await self.store.aset_many({"a": 1, "b": 2, "c": 3})
		result = await self.store.arange("a", "c")
		assert list(result.keys()) == ["a", "b"]

	async def test_apop(self):
		await self.store.aset("k", "v")
		val = await self.store.apop("k")
		assert val == "v"

	async def test_apopitem(self):
		await self.store.aset("a", 1)
		key, val = await self.store.apopitem()
		assert key == "a" and val == 1

	async def test_asetdefault(self):
		val = await self.store.asetdefault("k", 42)
		assert val == 42

	async def test_acontains(self):
		await self.store.aset("k", "v")
		assert await self.store.acontains("k") is True
		assert await self.store.acontains("x") is False

	async def test_aiter(self):
		await self.store.aset_many({"a": 1, "b": 2})
		keys = [k async for k in self.store]
		assert keys == ["a", "b"]

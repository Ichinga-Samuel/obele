"""Comprehensive test suite for obele.kv (sync API).

Uses an in-memory SQLite database for every test to ensure isolation.
"""
import pytest

from obele import Database, KVStore, KV


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def setup_db():
    """Create a fresh in-memory database for each test."""
    Database.configure(":memory:")
    yield
    KV.reset()
    Database.close()


def _make_store(**kwargs) -> KVStore:
    """Create a KVStore (table auto-created in __init__)."""
    return KVStore(**kwargs)


# ---------------------------------------------------------------------------
# Table Management
# ---------------------------------------------------------------------------

class TestTableManagement:
    def test_auto_creates_table(self):
        store = _make_store(table_name="t1")
        cursor = Database.execute_read(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='t1'"
        )
        assert cursor.fetchone() is not None

    def test_drop_table(self):
        store = _make_store(table_name="t2")
        store.drop_table()
        cursor = Database.execute_read(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='t2'"
        )
        assert cursor.fetchone() is None

    def test_recreate_after_drop(self):
        store = _make_store(table_name="t3")
        store.drop_table()
        store.create_table()
        cursor = Database.execute_read(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='t3'"
        )
        assert cursor.fetchone() is not None

    def test_invalid_table_name_raises(self):
        with pytest.raises(ValueError, match="identifier"):
            KVStore(table_name="bad table!")


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------

class TestBasicCRUD:
    def test_set_and_get(self):
        store = _make_store()
        store["hello"] = "world"
        assert store["hello"] == "world"

    def test_overwrite(self):
        store = _make_store()
        store["key"] = 1
        store["key"] = 2
        assert store["key"] == 2

    def test_delete(self):
        store = _make_store()
        store["key"] = "value"
        del store["key"]
        assert "key" not in store

    def test_delete_missing_raises(self):
        store = _make_store()
        with pytest.raises(KeyError):
            del store["nope"]

    def test_getitem_missing_raises(self):
        store = _make_store()
        with pytest.raises(KeyError):
            _ = store["nope"]

    def test_contains(self):
        store = _make_store()
        store["x"] = 1
        assert "x" in store
        assert "y" not in store

    def test_len(self):
        store = _make_store()
        assert len(store) == 0
        store["a"] = 1
        store["b"] = 2
        assert len(store) == 2

    def test_bool_empty(self):
        store = _make_store()
        assert not store

    def test_bool_non_empty(self):
        store = _make_store()
        store["x"] = 1
        assert store

    def test_iter(self):
        store = _make_store()
        store["b"] = 2
        store["a"] = 1
        store["c"] = 3
        assert list(store) == ["a", "b", "c"]

    def test_repr(self):
        store = _make_store(table_name="demo")
        store["a"] = 1
        r = repr(store)
        assert "KVStore" in r
        assert "demo" in r


# ---------------------------------------------------------------------------
# Extended Dict Interface
# ---------------------------------------------------------------------------

class TestDictInterface:
    def test_get_existing(self):
        store = _make_store()
        store["k"] = 42
        assert store.get("k") == 42

    def test_get_missing_default(self):
        store = _make_store()
        assert store.get("missing") is None
        assert store.get("missing", 99) == 99

    def test_pop_existing(self):
        store = _make_store()
        store["k"] = "val"
        result = store.pop("k")
        assert result == "val"
        assert "k" not in store

    def test_pop_missing_default(self):
        store = _make_store()
        assert store.pop("nope", "fallback") == "fallback"

    def test_pop_missing_raises(self):
        store = _make_store()
        with pytest.raises(KeyError):
            store.pop("nope")

    def test_popitem_last(self):
        store = _make_store()
        store["a"] = 1
        store["b"] = 2
        store["c"] = 3
        key, value = store.popitem(last=True)
        assert key == "c"
        assert value == 3
        assert len(store) == 2

    def test_popitem_first(self):
        store = _make_store()
        store["a"] = 1
        store["b"] = 2
        key, value = store.popitem(last=False)
        assert key == "a"
        assert value == 1

    def test_popitem_empty_raises(self):
        store = _make_store()
        with pytest.raises(KeyError, match="empty"):
            store.popitem()

    def test_setdefault_missing(self):
        store = _make_store()
        result = store.setdefault("k", 100)
        assert result == 100
        assert store["k"] == 100

    def test_setdefault_existing(self):
        store = _make_store()
        store["k"] = 1
        result = store.setdefault("k", 999)
        assert result == 1

    def test_update_mapping(self):
        store = _make_store()
        store.update({"a": 1, "b": 2})
        assert store["a"] == 1
        assert store["b"] == 2

    def test_update_kwargs(self):
        store = _make_store()
        store.update(x=10, y=20)
        assert store["x"] == 10
        assert store["y"] == 20

    def test_clear(self):
        store = _make_store()
        store["a"] = 1
        store["b"] = 2
        store.clear()
        assert len(store) == 0

    def test_keys(self):
        store = _make_store()
        store["b"] = 2
        store["a"] = 1
        assert store.keys() == ["a", "b"]

    def test_values(self):
        store = _make_store()
        store["a"] = 10
        store["b"] = 20
        assert store.values() == [10, 20]

    def test_items(self):
        store = _make_store()
        store["a"] = 10
        store["b"] = 20
        assert store.items() == [("a", 10), ("b", 20)]


# ---------------------------------------------------------------------------
# Slicing / Range Queries
# ---------------------------------------------------------------------------

class TestSlicing:
    def test_slice_text_keys(self):
        store = _make_store()
        for c in "abcdef":
            store[c] = c.upper()
        result = store["b":"e"]
        assert result == {"b": "B", "c": "C", "d": "D"}

    def test_slice_open_start(self):
        store = _make_store()
        for c in "abcde":
            store[c] = c
        result = store[:"c"]
        assert set(result.keys()) == {"a", "b"}

    def test_slice_open_end(self):
        store = _make_store()
        for c in "abcde":
            store[c] = c
        result = store["c":]
        assert set(result.keys()) == {"c", "d", "e"}

    def test_slice_open_both(self):
        store = _make_store()
        store["x"] = 1
        store["y"] = 2
        result = store[:]
        assert len(result) == 2

    def test_slice_integer_keys(self):
        store = _make_store(key_type=int)
        for i in range(10):
            store[i] = i * 10
        result = store[3:7]
        assert result == {3: 30, 4: 40, 5: 50, 6: 60}

    def test_range_method(self):
        store = _make_store(key_type=int)
        for i in range(10):
            store[i] = i * 100
        result = store.range(2, 5)
        assert result == {2: 200, 3: 300, 4: 400}

    def test_range_reverse(self):
        store = _make_store(key_type=int)
        for i in range(5):
            store[i] = i
        result = store.range(return_type="tuple", reverse=True)
        keys = [k for k, _ in result]
        assert keys == [4, 3, 2, 1, 0]

    def test_range_step(self):
        store = _make_store(key_type=int)
        for i in range(10):
            store[i] = i
        result = store.range(0, 10, step=3, return_type="tuple")
        keys = [k for k, _ in result]
        assert keys == [0, 3, 6, 9]

    def test_keys_slice(self):
        store = _make_store()
        for c in "abcdef":
            store[c] = c
        assert store.keys_slice("b", "e") == ("b", "c", "d")

    def test_values_slice(self):
        store = _make_store()
        for c in "abcdef":
            store[c] = c.upper()
        assert store.values_slice("b", "e") == ("B", "C", "D")

    def test_slice_disabled_enforce_raises(self):
        store = _make_store(enforce_key_type=False)
        store["x"] = 1
        with pytest.raises(TypeError, match="enforce_key_type"):
            store["a":"z"]


# ---------------------------------------------------------------------------
# Multi-Key Queries
# ---------------------------------------------------------------------------

class TestMultiKey:
    def test_get_many_dict(self):
        store = _make_store()
        store["a"] = 1
        store["b"] = 2
        store["c"] = 3
        result = store.get_many("a", "c")
        assert result == {"a": 1, "c": 3}

    def test_get_many_tuple(self):
        store = _make_store()
        store["a"] = 1
        store["b"] = 2
        result = store.get_many("a", "b", return_type="tuple")
        assert result == (("a", 1), ("b", 2))

    def test_get_many_skip_missing(self):
        store = _make_store()
        store["a"] = 1
        result = store.get_many("a", "missing", skip_missing=True)
        assert result == {"a": 1}

    def test_get_many_default(self):
        store = _make_store()
        store["a"] = 1
        result = store.get_many("a", "missing", default=None)
        assert result == {"a": 1, "missing": None}

    def test_get_many_missing_raises(self):
        store = _make_store()
        store["a"] = 1
        with pytest.raises(KeyError):
            store.get_many("a", "missing")

    def test_get_many_empty(self):
        store = _make_store()
        assert store.get_many() == {}


# ---------------------------------------------------------------------------
# Batch Operations
# ---------------------------------------------------------------------------

class TestBatchOps:
    def test_set_many_dict(self):
        store = _make_store()
        store.set_many({"x": 10, "y": 20, "z": 30})
        assert len(store) == 3
        assert store["y"] == 20

    def test_set_many_tuples(self):
        store = _make_store()
        store.set_many([("a", 1), ("b", 2)])
        assert store["a"] == 1
        assert store["b"] == 2

    def test_set_many_overwrites(self):
        store = _make_store()
        store["x"] = 1
        store.set_many({"x": 99})
        assert store["x"] == 99

    def test_set_many_empty(self):
        store = _make_store()
        store.set_many({})  # should not raise
        assert len(store) == 0

    def test_delete_many(self):
        store = _make_store()
        store.set_many({"a": 1, "b": 2, "c": 3, "d": 4})
        deleted = store.delete_many(["a", "c"])
        assert deleted == 2
        assert len(store) == 2
        assert "a" not in store

    def test_delete_many_partial(self):
        store = _make_store()
        store["a"] = 1
        deleted = store.delete_many(["a", "missing"])
        assert deleted == 1


# ---------------------------------------------------------------------------
# Key Type Enforcement
# ---------------------------------------------------------------------------

class TestKeyTypeEnforcement:
    def test_auto_detect_key_type(self):
        store = _make_store()
        store["hello"] = 1
        assert store.key_type is str

    def test_declared_key_type(self):
        store = _make_store(key_type=int)
        store[1] = "one"
        assert store.key_type is int

    def test_mixed_key_type_raises(self):
        store = _make_store()
        store["hello"] = 1
        with pytest.raises(TypeError, match="str"):
            store[42] = 2

    def test_enforcement_disabled(self):
        store = _make_store(enforce_key_type=False)
        store["hello"] = 1
        store[42] = 2
        assert store["hello"] == 1
        assert store[42] == 2

    def test_invalid_key_type_raises(self):
        with pytest.raises(TypeError, match="only supports"):
            KVStore(key_type=list, enforce_key_type=True)

    def test_bytes_key(self):
        store = _make_store(key_type=bytes)
        store[b"bin"] = "binary_key"
        assert store[b"bin"] == "binary_key"

    def test_float_key(self):
        store = _make_store(key_type=float)
        store[1.5] = "float_key"
        store[2.5] = "another"
        assert store[1.5] == "float_key"


# ---------------------------------------------------------------------------
# Serializer Modes
# ---------------------------------------------------------------------------

class TestSerializers:
    def test_auto_json_roundtrip(self):
        store = _make_store(serializer="auto")
        store["info"] = {"name": "Alice", "score": 95}
        assert store["info"] == {"name": "Alice", "score": 95}

    def test_auto_pickle_fallback(self):
        """Auto mode falls back to pickle for non-JSON-safe values."""
        store = _make_store(serializer="auto")
        store["data"] = {1, 2, 3}  # sets aren't JSON serializable
        assert store["data"] == {1, 2, 3}

    def test_json_serializer(self):
        store = _make_store(serializer="json")
        store["info"] = {"name": "Alice", "score": 95}
        assert store["info"] == {"name": "Alice", "score": 95}

    def test_json_rejects_non_serializable(self):
        store = _make_store(serializer="json")
        with pytest.raises(TypeError):
            store["data"] = {1, 2, 3}

    def test_pickle_serializer(self):
        store = _make_store(serializer="pickle")
        store["data"] = {"nested": [1, 2, {"deep": True}]}
        assert store["data"] == {"nested": [1, 2, {"deep": True}]}

    def test_custom_serializer(self):
        import json as _json

        custom_dumps = lambda v: _json.dumps(v, sort_keys=True).encode()
        custom_loads = lambda b: _json.loads(b)

        store = _make_store(serializer=(custom_dumps, custom_loads))
        store["x"] = {"b": 2, "a": 1}
        assert store["x"] == {"a": 1, "b": 2}

    def test_invalid_serializer_raises(self):
        with pytest.raises(ValueError, match="serializer"):
            KVStore(serializer="msgpack")


# ---------------------------------------------------------------------------
# KV Singleton
# ---------------------------------------------------------------------------

class TestKVSingleton:
    def test_first_call_creates(self):
        kv = KV("singleton_test")
        kv["key"] = "value"
        assert kv["key"] == "value"

    def test_subsequent_calls_same_instance(self):
        kv1 = KV("first_table")
        kv1["x"] = 1

        kv2 = KV("IGNORED_table")
        assert kv1 is kv2
        assert kv2["x"] == 1

    def test_reset_allows_new_instance(self):
        kv1 = KV("table_a")
        kv1["a"] = 1

        KV.reset()

        kv2 = KV("table_b")
        assert kv1 is not kv2
        assert "a" not in kv2

    def test_singleton_inherits_kvstore_api(self):
        kv = KV("api_test")
        kv.update({"a": 1, "b": 2, "c": 3})
        assert kv.keys() == ["a", "b", "c"]
        assert kv.get_many("a", "c") == {"a": 1, "c": 3}

    def test_repr(self):
        kv = KV("repr_test")
        r = repr(kv)
        assert "KV" in r
        assert "repr_test" in r

    def test_init_args_used_first_time(self):
        kv = KV("typed_kv", key_type=int)
        kv[1] = "one"
        assert kv[1] == "one"
        with pytest.raises(TypeError):
            kv["string_key"] = "nope"

    def test_init_args_ignored_after_first(self):
        kv1 = KV("first", key_type=str)
        kv1["hello"] = 1
        # Second call with different key_type â€” should be ignored
        kv2 = KV("second", key_type=int)
        assert kv1 is kv2
        assert kv2.key_type is str


# ---------------------------------------------------------------------------
# Complex Value Types
# ---------------------------------------------------------------------------

class TestComplexValues:
    def test_none_value(self):
        store = _make_store()
        store["k"] = None
        assert store["k"] is None

    def test_bytes_value(self):
        store = _make_store()
        store["bin"] = b"\x00\x01\x02"
        assert store["bin"] == b"\x00\x01\x02"

    def test_list_value(self):
        store = _make_store()
        store["nums"] = [1, 2, 3, 4, 5]
        assert store["nums"] == [1, 2, 3, 4, 5]

    def test_tuple_value(self):
        store = _make_store(serializer="pickle")
        store["pair"] = (10, 20)
        assert store["pair"] == (10, 20)

    def test_nested_dict(self):
        store = _make_store()
        data = {"users": [{"name": "Alice"}, {"name": "Bob"}], "count": 2}
        store["d"] = data
        assert store["d"] == data

    def test_set_value(self):
        store = _make_store(serializer="auto")
        store["s"] = {1, 2, 3}
        assert store["s"] == {1, 2, 3}


# ---------------------------------------------------------------------------
# Integer Key Store
# ---------------------------------------------------------------------------

class TestIntegerKeyStore:
    def test_sorted_iteration(self):
        store = _make_store(key_type=int)
        for i in [5, 3, 1, 4, 2]:
            store[i] = f"val_{i}"
        assert list(store) == [1, 2, 3, 4, 5]

    def test_range_query(self):
        store = _make_store(key_type=int)
        for i in range(1, 11):
            store[i] = i * 100
        result = store[3:8]
        assert result == {3: 300, 4: 400, 5: 500, 6: 600, 7: 700}


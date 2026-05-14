"""Tests for KVStore: prefix, scan, stats, TTL, atomic ops."""

import pytest
from obele import Database, KVStore


@pytest.fixture
def store():
    kv = KVStore("test_kv_suite", key_type=str)
    yield kv
    kv.clear()


class TestKVBasic:
    def test_set_get(self, store):
        store["key"] = "value"
        assert store["key"] == "value"

    def test_delete(self, store):
        store["key"] = "val"
        del store["key"]
        assert "key" not in store

    def test_len(self, store):
        store["a"] = 1
        store["b"] = 2
        assert len(store) == 2


class TestKVPrefix:
    def test_prefix_scan(self, store):
        store["user:1"] = "Alice"
        store["user:2"] = "Bob"
        store["post:1"] = "Hello"
        result = store.prefix("user:")
        assert len(result) == 2
        assert "user:1" in result

    def test_prefix_keys(self, store):
        store["a:1"] = 1
        store["a:2"] = 2
        store["b:1"] = 3
        keys = store.prefix_keys("a:")
        assert keys == ["a:1", "a:2"]

    def test_prefix_count(self, store):
        store["x:1"] = 1
        store["x:2"] = 2
        store["y:1"] = 3
        assert store.prefix_count("x:") == 2

    def test_prefix_delete(self, store):
        store["d:1"] = 1
        store["d:2"] = 2
        store["e:1"] = 3
        deleted = store.prefix_delete("d:")
        assert deleted == 2
        assert len(store) == 1


class TestKVScan:
    def test_glob_scan(self, store):
        store["user:active:1"] = "A"
        store["user:active:2"] = "B"
        store["user:inactive:1"] = "C"
        result = store.scan("user:active:*")
        assert len(result) == 2

    def test_glob_suffix(self, store):
        store["cache:a"] = 1
        store["cache:b"] = 2
        store["data:c"] = 3
        result = store.scan("cache:*")
        assert len(result) == 2


class TestKVStats:
    def test_stats(self, store):
        store["a"] = 1
        store["b"] = 2
        s = store.stats()
        assert s["total_keys"] == 2
        assert s["active_keys"] == 2
        assert "str" in s["key_format_counts"]


class TestKVAtomicOps:
    def test_increment(self, store):
        store["counter"] = 0
        store.increment("counter", 5)
        assert store["counter"] == 5

    def test_compare_and_swap(self, store):
        store["val"] = 10
        assert store.compare_and_swap("val", 10, 20) is True
        assert store["val"] == 20
        assert store.compare_and_swap("val", 10, 30) is False


class TestKVTTL:
    def test_ttl_set_and_check(self, store):
        store.set("temp", "val", ttl=3600)
        remaining = store.ttl("temp")
        assert remaining is not None
        assert remaining > 3500
